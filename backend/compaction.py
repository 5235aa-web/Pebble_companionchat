"""
compaction.py - Conversation compaction.

Old turns are replaced by a summary while the most recent turns and any
message flagged important are kept verbatim. Summary length is bounded in
tokens and measured with the same counter the chat request uses.
"""
import logging
import re
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime

from .token_engine import acount_messages, atruncate_to_tokens, count_text
from .token_counter import get_context_window
from .llm_client import call_llm_stream
from . import segmented_summary

logger = logging.getLogger(__name__)

# ==================== Compaction config ====================

DEFAULT_PRESERVE_RECENT = 5      # messages kept verbatim after a compaction
MIN_MESSAGES_TO_COMPACT = 10     # floor for the preserve_recent + 5 guard
MAX_SUMMARY_TOKENS = 2000        # hard ceiling for a generated summary
MIN_SUMMARY_TOKENS = 300         # below this the summary is probably degraded
SUMMARY_PREVIEW_LENGTH = 200     # preview length in the history panel
# A part covers one slice of the retirement set; several are merged afterwards.
PART_SUMMARY_CHARS = 1500
# Each part costs one summarizer call, so an enormous branch is consumed oldest
# first up to this many parts; whatever is left stays uncompacted for next run.
MAX_COMPACT_PARTS = 12

ENABLE_IMPORTANT_PRESERVATION = True
MAX_IMPORTANT_MESSAGES = 20

# Markers used to recognise a summary message in stored history
SUMMARY_MARKER_START = "[Conversation Summary"
SUMMARY_MARKER_END = "End of conversation summary"


# ==================== Prompts ====================

COMPACT_PROMPT = """You are summarizing a roleplay conversation so the AI can continue with a smaller context.

The AI character in this conversation is {character_name}. Assistant turns are labelled with that name.

Produce a summary with two parts:

1. **Topic**: what the user and {character_name} have been talking about.
2. **Content summary**: a bullet-point account of what happened, the key information exchanged, how both sides interacted, and how the emotional tone shifted.

Rules:
- Be faithful to what actually happened; never invent details.
- Keep the emotional texture. This is a roleplay, not a transcript.
- Do not drop context that later turns depend on.
- Write in third person and refer to the AI as {character_name}. Never write "the AI", and never invent a different name.
- Spend detail on important or intimate scenes and condense repetitive small talk.
- The summary must stay within {max_tokens} tokens.

Write the summary in English."""

INCREMENTAL_COMPACT_PROMPT_TEMPLATE = """You are updating an existing conversation summary with new turns.

The AI character in this conversation is {character_name}. Assistant turns are labelled with that name.

Existing summary:
{existing_summary}

New turns:
{new_content}

Produce one merged summary that:
1. Keeps everything still relevant from the existing summary.
2. Folds in what matters from the new turns.
3. Reads as a single coherent summary rather than two joined parts.
4. Refers to the AI as {character_name}, never as "the AI".
5. Stays within {max_tokens} tokens.

Write the summary in English."""


# ==================== 数据结构 ====================

@dataclass
class CompactionResult:
    """Outcome of a compaction run."""
    success: bool
    summary: str
    messages_before: int
    messages_after: int
    tokens_before: int
    tokens_after: int
    tokens_saved: int
    messages_compacted: int = 0
    important_preserved: int = 0
    compaction_version: int = 1
    is_incremental: bool = False
    error: Optional[str] = None
    # Exactly what should be written back to the branch when the caller still
    # rebuilds the list. Retained for compatibility; the current write path
    # flags messages instead of deleting them.
    preserved_messages: List[Dict] = field(default_factory=list)
    # Messages the caller must flag as compacted. They stay in the table so the
    # history panel can show them, but no longer reach the model.
    compacted_message_ids: List[int] = field(default_factory=list)
    # Superseded summary rows. Only the newest summary is kept as a chat card,
    # so these are deleted rather than archived.
    replaced_summary_ids: List[int] = field(default_factory=list)


@dataclass
class CompactionConfig:
    """Compaction parameters. Summary size is measured in tokens."""
    preserve_recent: int = DEFAULT_PRESERVE_RECENT
    max_summary_tokens: int = MAX_SUMMARY_TOKENS
    preserve_important: bool = ENABLE_IMPORTANT_PRESERVATION
    max_important_messages: int = MAX_IMPORTANT_MESSAGES


# ==================== 核心压缩函数 ====================

async def compact_conversation(
    messages: List[Dict],
    llm_config: Dict,
    config: Optional[CompactionConfig] = None,
    companion_id: str = "",
    branch_id: str = "main",
    character_name: str = "",
) -> CompactionResult:
    """
    压缩对话历史
    
    Args:
        messages: 消息列表，每条消息应包含 role, content, message_type, metadata, is_important 等字段
        llm_config: LLM配置
        config: 压缩配置
        companion_id: AI伴侣ID（用于记录历史）
        branch_id: 分支ID
        character_name: 角色名，注入提示词并替换 "AI" 标签
    
    Returns:
        CompactionResult: 压缩结果
    """
    if config is None:
        config = CompactionConfig()
    
    # 1. 检查消息数量
    if len(messages) <= config.preserve_recent + 5:
        return CompactionResult(
            success=False,
            summary="",
            messages_before=len(messages),
            messages_after=len(messages),
            tokens_before=0,
            tokens_after=0,
            tokens_saved=0,
            error="消息数太少，无需压缩",
        )
    
    # 2. Measure the branch with the same counter the chat request uses.
    count_kwargs = _count_kwargs(llm_config)
    tokens_before = (await acount_messages(messages, **count_kwargs)).tokens
    
    # 3. Split: summarize vs keep verbatim
    split_compact, to_preserve, important_msgs = _split_messages_for_compaction(
        messages, config
    )

    # The retirement set is summarized whole. An over-long one is split into
    # parts and the digests merged, so no turn is left out and nothing is marked
    # as folded into a summary that never saw it.
    budget = _summary_input_budget(llm_config)
    to_compact, leftover = _limit_retirement_set(split_compact, budget)
    
    if len(to_compact) < 5:
        return CompactionResult(
            success=False,
            summary="",
            messages_before=len(messages),
            messages_after=len(messages),
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            tokens_saved=0,
            error="待压缩消息数太少",
        )
    
    logger.info(
        f"Compacting {len(to_compact)} messages, "
        f"preserving {len(to_preserve)} recent + {len(important_msgs)} important"
    )
    
    # 4. Check whether a summary already exists (incremental path)
    existing_summary = _find_existing_summary(to_compact)
    is_incremental = existing_summary is not None
    
    # 5. Generate the summary
    try:
        summary = await _summarize_retirement_set(
            to_compact, llm_config, config, character_name, existing_summary
        )
        
        if not summary:
            return CompactionResult(
                success=False,
                summary="",
                messages_before=len(messages),
                messages_after=len(messages),
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                tokens_saved=0,
                error="生成摘要失败",
            )
        
        # 6. Bound the summary in tokens
        summary = await _ensure_summary_quality(summary, config.max_summary_tokens, llm_config)
        
        # 7. Build the summary message
        compaction_version = _get_next_compaction_version(messages)
        summary_message = _build_summary_message(
            summary, 
            len(to_compact), 
            compaction_version,
            is_incremental

        )
        
        # A superseded summary is merged into the new one, so its row is dropped
        # to keep exactly one summary card. Legacy archive rows are dropped the
        # same way: they hold summaries, never conversation.
        compacted_ids = [
            msg["id"] for msg in to_compact
            if isinstance(msg.get("id"), int)
            and msg.get("message_type", "chat") != "compaction_summary"
        ]
        replaced_summary_ids = [
            msg["id"] for msg in to_compact
            if isinstance(msg.get("id"), int)
            and msg.get("message_type", "chat") == "compaction_summary"
        ]
        preserved_messages = [summary_message] + important_msgs + to_preserve
        # Turns the budget left out stay in the branch, so they still count
        # toward the post-compaction size.
        remaining_messages = preserved_messages + leftover
        tokens_after = (await acount_messages(remaining_messages, **count_kwargs)).tokens
        tokens_saved = tokens_before - tokens_after
        
        logger.info(
            f"Compaction complete: saved {tokens_saved} tokens, "
            f"version={compaction_version}, incremental={is_incremental}"
        )
        
        return CompactionResult(
            success=True,
            summary=summary,
            messages_before=len(messages),
            messages_after=len(remaining_messages),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tokens_saved=tokens_saved,
            messages_compacted=len(to_compact),
            important_preserved=len(important_msgs),
            compaction_version=compaction_version,
            is_incremental=is_incremental,
            preserved_messages=preserved_messages,
            compacted_message_ids=compacted_ids,
            replaced_summary_ids=replaced_summary_ids,
        )
        
    except Exception as e:
        logger.error(f"Compaction failed: {e}")
        return CompactionResult(
            success=False,
            summary="",
            messages_before=len(messages),
            messages_after=len(messages),
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            tokens_saved=0,
            error=str(e),
        )


# ==================== 消息分割 ====================

def _count_kwargs(llm_config: Optional[Dict]) -> Dict:
    """Map an llm_config dict onto token_engine's provider arguments."""
    cfg = llm_config or {}
    return {
        "provider_type": cfg.get("provider_type") or cfg.get("provider"),
        "model": cfg.get("model") or "gpt-4",
        "base_url": cfg.get("base_url"),
        "api_key": cfg.get("api_key"),
    }


def _split_messages_for_compaction(
    messages: List[Dict],
    config: CompactionConfig,
) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """
    Split into (to_compact, to_preserve, important_msgs).

    A previous summary joins `to_compact` so the next run can merge it instead
    of silently dropping it. Summaries are always compacted, never left in the
    recent tail, otherwise two summaries would coexist.
    """
    preserve_recent = max(1, min(config.preserve_recent, max(1, len(messages) - 1)))

    important_pairs: List[Tuple[int, Dict]] = []
    normal_msgs: List[Dict] = []
    summaries: List[Dict] = []

    recent_start_idx = len(messages) - preserve_recent

    for i, msg in enumerate(messages):
        is_recent = i >= recent_start_idx
        is_important = msg.get("is_important", False)
        msg_type = msg.get("message_type", "chat")

        # Previous summaries feed the incremental merge
        if msg_type == "compaction_summary":
            summaries.append(msg)
            continue

        # 重要消息且不在最近N条中，需要单独保留
        if is_important and not is_recent and config.preserve_important:
            if len(important_pairs) < config.max_important_messages:
                important_pairs.append((i, msg))
                continue

        normal_msgs.append(msg)

    # 重新计算分割点（排除已提取的重要消息）
    if len(normal_msgs) > preserve_recent:
        to_preserve = normal_msgs[-preserve_recent:]
        to_compact = summaries + normal_msgs[:-preserve_recent]
    else:
        to_preserve = normal_msgs
        to_compact = summaries

    # Order by original position: dict equality would give two identical
    # messages the same index and shuffle them.
    important_pairs.sort(key=lambda pair: pair[0])
    important_msgs = [msg for _, msg in important_pairs]

    return to_compact, to_preserve, important_msgs


# ==================== 摘要识别 ====================

def _find_existing_summary(messages: List[Dict]) -> Optional[str]:
    """
    Return the body of the newest previous summary, preferring structured
    metadata over parsing the marker block.

    Scanned newest first: with more than one summary stored, the newest already
    contains everything the older ones carried, so picking the oldest would
    throw away the most recent merge.
    """
    for msg in reversed(messages):
        if msg.get("message_type") == "compaction_summary":
            meta = msg.get("metadata") or {}
            if isinstance(meta, dict) and meta.get("summary"):
                return str(meta["summary"]).strip()
            return _extract_summary_content(msg.get("content", ""))
        
        # 方式2：通过内容标记识别（兼容旧数据）
        content = msg.get("content", "")
        if SUMMARY_MARKER_START in content:
            return _extract_summary_content(content)
    
    return None


def _extract_summary_content(content: str) -> str:
    """
    Pull the summary body out of a stored summary message.
    """
    marker = re.escape(SUMMARY_MARKER_START)
    pattern = marker + r"[^\]]*\]\s*\n\n(.*?)\n\n---"
    match = re.search(pattern, content, re.DOTALL)
    if match:
        return match.group(1).strip()
    
    # Fallback: strip the markers and keep what is left
    content = re.sub(marker + r"[^\]]*\]\s*\n\n", "", content)
    content = re.sub(r"\n\n---\n.*$", "", content, flags=re.DOTALL)
    return content.strip()


def _get_next_compaction_version(messages: List[Dict]) -> int:
    """
    Next version = highest existing version + 1. The counter must not bump
    inside the loop, or v1 jumps straight to v3.
    """
    version = 0
    for msg in messages:
        if msg.get("message_type") == "compaction_summary":
            metadata = msg.get("metadata", {})
            if isinstance(metadata, dict):
                v = metadata.get("compaction_version", 0)
                if isinstance(v, int):
                    version = max(version, v)
    return version + 1


# ==================== 摘要生成 ====================

async def _generate_summary(
    messages: List[Dict], 
    llm_config: Dict,
    config: CompactionConfig,
    character_name: str = "",
) -> str:
    """Ask the LLM for a summary of the turns being retired."""
    name = character_name or "the AI character"
    conversation_text = _format_messages_for_summary(messages, character_name)
    
    request_messages = [
        {"role": "system", "content": COMPACT_PROMPT.format(
            max_tokens=config.max_summary_tokens,
            character_name=name,
        )},
        {"role": "user", "content": f"Summarize the following conversation:\n\n{conversation_text}"}
    ]
    
    summary = await _call_llm_for_summary(request_messages, llm_config)
    return summary


# ---- segmented path ----
# A branch can hold more transcript than one summarizer request can carry, and a
# local model's window is far smaller than a cloud one. Instead of truncating the
# retirement set, it is split and the digests merged: the cost is extra calls, the
# alternative was dropping the oldest turns out of the summary entirely.

COMPACT_PART_PROMPT = """You are summarizing part {index} of {total} of a roleplay
conversation that is too long to summarize in one pass. Summarize only what happens
inside this part.

Keep every fact a continuation needs: who did what, what changed between the
characters, and how things stand at the end of this part.

Rules:
- Write in the same language as the conversation.
- Plain prose: no bullet lists, no headings.
- Under {chars} characters.
- Do not describe anything outside this part and do not invent an ending.
"""

COMPACT_MERGE_PROMPT = """You are merging several part-summaries of one roleplay
conversation into the single summary a continuation will work from.

Rules:
- Write in the same language as the summaries.
- Plain prose: no bullet lists, no headings.
- Merge, do not concatenate: drop repetition, keep every event that matters.
- Keep the through-line of the relationship and how it changed.
- Under {chars} characters in total.
"""


def _row_tokens(text: str) -> int:
    return count_text(text).tokens


def _summary_input_budget(llm_config: Optional[Dict]) -> int:
    """How much rendered transcript one summarizer request may carry.

    Derived from the model window when it is known — the window minus the summary
    we ask for, minus the prompt — never a fixed number. A 128k model lands near
    60k, which is roughly what the old 80k-character cap approximated by hand.
    """
    cfg = llm_config or {}
    model = str(cfg.get("model") or "")
    window = 0
    if model:
        try:
            window = int(get_context_window(model) or 0)
        except Exception:  # noqa: BLE001 - an unknown model must not break compaction
            window = 0
    if window <= 0:
        window = 128_000
    return max(8_000, min(120_000, int(window * 0.6)))


def _limit_retirement_set(
    messages: List[Dict],
    budget: int,
) -> Tuple[List[Dict], List[Dict]]:
    """Cap how much one compaction consumes when the branch is enormous.

    Every part costs one summarizer call, so a branch with thousands of messages
    would turn a single click into dozens of requests. The oldest messages are
    taken up to `budget × MAX_COMPACT_PARTS`; the rest stay uncompacted and are
    picked up on the next run — the same oldest-first order the retired set has
    always been consumed in.
    """
    ceiling = max(1, budget) * MAX_COMPACT_PARTS
    kept: List[Dict] = []
    used = 0
    for msg in messages:
        cost = count_text(str(msg.get("content") or "")).tokens + 24
        if kept and used + cost > ceiling:
            break
        kept.append(msg)
        used += cost
    return kept, messages[len(kept) :]


async def _summarize_part(
    part: str,
    index: int,
    total: int,
    config: CompactionConfig,
    llm_config: Dict,
) -> str:
    request_messages = [
        {
            "role": "system",
            "content": COMPACT_PART_PROMPT.format(
                index=index, total=total, chars=PART_SUMMARY_CHARS
            ),
        },
        {"role": "user", "content": f"Conversation part:\n\n{part}"},
    ]
    return await _call_llm_for_summary(request_messages, llm_config)


async def _merge_part_digests(
    group: List[str],
    config: CompactionConfig,
    llm_config: Dict,
) -> str:
    request_messages = [
        {
            "role": "system",
            "content": COMPACT_MERGE_PROMPT.format(chars=config.max_summary_tokens * 4),
        },
        {"role": "user", "content": "Summaries to merge:\n\n" + "\n\n---\n\n".join(group)},
    ]
    return await _call_llm_for_summary(request_messages, llm_config)


async def _summarize_retirement_set(
    to_compact: List[Dict],
    llm_config: Dict,
    config: CompactionConfig,
    character_name: str,
    existing_summary: Optional[str],
) -> str:
    """Summarize every turn being retired, splitting when one request cannot.

    A set that fits keeps the original single-pass prompts, including the
    incremental path that merges a previous summary. A longer set is digested part
    by part and merged; the previous summary is folded in as one more digest so the
    incremental contract survives the split.
    """
    budget = _summary_input_budget(llm_config)
    rendered = _format_messages_for_summary(to_compact, character_name)
    rows = [row for row in rendered.split("\n\n") if row.strip()]
    parts = segmented_summary.pack_parts(rows, budget, _row_tokens)

    if len(parts) <= 1:
        if existing_summary:
            return await _generate_incremental_summary(
                existing_summary, to_compact, llm_config, config, character_name
            )
        return await _generate_summary(to_compact, llm_config, config, character_name)

    logger.info(
        "Compaction split into %s parts (budget %s tokens, %s messages)",
        len(parts),
        budget,
        len(to_compact),
    )
    digests: List[str] = []
    for index, part in enumerate(parts, 1):
        digest = await _summarize_part(part, index, len(parts), config, llm_config)
        if not digest:
            logger.warning("Compaction part %s/%s came back empty", index, len(parts))
            return ""
        digests.append(digest)

    if existing_summary:
        digests.insert(0, f"[Previous summary]\n{existing_summary}")

    merged = await segmented_summary.merge_digests(
        digests,
        lambda group: _merge_part_digests(list(group), config, llm_config),
        _row_tokens,
        budget,
        config.max_summary_tokens * 4,
    )
    if not merged:
        return ""
    # The merged digests are still slices, not a summary: one more pass produces
    # the single summary the branch will carry.
    return await _generate_summary_from_text(merged, llm_config, config, character_name)


async def _generate_summary_from_text(
    text: str,
    llm_config: Dict,
    config: CompactionConfig,
    character_name: str = "",
) -> str:
    """Final pass over merged slice digests.

    The digests still read as slices even after merging. Without this pass the
    stored summary would be their concatenation, which is not a summary.
    """
    name = character_name or "the AI character"
    request_messages = [
        {
            "role": "system",
            "content": COMPACT_PROMPT.format(
                max_tokens=config.max_summary_tokens, character_name=name
            ),
        },
        {"role": "user", "content": f"Summarize the following conversation:\n\n{text}"},
    ]
    return await _call_llm_for_summary(request_messages, llm_config)


async def _generate_incremental_summary(
    existing_summary: str,
    messages: List[Dict],
    llm_config: Dict,
    config: CompactionConfig,
    character_name: str = "",
) -> str:
    """Merge the existing summary with the turns being retired."""
    name = character_name or "the AI character"
    # Drop any stored summary so it is not fed back twice
    new_messages = [
        msg for msg in messages 
        if msg.get("message_type") != "compaction_summary" 
        and SUMMARY_MARKER_START not in msg.get("content", "")
    ]
    
    new_content = _format_messages_for_summary(new_messages, character_name)
    
    prompt = INCREMENTAL_COMPACT_PROMPT_TEMPLATE.format(
        existing_summary=existing_summary,
        new_content=new_content,
        max_tokens=config.max_summary_tokens,
        character_name=name,
    )
    
    request_messages = [
        {"role": "system", "content": prompt},
    ]
    
    summary = await _call_llm_for_summary(request_messages, llm_config)
    return summary


async def _call_llm_for_summary(
    request_messages: List[Dict],
    llm_config: Dict,
) -> str:
    """调用 LLM 生成摘要的通用方法"""
    summary = ""
    
    async for chunk in call_llm_stream(
        provider_type=llm_config.get("provider_type", "openai"),
        api_key=llm_config.get("api_key", ""),
        base_url=llm_config.get("base_url", ""),
        model=llm_config.get("model", "gpt-4"),
        messages=request_messages,
        temperature=0.3,
    ):
        if chunk.get("type") == "content":
            summary += chunk.get("content", "")
        elif chunk.get("type") == "error":
            raise Exception(f"LLM error: {chunk.get('message', 'Unknown error')}")
    
    return summary.strip()


# ==================== 摘要质量保障 ====================

async def _ensure_summary_quality(
    summary: str,
    max_tokens: int,
    llm_config: Optional[Dict] = None,
) -> str:
    """
    Bound the summary by the same counter that measures the chat request.
    Truncation happens on a token boundary, then the tail is closed so the
    summary does not end mid-sentence.
    """
    if not summary:
        return summary
    
    count_kwargs = _count_kwargs(llm_config)
    measured = (await acount_messages(
        [{"role": "assistant", "content": summary}], **count_kwargs
    )).tokens
    
    if measured > max_tokens:
        summary = await atruncate_to_tokens(summary, max_tokens, **count_kwargs)
        # Close the cut so the stored summary does not stop mid-thought.
        for mark in (". ", "! ", "? ", "\n"):
            idx = summary.rfind(mark)
            if idx > len(summary) * 0.7:
                summary = summary[: idx + len(mark)].rstrip()
                break
        else:
            summary = summary.rstrip() + " ..."
    
    if measured < MIN_SUMMARY_TOKENS:
        logger.warning(
            "Summary shorter than expected (%s tokens, floor %s)", measured, MIN_SUMMARY_TOKENS
        )
    
    return summary


# ==================== 摘要消息构建 ====================

def _build_summary_message(
    summary: str,
    messages_compacted: int,
    compaction_version: int,
    is_incremental: bool,
) -> Dict:
    """
    Build the stored summary message: marker block, body, closing marker.

    The body is English and sits between SUMMARY_MARKER_START and
    SUMMARY_MARKER_END so later turns can recognise and reuse it.
    """
    version_label = f" v{compaction_version}" if compaction_version > 1 else ""
    incremental_label = " (updated)" if is_incremental else ""
    
    content = f"""{SUMMARY_MARKER_START}{version_label}]{incremental_label}

{summary}

---
{SUMMARY_MARKER_END}: {messages_compacted} messages folded into this summary. The conversation continues from here."""
    
    # 构建元数据
    metadata = {
        "compaction_version": compaction_version,
        "messages_compacted": messages_compacted,
        "compacted_at": datetime.now().isoformat(),
        "is_incremental": is_incremental,
        "summary_length": len(summary),
        # The raw body, so the UI never has to parse the marker block back out.
        "summary": summary,
    }
    
    return {
        "role": "system",
        "content": content,
        "message_type": "compaction_summary",
        "metadata": metadata,
        "is_important": False,
    }


# ==================== 消息格式化 ====================

def _format_messages_for_summary(messages: List[Dict], character_name: str = "") -> str:
    """
    Render the retired turns as plain text for the summarizer prompt.

    Assistant turns are labelled with the character's actual name, so the
    summary can refer to them by name instead of guessing who "AI" is.
    A previous summary is reproduced in full rather than previewed: a truncated
    summary would lose exactly the detail the merge is supposed to carry over.

    The caller trims the input to the character budget before rendering, so
    everything passed here reaches the summarizer: the turns flagged as folded
    and the turns that were summarized are the same set.
    """
    ai_label = character_name or "AI"
    rendered: List[str] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        msg_type = msg.get("message_type", "chat")

        if not content:
            continue

        if msg_type == "compaction_summary" or SUMMARY_MARKER_START in content:
            summary_content = _extract_summary_content(content) if SUMMARY_MARKER_START in content else content
            rendered.append(f"[Previous summary]\n{summary_content}")
            continue

        if role == "user":
            rendered.append(f"User: {content}")
        elif role == "assistant":
            rendered.append(f"{ai_label}: {content}")
        elif role == "system":
            if len(content) < 100:
                rendered.append(f"[System instruction: {content}]")
            else:
                preview = content[:100] + "..."
                rendered.append(f"[System instruction: {preview}]")

    return "\n\n".join(rendered)


# ==================== 工具函数 ====================

def get_summary_preview(summary: str, length: int = SUMMARY_PREVIEW_LENGTH) -> str:
    """获取摘要预览"""
    if len(summary) <= length:
        return summary
    return summary[:length] + "..."


def is_summary_message(message: Dict) -> bool:
    """判断是否为摘要消息"""
    if message.get("message_type") == "compaction_summary":
        return True
    if SUMMARY_MARKER_START in message.get("content", ""):
        return True
    return False

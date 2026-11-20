"""
memory_engine.py - 角色记忆的提取与注入

两件事：

1. 提取：把一段还没扫描过的对话交给模型，让它挑出值得长期记住的内容，
   以 JSON 数组返回。写进 companion_memories。
2. 注入：在构建系统提示词时，按关键词挑出与当前话题相关的几条记忆，
   拼成一段 [Memories] 文本。

角色与分支是硬边界，两者都体现在每一次查询的 WHERE 里。
"""
import asyncio
import json
import logging
import re
from typing import Dict, List, Optional

from . import memory_db, memory_retriever
from .llm_client import call_llm_stream

logger = logging.getLogger(__name__)

# 每累积这么多条新消息触发一次提取。太小会浪费 LLM 调用，太大会漏掉细节。
EXTRACT_EVERY = 10

# 一次提取最多喂进去多少条消息，避免长对话把 prompt 撑爆。
EXTRACT_WINDOW = 40

# 一次后台任务最多处理几批。积压很多时分批跑，剩下的留给下一次触发。
MAX_BATCHES = 5

# 检索时只看最近这么多字符的对话。整段长回复拼起来有几千字，任何关键词都能
# 撞上，检索就退化成"全部命中"。
PROBE_CHARS = 600

# 同一角色+分支同时只允许一个提取任务在跑，否则两批重叠的对话会被重复记录。
_running: set = set()

# 后台任务的强引用。asyncio 只持弱引用，不保存的话任务可能跑到一半被 GC 回收。
_tasks: set = set()


# 从角色卡的 user_info 里认出用户叫什么。
#
# Terminal punctuation, half- and full-width. A name ends at any of these; leaving
# the ASCII period out let `"Name: Marie. She lives upstairs."` parse as a name
# with a whole sentence in it. Hyphens and apostrophes stay allowed: Jean-Luc and
# O'Brien are names.
_NAME_STOP = r"([^\n,，.。;；:：!！?？、]{1,24})"

# 这个字段是自由文本，用户可能写 "Name: Alex"、"Alex, 25, likes tea"，也可能
# 只写一段描述。猜错名字比不猜更糟——记忆会把人记成另一个称呼——所以只认
# 明确的写法，其余一律回退到 "the user"。
_NAME_PATTERNS = (
    r"(?:^|\n)\s*(?:name|full name|名字|姓名|用户名|用户名字)\s*[:：]\s*" + _NAME_STOP,
    # "The user's name is Alex", "Your name is Alex", "用户的名字是阿杰"
    r"(?:^|\n)\s*(?:the\s+)?(?:user'?s?\s+name\s+is|user\s+is\s+named|user\s+is\s+called"
    r"|your\s+name\s+is|you\s+are\s+called"
    r"|用户的名字是|用户名字是|用户名叫|用户名是|用户叫|你叫)\s*[:：]?\s*" + _NAME_STOP,
    r"(?:^|\n)\s*(?:you are|you're|你是|用户是|对方是|他叫|她叫)\s+" + _NAME_STOP,
)


def resolve_user_name(user_info: str) -> str:
    """从 user_info 里取出用户的名字，取不到就用 "the user"。"""
    text = (user_info or "").strip()
    if not text:
        return "the user"

    for pattern in _NAME_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            # Strip the sentence-final period so "Alex." does not become a name
            # with a dot in it. A title like "Mr. Smith" loses the dot too, but
            # that is rare enough not to complicate the rule for.
            candidate = match.group(1).strip().strip("'\"").rstrip(".").strip()
            if candidate:
                return candidate

    # A single short line with no punctuation and at most two words reads as a
    # name on its own ("Alex", "Li Lei"). Anything longer is a description, and
    # guessing from it is a coin flip — "A quiet person." is not a name.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if (
        len(lines) == 1
        and len(lines[0]) <= 20
        and not re.search(r"[.,，。;；:：!?！？]", lines[0])
        and len(lines[0].split()) <= 2
    ):
        return lines[0]

    return "the user"


EXTRACT_PROMPT = """You are maintaining the long-term memory of a roleplay character.

Read the conversation below and extract ONLY things worth remembering long after
this scene ends. The character is __CHARACTER_NAME__. The person they are talking
to is __USER_NAME__ — refer to them by that name, never as "the user".

Record these kinds:
- fact: something about the character the card does NOT already cover — a new detail that surfaced here, not their established background
- event: something that happened and changed the state of things
- preference: what the character likes or dislikes
- relationship: how the character and __USER_NAME__ relate now (promises, trust, conflict)

Do NOT record: greetings, small talk, transient moods, anything already stated
in the character card, or anything the character would not remember. Their
background, habits, fears and tastes are usually on the card already — restating
them adds nothing.

Rules:
- One sentence per memory, third person, self-contained. Never use pronouns whose
  referent lives only in the conversation. Name __CHARACTER_NAME__ and __USER_NAME__
  explicitly instead of "she" and "he".
- Give each memory 4-6 concise trigger keywords/phrases in `keys`: include core
  terms, colloquial synonyms, and bilingual (Chinese + English) equivalents so
  future messages match even with different wording.
- importance: 1 = nice to know, 2 = matters, 3 = central to this conversation.
- scope: "branch" (default) for events, promises, and relationship shifts in this
  timeline; "character" ONLY for enduring personal traits/tastes/habits of
  __CHARACTER_NAME__ that hold across every timeline.
- Do NOT repeat anything already covered in "Already remembered", even if
  phrased differently or in another language.
- supersedes: if this memory updates or contradicts an older `branch` memory
  that is no longer true — the relationship changed, a promise was broken, a
  state ended — put that old memory's numeric id (or opening words) here so it
  is retired. Omit the field otherwise. Never supersede `character` memories.
- If nothing deserves remembering, return an empty array [].

Already remembered:
__EXISTING__

Conversation:
__CONVERSATION__

Return ONLY a JSON array, no prose:
[{"content": "...", "kind": "fact", "scope": "branch", "keys": ["...", "..."], "importance": 2, "supersedes": "..."}]
"""


def _format_conversation(messages: List[Dict], character_name: str, user_name: str = "User") -> str:
    lines = []
    for msg in messages:
        role = msg.get("role", "user")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if role == "assistant":
            label = character_name or "Character"
        elif role == "user":
            # The name, not "User": the transcript is what the model reads the
            # relationship out of, and a generic label bleeds into the memories.
            label = user_name or "User"
        else:
            label = "Narration"
        lines.append(f"{label}: {content}")
    return "\n".join(lines)


def _parse_json_array(raw: str) -> Optional[List[Dict]]:
    """从模型输出里抠出 JSON 数组。

    区分 `[]`（模型明确认为无需记录，游标应推进）和 `None`（输出损坏或非
    JSON，游标保持不动以便下次重试）。
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None

    try:
        parsed = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None

    if not isinstance(parsed, list):
        return None
    return [item for item in parsed if isinstance(item, dict)]


def _select_existing_for_prompt(existing: List[Dict], conversation: str, limit: int = 45) -> List[Dict]:
    """Pick the most relevant + newest existing memories to show the extractor."""
    if len(existing) <= limit:
        return existing

    relevant = memory_retriever.rank_memories(
        conversation,
        existing,
        limit=25,
        max_required=0,
        min_score=0.5,
        relative_ratio=0.0,
    )
    picked: List[Dict] = []
    seen = set()
    for mem in relevant:
        mid = int(mem["id"])
        if mid not in seen:
            seen.add(mid)
            picked.append(mem)
    for mem in existing:
        if len(picked) >= limit:
            break
        mid = int(mem["id"])
        if mid not in seen:
            seen.add(mid)
            picked.append(mem)
    return picked


def _filter_dedup_items(items: List[Dict], existing: List[Dict]) -> List[Dict]:
    """Drop exact and near-duplicate entries before persisting."""
    retired_ids = set()
    branch_rows = [m for m in existing if m.get("scope") == "branch"]
    for item in items:
        target = str(item.get("supersedes") or "").strip()
        if not target:
            continue
        id_cand = target.strip("[]# ")
        if id_cand.isdigit():
            tid = int(id_cand)
            if any(int(r["id"]) == tid for r in branch_rows):
                retired_ids.add(tid)
        elif len(target) >= 4:
            low = target.lower()
            for r in branch_rows:
                if str(r.get("content") or "").lower().startswith(low):
                    retired_ids.add(int(r["id"]))

    pool = [
        str(m.get("content") or "")
        for m in existing
        if int(m["id"]) not in retired_ids and str(m.get("content") or "").strip()
    ]
    exact_set = {c.lower() for c in pool}

    accepted: List[Dict] = []
    for item in items:
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        if content.lower() in exact_set:
            continue
        if memory_retriever.is_near_duplicate(content, pool):
            continue
        accepted.append(item)
        pool.append(content)
        exact_set.add(content.lower())
    return accepted


async def extract_memories(
    companion_id: str,
    branch_id: str,
    history: List[Dict],
    llm_config: Dict,
    character_name: str = "",
    user_info: str = "",
) -> int:
    """从游标之后的对话里提取记忆，返回写入条数。

    调用方可以放心地重复调用：游标没推进到 EXTRACT_EVERY 条以上时直接返回 0，
    同一角色+分支已有任务在跑时也直接返回 0。

    积压可能远超一个窗口，所以分批处理而不是只取最后一批。只取最后一批会把
    中间的消息连同游标一起跳过，那些对话就再也不会有记忆，而且不会有任何报错。
    单次任务最多处理 MAX_BATCHES 批，剩下的留给下一次——一次跑太久会一直占着
    模型不放。
    """
    key = f"{companion_id}\u0000{branch_id}"
    if key in _running:
        return 0

    cursor = memory_db.get_cursor(companion_id, branch_id)
    pending = [m for m in history if isinstance(m.get("id"), int) and m["id"] > cursor]
    if len(pending) < EXTRACT_EVERY:
        return 0

    user_name = resolve_user_name(user_info)
    total = 0
    _running.add(key)
    try:
        for _ in range(MAX_BATCHES):
            window = pending[:EXTRACT_WINDOW]
            pending = pending[EXTRACT_WINDOW:]
            if not window:
                break
            written = await _extract_batch(
                companion_id, branch_id, window, llm_config, character_name, user_name
            )
            if written is None:
                # The batch failed; leave the cursor where it is so the next run
                # retries it rather than skipping past it.
                break
            total += written
            if not pending:
                break
        return total
    finally:
        _running.discard(key)


async def _extract_batch(
    companion_id: str,
    branch_id: str,
    window: List[Dict],
    llm_config: Dict,
    character_name: str,
    user_name: str,
) -> Optional[int]:
    """处理一个窗口。失败返回 None，成功返回写入条数（可能是 0）。"""
    conversation = _format_conversation(window, character_name, user_name)
    if not conversation.strip():
        return 0

    existing = memory_db.list_memories(companion_id, branch_id)
    selected_existing = _select_existing_for_prompt(existing, conversation)
    existing_text = (
        "\n".join(
            f"- [{int(m['id'])}] ({m.get('scope') or 'branch'}) {m['content']}"
            for m in selected_existing
        )
        or "(none yet)"
    )

    char_label = character_name or "the character"
    prompt = (
        EXTRACT_PROMPT
        .replace("__CHARACTER_NAME__", char_label)
        .replace("__USER_NAME__", user_name)
        .replace("__EXISTING__", existing_text)
        .replace("__CONVERSATION__", conversation)
    )

    try:
        raw = ""
        async for chunk in call_llm_stream(
            provider_type=llm_config.get("provider_type", "openai"),
            api_key=llm_config.get("api_key", ""),
            base_url=llm_config.get("base_url", ""),
            model=llm_config.get("model", "gpt-4"),
            messages=[{"role": "system", "content": prompt}],
            temperature=0.2,
        ):
            if chunk.get("type") == "content":
                raw += chunk.get("content", "")
            elif chunk.get("type") == "error":
                raise RuntimeError(chunk.get("message", "LLM error"))

        items = _parse_json_array(raw)
        if items is None:
            logger.warning(
                "memory extraction returned unusable JSON (companion=%s branch=%s)",
                companion_id, branch_id,
            )
            return None
        source_ids = [m["id"] for m in window if isinstance(m.get("id"), int)]
        if not source_ids:
            return 0

        filtered = _filter_dedup_items(items, existing)
        written = memory_db.add_memories(companion_id, branch_id, filtered, source_ids)

        # Advance even when nothing was extracted: the batch was read, and
        # re-reading it next time would only produce the same empty result.
        memory_db.set_cursor(companion_id, branch_id, source_ids[-1])
        logger.info(
            "memory extraction: %s memories from %s messages (companion=%s branch=%s)",
            written, len(window), companion_id, branch_id,
        )
        return written
    except Exception as exc:  # noqa: BLE001 - extraction is best effort
        logger.warning(
            "memory extraction failed (companion=%s branch=%s): %s",
            companion_id, branch_id, exc,
        )
        return None


def schedule_extraction(
    companion_id: str,
    branch_id: str,
    history: List[Dict],
    llm_config: Dict,
    character_name: str = "",
    user_info: str = "",
) -> None:
    """在后台跑一次提取，不阻塞正在进行的对话。

    没有事件循环时（比如在同步上下文里调用）静默跳过：记忆是增强，不是
    对话的必要条件。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(
        extract_memories(
            companion_id, branch_id, history, llm_config, character_name, user_info
        )
    )
    # Hold a strong reference until it finishes; asyncio keeps only a weak one.
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def build_memory_block(
    companion_id: str,
    branch_id: str,
    history: List[Dict],
    collect: Optional[List[Dict]] = None,
) -> Optional[str]:
    """构建要拼进系统提示词的 [Memories] 段；没有命中时返回 None。

    检索只看最近几条消息：记忆是"这句话让我想起了什么"，看太远反而会把
    无关记忆拉进来。

    *collect* is an out-parameter. When given, the hits that made it into the
    block are appended to it, so the caller can show which memories were in
    play without searching a second time.
    """
    if not history:
        return None

    probe = " ".join(
        (m.get("content") or "") for m in history[-4:] if m.get("role") != "system"
    )
    # Only the tail. A single character reply can run to a thousand characters,
    # so the four-message window is several thousand — long enough that almost
    # any keyword appears somewhere and every memory "matches".
    probe = probe[-PROBE_CHARS:]
    if not probe.strip():
        return None

    hits = memory_db.search_memories(companion_id, branch_id, probe)
    if not hits:
        return None

    lines = "\n".join(f"- {m['content']}" for m in hits)
    memory_db.touch_memories([m["id"] for m in hits])
    if collect is not None:
        collect.extend(hits)
    return (
        "[Memories]\n"
        "Things you remember from earlier with this person. They are your own "
        "recollection, so refer to them naturally when relevant, and never "
        "recite them as a list.\n"
        f"{lines}"
    )

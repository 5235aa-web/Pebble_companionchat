import datetime
import json
import logging
import os
import time
from typing import List, Dict, AsyncGenerator, Optional, Tuple

from .companion_db import (
    get_companion, get_companion_assets, get_branch_hud, update_branch_hud,
    get_branch_variables, set_branch_variables,
    save_message, save_message_with_swipes, get_messages,
    get_last_assistant_message, update_message_content, add_swipe_to_message,
    delete_message, delete_messages_after,
)
from .card_hud import (
    CardHudStreamFilter,
    build_card_hud_prompt,
    extract_var_updates,
    merge_card_hud,
)
from .card_presentation import (
    apply_regex_scripts,
    build_scene_rules_prompt,
    extract_input_regex,
    extract_output_regex,
    extract_prompt_regex,
    normalize_asset_tag_refs,
    normalize_hud_pinned_vars,
    normalize_scene_rules_list,
)
from .llm_client import call_llm_stream, load_default_llm_config, resolve_provider_model_config
from .world_book_engine import get_active_world_book_content
from .compaction import is_summary_message, SUMMARY_MARKER_START
from .memory_engine import build_memory_block, schedule_extraction, resolve_user_name
from .placeholders import substitute
from .global_prompts import load_global_prompts
from .attachment_parser import (
    format_documents_for_prompt,
    format_video_for_prompt,
    resolve_image_path,
)
from .token_engine import clear_recorded_usage, count_text as count_text_tokens
from .script_db import (
    get_session as get_script_session,
    update_session_hud as update_script_session_hud,
    set_scene_done as set_script_scene_done,
    increment_turn as increment_script_turn,
)
from .script_mode import ScriptStreamFilter, merge_hud, SCRIPT_DIRECTION_NOTE

logger = logging.getLogger("companion")

# Providers that have already refused an input_audio block, keyed by
# (provider, model). Nothing is decided up front: the audio goes out with the
# first request, and a refusal teaches us to stop sending it. A whitelist cannot
# work here - the capability belongs to the weights, not to the API vendor, and
# a local llama.cpp can serve a model that hears and one that does not from the
# same endpoint.
_AUDIO_REJECTED: set = set()
_IMAGE_REJECTED: set = set()

# How a provider words the refusal. DeepSeek answers "unknown variant
# `input_audio`, expected one of `text`, `image_url`, `file`"; others say
# "unsupported content type" or "invalid content".
_AUDIO_REJECTION_HINTS = ("input_audio", "audio_url", "input audio")
_IMAGE_REJECTION_HINTS = (
    "image_url",
    "image input",
    "does not support image",
    "vision is not",
    "multimodal",
    "expected a string",
    "content must be a string",
    "invalid content type",
)

# TEMPORARY GUARD - remove once local audio is usable.
#
# A local llama.cpp *can* ingest audio: Gemma 4 12B ships an audio projector,
# b11748 loads it, and the request is accepted. The cost is the problem - with
# audio in the prompt, prefill drops from ~585 to ~93 tokens/s (measured on
# gemma-4-12b-heretic, 3644 text tokens in 6.2 s versus 9070 mixed tokens in
# 64.4 s), so a single voice turn waits about a minute for its first token.
# Until that is addressed, every llama-cpp model is treated as text-only: the
# transcript goes out, the clip does not. Set PEBBLE_LLAMA_AUDIO=1 to restore the
# probe-and-retry path for local models without touching this file.
_LOCAL_AUDIO_DISABLED = os.getenv("PEBBLE_LLAMA_AUDIO", "").strip().lower() not in (
    "1", "true", "yes",
)


def _is_audio_rejection(chunk: Optional[Dict]) -> bool:
    """True when the provider refused the request because of the audio block."""
    if not isinstance(chunk, dict) or chunk.get("type") != "error":
        return False
    text = str(chunk.get("message") or "").lower()
    if not text:
        return False
    if any(hint in text for hint in _AUDIO_REJECTION_HINTS):
        return True
    # "unknown variant" on its own could be about any field, so the word audio
    # has to appear too before this counts as a rejection of the audio block.
    return "audio" in text and any(
        w in text for w in ("unknown variant", "unsupported", "invalid", "not support")
    )


def _strip_audio_parts(messages: List[Dict]) -> List[Dict]:
    """Drop input_audio parts, keeping the transcript beside them."""
    out: List[Dict] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            kept = [p for p in content if p.get("type") != "input_audio"]
            if not kept:
                kept = [{"type": "text", "text": "[语音消息]"}]
            m = {**m, "content": kept}
        out.append(m)
    return out


def _has_image_parts(messages: List[Dict]) -> bool:
    for m in messages:
        content = m.get("content")
        if isinstance(content, list) and any(
            isinstance(p, dict) and p.get("type") == "image_url" for p in content
        ):
            return True
    return False


def _is_image_rejection(chunk: Optional[Dict]) -> bool:
    """True when the provider refused the request because of image_url parts."""
    if not isinstance(chunk, dict) or chunk.get("type") != "error":
        return False
    text = str(chunk.get("message") or "").lower()
    if not text:
        return False
    if any(hint in text for hint in _IMAGE_REJECTION_HINTS):
        return True
    return "image" in text and any(
        w in text for w in ("unknown variant", "unsupported", "invalid", "not support")
    )


def _strip_image_parts(messages: List[Dict]) -> List[Dict]:
    """Drop image_url parts, flattening pure-text parts back to a string."""
    out: List[Dict] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            kept = [
                p for p in content
                if not (isinstance(p, dict) and p.get("type") == "image_url")
            ]
            if all(isinstance(p, dict) and p.get("type") == "text" for p in kept):
                text = "\n\n".join(str(p.get("text") or "") for p in kept if p.get("text")).strip()
                m = {**m, "content": text or "[附件]"}
            elif kept:
                m = {**m, "content": kept}
            else:
                m = {**m, "content": "[图片附件]"}
        out.append(m)
    return out


async def _first_or_none(stream):
    """First item of an async iterator, or None when it is empty."""
    try:
        return await stream.__anext__()
    except StopAsyncIteration:
        return None


def _is_local_base_url(url: Optional[str]) -> bool:
    """base_url 是否指向本机服务（本地 llama.cpp / ollama 等）。

    本机服务不在 llm_providers.json 的模型目录里，按模型名反查 provider 归属必然误判，
    因此这类配置跳过归属纠正，直接采用调用方传入的 provider/base_url/model。
    """
    if not url:
        return False
    lowered = str(url).lower()
    return 'localhost' in lowered or '127.0.0.1' in lowered or '0.0.0.0' in lowered


def _pick_model_sampling(provider: Dict, model_name: str) -> Dict:
    """按当前模型取采样参数。

    本地 llama.cpp provider 每个权重各持一套 modelParams（key 是 MODEL_REGISTRY 的 key，
    当前生效的是 provider.localModelKey）；云端 provider 没有 modelParams，用顶层字段。
    某个字段在模型那一份里缺席时回退顶层，旧配置文件因此照常工作。
    """
    entry = {}
    params = provider.get("modelParams")
    if isinstance(params, dict):
        key = provider.get("localModelKey") if provider.get("provider") == "llama-cpp" else model_name
        if isinstance(key, str) and key:
            candidate = params.get(key)
            if isinstance(candidate, dict):
                entry = candidate

    def pick(model_field: str, provider_field: str):
        # 模型那一份里显式带了该键就以它为准：null 表示不下发该字段、
        # 沿用 llama.cpp 服务端默认（min_p 0.05 / repeat_penalty 1.0）；
        # 只有键完全缺席时才回退顶层，兼容没有 modelParams 的旧配置。
        if model_field in entry:
            return entry[model_field]
        return provider.get(provider_field)

    return {
        "max_tokens": pick("maxTokens", "maxTokens"),
        "max_context": pick("maxContext", "maxContext") or 0,
        "top_k": pick("topK", "topK"),
        "min_p": pick("minP", "minP"),
        "repeat_penalty": pick("repeatPenalty", "repeatPenalty"),
    }


def _in_play_payload(memories: List[Dict]) -> List[Dict]:
    """Shape the injected memories for the rail's "In play" dots.

    One dot per memory, brightness from importance, the first keyword kept so a
    click can jump straight to that row in the panel.
    """
    out: List[Dict] = []
    for m in memories:
        keys = m.get("keys") or []
        try:
            importance = int(m.get("importance", 2))
        except (TypeError, ValueError):
            importance = 2
        out.append({
            "id": m.get("id"),
            "label": (m.get("content") or "")[:60],
            "weight": round(min(3, max(1, importance)) / 3, 2),
            "key": str(keys[0]) if keys else "",
        })
    return out


def _generation_record(
    llm_config: Optional[Dict],
    elapsed_ms: float,
    first_token_ms: Optional[float],
    usage: Optional[Dict],
    text: str,
    status: str = "ok",
) -> Dict:
    """What one LLM call cost, in the shape the message footer renders.

    Token counts come from the provider's own usage chunk, which is requested
    with `stream_options.include_usage`; both cloud providers and llama.cpp send
    one. A compatible endpoint that does not gets a local count of the reply
    text, labelled `estimated` rather than passed off as measured.
    """
    prompt = completion = total = None
    source = "none"
    if isinstance(usage, dict):
        used = usage.get("completion_tokens")
        if isinstance(used, int) and used > 0:
            prompt = usage.get("prompt_tokens")
            completion = used
            total = usage.get("total_tokens")
            if not (isinstance(total, int) and total > 0) and isinstance(prompt, int) and prompt > 0:
                total = prompt + used
            source = "usage"

    config = llm_config or {}
    if source == "none" and text:
        # No usage chunk, which a compatible endpoint is free not to send. Count
        # with the same counter the context estimator uses: for local weights it
        # asks llama.cpp's own /tokenize, so the number is exact when the server
        # is reachable and only degrades to an estimate when it has to fall back.
        try:
            measured = count_text_tokens(
                text,
                config.get("provider_type"),
                config.get("model"),
                config.get("base_url"),
                config.get("api_key"),
            )
            completion = measured.tokens
            total = completion
            # Only llama.cpp's own /tokenize counts as measured: it encodes with
            # the weights' own vocabulary. tiktoken reports `exact` for its own
            # encoding, but that encoding is not this model's, so it stays an
            # estimate in the footer.
            method = str(getattr(measured, "method", ""))
            source = "measured" if method.startswith("llama.cpp:") else "estimated"
        except Exception as exc:  # a counter failure must not cost the reply
            logger.warning("token count failed: %s", exc)

    return {
        "model": str(config.get("model") or ""),
        "provider": str(config.get("provider_type") or ""),
        "elapsed_ms": int(round(elapsed_ms)),
        "ttft_ms": int(round(first_token_ms)) if first_token_ms is not None else None,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "tokens_source": source,
        "status": status,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _merge_generation(previous: Optional[Dict], addition: Dict) -> Dict:
    """Fold a continuation into the record of the swipe it extends.

    One message can be produced by several calls (send, then continue). The
    counters accumulate so the footer reports what the whole message cost, while
    model and timestamp stay those of the latest call.
    """
    if not isinstance(previous, dict) or not previous:
        return addition
    merged = dict(addition)
    for key in ("elapsed_ms", "completion_tokens", "total_tokens"):
        old = previous.get(key)
        new = merged.get(key)
        if isinstance(old, int) and isinstance(new, int):
            merged[key] = old + new
        elif isinstance(old, int):
            merged[key] = old
    if previous.get("tokens_source") == "estimated":
        merged["tokens_source"] = "estimated"
    # Keep cumulative total_tokens for billing/cost while preserving the latest
    # single-request context size so the message footer and branch meter align
    # after a continuation.
    if (
        addition.get("tokens_source") == "usage"
        and isinstance(addition.get("total_tokens"), int)
        and addition["total_tokens"] > 0
    ):
        merged["context_tokens"] = addition["total_tokens"]
    else:
        merged.pop("context_tokens", None)
    return merged


class Companion:
    """
    完全独立的AI伴侣实例。
    每个实例对应一个companion_id，拥有独立的人格、聊天记录、分支。
    """
    
    def __init__(self, companion_id: str):
        self.id = companion_id
        self._load_from_db()
    
    def _load_from_db(self):
        """从独立数据库加载所有配置。"""
        data = get_companion(self.id)
        if not data:
            raise ValueError(f"Companion not found: {self.id}")
        
        self.name = data['name']
        self.description = data.get('description', '')
        self.personality = data.get('personality', '')
        self.scenario = data.get('scenario', '')
        self.first_mes = data.get('first_mes', '')
        self.mes_example = data.get('mes_example', '')
        self.advanced_prompt = data.get('advanced_prompt', '')
        self.user_info = data.get('user_info', '')
        self.system_prompt = data.get('system_prompt', '')
        self.post_history_instructions = data.get('post_history_instructions', '')
        self.creator_notes = data.get('creator_notes', '')
        self.depth_prompt = data.get('depth_prompt', {})
        self.raw_card_data = data.get('raw_card_data', '')
        self.card_ui = data.get('card_ui', {}) if isinstance(data.get('card_ui'), dict) else {}

    def _scene_rules(self) -> List[Dict]:
        card_ui = getattr(self, "card_ui", {}) or {}
        return normalize_scene_rules_list(card_ui.get("scene_rules"))

    def _asset_names(self) -> List[str]:
        return [
            str(a.get("name") or "").strip()
            for a in get_companion_assets(self.id)
            if str(a.get("name") or "").strip()
        ]

    def _finalize_assistant_output(self, text: str, branch_id: str = "main") -> str:
        """Apply card ``editoutput`` regex rules and high-confidence asset typo fixes."""
        if not text:
            return text
        asset_names = self._asset_names()
        output_rules = extract_output_regex(
            getattr(self, "raw_card_data", ""),
            getattr(self, "card_ui", {}),
        )
        cleaned = text
        if output_rules:
            vars_map = get_branch_variables(self.id, branch_id).get("variables", {})
            cleaned = apply_regex_scripts(
                cleaned,
                output_rules,
                char_name=self.name or "your character",
                user_name=resolve_user_name(self.user_info),
                variables=vars_map,
                seed=f"{self.id}:{branch_id}",
                asset_names=asset_names,
            )
        if asset_names:
            cleaned = normalize_asset_tag_refs(cleaned, asset_names)
        return cleaned
    
    def _depth_prompt_entry(self) -> Optional[Dict]:
        """
        Normalise a depth prompt into {text, role, depth}.

        `depth` counts back from the newest message: 0 means right after the
        last turn, 4 means four turns from the end. `role` decides which side
        the injected message appears on. Returns None when there is no text.
        """
        raw = self.depth_prompt
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return None
        if not isinstance(raw, dict):
            return None
        
        text = (raw.get("prompt") or "").strip()
        if not text:
            return None
        
        role = raw.get("role") or "system"
        if role not in ("system", "user", "assistant"):
            role = "system"
        
        try:
            depth = int(raw.get("depth") or 0)
        except (TypeError, ValueError):
            depth = 0
        
        return {"text": text, "role": role, "depth": max(0, depth)}
    
    def _macro_context(
        self,
        branch_id: str = "main",
        variables: Optional[Dict[str, str]] = None,
        history: Optional[List[Dict]] = None,
    ):
        """Return a function that resolves CBS macros & branch variables for this card.

        Cards are written with macros instead of names, so the same card works
        under any persona. Both the system prompt and the messages inserted into
        the history need the same resolution.
        """
        char_name = self.name or "your character"
        user_name = resolve_user_name(self.user_info)
        vars_map = (
            variables
            if variables is not None
            else get_branch_variables(self.id, branch_id).get("variables", {})
        )
        asset_names = self._asset_names()
        hist = history or []
        last_message_id = max(0, len(hist) - 1)
        last_message = str(hist[-1].get("content") or "") if hist else ""
        last_char_message = ""
        last_user_message = ""
        for item in reversed(hist):
            role = item.get("role")
            if not last_char_message and role == "assistant":
                last_char_message = str(item.get("content") or "")
            if not last_user_message and role == "user":
                last_user_message = str(item.get("content") or "")
            if last_char_message and last_user_message:
                break
        seed = f"{self.id}:{branch_id}"
        idle_minutes, idle_duration = self._compute_idle_metrics(hist)
        hud_state = get_branch_hud(self.id, branch_id).get("hud") or {}
        return lambda value: substitute(
            value,
            char_name,
            user_name,
            variables=vars_map,
            seed=seed,
            asset_names=asset_names,
            chat_index=last_message_id,
            last_message_id=last_message_id,
            last_message=last_message,
            last_char_message=last_char_message,
            last_user_message=last_user_message,
            idle_duration=idle_duration,
            idle_minutes=idle_minutes,
            hud=hud_state,
        )

    @staticmethod
    def _compute_idle_metrics(hist: List[Dict]) -> Tuple[int, str]:
        """Compute elapsed minutes and human-readable duration since the prior turn."""
        if not hist:
            return 0, "0m"
        target_msg = hist[-2] if (len(hist) >= 2 and hist[-1].get("role") == "user") else hist[-1]
        raw_ts = str(target_msg.get("created_at") or "").strip()
        if not raw_ts:
            return 0, "0m"
        parsed: Optional[datetime.datetime] = None
        for candidate in (raw_ts.replace("Z", "+00:00"),):
            try:
                parsed = datetime.datetime.fromisoformat(candidate)
                break
            except ValueError:
                pass
        if parsed is None:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    parsed = datetime.datetime.strptime(raw_ts[:19], fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            return 0, "0m"
        if parsed.tzinfo is None:
            # SQLite CURRENT_TIMESTAMP defaults to UTC.
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        mins = max(0, int((now_utc - parsed).total_seconds() // 60))
        if mins < 60:
            return mins, f"{mins}m"
        hours, rem = divmod(mins, 60)
        if hours < 24:
            return mins, f"{hours}h {rem}m" if rem else f"{hours}h"
        days, rem_h = divmod(hours, 24)
        return mins, f"{days}d {rem_h}h" if rem_h else f"{days}d"

    def build_system_prompt(
        self,
        history: Optional[List[Dict]] = None,
        branch_id: str = "main",
        memory_out: Optional[List[Dict]] = None,
        *,
        include_card_hud: bool = False,
    ) -> str:
        """
        Assemble the character-card system prompt.
        
        Every card field that reaches the model is wrapped in a bracketed
        English tag so the model can tell sections apart. World book content
        already carries its own "[World Info: ...]" tags and is appended as is.

        *memory_out* collects the memories that were injected, for callers that
        want to report which ones were in play.
        """
        history = history or []
        parts: List[str] = []

        # Cards write {{char}}, {{user}}, and CBS conditionals/variables instead
        # of static values. Resolve them here rather than at import so edits and
        # variable toggles take effect on the next turn.
        sub = self._macro_context(branch_id, history=history)

        if self.name:
            parts.append(f"[Character]\nYou are {self.name}.")

        if self.description:
            parts.append(f"[Description]\n{sub(self.description)}")

        if self.personality:
            parts.append(f"[Personality]\n{sub(self.personality)}")

        if self.scenario:
            parts.append(f"[Scenario]\n{sub(self.scenario)}")
        
        try:
            world_book_content = get_active_world_book_content(self.id, history)
            if world_book_content:
                parts.append(world_book_content)
                logger.info(f"Added world book content for companion {self.id}")
        except Exception as e:
            logger.warning(f"Failed to get world book content: {e}")

        # Long-term memories, retrieved by keyword against the recent turns.
        # Separate tag from world info: one is the rules of the world, the other
        # is what this character has lived through.
        try:
            memory_block = build_memory_block(self.id, branch_id, history, memory_out)
            if memory_block:
                parts.append(memory_block)
        except Exception as e:
            logger.warning(f"Failed to get memory block: {e}")
        
        # Rules that belong to the app rather than to this card: "never write my
        # lines" is about how the model talks to the user, not about who the
        # character is. They are appended after the card's own text in the same
        # slot, so a card without them behaves exactly as before.
        global_rules = load_global_prompts()

        if self.mes_example:
            parts.append(f"[Dialogue Examples]\n{sub(self.mes_example)}")

        advanced_parts = [
            text
            for text in (sub(self.advanced_prompt), sub(global_rules["advanced"]))
            if text.strip()
        ]
        if advanced_parts:
            parts.append("[Advanced Prompt]\n" + "\n\n".join(advanced_parts))

        if self.user_info:
            parts.append(f"[User Info]\n{sub(self.user_info)}")

        system_parts = [
            text
            for text in (sub(self.system_prompt), sub(global_rules["system"]))
            if text.strip()
        ]
        if system_parts:
            parts.append("[System Instruction]\n" + "\n\n".join(system_parts))

        scene_sys = build_scene_rules_prompt(self._scene_rules(), "system", sub)
        if scene_sys:
            parts.append("[Scene Rules]\n" + scene_sys)

        # Narrator protocol. Only teach the protocol when this branch actually
        # contains a narration so the model is not primed to invent one.
        has_narration = any(
            (m.get('message_type') == 'narration')
            or str(m.get('content') or '').startswith(('[Narration]', 'Narration:'))
            for m in (history or [])
        )
        if has_narration:
            parts.append(
                'Messages starting with "Narration:" are narration messages, not messages sent by the user '
                'and not the user speaking to you. Narration is a third-person voice-over that describes '
                'the actions, feelings, and inner states of the user or yourself. '
                'Your next reply must be based on that narration message.'
            )

        if include_card_hud:
            hud_state = get_branch_hud(self.id, branch_id)
            assets = get_companion_assets(self.id)
            vars_map = get_branch_variables(self.id, branch_id).get("variables", {})
            parts.append(
                build_card_hud_prompt(
                    hud_state.get("hud"),
                    assets,
                    variables=vars_map,
                    pinned_vars=self._hud_pinned_vars(),
                    thought_enabled=self._hud_thought_enabled(),
                )
            )
        
        # The opening message is deliberately absent. Feeding it to the model as
        # part of the system prompt made it treat the greeting as something to
        # reproduce, so it echoed that line at the head of every reply.
        #
        # The depth prompt is absent here on purpose too: it belongs inside the
        # history at its requested depth, which _build_messages handles.
        
        return "\n\n".join(parts)

    def _hud_pinned_vars(self) -> List[str]:
        card_ui = getattr(self, "card_ui", None) or {}
        return normalize_hud_pinned_vars(card_ui.get("hud_pinned_vars"))

    def _hud_thought_enabled(self) -> bool:
        card_ui = getattr(self, "card_ui", None) or {}
        return bool(card_ui.get("hud_thought_enabled", False))

    def _resolve_card_hud_state(self, branch_id: str) -> Optional[Dict]:
        """Return branch HUD state when single-chat HUD is active for this branch."""
        state = get_branch_hud(self.id, branch_id)
        return state if state.get("effective_enabled") else None

    def _finalize_card_hud(
        self,
        branch_id: str,
        hud_filter: CardHudStreamFilter,
        initial_hud: Dict,
    ) -> Tuple[str, Dict]:
        """Flush a CardHudStreamFilter, persist merged state & pinned vars, and build the SSE event."""
        tail, hud_update, media_cue = hud_filter.finish()
        pinned = self._hud_pinned_vars()
        clean_hud_update, var_updates = extract_var_updates(hud_update, pinned)
        merged_hud = merge_card_hud(
            initial_hud,
            clean_hud_update,
            clear_stale_thought=True,
        )
        if clean_hud_update or merged_hud != (initial_hud or {}):
            update_branch_hud(self.id, branch_id, hud=merged_hud)
        updated_vars: Optional[Dict[str, str]] = None
        if var_updates:
            current_vars = dict(get_branch_variables(self.id, branch_id).get("variables", {}))
            current_vars.update(var_updates)
            saved = set_branch_variables(self.id, branch_id, current_vars)
            updated_vars = saved.get("variables") or current_vars
        event: Dict = {
            "type": "card_hud",
            "companion_id": self.id,
            "branch_id": branch_id,
            "hud": merged_hud,
            "media_cue": media_cue,
        }
        if updated_vars is not None:
            event["variables"] = updated_vars
        return tail, event

    def _build_messages(
        self,
        history: List[Dict],
        branch_id: str = "main",
        memory_out: Optional[List[Dict]] = None,
        *,
        include_card_hud: bool = False,
    ) -> List[Dict]:
        """
        Build the LLM message list: one system prompt plus the branch history.
        Compaction summaries are replayed with the system role so they do not
        occupy an assistant turn.
        """
        messages = [{
            "role": "system",
            "content": self.build_system_prompt(
                history, branch_id, memory_out, include_card_hud=include_card_hud
            ),
        }]
        sub = self._macro_context(branch_id, history=history)
        asset_names = self._asset_names()
        prompt_rules = extract_prompt_regex(
            getattr(self, "raw_card_data", ""),
            getattr(self, "card_ui", {}),
        )
        vars_map = (
            get_branch_variables(self.id, branch_id).get("variables", {})
            if prompt_rules
            else {}
        )
        hud_map = (
            get_branch_hud(self.id, branch_id).get("hud") or {}
            if prompt_rules
            else {}
        )
        char_name = self.name or "your character"
        user_name = resolve_user_name(self.user_info)
        seed = f"{self.id}:{branch_id}"
        
        # A summary covers the earliest part of the conversation, so it belongs
        # right after the system prompt. Compaction appends the summary row, and
        # plain row order would otherwise place it at the very end of the
        # history. Only the newest summary is sent: older ones get merged into
        # it during compaction, so replaying them would duplicate content.
        summaries: List[Dict] = []
        hist_len = len(history)
        for idx, msg in enumerate(history):
            msg_type = msg.get("message_type", "chat")
            content = msg["content"]
            if msg.get("role") == "assistant" and asset_names and msg_type == "chat":
                content = normalize_asset_tag_refs(content, asset_names)
            if prompt_rules and msg_type == "chat" and content:
                content = apply_regex_scripts(
                    content,
                    prompt_rules,
                    char_name=char_name,
                    user_name=user_name,
                    variables=vars_map,
                    seed=seed,
                    asset_names=asset_names,
                    placement=1 if msg.get("role") == "user" else 2,
                    depth=max(0, hist_len - 1 - idx),
                    hud=hud_map,
                )
            
            if msg_type == "compaction_summary" or SUMMARY_MARKER_START in content:
                summaries.append({"role": "system", "content": content})
            elif msg_type == "narration":
                # Authored direction, not a line the user's character spoke.
                # Prefix cleanly without bracketed tags so the model does not
                # echo "[Narration]" tokens while still telling narration apart
                # from user dialogue.
                messages.append({
                    "role": "user",
                    "content": f"Narration: {content}",
                })
            else:
                messages.append({"role": msg["role"], "content": content})
        
        if summaries:
            messages.insert(1, summaries[-1])
        
        depth_entry = self._depth_prompt_entry()
        if depth_entry:
            # Count back from the newest message. Clamped so an oversized depth
            # lands right after the system prompt instead of out of range, while
            # depth 0 appends at the tail.
            index = len(messages) - depth_entry["depth"]
            index = max(1, min(index, len(messages)))
            messages.insert(index, {
                "role": depth_entry["role"],
                "content": f"[Depth Prompt]\n{sub(depth_entry['text'])}",
            })
        
        post_parts = [
            text
            for text in (
                sub(self.post_history_instructions),
                sub(load_global_prompts()["postHistory"]),
                build_scene_rules_prompt(self._scene_rules(), "post_history", sub),
            )
            if text.strip()
        ]
        if post_parts:
            messages.append({
                "role": "system",
                "content": "[Post-History Instructions]\n" + "\n\n".join(post_parts),
            })
        
        return messages

    def script_request_parts(
        self,
        history: List[Dict],
        session_id: int,
    ) -> Tuple[str, List[Dict]]:
        """System prompt and replayed history for a scripted scene.

        Chat and token accounting both call this, so a playthrough is measured
        with exactly the prompt it is sent. The whole playthrough is replayed:
        a scene depends on what actually happened in the ones before it, so the
        chain runs unbroken from the first turn to the last.
        """
        from .script_db import get_session, get_script, list_session_nodes
        from .script_mode import build_script_system_prompt

        session = get_session(session_id)
        if not session:
            raise ValueError(f"Script session not found: {session_id}")
        script = get_script(session["script_id"]) or {"title": "", "premise": ""}
        nodes = list_session_nodes(int(session["id"]))
        index = 0
        node = None
        if nodes:
            index = max(0, min(int(session["current_index"]), len(nodes) - 1))
            node = nodes[index]

        system_prompt = build_script_system_prompt(
            self,
            script,
            node,
            index,
            len(nodes) or 1,
            session.get("hud") or {},
            int(node.get("turns") or 0) if node else 0,
            int(session.get("turns_done") or 0),
        )
        replayed: List[Dict] = []

        # Segment summaries come first: they stand in for the messages that were
        # folded away, in the order they covered the story.
        from .script_db import list_scene_summaries

        for entry in list_scene_summaries(self.id, session["branch_id"]):
            label = entry.get("scene_titles") or ""
            if not label:
                label = f"Scene {entry.get('scene_from')}-{entry.get('scene_to')}"
            replayed.append({
                "role": "user",
                "content": f"[Story So Far · {label}]\n{entry.get('content') or ''}",
            })

        # A narration is a one-shot cue: it shapes the reply that follows and
        # then leaves the context. Replaying old stage directions would make the
        # model read them as part of the conversation.
        body = history[:-1] if history else []
        current = history[-1] if history else None
        for msg in body:
            if msg.get("message_type") in ("compaction_summary", "narration"):
                continue
            content = msg.get("content") or ""
            if not content:
                continue
            replayed.append({"role": msg["role"], "content": content})

        if current:
            content = (current.get("content") or "").strip()
            if content:
                if current.get("message_type") == "narration":
                    replayed.append({
                        "role": "user",
                        "content": f"[Scene Direction]\n{SCRIPT_DIRECTION_NOTE}\n{content}",
                    })
                else:
                    replayed.append(
                        {"role": current.get("role") or "user", "content": content}
                    )
        return system_prompt, replayed

    def _build_script_messages(
        self,
        history: List[Dict],
        session_id: int,
    ) -> List[Dict]:
        system_prompt, replayed = self.script_request_parts(history, session_id)
        return [{"role": "system", "content": system_prompt}] + replayed

    def _inject_audio_into_messages(self, messages: List[Dict], audio_file_path: str) -> List[Dict]:
        """
        将音频以 OpenAI input_audio 格式注入最后一条用户消息。
        保留原有文本，追加音频 content_part。
        """
        import os
        import base64
        
        if not audio_file_path or not os.path.exists(audio_file_path):
            return messages
        
        # 找到最后一条用户消息
        last_user_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                last_user_idx = i
                break
        
        if last_user_idx == -1:
            return messages
        
        # 读取音频文件并编码为 base64
        with open(audio_file_path, "rb") as f:
            audio_bytes = f.read()
        audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
        ext = os.path.splitext(audio_file_path)[1].lower().lstrip('.')
        mime = self._get_audio_mime(ext)
        format = ext if ext in ['wav', 'mp3', 'pcm'] else 'wav'
        
        # 只发音频，不带文字。支持音频的模型应该听原声：转录文本混进来会让它
        # 照文字回答，语气和停顿这些只有声音里才有的信息就白传了。降级路径
        # 用的是一份不含音频的消息副本，所以这里丢掉文本不会让转录消失。
        content_parts = [{
            "type": "input_audio",
            "input_audio": {
                "data": f"data:{mime};base64,{audio_b64}",
                "format": format
            }
        }]
        
        messages[last_user_idx]["content"] = content_parts
        return messages
    
    def _get_audio_mime(self, ext: str) -> str:
        """根据文件扩展名返回音频的 MIME 类型。"""
        mime_map = {
            'mp3': 'audio/mpeg',
            'wav': 'audio/wav',
            'flac': 'audio/flac',
            'm4a': 'audio/mp4',
            'ogg': 'audio/ogg',
            'pcm': 'audio/L16',
        }
        return mime_map.get(ext, 'audio/wav')

    def _compose_user_text(self, user_message: str, attachments: Optional[List[Dict]]) -> str:
        """
        Fold attachment references into the text that gets stored and displayed.

        Both images and documents are referenced by name only. Their payload
        rides along on the live request, so a stored turn stays small and a long
        document is not re-sent on every later turn.
        """
        text = (user_message or "").strip()
        if not attachments:
            return text

        blocks: List[str] = []
        if text:
            blocks.append(text)

        notes = []
        for a in attachments:
            kind = a.get("kind")
            if kind == "image":
                notes.append(f"[图片附件: {a.get('name') or 'image'}]")
            elif kind == "video":
                notes.append(f"[视频附件: {a.get('name') or 'video'}]")
            elif kind == "document":
                notes.append(f"[文档附件: {a.get('name') or 'document'}]")
        if notes:
            blocks.append("\n".join(notes))

        return "\n\n".join(blocks).strip() or "[附件]"

    def _inject_documents_into_messages(
        self,
        messages: List[Dict],
        attachments: Optional[List[Dict]],
    ) -> List[Dict]:
        """
        Append the extracted text of documents to the last user message.

        The text lives only in the live request. The stored message keeps a
        placeholder, so the same file is not replayed on every later turn.
        """
        block = format_documents_for_prompt(attachments or [])
        if not block:
            return messages

        target = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                target = i
                break
        if target < 0:
            return messages

        existing = messages[target].get("content")
        if isinstance(existing, list):
            parts = list(existing)
            parts.append({"type": "text", "text": block})
            messages[target]["content"] = parts
        else:
            messages[target]["content"] = f"{existing}\n\n{block}" if existing else block
        return messages

    def _inject_images_into_messages(
        self,
        messages: List[Dict],
        attachments: Optional[List[Dict]],
    ) -> List[Dict]:
        """
        Attach images to the last user message as image_url parts.

        When that message already carries parts (audio), the images are appended
        to the existing list instead of replacing it.
        """
        import base64

        images = [a for a in (attachments or []) if a.get("kind") == "image"]
        if not images:
            return messages

        target = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                target = i
                break
        if target < 0:
            return messages

        existing = messages[target].get("content")
        parts = list(existing) if isinstance(existing, list) else [{"type": "text", "text": existing or ""}]

        for item in images:
            path = resolve_image_path(item.get("url") or "")
            if not path:
                logger.warning("Image attachment missing on disk: %s", item.get("url"))
                continue
            try:
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError as e:
                logger.warning("Failed to read image attachment %s: %s", path, e)
                continue
            mime = item.get("mime") or "image/png"
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            })

        if len(parts) == 1:
            return messages

        messages[target]["content"] = parts
        return messages

    def _inject_videos_into_messages(
        self,
        messages: List[Dict],
        attachments: Optional[List[Dict]],
    ) -> List[Dict]:
        """
        Attach video summary/transcript text and sampled keyframe image_url parts
        to the last user message for the live request only.
        """
        import base64

        videos = [a for a in (attachments or []) if isinstance(a, dict) and a.get("kind") == "video"]
        if not videos:
            return messages

        target = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                target = i
                break
        if target < 0:
            return messages

        video_text = "\n\n".join(format_video_for_prompt(v) for v in videos)
        existing = messages[target].get("content")
        if isinstance(existing, list):
            parts = list(existing)
            if video_text:
                parts.append({"type": "text", "text": video_text})
        else:
            combined = f"{existing}\n\n{video_text}" if existing else video_text
            parts = [{"type": "text", "text": combined}]

        frame_count = 0
        for v in videos:
            for frame in v.get("frames") or []:
                if not isinstance(frame, dict) or not frame.get("url"):
                    continue
                path = resolve_image_path(str(frame["url"]))
                if not path:
                    continue
                try:
                    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                except OSError as e:
                    logger.warning("Failed to read video frame %s: %s", path, e)
                    continue
                mime = "image/webp" if path.suffix.lower() == ".webp" else "image/jpeg"
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                })
                frame_count += 1

        if frame_count == 0 and not isinstance(existing, list):
            messages[target]["content"] = parts[0]["text"]
        else:
            messages[target]["content"] = parts
        return messages
    
    async def generate_response_stream(
        self,
        user_message: str,
        branch_id: str = "main",
        llm_config: Optional[Dict] = None,
        enable_thinking: bool = False,
        audio_file_path: Optional[str] = None,
        temperature: float = 0.7,
        top_p: float = 0.9,
        attachments: Optional[List[Dict]] = None,
        sender: str = "user",
        is_voice: bool = False,
        script_session_id: Optional[int] = None,
    ) -> AsyncGenerator[Dict, None]:
        """
        独立流式生成回复。
        audio_file_path: 音频文件路径，如提供则将音频以 input_audio 格式注入最后一条用户消息。
        temperature: 控制回复随机性，值越低越确定。
        top_p: 核采样参数，控制词汇选择的多样性。
        sender: "user" 为用户角色发言，"narrator" 为作者旁白；旁白以独立
            message_type 落库，上下文里会以 "Narration: ..." 形式发给模型。
        """
        logger.info(f"generate_response_stream called: id={self.id}, branch={branch_id}, msg_len={len(user_message)}, sender={sender}, audio={audio_file_path is not None}, temp={temperature}, top_p={top_p}")

        # 1. 保存用户消息到独立数据库（文档正文并入文本，图片信息记入 metadata）
        is_narration = (sender or "user").strip().lower() == "narrator"
        effective_user_message = user_message
        if not is_narration and user_message:
            input_rules = extract_input_regex(
                getattr(self, "raw_card_data", ""),
                getattr(self, "card_ui", {}),
            )
            if input_rules:
                vars_map = get_branch_variables(self.id, branch_id).get("variables", {})
                effective_user_message = apply_regex_scripts(
                    user_message,
                    input_rules,
                    char_name=self.name or "your character",
                    user_name=resolve_user_name(self.user_info),
                    variables=vars_map,
                    seed=f"{self.id}:{branch_id}",
                    asset_names=self._asset_names(),
                )
        stored_text = self._compose_user_text(effective_user_message, attachments)
        # 语音消息的转录文本照常落库（AI 的上下文靠它），只额外标一个 voice，
        # 让前端把气泡渲染成「语音消息」而不是把转录文本摊开。
        user_meta: Dict = {}

        # Script mode: resolve the session before anything is written, so both
        # rows carry the scene they belong to and a stale id cannot turn an
        # ordinary chat into a scene.
        scene_index = 0
        if script_session_id:
            sess = get_script_session(int(script_session_id))
            if (
                not sess
                or sess.get("branch_id") != branch_id
                or sess.get("status") != "playing"
            ):
                script_session_id = None
            else:
                scene_index = int(sess.get("current_index") or 0)
                user_meta["scene_index"] = scene_index

        if attachments:
            user_meta["attachments"] = attachments
        if is_voice:
            user_meta["voice"] = True
        user_message_id = save_message(
            self.id,
            branch_id,
            "user",
            stored_text,
            message_type="narration" if is_narration else "chat",
            metadata=user_meta or None,
        )
        clear_recorded_usage(self.id, branch_id)
        logger.info(f"User message saved: id={user_message_id}")
        
        # 2. 获取该分支的完整上下文
        history = get_messages(self.id, branch_id)
        logger.info(f"History loaded: {len(history)} messages")
        
        # 3. 构建消息
        memories: List[Dict] = []
        card_hud_state: Optional[Dict] = None
        if script_session_id:
            # Script mode replaces the card's chat scaffolding wholesale, so no
            # world book, memory, examples or post-history instructions reach
            # the model.
            messages = self._build_script_messages(history, script_session_id)
        else:
            card_hud_state = self._resolve_card_hud_state(branch_id)
            messages = self._build_messages(
                history, branch_id, memories, include_card_hud=bool(card_hud_state)
            )
        logger.info(f"Messages built: {len(messages)} items")
        
        # 3.5 音频的注入挪到 LLM 配置解析之后：能否发送 input_audio 取决于
        # provider，见 4.5。
        
        # 3.55 文档正文只在当前轮发送；落库的是占位符，后续轮次不会重发全文
        if attachments:
            messages = self._inject_documents_into_messages(messages, attachments)

        # 3.6 图片与视频附件以 OpenAI 兼容 parts 注入当前轮用户消息
        if attachments:
            messages = self._inject_images_into_messages(messages, attachments)
            messages = self._inject_videos_into_messages(messages, attachments)
            logger.info(f"Attachments folded into the user message: {len(attachments)}")
        
        # 4. 获取LLM配置
        llm_config = self._resolve_llm_config_full(llm_config)

        if not llm_config:
            yield {"type": "error", "message": "No LLM provider configured"}
            return

        # 最终检查：确保非托管本地提供商有 base_url
        if not llm_config.get("base_url") and llm_config.get("provider_type") != "llama-cpp":
            yield {"type": "error", "message": "LLM Base URL is not configured. Please check your LLM settings."}
            return
        
        logger.info(f"LLM config: provider={llm_config['provider_type']}, model={llm_config['model']}, base_url={llm_config['base_url']}")
        
        # 4.5 音频默认就发：能不能收由 provider 说了算。被拒一次之后记下来，
        # 之后这个 provider/model 直接只发文本，不再浪费一次往返。
        audio_key = (llm_config["provider_type"], llm_config["model"])
        audio_sent = False
        # 不含音频的那一份，降级重试用它。_inject_audio_into_messages 会原地改
        # messages，所以副本必须在这里就留好。
        text_messages = messages
        local_text_only = (
            llm_config["provider_type"] == "llama-cpp" and _LOCAL_AUDIO_DISABLED
        )
        if audio_file_path and local_text_only:
            logger.info(
                "Audio skipped for local model %s/%s: text only until local audio "
                "prefill is fast enough (see PEBBLE_LLAMA_AUDIO)",
                *audio_key,
            )
        elif audio_file_path and audio_key not in _AUDIO_REJECTED:
            messages = self._inject_audio_into_messages(
                [dict(m) for m in messages], audio_file_path
            )
            audio_sent = True
            logger.info("Audio injected into last user message")
        elif audio_file_path:
            logger.info("Audio skipped for %s/%s: refused earlier", *audio_key)

        # 4.6 图片/视频帧能力探测与降级：若当前模型明确不支持 image_url，
        # 自动退回纯文本（保留视频时长与语音转录等文字信息）。
        has_images = _has_image_parts(messages)
        no_image_messages = _strip_image_parts(messages) if has_images else messages
        image_sent = False
        if has_images and audio_key in _IMAGE_REJECTED:
            logger.info("Image parts skipped for %s/%s: refused earlier", *audio_key)
            messages = no_image_messages
        elif has_images:
            image_sent = True

        # 5. 调用独立LLM流式接口
        full_content = ""
        chunk_count = 0
        usage_info: Optional[Dict] = None
        started = time.perf_counter()
        first_token_ms: Optional[float] = None
        call_kwargs = self._build_llm_call_kwargs(
            llm_config, temperature=temperature, top_p=top_p, enable_thinking=enable_thinking
        )
        stream = call_llm_stream(messages=messages, **call_kwargs)
        pending = await _first_or_none(stream)
        # A refusal is a request-level error, so it arrives as the very first
        # chunk, before any content. Retrying therefore cannot duplicate text.
        if audio_sent and _is_audio_rejection(pending):
            logger.info(
                "%s/%s rejected input_audio, retrying with the transcript only", *audio_key
            )
            _AUDIO_REJECTED.add(audio_key)
            messages = text_messages
            no_image_messages = _strip_image_parts(messages) if _has_image_parts(messages) else messages
            started = time.perf_counter()
            stream = call_llm_stream(messages=messages, **call_kwargs)
            pending = await _first_or_none(stream)

        if image_sent and _is_image_rejection(pending):
            logger.info(
                "%s/%s rejected image_url, retrying with text-only context", *audio_key
            )
            _IMAGE_REJECTED.add(audio_key)
            messages = no_image_messages
            started = time.perf_counter()
            stream = call_llm_stream(messages=messages, **call_kwargs)
            pending = await _first_or_none(stream)

        state_filter = ScriptStreamFilter() if script_session_id else None
        hud_filter = CardHudStreamFilter() if (not script_session_id and card_hud_state) else None
        while True:
            if pending is not None:
                chunk, pending = pending, None
            else:
                try:
                    chunk = await stream.__anext__()
                except StopAsyncIteration:
                    break
            chunk_count += 1
            if chunk["type"] == "content":
                if first_token_ms is None:
                    first_token_ms = (time.perf_counter() - started) * 1000
                text = chunk["content"]
                active_filter = state_filter or hud_filter
                if active_filter is not None:
                    text = active_filter.feed(text)
                    if not text:
                        continue
                    chunk = {**chunk, "content": text}
                full_content += text
                yield chunk
            elif chunk["type"] == "thinking":
                yield chunk
            elif chunk["type"] == "usage":
                usage_info = chunk
                yield chunk
            elif chunk["type"] == "error":
                logger.error(f"LLM error: {chunk.get('message')}")
                yield chunk
                return
        
        script_state: Optional[Dict] = None
        card_hud_event: Optional[Dict] = None
        if state_filter is not None:
            tail, hud_update, scene_done = state_filter.finish()
            if tail:
                full_content += tail
                yield {"type": "content", "content": tail}
            session_row = get_script_session(int(script_session_id))
            merged_hud = merge_hud((session_row or {}).get("hud") or {}, hud_update)
            if hud_update:
                update_script_session_hud(int(script_session_id), merged_hud)
            if scene_done:
                set_script_scene_done(int(script_session_id), True)
            # One reply is one exchange. The count resets when the user advances
            # the scene, and it is reported even when the model sent no state
            # block: the pacing guard must not depend on the model behaving.
            turns_done = increment_script_turn(int(script_session_id))
            script_state = {
                "type": "script_state",
                "session_id": int(script_session_id),
                "hud": merged_hud,
                "scene_done": bool(scene_done),
                "turns_done": turns_done,
            }
        elif hud_filter is not None and card_hud_state is not None:
            tail, card_hud_event = self._finalize_card_hud(
                branch_id, hud_filter, card_hud_state.get("hud") or {}
            )
            if tail:
                full_content += tail
                yield {"type": "content", "content": tail}

        logger.info(f"LLM stream complete, received {chunk_count} chunks, content_len={len(full_content)}")
        if full_content:
            full_content = self._finalize_assistant_output(full_content, branch_id)
        generation = _generation_record(
            llm_config,
            (time.perf_counter() - started) * 1000,
            first_token_ms,
            usage_info,
            full_content,
        )
        
        # 6. 保存AI回复到独立数据库（带swipe数据）
        msg_id = None
        if full_content:
            ai_meta: Dict = {"generation": [generation]}
            if script_session_id:
                ai_meta["scene_index"] = scene_index
            msg_id = save_message_with_swipes(
                self.id, branch_id, "assistant", full_content, [full_content], 0,
                metadata=ai_meta,
            )
            logger.info(f"AI response saved, id={msg_id}")

        # Long-term memory, extracted in the background. It re-reads the branch
        # itself and swallows its own errors, so a failure here cannot affect the
        # reply that just finished.
        if full_content and llm_config and not script_session_id:
            schedule_extraction(
                self.id,
                branch_id,
                get_messages(self.id, branch_id),
                llm_config,
                self.name,
                # Lets the extractor name the user the way the card does instead
                # of writing "the user" into every memory.
                self.user_info,
            )

        if script_state is not None:
            yield script_state
        if card_hud_event is not None:
            yield card_hud_event

        yield {"type": "done", "final_content": full_content, "message_id": msg_id, "user_message_id": user_message_id, "memories": _in_play_payload(memories), "generation": generation}

    async def generate_continue_stream(
        self,
        branch_id: str = "main",
        llm_config: Optional[Dict] = None,
        enable_thinking: bool = False,
        temperature: float = 0.7,
        top_p: float = 0.9,
    ) -> AsyncGenerator[Dict, None]:
        """
        续写最后一条AI回复。
        保留完整上下文，追加续写指令让LLM接着上一条回复继续写，然后将新内容追加到原消息。
        """
        logger.info(f"generate_continue_stream called: id={self.id}, branch={branch_id}, temp={temperature}, top_p={top_p}")
        
        # 1. 获取最后一条AI消息
        last_ai_msg = get_last_assistant_message(self.id, branch_id)
        if not last_ai_msg:
            yield {"type": "error", "message": "没有可续写的AI消息"}
            return
        
        last_content = last_ai_msg['content']
        if not last_content or not last_content.strip():
            yield {"type": "error", "message": "最后一条AI消息内容为空，无法续写"}
            return
        
        # 2. 获取完整历史（包含最后一条AI消息）
        history = get_messages(self.id, branch_id)
        
        # 3. 构建消息上下文，LLM看到完整的对话历史（含自己的上一条回复）
        memories: List[Dict] = []
        card_hud_state = self._resolve_card_hud_state(branch_id)
        messages = self._build_messages(
            history, branch_id, memories, include_card_hud=bool(card_hud_state)
        )
        
        # 4. 追加续写指令——告诉LLM接着上一条回复继续写，而不是重新回答
        messages.append({
            "role": "system",
            "content": "【续写指令】请直接续写你上一条回复，从中断处接着往下写。不要重复任何已有内容，不要加开场白或过渡句，直接输出续写部分。"
        })
        
        # 5. 获取LLM配置
        llm_config = self._resolve_llm_config_full(llm_config)
        if not llm_config:
            yield {"type": "error", "message": "No LLM provider configured"}
            return
        
        logger.info(f"Continue: LLM config: provider={llm_config['provider_type']}, model={llm_config['model']}")
        
        # 6. 调用LLM流式生成
        full_content = ""
        chunk_count = 0
        usage_info: Optional[Dict] = None
        started = time.perf_counter()
        first_token_ms: Optional[float] = None
        hud_filter = CardHudStreamFilter() if card_hud_state else None
        async for chunk in call_llm_stream(
            messages=messages,
            **self._build_llm_call_kwargs(
                llm_config, temperature=temperature, top_p=top_p, enable_thinking=enable_thinking
            ),
        ):
            chunk_count += 1
            if chunk["type"] == "content":
                if first_token_ms is None:
                    first_token_ms = (time.perf_counter() - started) * 1000
                text = chunk["content"]
                if hud_filter is not None:
                    text = hud_filter.feed(text)
                    if not text:
                        continue
                    chunk = {**chunk, "content": text}
                full_content += text
                yield chunk
            elif chunk["type"] == "thinking":
                yield chunk
            elif chunk["type"] == "usage":
                usage_info = chunk
                yield chunk
            elif chunk["type"] == "error":
                logger.error(f"LLM error: {chunk.get('message')}")
                yield chunk
                return

        card_hud_event: Optional[Dict] = None
        if hud_filter is not None and card_hud_state is not None:
            tail, card_hud_event = self._finalize_card_hud(
                branch_id, hud_filter, card_hud_state.get("hud") or {}
            )
            if tail:
                full_content += tail
                yield {"type": "content", "content": tail}
        
        logger.info(f"Continue stream complete, content_len={len(full_content)}")
        if full_content:
            full_content = self._finalize_assistant_output(full_content, branch_id)
        record = _generation_record(
            llm_config,
            (time.perf_counter() - started) * 1000,
            first_token_ms,
            usage_info,
            full_content,
        )
        
        # 7. 将续写内容追加到原消息（只追加新内容，不重复原文）
        generation = record
        if full_content:
            appended_content = self._finalize_assistant_output(last_content + full_content, branch_id)
            previous = (last_ai_msg.get("metadata") or {}).get("generation")
            swipe_index = int(last_ai_msg.get("swipe_id") or 0)
            previous_record = None
            if isinstance(previous, list) and 0 <= swipe_index < len(previous):
                previous_record = previous[swipe_index]
            generation = _merge_generation(previous_record, record)
            update_message_content(
                self.id, branch_id, last_ai_msg['id'], appended_content, generation=generation,
            )
            logger.info(f"AI response appended, total_len={len(appended_content)}")
        
        if card_hud_event is not None:
            yield card_hud_event
        yield {"type": "done", "final_content": full_content, "message_id": last_ai_msg['id'], "memories": _in_play_payload(memories), "generation": generation}
    
    async def generate_regenerate_stream(
        self,
        message_id: int,
        branch_id: str = "main",
        llm_config: Optional[Dict] = None,
        enable_thinking: bool = False,
        temperature: float = 0.7,
        top_p: float = 0.9,
    ) -> AsyncGenerator[Dict, None]:
        """
        重新生成指定的AI消息。
        将新生成的内容作为新的swipe版本添加到原消息，保留之前的版本。
        """
        logger.info(f"generate_regenerate_stream called: id={self.id}, branch={branch_id}, msg_id={message_id}, temp={temperature}, top_p={top_p}")
        
        # 1. 获取历史消息（到该消息之前的所有消息，不含该AI消息本身）
        history = get_messages(self.id, branch_id)
        
        # 找到要重新生成的消息
        target_msg = None
        for msg in history:
            if msg['id'] == message_id and msg['role'] == 'assistant':
                target_msg = msg
                break
        
        if not target_msg:
            yield {"type": "error", "message": "未找到指定的AI消息"}
            return
        
        # 构建上下文：只取该消息之前的所有消息
        context_history = []
        for msg in history:
            if msg['id'] == message_id:
                break
            context_history.append(msg)
        
        # 2. 构建消息
        memories: List[Dict] = []
        card_hud_state = self._resolve_card_hud_state(branch_id)
        messages = self._build_messages(
            context_history, branch_id, memories, include_card_hud=bool(card_hud_state)
        )
        # 3. 获取LLM配置
        llm_config = self._resolve_llm_config_full(llm_config)
        if not llm_config:
            yield {"type": "error", "message": "No LLM provider configured"}
            return
        
        logger.info(f"Regenerate: LLM config: provider={llm_config['provider_type']}, model={llm_config['model']}")
        
        # 4. 调用LLM流式生成
        full_content = ""
        chunk_count = 0
        usage_info: Optional[Dict] = None
        started = time.perf_counter()
        first_token_ms: Optional[float] = None
        hud_filter = CardHudStreamFilter() if card_hud_state else None
        async for chunk in call_llm_stream(
            messages=messages,
            **self._build_llm_call_kwargs(
                llm_config, temperature=temperature, top_p=top_p, enable_thinking=enable_thinking
            ),
        ):
            chunk_count += 1
            if chunk["type"] == "content":
                if first_token_ms is None:
                    first_token_ms = (time.perf_counter() - started) * 1000
                text = chunk["content"]
                if hud_filter is not None:
                    text = hud_filter.feed(text)
                    if not text:
                        continue
                    chunk = {**chunk, "content": text}
                full_content += text
                yield chunk
            elif chunk["type"] == "thinking":
                yield chunk
            elif chunk["type"] == "usage":
                usage_info = chunk
                yield chunk
            elif chunk["type"] == "error":
                logger.error(f"LLM error: {chunk.get('message')}")
                yield chunk
                return

        card_hud_event: Optional[Dict] = None
        if hud_filter is not None and card_hud_state is not None:
            tail, card_hud_event = self._finalize_card_hud(
                branch_id, hud_filter, card_hud_state.get("hud") or {}
            )
            if tail:
                full_content += tail
                yield {"type": "content", "content": tail}

        logger.info(f"Regenerate stream complete, content_len={len(full_content)}")
        if full_content:
            full_content = self._finalize_assistant_output(full_content, branch_id)
        generation = _generation_record(
            llm_config,
            (time.perf_counter() - started) * 1000,
            first_token_ms,
            usage_info,
            full_content,
        )
        
        # 5. 将新内容作为新swipe版本添加到原消息
        if full_content:
            result = add_swipe_to_message(
                self.id, branch_id, message_id, full_content, generation=generation,
            )
            if result:
                logger.info(f"New swipe added, total_swipes={len(result['swipes'])}, swipe_id={result['swipe_id']}")
        
        if card_hud_event is not None:
            yield card_hud_event
        yield {"type": "done", "final_content": full_content, "message_id": message_id, "memories": _in_play_payload(memories), "generation": generation}

    async def generate_reedit_stream(
        self,
        message_id: int,
        new_content: str,
        branch_id: str = "main",
        llm_config: Optional[Dict] = None,
        enable_thinking: bool = False,
        temperature: float = 0.7,
        top_p: float = 0.9,
    ) -> AsyncGenerator[Dict, None]:
        """改写一条用户消息，丢掉它之后的旧时间线，再让模型重新回答。

        和 regenerate 的区别：那个换的是 AI 回复的版本，这个换的是提问本身。
        改过的问题之后的每一轮问答都已经对不上了，所以先删干净再生成。
        """
        logger.info(
            f"generate_reedit_stream called: id={self.id}, branch={branch_id}, "
            f"msg_id={message_id}, len={len(new_content)}"
        )

        if not update_message_content(self.id, branch_id, message_id, new_content):
            yield {"type": "error", "message": "未找到指定的消息"}
            return

        dropped = delete_messages_after(self.id, branch_id, message_id)
        clear_recorded_usage(self.id, branch_id)
        logger.info(f"Reedit: rewrote msg {message_id}, dropped {dropped} later messages")

        history = get_messages(self.id, branch_id)
        memories: List[Dict] = []
        card_hud_state = self._resolve_card_hud_state(branch_id)
        messages = self._build_messages(
            history, branch_id, memories, include_card_hud=bool(card_hud_state)
        )

        llm_config = self._resolve_llm_config_full(llm_config)
        if not llm_config:
            yield {"type": "error", "message": "No LLM provider configured"}
            return

        logger.info(
            f"Reedit: LLM config: provider={llm_config['provider_type']}, "
            f"model={llm_config['model']}"
        )

        full_content = ""
        usage_info: Optional[Dict] = None
        started = time.perf_counter()
        first_token_ms: Optional[float] = None
        hud_filter = CardHudStreamFilter() if card_hud_state else None
        async for chunk in call_llm_stream(
            messages=messages,
            **self._build_llm_call_kwargs(
                llm_config, temperature=temperature, top_p=top_p, enable_thinking=enable_thinking
            ),
        ):
            if chunk["type"] == "content":
                if first_token_ms is None:
                    first_token_ms = (time.perf_counter() - started) * 1000
                text = chunk["content"]
                if hud_filter is not None:
                    text = hud_filter.feed(text)
                    if not text:
                        continue
                    chunk = {**chunk, "content": text}
                full_content += text
                yield chunk
            elif chunk["type"] == "usage":
                usage_info = chunk
                yield chunk
            elif chunk["type"] == "thinking":
                yield chunk
            elif chunk["type"] == "error":
                logger.error(f"LLM error: {chunk.get('message')}")
                yield chunk
                return

        card_hud_event: Optional[Dict] = None
        if hud_filter is not None and card_hud_state is not None:
            tail, card_hud_event = self._finalize_card_hud(
                branch_id, hud_filter, card_hud_state.get("hud") or {}
            )
            if tail:
                full_content += tail
                yield {"type": "content", "content": tail}

        logger.info(f"Reedit stream complete, content_len={len(full_content)}")
        if full_content:
            full_content = self._finalize_assistant_output(full_content, branch_id)
        generation = _generation_record(
            llm_config,
            (time.perf_counter() - started) * 1000,
            first_token_ms,
            usage_info,
            full_content,
        )

        msg_id = None
        if full_content:
            msg_id = save_message_with_swipes(
                self.id, branch_id, "assistant", full_content, [full_content], 0,
                metadata={"generation": [generation]},
            )
            logger.info(f"Reedit: AI response saved, id={msg_id}")

        if card_hud_event is not None:
            yield card_hud_event
        yield {"type": "done", "final_content": full_content, "message_id": msg_id, "memories": _in_play_payload(memories), "generation": generation}
    
    # 调用方（前端）只带 provider/base_url/model/temperature/top_p，本地权重的
    # top_k/min_p/repeat_penalty/max_context 存在 llm_providers.json 的 modelParams 里。
    # 不补齐这几个字段，llm_client 会当作「调用方没指定」而不下发，llama.cpp 回落到
    # repeat_penalty=1.0（完全不惩罚），长回复就会退化成复读。
    _SAMPLING_FALLBACK_KEYS = ("max_tokens", "max_context", "top_k", "min_p", "repeat_penalty", "enable_mtp")

    @staticmethod
    def _build_llm_call_kwargs(
        llm_config: Dict,
        temperature: float,
        top_p: float,
        enable_thinking: bool,
    ) -> Dict:
        return dict(
            provider_type=llm_config["provider_type"],
            api_key=llm_config.get("api_key", ""),
            base_url=llm_config.get("base_url", ""),
            model=llm_config.get("model", ""),
            temperature=temperature,
            top_p=top_p,
            max_tokens=llm_config.get("max_tokens"),
            top_k=llm_config.get("top_k"),
            min_p=llm_config.get("min_p"),
            repeat_penalty=llm_config.get("repeat_penalty"),
            frequency_penalty=llm_config.get("frequency_penalty"),
            presence_penalty=llm_config.get("presence_penalty"),
            enable_thinking=enable_thinking,
            enable_mtp=llm_config.get("enable_mtp"),
        )

    def _resolve_llm_config_full(self, partial_config: Optional[Dict]) -> Optional[Dict]:
        """统一的LLM配置解析方法。

        调用方显式给出的非空字段优先；缺席的采样字段用磁盘上同一模型的配置补齐。
        """
        if not partial_config:
            return load_default_llm_config()

        resolved = self._resolve_llm_config(partial_config) if partial_config.get("model") else None

        # 只给了模型名、没给地址：从磁盘取完整配置（llama-cpp 允许空地址交由引擎托管兜底）
        if not partial_config.get("base_url"):
            if resolved:
                return resolved
            if partial_config.get("provider_type") == "llama-cpp":
                return dict(partial_config)
            return load_default_llm_config()

        # 地址与磁盘不一致时以磁盘为准（模型归属由 provider 决定）
        if (
            resolved
            and resolved.get("base_url")
            and resolved["base_url"] != partial_config.get("base_url")
            and not _is_local_base_url(partial_config.get("base_url"))
        ):
            return resolved

        merged = dict(partial_config)
        if resolved and (
            not merged.get("model")
            or resolved.get("model") == merged.get("model")
        ):
            if not merged.get("model") and resolved.get("model"):
                merged["model"] = resolved["model"]
            for key in self._SAMPLING_FALLBACK_KEYS:
                # 键缺席才补；显式 null 表示调用方要求沿用服务端默认，必须原样保留
                if key not in merged and resolved.get(key) is not None:
                    merged[key] = resolved[key]
        return merged

    def _resolve_llm_config(self, partial_config: Dict) -> Optional[Dict]:
        """从用户与项目配置中解析完整 LLM 配置（兼容 localModelKey 与自定义 GGUF 注册表）。"""
        return resolve_provider_model_config(str(partial_config.get("model") or ""))

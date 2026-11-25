"""scene_rules_ai.py - Natural-language compiler & explainer for Pebble Scene Rules.

Turns plain-language user intent (+ optional sample messages) into either:
- zero-regex contextual prompt triggers (CBS ``#when`` / ``idle_duration`` / ``time`` / ``chat_index``), or
- deterministic Display / Output / Input regex rules validated live against the sample.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .card_presentation import (
    _js_flags_to_python,
    apply_regex_scripts,
    normalize_display_regex_list,
    normalize_scene_rules_list,
)
from .cbs import evaluate_cbs
from .llm_client import call_llm_stream, load_default_llm_config


_COMPILER_SYSTEM_PROMPT = """You are Pebble Companion Chat's Scene Rule Compiler.
Convert the user's plain-language request into ONE deterministic JSON rule object. No markdown commentary outside JSON.

Decide which `rule_kind` fits best:
1. `"scene_prompt"` — Use when the user wants to adjust the character's tone, behavior, relationship stage, time/idle awareness, or prompt instructions based on chat context/turn/time/variables. NO regex is needed here.
2. `"display"` — Visual-only formatting of displayed messages/greetings (keeps stored chat history untouched). Can output safe inline-styled HTML tags (`div`, `span`, `p`, `details`, `summary`, `small`, `em`, `strong`, `blockquote`) using `style="..."`. Never use `<script>`, `position:fixed`, or JavaScript events.
3. `"output"` — Rewrite/clean finalized AI replies before storing (e.g., removing unwanted OOC notes, disclaimers, trailing tags, verbal tics).
4. `"input"` — Rewrite/expand user input before sending to the AI.
5. `"prompt"` — Context-only rewrite applied to chat history right before sending to the LLM while keeping what appears on screen untouched (e.g., stripping HTML/CSS/status blocks from older turns to save tokens or stop model mimicry).

Available CBS macros for `prompt`, `condition_cbs`, and `replace`:
- `{{char}}`, `{{user}}`
- `{{chat_index}}` (0-based turn/message index), `{{idle_duration}}` (e.g. "2h 15m"), `{{idle_minutes}}` (integer minutes), `{{time}}` (HH:MM:SS), `{{hour}}` (0-23 integer), `{{date}}`, `{{datetime}}`
- `{{assetlist}}` (JSON list of companion asset names), `{{lastmessage}}`, `{{lastcharmessage}}`, `{{lastusermessage}}`
- `{{getvar::key}}`
- Live Stage HUD macros: `{{hud_mood}}`, `{{hud_affection}}` (0-100), `{{hud_location}}`, `{{hud_outfit}}`, `{{hud_expression}}`, `{{hud_scene}}`, `{{hud_bgm}}`, or `{{hud::field}}` for custom HUD fields
- Comparisons & logic returning "1" or "0": `{{equal::a::b}}`, `{{not_equal::a::b}}`, `{{greater::a::b}}`, `{{less::a::b}}`, `{{greater_equal::a::b}}`, `{{less_equal::a::b}}`, `{{and::a::b}}`, `{{or::a::b}}`, `{{not::a}}`
- Conditionals: `{{#when::...}}...{{:else}}...{{/when}}` or `{{#if ...}}...{{/if}}`
- Regex capture groups inside `replace`: `$1`, `$2`, ..., or `{{match}}` (full match).

Output schema when `rule_kind == "scene_prompt"`:
{
  "rule_kind": "scene_prompt",
  "rule": {
    "name": "Short user-friendly title (<= 24 chars)",
    "summary": "One concise sentence explaining what this rule does in chat",
    "trigger_type": "always" | "idle" | "chat_turn" | "time_window" | "variable" | "custom_cbs",
    "condition_cbs": "CBS expression that evaluates to truthy ('1') or empty string for always",
    "prompt": "Instruction injected to the character when active (may contain CBS macros)",
    "placement": "system" | "post_history",
    "enabled": true
  }
}

Output schema when `rule_kind` is `"display"`, `"output"`, `"input"`, or `"prompt"`:
{
  "rule_kind": "display" | "output" | "input" | "prompt",
  "rule": {
    "name": "Short user-friendly title (<= 24 chars)",
    "summary": "One concise sentence explaining what this rule matches and transforms",
    "pattern": "ECMAScript/Python compatible regex pattern WITHOUT surrounding slashes",
    "flags": "gi" | "g" | "gs" | "gm",
    "replace": "Replacement text/HTML using $1/$2 or {{match}}; empty string to delete/hide",
    "enabled": true
  }
}

Rules:
- Reply in the SAME language as the user's request (`zh` for Chinese, `en` for English) for `name` and `summary`.
- If `sample_text` is provided for a regex rule, `pattern` MUST actually match `sample_text`. Be careful with fullwidth vs halfwidth punctuation (`【】` vs `[]`, `：` vs `:`)—support both e.g. `[:：]` when reasonable!
- Keep inline CSS subtle and theme-adaptive (`rgba(255,255,255,0.06)` or `currentColor` / `var(--accent)` / `opacity:0.78`), never blinding white boxes unless requested.
- Return valid JSON only."""


_EXPLAIN_SYSTEM_PROMPT = """You translate technical character-card regex/macro rules into plain, friendly user-facing descriptions.
Input is a JSON array of rules (`id`, `kind`, `name`, `pattern`, `replace`, `prompt`, `condition_cbs`).
Output ONLY a JSON object:
{
  "explanations": [
    {
      "id": "same id as input",
      "name": "Concise human title (<= 24 chars)",
      "summary": "1-2 plain sentences explaining what the user will see or experience in chat"
    }
  ]
}
Write in the requested language (`zh` = Simplified Chinese, `en` = English). Do not include raw regex syntax in `summary` unless naming delimiters like `【...】`."""


def _extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        return None
    # Strip optional fenced code block
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            pass
    return None


async def _collect_llm_text(llm_config: Dict[str, Any], messages: List[Dict[str, str]]) -> str:
    if not llm_config or not llm_config.get("provider_type"):
        raise ValueError("No LLM provider configured")
    chunks: List[str] = []
    error_msg = ""
    async for ev in call_llm_stream(
        provider_type=str(llm_config["provider_type"]),
        api_key=str(llm_config.get("api_key") or ""),
        base_url=str(llm_config.get("base_url") or ""),
        model=str(llm_config.get("model") or ""),
        messages=messages,
        temperature=0.2,
        top_p=0.9,
        max_tokens=1200,
        enable_thinking=False,
    ):
        kind = ev.get("type")
        if kind == "content":
            chunks.append(str(ev.get("content") or ""))
        elif kind == "error":
            error_msg = str(ev.get("message") or "LLM request failed")
            break
    if error_msg and not chunks:
        raise RuntimeError(error_msg)
    return "".join(chunks)


def _validate_regex_on_sample(
    rule: Dict[str, Any],
    sample_text: str,
    *,
    char_name: str,
    user_name: str,
    variables: Dict[str, str],
    asset_names: Sequence[str],
    hud: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[bool], str, str]:
    """Validate regex compiles cleanly and check whether it matches *sample_text*."""
    pattern = str(rule.get("pattern") or "")
    if not pattern:
        return False, False if sample_text else None, sample_text, "Empty regex pattern"
    flags_str = str(rule.get("flags") or "g")
    py_flags, _ = _js_flags_to_python(flags_str)
    try:
        compiled = re.compile(pattern, py_flags)
    except re.error as exc:
        return False, False if sample_text else None, sample_text, f"Invalid regex: {exc}"

    if not sample_text:
        return True, None, "", ""

    matched = compiled.search(sample_text) is not None
    preview_after = apply_regex_scripts(
        sample_text,
        [rule],
        char_name=char_name,
        user_name=user_name,
        variables=variables,
        asset_names=asset_names,
        hud=hud,
    )
    return True, matched, preview_after, ("" if matched else "Pattern did not match the sample text")


async def compile_scene_rule(
    *,
    instruction: str,
    sample_text: str = "",
    preferred_mode: str = "auto",
    char_name: str = "",
    user_name: str = "the user",
    variables: Optional[Dict[str, str]] = None,
    hud: Optional[Dict[str, Any]] = None,
    asset_names: Optional[Sequence[str]] = None,
    llm_config: Optional[Dict[str, Any]] = None,
    rule_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Compile a natural-language instruction into a validated Pebble Scene/Regex rule."""
    clean_inst = (instruction or "").strip()
    if not clean_inst:
        raise ValueError("Instruction is required")

    cfg = llm_config or load_default_llm_config() or {}
    if not cfg.get("provider_type"):
        raise ValueError("No LLM model configured. Please configure an AI model in Backstage settings or use Offline Blocks / Recipes.")

    mode_hint = (preferred_mode or "auto").strip().lower()
    asset_preview = ", ".join(list(asset_names or ())[:20])
    hud_map = dict(hud or {})
    user_payload = {
        "instruction": clean_inst,
        "preferred_mode": mode_hint,
        "character_name": char_name or "Character",
        "sample_text": sample_text or "",
        "available_variables": sorted(list((variables or {}).keys())),
        "available_hud_fields": sorted(list(hud_map.keys())),
        "sample_asset_names": asset_preview,
    }
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": _COMPILER_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]

    raw_reply = await _collect_llm_text(cfg, messages)
    parsed = _extract_json_object(raw_reply)
    if not parsed or not isinstance(parsed.get("rule"), dict):
        raise ValueError("Compiler did not return a valid rule JSON object")

    kind = str(parsed.get("rule_kind") or mode_hint or "display").strip().lower()
    if mode_hint in ("scene_prompt", "display", "output", "input", "prompt"):
        kind = mode_hint
    if kind not in ("scene_prompt", "display", "output", "input", "prompt"):
        kind = "display"

    raw_rule = dict(parsed["rule"])
    rid = rule_id or f"nl_{kind}_{re.sub(r'[^a-z0-9]', '', str(abs(hash(clean_inst))))[:8]}"
    raw_rule["id"] = rid
    raw_rule["origin"] = "ai"

    vars_map = dict(variables or {})
    assets = list(asset_names or ())

    if kind == "scene_prompt":
        normalized = normalize_scene_rules_list([raw_rule])
        if not normalized:
            raise ValueError("Compiled prompt rule was empty")
        rule_obj = normalized[0]
        cond_eval = (
            evaluate_cbs(
                rule_obj["condition_cbs"],
                char_name=char_name,
                user_name=user_name,
                variables=vars_map,
                asset_names=assets,
                hud=hud_map,
            ).strip()
            if rule_obj.get("condition_cbs")
            else "1"
        )
        resolved = evaluate_cbs(
            rule_obj["prompt"],
            char_name=char_name,
            user_name=user_name,
            variables=vars_map,
            asset_names=assets,
            hud=hud_map,
        )
        return {
            "rule_kind": "scene_prompt",
            "rule": rule_obj,
            "matched_sample": None,
            "preview_before": rule_obj.get("prompt", ""),
            "preview_after": resolved,
            "condition_active_now": bool(cond_eval and cond_eval.lower() not in ("0", "false", "null", "undefined", "none", "nan")),
            "repaired": False,
            "note": "",
        }

    # Regex rule path (`display`, `output`, `input`, `prompt`) with sample validation + 1 auto-repair pass
    if kind in ("display", "prompt"):
        raw_rule.setdefault("placement", [1, 2])
    else:
        raw_rule["placement"] = [2] if kind == "output" else [1]

    normalized_list = normalize_display_regex_list([raw_rule])
    rule_obj = normalized_list[0] if normalized_list else {
        "id": rid,
        "name": str(raw_rule.get("name") or "Custom Rule"),
        "summary": str(raw_rule.get("summary") or clean_inst[:80]),
        "pattern": str(raw_rule.get("pattern") or ""),
        "flags": str(raw_rule.get("flags") or "g"),
        "replace": str(raw_rule.get("replace") or ""),
        "placement": raw_rule["placement"],
        "trim_strings": [],
        "enabled": True,
        "origin": "ai",
    }

    valid, matched, preview_after, issue = _validate_regex_on_sample(
        rule_obj,
        sample_text,
        char_name=char_name,
        user_name=user_name,
        variables=vars_map,
        asset_names=assets,
        hud=hud_map,
    )

    repaired = False
    if not valid or (sample_text and matched is False):
        # Auto-repair once with explicit failure feedback.
        repair_messages = messages + [
            {"role": "assistant", "content": raw_reply},
            {
                "role": "user",
                "content": (
                    f"Validation issue: {issue}. "
                    "Please adjust `pattern`, `flags`, or `replace` so the regex compiles cleanly "
                    "and matches the provided `sample_text`. Return the fixed JSON object only."
                ),
            },
        ]
        try:
            retry_reply = await _collect_llm_text(cfg, repair_messages)
            retry_parsed = _extract_json_object(retry_reply)
            if retry_parsed and isinstance(retry_parsed.get("rule"), dict):
                cand = dict(retry_parsed["rule"])
                cand["id"] = rid
                cand["origin"] = "ai"
                cand["placement"] = rule_obj["placement"]
                norm_retry = normalize_display_regex_list([cand])
                if norm_retry:
                    v2, m2, p2, issue2 = _validate_regex_on_sample(
                        norm_retry[0],
                        sample_text,
                        char_name=char_name,
                        user_name=user_name,
                        variables=vars_map,
                        asset_names=assets,
                        hud=hud_map,
                    )
                    if v2:
                        rule_obj = norm_retry[0]
                        valid, matched, preview_after, issue = v2, m2, p2, issue2
                        repaired = True
        except Exception:
            pass

    if not valid:
        raise ValueError(issue or "Compiled regex pattern is invalid")

    return {
        "rule_kind": kind,
        "rule": rule_obj,
        "matched_sample": matched,
        "preview_before": sample_text,
        "preview_after": preview_after if sample_text else "",
        "repaired": repaired,
        "note": issue if (sample_text and matched is False) else "",
    }


async def explain_scene_rules(
    *,
    rules: Sequence[Dict[str, Any]],
    language: str = "zh",
    llm_config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """Batch-translate technical card rules into plain-language summaries."""
    items = [r for r in rules if isinstance(r, dict) and r.get("id")]
    if not items:
        return []
    cfg = llm_config or load_default_llm_config() or {}
    if not cfg.get("provider_type"):
        raise ValueError("No LLM model configured")

    compact = [
        {
            "id": str(r.get("id")),
            "kind": str(r.get("kind") or "display"),
            "name": str(r.get("name") or ""),
            "pattern": str(r.get("pattern") or "")[:300],
            "replace": str(r.get("replace") or "")[:300],
            "condition_cbs": str(r.get("condition_cbs") or "")[:200],
            "prompt": str(r.get("prompt") or "")[:300],
        }
        for r in items[:20]
    ]
    payload = {
        "language": "zh" if str(language or "zh").lower().startswith("zh") else "en",
        "rules": compact,
    }
    raw = await _collect_llm_text(
        cfg,
        [
            {"role": "system", "content": _EXPLAIN_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    )
    parsed = _extract_json_object(raw) or {}
    explanations = parsed.get("explanations")
    if not isinstance(explanations, list):
        return []
    out: List[Dict[str, str]] = []
    for item in explanations:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        out.append({
            "id": str(item["id"]),
            "name": str(item.get("name") or "").strip(),
            "summary": str(item.get("summary") or "").strip(),
        })
    return out

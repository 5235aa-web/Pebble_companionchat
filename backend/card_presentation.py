"""Card presentation metadata extraction (CBS defaults, Display Regex, Card CSS).

Normalizes RisuAI (``extensions.risuai``) and SillyTavern (``extensions.regex_scripts``)
metadata from ``raw_card_data`` alongside optional per-companion overrides stored in
``companions.card_ui``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .cbs import evaluate_cbs

_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>([\s\S]*?)</style>", re.IGNORECASE)
_SLASH_REGEX_RE = re.compile(r"^/([\s\S]*)/([a-zA-Z]*)$")
_SUPPORTED_JS_FLAGS = frozenset("gimsuyd")
_REPLACE_TOKEN_RE = re.compile(r"\{\{\s*match\s*\}\}|\$(&|\d{1,2})", re.IGNORECASE)
_IMG_TAG_SHORTHAND_REF_RE = re.compile(
    r"""(<img\s*=\s*)(?:"([^"]+)"|'([^']+)'|([^"'\n=<>`]+?)(?=\s*/?>))""",
    re.IGNORECASE,
)
_HTML_IMG_SRC_REF_RE = re.compile(
    r"""(<img\b[^>]*?\bsrc\s*=\s*)(?:"([^"\n<>]+)"|'([^'\n<>]+)')""",
    re.IGNORECASE,
)
_ASSET_MACRO_REF_RE = re.compile(r"(\{\{\s*(?:img|image|asset|raw)\s*::\s*)([^}]+?)(\s*\}\})", re.IGNORECASE)
_MALFORMED_IMG_TAG_RE = re.compile(
    r"""<("""
    r"""([a-zA-Z][a-zA-Z0-9_-]{0,15})\s*\(\s*(?:"([^"\n<>]*)"|'([^'\n<>]*)'|([^()\n<>]+?))\s*\)\s*/?"""
    r"""|([a-zA-Z][a-zA-Z0-9_-]{1,15})\s*=\s*(?:"([^"\n<>]*)"|'([^'\n<>]*)'|([^"'\n=<>`]+?))(?:\s*/?)"""
    r"""|([a-zA-Z][a-zA-Z0-9_-]{1,15})\s+\bsrc\s*=\s*(?:"([^"\n<>]*)"|'([^'\n<>]*)')(?:\s*/?)"""
    r"""|([a-zA-Z][a-zA-Z0-9_-]{1,15})\s+(?:"([^"\n<>]+)"|'([^'\n<>]+)')\s*/?"""
    r""")>""",
)
_MALFORMED_MACRO_TAG_RE = re.compile(
    r"""\{\{\s*(?:mg|mgr|imb|imgr|im|irm|firm|fimg|ig|pic|cg|emote|sprite)\s*::\s*([^}\n]+?)\s*\}\}""",
    re.IGNORECASE,
)
_INLINE_BACKTICK_TAG_RE = re.compile(
    r"""`(<[^`\n<>]+>|\{\{\s*[a-zA-Z0-9_-]+\s*::[^`\n{}]+\}\})`"""
)
_PRECEDING_VALID_IMAGE_RE = re.compile(
    r"""(?:<img\b[^>]*>|\{\{\s*(?:img|image|asset|raw)\s*::[^}]+\}\}|!\[[^\]]*\]\([^)]+\))\s*$""",
    re.IGNORECASE,
)
_FENCED_CODE_BLOCK_RE = re.compile(
    r"""(^|\n)([ \t]*)(`{3,}|~{3,})[^\n]*\n[\s\S]*?\n\2\3[ \t]*(?=\n|$)""",
)
_COMPACT_SEP_RE = re.compile(r"[\s_\-]+")
_SNAKE_SEP_RE = re.compile(r"[\s\-]+")
_NON_PAREN_PROTOCOLS = frozenset({"http", "https", "ftp", "mailto"})
_HTML_SRC_TAGS = frozenset({
    "img", "audio", "video", "source", "track", "iframe", "embed", "input", "script", "frame",
})
_KNOWN_IMAGE_TAG_TYPOS = frozenset({
    "mg", "mgr", "imb", "imgr", "im", "irm", "firm", "fimg", "ig",
    "img_", "image", "pic", "cg", "em", "emo", "emote", "sprite", "asset",
})
_NON_REF_PREFIX_RE = re.compile(r"^(?:https?:|data:|blob:|pebble-asset:)", re.IGNORECASE)


def _safe_json_obj(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _card_extensions(raw_card: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    data = raw_card.get("data") if isinstance(raw_card.get("data"), dict) else raw_card
    ext = data.get("extensions") if isinstance(data.get("extensions"), dict) else {}
    risu = ext.get("risuai") if isinstance(ext.get("risuai"), dict) else {}
    return ext, risu


def parse_default_variables(raw_card: Dict[str, Any]) -> Dict[str, str]:
    """Extract default CBS variables from RisuAI / Pebble card extensions."""
    ext, risu = _card_extensions(raw_card)
    out: Dict[str, str] = {}

    raw_vars = risu.get("defaultVariables")
    if isinstance(raw_vars, str):
        for line in raw_vars.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, val = stripped.split("=", 1)
            k = key.strip()
            if k:
                out[k] = val.strip()
    elif isinstance(raw_vars, dict):
        for k, v in raw_vars.items():
            key = str(k).strip()
            if key:
                out[key] = "" if v is None else str(v)

    pebble_vars = ext.get("pebble_variables")
    if isinstance(pebble_vars, dict):
        for k, v in pebble_vars.items():
            key = str(k).strip()
            if key:
                out[key] = "" if v is None else str(v)

    return out


def _split_regex_literal(raw_pattern: str, fallback_flags: str = "g") -> Tuple[str, str]:
    text = str(raw_pattern or "")
    m = _SLASH_REGEX_RE.match(text)
    if m:
        pattern = m.group(1)
        flags = "".join(ch for ch in m.group(2) if ch in _SUPPORTED_JS_FLAGS)
        return pattern, flags or fallback_flags
    flags = "".join(ch for ch in str(fallback_flags or "g") if ch in _SUPPORTED_JS_FLAGS)
    return text, flags or "g"


def normalize_display_regex_list(rules: Any) -> List[Dict[str, Any]]:
    """Normalize arbitrary user/card display-regex descriptors into canonical dicts."""
    if not isinstance(rules, list):
        return []
    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(rules):
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern") or "")
        if not pattern:
            continue
        raw_flags = str(item.get("flags") or "g")
        flags = "".join(ch for ch in raw_flags if ch in _SUPPORTED_JS_FLAGS) or "g"
        placement = item.get("placement")
        if isinstance(placement, list):
            clean_placement = [int(p) for p in placement if isinstance(p, (int, float)) or str(p).isdigit()]
        else:
            clean_placement = [1, 2]
        trim_strings = [str(s) for s in (item.get("trim_strings") or []) if isinstance(s, str) and s]
        entry: Dict[str, Any] = {
            "id": str(item.get("id") or f"rule_{idx}"),
            "name": str(item.get("name") or f"Rule {idx + 1}"),
            "pattern": pattern,
            "flags": flags,
            "replace": str(item.get("replace") or ""),
            "placement": clean_placement or [1, 2],
            "trim_strings": trim_strings,
            "enabled": bool(item.get("enabled", True)),
        }
        for depth_key in ("min_depth", "max_depth"):
            d_val = _coerce_optional_int(item.get(depth_key))
            if d_val is not None:
                entry[depth_key] = d_val
        for meta_key in ("summary", "origin", "recipe_id"):
            meta_val = item.get(meta_key)
            if isinstance(meta_val, str) and meta_val.strip():
                entry[meta_key] = meta_val.strip()
        out.append(entry)
    return out


def _coerce_optional_int(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool) or val == "":
        return None
    try:
        n = int(val)
        return n if n >= 0 else None
    except (ValueError, TypeError):
        return None


def normalize_hud_pinned_vars(raw: Any) -> List[str]:
    """Normalize unique, non-empty CBS variable names pinned to Stage HUD."""
    if not isinstance(raw, list):
        return []
    seen = set()
    out: List[str] = []
    for item in raw:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def normalize_scene_rules_list(rules: Any) -> List[Dict[str, Any]]:
    """Normalize prompt-level immersive Scene Rules descriptors."""
    if not isinstance(rules, list):
        return []
    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(rules):
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "")
        if not prompt.strip():
            continue
        placement = str(item.get("placement") or "system").strip().lower()
        if placement not in ("system", "post_history"):
            placement = "system"
        trigger_type = str(item.get("trigger_type") or "always").strip().lower()
        if trigger_type not in ("always", "idle", "chat_turn", "time_window", "variable", "custom_cbs"):
            trigger_type = "always"
        entry: Dict[str, Any] = {
            "id": str(item.get("id") or f"scene_{idx}"),
            "name": str(item.get("name") or f"Scene Rule {idx + 1}"),
            "summary": str(item.get("summary") or "").strip(),
            "enabled": bool(item.get("enabled", True)),
            "trigger_type": trigger_type,
            "condition_cbs": str(item.get("condition_cbs") or "").strip(),
            "prompt": prompt,
            "placement": placement,
        }
        for meta_key in ("origin", "recipe_id"):
            meta_val = item.get(meta_key)
            if isinstance(meta_val, str) and meta_val.strip():
                entry[meta_key] = meta_val.strip()
        out.append(entry)
    return out


def build_scene_rules_prompt(
    scene_rules: Sequence[Dict[str, Any]],
    placement: str,
    sub_fn,
) -> str:
    """Evaluate active *scene_rules* for *placement* ('system' or 'post_history')."""
    if not scene_rules:
        return ""
    target = (placement or "system").strip().lower()
    active_lines: List[str] = []
    for rule in scene_rules:
        if not isinstance(rule, dict) or rule.get("enabled") is False:
            continue
        rule_place = str(rule.get("placement") or "system").strip().lower()
        if rule_place != target:
            continue
        cond = str(rule.get("condition_cbs") or "").strip()
        if cond:
            cond_eval = str(sub_fn(cond) or "").strip().lower()
            if cond_eval in ("", "0", "false", "null", "undefined", "none", "nan"):
                continue
        body = str(sub_fn(str(rule.get("prompt") or "")) or "").strip()
        if body:
            active_lines.append(body)
    return "\n\n".join(active_lines)


def _extract_risu_scripts(raw_card: Dict[str, Any], script_type: str, id_prefix: str) -> List[Dict[str, Any]]:
    """Extract normalized rules of *script_type* from ``extensions.risuai.customScripts``."""
    _, risu = _card_extensions(raw_card)
    rules: List[Dict[str, Any]] = []
    custom_scripts = risu.get("customScripts")
    if not isinstance(custom_scripts, list):
        return rules

    target_type = script_type.strip().lower()
    for idx, item in enumerate(custom_scripts):
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip().lower() != target_type:
            continue
        raw_in = str(item.get("in") or "")
        if not raw_in:
            continue
        flag = str(item.get("flag") or "g") if item.get("ableFlag", True) else "g"
        pattern, flags = _split_regex_literal(raw_in, flag)
        if not pattern:
            continue
        rules.append({
            "id": f"{id_prefix}_{idx}",
            "name": str(item.get("comment") or f"Risu {target_type} {idx + 1}"),
            "pattern": pattern,
            "flags": flags,
            "replace": str(item.get("out") or ""),
            "placement": [1, 2],
            "trim_strings": [],
            "enabled": True,
        })
    return rules


def extract_display_regex(raw_card: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract display-only regex scripts from RisuAI and SillyTavern cards."""
    ext, _ = _card_extensions(raw_card)
    rules = _extract_risu_scripts(raw_card, "editdisplay", "risu")

    # 2. SillyTavern extensions.regex_scripts
    st_scripts = ext.get("regex_scripts")
    if isinstance(st_scripts, list):
        for idx, item in enumerate(st_scripts):
            if not isinstance(item, dict):
                continue
            if item.get("promptOnly") and not item.get("markdownOnly"):
                continue
            raw_find = str(item.get("findRegex") or "")
            if not raw_find:
                continue
            pattern, flags = _split_regex_literal(raw_find, "g")
            if not pattern:
                continue
            placement = item.get("placement")
            clean_placement = (
                [int(p) for p in placement if isinstance(p, (int, float)) or str(p).isdigit()]
                if isinstance(placement, list)
                else [2]
            )
            trim_strings = [str(s) for s in (item.get("trimStrings") or []) if isinstance(s, str) and s]
            rules.append({
                "id": str(item.get("id") or f"st_{idx}"),
                "name": str(item.get("scriptName") or f"ST Regex {idx + 1}"),
                "pattern": pattern,
                "flags": flags,
                "replace": str(item.get("replaceString") or ""),
                "placement": clean_placement or [2],
                "trim_strings": trim_strings,
                "enabled": not bool(item.get("disabled", False)),
            })

    return rules


def extract_output_regex(raw_card_data: Any, card_ui: Any = None) -> List[Dict[str, Any]]:
    """Extract ``editoutput`` regex scripts, preferring user overrides in *card_ui*."""
    overrides = _safe_json_obj(card_ui)
    if "output_regex" in overrides and isinstance(overrides["output_regex"], list):
        return normalize_display_regex_list(overrides["output_regex"])
    raw_card = _safe_json_obj(raw_card_data)
    return _extract_risu_scripts(raw_card, "editoutput", "risu_out")


def extract_input_regex(raw_card_data: Any, card_ui: Any = None) -> List[Dict[str, Any]]:
    """Extract ``editinput`` regex scripts, preferring user overrides in *card_ui*."""
    overrides = _safe_json_obj(card_ui)
    if "input_regex" in overrides and isinstance(overrides["input_regex"], list):
        return normalize_display_regex_list(overrides["input_regex"])
    raw_card = _safe_json_obj(raw_card_data)
    return _extract_risu_scripts(raw_card, "editinput", "risu_in")


def extract_prompt_regex(raw_card_data: Any, card_ui: Any = None) -> List[Dict[str, Any]]:
    """Extract prompt-only context rewrite rules (ST ``promptOnly`` / RisuAI ``editprocess``)."""
    overrides = _safe_json_obj(card_ui)
    if "prompt_regex" in overrides and isinstance(overrides["prompt_regex"], list):
        return normalize_display_regex_list(overrides["prompt_regex"])

    raw_card = _safe_json_obj(raw_card_data)
    ext, _ = _card_extensions(raw_card)
    rules = _extract_risu_scripts(raw_card, "editprocess", "risu_proc")

    st_scripts = ext.get("regex_scripts")
    if isinstance(st_scripts, list):
        for idx, item in enumerate(st_scripts):
            if not isinstance(item, dict) or not item.get("promptOnly"):
                continue
            raw_find = str(item.get("findRegex") or "")
            if not raw_find:
                continue
            pattern, flags = _split_regex_literal(raw_find, "g")
            if not pattern:
                continue
            placement = item.get("placement")
            clean_placement = (
                [int(p) for p in placement if isinstance(p, (int, float)) or str(p).isdigit()]
                if isinstance(placement, list)
                else [1, 2]
            )
            trim_strings = [str(s) for s in (item.get("trimStrings") or []) if isinstance(s, str) and s]
            entry: Dict[str, Any] = {
                "id": str(item.get("id") or f"st_prompt_{idx}"),
                "name": str(item.get("scriptName") or f"ST Prompt {idx + 1}"),
                "pattern": pattern,
                "flags": flags,
                "replace": str(item.get("replaceString") or ""),
                "placement": clean_placement or [1, 2],
                "trim_strings": trim_strings,
                "enabled": not bool(item.get("disabled", False)),
            }
            for st_key, norm_key in (("minDepth", "min_depth"), ("maxDepth", "max_depth")):
                d_val = _coerce_optional_int(item.get(st_key))
                if d_val is not None:
                    entry[norm_key] = d_val
            rules.append(entry)
    return rules


def _js_flags_to_python(flags: str) -> Tuple[int, bool]:
    py_flags = 0
    low = (flags or "g").lower()
    if "i" in low:
        py_flags |= re.IGNORECASE
    if "m" in low:
        py_flags |= re.MULTILINE
    if "s" in low:
        py_flags |= re.DOTALL
    return py_flags, ("g" in low)


def apply_regex_scripts(
    text: str,
    rules: Sequence[Dict[str, Any]],
    *,
    char_name: str = "",
    user_name: str = "the user",
    variables: Optional[Dict[str, str]] = None,
    seed: str = "",
    asset_names: Optional[Sequence[str]] = None,
    placement: Optional[int] = None,
    depth: Optional[int] = None,
    hud: Optional[Dict[str, Any]] = None,
) -> str:
    """Apply RisuAI/SillyTavern regex scripts safely to *text*."""
    if not text or not rules:
        return text

    current = text
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("enabled") is False:
            continue
        if placement is not None:
            rule_place = rule.get("placement")
            if isinstance(rule_place, list) and rule_place and placement not in rule_place:
                continue
        if depth is not None:
            min_d = _coerce_optional_int(rule.get("min_depth"))
            max_d = _coerce_optional_int(rule.get("max_depth"))
            if min_d is not None and depth < min_d:
                continue
            if max_d is not None and depth > max_d:
                continue
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            continue
        py_flags, is_global = _js_flags_to_python(str(rule.get("flags") or "g"))
        try:
            compiled = re.compile(pattern, py_flags)
        except re.error:
            continue

        trims = [str(s) for s in (rule.get("trim_strings") or []) if isinstance(s, str) and s]
        replacement_raw = str(rule.get("replace") or "")

        def _replace(match: re.Match) -> str:
            matched = match.group(0) or ""
            for t in trims:
                matched = matched.replace(t, "")
            groups = [g if g is not None else "" for g in match.groups()]

            def _expand(tok: re.Match) -> str:
                ref = tok.group(1)
                if not ref or ref == "&":
                    return matched
                idx = int(ref)
                if 1 <= idx <= len(groups):
                    return groups[idx - 1]
                return tok.group(0)

            expanded = _REPLACE_TOKEN_RE.sub(_expand, replacement_raw)
            if "{{" in expanded:
                return evaluate_cbs(
                    expanded,
                    char_name=char_name,
                    user_name=user_name,
                    variables=variables,
                    seed=seed,
                    asset_names=asset_names,
                    hud=hud,
                )
            return expanded

        try:
            current = compiled.sub(_replace, current, count=0 if is_global else 1)
        except Exception:
            continue
    return current


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def _common_prefix_len(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i


def resolve_fuzzy_asset_name(ref: str, asset_names: Sequence[str]) -> Optional[str]:
    """Return canonical asset name for an exact or high-confidence typo match."""
    cleaned = (ref or "").strip()
    if (
        not cleaned
        or len(cleaned) < 4
        or "{{" in cleaned
        or "<" in cleaned
        or ">" in cleaned
        or _NON_REF_PREFIX_RE.match(cleaned)
    ):
        return None

    low_map: Dict[str, str] = {}
    compact_map: Dict[str, Optional[str]] = {}
    candidates: List[Tuple[str, str, str]] = []
    for name in asset_names:
        s = str(name or "").strip()
        if not s:
            continue
        if s == cleaned:
            return s
        low = s.lower()
        if low not in low_map:
            low_map[low] = s
            compact = _COMPACT_SEP_RE.sub("", low)
            candidates.append((low, s, compact))
            if compact:
                if compact not in compact_map:
                    compact_map[compact] = s
                elif compact_map[compact] != s:
                    compact_map[compact] = None

    q = cleaned.lower()
    if q in low_map:
        return low_map[q]

    q_compact = _COMPACT_SEP_RE.sub("", q)
    if len(q_compact) >= 4:
        compact_exact = compact_map.get(q_compact)
        if compact_exact:
            return compact_exact

    # Unique suffix match when the model drops a shared card prefix (e.g. `fellatio_penetration` -> `Motoko_fellatio_penetration`).
    q_snake = _SNAKE_SEP_RE.sub("_", q).strip("_")
    if len(q_snake) >= 4:
        suffix_hits = [
            c_orig
            for c_low, c_orig, _ in candidates
            if c_low.endswith("_" + q_snake) or c_low.endswith("-" + q_snake)
        ]
        if len(suffix_hits) == 1:
            return suffix_hits[0]

    best_score = 0.0
    best_prefix = -1
    best_name: Optional[str] = None
    tied = False

    for c_low, c_orig, c_compact in candidates:
        if len(c_low) < 4:
            continue
        cand_score = 0.0
        cand_prefix = -1

        max_len = max(len(q), len(c_low))
        max_dist = max(3, len(c_low) // 4)
        if abs(len(q) - len(c_low)) <= max_dist:
            dist = _edit_distance(q, c_low)
            if dist <= max_dist:
                sim = 1.0 - (dist / max_len)
                if sim >= 0.75:
                    cand_score = sim
                    cand_prefix = _common_prefix_len(q, c_low)

        if len(q_compact) >= 4 and len(c_compact) >= 4:
            max_cmp_len = max(len(q_compact), len(c_compact))
            max_cmp_dist = max(2, len(c_compact) // 4)
            if abs(len(q_compact) - len(c_compact)) <= max_cmp_dist:
                dist_cmp = _edit_distance(q_compact, c_compact)
                if dist_cmp <= max_cmp_dist:
                    sim_cmp = 1.0 - (dist_cmp / max_cmp_len)
                    if sim_cmp >= 0.78 and sim_cmp > cand_score:
                        cand_score = sim_cmp
                        cand_prefix = _common_prefix_len(q_compact, c_compact)

        if cand_score <= 0.0:
            continue
        if cand_score > best_score + 1e-9 or (
            abs(cand_score - best_score) <= 1e-9 and cand_prefix > best_prefix
        ):
            best_score = cand_score
            best_prefix = cand_prefix
            best_name = c_orig
            tied = False
        elif abs(cand_score - best_score) <= 1e-9 and cand_prefix == best_prefix:
            tied = True

    return None if tied else best_name


def _normalize_asset_segment(text: str, asset_names: Sequence[str]) -> str:
    def _unwrap_inline_backtick(match: re.Match) -> str:
        inner = match.group(1)
        if (
            _IMG_TAG_SHORTHAND_REF_RE.match(inner)
            or _HTML_IMG_SRC_REF_RE.match(inner)
            or _ASSET_MACRO_REF_RE.match(inner)
            or _MALFORMED_MACRO_TAG_RE.match(inner)
        ):
            return inner
        m_bad = _MALFORMED_IMG_TAG_RE.fullmatch(inner)
        if m_bad:
            src_tag = m_bad.group(10)
            if not src_tag or src_tag.lower() not in _HTML_SRC_TAGS:
                return inner
        return match.group(0)

    def _fix_shorthand(match: re.Match) -> str:
        prefix = match.group(1)
        raw_ref = next((g for g in match.groups()[1:] if g is not None), "").strip()
        if match.group(2) is not None:
            quote = '"'
        elif match.group(3) is not None:
            quote = "'"
        else:
            quote = '"'
        fixed = resolve_fuzzy_asset_name(raw_ref, asset_names)
        if not fixed or (fixed == raw_ref and match.group(4) is None):
            return match.group(0)
        return f"{prefix}{quote}{fixed}{quote}"

    def _fix_html_src(match: re.Match) -> str:
        prefix = match.group(1)
        raw_ref = (match.group(2) if match.group(2) is not None else match.group(3) or "").strip()
        if not raw_ref or "{{" in raw_ref or _NON_REF_PREFIX_RE.match(raw_ref):
            return match.group(0)
        quote = '"' if match.group(2) is not None else "'"
        fixed = resolve_fuzzy_asset_name(raw_ref, asset_names)
        if not fixed or fixed == raw_ref:
            return match.group(0)
        return f"{prefix}{quote}{fixed}{quote}"

    def _fix_macro(match: re.Match) -> str:
        raw_ref = match.group(2)
        fixed = resolve_fuzzy_asset_name(raw_ref, asset_names)
        if not fixed or fixed == raw_ref:
            return match.group(0)
        return f"{match.group(1)}{fixed}{match.group(3)}"

    out = _INLINE_BACKTICK_TAG_RE.sub(_unwrap_inline_backtick, text)
    out = _IMG_TAG_SHORTHAND_REF_RE.sub(_fix_shorthand, out)
    out = _HTML_IMG_SRC_REF_RE.sub(_fix_html_src, out)
    out = _ASSET_MACRO_REF_RE.sub(_fix_macro, out)

    removed_any = False

    if _MALFORMED_IMG_TAG_RE.search(out):
        parts: List[str] = []
        cursor = 0
        for m in _MALFORMED_IMG_TAG_RE.finditer(out):
            parts.append(out[cursor:m.start()])
            cursor = m.end()
            paren_tag = m.group(2)
            equals_tag = m.group(6)
            src_tag = m.group(10)
            quoted_tag = m.group(13)

            if equals_tag and equals_tag.lower() == "img":
                parts.append(m.group(0))
                continue
            if paren_tag and paren_tag.lower() in _NON_PAREN_PROTOCOLS:
                parts.append(m.group(0))
                continue
            if src_tag and src_tag.lower() in _HTML_SRC_TAGS:
                parts.append(m.group(0))
                continue

            raw_ref = next(
                (
                    g
                    for g in (
                        m.group(3), m.group(4), m.group(5),
                        m.group(7), m.group(8), m.group(9),
                        m.group(11), m.group(12),
                        m.group(14), m.group(15),
                    )
                    if g is not None
                ),
                "",
            ).strip()
            preceding = "".join(parts)
            if _PRECEDING_VALID_IMAGE_RE.search(preceding):
                removed_any = True
                continue
            fixed = resolve_fuzzy_asset_name(raw_ref, asset_names)
            if fixed:
                parts.append(f'<img="{fixed}">')
                continue

            if equals_tag:
                is_quoted = m.group(7) is not None or m.group(8) is not None
                looks_like_asset_ref = (
                    is_quoted
                    or bool(re.search(r"[\s_/-]", raw_ref))
                    or equals_tag.lower() in _KNOWN_IMAGE_TAG_TYPOS
                )
                if not looks_like_asset_ref:
                    parts.append(m.group(0))
                    continue
            elif src_tag and src_tag.lower() not in _KNOWN_IMAGE_TAG_TYPOS:
                parts.append(m.group(0))
                continue
            elif quoted_tag and quoted_tag.lower() != "img" and quoted_tag.lower() not in _KNOWN_IMAGE_TAG_TYPOS:
                parts.append(m.group(0))
                continue

            removed_any = True
        parts.append(out[cursor:])
        out = "".join(parts)

    if _MALFORMED_MACRO_TAG_RE.search(out):
        parts = []
        cursor = 0
        for m in _MALFORMED_MACRO_TAG_RE.finditer(out):
            parts.append(out[cursor:m.start()])
            cursor = m.end()
            preceding = "".join(parts)
            raw_ref = (m.group(1) or "").strip()
            if _PRECEDING_VALID_IMAGE_RE.search(preceding):
                removed_any = True
                continue
            fixed = resolve_fuzzy_asset_name(raw_ref, asset_names)
            if fixed:
                parts.append(f"{{{{img::{fixed}}}}}")
            else:
                removed_any = True
        parts.append(out[cursor:])
        out = "".join(parts)

    if removed_any:
        out = re.sub(r"[ \t]+\n", "\n", out)
        out = re.sub(r"\n{3,}", "\n\n", out).rstrip()

    return out


def normalize_asset_tag_refs(text: str, asset_names: Sequence[str]) -> str:
    """Fix typos in ``<img="...">`` / ``{{img/raw::...}}`` and repair/strip malformed LLM image tags."""
    if not text or not asset_names:
        return text

    stashed: List[str] = []

    def _stash_fence(match: re.Match) -> str:
        idx = len(stashed)
        stashed.append(match.group(0))
        return f"\x00PEBBLE_FENCE_{idx}\x00"

    masked = _FENCED_CODE_BLOCK_RE.sub(_stash_fence, text)
    out = _normalize_asset_segment(masked, asset_names)
    if stashed:
        out = re.sub(r"\x00PEBBLE_FENCE_(\d+)\x00", lambda m: stashed[int(m.group(1))], out)
    return out


def extract_background_css(raw_card: Dict[str, Any]) -> str:
    """Extract card-scoped CSS from RisuAI ``backgroundHTML`` or Pebble extensions."""
    ext, risu = _card_extensions(raw_card)
    chunks: List[str] = []

    bg = risu.get("backgroundHTML")
    if isinstance(bg, str) and bg.strip():
        styles = [m.group(1).strip() for m in _STYLE_BLOCK_RE.finditer(bg) if m.group(1).strip()]
        if styles:
            chunks.extend(styles)
        elif "<" not in bg and "{" in bg:
            chunks.append(bg.strip())

    custom_css = ext.get("pebble_card_css")
    if isinstance(custom_css, str) and custom_css.strip():
        chunks.append(custom_css.strip())

    return "\n\n".join(chunks)


_AUTO_FRAME_BRIDGES: Tuple[Dict[str, Any], ...] = (
    {
        "markers": (".twu-asset", ".twu-asset-inner", ".twu-asset-trigger", ".twu-asset-profile"),
        "rule": {
            "id": "auto_twu_frame",
            "name": "TWU Asset Frame (Auto)",
            "pattern": r"""<img=["']?([^"'>]+)["']?\s*>""",
            "flags": "gi",
            "replace": (
                '<div class="twu-asset twu-image-$1">'
                '<details class="twu-asset-inner">'
                '<summary class="twu-asset-trigger"><img src="$1" alt="$1"></summary>'
                '<div class="twu-asset-profile">'
                '<span class="twu-asset-profile-role"></span>'
                '<span class="twu-asset-profile-name"></span>'
                '<span class="twu-asset-profile-rule"></span>'
                '<span class="twu-asset-profile-line">'
                '<span class="twu-asset-profile-key twu-profile-key-courses"></span>'
                '<span class="twu-asset-profile-courses"></span>'
                '</span>'
                '<span class="twu-asset-profile-line">'
                '<span class="twu-asset-profile-key twu-profile-key-lab"></span>'
                '<span class="twu-asset-profile-lab"></span>'
                '</span>'
                '</div>'
                '</details>'
                '</div>'
            ),
            "placement": [1, 2],
            "trim_strings": [],
            "enabled": True,
        },
    },
)


def _apply_auto_frame_bridges(
    display_regex: List[Dict[str, Any]],
    card_css: str,
) -> List[Dict[str, Any]]:
    """Synthesize a fallback Display Regex when a card ships frame CSS without its regex."""
    if not card_css:
        return display_regex
    has_img_rule = any("<img" in str(r.get("pattern") or "").lower() for r in display_regex)
    if has_img_rule:
        return display_regex

    out = list(display_regex)
    for bridge in _AUTO_FRAME_BRIDGES:
        if all(marker in card_css for marker in bridge["markers"]):
            out.append(dict(bridge["rule"]))
            break
    return out


def extract_card_presentation(raw_card_data: Any, card_ui: Any = None) -> Dict[str, Any]:
    """Build normalized ``card_presentation`` payload from raw card + user overrides."""
    raw_card = _safe_json_obj(raw_card_data)
    overrides = _safe_json_obj(card_ui)

    default_vars = parse_default_variables(raw_card)
    if isinstance(overrides.get("default_variables"), dict):
        for k, v in overrides["default_variables"].items():
            key = str(k).strip()
            if key:
                default_vars[key] = "" if v is None else str(v)

    if "display_regex" in overrides and isinstance(overrides["display_regex"], list):
        display_regex = normalize_display_regex_list(overrides["display_regex"])
    else:
        display_regex = extract_display_regex(raw_card)

    if "card_css" in overrides and isinstance(overrides["card_css"], str):
        card_css = overrides["card_css"]
    else:
        card_css = extract_background_css(raw_card)

    display_regex = _apply_auto_frame_bridges(display_regex, card_css)
    output_regex = extract_output_regex(raw_card, overrides)
    input_regex = extract_input_regex(raw_card, overrides)
    prompt_regex = extract_prompt_regex(raw_card, overrides)
    scene_rules = normalize_scene_rules_list(overrides.get("scene_rules"))
    hud_pinned_vars = normalize_hud_pinned_vars(overrides.get("hud_pinned_vars"))
    hud_thought_enabled = bool(overrides.get("hud_thought_enabled", False))

    return {
        "default_variables": default_vars,
        "display_regex": display_regex,
        "output_regex": output_regex,
        "input_regex": input_regex,
        "prompt_regex": prompt_regex,
        "scene_rules": scene_rules,
        "hud_pinned_vars": hud_pinned_vars,
        "hud_thought_enabled": hud_thought_enabled,
        "card_css": card_css,
    }

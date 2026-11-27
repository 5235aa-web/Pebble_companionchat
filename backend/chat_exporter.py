"""Export single-companion conversation branches into DATA_ROOT/outputchat."""

from __future__ import annotations

import base64
import html as html_lib
import json
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import paths
from .card_presentation import (
    apply_regex_scripts,
    extract_card_presentation,
    normalize_asset_tag_refs,
    repair_asset_tag_refs_only,
    resolve_fuzzy_asset_name,
)
from .companion_db import (
    get_branch_export_messages,
    get_branch_hud,
    get_branch_variables,
    get_branches,
    get_companion,
    get_companion_assets,
    get_compaction_history,
    get_user_settings,
)
from .memory_db import list_memories
from .placeholders import substitute
from .script_db import list_scene_summaries

VALID_EXPORT_FORMATS = ("md", "json", "html", "jsonl")
_MAX_EMBED_IMAGE_BYTES = 8 * 1024 * 1024

_FILENAME_INVALID_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>([\s\S]*?)</style>", re.IGNORECASE)
_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>[\s\S]*?</script>", re.IGNORECASE)
_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_DOC_SHELL_RE = re.compile(
    r"<!DOCTYPE\b[^>]*>"
    r"|<head\b[^>]*>[\s\S]*?</head>"
    r"|<title\b[^>]*>[\s\S]*?</title>"
    r"|</?(?:html|head|body)\b[^>]*>"
    r"|<(?:meta|link|base)\b[^>]*>",
    re.IGNORECASE,
)
_HTML_INLINE_STYLE_ATTR_RE = re.compile(
    r"""(\bstyle\s*=\s*)(?:"([^"]*)"|'([^']*)')""",
    re.IGNORECASE,
)
_IMG_MACRO_SRC_RE = re.compile(
    r"^\{\{\s*(?:img|image|asset|raw)\s*::\s*([^}]+?)\s*\}\}$",
    re.IGNORECASE,
)
_HTML_EVENT_ATTR_RE = re.compile(r"""\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_DANGEROUS_URI_RE = re.compile(r"""(?:javascript|vbscript)\s*:""", re.IGNORECASE)
_INLINE_MEDIA_RE = re.compile(
    r"\{\{\s*(img|image|asset|raw|audio|bgm|video)\s*::\s*([^}]+?)\s*\}\}"
    r"|!\[([^\]]*)\]\(([^)]+)\)"
    r"|((?:embeded|embedded)://[^\s\"'<>)]+)",
    re.IGNORECASE,
)
_IMG_SHORTHAND_RE = re.compile(
    r"""<img\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))([\s\S]*?)>""",
    re.IGNORECASE,
)
_HTML_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_HTML_SRC_ATTR_RE = re.compile(
    r"""(\bsrc\s*=\s*)(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""",
    re.IGNORECASE,
)
_HTML_ALT_ATTR_RE = re.compile(r"""\balt\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_CSS_URL_RE = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*))\s*\)""", re.IGNORECASE)
_PEBBLE_ASSET_ID_RE = re.compile(r"^pebble-asset:(\d+)$", re.IGNORECASE)
_EMBEDDED_URI_RE = re.compile(r"^(?:embeded|embedded)://(.*)$", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_BLOCK_TAG_RE = re.compile(
    r"^\s*<(?:div|section|article|header|footer|aside|details|summary|table|thead|tbody|tfoot|tr|td|th|ul|ol|li|dl|dt|dd|blockquote|pre|h[1-6]|hr|figure|figcaption|svg|style|p|center|input|label|button)\b",
    re.IGNORECASE,
)
_SAFE_INLINE_HTML_TAG_RE = re.compile(
    r"</?(?:span|b|i|strong|em|u|s|del|ins|mark|small|sub|sup|ruby|rt|rp|br|font|kbd|abbr|a|img|input|label|button)\b[^>]*>",
    re.IGNORECASE,
)
_HTML_CONTAINER_OPEN_RE = re.compile(
    r"<(div|section|article|header|footer|aside|details|table|ul|ol|dl|blockquote|figure|svg)\b[^>]*>",
    re.IGNORECASE,
)
_HTML_CONTAINER_CLOSE_RE = re.compile(
    r"</(div|section|article|header|footer|aside|details|table|ul|ol|dl|blockquote|figure|svg)\s*>",
    re.IGNORECASE,
)


_WINDOWS_RESERVED_FILENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _sanitize_filename_part(value: str, fallback: str) -> str:
    cleaned = _FILENAME_INVALID_RE.sub("_", (value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        return fallback
    cleaned = cleaned[:60].strip(" .")
    if not cleaned:
        return fallback
    if cleaned.upper() in _WINDOWS_RESERVED_FILENAMES:
        return f"{cleaned}_"
    return cleaned


def _next_output_path(companion_name: str, branch_label: str, fmt: str) -> Path:
    safe_char = _sanitize_filename_part(companion_name, "companion")
    safe_branch = _sanitize_filename_part(branch_label, "main")
    char_dir = paths.OUTPUT_CHAT_DIR / safe_char
    paths.assert_safe_data_path(char_dir)
    char_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{safe_char}_{safe_branch}_{stamp}"
    candidate = char_dir / f"{stem}.{fmt}"
    idx = 2
    while candidate.exists():
        candidate = char_dir / f"{stem}_{idx}.{fmt}"
        idx += 1
    return candidate


def _resolve_safe_upload_path(rel_path: str) -> Optional[Path]:
    raw = (rel_path or "").strip().lstrip("/")
    if not raw:
        return None
    if raw.startswith("uploads/"):
        rel = raw[len("uploads/"):]
    else:
        rel = raw
    root = paths.DATA_ROOT.resolve()
    candidates = [
        (paths.UPLOAD_ROOT / rel).resolve(),
        (root / raw).resolve(),
    ]
    for cand in candidates:
        if cand.is_relative_to(root) and cand.is_file():
            return cand
    return None


def _file_to_data_uri(path: Path, mime_hint: str = "") -> Optional[str]:
    try:
        if path.stat().st_size > _MAX_EMBED_IMAGE_BYTES:
            return None
        mime = (mime_hint or "").strip() or (mimetypes.guess_type(str(path))[0] or "")
        if not mime.startswith("image/"):
            return None
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"
    except OSError:
        return None


class _AssetResolver:
    """Resolve CHARX assets and local uploads for HTML/Markdown export."""

    def __init__(self, assets: Sequence[Dict[str, Any]]) -> None:
        self.by_id: Dict[int, Dict[str, Any]] = {}
        self.by_name: Dict[str, Dict[str, Any]] = {}
        self.by_lower: Dict[str, Dict[str, Any]] = {}
        self.by_stem_lower: Dict[str, Dict[str, Any]] = {}
        self.names: List[str] = []
        self._data_uri_cache: Dict[str, Optional[str]] = {}

        for asset in assets:
            aid = asset.get("id")
            if isinstance(aid, int):
                self.by_id[aid] = asset
            name = str(asset.get("name") or "").strip()
            if not name:
                continue
            self.names.append(name)
            self.by_name.setdefault(name, asset)
            self.by_lower.setdefault(name.lower(), asset)
            base_name = Path(name).name.lower()
            if base_name:
                self.by_lower.setdefault(base_name, asset)
            stem = Path(name).stem.lower()
            if stem:
                self.by_stem_lower.setdefault(stem, asset)

    def find(self, ref: str) -> Optional[Dict[str, Any]]:
        raw = (ref or "").strip()
        if not raw:
            return None
        m_id = _PEBBLE_ASSET_ID_RE.match(raw)
        if m_id:
            return self.by_id.get(int(m_id.group(1)))
        m_emb = _EMBEDDED_URI_RE.match(raw)
        if m_emb:
            raw = m_emb.group(1).strip()
        if raw in self.by_name:
            return self.by_name[raw]
        low = raw.lower()
        if low in self.by_lower:
            return self.by_lower[low]
        base_low = Path(raw).name.lower()
        if base_low in self.by_lower:
            return self.by_lower[base_low]
        stem = Path(raw).stem.lower()
        if stem in self.by_stem_lower:
            return self.by_stem_lower[stem]
        fuzzy = resolve_fuzzy_asset_name(raw, self.names)
        if fuzzy:
            return self.by_name.get(fuzzy)
        return None

    def asset_data_uri(self, asset: Dict[str, Any]) -> Optional[str]:
        rel = str(asset.get("rel_path") or "")
        if not rel:
            return None
        if rel in self._data_uri_cache:
            return self._data_uri_cache[rel]
        disk_path = _resolve_safe_upload_path(rel)
        uri = _file_to_data_uri(disk_path, str(asset.get("mime_type") or "")) if disk_path else None
        self._data_uri_cache[rel] = uri
        return uri

    def rel_path_data_uri(self, rel_path: str, mime_hint: str = "") -> Optional[str]:
        key = rel_path.strip()
        if not key:
            return None
        if key in self._data_uri_cache:
            return self._data_uri_cache[key]
        disk_path = _resolve_safe_upload_path(key)
        uri = _file_to_data_uri(disk_path, mime_hint) if disk_path else None
        self._data_uri_cache[key] = uri
        return uri


def _sanitize_css(raw_css: str, resolver: _AssetResolver, *, scope_host: str = ":host") -> str:
    if not raw_css or not raw_css.strip():
        return ""
    cleaned = _STYLE_BLOCK_RE.sub(lambda m: m.group(1), raw_css)
    cleaned = _SCRIPT_BLOCK_RE.sub("", cleaned)
    cleaned = re.sub(r"@import\b[^;\n]*(?:;|$)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"expression\s*\([^)]*\)", "none", cleaned, flags=re.IGNORECASE)
    cleaned = _DANGEROUS_URI_RE.sub("", cleaned)
    cleaned = re.sub(r"-moz-binding\s*:[^;]*(?:;|$)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"behavior\s*:[^;]*(?:;|$)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(\bposition\s*:\s*)fixed\b", r"\1absolute", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"(^|[},\s])(?::root|html|body)(?=\s*[{,.:#[\s])",
        rf"\1{scope_host}",
        cleaned,
        flags=re.IGNORECASE,
    )

    def _rewrite_url(match: re.Match) -> str:
        target = (match.group(1) or match.group(2) or match.group(3) or "").strip()
        if not target:
            return "none"
        macro_m = _IMG_MACRO_SRC_RE.match(target)
        ref = macro_m.group(1).strip() if macro_m else target
        asset = resolver.find(ref)
        if asset:
            uri = resolver.asset_data_uri(asset)
            if uri:
                return f'url("{uri}")'
        if re.match(r"^(?:https?://|data:image/)", target, re.IGNORECASE):
            safe = target.replace('"', "").replace("'", "").replace("(", "").replace(")", "")
            return f'url("{safe}")'
        return "none"

    return _CSS_URL_RE.sub(_rewrite_url, cleaned).strip()


def _resolve_message_text(
    raw_text: str,
    *,
    is_user: bool,
    is_ai: bool,
    ctx: Dict[str, Any],
    display_regex: Sequence[Dict[str, Any]],
    asset_names: Sequence[str],
) -> str:
    text = substitute(
        raw_text,
        char_name=ctx["char_name"],
        user_name=ctx["user_name"],
        variables=ctx["variables"],
        seed=ctx["seed"],
        asset_names=asset_names,
        chat_index=ctx["chat_index"],
        last_message_id=ctx["last_message_id"],
        last_message=ctx["last_message"],
        last_char_message=ctx["last_char_message"],
        last_user_message=ctx["last_user_message"],
        hud=ctx["hud"],
    )
    if is_ai and asset_names:
        text = normalize_asset_tag_refs(text, asset_names, rules=display_regex)
    if display_regex and (is_user or is_ai):
        text = apply_regex_scripts(
            text,
            display_regex,
            char_name=ctx["char_name"],
            user_name=ctx["user_name"],
            variables=ctx["variables"],
            seed=ctx["seed"],
            asset_names=asset_names,
            placement=1 if is_user else 2,
            hud=ctx["hud"],
        )
        if is_ai and asset_names:
            text = repair_asset_tag_refs_only(text, asset_names, rules=display_regex)
    return text


def _build_timeline(
    messages: Sequence[Dict[str, Any]],
    scene_summaries: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge branch messages and script-mode scene summaries in chronological order."""
    if not scene_summaries:
        return [{"kind": "message", "data": m} for m in messages]

    out: List[Dict[str, Any]] = []
    idx = 0
    for card in scene_summaries:
        cutoff = int(card.get("message_to_id") or 0)
        while idx < len(messages) and int(messages[idx].get("id") or 0) <= cutoff:
            out.append({"kind": "message", "data": messages[idx]})
            idx += 1
        out.append({"kind": "scene_summary", "data": card})
    while idx < len(messages):
        out.append({"kind": "message", "data": messages[idx]})
        idx += 1
    return out


def _clean_markdown_body(text: str, resolver: _AssetResolver) -> str:
    """Strip HTML status bars/styles so Markdown stays clean and readable."""
    if not text:
        return ""
    cleaned = _STYLE_BLOCK_RE.sub("", text)
    cleaned = _SCRIPT_BLOCK_RE.sub("", cleaned)
    cleaned = _COMMENT_RE.sub("", cleaned)
    cleaned = _DOC_SHELL_RE.sub("", cleaned)

    def _replace_html_img(match: re.Match) -> str:
        tag = match.group(0)
        shorthand = _IMG_SHORTHAND_RE.match(tag)
        if shorthand:
            ref = (shorthand.group(1) or shorthand.group(2) or shorthand.group(3) or "").strip()
            asset = resolver.find(ref) if ref else None
            label = asset["name"] if asset else (ref or "image")
            return f"*[图片: {label}]*"
        alt_m = _HTML_ALT_ATTR_RE.search(tag)
        src_m = _HTML_SRC_ATTR_RE.search(tag)
        alt = (alt_m.group(1) or alt_m.group(2) or "").strip() if alt_m else ""
        src = (src_m.group(2) or src_m.group(3) or src_m.group(4) or "").strip() if src_m else ""
        macro_m = _IMG_MACRO_SRC_RE.match(src)
        ref = macro_m.group(1).strip() if macro_m else src
        asset = resolver.find(ref) if ref else None
        label = alt or (asset["name"] if asset else "") or Path(ref).name or "image"
        return f"*[图片: {label}]*"

    # Replace <img> tags before inline macro extraction so <img src="{{raw::...}}"> does not nest.
    cleaned = _HTML_IMG_TAG_RE.sub(_replace_html_img, cleaned)

    def _replace_macro(match: re.Match) -> str:
        macro_kind = (match.group(1) or "").lower()
        macro_arg = match.group(2)
        md_alt = match.group(3)
        md_target = match.group(4)
        embed_uri = match.group(5)
        ref = (macro_arg or md_target or embed_uri or "").strip()
        if md_target is not None:
            return f"![{md_alt or 'image'}]({md_target})"
        asset = resolver.find(ref) if ref else None
        label = asset["name"] if asset else (ref or "media")
        if macro_kind in ("audio", "bgm"):
            return f"*[音频: {label}]*"
        if macro_kind == "video":
            return f"*[视频: {label}]*"
        return f"*[图片: {label}]*"

    cleaned = _INLINE_MEDIA_RE.sub(_replace_macro, cleaned)
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<hr\s*/?>", "\n---\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</(?:td|th)\s*>", " | ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"</(?:p|div|section|article|header|footer|aside|details|summary|tr|li|blockquote|pre|h[1-6])\s*>",
        "\n",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = _HTML_TAG_RE.sub("", cleaned)
    cleaned = html_lib.unescape(cleaned)
    lines = [line.rstrip() for line in cleaned.splitlines()]
    normalized = "\n".join(lines)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _inline_markdown_to_html(text: str) -> str:
    """Convert inline Markdown spans (code, bold, italic, links) safely to HTML."""
    tokens: List[str] = []

    def _stash(html_frag: str) -> str:
        idx = len(tokens)
        tokens.append(html_frag)
        return f"\x00MDTOK{idx}\x00"

    def _code_sub(m: re.Match) -> str:
        return _stash(f"<code>{html_lib.escape(m.group(1))}</code>")

    work = re.sub(r"`([^`\n]+)`", _code_sub, text)

    def _link_sub(m: re.Match) -> str:
        label = html_lib.escape(m.group(1))
        href = m.group(2).strip()
        if not re.match(r"^(?:https?://|mailto:|#)", href, re.IGNORECASE):
            return html_lib.escape(m.group(0))
        safe_href = html_lib.escape(href, quote=True)
        return _stash(f'<a href="{safe_href}" target="_blank" rel="noopener noreferrer">{label}</a>')

    work = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _link_sub, work)
    def _stash_safe_html(m: re.Match) -> str:
        tag = _HTML_EVENT_ATTR_RE.sub("", m.group(0))
        tag = _DANGEROUS_URI_RE.sub("", tag)
        return _stash(tag)

    work = _SAFE_INLINE_HTML_TAG_RE.sub(_stash_safe_html, work)
    work = html_lib.escape(work)
    work = re.sub(r"\*\*([^\n*]+?)\*\*", r"<strong>\1</strong>", work)
    work = re.sub(r"__([^\n_]+?)__", r"<strong>\1</strong>", work)
    work = re.sub(r"(?<!\*)\*([^\n*]+?)\*(?!\*)", r"<em>\1</em>", work)
    work = re.sub(r"~~([^\n~]+?)~~", r"<del>\1</del>", work)

    for idx, frag in enumerate(tokens):
        work = work.replace(f"\x00MDTOK{idx}\x00", frag)
    return work


def _prepare_rich_html_source(text: str, resolver: _AssetResolver) -> Tuple[str, List[str], List[Dict[str, str]]]:
    """Rewrite asset tags/URIs and extract sanitized <style> blocks for HTML export."""
    if not text:
        return "", [], []

    styles: List[str] = []
    inline_media_notes: List[Dict[str, str]] = []
    stashed_imgs: List[str] = []

    def _collect_style(m: re.Match) -> str:
        css = _sanitize_css(m.group(1), resolver, scope_host=":host")
        if css:
            styles.append(css)
        return ""

    work = text.replace("\u0000", "")
    work = _SCRIPT_BLOCK_RE.sub("", work)
    work = _STYLE_BLOCK_RE.sub(_collect_style, work)
    work = _COMMENT_RE.sub("", work)
    work = _DOC_SHELL_RE.sub("", work)
    work = _HTML_EVENT_ATTR_RE.sub("", work)
    work = _DANGEROUS_URI_RE.sub("", work)

    def _rewrite_inline_style(m: re.Match) -> str:
        prefix = m.group(1)
        val = m.group(2) if m.group(2) is not None else (m.group(3) or "")
        if "url(" not in val.lower():
            return m.group(0)
        cleaned_val = _sanitize_css(val, resolver, scope_host=":host")
        quote = '"' if m.group(2) is not None else "'"
        return f"{prefix}{quote}{cleaned_val}{quote}"

    work = _HTML_INLINE_STYLE_ATTR_RE.sub(_rewrite_inline_style, work)

    def _stash_html_img(m: re.Match) -> str:
        tag = m.group(0)
        shorthand = _IMG_SHORTHAND_RE.match(tag)
        if shorthand:
            ref = (shorthand.group(1) or shorthand.group(2) or shorthand.group(3) or "").strip()
            rest = (shorthand.group(4) or "").rstrip("/ ")
            extra = f" {rest}" if rest else ""
            asset = resolver.find(ref) if ref else None
            if asset:
                uri = resolver.asset_data_uri(asset)
                if uri:
                    resolved_tag = f'<img class="rich-img" src="{uri}" alt="{html_lib.escape(asset["name"], quote=True)}"{extra}>'
                else:
                    resolved_tag = f'<span class="media-chip">[图片: {html_lib.escape(ref)}]</span>'
            elif re.match(r"^(?:https?://|data:image/)", ref, re.IGNORECASE):
                resolved_tag = f'<img class="rich-img" src="{html_lib.escape(ref, quote=True)}"{extra}>'
            else:
                resolved_tag = f'<span class="media-chip">[图片: {html_lib.escape(ref)}]</span>'
        else:
            unresolved_ref: Optional[str] = None

            def _rewrite_src(sm: re.Match) -> str:
                nonlocal unresolved_ref
                prefix = sm.group(1)
                raw_src = (sm.group(2) or sm.group(3) or sm.group(4) or "").strip()
                macro_m = _IMG_MACRO_SRC_RE.match(raw_src)
                ref = macro_m.group(1).strip() if macro_m else raw_src
                asset = resolver.find(ref) if ref else None
                if asset:
                    uri = resolver.asset_data_uri(asset)
                    if uri:
                        return f'{prefix}"{uri}"'
                if re.match(r"^(?:https?://|data:image/)", ref, re.IGNORECASE):
                    return f'{prefix}"{html_lib.escape(ref, quote=True)}"'
                if ref:
                    uri = resolver.rel_path_data_uri(ref)
                    if uri:
                        return f'{prefix}"{uri}"'
                    unresolved_ref = ref
                return sm.group(0)

            resolved_tag = _HTML_IMG_TAG_RE.sub(lambda im: _HTML_SRC_ATTR_RE.sub(_rewrite_src, im.group(0)), tag)
            if unresolved_ref is not None:
                resolved_tag = f'<span class="media-chip">[图片: {html_lib.escape(unresolved_ref)}]</span>'

        idx = len(stashed_imgs)
        stashed_imgs.append(resolved_tag)
        return f"\u0000PEBBLE_IMG_{idx}\u0000"

    # Protect and rewrite HTML <img> tags before running _INLINE_MEDIA_RE so macros
    # inside <img src="{{raw::...}}"> are rewritten in place instead of nested.
    work = _HTML_IMG_TAG_RE.sub(_stash_html_img, work)

    def _replace_macro(m: re.Match) -> str:
        macro_kind = (m.group(1) or "").lower()
        macro_arg = m.group(2)
        md_alt = m.group(3)
        md_target = m.group(4)
        embed_uri = m.group(5)
        ref = (macro_arg or md_target or embed_uri or "").strip()
        asset = resolver.find(ref) if ref else None
        if asset:
            mime = str(asset.get("mime_type") or "")
            if mime.startswith("image/"):
                uri = resolver.asset_data_uri(asset)
                if uri:
                    alt = md_alt or asset["name"]
                    return f'<img class="rich-img" src="{uri}" alt="{html_lib.escape(alt, quote=True)}">'
            if mime.startswith("audio/") or macro_kind in ("audio", "bgm"):
                inline_media_notes.append({"kind": "audio", "name": asset["name"]})
                return ""
            if mime.startswith("video/") or macro_kind == "video":
                inline_media_notes.append({"kind": "video", "name": asset["name"]})
                return ""
        if md_target is not None:
            if re.match(r"^(?:https?://|data:image/)", md_target, re.IGNORECASE):
                return f'<img class="rich-img" src="{html_lib.escape(md_target, quote=True)}" alt="{html_lib.escape(md_alt or "", quote=True)}">'
            uri = resolver.rel_path_data_uri(md_target)
            if uri:
                return f'<img class="rich-img" src="{uri}" alt="{html_lib.escape(md_alt or "", quote=True)}">'
            return f'<span class="media-chip">[图片: {html_lib.escape(md_alt or md_target)}]</span>'
        if macro_kind in ("audio", "bgm"):
            inline_media_notes.append({"kind": "audio", "name": ref})
            return ""
        if macro_kind == "video":
            inline_media_notes.append({"kind": "video", "name": ref})
            return ""
        return f'<span class="media-chip">[媒体: {html_lib.escape(ref)}]</span>'

    work = _INLINE_MEDIA_RE.sub(_replace_macro, work)
    work = re.sub(r"\u0000PEBBLE_IMG_(\d+)\u0000", lambda m: stashed_imgs[int(m.group(1))], work)
    return work, styles, inline_media_notes


def _render_markdown_and_html_to_html(prepared: str) -> str:
    """Render mixed Markdown + preserved HTML blocks into clean HTML fragments."""
    if not prepared.strip():
        return ""

    lines = prepared.splitlines()
    out: List[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        # Fenced code block
        fence_m = re.match(r"^(`{3,}|~{3,})(.*)$", stripped)
        if fence_m:
            marker_ch = fence_m.group(1)[0]
            marker_len = len(fence_m.group(1))
            lang = fence_m.group(2).strip()
            code_lines: List[str] = []
            i += 1
            while i < n:
                cur = lines[i]
                if re.match(rf"^\s*{re.escape(marker_ch)}{{{marker_len},}}\s*$", cur):
                    i += 1
                    break
                code_lines.append(cur)
                i += 1
            cls = f' class="language-{html_lib.escape(lang, quote=True)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html_lib.escape(chr(10).join(code_lines))}</code></pre>")
            continue

        # Preserved HTML block (e.g. status bars, <details>, <table>, <div>)
        if _HTML_BLOCK_TAG_RE.match(stripped):
            html_lines = [line]
            depth = len(_HTML_CONTAINER_OPEN_RE.findall(line)) - len(_HTML_CONTAINER_CLOSE_RE.findall(line))
            has_closing_ahead = depth <= 0 or bool(
                _HTML_CONTAINER_CLOSE_RE.search("\n".join(lines[i + 1 :]))
            )
            i += 1
            while i < n and ((depth > 0 and has_closing_ahead) or lines[i].strip()):
                cur = lines[i]
                html_lines.append(cur)
                depth += len(_HTML_CONTAINER_OPEN_RE.findall(cur)) - len(_HTML_CONTAINER_CLOSE_RE.findall(cur))
                i += 1
                if depth <= 0 and i < n and not lines[i].strip():
                    break
            out.append("\n".join(html_lines))
            continue

        # Single-line standalone HTML image or media chip
        if re.match(r"^\s*<(?:img|span\s+class=\"media-chip\")\b", stripped, re.IGNORECASE) and stripped.endswith(">"):
            out.append(f'<div class="inline-media-line">{stripped}</div>')
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^(?:---+|\*\*\*+|___+)\s*$", stripped):
            out.append("<hr>")
            i += 1
            continue

        # ATX Heading
        h_m = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if h_m:
            lvl = len(h_m.group(1))
            out.append(f"<h{lvl}>{_inline_markdown_to_html(h_m.group(2).strip())}</h{lvl}>")
            i += 1
            continue

        # Blockquote
        if stripped.startswith(">"):
            q_lines: List[str] = []
            while i < n and lines[i].strip().startswith(">"):
                q_lines.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            inner = "<br>".join(_inline_markdown_to_html(ql) for ql in q_lines if ql.strip())
            out.append(f"<blockquote><p>{inner}</p></blockquote>")
            continue

        # Unordered list
        if re.match(r"^[-*+]\s+", stripped):
            items: List[str] = []
            while i < n and re.match(r"^\s*[-*+]\s+", lines[i]):
                item_txt = re.sub(r"^\s*[-*+]\s+", "", lines[i])
                items.append(f"<li>{_inline_markdown_to_html(item_txt)}</li>")
                i += 1
            out.append(f"<ul>{''.join(items)}</ul>")
            continue

        # Ordered list
        if re.match(r"^\d+[.)]\s+", stripped):
            items = []
            while i < n and re.match(r"^\s*\d+[.)]\s+", lines[i]):
                item_txt = re.sub(r"^\s*\d+[.)]\s+", "", lines[i])
                items.append(f"<li>{_inline_markdown_to_html(item_txt)}</li>")
                i += 1
            out.append(f"<ol>{''.join(items)}</ol>")
            continue

        # Plain dialogue / stage direction line
        cls = "direction" if stripped.startswith(("(", "（")) else "say"
        out.append(f'<p class="{cls}">{_inline_markdown_to_html(stripped)}</p>')
        i += 1

    return "\n".join(out)


def _format_attachments_md(attachments: Any) -> List[str]:
    if not isinstance(attachments, list):
        return []
    lines: List[str] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        kind = str(att.get("kind") or "file").lower()
        name = str(att.get("name") or kind)
        url = str(att.get("url") or "")
        if kind == "video":
            lines.append(f"*[附件视频: {name} ({url})]*")
        else:
            lines.append(f"*[附件图片: {name} ({url})]*")
    return lines


def _format_attachments_html(attachments: Any, resolver: _AssetResolver) -> str:
    if not isinstance(attachments, list) or not attachments:
        return ""
    frags: List[str] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        kind = str(att.get("kind") or "image").lower()
        name = str(att.get("name") or kind)
        url = str(att.get("url") or "")
        mime = str(att.get("mime") or "")
        if kind == "video":
            frags.append(f'<span class="media-chip">🎬 {html_lib.escape(name)}</span>')
            continue
        uri = resolver.rel_path_data_uri(url, mime)
        if uri:
            frags.append(
                f'<img class="att-img" src="{uri}" alt="{html_lib.escape(name, quote=True)}">'
            )
        else:
            frags.append(f'<span class="media-chip">🖼️ {html_lib.escape(name)}</span>')
    return f'<div class="att-strip">{"".join(frags)}</div>' if frags else ""


def _render_markdown_export(
    ctx: Dict[str, Any],
    timeline: Sequence[Dict[str, Any]],
    resolver: _AssetResolver,
) -> str:
    char_name = ctx["char_name"]
    user_name = ctx["user_name"]
    branch_label = ctx["branch_label"]
    exported_at = ctx["exported_at"]
    include_compacted = ctx["include_compacted"]

    lines: List[str] = [
        f"# {char_name} — {branch_label}",
        "",
        f"- **角色**: {char_name}",
        f"- **用户**: {user_name}",
        f"- **分支**: {branch_label} (`{ctx['branch_id']}`)",
        f"- **导出时间**: {exported_at}",
        f"- **包含已压缩消息**: {'是' if include_compacted else '否'}",
        f"- **消息条数**: {ctx['message_count']}",
    ]
    scenario = str(ctx["companion"].get("scenario") or "").strip()
    if scenario:
        lines.append(f"- **场景**: {scenario}")
    lines.extend(["", "---", ""])

    for entry in timeline:
        if entry["kind"] == "scene_summary":
            card = entry["data"]
            s_from = card.get("scene_from", 0)
            s_to = card.get("scene_to", 0)
            titles = str(card.get("scene_titles") or "").strip()
            header = f"Scene {s_from}-{s_to}" + (f" · {titles}" if titles else "")
            body = _clean_markdown_body(str(card.get("content") or ""), resolver)
            lines.append(f"### 📜 剧本分段摘要 ({header})")
            lines.append("")
            for bl in body.splitlines():
                lines.append(f"> {bl}" if bl else ">")
            lines.extend(["", "---", ""])
            continue

        msg = entry["data"]
        mtype = str(msg.get("message_type") or "chat")
        role = str(msg.get("role") or "")
        created_at = str(msg.get("created_at") or "")
        meta = msg.get("metadata") or {}

        if mtype == "compaction_summary":
            folded = int(meta.get("messages_compacted") or 0)
            ver = int(meta.get("compaction_version") or 1)
            raw_sum = str(meta.get("summary") or msg.get("content") or "")
            body = _clean_markdown_body(raw_sum, resolver)
            lines.append(f"### 🗜️ 记忆压缩摘要 (v{ver} · 折叠 {folded} 条) `{created_at}`")
            lines.append("")
            for bl in body.splitlines():
                lines.append(f"> {bl}" if bl else ">")
            lines.extend(["", "---", ""])
            continue

        if mtype == "narration":
            speaker = "旁白 (Narration)"
        elif role == "user":
            speaker = user_name
        else:
            speaker = char_name

        badges: List[str] = []
        if meta.get("is_greeting"):
            badges.append("开场白")
        if msg.get("is_important"):
            badges.append("★重要")
        if msg.get("is_compacted"):
            badges.append("已压缩")
        if meta.get("voice"):
            badges.append("语音")
        badge_str = f" [{' · '.join(badges)}]" if badges else ""
        time_str = f" · `{created_at}`" if created_at else ""

        lines.append(f"### {speaker}{badge_str}{time_str}")
        lines.append("")

        body = _clean_markdown_body(msg.get("resolved_content") or "", resolver)
        if body:
            lines.append(body)

        att_lines = _format_attachments_md(meta.get("attachments"))
        if att_lines:
            if body:
                lines.append("")
            lines.extend(att_lines)

        lines.extend(["", "---", ""])

    return "\n".join(lines).rstrip() + "\n"


def _render_pebble_json_export(ctx: Dict[str, Any]) -> str:
    comp = ctx["companion"]
    payload = {
        "format": "pebble_chat_export",
        "schema_version": 1,
        "exported_at": ctx["exported_at"],
        "include_compacted": ctx["include_compacted"],
        "companion": {
            "id": comp.get("id", ""),
            "name": comp.get("name", ""),
            "creator": comp.get("creator", ""),
            "character_version": comp.get("character_version", ""),
            "scenario": comp.get("scenario", ""),
            "card_format": comp.get("card_format", ""),
        },
        "user": {
            "name": ctx["user_name"],
        },
        "branch": {
            "branch_id": ctx["branch_id"],
            "branch_name": ctx["branch_label"],
            "parent_branch_id": ctx["branch"].get("parent_branch_id", ""),
            "fork_from_message_id": ctx["branch"].get("fork_from_message_id"),
            "created_at": ctx["branch"].get("created_at", ""),
            "variables": ctx["variables"],
            "hud": ctx["hud"],
        },
        "stats": {
            "message_count": ctx["message_count"],
            "compacted_count": ctx["compacted_count"],
        },
        "messages": [
            {
                "id": m["id"],
                "role": m["role"],
                "message_type": m.get("message_type") or "chat",
                "content": m["content"],
                "resolved_content": m.get("resolved_content", m["content"]),
                "swipes": m.get("swipes") or [m["content"]],
                "swipe_id": int(m.get("swipe_id") or 0),
                "is_important": bool(m.get("is_important")),
                "is_compacted": bool(m.get("is_compacted")),
                "compaction_batch_id": m.get("compaction_batch_id") or "",
                "metadata": m.get("metadata") or {},
                "created_at": m.get("created_at") or "",
            }
            for m in ctx["messages"]
        ],
        "compaction_history": ctx["compaction_history"],
        "script_scene_summaries": ctx["scene_summaries"],
        "memories": ctx["memories"],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _render_sillytavern_jsonl_export(ctx: Dict[str, Any]) -> str:
    char_name = ctx["char_name"]
    user_name = ctx["user_name"]
    header = {
        "user_name": user_name,
        "character_name": char_name,
        "create_date": ctx["branch"].get("created_at") or ctx["exported_at"],
        "chat_metadata": {
            "pebble_companion_id": ctx["companion_id"],
            "pebble_branch_id": ctx["branch_id"],
            "pebble_branch_name": ctx["branch_label"],
            "exported_at": ctx["exported_at"],
            "include_compacted": ctx["include_compacted"],
        },
    }
    lines = [json.dumps(header, ensure_ascii=False)]

    for m in ctx["messages"]:
        mtype = str(m.get("message_type") or "chat")
        role = str(m.get("role") or "")
        meta = m.get("metadata") or {}

        if mtype == "compaction_summary":
            entry = {
                "name": "System",
                "is_user": False,
                "is_system": True,
                "send_date": m.get("created_at") or "",
                "mes": str(meta.get("summary") or m.get("content") or ""),
                "extra": {
                    "pebble_message_type": "compaction_summary",
                    "messages_compacted": meta.get("messages_compacted", 0),
                },
            }
            lines.append(json.dumps(entry, ensure_ascii=False))
            continue

        if mtype == "narration":
            entry = {
                "name": "Narration",
                "is_user": False,
                "is_system": True,
                "send_date": m.get("created_at") or "",
                "mes": m.get("content") or "",
                "extra": {
                    "pebble_message_type": "narration",
                    "is_compacted": bool(m.get("is_compacted")),
                    "is_important": bool(m.get("is_important")),
                },
            }
            lines.append(json.dumps(entry, ensure_ascii=False))
            continue

        is_user = role == "user"
        swipes = m.get("swipes") or [m.get("content") or ""]
        swipe_id = int(m.get("swipe_id") or 0)
        entry = {
            "name": user_name if is_user else char_name,
            "is_user": is_user,
            "is_system": False,
            "send_date": m.get("created_at") or "",
            "mes": m.get("content") or "",
            "swipes": swipes,
            "swipe_id": swipe_id,
            "extra": {
                "pebble_message_id": m.get("id"),
                "pebble_message_type": "greeting" if meta.get("is_greeting") else "chat",
                "is_compacted": bool(m.get("is_compacted")),
                "is_important": bool(m.get("is_important")),
            },
        }
        lines.append(json.dumps(entry, ensure_ascii=False))

    return "\n".join(lines) + "\n"


_HTML_SHELL_CSS = """
:root {
  --bg: #111114;
  --bg-2: #191a1f;
  --bg-3: #22242b;
  --ink: #ebe7df;
  --ink-2: #cfc9be;
  --ink-3: #9b958a;
  --ink-4: #6d685f;
  --line: rgba(255, 255, 255, .08);
  --line-2: rgba(255, 255, 255, .15);
  --accent: #e0a35e;
  --accent-dim: #b47e42;
  --accent-rgb: 224 163 94;
}
html[data-theme="day"] {
  --bg: #f6f3ed;
  --bg-2: #ffffff;
  --bg-3: #eeeae1;
  --ink: #24211c;
  --ink-2: #3d3830;
  --ink-3: #6e675c;
  --ink-4: #999184;
  --line: rgba(36, 33, 28, .09);
  --line-2: rgba(36, 33, 28, .17);
  --accent: #b86c25;
  --accent-dim: #935318;
  --accent-rgb: 184 108 37;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: "LXGW WenKai", "PingFang SC", "Microsoft YaHei", system-ui, -apple-system, sans-serif;
  line-height: 1.72;
  -webkit-font-smoothing: antialiased;
}
.page {
  max-width: 880px;
  margin: 0 auto;
  padding: 40px 24px 80px;
}
.doc-head {
  padding-bottom: 24px;
  margin-bottom: 32px;
  border-bottom: 1px solid var(--line-2);
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}
.doc-kicker {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  letter-spacing: .18em;
  text-transform: uppercase;
  color: var(--accent-dim);
  margin-bottom: 6px;
}
.doc-title {
  margin: 0 0 10px;
  font-size: 26px;
  font-weight: 600;
  letter-spacing: .01em;
}
.doc-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 18px;
  font-size: 13px;
  color: var(--ink-3);
}
.theme-btn {
  flex: none;
  padding: 6px 12px;
  border: 1px solid var(--line-2);
  background: var(--bg-2);
  color: var(--ink-2);
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
}
.theme-btn:hover { border-color: var(--accent); color: var(--accent); }
.beat {
  padding: 20px 0;
  border-bottom: 1px solid var(--line);
}
.beat.is-compacted { opacity: .82; }
.beat-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}
.speaker {
  font-size: 13px;
  font-weight: 600;
  letter-spacing: .04em;
  color: var(--accent);
}
.beat[data-role="you"] .speaker { color: var(--ink-2); }
.beat[data-role="narration"] .speaker { color: var(--ink-3); }
.beat-time {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--ink-4);
}
.badge {
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 999px;
  border: 1px solid var(--line-2);
  color: var(--ink-3);
}
.badge.important { border-color: var(--accent-dim); color: var(--accent); }
.summary-card {
  margin: 18px 0;
  padding: 16px 20px;
  background: var(--bg-2);
  border: 1px solid var(--line-2);
  border-left: 3px solid var(--accent);
  border-radius: 6px;
}
.summary-head {
  font-size: 12px;
  font-weight: 600;
  color: var(--accent);
  margin-bottom: 8px;
  letter-spacing: .04em;
}
.summary-body { font-size: 14.5px; color: var(--ink-2); }
.att-strip { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; }
.att-img, .rich-img { max-width: min(100%, 480px); border-radius: 6px; display: block; margin: 8px 0; }
.media-chip {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--bg-3);
  border: 1px solid var(--line-2);
  font-size: 12px;
  color: var(--ink-3);
}
"""

_SHADOW_MESSAGE_CSS = """
:host { display: block; position: relative; min-width: 0; color: var(--ink); }
.rich-msg { position: relative; }
.say { font-size: 16.5px; line-height: 1.76; color: var(--ink); margin: 0 0 .62em; }
.say:last-child { margin-bottom: 0; }
:host([data-role="you"]) .say { color: var(--ink-2); font-size: 15.5px; }
.direction {
  font-size: 15.5px; line-height: 1.68; font-style: italic; color: var(--ink-3);
  border-left: 2px solid var(--line-2); padding-left: 14px; margin: 0 0 .74em;
}
:host([data-role="you"]) .direction { border-left: none; padding-left: 0; opacity: .85; }
.direction:last-child { margin-bottom: 0; }
.rich-img { max-width: 100%; border-radius: 6px; display: block; margin: 8px 0; }
.media-chip {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  background: var(--bg-3); border: 1px solid var(--line-2);
  font-size: 12px; color: var(--ink-3);
}
blockquote { margin: .6em 0; padding: 4px 14px; border-left: 2px solid var(--line-2); color: var(--ink-3); }
pre { overflow-x: auto; padding: 12px 14px; border-radius: 6px; background: var(--bg-3); font-size: 13px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .9em; }
a { color: var(--accent); }
table { border-collapse: collapse; max-width: 100%; }
"""

_PLAIN_MESSAGE_CSS = _SHADOW_MESSAGE_CSS.replace(":host([data-role=\"you\"])", '.msg-plain[data-role="you"]').replace(":host", ".msg-plain")


def _render_html_export(
    ctx: Dict[str, Any],
    timeline: Sequence[Dict[str, Any]],
    resolver: _AssetResolver,
) -> str:
    char_name = ctx["char_name"]
    user_name = ctx["user_name"]
    branch_label = ctx["branch_label"]
    raw_card_css = str(ctx["presentation"].get("card_css") or "")
    if raw_card_css:
        asset_names = [str(a["name"]) for a in ctx["assets"] if a.get("name")]
        evaluated_css = substitute(
            raw_card_css,
            char_name=char_name,
            user_name=user_name,
            variables=ctx["variables"],
            seed=f"{ctx['companion_id']}:{ctx['branch_id']}",
            asset_names=asset_names,
            hud=ctx["hud"],
        )
        display_regex = ctx["presentation"].get("display_regex") or []
        if display_regex:
            evaluated_css = apply_regex_scripts(
                evaluated_css,
                display_regex,
                char_name=char_name,
                user_name=user_name,
                variables=ctx["variables"],
                seed=f"{ctx['companion_id']}:{ctx['branch_id']}",
                asset_names=asset_names,
                placement=2,
                hud=ctx["hud"],
            )
        raw_card_css = evaluated_css
    sanitized_card_css = _sanitize_css(raw_card_css, resolver, scope_host=":host")

    body_parts: List[str] = []
    for entry in timeline:
        if entry["kind"] == "scene_summary":
            card = entry["data"]
            s_from = card.get("scene_from", 0)
            s_to = card.get("scene_to", 0)
            titles = str(card.get("scene_titles") or "").strip()
            header = f"Scene {s_from}-{s_to}" + (f" · {titles}" if titles else "")
            prep, _, _ = _prepare_rich_html_source(str(card.get("content") or ""), resolver)
            rendered = _render_markdown_and_html_to_html(prep)
            body_parts.append(
                f'<section class="summary-card">'
                f'<div class="summary-head">📜 剧本分段摘要 · {html_lib.escape(header)}</div>'
                f'<div class="summary-body">{rendered}</div>'
                f"</section>"
            )
            continue

        msg = entry["data"]
        mtype = str(msg.get("message_type") or "chat")
        role = str(msg.get("role") or "")
        created_at = str(msg.get("created_at") or "")
        meta = msg.get("metadata") or {}

        if mtype == "compaction_summary":
            folded = int(meta.get("messages_compacted") or 0)
            ver = int(meta.get("compaction_version") or 1)
            raw_sum = str(meta.get("summary") or msg.get("content") or "")
            prep, _, _ = _prepare_rich_html_source(raw_sum, resolver)
            rendered = _render_markdown_and_html_to_html(prep)
            body_parts.append(
                f'<section class="summary-card">'
                f'<div class="summary-head">🗜️ 记忆压缩摘要 v{ver} · 折叠 {folded} 条 · {html_lib.escape(created_at)}</div>'
                f'<div class="summary-body">{rendered}</div>'
                f"</section>"
            )
            continue

        if mtype == "narration":
            ui_role = "narration"
            speaker = "旁白 (Narration)"
        elif role == "user":
            ui_role = "you"
            speaker = user_name
        else:
            ui_role = "ai"
            speaker = char_name

        badges: List[str] = []
        if meta.get("is_greeting"):
            badges.append('<span class="badge">开场白</span>')
        if msg.get("is_important"):
            badges.append('<span class="badge important">★ 重要</span>')
        if msg.get("is_compacted"):
            badges.append('<span class="badge">已压缩</span>')
        if meta.get("voice"):
            badges.append('<span class="badge">语音</span>')

        prepared, inline_styles, inline_notes = _prepare_rich_html_source(
            msg.get("resolved_content") or "",
            resolver,
        )
        content_html = _render_markdown_and_html_to_html(prepared)
        if inline_notes:
            chips = "".join(
                f'<span class="media-chip">{"🎵" if n["kind"] == "audio" else "🎬"} {html_lib.escape(n["name"])}</span>'
                for n in inline_notes
            )
            content_html += f'<div class="att-strip">{chips}</div>'

        use_shadow = bool(sanitized_card_css or inline_styles or re.search(r"<(?:div|table|details|section|style|input|label)\b", prepared, re.IGNORECASE))
        if use_shadow:
            shadow_css = "\n".join(filter(None, [_SHADOW_MESSAGE_CSS, sanitized_card_css, *inline_styles]))
            msg_body_html = (
                f'<div class="msg-host" data-role="{ui_role}">'
                f'<template shadowrootmode="open">'
                f"<style>{shadow_css}</style>"
                f'<div class="rich-msg">{content_html}</div>'
                f"</template>"
                f"</div>"
            )
        else:
            msg_body_html = f'<div class="msg-plain" data-role="{ui_role}">{content_html}</div>'

        att_html = _format_attachments_html(meta.get("attachments"), resolver)
        compacted_cls = " is-compacted" if msg.get("is_compacted") else ""
        body_parts.append(
            f'<article class="beat{compacted_cls}" data-role="{ui_role}">'
            f'<div class="beat-head">'
            f'<span class="speaker">{html_lib.escape(speaker)}</span>'
            f'<span class="beat-time">{html_lib.escape(created_at)}</span>'
            f'{"".join(badges)}'
            f"</div>"
            f"{msg_body_html}"
            f"{att_html}"
            f"</article>"
        )

    title = f"{char_name} — {branch_label}"
    scenario = str(ctx["companion"].get("scenario") or "").strip()
    scenario_meta = f"<span>场景: {html_lib.escape(scenario)}</span>" if scenario else ""
    compacted_flag = "是" if ctx["include_compacted"] else "否"

    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html_lib.escape(title)}</title>\n"
        f"<style>{_HTML_SHELL_CSS}\n{_PLAIN_MESSAGE_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        '<main class="page">\n'
        '  <header class="doc-head">\n'
        "    <div>\n"
        '      <div class="doc-kicker">PEBBLE CONVERSATION EXPORT</div>\n'
        f'      <h1 class="doc-title">{html_lib.escape(title)}</h1>\n'
        '      <div class="doc-meta">\n'
        f"        <span>角色: {html_lib.escape(char_name)}</span>\n"
        f"        <span>用户: {html_lib.escape(user_name)}</span>\n"
        f"        <span>分支: {html_lib.escape(branch_label)}</span>\n"
        f"        <span>消息数: {ctx['message_count']}</span>\n"
        f"        <span>包含已压缩: {compacted_flag}</span>\n"
        f"        <span>导出时间: {html_lib.escape(ctx['exported_at'])}</span>\n"
        f"        {scenario_meta}\n"
        "      </div>\n"
        "    </div>\n"
        '    <button class="theme-btn" type="button" onclick="document.documentElement.dataset.theme = document.documentElement.dataset.theme === \'day\' ? \'night\' : \'day\'">昼 / 夜</button>\n'
        "  </header>\n"
        f'  {"".join(body_parts)}\n'
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


def _load_branch_export_context(
    companion_id: str,
    branch_id: str,
    include_compacted: bool,
) -> Dict[str, Any]:
    companion = get_companion(companion_id)
    if not companion:
        raise LookupError(f"Companion not found: {companion_id}")

    branches = get_branches(companion_id)
    branch = next((b for b in branches if b.get("branch_id") == branch_id), None)
    if not branch:
        if branch_id == "main":
            branch = {"branch_id": "main", "branch_name": ""}
        else:
            raise LookupError(f"Branch not found: {branch_id}")

    user_settings = get_user_settings() or {}
    user_name = str(user_settings.get("name") or "").strip() or "You"
    char_name = str(companion.get("name") or companion_id)
    branch_name = str(branch.get("branch_name") or "").strip()
    branch_label = branch_name or ("主对话" if branch_id == "main" else branch_id)

    messages = get_branch_export_messages(
        companion_id,
        branch_id,
        include_compacted=include_compacted,
    )
    all_branch_msgs = (
        messages
        if include_compacted
        else get_branch_export_messages(companion_id, branch_id, include_compacted=True)
    )
    raw_first_mes = str(companion.get("first_mes") or "")
    alt_greetings = [
        g
        for g in (companion.get("alternate_greetings") or [])
        if isinstance(g, str) and g.strip()
    ]
    opening_text = (
        raw_first_mes
        if raw_first_mes.strip()
        else (alt_greetings[0] if alt_greetings else "")
    )
    has_compacted_rows = any(bool(m.get("is_compacted")) for m in all_branch_msgs)
    # Pebble renders the card's opening greeting on the empty stage without storing
    # a companion_messages row, so prepend it unless this branch already begins with it.
    if opening_text and (include_compacted or not has_compacted_rows):
        greeting_swipes = [opening_text] + [g for g in alt_greetings if g != opening_text]
        greeting_candidates = {s.strip() for s in greeting_swipes if s.strip()}
        first_row = all_branch_msgs[0] if all_branch_msgs else None
        already_present = bool(
            first_row
            and str(first_row.get("role") or "") == "assistant"
            and (
                bool((first_row.get("metadata") or {}).get("is_greeting"))
                or str(first_row.get("content") or "").strip() in greeting_candidates
            )
        )
        if not already_present:
            messages = [
                {
                    "id": 0,
                    "role": "assistant",
                    "content": opening_text,
                    "swipes": greeting_swipes,
                    "swipe_id": 0,
                    "created_at": str(branch.get("created_at") or companion.get("created_at") or ""),
                    "message_type": "chat",
                    "metadata": {"is_greeting": True},
                    "is_important": False,
                    "is_compacted": False,
                    "compaction_batch_id": "",
                },
                *messages,
            ]
    compaction_history = get_compaction_history(companion_id, branch_id, limit=500)
    scene_summaries = list_scene_summaries(companion_id, branch_id)
    memories = list_memories(companion_id, branch_id)
    vars_info = get_branch_variables(companion_id, branch_id)
    variables = vars_info.get("variables") or {}
    hud_info = get_branch_hud(companion_id, branch_id)
    hud = hud_info.get("hud") or {}
    assets = get_companion_assets(companion_id)
    asset_names = [str(a["name"]) for a in assets if a.get("name")]
    presentation = extract_card_presentation(
        companion.get("raw_card_data"),
        companion.get("card_ui"),
    )
    display_regex = presentation.get("display_regex") or []

    last_idx = max(0, len(messages) - 1)
    last_msg_raw = str(messages[last_idx].get("content") or "") if messages else ""
    last_ai = ""
    last_user = ""
    for msg in reversed(messages):
        mtype = str(msg.get("message_type") or "chat")
        if mtype == "compaction_summary":
            continue
        if not last_ai and msg.get("role") == "assistant":
            last_ai = str(msg.get("content") or "")
        if not last_user and msg.get("role") == "user" and mtype != "narration":
            last_user = str(msg.get("content") or "")
        if last_ai and last_user:
            break

    seed = f"{companion_id}:{branch_id}"
    for idx, msg in enumerate(messages):
        mtype = str(msg.get("message_type") or "chat")
        if mtype == "compaction_summary":
            meta = msg.get("metadata") or {}
            msg["resolved_content"] = str(meta.get("summary") or msg.get("content") or "")
            continue
        is_user = msg.get("role") == "user" and mtype != "narration"
        is_ai = msg.get("role") == "assistant"
        msg["resolved_content"] = _resolve_message_text(
            str(msg.get("content") or ""),
            is_user=is_user,
            is_ai=is_ai,
            ctx={
                "char_name": char_name,
                "user_name": user_name,
                "variables": variables,
                "hud": hud,
                "seed": seed,
                "chat_index": idx,
                "last_message_id": last_idx,
                "last_message": last_msg_raw,
                "last_char_message": last_ai,
                "last_user_message": last_user,
            },
            display_regex=display_regex,
            asset_names=asset_names,
        )

    compacted_count = sum(1 for m in messages if m.get("is_compacted"))
    return {
        "companion_id": companion_id,
        "branch_id": branch_id,
        "companion": companion,
        "branch": branch,
        "char_name": char_name,
        "user_name": user_name,
        "branch_label": branch_label,
        "include_compacted": include_compacted,
        "exported_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "messages": messages,
        "message_count": len(messages),
        "compacted_count": compacted_count,
        "compaction_history": compaction_history,
        "scene_summaries": scene_summaries,
        "memories": memories,
        "variables": variables,
        "hud": hud,
        "assets": assets,
        "presentation": presentation,
    }


def export_branch_chat(
    companion_id: str,
    branch_id: str = "main",
    fmt: str = "md",
    include_compacted: bool = True,
) -> Dict[str, Any]:
    """Export one companion branch to DATA_ROOT/outputchat and return file metadata."""
    normalized_fmt = (fmt or "").strip().lower()
    if normalized_fmt not in VALID_EXPORT_FORMATS:
        raise ValueError(f"Unsupported export format: {fmt}")

    ctx = _load_branch_export_context(companion_id, branch_id, include_compacted)
    resolver = _AssetResolver(ctx["assets"])
    timeline = _build_timeline(ctx["messages"], ctx["scene_summaries"])

    if normalized_fmt == "md":
        payload_text = _render_markdown_export(ctx, timeline, resolver)
    elif normalized_fmt == "json":
        payload_text = _render_pebble_json_export(ctx)
    elif normalized_fmt == "html":
        payload_text = _render_html_export(ctx, timeline, resolver)
    else:
        payload_text = _render_sillytavern_jsonl_export(ctx)

    target = _next_output_path(ctx["char_name"], ctx["branch_label"], normalized_fmt)
    paths.assert_safe_data_path(target)
    payload_bytes = payload_text.encode("utf-8")
    target.write_bytes(payload_bytes)

    return {
        "status": "success",
        "format": normalized_fmt,
        "file_path": str(target),
        "output_dir": str(target.parent),
        "filename": target.name,
        "companion_id": companion_id,
        "companion_name": ctx["char_name"],
        "branch_id": branch_id,
        "branch_name": ctx["branch_label"],
        "include_compacted": include_compacted,
        "message_count": ctx["message_count"],
        "compacted_count": ctx["compacted_count"],
        "size_bytes": len(payload_bytes),
    }

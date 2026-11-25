"""Contextual Binding Substitutions (CBS) and variable macro evaluator.

Supports standard character-card macros (``{{char}}``, ``{{user}}``), RisuAI /
CCv3 variable macros (``{{getvar::k}}``, ``{{setvar::k::v}}``, ``{{addvar::k::n}}``),
safe arithmetic/comparison expressions (``{{? expr}}``, ``{{calc::expr}}``),
nested conditionals (``{{#if cond}}...{{/if}}``, ``{{#if_pure cond}}...{{/if_pure}}``,
``{{:else}}``), selection (``{{random::...}}``, ``{{pick::...}}``), and comments
(``{{// ...}}``).

Unknown ``{{...}}`` macros are preserved untouched so cards targeting other
frontends never lose text silently.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import random
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

_CHARACTER_ALIASES = frozenset({"char", "character"})
_USER_ALIASES = frozenset({"user", "persona"})
_BLOCK_TAGS = frozenset({"if", "if_pure", "each", "when"})
_MAX_RECURSION_DEPTH = 16


@dataclass
class _SpreadList:
    """Marker wrapper for ``{{spread::...}}`` results inside ``::`` macro args."""

    items: List[str]


def _is_truthy(value: str) -> bool:
    s = (value or "").strip().lower()
    if not s or s in ("0", "false", "null", "undefined", "none", "nan"):
        return False
    return True


def _match_block_open_kind(inner: str) -> Optional[str]:
    low = inner.strip().lower()
    if not low.startswith("#"):
        return None
    for kind in ("if_pure", "if", "each", "when"):
        prefix = f"#{kind}"
        if low.startswith(prefix) and (
            len(low) == len(prefix) or low[len(prefix)] == ":" or low[len(prefix)].isspace()
        ):
            return kind
    return None


def _match_block_close_kind(inner: str) -> Optional[str]:
    low = inner.strip().lower()
    if not low.startswith("/"):
        return None
    tag = low[1:]
    return tag if tag in _BLOCK_TAGS else None


def _split_top_level(text: str, sep: str = "::") -> List[str]:
    """Split *text* by *sep* only at brace depth 0 and block depth 0."""
    parts: List[str] = []
    brace_depth = 0
    block_stack: List[str] = []
    start = 0
    i = 0
    n = len(text)
    sep_len = len(sep)
    while i < n:
        if text.startswith("{{", i):
            if brace_depth == 0:
                end = _find_matching_close(text, i)
                if end is not None:
                    inner = text[i + 2 : end - 2]
                    open_kind = _match_block_open_kind(inner)
                    if open_kind is not None:
                        block_stack.append(open_kind)
                        i = end
                        continue
                    close_kind = _match_block_close_kind(inner)
                    if close_kind is not None and block_stack and block_stack[-1] == close_kind:
                        block_stack.pop()
                        i = end
                        continue
            brace_depth += 1
            i += 2
            continue
        if text.startswith("}}", i) and brace_depth > 0:
            brace_depth -= 1
            i += 2
            continue
        if brace_depth == 0 and not block_stack and text.startswith(sep, i):
            parts.append(text[start:i])
            i += sep_len
            start = i
            continue
        i += 1
    parts.append(text[start:])
    return parts


def _find_matching_close(text: str, start: int) -> Optional[int]:
    """Return index right after the matching ``}}`` for ``{{`` at *start*."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        if text.startswith("{{", i):
            depth += 1
            i += 2
            continue
        if text.startswith("}}", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        i += 1
    return None


# ==================== Safe Expression Evaluator ====================

_EXPR_TOKEN_RE = re.compile(
    r"""\s*(?:
        (==|!=|>=|<=|&&|\|\|)
      | ([()+\-*/%<>!])
      | ("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
      | (\d+(?:\.\d+)?)
      | ([A-Za-z_][A-Za-z0-9_.\-]*)
    )""",
    re.VERBOSE,
)


def _tokenize_expr(expr: str) -> Optional[List[Tuple[str, str]]]:
    tokens: List[Tuple[str, str]] = []
    pos = 0
    n = len(expr)
    while pos < n:
        if expr[pos].isspace():
            pos += 1
            continue
        m = _EXPR_TOKEN_RE.match(expr, pos)
        if not m:
            return None
        pos = m.end()
        if m.group(1):
            tokens.append(("OP", m.group(1)))
        elif m.group(2):
            tokens.append(("OP", m.group(2)))
        elif m.group(3):
            raw = m.group(3)
            tokens.append(("STR", raw[1:-1]))
        elif m.group(4):
            tokens.append(("NUM", m.group(4)))
        elif m.group(5):
            tokens.append(("IDENT", m.group(5)))
    return tokens


def _to_number(val: object) -> Optional[float]:
    if isinstance(val, bool):
        return 1.0 if val else 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return 0.0
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _to_bool(val: object) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return _is_truthy(val)
    return bool(val)


def _format_expr_result(val: object) -> str:
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, float):
        if val != val or val in (float("inf"), float("-inf")):
            return "0"
        if val.is_integer():
            return str(int(val))
        return f"{val:.6f}".rstrip("0").rstrip(".")
    if isinstance(val, int):
        return str(val)
    return str(val)


class _ExprParser:
    def __init__(self, tokens: List[Tuple[str, str]], variables: Dict[str, str]):
        self.tokens = tokens
        self.pos = 0
        self.variables = variables

    def peek(self) -> Optional[Tuple[str, str]]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def consume(self) -> Tuple[str, str]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse(self) -> object:
        val = self.parse_or()
        if self.pos < len(self.tokens):
            raise ValueError("unexpected trailing token")
        return val

    def parse_or(self) -> object:
        left = self.parse_and()
        while (tok := self.peek()) and tok == ("OP", "||"):
            self.consume()
            right = self.parse_and()
            left = _to_bool(left) or _to_bool(right)
        return left

    def parse_and(self) -> object:
        left = self.parse_eq()
        while (tok := self.peek()) and tok == ("OP", "&&"):
            self.consume()
            right = self.parse_eq()
            left = _to_bool(left) and _to_bool(right)
        return left

    def parse_eq(self) -> object:
        left = self.parse_rel()
        while (tok := self.peek()) and tok[0] == "OP" and tok[1] in ("==", "!="):
            op = self.consume()[1]
            right = self.parse_rel()
            ln, rn = _to_number(left), _to_number(right)
            if ln is not None and rn is not None:
                eq = ln == rn
            else:
                eq = str(left) == str(right)
            left = eq if op == "==" else not eq
        return left

    def parse_rel(self) -> object:
        left = self.parse_add()
        while (tok := self.peek()) and tok[0] == "OP" and tok[1] in (">", "<", ">=", "<="):
            op = self.consume()[1]
            right = self.parse_add()
            ln, rn = _to_number(left), _to_number(right)
            if ln is None or rn is None:
                ls, rs = str(left), str(right)
                left = (
                    ls > rs if op == ">" else
                    ls < rs if op == "<" else
                    ls >= rs if op == ">=" else
                    ls <= rs
                )
            else:
                left = (
                    ln > rn if op == ">" else
                    ln < rn if op == "<" else
                    ln >= rn if op == ">=" else
                    ln <= rn
                )
        return left

    def parse_add(self) -> object:
        left = self.parse_mul()
        while (tok := self.peek()) and tok[0] == "OP" and tok[1] in ("+", "-"):
            op = self.consume()[1]
            right = self.parse_mul()
            ln, rn = _to_number(left), _to_number(right)
            if ln is None or rn is None:
                left = f"{left}{right}" if op == "+" else 0.0
            else:
                left = ln + rn if op == "+" else ln - rn
        return left

    def parse_mul(self) -> object:
        left = self.parse_unary()
        while (tok := self.peek()) and tok[0] == "OP" and tok[1] in ("*", "/", "%"):
            op = self.consume()[1]
            right = self.parse_unary()
            ln = _to_number(left) or 0.0
            rn = _to_number(right) or 0.0
            if op == "*":
                left = ln * rn
            elif op == "/":
                left = ln / rn if rn != 0 else 0.0
            else:
                left = ln % rn if rn != 0 else 0.0
        return left

    def parse_unary(self) -> object:
        tok = self.peek()
        if tok == ("OP", "!"):
            self.consume()
            return not _to_bool(self.parse_unary())
        if tok == ("OP", "-"):
            self.consume()
            return -(_to_number(self.parse_unary()) or 0.0)
        if tok == ("OP", "+"):
            self.consume()
            return _to_number(self.parse_unary()) or 0.0
        return self.parse_primary()

    def parse_primary(self) -> object:
        tok = self.peek()
        if not tok:
            raise ValueError("unexpected end of expression")
        kind, val = self.consume()
        if kind == "NUM":
            return float(val)
        if kind == "STR":
            return val
        if kind == "IDENT":
            low = val.lower()
            if low == "true":
                return True
            if low == "false":
                return False
            if low in ("null", "none", "undefined"):
                return ""
            if val in self.variables:
                return self.variables[val]
            return val
        if (kind, val) == ("OP", "("):
            inner = self.parse_or()
            if self.peek() != ("OP", ")"):
                raise ValueError("missing closing paren")
            self.consume()
            return inner
        raise ValueError(f"unexpected token {val!r}")


def _eval_expr(expr: str, variables: Dict[str, str]) -> Optional[str]:
    tokens = _tokenize_expr(expr.strip())
    if not tokens:
        return None
    try:
        res = _ExprParser(tokens, variables).parse()
    except ValueError:
        return None
    return _format_expr_result(res)


# ==================== Block & Array Helpers ====================

_BLOCK_OPEN_RE = re.compile(
    r"^\{\{\s*#(if_pure|if|each|when)(?=[\s:]|$)([\s\S]*?)\}\}$",
    re.IGNORECASE,
)
_ARRAY_SPACE_RE = re.compile(r"^(?:array|makearray)(?:\s|$)", re.IGNORECASE)


@dataclass
class _CbsState:
    char_name: str
    user_name: str
    variables: Dict[str, str]
    allow_mutation: bool
    seed: str
    temp_vars: Dict[str, str]
    slot_vars: Dict[str, str]
    asset_names: List[str]
    chat_index: int = 0
    last_message_id: int = 0
    last_message: str = ""
    last_char_message: str = ""
    last_user_message: str = ""
    idle_duration: str = ""
    idle_minutes: int = 0


def _find_block_clauses(
    text: str,
    body_start: int,
    tag_kind: str,
) -> Optional[Tuple[int, int, Optional[int]]]:
    """Scan from *body_start* for matching closing block tag.

    Tracks nested ``if`` / ``if_pure`` / ``each`` / ``when`` blocks on a stack
    so an inner ``{{:else}}`` never shadows an outer conditional's branch.
    Returns ``(close_start, close_end, else_span_start)`` or None if unmatched.
    """
    stack: List[str] = [tag_kind]
    else_start: Optional[int] = None
    i = body_start
    n = len(text)

    while i < n:
        if not text.startswith("{{", i):
            i += 1
            continue
        end = _find_matching_close(text, i)
        if end is None:
            return None
        inner = text[i + 2 : end - 2].strip()
        open_kind = _match_block_open_kind(inner)
        if open_kind is not None:
            stack.append(open_kind)
        else:
            close_kind = _match_block_close_kind(inner)
            if close_kind is not None and stack and stack[-1] == close_kind:
                stack.pop()
                if not stack:
                    return i, end, else_start
            elif (
                len(stack) == 1
                and tag_kind != "each"
                and else_start is None
                and inner.lower() in (":else", "#else")
            ):
                else_start = i
        i = end
    return None


def _trim_if_block_edges(branch_text: str) -> str:
    if branch_text.startswith("\r\n"):
        branch_text = branch_text[2:]
    elif branch_text.startswith("\n"):
        branch_text = branch_text[1:]
    if branch_text.endswith("\r\n"):
        branch_text = branch_text[:-2]
    elif branch_text.endswith("\n"):
        branch_text = branch_text[:-1]
    return branch_text


def _parse_array_items(raw: str) -> List[str]:
    s = (raw or "").strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                out: List[str] = []
                for item in parsed:
                    if item is None:
                        out.append("")
                    elif isinstance(item, str):
                        out.append(item)
                    elif isinstance(item, bool):
                        out.append("1" if item else "0")
                    elif isinstance(item, (dict, list)):
                        out.append(json.dumps(item, ensure_ascii=False))
                    else:
                        out.append(str(item))
                return out
        except (ValueError, TypeError):
            pass
    if "::" in s:
        return [p for p in s.split("::") if p != ""]
    return [s]


def _split_top_level_whitespace(text: str) -> List[str]:
    """Split *text* on whitespace only at brace depth 0."""
    tokens: List[str] = []
    depth = 0
    start: Optional[int] = None
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("{{", i):
            if start is None:
                start = i
            depth += 1
            i += 2
            continue
        if text.startswith("}}", i) and depth > 0:
            depth -= 1
            i += 2
            continue
        if depth == 0 and text[i].isspace():
            if start is not None:
                tokens.append(text[start:i])
                start = None
            i += 1
            continue
        if start is None:
            start = i
        i += 1
    if start is not None:
        tokens.append(text[start:])
    return tokens


def _parse_each_header(header_raw: str) -> Optional[Tuple[str, str]]:
    tokens = _split_top_level_whitespace(header_raw)
    if len(tokens) < 2:
        return None
    var_name = tokens[-1].strip()
    if not var_name:
        return None
    if len(tokens) >= 3 and tokens[-2].lower() == "as":
        expr_tokens = tokens[:-2]
    else:
        expr_tokens = tokens[:-1]
    expr = " ".join(expr_tokens).strip()
    return (expr, var_name) if expr else None


def _compare_values(left: str, right: str, op: str) -> bool:
    ln, rn = _to_number(left), _to_number(right)
    if op in ("is", "eq", "=="):
        return left == right
    if op in ("isnot", "neq", "!="):
        return left != right
    if op in (">", "gt"):
        return (ln > rn) if (ln is not None and rn is not None) else (left > right)
    if op in ("<", "lt"):
        return (ln < rn) if (ln is not None and rn is not None) else (left < right)
    if op in (">=", "gte"):
        return (ln >= rn) if (ln is not None and rn is not None) else (left >= right)
    if op in ("<=", "lte"):
        return (ln <= rn) if (ln is not None and rn is not None) else (left <= right)
    if op in ("in", "notin"):
        items = _parse_array_items(right) if right.strip().startswith("[") else None
        contained = (left in items) if items is not None else (left in right)
        return contained if op == "in" else not contained
    return False


def _eval_when_segment(seg: List[str], state: _CbsState) -> bool:
    if not seg:
        return False
    head_low = seg[0].strip().lower()
    if head_low == "not" and len(seg) >= 2:
        return not _eval_when_segment(seg[1:], state)
    prefix_ops = {
        "equal": "==",
        "not_equal": "!=",
        "notequal": "!=",
        "greater": ">",
        "less": "<",
        "greater_equal": ">=",
        "greaterequal": ">=",
        "less_equal": "<=",
        "lessequal": "<=",
    }
    if head_low in prefix_ops and len(seg) >= 3:
        return _compare_values(seg[1], seg[2], prefix_ops[head_low])
    if len(seg) == 1:
        return _is_truthy(seg[0])
    if len(seg) == 2:
        if head_low in ("toggle", "var"):
            return _is_truthy(str(state.variables.get(seg[1].strip(), "")))
        if head_low == "tempvar":
            return _is_truthy(str(state.temp_vars.get(seg[1].strip(), "")))
        return _is_truthy(seg[0])
    left, op, right = seg[0], seg[1].strip().lower(), seg[2]
    if op == "vis":
        return str(state.variables.get(left.strip(), "")) == right
    if op == "visnot":
        return str(state.variables.get(left.strip(), "")) != right
    if op == "tis":
        return str(state.temp_vars.get(left.strip(), "")) == right
    if op == "tisnot":
        return str(state.temp_vars.get(left.strip(), "")) != right
    return _compare_values(left, right, op)


def _eval_when_condition(header_raw: str, state: _CbsState, depth: int) -> Tuple[bool, bool]:
    """Evaluate ``{{#when ...}}`` header; returns ``(truthy, keep_whitespace)``."""
    s = header_raw.strip()
    if not s:
        return False, False
    if "::" not in s:
        return _is_truthy(_eval_with_state(s, state, depth + 1)), False

    if s.startswith("::"):
        s = s[2:]
    raw_parts = _split_top_level(s, "::")
    parts = [_eval_with_state(p, state, depth + 1) for p in raw_parts]
    keep_ws = False
    if len(parts) > 1 and parts[0].strip().lower() == "keep":
        keep_ws = True
        parts = parts[1:]

    # Split by top-level `or` (lower precedence) then `and` (higher precedence).
    or_groups: List[List[str]] = [[]]
    for tok in parts:
        if tok.strip().lower() in ("or", "||"):
            or_groups.append([])
        else:
            or_groups[-1].append(tok)

    for group in or_groups:
        and_segs: List[List[str]] = [[]]
        for tok in group:
            if tok.strip().lower() in ("and", "&&"):
                and_segs.append([])
            else:
                and_segs[-1].append(tok)
        if and_segs and all(_eval_when_segment(seg, state) for seg in and_segs):
            return True, keep_ws
    return False, keep_ws


def _evaluate_macro_value(
    raw_macro: str,
    inner: str,
    state: _CbsState,
    depth: int,
) -> object:
    stripped = inner.strip()
    if not stripped:
        return raw_macro

    # Comments: {{// ...}}
    if stripped.startswith("//"):
        return ""

    # Expression macros: {{? expr}}
    if stripped.startswith("?"):
        expr_raw = stripped[1:]
        expr_eval = _eval_with_state(expr_raw, state, depth + 1)
        merged_vars = {**state.variables, **state.temp_vars}
        res = _eval_expr(expr_eval, merged_vars)
        return res if res is not None else raw_macro

    # Block-body array/makearray without top-level `::`, e.g. `{{array\n{{#each ...}}...{{/each}}\n}}`
    if _ARRAY_SPACE_RE.match(stripped):
        top_parts = _split_top_level(stripped, "::")
        if len(top_parts) == 1:
            cmd_len = len(stripped.split(None, 1)[0])
            body_eval = _eval_with_state(stripped[cmd_len:], state, depth + 1)
            if not body_eval.strip():
                return "[]"
            raw_items = body_eval.split("::")
            if len(raw_items) > 1 and not raw_items[0].strip():
                raw_items = raw_items[1:]
            return json.dumps([item.strip() for item in raw_items], ensure_ascii=False)

    # Colon-separated CBS macros: {{cmd::arg1::arg2}}
    parts = _split_top_level(stripped, "::")
    if len(parts) > 1:
        cmd = parts[0].strip().lower()
        args: List[str] = []
        for p in parts[1:]:
            p_strip = p.strip()
            if (
                p_strip.startswith("{{")
                and p_strip.endswith("}}")
                and _find_matching_close(p_strip, 0) == len(p_strip)
            ):
                val = _evaluate_macro_value(p_strip, p_strip[2:-2], state, depth + 1)
                if isinstance(val, _SpreadList):
                    args.extend(val.items)
                else:
                    args.append(str(val))
            else:
                args.append(_eval_with_state(p, state, depth + 1))

        if cmd in ("getvar", "getglobalvar") and args:
            return str(state.variables.get(args[0].strip(), ""))
        if cmd == "setvar" and len(args) >= 2:
            if state.allow_mutation:
                state.variables[args[0].strip()] = args[1]
            return ""
        if cmd == "addvar" and len(args) >= 2:
            if state.allow_mutation:
                key = args[0].strip()
                cur = _to_number(state.variables.get(key, "0")) or 0.0
                delta = _to_number(args[1]) or 0.0
                state.variables[key] = _format_expr_result(cur + delta)
            return ""
        if cmd in ("gettempvar", "tempvar") and args:
            return str(state.temp_vars.get(args[0].strip(), ""))
        if cmd == "settempvar" and len(args) >= 2:
            state.temp_vars[args[0].strip()] = args[1]
            return ""
        if cmd == "addtempvar" and len(args) >= 2:
            key = args[0].strip()
            cur = _to_number(state.temp_vars.get(key, "0")) or 0.0
            delta = _to_number(args[1]) or 0.0
            state.temp_vars[key] = _format_expr_result(cur + delta)
            return ""
        if cmd == "slot" and args:
            return str(state.slot_vars.get(args[0].strip(), ""))
        if cmd in ("array", "makearray"):
            return json.dumps(args, ensure_ascii=False)
        if cmd == "spread":
            return _SpreadList(_parse_array_items(args[0]) if args else [])
        if cmd == "equal" and len(args) >= 2:
            return "1" if args[0] == args[1] else "0"
        if cmd in ("not_equal", "notequal") and len(args) >= 2:
            return "1" if args[0] != args[1] else "0"
        if cmd == "greater" and len(args) >= 2:
            return "1" if _compare_values(args[0], args[1], ">") else "0"
        if cmd == "less" and len(args) >= 2:
            return "1" if _compare_values(args[0], args[1], "<") else "0"
        if cmd in ("greater_equal", "greaterequal") and len(args) >= 2:
            return "1" if _compare_values(args[0], args[1], ">=") else "0"
        if cmd in ("less_equal", "lessequal") and len(args) >= 2:
            return "1" if _compare_values(args[0], args[1], "<=") else "0"
        if cmd == "and" and len(args) >= 2:
            return "1" if (_is_truthy(args[0]) and _is_truthy(args[1])) else "0"
        if cmd == "or" and len(args) >= 2:
            return "1" if (_is_truthy(args[0]) or _is_truthy(args[1])) else "0"
        if cmd == "not" and args:
            return "1" if not _is_truthy(args[0]) else "0"
        if cmd == "replace" and len(args) >= 2:
            target_str = args[0]
            find_str = args[1]
            repl_str = args[2] if len(args) >= 3 else ""
            return target_str.replace(find_str, repl_str) if find_str else target_str
        if cmd == "lower" and args:
            return args[0].lower()
        if cmd == "upper" and args:
            return args[0].upper()
        if cmd == "trim" and args:
            return args[0].strip()
        if cmd in ("length", "array_length") and args:
            s = args[0].strip()
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return str(len(parsed))
                except (ValueError, TypeError):
                    pass
            return str(len(args[0]))
        if cmd == "contains" and len(args) >= 2:
            hay = args[0].strip()
            if hay.startswith("["):
                try:
                    parsed = json.loads(hay)
                    if isinstance(parsed, list):
                        return "1" if args[1] in _parse_array_items(hay) else "0"
                except (ValueError, TypeError):
                    pass
            return "1" if args[1] in args[0] else "0"
        if cmd == "startswith" and len(args) >= 2:
            return "1" if args[0].startswith(args[1]) else "0"
        if cmd == "endswith" and len(args) >= 2:
            return "1" if args[0].endswith(args[1]) else "0"
        if cmd == "split" and args:
            sep = args[1] if len(args) >= 2 else ""
            parts_out = args[0].split(sep) if sep else args[0].split()
            return json.dumps(parts_out, ensure_ascii=False)
        if cmd == "join" and args:
            sep = args[1] if len(args) >= 2 else ","
            return sep.join(_parse_array_items(args[0]))
        if cmd == "calc" and args:
            merged_vars = {**state.variables, **state.temp_vars}
            res = _eval_expr(args[0], merged_vars)
            return res if res is not None else raw_macro
        if cmd in ("random", "pick"):
            if not args:
                return ""
            choices = [a for a in args if a != ""] or args
            if cmd == "pick":
                digest = hashlib.sha256(f"{state.seed}:{stripped}".encode("utf-8")).digest()
                idx = int.from_bytes(digest[:4], "big") % len(choices)
                return choices[idx]
            return random.choice(choices)
        if cmd == "comment":
            return ""
        return raw_macro

    # Single-colon random: {{random:a,b,c}} or {{pick:a,b,c}}
    if ":" in stripped and not stripped.startswith("#"):
        head, rest = stripped.split(":", 1)
        cmd = head.strip().lower()
        if cmd in ("random", "pick"):
            opts = [o.strip() for o in rest.split(",")]
            if opts:
                if cmd == "pick":
                    digest = hashlib.sha256(f"{state.seed}:{stripped}".encode("utf-8")).digest()
                    return opts[int.from_bytes(digest[:4], "big") % len(opts)]
                return random.choice(opts)

    low = stripped.lower()
    if low in _CHARACTER_ALIASES:
        return state.char_name or "the character"
    if low in _USER_ALIASES:
        return state.user_name or "the user"
    if low in ("newline", "br"):
        return "\n"
    if low == "trim":
        return ""
    if low == "assetlist":
        return json.dumps(state.asset_names, ensure_ascii=False)
    if low in ("array", "makearray"):
        return "[]"
    if low == "slot":
        return str(state.slot_vars.get("", state.slot_vars.get("slot", "")))
    if low == "chat_index":
        return str(state.chat_index)
    if low == "lastmessageid":
        return str(state.last_message_id)
    if low == "lastmessage":
        return state.last_message
    if low == "lastcharmessage":
        return state.last_char_message
    if low == "lastusermessage":
        return state.last_user_message
    if low in ("idle_duration", "idleduration"):
        return state.idle_duration or "0m"
    if low in ("idle_minutes", "idleminutes"):
        return str(max(0, int(state.idle_minutes or 0)))
    if low == "hour":
        return str(datetime.datetime.now().hour)
    if low == "time":
        return datetime.datetime.now().strftime("%H:%M:%S")
    if low == "isotime":
        return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    if low == "date":
        return datetime.datetime.now().strftime("%Y-%m-%d")
    if low == "isodate":
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if low == "datetime":
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return raw_macro


def _evaluate_macro(
    raw_macro: str,
    inner: str,
    state: _CbsState,
    depth: int,
) -> str:
    val = _evaluate_macro_value(raw_macro, inner, state, depth)
    if isinstance(val, _SpreadList):
        return "::".join(val.items)
    return str(val)


def _eval_with_state(text: Optional[str], state: _CbsState, depth: int) -> str:
    if not text or "{{" not in text:
        return text or ""
    if depth >= _MAX_RECURSION_DEPTH:
        return text

    out: List[str] = []
    i = 0
    n = len(text)

    while i < n:
        idx = text.find("{{", i)
        if idx == -1:
            out.append(text[i:])
            break
        if idx > i:
            out.append(text[i:idx])

        end = _find_matching_close(text, idx)
        if end is None:
            out.append(text[idx:])
            break

        token = text[idx:end]
        open_match = _BLOCK_OPEN_RE.match(token)
        if open_match:
            tag_kind = open_match.group(1).lower()
            header_raw = open_match.group(2)
            clauses = _find_block_clauses(text, end, tag_kind)
            if clauses is not None:
                close_start, close_end, else_start = clauses
                if tag_kind == "each":
                    parsed = _parse_each_header(header_raw)
                    if parsed is not None:
                        expr, var_name = parsed
                        arr_eval = _eval_with_state(expr, state, depth + 1)
                        items = _parse_array_items(arr_eval)
                        body = text[end:close_start]
                        had_prev = var_name in state.slot_vars
                        prev_val = state.slot_vars.get(var_name, "")
                        try:
                            for item in items:
                                state.slot_vars[var_name] = item
                                out.append(_eval_with_state(body, state, depth + 1))
                        finally:
                            if had_prev:
                                state.slot_vars[var_name] = prev_val
                            else:
                                state.slot_vars.pop(var_name, None)
                        i = close_end
                        continue
                else:
                    if tag_kind == "when":
                        truthy, keep_ws = _eval_when_condition(header_raw, state, depth)
                    else:
                        cond_eval = _eval_with_state(header_raw.strip(), state, depth + 1)
                        truthy = _is_truthy(cond_eval)
                        keep_ws = tag_kind == "if_pure"

                    if else_start is not None:
                        else_end = _find_matching_close(text, else_start) or else_start
                        chosen = (
                            text[end:else_start]
                            if truthy
                            else text[else_end:close_start]
                        )
                    else:
                        chosen = text[end:close_start] if truthy else ""

                    if not keep_ws and chosen:
                        chosen = _trim_if_block_edges(chosen)

                    out.append(_eval_with_state(chosen, state, depth + 1))
                    i = close_end
                    continue

        inner = text[idx + 2 : end - 2]
        out.append(_evaluate_macro(token, inner, state, depth))
        i = end

    return "".join(out)


def evaluate_cbs(
    text: Optional[str],
    *,
    char_name: str = "",
    user_name: str = "the user",
    variables: Optional[Dict[str, str]] = None,
    allow_mutation: bool = False,
    seed: str = "",
    asset_names: Optional[Sequence[str]] = None,
    chat_index: int = 0,
    last_message_id: int = 0,
    last_message: str = "",
    last_char_message: str = "",
    last_user_message: str = "",
    idle_duration: str = "",
    idle_minutes: int = 0,
    _depth: int = 0,
) -> str:
    """Resolve CBS macros, variables, loops, and conditionals in *text*."""
    if not text or "{{" not in text:
        return text or ""
    if _depth >= _MAX_RECURSION_DEPTH:
        return text

    state = _CbsState(
        char_name=char_name,
        user_name=user_name,
        variables=variables if variables is not None else {},
        allow_mutation=allow_mutation,
        seed=seed,
        temp_vars={},
        slot_vars={},
        asset_names=[str(a) for a in (asset_names or ()) if str(a)],
        chat_index=chat_index,
        last_message_id=last_message_id,
        last_message=last_message,
        last_char_message=last_char_message,
        last_user_message=last_user_message,
        idle_duration=idle_duration,
        idle_minutes=idle_minutes,
    )
    return _eval_with_state(text, state, _depth)

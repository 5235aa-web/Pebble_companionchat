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

import hashlib
import random
import re
from typing import Dict, List, Optional, Tuple

_CHARACTER_ALIASES = frozenset({"char", "character"})
_USER_ALIASES = frozenset({"user", "persona"})
_MAX_RECURSION_DEPTH = 16


def _is_truthy(value: str) -> bool:
    s = (value or "").strip().lower()
    if not s or s in ("0", "false", "null", "undefined", "none", "nan"):
        return False
    return True


def _split_top_level(text: str, sep: str = "::") -> List[str]:
    """Split *text* by *sep* only at brace depth 0."""
    parts: List[str] = []
    depth = 0
    start = 0
    i = 0
    n = len(text)
    sep_len = len(sep)
    while i < n:
        if text.startswith("{{", i):
            depth += 1
            i += 2
            continue
        if text.startswith("}}", i) and depth > 0:
            depth -= 1
            i += 2
            continue
        if depth == 0 and text.startswith(sep, i):
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


# ==================== Block Conditional Parsing ====================

_IF_OPEN_RE = re.compile(r"^\{\{\s*#(if_pure|if)\b([\s\S]*?)\}\}$", re.IGNORECASE)


def _find_conditional_clauses(
    text: str,
    body_start: int,
    tag_kind: str,
) -> Optional[Tuple[int, int, Optional[int]]]:
    """Scan from *body_start* for matching ``{{/if}}`` or ``{{/if_pure}}``.

    Returns ``(close_start, close_end, else_span_start)`` or None if unmatched.
    """
    depth = 1
    else_start: Optional[int] = None
    i = body_start
    n = len(text)
    close_tag = f"/{tag_kind}"

    while i < n:
        if not text.startswith("{{", i):
            i += 1
            continue
        end = _find_matching_close(text, i)
        if end is None:
            return None
        inner = text[i + 2 : end - 2].strip()
        low = inner.lower()
        if low.startswith(f"#{tag_kind}") and (
            len(low) == len(tag_kind) + 1 or low[len(tag_kind) + 1].isspace()
        ):
            depth += 1
        elif low == close_tag:
            depth -= 1
            if depth == 0:
                return i, end, else_start
        elif depth == 1 and else_start is None and low in (":else", "#else"):
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


def _evaluate_macro(
    raw_macro: str,
    inner: str,
    char_name: str,
    user_name: str,
    variables: Dict[str, str],
    allow_mutation: bool,
    seed: str,
    depth: int,
) -> str:
    stripped = inner.strip()
    if not stripped:
        return raw_macro

    # Comments: {{// ...}}
    if stripped.startswith("//"):
        return ""

    # Expression macros: {{? expr}}
    if stripped.startswith("?"):
        expr_raw = stripped[1:]
        expr_eval = evaluate_cbs(
            expr_raw,
            char_name=char_name,
            user_name=user_name,
            variables=variables,
            allow_mutation=allow_mutation,
            seed=seed,
            _depth=depth + 1,
        )
        res = _eval_expr(expr_eval, variables)
        return res if res is not None else raw_macro

    # Colon-separated CBS macros: {{cmd::arg1::arg2}}
    if "::" in stripped:
        parts = _split_top_level(stripped, "::")
        cmd = parts[0].strip().lower()
        args = [
            evaluate_cbs(
                p,
                char_name=char_name,
                user_name=user_name,
                variables=variables,
                allow_mutation=allow_mutation,
                seed=seed,
                _depth=depth + 1,
            )
            for p in parts[1:]
        ]

        if cmd in ("getvar", "getglobalvar") and args:
            key = args[0].strip()
            return str(variables.get(key, ""))
        if cmd == "setvar" and len(args) >= 2:
            if allow_mutation:
                variables[args[0].strip()] = args[1]
            return ""
        if cmd == "addvar" and len(args) >= 2:
            if allow_mutation:
                key = args[0].strip()
                cur = _to_number(variables.get(key, "0")) or 0.0
                delta = _to_number(args[1]) or 0.0
                variables[key] = _format_expr_result(cur + delta)
            return ""
        if cmd == "calc" and args:
            res = _eval_expr(args[0], variables)
            return res if res is not None else raw_macro
        if cmd in ("random", "pick") and args:
            choices = [a for a in args if a != ""] or args
            if cmd == "pick":
                digest = hashlib.sha256(f"{seed}:{stripped}".encode("utf-8")).digest()
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
                    digest = hashlib.sha256(f"{seed}:{stripped}".encode("utf-8")).digest()
                    return opts[int.from_bytes(digest[:4], "big") % len(opts)]
                return random.choice(opts)

    low = stripped.lower()
    if low in _CHARACTER_ALIASES:
        return char_name or "the character"
    if low in _USER_ALIASES:
        return user_name or "the user"
    if low in ("newline", "br"):
        return "\n"
    if low == "trim":
        return ""
    return raw_macro


def evaluate_cbs(
    text: Optional[str],
    *,
    char_name: str = "",
    user_name: str = "the user",
    variables: Optional[Dict[str, str]] = None,
    allow_mutation: bool = False,
    seed: str = "",
    _depth: int = 0,
) -> str:
    """Resolve CBS macros, variables, and conditionals in *text*."""
    if not text or "{{" not in text:
        return text or ""
    if _depth >= _MAX_RECURSION_DEPTH:
        return text

    vars_map = variables if variables is not None else {}
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
        open_match = _IF_OPEN_RE.match(token)
        if open_match:
            tag_kind = open_match.group(1).lower()
            cond_raw = open_match.group(2).strip()
            clauses = _find_conditional_clauses(text, end, tag_kind)
            if clauses is not None:
                close_start, close_end, else_start = clauses
                cond_eval = evaluate_cbs(
                    cond_raw,
                    char_name=char_name,
                    user_name=user_name,
                    variables=vars_map,
                    allow_mutation=allow_mutation,
                    seed=seed,
                    _depth=_depth + 1,
                )
                truthy = _is_truthy(cond_eval)
                if else_start is not None:
                    else_end = _find_matching_close(text, else_start) or else_start
                    chosen = (
                        text[end:else_start]
                        if truthy
                        else text[else_end:close_start]
                    )
                else:
                    chosen = text[end:close_start] if truthy else ""

                if tag_kind == "if" and chosen:
                    chosen = _trim_if_block_edges(chosen)

                out.append(
                    evaluate_cbs(
                        chosen,
                        char_name=char_name,
                        user_name=user_name,
                        variables=vars_map,
                        allow_mutation=allow_mutation,
                        seed=seed,
                        _depth=_depth + 1,
                    )
                )
                i = close_end
                continue

        inner = text[idx + 2 : end - 2]
        out.append(
            _evaluate_macro(
                token,
                inner,
                char_name,
                user_name,
                vars_map,
                allow_mutation,
                seed,
                _depth,
            )
        )
        i = end

    return "".join(out)

/**
 * Contextual Binding Substitutions (CBS) and variable macro evaluator.
 *
 * Mirrors `backend/cbs.py` so greetings, displayed messages, and regex
 * replacements evaluate identical conditionals, variables, and expressions.
 */

const CHARACTER_ALIASES = new Set(['char', 'character'])
const USER_ALIASES = new Set(['user', 'persona'])
const MAX_RECURSION_DEPTH = 16

export interface CbsOptions {
  charName?: string | null
  userName?: string | null
  variables?: Record<string, string>
  seed?: string
}

function isTruthy(value: string): boolean {
  const s = (value || '').trim().toLowerCase()
  if (!s || s === '0' || s === 'false' || s === 'null' || s === 'undefined' || s === 'none' || s === 'nan') {
    return false
  }
  return true
}

function findMatchingClose(text: string, start: number): number | null {
  let depth = 0
  let i = start
  const n = text.length
  while (i < n) {
    if (text.startsWith('{{', i)) {
      depth += 1
      i += 2
      continue
    }
    if (text.startsWith('}}', i)) {
      depth -= 1
      i += 2
      if (depth === 0) return i
      continue
    }
    i += 1
  }
  return null
}

function splitTopLevel(text: string, sep = '::'): string[] {
  const parts: string[] = []
  let depth = 0
  let start = 0
  let i = 0
  const n = text.length
  while (i < n) {
    if (text.startsWith('{{', i)) {
      depth += 1
      i += 2
      continue
    }
    if (text.startsWith('}}', i) && depth > 0) {
      depth -= 1
      i += 2
      continue
    }
    if (depth === 0 && text.startsWith(sep, i)) {
      parts.push(text.slice(start, i))
      i += sep.length
      start = i
      continue
    }
    i += 1
  }
  parts.push(text.slice(start))
  return parts
}

type ExprToken = { kind: 'OP' | 'STR' | 'NUM' | 'IDENT'; val: string }

const EXPR_TOKEN_RE =
  /\s*(?:(==|!=|>=|<=|&&|\|\|)|([()+\-*/%<>!])|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z0-9_.-]*))/y

function tokenizeExpr(expr: string): ExprToken[] | null {
  const tokens: ExprToken[] = []
  let pos = 0
  const n = expr.length
  while (pos < n) {
    if (/\s/.test(expr[pos])) {
      pos += 1
      continue
    }
    EXPR_TOKEN_RE.lastIndex = pos
    const m = EXPR_TOKEN_RE.exec(expr)
    if (!m) return null
    pos = EXPR_TOKEN_RE.lastIndex
    if (m[1]) tokens.push({ kind: 'OP', val: m[1] })
    else if (m[2]) tokens.push({ kind: 'OP', val: m[2] })
    else if (m[3]) tokens.push({ kind: 'STR', val: m[3].slice(1, -1) })
    else if (m[4]) tokens.push({ kind: 'NUM', val: m[4] })
    else if (m[5]) tokens.push({ kind: 'IDENT', val: m[5] })
  }
  return tokens
}

function toNumber(val: unknown): number | null {
  if (typeof val === 'boolean') return val ? 1 : 0
  if (typeof val === 'number') return Number.isFinite(val) ? val : 0
  if (typeof val === 'string') {
    const s = val.trim()
    if (!s) return 0
    const num = Number(s)
    return Number.isNaN(num) ? null : num
  }
  return null
}

function toBool(val: unknown): boolean {
  if (typeof val === 'boolean') return val
  if (typeof val === 'number') return val !== 0
  if (typeof val === 'string') return isTruthy(val)
  return Boolean(val)
}

function formatExprResult(val: unknown): string {
  if (typeof val === 'boolean') return val ? '1' : '0'
  if (typeof val === 'number') {
    if (!Number.isFinite(val)) return '0'
    if (Number.isInteger(val)) return String(val)
    return val.toFixed(6).replace(/\.?0+$/, '')
  }
  return String(val ?? '')
}

class ExprParser {
  private pos = 0
  constructor(
    private readonly tokens: ExprToken[],
    private readonly variables: Record<string, string>,
  ) {}

  private peek(): ExprToken | undefined {
    return this.tokens[this.pos]
  }

  private consume(): ExprToken {
    return this.tokens[this.pos++]
  }

  parse(): unknown {
    const val = this.parseOr()
    if (this.pos < this.tokens.length) throw new Error('unexpected trailing token')
    return val
  }

  private parseOr(): unknown {
    let left = this.parseAnd()
    while (this.peek()?.kind === 'OP' && this.peek()?.val === '||') {
      this.consume()
      const right = this.parseAnd()
      left = toBool(left) || toBool(right)
    }
    return left
  }

  private parseAnd(): unknown {
    let left = this.parseEq()
    while (this.peek()?.kind === 'OP' && this.peek()?.val === '&&') {
      this.consume()
      const right = this.parseEq()
      left = toBool(left) && toBool(right)
    }
    return left
  }

  private parseEq(): unknown {
    let left = this.parseRel()
    while (this.peek()?.kind === 'OP' && (this.peek()?.val === '==' || this.peek()?.val === '!=')) {
      const op = this.consume().val
      const right = this.parseRel()
      const ln = toNumber(left)
      const rn = toNumber(right)
      const eq = ln !== null && rn !== null ? ln === rn : String(left) === String(right)
      left = op === '==' ? eq : !eq
    }
    return left
  }

  private parseRel(): unknown {
    let left = this.parseAdd()
    while (
      this.peek()?.kind === 'OP' &&
      (this.peek()?.val === '>' ||
        this.peek()?.val === '<' ||
        this.peek()?.val === '>=' ||
        this.peek()?.val === '<=')
    ) {
      const op = this.consume().val
      const right = this.parseAdd()
      const ln = toNumber(left)
      const rn = toNumber(right)
      if (ln === null || rn === null) {
        const ls = String(left)
        const rs = String(right)
        left = op === '>' ? ls > rs : op === '<' ? ls < rs : op === '>=' ? ls >= rs : ls <= rs
      } else {
        left = op === '>' ? ln > rn : op === '<' ? ln < rn : op === '>=' ? ln >= rn : ln <= rn
      }
    }
    return left
  }

  private parseAdd(): unknown {
    let left = this.parseMul()
    while (this.peek()?.kind === 'OP' && (this.peek()?.val === '+' || this.peek()?.val === '-')) {
      const op = this.consume().val
      const right = this.parseMul()
      const ln = toNumber(left)
      const rn = toNumber(right)
      if (ln === null || rn === null) {
        left = op === '+' ? `${left}${right}` : 0
      } else {
        left = op === '+' ? ln + rn : ln - rn
      }
    }
    return left
  }

  private parseMul(): unknown {
    let left = this.parseUnary()
    while (
      this.peek()?.kind === 'OP' &&
      (this.peek()?.val === '*' || this.peek()?.val === '/' || this.peek()?.val === '%')
    ) {
      const op = this.consume().val
      const right = this.parseUnary()
      const ln = toNumber(left) ?? 0
      const rn = toNumber(right) ?? 0
      left = op === '*' ? ln * rn : op === '/' ? (rn !== 0 ? ln / rn : 0) : rn !== 0 ? ln % rn : 0
    }
    return left
  }

  private parseUnary(): unknown {
    const tok = this.peek()
    if (tok?.kind === 'OP' && tok.val === '!') {
      this.consume()
      return !toBool(this.parseUnary())
    }
    if (tok?.kind === 'OP' && tok.val === '-') {
      this.consume()
      return -(toNumber(this.parseUnary()) ?? 0)
    }
    if (tok?.kind === 'OP' && tok.val === '+') {
      this.consume()
      return toNumber(this.parseUnary()) ?? 0
    }
    return this.parsePrimary()
  }

  private parsePrimary(): unknown {
    const tok = this.peek()
    if (!tok) throw new Error('unexpected end')
    this.consume()
    if (tok.kind === 'NUM') return Number(tok.val)
    if (tok.kind === 'STR') return tok.val
    if (tok.kind === 'IDENT') {
      const low = tok.val.toLowerCase()
      if (low === 'true') return true
      if (low === 'false') return false
      if (low === 'null' || low === 'none' || low === 'undefined') return ''
      if (Object.prototype.hasOwnProperty.call(this.variables, tok.val)) {
        return this.variables[tok.val]
      }
      return tok.val
    }
    if (tok.kind === 'OP' && tok.val === '(') {
      const inner = this.parseOr()
      if (this.peek()?.kind !== 'OP' || this.peek()?.val !== ')') {
        throw new Error('missing )')
      }
      this.consume()
      return inner
    }
    throw new Error('unexpected token')
  }
}

function evalExpr(expr: string, variables: Record<string, string>): string | null {
  const tokens = tokenizeExpr(expr.trim())
  if (!tokens) return null
  try {
    return formatExprResult(new ExprParser(tokens, variables).parse())
  } catch {
    return null
  }
}

function stableHashIndex(key: string, count: number): number {
  let h = 2166136261
  for (let i = 0; i < key.length; i += 1) {
    h ^= key.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return (h >>> 0) % count
}

const IF_OPEN_RE = /^\{\{\s*#(if_pure|if)\b([\s\S]*?)\}\}$/i

function findConditionalClauses(
  text: string,
  bodyStart: number,
  tagKind: string,
): { closeStart: number; closeEnd: number; elseStart: number | null } | null {
  let depth = 1
  let elseStart: number | null = null
  let i = bodyStart
  const n = text.length
  const closeTag = `/${tagKind}`

  while (i < n) {
    if (!text.startsWith('{{', i)) {
      i += 1
      continue
    }
    const end = findMatchingClose(text, i)
    if (end === null) return null
    const inner = text.slice(i + 2, end - 2).trim()
    const low = inner.toLowerCase()
    if (
      low.startsWith(`#${tagKind}`) &&
      (low.length === tagKind.length + 1 || /\s/.test(low[tagKind.length + 1]))
    ) {
      depth += 1
    } else if (low === closeTag) {
      depth -= 1
      if (depth === 0) return { closeStart: i, closeEnd: end, elseStart }
    } else if (depth === 1 && elseStart === null && (low === ':else' || low === '#else')) {
      elseStart = i
    }
    i = end
  }
  return null
}

function trimIfBlockEdges(text: string): string {
  let out = text
  if (out.startsWith('\r\n')) out = out.slice(2)
  else if (out.startsWith('\n')) out = out.slice(1)
  if (out.endsWith('\r\n')) out = out.slice(0, -2)
  else if (out.endsWith('\n')) out = out.slice(0, -1)
  return out
}

function evaluateMacro(
  rawMacro: string,
  inner: string,
  opts: CbsOptions,
  varsMap: Record<string, string>,
  depth: number,
): string {
  const stripped = inner.trim()
  if (!stripped) return rawMacro
  if (stripped.startsWith('//')) return ''

  if (stripped.startsWith('?')) {
    const exprEval = evaluateCbsInternal(stripped.slice(1), opts, varsMap, depth + 1)
    const res = evalExpr(exprEval, varsMap)
    return res !== null ? res : rawMacro
  }

  if (stripped.includes('::')) {
    const parts = splitTopLevel(stripped, '::')
    const cmd = parts[0].trim().toLowerCase()
    const args = parts
      .slice(1)
      .map((p) => evaluateCbsInternal(p, opts, varsMap, depth + 1))

    if ((cmd === 'getvar' || cmd === 'getglobalvar') && args.length) {
      const key = args[0].trim()
      return varsMap[key] ?? ''
    }
    if (cmd === 'calc' && args.length) {
      const res = evalExpr(args[0], varsMap)
      return res !== null ? res : rawMacro
    }
    if ((cmd === 'random' || cmd === 'pick') && args.length) {
      const nonEmpty = args.filter((a) => a !== '')
      const choices = nonEmpty.length ? nonEmpty : args
      if (cmd === 'pick') {
        return choices[stableHashIndex(`${opts.seed ?? ''}:${stripped}`, choices.length)]
      }
      return choices[Math.floor(Math.random() * choices.length)]
    }
    if (cmd === 'comment') return ''
    if (cmd === 'setvar' || cmd === 'addvar') return ''
    return rawMacro
  }

  if (stripped.includes(':') && !stripped.startsWith('#')) {
    const colon = stripped.indexOf(':')
    const cmd = stripped.slice(0, colon).trim().toLowerCase()
    const rest = stripped.slice(colon + 1)
    if (cmd === 'random' || cmd === 'pick') {
      const list = rest.split(',').map((o) => o.trim())
      if (list.length) {
        if (cmd === 'pick') {
          return list[stableHashIndex(`${opts.seed ?? ''}:${stripped}`, list.length)]
        }
        return list[Math.floor(Math.random() * list.length)]
      }
    }
  }

  const low = stripped.toLowerCase()
  if (CHARACTER_ALIASES.has(low)) return opts.charName || 'the character'
  if (USER_ALIASES.has(low)) return opts.userName || 'the user'
  if (low === 'newline' || low === 'br') return '\n'
  if (low === 'trim') return ''
  return rawMacro
}

function evaluateCbsInternal(
  text: string,
  opts: CbsOptions,
  varsMap: Record<string, string>,
  depth: number,
): string {
  if (!text || !text.includes('{{')) return text || ''
  if (depth >= MAX_RECURSION_DEPTH) return text

  const parts: string[] = []
  let i = 0
  const n = text.length

  while (i < n) {
    const idx = text.indexOf('{{', i)
    if (idx === -1) {
      parts.push(text.slice(i))
      break
    }
    if (idx > i) parts.push(text.slice(i, idx))

    const end = findMatchingClose(text, idx)
    if (end === null) {
      parts.push(text.slice(idx))
      break
    }

    const token = text.slice(idx, end)
    const openMatch = token.match(IF_OPEN_RE)
    if (openMatch) {
      const tagKind = openMatch[1].toLowerCase()
      const condRaw = openMatch[2].trim()
      const clauses = findConditionalClauses(text, end, tagKind)
      if (clauses) {
        const condEval = evaluateCbsInternal(condRaw, opts, varsMap, depth + 1)
        const truthy = isTruthy(condEval)
        let chosen: string
        if (clauses.elseStart !== null) {
          const elseEnd = findMatchingClose(text, clauses.elseStart) ?? clauses.elseStart
          chosen = truthy
            ? text.slice(end, clauses.elseStart)
            : text.slice(elseEnd, clauses.closeStart)
        } else {
          chosen = truthy ? text.slice(end, clauses.closeStart) : ''
        }
        if (tagKind === 'if' && chosen) chosen = trimIfBlockEdges(chosen)
        parts.push(evaluateCbsInternal(chosen, opts, varsMap, depth + 1))
        i = clauses.closeEnd
        continue
      }
    }

    const inner = text.slice(idx + 2, end - 2)
    parts.push(evaluateMacro(token, inner, opts, varsMap, depth))
    i = end
  }

  return parts.join('')
}

export function evaluateCbs(text: string | null | undefined, opts: CbsOptions = {}): string {
  const source = text || ''
  if (!source.includes('{{')) return source
  return evaluateCbsInternal(source, opts, opts.variables ?? {}, 0)
}

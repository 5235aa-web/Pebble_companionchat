/**
 * Contextual Binding Substitutions (CBS) and variable macro evaluator.
 *
 * Mirrors `backend/cbs.py` so greetings, displayed messages, and regex
 * replacements evaluate identical conditionals, variables, and expressions.
 */

const CHARACTER_ALIASES = new Set(['char', 'character'])
const USER_ALIASES = new Set(['user', 'persona'])
const BLOCK_TAGS = new Set(['if', 'if_pure', 'each', 'when'])
const MAX_RECURSION_DEPTH = 16

export interface CbsOptions {
  charName?: string | null
  userName?: string | null
  variables?: Record<string, string>
  seed?: string
  assetNames?: string[]
  allowMutation?: boolean
  chatIndex?: number
  lastMessageId?: number
  lastMessage?: string
  lastCharMessage?: string
  lastUserMessage?: string
  idleDuration?: string
  idleMinutes?: number
}

interface SpreadList {
  readonly __spread: true
  readonly items: string[]
}

function isSpreadList(val: unknown): val is SpreadList {
  return typeof val === 'object' && val !== null && (val as SpreadList).__spread === true
}

interface CbsState {
  charName: string
  userName: string
  variables: Record<string, string>
  allowMutation: boolean
  seed: string
  tempVars: Record<string, string>
  slotVars: Record<string, string>
  assetNames: string[]
  chatIndex: number
  lastMessageId: number
  lastMessage: string
  lastCharMessage: string
  lastUserMessage: string
  idleDuration: string
  idleMinutes: number
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

function matchBlockOpenKind(inner: string): string | null {
  const low = inner.trim().toLowerCase()
  if (!low.startsWith('#')) return null
  for (const kind of ['if_pure', 'if', 'each', 'when']) {
    const prefix = `#${kind}`
    if (low.startsWith(prefix)) {
      const next = low[prefix.length]
      if (next === undefined || next === ':' || /\s/.test(next)) return kind
    }
  }
  return null
}

function matchBlockCloseKind(inner: string): string | null {
  const low = inner.trim().toLowerCase()
  if (!low.startsWith('/')) return null
  const tag = low.slice(1)
  return BLOCK_TAGS.has(tag) ? tag : null
}

function splitTopLevel(text: string, sep = '::'): string[] {
  const parts: string[] = []
  let braceDepth = 0
  const blockStack: string[] = []
  let start = 0
  let i = 0
  const n = text.length
  while (i < n) {
    if (text.startsWith('{{', i)) {
      if (braceDepth === 0) {
        const end = findMatchingClose(text, i)
        if (end !== null) {
          const inner = text.slice(i + 2, end - 2)
          const openKind = matchBlockOpenKind(inner)
          if (openKind !== null) {
            blockStack.push(openKind)
            i = end
            continue
          }
          const closeKind = matchBlockCloseKind(inner)
          if (
            closeKind !== null &&
            blockStack.length > 0 &&
            blockStack[blockStack.length - 1] === closeKind
          ) {
            blockStack.pop()
            i = end
            continue
          }
        }
      }
      braceDepth += 1
      i += 2
      continue
    }
    if (text.startsWith('}}', i) && braceDepth > 0) {
      braceDepth -= 1
      i += 2
      continue
    }
    if (braceDepth === 0 && blockStack.length === 0 && text.startsWith(sep, i)) {
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

const BLOCK_OPEN_RE = /^\{\{\s*#(if_pure|if|each|when)(?=[\s:]|$)([\s\S]*?)\}\}$/i
const ARRAY_SPACE_RE = /^(?:array|makearray)(?:\s|$)/i

function findBlockClauses(
  text: string,
  bodyStart: number,
  tagKind: string,
): { closeStart: number; closeEnd: number; elseStart: number | null } | null {
  const stack: string[] = [tagKind]
  let elseStart: number | null = null
  let i = bodyStart
  const n = text.length

  while (i < n) {
    if (!text.startsWith('{{', i)) {
      i += 1
      continue
    }
    const end = findMatchingClose(text, i)
    if (end === null) return null
    const inner = text.slice(i + 2, end - 2).trim()
    const openKind = matchBlockOpenKind(inner)
    if (openKind !== null) {
      stack.push(openKind)
    } else {
      const closeKind = matchBlockCloseKind(inner)
      if (closeKind !== null && stack.length > 0 && stack[stack.length - 1] === closeKind) {
        stack.pop()
        if (stack.length === 0) return { closeStart: i, closeEnd: end, elseStart }
      } else if (
        stack.length === 1 &&
        tagKind !== 'each' &&
        elseStart === null &&
        (inner.toLowerCase() === ':else' || inner.toLowerCase() === '#else')
      ) {
        elseStart = i
      }
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

function parseArrayItems(raw: string): string[] {
  const s = (raw || '').trim()
  if (!s) return []
  if (s.startsWith('[')) {
    try {
      const parsed: unknown = JSON.parse(s)
      if (Array.isArray(parsed)) {
        return parsed.map((item) => {
          if (item === null || item === undefined) return ''
          if (typeof item === 'string') return item
          if (typeof item === 'boolean') return item ? '1' : '0'
          if (typeof item === 'object') return JSON.stringify(item)
          return String(item)
        })
      }
    } catch {
      // fall through
    }
  }
  if (s.includes('::')) {
    return s.split('::').filter((p) => p !== '')
  }
  return [s]
}

function splitTopLevelWhitespace(text: string): string[] {
  const tokens: string[] = []
  let depth = 0
  let start: number | null = null
  let i = 0
  const n = text.length
  while (i < n) {
    if (text.startsWith('{{', i)) {
      if (start === null) start = i
      depth += 1
      i += 2
      continue
    }
    if (text.startsWith('}}', i) && depth > 0) {
      depth -= 1
      i += 2
      continue
    }
    if (depth === 0 && /\s/.test(text[i])) {
      if (start !== null) {
        tokens.push(text.slice(start, i))
        start = null
      }
      i += 1
      continue
    }
    if (start === null) start = i
    i += 1
  }
  if (start !== null) tokens.push(text.slice(start))
  return tokens
}

function parseEachHeader(headerRaw: string): { expr: string; varName: string } | null {
  const tokens = splitTopLevelWhitespace(headerRaw)
  if (tokens.length < 2) return null
  const varName = tokens[tokens.length - 1].trim()
  if (!varName) return null
  const exprTokens =
    tokens.length >= 3 && tokens[tokens.length - 2].toLowerCase() === 'as'
      ? tokens.slice(0, -2)
      : tokens.slice(0, -1)
  const expr = exprTokens.join(' ').trim()
  return expr ? { expr, varName } : null
}

function compareValues(left: string, right: string, op: string): boolean {
  const ln = toNumber(left)
  const rn = toNumber(right)
  if (op === 'is' || op === 'eq' || op === '==') return left === right
  if (op === 'isnot' || op === 'neq' || op === '!=') return left !== right
  if (op === '>' || op === 'gt') return ln !== null && rn !== null ? ln > rn : left > right
  if (op === '<' || op === 'lt') return ln !== null && rn !== null ? ln < rn : left < right
  if (op === '>=' || op === 'gte') return ln !== null && rn !== null ? ln >= rn : left >= right
  if (op === '<=' || op === 'lte') return ln !== null && rn !== null ? ln <= rn : left <= right
  if (op === 'in' || op === 'notin') {
    const items = right.trim().startsWith('[') ? parseArrayItems(right) : null
    const contained = items !== null ? items.includes(left) : right.includes(left)
    return op === 'in' ? contained : !contained
  }
  return false
}

const WHEN_PREFIX_OPS: Record<string, string> = {
  equal: '==',
  not_equal: '!=',
  notequal: '!=',
  greater: '>',
  less: '<',
  greater_equal: '>=',
  greaterequal: '>=',
  less_equal: '<=',
  lessequal: '<=',
}

function evalWhenSegment(seg: string[], state: CbsState): boolean {
  if (!seg.length) return false
  const headLow = seg[0].trim().toLowerCase()
  if (headLow === 'not' && seg.length >= 2) {
    return !evalWhenSegment(seg.slice(1), state)
  }
  if (headLow in WHEN_PREFIX_OPS && seg.length >= 3) {
    return compareValues(seg[1], seg[2], WHEN_PREFIX_OPS[headLow])
  }
  if (seg.length === 1) return isTruthy(seg[0])
  if (seg.length === 2) {
    if (headLow === 'toggle' || headLow === 'var') {
      return isTruthy(state.variables[seg[1].trim()] ?? '')
    }
    if (headLow === 'tempvar') {
      return isTruthy(state.tempVars[seg[1].trim()] ?? '')
    }
    return isTruthy(seg[0])
  }
  const left = seg[0]
  const op = seg[1].trim().toLowerCase()
  const right = seg[2]
  if (op === 'vis') return (state.variables[left.trim()] ?? '') === right
  if (op === 'visnot') return (state.variables[left.trim()] ?? '') !== right
  if (op === 'tis') return (state.tempVars[left.trim()] ?? '') === right
  if (op === 'tisnot') return (state.tempVars[left.trim()] ?? '') !== right
  return compareValues(left, right, op)
}

function evalWhenCondition(
  headerRaw: string,
  state: CbsState,
  depth: number,
): { truthy: boolean; keepWhitespace: boolean } {
  let s = headerRaw.trim()
  if (!s) return { truthy: false, keepWhitespace: false }
  if (!s.includes('::')) {
    return { truthy: isTruthy(evalWithState(s, state, depth + 1)), keepWhitespace: false }
  }
  if (s.startsWith('::')) s = s.slice(2)
  const rawParts = splitTopLevel(s, '::')
  let parts = rawParts.map((p) => evalWithState(p, state, depth + 1))
  let keepWhitespace = false
  if (parts.length > 1 && parts[0].trim().toLowerCase() === 'keep') {
    keepWhitespace = true
    parts = parts.slice(1)
  }

  const orGroups: string[][] = [[]]
  for (const tok of parts) {
    const low = tok.trim().toLowerCase()
    if (low === 'or' || low === '||') orGroups.push([])
    else orGroups[orGroups.length - 1].push(tok)
  }

  for (const group of orGroups) {
    const andSegs: string[][] = [[]]
    for (const tok of group) {
      const low = tok.trim().toLowerCase()
      if (low === 'and' || low === '&&') andSegs.push([])
      else andSegs[andSegs.length - 1].push(tok)
    }
    if (andSegs.length && andSegs.every((seg) => evalWhenSegment(seg, state))) {
      return { truthy: true, keepWhitespace }
    }
  }
  return { truthy: false, keepWhitespace }
}

function evaluateMacroValue(
  rawMacro: string,
  inner: string,
  state: CbsState,
  depth: number,
): string | SpreadList {
  const stripped = inner.trim()
  if (!stripped) return rawMacro
  if (stripped.startsWith('//')) return ''

  if (stripped.startsWith('?')) {
    const exprEval = evalWithState(stripped.slice(1), state, depth + 1)
    const mergedVars = { ...state.variables, ...state.tempVars }
    const res = evalExpr(exprEval, mergedVars)
    return res !== null ? res : rawMacro
  }

  if (ARRAY_SPACE_RE.test(stripped)) {
    const topParts = splitTopLevel(stripped, '::')
    if (topParts.length === 1) {
      const cmdMatch = stripped.match(/^\S+/)
      const cmdLen = cmdMatch ? cmdMatch[0].length : stripped.length
      const bodyEval = evalWithState(stripped.slice(cmdLen), state, depth + 1)
      if (!bodyEval.trim()) return '[]'
      let rawItems = bodyEval.split('::')
      if (rawItems.length > 1 && !rawItems[0].trim()) rawItems = rawItems.slice(1)
      return JSON.stringify(rawItems.map((item) => item.trim()))
    }
  }

  const parts = splitTopLevel(stripped, '::')
  if (parts.length > 1) {
    const cmd = parts[0].trim().toLowerCase()
    const args: string[] = []
    for (const p of parts.slice(1)) {
      const pStrip = p.trim()
      if (
        pStrip.startsWith('{{') &&
        pStrip.endsWith('}}') &&
        findMatchingClose(pStrip, 0) === pStrip.length
      ) {
        const val = evaluateMacroValue(pStrip, pStrip.slice(2, -2), state, depth + 1)
        if (isSpreadList(val)) args.push(...val.items)
        else args.push(val)
      } else {
        args.push(evalWithState(p, state, depth + 1))
      }
    }

    if ((cmd === 'getvar' || cmd === 'getglobalvar') && args.length) {
      return state.variables[args[0].trim()] ?? ''
    }
    if (cmd === 'setvar' && args.length >= 2) {
      if (state.allowMutation) state.variables[args[0].trim()] = args[1]
      return ''
    }
    if (cmd === 'addvar' && args.length >= 2) {
      if (state.allowMutation) {
        const key = args[0].trim()
        const cur = toNumber(state.variables[key] ?? '0') ?? 0
        const delta = toNumber(args[1]) ?? 0
        state.variables[key] = formatExprResult(cur + delta)
      }
      return ''
    }
    if ((cmd === 'gettempvar' || cmd === 'tempvar') && args.length) {
      return state.tempVars[args[0].trim()] ?? ''
    }
    if (cmd === 'settempvar' && args.length >= 2) {
      state.tempVars[args[0].trim()] = args[1]
      return ''
    }
    if (cmd === 'addtempvar' && args.length >= 2) {
      const key = args[0].trim()
      const cur = toNumber(state.tempVars[key] ?? '0') ?? 0
      const delta = toNumber(args[1]) ?? 0
      state.tempVars[key] = formatExprResult(cur + delta)
      return ''
    }
    if (cmd === 'slot' && args.length) {
      return state.slotVars[args[0].trim()] ?? ''
    }
    if (cmd === 'array' || cmd === 'makearray') {
      return JSON.stringify(args)
    }
    if (cmd === 'spread') {
      return { __spread: true, items: args.length ? parseArrayItems(args[0]) : [] }
    }
    if (cmd === 'equal' && args.length >= 2) return args[0] === args[1] ? '1' : '0'
    if ((cmd === 'not_equal' || cmd === 'notequal') && args.length >= 2) {
      return args[0] !== args[1] ? '1' : '0'
    }
    if (cmd === 'greater' && args.length >= 2) {
      return compareValues(args[0], args[1], '>') ? '1' : '0'
    }
    if (cmd === 'less' && args.length >= 2) {
      return compareValues(args[0], args[1], '<') ? '1' : '0'
    }
    if ((cmd === 'greater_equal' || cmd === 'greaterequal') && args.length >= 2) {
      return compareValues(args[0], args[1], '>=') ? '1' : '0'
    }
    if ((cmd === 'less_equal' || cmd === 'lessequal') && args.length >= 2) {
      return compareValues(args[0], args[1], '<=') ? '1' : '0'
    }
    if (cmd === 'and' && args.length >= 2) {
      return isTruthy(args[0]) && isTruthy(args[1]) ? '1' : '0'
    }
    if (cmd === 'or' && args.length >= 2) {
      return isTruthy(args[0]) || isTruthy(args[1]) ? '1' : '0'
    }
    if (cmd === 'not' && args.length) {
      return !isTruthy(args[0]) ? '1' : '0'
    }
    if (cmd === 'replace' && args.length >= 2) {
      const targetStr = args[0]
      const findStr = args[1]
      const replStr = args[2] ?? ''
      return findStr ? targetStr.split(findStr).join(replStr) : targetStr
    }
    if (cmd === 'lower' && args.length) return args[0].toLowerCase()
    if (cmd === 'upper' && args.length) return args[0].toUpperCase()
    if (cmd === 'trim' && args.length) return args[0].trim()
    if ((cmd === 'length' || cmd === 'array_length') && args.length) {
      const s = args[0].trim()
      if (s.startsWith('[')) {
        try {
          const parsed: unknown = JSON.parse(s)
          if (Array.isArray(parsed)) return String(parsed.length)
        } catch {
          // fall through
        }
      }
      return String(args[0].length)
    }
    if (cmd === 'contains' && args.length >= 2) {
      const hay = args[0].trim()
      if (hay.startsWith('[')) {
        try {
          const parsed: unknown = JSON.parse(hay)
          if (Array.isArray(parsed)) {
            return parseArrayItems(hay).includes(args[1]) ? '1' : '0'
          }
        } catch {
          // fall through
        }
      }
      return args[0].includes(args[1]) ? '1' : '0'
    }
    if (cmd === 'startswith' && args.length >= 2) return args[0].startsWith(args[1]) ? '1' : '0'
    if (cmd === 'endswith' && args.length >= 2) return args[0].endsWith(args[1]) ? '1' : '0'
    if (cmd === 'split' && args.length) {
      const sep = args[1] ?? ''
      const out = sep ? args[0].split(sep) : args[0].trim().split(/\s+/).filter(Boolean)
      return JSON.stringify(out)
    }
    if (cmd === 'join' && args.length) {
      const sep = args[1] ?? ','
      return parseArrayItems(args[0]).join(sep)
    }
    if (cmd === 'calc' && args.length) {
      const mergedVars = { ...state.variables, ...state.tempVars }
      const res = evalExpr(args[0], mergedVars)
      return res !== null ? res : rawMacro
    }
    if (cmd === 'random' || cmd === 'pick') {
      if (!args.length) return ''
      const nonEmpty = args.filter((a) => a !== '')
      const choices = nonEmpty.length ? nonEmpty : args
      if (cmd === 'pick') {
        return choices[stableHashIndex(`${state.seed}:${stripped}`, choices.length)]
      }
      return choices[Math.floor(Math.random() * choices.length)]
    }
    if (cmd === 'comment') return ''
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
          return list[stableHashIndex(`${state.seed}:${stripped}`, list.length)]
        }
        return list[Math.floor(Math.random() * list.length)]
      }
    }
  }

  const low = stripped.toLowerCase()
  if (CHARACTER_ALIASES.has(low)) return state.charName || 'the character'
  if (USER_ALIASES.has(low)) return state.userName || 'the user'
  if (low === 'newline' || low === 'br') return '\n'
  if (low === 'trim') return ''
  if (low === 'assetlist') return JSON.stringify(state.assetNames)
  if (low === 'array' || low === 'makearray') return '[]'
  if (low === 'slot') return state.slotVars[''] ?? state.slotVars.slot ?? ''
  if (low === 'chat_index') return String(state.chatIndex)
  if (low === 'lastmessageid') return String(state.lastMessageId)
  if (low === 'lastmessage') return state.lastMessage
  if (low === 'lastcharmessage') return state.lastCharMessage
  if (low === 'lastusermessage') return state.lastUserMessage
  if (low === 'idle_duration' || low === 'idleduration') return state.idleDuration || '0m'
  if (low === 'idle_minutes' || low === 'idleminutes') {
    return String(Math.max(0, Math.floor(state.idleMinutes || 0)))
  }
  if (low === 'hour') return String(new Date().getHours())
  if (low === 'time') {
    const d = new Date()
    return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':')
  }
  if (low === 'isotime') {
    return new Date().toISOString().slice(11, 19)
  }
  if (low === 'date') {
    const d = new Date()
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  }
  if (low === 'isodate') {
    return new Date().toISOString().slice(0, 10)
  }
  if (low === 'datetime') {
    const d = new Date()
    const dt = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
    const tm = [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':')
    return `${dt} ${tm}`
  }
  return rawMacro
}

function evaluateMacro(
  rawMacro: string,
  inner: string,
  state: CbsState,
  depth: number,
): string {
  const val = evaluateMacroValue(rawMacro, inner, state, depth)
  return isSpreadList(val) ? val.items.join('::') : val
}

function evalWithState(text: string, state: CbsState, depth: number): string {
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
    const openMatch = token.match(BLOCK_OPEN_RE)
    if (openMatch) {
      const tagKind = openMatch[1].toLowerCase()
      const headerRaw = openMatch[2]
      const clauses = findBlockClauses(text, end, tagKind)
      if (clauses) {
        if (tagKind === 'each') {
          const parsed = parseEachHeader(headerRaw)
          if (parsed) {
            const arrEval = evalWithState(parsed.expr, state, depth + 1)
            const items = parseArrayItems(arrEval)
            const body = text.slice(end, clauses.closeStart)
            const hadPrev = Object.prototype.hasOwnProperty.call(state.slotVars, parsed.varName)
            const prevVal = state.slotVars[parsed.varName] ?? ''
            try {
              for (const item of items) {
                state.slotVars[parsed.varName] = item
                parts.push(evalWithState(body, state, depth + 1))
              }
            } finally {
              if (hadPrev) state.slotVars[parsed.varName] = prevVal
              else delete state.slotVars[parsed.varName]
            }
            i = clauses.closeEnd
            continue
          }
        } else {
          let truthy: boolean
          let keepWhitespace: boolean
          if (tagKind === 'when') {
            const res = evalWhenCondition(headerRaw, state, depth)
            truthy = res.truthy
            keepWhitespace = res.keepWhitespace
          } else {
            const condEval = evalWithState(headerRaw.trim(), state, depth + 1)
            truthy = isTruthy(condEval)
            keepWhitespace = tagKind === 'if_pure'
          }

          let chosen: string
          if (clauses.elseStart !== null) {
            const elseEnd = findMatchingClose(text, clauses.elseStart) ?? clauses.elseStart
            chosen = truthy
              ? text.slice(end, clauses.elseStart)
              : text.slice(elseEnd, clauses.closeStart)
          } else {
            chosen = truthy ? text.slice(end, clauses.closeStart) : ''
          }
          if (!keepWhitespace && chosen) chosen = trimIfBlockEdges(chosen)
          parts.push(evalWithState(chosen, state, depth + 1))
          i = clauses.closeEnd
          continue
        }
      }
    }

    const inner = text.slice(idx + 2, end - 2)
    parts.push(evaluateMacro(token, inner, state, depth))
    i = end
  }

  return parts.join('')
}

export function evaluateCbs(text: string | null | undefined, opts: CbsOptions = {}): string {
  const source = text || ''
  if (!source.includes('{{')) return source
  const state: CbsState = {
    charName: opts.charName || '',
    userName: opts.userName || 'the user',
    variables: opts.variables ?? {},
    allowMutation: opts.allowMutation ?? false,
    seed: opts.seed ?? '',
    tempVars: {},
    slotVars: {},
    assetNames: (opts.assetNames ?? []).filter(Boolean),
    chatIndex: opts.chatIndex ?? 0,
    lastMessageId: opts.lastMessageId ?? 0,
    lastMessage: opts.lastMessage ?? '',
    lastCharMessage: opts.lastCharMessage ?? '',
    lastUserMessage: opts.lastUserMessage ?? '',
    idleDuration: opts.idleDuration ?? '',
    idleMinutes: opts.idleMinutes ?? 0,
  }
  return evalWithState(source, state, 0)
}

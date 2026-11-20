/**
 * Card text macros.
 *
 * Character cards write {{char}} and {{user}} instead of concrete names, so one
 * card works with any persona. The backend resolves them when it builds the
 * prompt; the frontend needs the same rule for the parts it renders itself —
 * the opening line, story text and anything shown before the model is involved.
 *
 * Keep this in step with `backend/placeholders.py`: same aliases, same
 * case-insensitivity, same fallbacks.
 */

const MACRO = /\{\{\s*([A-Za-z_]+)\s*\}\}/g

const CHARACTER_ALIASES = new Set(['char', 'character'])
const USER_ALIASES = new Set(['user', 'persona'])

/**
 * Replace macros with the names this chat is using. Unknown macros survive: a
 * card may carry placeholders for some other frontend, and deleting them would
 * quietly lose text the author wrote.
 */
export function resolvePlaceholders(
  text: string | null | undefined,
  charName: string | null | undefined,
  userName: string | null | undefined,
): string {
  const source = text || ''
  if (!source.includes('{{')) return source
  return source.replace(MACRO, (whole, kind: string) => {
    const key = kind.toLowerCase()
    if (CHARACTER_ALIASES.has(key)) return charName || 'the character'
    if (USER_ALIASES.has(key)) return userName || 'the user'
    return whole
  })
}

/**
 * Read the user's name out of the free-text `user_info` field.
 *
 * Mirrors the recognisable patterns from `memory_engine.resolve_user_name`, not
 * its full heuristic: this copy only feeds the rendering of the user's own
 * name, so a miss falls back to "the user" and nothing breaks. The backend
 * stays authoritative for what the model is told.
 */
const NAME_PATTERNS: RegExp[] = [
  /(?:^|\n)\s*(?:name|full name|\u540d\u5b57|\u59d3\u540d|\u7528\u6237\u540d|\u7528\u6237\u540d\u5b57)\s*[:\uff1a]\s*([^\n,\uff0c\u3002;\uff1b]{1,24})/i,
  /(?:^|\n)\s*(?:the\s+)?(?:user'?s?\s+name\s+is|user\s+is\s+named|user\s+is\s+called|your\s+name\s+is)\s*[:\uff1a]?\s*([^\n,\uff0c\u3002;\uff1b]{1,24})/i,
]

export function resolveUserName(userInfo: string | null | undefined): string {
  const text = (userInfo || '').trim()
  if (!text) return 'the user'

  for (const pattern of NAME_PATTERNS) {
    const match = text.match(pattern)
    if (match) {
      const candidate = match[1].trim().replace(/^['"]|['"]$/g, '').replace(/\.$/, '').trim()
      if (candidate) return candidate
    }
  }

  const lines = text.split('\n').map((line) => line.trim()).filter(Boolean)
  if (
    lines.length === 1 &&
    lines[0].length <= 20 &&
    !/[.,;:!?\u3002\uff0c\uff1b\uff1f\uff01]/.test(lines[0]) &&
    lines[0].split(/\s+/).length <= 2
  ) {
    return lines[0]
  }

  return 'the user'
}

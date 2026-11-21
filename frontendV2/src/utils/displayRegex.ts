import { evaluateCbs, type CbsOptions } from './cbs'

export interface DisplayRegexRule {
  id?: string
  name?: string
  pattern: string
  flags?: string
  replace: string
  placement?: number[]
  trim_strings?: string[]
  enabled?: boolean
}

export interface DisplayRegexContext extends CbsOptions {
  /** 1 = user input display, 2 = AI output / greeting display. */
  placement: 1 | 2
}

const VALID_FLAGS_RE = /[gimsuyd]/g

function sanitizeFlags(raw?: string): string {
  const seen = new Set<string>()
  const matches = (raw || 'g').match(VALID_FLAGS_RE) ?? ['g']
  for (const ch of matches) seen.add(ch)
  return [...seen].join('') || 'g'
}

/**
 * Apply SillyTavern / RisuAI Display Regex rules to rendered text without
 * mutating the underlying stored conversation history.
 */
export function applyDisplayRegex(
  text: string,
  rules: DisplayRegexRule[] | undefined,
  ctx: DisplayRegexContext,
): string {
  if (!text || !rules?.length) return text

  let current = text
  for (const rule of rules) {
    if (!rule || rule.enabled === false || !rule.pattern) continue
    if (
      Array.isArray(rule.placement) &&
      rule.placement.length > 0 &&
      !rule.placement.includes(ctx.placement)
    ) {
      continue
    }

    let re: RegExp
    try {
      re = new RegExp(rule.pattern, sanitizeFlags(rule.flags))
    } catch {
      continue
    }

    const trims = Array.isArray(rule.trim_strings) ? rule.trim_strings.filter(Boolean) : []
    const replacementRaw = rule.replace ?? ''
    let count = 0
    const MAX_REPLACEMENTS = 2000

    try {
      current = current.replace(re, (...args) => {
        if (count >= MAX_REPLACEMENTS) return args[0]
        count += 1

        let matchText = String(args[0] ?? '')
        for (const t of trims) {
          matchText = matchText.split(t).join('')
        }

        // Capture groups sit between args[1] and the numeric offset arg.
        const groups: string[] = []
        for (let i = 1; i < args.length; i += 1) {
          if (typeof args[i] === 'number') break
          groups.push(args[i] == null ? '' : String(args[i]))
        }

        const expanded = replacementRaw.replace(
          /\{\{\s*match\s*\}\}|\$(&|\d{1,2})/gi,
          (tok, ref?: string) => {
            if (!ref) return matchText
            if (ref === '&') return matchText
            const idx = Number(ref)
            if (idx >= 1 && idx <= groups.length) return groups[idx - 1]
            return tok
          },
        )
        return evaluateCbs(expanded, ctx)
      })
    } catch {
      // Never let a malformed card regex break message rendering.
    }
  }
  return current
}

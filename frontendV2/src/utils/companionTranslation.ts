export type CompanionLocale = '' | 'zh' | 'en'

export interface CompanionTranslationBucket {
  description?: string
  personality?: string
  scenario?: string
  tags?: string[]
  first_mes?: string
  alternate_greetings?: string[]
  creator_notes?: string
  translated_at?: string
}

export type CompanionTranslations = Partial<Record<'zh' | 'en', CompanionTranslationBucket>>

export interface TranslatableCompanionSource {
  description?: string
  personality?: string
  scenario?: string
  tags?: string[]
  first_mes?: string
  alternate_greetings?: string[]
  creator_notes?: string
  translations?: CompanionTranslations
  active_locale?: string
}

export function normalizeCompanionLocale(raw: unknown): CompanionLocale {
  const val = String(raw ?? '').trim().toLowerCase()
  return val === 'zh' || val === 'en' ? val : ''
}

export function hasLocaleTranslation(
  translations: CompanionTranslations | undefined,
  locale: 'zh' | 'en',
): boolean {
  const bucket = translations?.[locale]
  if (!bucket) return false
  const hasStr = Boolean(
    bucket.description?.trim() ||
      bucket.personality?.trim() ||
      bucket.scenario?.trim() ||
      bucket.first_mes?.trim() ||
      bucket.creator_notes?.trim(),
  )
  if (hasStr) return true
  if (Array.isArray(bucket.tags) && bucket.tags.some((t) => Boolean(t?.trim()))) return true
  if (
    Array.isArray(bucket.alternate_greetings) &&
    bucket.alternate_greetings.some((g) => Boolean(g?.trim()))
  ) {
    return true
  }
  return false
}

export function availableCompanionLocales(
  translations: CompanionTranslations | undefined,
): Array<'zh' | 'en'> {
  const out: Array<'zh' | 'en'> = []
  if (hasLocaleTranslation(translations, 'zh')) out.push('zh')
  if (hasLocaleTranslation(translations, 'en')) out.push('en')
  return out
}

export function resolveCompanionDisplayText(
  source: TranslatableCompanionSource | undefined | null,
  field: 'description' | 'personality' | 'scenario' | 'first_mes' | 'creator_notes',
): string {
  if (!source) return ''
  const orig = String(source[field] ?? '')
  const loc = normalizeCompanionLocale(source.active_locale)
  if (!loc) return orig
  const tr = source.translations?.[loc]?.[field]
  return typeof tr === 'string' && tr.trim() ? tr : orig
}

export function resolveCompanionDisplayTags(
  source: TranslatableCompanionSource | undefined | null,
): string[] {
  if (!source) return []
  const orig = Array.isArray(source.tags) ? source.tags : []
  const loc = normalizeCompanionLocale(source.active_locale)
  if (!loc) return orig
  const trTags = source.translations?.[loc]?.tags
  if (!Array.isArray(trTags)) return orig
  const clean = trTags.map((t) => String(t ?? '').trim()).filter(Boolean)
  return clean.length ? clean : orig
}

export function resolveCompanionDisplayGreetings(
  source: TranslatableCompanionSource | undefined | null,
): { firstMes: string; alternateGreetings: string[] } {
  if (!source) return { firstMes: '', alternateGreetings: [] }
  const firstMes = resolveCompanionDisplayText(source, 'first_mes')
  const origAlts = Array.isArray(source.alternate_greetings) ? source.alternate_greetings : []
  const loc = normalizeCompanionLocale(source.active_locale)
  if (!loc) return { firstMes, alternateGreetings: origAlts }
  const trAlts = source.translations?.[loc]?.alternate_greetings
  if (!Array.isArray(trAlts)) return { firstMes, alternateGreetings: origAlts }
  const merged = origAlts.map((orig, idx) => {
    const tr = trAlts[idx]
    return typeof tr === 'string' && tr.trim() ? tr : orig
  })
  return { firstMes, alternateGreetings: merged }
}

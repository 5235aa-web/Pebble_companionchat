import { ref } from 'vue'
import { en, type LabelKey } from './en'
import { zh } from './zh'

/**
 * Locale registry. To add a language:
 *   1. drop a file next to en.ts with the same keys
 *   2. import it here and add it to `messages`
 * Nothing else in the app has to change.
 */
export type Locale = 'en' | 'zh'

const messages: Record<Locale, Record<string, string>> = {
  en,
  zh,
}

const locale = ref<Locale>('en')

function interpolate(text: string, vars?: Record<string, string | number>): string {
  if (!vars) return text
  return text.replace(/\{(\w+)\}/g, (whole, name: string) =>
    name in vars ? String(vars[name]) : whole,
  )
}

export function t(key: LabelKey, vars?: Record<string, string | number>): string {
  const table = messages[locale.value] ?? en
  const raw = table[key] ?? en[key] ?? key
  return interpolate(raw, vars)
}

const localeOrder: Locale[] = ['en', 'zh']

export function cycleLocale() {
  const i = localeOrder.indexOf(locale.value)
  locale.value = localeOrder[(i + 1) % localeOrder.length]
}

export function setLocale(next: Locale) {
  locale.value = next
}

export function useI18n() {
  return {
    t,
    locale,
    setLocale,
    cycleLocale,
    available: Object.keys(messages) as Locale[],
  }
}

export type { LabelKey }

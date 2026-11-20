/**
 * Timestamps from the backend.
 *
 * SQLite's CURRENT_TIMESTAMP writes UTC as "YYYY-MM-DD HH:MM:SS" with no zone
 * marker. Parsed as-is, the browser treats that as local time, so every stamp
 * lands shifted by the UTC offset — a 12:35 compaction rendered as 04:35 in
 * UTC+8. Anything without an offset is taken as UTC here and rendered in the
 * reader's own zone; anything that already carries one is trusted as written.
 *
 * Every clock in the app goes through these helpers. Three components each had
 * their own copy of this rule and one more forgot it, which is exactly how the
 * compaction panel ended up showing UTC.
 */

const HAS_ZONE = /[zZ]$|[+-]\d{2}:?\d{2}$/

const pad = (n: number) => String(n).padStart(2, '0')

/** Absolute instant a backend timestamp refers to, or null when unparseable. */
export function parseServerTime(raw?: string | null): Date | null {
  if (!raw) return null
  const text = String(raw).trim()
  if (!text) return null
  const iso = HAS_ZONE.test(text) ? text : `${text.replace(' ', 'T')}Z`
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? null : at
}

/** HH:mm in the reader's zone; the message clock. */
export function formatClock(raw?: string | null): string {
  const at = parseServerTime(raw)
  return at ? `${pad(at.getHours())}:${pad(at.getMinutes())}` : ''
}

/** YYYY-MM-DD HH:mm in the reader's zone; panels and record rows. */
export function formatDateTime(raw?: string | null): string {
  const at = parseServerTime(raw)
  if (!at) return ''
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(at.getHours())}:${pad(at.getMinutes())}`
}

/** YYYY-MM-DD in the reader's zone. */
export function formatDate(raw?: string | null): string {
  const at = parseServerTime(raw)
  if (!at) return ''
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`
}

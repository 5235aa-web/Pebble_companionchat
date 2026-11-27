import { del, get, getBaseURL, post, postForm, put, streamSseJson } from './client'
import type { LlmConfig } from './stream'

const C = '/companions'

export type WorldBookEntryCategory = 'media' | 'lore' | 'rpg'
export type WorldBookLocale = 'zh' | 'en'

export interface WorldBookEntryTranslationBucket {
  comment?: string
  content?: string
  translated_at?: string
}

export type WorldBookEntryTranslations = Partial<
  Record<WorldBookLocale, WorldBookEntryTranslationBucket>
>

export interface WorldBookEntry {
  id: number
  world_book_id: number
  uid?: number
  name?: string
  comment: string
  content: string
  keys: string[]
  secondary_keys: string[]
  position: number | string
  depth: number
  probability: number
  constant: boolean
  selective: boolean
  enabled?: boolean
  disabled?: boolean
  selective_logic?: number
  logic?: 'AND' | 'OR'
  category?: WorldBookEntryCategory
  translations?: WorldBookEntryTranslations
  created_at?: string
  updated_at?: string
}

export interface WorldBookTranslateEvent {
  type: 'start' | 'progress' | 'entry_done' | 'done' | 'error'
  locale?: WorldBookLocale
  done?: number
  total?: number
  skipped?: number
  entry_id?: number
  comment?: string
  message?: string
  translations?: WorldBookEntryTranslations
  entries?: WorldBookEntry[]
}

export interface WorldBook {
  id: number
  name: string
  description: string
  entries?: WorldBookEntry[]
  created_at?: string
  updated_at?: string
}

export interface CompanionWorldBooksInfo {
  worldBooks: WorldBook[]
  cardLoreCount: number
}

export interface SyncCardWorldBookResult {
  status: string
  world_book: WorldBook | null
  entries: WorldBookEntry[]
  world_book_id: number | null
  imported_count: number
  media_enabled_count: number
  already_linked?: boolean
}

export const listWorldBooks = async (): Promise<WorldBook[]> => {
  const r = await get<{ world_books?: WorldBook[] }>(`${C}/world-books`)
  return r.world_books ?? []
}

export const getWorldBook = async (id: number): Promise<WorldBook> => {
  const r = await get<{ world_book?: WorldBook; entries?: WorldBookEntry[] }>(
    `${C}/world-books/${id}`,
  )
  const book = r.world_book ?? ({} as WorldBook)
  return { ...book, entries: r.entries ?? book.entries ?? [] }
}

export const createWorldBook = (fields: Partial<WorldBook>): Promise<WorldBook> =>
  post(`${C}/world-books`, fields)

export const updateWorldBook = (id: number, fields: Partial<WorldBook>): Promise<unknown> =>
  put(`${C}/world-books/${id}`, fields)

export const deleteWorldBook = (id: number): Promise<unknown> => del(`${C}/world-books/${id}`)

export const createEntry = async (
  worldBookId: number,
  fields: Partial<WorldBookEntry>,
): Promise<WorldBookEntry> => {
  const r = await post<{ status?: string; entry_id?: number }>(
    `${C}/world-books/${worldBookId}/entries`,
    fields,
  )
  return { ...(fields as WorldBookEntry), id: r.entry_id ?? 0, world_book_id: worldBookId }
}

export const updateEntry = (
  worldBookId: number,
  entryId: number,
  fields: Partial<WorldBookEntry>,
): Promise<unknown> => put(`${C}/world-books/${worldBookId}/entries/${entryId}`, fields)

export const deleteEntry = (worldBookId: number, entryId: number): Promise<unknown> =>
  del(`${C}/world-books/${worldBookId}/entries/${entryId}`)

export const importWorldBook = (file: File): Promise<WorldBook> => {
  const fd = new FormData()
  fd.append('file', file)
  return postForm(`${C}/world-books/import`, fd)
}

export async function exportWorldBook(id: number): Promise<Blob> {
  const res = await fetch(`${getBaseURL()}/companions/world-books/${id}/export`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.blob()
}

export const getCompanionWorldBooks = async (companionId: string): Promise<WorldBook[]> => {
  const r = await get<{ world_books?: WorldBook[] }>(`${C}/${encodeURIComponent(companionId)}/world-books`)
  return r.world_books ?? []
}

export const getCompanionWorldBooksInfo = async (
  companionId: string,
): Promise<CompanionWorldBooksInfo> => {
  const r = await get<{ world_books?: WorldBook[]; card_lore_count?: number }>(
    `${C}/${encodeURIComponent(companionId)}/world-books`,
  )
  return {
    worldBooks: r.world_books ?? [],
    cardLoreCount: Number(r.card_lore_count ?? 0),
  }
}

export const syncCompanionCardWorldBook = (
  companionId: string,
  force = false,
): Promise<SyncCardWorldBookResult> =>
  post(`${C}/${encodeURIComponent(companionId)}/world-books/sync-card`, { force })

export const bulkToggleWorldBookEntries = (
  worldBookId: number,
  mode: 'media_only' | 'all_on' | 'all_off',
): Promise<{ status: string; updated: number; entries: WorldBookEntry[] }> =>
  post(`${C}/world-books/${worldBookId}/entries/bulk-toggle`, { mode })

export const associateCompanionWorldBook = (
  companionId: string,
  worldBookId: number,
  isPrimary = false,
): Promise<unknown> =>
  post(`${C}/${encodeURIComponent(companionId)}/world-books`, {
    world_book_id: worldBookId,
    is_primary: isPrimary,
  })

export const removeCompanionWorldBook = (companionId: string, worldBookId: number): Promise<unknown> =>
  del(`${C}/${encodeURIComponent(companionId)}/world-books/${worldBookId}`)

export const translateWorldBook = (
  worldBookId: number,
  payload: {
    target_locale: WorldBookLocale
    entry_ids?: number[]
    force?: boolean
    companion_id?: string | null
    llm_config?: LlmConfig
  },
  handlers: {
    onEvent: (ev: WorldBookTranslateEvent) => void
    signal?: AbortSignal
  },
): Promise<WorldBookTranslateEvent | null> =>
  streamSseJson<WorldBookTranslateEvent>(
    `${C}/world-books/${worldBookId}/translate`,
    payload,
    handlers,
  )

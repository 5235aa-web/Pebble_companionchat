import { del, get, getBaseURL, post, postForm, put } from './client'

const C = '/companions'

export interface WorldBookEntry {
  id: number
  world_book_id: number
  name: string
  comment: string
  content: string
  keys: string[]
  secondary_keys: string[]
  position: string
  depth: number
  probability: number
  constant: boolean
  selective: boolean
  enabled: boolean
  logic: 'AND' | 'OR'
  created_at?: string
  updated_at?: string
}

export interface WorldBook {
  id: number
  name: string
  description: string
  entries?: WorldBookEntry[]
  created_at?: string
  updated_at?: string
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

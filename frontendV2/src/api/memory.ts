import { del, get, request } from './client'

/**
 * One long-term memory a character extracted from the conversation.
 *
 * `keys` are the retrieval keywords: they decide when this memory is injected
 * into the system prompt again, so they are shown in the panel — a memory with
 * bad keywords never comes back.
 */
export interface Memory {
  id: number
  content: string
  kind: 'fact' | 'event' | 'preference' | 'relationship' | string
  /**
   * 'character' memories are true on every branch of this companion;
   * 'branch' memories belong to the timeline that produced them.
   */
  scope: 'character' | 'branch' | string
  keys: string[]
  importance: number
  created_at: string
  last_used_at: string | null
  use_count: number
  /**
   * Message ids the extractor read when it produced this memory. Lets the panel
   * jump back to the lines it was derived from.
   */
  source_message_ids: number[]
}

const C = '/companions'

export const getMemories = (
  companionId: string,
  branchId: string,
): Promise<{ items: Memory[]; limit: number }> =>
  get<{ memories?: Memory[]; limit?: number }>(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/memories`,
  ).then((r) => ({ items: r.memories ?? [], limit: r.limit ?? 100 }))

export const deleteMemory = (companionId: string, branchId: string, memoryId: number): Promise<unknown> =>
  del(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/memories/${memoryId}`,
  )

/**
 * Edit a memory in place.
 *
 * Extraction usually gets the shape right and the wording slightly wrong, so
 * being able to fix a sentence beats deleting and waiting for the next run.
 */
export const updateMemory = (
  companionId: string,
  branchId: string,
  memoryId: number,
  fields: { content?: string; keys?: string[] },
): Promise<unknown> =>
  request(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/memories/${memoryId}`,
    { method: 'PATCH', body: JSON.stringify(fields) },
  )

export const clearMemories = (companionId: string, branchId: string): Promise<{ removed?: number }> =>
  del(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/memories`,
  )

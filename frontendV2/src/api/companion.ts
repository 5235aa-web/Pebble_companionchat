import { del, get, post, postForm, put } from './client'

const C = '/companions'

export interface Companion {
  id: string
  name: string
  description: string
  personality: string
  scenario: string
  first_mes: string
  mes_example: string
  advanced_prompt: string
  user_info: string
  creator_notes: string
  system_prompt: string
  post_history_instructions: string
  creator: string
  character_version: string
  tags: string[]
  alternate_greetings: string[]
  avatar_path: string
  llm_provider_id: string
  tts_config: Record<string, unknown>
  raw_card_data?: string
  pinned?: boolean
  created_at: string
  updated_at: string
}

export interface CompanionBranch {
  id: number
  companion_id: string
  branch_id: string
  branch_name: string
  parent_branch_id: string
  fork_from_message_id: number | null
  created_at: string
  /** Total messages on this branch, user and assistant alike. */
  message_count?: number
}

export interface CompanionMessage {
  id: number
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at: string
  swipes?: string[]
  swipe_id?: number
  message_type?: 'chat' | 'narration' | 'compaction_summary' | 'system_instruction'
  metadata?: Record<string, unknown>
  is_important?: boolean
}

export interface UserSettings {
  name?: string
  avatar_path?: string
}

/* ---- companions ---- */

export const listCompanions = async (): Promise<Companion[]> => {
  const r = await get<{ companions?: Companion[] }>(`${C}/list`)
  return r.companions ?? []
}

export const getCompanion = async (id: string): Promise<Companion | null> => {
  const r = await get<{ companion?: Companion }>(`${C}/${encodeURIComponent(id)}`)
  return r.companion ?? null
}

export const createCompanion = (id: string, fields: Partial<Companion>): Promise<unknown> =>
  post(`${C}/${encodeURIComponent(id)}`, fields)

export const updateCompanion = (id: string, fields: Partial<Companion>): Promise<unknown> =>
  put(`${C}/${encodeURIComponent(id)}`, fields)

export const deleteCompanion = (id: string): Promise<unknown> =>
  del(`${C}/${encodeURIComponent(id)}`)

export const importTavernJson = (file: File): Promise<{ companion_id: string }> => {
  const fd = new FormData()
  fd.append('file', file)
  return postForm(`${C}/import/json`, fd)
}

export const importTavernPng = (file: File): Promise<{ companion_id: string }> => {
  const fd = new FormData()
  fd.append('file', file)
  return postForm(`${C}/import/png`, fd)
}

export const uploadCompanionAvatar = (id: string, file: File): Promise<{ avatar_path: string }> => {
  const fd = new FormData()
  fd.append('file', file)
  return postForm(`${C}/${encodeURIComponent(id)}/avatar`, fd)
}

export const exportCompanion = async (id: string, kind: 'json' | 'png'): Promise<Blob> => {
  const res = await fetch(`/api${C}/${encodeURIComponent(id)}/export/${kind}`)
  if (!res.ok) throw new Error(`export failed: HTTP ${res.status}`)
  return res.blob()
}

/* ---- branches ---- */

export const getBranches = async (companionId: string): Promise<CompanionBranch[]> => {
  const r = await get<{ branches?: CompanionBranch[] }>(`${C}/${encodeURIComponent(companionId)}/branches`)
  return r.branches ?? []
}

export const createBranch = (
  companionId: string,
  branchName: string,
  forkFromMessageId?: number,
): Promise<{ branch_id: string }> =>
  post(`${C}/${encodeURIComponent(companionId)}/branches`, {
    branch_name: branchName,
    fork_from_message_id: forkFromMessageId,
  })

export const renameBranch = (
  companionId: string,
  branchId: string,
  branchName: string,
): Promise<unknown> =>
  put(`${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}`, {
    branch_name: branchName,
  })

export const deleteBranch = (companionId: string, branchId: string): Promise<unknown> =>
  del(`${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}`)

export const clearBranchMessages = (
  companionId: string,
  branchId: string,
): Promise<{ deleted_count: number }> =>
  del(`${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/messages`)

/* ---- messages ---- */

export const getMessages = async (companionId: string, branchId: string): Promise<CompanionMessage[]> => {
  const r = await get<{ messages?: CompanionMessage[] }>(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/messages`,
  )
  return r.messages ?? []
}

export const getMessagesWithSwipes = async (
  companionId: string,
  branchId: string,
): Promise<CompanionMessage[]> => {
  const r = await get<{ messages?: CompanionMessage[] }>(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/messages-with-swipes`,
  )
  return r.messages ?? []
}

export const deleteSingleMessage = (
  companionId: string,
  branchId: string,
  messageId: number,
): Promise<unknown> =>
  del(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/messages/${messageId}`,
  )

export const updateMessage = (
  companionId: string,
  branchId: string,
  messageId: number,
  fields: { content?: string; is_important?: boolean },
): Promise<unknown> =>
  put(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/messages/${messageId}`,
    fields,
  )

export const swipeMessage = (
  companionId: string,
  branchId: string,
  messageId: number,
  swipeId: number,
): Promise<{ content: string; swipe_id: number; swipes: string[] }> =>
  put(`${C}/chat/swipe`, {
    companion_id: companionId,
    branch_id: branchId,
    message_id: messageId,
    swipe_id: swipeId,
  })

export const searchMessages = async (
  companionId: string,
  branchId: string,
  q: string,
): Promise<CompanionMessage[]> => {
  const r = await get<{ messages?: CompanionMessage[] }>(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/messages/search?q=${encodeURIComponent(q)}`,
  )
  return r.messages ?? []
}

/* ---- user ---- */

export const getUserSettings = async (): Promise<UserSettings> => {
  const r = await get<{ settings?: UserSettings }>(`${C}/user/settings`)
  return r.settings ?? {}
}

export const updateUserSettings = (name?: string, avatarPath?: string): Promise<unknown> => {
  const p = new URLSearchParams()
  if (name) p.append('name', name)
  if (avatarPath !== undefined) p.append('avatar_path', avatarPath)
  const qs = p.toString()
  return put(`${C}/user/settings${qs ? `?${qs}` : ''}`)
}

export const uploadUserAvatar = (file: File): Promise<{ avatar_path: string }> => {
  const fd = new FormData()
  fd.append('file', file)
  return postForm(`${C}/user/avatar`, fd)
}

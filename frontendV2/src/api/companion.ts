import {
  binaryGetRequest,
  bytesToBase64,
  del,
  get,
  getBaseURL,
  post,
  postForm,
  put,
  useSocket,
} from './client'
import type { DisplayRegexRule } from '@/utils/displayRegex'

const C = '/companions'

export interface CardPresentation {
  default_variables: Record<string, string>
  display_regex: DisplayRegexRule[]
  card_css: string
}

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
  card_ui?: Record<string, unknown>
  card_presentation?: CardPresentation
  pinned?: boolean
  asset_count?: number
  card_format?: string
  created_at: string
  updated_at: string
}

export interface CompanionAsset {
  id: number
  companion_id: string
  asset_type: string
  name: string
  ext: string
  mime_type: string
  rel_path: string
  source_uri: string
  size_bytes: number
  sort_order: number
  created_at: string
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

export const importTavernCharx = (file: File): Promise<{ companion_id: string }> => {
  const fd = new FormData()
  fd.append('file', file)
  return postForm(`${C}/import/charx`, fd)
}

/**
 * Import a character card (.charx, .zip, .png, or .json).
 *
 * Uses disk-backed chunked upload in the Tauri shell (where FormData fetch is
 * unreliable) and for any file larger than 4 MB so multi-hundred-megabyte
 * CHARX cards transfer steadily with progress feedback and minimal RAM use.
 */
export async function importCharacterCard(
  file: File,
  onProgress?: (pct: number) => void,
): Promise<{ companion_id: string }> {
  const lower = file.name.toLowerCase()
  const useChunked = useSocket() || file.size > 4 * 1024 * 1024

  if (!useChunked) {
    onProgress?.(20)
    const res = lower.endsWith('.png')
      ? await importTavernPng(file)
      : lower.endsWith('.charx') || lower.endsWith('.zip')
        ? await importTavernCharx(file)
        : await importTavernJson(file)
    onProgress?.(100)
    return res
  }

  const init = await post<{
    upload_id: string
    chunk_size: number
  }>(`${C}/import/upload/init`, {
    filename: file.name,
    total_size: file.size,
  })

  const chunkSize = Math.max(64 * 1024, init.chunk_size || 256 * 1024)
  let offset = 0
  try {
    while (offset < file.size) {
      const end = Math.min(offset + chunkSize, file.size)
      const buf = new Uint8Array(await file.slice(offset, end).arrayBuffer())
      await post(`${C}/import/upload/${encodeURIComponent(init.upload_id)}/chunk`, {
        offset,
        data_b64: bytesToBase64(buf),
      })
      offset = end
      onProgress?.(Math.min(95, Math.round((offset / Math.max(1, file.size)) * 95)))
    }
    const result = await post<{ companion_id: string }>(
      `${C}/import/upload/${encodeURIComponent(init.upload_id)}/commit`,
    )
    onProgress?.(100)
    return result
  } catch (err) {
    await del(`${C}/import/upload/${encodeURIComponent(init.upload_id)}`).catch(() => undefined)
    throw err
  }
}

export const uploadCompanionAvatar = (id: string, file: File): Promise<{ avatar_path: string }> => {
  const fd = new FormData()
  fd.append('file', file)
  return postForm(`${C}/${encodeURIComponent(id)}/avatar`, fd)
}

export const exportCompanion = async (
  id: string,
  kind: 'json' | 'png' | 'charx',
  onProgress?: (pct: number) => void,
): Promise<Blob> => {
  if (useSocket()) {
    if (kind === 'charx') {
      const prep = await post<{
        export_id: string
        filename: string
        total_size: number
        chunk_size: number
      }>(`${C}/${encodeURIComponent(id)}/export/charx/prepare`)

      const total = Math.max(0, prep.total_size || 0)
      const step = Math.max(64 * 1024, prep.chunk_size || 256 * 1024)
      const parts: BlobPart[] = []
      let offset = 0
      try {
        while (offset < total) {
          const slice = await binaryGetRequest(
            `${C}/export/download/${encodeURIComponent(prep.export_id)}/chunk?offset=${offset}&length=${step}`,
          )
          if (!slice.length) break
          parts.push(slice as BlobPart)
          offset += slice.length
          onProgress?.(total > 0 ? Math.min(100, Math.round((offset / total) * 100)) : 100)
        }
        onProgress?.(100)
        return new Blob(parts, { type: 'application/zip' })
      } finally {
        await del(`${C}/export/download/${encodeURIComponent(prep.export_id)}`).catch(
          () => undefined,
        )
      }
    }

    const bytes = await binaryGetRequest(`${C}/${encodeURIComponent(id)}/export/${kind}`)
    onProgress?.(100)
    return new Blob([bytes as BlobPart], {
      type: kind === 'png' ? 'image/png' : 'application/json',
    })
  }

  const res = await fetch(`${getBaseURL()}${C}/${encodeURIComponent(id)}/export/${kind}`)
  if (!res.ok) throw new Error(`export failed: HTTP ${res.status}`)
  const total = Number(res.headers.get('Content-Length') || 0)
  if (res.body && total > 0 && onProgress) {
    const reader = res.body.getReader()
    const chunks: BlobPart[] = []
    let received = 0
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      chunks.push(value as BlobPart)
      received += value.length
      onProgress(Math.min(100, Math.round((received / total) * 100)))
    }
    return new Blob(chunks, {
      type: kind === 'charx' ? 'application/zip' : kind === 'png' ? 'image/png' : 'application/json',
    })
  }
  const blob = await res.blob()
  onProgress?.(100)
  return blob
}

/* ---- companion assets ---- */

export const listCompanionAssets = async (
  companionId: string,
  assetType?: string,
): Promise<CompanionAsset[]> => {
  const qs = assetType ? `?asset_type=${encodeURIComponent(assetType)}` : ''
  const r = await get<{ assets?: CompanionAsset[] }>(
    `${C}/${encodeURIComponent(companionId)}/assets${qs}`,
  )
  return r.assets ?? []
}

export const uploadCompanionAsset = (
  companionId: string,
  file: File,
  assetType = 'other',
  name = '',
): Promise<{ asset: CompanionAsset }> => {
  const fd = new FormData()
  fd.append('file', file)
  fd.append('asset_type', assetType)
  if (name) fd.append('name', name)
  return postForm(`${C}/${encodeURIComponent(companionId)}/assets`, fd)
}

export const deleteCompanionAsset = (companionId: string, assetId: number): Promise<unknown> =>
  del(`${C}/${encodeURIComponent(companionId)}/assets/${assetId}`)

export const setAvatarFromAsset = (
  companionId: string,
  assetId: number,
): Promise<{ avatar_path: string }> =>
  post(`${C}/${encodeURIComponent(companionId)}/avatar/from-asset/${assetId}`)

/* ---- branch stage HUD ---- */

export interface CardHudState {
  hud: Record<string, unknown>
  enabled: boolean | null
  effective_enabled: boolean
  has_assets: boolean
}

export const getBranchHud = (companionId: string, branchId: string): Promise<CardHudState> =>
  get<CardHudState>(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/hud`,
  )

export const updateBranchHud = (
  companionId: string,
  branchId: string,
  payload: { hud?: Record<string, unknown>; enabled?: boolean; clear_enabled?: boolean },
): Promise<CardHudState> =>
  put<CardHudState>(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/hud`,
    payload,
  )

export const resetBranchHud = (companionId: string, branchId: string): Promise<CardHudState> =>
  post<CardHudState>(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/hud/reset`,
  )

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

/* ---- branch CBS variables ---- */

export interface BranchVariablesPayload {
  defaults: Record<string, string>
  variables: Record<string, string>
}

export const getBranchVariables = (
  companionId: string,
  branchId: string,
): Promise<BranchVariablesPayload> =>
  get(`${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/variables`)

export const updateBranchVariables = (
  companionId: string,
  branchId: string,
  variables: Record<string, string>,
): Promise<BranchVariablesPayload> =>
  put(`${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/variables`, {
    variables,
  })

export const resetBranchVariables = (
  companionId: string,
  branchId: string,
): Promise<BranchVariablesPayload> =>
  post(`${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/variables/reset`, {})

export interface RisumImportResult {
  status: string
  module_name: string
  added_regex: number
  added_css: boolean
  added_assets: number
  card_presentation?: CardPresentation
}

export const importCompanionRisum = async (
  companionId: string,
  file: File,
): Promise<RisumImportResult> => {
  const id = encodeURIComponent(companionId)
  if (!useSocket() && file.size <= 4 * 1024 * 1024) {
    const fd = new FormData()
    fd.append('file', file)
    return postForm(`${C}/${id}/import/risum`, fd)
  }
  const buf = new Uint8Array(await file.arrayBuffer())
  return post(`${C}/${id}/import/risum/json`, {
    filename: file.name,
    data_b64: bytesToBase64(buf),
  })
}

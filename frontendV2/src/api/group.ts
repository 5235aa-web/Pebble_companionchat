import { del, get, patch, post } from './client'
import { run, runAudioStream, type LlmConfig, type StreamHandlers } from './stream'
import type { CompanionLocale, CompanionTranslations } from '@/utils/companionTranslation'

/**
 * Group rooms.
 *
 * Separate from the companion API on purpose: a room has its own sessions,
 * members, messages, memory and compaction, and none of these calls can reach a
 * companion thread.
 */
export interface GroupSession {
  id: number
  name: string
  scene: string
  user_note: string
  /** Filled in by the list route only, for the room column. */
  member_count?: number
  message_count?: number
  created_at?: string
  updated_at?: string
}

export interface GroupMember {
  companion_id: string
  name: string
  description?: string
  personality?: string
  scenario?: string
  avatar_path?: string
  user_info?: string
  translations?: CompanionTranslations
  active_locale?: CompanionLocale
  prompt_use_original?: boolean
  turn_order: number
  focus: number
}

export interface GroupAttachment {
  kind: string
  name?: string
  url?: string
  poster_url?: string
  path?: string
  mime?: string
  size?: number
  width?: number
  height?: number
  duration?: number
  frames?: Array<{ time: number; url: string }>
  transcript?: string
  content?: string
}

export interface GroupMessage {
  id: number
  session_id: number
  speaker_id: string | null
  speaker_name: string
  role: string
  kind: string
  content: string
  /** Pictures sent with this line; recent turns are also sent to vision models. */
  attachments?: GroupAttachment[]
  created_at?: string
}

export interface GroupMemory {
  id: number
  content: string
  about: string
  keywords: string
  /** 0 plain, 1 pinned, 2 strong. */
  mark?: number
  /** How many replies pulled this memory in. */
  use_count?: number
  created_at?: string
  last_used_at?: string
}

export interface GroupCompaction {
  id: number
  summary: string
  /** The message id the summary reached — a boundary, not a row count. */
  messages_compacted: number
  /** How many messages that boundary actually covers, computed by the backend. */
  covered_count?: number
  created_at?: string
}

export interface GroupDetail {
  session: GroupSession
  members: GroupMember[]
  messages: GroupMessage[]
  memories: GroupMemory[]
  /** The newest summary only; the full list lives on its own route. */
  compaction: GroupCompaction | null
}

export const listGroupSessions = (): Promise<{ sessions: GroupSession[] }> =>
  get('/group/sessions')

export const createGroupSession = (body: {
  name?: string
  scene?: string
  user_note?: string
}): Promise<{ session: GroupSession }> => post('/group/sessions', body)

export const getGroupSession = (id: number): Promise<GroupDetail> =>
  get(`/group/sessions/${id}`)

export const patchGroupSession = (
  id: number,
  body: Partial<Pick<GroupSession, 'name' | 'scene' | 'user_note'>>,
): Promise<{ session: GroupSession }> => patch(`/group/sessions/${id}`, body)

export const deleteGroupSession = (id: number): Promise<unknown> =>
  del(`/group/sessions/${id}`)

export const addGroupMember = (id: number, companionId: string): Promise<unknown> =>
  post(`/group/sessions/${id}/members`, { companion_id: companionId })

export const removeGroupMember = (id: number, companionId: string): Promise<unknown> =>
  del(`/group/sessions/${id}/members/${encodeURIComponent(companionId)}`)

export const setGroupFocus = (id: number, companionId: string): Promise<unknown> =>
  post(`/group/sessions/${id}/focus`, { companion_id: companionId })

export const listGroupMemories = (id: number): Promise<{ memories: GroupMemory[] }> =>
  get(`/group/sessions/${id}/memories`)

export const deleteGroupMemory = (id: number, memoryId: number): Promise<unknown> =>
  del(`/group/sessions/${id}/memories/${memoryId}`)

/** Edit a memory. Changing the text re-encodes it on the backend. */
export const updateGroupMemory = (
  id: number,
  memoryId: number,
  body: { content?: string; keywords?: string; about?: string; mark?: number },
): Promise<unknown> => patch(`/group/sessions/${id}/memories/${memoryId}`, body)

export const listGroupCompactions = (
  id: number,
): Promise<{ compactions: GroupCompaction[] }> => get(`/group/sessions/${id}/compactions`)

/**
 * One turn: the user's line is stored, a speaker is chosen, the reply streams
 * back, and the result is persisted, all server-side.
 */
export function groupStream(
  sessionId: number,
  message: string,
  llmConfig: LlmConfig,
  h: StreamHandlers,
  sender: 'user' | 'narrator' = 'user',
  attachments: GroupAttachment[] = [],
  speakerId?: string,
): AbortController {
  return run(
    `/group/sessions/${sessionId}/stream`,
    {
      message,
      llm_config: llmConfig,
      sender,
      attachments,
      ...(speakerId ? { speaker_id: speakerId } : {}),
    },
    h,
  )
}

/**
 * Voice turn in a group room. Uploads WAV audio; the backend transcribes it,
 * picks or honors the speaker, forwards raw audio when supported, and streams
 * the reply back over SSE.
 */
export function groupAudioStream(
  sessionId: number,
  audio: Blob,
  llmConfig: LlmConfig,
  h: StreamHandlers,
  opts: { sender?: 'user' | 'narrator'; speakerId?: string } = {},
): AbortController {
  const sender = opts.sender ?? 'user'
  const speakerId = opts.speakerId
  const formFields: Record<string, string> = {
    llm_config: JSON.stringify(llmConfig),
    sender,
  }
  if (speakerId) formFields.speaker_id = speakerId

  return runAudioStream({
    jsonPath: `/group/sessions/${sessionId}/audio/stream-json`,
    multipartPath: `/group/sessions/${sessionId}/audio/stream`,
    audio,
    jsonBody: {
      llm_config: llmConfig,
      sender,
      ...(speakerId ? { speaker_id: speakerId } : {}),
    },
    formFields,
    handlers: h,
  })
}

/** Replace the latest assistant line, optionally handing the turn to another member. */
export function groupRerollStream(
  sessionId: number,
  messageId: number,
  llmConfig: LlmConfig,
  h: StreamHandlers,
  opts: { speakerId?: string; switchSpeaker?: boolean } = {},
): AbortController {
  return run(
    `/group/sessions/${sessionId}/reroll/stream`,
    {
      message_id: messageId,
      llm_config: llmConfig,
      ...(opts.speakerId ? { speaker_id: opts.speakerId } : {}),
      ...(opts.switchSpeaker ? { switch_speaker: true } : {}),
    },
    h,
  )
}

/** Remove one line from the transcript. */
export const deleteGroupMessage = (sessionId: number, messageId: number): Promise<unknown> =>
  del(`/group/sessions/${sessionId}/messages/${messageId}`)

/**
 * Empty the transcript. `withMembers` also clears the cast; the group composer
 * leaves it false, so clearing keeps the cast and only removes the lines.
 */
export const clearGroupMessages = (sessionId: number, withMembers = false) =>
  del<{ ok: boolean; removed: number }>(
    `/group/sessions/${sessionId}/messages?with_members=${withMembers}`,
  )

export interface GroupCompactedBatch {
  batch_id: string
  message_count: number
  started_at?: string
  ended_at?: string
  messages: GroupMessage[]
}

/** The turns the newest summary covers — the "view source" of a compaction. */
export const compactedGroupMessages = (sessionId: number) =>
  get<{ messages: GroupMessage[]; batches?: GroupCompactedBatch[] }>(
    `/group/sessions/${sessionId}/compacted`,
  )

export interface GroupImage {
  url: string
  name: string
  mime: string
  size: number
  width: number
  height: number
  message_id: number
  created_at: string
}

/** Pictures the room's messages reference, newest first. */
export const getGroupImages = (
  sessionId: number,
): Promise<{ images: GroupImage[]; count: number }> =>
  get(`/group/sessions/${sessionId}/images`)

/** Remove one picture: the stored file and every message reference to it. */
export const deleteGroupImage = (sessionId: number, url: string): Promise<unknown> =>
  del(`/group/sessions/${sessionId}/images?url=${encodeURIComponent(url)}`)

/** Token footprint of this room's next turn, built from the message list it sends. */
export const groupContext = (sessionId: number, model: string, providerType: string) =>
  get<{ tokens: number; messages: number; members: number }>(
    `/group/sessions/${sessionId}/context?model=${encodeURIComponent(model)}&provider_type=${encodeURIComponent(providerType)}`,
  )

/**
 * Compact on demand. Same trigger model as the single thread: nothing happens
 * in the background, the button is the only way a summary gets written.
 */
export const compactGroup = (sessionId: number, llmConfig: LlmConfig) =>
  post<{ summary: string; messages_compacted: number }>(
    `/group/sessions/${sessionId}/compact`,
    { llm_config: llmConfig },
  )

import { getBaseURL, streamRequest, useSocket } from './client'
import type { GenerationMeta } from '@/data/chat'

export interface ChatChunk {
  type:
    | 'content'
    | 'thinking'
    | 'done'
    | 'error'
    | 'stt_transcript'
    | 'script_state'
    | 'card_hud'
    | 'speaker'
  content?: string
  final_content?: string
  message?: string
  /** Present on a group turn's first frame: which member is about to speak. */
  speaker?: { companion_id?: string; name?: string }
  /** Present on a group turn's first frame when a user/narration line was persisted. */
  user_message?: { id: number; [key: string]: unknown }
  /** Present on script_state / card_hud frames: live HUD state. */
  hud?: Record<string, unknown>
  /** Present on a card_hud frame: expression / scene / bgm / sfx triggers. */
  media_cue?: Record<string, unknown>
  scene_done?: boolean
  turns_done?: number
  /** Present on the done frame: the id the assistant message was stored under. */
  message_id?: number
  /**
   * Present on the done frame of a fresh send: the id the user's own message
   * was stored under. The beat was rendered before the request with a
   * temporary negative id, and search results and memory sources reference the
   * database id, so the beat has to adopt it or nothing can scroll to it.
   */
  user_message_id?: number
  /**
   * Present on the done frame: the memories that were injected into this
   * reply. Declared structurally rather than imported from the session store
   * so the api layer stays free of store imports.
   */
  memories?: Array<{ id: number; label: string; weight: number; key: string }>
  usage?: {
    prompt_tokens?: number
    completion_tokens?: number
    total_tokens?: number
  }
  /** Present on the done frame: model, duration and tokens for this reply. */
  generation?: GenerationMeta
}

export interface LlmConfig {
  provider_type: string
  api_key: string
  base_url: string
  model: string
  temperature?: number
  top_p?: number
  max_tokens?: number
  // Only sent for llama-cpp; the backend drops them for cloud providers.
  top_k?: number | null
  min_p?: number | null
  repeat_penalty?: number | null
  // Only sent for OpenAI-compatible providers.
  frequency_penalty?: number | null
  presence_penalty?: number | null
}

/**
 * Shape the backend reads: `kind` decides image vs document, `url` points at a
 * stored upload, `content` carries extracted text.
 */
export interface AttachmentRef {
  kind: string
  name?: string
  url?: string
  path?: string
  content?: string
  mime?: string
}

export interface StreamHandlers {
  onChunk: (chunk: ChatChunk) => void
  onError: (err: Error) => void
  onComplete: () => void
}

/**
 * Reads an SSE response and turns `data:` lines into chunks.
 * Streaming endpoints talk to the backend directly: the Vite proxy buffers
 * the body and collapses the stream into one delivery.
 */
async function consume(res: Response, h: StreamHandlers): Promise<void> {
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`)
  }
  if (!res.body) throw new Error('empty response body')

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''

    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed.startsWith('data:')) continue
      const payload = trimmed.slice(5).trim()
      if (!payload || payload === '[DONE]') continue
      try {
        h.onChunk(JSON.parse(payload) as ChatChunk)
      } catch {
        // A malformed frame must not tear down the stream.
      }
    }
  }
  h.onComplete()
}

/**
 * SSE runner shared by every streaming endpoint, chat and group alike: one
 * line parser, one abort path, one place where a malformed frame is ignored
 * rather than allowed to tear the stream down.
 */
export function run(
  path: string,
  body: unknown,
  h: StreamHandlers,
  controller: AbortController = new AbortController(),
): AbortController {
  let buffer = ''

  // The socket delivers raw bytes; the payloads are still SSE frames, so the
  // same line parser applies whether they arrive over fetch or the socket.
  const feed = (text: string) => {
    buffer += text
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed.startsWith('data:')) continue
      const payload = trimmed.slice(5).trim()
      if (!payload || payload === '[DONE]') continue
      try {
        h.onChunk(JSON.parse(payload) as ChatChunk)
      } catch {
        // A malformed frame must not tear down the stream.
      }
    }
  }

  streamRequest(path, body, feed, { signal: controller.signal })
    .then(() => h.onComplete())
    .catch((err: unknown) => {
      if (err instanceof Error && err.name === 'AbortError') return
      h.onError(err instanceof Error ? err : new Error(String(err)))
    })

  return controller
}

export function chatStream(
  companionId: string,
  branchId: string,
  message: string,
  llmConfig: LlmConfig,
  enableThinking: boolean,
  h: StreamHandlers,
  attachments: AttachmentRef[] = [],
  sender: 'user' | 'narrator' = 'user',
  scriptSessionId = 0,
): AbortController {
  return run(
    '/companions/chat/stream',
    {
      message,
      companion_id: companionId,
      branch_id: branchId,
      enable_thinking: enableThinking,
      llm_config: llmConfig,
      attachments,
      sender,
      script_session_id: scriptSessionId || undefined,
    },
    h,
  )
}

export function continueStream(
  companionId: string,
  branchId: string,
  llmConfig: LlmConfig,
  enableThinking: boolean,
  h: StreamHandlers,
): AbortController {
  return run(
    '/companions/chat/continue/stream',
    {
      companion_id: companionId,
      branch_id: branchId,
      enable_thinking: enableThinking,
      llm_config: llmConfig,
      temperature: llmConfig.temperature ?? 0.7,
      top_p: llmConfig.top_p ?? 0.9,
    },
    h,
  )
}

export function regenerateStream(
  companionId: string,
  branchId: string,
  messageId: number,
  llmConfig: LlmConfig,
  enableThinking: boolean,
  h: StreamHandlers,
): AbortController {
  return run(
    '/companions/chat/regenerate/stream',
    {
      companion_id: companionId,
      branch_id: branchId,
      message_id: messageId,
      enable_thinking: enableThinking,
      llm_config: llmConfig,
    },
    h,
  )
}

/**
 * Rewrite a user message and let the model answer again.
 *
 * The server drops every message after the edited one, so the caller must
 * discard those beats locally too; the `done` frame carries only the new reply.
 */
export function reeditStream(
  companionId: string,
  branchId: string,
  messageId: number,
  content: string,
  llmConfig: LlmConfig,
  enableThinking: boolean,
  h: StreamHandlers,
): AbortController {
  return run(
    '/companions/chat/reedit/stream',
    {
      companion_id: companionId,
      branch_id: branchId,
      message_id: messageId,
      content,
      enable_thinking: enableThinking,
      llm_config: llmConfig,
    },
    h,
  )
}

/**
 * Audio upload. The response is SSE, same as the text path.
 *
 * Over the socket the clip travels base64-encoded in the JSON body, because
 * the bridge only carries JSON frames. Over HTTP it stays multipart.
 */
export function audioStream(
  companionId: string,
  branchId: string,
  audio: Blob,
  llmConfig: LlmConfig,
  enableThinking: boolean,
  h: StreamHandlers,
  sender: 'user' | 'narrator' = 'user',
): AbortController {
  const controller = new AbortController()
  const extra = {
    companion_id: companionId,
    branch_id: branchId,
    enable_thinking: enableThinking,
    llm_config: llmConfig,
    temperature: llmConfig.temperature ?? 0.7,
    top_p: llmConfig.top_p ?? 0.9,
    sender,
  }

  if (useSocket()) {
    void audio
      .arrayBuffer()
      .then((buf) => {
        const bytes = new Uint8Array(buf)
        // Chunked: a multi-megabyte spread overflows the call stack.
        let bin = ''
        const STEP = 0x8000
        for (let i = 0; i < bytes.length; i += STEP) {
          bin += String.fromCharCode(...bytes.subarray(i, i + STEP))
        }
        // The user may have hit stop while the clip was being encoded.
        if (controller.signal.aborted) return
        run(
          '/companions/chat/audio/stream-json',
          { ...extra, audio_b64: btoa(bin) },
          h,
          controller,
        )
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        h.onError(err instanceof Error ? err : new Error(String(err)))
      })
    return controller
  }

  const form = new FormData()
  form.append('file', audio, 'recording.wav')
  form.append('companion_id', companionId)
  form.append('branch_id', branchId)
  form.append('enable_thinking', String(enableThinking))
  form.append('llm_config', JSON.stringify(llmConfig))
  form.append('temperature', String(llmConfig.temperature ?? 0.7))
  form.append('top_p', String(llmConfig.top_p ?? 0.9))
  form.append('sender', sender)

  fetch(`${getBaseURL()}/companions/chat/audio/stream`, {
    method: 'POST',
    body: form,
    signal: controller.signal,
  })
    .then((res) => consume(res, h))
    .catch((err: unknown) => {
      if (err instanceof Error && err.name === 'AbortError') return
      h.onError(err instanceof Error ? err : new Error(String(err)))
    })
  return controller
}

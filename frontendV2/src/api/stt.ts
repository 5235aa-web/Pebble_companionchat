import { del, get, post, postForm } from './client'
import type { AudioModelSpec } from './tts'

export interface SttEngineInfo {
  id: string
  label: string
  desc: string
  kind?: 'builtin' | 'audiocpp' | 'openai_stt'
  builtin?: boolean
  family?: string
  modelPath?: string
  resolvedModelPath?: string
  defaultModelPath?: string
  modelExists?: boolean
  computeBackend?: string
  baseUrl?: string
  apiKey?: string
  model?: string
  language?: string
  requestOptions?: Record<string, unknown>
  sessionOptions?: Record<string, unknown>
  loadOptions?: Record<string, unknown>
  loaded: boolean
  error: string | null
}

export interface SttConfigPayload {
  engines: SttEngineInfo[]
  active: string
}

export interface SttEngineSavePayload {
  id?: string
  label?: string
  kind?: 'builtin' | 'audiocpp' | 'openai_stt'
  family?: string
  modelPath?: string
  computeBackend?: string
  baseUrl?: string
  apiKey?: string
  model?: string
  language?: string
  requestOptions?: Record<string, unknown>
  sessionOptions?: Record<string, unknown>
  loadOptions?: Record<string, unknown>
}

export const fetchSttConfig = (): Promise<SttConfigPayload> => get('/companions/stt/config')

export const fetchSttSpecs = (): Promise<AudioModelSpec[]> =>
  get<{ specs?: AudioModelSpec[] }>('/companions/stt/specs').then((r) => r.specs ?? [])

export const saveSttConfig = (active: string): Promise<{ status: string; active: string }> =>
  post('/companions/stt/config', { active })

export const saveSttEngine = (
  payload: SttEngineSavePayload,
): Promise<{ status: string; id: string } & SttConfigPayload> =>
  post('/companions/stt/engines', payload)

export const deleteSttEngine = (
  id: string,
): Promise<{ status: string } & SttConfigPayload> =>
  del(`/companions/stt/engines/${encodeURIComponent(id)}`)

export type SttStopVerdict = 'stopped' | 'idle' | 'failed'

export interface SttStopResult {
  engine: string
  /** The engine's own verdict: ended, nothing was listening, or refused. */
  result: SttStopVerdict
  active: string
}

/** End the active engine's server and free its VRAM immediately. */
export const stopSttEngine = (): Promise<SttStopResult> =>
  post('/companions/stt/stop', {})

export const transcribeAudio = async (wav: Blob, timeoutMs = 60000): Promise<string> => {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const fd = new FormData()
    fd.append('file', wav, 'recording.wav')
    fd.append('sample_rate', '16000')
    const r = await postForm<{ text?: string }>('/companions/stt', fd, { signal: controller.signal })
    return (r.text || '').trim()
  } finally {
    clearTimeout(timer)
  }
}

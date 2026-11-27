import { binaryRequest, binaryStreamRequest, get, getBaseURL, post, useSocket } from './client'

/**
 * One TTS config. The field set is the union of every engine's options: the
 * backend stores one blob per config and each engine reads only its own keys.
 */
export interface TtsConfig {
  id: string
  name?: string
  provider: string
  apiKey?: string
  baseUrl?: string
  model?: string
  voiceName?: string
  voiceId?: string
  emotion?: string
  speed?: number
  outputFormat?: string

  modelPath?: string
  computeBackend?: string
  audiocppFamily?: string
  audiocppTask?: string
  audiocppOptions?: Record<string, unknown>
  audiocppSessionOptions?: Record<string, unknown>
  audiocppLoadOptions?: Record<string, unknown>
  streamEnabled?: boolean
  sampleRate?: number | string
  speakStageDirections?: boolean


  omniVoice?: string
  omniInstruct?: string
  omniSpeed?: number
  omniNumSteps?: string | number

  dotsVoice?: string
  dotsLanguage?: string
  dotsNumSteps?: string | number

  fishVoice?: string
  fishTemperature?: number
  fishTopP?: number
  fishTopK?: number

  ming3Task?: string
  ming3Voice?: string
  ming3Template?: string
  ming3Language?: string
  ming3Instruct?: string
  ming3InstructionSchema?: string
  ming3Speed?: string
  ming3Pitch?: string
  ming3Volume?: string
  ming3Emotion?: string
  ming3Dialect?: string
  ming3VoiceId?: string
  ming3ReferenceText?: string
  ming3SpeakerEmbeddingFile?: string
  ming3ExportSpeakerEmbeddingFile?: string
  ming3UseSpeakerEmbedding?: boolean
  ming3ZeroSpeakerEmbedding?: boolean
  ming3MaxTokens?: number | string
  ming3GuidanceScale?: number | string
  ming3Sigma?: number | string
  ming3Temperature?: number | string
  ming3Seed?: number | string
  ming3RawText?: boolean
  ming3TextChunkSize?: number | string
  ming3TextChunkMode?: string
  ming3TrimSilence?: boolean
  ming3StreamFramesPerEvent?: number | string
  ming3StreamChunkPolicy?: string
  ming3ReferenceCache?: boolean
}

export interface TtsConfigPayload {
  configs: TtsConfig[]
  activeIndex: number
}

export type TtsEngineKind = 'local' | 'cloud'

export interface TtsEngineOption {
  value: string
  label: string
  /** Local engines hold VRAM and share one GPU; cloud engines are HTTP only. */
  kind: TtsEngineKind
}

export const TTS_ENGINES: TtsEngineOption[] = [
  { value: 'omnivoice', label: 'OmniVoice', kind: 'local' },
  { value: 'dotstts', label: 'DotTTS', kind: 'local' },
  { value: 'fishaudio', label: 'Fish Audio', kind: 'local' },
  { value: 'ming3tts', label: 'Ming 3.0', kind: 'local' },
  { value: 'audiocpp_tts', label: 'Custom GGUF (audio.cpp)', kind: 'local' },
  { value: 'openai_tts', label: 'Local / OpenAI HTTP', kind: 'cloud' },
  { value: 'minimax', label: 'MiniMax', kind: 'cloud' },
  { value: 'elevenlabs', label: 'ElevenLabs', kind: 'cloud' },
]

/** Engines that can have multiple independent configs in the tab strip. */
export const MULTI_INSTANCE_TTS_ENGINES = new Set(['audiocpp_tts', 'openai_tts'])

export interface AudioSpecOption {
  name: string
  type: string
  description: string
  required: boolean
  default?: unknown
  values?: unknown[] | null
  min?: number | null
  max?: number | null
}

export interface AudioModelSpec {
  family: string
  displayName: string
  description: string
  category: string
  status: string
  tasks: string[]
  serverTasks?: string[]
  modes: string[]
  sampleRate?: number | null
  pathKind?: 'file' | 'dir' | 'either'
  supportsVoiceId?: boolean
  supportsReferenceText?: boolean
  languages: string[]
  requestOptions: AudioSpecOption[]
  sessionOptions?: AudioSpecOption[]
  loadOptions?: AudioSpecOption[]
}

/** Engines that hold VRAM locally; only one should be resident at a time. */
const LOCAL_ENGINES = new Set(
  TTS_ENGINES.filter((e) => e.kind === 'local').map((e) => e.value),
)

/** Cloud engines speak over HTTP: no VRAM, and voice ids live in the panel. */
export function isCloudEngine(provider: string): boolean {
  return TTS_ENGINES.find((e) => e.value === provider)?.kind === 'cloud'
}

/**
 * What a fresh config of each engine starts with. The four local engines share
 * one reference clip; cloud engines need an endpoint and a model before they
 * can speak at all, so their defaults mirror the backend's own fallbacks.
 */
export const TTS_ENGINE_DEFAULTS: Record<string, Partial<TtsConfig>> = {
  omnivoice: { omniVoice: 'mayathesecond' },
  dotstts: { dotsVoice: 'mayathesecond' },
  fishaudio: { fishVoice: 'mayathesecond' },
  ming3tts: { ming3Voice: 'mayathesecond' },
  audiocpp_tts: {
    audiocppFamily: 'kokoro_tts',
    audiocppTask: 'tts',
    computeBackend: 'vulkan',
    audiocppOptions: {},
    audiocppSessionOptions: {},
    audiocppLoadOptions: {},
  },
  openai_tts: {
    baseUrl: 'http://127.0.0.1:8880/v1',
    model: 'kokoro',
    voiceId: 'af_heart',
    outputFormat: 'wav',
    speed: 1,
  },
  minimax: {
    baseUrl: 'https://api.minimaxi.com/v1',
    model: 'speech-2.8-turbo',
    voiceId: 'female-shaonv',
    emotion: 'calm',
    speed: 1,
  },
  elevenlabs: {
    baseUrl: 'https://api.elevenlabs.io',
    model: 'eleven_multilingual_v2',
    outputFormat: 'mp3_44100_128',
  },
}

export const fetchTtsSpecs = (): Promise<AudioModelSpec[]> =>
  get<{ specs?: AudioModelSpec[] }>('/tts/specs').then((r) => r.specs ?? [])

export const getTtsConfig = (): Promise<TtsConfigPayload> => get('/tts/config')

export const saveTtsConfig = (payload: TtsConfigPayload): Promise<unknown> =>
  post('/tts/config', payload)

export const getTtsVoices = (): Promise<string[]> =>
  get<{ voices?: string[] }>('/tts/voices').then((r) => r.voices ?? [])

export const releaseOtherTts = (keep: string): Promise<string[]> =>
  post<{ released?: string[] }>('/tts/release_others', { keep }).then((r) => r.released ?? [])

/** Map a stored config onto the flat snake_case body POST /tts expects. */
/**
 * Numeric fields are edited as text, so an untouched one is "" rather than
 * undefined. Sending it verbatim makes the backend reject the whole request;
 * dropping it lets the engine use its own default.
 */
function num(v: unknown): unknown {
  if (v === '' || v === undefined || v === null) return undefined
  return v
}

/**
 * Ming3's 26 request options. audio.cpp rejects unknown keys and empty enum
 * values outright, so blank strings are dropped rather than sent; booleans are
 * always included because `false` is a meaningful choice for the speaker
 * embedding flags.
 */
function buildMing3Options(c: TtsConfig): Record<string, unknown> {
  const opts: Record<string, unknown> = {}
  const putStr = (key: string, v?: string) => {
    const s = (v || '').trim()
    if (s) opts[key] = s
  }
  const putNum = (key: string, v?: number | string) => {
    const s = String(v ?? '').trim()
    if (!s) return
    const n = Number(s)
    if (!Number.isNaN(n)) opts[key] = n
  }
  putStr('template_name', c.ming3Template)
  putStr('language', c.ming3Language)
  putStr('instruct', c.ming3Instruct)
  putStr('instruction_schema', c.ming3InstructionSchema)
  putStr('speed', c.ming3Speed)
  putStr('pitch', c.ming3Pitch)
  putStr('volume', c.ming3Volume)
  putStr('emotion', c.ming3Emotion)
  putStr('dialect', c.ming3Dialect)
  putStr('voice_id', c.ming3VoiceId)
  putStr('reference_text', c.ming3ReferenceText)
  putStr('speaker_embedding_file', c.ming3SpeakerEmbeddingFile)
  putStr('export_speaker_embedding_file', c.ming3ExportSpeakerEmbeddingFile)
  putStr('text_chunk_mode', c.ming3TextChunkMode)
  putStr('stream_chunk_policy', c.ming3StreamChunkPolicy)
  putNum('max_tokens', c.ming3MaxTokens)
  putNum('guidance_scale', c.ming3GuidanceScale)
  putNum('sigma', c.ming3Sigma)
  putNum('temperature', c.ming3Temperature)
  putNum('seed', c.ming3Seed)
  putNum('text_chunk_size', c.ming3TextChunkSize)
  putNum('stream_frames_per_event', c.ming3StreamFramesPerEvent)
  if (typeof c.ming3UseSpeakerEmbedding === 'boolean') {
    opts.use_speaker_embedding = c.ming3UseSpeakerEmbedding
  }
  if (typeof c.ming3ZeroSpeakerEmbedding === 'boolean') {
    opts.zero_speaker_embedding = c.ming3ZeroSpeakerEmbedding
  }
  if (typeof c.ming3RawText === 'boolean') opts.raw_text = c.ming3RawText
  if (typeof c.ming3TrimSilence === 'boolean') opts.trim_silence = c.ming3TrimSilence
  return opts
}

function toRequest(c: TtsConfig) {
  return {
    provider: c.provider,
    api_key: c.apiKey,
    base_url: c.baseUrl,
    model: c.model,
    voice_name: c.voiceName,
    voice_id: c.voiceId,
    emotion: c.emotion,
    speed: num(c.speed),
    output_format: c.outputFormat,
    model_path: c.modelPath || undefined,
    compute_backend: c.computeBackend || undefined,
    audiocpp_family: c.audiocppFamily || undefined,
    audiocpp_task: c.audiocppTask || undefined,
    audiocpp_options: c.audiocppOptions || undefined,
    audiocpp_session_options: c.audiocppSessionOptions || undefined,
    audiocpp_load_options: c.audiocppLoadOptions || undefined,
    sample_rate: num(c.sampleRate),
    speak_stage_directions: c.speakStageDirections,
    omni_voice: c.omniVoice,
    omni_instruct: c.omniInstruct,
    omni_speed: num(c.omniSpeed),
    omni_num_steps: num(c.omniNumSteps),
    dots_voice: c.dotsVoice,
    dots_language: c.dotsLanguage,
    dots_num_steps: num(c.dotsNumSteps),
    fish_voice: c.fishVoice,
    fish_temperature: num(c.fishTemperature),
    fish_top_p: num(c.fishTopP),
    fish_top_k: num(c.fishTopK),
    ming3_task: c.ming3Task,
    ming3_voice: c.ming3Voice,
    ming3_options: buildMing3Options(c),
  }
}

/**
 * Container for the bytes coming back over the socket path. There is no
 * Content-Type header there, so it has to be derived: MiniMax always answers in
 * mp3, ElevenLabs answers in whatever `output_format` asks for (e.g.
 * `mp3_44100_128`, `opus_48000_64`), and the local engines emit wav.
 */
function audioMime(config: TtsConfig): string {
  if (config.provider === 'minimax') return 'audio/mpeg'
  const fmt = (config.outputFormat || 'wav').toLowerCase()
  if (fmt.startsWith('mp3') || fmt.includes('mpeg')) return 'audio/mpeg'
  if (fmt.startsWith('opus') || fmt.startsWith('ogg')) return 'audio/ogg'
  return `audio/${fmt}`
}

export async function synthesize(
  config: TtsConfig,
  text: string,
  signal?: AbortSignal,
): Promise<Blob> {
  // Reclaim the other local engines' VRAM right before this one starts. Doing
  // it here — rather than when the provider is picked in the panel — keeps
  // browsing engines free: nothing is loaded or unloaded until speech is
  // actually requested.
  if (LOCAL_ENGINES.has(config.provider)) {
    await releaseOtherTts(config.provider).catch(() => undefined)
  }
  const payload = { ...toRequest(config), text }
  if (useSocket()) {
    // Loopback HTTP is not reliable from the shell, so the audio comes back
    // over the same socket the rest of the API uses. There is no response
    // header on that path, so the container has to be inferred from the config.
    const bytes = await binaryRequest('/tts', payload, signal)
    return new Blob([bytes as BlobPart], { type: audioMime(config) })
  }
  const res = await fetch(`${getBaseURL()}/tts`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`TTS failed: ${detail.slice(0, 160)}`)
  }
  return res.blob()
}
/**
 * Engines whose backend exposes a streaming PCM endpoint, with the sample rate
 * each one emits (audiocpp_tts resolves per-family via specs / config override).
 */
export const STREAM_SAMPLE_RATES: Record<string, number> = {
  omnivoice: 24000,
  dotstts: 48000,
  ming3tts: 44100,
  audiocpp_tts: 24000,
}

export const canStream = (provider: string): boolean => provider in STREAM_SAMPLE_RATES

export function supportsStreaming(config: TtsConfig, specs?: AudioModelSpec[]): boolean {
  if (!canStream(config.provider)) return false
  if (config.provider === 'audiocpp_tts') {
    const spec = specs?.find((s) => s.family === config.audiocppFamily)
    if (spec && !spec.modes.includes('streaming')) return false
  }
  return true
}

export function shouldStream(config: TtsConfig, specs?: AudioModelSpec[]): boolean {
  return supportsStreaming(config, specs) && config.streamEnabled !== false
}

export function streamSampleRate(config: TtsConfig, specs?: AudioModelSpec[]): number {
  if (config.provider === 'audiocpp_tts') {
    const explicit = Number(config.sampleRate)
    if (Number.isFinite(explicit) && explicit > 0) return explicit
    const spec = specs?.find((s) => s.family === config.audiocppFamily)
    if (spec?.sampleRate && spec.sampleRate > 0) return spec.sampleRate
  }
  return STREAM_SAMPLE_RATES[config.provider] ?? 24000
}

/**
 * Synthesize into a live PCM stream.
 *
 * `onChunk` receives s16le mono bytes at streamSampleRate(config) as the
 * engine produces them, so playback can begin on the first chunk instead of
 * after the whole clip exists. Resolves when the engine is done; rejects with
 * ApiError('aborted') if `signal` fires, which is how stop works.
 */
export async function synthesizeStream(
  config: TtsConfig,
  text: string,
  onChunk: (pcm: Uint8Array) => void,
  signal?: AbortSignal,
  specs?: AudioModelSpec[],
): Promise<void> {
  if (!supportsStreaming(config, specs)) {
    throw new Error(`${config.provider} has no streaming API`)
  }
  // Same rule as the blocking path: reclaim the other engines' VRAM at the
  // moment speech is requested, not when the engine is picked in the panel.
  if (LOCAL_ENGINES.has(config.provider)) {
    await releaseOtherTts(config.provider).catch(() => undefined)
  }
  const payload = { ...toRequest(config), text }

  if (useSocket()) {
    return binaryStreamRequest('/tts/stream', payload, onChunk, signal)
  }

  // Browser/dev path: plain fetch, read the body as it arrives.
  const res = await fetch(`${getBaseURL()}/tts/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  })
  if (!res.ok || !res.body) {
    const detail = await res.text().catch(() => '')
    throw new Error(`TTS stream failed: ${detail.slice(0, 160)}`)
  }
  const reader = res.body.getReader()
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    if (value?.length) onChunk(value)
  }
}



/** Free the VRAM of one local engine (empty string = every local engine). */
export const unloadTts = (engine = ''): Promise<{ released: string[]; local?: boolean }> =>
  post('/tts/unload', { engine })
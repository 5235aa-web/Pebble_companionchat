import { del, get, post } from './client'

export interface ModelParams {
  temperature: number
  maxTokens: number
  maxContext: number
  topP: number
  topK: number | null
  minP: number | null
  /** llama.cpp extension; cloud providers reject the field, so it is filtered server-side. */
  repeatPenalty: number | null
  /** OpenAI-compatible penalty for cloud providers. null means "leave it out". */
  frequencyPenalty: number | null
  /** OpenAI-compatible penalty for cloud providers. null means "leave it out". */
  presencePenalty: number | null
  enableMtp: boolean
}

export interface LlmProvider extends ModelParams {
  id: string
  /**
   * Display name typed by the user. Blank means the tab falls back to the
   * protocol label, which is also what every pre-existing entry does.
   */
  name?: string
  provider: string
  apiKey: string
  baseUrl: string
  models: string[]
  localModelKey: string
  modelParams: Record<string, Partial<ModelParams>>
}

export interface LlmConfigPayload {
  providers: LlmProvider[]
  activeProviderIndex: number
}

export interface ProviderTypeOption {
  value: string
  label: string
  defaultBaseUrl: string
}

/** Mirrors llm_client.SUPPORTED_PROVIDERS on the backend. */
export const PROVIDER_TYPES: ProviderTypeOption[] = [
  { value: 'openai', label: 'OpenAI', defaultBaseUrl: 'https://api.openai.com/v1/' },
  { value: 'chatgpt', label: 'ChatGPT', defaultBaseUrl: 'https://api.openai.com/v1/' },
  { value: 'deepseek', label: 'DeepSeek', defaultBaseUrl: 'https://api.deepseek.com/v1/' },
  { value: 'qwen', label: 'Qwen', defaultBaseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1/' },
  { value: 'grok', label: 'Grok', defaultBaseUrl: 'https://api.x.ai/v1/' },
  { value: 'xiaomimimo', label: 'Xiaomi MiMo', defaultBaseUrl: 'https://api.xiaomimimo.com/v1/' },
  { value: 'volcengine', label: 'Volcengine', defaultBaseUrl: 'https://ark.cn-beijing.volces.com/api/v3/' },
  { value: 'anthropic', label: 'Anthropic', defaultBaseUrl: '' },
  { value: 'gemini', label: 'Gemini', defaultBaseUrl: '' },
  { value: 'ollama', label: 'Ollama (Local)', defaultBaseUrl: 'http://127.0.0.1:11434/v1/' },
  { value: 'lmstudio', label: 'LM Studio (Local)', defaultBaseUrl: 'http://127.0.0.1:1234/v1/' },
  { value: 'local-openai', label: 'Local OpenAI API', defaultBaseUrl: 'http://127.0.0.1:8080/v1/' },
  { value: 'llama-cpp', label: 'Local (llama.cpp)', defaultBaseUrl: 'http://localhost:8080/v1/' },
]

export const DEFAULT_PARAMS: ModelParams = {
  temperature: 0.6,
  maxTokens: 4096,
  maxContext: 0,
  topP: 0.9,
  topK: 64,
  minP: 0.05,
  repeatPenalty: 1.1,
  frequencyPenalty: null,
  presencePenalty: null,
  enableMtp: false,
}

export const getLlmConfig = (): Promise<LlmConfigPayload> => get('/llm/config')

export const saveLlmConfig = (payload: LlmConfigPayload): Promise<unknown> =>
  post('/llm/config', payload)

export interface LocalModelInfo {
  key: string
  label?: string
  detail?: string
  path?: string
  mmproj?: string
  mtp?: string
  file?: string
  size_gb?: number
  available?: boolean
  has_mmproj?: boolean
  has_mtp?: boolean
  /** Context window the weights were launched with, in tokens. */
  ctx?: number
  ngl?: number
  device?: string
  kv_type?: string
  flash_attn?: boolean
  jinja?: boolean
  builtin?: boolean
  [k: string]: unknown
}

export interface LocalModelSavePayload {
  key: string
  label?: string
  detail?: string
  model: string
  mmproj?: string
  mtp?: string
  ctx?: number
  ngl?: number | null
  device?: string
  kv_type?: string
  flash_attn?: boolean
  jinja?: boolean
}

export interface ScannedGgufModel {
  key: string
  label: string
  path: string
  relPath: string
  mmproj: string
  mtp: string
  sizeGb: number
}

export interface LocalStatus {
  running: boolean
  model?: string
  model_path?: string
  mtp?: boolean
  port?: number
  [k: string]: unknown
}

export const localStatus = (): Promise<LocalStatus> => get('/local-llm/status')

export const localModels = (): Promise<{
  models: LocalModelInfo[]
  default: string
  current: string
  mtp: boolean
}> => get('/local-llm/models')

export const localDevices = (): Promise<{
  devices: unknown[]
  resolved: unknown
  server_exe: string
  model: string
  model_path: string
}> => get('/local-llm/devices')

export const localStart = (modelKey = '', enableMtp?: boolean): Promise<unknown> =>
  post('/local-llm/start', { model_key: modelKey, enable_mtp: enableMtp })

export const localStop = (): Promise<unknown> => post('/local-llm/stop', {})

export const localRestart = (): Promise<unknown> => post('/local-llm/restart', {})

export const localRelease = (): Promise<unknown> => post('/local-llm/release', {})

export const saveLocalModel = (
  payload: LocalModelSavePayload,
): Promise<{ status: string; entry: LocalModelInfo; models: LocalModelInfo[] }> =>
  post('/local-llm/models', payload)

export const deleteLocalModel = (
  modelKey: string,
): Promise<{ status: string; models: LocalModelInfo[] }> =>
  del(`/local-llm/models/${encodeURIComponent(modelKey)}`)

export const scanLocalGgufModels = (
  root = '',
): Promise<{ models: ScannedGgufModel[]; root: string }> =>
  post('/local-llm/scan', { root })

export const probeProviderModels = (
  baseUrl: string,
  apiKey = '',
): Promise<{ models: string[] }> =>
  post('/llm/probe-models', { baseUrl, apiKey })

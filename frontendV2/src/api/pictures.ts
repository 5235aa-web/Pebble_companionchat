import { del, get, post, put } from './client'

const C = '/pictures'

export type PictureStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped'

export type PictureParamValue = string | number | boolean

export interface PictureProviderConfig {
  enabled: boolean
  apiKey: string
  baseUrl: string
  model: string
  accountId: string
  /** Vendor-specific knobs, keyed by the catalogue's param key. */
  params?: Record<string, PictureParamValue>
}

export interface PictureParamMeta {
  key: string
  type: 'number' | 'text' | 'bool' | 'select'
  label: string
  labelZh?: string
  min?: number
  max?: number
  step?: number
  options?: string[]
  default?: PictureParamValue
  /** What the knob does, straight from the vendor docs. Shown as a tooltip. */
  tip?: string
  tipZh?: string
}

export interface PictureProviderMeta {
  label: string
  homepage: string
  fields: string[]
  free: boolean
  note: string
  /** False for local runners: they are usable without any key. */
  requiresKey?: boolean
  params: PictureParamMeta[]
  defaults: PictureProviderConfig
}

export interface PromptStyleOption {
  id: string
  label: string
  labelZh?: string
}

export interface PictureConfig {
  enabled: boolean
  autoChat: boolean
  autoScript: boolean
  cooldownSeconds: number
  dailyLimit: number
  size: string
  stylePrompt: string
  negativePrompt: string
  promptProviderId: string
  promptStyle?: string
  promptStyles?: PromptStyleOption[]
  activeProvider: string
  providers: Record<string, PictureProviderConfig>
  catalogue: Record<string, PictureProviderMeta>
}

export interface GeneratedPicture {
  id: number
  companion_id: string
  branch_id: string
  source: string
  status: PictureStatus
  instruction: string
  prompt: string
  negative_prompt: string
  provider: string
  model: string
  seed: number | null
  url: string
  width: number
  height: number
  size: number
  /** 0-99 while a local runner reports sampling steps; 100 once it is done. */
  progress: number
  error: string
  message_id: number | null
  session_id: number | null
  created_at: string
}

export interface PictureUsage {
  today: number
  dailyLimit: number
  cooldownSeconds: number
  branchTotal?: number
  branchLastAgeSeconds?: number | null
}

export const getPictureConfig = (): Promise<PictureConfig> => get(`${C}/config`)

export const savePictureConfig = (patch: Partial<PictureConfig>): Promise<PictureConfig> =>
  put(`${C}/config`, patch)

export const listPictures = (
  companionId: string,
  branchId: string,
  limit = 200,
): Promise<{ pictures: GeneratedPicture[]; count: number }> =>
  get(
    `${C}?companionId=${encodeURIComponent(companionId)}` +
      `&branchId=${encodeURIComponent(branchId)}&limit=${limit}`,
  )

export const getPictureUsage = (
  companionId: string,
  branchId: string,
): Promise<PictureUsage> =>
  get(
    `${C}/usage?companionId=${encodeURIComponent(companionId)}` +
      `&branchId=${encodeURIComponent(branchId)}`,
  )

export const generatePicture = (body: {
  companionId: string
  branchId: string
  instruction?: string
  messageId?: number | null
  sessionId?: number | null
  chatProviderId?: string
  chatModel?: string
}): Promise<{ status: string; id: number }> => post(`${C}/generate`, body)

export const retryPicture = (id: number): Promise<{ status: string; id: number }> =>
  post(`${C}/${id}/retry`)

export const cancelPicture = (id: number): Promise<{ status: string; id: number }> =>
  post(`${C}/${id}/cancel`)

export const deletePicture = (
  id: number,
): Promise<{ status: string; id: number; file_deleted: boolean }> => del(`${C}/${id}`)

/* ---- ComfyUI -------------------------------------------------------------
 *
 * The panel never assumes the instance is running: `connected` is part of the
 * status payload, and every field beyond it is optional. Workflows live inside
 * this project's config, so importing one does not depend on where ComfyUI was
 * installed.
 */

export interface ComfySamplerDefaults {
  steps?: number
  cfg?: number
  sampler?: string
  scheduler?: string
  denoise?: number
}

export interface ComfyWorkflow {
  id: string
  label: string
  note: string
  builtin: boolean
  readonly?: boolean
  source?: 'builtin' | 'imported' | 'comfyui'
  loaderType?: 'checkpoint' | 'unet' | 'both' | 'none' | ''
  samplerDefaults?: ComfySamplerDefaults
}

export interface ComfyModels {
  checkpoints: string[]
  unets?: string[]
  samplers: string[]
  schedulers: string[]
}

export interface ComfyStatus {
  baseUrl: string
  connected: boolean
  error: string
  system?: {
    comfyuiVersion?: string | null
    pythonVersion?: string | null
    os?: string | null
  }
  device?: string
  vramTotal?: number
  models?: ComfyModels
  modelsError?: string
  workflows: ComfyWorkflow[]
}

export const getComfyStatus = (baseUrl = ''): Promise<ComfyStatus> =>
  get(
    `${C}/comfyui/status${baseUrl ? `?baseUrl=${encodeURIComponent(baseUrl)}` : ''}`,
  )

export const listComfyWorkflows = (): Promise<{ items: ComfyWorkflow[] }> =>
  get(`${C}/comfyui/workflows`)

export const importComfyWorkflow = (
  name: string,
  workflow: unknown,
): Promise<{ status: string; item: ComfyWorkflow; items: ComfyWorkflow[] }> =>
  post(`${C}/comfyui/workflows`, { name, workflow })

export const deleteComfyWorkflow = (
  name: string,
): Promise<{ status: string; items: ComfyWorkflow[] }> =>
  del(`${C}/comfyui/workflows/${encodeURIComponent(name)}`)

export const getComfyWorkflow = (
  name: string,
): Promise<{ id: string; workflow: Record<string, unknown> }> =>
  get(`${C}/comfyui/workflows/${encodeURIComponent(name)}`)

import { get, post } from './client'

export interface AppConfig {
  modelsRoot: string
  resolvedModelsRoot: string
  modelsRootExists: boolean
  configuredLlamaRoot?: string
  /** Where the llama.cpp runtime was found. Not bundled: ~99 MB of backends. */
  llamaRoot: string
  llamaFound: boolean
  audioCppDir?: string
  audioCppFound?: boolean
  dataRoot: string
}

export interface BrowseEntry {
  name: string
  path: string
  isDir: boolean
  sizeMb?: number
}

export interface BrowseResult {
  path: string
  parent: string
  entries: BrowseEntry[]
}

export const getAppConfig = (): Promise<AppConfig> => get<AppConfig>('/app/config')

/** Persist model/runtime roots. Engines read them on next start or scan. */
export const setAppConfig = (payload: {
  modelsRoot?: string
  llamaRoot?: string
}): Promise<{ status: string; modelsRoot: string; llamaRoot: string }> =>
  post('/app/config', payload)

export const setModelsRoot = (modelsRoot: string): Promise<{ status: string; modelsRoot: string }> =>
  post('/app/config', { modelsRoot })

export const browsePath = (path = '', ext = '', dirsOnly = false): Promise<BrowseResult> => {
  const qs = new URLSearchParams()
  if (path) qs.set('path', path)
  if (ext) qs.set('ext', ext)
  if (dirsOnly) qs.set('dirs_only', 'true')
  const q = qs.toString()
  return get<BrowseResult>(`/app/browse${q ? `?${q}` : ''}`)
}

export interface UpdateCheckResult {
  ok: boolean
  hasRelease?: boolean
  currentVersion: string
  latestVersion?: string
  tagName?: string
  hasUpdate?: boolean
  releaseName?: string
  notes?: string
  publishedAt?: string
  releaseUrl?: string
  downloadUrl?: string
  assetName?: string
  assetSize?: number
  error?: string
  detail?: string
}

export interface UpdateInstallStatus {
  phase: 'idle' | 'downloading' | 'ready' | 'launching' | 'error'
  downloadedBytes: number
  totalBytes: number
  percent: number
  installerPath: string
  assetName: string
  error: string
}

export const checkAppUpdate = (): Promise<UpdateCheckResult> =>
  get<UpdateCheckResult>('/app/update/check')

export const startAppUpdateInstall = (
  downloadUrl: string,
  assetName = '',
): Promise<UpdateInstallStatus> =>
  post<UpdateInstallStatus>('/app/update/install', { downloadUrl, assetName })

export const getAppUpdateStatus = (): Promise<UpdateInstallStatus> =>
  get<UpdateInstallStatus>('/app/update/status')

export const launchAppUpdate = (): Promise<{ ok: boolean; installerPath: string }> =>
  post<{ ok: boolean; installerPath: string }>('/app/update/launch')

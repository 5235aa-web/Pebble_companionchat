import { get, post } from './client'

export interface AppConfig {
  modelsRoot: string
  resolvedModelsRoot: string
  modelsRootExists: boolean
  configuredLlamaRoot?: string
  /** Where the llama.cpp runtime was found. Not bundled: ~99 MB of backends. */
  llamaRoot: string
  llamaFound: boolean
  configuredInochiPython?: string
  inochiPython?: string
  configuredFfmpegPath?: string
  ffmpegPath?: string
  ffmpegFound?: boolean
  videoAutoStt?: boolean
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
  inochiPythonPath?: string
  ffmpegPath?: string
  videoAutoStt?: boolean
}): Promise<{
  status: string
  modelsRoot: string
  llamaRoot: string
  inochiPythonPath?: string
  ffmpegPath?: string
  videoAutoStt?: boolean
}> =>
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

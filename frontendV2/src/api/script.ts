import { del, get, post, put } from './client'

export interface ScriptNode {
  id?: number
  title: string
  instruction: string
  /** Planned exchanges for this scene; 0 means no fixed length. */
  turns?: number
}

export interface ScriptMeta {
  id: number
  title: string
  premise: string
  node_count?: number
  created_at?: string
  updated_at?: string
}

export interface ScriptSession {
  id: number
  script_id: number
  companion_id: string
  branch_id: string
  current_index: number
  scene_done: boolean
  turns_done: number
  hud: Record<string, unknown>
  status: string
}

export interface ScriptPayload {
  session: ScriptSession | null
  script: ScriptMeta | null
  nodes: ScriptNode[]
  current_index: number
  current_node: ScriptNode | null
  node_count: number
}

export const listScripts = () => get<{ scripts: ScriptMeta[] }>('/scripts')

export const createScript = (title: string, premise = '') =>
  post<{ script_id: number }>('/scripts', { title, premise })

export const getScript = (id: number) =>
  get<{ script: ScriptMeta; nodes: ScriptNode[] }>(`/scripts/${id}`)

export const updateScript = (id: number, title: string, premise: string) =>
  put<{ status: string }>(`/scripts/${id}`, { title, premise })

export const deleteScript = (id: number) => del<{ status: string }>(`/scripts/${id}`)

export const saveNodes = (id: number, nodes: ScriptNode[]) =>
  put<{ nodes: ScriptNode[] }>(`/scripts/${id}/nodes`, { nodes })

export const startScript = (id: number, companionId: string, language = 'en') =>
  post<ScriptPayload>(`/scripts/${id}/start`, { companion_id: companionId, language })

export const getActiveSession = (companionId: string) =>
  get<ScriptPayload>(`/companions/${companionId}/script-session`)

export const advanceSession = (sessionId: number) =>
  post<ScriptPayload>(`/script-sessions/${sessionId}/advance`)

export const finishSession = (sessionId: number) =>
  post<{ status: string }>(`/script-sessions/${sessionId}/finish`)

export const updateHud = (sessionId: number, hud: Record<string, unknown>) =>
  put<{ hud: Record<string, unknown> }>(`/script-sessions/${sessionId}/hud`, { hud })

export const rewindSession = (sessionId: number, index: number, language = 'en') =>
  post<ScriptPayload>(`/script-sessions/${sessionId}/rewind`, { index, language })

export interface SceneSummary {
  id: number
  scene_from: number
  scene_to: number
  scene_titles: string
  content: string
  message_from_id: number
  message_to_id: number
  created_at?: string
}

export interface SceneCompactResult {
  ok: boolean
  error?: string
  detail?: string
  summary?: string
  scene_from?: number
  scene_to?: number
  compressed?: number
  kept?: number
  /** True when the opening of the segment was dropped to fit the limit. */
  trimmed?: boolean
  tokens?: number
  limit?: number
}

export const compactScene = (
  sessionId: number,
  llmConfig: unknown,
  allowTrim = false,
) =>
  post<SceneCompactResult>(`/script-sessions/${sessionId}/compact`, {
    llm_config: llmConfig,
    allow_trim: allowTrim,
  })

export const listSceneSummaries = (sessionId: number) =>
  get<{ summaries: SceneSummary[] }>(`/script-sessions/${sessionId}/summaries`)

/** Scenes this playthrough actually plays: its own copy, not the shared script. */
export const getSessionNodes = (sessionId: number) =>
  get<{ nodes: ScriptNode[] }>(`/script-sessions/${sessionId}/nodes`)

export const saveSessionNodes = (sessionId: number, nodes: ScriptNode[]) =>
  put<{ nodes: ScriptNode[] }>(`/script-sessions/${sessionId}/nodes`, { nodes })

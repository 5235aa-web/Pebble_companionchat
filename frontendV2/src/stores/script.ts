import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  advanceSession,
  compactScene,
  createScript,
  deleteScript,
  finishSession,
  getActiveSession,
  getScript,
  listSceneSummaries,
  listScripts,
  rewindSession,
  saveNodes,
  startScript,
  updateHud,
  updateScript,
  type SceneCompactResult,
  type SceneSummary,
  type ScriptMeta,
  type ScriptNode,
  type ScriptPayload,
} from '@/api/script'

export const useScriptStore = defineStore('script', () => {
  const scripts = ref<ScriptMeta[]>([])
  const current = ref<ScriptMeta | null>(null)
  const nodes = ref<ScriptNode[]>([])
  const play = ref<ScriptPayload | null>(null)
  /** Segment cards for the running playthrough, oldest first. */
  const summaries = ref<SceneSummary[]>([])
  const loading = ref(false)

  const session = computed(() => play.value?.session ?? null)
  const playing = computed(() => Boolean(session.value && session.value.status === 'playing'))
  const currentIndex = computed(() => play.value?.current_index ?? 0)
  const nodeCount = computed(() => play.value?.node_count ?? 0)
  const currentNode = computed(() => play.value?.current_node ?? null)
  const hud = computed<Record<string, unknown>>(() => session.value?.hud ?? {})
  const sceneDone = computed(() => Boolean(session.value?.scene_done))

  async function loadScripts() {
    loading.value = true
    try {
      const r = await listScripts()
      scripts.value = r.scripts ?? []
    } finally {
      loading.value = false
    }
  }

  async function open(id: number) {
    const r = await getScript(id)
    current.value = r.script
    // Every field the editor writes back has to survive this rebuild: a dropped
    // turns value would show as 0 and then be saved over the real one.
    nodes.value = (r.nodes ?? []).map((n) => ({
      id: n.id,
      title: n.title,
      instruction: n.instruction,
      turns: Number(n.turns ?? 0),
    }))
    return r
  }

  async function create(title: string, premise = '') {
    const r = await createScript(title, premise)
    await loadScripts()
    await open(r.script_id)
    return r.script_id
  }

  async function saveMeta(title: string, premise: string) {
    if (!current.value) return false
    await updateScript(current.value.id, title, premise)
    current.value.title = title
    current.value.premise = premise
    await loadScripts()
    return true
  }

  async function persistNodes() {
    if (!current.value) return false
    const r = await saveNodes(current.value.id, nodes.value)
    nodes.value = (r.nodes ?? []).map((n) => ({
      id: n.id,
      title: n.title,
      instruction: n.instruction,
      turns: Number(n.turns ?? 0),
    }))
    syncPlayNodes()
    await loadScripts()
    return true
  }

  /**
   * Mirror the edited scene list into a running playthrough. The rail and the
   * prompt both read from `play`, so a scene added mid-play has to land there
   * immediately instead of waiting for the next reply.
   */
  function syncPlayNodes() {
    if (!play.value || !current.value) return
    if (play.value.script?.id !== current.value.id) return
    const list = nodes.value.map((n) => ({ ...n }))
    if (!list.length) return
    const idx = Math.max(0, Math.min(play.value.current_index, list.length - 1))
    play.value = {
      ...play.value,
      nodes: list,
      node_count: list.length,
      current_index: idx,
      current_node: list[idx] ?? null,
    }
  }

  async function remove(id: number) {
    await deleteScript(id)
    if (current.value?.id === id) {
      current.value = null
      nodes.value = []
      // The playthrough is gone with the script: leaving it in memory would
      // keep a progress rail pointing at a deleted session.
      if (play.value?.script?.id === id) play.value = null
    }
    await loadScripts()
  }

  async function start(companionId: string, language = 'en') {
    if (!current.value) return null
    const r = await startScript(current.value.id, companionId, language)
    play.value = r
    summaries.value = []
    return r
  }

  async function loadActive(companionId: string) {
    const r = await getActiveSession(companionId)
    play.value = r?.session ? r : null
    if (play.value?.session) await loadSummaries(play.value.session.id)
    else summaries.value = []
    return play.value
  }

  /** Segment cards are per branch: they never travel with the script. */
  async function loadSummaries(sessionId: number) {
    try {
      const r = await listSceneSummaries(sessionId)
      summaries.value = r.summaries ?? []
    } catch {
      summaries.value = []
    }
  }

  async function compactPlan(
    llmConfig: unknown,
    allowTrim = false,
  ): Promise<SceneCompactResult> {
    const active = session.value
    if (!active) return { ok: false, error: 'no_session' }
    const r = await compactScene(active.id, llmConfig, allowTrim)
    if (r.ok) await loadSummaries(active.id)
    return r
  }

  async function advance() {
    if (!session.value) return null
    const r = await advanceSession(session.value.id)
    play.value = r
    return r
  }

  async function finish() {
    if (!session.value) return
    await finishSession(session.value.id)
    if (play.value?.session) play.value.session.status = 'finished'
  }

  async function rewind(index: number, language = 'en') {
    if (!session.value) return null
    const r = await rewindSession(session.value.id, index, language)
    play.value = r
    summaries.value = []
    return r
  }

  async function setHud(next: Record<string, unknown>) {
    if (!session.value) return
    const r = await updateHud(session.value.id, next)
    if (play.value?.session) play.value.session.hud = r.hud
  }

  /** Merge the state block the backend attaches to a scripted reply. */
  function applyState(
    hudUpdate: Record<string, unknown>,
    sceneDone: boolean,
    turnsDone = 0,
  ) {
    if (!play.value?.session) return
    if (hudUpdate && Object.keys(hudUpdate).length) {
      play.value.session.hud = { ...(play.value.session.hud ?? {}), ...hudUpdate }
    }
    play.value.session.scene_done = sceneDone
    // One reply is one exchange; the backend counts it even when the model
    // omitted its state block.
    play.value.session.turns_done = turnsDone
  }

  function clear() {
    play.value = null
    summaries.value = []
  }

  return {
    scripts,
    current,
    nodes,
    play,
    summaries,
    loading,
    session,
    playing,
    currentIndex,
    nodeCount,
    currentNode,
    hud,
    sceneDone,
    loadScripts,
    open,
    create,
    saveMeta,
    persistNodes,
    remove,
    start,
    loadActive,
    loadSummaries,
    compactPlan,
    advance,
    finish,
    rewind,
    setHud,
    applyState,
    clear,
  }
})

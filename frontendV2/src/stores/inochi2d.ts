import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import {
  buildPuppet,
  buildPuppetPack,
  buildPuppetUpload,
  cancelPuppetBuild,
  deletePuppetOutfit,
  getInochiStatus,
  getPuppet,
  savePuppetConfig,
  type InochiEnvStatus,
  type PuppetConfig,
  type PuppetJobState,
  type PuppetManifest,
} from '@/api/inochi2d'
import { useSessionStore } from './session'

const MODE_MAP_KEY = 'pebble.sceneModeMap'

export type SceneStageMode = 'scene' | 'live2d'

const DEFAULT_CONFIG: PuppetConfig = {
  motionEnabled: true,
  breathAmp: 1.0,
  breathSpeed: 1.0,
  swayAmp: 0.9,
  headParallax: 1.0,
  eyeTrack: true,
  blinkRate: 1.0,
  mouthSens: 1.2,
  physicsStrength: 1.0,
  physicsDamping: 0.85,
  emotion: 'neutral',
  outfit: '',
  testMouth: 0.0,
  hiddenLayers: [],
}

function readModeMap(): Record<string, SceneStageMode> {
  try {
    const raw = localStorage.getItem(MODE_MAP_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function writeModeMap(map: Record<string, SceneStageMode>): void {
  try {
    localStorage.setItem(MODE_MAP_KEY, JSON.stringify(map))
  } catch {
    /* ignore storage failures */
  }
}

export const useInochi2dStore = defineStore('inochi2d', () => {
  const session = useSessionStore()

  const modeMap = ref<Record<string, SceneStageMode>>(readModeMap())
  const editorOpen = ref(false)
  const editorTab = ref<'motion' | 'layers' | 'build'>('motion')

  const env = ref<InochiEnvStatus | null>(null)
  const puppet = ref<PuppetManifest | null>(null)
  const config = ref<PuppetConfig>({ ...DEFAULT_CONFIG })
  const job = ref<PuppetJobState>({ status: 'idle', stage: '', progress: 0, error: '' })
  const loading = ref(false)
  const loadedForId = ref('')

  let pollTimer: number | null = null
  let saveTimer: number | null = null

  const activeMode = computed<SceneStageMode>(() => {
    const id = session.activeCompanionId
    if (!id) return 'scene'
    return modeMap.value[id] === 'live2d' ? 'live2d' : 'scene'
  })

  function setMode(mode: SceneStageMode): void {
    const id = session.activeCompanionId
    if (!id) return
    modeMap.value = { ...modeMap.value, [id]: mode }
    writeModeMap(modeMap.value)
    if (mode === 'live2d') {
      void ensureLoaded()
    } else {
      editorOpen.value = false
    }
  }

  function toggleMode(): void {
    setMode(activeMode.value === 'live2d' ? 'scene' : 'live2d')
  }

  function toggleEditor(): void {
    editorOpen.value = !editorOpen.value
  }

  function openEditor(tab?: 'motion' | 'layers' | 'build'): void {
    if (tab) editorTab.value = tab
    editorOpen.value = true
  }

  function closeEditor(): void {
    editorOpen.value = false
  }

  function stopPoll(): void {
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer)
      pollTimer = null
    }
  }

  function schedulePoll(companionId: string): void {
    stopPoll()
    pollTimer = window.setTimeout(() => {
      void refresh(companionId)
    }, 1800)
  }

  async function refresh(companionId?: string): Promise<void> {
    const id = companionId || session.activeCompanionId
    if (!id) return
    try {
      const payload = await getPuppet(id)
      if (session.activeCompanionId !== id) return
      env.value = payload.env
      job.value = payload.job
      puppet.value = payload.puppet
      config.value = { ...DEFAULT_CONFIG, ...(payload.config || {}) }
      loadedForId.value = id

      if (payload.job.status === 'running') {
        schedulePoll(id)
      } else {
        stopPoll()
      }
    } catch {
      stopPoll()
    }
  }

  async function ensureLoaded(): Promise<void> {
    const id = session.activeCompanionId
    if (!id) return
    if (loadedForId.value === id && puppet.value) return
    loading.value = true
    try {
      await refresh(id)
    } finally {
      loading.value = false
    }
  }

  async function startBuild(sourceRelPath?: string, resolution = 768, steps = 20): Promise<void> {
    const id = session.activeCompanionId
    if (!id) return
    const res = await buildPuppet({ companionId: id, sourceRelPath, resolution, steps })
    job.value = res.job
    if (res.job.status === 'running') {
      schedulePoll(id)
    }
  }

  async function startBuildFromFile(file: File, resolution = 768, steps = 20): Promise<void> {
    const id = session.activeCompanionId
    if (!id) return
    const res = await buildPuppetUpload(id, file, resolution, steps)
    job.value = res.job
    if (res.job.status === 'running') {
      schedulePoll(id)
    }
  }

  async function startBuildPack(opts: {
    mode: 'base' | 'outfit'
    outfitId?: string
    outfitLabel?: string
    baseIndex?: number
    labels?: string[]
    assetIds?: number[]
    files?: File[]
    saveToAssets?: boolean
    resolution?: number
    steps?: number
  }): Promise<void> {
    const id = session.activeCompanionId
    if (!id) return
    const res = await buildPuppetPack({ companionId: id, ...opts })
    job.value = res.job
    if (opts.saveToAssets !== false && (opts.files?.length ?? 0) > 0) {
      void session.loadCompanionAssets(id)
      void session.loadCompanions()
    }
    if (res.job.status === 'running') {
      schedulePoll(id)
    }
  }

  async function removeOutfit(outfitId: string): Promise<void> {
    const id = session.activeCompanionId
    if (!id) return
    const res = await deletePuppetOutfit(id, outfitId)
    puppet.value = res.puppet
    config.value = { ...DEFAULT_CONFIG, ...(res.config || {}) }
  }

  async function refreshEnv(): Promise<void> {
    try {
      env.value = await getInochiStatus(true)
    } catch {
      /* ignore */
    }
  }

  async function cancelBuild(): Promise<void> {
    const id = session.activeCompanionId
    if (!id) return
    stopPoll()
    const res = await cancelPuppetBuild(id)
    job.value = res.job
  }

  function updateConfig(patch: Partial<PuppetConfig>): void {
    config.value = { ...config.value, ...patch }
    scheduleSave()
  }

  function toggleLayerVisibility(layerId: string): void {
    const set = new Set(config.value.hiddenLayers || [])
    if (set.has(layerId)) set.delete(layerId)
    else set.add(layerId)
    updateConfig({ hiddenLayers: Array.from(set) })
  }

  function resetConfig(): void {
    config.value = { ...DEFAULT_CONFIG }
    scheduleSave()
  }

  function scheduleSave(): void {
    const id = session.activeCompanionId
    if (!id) return
    if (saveTimer !== null) window.clearTimeout(saveTimer)
    saveTimer = window.setTimeout(() => {
      saveTimer = null
      void savePuppetConfig(id, config.value).catch(() => undefined)
    }, 350)
  }

  watch(
    () => session.activeCompanionId,
    (id) => {
      stopPoll()
      puppet.value = null
      job.value = { status: 'idle', stage: '', progress: 0, error: '' }
      loadedForId.value = ''
      if (id && activeMode.value === 'live2d') {
        void ensureLoaded()
      } else {
        editorOpen.value = false
      }
    },
  )

  return {
    activeMode,
    editorOpen,
    editorTab,
    env,
    puppet,
    config,
    job,
    loading,
    setMode,
    toggleMode,
    toggleEditor,
    openEditor,
    closeEditor,
    ensureLoaded,
    refresh,
    refreshEnv,
    startBuild,
    startBuildFromFile,
    startBuildPack,
    removeOutfit,
    cancelBuild,
    updateConfig,
    toggleLayerVisibility,
    resetConfig,
  }
})

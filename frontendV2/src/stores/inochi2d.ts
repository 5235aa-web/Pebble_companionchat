import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import type {
  InochiEnvStatus,
  PuppetActionId,
  PuppetAnchors,
  PuppetConfig,
  PuppetJobState,
  PuppetManifest,
  PuppetPosesIndex,
  PuppetSubject,
  RigLayerPatch,
} from '@/api/inochi2d'
import {
  buildPuppet,
  buildPuppetPack,
  buildPuppetUpload,
  cancelPuppetBuild,
  deletePuppetOutfit,
  deletePuppetPose,
  getInochiStatus,
  getPuppet,
  importInxPuppet,
  importPsdPuppet,
  matchCompanionLive2dCue,
  savePuppetConfig,
  savePuppetRig,
  selectPuppetPose,
  updatePuppetPoseMeta,
} from '@/api/inochi2d'
import { useSessionStore } from './session'

const MODE_MAP_KEY = 'pebble.sceneModeMap'

export type SceneStageMode = 'scene' | 'live2d'

export interface PuppetActionCommand {
  id: PuppetActionId
  seq: number
  intensity: number
}

const DEFAULT_CONFIG: PuppetConfig = {
  motionEnabled: true,
  breathAmp: 1.0,
  breathSpeed: 1.0,
  swayAmp: 0.9,
  headParallax: 1.0,
  depthRelief: 1.0,
  limbMotion: 1.0,
  eyeTrack: true,
  blinkRate: 1.0,
  mouthSens: 1.2,
  physicsStrength: 1.0,
  physicsDamping: 0.85,
  autoChatSync: true,
  emotion: 'neutral',
  outfit: '',
  testMouth: 0.0,
  hiddenLayers: [],
}

function inferPuppetCue(text: string, mood?: string, exprHint?: string): {
  emotion?: string
  action?: PuppetActionId
} {
  const combined = `${exprHint || ''} ${mood || ''} ${text || ''}`.toLowerCase()
  if (!combined.trim()) return {}

  let action: PuppetActionId | undefined
  if (/拥抱|抱住|抱紧|抱抱|依偎|靠在.*怀里|扑进.*怀|hug|embrace|cuddle/.test(combined)) {
    action = 'hug_lean'
  } else if (/凑近|贴近|靠近|俯身|盯著|盯着|lean\s*in|closer/.test(combined)) {
    action = 'lean_in'
  } else if (/鞠躬|行礼|致意|bow/.test(combined)) {
    action = 'bow'
  } else if (/点头|嗯嗯|答应|同意|nod/.test(combined)) {
    action = 'nod'
  } else if (/摇头|才不是|不要|拒绝|shake\s*head/.test(combined)) {
    action = 'shake'
  } else if (/捂脸|害羞|脸红|羞涩|不好意思|shy|blush|embarrass/.test(combined)) {
    action = 'shy_hide'
  } else if (/吓一跳|震惊|惊呼|猛地|一颤|startle|gasp|shock/.test(combined)) {
    action = 'startle'
  } else if (/叹气|唉|无奈|沮丧|失落|sigh/.test(combined)) {
    action = 'sigh'
  } else if (/欢呼|开心|雀跃|太好了|耶|哈哈|laugh|cheer|excited/.test(combined)) {
    action = 'cheer'
  }

  let emotion: string | undefined
  const moodStr = `${exprHint || ''} ${mood || ''}`.toLowerCase()
  const emoSrc = moodStr.trim() ? moodStr : combined
  if (/害羞|羞涩|脸红|亲密|撒娇|依偎|抱|shy|blush|affection|love|fond/.test(emoSrc)) {
    emotion = 'shy'
  } else if (/惊讶|震惊|吃惊|吓|surpris|shock|amaze|startl/.test(emoSrc)) {
    emotion = 'surprised'
  } else if (/生气|愤怒|恼|哼|气呼呼|angry|mad|furious|annoy/.test(emoSrc)) {
    emotion = 'angry'
  } else if (/难过|悲伤|委屈|哭|伤心|失落|叹气|sad|cry|upset|sorrow|worry|troubled/.test(emoSrc)) {
    emotion = 'sad'
  } else if (/开心|高兴|微笑|笑|得意|愉悦|happy|joy|smile|glad|smug|cheer/.test(emoSrc)) {
    emotion = 'happy'
  } else if (/平静|正常|默认|neutral|calm|normal/.test(emoSrc)) {
    emotion = 'neutral'
  }

  return { emotion, action }
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
  const posesIndex = ref<PuppetPosesIndex>({ activePoseId: 'default', poses: [] })
  const config = ref<PuppetConfig>({ ...DEFAULT_CONFIG })
  const liveEmotion = ref<string | null>(null)
  const actionCommand = ref<PuppetActionCommand | null>(null)
  const rigEditMode = ref(false)
  const rigSubjectIndex = ref(0)
  const liveRigPatch = ref<{
    anchors?: Partial<PuppetAnchors>
    subjects?: PuppetSubject[]
    layers?: RigLayerPatch[]
    seq: number
  } | null>(null)
  const job = ref<PuppetJobState>({ status: 'idle', stage: '', progress: 0, error: '' })
  const loading = ref(false)
  const loadedForId = ref('')

  let rigPatchSeq = 0
  let actionSeq = 0
  let actionClearTimer: number | null = null
  let pollTimer: number | null = null
  let saveTimer: number | null = null

  const effectiveEmotion = computed(() => liveEmotion.value ?? config.value.emotion)

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
      posesIndex.value = payload.posesIndex ?? { activePoseId: 'default', poses: [] }
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

  async function startBuild(
    sourceRelPath?: string,
    resolution = 768,
    steps = 20,
    poseId = '',
    poseLabel = '',
  ): Promise<void> {
    const id = session.activeCompanionId
    if (!id) return
    const res = await buildPuppet({
      companionId: id,
      sourceRelPath,
      resolution,
      steps,
      poseId,
      poseLabel,
    })
    job.value = res.job
    if (res.job.status === 'running') {
      schedulePoll(id)
    }
  }

  async function startBuildFromFile(
    file: File,
    resolution = 768,
    steps = 20,
    poseId = '',
    poseLabel = '',
  ): Promise<void> {
    const id = session.activeCompanionId
    if (!id) return
    const res = await buildPuppetUpload(id, file, resolution, steps, poseId, poseLabel)
    job.value = res.job
    if (res.job.status === 'running') {
      schedulePoll(id)
    }
  }

  async function startBuildPack(opts: {
    mode: 'base' | 'outfit'
    outfitId?: string
    outfitLabel?: string
    poseId?: string
    poseLabel?: string
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

  async function switchPose(poseId: string): Promise<void> {
    const id = session.activeCompanionId
    if (!id) return
    const res = await selectPuppetPose(id, poseId)
    posesIndex.value = res.posesIndex
    if (res.puppet) puppet.value = res.puppet
  }

  async function savePoseMeta(
    poseId: string,
    patch: { label?: string; triggerMoods?: string[]; triggerKeywords?: string[] },
  ): Promise<void> {
    const id = session.activeCompanionId
    if (!id) return
    const res = await updatePuppetPoseMeta({ companionId: id, poseId, ...patch })
    posesIndex.value = res.posesIndex
  }

  async function removePose(poseId: string): Promise<void> {
    const id = session.activeCompanionId
    if (!id) return
    const res = await deletePuppetPose(id, poseId)
    posesIndex.value = res.posesIndex
    puppet.value = res.puppet
  }

  async function importLayeredPsd(file: File, poseId = '', poseLabel = ''): Promise<void> {
    const id = session.activeCompanionId
    if (!id) return
    const res = await importPsdPuppet(id, file, poseId, poseLabel)
    posesIndex.value = res.posesIndex
    puppet.value = res.puppet
  }

  async function importInochiInx(file: File, poseId = '', poseLabel = ''): Promise<void> {
    const id = session.activeCompanionId
    if (!id) return
    const res = await importInxPuppet(id, file, poseId, poseLabel)
    posesIndex.value = res.posesIndex
    puppet.value = res.puppet
  }

  function previewRigPatch(
    anchors?: Partial<PuppetAnchors>,
    subjects?: PuppetSubject[],
    layers?: RigLayerPatch[],
  ): void {
    if (puppet.value) {
      const next = { ...puppet.value }
      if (anchors) next.anchors = { ...next.anchors, ...anchors }
      if (subjects) next.subjects = subjects
      if (layers?.length) {
        const byId = new Map(layers.map((p) => [p.id, p]))
        next.layers = next.layers
          .map((l) => {
            const patch = byId.get(l.id)
            return patch ? { ...l, ...patch } : l
          })
          .sort((a, b) => a.zIndex - b.zIndex)
      }
      puppet.value = next
    }
    rigPatchSeq += 1
    liveRigPatch.value = { anchors, subjects, layers, seq: rigPatchSeq }
  }

  async function commitRig(layers?: RigLayerPatch[]): Promise<void> {
    const id = session.activeCompanionId
    if (!id || !puppet.value) return
    const res = await savePuppetRig({
      companionId: id,
      poseId: posesIndex.value.activePoseId,
      anchors: puppet.value.anchors,
      subjects: puppet.value.subjects,
      layers,
    })
    puppet.value = res.puppet
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
    if (patch.emotion !== undefined) {
      liveEmotion.value = null
    }
    config.value = { ...config.value, ...patch }
    scheduleSave()
  }

  function triggerAction(id: PuppetActionId, intensity = 1): void {
    actionSeq += 1
    const seq = actionSeq
    actionCommand.value = { id, seq, intensity }
    if (actionClearTimer !== null) window.clearTimeout(actionClearTimer)
    const holdMs = id === 'hug_lean' ? 2900 : id === 'bow' || id === 'sigh' ? 2100 : 1800
    actionClearTimer = window.setTimeout(() => {
      if (actionCommand.value?.seq === seq) {
        actionCommand.value = null
      }
      actionClearTimer = null
    }, holdMs)
  }

  function applyChatCue(text: string, mood?: string, exprHint?: string): void {
    if (!config.value.autoChatSync || !config.value.motionEnabled) return
    const { emotion, action } = inferPuppetCue(text, mood, exprHint)
    if (emotion) {
      liveEmotion.value = emotion
    }
    if (action) {
      triggerAction(action, 1.0)
    }
    const id = session.activeCompanionId
    if (id && activeMode.value === 'live2d' && posesIndex.value.poses.length > 1) {
      void matchCompanionLive2dCue(id, { text, mood, expression: exprHint })
        .then((res) => {
          if (
            session.activeCompanionId === id &&
            res.matchedPoseId &&
            res.matchedPoseId !== posesIndex.value.activePoseId &&
            res.puppet
          ) {
            posesIndex.value = res.posesIndex
            puppet.value = res.puppet
          }
        })
        .catch(() => undefined)
    }
  }

  function toggleLayerVisibility(layerId: string): void {
    const set = new Set(config.value.hiddenLayers || [])
    if (set.has(layerId)) set.delete(layerId)
    else set.add(layerId)
    updateConfig({ hiddenLayers: Array.from(set) })
  }

  function resetConfig(): void {
    liveEmotion.value = null
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
      posesIndex.value = { activePoseId: 'default', poses: [] }
      rigEditMode.value = false
      rigSubjectIndex.value = 0
      liveRigPatch.value = null
      liveEmotion.value = null
      actionCommand.value = null
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
    posesIndex,
    config,
    liveEmotion,
    effectiveEmotion,
    actionCommand,
    rigEditMode,
    rigSubjectIndex,
    liveRigPatch,
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
    switchPose,
    savePoseMeta,
    removePose,
    importLayeredPsd,
    importInochiInx,
    previewRigPatch,
    commitRig,
    removeOutfit,
    cancelBuild,
    updateConfig,
    triggerAction,
    applyChatCue,
    toggleLayerVisibility,
    resetConfig,
  }
})

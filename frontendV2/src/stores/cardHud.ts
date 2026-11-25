import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import {
  getBranchHud,
  resetBranchHud,
  updateBranchHud,
  type CompanionAsset,
} from '@/api/companion'
import { useSessionStore } from './session'
import { useCardAudioStore } from './cardAudio'

const MOOD_SYNONYMS: string[][] = [
  ['happy', 'joy', 'smile', 'cheerful', 'delighted', 'excited', '开心', '高兴', '微笑', '愉快'],
  ['sad', 'sorrow', 'cry', 'tears', 'melancholy', 'depressed', '悲伤', '难过', '哭', '忧郁'],
  ['angry', 'mad', 'furious', 'annoyed', 'irritated', '生气', '愤怒', '恼火'],
  ['shy', 'blush', 'embarrassed', 'flustered', 'bashful', '害羞', '脸红', '羞涩'],
  ['surprised', 'shock', 'amazed', 'startled', '惊讶', '震惊', '吃惊'],
  ['love', 'affection', 'warm', 'fond', 'heart', '喜欢', '温柔', '心动', '爱'],
  ['scared', 'fear', 'nervous', 'anxious', 'worried', '害怕', '紧张', '担心'],
  ['calm', 'neutral', 'normal', 'serene', 'relaxed', '平静', '冷静', '放松', '平常'],
  ['smug', 'teasing', 'playful', 'wink', '得意', '调侃', '俏皮', '坏笑'],
  ['sleepy', 'tired', 'drowsy', 'yawn', '困', '疲惫', '慵懒'],
]

function expandTerms(raw: string): string[] {
  const cleaned = raw.trim().toLowerCase()
  if (!cleaned) return []
  const tokens = cleaned.split(/[\s,/|·_-]+/).filter(Boolean)
  const out = new Set<string>([cleaned, ...tokens])
  for (const group of MOOD_SYNONYMS) {
    if (group.some((w) => cleaned.includes(w) || tokens.includes(w))) {
      for (const w of group) out.add(w)
    }
  }
  return [...out]
}

const HUD_COMPACT_KEY = 'pebble.hud.compact'

const MOOD_ATMOSPHERE_PALETTE: Array<{ keys: string[]; rgb: string }> = [
  {
    keys: ['shy', 'blush', 'flustered', 'embarrassed', 'bashful', 'crush', '害羞', '脸红', '羞涩', '心动', '局促'],
    rgb: '212, 152, 162',
  },
  {
    keys: ['tender', 'gentle', 'warm', 'fond', 'love', 'affection', 'soft', '温柔', '暖', '爱', '心软', '依恋', '柔软'],
    rgb: '216, 174, 134',
  },
  {
    keys: ['happy', 'joy', 'cheerful', 'playful', 'teasing', 'smug', 'delight', 'excited', 'smile', '开心', '高兴', '愉快', '欢喜', '调皮', '俏皮', '得意', '愉悦'],
    rgb: '220, 188, 122',
  },
  {
    keys: ['tense', 'nervous', 'anxious', 'worried', 'angry', 'annoyed', 'furious', 'jealous', 'sulking', 'mad', '紧张', '焦虑', '生闷气', '赌气', '憋气', '委屈', '吃醋', '恼', '愤怒', '慌', '不安'],
    rgb: '198, 132, 122',
  },
  {
    keys: ['sad', 'sorrow', 'cry', 'tears', 'melancholy', 'lonely', 'sleepy', 'tired', 'drowsy', '悲伤', '难过', '伤感', '忧郁', '失落', '思念', '困', '慵懒'],
    rgb: '148, 166, 190',
  },
]

function resolveMoodRgb(mood: string): string {
  const cleaned = mood.trim().toLowerCase()
  if (!cleaned) return ''
  for (const band of MOOD_ATMOSPHERE_PALETTE) {
    if (band.keys.some((k) => cleaned.includes(k))) {
      return band.rgb
    }
  }
  return ''
}

function readCompactPref(): boolean {
  try {
    return window.localStorage.getItem(HUD_COMPACT_KEY) === '1'
  } catch {
    return false
  }
}

export const useCardHudStore = defineStore('cardHud', () => {
  const session = useSessionStore()
  const cardAudio = useCardAudioStore()

  const hud = ref<Record<string, unknown>>({})
  const enabled = ref<boolean | null>(null)
  const effectiveEnabled = ref(false)
  const hasAssets = ref(false)
  const loading = ref(false)
  const open = ref(false)
  const compact = ref(readCompactPref())

  function toggleCompact() {
    compact.value = !compact.value
    try {
      window.localStorage.setItem(HUD_COMPACT_KEY, compact.value ? '1' : '0')
    } catch {
      // Ignore storage failures in restricted contexts.
    }
  }

  const affectionDelta = ref<number | null>(null)
  let deltaTimer = 0

  /** Foreground portrait/expression override (empty = default slide). */
  const stageExpressionRelPath = ref('')
  const activeExpressionLabel = ref('')
  /** Background scene backdrop override (empty = soft blurred portrait glow). */
  const stageSceneRelPath = ref('')
  const activeSceneLabel = ref('')
  const stageOverrideRelPath = computed(() => stageExpressionRelPath.value)

  const moodText = computed(() => String(hud.value.mood ?? '').trim())
  const moodRgb = computed(() => resolveMoodRgb(moodText.value))
  const affectionValue = computed<number | null>(() => {
    const raw = hud.value.affection
    if (typeof raw === 'number' && Number.isFinite(raw)) return raw
    if (typeof raw === 'string' && raw.trim() && Number.isFinite(Number(raw))) {
      return Number(raw)
    }
    return null
  })
  const locationText = computed(() => String(hud.value.location ?? '').trim())
  const outfitText = computed(() => String(hud.value.outfit ?? '').trim())
  const thoughtText = computed(() => String(hud.value.thought ?? '').trim())
  const thoughtEnabled = computed(() => session.hudThoughtEnabled)

  async function setThoughtEnabled(next: boolean) {
    await session.saveSceneRules({ hud_thought_enabled: next })
  }

  const extraEntries = computed(() => {
    const skip = new Set([
      'mood',
      'affection',
      'location',
      'outfit',
      'thought',
      'expression',
      'scene',
      'bgm',
      'sfx',
    ])
    return Object.entries(hud.value).filter(
      ([k, v]) => !skip.has(k) && v !== '' && v !== null && v !== undefined,
    )
  })

  const pinnedVarEntries = computed(() =>
    session.hudPinnedVars.map((key) => ({
      key,
      value: session.branchVariables[key] ?? '',
    })),
  )

  function isVarPinned(key: string): boolean {
    return session.hudPinnedVars.includes(key)
  }

  async function togglePinVar(key: string) {
    const k = key.trim()
    if (!k) return
    const next = isVarPinned(k)
      ? session.hudPinnedVars.filter((x) => x !== k)
      : [...session.hudPinnedVars, k]
    await session.saveSceneRules({ hud_pinned_vars: next })
  }

  async function savePinnedVar(key: string, val: string) {
    const k = key.trim()
    if (!k) return
    await session.saveBranchVariables({
      ...session.branchVariables,
      [k]: val,
    })
  }

  function hasExplicitBackgroundAssets(): boolean {
    return session.imageAssets.some((a) => (a.asset_type || '').toLowerCase() === 'background')
  }

  function hasExplicitExpressionAssets(): boolean {
    return session.imageAssets.some((a) =>
      ['emotion', 'portrait', 'icon'].includes((a.asset_type || '').toLowerCase()),
    )
  }

  function matchImageAsset(
    cue: string,
    mode: 'expression' | 'scene' = 'expression',
  ): CompanionAsset | undefined {
    const images = session.imageAssets
    const cleaned = cue.trim().toLowerCase()
    if (!images.length || !cleaned) return undefined

    const rank = (x: CompanionAsset) => {
      const t = (x.asset_type || '').toLowerCase()
      if (mode === 'scene') {
        return t === 'background' ? 0 : t === 'image' ? 1 : t === 'emotion' || t === 'portrait' ? 8 : 3
      }
      return t === 'emotion' ? 0 : t === 'portrait' ? 1 : t === 'icon' ? 2 : t === 'background' ? 8 : 3
    }
    const sorted = [...images].sort((a, b) => rank(a) - rank(b))
    const exactRanked = sorted.find(
      (a) => a.name.toLowerCase() === cleaned || a.rel_path.toLowerCase().endsWith(`/${cleaned}`),
    )
    if (exactRanked) return exactRanked

    const exact = session.findAssetByRef(cue)
    if (exact && exact.mime_type.startsWith('image/')) {
      const t = (exact.asset_type || '').toLowerCase()
      if (mode === 'scene' && (t === 'background' || !hasExplicitBackgroundAssets())) return exact
      if (mode === 'expression' && (t !== 'background' || !hasExplicitExpressionAssets())) return exact
    }

    const terms = expandTerms(cue)
    for (const term of terms) {
      const hit = sorted.find((a) => {
        const t = (a.asset_type || '').toLowerCase()
        if (mode === 'scene' && (t === 'emotion' || t === 'portrait') && hasExplicitBackgroundAssets()) {
          return false
        }
        if (mode === 'expression' && t === 'background' && hasExplicitExpressionAssets()) {
          return false
        }
        return a.name.toLowerCase().includes(term)
      })
      if (hit) return hit
    }
    return undefined
  }

  function clearExpressionOverride() {
    stageExpressionRelPath.value = ''
    activeExpressionLabel.value = ''
  }

  function clearSceneOverride() {
    stageSceneRelPath.value = ''
    activeSceneLabel.value = ''
  }

  function clearStageOverride() {
    clearExpressionOverride()
    clearSceneOverride()
  }

  function syncVisualLayersFromHud(
    hudData: Record<string, unknown>,
    mediaCue: Record<string, unknown> = {},
  ) {
    // 1) Foreground expression portrait layer
    const exprCueRaw = mediaCue.expression ?? hudData.expression ?? hudData.mood ?? ''
    const exprCue = String(exprCueRaw).trim()
    if (exprCue) {
      const low = exprCue.toLowerCase()
      if (low === 'none' || low === 'default') {
        clearExpressionOverride()
      } else {
        const matched = matchImageAsset(exprCue, 'expression')
        if (matched) {
          stageExpressionRelPath.value = matched.rel_path
          activeExpressionLabel.value = matched.name || exprCue
        }
      }
    } else if (!String(hudData.expression ?? '').trim() && !String(hudData.mood ?? '').trim()) {
      clearExpressionOverride()
    }

    // 2) Background scene layer ("人在景中")
    const sceneCueRaw =
      mediaCue.scene ?? hudData.scene ?? (hasExplicitBackgroundAssets() ? hudData.location ?? '' : '')
    const sceneCue = String(sceneCueRaw).trim()
    if (sceneCue) {
      const low = sceneCue.toLowerCase()
      if (low === 'none' || low === 'default' || low === 'off') {
        clearSceneOverride()
      } else {
        const matchedBg = matchImageAsset(sceneCue, 'scene')
        if (matchedBg) {
          stageSceneRelPath.value = matchedBg.rel_path
          activeSceneLabel.value = matchedBg.name || sceneCue
        }
      }
    } else if (!String(hudData.scene ?? '').trim()) {
      clearSceneOverride()
    }
  }

  async function load(cid = session.activeCompanionId, bid = session.activeBranch) {
    if (!cid || !bid) {
      hud.value = {}
      session.branchHud = {}
      enabled.value = null
      effectiveEnabled.value = false
      hasAssets.value = false
      clearStageOverride()
      return
    }
    loading.value = true
    try {
      const res = await getBranchHud(cid, bid)
      if (session.activeCompanionId !== cid || session.activeBranch !== bid) return
      hud.value = res.hud ?? {}
      session.branchHud = hud.value
      enabled.value = res.enabled
      effectiveEnabled.value = Boolean(res.effective_enabled)
      hasAssets.value = Boolean(res.has_assets)
      syncVisualLayersFromHud(hud.value)
    } catch {
      // Non-fatal: keep HUD clean if branch endpoint unavailable.
    } finally {
      loading.value = false
    }
  }

  function applyStreamUpdate(
    nextHud: Record<string, unknown>,
    mediaCue: Record<string, unknown> = {},
    nextVariables?: Record<string, string>,
  ) {
    const prevAff = affectionValue.value
    hud.value = { ...(nextHud ?? {}) }
    session.branchHud = hud.value
    if (nextVariables && typeof nextVariables === 'object') {
      session.branchVariables = { ...nextVariables }
    }
    const nextAff = affectionValue.value
    if (prevAff !== null && nextAff !== null && nextAff !== prevAff) {
      affectionDelta.value = nextAff - prevAff
      window.clearTimeout(deltaTimer)
      deltaTimer = window.setTimeout(() => {
        affectionDelta.value = null
      }, 4500)
    }

    // 1) Dual-layer visual stage sync (expression portrait + scene backdrop)
    syncVisualLayersFromHud(hud.value, mediaCue)

    // 2) BGM loop switch
    const bgmCue = String(mediaCue.bgm ?? '').trim()
    if (bgmCue) {
      if (['none', 'stop', 'off'].includes(bgmCue.toLowerCase())) {
        cardAudio.stopBgm()
      } else {
        const bgmAsset = session.findAssetByRef(bgmCue)
        if (bgmAsset && bgmAsset.mime_type.startsWith('audio/')) {
          void cardAudio.playBgmAsset(bgmAsset)
        }
      }
    }

    // 3) One-shot SFX trigger
    const sfxCue = String(mediaCue.sfx ?? '').trim()
    if (sfxCue && !['none', 'off'].includes(sfxCue.toLowerCase())) {
      const sfxAsset = session.findAssetByRef(sfxCue)
      if (sfxAsset && sfxAsset.mime_type.startsWith('audio/')) {
        void cardAudio.playSfx(sfxAsset)
      }
    }
  }

  async function saveHudFields(patch: Record<string, unknown>) {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) return
    const next = { ...hud.value, ...patch }
    hud.value = next
    session.branchHud = next
    const res = await updateBranchHud(cid, bid, { hud: next })
    hud.value = res.hud ?? next
    session.branchHud = hud.value
    effectiveEnabled.value = Boolean(res.effective_enabled)
    syncVisualLayersFromHud(hud.value)
  }

  async function removeHudField(key: string) {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid || !(key in hud.value)) return
    const next = { ...hud.value }
    delete next[key]
    hud.value = next
    session.branchHud = next
    const res = await updateBranchHud(cid, bid, { hud: next })
    hud.value = res.hud ?? next
    session.branchHud = hud.value
    effectiveEnabled.value = Boolean(res.effective_enabled)
    syncVisualLayersFromHud(hud.value)
  }

  const expressionCandidates = computed(() => {
    const rank = (a: CompanionAsset) => {
      const t = (a.asset_type || '').toLowerCase()
      return t === 'emotion' ? 0 : t === 'portrait' ? 1 : t === 'icon' ? 2 : t === 'background' ? 8 : 3
    }
    return [...session.imageAssets].sort((a, b) => rank(a) - rank(b))
  })

  const sceneCandidates = computed(() => {
    const rank = (a: CompanionAsset) => {
      const t = (a.asset_type || '').toLowerCase()
      return t === 'background' ? 0 : t === 'image' ? 1 : t === 'emotion' || t === 'portrait' ? 8 : 3
    }
    return [...session.imageAssets].sort((a, b) => rank(a) - rank(b))
  })

  async function setManualExpression(asset: CompanionAsset | null) {
    if (!asset) {
      await saveHudFields({ expression: '' })
      clearExpressionOverride()
      return
    }
    stageExpressionRelPath.value = asset.rel_path
    activeExpressionLabel.value = asset.name
    await saveHudFields({ expression: asset.name })
  }

  async function setManualScene(asset: CompanionAsset | null) {
    if (!asset) {
      await saveHudFields({ scene: '' })
      clearSceneOverride()
      return
    }
    stageSceneRelPath.value = asset.rel_path
    activeSceneLabel.value = asset.name
    await saveHudFields({ scene: asset.name })
  }

  async function setEnabled(next: boolean) {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) return
    const res = await updateBranchHud(cid, bid, { enabled: next })
    enabled.value = res.enabled
    effectiveEnabled.value = Boolean(res.effective_enabled)
    if (!effectiveEnabled.value) {
      affectionDelta.value = null
      clearStageOverride()
    }
  }

  async function reset() {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) return
    const res = await resetBranchHud(cid, bid)
    hud.value = res.hud ?? {}
    session.branchHud = hud.value
    affectionDelta.value = null
    clearStageOverride()
  }

  watch(
    () => [session.activeCompanionId, session.activeBranch, session.imageAssets.length] as const,
    ([cid, bid]) => {
      if (cid && bid) void load(cid, bid)
      else {
        hud.value = {}
        session.branchHud = {}
        clearStageOverride()
      }
    },
    { immediate: true },
  )

  return {
    hud,
    enabled,
    effectiveEnabled,
    hasAssets,
    loading,
    open,
    compact,
    toggleCompact,
    moodText,
    moodRgb,
    affectionValue,
    affectionDelta,
    locationText,
    outfitText,
    thoughtText,
    thoughtEnabled,
    setThoughtEnabled,
    extraEntries,
    pinnedVarEntries,
    isVarPinned,
    togglePinVar,
    savePinnedVar,
    expressionCandidates,
    sceneCandidates,
    stageExpressionRelPath,
    activeExpressionLabel,
    stageSceneRelPath,
    activeSceneLabel,
    stageOverrideRelPath,
    load,
    applyStreamUpdate,
    saveHudFields,
    removeHudField,
    setManualExpression,
    setManualScene,
    setEnabled,
    reset,
    clearExpressionOverride,
    clearSceneOverride,
    clearStageOverride,
  }
})

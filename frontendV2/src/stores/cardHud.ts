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

export const useCardHudStore = defineStore('cardHud', () => {
  const session = useSessionStore()
  const cardAudio = useCardAudioStore()

  const hud = ref<Record<string, unknown>>({})
  const enabled = ref<boolean | null>(null)
  const effectiveEnabled = ref(false)
  const hasAssets = ref(false)
  const loading = ref(false)
  const open = ref(false)

  const affectionDelta = ref<number | null>(null)
  let deltaTimer = 0

  /** Matched asset path for the active stage portrait (empty = default slide). */
  const stageOverrideRelPath = ref('')
  const activeExpressionLabel = ref('')

  const moodText = computed(() => String(hud.value.mood ?? '').trim())
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

  const extraEntries = computed(() => {
    const skip = new Set(['mood', 'affection', 'location', 'outfit', 'expression', 'scene', 'bgm', 'sfx'])
    return Object.entries(hud.value).filter(
      ([k, v]) => !skip.has(k) && v !== '' && v !== null && v !== undefined,
    )
  })

  function matchImageAsset(cue: string): CompanionAsset | undefined {
    const images = session.imageAssets
    if (!images.length || !cue.trim()) return undefined

    const exact = session.findAssetByRef(cue)
    if (exact && exact.mime_type.startsWith('image/')) return exact

    const terms = expandTerms(cue)
    // Prefer emotion/background assets over generic icon/other images.
    const sorted = [...images].sort((a, b) => {
      const score = (x: CompanionAsset) =>
        x.asset_type === 'emotion' ? 0 : x.asset_type === 'background' ? 1 : 2
      return score(a) - score(b)
    })

    for (const term of terms) {
      const hit = sorted.find((a) => a.name.toLowerCase().includes(term))
      if (hit) return hit
    }
    return undefined
  }

  function clearStageOverride() {
    stageOverrideRelPath.value = ''
    activeExpressionLabel.value = ''
  }

  async function load(cid = session.activeCompanionId, bid = session.activeBranch) {
    if (!cid || !bid) {
      hud.value = {}
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
      enabled.value = res.enabled
      effectiveEnabled.value = Boolean(res.effective_enabled)
      hasAssets.value = Boolean(res.has_assets)
      // Sync stage visual gently from stored expression/mood if available.
      const storedCue = String(hud.value.expression ?? hud.value.scene ?? hud.value.mood ?? '').trim()
      const matched = storedCue ? matchImageAsset(storedCue) : undefined
      if (matched) {
        stageOverrideRelPath.value = matched.rel_path
        activeExpressionLabel.value = matched.name || storedCue
      } else {
        clearStageOverride()
      }
    } catch {
      // Non-fatal: keep HUD clean if branch endpoint unavailable.
    } finally {
      loading.value = false
    }
  }

  function applyStreamUpdate(
    nextHud: Record<string, unknown>,
    mediaCue: Record<string, unknown> = {},
  ) {
    const prevAff = affectionValue.value
    hud.value = { ...(nextHud ?? {}) }
    const nextAff = affectionValue.value
    if (prevAff !== null && nextAff !== null && nextAff !== prevAff) {
      affectionDelta.value = nextAff - prevAff
      window.clearTimeout(deltaTimer)
      deltaTimer = window.setTimeout(() => {
        affectionDelta.value = null
      }, 4500)
    }

    // 1) Visual stage expression / scene switch
    const visualCue = String(
      mediaCue.expression ?? mediaCue.scene ?? hud.value.expression ?? hud.value.mood ?? '',
    ).trim()
    if (visualCue) {
      if (visualCue.toLowerCase() === 'none' || visualCue.toLowerCase() === 'default') {
        clearStageOverride()
      } else {
        const matched = matchImageAsset(visualCue)
        if (matched) {
          stageOverrideRelPath.value = matched.rel_path
          activeExpressionLabel.value = matched.name || visualCue
        }
      }
    }

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
    const res = await updateBranchHud(cid, bid, { hud: next })
    hud.value = res.hud ?? next
    effectiveEnabled.value = Boolean(res.effective_enabled)
  }

  async function setEnabled(next: boolean) {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) return
    const res = await updateBranchHud(cid, bid, { enabled: next })
    enabled.value = res.enabled
    effectiveEnabled.value = Boolean(res.effective_enabled)
  }

  async function reset() {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) return
    const res = await resetBranchHud(cid, bid)
    hud.value = res.hud ?? {}
    affectionDelta.value = null
    clearStageOverride()
  }

  watch(
    () => [session.activeCompanionId, session.activeBranch, session.imageAssets.length] as const,
    ([cid, bid]) => {
      if (cid && bid) void load(cid, bid)
      else {
        hud.value = {}
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
    moodText,
    affectionValue,
    affectionDelta,
    locationText,
    outfitText,
    extraEntries,
    stageOverrideRelPath,
    activeExpressionLabel,
    load,
    applyStreamUpdate,
    saveHudFields,
    setEnabled,
    reset,
    clearStageOverride,
  }
})

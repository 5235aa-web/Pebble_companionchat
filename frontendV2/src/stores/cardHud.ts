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
import { useInochi2dStore } from './inochi2d'

const MOOD_SYNONYMS: string[][] = [
  ['happy', 'joy', 'smile', 'cheerful', 'delighted', 'excited', 'contented', 'glad', '开心', '高兴', '微笑', '愉快'],
  ['sad', 'sorrow', 'cry', 'tears', 'melancholy', 'depressed', '悲伤', '难过', '哭', '忧郁'],
  ['angry', 'mad', 'furious', 'annoyed', 'irritated', '生气', '愤怒', '恼火'],
  ['shy', 'blush', 'embarrassed', 'flustered', 'bashful', '害羞', '脸红', '羞涩'],
  ['surprised', 'shock', 'amazed', 'startled', 'curious', '惊讶', '震惊', '吃惊', '好奇'],
  ['love', 'affection', 'warm', 'fond', 'heart', 'lovestruck', 'aroused', 'tender', '喜欢', '温柔', '心动', '爱'],
  ['scared', 'fear', 'nervous', 'anxious', 'worried', '害怕', '紧张', '担心'],
  ['calm', 'neutral', 'normal', 'serene', 'relaxed', 'relieved', 'thinking', '平静', '冷静', '放松', '平常'],
  ['smug', 'teasing', 'playful', 'wink', 'proud', '得意', '调侃', '俏皮', '坏笑'],
  ['sleepy', 'tired', 'drowsy', 'yawn', 'bored', 'exhausted', '困', '疲惫', '慵懒'],
]

function firstNonEmpty(...vals: unknown[]): string {
  for (const v of vals) {
    const s = String(v ?? '').trim()
    if (s) return s
  }
  return ''
}

const INLINE_REPLY_IMG_RE =
  /<img\s*=\s*(?:"([^"]+)"|'([^']+)')|<img\b[^>]*\bsrc\s*=\s*(?:"([^"]+)"|'([^']+)')|\{\{\s*(?:img|image|asset|raw)\s*::\s*([^}]+?)\s*\}\}/gi

function extractLastInlineImageRef(replyText: string): string {
  if (!replyText) return ''
  let lastRef = ''
  for (const m of replyText.matchAll(INLINE_REPLY_IMG_RE)) {
    const cand = (m[1] ?? m[2] ?? m[3] ?? m[4] ?? m[5] ?? '').trim()
    if (!cand || /^(?:https?:|data:|blob:|pebble-asset:)/i.test(cand)) continue
    lastRef = cand
  }
  return lastRef
}

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
  const stageExpressionSeq = ref(0)
  const stageOverrideRelPath = computed(() => stageExpressionRelPath.value)
  let explicitExpressionLocked = false
  let lastAssistantReplyText = ''

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

  function hasExplicitExpressionAssets(): boolean {
    return session.imageAssets.some((a) =>
      ['emotion', 'portrait', 'icon'].includes((a.asset_type || '').toLowerCase()),
    )
  }

  function matchImageAsset(cue: string): CompanionAsset | undefined {
    const images = session.imageAssets
    const cleaned = cue.trim().toLowerCase()
    if (!images.length || !cleaned) return undefined

    const rank = (x: CompanionAsset) => {
      const t = (x.asset_type || '').toLowerCase()
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
      if (t !== 'background' || !hasExplicitExpressionAssets()) return exact
    }

    const terms = expandTerms(cue)
    for (const term of terms) {
      const hit = sorted.find((a) => {
        const t = (a.asset_type || '').toLowerCase()
        if (t === 'background' && hasExplicitExpressionAssets()) {
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

  function clearStageOverride() {
    clearExpressionOverride()
  }

  function syncVisualLayersFromHud(
    hudData: Record<string, unknown>,
    mediaCue: Record<string, unknown> = {},
  ) {
    // Foreground expression portrait layer (avoid empty "" blocking fallback to mood)
    const cueExprRaw = String(mediaCue.expression ?? '').trim()
    const cueExprLow = cueExprRaw.toLowerCase()
    if (cueExprLow === 'none' || cueExprLow === 'default') {
      explicitExpressionLocked = true
      clearExpressionOverride()
      stageExpressionSeq.value += 1
    } else {
      const explicitExpr = firstNonEmpty(mediaCue.expression, hudData.expression)
      if (explicitExpr) {
        const matched = matchImageAsset(explicitExpr)
        if (matched) {
          explicitExpressionLocked = true
          stageExpressionRelPath.value = matched.rel_path
          activeExpressionLabel.value = matched.name || explicitExpr
          stageExpressionSeq.value += 1
        } else {
          explicitExpressionLocked = false
        }
      } else {
        explicitExpressionLocked = false
      }

      if (!explicitExpressionLocked) {
        const moodCue = String(hudData.mood ?? '').trim()
        if (moodCue) {
          const matchedMood = matchImageAsset(moodCue)
          if (matchedMood) {
            stageExpressionRelPath.value = matchedMood.rel_path
            activeExpressionLabel.value = matchedMood.name || moodCue
            stageExpressionSeq.value += 1
          }
        } else if (!explicitExpr) {
          clearExpressionOverride()
        }
      }
    }
  }

  function syncFromAssistantReply(replyText: string) {
    lastAssistantReplyText = replyText || ''
    if (replyText) {
      useInochi2dStore().applyChatCue(
        replyText,
        moodText.value,
        activeExpressionLabel.value || String(hud.value.expression ?? ''),
      )
    }
    if (!replyText || explicitExpressionLocked || !session.imageAssets.length) return
    const ref = extractLastInlineImageRef(replyText)
    if (!ref) return
    const exact = session.findAssetByRef(ref)
    const asset =
      exact && exact.mime_type.startsWith('image/')
        ? exact
        : matchImageAsset(ref)
    if (!asset) return
    const aType = (asset.asset_type || '').toLowerCase()
    if (aType === 'background') return
    stageExpressionRelPath.value = asset.rel_path
    activeExpressionLabel.value = asset.name || ref
    stageExpressionSeq.value += 1
  }

  async function load(cid = session.activeCompanionId, bid = session.activeBranch) {
    if (!cid || !bid) {
      hud.value = {}
      session.branchHud = {}
      enabled.value = null
      effectiveEnabled.value = false
      hasAssets.value = false
      lastAssistantReplyText = ''
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
      if (!explicitExpressionLocked && lastAssistantReplyText) {
        syncFromAssistantReply(lastAssistantReplyText)
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
    useInochi2dStore().applyChatCue(
      '',
      String(hud.value.mood ?? ''),
      String(mediaCue.expression ?? hud.value.expression ?? ''),
    )

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
    ([cid, bid], prev) => {
      if (prev && (prev[0] !== cid || prev[1] !== bid)) {
        lastAssistantReplyText = ''
      }
      if (cid && bid) void load(cid, bid)
      else {
        hud.value = {}
        session.branchHud = {}
        lastAssistantReplyText = ''
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
    stageExpressionRelPath,
    activeExpressionLabel,
    stageExpressionSeq,
    stageOverrideRelPath,
    load,
    applyStreamUpdate,
    syncFromAssistantReply,
    saveHudFields,
    removeHudField,
    setManualExpression,
    setEnabled,
    reset,
    clearExpressionOverride,
    clearStageOverride,
  }
})

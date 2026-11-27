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
import { extractProtectedAssetTags } from '@/utils/inlineMedia'
import type { DisplayRegexRule } from '@/utils/displayRegex'

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

const PLACEHOLDER_ICON_NAMES = new Set(['iconx', 'main', 'avatar', 'cover', 'thumb', 'thumbnail'])
const GENERIC_SCENE_NAME_RE = /^(?:\d+|(?:scene|bg|cg|map|item|ui|icon)[-_\s]*\d*)$/i

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function parseAffectionValue(raw: unknown): number | null {
  if (raw === null || raw === undefined || typeof raw === 'boolean') return null
  if (typeof raw === 'number') {
    return Number.isFinite(raw) ? Math.max(0, Math.min(100, Math.round(raw))) : null
  }
  const text = String(raw).trim()
  if (!text) return null
  if (Number.isFinite(Number(text))) {
    return Math.max(0, Math.min(100, Math.round(Number(text))))
  }
  const mSigned = text.match(/^\s*([+-])\s*(\d+(?:\.\d+)?)\b/)
  if (mSigned) {
    const n = (mSigned[1] === '-' ? -1 : 1) * Number(mSigned[2])
    return Number.isFinite(n) ? Math.max(0, Math.min(100, Math.round(n))) : null
  }
  const mNum = text.match(/^\s*(?:[^\d+\-.]{0,18}?[:：=♥♡\s])?\s*(\d+(?:\.\d+)?)\b/)
  if (mNum) {
    const n = Number(mNum[1])
    return Number.isFinite(n) ? Math.max(0, Math.min(100, Math.round(n))) : null
  }
  return null
}

function extractLastInlineImageRef(replyText: string, rules?: DisplayRegexRule[]): string {
  if (!replyText) return ''
  const protectedTags = extractProtectedAssetTags(rules)
  const htmlAlts = [...new Set(['img', ...protectedTags.htmlTags])].map(escapeRegExp).join('|')
  const macroAlts = [...new Set(['img', 'image', 'asset', 'raw', ...protectedTags.macroTags])]
    .map(escapeRegExp)
    .join('|')
  const tagRe = new RegExp(
    `<(?:${htmlAlts})\\s*=\\s*(?:"([^"]+)"|'([^']+)')|<img\\b[^>]*\\bsrc\\s*=\\s*(?:"([^"]+)"|'([^']+)')|\\{\\{\\s*(?:${macroAlts})\\s*::\\s*([^}]+?)\\s*\\}\\}`,
    'gi',
  )

  const candidates: string[] = []
  for (const m of replyText.matchAll(tagRe)) {
    const cand = (m[1] ?? m[2] ?? m[3] ?? m[4] ?? m[5] ?? '').trim()
    if (!cand || /^(?:https?:|data:|blob:|pebble-asset:)/i.test(cand)) continue
    candidates.push(cand)
  }
  if (!candidates.length) return ''
  for (let i = candidates.length - 1; i >= 0; i--) {
    if (!GENERIC_SCENE_NAME_RE.test(candidates[i])) {
      return candidates[i]
    }
  }
  return candidates[candidates.length - 1]
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
  let inlinePortraitLocked = false
  let lastAssistantReplyText = ''

  const moodText = computed(() => String(hud.value.mood ?? '').trim())
  const moodRgb = computed(() => resolveMoodRgb(moodText.value))
  const affectionValue = computed<number | null>(() => parseAffectionValue(hud.value.affection))
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
    return session.imageAssets.some((a) => {
      const t = (a.asset_type || '').toLowerCase()
      const n = (a.name || '').trim().toLowerCase()
      if (t === 'emotion' || t === 'portrait') return true
      return t === 'icon' && !PLACEHOLDER_ICON_NAMES.has(n)
    })
  }

  function collectCharacterGroups(images: CompanionAsset[]): Set<string> {
    const counts = new Map<string, number>()
    for (const a of images) {
      const t = (a.asset_type || '').toLowerCase()
      const nameLow = (a.name || '').trim().toLowerCase()
      if (!nameLow || (t === 'icon' && PLACEHOLDER_ICON_NAMES.has(nameLow))) continue
      if (GENERIC_SCENE_NAME_RE.test(nameLow)) continue
      const parts = nameLow.split('_').filter(Boolean)
      if (parts.length !== 2) continue
      const prefix = parts[0].trim()
      if (prefix.length < 2 || GENERIC_SCENE_NAME_RE.test(prefix)) continue
      counts.set(prefix, (counts.get(prefix) ?? 0) + 1)
    }
    const groups = new Set<string>()
    for (const [k, cnt] of counts.entries()) {
      if (cnt >= 2) groups.add(k)
    }
    return groups
  }

  function extractCueCharacterGroup(cue: string, groups: Set<string>): string {
    if (!cue || !groups.size) return ''
    const tokens = cue
      .trim()
      .toLowerCase()
      .split(/[_\-\s,/|·()[\]]+/)
      .filter(Boolean)
    for (let i = tokens.length - 1; i >= 0; i -= 1) {
      if (groups.has(tokens[i])) return tokens[i]
    }
    return ''
  }

  function inferActiveCharacterGroup(groups: Set<string>): string {
    if (groups.size === 1) return [...groups][0]
    if (!groups.size) return ''
    const inlineRef = extractLastInlineImageRef(lastAssistantReplyText, session.allCardRegexRules)
    const fromReply = extractCueCharacterGroup(inlineRef, groups)
    if (fromReply) return fromReply
    if (inlinePortraitLocked || explicitExpressionLocked) {
      const fromActive = extractCueCharacterGroup(activeExpressionLabel.value, groups)
      if (fromActive) return fromActive
    }
    const fromCardName = extractCueCharacterGroup(session.act || '', groups)
    if (fromCardName) return fromCardName
    const greetingRef = extractLastInlineImageRef(session.emptyLine || '', session.allCardRegexRules)
    return extractCueCharacterGroup(greetingRef, groups)
  }

  function matchImageAsset(cue: string, preferredGroup = ''): CompanionAsset | undefined {
    const images = session.imageAssets
    const cleaned = cue.trim().toLowerCase()
    if (!images.length || !cleaned) return undefined

    const groups = collectCharacterGroups(images)
    const cueGroup = extractCueCharacterGroup(cue, groups)
    const activeGroup = cueGroup || preferredGroup

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
    const cueParts = cleaned.split(/[_-]+/).filter(Boolean)
    const cueTail = cueParts.length ? cueParts[cueParts.length - 1] : cleaned
    const charTokens = (session.act || '')
      .toLowerCase()
      .split(/[\s,·/|()[\]<>'"!?！？~_-]+/)
      .filter((t) => t.length >= 2)
    let bestHit: CompanionAsset | undefined
    let bestScore = 0
    for (const a of sorted) {
      const t = (a.asset_type || '').toLowerCase()
      const nameLow = (a.name || '').trim().toLowerCase()
      if (!nameLow) continue
      if (t === 'background' && hasExplicitExpressionAssets()) continue
      if (t === 'icon' && PLACEHOLDER_ICON_NAMES.has(nameLow)) continue
      if (GENERIC_SCENE_NAME_RE.test(nameLow)) continue

      const assetGroup = extractCueCharacterGroup(nameLow, groups)
      if (groups.size >= 2 && assetGroup) {
        if (activeGroup && assetGroup !== activeGroup) continue
        if (!activeGroup) continue
      }

      let score = 0
      const matchedTerms = new Set<string>()
      for (const term of terms) {
        if (!term || term.length < 2) continue
        if (!nameLow.includes(term)) continue
        const boundaryRe = new RegExp(`(?:^|[_\\-\\s])${escapeRegExp(term)}(?:$|[_\\-\\s])`, 'i')
        const pts = boundaryRe.test(nameLow) ? 40 : 12
        if (!matchedTerms.has(term)) {
          matchedTerms.add(term)
          score += pts
        }
      }
      if (score <= 0) continue
      if (cueGroup && nameLow === `${cueGroup}_${cueTail}`) score += 120
      else if (cueTail.length >= 2 && (nameLow === cueTail || nameLow.endsWith(`_${cueTail}`))) score += 60
      if (t === 'emotion' || t === 'portrait') score += 50
      if (charTokens.some((ct) => nameLow.includes(ct))) score += 80
      if (score > bestScore) {
        bestScore = score
        bestHit = a
      }
    }
    return bestHit
  }

  function clearExpressionOverride() {
    inlinePortraitLocked = false
    stageExpressionRelPath.value = ''
    activeExpressionLabel.value = ''
  }

  function clearStageOverride() {
    explicitExpressionLocked = false
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
          inlinePortraitLocked = false
          stageExpressionRelPath.value = matched.rel_path
          activeExpressionLabel.value = matched.name || explicitExpr
          stageExpressionSeq.value += 1
        } else {
          explicitExpressionLocked = false
        }
      } else {
        explicitExpressionLocked = false
      }

      if (!explicitExpressionLocked && !inlinePortraitLocked) {
        const moodCue = String(hudData.mood ?? '').trim()
        if (moodCue) {
          const groups = collectCharacterGroups(session.imageAssets)
          const activeGroup = inferActiveCharacterGroup(groups)
          const matchedMood = matchImageAsset(moodCue, activeGroup)
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
    if (!replyText || explicitExpressionLocked || !session.imageAssets.length) {
      if (!replyText) inlinePortraitLocked = false
      return
    }
    const ref = extractLastInlineImageRef(replyText, session.allCardRegexRules)
    if (!ref) {
      inlinePortraitLocked = false
      return
    }
    const exact = session.findAssetByRef(ref)
    const asset =
      exact && exact.mime_type.startsWith('image/')
        ? exact
        : matchImageAsset(ref)
    if (!asset) {
      inlinePortraitLocked = false
      return
    }
    const aType = (asset.asset_type || '').toLowerCase()
    if (aType === 'background') return
    inlinePortraitLocked = true
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

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { Branch, CastMember } from '@/data/chat'
import { getTokenStats } from '@/api/compaction'
import { resolvePlaceholders, resolveUserName } from '@/utils/text'
import { evaluateCbs } from '@/utils/cbs'
import { applyDisplayRegex } from '@/utils/displayRegex'
import {
  normalizeCompanionLocale,
  resolveCompanionDisplayGreetings,
  resolveCompanionDisplayTags,
  resolveCompanionDisplayText,
  type CompanionLocale,
} from '@/utils/companionTranslation'
import {
  createBranch,
  deleteBranch,
  getBranchVariables,
  getBranches,
  renameBranch,
  resetBranchVariables,
  getCompanion,
  listCompanionAssets,
  listCompanions,
  updateBranchVariables,
  updateCompanion,
  updateSceneRules,
  type CardPresentation,
  type Companion,
  type CompanionAsset,
  type CompanionBranch,
  type SceneRulesUpdatePayload,
} from '@/api/companion'

export interface TokenQuery {
  model: string
  providerType: string
  baseUrl: string
  maxContext: number
}

export interface InPlayEntry {
  id: number
  label: string
  weight: number
  key: string
}

/**
 * Which branch each companion was last reading, keyed by companion id.
 *
 * Persisted so reopening the app lands on the branch the user left, instead of
 * silently dropping them back into main. A single map keeps it to one storage
 * key however many companions exist.
 */
const BRANCH_MAP_KEY = 'pebble.branches'

/**
 * Which companion the app was last talking to. Without this the store starts
 * empty and `loadCompanions` falls back to the first entry, so every restart
 * silently switched characters while the branch map still pointed at the old
 * one's branch.
 */
const COMPANION_KEY = 'pebble.activeCompanion'

function readActiveCompanion(): string {
  try {
    return localStorage.getItem(COMPANION_KEY) ?? ''
  } catch {
    return ''
  }
}

function writeActiveCompanion(id: string): void {
  try {
    if (id) localStorage.setItem(COMPANION_KEY, id)
    else localStorage.removeItem(COMPANION_KEY)
  } catch {
    /* storage may be unavailable; the session still works, it just forgets */
  }
}

function readBranchMap(): Record<string, string> {
  try {
    const raw = localStorage.getItem(BRANCH_MAP_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return {}
    const out: Record<string, string> = {}
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof v === 'string' && v) out[k] = v
    }
    return out
  } catch {
    return {}
  }
}

function writeBranchMap(map: Record<string, string>) {
  try {
    localStorage.setItem(BRANCH_MAP_KEY, JSON.stringify(map))
  } catch {
    /* storage may be unavailable; the session still works, it just forgets */
  }
}

function rememberBranch(companionId: string, branchId: string) {
  if (!companionId || !branchId) return
  const map = readBranchMap()
  if (map[companionId] === branchId) return
  map[companionId] = branchId
  writeBranchMap(map)
}

/**
 * Pull the voice identity out of a companion's stored `tts_config`.
 *
 * `voiceRef` is a clip in the shared voice library (cloning engines); `voiceId`
 * is a built-in id (MiniMax, ElevenLabs, Ming3's tts task). Either may be empty.
 */
function readVoice(cfg: Record<string, unknown> | undefined): CastMember['ttsVoice'] {
  if (!cfg) return undefined
  const ref = typeof cfg.voiceRef === 'string' ? cfg.voiceRef : ''
  const id = typeof cfg.voiceId === 'string' ? cfg.voiceId : ''
  return ref || id ? { ref, id } : undefined
}

const toCast = (c: Companion): CastMember => {
  const displayDesc = resolveCompanionDisplayText(c, 'description')
  const displayTags = resolveCompanionDisplayTags(c)
  return {
    id: c.id,
    avatar: c.avatar_path || '',
    letter: (c.name || '?').charAt(0).toUpperCase(),
    name: c.name || c.id,
    role: c.creator || '',
    desc: displayDesc,
    meta: displayTags.join(' · '),
    tags: displayTags.join(', '),
    acts: '',
    lore: '',
    lastSeen: (c.updated_at || '').slice(0, 10),
    branch: '',
    active: false,
    pinned: Boolean(c.pinned),
    assetCount: Number(c.asset_count ?? 0),
    cardFormat: c.card_format || '',
    translations: c.translations,
    activeLocale: normalizeCompanionLocale(c.active_locale),
    promptUseOriginal: Boolean(c.prompt_use_original),
    ttsVoice: readVoice(c.tts_config),
  }
}

const toBranch = (b: CompanionBranch, activeId: string): Branch => ({
  id: b.branch_id,
  // Empty for the main branch: the UI substitutes a localised label, so the
  // stored value never pins the name to one language.
  name: b.branch_name || (b.branch_id === 'main' ? '' : b.branch_id),
  count: b.message_count ?? 0,
  active: b.branch_id === activeId,
})

export const useSessionStore = defineStore('session', () => {
  /* ---- cast ---- */
  const companions = ref<CastMember[]>([])
  const activeCompanionId = ref(readActiveCompanion())
  const loadingCast = ref(false)

  /* ---- act / branch ---- */
  const act = ref('')
  const branchTitle = ref('')
  const branches = ref<Branch[]>([])
  const activeBranch = ref('')

  /* ---- the strip ---- */
  const scenario = ref('')
  const model = ref('')
  const voice = ref('')

  const modelBackend = ref('')
  const voiceBackend = ref('')
  const asrModel = ref('')
  const asrBackend = ref('')
  const endpoint = ref('')
  const apiKey = ref('')

  const inPlay = ref<InPlayEntry[]>([])

  const tokensUsed = ref(0)
  const tokensTotal = ref(0)
  const tokenPct = computed(() =>
    tokensTotal.value > 0 ? (tokensUsed.value / tokensTotal.value) * 100 : 0,
  )

  /**
   * The readout keeps two decimals. Rounding to whole percent hides exactly the
   * movement this strip is for: a reply that costs 0.4% of the window would not
   * move the number at all.
   */
  const tokenPctText = computed(() => tokenPct.value.toFixed(2))

  const sceneBreak = ref('')

  /* ---- empty state copy comes from the loaded character ---- */
  const rawGreetings = ref<string[]>([])
  const greetingIndex = ref(0)
  const emptyHints = ref<string[]>([])

  /* ---- card presentation (Display Regex, Scene Rules, Card CSS, CBS variables) ---- */
  const cardPresentation = ref<CardPresentation>({
    default_variables: {},
    display_regex: [],
    output_regex: [],
    input_regex: [],
    prompt_regex: [],
    scene_rules: [],
    hud_pinned_vars: [],
    hud_thought_enabled: false,
    card_css: '',
  })
  const defaultVariables = ref<Record<string, string>>({})
  const branchVariables = ref<Record<string, string>>({})
  const branchHud = ref<Record<string, unknown>>({})
  const displayRegexRules = computed(() => cardPresentation.value.display_regex)
  const outputRegexRules = computed(() => cardPresentation.value.output_regex ?? [])
  const inputRegexRules = computed(() => cardPresentation.value.input_regex ?? [])
  const promptRegexRules = computed(() => cardPresentation.value.prompt_regex ?? [])
  const sceneRules = computed(() => cardPresentation.value.scene_rules ?? [])
  const hudPinnedVars = computed(() => cardPresentation.value.hud_pinned_vars ?? [])
  const hudThoughtEnabled = computed(() => Boolean(cardPresentation.value.hud_thought_enabled))
  const cardCss = computed(() => {
    const raw = cardPresentation.value.card_css || ''
    if (!raw) return ''
    const opts = {
      charName: act.value,
      userName: userName.value,
      variables: branchVariables.value,
      hud: branchHud.value,
      seed: `${activeCompanionId.value}:${activeBranch.value}`,
      assetNames: assetNames.value,
    }
    const withCbs = evaluateCbs(raw, opts)
    return applyDisplayRegex(withCbs, displayRegexRules.value, { ...opts, placement: 2 })
  })

  const greetings = computed(() =>
    rawGreetings.value.map((raw) =>
      resolvePlaceholders(
        raw,
        act.value,
        userName.value,
        branchVariables.value,
        `${activeCompanionId.value}:${activeBranch.value}`,
        assetNames.value,
        branchHud.value,
      ),
    ),
  )
  const emptyLine = computed(() => greetings.value[greetingIndex.value] ?? '')

  function cycleGreeting(step: number) {
    const n = greetings.value.length
    if (n < 2) return
    greetingIndex.value = (greetingIndex.value + step + n) % n
  }

  const error = ref('')

  /**
   * The user's name as read from the card's `user_info`. Card macros need it,
   * and it is computed once here rather than re-parsed by every renderer.
   */
  const userName = ref('the user')

  const activeCast = computed(() =>
    companions.value.find((c) => c.id === activeCompanionId.value),
  )

  const companionAssets = ref<CompanionAsset[]>([])
  const assetNames = computed(() =>
    companionAssets.value.map((a) => a.name).filter(Boolean),
  )
  const imageAssets = computed(() =>
    companionAssets.value.filter((a) => (a.mime_type || '').startsWith('image/')),
  )
  const audioAssets = computed(() =>
    companionAssets.value.filter((a) => (a.mime_type || '').startsWith('audio/')),
  )
  const videoAssets = computed(() =>
    companionAssets.value.filter(
      (a) => (a.mime_type || '').startsWith('video/') || a.asset_type === 'video',
    ),
  )

  async function loadCompanionAssets(id = activeCompanionId.value) {
    if (!id) {
      companionAssets.value = []
      return
    }
    try {
      const list = await listCompanionAssets(id)
      if (activeCompanionId.value === id) companionAssets.value = list
    } catch {
      if (activeCompanionId.value === id) companionAssets.value = []
    }
  }

  function safeDecodeUri(value: string): string {
    try {
      return decodeURIComponent(value)
    } catch {
      return value
    }
  }

  function normalizeAssetRef(value: string): string {
    return safeDecodeUri((value || '').trim())
      .split(/[?#]/, 1)[0]
      .trim()
      .toLowerCase()
      .replace(/^embeded:\/\//, '')
      .replace(/^embedded:\/\//, '')
      .replace(/^__asset:/, '')
      .replace(/^\.?\//, '')
  }

  function findAssetByRef(refText: string): CompanionAsset | undefined {
    const rawQ = (refText || '').trim().toLowerCase()
    const q = safeDecodeUri(refText || '').split(/[?#]/, 1)[0].trim().toLowerCase()
    if (!q) return undefined
    const stripped = normalizeAssetRef(refText)
    if (!stripped) return undefined
    const base = stripped.split('/').pop() || stripped
    const stem = base.replace(/\.[^.]+$/, '')
    return companionAssets.value.find((a) => {
      const name = (a.name || '').toLowerCase()
      const uriRaw = (a.source_uri || '').toLowerCase()
      const uriNorm = normalizeAssetRef(a.source_uri || '')
      const relBase = safeDecodeUri(a.rel_path.split('/').pop() || '').toLowerCase()
      return (
        name === q ||
        name === stripped ||
        name === base ||
        name === stem ||
        uriRaw === rawQ ||
        uriRaw === q ||
        (uriNorm && (uriNorm === stripped || uriNorm.endsWith('/' + stripped))) ||
        relBase === base
      )
    })
  }

  function applyCompanion(c: Companion) {
    act.value = c.name || c.id
    scenario.value = resolveCompanionDisplayText(c, 'scenario')
    const name = resolveUserName(c.user_info)
    userName.value = name
    cardPresentation.value = c.card_presentation ?? {
      default_variables: {},
      display_regex: [],
      output_regex: [],
      input_regex: [],
      prompt_regex: [],
      scene_rules: [],
      card_css: '',
    }
    defaultVariables.value = { ...(cardPresentation.value.default_variables ?? {}) }
    if (!Object.keys(branchVariables.value).length) {
      branchVariables.value = { ...defaultVariables.value }
    }
    // Keep raw greetings so CBS conditionals & variables re-evaluate reactively
    // whenever branchVariables change.
    const resolvedGreetings = resolveCompanionDisplayGreetings(c)
    const list: string[] = []
    if ((resolvedGreetings.firstMes || '').trim()) {
      list.push(resolvedGreetings.firstMes)
    }
    for (const alt of resolvedGreetings.alternateGreetings) {
      if (typeof alt === 'string' && alt.trim()) {
        list.push(alt)
      }
    }
    rawGreetings.value = list
    greetingIndex.value = 0
    const castPatch = toCast(c)
    companions.value = companions.value.map((m) =>
      m.id === c.id ? { ...castPatch, active: true } : { ...m, active: false },
    )
  }

  async function setCompanionLocale(id: string, locale: CompanionLocale) {
    if (!id) return
    const nextLocale = normalizeCompanionLocale(locale)
    try {
      await updateCompanion(id, { active_locale: nextLocale })
      await loadCompanions()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  async function loadBranchVariables(
    cid = activeCompanionId.value,
    bid = activeBranch.value || 'main',
  ) {
    if (!cid || !bid) return
    try {
      const res = await getBranchVariables(cid, bid)
      if (activeCompanionId.value !== cid || (activeBranch.value || 'main') !== bid) return
      defaultVariables.value = res.defaults ?? {}
      branchVariables.value = res.variables ?? {}
    } catch {
      // Fall back to card defaults if branch variable lookup fails.
    }
  }

  async function saveBranchVariables(vars: Record<string, string>) {
    const cid = activeCompanionId.value
    const bid = activeBranch.value || 'main'
    if (!cid) return
    const res = await updateBranchVariables(cid, bid, vars)
    defaultVariables.value = res.defaults ?? {}
    branchVariables.value = res.variables ?? {}
  }

  async function resetBranchVariablesLocal() {
    const cid = activeCompanionId.value
    const bid = activeBranch.value || 'main'
    if (!cid) return
    const res = await resetBranchVariables(cid, bid)
    defaultVariables.value = res.defaults ?? {}
    branchVariables.value = res.variables ?? {}
  }

  async function saveSceneRules(payload: SceneRulesUpdatePayload) {
    const cid = activeCompanionId.value
    if (!cid) return null
    const res = await updateSceneRules(cid, payload)
    if (activeCompanionId.value !== cid) return res
    if (res.card_presentation) {
      cardPresentation.value = res.card_presentation
    } else {
      cardPresentation.value = {
        ...cardPresentation.value,
        ...(payload.display_regex !== undefined ? { display_regex: payload.display_regex } : {}),
        ...(payload.output_regex !== undefined ? { output_regex: payload.output_regex } : {}),
        ...(payload.input_regex !== undefined ? { input_regex: payload.input_regex } : {}),
        ...(payload.prompt_regex !== undefined ? { prompt_regex: payload.prompt_regex } : {}),
        ...(payload.scene_rules !== undefined ? { scene_rules: payload.scene_rules } : {}),
        ...(payload.hud_pinned_vars !== undefined
          ? { hud_pinned_vars: payload.hud_pinned_vars }
          : {}),
        ...(payload.hud_thought_enabled !== undefined
          ? { hud_thought_enabled: payload.hud_thought_enabled }
          : {}),
        ...(payload.card_css !== undefined ? { card_css: payload.card_css } : {}),
      }
    }
    return res
  }

  async function loadCompanions() {
    loadingCast.value = true
    error.value = ''
    try {
      const list = await listCompanions()
      companions.value = list.map(toCast)
      // Prefer the remembered companion; fall back to the first only when it is
      // gone (deleted on another machine, or storage was cleared).
      const remembered = activeCompanionId.value
      const exists = remembered && list.some((c) => c.id === remembered)
      if (!exists && list.length) {
        await selectCompanion(list[0].id)
      } else if (exists) {
        await selectCompanion(remembered)
      }
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
      companions.value = []
    } finally {
      loadingCast.value = false
    }
  }

  async function togglePin(id: string) {
    const current = companions.value.find((c) => c.id === id)
    if (!current) return
    const next = !current.pinned
    // Optimistic flip so the star reacts on click; a failed write reloads the
    // list from the server and the star snaps back.
    companions.value = companions.value.map((c) =>
      c.id === id ? { ...c, pinned: next } : c,
    )
    try {
      await updateCompanion(id, { pinned: next })
      await loadCompanions()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
      await loadCompanions()
    }
  }

  async function selectCompanion(id: string) {
    activeCompanionId.value = id
    writeActiveCompanion(id)
    // Restore the branch this companion was last read on. It also has to be
    // cleared here: leaving the previous companion's branch in place would
    // carry an id that does not exist in the new companion's branch list.
    activeBranch.value = readBranchMap()[id] ?? ''
    branchTitle.value = ''
    inPlay.value = []
    companionAssets.value = []
    branchVariables.value = {}
    defaultVariables.value = {}
    cardPresentation.value = {
      default_variables: {},
      display_regex: [],
      output_regex: [],
      input_regex: [],
      prompt_regex: [],
      scene_rules: [],
      card_css: '',
    }
    companions.value = companions.value.map((m) => ({ ...m, active: m.id === id }))
    error.value = ''
    try {
      const [full] = await Promise.all([getCompanion(id), loadCompanionAssets(id)])
      if (full) applyCompanion(full)
      await loadBranches()
      await loadBranchVariables(id, activeBranch.value || 'main')
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  async function loadBranches() {
    if (!activeCompanionId.value) return
    try {
      const list = await getBranches(activeCompanionId.value)
      // The remembered branch may have been deleted since it was stored, so
      // fall back to the first one rather than pointing at a missing id.
      const remembered = activeBranch.value
      if (!remembered || !list.some((b) => b.branch_id === remembered)) {
        activeBranch.value = list[0]?.branch_id ?? ''
      }
      branches.value = list.map((b) => toBranch(b, activeBranch.value))
      if (activeBranch.value) rememberBranch(activeCompanionId.value, activeBranch.value)
      const cur = branches.value.find((b) => b.active)
      branchTitle.value = cur?.name ?? ''
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
      branches.value = []
    }
  }

  async function loadTokens(q?: TokenQuery) {
    const cid = activeCompanionId.value
    const bid = activeBranch.value
    if (!cid || !bid) return
    try {
      const st = await getTokenStats(
        cid,
        bid,
        q?.model ?? '',
        q?.providerType ?? '',
        q?.baseUrl ?? '',
        q?.maxContext ?? 0,
      )
      tokensUsed.value = st.token_count ?? 0
      tokensTotal.value = st.context_window ?? 0
    } catch {
      // Token accounting is advisory: a failure must not blank the strip.
    }
  }

  /**
   * Which memories went into the latest reply.
   *
   * Written by the chat stream when a reply completes and cleared whenever the
   * conversation underneath it changes, so the dots never describe a reply the
   * user is no longer looking at.
   */
  function setInPlay(list: InPlayEntry[]) {
    inPlay.value = Array.isArray(list) ? list : []
  }

  function selectBranch(id: string) {
    branches.value = branches.value.map((b) => ({ ...b, active: b.id === id }))
    activeBranch.value = id
    branchTitle.value = branches.value.find((b) => b.id === id)?.name ?? ''
    rememberBranch(activeCompanionId.value, id)
    inPlay.value = []
    void loadBranchVariables(activeCompanionId.value, id)
  }

  async function newBranch() {
    if (!activeCompanionId.value) return
    try {
      const r = await createBranch(
        activeCompanionId.value,
        'branch ' + (branches.value.length + 1),
      )
      await loadBranches()
      if (r.branch_id) selectBranch(r.branch_id)
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  async function renameBranchLocal(id: string, name: string) {
    // The main branch is renameable; deleting it is what stays blocked. An
    // empty name is valid and means "fall back to the localised label".
    if (!activeCompanionId.value) return
    const trimmed = name.trim()
    try {
      await renameBranch(activeCompanionId.value, id, trimmed)
      const b = branches.value.find((x) => x.id === id)
      if (b) b.name = trimmed
      if (activeBranch.value === id) branchTitle.value = trimmed
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  async function removeBranch(id: string) {
    if (!activeCompanionId.value || id === 'main') return
    try {
      await deleteBranch(activeCompanionId.value, id)
      const wasActive = activeBranch.value === id
      branches.value = branches.value.filter((b) => b.id !== id)
      if (wasActive && branches.value.length) selectBranch(branches.value[0].id)
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  return {
    companions,
    activeCompanionId,
    loadingCast,
    activeCast,
    error,
    act,
    branchTitle,
    branches,
    activeBranch,
    scenario,
    model,
    voice,
    modelBackend,
    voiceBackend,
    asrModel,
    asrBackend,
    endpoint,
    apiKey,
    inPlay,
    setInPlay,
    tokensUsed,
    tokensTotal,
    tokenPct,
    tokenPctText,
    sceneBreak,
    greetings,
    greetingIndex,
    cycleGreeting,
    emptyLine,
    userName,
    emptyHints,
    loadCompanions,
    companionAssets,
    assetNames,
    imageAssets,
    audioAssets,
    videoAssets,
    loadCompanionAssets,
    findAssetByRef,
    selectCompanion,
    setCompanionLocale,
    togglePin,
    loadBranches,
    loadTokens,
    cardPresentation,
    defaultVariables,
    branchVariables,
    branchHud,
    displayRegexRules,
    outputRegexRules,
    inputRegexRules,
    promptRegexRules,
    sceneRules,
    hudPinnedVars,
    hudThoughtEnabled,
    cardCss,
    loadBranchVariables,
    saveBranchVariables,
    resetBranchVariablesLocal,
    saveSceneRules,
    newBranch,
    renameBranchLocal,
    removeBranch,
    selectBranch,
  }
})

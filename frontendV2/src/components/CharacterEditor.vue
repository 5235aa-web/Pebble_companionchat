<script setup lang="ts">
import { computed, onUnmounted, reactive, ref, watch, type WritableComputedRef } from 'vue'
import { useUiStore } from '@/stores/ui'
import { useSessionStore } from '@/stores/session'
import { useLlmStore } from '@/stores/llm'
import { useCardAudioStore } from '@/stores/cardAudio'
import { useInochi2dStore } from '@/stores/inochi2d'
import { useI18n } from '@/i18n'
import type { DisplayRegexRule } from '@/utils/displayRegex'
import type { CharacterCard } from '@/data/character'
import { isCharxCard } from '@/data/chat'
import {
  hasLocaleTranslation,
  normalizeCompanionLocale,
  type CompanionLocale,
  type CompanionTranslationBucket,
  type CompanionTranslations,
} from '@/utils/companionTranslation'
import {
  createCompanion,
  deleteCompanionAsset,
  exportCompanion,
  getCompanion,
  importCompanionRisum,
  setAvatarFromAsset,
  translateCompanionCard,
  updateCompanion,
  uploadCompanionAsset,
  uploadCompanionAvatar,
  type Companion,
  type CompanionAsset,
} from '@/api/companion'
import { useFileUrl, useFileUrls } from '@/composables/useFileUrl'
import { getTtsVoices } from '@/api/tts'
import InlineVideoCard from './InlineVideoCard.vue'
import UiSelect from './ui/UiSelect.vue'

const ui = useUiStore()
const session = useSessionStore()
const llm = useLlmStore()
const cardAudio = useCardAudioStore()
const inochi = useInochi2dStore()
const { t } = useI18n()

const makeEmpty = (): CharacterCard => ({
  avatar: '',
  name: '',
  role: '',
  tags: [],
  description: '',
  personality: '',
  scenario: '',
  firstMes: '',
  alternateGreetings: [],
  mesExample: '',
  advancedPrompt: '',
  systemPrompt: '',
  postHistory: '',
  userInfo: '',
  creatorNotes: '',
  raw: '',
  ttsVoiceRef: '',
  ttsVoiceId: '',
})

const card = reactive<CharacterCard>(makeEmpty())

// Declared after `card`: the composable reads the source immediately, and a
// reference before the const is initialised throws a TDZ error at mount time.
const avatarUrl = useFileUrl(() => card.avatar, 1024)
const newTag = ref('')
const rawOpen = ref(false)
const saving = ref(false)
const error = ref('')

const editorLocale = ref<CompanionLocale>('')
const translationsDraft = reactive<CompanionTranslations>({})
const promptUseOriginal = ref(false)
const translatingLocale = ref<'zh' | 'en' | null>(null)
const translatingField = ref('')
const translateProgress = ref<{ done: number; total: number }>({ done: 0, total: 0 })
let translateAbort: AbortController | null = null

const isTranslationView = computed(() => editorLocale.value !== '')
const currentTranslationHasContent = computed(() =>
  editorLocale.value === ''
    ? true
    : hasLocaleTranslation(translationsDraft, editorLocale.value),
)

function cloneTranslations(raw: CompanionTranslations | undefined): CompanionTranslations {
  const out: CompanionTranslations = {}
  for (const loc of ['zh', 'en'] as const) {
    const b = raw?.[loc]
    if (!b || typeof b !== 'object') continue
    out[loc] = {
      ...(typeof b.description === 'string' ? { description: b.description } : {}),
      ...(typeof b.personality === 'string' ? { personality: b.personality } : {}),
      ...(typeof b.scenario === 'string' ? { scenario: b.scenario } : {}),
      ...(typeof b.first_mes === 'string' ? { first_mes: b.first_mes } : {}),
      ...(typeof b.creator_notes === 'string' ? { creator_notes: b.creator_notes } : {}),
      ...(Array.isArray(b.tags) ? { tags: [...b.tags] } : {}),
      ...(Array.isArray(b.alternate_greetings)
        ? { alternate_greetings: [...b.alternate_greetings] }
        : {}),
      ...(typeof b.translated_at === 'string' ? { translated_at: b.translated_at } : {}),
    }
  }
  return out
}

function resetTranslationsDraft(raw?: CompanionTranslations): void {
  delete translationsDraft.zh
  delete translationsDraft.en
  const cloned = cloneTranslations(raw)
  if (cloned.zh) translationsDraft.zh = cloned.zh
  if (cloned.en) translationsDraft.en = cloned.en
}

function ensureBucket(loc: 'zh' | 'en'): CompanionTranslationBucket {
  if (!translationsDraft[loc]) {
    translationsDraft[loc] = {
      description: '',
      personality: '',
      scenario: '',
      tags: [],
      first_mes: '',
      alternate_greetings: card.alternateGreetings.map(() => ''),
      creator_notes: '',
    }
  }
  return translationsDraft[loc]!
}

function bindStringField(
  origKey: 'description' | 'personality' | 'scenario' | 'firstMes' | 'creatorNotes',
  trKey: 'description' | 'personality' | 'scenario' | 'first_mes' | 'creator_notes',
): WritableComputedRef<string> {
  return computed({
    get() {
      const loc = editorLocale.value
      if (!loc) return card[origKey]
      return String(translationsDraft[loc]?.[trKey] ?? '')
    },
    set(val: string) {
      const loc = editorLocale.value
      if (!loc) {
        card[origKey] = val
        return
      }
      ensureBucket(loc)[trKey] = val
    },
  })
}

const activeDescription = bindStringField('description', 'description')
const activePersonality = bindStringField('personality', 'personality')
const activeScenario = bindStringField('scenario', 'scenario')
const activeFirstMes = bindStringField('firstMes', 'first_mes')
const activeCreatorNotes = bindStringField('creatorNotes', 'creator_notes')

const activeTags = computed(() => {
  const loc = editorLocale.value
  if (!loc) return card.tags
  return translationsDraft[loc]?.tags ?? []
})

function greetingText(index: number): string {
  const loc = editorLocale.value
  if (!loc) return card.alternateGreetings[index] ?? ''
  return translationsDraft[loc]?.alternate_greetings?.[index] ?? ''
}

function setGreetingText(index: number, val: string): void {
  const loc = editorLocale.value
  if (!loc) {
    card.alternateGreetings[index] = val
    return
  }
  const bucket = ensureBucket(loc)
  if (!Array.isArray(bucket.alternate_greetings)) {
    bucket.alternate_greetings = card.alternateGreetings.map(() => '')
  }
  while (bucket.alternate_greetings.length <= index) {
    bucket.alternate_greetings.push('')
  }
  bucket.alternate_greetings[index] = val
}

function buildCleanTranslationsPayload(): CompanionTranslations {
  const out: CompanionTranslations = {}
  for (const loc of ['zh', 'en'] as const) {
    const b = translationsDraft[loc]
    if (!b || !hasLocaleTranslation(translationsDraft, loc)) continue
    out[loc] = {
      ...(typeof b.description === 'string' && b.description.trim()
        ? { description: b.description }
        : {}),
      ...(typeof b.personality === 'string' && b.personality.trim()
        ? { personality: b.personality }
        : {}),
      ...(typeof b.scenario === 'string' && b.scenario.trim()
        ? { scenario: b.scenario }
        : {}),
      ...(typeof b.first_mes === 'string' && b.first_mes.trim()
        ? { first_mes: b.first_mes }
        : {}),
      ...(typeof b.creator_notes === 'string' && b.creator_notes.trim()
        ? { creator_notes: b.creator_notes }
        : {}),
      ...(Array.isArray(b.tags) && b.tags.some((t) => t.trim())
        ? { tags: b.tags.map((t) => t.trim()).filter(Boolean) }
        : {}),
      ...(Array.isArray(b.alternate_greetings) && b.alternate_greetings.some((g) => g.trim())
        ? { alternate_greetings: [...b.alternate_greetings] }
        : {}),
      ...(typeof b.translated_at === 'string' && b.translated_at ? { translated_at: b.translated_at } : {}),
    }
  }
  return out
}

const translateProgressText = computed(() => {
  if (!translatingLocale.value) return ''
  const { done, total } = translateProgress.value
  return total > 0
    ? t('card.translatingProgress', { done, total })
    : t('card.translating')
})

function cancelTranslation(): void {
  translateAbort?.abort()
  translateAbort = null
  translatingLocale.value = null
  translatingField.value = ''
}

async function runTranslate(target: 'zh' | 'en', fields?: string[]): Promise<void> {
  const id = session.activeCompanionId
  if (!id || ui.newCastDraft || translatingLocale.value) return
  error.value = ''
  translatingLocale.value = target
  translatingField.value = fields?.length === 1 ? fields[0] : ''
  translateProgress.value = { done: 0, total: fields?.length ?? 0 }
  translateAbort = new AbortController()
  try {
    const finalEv = await translateCompanionCard(
      id,
      { target_locale: target, fields, activate: true },
      {
        signal: translateAbort.signal,
        onEvent(ev) {
          if (ev.type === 'start' || ev.type === 'progress' || ev.type === 'field_done') {
            translateProgress.value = {
              done: ev.done ?? 0,
              total: ev.total ?? translateProgress.value.total,
            }
          } else if (ev.type === 'done' && ev.translations) {
            resetTranslationsDraft(ev.translations)
          }
        },
      },
    )
    if (finalEv?.translations) {
      resetTranslationsDraft(finalEv.translations)
    } else {
      const refreshed = await getCompanion(id)
      if (refreshed) resetTranslationsDraft(refreshed.translations)
    }
    editorLocale.value = target
    await session.loadCompanions()
  } catch (e) {
    if ((e as { name?: string })?.name !== 'AbortError') {
      error.value = e instanceof Error ? e.message : String(e)
    }
    try {
      const refreshed = await getCompanion(id)
      if (refreshed) resetTranslationsDraft(refreshed.translations)
    } catch {
      /* keep current draft on secondary fetch error */
    }
  } finally {
    translateAbort = null
    translatingLocale.value = null
    translatingField.value = ''
  }
}

onUnmounted(() => {
  translateAbort?.abort()
})

/**
 * Reference clips available to the cloning engines. Loaded once per open rather
 * than per character: the list is a property of the voice library, not of the
 * card being edited.
 */
const voiceClips = ref<string[]>([])

const collapsed = reactive({
  identity: false,
  voice: false,
  assets: true,
  assetsImages: true,
  assetsAudio: true,
  assetsVideos: true,
  tts: true,
  interactive: true,
  hood: true,
})

const varsText = ref('')
const regexRules = ref<DisplayRegexRule[]>([])
const cardCssText = ref('')
const risumInput = ref<HTMLInputElement | null>(null)
const importingRisum = ref(false)
let loadedCardUiSnapshot = ''
let loadedVarsSnapshot = ''

function formatVarsText(vars: Record<string, string>): string {
  return Object.entries(vars)
    .map(([k, v]) => `${k}=${v}`)
    .join('\n')
}

function parseVarsText(raw: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of raw.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith('#')) continue
    const eq = trimmed.indexOf('=')
    if (eq <= 0) continue
    const k = trimmed.slice(0, eq).trim()
    const v = trimmed.slice(eq + 1).trim()
    if (k) out[k] = v
  }
  return out
}

function addRegexRule() {
  regexRules.value.push({
    id: `custom_${Date.now().toString(36)}`,
    name: `Rule ${regexRules.value.length + 1}`,
    pattern: '',
    flags: 'g',
    replace: '',
    placement: [1, 2],
    enabled: true,
  })
}

function removeRegexRule(idx: number) {
  regexRules.value.splice(idx, 1)
}

async function resetVars() {
  if (ui.newCastDraft || !session.activeCompanionId) return
  await session.resetBranchVariablesLocal()
  varsText.value = formatVarsText(session.branchVariables)
  loadedVarsSnapshot = varsText.value
  void session.loadTokens(llm.tokenQuery)
}

function pickRisumFile() {
  risumInput.value?.click()
}

async function onRisumPicked(ev: Event) {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  const id = session.activeCompanionId
  if (!file || !id || ui.newCastDraft) return
  importingRisum.value = true
  error.value = ''
  try {
    await importCompanionRisum(id, file)
    await session.selectCompanion(id)
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    importingRisum.value = false
  }
}

const IMAGE_PAGE = 36
const imageLimit = ref(IMAGE_PAGE)
const assetFilter = ref('')

const filteredImages = computed(() => {
  const q = assetFilter.value.trim().toLowerCase()
  if (!q) return session.imageAssets
  return session.imageAssets.filter(
    (a) =>
      a.name.toLowerCase().includes(q) ||
      a.asset_type.toLowerCase().includes(q),
  )
})

const visibleImages = computed(() => filteredImages.value.slice(0, imageLimit.value))
const remainingImages = computed(() =>
  Math.max(0, filteredImages.value.length - visibleImages.value.length),
)

// Only load thumbnails for currently visible items while the panel AND the
// Images sub-section are actually expanded (prevents loading 2000+ images at rest).
const assetThumbMap = useFileUrls(() => {
  if (!ui.isOpen('profile') || collapsed.assets || collapsed.assetsImages) return []
  return visibleImages.value.map((a) => a.rel_path).filter(Boolean)
}, 256)

const assetCountLabel = computed(() => {
  const n = session.companionAssets.length
  return n > 0 ? `${t('card.assets')} (${n})` : t('card.assets')
})
const assetInput = ref<HTMLInputElement | null>(null)
const uploadingAsset = ref(false)
const cardFormat = ref('')
const isCharx = computed(() => {
  if (ui.newCastDraft) return false
  const active = session.companions.find((c) => c.id === session.activeCompanionId)
  return isCharxCard({
    cardFormat: cardFormat.value || active?.cardFormat,
    assetCount: active?.assetCount,
  })
})

const fromCompanion = (c: Companion): CharacterCard => ({
  avatar: c.avatar_path || '',
  name: c.name || '',
  role: c.creator || '',
  tags: [...(c.tags ?? [])],
  description: c.description || '',
  personality: c.personality || '',
  scenario: c.scenario || '',
  firstMes: c.first_mes || '',
  alternateGreetings: [...(c.alternate_greetings ?? [])],
  mesExample: c.mes_example || '',
  advancedPrompt: c.advanced_prompt || '',
  systemPrompt: c.system_prompt || '',
  postHistory: c.post_history_instructions || '',
  userInfo: c.user_info || '',
  creatorNotes: c.creator_notes || '',
  raw: c.raw_card_data || '',
  ttsVoiceRef: typeof c.tts_config?.voiceRef === 'string' ? c.tts_config.voiceRef : '',
  ttsVoiceId: typeof c.tts_config?.voiceId === 'string' ? c.tts_config.voiceId : '',
})

async function load() {
  void loadVoiceClips()
  // A draft is a card that does not exist yet: blank it out instead of loading
  // whichever companion happens to be selected behind the panel.
  if (ui.newCastDraft) {
    cardFormat.value = ''
    Object.assign(card, makeEmpty())
    editorLocale.value = ''
    resetTranslationsDraft(undefined)
    promptUseOriginal.value = false
    varsText.value = ''
    regexRules.value = []
    cardCssText.value = ''
    loadedCardUiSnapshot = ''
    loadedVarsSnapshot = ''
    error.value = ''
    return
  }
  const id = session.activeCompanionId
  if (!id) {
    cardFormat.value = ''
    Object.assign(card, makeEmpty())
    return
  }
  cardFormat.value = ''
  imageLimit.value = IMAGE_PAGE
  assetFilter.value = ''
  try {
    const [c] = await Promise.all([
      getCompanion(id),
      session.loadCompanionAssets(id),
      session.loadBranchVariables(id, session.activeBranch || 'main'),
    ])
    cardFormat.value = c?.card_format || ''
    Object.assign(card, c ? fromCompanion(c) : makeEmpty())
    editorLocale.value = normalizeCompanionLocale(c?.active_locale)
    resetTranslationsDraft(c?.translations)
    promptUseOriginal.value = Boolean(c?.prompt_use_original)
    const pres = c?.card_presentation
    regexRules.value = (pres?.display_regex ?? []).map((r) => ({ ...r }))
    cardCssText.value = pres?.card_css ?? ''
    varsText.value = formatVarsText(session.branchVariables)
    loadedCardUiSnapshot = JSON.stringify({
      display_regex: regexRules.value,
      card_css: cardCssText.value,
    })
    loadedVarsSnapshot = varsText.value
    // Keep image/audio sub-groups collapsed by default so large 2000+ asset
    // cards never lag or bury the rest of the editor.
    collapsed.assetsImages = true
    collapsed.assetsVideos = true
    collapsed.assetsAudio = session.audioAssets.length === 0
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

/** Failure here is not worth surfacing: the field simply stays a text input. */
async function loadVoiceClips() {
  try {
    voiceClips.value = await getTtsVoices()
  } catch {
    voiceClips.value = []
  }
}

const voiceClipOptions = computed(() => {
  const seen = new Set<string>()
  const opts = [{ value: '', label: t('card.voiceInherit') }]
  for (const v of voiceClips.value) {
    if (!v || seen.has(v)) continue
    seen.add(v)
    opts.push({ value: v, label: v })
  }
  const cur = (card.ttsVoiceRef || '').trim()
  if (cur && !seen.has(cur)) {
    opts.push({ value: cur, label: cur })
  }
  return opts
})

watch(
  () => [session.activeCompanionId, ui.isOpen('profile')],
  () => {
    if (ui.isOpen('profile')) {
      void load()
    } else if (ui.newCastDraft) {
      // The panel closed without saving: drop the draft so the next open starts
      // from the selected companion again.
      ui.clearNewCast()
    }
  },
  { immediate: true },
)

async function save() {
  const draft = ui.newCastDraft
  const id = draft ? 'card-' + Date.now().toString(36) : session.activeCompanionId
  if (!id) return
  if (draft && !card.name.trim()) {
    error.value = t('card.nameRequired')
    return
  }
  saving.value = true
  error.value = ''
  try {
    const cleanRules = regexRules.value.filter((r) => r.pattern.trim().length > 0)
    const prevPres = session.cardPresentation
    const currentCardUiSnapshot = JSON.stringify({
      display_regex: cleanRules,
      card_css: cardCssText.value,
    })
    const cleanTranslations = buildCleanTranslationsPayload()
    const nextLocale: CompanionLocale =
      editorLocale.value && hasLocaleTranslation(cleanTranslations, editorLocale.value)
        ? editorLocale.value
        : ''
    const fields: Record<string, unknown> = {
      name: card.name,
      description: card.description,
      personality: card.personality,
      scenario: card.scenario,
      first_mes: card.firstMes,
      alternate_greetings: card.alternateGreetings.filter((s) => s.trim().length > 0),
      mes_example: card.mesExample,
      advanced_prompt: card.advancedPrompt,
      system_prompt: card.systemPrompt,
      post_history_instructions: card.postHistory,
      user_info: card.userInfo,
      creator_notes: card.creatorNotes,
      tags: card.tags,
      creator: card.role,
      // Voice identity lives on the companion row so it travels with the card
      // and survives a restart; the engine itself stays in the TTS panel.
      tts_config: {
        voiceRef: card.ttsVoiceRef.trim(),
        voiceId: card.ttsVoiceId.trim(),
      },
      ...(!draft
        ? {
            translations: cleanTranslations,
            active_locale: nextLocale,
            prompt_use_original: promptUseOriginal.value,
          }
        : {}),
    }
    if (currentCardUiSnapshot !== loadedCardUiSnapshot) {
      fields.card_ui = {
        display_regex: cleanRules,
        card_css: cardCssText.value,
        ...(prevPres?.scene_rules ? { scene_rules: prevPres.scene_rules } : {}),
        ...(prevPres?.output_regex ? { output_regex: prevPres.output_regex } : {}),
        ...(prevPres?.input_regex ? { input_regex: prevPres.input_regex } : {}),
        ...(prevPres?.prompt_regex ? { prompt_regex: prevPres.prompt_regex } : {}),
      }
    }
    // A draft only reaches the database here, so backing out of the editor
    // leaves the cast list untouched.
    if (draft) await createCompanion(id, fields)
    else await updateCompanion(id, fields)
    if (!draft && varsText.value !== loadedVarsSnapshot) {
      await session.saveBranchVariables(parseVarsText(varsText.value))
      void session.loadTokens(llm.tokenQuery)
    }
    ui.clearNewCast()
    await session.loadCompanions()
    if (draft) await session.selectCompanion(id)
    ui.closePanel('profile')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

function download(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

const fileInput = ref<HTMLInputElement | null>(null)

function pickAvatar() {
  fileInput.value?.click()
}

async function onAvatarPicked(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  const id = session.activeCompanionId
  if (!file || !id) return
  try {
    const r = await uploadCompanionAvatar(id, file)
    card.avatar = r.avatar_path
    await session.loadCompanions()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

function pickAssetFiles() {
  assetInput.value?.click()
}

function goLive2dBuilder() {
  inochi.setMode('live2d')
  inochi.openEditor('build')
  ui.closePanel('profile')
}

async function onAssetFilesPicked(e: Event) {
  const input = e.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  const id = session.activeCompanionId
  if (!files.length || !id) return
  uploadingAsset.value = true
  error.value = ''
  try {
    for (const file of files) {
      const kind = file.type.startsWith('audio/')
        ? 'audio'
        : file.type.startsWith('video/')
          ? 'video'
          : file.type.startsWith('image/')
            ? 'image'
            : 'other'
      await uploadCompanionAsset(id, file, kind)
    }
    await Promise.all([session.loadCompanionAssets(id), session.loadCompanions()])
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    uploadingAsset.value = false
  }
}

async function makeAvatar(asset: CompanionAsset) {
  const id = session.activeCompanionId
  if (!id) return
  try {
    const r = await setAvatarFromAsset(id, asset.id)
    card.avatar = r.avatar_path
    await session.loadCompanions()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function removeAsset(asset: CompanionAsset) {
  const id = session.activeCompanionId
  if (!id) return
  try {
    if (cardAudio.bgmAssetId === asset.id) cardAudio.stopBgm()
    if (cardAudio.sfxAssetId === asset.id) cardAudio.stopSfx()
    await deleteCompanionAsset(id, asset.id)
    await Promise.all([session.loadCompanionAssets(id), session.loadCompanions()])
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function exportPng() {
  const id = session.activeCompanionId
  if (!id) return
  try {
    download(await exportCompanion(id, 'png'), (card.name || id) + '.png')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function exportJson() {
  const id = session.activeCompanionId
  if (!id) return
  try {
    download(await exportCompanion(id, 'json'), (card.name || id) + '.json')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

function removeTag(tag: string) {
  const loc = editorLocale.value
  if (!loc) {
    card.tags = card.tags.filter((x) => x !== tag)
    return
  }
  const bucket = ensureBucket(loc)
  bucket.tags = (bucket.tags ?? []).filter((x) => x !== tag)
}

function addTag() {
  const v = newTag.value.trim()
  if (!v) return
  const loc = editorLocale.value
  if (!loc) {
    card.tags.push(v)
  } else {
    const bucket = ensureBucket(loc)
    if (!Array.isArray(bucket.tags)) bucket.tags = []
    bucket.tags.push(v)
  }
  newTag.value = ''
}

function addGreeting() {
  card.alternateGreetings.push('')
}

function removeGreeting(index: number) {
  card.alternateGreetings.splice(index, 1)
}
</script>

<template>
  <div class="profile" :class="{ show: ui.isOpen('profile') }">
    <div class="pf">
      <div class="pf-head">
        <div class="pf-avatar-wrap">
          <img v-if="card.avatar" class="pf-avatar-big" :src="avatarUrl" :alt="card.name" />
          <div v-else class="pf-avatar-big">{{ (card.name || '?').charAt(0) }}</div>
          <input ref="fileInput" type="file" accept="image/*" hidden @change="onAvatarPicked" />
          <!-- Uploading needs a stored companion, so a draft offers it after save. -->
          <button v-if="!ui.newCastDraft" class="pf-avatar-btn" @click="pickAvatar">
            {{ t('card.change') }}
          </button>
        </div>
        <div class="pf-id">
          <div class="pf-name-row">
            <input
              v-model="card.name"
              class="pf-name"
              :class="{ readonly: isTranslationView }"
              :readonly="isTranslationView"
              aria-label="character name"
            />
            <span v-if="isTranslationView" class="pf-tr-badge">{{ t('card.translationNameBadge') }}</span>
          </div>
          <div class="pf-role">{{ card.role }}</div>
        </div>
        <div v-if="!ui.newCastDraft && !isCharx" class="pf-exports">
          <button class="mini-btn" @click="exportPng">
            {{ t('card.exportPng') }}
          </button>
          <button class="mini-btn" @click="exportJson">
            {{ t('card.exportJson') }}
          </button>
        </div>
        <button class="d-close" @click="ui.closePanel('profile')">
          <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>

      <div v-if="!ui.newCastDraft" class="pf-trans-bar">
        <div class="pf-trans-left">
          <div class="pf-lang-seg" role="group" :aria-label="t('card.localeSwitchTip')">
            <button
              type="button"
              class="pf-lang-btn"
              :class="{ on: editorLocale === '' }"
              @click="editorLocale = ''"
            >
              {{ t('card.langOriginal') }}
            </button>
            <button
              type="button"
              class="pf-lang-btn"
              :class="{ on: editorLocale === 'zh', has: hasLocaleTranslation(translationsDraft, 'zh') }"
              @click="editorLocale = 'zh'"
            >
              {{ t('card.langZh') }}
            </button>
            <button
              type="button"
              class="pf-lang-btn"
              :class="{ on: editorLocale === 'en', has: hasLocaleTranslation(translationsDraft, 'en') }"
              @click="editorLocale = 'en'"
            >
              {{ t('card.langEn') }}
            </button>
          </div>

          <label
            class="pf-prompt-orig"
            :data-tip="t('card.promptUseOriginalTip')"
            data-tip-float
          >
            <input v-model="promptUseOriginal" type="checkbox" />
            <span>{{ t('card.promptUseOriginal') }}</span>
          </label>
        </div>

        <div class="pf-trans-actions">
          <template v-if="translatingLocale">
            <span class="pf-trans-prog">{{ translateProgressText }}</span>
            <button type="button" class="mini-btn" @click="cancelTranslation">
              {{ t('card.cancel') }}
            </button>
          </template>
          <template v-else>
            <button type="button" class="mini-btn" @click="runTranslate('zh')">
              {{ t('card.translateZh') }}
            </button>
            <button type="button" class="mini-btn" @click="runTranslate('en')">
              {{ t('card.translateEn') }}
            </button>
          </template>
        </div>
      </div>

      <div v-if="error" class="pf-err">{{ error }}</div>
      <div v-else-if="isTranslationView && !currentTranslationHasContent" class="pf-trans-note">
        {{
          t('card.translationUntranslated', {
            lang: editorLocale === 'zh' ? t('card.langZh') : t('card.langEn'),
          })
        }}
      </div>
      <div v-else-if="isTranslationView" class="pf-trans-note soft">
        {{ t('card.translationOriginalLocked') }}
      </div>

      <div class="pf-body">
        <!-- Identity -->
        <div class="pf-group" :class="{ collapsed: collapsed.identity }">
          <div class="pf-glabel" data-group @click="collapsed.identity = !collapsed.identity">
            <svg class="caret" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
            <span>{{ t('card.identity') }}</span>
          </div>
          <div class="pf-fields">
            <div class="pf-field">
              <div class="pf-field-head">
                <label>{{ t('card.description') }}</label>
                <button
                  v-if="isTranslationView && card.description.trim()"
                  type="button"
                  class="pf-raw-toggle"
                  :disabled="Boolean(translatingLocale)"
                  @click="runTranslate(editorLocale as 'zh' | 'en', ['description'])"
                >
                  {{ translatingField === 'description' ? t('card.translating') : t('card.retranslateField') }}
                </button>
              </div>
              <textarea
                v-model="activeDescription"
                class="pf-area"
                rows="3"
                :placeholder="isTranslationView ? t('card.translationFallbackHint') : undefined"
              ></textarea>
            </div>
            <div class="pf-field">
              <div class="pf-field-head">
                <label>{{ t('card.personality') }}</label>
                <button
                  v-if="isTranslationView && card.personality.trim()"
                  type="button"
                  class="pf-raw-toggle"
                  :disabled="Boolean(translatingLocale)"
                  @click="runTranslate(editorLocale as 'zh' | 'en', ['personality'])"
                >
                  {{ translatingField === 'personality' ? t('card.translating') : t('card.retranslateField') }}
                </button>
              </div>
              <textarea
                v-model="activePersonality"
                class="pf-area"
                rows="2"
                :placeholder="isTranslationView ? t('card.translationFallbackHint') : undefined"
              ></textarea>
            </div>
            <div class="pf-field">
              <div class="pf-field-head">
                <label>{{ t('card.scenario') }}</label>
                <button
                  v-if="isTranslationView && card.scenario.trim()"
                  type="button"
                  class="pf-raw-toggle"
                  :disabled="Boolean(translatingLocale)"
                  @click="runTranslate(editorLocale as 'zh' | 'en', ['scenario'])"
                >
                  {{ translatingField === 'scenario' ? t('card.translating') : t('card.retranslateField') }}
                </button>
              </div>
              <textarea
                v-model="activeScenario"
                class="pf-area"
                rows="2"
                :placeholder="isTranslationView ? t('card.translationFallbackHint') : undefined"
              ></textarea>
            </div>
            <div class="pf-field">
              <div class="pf-field-head">
                <label>{{ t('card.tags') }}</label>
                <button
                  v-if="isTranslationView && card.tags.length"
                  type="button"
                  class="pf-raw-toggle"
                  :disabled="Boolean(translatingLocale)"
                  @click="runTranslate(editorLocale as 'zh' | 'en', ['tags'])"
                >
                  {{ translatingField === 'tags' ? t('card.translating') : t('card.retranslateField') }}
                </button>
              </div>
              <div class="pf-tags">
                <span v-for="tag in activeTags" :key="tag" class="pf-tag">
                  {{ tag }}
                  <button aria-label="remove tag" @click="removeTag(tag)">
                    <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
                  </button>
                </span>
                <input
                  v-model="newTag"
                  class="pf-tag-input"
                  :placeholder="t('card.addTag')"
                  aria-label="add tag"
                  @keydown.enter="addTag"
                />
              </div>
            </div>
          </div>
        </div>

        <!-- Voice -->
        <div class="pf-group" :class="{ collapsed: collapsed.voice }">
          <div class="pf-glabel" data-group @click="collapsed.voice = !collapsed.voice">
            <svg class="caret" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
            <span>{{ t('card.voice') }}</span>
          </div>
          <div class="pf-fields">
            <div class="pf-field">
              <div class="pf-field-head">
                <label>{{ t('card.firstMessage') }}</label>
                <button
                  v-if="isTranslationView && card.firstMes.trim()"
                  type="button"
                  class="pf-raw-toggle"
                  :disabled="Boolean(translatingLocale)"
                  @click="runTranslate(editorLocale as 'zh' | 'en', ['first_mes'])"
                >
                  {{ translatingField === 'first_mes' ? t('card.translating') : t('card.retranslateField') }}
                </button>
              </div>
              <textarea
                v-model="activeFirstMes"
                class="pf-area"
                rows="3"
                :placeholder="isTranslationView ? t('card.translationFallbackHint') : undefined"
              ></textarea>
            </div>
            <div class="pf-field">
              <div class="pf-field-head">
                <label>{{ t('card.alternateGreetings') }}</label>
                <button v-if="!isTranslationView" type="button" class="pf-raw-toggle" @click="addGreeting">
                  + {{ t('card.addGreeting') }}
                </button>
              </div>
              <div v-if="card.alternateGreetings.length" class="pf-greet-list">
                <div
                  v-for="(origAlt, i) in card.alternateGreetings"
                  :key="i"
                  class="pf-greet-item"
                >
                  <div class="pf-greet-meta">
                    <span>#{{ i + 1 }}</span>
                    <button
                      v-if="isTranslationView && origAlt.trim()"
                      type="button"
                      class="pf-raw-toggle"
                      :disabled="Boolean(translatingLocale)"
                      @click="runTranslate(editorLocale as 'zh' | 'en', [`alternate_greetings:${i}`])"
                    >
                      {{
                        translatingField === `alternate_greetings:${i}`
                          ? t('card.translating')
                          : t('card.retranslateField')
                      }}
                    </button>
                    <button
                      v-if="!isTranslationView"
                      type="button"
                      class="pf-asset-btn danger"
                      aria-label="remove greeting"
                      @click="removeGreeting(i)"
                    >
                      ×
                    </button>
                  </div>
                  <textarea
                    :value="greetingText(i)"
                    class="pf-area"
                    rows="3"
                    :placeholder="isTranslationView ? t('card.translationFallbackHint') : undefined"
                    @input="setGreetingText(i, ($event.target as HTMLTextAreaElement).value)"
                  ></textarea>
                </div>
              </div>
            </div>
            <div class="pf-field">
              <div class="pf-field-head">
                <label>{{ t('card.conversationExamples') }}</label>
                <span v-if="isTranslationView" class="pf-tr-badge">{{ t('card.translationOriginalBadge') }}</span>
              </div>
              <textarea
                v-model="card.mesExample"
                class="pf-area"
                :class="{ readonly: isTranslationView }"
                :readonly="isTranslationView"
                rows="4"
              ></textarea>
            </div>
            <div class="pf-field">
              <div class="pf-field-head">
                <label>{{ t('card.advancedPrompt') }}</label>
                <span v-if="isTranslationView" class="pf-tr-badge">{{ t('card.translationOriginalBadge') }}</span>
              </div>
              <textarea
                v-model="card.advancedPrompt"
                class="pf-area"
                :class="{ readonly: isTranslationView }"
                :readonly="isTranslationView"
                rows="3"
              ></textarea>
            </div>
          </div>
        </div>

        <!-- Media assets (CHARX images, expressions, music, SFX) -->
        <div v-if="!ui.newCastDraft" class="pf-group" :class="{ collapsed: collapsed.assets }">
          <div class="pf-glabel" data-group @click="collapsed.assets = !collapsed.assets">
            <svg class="caret" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
            <span>{{ assetCountLabel }}</span>
          </div>
          <div class="pf-fields">
            <div class="pf-asset-bar">
              <button class="mini-btn" type="button" :disabled="uploadingAsset" @click="pickAssetFiles">
                {{ t('card.assetsAdd') }}
              </button>
              <button class="mini-btn" type="button" @click="goLive2dBuilder">
                {{ t('card.assetsToLive2d') }}
              </button>
              <input
                ref="assetInput"
                type="file"
                accept="image/*,audio/*,video/*"
                multiple
                hidden
                @change="onAssetFilesPicked"
              />
            </div>
            <p class="cp-hint" style="margin: 0 0 8px">{{ t('card.assetsLive2dHint') }}</p>

            <div v-if="!session.companionAssets.length" class="pf-asset-empty">
              {{ t('card.assetsEmpty') }}
            </div>

            <div v-if="session.imageAssets.length" class="pf-sub-group">
              <button
                type="button"
                class="pf-sub-toggle"
                :class="{ collapsed: collapsed.assetsImages }"
                @click="collapsed.assetsImages = !collapsed.assetsImages"
              >
                <svg class="caret" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
                <span>{{ t('card.assetsImages') }} ({{ session.imageAssets.length }})</span>
              </button>
              <div v-if="!collapsed.assetsImages" class="pf-sub-body">
                <input
                  v-model="assetFilter"
                  class="pf-input pf-asset-filter"
                  :placeholder="t('card.assetsFilterPlaceholder')"
                  @input="imageLimit = IMAGE_PAGE"
                />
                <div class="pf-asset-grid">
                  <div v-for="a in visibleImages" :key="a.id" class="pf-asset-card">
                    <div class="pf-asset-thumb">
                      <img
                        v-if="assetThumbMap[a.rel_path]"
                        :src="assetThumbMap[a.rel_path]"
                        :alt="a.name"
                        loading="lazy"
                      />
                    </div>
                    <div class="pf-asset-meta">
                      <span class="pf-asset-name" :data-tip="a.name || undefined" data-tip-float>{{ a.name }}</span>
                      <span class="pf-asset-type">{{ a.asset_type }}</span>
                    </div>
                    <div class="pf-asset-actions">
                      <button
                        type="button"
                        class="pf-asset-btn"
                        :disabled="card.avatar === a.rel_path"
                        @click="makeAvatar(a)"
                      >
                        {{ t('card.assetSetAvatar') }}
                      </button>
                      <button
                        type="button"
                        class="pf-asset-btn danger"
                        aria-label="delete asset"
                        @click="removeAsset(a)"
                      >
                        ×
                      </button>
                    </div>
                  </div>
                </div>
                <div v-if="remainingImages > 0" class="pf-asset-more">
                  <button
                    type="button"
                    class="mini-btn"
                    @click="imageLimit += IMAGE_PAGE"
                  >
                    {{ t('card.assetsShowMore', { left: remainingImages }) }}
                  </button>
                </div>
              </div>
            </div>

            <div v-if="session.audioAssets.length" class="pf-sub-group">
              <button
                type="button"
                class="pf-sub-toggle"
                :class="{ collapsed: collapsed.assetsAudio }"
                @click="collapsed.assetsAudio = !collapsed.assetsAudio"
              >
                <svg class="caret" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
                <span>{{ t('card.assetsAudio') }} ({{ session.audioAssets.length }})</span>
              </button>
              <div v-if="!collapsed.assetsAudio" class="pf-sub-body">
                <div class="pf-audio-list">
                  <div v-for="a in session.audioAssets" :key="a.id" class="pf-audio-row">
                    <span class="pf-audio-name" :data-tip="a.name || undefined" data-tip-float>{{ a.name }}</span>
                    <span class="pf-asset-type">{{ a.asset_type }}</span>
                    <button
                      type="button"
                      class="pf-asset-btn"
                      :class="{ active: cardAudio.bgmAssetId === a.id && cardAudio.bgmPlaying }"
                      @click="cardAudio.toggleBgm(a)"
                    >
                      {{
                        cardAudio.bgmAssetId === a.id && cardAudio.bgmPlaying
                          ? t('card.assetStop')
                          : t('card.assetPlayBgm')
                      }}
                    </button>
                    <button
                      type="button"
                      class="pf-asset-btn"
                      :class="{ active: cardAudio.sfxAssetId === a.id && cardAudio.sfxPlaying }"
                      @click="cardAudio.playSfx(a)"
                    >
                      {{
                        cardAudio.sfxAssetId === a.id && cardAudio.sfxPlaying
                          ? t('card.assetStop')
                          : t('card.assetPlaySfx')
                      }}
                    </button>
                    <button
                      type="button"
                      class="pf-asset-btn danger"
                      aria-label="delete audio asset"
                      @click="removeAsset(a)"
                    >
                      ×
                    </button>
                  </div>
                </div>
              </div>
            </div>

            <div v-if="session.videoAssets.length" class="pf-sub-group">
              <button
                type="button"
                class="pf-sub-toggle"
                :class="{ collapsed: collapsed.assetsVideos }"
                @click="collapsed.assetsVideos = !collapsed.assetsVideos"
              >
                <svg class="caret" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
                <span>{{ t('card.assetsVideos') }} ({{ session.videoAssets.length }})</span>
              </button>
              <div
                v-if="ui.isOpen('profile') && !collapsed.assets && !collapsed.assetsVideos"
                class="pf-sub-body"
              >
                <div class="pf-video-list">
                  <div v-for="a in session.videoAssets" :key="a.id" class="pf-video-item">
                    <InlineVideoCard :asset="a" compact />
                    <div class="pf-video-foot">
                      <span class="pf-asset-type">{{ a.asset_type }}</span>
                      <button
                        type="button"
                        class="pf-asset-btn danger"
                        aria-label="delete video asset"
                        @click="removeAsset(a)"
                      >
                        ×
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- TTS voice -->
        <div class="pf-group" :class="{ collapsed: collapsed.tts }">
          <div class="pf-glabel" data-group @click="collapsed.tts = !collapsed.tts">
            <svg class="caret" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
            <span>{{ t('card.ttsVoice') }}</span>
          </div>
          <div class="pf-fields">
            <div class="pf-field">
              <label>{{ t('card.voiceClip') }}</label>
              <UiSelect
                :model-value="card.ttsVoiceRef"
                :options="voiceClipOptions"
                @update:model-value="card.ttsVoiceRef = $event"
              />
            </div>
            <div class="pf-field">
              <label>{{ t('card.voiceId') }}</label>
              <input v-model="card.ttsVoiceId" class="pf-input" :placeholder="t('card.voiceIdHint')" />
            </div>
          </div>
        </div>

        <!-- Interactive Rules: CBS variables, Display Regex, Scoped CSS -->
        <div class="pf-group" :class="{ collapsed: collapsed.interactive }">
          <div class="pf-glabel" data-group @click="collapsed.interactive = !collapsed.interactive">
            <svg class="caret" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
            <span>{{ t('card.interactiveRules') }}</span>
          </div>
          <div class="pf-fields">
            <div v-if="!ui.newCastDraft" class="pf-asset-bar" style="margin-bottom: 10px;">
              <button
                type="button"
                class="mini-btn"
                :disabled="importingRisum"
                @click="pickRisumFile"
              >
                {{ t('card.importRisum') }}
              </button>
              <span class="pf-asset-empty" style="margin: 0;">{{ t('card.importRisumNote') }}</span>
              <input
                ref="risumInput"
                type="file"
                accept=".risum,.json"
                hidden
                @change="onRisumPicked"
              />
            </div>
            <div v-if="!ui.newCastDraft" class="pf-field">
              <div class="pf-greet-head">
                <label>{{ t('card.cbsVariables') }}</label>
                <button type="button" class="mini-btn" @click="resetVars">{{ t('card.resetVars') }}</button>
              </div>
              <textarea
                v-model="varsText"
                class="pf-area"
                rows="3"
                spellcheck="false"
                :placeholder="t('card.cbsVariablesHint')"
              ></textarea>
            </div>
            <div class="pf-field">
              <div class="pf-greet-head">
                <label>{{ t('card.displayRegex') }}</label>
                <button type="button" class="mini-btn" @click="addRegexRule">{{ t('card.addRegex') }}</button>
              </div>
              <div v-if="!regexRules.length" class="pf-asset-empty">{{ t('card.regexEmpty') }}</div>
              <div
                v-for="(rule, ri) in regexRules"
                :key="rule.id || ri"
                class="pf-greet-row"
                style="flex-direction: column; align-items: stretch; gap: 6px; padding: 8px; border: 1px solid var(--line); border-radius: 6px; margin-bottom: 8px;"
              >
                <div style="display: flex; gap: 6px; align-items: center;">
                  <input v-model="rule.enabled" type="checkbox" :aria-label="rule.name || 'enable rule'" />
                  <input v-model="rule.name" class="pf-input" :placeholder="t('card.regexName')" style="flex: 1;" />
                  <input v-model="rule.flags" class="pf-input" :placeholder="t('card.regexFlags')" style="width: 68px;" />
                  <button
                    type="button"
                    class="pf-greet-del"
                    aria-label="remove regex rule"
                    @click="removeRegexRule(ri)"
                  >
                    ×
                  </button>
                </div>
                <input v-model="rule.pattern" class="pf-input" spellcheck="false" :placeholder="t('card.regexPattern')" />
                <textarea
                  v-model="rule.replace"
                  class="pf-area"
                  rows="2"
                  spellcheck="false"
                  :placeholder="t('card.regexReplace')"
                ></textarea>
              </div>
            </div>
            <div class="pf-field">
              <label>{{ t('card.cardCss') }}</label>
              <textarea
                v-model="cardCssText"
                class="pf-area"
                rows="4"
                spellcheck="false"
                :placeholder="t('card.cardCssHint')"
              ></textarea>
            </div>
          </div>
        </div>

        <!-- Under the hood -->
        <div class="pf-group" :class="{ collapsed: collapsed.hood }">
          <div class="pf-glabel" data-group @click="collapsed.hood = !collapsed.hood">
            <svg class="caret" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
            <span>{{ t('card.underTheHood') }}</span>
          </div>
          <div class="pf-fields">
            <div class="pf-field">
              <div class="pf-field-head">
                <label>{{ t('card.systemInstructions') }}</label>
                <span v-if="isTranslationView" class="pf-tr-badge">{{ t('card.translationOriginalBadge') }}</span>
              </div>
              <textarea
                v-model="card.systemPrompt"
                class="pf-area"
                :class="{ readonly: isTranslationView }"
                :readonly="isTranslationView"
                rows="3"
              ></textarea>
            </div>
            <div class="pf-field">
              <div class="pf-field-head">
                <label>{{ t('card.postHistory') }}</label>
                <span v-if="isTranslationView" class="pf-tr-badge">{{ t('card.translationOriginalBadge') }}</span>
              </div>
              <textarea
                v-model="card.postHistory"
                class="pf-area"
                :class="{ readonly: isTranslationView }"
                :readonly="isTranslationView"
                rows="2"
              ></textarea>
            </div>
            <div class="pf-field">
              <div class="pf-field-head">
                <label>{{ t('card.userInfo') }}</label>
                <span v-if="isTranslationView" class="pf-tr-badge">{{ t('card.translationOriginalBadge') }}</span>
              </div>
              <textarea
                v-model="card.userInfo"
                class="pf-area"
                :class="{ readonly: isTranslationView }"
                :readonly="isTranslationView"
                rows="2"
              ></textarea>
            </div>
            <div class="pf-field">
              <div class="pf-field-head">
                <label>{{ t('card.creatorNotes') }}</label>
                <button
                  v-if="isTranslationView && card.creatorNotes.trim()"
                  type="button"
                  class="pf-raw-toggle"
                  :disabled="Boolean(translatingLocale)"
                  @click="runTranslate(editorLocale as 'zh' | 'en', ['creator_notes'])"
                >
                  {{ translatingField === 'creator_notes' ? t('card.translating') : t('card.retranslateField') }}
                </button>
              </div>
              <textarea
                v-model="activeCreatorNotes"
                class="pf-area"
                rows="2"
                :placeholder="isTranslationView ? t('card.translationFallbackHint') : undefined"
              ></textarea>
            </div>
            <div class="pf-field">
              <button class="pf-raw-toggle" @click="rawOpen = !rawOpen">{{ t('card.rawJson') }}</button>
              <div class="pf-raw" :class="{ show: rawOpen }">
                <textarea v-model="card.raw" spellcheck="false" aria-label="raw card json"></textarea>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="pf-foot">
        <button class="mini-btn danger" @click="ui.openPanel('dialog')">{{ t('card.delete') }}</button>
        <div class="pf-right">
          <button class="mini-btn" @click="ui.closePanel('profile')">{{ t('card.cancel') }}</button>
          <button class="mini-btn primary" :disabled="saving" @click="save">{{ t('card.save') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

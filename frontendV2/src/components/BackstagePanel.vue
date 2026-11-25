<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useUiStore } from '@/stores/ui'
import UiSlider from './ui/UiSlider.vue'
import UiLever from './ui/UiLever.vue'
import UiSelect from './ui/UiSelect.vue'
import { useTtsStore } from '@/stores/tts'
import {
  TTS_ENGINES,
  MULTI_INSTANCE_TTS_ENGINES,
  getTtsVoices,
  shouldStream,
  streamSampleRate,
  supportsStreaming,
  synthesize,
  synthesizeStream,
  type AudioSpecOption,
  type TtsConfig,
} from '@/api/tts'
import { PcmPlayer } from '@/lib/pcmPlayer'
import { useSessionStore } from '@/stores/session'
import { useSttStore } from '@/stores/stt'
import { useLlmStore } from '@/stores/llm'
import { usePictureStore } from '@/stores/pictures'
import type { PictureConfig, PictureParamMeta } from '@/api/pictures'
import {
  deleteComfyWorkflow,
  getComfyStatus,
  importComfyWorkflow,
  type ComfyStatus,
} from '@/api/pictures'
import {
  LOCAL_SAMPLING_PROVIDERS,
  OPENAI_PENALTY_PROVIDERS,
  PROVIDER_TYPES,
  type LlmProvider,
  type LocalModelInfo,
  type ScannedGgufModel,
} from '@/api/llm'
import { useI18n } from '@/i18n'
import { getAppConfig, setAppConfig } from '@/api/app'
import { invalidateAttachmentCaps } from '@/api/gallery'
import { getInochiStatus, type InochiEnvStatus } from '@/api/inochi2d'
import type { SttEngineInfo } from '@/api/stt'
import PathPickerModal from './PathPickerModal.vue'

const ui = useUiStore()
const session = useSessionStore()
const stt = useSttStore()
const llm = useLlmStore()
const tts = useTtsStore()
const { t, locale } = useI18n()

type TabId = 'llm' | 'tts' | 'asr' | 'paths' | 'pictures'
const tab = ref<TabId>('llm')

const pictures = usePictureStore()
const pictureTab = ref('siliconflow')
const pictureSaved = ref(false)
const pictureSaveError = ref('')
const PICTURE_SIZES = [
  '1024x1024',
  '768x1344',
  '864x1152',
  '1344x768',
  '1152x864',
  '1440x720',
  '720x1440',
]

/**
 * Edits happen on a local copy. The store's config is replaced after every
 * save, so binding inputs straight to it would reset the form mid-typing once
 * the response lands.
 */
const EMPTY_FORM: PictureConfig = {
  enabled: false,
  autoChat: false,
  autoScript: false,
  cooldownSeconds: 180,
  dailyLimit: 40,
  size: '1024x1024',
  stylePrompt: '',
  negativePrompt: '',
  promptProviderId: '',
  promptStyle: 'auto',
  activeProvider: 'siliconflow',
  providers: {},
  catalogue: {},
}

const pictureForm = ref<PictureConfig>({ ...EMPTY_FORM })

watch(
  () => pictures.config,
  (cfg) => {
    if (cfg) pictureForm.value = JSON.parse(JSON.stringify(cfg)) as PictureConfig
  },
  { immediate: true },
)

const pictureProviderMeta = computed(() => pictureForm.value.catalogue[pictureTab.value])
const pictureProvider = computed(() => pictureForm.value.providers[pictureTab.value])

/**
 * Picking a card also makes it the default.
 *
 * These were two independent controls at first, and that was wrong: the card
 * below highlighted one provider while the dropdown above still said
 * "siliconflow", so the panel looked broken. Choosing a provider in the editor
 * is the decision to use it; the dropdown stays for when the user wants to set
 * the default without editing that provider.
 */
function selectPictureProvider(id: string) {
  pictureTab.value = id
  pictureForm.value.activeProvider = id
}

/** True while the form differs from what is on disk. */
const pictureDirty = computed(
  () =>
    !!pictures.config &&
    JSON.stringify(pictureForm.value) !== JSON.stringify(pictures.config),
)

/** A card is ready only when it is enabled and has whatever key it needs. */
function providerReady(id: string): boolean {
  const cfg = pictureForm.value.providers[id]
  const meta = pictureForm.value.catalogue[id]
  if (!cfg || !meta || !cfg.enabled) return false
  // Mirrors service._ready_providers. AI Horde falls back to its documented
  // anonymous key, so an empty field is still usable there and only there.
  if (id === 'aihorde') return true
  // Local runners need no key. `requiresKey` comes from the catalogue so this
  // stays in step with service._ready_providers instead of hardcoding ids.
  if (meta.requiresKey === false) return true
  if (meta.fields.includes('accountId') && !cfg.accountId.trim()) return false
  if (meta.fields.includes('apiKey') && !cfg.apiKey.trim()) return false
  return true
}

/* ---- provider parameters ------------------------------------------------
 *
 * The catalogue declares what each vendor accepts, so the form is generated
 * rather than hand-written per provider. Values live in `params`, a free-form
 * bag the backend reads with the same keys.
 */

function paramLabel(meta: PictureParamMeta): string {
  return locale.value === 'zh' && meta.labelZh ? meta.labelZh : meta.label
}

/**
 * Tooltip for a provider parameter: what the knob does, plus the value the
 * vendor documents as its default.
 *
 * The default is read from the catalogue instead of being repeated inside each
 * tip, so the two can never drift apart. Booleans read as on/off because
 * "Default true" tells the user nothing about what happens on screen.
 */
function paramTip(meta: PictureParamMeta): string {
  const zh = locale.value === 'zh'
  const base = (zh && meta.tipZh) || meta.tip || ''
  if (!base) return ''
  const def = meta.default
  if (def === undefined || def === null || def === '') return base
  let shown: string
  if (typeof def === 'boolean') {
    shown = zh ? (def ? '开' : '关') : def ? 'on' : 'off'
  } else {
    shown = String(def)
  }
  return `${base}${zh ? ' 默认 ' : ' Default '}${shown}`
}

function paramValue(meta: PictureParamMeta): string {
  const raw = pictureProvider.value?.params?.[meta.key]
  return raw === undefined || raw === null ? '' : String(raw)
}

function applyWorkflowDefaults(workflowId: string) {
  const cfg = pictureProvider.value
  if (!cfg) return
  if (!cfg.params) cfg.params = {}
  cfg.params.unet = ''
  if (!workflowId.startsWith('builtin:')) {
    cfg.params.ckpt = ''
  }
  const found = comfyWorkflows.value.find((item) => item.id === workflowId)
  const defaults = found?.samplerDefaults
  const catDefaults = pictureProviderMeta.value?.defaults?.params || {}
  const keys = ['steps', 'cfg', 'sampler', 'scheduler', 'denoise'] as const
  for (const key of keys) {
    const val = defaults?.[key]
    if (val !== undefined && val !== null && val !== '') {
      cfg.params[key] = val
    } else if (workflowId.startsWith('builtin:') && catDefaults[key] !== undefined) {
      cfg.params[key] = catDefaults[key]
    }
  }
}

function setParam(meta: PictureParamMeta, raw: string) {
  const cfg = pictureProvider.value
  if (!cfg) return
  if (!cfg.params) cfg.params = {}
  const prev = cfg.params[meta.key]
  cfg.params[meta.key] = meta.type === 'number' ? (raw === '' ? '' : Number(raw)) : raw
  if (isComfyTab.value && meta.key === 'workflow' && prev !== raw) {
    applyWorkflowDefaults(raw)
  }
}

/* ---- ComfyUI connection -------------------------------------------------
 *
 * ComfyUI is not a service the app owns: it is a desktop program the user
 * starts when they want a picture. So "not connected" is a first-class state
 * shown on the card, never an error thrown at generation time, and every model
 * list here comes from the running instance rather than from the catalogue.
 */

const comfyStatus = ref<ComfyStatus | null>(null)
const comfyChecking = ref(false)
const comfyError = ref('')

const isComfyTab = computed(() => pictureTab.value === 'comfyui')
const comfyConnected = computed(() => comfyStatus.value?.connected === true)
const comfyWorkflows = computed(() => comfyStatus.value?.workflows ?? [])
const comfyWorkflowId = computed(() =>
  String(pictureProvider.value?.params?.workflow || ''),
)

async function checkComfy(baseUrl = '') {
  if (comfyChecking.value) return
  comfyChecking.value = true
  comfyError.value = ''
  try {
    comfyStatus.value = await getComfyStatus(baseUrl || pictureProvider.value?.baseUrl || '')
  } catch (e) {
    comfyStatus.value = null
    comfyError.value = e instanceof Error ? e.message : String(e)
  } finally {
    comfyChecking.value = false
  }
}

// Probing on tab switch is what makes the card honest the moment it opens,
// instead of only when the user presses generate.
watch(pictureTab, (tab) => {
  if (tab === 'comfyui' && !comfyStatus.value && !comfyChecking.value) void checkComfy()
})

function withCurrentOption(base: string[] | undefined, current: string): string[] {
  const list = [...(base ?? [])]
  if (current && !list.includes(current)) list.push(current)
  return list
}

/** Options that come from the instance, with the catalogue list as fallback. */
function paramOptions(meta: PictureParamMeta): string[] {
  if (isComfyTab.value) {
    const current = paramValue(meta)
    if (meta.key === 'ckpt') {
      const list = withCurrentOption(comfyStatus.value?.models?.checkpoints, current)
      return isBuiltinWorkflow(comfyWorkflowId.value) ? list : ['', ...list]
    }
    if (meta.key === 'unet') {
      return ['', ...withCurrentOption(comfyStatus.value?.models?.unets, current)]
    }
    if (meta.key === 'sampler') {
      return withCurrentOption(comfyStatus.value?.models?.samplers, current)
    }
    if (meta.key === 'scheduler') {
      return withCurrentOption(comfyStatus.value?.models?.schedulers, current)
    }
    if (meta.key === 'workflow') {
      const items = comfyWorkflows.value.map((item) => item.id)
      // Before the first probe answers, fall back to what the catalogue ships so
      // the built-in graph is still selectable instead of an empty dropdown.
      const base = items.length ? items : [...(meta.options || [])]
      return withCurrentOption(base, comfyWorkflowId.value)
    }
  }
  return meta.options || []
}

function paramOptionLabel(meta: PictureParamMeta, value: string): string {
  if (isComfyTab.value) {
    if ((meta.key === 'unet' || meta.key === 'ckpt') && value === '') {
      return t('picture.comfyFollowWorkflow')
    }
    if (meta.key === 'workflow') {
      const found = comfyWorkflows.value.find((item) => item.id === value)
      if (found) return found.label
    }
  }
  return value || '—'
}

/** UiSelect takes {value,label}; the label already carries i18n or catalogue text. */
function paramSelectOptions(meta: PictureParamMeta): { value: string; label: string }[] {
  return paramOptions(meta).map((value) => ({ value, label: paramOptionLabel(meta, value) }))
}

const sizeOptions = computed(() => PICTURE_SIZES.map((s) => ({ value: s, label: s })))

const providerOptions = computed(() =>
  Object.entries(pictureForm.value.catalogue).map(([id, meta]) => ({
    value: id,
    label: meta.label,
  })),
)

const promptModelOptions = computed(() => {
  const opts = [{ value: '', label: t('picture.promptModelHint') }]
  for (const p of llm.providers) {
    const prefix = p.name || p.provider
    const models = (p.models ?? []).map((m) => m.trim()).filter(Boolean)
    if (p.provider === 'llama-cpp' && p.localModelKey && !models.includes(p.localModelKey)) {
      models.unshift(p.localModelKey)
    }
    if (models.length) {
      for (const m of models) {
        opts.push({ value: `${p.id}::${m}`, label: `${prefix} / ${m}` })
      }
    } else {
      opts.push({ value: p.id, label: prefix })
    }
  }
  return opts
})

const promptModelSelection = computed(() => {
  const raw = (pictureForm.value.promptProviderId || '').trim()
  if (!raw) return ''
  if (promptModelOptions.value.some((o) => o.value === raw)) return raw
  if (!raw.includes('::')) {
    const p = llm.providers.find((item) => item.id === raw)
    if (p) {
      const first =
        p.provider === 'llama-cpp'
          ? p.localModelKey || p.models?.[0]
          : p.models?.[0]
      if (first) return `${p.id}::${first}`
    }
  }
  return raw
})

const FALLBACK_PROMPT_STYLES = [
  { id: 'none', label: 'Empty (No style injection · Original)', labelZh: '空（不注入风格指令 · 还原最初）' },
  { id: 'auto', label: 'Auto (match model / workflow)', labelZh: '自动匹配（按模型/工作流）' },
  { id: 'tags', label: 'Tags (SDXL / Illustrious / Pony)', labelZh: '标签式（SDXL / Illustrious / Pony）' },
  { id: 'anima', label: 'Anima (Tags + Natural Language)', labelZh: 'Anima 混合流（标签 + 自然语言）' },
  { id: 'qwen-anime', label: 'Qwen-Image 2.1 · Anime (Natural Language)', labelZh: 'Qwen-Image 2.1 · 动漫（自然语言）' },
  { id: 'qwen-real', label: 'Qwen-Image 2.1 · Realism (Natural Language)', labelZh: 'Qwen-Image 2.1 · 写实（自然语言）' },
  { id: 'natural', label: 'General Natural Language (FLUX / Gemini / Cloud)', labelZh: '通用自然语言（FLUX / Gemini / 云端）' },
]

const promptStyleOptions = computed(() => {
  const list = pictureForm.value.promptStyles?.length
    ? pictureForm.value.promptStyles
    : FALLBACK_PROMPT_STYLES
  const zh = locale.value === 'zh'
  return list.map((s) => ({ value: s.id, label: zh ? s.labelZh || s.label : s.label }))
})

function isBuiltinWorkflow(id: string): boolean {
  const found = comfyWorkflows.value.find((item) => item.id === id)
  return found ? found.builtin === true : id.startsWith('builtin:')
}

function canDeleteWorkflow(id: string): boolean {
  if (!id) return false
  const found = comfyWorkflows.value.find((item) => item.id === id)
  if (!found) return false
  return !found.builtin && !found.readonly
}

const comfyWorkflowNote = computed(
  () => comfyWorkflows.value.find((item) => item.id === comfyWorkflowId.value)?.note || '',
)

const comfyLoaderType = computed(() => {
  const found = comfyWorkflows.value.find((item) => item.id === comfyWorkflowId.value)
  if (found?.loaderType) return found.loaderType
  return isBuiltinWorkflow(comfyWorkflowId.value) ? 'checkpoint' : ''
})

const visiblePictureParams = computed(() => {
  const list = pictureProviderMeta.value?.params || []
  if (!isComfyTab.value) return list
  const loader = comfyLoaderType.value
  return list.filter((meta) => {
    if (meta.key === 'ckpt') return !loader || loader === 'checkpoint' || loader === 'both'
    if (meta.key === 'unet') return loader === 'unet' || loader === 'both'
    return true
  })
})

async function importComfyFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  comfyError.value = ''
  try {
    const text = await file.text()
    let parsed: unknown
    try {
      parsed = JSON.parse(text)
    } catch {
      throw new Error(t('picture.comfyBadJson'))
    }
    const name = file.name.replace(/\.json$/i, '')
    const result = await importComfyWorkflow(name, parsed)
    const status = comfyStatus.value
    comfyStatus.value = status
      ? { ...status, workflows: result.items }
      : {
          baseUrl: '',
          connected: false,
          error: '',
          workflows: result.items,
        }
    const workflowMeta = pictureProviderMeta.value?.params.find(
      (item) => item.key === 'workflow',
    )
    if (workflowMeta) setParam(workflowMeta, result.item.id)
  } catch (e) {
    comfyError.value = e instanceof Error ? e.message : String(e)
  }
}

async function removeComfyWorkflow(id: string) {
  if (!id || isBuiltinWorkflow(id)) return
  comfyError.value = ''
  try {
    const result = await deleteComfyWorkflow(id)
    const status = comfyStatus.value
    if (status) comfyStatus.value = { ...status, workflows: result.items }
    const workflowMeta = pictureProviderMeta.value?.params.find(
      (item) => item.key === 'workflow',
    )
    if (workflowMeta) setParam(workflowMeta, 'builtin:sd')
  } catch (e) {
    comfyError.value = e instanceof Error ? e.message : String(e)
  }
}

function setParamBool(meta: PictureParamMeta, value: boolean) {
  const cfg = pictureProvider.value
  if (!cfg) return
  if (!cfg.params) cfg.params = {}
  cfg.params[meta.key] = value
}

/**
 * Warn while the form is still editable, not after a generation fails.
 * Falling back to another provider is by design, but silently using a different
 * one than the user picked is not: say so up front.
 */const pictureWarning = computed(() => {
  if (!pictureForm.value.enabled) return ''
  const ids = Object.keys(pictureForm.value.catalogue)
  if (!ids.some((id) => providerReady(id))) return t('picture.noneReady')
  if (!providerReady(pictureForm.value.activeProvider)) return t('picture.defaultNotReady')
  return ''
})

async function savePictures() {
  pictureSaveError.value = ''
  try {
    await pictures.saveConfig(pictureForm.value)
    pictureSaved.value = true
    window.setTimeout(() => {
      pictureSaved.value = false
    }, 2400)
  } catch (e) {
    pictureSaveError.value = e instanceof Error ? e.message : String(e)
  }
}


const modelsRoot = ref('')
const resolvedModelsRoot = ref('')
const configuredLlamaRoot = ref('')
const llamaRoot = ref('')
const llamaFound = ref(false)
const configuredInochiPython = ref('')
const inochiEnv = ref<InochiEnvStatus | null>(null)
const configuredFfmpegPath = ref('')
const ffmpegPath = ref('')
const ffmpegFound = ref(false)
const videoAutoStt = ref(false)
const audioCppDir = ref('')
const audioCppFound = ref(false)
const hostExeExt = ref('.exe')
const modelsRootError = ref('')
const modelsSaved = ref(false)

/* ---- reusable path picker ---- */
const pickerOpen = ref(false)
const pickerTitle = ref('')
const pickerInitial = ref('')
const pickerExt = ref('')
const pickerDirsOnly = ref(false)
const pickerAllowDir = ref(false)
let pickerCallback: ((path: string) => void) | null = null

function openPathPicker(opts: {
  title: string
  initialPath?: string
  ext?: string
  dirsOnly?: boolean
  allowDir?: boolean
  onSelect: (path: string) => void
}) {
  pickerTitle.value = opts.title
  pickerInitial.value = opts.initialPath || resolvedModelsRoot.value || ''
  pickerExt.value = opts.ext || ''
  pickerDirsOnly.value = Boolean(opts.dirsOnly)
  pickerAllowDir.value = Boolean(opts.allowDir)
  pickerCallback = opts.onSelect
  pickerOpen.value = true
}

function handlePickerSelect(path: string) {
  pickerCallback?.(path)
  pickerCallback = null
}

watch(
  () => [ui.isOpen('backstage'), ui.backstageTabRequest] as const,
  ([open, req]) => {
    if (open && req && ['llm', 'tts', 'asr', 'paths', 'pictures'].includes(req)) {
      tab.value = req as TabId
      ui.backstageTabRequest = ''
      if (req === 'paths') void loadPaths(true)
    }
  },
)

async function loadPaths(refreshInochi = false) {
  try {
    const [cfg, iEnv] = await Promise.all([
      getAppConfig(),
      getInochiStatus(refreshInochi).catch(() => null),
    ])
    modelsRoot.value = cfg.modelsRoot || ''
    resolvedModelsRoot.value = cfg.resolvedModelsRoot
    configuredLlamaRoot.value = cfg.configuredLlamaRoot || ''
    llamaRoot.value = cfg.llamaRoot
    llamaFound.value = cfg.llamaFound
    configuredInochiPython.value = cfg.configuredInochiPython || ''
    inochiEnv.value = iEnv
    configuredFfmpegPath.value = cfg.configuredFfmpegPath || ''
    ffmpegPath.value = cfg.ffmpegPath || ''
    ffmpegFound.value = Boolean(cfg.ffmpegFound)
    videoAutoStt.value = Boolean(cfg.videoAutoStt)
    invalidateAttachmentCaps()
    audioCppDir.value = cfg.audioCppDir || ''
    audioCppFound.value = Boolean(cfg.audioCppFound)
    hostExeExt.value = cfg.exeExt ?? '.exe'
  } catch (err) {
    modelsRootError.value = err instanceof Error ? err.message : String(err)
  }
}

async function applyModelsRoot() {
  modelsRootError.value = ''
  try {
    await setAppConfig({
      modelsRoot: modelsRoot.value,
      llamaRoot: configuredLlamaRoot.value,
      inochiPythonPath: configuredInochiPython.value,
      ffmpegPath: configuredFfmpegPath.value,
    })
    invalidateAttachmentCaps()
    await loadPaths(true)
    if (llm.isLocal) void llm.refreshLocal()
    modelsSaved.value = true
    window.setTimeout(() => (modelsSaved.value = false), 2200)
  } catch (err) {
    modelsRootError.value = err instanceof Error ? err.message : String(err)
  }
}

/* ---- LLM: protocol picker, model probing & custom GGUF form ---- */
const nonLocalProviderOptions = computed(() =>
  PROVIDER_TYPES.filter((p) => p.value !== 'llama-cpp').map((p) => ({
    value: p.value,
    label: p.label,
  })),
)
const probingModels = ref(false)
const probeNote = ref('')

async function onProbeModels() {
  if (probingModels.value) return
  probingModels.value = true
  probeNote.value = ''
  try {
    const found = await llm.probeModels()
    if (!found.length) probeNote.value = t('backstage.probeEmpty')
  } catch (err) {
    probeNote.value = err instanceof Error ? err.message : String(err)
  } finally {
    probingModels.value = false
  }
}

const localFormOpen = ref(false)
const localFormOrigKey = ref('')
const localFormError = ref('')
const localForm = ref({
  key: '',
  label: '',
  model: '',
  mmproj: '',
  mtp: '',
  ctx: 32768,
  ngl: '99',
  device: 'auto',
  kv_type: 'q8_0',
  flash_attn: true,
  jinja: true,
})
const scanningGguf = ref(false)
const scannedGgufs = ref<ScannedGgufModel[]>([])
const scanNote = ref('')

const deviceSelectOptions = computed(() => [
  { value: 'auto', label: 'Auto (GPU)' },
  { value: 'cpu', label: 'CPU' },
  ...llm.localDeviceList.map((d) => ({
    value: d.id,
    label: `${d.id}: ${d.name}`,
  })),
])

const kvTypeOptions = [
  { value: 'q8_0', label: 'q8_0' },
  { value: 'f16', label: 'f16' },
  { value: 'q4_0', label: 'q4_0' },
]

function onLocalKvChange(nextKv: string) {
  localForm.value.kv_type = nextKv
  if (nextKv !== 'f16') {
    localForm.value.flash_attn = true
  }
}

function onLocalFlashAttnChange(nextFlash: boolean) {
  localForm.value.flash_attn = nextFlash
  if (!nextFlash && localForm.value.kv_type !== 'f16') {
    localForm.value.kv_type = 'f16'
  }
}

function openNewLocalModelForm() {
  localFormOrigKey.value = ''
  localFormError.value = ''
  localForm.value = {
    key: '',
    label: '',
    model: '',
    mmproj: '',
    mtp: '',
    ctx: 32768,
    ngl: '99',
    device: 'auto',
    kv_type: 'q8_0',
    flash_attn: true,
    jinja: true,
  }
  localFormOpen.value = true
}

function openEditLocalModelForm(m: LocalModelInfo) {
  localFormOrigKey.value = m.key
  localFormError.value = ''
  const flash = m.flash_attn !== false
  const rawKv = String(m.kv_type || 'q8_0')
  localForm.value = {
    key: m.key,
    label: m.label || m.key,
    model: String(m.path || ''),
    mmproj: String(m.mmproj || ''),
    mtp: String(m.mtp || ''),
    ctx: Number(m.ctx || 32768),
    ngl: m.ngl !== undefined && m.ngl !== null ? String(m.ngl) : '99',
    device: String(m.device || 'auto'),
    kv_type: flash || rawKv === 'f16' ? rawKv : 'f16',
    flash_attn: flash,
    jinja: m.jinja !== false,
  }
  localFormOpen.value = true
}

async function saveLocalModelForm() {
  localFormError.value = ''
  const f = localForm.value
  const modelPath = f.model.trim()
  if (!modelPath) {
    localFormError.value = 'GGUF path is required'
    return
  }
  const stem =
    modelPath
      .split(/[\\/]/)
      .pop()
      ?.replace(/\.gguf$/i, '')
      .replace(/-\d{5}-of-\d{5}$/i, '') || 'custom-model'
  const key = f.key.trim() || stem.toLowerCase().replace(/[^a-z0-9._-]+/g, '-')
  try {
    await llm.saveCustomLocalModel({
      key,
      old_key: localFormOrigKey.value && localFormOrigKey.value !== key ? localFormOrigKey.value : undefined,
      label: f.label.trim() || stem,
      model: modelPath,
      mmproj: f.mmproj.trim(),
      mtp: f.mtp.trim(),
      ctx: Number(f.ctx) || 32768,
      ngl: f.ngl.trim() === '' ? null : Number(f.ngl),
      device: f.device || 'auto',
      kv_type: f.kv_type || 'q8_0',
      flash_attn: f.flash_attn,
      jinja: f.jinja,
    })
    localFormOrigKey.value = ''
    localFormOpen.value = false
  } catch (err) {
    localFormError.value = err instanceof Error ? err.message : String(err)
  }
}

async function onDeleteLocalModel(key: string) {
  localFormError.value = ''
  try {
    await llm.removeCustomLocalModel(key)
    if (localForm.value.key === key || localFormOrigKey.value === key) localFormOpen.value = false
  } catch (err) {
    localFormError.value = err instanceof Error ? err.message : String(err)
  }
}

async function onScanGguf() {
  if (scanningGguf.value) return
  scanningGguf.value = true
  scanNote.value = ''
  try {
    const found = await llm.scanGgufModels()
    scannedGgufs.value = found
    if (!found.length) scanNote.value = t('backstage.scanEmpty')
  } catch (err) {
    scanNote.value = err instanceof Error ? err.message : String(err)
  } finally {
    scanningGguf.value = false
  }
}

async function addScannedModel(item: ScannedGgufModel) {
  await llm.saveCustomLocalModel({
    key: item.key,
    label: item.label,
    model: item.path,
    mmproj: item.mmproj,
    mtp: item.mtp,
    ctx: 32768,
    ngl: 99,
    device: 'auto',
    kv_type: 'q8_0',
    flash_attn: true,
    jinja: true,
  })
  scannedGgufs.value = scannedGgufs.value.filter((x) => x.path !== item.path)
}

const selectedLocalInfo = computed(
  () => llm.localModelList.find((m) => m.key === llm.localSelection) ?? null,
)
const supportsLocalSampling = computed(() =>
  LOCAL_SAMPLING_PROVIDERS.has(llm.currentProvider?.provider ?? ''),
)
const supportsOpenAiPenalties = computed(() =>
  OPENAI_PENALTY_PROVIDERS.has(llm.currentProvider?.provider ?? ''),
)

const engineMenu = ref(false)
const newModel = ref('')

/**
 * Result of the last unload click. The button has no other visible effect —
 * weights vanish from a panel the user cannot see — so it has to say what
 * happened or the click looks broken.
 */
const unloadNote = ref('')
let unloadTimer = 0

async function onUnload() {
  unloadNote.value = ''
  const result = await tts.unload()
  const key =
    result === 'released'
      ? 'backstage.unloadDone'
      : result === 'cloud'
        ? 'backstage.unloadCloud'
        : result === 'idle'
          ? 'backstage.unloadIdle'
          : 'backstage.unloadFailed'
  unloadNote.value = t(key)
  window.clearTimeout(unloadTimer)
  unloadTimer = window.setTimeout(() => (unloadNote.value = ''), 3200)
}

/* ---- voice audition ---- */

const auditionText = ref('')
const auditionPlaying = ref(false)
const auditionLoading = ref(false)
const auditionError = ref('')
const voiceClips = ref<string[]>([])
let auditionPcm: PcmPlayer | null = null
let auditionAbort: AbortController | null = null
let auditionAudio: HTMLAudioElement | null = null

/**
 * Speak the typed line in the selected voice.
 *
 * Two jobs at once: it is the only way to judge a voice without leaving the
 * panel, and the first call is what loads the engine, so the next chat reply
 * does not pay the model-load cost.
 */
async function toggleAudition() {
  if (auditionPlaying.value) {
    auditionAbort?.abort()
    auditionPcm?.abort()
    auditionAudio?.pause()
    auditionPlaying.value = false
    return
  }
  const cfg = tts.active
  if (!cfg) return
  const text = auditionText.value.trim() || 'Hello, this is a voice preview.'
  auditionError.value = ''
  auditionPlaying.value = true
  auditionLoading.value = true
  try {
    if (shouldStream(cfg, tts.specs)) {
      const player = new PcmPlayer(streamSampleRate(cfg, tts.specs))
      auditionPcm = player
      const ac = new AbortController()
      auditionAbort = ac
      await player.start()
      await synthesizeStream(cfg, text, (chunk) => player.push(chunk), ac.signal, tts.specs)
      await player.drain()
    } else {
      const blob = await synthesize(cfg, text)
      const url = URL.createObjectURL(blob)
      const audio = new Audio(url)
      auditionAudio = audio
      audio.onended = () => URL.revokeObjectURL(url)
      await audio.play()
    }
  } catch (err) {
    const isAbort =
      (err instanceof DOMException && err.name === 'AbortError') ||
      (err instanceof Error && (err.name === 'AbortError' || err.message === 'aborted'))
    if (!isAbort) {
      auditionError.value = err instanceof Error ? err.message : String(err)
    }
  } finally {
    auditionLoading.value = false
    auditionPlaying.value = false
    auditionPcm = null
    auditionAbort = null
    auditionAudio = null
  }
}

onUnmounted(() => {
  auditionAbort?.abort()
  auditionPcm?.abort()
  auditionAudio?.pause()
})

onMounted(() => {
  void loadPaths()
  void stt.load()
  void llm.load()
  void tts.load()
  void getTtsVoices().then((v) => { voiceClips.value = v }).catch(() => undefined)
  void pictures.loadConfig()
  void pictures.refresh()
})

watch(
  () => llm.effectiveModel,
  (m) => {
    session.model = m
    session.modelBackend = llm.currentProvider?.provider ?? ''
  },
  { immediate: true },
)
watch(
  () => llm.currentProvider?.baseUrl,
  (u) => {
    session.endpoint = u ?? ''
  },
  { immediate: true },
)

/**
 * Tab label for a provider: the name the user typed, or the protocol label for
 * entries that predate custom names. Two unnamed providers on the same protocol
 * would read identically, so they get a running number.
 */
function providerBase(p: LlmProvider): string {
  return (
    (p.name || '').trim() ||
    PROVIDER_TYPES.find((x) => x.value === p.provider)?.label ||
    p.provider ||
    'LLM'
  )
}

function providerLabel(p: LlmProvider): string {
  const base = providerBase(p)
  const same = llm.providers.filter((x) => providerBase(x) === base)
  if (same.length < 2) return base
  return `${base} ${same.findIndex((x) => x.id === p.id) + 1}`
}

const hasLocalProvider = computed(() => llm.providers.some((p) => p.provider === 'llama-cpp'))
/**
 * A weight is loaded that is not the one selected. The bar has to say so:
 * RUNNING beside a different model name otherwise reads as "this one is up".
 */
const localLoadedOther = computed(
  () =>
    llm.localRunning &&
    Boolean(llm.localModelKey) &&
    llm.localModelKey !== llm.localSelection,
)

/** Deleting a provider takes its model list and per-model parameters with it. */
function askRemoveProvider() {
  const p = llm.currentProvider
  if (!p) return
  const idx = llm.activeIndex
  ui.askConfirm({
    title: t('backstage.removeProviderTitle'),
    text: t('backstage.removeProviderText', { name: providerBase(p) }),
    onConfirm: () => llm.removeProvider(idx),
  })
}

function engineLabel(v: string): string {
  return TTS_ENGINES.find((e) => e.value === v)?.label ?? (v || 'TTS')
}

/** Load state of one STT engine, as the pill next to its name. */
function sttStateLabel(e: SttEngineInfo | null | undefined): string {
  if (!e) return ''
  return e.error
    ? t('backstage.sttFailed')
    : e.loaded
      ? t('backstage.sttLoaded')
      : t('backstage.sttLazy')
}

const sttStopNote = ref('')
let sttStopTimer = 0

async function onToggleVideoAutoStt(enabled: boolean) {
  const prev = videoAutoStt.value
  videoAutoStt.value = enabled
  try {
    await setAppConfig({ videoAutoStt: enabled })
    invalidateAttachmentCaps()
  } catch (err) {
    videoAutoStt.value = prev
    modelsRootError.value = err instanceof Error ? err.message : String(err)
  }
}

/**
 * Result line for the ASR stop button. Freeing VRAM behind a process the panel
 * cannot show means the click has no visible effect, so it has to say what
 * happened or it reads as broken. Three outcomes, three messages: stopped,
 * nothing was running, or the backend could not end it.
 */
async function onStopStt() {
  sttStopNote.value = ''
  const result = await stt.stopActive()
  const key =
    result === 'stopped'
      ? 'backstage.sttStopDone'
      : result === 'idle'
        ? 'backstage.sttStopIdle'
        : 'backstage.sttStopFailed'
  sttStopNote.value = t(key)
  window.clearTimeout(sttStopTimer)
  sttStopTimer = window.setTimeout(() => (sttStopNote.value = ''), 3200)
}

/**
 * Engines still missing from the config list, split by how they run. Only these
 * are offered when adding: one config per engine, so re-offering an engine that
 * is already there is what let the panel fill up with duplicates.
 */
const engineGroups = computed(() => {
  const used = new Set(tts.configs.map((c) => c.provider))
  const free = TTS_ENGINES.filter(
    (e) => MULTI_INSTANCE_TTS_ENGINES.has(e.value) || !used.has(e.value),
  )
  return [
    {
      kind: 'local' as const,
      label: t('backstage.engineGroupLocal'),
      items: free.filter((e) => e.kind === 'local'),
    },
    {
      kind: 'cloud' as const,
      label: t('backstage.engineGroupCloud'),
      items: free.filter((e) => e.kind === 'cloud'),
    },
  ].filter((g) => g.items.length > 0)
})

function configLabel(c: TtsConfig): string {
  const custom = (c.name || '').trim()
  if (custom) return custom
  if (c.provider === 'audiocpp_tts' && c.audiocppFamily) {
    const spec = tts.specs.find((s) => s.family === c.audiocppFamily)
    if (spec?.displayName) return spec.displayName
    return c.audiocppFamily
  }
  const base = engineLabel(c.provider)
  const same = tts.configs.filter((x) => x.provider === c.provider)
  if (same.length < 2) return base
  return `${base} ${same.findIndex((x) => x.id === c.id) + 1}`
}

const computeBackendOptions = [
  { value: 'vulkan', label: 'Vulkan (GPU)' },
  { value: 'cpu', label: 'CPU' },
]

const ttsFamilyOptions = computed(() =>
  tts.specs.map((s) => ({
    value: s.family,
    label: `${s.displayName} (${s.family})`,
  })),
)

const activeTtsSpec = computed(() => {
  const fam = tts.active?.audiocppFamily
  return fam ? tts.specs.find((s) => s.family === fam) ?? null : null
})

const ttsTaskOptions = computed(() => {
  const tasks = activeTtsSpec.value?.serverTasks?.length ? activeTtsSpec.value.serverTasks : ['tts']
  const labels: Record<string, string> = {
    tts: 'TTS',
    clon: 'Clone (clon)',
    vdes: 'Voice Design (vdes)',
  }
  return tasks.map((v) => ({ value: v, label: labels[v] || v }))
})

const activeTtsPathHint = computed(() => {
  const kind = activeTtsSpec.value?.pathKind
  if (kind === 'dir') return t('backstage.pathHintDir')
  if (kind === 'file') return t('backstage.pathHintFile')
  return t('backstage.pathHintEither')
})

const audiocppVoiceOptions = computed(() => {
  const seen = new Set<string>()
  const opts = [{ value: '', label: '(default)' }]
  for (const v of voiceClips.value) {
    if (!v || seen.has(v)) continue
    seen.add(v)
    opts.push({ value: v, label: v })
  }
  const cur = (tts.active?.voiceName || '').trim()
  if (cur && !seen.has(cur)) {
    opts.push({ value: cur, label: cur })
  }
  return opts
})

function onChangeAudiocppFamily(family: string) {
  const spec = tts.specs.find((s) => s.family === family)
  const defaultTask = spec?.serverTasks?.[0] || 'tts'
  void tts.patch({
    audiocppFamily: family,
    audiocppTask: defaultTask,
    audiocppOptions: {},
    audiocppSessionOptions: {},
    audiocppLoadOptions: {},
  })
}

function specOptStep(opt: AudioSpecOption): string | undefined {
  if (opt.type === 'int') return '1'
  if (opt.type === 'float') return 'any'
  return undefined
}

function patchAudioCppScopedOpt(
  scope: 'request' | 'session' | 'load',
  key: string,
  val: unknown,
) {
  const c = tts.active
  if (!c) return
  const field =
    scope === 'session'
      ? 'audiocppSessionOptions'
      : scope === 'load'
        ? 'audiocppLoadOptions'
        : 'audiocppOptions'
  const next = { ...((c[field] as Record<string, unknown> | undefined) || {}) }
  if (val === '' || val === undefined || val === null) {
    delete next[key]
  } else {
    next[key] = val
  }
  void tts.patch({ [field]: next })
}

/* ---- ASR custom engine editor ---- */
const sttFormOpen = ref(false)
const sttFormError = ref('')
const sttForm = ref({
  id: '',
  label: '',
  kind: 'audiocpp' as 'builtin' | 'audiocpp' | 'openai_stt',
  family: 'sense_asr',
  modelPath: '',
  computeBackend: 'vulkan',
  baseUrl: 'http://127.0.0.1:8081/v1',
  apiKey: '',
  model: 'whisper-1',
  language: '',
  requestOptions: {} as Record<string, unknown>,
  sessionOptions: {} as Record<string, unknown>,
  loadOptions: {} as Record<string, unknown>,
})

const sttFamilyOptions = computed(() =>
  stt.specs.map((s) => ({
    value: s.family,
    label: `${s.displayName} (${s.family})`,
  })),
)

const sttSpec = computed(() =>
  stt.specs.find((s) => s.family === sttForm.value.family) ?? null,
)

function patchSttScopedOpt(
  scope: 'request' | 'session' | 'load',
  key: string,
  val: unknown,
) {
  const field =
    scope === 'session'
      ? 'sessionOptions'
      : scope === 'load'
        ? 'loadOptions'
        : 'requestOptions'
  const next = { ...(sttForm.value[field] || {}) }
  if (val === '' || val === undefined || val === null) {
    delete next[key]
  } else {
    next[key] = val
  }
  sttForm.value[field] = next
}

function onChangeSttFamily(family: string) {
  sttForm.value.family = family
  sttForm.value.requestOptions = {}
  sttForm.value.sessionOptions = {}
  sttForm.value.loadOptions = {}
}

const sttKindOptions = computed(() => [
  { value: 'audiocpp', label: t('backstage.asrKindAudiocpp') },
  { value: 'openai_stt', label: t('backstage.asrKindHttp') },
])

function openNewSttForm() {
  sttFormError.value = ''
  sttForm.value = {
    id: '',
    label: '',
    kind: 'audiocpp',
    family: stt.specs[0]?.family || 'sense_asr',
    modelPath: '',
    computeBackend: 'vulkan',
    baseUrl: 'http://127.0.0.1:8081/v1',
    apiKey: '',
    model: 'whisper-1',
    language: '',
    requestOptions: {},
    sessionOptions: {},
    loadOptions: {},
  }
  sttFormOpen.value = true
}

function openEditSttForm(e: SttEngineInfo) {
  sttFormError.value = ''
  sttForm.value = {
    id: e.id,
    label: e.label,
    kind: e.kind || (e.builtin ? 'builtin' : 'audiocpp'),
    family: e.family || 'qwen3_asr',
    modelPath: e.modelPath || '',
    computeBackend: e.computeBackend || 'vulkan',
    baseUrl: e.baseUrl || 'http://127.0.0.1:8081/v1',
    apiKey: e.apiKey || '',
    model: e.model || 'whisper-1',
    language: e.language || '',
    requestOptions: { ...(e.requestOptions || {}) },
    sessionOptions: { ...(e.sessionOptions || {}) },
    loadOptions: { ...(e.loadOptions || {}) },
  }
  sttFormOpen.value = true
}

async function saveSttForm() {
  sttFormError.value = ''
  const f = sttForm.value
  try {
    await stt.upsertEngine({
      id: f.id || undefined,
      label: f.label.trim() || f.family || 'Custom ASR',
      kind: f.kind,
      family: f.family,
      modelPath: f.modelPath.trim(),
      computeBackend: f.computeBackend,
      baseUrl: f.baseUrl.trim(),
      apiKey: f.apiKey.trim(),
      model: f.model.trim() || 'whisper-1',
      language: f.language.trim(),
      requestOptions: f.requestOptions,
      sessionOptions: f.sessionOptions,
      loadOptions: f.loadOptions,
    })
    sttFormOpen.value = false
  } catch (err) {
    sttFormError.value = err instanceof Error ? err.message : String(err)
  }
}

async function onDeleteSttEngine(id: string) {
  sttFormError.value = ''
  try {
    await stt.removeEngine(id)
    if (sttForm.value.id === id) sttFormOpen.value = false
  } catch (err) {
    sttFormError.value = err instanceof Error ? err.message : String(err)
  }
}

function pickEngine(provider: string) {
  tts.addConfig(provider)
  engineMenu.value = false
}

function askRemoveConfig() {
  const c = tts.active
  if (!c) return
  const idx = tts.activeIndex
  ui.askConfirm({
    title: t('backstage.removeEngineTitle'),
    text: t('backstage.removeEngineText', { name: engineLabel(c.provider) }),
    onConfirm: () => tts.removeConfig(idx),
  })
}

function commitModel() {
  const name = newModel.value.trim()
  if (!name) return
  llm.addModel(name)
  newModel.value = ''
}
</script>

<template>
  <aside class="panel desk" :class="{ show: ui.isOpen('backstage') }">
    <div class="d-head">
      <span class="d-kicker">{{ t('backstage.title') }}</span>
      <span class="d-title">{{ t('backstage.title') }}</span>
      <button class="d-close" @click="ui.closePanel('backstage')">
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>

    <div class="tabs">
      <button class="tab" :class="{ on: tab === 'llm' }" @click="tab = 'llm'">{{ t('backstage.tabModel') }}</button>
      <button class="tab" :class="{ on: tab === 'tts' }" @click="tab = 'tts'">{{ t('backstage.tabVoice') }}</button>
      <button class="tab" :class="{ on: tab === 'asr' }" @click="tab = 'asr'">{{ t('backstage.tabEars') }}</button>
      <button class="tab" :class="{ on: tab === 'paths' }" @click="tab = 'paths'">{{ t('backstage.tabPaths') }}</button>
      <button class="tab" :class="{ on: tab === 'pictures' }" @click="tab = 'pictures'">{{ t('picture.settings') }}</button>
    </div>

    <div class="d-body" style="padding: 0">
      <!-- Model -->
      <div class="tab-pane" :class="{ on: tab === 'llm' }">
        <div class="pv-tabs">
          <button
            v-for="(p, i) in llm.providers"
            :key="p.id"
            class="pv-tab"
            :class="{ on: i === llm.activeIndex }"
            @click="llm.setActiveProvider(i)"
          >
            {{ providerLabel(p) }}
          </button>
          <button class="pv-tab add" :aria-label="t('backstage.addProvider')" @click="llm.addProvider()">
            +
          </button>
        </div>

        <div v-if="!llm.providers.length" class="empty-block">
          <div class="eb-title">{{ t('backstage.noProvider') }}</div>
          <div class="eb-hint">{{ t('backstage.noProviderHint') }}</div>
        </div>

        <template v-else>
          <div class="desk-strip">
            <button class="tape">
              <span class="tape-reel"></span>
              <span class="tape-label"><b>{{ llm.effectiveModel || '-' }}</b><i>{{ llm.currentProvider?.provider }}</i></span>
              <span class="tape-reel"></span>
            </button>
          </div>

          <div v-if="llm.isLocal" class="local-bar">
            <span class="lb-dot" :class="{ on: llm.localRunning }"></span>
            <span class="lb-state">{{ llm.localRunning ? t('backstage.localRunning') : t('backstage.localStopped') }}</span>
            <span v-if="localLoadedOther" class="lb-note-wrap" :data-tip="t('backstage.localLoadedHint')">
              <span class="lb-note">{{ t('backstage.localLoaded', { model: llm.localModelKey }) }}</span>
            </span>
            <button
              class="lb-btn"
              :disabled="llm.localBusy || (!llm.localRunning && selectedLocalInfo?.available === false)"
              @click="llm.localRunning && !localLoadedOther ? llm.stopLocal() : llm.startLocal(undefined, llm.activeParams.enableMtp)"
            >
              {{ llm.localRunning && !localLoadedOther ? t('backstage.localStop') : t('backstage.localStart') }}
            </button>
            <button class="lb-btn ghost" :disabled="llm.localBusy" @click="llm.refreshLocal()">
              {{ t('backstage.localRefresh') }}
            </button>
            <button
              class="lb-btn ghost"
              :disabled="llm.localBusy || !llm.localRunning"
              :data-tip="t('backstage.localReleaseHint')"
              @click="llm.releaseLocal()"
            >
              {{ t('backstage.localRelease') }}
            </button>
          </div>
          <div v-if="llm.isLocal && llm.lastError" class="path-err">{{ llm.lastError }}</div>

          <div class="jacks">
            <label v-if="!llm.isLocal" class="jack full">
              <span class="jack-label">{{ t('backstage.providerType') }}</span>
              <UiSelect
                :model-value="llm.currentProvider?.provider || 'openai'"
                :options="nonLocalProviderOptions"
                @update:model-value="llm.setProviderType($event)"
              />
            </label>
            <label class="jack full">
              <span class="jack-label">{{ t('backstage.providerName') }}</span>
              <input
                class="jack-in"
                :value="llm.currentProvider?.name"
                :placeholder="llm.currentProvider ? providerBase(llm.currentProvider) : ''"
                @change="llm.setProviderField('name', ($event.target as HTMLInputElement).value)"
              />
            </label>
            <label class="jack">
              <span class="jack-label">{{ t('backstage.endpoint') }}</span>
              <input
                class="jack-in"
                :value="llm.currentProvider?.baseUrl"
                placeholder="https://api.example.com/v1/"
                @change="llm.setProviderField('baseUrl', ($event.target as HTMLInputElement).value)"
              />
            </label>
            <label class="jack">
              <span class="jack-label">{{ t('backstage.apiKey') }}</span>
              <input
                class="jack-in"
                type="password"
                :value="llm.currentProvider?.apiKey"
                @change="llm.setProviderField('apiKey', ($event.target as HTMLInputElement).value)"
              />
            </label>
          </div>

          <div class="models">
            <template v-if="llm.isLocal">
              <button
                v-for="m in llm.localModelList"
                :key="m.key"
                class="mdl"
                :class="{ on: m.key === llm.localSelection, missing: m.available === false }"
                :title="m.path || ''"
                @click="llm.selectLocalModel(m.key)"
              >
                {{ m.label || m.key }}
                <span v-if="m.size_gb" class="mdl-sz">{{ m.size_gb }}G</span>
              </button>
              <button class="mdl add" @click="openNewLocalModelForm">{{ t('backstage.addLocalModel') }}</button>
              <button class="mdl add" :disabled="scanningGguf" @click="onScanGguf">
                {{ scanningGguf ? t('backstage.scanning') : t('backstage.scanLocalModels') }}
              </button>
              <button
                v-if="selectedLocalInfo"
                class="mdl add"
                @click="openEditLocalModelForm(selectedLocalInfo)"
              >
                {{ t('backstage.editModel') }}
              </button>
            </template>
            <template v-else>
              <button
                v-for="m in llm.currentProvider?.models"
                :key="m"
                class="mdl"
                :class="{ on: m === llm.selectedModel }"
                @click="llm.setSelectedModel(m)"
              >
                {{ m }}
              </button>
              <input
                v-model="newModel"
                class="mdl-in"
                :placeholder="t('backstage.addModel')"
                @keydown.enter.prevent="commitModel"
              />
              <button class="mdl add" :disabled="probingModels" @click="onProbeModels">
                {{ probingModels ? t('backstage.probing') : t('backstage.probeModels') }}
              </button>
            </template>
          </div>
          <div v-if="!llm.isLocal && probeNote" class="path-err">{{ probeNote }}</div>
          <div v-if="llm.isLocal && scanNote" class="path-hint" style="padding: 0 26px 8px">{{ scanNote }}</div>

          <div v-if="llm.isLocal && scannedGgufs.length" class="sub-box">
            <div v-for="s in scannedGgufs" :key="s.path" class="scan-row">
              <span class="scan-name" :title="s.path">{{ s.relPath }} ({{ s.sizeGb }} GB)</span>
              <button class="mini-btn" @click="addScannedModel(s)">+ {{ t('backstage.addModel') }}</button>
            </div>
          </div>

          <div v-if="llm.isLocal && localFormOpen" class="sub-box">
            <div class="sub-box-head">{{ t('backstage.localFormTitle') }}</div>
            <label class="num">
              <span class="num-name">{{ t('backstage.modelKey') }}</span>
              <input v-model="localForm.key" class="num-in" placeholder="qwen3-8b" />
            </label>
            <label class="num">
              <span class="num-name">{{ t('backstage.modelLabel') }}</span>
              <input v-model="localForm.label" class="num-in" placeholder="Qwen3 8B" />
            </label>
            <label class="num">
              <span class="num-name">{{ t('backstage.modelFilePath') }}</span>
              <div class="path-pick-wrap">
                <input v-model="localForm.model" class="num-in path" placeholder="D:\models\model.gguf" />
                <button
                  type="button"
                  class="mini-btn"
                  @click="openPathPicker({
                    title: t('backstage.modelFilePath'),
                    ext: '.gguf',
                    onSelect: (p) => { localForm.model = p },
                  })"
                >{{ t('backstage.browse') }}</button>
              </div>
            </label>
            <label class="num">
              <span class="num-name">{{ t('backstage.mmprojPath') }}</span>
              <div class="path-pick-wrap">
                <input v-model="localForm.mmproj" class="num-in path" placeholder="(optional)" />
                <button
                  type="button"
                  class="mini-btn"
                  @click="openPathPicker({
                    title: t('backstage.mmprojPath'),
                    ext: '.gguf',
                    onSelect: (p) => { localForm.mmproj = p },
                  })"
                >{{ t('backstage.browse') }}</button>
              </div>
            </label>
            <label class="num">
              <span class="num-name">{{ t('backstage.mtpPath') }}</span>
              <div class="path-pick-wrap">
                <input v-model="localForm.mtp" class="num-in path" placeholder="(optional)" />
                <button
                  type="button"
                  class="mini-btn"
                  @click="openPathPicker({
                    title: t('backstage.mtpPath'),
                    ext: '.gguf',
                    onSelect: (p) => { localForm.mtp = p },
                  })"
                >{{ t('backstage.browse') }}</button>
              </div>
            </label>
            <label class="num">
              <span class="num-name">{{ t('backstage.ctxSize') }}</span>
              <input v-model.number="localForm.ctx" class="num-in" type="number" min="512" />
            </label>
            <label class="num">
              <span class="num-name">{{ t('backstage.nglLayers') }}</span>
              <input v-model="localForm.ngl" class="num-in" placeholder="99" />
            </label>
            <label class="num">
              <span class="num-name">{{ t('backstage.computeDevice') }}</span>
              <UiSelect
                :model-value="localForm.device"
                :options="deviceSelectOptions"
                @update:model-value="localForm.device = $event"
              />
            </label>
            <label class="num">
              <span class="num-name">{{ t('backstage.kvType') }}</span>
              <UiSelect
                :model-value="localForm.kv_type"
                :options="kvTypeOptions"
                @update:model-value="onLocalKvChange($event)"
              />
            </label>
            <UiLever
              :model-value="localForm.flash_attn"
              :label="t('backstage.flashAttn')"
              @update:model-value="onLocalFlashAttnChange($event)"
            />
            <div class="path-hint">{{ t('backstage.kvFlashHint') }}</div>
            <UiLever v-model="localForm.jinja" :label="t('backstage.useJinja')" />
            <div v-if="localFormError" class="path-err">{{ localFormError }}</div>
            <div class="path-row">
              <button class="mini-btn primary" @click="saveLocalModelForm">{{ t('backstage.saveModel') }}</button>
              <button class="mini-btn" @click="localFormOpen = false">{{ t('dialog.cancel') }}</button>
              <button
                v-if="localFormOrigKey && !selectedLocalInfo?.builtin"
                class="mini-btn danger"
                @click="onDeleteLocalModel(localFormOrigKey)"
              >{{ t('backstage.deleteModel') }}</button>
            </div>
          </div>

          <div class="faders">
            <UiSlider
              :model-value="llm.activeParams.temperature"
              :label="t('backstage.temperature')"
              :max="2"
              @update:model-value="(v: number) => llm.setParam('temperature', v)"
            />
            <UiSlider
              :model-value="llm.activeParams.topP"
              :label="t('backstage.topP')"
              :max="1"
              @update:model-value="(v: number) => llm.setParam('topP', v)"
            />
            <template v-if="supportsLocalSampling">
              <UiSlider
                :model-value="llm.activeParams.topK ?? 64"
                :label="t('backstage.topK')"
                :max="200"
                @update:model-value="(v: number) => llm.setParam('topK', v)"
              />
              <UiSlider
                :model-value="llm.activeParams.minP ?? 0.05"
                :label="t('backstage.minP')"
                :max="1"
                @update:model-value="(v: number) => llm.setParam('minP', v)"
              />
              <UiSlider
                :model-value="llm.activeParams.repeatPenalty ?? 1.1"
                :label="t('backstage.repeat')"
                :max="2"
                @update:model-value="(v: number) => llm.setParam('repeatPenalty', v)"
              />
            </template>
            <template v-if="supportsOpenAiPenalties">
              <UiSlider
                :model-value="llm.activeParams.frequencyPenalty ?? 0"
                :label="t('backstage.frequencyPenalty')"
                :max="2"
                :step="0.05"
                @update:model-value="(v: number) => llm.setParam('frequencyPenalty', v)"
              />
              <UiSlider
                :model-value="llm.activeParams.presencePenalty ?? 0"
                :label="t('backstage.presencePenalty')"
                :max="2"
                :step="0.05"
                @update:model-value="(v: number) => llm.setParam('presencePenalty', v)"
              />
            </template>
            <UiLever
              v-if="llm.isLocal && (selectedLocalInfo?.has_mtp || selectedLocalInfo?.mtp)"
              :model-value="llm.activeParams.enableMtp"
              :label="t('backstage.enableMtp')"
              @update:model-value="(v: boolean) => llm.setParam('enableMtp', v)"
            />
            <div class="nums">
              <div class="num">
                <span class="num-name">{{ t('backstage.maxTokens') }}</span>
                <input
                  class="num-in"
                  :value="llm.activeParams.maxTokens"
                  @change="llm.setParam('maxTokens', Number(($event.target as HTMLInputElement).value))"
                />
              </div>
              <div class="num">
                <span class="num-name">{{ t('backstage.maxContext') }}</span>
                <input
                  class="num-in"
                  :value="llm.activeParams.maxContext"
                  @change="llm.setParam('maxContext', Number(($event.target as HTMLInputElement).value))"
                />
              </div>
            </div>
          </div>

          <div class="desk-foot">
            <button
              v-if="!hasLocalProvider"
              class="mini-btn"
              @click="llm.addLocalProvider()"
            >
              {{ t('backstage.addLocalProvider') }}
            </button>
            <button class="mini-btn" :disabled="!llm.currentProvider" @click="askRemoveProvider">
              {{ t('backstage.removeProvider') }}
            </button>
          </div>
        </template>
      </div>

      <!-- Voice -->
      <div class="tab-pane" :class="{ on: tab === 'tts' }">
        <div class="pv-tabs">
          <button
            v-for="(c, i) in tts.configs"
            :key="c.id"
            class="pv-tab"
            :class="{ on: i === tts.activeIndex }"
            @click="tts.setActiveIndex(i)"
          >
            {{ configLabel(c) }}
          </button>
          <button
            v-if="engineGroups.length"
            class="pv-tab add"
            :aria-label="t('backstage.addEngine')"
            @click="engineMenu = !engineMenu"
          >
            +
          </button>
        </div>

        <div v-if="engineMenu && engineGroups.length" class="pv-menu">
          <template v-for="g in engineGroups" :key="g.kind">
            <span class="pv-group">{{ g.label }}</span>
            <button v-for="e in g.items" :key="e.value" class="pv-opt" @click="pickEngine(e.value)">
              {{ e.label }}
            </button>
          </template>
        </div>

        <div v-if="!tts.active" class="empty-block">
          <div class="eb-title">{{ t('backstage.noVoice') }}</div>
          <div class="eb-hint">{{ t('backstage.noVoiceHint') }}</div>
        </div>

        <template v-else>
          <div class="faders">

            <template v-if="tts.active.provider === 'omnivoice'">
              <label class="num">
                <span class="num-name">{{ t('backstage.voice') }}</span>
                <input class="num-in" :value="tts.active.omniVoice" placeholder="voice/clone.wav"
                  @change="tts.patch({ omniVoice: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num wide">
                <span class="num-name">{{ t('backstage.instruct') }}</span>
                <input class="num-in" :value="tts.active.omniInstruct" placeholder="a warm low voice"
                  @change="tts.patch({ omniInstruct: ($event.target as HTMLInputElement).value })" />
              </label>
              <UiSlider
                :model-value="tts.active.omniSpeed ?? 1"
                :label="t('backstage.speed')"
                :max="2"
                @update:model-value="(v: number) => tts.patch({ omniSpeed: v })"
              />
              <label class="num">
                <span class="num-name">{{ t('backstage.steps') }}</span>
                <input class="num-in" :value="tts.active.omniNumSteps"
                  @change="tts.patch({ omniNumSteps: ($event.target as HTMLInputElement).value })" />
              </label>
            </template>

            <template v-else-if="tts.active.provider === 'dotstts'">
              <label class="num">
                <span class="num-name">{{ t('backstage.voice') }}</span>
                <input class="num-in" :value="tts.active.dotsVoice"
                  @change="tts.patch({ dotsVoice: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.language') }}</span>
                <input class="num-in" :value="tts.active.dotsLanguage"
                  @change="tts.patch({ dotsLanguage: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.steps') }}</span>
                <input class="num-in" :value="tts.active.dotsNumSteps"
                  @change="tts.patch({ dotsNumSteps: ($event.target as HTMLInputElement).value })" />
              </label>
            </template>

            <template v-else-if="tts.active.provider === 'fishaudio'">
              <label class="num">
                <span class="num-name">{{ t('backstage.voice') }}</span>
                <input class="num-in" :value="tts.active.fishVoice"
                  @change="tts.patch({ fishVoice: ($event.target as HTMLInputElement).value })" />
              </label>
              <UiSlider
                :model-value="tts.active.fishTemperature ?? 0.8"
                :label="t('backstage.temperature')"
                :max="2"
                @update:model-value="(v: number) => tts.patch({ fishTemperature: v })"
              />
              <UiSlider
                :model-value="tts.active.fishTopP ?? 0.8"
                :label="t('backstage.topP')"
                :max="1"
                @update:model-value="(v: number) => tts.patch({ fishTopP: v })"
              />
              <label class="num">
                <span class="num-name">{{ t('backstage.topK') }}</span>
                <input class="num-in" :value="tts.active.fishTopK"
                  @change="tts.patch({ fishTopK: Number(($event.target as HTMLInputElement).value) })" />
              </label>
            </template>

            <template v-else-if="tts.active.provider === 'ming3tts'">
              <label class="num">
                <span class="num-name">{{ t('backstage.task') }}</span>
                <input class="num-in" :value="tts.active.ming3Task" placeholder="tts"
                  @change="tts.patch({ ming3Task: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.voice') }}</span>
                <input class="num-in" :value="tts.active.ming3Voice" placeholder="US_Emma"
                  @change="tts.patch({ ming3Voice: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.template') }}</span>
                <input class="num-in" :value="tts.active.ming3Template" placeholder="basic"
                  @change="tts.patch({ ming3Template: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num wide">
                <span class="num-name">{{ t('backstage.instruct') }}</span>
                <input class="num-in" :value="tts.active.ming3Instruct"
                  @change="tts.patch({ ming3Instruct: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.emotion') }}</span>
                <input class="num-in" :value="tts.active.ming3Emotion"
                  @change="tts.patch({ ming3Emotion: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.dialect') }}</span>
                <input class="num-in" :value="tts.active.ming3Dialect"
                  @change="tts.patch({ ming3Dialect: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.pitch') }}</span>
                <input class="num-in" :value="tts.active.ming3Pitch"
                  @change="tts.patch({ ming3Pitch: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.volume') }}</span>
                <input class="num-in" :value="tts.active.ming3Volume"
                  @change="tts.patch({ ming3Volume: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.language') }}</span>
                <input class="num-in" :value="tts.active.ming3Language"
                  @change="tts.patch({ ming3Language: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num wide">
                <span class="num-name">{{ t('backstage.refText') }}</span>
                <input class="num-in" :value="tts.active.ming3ReferenceText"
                  @change="tts.patch({ ming3ReferenceText: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.schema') }}</span>
                <input class="num-in" :value="tts.active.ming3InstructionSchema"
                  @change="tts.patch({ ming3InstructionSchema: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.voiceId') }}</span>
                <input class="num-in" :value="tts.active.ming3VoiceId"
                  @change="tts.patch({ ming3VoiceId: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.guidance') }}</span>
                <input class="num-in" :value="tts.active.ming3GuidanceScale"
                  @change="tts.patch({ ming3GuidanceScale: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.sigma') }}</span>
                <input class="num-in" :value="tts.active.ming3Sigma"
                  @change="tts.patch({ ming3Sigma: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.temperature') }}</span>
                <input class="num-in" :value="tts.active.ming3Temperature"
                  @change="tts.patch({ ming3Temperature: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.seed') }}</span>
                <input class="num-in" :value="tts.active.ming3Seed"
                  @change="tts.patch({ ming3Seed: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.maxTokens') }}</span>
                <input class="num-in" :value="tts.active.ming3MaxTokens"
                  @change="tts.patch({ ming3MaxTokens: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.chunkSize') }}</span>
                <input class="num-in" :value="tts.active.ming3TextChunkSize"
                  @change="tts.patch({ ming3TextChunkSize: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.chunkMode') }}</span>
                <input class="num-in" :value="tts.active.ming3TextChunkMode" placeholder="tag_aware"
                  @change="tts.patch({ ming3TextChunkMode: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.framesPerEvent') }}</span>
                <input class="num-in" :value="tts.active.ming3StreamFramesPerEvent"
                  @change="tts.patch({ ming3StreamFramesPerEvent: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.chunkPolicy') }}</span>
                <input class="num-in" :value="tts.active.ming3StreamChunkPolicy" placeholder="grow"
                  @change="tts.patch({ ming3StreamChunkPolicy: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num wide">
                <span class="num-name">{{ t('backstage.embeddingFile') }}</span>
                <input class="num-in" :value="tts.active.ming3SpeakerEmbeddingFile"
                  @change="tts.patch({ ming3SpeakerEmbeddingFile: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num wide">
                <span class="num-name">{{ t('backstage.exportEmbedding') }}</span>
                <input class="num-in" :value="tts.active.ming3ExportSpeakerEmbeddingFile"
                  @change="tts.patch({ ming3ExportSpeakerEmbeddingFile: ($event.target as HTMLInputElement).value })" />
              </label>
              <UiLever
                :model-value="tts.active.ming3UseSpeakerEmbedding ?? false"
                :label="t('backstage.useEmbedding')"
                @update:model-value="(v: boolean) => tts.patch({ ming3UseSpeakerEmbedding: v })"
              />
              <UiLever
                :model-value="tts.active.ming3ZeroSpeakerEmbedding ?? false"
                :label="t('backstage.zeroEmbedding')"
                @update:model-value="(v: boolean) => tts.patch({ ming3ZeroSpeakerEmbedding: v })"
              />
              <UiLever
                :model-value="tts.active.ming3RawText ?? false"
                :label="t('backstage.rawText')"
                @update:model-value="(v: boolean) => tts.patch({ ming3RawText: v })"
              />
              <UiLever
                :model-value="tts.active.ming3TrimSilence ?? false"
                :label="t('backstage.trimSilence')"
                @update:model-value="(v: boolean) => tts.patch({ ming3TrimSilence: v })"
              />
              <UiLever
                :model-value="tts.active.ming3ReferenceCache ?? true"
                :label="t('backstage.refCache')"
                @update:model-value="(v: boolean) => tts.patch({ ming3ReferenceCache: v })"
              />
            </template>

            <template v-else-if="tts.active.provider === 'audiocpp_tts'">
              <label class="num">
                <span class="num-name">{{ t('backstage.displayName') }}</span>
                <input class="num-in" :value="tts.active.name" placeholder="Kokoro / CosyVoice3"
                  @change="tts.patch({ name: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.modelFamily') }}</span>
                <UiSelect
                  :model-value="tts.active.audiocppFamily || ''"
                  :options="ttsFamilyOptions"
                  @update:model-value="onChangeAudiocppFamily"
                />
              </label>
              <label v-if="ttsTaskOptions.length > 1" class="num">
                <span class="num-name">{{ t('backstage.task') }}</span>
                <UiSelect
                  :model-value="tts.active.audiocppTask || ttsTaskOptions[0]?.value || 'tts'"
                  :options="ttsTaskOptions"
                  @update:model-value="tts.patch({ audiocppTask: $event })"
                />
              </label>
              <label class="num">
                <span class="num-name" :data-tip="activeTtsPathHint">{{ t('backstage.customPath') }}</span>
                <div class="path-pick-wrap">
                  <input class="num-in path" :value="tts.active.modelPath" :placeholder="activeTtsPathHint"
                    @change="tts.patch({ modelPath: ($event.target as HTMLInputElement).value })" />
                  <button
                    type="button"
                    class="mini-btn"
                    @click="openPathPicker({
                      title: t('backstage.customPath'),
                      ext: activeTtsSpec?.pathKind === 'dir' ? '' : '.gguf',
                      dirsOnly: activeTtsSpec?.pathKind === 'dir',
                      allowDir: activeTtsSpec?.pathKind !== 'file',
                      onSelect: (p) => tts.patch({ modelPath: p }),
                    })"
                  >{{ t('backstage.browse') }}</button>
                </div>
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.computeBackend') }}</span>
                <UiSelect
                  :model-value="tts.active.computeBackend || 'vulkan'"
                  :options="computeBackendOptions"
                  @update:model-value="tts.patch({ computeBackend: $event })"
                />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.voice') }}</span>
                <UiSelect
                  :model-value="tts.active.voiceName || ''"
                  :options="audiocppVoiceOptions"
                  @update:model-value="tts.patch({ voiceName: $event })"
                />
              </label>
              <label v-if="activeTtsSpec?.supportsVoiceId" class="num">
                <span class="num-name">{{ t('backstage.voiceId') }}</span>
                <input class="num-in" :value="tts.active.voiceId" placeholder="af_heart / US_Emma"
                  @change="tts.patch({ voiceId: ($event.target as HTMLInputElement).value })" />
              </label>
              <UiSlider
                :model-value="tts.active.speed ?? 1"
                :label="t('backstage.speed')"
                :max="2"
                @update:model-value="(v: number) => tts.patch({ speed: v })"
              />
              <label v-if="activeTtsSpec?.modes?.includes('streaming')" class="num">
                <span class="num-name">{{ t('backstage.sampleRate') }}</span>
                <input
                  class="num-in"
                  type="number"
                  :value="tts.active.sampleRate ?? ''"
                  :placeholder="String(activeTtsSpec.sampleRate ?? 24000)"
                  @change="tts.patch({ sampleRate: ($event.target as HTMLInputElement).value === '' ? undefined : Number(($event.target as HTMLInputElement).value) })"
                />
              </label>

              <template v-if="activeTtsSpec?.requestOptions?.length">
                <div class="sub-box-head" style="margin-top: 8px">{{ t('backstage.advancedOpts') }}</div>
                <label
                  v-for="opt in activeTtsSpec.requestOptions"
                  :key="`req-${opt.name}`"
                  class="num"
                >
                  <span class="num-name" :data-tip="opt.description || undefined">{{ opt.name }}</span>
                  <UiLever
                    v-if="opt.type === 'bool'"
                    :model-value="Boolean((tts.active.audiocppOptions?.[opt.name] ?? opt.default) ?? false)"
                    :label="''"
                    @update:model-value="patchAudioCppScopedOpt('request', opt.name, $event)"
                  />
                  <UiSelect
                    v-else-if="opt.type === 'enum' && opt.values?.length"
                    :model-value="String(tts.active.audiocppOptions?.[opt.name] ?? opt.default ?? '')"
                    :options="opt.values.map((v) => ({ value: String(v), label: String(v) }))"
                    @update:model-value="patchAudioCppScopedOpt('request', opt.name, $event)"
                  />
                  <div v-else-if="opt.type === 'path' || opt.type === 'audio_path'" class="path-pick-wrap">
                    <input
                      class="num-in path"
                      :value="tts.active.audiocppOptions?.[opt.name] ?? ''"
                      :placeholder="opt.default !== undefined && opt.default !== null ? String(opt.default) : ''"
                      @change="patchAudioCppScopedOpt('request', opt.name, ($event.target as HTMLInputElement).value)"
                    />
                    <button
                      type="button"
                      class="mini-btn"
                      @click="openPathPicker({
                        title: opt.name,
                        ext: opt.type === 'audio_path' ? '.wav' : '',
                        allowDir: opt.type === 'path',
                        onSelect: (p) => patchAudioCppScopedOpt('request', opt.name, p),
                      })"
                    >{{ t('backstage.browse') }}</button>
                  </div>
                  <input
                    v-else
                    class="num-in"
                    :type="opt.type === 'int' || opt.type === 'float' ? 'number' : 'text'"
                    :step="specOptStep(opt)"
                    :min="opt.min ?? undefined"
                    :max="opt.max ?? undefined"
                    :value="tts.active.audiocppOptions?.[opt.name] ?? ''"
                    :placeholder="opt.default !== undefined && opt.default !== null ? String(opt.default) : ''"
                    @change="patchAudioCppScopedOpt('request', opt.name, opt.type === 'int' || opt.type === 'float' ? (($event.target as HTMLInputElement).value === '' ? '' : Number(($event.target as HTMLInputElement).value)) : ($event.target as HTMLInputElement).value)"
                  />
                </label>
              </template>

              <template v-if="activeTtsSpec?.loadOptions?.length">
                <div class="sub-box-head" style="margin-top: 8px">{{ t('backstage.loadOpts') }}</div>
                <label
                  v-for="opt in activeTtsSpec.loadOptions"
                  :key="`load-${opt.name}`"
                  class="num"
                >
                  <span class="num-name" :data-tip="opt.description || undefined">{{ opt.name }}</span>
                  <UiLever
                    v-if="opt.type === 'bool'"
                    :model-value="Boolean((tts.active.audiocppLoadOptions?.[opt.name] ?? opt.default) ?? false)"
                    :label="''"
                    @update:model-value="patchAudioCppScopedOpt('load', opt.name, $event)"
                  />
                  <UiSelect
                    v-else-if="opt.type === 'enum' && opt.values?.length"
                    :model-value="String(tts.active.audiocppLoadOptions?.[opt.name] ?? opt.default ?? '')"
                    :options="opt.values.map((v) => ({ value: String(v), label: String(v) }))"
                    @update:model-value="patchAudioCppScopedOpt('load', opt.name, $event)"
                  />
                  <div v-else-if="opt.type === 'path' || opt.type === 'audio_path'" class="path-pick-wrap">
                    <input
                      class="num-in path"
                      :value="tts.active.audiocppLoadOptions?.[opt.name] ?? ''"
                      :placeholder="opt.default !== undefined && opt.default !== null ? String(opt.default) : ''"
                      @change="patchAudioCppScopedOpt('load', opt.name, ($event.target as HTMLInputElement).value)"
                    />
                    <button
                      type="button"
                      class="mini-btn"
                      @click="openPathPicker({
                        title: opt.name,
                        allowDir: true,
                        onSelect: (p) => patchAudioCppScopedOpt('load', opt.name, p),
                      })"
                    >{{ t('backstage.browse') }}</button>
                  </div>
                  <input
                    v-else
                    class="num-in"
                    :type="opt.type === 'int' || opt.type === 'float' ? 'number' : 'text'"
                    :step="specOptStep(opt)"
                    :min="opt.min ?? undefined"
                    :max="opt.max ?? undefined"
                    :value="tts.active.audiocppLoadOptions?.[opt.name] ?? ''"
                    :placeholder="opt.default !== undefined && opt.default !== null ? String(opt.default) : ''"
                    @change="patchAudioCppScopedOpt('load', opt.name, opt.type === 'int' || opt.type === 'float' ? (($event.target as HTMLInputElement).value === '' ? '' : Number(($event.target as HTMLInputElement).value)) : ($event.target as HTMLInputElement).value)"
                  />
                </label>
              </template>

              <template v-if="activeTtsSpec?.sessionOptions?.length">
                <div class="sub-box-head" style="margin-top: 8px">{{ t('backstage.sessionOpts') }}</div>
                <label
                  v-for="opt in activeTtsSpec.sessionOptions"
                  :key="`sess-${opt.name}`"
                  class="num"
                >
                  <span class="num-name" :data-tip="opt.description || undefined">{{ opt.name }}</span>
                  <UiLever
                    v-if="opt.type === 'bool'"
                    :model-value="Boolean((tts.active.audiocppSessionOptions?.[opt.name] ?? opt.default) ?? false)"
                    :label="''"
                    @update:model-value="patchAudioCppScopedOpt('session', opt.name, $event)"
                  />
                  <UiSelect
                    v-else-if="opt.type === 'enum' && opt.values?.length"
                    :model-value="String(tts.active.audiocppSessionOptions?.[opt.name] ?? opt.default ?? '')"
                    :options="opt.values.map((v) => ({ value: String(v), label: String(v) }))"
                    @update:model-value="patchAudioCppScopedOpt('session', opt.name, $event)"
                  />
                  <div v-else-if="opt.type === 'path' || opt.type === 'audio_path'" class="path-pick-wrap">
                    <input
                      class="num-in path"
                      :value="tts.active.audiocppSessionOptions?.[opt.name] ?? ''"
                      :placeholder="opt.default !== undefined && opt.default !== null ? String(opt.default) : ''"
                      @change="patchAudioCppScopedOpt('session', opt.name, ($event.target as HTMLInputElement).value)"
                    />
                    <button
                      type="button"
                      class="mini-btn"
                      @click="openPathPicker({
                        title: opt.name,
                        allowDir: true,
                        onSelect: (p) => patchAudioCppScopedOpt('session', opt.name, p),
                      })"
                    >{{ t('backstage.browse') }}</button>
                  </div>
                  <input
                    v-else
                    class="num-in"
                    :type="opt.type === 'int' || opt.type === 'float' ? 'number' : 'text'"
                    :step="specOptStep(opt)"
                    :min="opt.min ?? undefined"
                    :max="opt.max ?? undefined"
                    :value="tts.active.audiocppSessionOptions?.[opt.name] ?? ''"
                    :placeholder="opt.default !== undefined && opt.default !== null ? String(opt.default) : ''"
                    @change="patchAudioCppScopedOpt('session', opt.name, opt.type === 'int' || opt.type === 'float' ? (($event.target as HTMLInputElement).value === '' ? '' : Number(($event.target as HTMLInputElement).value)) : ($event.target as HTMLInputElement).value)"
                  />
                </label>
              </template>
            </template>

            <template v-else-if="tts.active.provider === 'openai_tts'">
              <label class="num">
                <span class="num-name">{{ t('backstage.displayName') }}</span>
                <input class="num-in" :value="tts.active.name" placeholder="Kokoro-FastAPI / GPT-SoVITS"
                  @change="tts.patch({ name: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num wide">
                <span class="num-name">{{ t('backstage.endpoint') }}</span>
                <input class="num-in path" :value="tts.active.baseUrl" placeholder="http://127.0.0.1:8880/v1"
                  @change="tts.patch({ baseUrl: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num wide">
                <span class="num-name">{{ t('backstage.apiKey') }}</span>
                <input class="num-in" type="password" :value="tts.active.apiKey" placeholder="(optional)"
                  @change="tts.patch({ apiKey: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.modelName') }}</span>
                <input class="num-in" :value="tts.active.model" placeholder="tts-1"
                  @change="tts.patch({ model: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.voiceId') }}</span>
                <input class="num-in" :value="tts.active.voiceId" placeholder="alloy"
                  @change="tts.patch({ voiceId: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.outputFormat') }}</span>
                <input class="num-in" :value="tts.active.outputFormat" placeholder="wav"
                  @change="tts.patch({ outputFormat: ($event.target as HTMLInputElement).value })" />
              </label>
              <UiSlider
                :model-value="tts.active.speed ?? 1"
                :label="t('backstage.speed')"
                :max="2"
                @update:model-value="(v: number) => tts.patch({ speed: v })"
              />
            </template>

            <template v-else-if="tts.active.provider === 'minimax'">
              <label class="num wide">
                <span class="num-name">{{ t('backstage.apiKey') }}</span>
                <input class="num-in" type="password" :value="tts.active.apiKey" placeholder="sk-..."
                  @change="tts.patch({ apiKey: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num wide">
                <span class="num-name">{{ t('backstage.endpoint') }}</span>
                <input class="num-in" :value="tts.active.baseUrl" placeholder="https://api.minimaxi.com/v1"
                  @change="tts.patch({ baseUrl: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.modelName') }}</span>
                <input class="num-in" :value="tts.active.model" placeholder="speech-2.8-turbo"
                  @change="tts.patch({ model: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.voiceId') }}</span>
                <input class="num-in" :value="tts.active.voiceId" placeholder="female-shaonv"
                  @change="tts.patch({ voiceId: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.emotion') }}</span>
                <input class="num-in" :value="tts.active.emotion" placeholder="calm"
                  @change="tts.patch({ emotion: ($event.target as HTMLInputElement).value })" />
              </label>
              <UiSlider
                :model-value="tts.active.speed ?? 1"
                :label="t('backstage.speed')"
                :max="2"
                @update:model-value="(v: number) => tts.patch({ speed: v })"
              />
            </template>

            <template v-else-if="tts.active.provider === 'elevenlabs'">
              <label class="num wide">
                <span class="num-name">{{ t('backstage.apiKey') }}</span>
                <input class="num-in" type="password" :value="tts.active.apiKey" placeholder="sk_..."
                  @change="tts.patch({ apiKey: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num wide">
                <span class="num-name">{{ t('backstage.endpoint') }}</span>
                <input class="num-in" :value="tts.active.baseUrl" placeholder="https://api.elevenlabs.io"
                  @change="tts.patch({ baseUrl: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.modelName') }}</span>
                <input class="num-in" :value="tts.active.model" placeholder="eleven_multilingual_v2"
                  @change="tts.patch({ model: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.voiceId') }}</span>
                <input class="num-in" :value="tts.active.voiceId" placeholder="21m00Tcm4TlvDq8ikWAM"
                  @change="tts.patch({ voiceId: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="num">
                <span class="num-name">{{ t('backstage.outputFormat') }}</span>
                <input class="num-in" :value="tts.active.outputFormat" placeholder="mp3_44100_128"
                  @change="tts.patch({ outputFormat: ($event.target as HTMLInputElement).value })" />
              </label>
            </template>
          </div>

          <div
            v-if="['omnivoice', 'dotstts', 'fishaudio', 'ming3tts'].includes(tts.active.provider)"
            class="faders"
            style="padding-top: 0"
          >
            <label class="num">
              <span class="num-name">{{ t('backstage.customPath') }}</span>
              <div class="path-pick-wrap">
                <input
                  class="num-in path"
                  :value="tts.active.modelPath"
                  placeholder="(default under modelsRoot)"
                  @change="tts.patch({ modelPath: ($event.target as HTMLInputElement).value })"
                />
                <button
                  type="button"
                  class="mini-btn"
                  @click="openPathPicker({
                    title: t('backstage.customPath'),
                    ext: '.gguf',
                    onSelect: (p) => tts.patch({ modelPath: p }),
                  })"
                >{{ t('backstage.browse') }}</button>
              </div>
            </label>
            <label class="num">
              <span class="num-name">{{ t('backstage.computeBackend') }}</span>
              <UiSelect
                :model-value="tts.active.computeBackend || 'vulkan'"
                :options="computeBackendOptions"
                @update:model-value="tts.patch({ computeBackend: $event })"
              />
            </label>
          </div>
          <div class="levers" style="padding-top: 0">
            <UiLever
              v-if="supportsStreaming(tts.active, tts.specs)"
              :model-value="tts.active.streamEnabled !== false"
              :label="t('backstage.streaming')"
              @update:model-value="tts.patch({ streamEnabled: $event })"
            />
            <UiLever
              :model-value="tts.active.speakStageDirections !== false"
              :label="t('backstage.stageDirections')"
              @update:model-value="tts.patch({ speakStageDirections: $event })"
            />
          </div>
        </template>

        <div v-if="tts.active" class="audition">
          <input
            v-model="auditionText"
            class="audition-in"
            :placeholder="t('backstage.auditionHint')"
            @keydown.enter.prevent="toggleAudition"
          />
          <button class="audition-btn" :class="{ on: auditionPlaying }" @click="toggleAudition">
            {{
              auditionLoading
                ? t('backstage.auditionLoading')
                : auditionPlaying
                  ? t('backstage.auditionStop')
                  : t('backstage.auditionPlay')
            }}
          </button>
        </div>
        <div v-if="auditionError" class="path-err">{{ auditionError }}</div>

        <div class="desk-foot">
          <button class="mini-btn" :disabled="!tts.active" @click="askRemoveConfig">
            {{ t('backstage.removeEngine') }}
          </button>
          <button class="mini-btn danger" :disabled="tts.unloading" @click="onUnload">
            {{ t('backstage.unload') }}
          </button>
          <span v-if="unloadNote" class="unload-note">{{ unloadNote }}</span>
        </div>
      </div>

      <!-- Ears: engine picker + custom local GGUF / HTTP ASR -->
      <div class="tab-pane" :class="{ on: tab === 'asr' }">
        <div class="stt-head">
          <span class="stt-kicker">{{ t('backstage.sttActive') }}</span>
          <div class="stt-title-row">
            <span class="stt-current">{{ stt.activeLabel }}</span>
            <span
              class="stt-state"
              :class="{ ok: stt.activeEngine?.loaded, err: stt.activeEngine?.error }"
            >
              {{ sttStateLabel(stt.activeEngine) }}
            </span>
          </div>
          <span class="stt-desc">{{ stt.activeEngine?.desc }}</span>
        </div>

        <div v-if="stt.engines.length" class="stt-list">
          <div
            v-for="e in stt.engines"
            :key="e.id"
            class="stt-row"
            :class="{ on: e.id === stt.active }"
            @click="stt.setActive(e.id)"
          >
            <span class="stt-dot" :class="{ ok: e.loaded, err: e.error }"></span>
            <span class="stt-main">
              <span class="stt-name">{{ e.label }}</span>
              <span class="stt-sub">{{ e.desc }}</span>
            </span>
            <span
              v-if="e.kind !== 'openai_stt'"
              class="stt-state"
              :class="{ ok: e.modelExists, err: e.modelExists === false }"
            >
              {{ e.modelExists ? t('backstage.modelReady') : t('backstage.modelMissing') }}
            </span>
            <button
              type="button"
              class="mini-btn"
              @click.stop="openEditSttForm(e)"
            >{{ t('backstage.editModel') }}</button>
          </div>
        </div>

        <div v-if="!stt.engines.length" class="empty-block">
          <div class="eb-title">{{ stt.loading ? t('backstage.sttLoading') : t('empty.noCast') }}</div>
          <div v-if="stt.lastError" class="eb-hint">{{ stt.lastError }}</div>
        </div>

        <div v-if="sttFormOpen" class="sub-box" style="margin-top: 12px">
          <div class="sub-box-head">{{ sttForm.id ? sttForm.label || sttForm.id : t('backstage.addAsr') }}</div>
          <label v-if="sttForm.kind !== 'builtin'" class="num">
            <span class="num-name">{{ t('backstage.providerType') }}</span>
            <UiSelect
              :model-value="sttForm.kind"
              :options="sttKindOptions"
              @update:model-value="sttForm.kind = $event as 'audiocpp' | 'openai_stt'"
            />
          </label>
          <label v-if="sttForm.kind !== 'builtin'" class="num">
            <span class="num-name">{{ t('backstage.displayName') }}</span>
            <input v-model="sttForm.label" class="num-in" placeholder="SenseVoice / Whisper" />
          </label>
          <template v-if="sttForm.kind === 'audiocpp' || sttForm.kind === 'builtin'">
            <label v-if="sttForm.kind === 'audiocpp'" class="num">
              <span class="num-name">{{ t('backstage.modelFamily') }}</span>
              <UiSelect
                :model-value="sttForm.family"
                :options="sttFamilyOptions"
                @update:model-value="onChangeSttFamily"
              />
            </label>
            <label class="num">
              <span class="num-name">{{ t('backstage.customPath') }}</span>
              <div class="path-pick-wrap">
                <input
                  v-model="sttForm.modelPath"
                  class="num-in path"
                  :placeholder="sttForm.kind === 'builtin' ? '(default under modelsRoot)' : 'D:\\models\\asr.gguf'"
                />
                <button
                  type="button"
                  class="mini-btn"
                  @click="openPathPicker({
                    title: t('backstage.customPath'),
                    ext: sttForm.kind === 'audiocpp' && sttSpec?.pathKind === 'dir' ? '' : '.gguf',
                    dirsOnly: sttForm.kind === 'audiocpp' && sttSpec?.pathKind === 'dir',
                    allowDir: sttForm.kind === 'audiocpp' && sttSpec?.pathKind !== 'file',
                    onSelect: (p) => { sttForm.modelPath = p },
                  })"
                >{{ t('backstage.browse') }}</button>
              </div>
            </label>
            <label class="num">
              <span class="num-name">{{ t('backstage.computeBackend') }}</span>
              <UiSelect
                :model-value="sttForm.computeBackend"
                :options="computeBackendOptions"
                @update:model-value="sttForm.computeBackend = $event"
              />
            </label>
            <label v-if="sttForm.kind === 'audiocpp'" class="num">
              <span class="num-name">{{ t('backstage.language') }}</span>
              <input v-model="sttForm.language" class="num-in" placeholder="auto / zh / en" />
            </label>
            <template v-if="sttForm.kind === 'audiocpp' && sttSpec?.requestOptions?.length">
              <div class="sub-box-head" style="margin-top: 8px">{{ t('backstage.advancedOpts') }}</div>
              <label
                v-for="opt in sttSpec.requestOptions"
                :key="`stt-req-${opt.name}`"
                class="num"
              >
                <span class="num-name" :data-tip="opt.description || undefined">{{ opt.name }}</span>
                <UiLever
                  v-if="opt.type === 'bool'"
                  :model-value="Boolean((sttForm.requestOptions[opt.name] ?? opt.default) ?? false)"
                  :label="''"
                  @update:model-value="patchSttScopedOpt('request', opt.name, $event)"
                />
                <UiSelect
                  v-else-if="opt.type === 'enum' && opt.values?.length"
                  :model-value="String(sttForm.requestOptions[opt.name] ?? opt.default ?? '')"
                  :options="opt.values.map((v) => ({ value: String(v), label: String(v) }))"
                  @update:model-value="patchSttScopedOpt('request', opt.name, $event)"
                />
                <input
                  v-else
                  class="num-in"
                  :type="opt.type === 'int' || opt.type === 'float' ? 'number' : 'text'"
                  :step="specOptStep(opt)"
                  :min="opt.min ?? undefined"
                  :max="opt.max ?? undefined"
                  :value="sttForm.requestOptions[opt.name] ?? ''"
                  :placeholder="opt.default !== undefined && opt.default !== null ? String(opt.default) : ''"
                  @change="patchSttScopedOpt('request', opt.name, opt.type === 'int' || opt.type === 'float' ? (($event.target as HTMLInputElement).value === '' ? '' : Number(($event.target as HTMLInputElement).value)) : ($event.target as HTMLInputElement).value)"
                />
              </label>
            </template>
            <template v-if="sttForm.kind === 'audiocpp' && sttSpec?.loadOptions?.length">
              <div class="sub-box-head" style="margin-top: 8px">{{ t('backstage.loadOpts') }}</div>
              <label
                v-for="opt in sttSpec.loadOptions"
                :key="`stt-load-${opt.name}`"
                class="num"
              >
                <span class="num-name" :data-tip="opt.description || undefined">{{ opt.name }}</span>
                <UiLever
                  v-if="opt.type === 'bool'"
                  :model-value="Boolean((sttForm.loadOptions[opt.name] ?? opt.default) ?? false)"
                  :label="''"
                  @update:model-value="patchSttScopedOpt('load', opt.name, $event)"
                />
                <UiSelect
                  v-else-if="opt.type === 'enum' && opt.values?.length"
                  :model-value="String(sttForm.loadOptions[opt.name] ?? opt.default ?? '')"
                  :options="opt.values.map((v) => ({ value: String(v), label: String(v) }))"
                  @update:model-value="patchSttScopedOpt('load', opt.name, $event)"
                />
                <input
                  v-else
                  class="num-in"
                  :type="opt.type === 'int' || opt.type === 'float' ? 'number' : 'text'"
                  :step="specOptStep(opt)"
                  :min="opt.min ?? undefined"
                  :max="opt.max ?? undefined"
                  :value="sttForm.loadOptions[opt.name] ?? ''"
                  :placeholder="opt.default !== undefined && opt.default !== null ? String(opt.default) : ''"
                  @change="patchSttScopedOpt('load', opt.name, opt.type === 'int' || opt.type === 'float' ? (($event.target as HTMLInputElement).value === '' ? '' : Number(($event.target as HTMLInputElement).value)) : ($event.target as HTMLInputElement).value)"
                />
              </label>
            </template>
            <template v-if="sttForm.kind === 'audiocpp' && sttSpec?.sessionOptions?.length">
              <div class="sub-box-head" style="margin-top: 8px">{{ t('backstage.sessionOpts') }}</div>
              <label
                v-for="opt in sttSpec.sessionOptions"
                :key="`stt-sess-${opt.name}`"
                class="num"
              >
                <span class="num-name" :data-tip="opt.description || undefined">{{ opt.name }}</span>
                <UiLever
                  v-if="opt.type === 'bool'"
                  :model-value="Boolean((sttForm.sessionOptions[opt.name] ?? opt.default) ?? false)"
                  :label="''"
                  @update:model-value="patchSttScopedOpt('session', opt.name, $event)"
                />
                <UiSelect
                  v-else-if="opt.type === 'enum' && opt.values?.length"
                  :model-value="String(sttForm.sessionOptions[opt.name] ?? opt.default ?? '')"
                  :options="opt.values.map((v) => ({ value: String(v), label: String(v) }))"
                  @update:model-value="patchSttScopedOpt('session', opt.name, $event)"
                />
                <div v-else-if="opt.type === 'path' || opt.type === 'audio_path'" class="path-pick-wrap">
                  <input
                    class="num-in path"
                    :value="sttForm.sessionOptions[opt.name] ?? ''"
                    :placeholder="opt.default !== undefined && opt.default !== null ? String(opt.default) : ''"
                    @change="patchSttScopedOpt('session', opt.name, ($event.target as HTMLInputElement).value)"
                  />
                  <button
                    type="button"
                    class="mini-btn"
                    @click="openPathPicker({
                      title: opt.name,
                      allowDir: true,
                      onSelect: (p) => patchSttScopedOpt('session', opt.name, p),
                    })"
                  >{{ t('backstage.browse') }}</button>
                </div>
                <input
                  v-else
                  class="num-in"
                  :type="opt.type === 'int' || opt.type === 'float' ? 'number' : 'text'"
                  :step="specOptStep(opt)"
                  :min="opt.min ?? undefined"
                  :max="opt.max ?? undefined"
                  :value="sttForm.sessionOptions[opt.name] ?? ''"
                  :placeholder="opt.default !== undefined && opt.default !== null ? String(opt.default) : ''"
                  @change="patchSttScopedOpt('session', opt.name, opt.type === 'int' || opt.type === 'float' ? (($event.target as HTMLInputElement).value === '' ? '' : Number(($event.target as HTMLInputElement).value)) : ($event.target as HTMLInputElement).value)"
                />
              </label>
            </template>
          </template>
          <template v-else>
            <label class="num wide">
              <span class="num-name">{{ t('backstage.endpoint') }}</span>
              <input v-model="sttForm.baseUrl" class="num-in path" placeholder="http://127.0.0.1:8081/v1" />
            </label>
            <label class="num wide">
              <span class="num-name">{{ t('backstage.apiKey') }}</span>
              <input v-model="sttForm.apiKey" class="num-in" type="password" placeholder="(optional)" />
            </label>
            <label class="num">
              <span class="num-name">{{ t('backstage.modelName') }}</span>
              <input v-model="sttForm.model" class="num-in" placeholder="whisper-1" />
            </label>
            <label class="num">
              <span class="num-name">{{ t('backstage.language') }}</span>
              <input v-model="sttForm.language" class="num-in" placeholder="zh / en" />
            </label>
          </template>
          <div v-if="sttFormError" class="path-err">{{ sttFormError }}</div>
          <div class="path-row">
            <button class="mini-btn primary" @click="saveSttForm">{{ t('backstage.saveModel') }}</button>
            <button class="mini-btn" @click="sttFormOpen = false">{{ t('dialog.cancel') }}</button>
            <button
              v-if="sttForm.id && sttForm.kind !== 'builtin'"
              class="mini-btn danger"
              @click="onDeleteSttEngine(sttForm.id)"
            >{{ t('backstage.deleteModel') }}</button>
          </div>
        </div>

        <div class="stt-foot">
          <button class="mini-btn" @click="openNewSttForm">{{ t('backstage.addAsr') }}</button>
          <button class="mini-btn danger" :disabled="stt.stopping" @click="onStopStt">
            {{ t('backstage.sttStop') }}
          </button>
          <span v-if="sttStopNote" class="unload-note">{{ sttStopNote }}</span>
        </div>

        <div class="faders" style="margin-top: 14px">
          <UiLever
            :model-value="videoAutoStt"
            :label="t('backstage.videoAutoStt')"
            @update:model-value="onToggleVideoAutoStt"
          />
          <div class="path-hint">{{ t('backstage.videoAutoSttHint') }}</div>
        </div>

        <div class="stt-note">{{ t('backstage.sttNote') }}</div>
      </div>

      <!-- Paths -->
      <div class="tab-pane" :class="{ on: tab === 'paths' }">
        <div class="faders">
          <label class="num">
            <span class="num-name">{{ t('backstage.modelsRoot') }}</span>
            <div class="path-pick-wrap">
              <input
                class="num-in path"
                :value="modelsRoot"
                :placeholder="resolvedModelsRoot"
                @input="modelsRoot = ($event.target as HTMLInputElement).value"
              />
              <button
                type="button"
                class="mini-btn"
                @click="openPathPicker({
                  title: t('backstage.modelsRoot'),
                  initialPath: modelsRoot || resolvedModelsRoot,
                  dirsOnly: true,
                  onSelect: (p) => { modelsRoot = p },
                })"
              >{{ t('backstage.browse') }}</button>
            </div>
          </label>
          <div class="path-hint">{{ t('backstage.modelsRootHint') }}</div>

          <label class="num">
            <span class="num-name">{{ t('backstage.llamaRoot') }}</span>
            <div class="path-pick-wrap">
              <input
                class="num-in path"
                :value="configuredLlamaRoot"
                :placeholder="llamaRoot"
                @input="configuredLlamaRoot = ($event.target as HTMLInputElement).value"
              />
              <button
                type="button"
                class="mini-btn"
                @click="openPathPicker({
                  title: t('backstage.llamaRoot'),
                  initialPath: configuredLlamaRoot || llamaRoot,
                  ext: hostExeExt,
                  allowDir: true,
                  onSelect: (p) => { configuredLlamaRoot = p },
                })"
              >{{ t('backstage.browse') }}</button>
            </div>
          </label>
          <div class="path-hint">{{ t('backstage.llamaRootHint') }}</div>

          <label class="num">
            <span class="num-name">{{ t('backstage.inochiPython') }}</span>
            <div class="path-pick-wrap">
              <input
                class="num-in path"
                :value="configuredInochiPython"
                :placeholder="inochiEnv?.pythonPath || inochiEnv?.expectedVenvDir || '~/.pebblecompanion/inochi2d-venv'"
                @input="configuredInochiPython = ($event.target as HTMLInputElement).value"
              />
              <button
                type="button"
                class="mini-btn"
                @click="openPathPicker({
                  title: t('backstage.inochiPython'),
                  initialPath: configuredInochiPython || inochiEnv?.pythonPath || '',
                  allowDir: true,
                  onSelect: (p) => { configuredInochiPython = p },
                })"
              >{{ t('backstage.browse') }}</button>
            </div>
          </label>
          <div class="path-hint">{{ t('backstage.inochiPythonHint') }}</div>

          <label class="num">
            <span class="num-name">{{ t('backstage.ffmpegPath') }}</span>
            <div class="path-pick-wrap">
              <input
                class="num-in path"
                :value="configuredFfmpegPath"
                :placeholder="ffmpegPath || `ffmpeg${hostExeExt}`"
                @input="configuredFfmpegPath = ($event.target as HTMLInputElement).value"
              />
              <button
                type="button"
                class="mini-btn"
                @click="openPathPicker({
                  title: t('backstage.ffmpegPath'),
                  initialPath: configuredFfmpegPath || ffmpegPath,
                  ext: hostExeExt,
                  allowDir: true,
                  onSelect: (p) => { configuredFfmpegPath = p },
                })"
              >{{ t('backstage.browse') }}</button>
            </div>
          </label>
          <div class="path-hint">{{ t('backstage.ffmpegHint') }}</div>
        </div>
        <div class="path-row">
          <button class="mini-btn primary" @click="applyModelsRoot">{{ t('backstage.apply') }}</button>
          <button class="mini-btn" type="button" @click="loadPaths(true)">{{ t('backstage.recheck') }}</button>
          <span v-if="modelsSaved" class="path-ok">{{ t('backstage.modelsSaved') }}</span>
        </div>
        <div v-if="modelsRootError" class="path-err">{{ modelsRootError }}</div>
        <div class="faders">
          <label class="num">
            <span class="num-name">{{ t('backstage.effective') }}</span>
            <span class="path-v">{{ resolvedModelsRoot }}</span>
          </label>
          <label class="num">
            <span class="num-name">{{ t('backstage.llamaRoot') }}</span>
            <span class="path-v" :class="{ warn: !llamaFound }">{{ llamaRoot }}</span>
          </label>
          <label class="num">
            <span class="num-name">{{ t('backstage.audioCppStatus') }}</span>
            <span class="path-v" :class="{ warn: !audioCppFound }">
              {{ audioCppFound ? t('backstage.audioCppReady') : t('backstage.audioCppMissing') }} · {{ audioCppDir }}
            </span>
          </label>
          <label class="num">
            <span class="num-name">{{ t('backstage.ffmpegPath') }}</span>
            <span class="path-v" :class="{ warn: !ffmpegFound }">
              {{ ffmpegFound ? `${t('backstage.ffmpegReady')} · ${ffmpegPath}` : t('backstage.ffmpegMissing') }}
            </span>
          </label>
          <label v-if="inochiEnv" class="num">
            <span class="num-name">{{ t('backstage.inochiRuntimeStatus') }}</span>
            <span class="path-v" :class="{ warn: !inochiEnv.pythonPath || (inochiEnv.missingDeps?.length ?? 0) > 0 }">
              <template v-if="inochiEnv.pythonPath && !(inochiEnv.missingDeps?.length)">
                {{ inochiEnv.runtime?.gpuName || inochiEnv.runtime?.backend?.toUpperCase() || 'OK' }}
                <template v-if="inochiEnv.runtime?.vramGb"> · {{ inochiEnv.runtime.vramGb }}GB</template>
                · {{ inochiEnv.runtime?.backend }} / {{ inochiEnv.runtime?.dtype }}
              </template>
              <template v-else-if="inochiEnv.pythonPath">
                {{ t('backstage.inochiDepsMissing') }}: {{ inochiEnv.missingDeps?.join(', ') }}
              </template>
              <template v-else>
                {{ t('backstage.inochiVenvMissing') }}
              </template>
            </span>
          </label>
          <label v-if="inochiEnv" class="num">
            <span class="num-name">LayerDiff3D</span>
            <span class="path-v" :class="{ warn: !inochiEnv.layerdiffPath }">
              {{ inochiEnv.layerdiffPath || `${t('backstage.weightExpected')}: ${inochiEnv.expectedLayerdiffDir}` }}
            </span>
          </label>
          <label v-if="inochiEnv" class="num">
            <span class="num-name">Marigold</span>
            <span class="path-v" :class="{ warn: !inochiEnv.marigoldPath }">
              {{ inochiEnv.marigoldPath || `${t('backstage.weightExpected')}: ${inochiEnv.expectedMarigoldDir}` }}
            </span>
          </label>
        </div>
        <div v-if="inochiEnv && !inochiEnv.ready" class="path-hint">
          {{ t('backstage.inochiInstallTip', { req: inochiEnv.requirementsPath || 'backend/Inochi2D/requirements.txt' }) }}
          <div class="inochi-dl-links">
            <a href="https://huggingface.co/layerdifforg/seethroughv0.0.2_layerdiff3d" target="_blank" rel="noopener noreferrer">LayerDiff3D (HuggingFace)</a>
            <span>·</span>
            <a href="https://huggingface.co/24yearsold/seethroughv0.0.1_marigold" target="_blank" rel="noopener noreferrer">Marigold Depth (HuggingFace)</a>
          </div>
        </div>
        <div class="path-note">{{ t('backstage.modelsRestart') }}</div>
      </div>

      <!-- Pictures -->
      <div class="tab-pane" :class="{ on: tab === 'pictures' }">
        <div class="faders">
          <UiLever v-model="pictureForm.enabled" :label="t('picture.master')" />
          <UiLever v-model="pictureForm.autoChat" :label="t('picture.autoChat')" />
          <UiLever v-model="pictureForm.autoScript" :label="t('picture.autoScript')" />
        </div>

        <div class="faders">
          <label class="num">
            <span class="num-name">{{ t('picture.dailyLimit') }}</span>
            <input
              class="num-in"
              type="number"
              min="0"
              max="1000"
              v-model.number="pictureForm.dailyLimit"
            />
          </label>
          <label class="num">
            <span class="num-name">{{ t('picture.cooldown') }}</span>
            <input
              class="num-in"
              type="number"
              min="0"
              max="86400"
              v-model.number="pictureForm.cooldownSeconds"
            />
          </label>
          <label class="num">
            <span class="num-name">{{ t('picture.size') }}</span>
            <UiSelect
              :model-value="pictureForm.size"
              :options="sizeOptions"
              @update:model-value="pictureForm.size = $event"
            />
          </label>
          <label class="num">
            <span class="num-name">{{ t('picture.activeProvider') }}</span>
            <UiSelect
              :model-value="pictureForm.activeProvider"
              :options="providerOptions"
              @update:model-value="pictureForm.activeProvider = $event"
            />
          </label>
          <label class="num">
            <span class="num-name">{{ t('picture.promptModel') }}</span>
            <UiSelect
              :model-value="promptModelSelection"
              :options="promptModelOptions"
              @update:model-value="pictureForm.promptProviderId = $event"
            />
          </label>
          <label class="num">
            <span class="num-name">{{ t('picture.promptStyle') }}</span>
            <UiSelect
              :model-value="pictureForm.promptStyle || 'auto'"
              :options="promptStyleOptions"
              @update:model-value="pictureForm.promptStyle = $event"
            />
          </label>
        </div>

        <div class="faders">
          <label class="num">
            <span class="num-name">{{ t('picture.style') }}</span>
            <textarea
              class="pf-input pic-area"
              rows="2"
              v-model="pictureForm.stylePrompt"
              :placeholder="t('picture.styleHint')"
            ></textarea>
          </label>
          <label class="num">
            <span class="num-name">{{ t('picture.negative') }}</span>
            <textarea
              class="pf-input pic-area"
              rows="2"
              v-model="pictureForm.negativePrompt"
            ></textarea>
          </label>
        </div>

        <div class="pic-note">{{ t('picture.providerHint') }}</div>
        <div class="pv-tabs pic-tabs">
          <button
            v-for="(meta, id) in pictureForm.catalogue"
            :key="id"
            class="pv-tab pic-provider"
            :class="{
              on: pictureTab === String(id),
              ready: providerReady(String(id)),
              default: pictureForm.activeProvider === String(id),
            }"
            @click="selectPictureProvider(String(id))"
          >
            <span class="pic-dot"></span>
            <span>{{ meta.label }}</span>
            <span v-if="pictureForm.activeProvider === String(id)" class="pic-badge">
              {{ t('picture.active') }}
            </span>
          </button>
        </div>

        <div v-if="pictureProvider" class="faders pic-params">
          <div v-if="pictureProviderMeta?.note || pictureProviderMeta?.free" class="pic-note">
            <span v-if="pictureProviderMeta?.note">{{ pictureProviderMeta.note }}</span>
            <span v-if="pictureProviderMeta?.free" class="pic-free">{{ t('picture.free') }}</span>
          </div>
          <UiLever v-model="pictureProvider.enabled" :label="t('picture.enabled')" />

          <div v-if="isComfyTab" class="comfy-box">
            <div class="comfy-state" :class="{ on: comfyConnected }">
              <span class="comfy-dot"></span>
              <span class="comfy-name">
                {{
                  comfyChecking
                    ? t('picture.comfyChecking')
                    : comfyConnected
                      ? t('picture.comfyConnected')
                      : t('picture.comfyOffline')
                }}
              </span>
              <span
                v-if="comfyConnected && comfyStatus?.system?.comfyuiVersion"
                class="comfy-ver"
              >
                {{ comfyStatus.system.comfyuiVersion }}
              </span>
              <button class="comfy-retry" type="button" @click="checkComfy()">
                {{ t('picture.comfyRetry') }}
              </button>
            </div>
            <p v-if="comfyError || comfyStatus?.error" class="comfy-err">
              {{ comfyError || comfyStatus?.error }}
            </p>
            <p v-else-if="comfyConnected && comfyStatus?.device" class="comfy-device">
              {{ comfyStatus.device }}
            </p>
            <p
              v-if="
                comfyConnected &&
                !comfyStatus?.models?.checkpoints?.length &&
                !comfyStatus?.models?.unets?.length
              "
              class="comfy-err"
            >
              {{ t('picture.comfyNoModels') }}
            </p>
            <p
              v-else-if="
                comfyConnected &&
                (!comfyWorkflowId || isBuiltinWorkflow(comfyWorkflowId)) &&
                !String(pictureProvider?.params?.ckpt || '')
              "
              class="comfy-err"
            >
              {{ t('picture.comfyNeedCkpt') }}
            </p>
            <div class="comfy-actions">
              <label class="comfy-import">
                <input type="file" accept=".json,application/json" @change="importComfyFile" />
                <span>{{ t('picture.comfyImport') }}</span>
              </label>
              <button
                v-if="canDeleteWorkflow(comfyWorkflowId)"
                class="comfy-del"
                type="button"
                @click="removeComfyWorkflow(comfyWorkflowId)"
              >
                {{ t('picture.comfyDelete') }}
              </button>
            </div>
            <p v-if="comfyWorkflowNote" class="comfy-device">
              {{ comfyWorkflowNote }}
            </p>
          </div>
          <label v-if="pictureProviderMeta?.fields.includes('baseUrl')" class="num">
            <span class="num-name">{{ t('picture.baseUrl') }}</span>
            <input
              class="num-in path"
              type="text"
              spellcheck="false"
              v-model="pictureProvider.baseUrl"
            />
          </label>
          <label v-if="pictureProviderMeta?.fields.includes('accountId')" class="num">
            <span class="num-name">{{ t('picture.accountId') }}</span>
            <input class="num-in" type="text" v-model="pictureProvider.accountId" />
          </label>
          <label class="num">
            <span
              class="num-name"
              :data-tip="pictureTab === 'comfyui' ? t('picture.comfyNoKeyNeeded') : undefined"
              >{{ t('picture.apiKey') }}</span
            >
            <input class="num-in" type="password" v-model="pictureProvider.apiKey" />
          </label>
          <label v-if="pictureTab !== 'comfyui'" class="num">
            <span class="num-name">{{ t('picture.model') }}</span>
            <input class="num-in" type="text" v-model="pictureProvider.model" />
          </label>

          <label
            v-for="meta in visiblePictureParams"
            :key="meta.key"
            class="num"
          >
            <span class="num-name" :data-tip="paramTip(meta) || undefined">{{ paramLabel(meta) }}</span>
            <UiLever
              v-if="meta.type === 'bool'"
              :model-value="paramValue(meta) === 'true'"
              :label="''"
              @update:model-value="setParamBool(meta, $event)"
            />
            <UiSelect
              v-else-if="meta.type === 'select'"
              :model-value="paramValue(meta)"
              :options="paramSelectOptions(meta)"
              @update:model-value="setParam(meta, $event)"
            />
            <input
              v-else
              class="num-in path"
              :type="meta.type === 'number' ? 'number' : 'text'"
              :value="paramValue(meta)"
              :min="meta.min"
              :max="meta.max"
              :step="meta.step"
              @input="setParam(meta, ($event.target as HTMLInputElement).value)"
            />
          </label>
        </div>

        <div class="path-row">
          <button class="mini-btn primary" @click="savePictures">{{ t('picture.save') }}</button>
          <span v-if="pictureSaved" class="path-ok">{{ t('picture.saved') }}</span>
          <span v-if="pictureDirty" class="pic-dirty">{{ t('picture.unsaved') }}</span>
          <span class="pic-usage">
            {{ t('picture.usageToday') }} {{ pictures.usage?.today ?? 0 }} /
            {{ pictureForm.dailyLimit }}
          </span>
        </div>
        <div v-if="pictureSaveError" class="path-err">{{ pictureSaveError }}</div>
        <div v-if="pictureWarning" class="path-err">{{ pictureWarning }}</div>
        <div class="path-note">{{ t('picture.hint') }}</div>
      </div>
    </div>

    <PathPickerModal
      :open="pickerOpen"
      :title="pickerTitle"
      :initial-path="pickerInitial"
      :ext="pickerExt"
      :dirs-only="pickerDirsOnly"
      :allow-dir="pickerAllowDir"
      @close="pickerOpen = false"
      @select="handlePickerSelect"
    />
  </aside>
</template>

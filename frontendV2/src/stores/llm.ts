import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  DEFAULT_PARAMS,
  PROVIDER_TYPES,
  deleteLocalModel,
  getLlmConfig,
  localDevices,
  localModels,
  localStart,
  localStatus,
  localRelease,
  localStop,
  probeProviderModels,
  saveLlmConfig,
  saveLocalModel,
  scanLocalGgufModels,
  type LlmProvider,
  type LocalModelInfo,
  type LocalModelSavePayload,
  type ModelParams,
  type ScannedGgufModel,
} from '@/api/llm'

const ACTIVE_KEY = 'llmActiveIndex'
const ACTIVE_ID_KEY = 'llmActiveProviderId'
const MODEL_KEY = 'llmSelectedModel'
const LOCAL_MODEL_KEY = 'llmLocalModel'
const SYNC_DEBOUNCE_MS = 600
const RETRY_DELAYS_MS = [3000, 10000, 30000]

export type LlmSyncState = 'idle' | 'pending' | 'saving' | 'saved' | 'error'

const read = (k: string): string => {
  try {
    return localStorage.getItem(k) ?? ''
  } catch {
    return ''
  }
}

const write = (k: string, v: string): void => {
  try {
    localStorage.setItem(k, v)
  } catch {
    /* storage disabled */
  }
}

const newId = (): string => Date.now().toString(36) + Math.random().toString(36).slice(2, 10)

const flat = (p: LlmProvider): Partial<ModelParams> => ({
  temperature: p.temperature,
  maxTokens: p.maxTokens,
  maxContext: p.maxContext,
  topP: p.topP,
  topK: p.topK,
  minP: p.minP,
  repeatPenalty: p.repeatPenalty,
  frequencyPenalty: p.frequencyPenalty,
  presencePenalty: p.presencePenalty,
  enableMtp: p.enableMtp,
})

/**
 * LLM configuration.
 *
 * Parameters belong to a model, not to the app: `modelParams[model]` holds one
 * set per model, so switching models swaps the whole set. The flat fields on the
 * provider stay in sync because the backend streams them as a fallback.
 */
export const useLlmStore = defineStore('llm', () => {
  const providers = ref<LlmProvider[]>([])
  const activeIndex = ref(Number(read(ACTIVE_KEY)) || 0)
  const selectedModel = ref(read(MODEL_KEY))
  /**
   * The local weights the user picked. Deliberately separate from the one being
   * served: clicking a weight selects it for configuration, START loads it.
   */
  const selectedLocalModel = ref(read(LOCAL_MODEL_KEY))
  const loading = ref(false)
  const lastError = ref('')
  const syncState = ref<LlmSyncState>('idle')

  const localRunning = ref(false)
  const localModelKey = ref('')
  const localModelList = ref<LocalModelInfo[]>([])
  const localDeviceList = ref<{ id: string; name: string; vram_mib?: string; free_mib?: string }[]>([])
  const localBusy = ref(false)

  let saveTimer: number | null = null
  const retryTimers: number[] = []

  const currentProvider = computed<LlmProvider | null>(
    () => providers.value[activeIndex.value] ?? null,
  )

  const isLocal = computed(() => currentProvider.value?.provider === 'llama-cpp')

  /**
   * The local weights the panel is talking about: what the user selected when it
   * still exists, otherwise what is loaded, otherwise the first weights. This is
   * for display and parameter lookup — the request itself keeps using the model
   * that is actually running.
   */
  const localSelection = computed(() => {
    const p = currentProvider.value
    if (!p) return ''
    const known = p.models ?? []
    const picked = selectedLocalModel.value
    const exists =
      Boolean(picked) &&
      (known.includes(picked) || localModelList.value.some((m) => m.key === picked))
    if (exists) return picked
    return p.localModelKey || known[0] || ''
  })

  /** Key that identifies the model whose parameters are showing. */
  const paramKey = computed(() => {
    const p = currentProvider.value
    if (!p) return ''
    return isLocal.value ? localSelection.value : selectedModel.value
  })

  const activeParams = computed<ModelParams>(() => {
    const p = currentProvider.value
    if (!p) return { ...DEFAULT_PARAMS }
    const per = paramKey.value ? p.modelParams?.[paramKey.value] : undefined
    return { ...DEFAULT_PARAMS, ...flat(p), ...(per ?? {}) }
  })

  /** The id the backend must receive: a weights key locally, a model name remotely. */
  const requestModel = computed(() => {
    const p = currentProvider.value
    if (!p) return ''
    if (p.provider === 'llama-cpp') return p.localModelKey || localModelKey.value || ''
    return selectedModel.value || p.models[0] || ''
  })

  /** What the UI shows: the human label for a local weight, the model name otherwise. */
  const effectiveModel = computed(() => {
    const p = currentProvider.value
    if (!p) return ''
    if (p.provider === 'llama-cpp') {
      const key = localSelection.value
      const hit = localModelList.value.find((m) => m.key === key)
      return hit?.label || key
    }
    return requestModel.value
  })

  /** Everything the token endpoint needs to size the context window correctly. */
  const tokenQuery = computed(() => {
    const p = currentProvider.value
    let maxContext = activeParams.value.maxContext
    // Local weights carry their window in MODEL_REGISTRY, not in the provider.
    if (!maxContext && p?.provider === 'llama-cpp') {
      maxContext = localModelList.value.find((m) => m.key === requestModel.value)?.ctx ?? 0
    }
    return {
      model: requestModel.value,
      providerType: p?.provider ?? '',
      baseUrl: p?.baseUrl ?? '',
      maxContext,
    }
  })

  async function load(): Promise<void> {
    loading.value = true
    lastError.value = ''
    try {
      const cfg = await getLlmConfig()
      providers.value = (cfg.providers ?? []).map((p) => ({
        ...p,
        modelParams: p.modelParams ?? {},
        models: p.models ?? [],
      }))
      const n = providers.value.length
      // Restore by provider id first. The backend copy only reaches disk after
      // a debounced save, so it can lag one click behind; the id survives
      // reordering and deletion, the index does not.
      const wantId = read(ACTIVE_ID_KEY)
      const byId = wantId ? providers.value.findIndex((p) => p.id === wantId) : -1
      const stored = read(ACTIVE_KEY)
      const byIndex = stored === '' ? (cfg.activeProviderIndex ?? 0) : Number(stored)
      const want = byId >= 0 ? byId : Number.isFinite(byIndex) ? byIndex : 0
      activeIndex.value = Math.min(Math.max(0, want), Math.max(0, n - 1))
      if (!selectedModel.value && currentProvider.value?.models.length) {
        setSelectedModel(currentProvider.value.models[0])
      }
      if (isLocal.value) void refreshLocal()
    } catch (e) {
      lastError.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  function setActiveProvider(idx: number): void {
    if (idx < 0 || idx >= providers.value.length) return
    activeIndex.value = idx
    write(ACTIVE_KEY, String(idx))
    const p = providers.value[idx]
    if (p) write(ACTIVE_ID_KEY, p.id)
    if (p && p.models.length && !p.models.includes(selectedModel.value)) {
      setSelectedModel(p.models[0])
    }
    if (p?.provider === 'llama-cpp') void refreshLocal()
    // Without this the choice lived only in localStorage, and load() overwrote
    // it with the stale index the backend still had on disk.
    scheduleSave()
  }

  function setSelectedModel(name: string): void {
    selectedModel.value = name
    write(MODEL_KEY, name)
  }

  /**
   * Pick a weight without loading it.
   *
   * Loading belongs to START. Clicking a weight to inspect or configure it must
   * never spawn a multi-gigabyte load: the tab is for choosing, not for
   * committing the GPU.
   */
  function selectLocalModel(key: string): void {
    selectedLocalModel.value = key
    write(LOCAL_MODEL_KEY, key)
  }

  function setParam(key: keyof ModelParams, value: number | boolean | null): void {
    const p = currentProvider.value
    const key2 = paramKey.value
    if (!p || !key2) return
    p.modelParams = p.modelParams ?? {}
    p.modelParams[key2] = { ...(p.modelParams[key2] ?? {}), [key]: value }
    ;(p as unknown as Record<string, unknown>)[key] = value
    scheduleSave()
  }

  /**
   * Append a blank provider and focus it. Nothing is pre-filled: a provider is
   * whatever the user types into it, and the wire protocol is the OpenAI-
   * compatible one the backend speaks for every non-local entry.
   */
  function addProvider(): void {
    providers.value.push({
      ...DEFAULT_PARAMS,
      id: newId(),
      name: '',
      provider: 'openai',
      apiKey: '',
      baseUrl: '',
      models: [],
      localModelKey: '',
      modelParams: {},
    })
    setActiveProvider(providers.value.length - 1)
    scheduleSave()
  }

  /**
   * The built-in local llama.cpp entry. It is not a user-defined provider: the
   * backend recognises the type and drives the managed server, so there can
   * only be one and it keeps its fixed default endpoint.
   */
  function addLocalProvider(): void {
    const at = providers.value.findIndex((p) => p.provider === 'llama-cpp')
    if (at >= 0) {
      setActiveProvider(at)
      return
    }
    providers.value.push({
      ...DEFAULT_PARAMS,
      id: newId(),
      name: '',
      provider: 'llama-cpp',
      apiKey: '',
      baseUrl: 'http://localhost:8080/v1/',
      models: [],
      localModelKey: '',
      modelParams: {},
    })
    setActiveProvider(providers.value.length - 1)
    scheduleSave()
  }

  function removeProvider(idx: number): void {
    if (idx < 0 || idx >= providers.value.length) return
    providers.value.splice(idx, 1)
    activeIndex.value = Math.min(activeIndex.value, Math.max(0, providers.value.length - 1))
    // The selection may have pointed into the provider that just left; move it
    // to whatever the new active provider offers instead of keeping a name that
    // no longer belongs to any visible model.
    const p = currentProvider.value
    if (p && !p.models.includes(selectedModel.value)) {
      setSelectedModel(p.models[0] ?? '')
    }
    scheduleSave()
  }

  function setProviderField(key: 'apiKey' | 'baseUrl' | 'name', value: string): void {
    const p = currentProvider.value
    if (!p) return
    p[key] = value
    scheduleSave()
  }

  function setProviderType(nextType: string): void {
    const p = currentProvider.value
    if (!p || p.provider === nextType) return
    const prevMeta = PROVIDER_TYPES.find((t) => t.value === p.provider)
    const nextMeta = PROVIDER_TYPES.find((t) => t.value === nextType)
    p.provider = nextType
    const curUrl = (p.baseUrl || '').trim()
    if (!curUrl || (prevMeta && curUrl === prevMeta.defaultBaseUrl)) {
      p.baseUrl = nextMeta?.defaultBaseUrl ?? ''
    }
    if (nextType === 'llama-cpp') void refreshLocal()
    scheduleSave()
  }

  async function probeModels(): Promise<string[]> {
    const p = currentProvider.value
    if (!p || !p.baseUrl.trim()) return []
    const res = await probeProviderModels(p.baseUrl, p.apiKey)
    const found = res.models ?? []
    for (const m of found) {
      if (!p.models.includes(m)) p.models.push(m)
    }
    if (found.length && !p.models.includes(selectedModel.value)) {
      setSelectedModel(found[0])
    }
    if (found.length) scheduleSave()
    return found
  }

  function addModel(name: string): void {
    const p = currentProvider.value
    const trimmed = name.trim()
    if (!p || !trimmed || p.models.includes(trimmed)) return
    p.models.push(trimmed)
    // A model the user just typed is the one they mean to use: take it whenever
    // the current selection belongs to some other provider.
    if (!p.models.includes(selectedModel.value)) setSelectedModel(trimmed)
    scheduleSave()
  }

  function removeModel(name: string): void {
    const p = currentProvider.value
    if (!p) return
    p.models = p.models.filter((m) => m !== name)
    delete p.modelParams[name]
    if (selectedModel.value === name) setSelectedModel(p.models[0] ?? '')
    scheduleSave()
  }

  function scheduleSave(): void {
    syncState.value = 'pending'
    if (saveTimer !== null) clearTimeout(saveTimer)
    saveTimer = window.setTimeout(() => void flush(), SYNC_DEBOUNCE_MS)
  }

  async function flush(attempt = 0): Promise<void> {
    saveTimer = null
    syncState.value = 'saving'
    try {
      await saveLlmConfig({
        providers: providers.value,
        activeProviderIndex: activeIndex.value,
      })
      syncState.value = 'saved'
      window.setTimeout(() => {
        if (syncState.value === 'saved') syncState.value = 'idle'
      }, 1600)
    } catch (e) {
      lastError.value = e instanceof Error ? e.message : String(e)
      if (attempt < RETRY_DELAYS_MS.length) {
        syncState.value = 'pending'
        retryTimers.push(
          window.setTimeout(() => void flush(attempt + 1), RETRY_DELAYS_MS[attempt]),
        )
      } else {
        syncState.value = 'error'
      }
    }
  }

  async function releaseLocal(): Promise<void> {
    localBusy.value = true
    try {
      await localRelease()
      await refreshLocal()
    } catch (e) {
      lastError.value = e instanceof Error ? e.message : String(e)
    } finally {
      localBusy.value = false
    }
  }

  async function refreshLocal(): Promise<void> {
    try {
      const [st, models, devs] = await Promise.all([
        localStatus(),
        localModels(),
        localDevices().catch(() => ({ devices: [] })),
      ])
      localRunning.value = Boolean(st.running)
      localModelKey.value = String(st.model ?? '')
      if (currentProvider.value && st.model) {
        currentProvider.value.localModelKey = String(st.model)
      }
      localModelList.value = models.models ?? []
      localDeviceList.value = (devs.devices as typeof localDeviceList.value) ?? []
      const p = currentProvider.value
      if (p && !p.localModelKey && models.current) p.localModelKey = models.current
    } catch (e) {
      lastError.value = e instanceof Error ? e.message : String(e)
    }
  }

  async function saveCustomLocalModel(payload: LocalModelSavePayload): Promise<void> {
    const res = await saveLocalModel(payload)
    localModelList.value = res.models ?? []
    selectLocalModel(res.entry.key)
  }

  async function removeCustomLocalModel(key: string): Promise<void> {
    const res = await deleteLocalModel(key)
    localModelList.value = res.models ?? []
    if (selectedLocalModel.value === key) {
      selectLocalModel(localModelList.value[0]?.key ?? '')
    }
  }

  async function scanGgufModels(root = ''): Promise<ScannedGgufModel[]> {
    const res = await scanLocalGgufModels(root)
    return res.models ?? []
  }

  async function startLocal(modelKey?: string): Promise<void> {
    localBusy.value = true
    try {
      await localStart(modelKey ?? localSelection.value)
      await refreshLocal()
    } catch (e) {
      lastError.value = e instanceof Error ? e.message : String(e)
    } finally {
      localBusy.value = false
    }
  }

  async function stopLocal(): Promise<void> {
    localBusy.value = true
    try {
      await localStop()
      await refreshLocal()
    } catch (e) {
      lastError.value = e instanceof Error ? e.message : String(e)
    } finally {
      localBusy.value = false
    }
  }

  function dispose(): void {
    if (saveTimer !== null) clearTimeout(saveTimer)
    retryTimers.forEach((t) => clearTimeout(t))
    retryTimers.length = 0
  }

  return {
    providers,
    activeIndex,
    selectedModel,
    loading,
    lastError,
    syncState,
    localRunning,
    localModelKey,
    selectedLocalModel,
    localSelection,
    localModelList,
    localDeviceList,
    localBusy,
    currentProvider,
    isLocal,
    activeParams,
    effectiveModel,
    requestModel,
    tokenQuery,
    load,
    setActiveProvider,
    setSelectedModel,
    selectLocalModel,
    setParam,
    addProvider,
    addLocalProvider,
    removeProvider,
    setProviderField,
    setProviderType,
    probeModels,
    addModel,
    removeModel,
    refreshLocal,
    saveCustomLocalModel,
    removeCustomLocalModel,
    scanGgufModels,
    startLocal,
    stopLocal,
    releaseLocal,
    dispose,
  }
})

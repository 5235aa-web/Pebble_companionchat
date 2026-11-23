import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  MULTI_INSTANCE_TTS_ENGINES,
  TTS_ENGINE_DEFAULTS,
  fetchTtsSpecs,
  getTtsConfig,
  isCloudEngine,
  saveTtsConfig,
  unloadTts,
  type AudioModelSpec,
  type TtsConfig,
} from '@/api/tts'

const SAVE_DEBOUNCE_MS = 600

function newId(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8)
}

/**
 * TTS configurations. Each config is one engine plus its own field set; the
 * backend stores them all in tts_providers.json and only reads the keys that
 * belong to the selected engine.
 */
export const useTtsStore = defineStore('tts', () => {
  const configs = ref<TtsConfig[]>([])
  const specs = ref<AudioModelSpec[]>([])
  const activeIndex = ref(0)
  const loading = ref(false)
  const lastError = ref('')

  let saveTimer: number | null = null

  const active = computed<TtsConfig | null>(() => configs.value[activeIndex.value] ?? null)

  /**
   * The config to speak a given character with.
   *
   * A character carries only voice identity: which reference clip, which
   * built-in id. Engine and sampling stay in the panel, so switching engines
   * there still applies to every character at once.
   */
  function activeFor(voice?: { ref?: string; id?: string }): TtsConfig | null {
    const base = active.value
    if (!base) return null
    // A cloud engine addresses voices by the id typed into the panel; a
    // character's local clip name would overwrite it with something meaningless.
    if (isCloudEngine(base.provider)) return base
    const ref = (voice?.ref ?? '').trim()
    const id = (voice?.id ?? '').trim()
    if (!ref && !id) return base

    const merged: TtsConfig = { ...base }
    if (ref) {
      // Same thing across all local cloning engines: character ref always overrides panel fallback.
      merged.omniVoice = ref
      merged.dotsVoice = ref
      merged.fishVoice = ref
      merged.ming3Voice = ref
      merged.voiceName = ref
    }
    if (id) {
      merged.voiceId = id
      merged.ming3VoiceId = id
    }
    return merged
  }

  /**
   * Collapse configs that share an engine. The panel only ever creates one per
   * engine now, but older builds let duplicates pile up and the tab strip has
   * no way to tell them apart. Later values win only when they are set, so
   * nothing typed into a duplicate is thrown away.
   */
  function collapseDuplicates(list: TtsConfig[]): { merged: TtsConfig[]; indexMap: number[] } {
    const merged: TtsConfig[] = []
    const slot = new Map<string, number>()
    const indexMap: number[] = []
    for (const c of list) {
      if (MULTI_INSTANCE_TTS_ENGINES.has(c.provider)) {
        indexMap.push(merged.length)
        merged.push({ ...c })
        continue
      }
      const at = slot.get(c.provider)
      if (at === undefined) {
        slot.set(c.provider, merged.length)
        indexMap.push(merged.length)
        merged.push({ ...c })
        continue
      }
      const target = merged[at] as unknown as Record<string, unknown>
      for (const [k, v] of Object.entries(c)) {
        if (k === 'id' || v === undefined || v === null || v === '') continue
        target[k] = v
      }
      indexMap.push(at)
    }
    return { merged, indexMap }
  }

  async function load(): Promise<void> {
    loading.value = true
    lastError.value = ''
    try {
      const [cfg, specList] = await Promise.all([
        getTtsConfig(),
        fetchTtsSpecs().catch(() => [] as AudioModelSpec[]),
      ])
      specs.value = specList
      const raw = cfg.configs ?? []
      const { merged, indexMap } = collapseDuplicates(raw)
      configs.value = merged
      activeIndex.value = Math.min(
        Math.max(0, indexMap[cfg.activeIndex ?? 0] ?? 0),
        Math.max(0, merged.length - 1),
      )
      // Persist the cleanup, otherwise the duplicate returns on the next load.
      if (merged.length !== raw.length) scheduleSave()
    } catch (e) {
      lastError.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  function setActiveIndex(idx: number): void {
    if (idx < 0 || idx >= configs.value.length) return
    activeIndex.value = idx
    scheduleSave()
  }

  /** Patch the active config and persist. */
  /**
   * Applying a new provider frees the engine that was loaded before it: every
   * local TTS engine holds a model in VRAM, so switching without releasing
   * would leave two of them resident.
   */
  const unloading = ref(false)

  /** Drop this engine's weights so the GPU is free right now. */
  async function unload(): Promise<'released' | 'idle' | 'cloud' | 'error'> {
    const c = active.value
    if (!c || unloading.value) return 'idle'
    unloading.value = true
    lastError.value = ''
    try {
      const r = await unloadTts(c.provider)
      if (r.local === false) return 'cloud'
      return r.released?.length ? 'released' : 'idle'
    } catch (e) {
      lastError.value = e instanceof Error ? e.message : String(e)
      return 'error'
    } finally {
      unloading.value = false
    }
  }

  async function patch(fields: Partial<TtsConfig>): Promise<void> {
    const c = active.value
    if (!c) return
    // Picking an engine here only edits the stored config. Nothing is loaded
    // and nothing is unloaded until read-aloud actually fires, so browsing
    // through the engines costs nothing.
    Object.assign(c, fields)
    scheduleSave()
  }

  /**
   * Add one config for *provider* and make it active. Engines are unique: a
   * second config for the same engine would be indistinguishable in the tab
   * strip and would only split the same parameters across two blobs, so an
   * existing one is selected instead.
   */
  function addConfig(provider: string): void {
    if (!MULTI_INSTANCE_TTS_ENGINES.has(provider)) {
      const existing = configs.value.findIndex((c) => c.provider === provider)
      if (existing >= 0) {
        activeIndex.value = existing
        return
      }
    }
    const defaults = TTS_ENGINE_DEFAULTS[provider] ?? {}
    configs.value.push({
      id: newId(),
      provider,
      ...JSON.parse(JSON.stringify(defaults)),
    })
    activeIndex.value = configs.value.length - 1
    scheduleSave()
  }

  function removeConfig(idx: number): void {
    if (idx < 0 || idx >= configs.value.length) return
    configs.value.splice(idx, 1)
    activeIndex.value = Math.min(activeIndex.value, Math.max(0, configs.value.length - 1))
    scheduleSave()
  }

  function scheduleSave(): void {
    if (saveTimer !== null) clearTimeout(saveTimer)
    saveTimer = window.setTimeout(() => void flush(), SAVE_DEBOUNCE_MS)
  }

  async function flush(): Promise<void> {
    saveTimer = null
    try {
      await saveTtsConfig({ configs: configs.value, activeIndex: activeIndex.value })
    } catch (e) {
      lastError.value = e instanceof Error ? e.message : String(e)
    }
  }

  return {
    configs,
    specs,
    activeIndex,
    loading,
    lastError,
    active,
    activeFor,
    load,
    setActiveIndex,
    patch,
    addConfig,
    removeConfig,
    unloading,
    unload,
  }
})

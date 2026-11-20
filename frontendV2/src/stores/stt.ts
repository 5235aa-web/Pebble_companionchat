import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  deleteSttEngine,
  fetchSttConfig,
  fetchSttSpecs,
  saveSttConfig,
  saveSttEngine,
  stopSttEngine,
  type SttEngineInfo,
  type SttEngineSavePayload,
  type SttStopVerdict,
} from '@/api/stt'
import type { AudioModelSpec } from '@/api/tts'

/**
 * STT registry store: lists built-in + custom audio.cpp / OpenAI-HTTP engines,
 * supports switching active engine, editing model paths, and stopping VRAM.
 */
export const useSttStore = defineStore('stt', () => {
  const engines = ref<SttEngineInfo[]>([])
  const specs = ref<AudioModelSpec[]>([])
  const active = ref('qwen3_asr')
  const loading = ref(false)
  const stopping = ref(false)
  const lastError = ref('')

  const activeEngine = computed(() => engines.value.find((e) => e.id === active.value) ?? null)
  const activeLabel = computed(() => activeEngine.value?.label || active.value || 'STT')

  async function load() {
    loading.value = true
    lastError.value = ''
    try {
      const [j, specList] = await Promise.all([
        fetchSttConfig(),
        fetchSttSpecs().catch(() => [] as AudioModelSpec[]),
      ])
      engines.value = j.engines ?? []
      active.value = j.active || 'qwen3_asr'
      if (specList.length) specs.value = specList
    } catch (e) {
      lastError.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  async function setActive(id: string): Promise<boolean> {
    if (id === active.value) return true
    const prev = active.value
    active.value = id
    try {
      await saveSttConfig(id)
      window.setTimeout(() => void load(), 1500)
      return true
    } catch (e) {
      active.value = prev
      lastError.value = e instanceof Error ? e.message : String(e)
      return false
    }
  }

  async function upsertEngine(payload: SttEngineSavePayload): Promise<string> {
    lastError.value = ''
    const res = await saveSttEngine(payload)
    engines.value = res.engines ?? []
    active.value = res.active || active.value
    return res.id
  }

  async function removeEngine(id: string): Promise<void> {
    lastError.value = ''
    const res = await deleteSttEngine(id)
    engines.value = res.engines ?? []
    active.value = res.active || active.value
  }

  async function stopActive(): Promise<SttStopVerdict | null> {
    if (stopping.value) return null
    stopping.value = true
    lastError.value = ''
    try {
      const r = await stopSttEngine()
      await load()
      return r.result
    } catch (e) {
      lastError.value = e instanceof Error ? e.message : String(e)
      return null
    } finally {
      stopping.value = false
    }
  }

  return {
    engines,
    specs,
    active,
    loading,
    stopping,
    lastError,
    activeEngine,
    activeLabel,
    load,
    setActive,
    upsertEngine,
    removeEngine,
    stopActive,
  }
})

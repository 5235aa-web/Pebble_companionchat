import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { useSessionStore } from './session'
import { useLlmStore } from './llm'
import { ApiError } from '@/api/client'
import {
  cancelPicture,
  deletePicture,
  generatePicture,
  getPictureConfig,
  getPictureUsage,
  listPictures,
  retryPicture,
  savePictureConfig,
  type GeneratedPicture,
  type PictureConfig,
  type PictureUsage,
} from '@/api/pictures'

/**
 * Picture Studio state.
 *
 * The stage always shows the newest finished picture. A manual swipe moves the
 * index, but the next completion snaps back to 0 — that is what "a new picture
 * replaces the one on screen" means, and keeping it in a watcher means every
 * caller gets the same behaviour without repeating the rule.
 */
export const usePictureStore = defineStore('pictures', () => {
  const session = useSessionStore()

  const config = ref<PictureConfig | null>(null)
  const configError = ref('')
  const items = ref<GeneratedPicture[]>([])
  const usage = ref<PictureUsage | null>(null)
  const index = ref(0)
  const loading = ref(false)
  const error = ref('')

  /** Finished pictures, newest first. Pending rows never occupy the stage. */
  const doneItems = computed(() => items.value.filter((it) => it.status === 'done' && it.url))
  const current = computed<GeneratedPicture | null>(() => doneItems.value[index.value] ?? null)
  const generating = computed(() =>
    items.value.some((it) => it.status === 'pending' || it.status === 'running'),
  )
  /**
   * The job being drawn right now. Only local ComfyUI jobs report a nonzero
   * progress value, so the stage shows a bar for those and a dot for the rest.
   */
  const runningItem = computed<GeneratedPicture | null>(
    () => items.value.find((it) => it.status === 'pending' || it.status === 'running') ?? null,
  )
  const failed = computed(() => items.value.filter((it) => it.status === 'failed'))
  const canSwipe = computed(() => doneItems.value.length > 1)

  let timer: number | null = null
  let pollUntil = 0
  let pollGen = 0
  let loadedKey = ''

  const key = (): string =>
    `${session.activeCompanionId || ''}|${session.activeBranch || ''}`

  /**
   * Show the backend's own message. `ApiError.message` is prefixed with
   * "HTTP 400:", which is noise in the scene panel and hides the actionable
   * part ("no provider configured", "daily limit reached").
   */
  function messageOf(e: unknown): string {
    if (e instanceof ApiError) return e.detail
    return e instanceof Error ? e.message : String(e)
  }

  function stopPolling() {
    pollGen += 1
    if (timer !== null) {
      window.clearTimeout(timer)
      timer = null
    }
  }

  function schedule(delay = 3000) {
    stopPolling()
    const gen = pollGen
    timer = window.setTimeout(async () => {
      // Clear `timer` as soon as it fires so a later `kick()` never mistakes an
      // expired timer handle for an active polling loop.
      timer = null
      await load(true)
      if (gen !== pollGen) return
      if (Date.now() < pollUntil || generating.value) schedule(3000)
    }, delay)
  }

  /** Keep polling for a while after a trigger, since the queue is not instant. */
  function kick(windowMs = 90000) {
    pollUntil = Math.max(pollUntil, Date.now() + windowMs)
    if (timer === null) schedule(1200)
  }

  async function load(quiet = false) {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) {
      items.value = []
      usage.value = null
      loadedKey = ''
      return
    }
    if (!quiet) loading.value = true
    try {
      const [list, use] = await Promise.all([listPictures(cid, bid), getPictureUsage(cid, bid)])
      items.value = list.pictures
      usage.value = use
      loadedKey = key()
      error.value = ''
    } catch (e) {
      error.value = messageOf(e)
    } finally {
      loading.value = false
    }
  }

  /** Called by views on mount and whenever the branch changes. */
  async function ensureLoaded() {
    if (!config.value) void loadConfig()
    if (loadedKey === key() && items.value.length) return
    await load()
    if (generating.value) kick(60000)
  }

  async function refresh() {
    await load(true)
  }

  async function generate(instruction = ''): Promise<number> {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) throw new Error('no active branch')
    error.value = ''
    const llm = useLlmStore()
    try {
      const res = await generatePicture({
        companionId: cid,
        branchId: bid,
        instruction,
        chatProviderId: llm.currentProvider?.id ?? '',
        chatModel: llm.requestModel || '',
      })
      await load(true)
      kick(150000)
      return res.id
    } catch (e) {
      throw new Error(messageOf(e))
    }
  }

  async function remove(id: number) {
    try {
      await deletePicture(id)
      await load(true)
    } catch (e) {
      throw new Error(messageOf(e))
    }
  }

  async function retry(id: number) {
    try {
      await retryPicture(id)
      await load(true)
      kick(150000)
    } catch (e) {
      throw new Error(messageOf(e))
    }
  }

  /** Stop a queued or running job. The backend refuses one that just finished. */
  async function cancel(id: number) {
    try {
      await cancelPicture(id)
      await load(true)
    } catch (e) {
      throw new Error(messageOf(e))
    }
  }

  async function loadConfig() {
    try {
      config.value = await getPictureConfig()
      configError.value = ''
    } catch (e) {
      configError.value = messageOf(e)
    }
  }

  /**
   * Persist the panel's form.
   *
   * `catalogue` is server-supplied metadata, not user state: sending it back
   * would copy the whole provider dictionary into the config file and pin a
   * stale copy there. It is stripped here so no caller can forget.
   */
  async function saveConfig(patch: Partial<PictureConfig>) {
    const { catalogue, promptStyles, ...payload } = patch as PictureConfig
    void catalogue
    void promptStyles
    config.value = await savePictureConfig(payload)
    return config.value
  }

  function go(step: number) {
    const total = doneItems.value.length
    if (!total) return
    index.value = (index.value + step + total) % total
  }

  function jump(target: number) {
    const total = doneItems.value.length
    if (!total) return
    index.value = Math.max(0, Math.min(target, total - 1))
  }

  // A newly finished picture takes over the stage.
  watch(
    () => doneItems.value[0]?.id,
    (id, previous) => {
      if (id && id !== previous) index.value = 0
    },
  )

  watch(
    () => key(),
    async () => {
      stopPolling()
      pollUntil = 0
      index.value = 0
      items.value = []
      await load()
      if (generating.value) kick(60000)
    },
  )

  return {
    config,
    configError,
    items,
    usage,
    index,
    loading,
    error,
    doneItems,
    current,
    generating,
    runningItem,
    failed,
    canSwipe,
    load,
    ensureLoaded,
    refresh,
    kick,
    generate,
    remove,
    retry,
    cancel,
    loadConfig,
    saveConfig,
    go,
    jump,
    stopPolling,
  }
})

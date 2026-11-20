import { defineStore } from 'pinia'
import { ref } from 'vue'
import { clearMemories, deleteMemory, getMemories, updateMemory, type Memory } from '@/api/memory'
import { useSessionStore } from '@/stores/session'

/**
 * What the current character remembers on the current branch.
 *
 * Scoped the same way the backend is: memories belong to one companion on one
 * branch, so switching either one has to reload rather than merge.
 */
export const useMemoryStore = defineStore('memory', () => {
  const session = useSessionStore()

  const items = ref<Memory[]>([])
  const loading = ref(false)
  const error = ref('')
  const limit = ref(100)
  /**
   * Filter text for the panel.
   *
   * Lives in the store rather than inside the panel because the rail's dots
   * set it before opening the panel, so a click lands on that one memory
   * instead of the full list.
   */
  const query = ref('')

  async function load(): Promise<void> {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    items.value = []
    error.value = ''
    if (!cid || !bid) return
    loading.value = true
    try {
      const r = await getMemories(cid, bid)
      items.value = r.items
      // The cap is the backend's decision; the panel only reports it.
      limit.value = r.limit
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  async function remove(id: number): Promise<void> {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) return
    const before = items.value
    // Drop it locally first: the row is going away either way, and waiting for
    // the round trip makes the list feel stuck.
    items.value = items.value.filter((m) => m.id !== id)
    try {
      await deleteMemory(cid, bid, id)
    } catch (e) {
      items.value = before
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  /**
   * Save an edit to one memory.
   *
   * Applied locally first for the same reason as removal: the row is already
   * on screen, and waiting for the round trip makes the edit feel rejected.
   */
  async function edit(
    id: number,
    fields: { content?: string; keys?: string[] },
  ): Promise<boolean> {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) return false
    const before = items.value
    items.value = items.value.map((m) =>
      m.id === id
        ? {
            ...m,
            content: fields.content ?? m.content,
            keys: fields.keys ?? m.keys,
          }
        : m,
    )
    try {
      await updateMemory(cid, bid, id, fields)
      return true
    } catch (e) {
      items.value = before
      error.value = e instanceof Error ? e.message : String(e)
      return false
    }
  }

  async function clear(): Promise<void> {    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) return
    try {
      await clearMemories(cid, bid)
      items.value = []
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  return { items, loading, error, limit, query, load, remove, edit, clear }
})

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useUiStore } from '@/stores/ui'
import { useMemoryStore } from '@/stores/memory'
import { useSessionStore } from '@/stores/session'
import { useChatStore } from '@/stores/chat'
import { useI18n } from '@/i18n'
import type { Memory } from '@/api/memory'
import { listGroupMemories, deleteGroupMemory, updateGroupMemory, type GroupMemory } from '@/api/group'
import { formatDateTime } from '@/utils/time'

const ui = useUiStore()
const memory = useMemoryStore()
const session = useSessionStore()
const chat = useChatStore()
const { t } = useI18n()

const who = computed(() => session.activeCast?.name ?? '')

/**
 * The panel serves two separate stores: this companion's memories, and the
 * shared memory of the group room. They are never merged — switching the tab
 * swaps the data source, and each keeps its own empty state.
 */
const tab = ref<'single' | 'group'>('single')
const groupMemories = ref<GroupMemory[]>([])
const groupLoading = ref(false)

const roomId = computed(() => ui.groupRoomId)

/**
 * The panel serves two stores, and only one of them belongs to the open view.
 * Showing the other one would put a room's shared memory under the cursor of
 * someone talking to a single character, where one stray click deletes it.
 */
const isGroupView = computed(() => ui.view === 'group')
const singleItems = computed(() => (isGroupView.value ? [] : memory.items))
const singleLoading = computed(() => !isGroupView.value && memory.loading)

async function loadGroupMemories(): Promise<void> {
  if (!roomId.value || !isGroupView.value) {
    groupMemories.value = []
    return
  }
  groupLoading.value = true
  try {
    const r = await listGroupMemories(roomId.value)
    groupMemories.value = r.memories ?? []
  } catch {
    groupMemories.value = []
  } finally {
    groupLoading.value = false
  }
}

watch(
  () => [ui.isOpen('memory'), tab.value, roomId.value] as const,
  ([open, which]) => {
    if (open && which === 'group') void loadGroupMemories()
  },
)

/**
 * A dot in the rail asks the panel to locate one memory. The request arrives
 * before the panel is open, so it is held in the store and consumed here: the
 * tab switches to match, and the search box is filled with the memory's text.
 */
watch(
  () => ui.memoryFocus,
  (focus) => {
    if (!focus) return
    tab.value = focus.tab
    if (focus.tab === 'group') {
      void loadGroupMemories().then(() => {
        groupSearch.value = focus.text.slice(0, 24)
      })
    } else {
      search.value = focus.text.slice(0, 24)
    }
    ui.memoryFocus = null
  },
  { immediate: true },
)

/* ---- room memory: search, edit, delete ---- */

const groupSearch = ref('')
const groupError = ref('')
const groupEditingId = ref(0)
const groupDraftText = ref('')

const filteredGroupMemories = computed(() => {
  const q = groupSearch.value.trim().toLowerCase()
  if (!q) return groupMemories.value
  return groupMemories.value.filter((m) =>
    `${m.content} ${m.keywords ?? ''} ${m.about ?? ''}`.toLowerCase().includes(q),
  )
})

function groupStartEdit(m: GroupMemory): void {
  groupEditingId.value = m.id
  groupDraftText.value = m.content
}

function groupCancelEdit(): void {
  groupEditingId.value = 0
  groupDraftText.value = ''
}

async function groupSaveEdit(m: GroupMemory): Promise<void> {
  const text = groupDraftText.value.trim()
  if (!text || !roomId.value) return
  groupError.value = ''
  try {
    await updateGroupMemory(roomId.value, m.id, { content: text })
    // The backend re-encodes the vector; the list only needs the new text.
    m.content = text
    groupCancelEdit()
  } catch (e) {
    groupError.value = e instanceof Error ? e.message : String(e)
  }
}

async function removeGroupMemory(m: GroupMemory): Promise<void> {
  if (!roomId.value) return
  groupError.value = ''
  try {
    await deleteGroupMemory(roomId.value, m.id)
    groupMemories.value = groupMemories.value.filter((x) => x.id !== m.id)
  } catch (e) {
    groupError.value = e instanceof Error ? e.message : String(e)
  }
}

/**
 * Three states, one click apart: plain -> pinned -> strong -> plain.
 *
 * Pinned survives trimming; strong also rides along in every prompt. The write
 * is optimistic and rolls back on failure, so the heart never claims something
 * the backend did not store.
 */
function cycleMark(m: GroupMemory): void {
  void setMark(m, ((m.mark ?? 0) + 1) % 3)
}

async function setMark(m: GroupMemory, mark: number): Promise<void> {
  if (!roomId.value) return
  const prev = m.mark ?? 0
  m.mark = mark
  groupError.value = ''
  try {
    await updateGroupMemory(roomId.value, m.id, { mark })
  } catch (e) {
    m.mark = prev
    groupError.value = e instanceof Error ? e.message : String(e)
  }
}

function heartTip(m: GroupMemory): string {
  const mark = m.mark ?? 0
  if (mark === 0) return t('memory.pin')
  if (mark === 1) return t('memory.pinStrong')
  return t('memory.unpin')
}

/**
 * Deleting one room memory asks first, same as the single thread.
 *
 * The cursor has already moved past the messages it came from, so extraction
 * will not rebuild the row. The lost fact stays lost.
 */
function askForgetGroup(m: GroupMemory) {
  ui.askConfirm({
    title: t('memory.forgetTitle'),
    text: m.content,
    confirmLabel: t('memory.forgetGo'),
    danger: true,
    onConfirm: () => void removeGroupMemory(m),
  })
}

const KIND_LABEL: Record<string, string> = {
  fact: 'memory.kindFact',
  event: 'memory.kindEvent',
  preference: 'memory.kindPreference',
  relationship: 'memory.kindRelationship',
}

function kindLabel(kind: string): string {
  const key = KIND_LABEL[kind]
  return key ? t(key as never) : kind
}

/** Three dots rather than stars: they read as weight, not as a rating. */
function pips(importance: number): number[] {
  return [1, 2, 3].filter((n) => n <= Math.max(1, Math.min(3, importance)))
}

/**
 * SQLite's CURRENT_TIMESTAMP is UTC and comes back as "YYYY-MM-DD HH:MM:SS"
 * with no zone marker. Parsed as-is the browser treats it as local time, so the
 * stamp would be off by the UTC offset. The zone is added explicitly before
 * converting to the reader's local time.
 */
function stamp(raw: string): string {
  return formatDateTime(raw)
}

/**
 * Inline editing. Extraction usually gets the shape of a memory right and the
 * wording slightly wrong; deleting and waiting for the next run would lose the
 * detail in the meantime, so the row edits in place.
 */
const editingId = ref(0)
const draftText = ref('')
const draftKeys = ref('')

function startEdit(id: number, content: string, keys: string[]) {
  editingId.value = id
  draftText.value = content
  draftKeys.value = keys.join(', ')
}

function cancelEdit() {
  editingId.value = 0
}

async function saveEdit(id: number) {
  const text = draftText.value.trim()
  if (!text) return
  const keys = draftKeys.value
    .split(',')
    .map((k) => k.trim())
    .filter(Boolean)
  const ok = await memory.edit(id, { content: text, keys })
  if (ok) editingId.value = 0
}

/**
 * Filter box. The whole list is at most a couple hundred rows and already in
 * memory, so filtering happens here rather than over the wire — typing must
 * not wait for a round trip.
 *
 * The text itself lives in the store: the rail's dots set it before opening
 * the panel, so a click can land on one specific memory.
 */
const search = computed({
  get: () => memory.query,
  set: (v: string) => {
    memory.query = v
  },
})

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return singleItems.value
  return singleItems.value.filter(
    (m) =>
      m.content.toLowerCase().includes(q) ||
      m.keys.some((k) => k.toLowerCase().includes(q)),
  )
})

/**
 * Jump back to the lines this memory was extracted from.
 *
 * The panel covers the thread, so it closes first; the scroll then lands on a
 * message the user can actually see.
 */
function jumpToSource(m: Memory) {
  const id = m.source_message_ids?.[0]
  if (!id) return
  ui.closePanel('memory')
  chat.jumpToBeat(id)
}

/**
 * Deleting one memory asks first.
 *
 * The row is gone for good and the extractor will not necessarily rebuild it —
 * it only re-reads the conversation from the cursor forward, which has already
 * passed the messages this memory came from.
 */
function askForget(m: Memory) {
  ui.askConfirm({
    title: t('memory.forgetTitle'),
    text: m.content,
    confirmLabel: t('memory.forgetGo'),
    danger: true,
    onConfirm: () => void memory.remove(m.id),
  })
}

function askClear() {
  if (!singleItems.value.length) return
  ui.askConfirm({
    title: t('memory.clearTitle'),
    text: t('memory.clearText'),
    confirmLabel: t('memory.clearGo'),
    danger: true,
    onConfirm: () => void memory.clear(),
  })
}

// Reloading on open is enough: the list only changes when a reply finishes,
// and the panel is not on screen while that happens.
watch(
  // Switching character or branch while the panel is open has to reload too:
  // the list belongs to one companion on one branch.
  () => [ui.isOpen('memory'), session.activeCompanionId, session.activeBranch, ui.view],
  ([open, , , view], prev) => {
    // The filter belongs to the list it was typed against. Clear it when the
    // panel closes or the list underneath changes, but not when the panel
    // opens: the rail's dots set it first so a click can land on one row.
    const switched =
      !!prev &&
      (prev[1] !== session.activeCompanionId || prev[2] !== session.activeBranch)
    if (switched || !open) memory.query = ''
    // The group view never loads or shows the single thread's list.
    if (open && view !== 'group') void memory.load()
  },
)

// One store per view. Crossing over drops the other list instead of leaving it
// one click away from a delete button.
watch(
  () => ui.view,
  (view) => {
    if (view !== 'group') groupMemories.value = []
    else if (ui.isOpen('memory') && tab.value === 'group') void loadGroupMemories()
  },
)
</script>

<template>
  <aside class="panel" :class="{ show: ui.isOpen('memory') }">
    <div class="d-head">
      <span class="d-kicker">{{ t('memory.title') }}</span>
      <span class="d-title">
        {{ who || t('memory.title') }}
        <span v-if="singleItems.length" class="mem-count">{{ singleItems.length }}</span>
      </span>
      <button class="d-close" @click="ui.closePanel('memory')">
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>

    <div class="d-tabs">
      <button class="d-tab" :class="{ on: tab === 'single' }" @click="tab = 'single'">
        {{ t('panel.single') }}
      </button>
      <button class="d-tab" :class="{ on: tab === 'group' }" @click="tab = 'group'">
        {{ t('panel.group') }}
        <span class="d-tab-n">{{ groupMemories.length }}</span>
      </button>
    </div>

    <div v-if="tab === 'single'" class="d-body">
      <p v-if="isGroupView" class="mem-note">{{ t('memory.isolated') }}</p>
      <p v-else class="mem-note">{{ t('memory.note') }}</p>

      <div v-if="memory.error" class="mem-error">{{ memory.error }}</div>

      <div v-if="singleItems.length" class="mem-search">
        <svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5" /><path d="M15.5 15.5L21 21" /></svg>
        <input v-model="search" :placeholder="t('memory.search')" spellcheck="false" />
        <button v-if="search" class="mem-search-x" :aria-label="t('memory.searchClear')" @click="search = ''">
          <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>

      <div v-if="singleItems.length && !filtered.length" class="empty-block">
        <div class="eb-title">{{ t('memory.noMatch') }}</div>
        <div class="eb-hint">{{ t('memory.noMatchHint') }}</div>
      </div>

      <div v-if="!singleItems.length && !singleLoading" class="empty-block">
        <div class="eb-title">{{ t('memory.empty') }}</div>
        <div class="eb-hint">{{ t('memory.emptyHint') }}</div>
      </div>

      <div v-for="m in filtered" :key="m.id" class="mem-card">
        <div class="mem-top">
          <span class="mem-kind" :data-kind="m.kind">{{ kindLabel(m.kind) }}</span>
          <span v-if="m.scope === 'character'" class="mem-scope">{{ t('memory.shared') }}</span>
          <span class="mem-pips">
            <i v-for="n in pips(m.importance)" :key="n"></i>
          </span>
          <div class="mem-acts">
            <button
              v-if="editingId !== m.id"
              class="mem-x mem-edit"
              :aria-label="t('memory.edit')"
              @click="startEdit(m.id, m.content, m.keys)"
            >
              <svg viewBox="0 0 24 24"><path d="M4 20h4l10-10-4-4L4 16v4z" /><path d="M14 6l4 4" /></svg>
            </button>
            <button class="mem-x" :aria-label="t('memory.forget')" @click="askForget(m)">
              <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
            </button>
          </div>
        </div>

        <template v-if="editingId === m.id">
          <textarea v-model="draftText" class="mem-area" rows="3"></textarea>
          <input v-model="draftKeys" class="mem-area mem-keys-in" :placeholder="t('memory.keysHint')" />
          <div class="mem-actions">
            <button class="mini-btn" @click="cancelEdit">{{ t('beat.cancel') }}</button>
            <button class="mini-btn primary" @click="saveEdit(m.id)">{{ t('beat.save') }}</button>
          </div>
        </template>

        <template v-else>
          <p class="mem-text">{{ m.content }}</p>

          <div class="mem-foot">
            <span v-for="(k, i) in m.keys" :key="i + '-' + k" class="mem-key">{{ k }}</span>
            <span v-if="m.use_count" class="mem-used">{{ m.use_count }}×</span>
            <button
              v-if="m.source_message_ids?.length"
              class="mem-src"
              :data-tip="t('memory.fromMessages')"
              @click="jumpToSource(m)"
            >{{ m.source_message_ids.length }} →</button>
            <span class="mem-time">{{ stamp(m.created_at) }}</span>
          </div>
        </template>
      </div>
    </div>

    <div v-else class="d-body">
      <p v-if="isGroupView" class="mem-note">{{ t('memory.groupNote') }}</p>
      <p v-else class="mem-note">{{ t('memory.isolatedGroup') }}</p>
      <div v-if="groupLoading" class="empty-block">
        <div class="eb-title">{{ t('memory.loading') }}</div>
      </div>
      <div v-else-if="!groupMemories.length" class="empty-block">
        <div class="eb-title">{{ t('memory.groupEmpty') }}</div>
        <div class="eb-hint">{{ t('memory.groupEmptyHint') }}</div>
      </div>

      <div v-if="groupMemories.length" class="mem-search">
        <svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5" /><path d="M15.5 15.5L21 21" /></svg>
        <input v-model="groupSearch" :placeholder="t('memory.search')" spellcheck="false" />
        <button v-if="groupSearch" class="mem-search-x" :aria-label="t('memory.searchClear')" @click="groupSearch = ''">
          <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>

      <div v-if="groupError" class="mem-error">{{ groupError }}</div>

      <div v-if="!groupLoading && groupMemories.length && !filteredGroupMemories.length" class="empty-block">
        <div class="eb-title">{{ t('memory.noMatch') }}</div>
        <div class="eb-hint">{{ t('memory.noMatchHint') }}</div>
      </div>

      <div v-for="m in filteredGroupMemories" :key="m.id" class="grp-row">
        <div class="grp-meta">
          <span v-if="m.about" class="grp-who">{{ m.about }}</span>
          <span v-if="m.use_count" class="mem-used">{{ m.use_count }}×</span>
          <span v-if="m.created_at" class="grp-time">{{ m.created_at }}</span>
          <span class="grp-actions">
            <button
              class="grp-act heart"
              :class="{ on: (m.mark ?? 0) > 0, full: (m.mark ?? 0) > 1 }"
              :data-tip="heartTip(m)"
              @click="cycleMark(m)"
            >
              <svg viewBox="0 0 24 24">
                <path d="M12 20s-7-4.3-7-9.2A4.2 4.2 0 0 1 12 7.2a4.2 4.2 0 0 1 7 3.6C19 15.7 12 20 12 20z" />
              </svg>
            </button>
            <button class="grp-act" :data-tip="t('memory.edit')" @click="groupStartEdit(m)">
              <svg viewBox="0 0 24 24"><path d="M4 20h4L20 8l-4-4L4 16v4z" /></svg>
            </button>
            <button class="grp-act danger" :data-tip="t('memory.delete')" @click="askForgetGroup(m)">
              <svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13" /></svg>
            </button>
          </span>
        </div>

        <template v-if="groupEditingId === m.id">
          <textarea v-model="groupDraftText" class="mem-area" rows="3"></textarea>
          <div class="mem-actions">
            <button class="mini-btn" @click="groupCancelEdit">{{ t('beat.cancel') }}</button>
            <button class="mini-btn primary" @click="groupSaveEdit(m)">{{ t('beat.save') }}</button>
          </div>
        </template>
        <p v-else class="grp-text">{{ m.content }}</p>
      </div>
    </div>

    <div v-if="tab === 'single' && singleItems.length" class="d-foot">
      <button class="mini-btn danger" @click="askClear">{{ t('memory.clear') }}</button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useUiStore } from '@/stores/ui'
import { useSessionStore } from '@/stores/session'
import { useI18n } from '@/i18n'
import type { LoreEntry } from '@/data/lore'
import {
  associateCompanionWorldBook,
  createEntry,
  deleteEntry,
  getCompanionWorldBooks,
  getWorldBook,
  updateEntry,
  createWorldBook,
  exportWorldBook,
  importWorldBook,
  type WorldBookEntry,
} from '@/api/worldbook'

const ui = useUiStore()
const session = useSessionStore()
const { t } = useI18n()

const entries = ref<LoreEntry[]>([])
const worldBookId = ref(0)
const loading = ref(false)
const error = ref('')

const toLore = (e: WorldBookEntry): LoreEntry => ({
  id: e.id,
  name: e.name || '',
  badge: null,
  open: false,
  on: e.enabled,
  comment: e.comment || '',
  content: e.content || '',
  keys: [...(e.keys ?? [])],
  secKeys: [...(e.secondary_keys ?? [])],
  position: e.position || 'before_char',
  depth: String(e.depth ?? 4),
  prob: String(Math.round((e.probability ?? 1) * 100)),
  constant: !!e.constant,
  selective: !!e.selective,
  enabled: !!e.enabled,
  logic: e.logic === 'OR' ? 'OR' : 'AND',
  draftKey: '',
  draftSecKey: '',
})

const toFields = (e: LoreEntry): Partial<WorldBookEntry> => ({
  name: e.name,
  comment: e.comment,
  content: e.content,
  keys: e.keys,
  secondary_keys: e.secKeys,
  position: e.position,
  depth: Number(e.depth) || 0,
  probability: (Number(e.prob) || 0) / 100,
  constant: e.constant,
  selective: e.selective,
  enabled: e.enabled,
  logic: e.logic,
})

async function load() {
  entries.value = []
  worldBookId.value = 0
  const cid = session.activeCompanionId
  if (!cid) return
  loading.value = true
  error.value = ''
  try {
    const books = await getCompanionWorldBooks(cid)
    if (!books.length) return
    const book = await getWorldBook(books[0].id)
    worldBookId.value = book.id
    entries.value = (book.entries ?? []).map(toLore)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

watch(() => session.activeCompanionId, load, { immediate: true })

async function persist(entry: LoreEntry) {
  if (!worldBookId.value || entry.id <= 0) return
  try {
    await updateEntry(worldBookId.value, entry.id, toFields(entry))
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

function toggle(entry: LoreEntry) {
  entry.open = !entry.open
}

function removeKey(entry: LoreEntry, key: string) {
  entry.keys = entry.keys.filter((k) => k !== key)
  void persist(entry)
}

function addKey(entry: LoreEntry) {
  const v = entry.draftKey.trim()
  if (!v) return
  entry.keys.push(v)
  entry.draftKey = ''
  void persist(entry)
}

function removeSecKey(entry: LoreEntry, key: string) {
  entry.secKeys = entry.secKeys.filter((k) => k !== key)
  void persist(entry)
}

function addSecKey(entry: LoreEntry) {
  const v = entry.draftSecKey.trim()
  if (!v) return
  entry.secKeys.push(v)
  entry.draftSecKey = ''
  void persist(entry)
}

function setLogic(entry: LoreEntry, v: 'AND' | 'OR') {
  entry.logic = v
  void persist(entry)
}

async function ensureWorldBook(): Promise<number> {
  if (worldBookId.value) return worldBookId.value
  const cid = session.activeCompanionId
  if (!cid) return 0
  const created = await createWorldBook({ name: `${session.act || cid} - lore`, description: '' })
  const id = Number((created as unknown as { world_book_id?: number }).world_book_id ?? 0)
  if (!id) return 0
  await associateCompanionWorldBook(cid, id, true)
  worldBookId.value = id
  return id
}

async function addEntry() {
  try {
    const bookId = await ensureWorldBook()
    if (!bookId) {
      error.value = 'no world book on this character'
      return
    }
    const created = await createEntry(bookId, {
      name: 'New entry',
      content: '',
      keys: [],
      secondary_keys: [],
      enabled: true,
      constant: false,
      selective: false,
      logic: 'AND',
      position: 'before_char',
      depth: 4,
      probability: 1,
    })
    entries.value.push(toLore(created))
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

const fileInput = ref<HTMLInputElement | null>(null)

function pickFile() {
  fileInput.value?.click()
}

async function onFilePicked(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  const cid = session.activeCompanionId
  if (!file || !cid) return
  try {
    const book = await importWorldBook(file)
    const id = Number((book as unknown as { world_book_id?: number }).world_book_id ?? book?.id ?? 0)
    if (id) await associateCompanionWorldBook(cid, id, true)
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function exportBook() {
  if (!worldBookId.value) return
  try {
    const blob = await exportWorldBook(worldBookId.value)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = (session.act || 'lore') + '.json'
    a.click()
    URL.revokeObjectURL(url)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function removeEntry(entry: LoreEntry) {
  if (!worldBookId.value) return
  try {
    await deleteEntry(worldBookId.value, entry.id)
    entries.value = entries.value.filter((e) => e.id !== entry.id)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}
</script>

<template>
  <aside class="panel" :class="{ show: ui.isOpen('lore') }">
    <div class="d-head">
      <span class="d-kicker">{{ t('lore.title') }}</span>
      <span class="d-title">{{ t('lore.title') }}</span>
      <button class="d-close" @click="ui.closePanel('lore')">
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>

    <div class="d-body">
      <div v-for="e in entries" :key="e.id" class="lore-entry" :class="{ open: e.open }">
        <button class="le-head" @click="toggle(e)">
          <span class="le-on" :class="{ off: !e.enabled }"></span>
          <span class="le-name">{{ e.name }}</span>
          <span v-if="e.badge" class="le-badge">{{ e.badge }}</span>
          <svg class="le-caret" viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>

        <div class="le-body">
          <div class="le-inner">
            <div class="field">
              <label>{{ t('lore.comment') }}</label>
              <input v-model="e.comment" class="inp" aria-label="comment" />
            </div>

            <div class="field">
              <label>{{ t('lore.content') }}</label>
              <textarea v-model="e.content" class="area" aria-label="lore content"></textarea>
            </div>

            <div class="field">
              <label>{{ t('lore.primaryKeys') }}</label>
              <div class="key-chips">
                <span v-for="k in e.keys" :key="k" class="key-chip">
                  {{ k }}
                  <button aria-label="remove" @click="removeKey(e, k)">
                    <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
                  </button>
                </span>
              </div>
              <input
                v-model="e.draftKey"
                class="inp key-add"
                aria-label="add primary key"
                :placeholder="t('lore.addKeyword')"
                @keydown.enter="addKey(e)"
              />
            </div>

            <div class="field">
              <label>{{ t('lore.secondaryKeys') }}</label>
              <div class="key-chips">
                <span v-for="k in e.secKeys" :key="k" class="key-chip sec">
                  {{ k }}
                  <button aria-label="remove" @click="removeSecKey(e, k)">
                    <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
                  </button>
                </span>
                <span v-if="!e.secKeys.length" class="key-chip dim">{{ t('lore.none') }}</span>
              </div>
              <input
                v-model="e.draftSecKey"
                class="inp key-add"
                aria-label="add secondary key"
                :placeholder="t('lore.addKeyword')"
                @keydown.enter="addSecKey(e)"
              />
            </div>

            <div class="le-grid">
              <div class="field">
                <label>{{ t('lore.position') }}</label>
                <input v-model="e.position" class="inp" aria-label="position" />
              </div>
              <div class="field">
                <label>{{ t('lore.depth') }}</label>
                <input v-model="e.depth" class="inp" aria-label="depth" />
              </div>
              <div class="field">
                <label>{{ t('lore.prob') }}</label>
                <input v-model="e.prob" class="inp" aria-label="probability" />
              </div>
            </div>

            <div class="lore-toggles">
              <button class="lever" :class="{ on: e.constant }" @click="e.constant = !e.constant">
                <i></i><span>{{ t('lore.constant') }}</span>
              </button>
              <button class="lever" :class="{ on: e.selective }" @click="e.selective = !e.selective">
                <i></i><span>{{ t('lore.selective') }}</span>
              </button>
              <button class="lever" :class="{ on: e.enabled }" @click="e.enabled = !e.enabled">
                <i></i><span>{{ t('lore.enabled') }}</span>
              </button>
            </div>

            <div class="lore-logic">
              <span class="lg-label">{{ t('lore.keyLogic') }}</span>
              <div class="seg">
                <button :class="{ on: e.logic === 'AND' }" @click="setLogic(e, 'AND')">AND</button>
                <button :class="{ on: e.logic === 'OR' }" @click="setLogic(e, 'OR')">OR</button>
              </div>
            </div>
          </div>
        </div>
      </div>
          <div v-if="!entries.length" class="empty-block">
        <div class="eb-title">{{ t('empty.noLore') }}</div>
        <div class="eb-hint">{{ t('empty.noLoreHint') }}</div>
      </div>
    </div>

    <div class="d-foot">
      <button class="mini-btn primary" @click="addEntry">{{ t('lore.newEntry') }}</button>
      <button class="mini-btn" @click="pickFile">{{ t('lore.import') }}</button>
      <input ref="fileInput" type="file" accept=".json" hidden @change="onFilePicked" />
      <button class="mini-btn" @click="exportBook">{{ t('lore.export') }}</button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useUiStore } from '@/stores/ui'
import { useSessionStore } from '@/stores/session'
import { llmConfig } from '@/stores/chat'
import { useI18n } from '@/i18n'
import type { LoreEntry } from '@/data/lore'
import UiSelect, { type UiOption } from './ui/UiSelect.vue'
import {
  associateCompanionWorldBook,
  bulkToggleWorldBookEntries,
  createEntry,
  deleteEntry,
  getCompanionWorldBooksInfo,
  getWorldBook,
  syncCompanionCardWorldBook,
  translateWorldBook,
  updateEntry,
  createWorldBook,
  exportWorldBook,
  importWorldBook,
  type WorldBookEntry,
  type WorldBookEntryCategory,
  type WorldBookEntryTranslations,
  type WorldBookLocale,
} from '@/api/worldbook'

const ui = useUiStore()
const session = useSessionStore()
const { t } = useI18n()

const entries = ref<LoreEntry[]>([])
const worldBookId = ref(0)
const cardLoreCount = ref(0)
const categoryFilter = ref<'all' | WorldBookEntryCategory>('all')
const viewLocale = ref<'' | WorldBookLocale>('')
const translatingLocale = ref<WorldBookLocale | null>(null)
const translateProgress = ref({ done: 0, total: 0 })
let translateAbort: AbortController | null = null
const loading = ref(false)
const error = ref('')

const positionOptions = computed<UiOption[]>(() => [
  { value: 'before_char', label: t('lore.posBefore') },
  { value: 'after_char', label: t('lore.posAfter') },
  { value: 'at_depth', label: t('lore.posDepth') },
])

function posToLabel(raw: number | string | undefined): string {
  if (raw === 1 || raw === '1' || raw === 'after_char') return 'after_char'
  if (raw === 4 || raw === '4' || raw === 'at_depth') return 'at_depth'
  return 'before_char'
}

function normalizeProbPercent(raw: number | undefined): string {
  const n = Number(raw ?? 100)
  if (!Number.isFinite(n)) return '100'
  if (n > 0 && n <= 1) return String(Math.round(n * 100))
  return String(Math.max(0, Math.min(100, Math.round(n))))
}

const toLore = (e: WorldBookEntry): LoreEntry => {
  const title = (e.comment || e.name || '').trim() || `Entry #${e.uid ?? e.id}`
  const isEnabled = e.enabled !== undefined ? !!e.enabled : !e.disabled
  const cat: WorldBookEntryCategory =
    e.category === 'media' || e.category === 'rpg' ? e.category : 'lore'
  const isAnd =
    e.logic === 'AND' ||
    e.selective_logic === 3
  return {
    id: e.id,
    name: title,
    badge: e.constant ? t('lore.constant') : null,
    category: cat,
    open: false,
    on: isEnabled,
    comment: e.comment || e.name || '',
    content: e.content || '',
    translations: e.translations ? { ...e.translations } : {},
    keys: [...(e.keys ?? [])],
    secKeys: [...(e.secondary_keys ?? [])],
    position: posToLabel(e.position),
    depth: String(e.depth ?? 4),
    prob: normalizeProbPercent(e.probability),
    constant: !!e.constant,
    selective: !!e.selective,
    enabled: isEnabled,
    logic: isAnd ? 'AND' : 'OR',
    draftKey: '',
    draftSecKey: '',
  }
}

const toFields = (e: LoreEntry): Partial<WorldBookEntry> => {
  const probNum = Math.max(0, Math.min(100, Math.round(Number(e.prob) || 0)))
  return {
    name: e.comment || e.name,
    comment: e.comment,
    content: e.content,
    keys: e.keys,
    secondary_keys: e.secKeys,
    position: e.position || 'before_char',
    depth: Math.max(0, Math.round(Number(e.depth) || 0)),
    probability: probNum,
    constant: e.constant,
    selective: e.selective,
    enabled: e.enabled,
    disabled: !e.enabled,
    logic: e.logic,
    selective_logic: e.logic === 'AND' ? 3 : 0,
    category: e.category,
  }
}

const enabledCount = computed(() => entries.value.filter((e) => e.enabled).length)
const categoryCounts = computed(() => {
  const counts = { all: entries.value.length, media: 0, lore: 0, rpg: 0 }
  for (const e of entries.value) {
    counts[e.category] += 1
  }
  return counts
})
const filteredEntries = computed(() =>
  categoryFilter.value === 'all'
    ? entries.value
    : entries.value.filter((e) => e.category === categoryFilter.value),
)

function categoryLabel(cat: WorldBookEntryCategory): string {
  if (cat === 'media') return t('lore.catMedia')
  if (cat === 'rpg') return t('lore.catRpg')
  return t('lore.catLore')
}

async function load() {
  cancelTranslation()
  entries.value = []
  worldBookId.value = 0
  cardLoreCount.value = 0
  categoryFilter.value = 'all'
  viewLocale.value = ''
  const cid = session.activeCompanionId
  if (!cid) return
  loading.value = true
  error.value = ''
  try {
    const info = await getCompanionWorldBooksInfo(cid)
    cardLoreCount.value = info.cardLoreCount
    if (!info.worldBooks.length) {
      if (info.cardLoreCount > 0) {
        const synced = await syncCompanionCardWorldBook(cid)
        if (session.activeCompanionId !== cid) return
        if (synced.world_book_id) {
          worldBookId.value = synced.world_book_id
          entries.value = (synced.entries ?? []).map(toLore)
        }
      }
      return
    }
    const book = await getWorldBook(info.worldBooks[0].id)
    if (session.activeCompanionId !== cid) return
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
  entry.name = (entry.comment || '').trim() || entry.name
  entry.badge = entry.constant ? t('lore.constant') : null
  entry.on = entry.enabled
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

function toggleEnabled(entry: LoreEntry) {
  entry.enabled = !entry.enabled
  void persist(entry)
}

function toggleConstant(entry: LoreEntry) {
  entry.constant = !entry.constant
  void persist(entry)
}

function toggleSelective(entry: LoreEntry) {
  entry.selective = !entry.selective
  void persist(entry)
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

function setPosition(entry: LoreEntry, v: string) {
  entry.position = v
  void persist(entry)
}

async function applyBulkMode(mode: 'media_only' | 'all_on' | 'all_off') {
  if (!worldBookId.value) return
  error.value = ''
  try {
    const res = await bulkToggleWorldBookEntries(worldBookId.value, mode)
    const openIds = new Set(entries.value.filter((e) => e.open).map((e) => e.id))
    entries.value = (res.entries ?? []).map((item) => {
      const mapped = toLore(item)
      mapped.open = openIds.has(mapped.id)
      return mapped
    })
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function syncFromCard() {
  const cid = session.activeCompanionId
  if (!cid) return
  loading.value = true
  error.value = ''
  try {
    const synced = await syncCompanionCardWorldBook(cid, true)
    if (session.activeCompanionId !== cid) return
    if (synced.world_book_id) {
      worldBookId.value = synced.world_book_id
      entries.value = (synced.entries ?? []).map(toLore)
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
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
      comment: 'New entry',
      content: '',
      keys: [],
      secondary_keys: [],
      enabled: true,
      disabled: false,
      constant: false,
      selective: false,
      logic: 'OR',
      selective_logic: 0,
      position: 0,
      depth: 4,
      probability: 100,
      category: 'lore',
    })
    const mapped = toLore(created)
    mapped.open = true
    entries.value.push(mapped)
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

function entryHasTranslation(entry: LoreEntry, locale: WorldBookLocale): boolean {
  const b = entry.translations?.[locale]
  return !!(b && ((b.comment && b.comment.trim()) || (b.content && b.content.trim())))
}

function displayComment(entry: LoreEntry): string {
  if (viewLocale.value) {
    const tr = entry.translations?.[viewLocale.value]?.comment?.trim()
    if (tr) return tr
  }
  return entry.comment || entry.name
}

function displayContent(entry: LoreEntry): string {
  if (viewLocale.value) {
    const tr = entry.translations?.[viewLocale.value]?.content
    if (typeof tr === 'string' && tr.trim()) return tr
  }
  return entry.content
}

function isTranslationMissing(entry: LoreEntry): boolean {
  return !!viewLocale.value && !entryHasTranslation(entry, viewLocale.value)
}

const translateProgressText = computed(() => {
  if (!translatingLocale.value) return ''
  const { done, total } = translateProgress.value
  return total > 0
    ? t('lore.translatingProgress', { done, total })
    : t('lore.translating')
})

function cancelTranslation() {
  translateAbort?.abort()
  translateAbort = null
  translatingLocale.value = null
}

function mergeBackendEntries(rawEntries: WorldBookEntry[]) {
  const openIds = new Set(entries.value.filter((e) => e.open).map((e) => e.id))
  entries.value = rawEntries.map((item) => {
    const mapped = toLore(item)
    mapped.open = openIds.has(mapped.id)
    return mapped
  })
}

async function runTranslate(
  target: WorldBookLocale,
  opts?: { entryIds?: number[]; force?: boolean },
) {
  if (!worldBookId.value || translatingLocale.value) return
  error.value = ''
  viewLocale.value = target
  translatingLocale.value = target
  translateProgress.value = { done: 0, total: opts?.entryIds?.length ?? 0 }
  translateAbort = new AbortController()
  try {
    const finalEv = await translateWorldBook(
      worldBookId.value,
      {
        target_locale: target,
        entry_ids: opts?.entryIds,
        force: opts?.force ?? false,
        companion_id: session.activeCompanionId || undefined,
        llm_config: llmConfig(),
      },
      {
        signal: translateAbort.signal,
        onEvent(ev) {
          if (ev.type === 'start' || ev.type === 'progress') {
            translateProgress.value = {
              done: ev.done ?? 0,
              total: ev.total ?? translateProgress.value.total,
            }
          } else if (ev.type === 'entry_done') {
            translateProgress.value = {
              done: ev.done ?? 0,
              total: ev.total ?? translateProgress.value.total,
            }
            if (ev.entry_id && ev.translations) {
              const targetEntry = entries.value.find((item) => item.id === ev.entry_id)
              if (targetEntry) {
                targetEntry.translations = ev.translations as WorldBookEntryTranslations
              }
            }
          } else if (ev.type === 'done' && ev.entries?.length) {
            mergeBackendEntries(ev.entries)
          }
        },
      },
    )
    if (finalEv?.entries?.length) {
      mergeBackendEntries(finalEv.entries)
    }
  } catch (e) {
    if ((e as { name?: string })?.name !== 'AbortError') {
      error.value = e instanceof Error ? e.message : String(e)
    }
  } finally {
    translateAbort = null
    translatingLocale.value = null
  }
}

onUnmounted(() => {
  cancelTranslation()
})
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
      <div v-if="error" class="path-err">{{ error }}</div>

      <div v-if="entries.length" class="lore-bar">
        <div class="lore-bar-top">
          <span class="lore-stat">
            {{ t('lore.activeSummary', { on: enabledCount, total: entries.length }) }}
          </span>
          <div class="lore-bulk">
            <button
              v-if="categoryCounts.media > 0"
              type="button"
              class="mini-btn"
              @click="applyBulkMode('media_only')"
            >
              {{ t('lore.mediaOnly') }}
            </button>
            <button
              type="button"
              class="mini-btn"
              :class="{ on: viewLocale === '' }"
              :disabled="!!translatingLocale"
              @click="viewLocale = ''"
            >
              {{ t('lore.orig') }}
            </button>
            <button
              type="button"
              class="mini-btn"
              :class="{ on: viewLocale === 'zh' }"
              :disabled="!!translatingLocale"
              @click="runTranslate('zh')"
            >
              {{ t('lore.translateZh') }}
            </button>
            <button
              type="button"
              class="mini-btn"
              :class="{ on: viewLocale === 'en' }"
              :disabled="!!translatingLocale"
              @click="runTranslate('en')"
            >
              {{ t('lore.translateEn') }}
            </button>
            <button
              type="button"
              class="mini-btn"
              @click="applyBulkMode('all_off')"
            >
              {{ t('lore.allOff') }}
            </button>
          </div>
        </div>
        <div v-if="translatingLocale" class="lore-trans-row active">
          <span class="lore-trans-prog">{{ translateProgressText }}</span>
          <button type="button" class="mini-btn" @click="cancelTranslation">
            {{ t('lore.cancel') }}
          </button>
        </div>
        <div v-else-if="viewLocale" class="lore-trans-row">
          <span>{{ t('lore.translationReadOnlyNote') }}</span>
        </div>
        <div class="lore-filters">
          <button
            type="button"
            class="lore-pill"
            :class="{ on: categoryFilter === 'all' }"
            @click="categoryFilter = 'all'"
          >
            {{ t('lore.filterAll') }} ({{ categoryCounts.all }})
          </button>
          <button
            v-if="categoryCounts.media > 0"
            type="button"
            class="lore-pill"
            :class="{ on: categoryFilter === 'media' }"
            @click="categoryFilter = 'media'"
          >
            {{ t('lore.filterMedia') }} ({{ categoryCounts.media }})
          </button>
          <button
            v-if="categoryCounts.lore > 0"
            type="button"
            class="lore-pill"
            :class="{ on: categoryFilter === 'lore' }"
            @click="categoryFilter = 'lore'"
          >
            {{ t('lore.filterLore') }} ({{ categoryCounts.lore }})
          </button>
          <button
            v-if="categoryCounts.rpg > 0"
            type="button"
            class="lore-pill"
            :class="{ on: categoryFilter === 'rpg' }"
            @click="categoryFilter = 'rpg'"
          >
            {{ t('lore.filterRpg') }} ({{ categoryCounts.rpg }})
          </button>
        </div>
      </div>

      <div v-for="e in filteredEntries" :key="e.id" class="lore-entry" :class="{ open: e.open }">
        <div class="le-head">
          <button
            type="button"
            class="le-toggle-btn"
            :aria-label="t('lore.enabled')"
            :aria-pressed="e.enabled"
            @click.stop="toggleEnabled(e)"
          >
            <span class="switch le-switch" :class="{ on: e.enabled }"><i></i></span>
          </button>
          <button type="button" class="le-expand" @click="toggle(e)">
            <span class="le-name">{{ displayComment(e) }}</span>
            <span class="le-cat" :class="e.category">{{ categoryLabel(e.category) }}</span>
            <span v-if="e.badge" class="le-badge">{{ e.badge }}</span>
            <svg class="le-caret" viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">
              <path d="M6 9l6 6 6-6" />
            </svg>
          </button>
        </div>

        <div class="le-body">
          <div class="le-inner">
            <div v-if="isTranslationMissing(e)" class="le-miss-note">
              {{ t('lore.translationMissing') }}
            </div>

            <div class="field">
              <label>{{ t('lore.comment') }}</label>
              <input
                v-if="!viewLocale"
                v-model="e.comment"
                class="inp"
                aria-label="comment"
                @change="persist(e)"
              />
              <input
                v-else
                :value="displayComment(e)"
                class="inp"
                aria-label="comment"
                readonly
              />
            </div>

            <div class="field">
              <label>{{ t('lore.content') }}</label>
              <textarea
                v-if="!viewLocale"
                v-model="e.content"
                class="area"
                aria-label="lore content"
                @change="persist(e)"
              ></textarea>
              <textarea
                v-else
                :value="displayContent(e)"
                class="area"
                aria-label="lore content"
                readonly
              ></textarea>
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
                <UiSelect
                  :model-value="e.position"
                  :options="positionOptions"
                  @update:model-value="setPosition(e, $event)"
                />
              </div>
              <div class="field">
                <label>{{ t('lore.depth') }}</label>
                <input v-model="e.depth" class="inp" aria-label="depth" @change="persist(e)" />
              </div>
              <div class="field">
                <label>{{ t('lore.prob') }}</label>
                <input v-model="e.prob" class="inp" aria-label="probability" @change="persist(e)" />
              </div>
            </div>

            <div class="lore-toggles">
              <button type="button" class="lever" :class="{ on: e.constant }" @click="toggleConstant(e)">
                <i></i><span>{{ t('lore.constant') }}</span>
              </button>
              <button type="button" class="lever" :class="{ on: e.selective }" @click="toggleSelective(e)">
                <i></i><span>{{ t('lore.selective') }}</span>
              </button>
              <button type="button" class="lever" :class="{ on: e.enabled }" @click="toggleEnabled(e)">
                <i></i><span>{{ t('lore.enabled') }}</span>
              </button>
            </div>

            <div class="lore-logic">
              <div class="lore-logic-left">
                <span class="lg-label">{{ t('lore.keyLogic') }}</span>
                <div class="seg">
                  <button type="button" :class="{ on: e.logic === 'AND' }" @click="setLogic(e, 'AND')">AND</button>
                  <button type="button" :class="{ on: e.logic === 'OR' }" @click="setLogic(e, 'OR')">OR</button>
                </div>
              </div>
              <div class="lore-bulk">
                <button
                  v-if="viewLocale"
                  type="button"
                  class="mini-btn"
                  :disabled="!!translatingLocale"
                  @click="runTranslate(viewLocale, { entryIds: [e.id], force: true })"
                >
                  {{ t('lore.retranslate') }}
                </button>
                <button type="button" class="mini-btn danger" @click="removeEntry(e)">
                  {{ t('lore.deleteEntry') }}
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div v-if="!loading && !entries.length" class="empty-block">
        <div class="eb-title">{{ t('empty.noLore') }}</div>
        <div class="eb-hint">{{ t('empty.noLoreHint') }}</div>
      </div>
    </div>

    <div class="d-foot">
      <button class="mini-btn primary" @click="addEntry">{{ t('lore.newEntry') }}</button>
      <button v-if="cardLoreCount > 0" class="mini-btn" @click="syncFromCard">{{ t('lore.syncCard') }}</button>
      <button class="mini-btn" @click="pickFile">{{ t('lore.import') }}</button>
      <input ref="fileInput" type="file" accept=".json" hidden @change="onFilePicked" />
      <button class="mini-btn" @click="exportBook">{{ t('lore.export') }}</button>
    </div>
  </aside>
</template>

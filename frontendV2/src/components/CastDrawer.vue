<script setup lang="ts">
import { computed, ref } from 'vue'
import { useUiStore } from '@/stores/ui'
import { useChatStore } from '@/stores/chat'
import { useSessionStore } from '@/stores/session'
import { useFileUrls } from '@/composables/useFileUrl'
import { exportCompanion, importTavernJson, importTavernPng } from '@/api/companion'
import { useI18n } from '@/i18n'

const ui = useUiStore()
const chat = useChatStore()

const avatarMap = useFileUrls(
  () => chat.cast.map((c) => c.avatar).filter((a): a is string => Boolean(a)),
  128,
)
const session = useSessionStore()
const { t } = useI18n()

const query = ref('')

const visible = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return chat.cast
  return chat.cast.filter(
    (c) => c.name.toLowerCase().includes(q) || c.desc.toLowerCase().includes(q),
  )
})

function pick(id: string) {
  void session.selectCompanion(id)
}

function edit(id: string) {
  void session.selectCompanion(id).then(() => ui.openPanel('profile'))
}

const fileInput = ref<HTMLInputElement | null>(null)
const error = ref('')

function pickFile() {
  fileInput.value?.click()
}

async function onFilePicked(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  error.value = ''
  try {
    const r = file.name.toLowerCase().endsWith('.png')
      ? await importTavernPng(file)
      : await importTavernJson(file)
    await session.loadCompanions()
    if (r?.companion_id) await session.selectCompanion(r.companion_id)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function exportActive() {
  const id = session.activeCompanionId
  if (!id) return
  error.value = ''
  try {
    const blob = await exportCompanion(id, 'json')
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = id + '.json'
    a.click()
    URL.revokeObjectURL(url)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

function askDelete(id: string) {
  ui.askConfirm({
    title: t('dialog.deleteCharacter.title'),
    text: t('dialog.deleteCharacter.text'),
    onConfirm: () => chat.removeCast(id),
  })
}
</script>

<template>
  <aside class="drawer" :class="{ show: ui.isOpen('drawer') }">
    <div class="d-head">
      <span class="d-kicker">{{ t('cast.title') }}</span>
      <span class="d-title">{{ t('cast.title') }}</span>
      <button class="d-close" @click="ui.closePanel('drawer')">
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>

    <div class="cast-search">
      <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6" /><path d="M20 20l-4.2-4.2" /></svg>
      <input v-model="query" :placeholder="t('cast.searchPlaceholder')" aria-label="search cast" />
    </div>

    <div class="d-body">
      <div
        v-for="c in visible"
        :key="c.id"
        class="cast-item"
        :class="{ active: c.active }"
        role="button"
        tabindex="0"
        @click="pick(c.id)"
        @keydown.enter="pick(c.id)"
        @keydown.space.prevent="pick(c.id)"
      >
        <div class="cast-avatar">
          <img v-if="c.avatar" :src="avatarMap[c.avatar]" :alt="c.name" />
          <span v-else>{{ c.letter }}</span>
        </div>
        <div class="cast-main">
          <div class="cast-head">
            <div class="cast-name">{{ c.name }}</div>
            <button
              class="cast-pin"
              :class="{ on: c.pinned }"
              :aria-label="c.pinned ? t('cast.unpin') : t('cast.pin')"
              :data-tip="c.pinned ? t('cast.unpin') : t('cast.pin')"
              :aria-pressed="c.pinned ? 'true' : 'false'"
              @click.stop="session.togglePin(c.id)"
            >
              <svg viewBox="0 0 24 24">
                <path
                  d="M12 3.6l2.6 5.3 5.8.8-4.2 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8L3.6 9.7l5.8-.8z"
                />
              </svg>
            </button>
            <button class="cast-edit" :aria-label="t('cast.editCard')" @click.stop="edit(c.id)">
              <svg viewBox="0 0 24 24"><path d="M4 20h4L18 10l-4-4L4 16v4z" /><path d="M14 6l4 4" /></svg>
            </button>
          </div>
          <div class="cast-desc">{{ c.desc }}</div>
          <div class="cast-meta">{{ c.meta }}</div>
        </div>
        <button class="cast-del" aria-label="delete character" @click.stop="askDelete(c.id)">
          <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>
      <div v-if="error" class="empty-block"><div class="eb-hint">{{ error }}</div></div>
      <div v-if="!visible.length" class="empty-block">
        <div class="eb-title">{{ t('empty.noCast') }}</div>
        <div class="eb-hint">{{ t('empty.noCastHint') }}</div>
      </div>
    </div>

    <div class="d-foot">
      <button class="mini-btn primary" @click="pickFile">{{ t('cast.import') }}</button>
      <input ref="fileInput" type="file" accept=".json,.png" hidden @change="onFilePicked" />
      <button class="mini-btn" @click="ui.startNewCast()">{{ t('cast.new') }}</button>
      <button class="mini-btn" @click="exportActive">{{ t('cast.export') }}</button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from '@/i18n'
import { isDesktop } from '@/lib/window'

type Phase = 'idle' | 'checking' | 'available' | 'downloading' | 'ready' | 'current' | 'error'

type PendingUpdate = {
  version: string
  body?: string
  date?: string
  downloadAndInstall: (cb: (e: unknown) => void) => Promise<void>
}

const { t } = useI18n()

const currentVersion = typeof __APP_VERSION__ === 'string' ? __APP_VERSION__ : '0.1.0'
const supported = ref(false)
const panelOpen = ref(false)
const phase = ref<Phase>('idle')
const latestVersion = ref('')
const notes = ref('')
const released = ref('')
const percent = ref(0)
const errMsg = ref('')

let pending: PendingUpdate | null = null

const hasUpdate = computed(() => phase.value === 'available' || phase.value === 'ready')
const isBusy = computed(() => phase.value === 'checking' || phase.value === 'downloading')

const headerTitle = computed(() => {
  if (phase.value === 'checking' && !pending) return t('update.titleChecking')
  if (hasUpdate.value) return t('update.titleAvailable')
  return t('update.titleLatest')
})

function formatDate(iso?: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

async function checkNow(silent = false): Promise<void> {
  if (!supported.value || isBusy.value) return
  if (!silent) errMsg.value = ''
  phase.value = 'checking'
  try {
    const { check } = await import('@tauri-apps/plugin-updater')
    const found = await check()
    if (found) {
      const meta = found as unknown as PendingUpdate
      pending = meta
      latestVersion.value = meta.version
      notes.value = (meta.body ?? '').trim()
      released.value = formatDate(meta.date)
      phase.value = 'available'
      return
    }
    pending = null
    latestVersion.value = currentVersion
    notes.value = ''
    released.value = ''
    phase.value = 'current'
  } catch (err) {
    console.warn('[Updater] check failed', err)
    if (silent) {
      phase.value = 'idle'
    } else {
      errMsg.value = err instanceof Error ? err.message : String(err)
      phase.value = 'error'
    }
  }
}

async function install(): Promise<void> {
  if (!pending || isBusy.value) return
  phase.value = 'downloading'
  percent.value = 0
  errMsg.value = ''
  let total = 0
  let received = 0
  try {
    await pending.downloadAndInstall((event: unknown) => {
      const e = (event ?? {}) as {
        event?: string
        data?: { contentLength?: number; chunkLength?: number }
      }
      if (e.event === 'Started') {
        total = Number(e.data?.contentLength ?? 0)
      } else if (e.event === 'Progress') {
        received += Number(e.data?.chunkLength ?? 0)
        if (total > 0) {
          percent.value = Math.min(99, Math.round((received / total) * 100))
        }
      }
    })
    percent.value = 100
    phase.value = 'ready'
  } catch (err) {
    console.warn('[Updater] install failed', err)
    errMsg.value = err instanceof Error ? err.message : String(err)
    phase.value = 'error'
  }
}

async function restart(): Promise<void> {
  try {
    const { relaunch } = await import('@tauri-apps/plugin-process')
    await relaunch()
  } catch (err) {
    console.warn('[Updater] relaunch failed', err)
    errMsg.value = err instanceof Error ? err.message : String(err)
    phase.value = 'error'
  }
}

function togglePanel(): void {
  panelOpen.value = !panelOpen.value
  if (panelOpen.value && (phase.value === 'idle' || phase.value === 'error')) {
    void checkNow(false)
  }
}

function onDocDown(e: MouseEvent): void {
  if (!panelOpen.value) return
  const target = e.target as HTMLElement | null
  if (target?.closest('.update-wrap')) return
  panelOpen.value = false
}

function onKeyDown(e: KeyboardEvent): void {
  if (e.key === 'Escape' && panelOpen.value) {
    panelOpen.value = false
  }
}

onMounted(() => {
  supported.value = isDesktop()
  document.addEventListener('mousedown', onDocDown)
  window.addEventListener('keydown', onKeyDown)
  if (supported.value) {
    void checkNow(true)
  }
})

onUnmounted(() => {
  document.removeEventListener('mousedown', onDocDown)
  window.removeEventListener('keydown', onKeyDown)
})
</script>

<template>
  <div v-if="supported" class="update-wrap">
    <button
      class="dial-btn update-btn"
      :class="{ on: panelOpen, has: hasUpdate }"
      :data-tip="panelOpen ? undefined : t('rail.update')"
      @click="togglePanel"
    >
      <svg viewBox="0 0 24 24">
        <path d="M12 4v10" />
        <path d="M8 10l4 4 4-4" />
        <path d="M5 17v1.5A1.5 1.5 0 006.5 20h11a1.5 1.5 0 001.5-1.5V17" />
      </svg>
      <span v-if="hasUpdate" class="update-dot" aria-hidden="true"></span>
    </button>

    <div
      v-if="panelOpen"
      class="upd-panel"
      role="dialog"
      aria-label="Updates"
    >
      <div class="upd-head">
        <div class="upd-head-copy">
          <div class="upd-kicker">{{ t('update.kicker') }}</div>
          <div class="upd-title">{{ headerTitle }}</div>
        </div>
        <button
          class="upd-refresh"
          :class="{ spinning: phase === 'checking' }"
          :data-tip="t('update.refresh')"
          data-tip-float
          :disabled="isBusy || phase === 'ready'"
          @click="checkNow(false)"
        >
          <svg viewBox="0 0 24 24">
            <path d="M20 11a8.1 8.1 0 00-15.5-2M4 4v5h5" />
            <path d="M4 13a8.1 8.1 0 0015.5 2M20 20v-5h-5" />
          </svg>
        </button>
      </div>

      <div class="upd-versions">
        <span class="upd-ver">
          {{ t('update.current') }} <b>v{{ currentVersion }}</b>
        </span>
        <template v-if="latestVersion">
          <span class="upd-arrow">→</span>
          <span class="upd-ver" :class="{ hot: hasUpdate }">
            {{ t('update.latest') }} <b>v{{ latestVersion }}</b>
          </span>
        </template>
        <span v-if="released" class="upd-date">{{ released }}</span>
      </div>

      <div class="upd-body">
        <div v-if="phase === 'checking' && !pending" class="upd-state">
          {{ t('update.checking') }}
        </div>
        <div v-else-if="phase === 'error'" class="upd-state err">
          <div>{{ t('update.checkFailed') }}</div>
          <div v-if="errMsg" class="upd-err-detail">{{ errMsg }}</div>
        </div>
        <template v-else-if="hasUpdate">
          <div v-if="notes" class="upd-notes">{{ notes }}</div>
          <div v-else class="upd-state">{{ t('update.emptyNotes') }}</div>
        </template>
        <div v-else class="upd-state">
          {{ t('update.upToDate') }}
        </div>
      </div>

      <div v-if="phase === 'downloading' || phase === 'ready'" class="upd-progress-wrap">
        <div class="upd-progress-info">
          <span>
            {{
              phase === 'ready'
                ? t('update.ready')
                : t('update.downloading', { pct: String(percent) })
            }}
          </span>
        </div>
        <div class="upd-progress-bar">
          <i :style="{ width: `${percent}%` }"></i>
        </div>
      </div>

      <div class="upd-foot">
        <button
          class="mini-btn"
          :disabled="phase === 'downloading'"
          @click="panelOpen = false"
        >
          {{ t('update.later') }}
        </button>
        <button
          v-if="phase === 'ready'"
          class="mini-btn primary"
          @click="restart"
        >
          {{ t('update.restart') }}
        </button>
        <button
          v-else
          class="mini-btn"
          :class="{ primary: hasUpdate }"
          :disabled="!hasUpdate || isBusy"
          @click="install"
        >
          {{ t('update.update') }}
        </button>
      </div>
    </div>
  </div>
</template>

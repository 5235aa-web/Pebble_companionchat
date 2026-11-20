<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { useI18n } from '@/i18n'
import { useUpdaterStore } from '@/stores/updater'

const { t } = useI18n()
const updater = useUpdaterStore()

const headerTitle = computed(() => {
  if (updater.checking && !updater.info) return t('update.titleChecking')
  if (updater.hasUpdate) return t('update.titleAvailable')
  return t('update.titleLatest')
})

const publishedDate = computed(() => {
  const raw = updater.info?.publishedAt
  if (!raw) return ''
  const d = new Date(raw)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString()
})

const progressLabel = computed(() => {
  const st = updater.installStatus
  if (st.phase === 'launching') return t('update.launching')
  if (st.phase === 'downloading') {
    return t('update.downloading', { pct: String(st.percent || 0) })
  }
  return ''
})

function onDocDown(e: MouseEvent) {
  if (!updater.panelOpen) return
  const target = e.target as HTMLElement | null
  if (target?.closest('.update-wrap')) return
  updater.closePanel()
}

function onKeyDown(e: KeyboardEvent) {
  if (e.key === 'Escape' && updater.panelOpen) {
    updater.closePanel()
  }
}

onMounted(() => {
  document.addEventListener('mousedown', onDocDown)
  window.addEventListener('keydown', onKeyDown)
})

onUnmounted(() => {
  document.removeEventListener('mousedown', onDocDown)
  window.removeEventListener('keydown', onKeyDown)
})
</script>

<template>
  <div
    v-if="updater.panelOpen"
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
        :class="{ spinning: updater.checking }"
        :title="t('update.refresh')"
        :disabled="updater.checking || updater.isBusy"
        @click="updater.check(false)"
      >
        <svg viewBox="0 0 24 24">
          <path d="M20 11a8.1 8.1 0 00-15.5-2M4 4v5h5" />
          <path d="M4 13a8.1 8.1 0 0015.5 2M20 20v-5h-5" />
        </svg>
      </button>
    </div>

    <div v-if="updater.info" class="upd-versions">
      <span class="upd-ver">
        {{ t('update.current') }} <b>v{{ updater.info.currentVersion }}</b>
      </span>
      <template v-if="updater.info.hasRelease">
        <span class="upd-arrow">→</span>
        <span class="upd-ver" :class="{ hot: updater.hasUpdate }">
          {{ t('update.latest') }} <b>v{{ updater.info.latestVersion }}</b>
        </span>
      </template>
      <span v-if="publishedDate" class="upd-date">{{ publishedDate }}</span>
    </div>

    <div class="upd-body">
      <div v-if="updater.checking && !updater.info" class="upd-state">
        {{ t('update.checking') }}
      </div>
      <div v-else-if="updater.checkError" class="upd-state err">
        <div>{{ t('update.checkFailed') }}</div>
        <div class="upd-err-detail">{{ updater.checkError }}</div>
      </div>
      <div v-else-if="updater.info && !updater.info.hasRelease" class="upd-state">
        {{ t('update.noRelease') }}
      </div>
      <template v-else-if="updater.info">
        <div v-if="updater.info.releaseName" class="upd-release-name">
          {{ updater.info.releaseName }}
        </div>
        <div v-if="updater.info.notes?.trim()" class="upd-notes">
          {{ updater.info.notes }}
        </div>
        <div v-else class="upd-state">
          {{ updater.hasUpdate ? t('update.emptyNotes') : t('update.upToDate') }}
        </div>
      </template>
    </div>

    <div
      v-if="updater.isBusy || updater.installStatus.phase === 'error'"
      class="upd-progress-wrap"
    >
      <div v-if="updater.isBusy" class="upd-progress-info">
        <span>{{ progressLabel }}</span>
      </div>
      <div v-if="updater.isBusy" class="upd-progress-bar">
        <i :style="{ width: `${updater.installStatus.percent}%` }"></i>
      </div>
      <div
        v-if="updater.installStatus.phase === 'error'"
        class="upd-err-detail"
      >
        {{ t('update.installFailed') }}: {{ updater.installStatus.error }}
      </div>
    </div>

    <div class="upd-foot">
      <button
        class="mini-btn"
        :disabled="updater.isBusy"
        @click="updater.closePanel()"
      >
        {{ t('update.later') }}
      </button>
      <button
        class="mini-btn"
        :class="{ primary: updater.hasUpdate }"
        :disabled="!updater.hasUpdate || updater.isBusy"
        @click="updater.runInstall()"
      >
        {{ t('update.update') }}
      </button>
    </div>
  </div>
</template>

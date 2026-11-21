<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { CompanionAsset } from '@/api/companion'
import { fileUrl, useSocket } from '@/api/client'
import { useI18n } from '@/i18n'

const props = withDefaults(
  defineProps<{
    asset: CompanionAsset
    compact?: boolean
  }>(),
  {
    compact: false,
  },
)

const { t } = useI18n()

const videoEl = ref<HTMLVideoElement | null>(null)
const srcUrl = ref('')
const loading = ref(false)
const loadError = ref('')

const sizeLabel = computed(() => {
  const bytes = Number(props.asset.size_bytes || 0)
  if (!bytes) return ''
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
})

const metaLabel = computed(() => {
  const ext = (props.asset.ext || '').toUpperCase()
  return [ext, sizeLabel.value].filter(Boolean).join(' · ')
})

async function loadVideo(autoplay = false): Promise<void> {
  if (!props.asset.rel_path || loading.value) return
  loading.value = true
  loadError.value = ''
  try {
    srcUrl.value = await fileUrl(props.asset.rel_path, 0)
    if (autoplay && srcUrl.value) {
      await nextTick()
      void videoEl.value?.play().catch(() => undefined)
    }
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

watch(
  () => props.asset.rel_path,
  (path) => {
    srcUrl.value = ''
    loadError.value = ''
    // In browser HTTP mode `<video preload="metadata">` fetches only headers.
    // Over the Tauri WebSocket bridge, defer transferring large video blobs
    // until the user explicitly clicks play.
    if (path && !useSocket()) {
      void loadVideo(false)
    }
  },
  { immediate: true },
)
</script>

<template>
  <div class="inline-video-card" :class="{ compact }">
    <div class="iv-head">
      <span class="iv-title" :title="asset.name">{{ asset.name || 'video' }}</span>
      <span v-if="metaLabel" class="iv-meta">{{ metaLabel }}</span>
    </div>

    <div v-if="srcUrl" class="iv-stage">
      <video
        ref="videoEl"
        class="iv-player"
        :src="srcUrl"
        controls
        playsinline
        preload="metadata"
      ></video>
    </div>

    <button
      v-else
      type="button"
      class="iv-trigger"
      :disabled="loading"
      @click="loadVideo(true)"
    >
      <span class="iv-play-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24"><path d="M8 5.5v13l11-6.5z" /></svg>
      </span>
      <span class="iv-trigger-text">{{ loading ? '...' : t('card.assetPlayVideo') }}</span>
    </button>

    <div v-if="loadError" class="iv-err">{{ loadError }}</div>
  </div>
</template>

<style scoped>
.inline-video-card {
  border: 1px solid var(--line-2);
  border-radius: 6px;
  background: rgb(var(--ink-rgb) / 0.025);
  overflow: hidden;
  max-width: 520px;
}

.inline-video-card.compact {
  max-width: none;
}

.iv-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 7px 11px;
  border-bottom: 1px solid var(--line);
}

.iv-title {
  font-family: var(--serif);
  font-size: 13px;
  color: var(--ink-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.iv-meta {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.08em;
  color: var(--ink-4);
  flex: none;
}

.iv-stage {
  background: rgb(var(--shadow-rgb) / 0.35);
}

.iv-player {
  display: block;
  width: 100%;
  max-height: 320px;
  background: #000;
}

.inline-video-card.compact .iv-player {
  max-height: 220px;
}

.iv-trigger {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 9px;
  padding: 20px 14px;
  color: var(--ink-3);
  background: rgb(var(--ink-rgb) / 0.02);
  transition: color 0.2s ease, background-color 0.2s ease;
}

.iv-trigger:hover:not(:disabled) {
  color: var(--accent);
  background: rgb(var(--accent-rgb) / 0.06);
}

.iv-play-icon {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  border: 1px solid currentColor;
  display: grid;
  place-items: center;
}

.iv-play-icon svg {
  width: 12px;
  height: 12px;
  fill: currentColor;
}

.iv-trigger-text {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.iv-err {
  padding: 6px 11px;
  font-family: var(--mono);
  font-size: 10px;
  color: var(--ember);
  border-top: 1px solid var(--line);
}
</style>

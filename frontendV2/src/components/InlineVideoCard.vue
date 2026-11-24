<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import type { CompanionAsset } from '@/api/companion'
import { fileUrl } from '@/api/client'
import { useFileUrl } from '@/composables/useFileUrl'
import { useUiStore } from '@/stores/ui'
import { useI18n } from '@/i18n'
import { formatVideoDuration } from '@/utils/video'

export interface VideoAttachmentView {
  name?: string
  url: string
  posterUrl?: string
  sizeBytes?: number
  duration?: number
  ext?: string
}

let playerSeq = 0

const props = withDefaults(
  defineProps<{
    asset?: CompanionAsset
    video?: VideoAttachmentView
    compact?: boolean
    galleryTarget?: string
  }>(),
  {
    compact: false,
  },
)

const ui = useUiStore()
const { t } = useI18n()

const playerKey = `inline-video-${++playerSeq}`
const rootEl = ref<HTMLElement | null>(null)
const videoEl = ref<HTMLVideoElement | null>(null)
const srcUrl = ref('')
const loading = ref(false)
const loadError = ref('')

const videoPath = computed(() => props.asset?.rel_path || props.video?.url || '')
const videoName = computed(() => props.asset?.name || props.video?.name || 'video')
const posterPath = computed(() => props.video?.posterUrl || undefined)
const posterSrc = useFileUrl(posterPath, 512)

const active = computed(() => Boolean(srcUrl.value) && ui.activeVideoId === playerKey)

const sizeLabel = computed(() => {
  const bytes = Number(props.asset?.size_bytes || props.video?.sizeBytes || 0)
  if (!bytes) return ''
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
})

const durationLabel = computed(() => formatVideoDuration(props.video?.duration))

const extLabel = computed(() => {
  const raw = props.asset?.ext || props.video?.ext || videoName.value.split('.').pop() || ''
  return raw.toUpperCase().slice(0, 5)
})

const metaLabel = computed(() =>
  [durationLabel.value, extLabel.value, sizeLabel.value].filter(Boolean).join(' · '),
)

function unloadVideo(): void {
  if (videoEl.value) {
    try {
      videoEl.value.pause()
      videoEl.value.removeAttribute('src')
      videoEl.value.load()
    } catch {
      /* ignore */
    }
  }
  srcUrl.value = ''
}

function stopVideo(): void {
  if (ui.activeVideoId === playerKey) {
    ui.setActiveVideo(null)
  }
  unloadVideo()
}

async function playVideo(): Promise<void> {
  if (!videoPath.value || loading.value) return
  ui.setActiveVideo(playerKey)
  loading.value = true
  loadError.value = ''
  try {
    const resolved = await fileUrl(videoPath.value, 0)
    if (ui.activeVideoId !== playerKey) return
    srcUrl.value = resolved
    await nextTick()
    void videoEl.value?.play().catch(() => undefined)
  } catch (err) {
    if (ui.activeVideoId === playerKey) ui.setActiveVideo(null)
    loadError.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

function openInGallery(): void {
  if (!props.galleryTarget) return
  stopVideo()
  ui.galleryTarget = props.galleryTarget
  ui.openPanel('gallery')
}

watch(
  () => ui.activeVideoId,
  (id) => {
    if (id !== playerKey && srcUrl.value) {
      unloadVideo()
    }
  },
)

watch(videoPath, () => {
  stopVideo()
  loadError.value = ''
})

let observer: IntersectionObserver | null = null

onMounted(() => {
  if (typeof IntersectionObserver !== 'undefined' && rootEl.value) {
    observer = new IntersectionObserver(
      (entries) => {
        const entry = entries[0]
        if (entry && !entry.isIntersecting && active.value && videoEl.value?.paused) {
          stopVideo()
        }
      },
      { threshold: 0 },
    )
    observer.observe(rootEl.value)
  }
})

onUnmounted(() => {
  observer?.disconnect()
  observer = null
  stopVideo()
})
</script>

<template>
  <div ref="rootEl" class="inline-video-card" :class="{ compact }">
    <div class="iv-head">
      <span class="iv-title" :title="videoName">{{ videoName }}</span>
      <span v-if="metaLabel" class="iv-meta">{{ metaLabel }}</span>
      <button
        v-if="galleryTarget"
        type="button"
        class="iv-act"
        :title="t('picture.openAlbum')"
        :aria-label="t('picture.openAlbum')"
        @click="openInGallery"
      >
        <svg viewBox="0 0 24 24">
          <rect x="3.5" y="5" width="17" height="14" rx="1.5" />
          <path d="M8.5 10l4 3 4-3" />
        </svg>
      </button>
      <button
        v-if="active"
        type="button"
        class="iv-act"
        :title="t('card.assetStopVideo')"
        :aria-label="t('card.assetStopVideo')"
        @click="stopVideo"
      >
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>

    <div v-if="active" class="iv-stage">
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
      :class="{ 'has-poster': Boolean(posterSrc) }"
      :disabled="loading"
      @click="playVideo"
    >
      <img v-if="posterSrc" class="iv-poster" :src="posterSrc" :alt="videoName" />
      <span class="iv-overlay">
        <span class="iv-play-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M8 5.5v13l11-6.5z" /></svg>
        </span>
        <span class="iv-trigger-text">{{ loading ? '...' : t('card.assetPlayVideo') }}</span>
      </span>
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
  gap: 8px;
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
  flex: 1;
}

.iv-meta {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.08em;
  color: var(--ink-4);
  flex: none;
}

.iv-act {
  width: 22px;
  height: 22px;
  display: grid;
  place-items: center;
  color: var(--ink-4);
  border-radius: 4px;
  flex: none;
  transition: color 0.2s ease, background-color 0.2s ease;
}

.iv-act:hover {
  color: var(--accent);
  background: rgb(var(--accent-rgb) / 0.08);
}

.iv-act svg {
  width: 13px;
  height: 13px;
  stroke: currentColor;
  fill: none;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
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
  position: relative;
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px 14px;
  color: var(--ink-3);
  background: rgb(var(--ink-rgb) / 0.02);
  overflow: hidden;
  transition: color 0.2s ease, background-color 0.2s ease;
}

.iv-trigger.has-poster {
  padding: 0;
  min-height: 140px;
  max-height: 240px;
  background: #0b0b0d;
}

.inline-video-card.compact .iv-trigger.has-poster {
  min-height: 110px;
  max-height: 170px;
}

.iv-poster {
  display: block;
  width: 100%;
  max-height: 240px;
  object-fit: cover;
  opacity: 0.72;
  transition: opacity 0.25s ease, transform 0.35s ease;
}

.inline-video-card.compact .iv-poster {
  max-height: 170px;
}

.iv-trigger:hover:not(:disabled) .iv-poster {
  opacity: 0.9;
  transform: scale(1.015);
}

.iv-overlay {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 9px;
}

.iv-trigger.has-poster .iv-overlay {
  position: absolute;
  inset: 0;
  background: linear-gradient(180deg, transparent, rgba(0, 0, 0, 0.48));
  color: #f4f2ee;
}

.iv-trigger:hover:not(:disabled) {
  color: var(--accent);
  background: rgb(var(--accent-rgb) / 0.06);
}

.iv-trigger.has-poster:hover:not(:disabled) {
  color: #fff;
  background: #0b0b0d;
}

.iv-play-icon {
  width: 30px;
  height: 30px;
  border-radius: 50%;
  border: 1px solid currentColor;
  background: rgba(0, 0, 0, 0.28);
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

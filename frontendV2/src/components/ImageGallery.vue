<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useUiStore } from '@/stores/ui'
import { useSessionStore } from '@/stores/session'
import { useI18n } from '@/i18n'
import { fileUrl } from '@/api/client'
import { useFileUrls } from '@/composables/useFileUrl'
import { deleteBranchImage, getBranchImages, type BranchImage } from '@/api/gallery'
import { deleteGroupImage, getGroupImages, type GroupImage } from '@/api/group'
import { useChatStore } from '@/stores/chat'
import { usePictureStore } from '@/stores/pictures'
import type { LabelKey } from '@/i18n'
import { formatDateTime } from '@/utils/time'
import { formatVideoDuration } from '@/utils/video'

const ui = useUiStore()
const session = useSessionStore()
const chat = useChatStore()
const pictures = usePictureStore()
const { t } = useI18n()

/**
 * Two albums in one drawer.
 *
 * Scene pictures and attachments are separate lists with separate lifetimes:
 * a generated picture lives in its own table, an attachment lives in message
 * metadata. They share this panel because the user thinks of both as "images",
 * and nothing else.
 */
type Tab = 'scene' | 'files' | 'group'
const tab = ref<Tab>('scene')

/* ---- attachments ---- */
const fileItems = ref<BranchImage[]>([])
const groupItems = ref<GroupImage[]>([])
/** The album is one viewer with three sources; the tab picks which one it reads. */
const items = computed<Array<BranchImage | GroupImage>>(() =>
  tab.value === 'group' ? groupItems.value : fileItems.value,
)
const index = ref(0)
const fileError = ref('')

function isVideoItem(it: Partial<BranchImage> | null | undefined): boolean {
  return Boolean(it && it.kind === 'video')
}

function thumbPathOf(it: Partial<BranchImage> | null | undefined): string {
  if (!it) return ''
  return isVideoItem(it) ? it.poster_url || '' : it.url || ''
}

// Only fetch image URLs and lightweight video poster URLs here; full video
// blobs are fetched lazily only when the user explicitly clicks Play.
const urlMap = useFileUrls(
  () => items.value.map((i) => thumbPathOf(i)).filter(Boolean),
  0,
)

const fileItem = computed(() => items.value[index.value] ?? null)
const fileIsVideo = computed(() => isVideoItem(fileItem.value))
const fileCurrent = computed(() => {
  const path = thumbPathOf(fileItem.value)
  return path ? urlMap.value[path] || '' : ''
})
const fileName = computed(() => fileItem.value?.name ?? (fileIsVideo.value ? 'video' : 'image'))
const fileMeta = computed(() => {
  const it = fileItem.value
  if (!it) return ''
  const bytes = it.size || 0
  const sizeStr =
    bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`
  const dur = isVideoItem(it) ? formatVideoDuration((it as BranchImage).duration) : ''
  const dims = `${it.width || 0} x ${it.height || 0}`
  return [dur, dims, sizeStr].filter(Boolean).join(' · ')
})

/* ---- throttled gallery video playback ---- */
const GALLERY_VIDEO_KEY = 'gallery-video'
const videoEl = ref<HTMLVideoElement | null>(null)
const videoSrc = ref('')
const videoLoading = ref(false)
const videoPlaying = computed(
  () => Boolean(videoSrc.value) && ui.activeVideoId === GALLERY_VIDEO_KEY,
)

function unloadGalleryVideo() {
  if (videoEl.value) {
    try {
      videoEl.value.pause()
      videoEl.value.removeAttribute('src')
      videoEl.value.load()
    } catch {
      /* ignore */
    }
  }
  videoSrc.value = ''
}

function stopGalleryVideo() {
  if (ui.activeVideoId === GALLERY_VIDEO_KEY) {
    ui.setActiveVideo(null)
  }
  unloadGalleryVideo()
}

async function playGalleryVideo() {
  const it = fileItem.value
  if (!it || !isVideoItem(it) || videoLoading.value) return
  ui.setActiveVideo(GALLERY_VIDEO_KEY)
  videoLoading.value = true
  fileError.value = ''
  try {
    const resolved = await fileUrl(it.url, 0)
    if (ui.activeVideoId !== GALLERY_VIDEO_KEY) return
    videoSrc.value = resolved
    await nextTick()
    void videoEl.value?.play().catch(() => undefined)
  } catch (e) {
    if (ui.activeVideoId === GALLERY_VIDEO_KEY) ui.setActiveVideo(null)
    fileError.value = e instanceof Error ? e.message : String(e)
  } finally {
    videoLoading.value = false
  }
}

/** Locale lookup for a free-form source tag, with the raw tag as fallback. */
function sourceLabel(source: string): string {
  return t(`picture.source.${source}` as LabelKey)
}

function goFile(step: number) {
  const n = items.value.length
  if (!n) return
  index.value = (index.value + step + n) % n
}

async function loadFiles() {
  fileItems.value = []
  index.value = 0
  const cid = session.activeCompanionId
  const bid = session.activeBranch
  if (!cid || !bid) return
  try {
    const r = await getBranchImages(cid, bid)
    fileItems.value = r.images
    fileError.value = ''
  } catch (e) {
    fileError.value = e instanceof Error ? e.message : String(e)
  }
}

/** Pictures sent into the open room. Empty until the stage has opened one. */
async function loadGroup() {
  groupItems.value = []
  index.value = 0
  if (!ui.groupRoomId) return
  try {
    const r = await getGroupImages(ui.groupRoomId)
    groupItems.value = r.images
    fileError.value = ''
  } catch (e) {
    fileError.value = e instanceof Error ? e.message : String(e)
  }
}

async function removeFile() {
  const it = items.value[index.value]
  if (!it) return
  try {
    if (tab.value === 'group') {
      await deleteGroupImage(ui.groupRoomId, it.url)
      await loadGroup()
      ui.bumpGroupEpoch()
      return
    }
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) return
    await deleteBranchImage(cid, bid, it.url)
    await loadFiles()
    await chat.loadMessages()
  } catch (e) {
    fileError.value = e instanceof Error ? e.message : String(e)
  }
}

/** Hand the current attachment back to the composer as a pending attachment. */
function quoteFile() {
  const item = items.value[index.value] as BranchImage | undefined
  if (!item) return
  ui.quotedImage = {
    url: item.url,
    name: item.name,
    kind: isVideoItem(item) ? 'video' : 'image',
    poster_url: item.poster_url,
    duration: item.duration,
    mime: item.mime,
    size: item.size,
    width: item.width,
    height: item.height,
    frames: item.frames,
    transcript: item.transcript,
  }
  ui.closePanel('gallery')
}

function askDeleteFile() {
  const item = items.value[index.value]
  if (!item) return
  ui.askConfirm({
    title: t('picture.delete'),
    text: item.name,
    confirmLabel: t('picture.delete'),
    danger: true,
    onConfirm: removeFile,
  })
}

/* ---- generated scenes ---- */
const sceneId = ref<number | null>(null)
const sceneUrlMap = useFileUrls(() => pictures.doneItems.map((p) => p.url), 0)

const scene = computed(() => {
  const list = pictures.doneItems
  if (!list.length) return null
  const hit = list.find((p) => p.id === sceneId.value)
  return hit ?? list[0]
})
const sceneUrl = computed(() => (scene.value ? sceneUrlMap.value[scene.value.url] || '' : ''))
const sceneMeta = computed(() => {
  const it = scene.value
  if (!it) return ''
  const kb = Math.round((it.size || 0) / 1024)
  return `${it.width || 0} x ${it.height || 0} · ${kb} KB`
})

function pickScene(id: number) {
  sceneId.value = id
}

function formatTime(raw: string): string {
  return formatDateTime(raw)
}

async function download(url: string, name: string) {
  const a = document.createElement('a')
  a.href = await fileUrl(url)
  a.download = name || 'image'
  a.click()
}

async function removeScene() {
  const it = scene.value
  if (!it) return
  try {
    await pictures.remove(it.id)
    sceneId.value = null
  } catch (e) {
    fileError.value = e instanceof Error ? e.message : String(e)
  }
}

function askDeleteScene() {
  const it = scene.value
  if (!it) return
  ui.askConfirm({
    title: t('picture.delete'),
    text: `${sourceLabel(it.source)} · ${sceneMeta.value}`,
    confirmLabel: t('picture.delete'),
    danger: true,
    onConfirm: removeScene,
  })
}

async function retryScene() {
  const it = scene.value
  if (!it) return
  try {
    await pictures.retry(it.id)
  } catch (e) {
    fileError.value = e instanceof Error ? e.message : String(e)
  }
}

/* ---- centered image lightbox viewer ---- */
const lightboxOpen = ref(false)
const lightboxSrc = ref('')
const lightboxAlt = ref('')
const zoom = ref(1)
const panX = ref(0)
const panY = ref(0)
const dragging = ref(false)
let panPointerId: number | null = null
let dragStartX = 0
let dragStartY = 0
let basePanX = 0
let basePanY = 0
let didPan = false

const MIN_ZOOM = 0.25
const MAX_ZOOM = 4
const ZOOM_STEP = 0.25

const lightboxStyle = computed(() => ({
  transform: `translate3d(${panX.value}px, ${panY.value}px, 0) scale(${zoom.value})`,
}))

function resetZoom() {
  zoom.value = 1
  panX.value = 0
  panY.value = 0
}

function setZoom(next: number) {
  const clamped = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Math.round(next * 100) / 100))
  zoom.value = clamped
  if (clamped <= 1) {
    panX.value = 0
    panY.value = 0
  }
}

function zoomBy(delta: number) {
  setZoom(zoom.value + delta)
}

function openLightbox(src: string, alt: string) {
  if (!src) return
  lightboxSrc.value = src
  lightboxAlt.value = alt
  resetZoom()
  lightboxOpen.value = true
}

function closeLightbox() {
  lightboxOpen.value = false
  dragging.value = false
  panPointerId = null
  resetZoom()
}

function onLightboxWheel(e: WheelEvent) {
  const delta = e.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP
  zoomBy(delta)
}

function onLightboxDoubleClick() {
  if (zoom.value > 1) resetZoom()
  else setZoom(2)
}

function onLightboxPointerDown(e: PointerEvent) {
  if (zoom.value <= 1) return
  panPointerId = e.pointerId
  dragStartX = e.clientX
  dragStartY = e.clientY
  basePanX = panX.value
  basePanY = panY.value
  didPan = false
  dragging.value = true
  ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
}

function onLightboxPointerMove(e: PointerEvent) {
  if (panPointerId === null || e.pointerId !== panPointerId) return
  const dx = e.clientX - dragStartX
  const dy = e.clientY - dragStartY
  if (Math.abs(dx) > 3 || Math.abs(dy) > 3) didPan = true
  panX.value = basePanX + dx
  panY.value = basePanY + dy
}

function onLightboxPointerUp(e: PointerEvent) {
  if (panPointerId === null || e.pointerId !== panPointerId) return
  panPointerId = null
  dragging.value = false
  try {
    ;(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId)
  } catch {
    /* ignore */
  }
}

function onStageBackdropClick() {
  if (didPan) {
    didPan = false
    return
  }
  closeLightbox()
}

function onKeyDown(e: KeyboardEvent) {
  if (!lightboxOpen.value) return
  if (e.key === 'Escape') {
    e.preventDefault()
    closeLightbox()
  } else if (e.key === '+' || e.key === '=') {
    e.preventDefault()
    zoomBy(ZOOM_STEP)
  } else if (e.key === '-' || e.key === '_') {
    e.preventDefault()
    zoomBy(-ZOOM_STEP)
  } else if (e.key === '0') {
    e.preventDefault()
    resetZoom()
  }
}

watch(lightboxOpen, (open) => {
  if (open) window.addEventListener('keydown', onKeyDown)
  else window.removeEventListener('keydown', onKeyDown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKeyDown)
  stopGalleryVideo()
})

watch(
  () => ui.activeVideoId,
  (id) => {
    if (id !== GALLERY_VIDEO_KEY && videoSrc.value) {
      unloadGalleryVideo()
    }
  },
)

watch(index, () => {
  stopGalleryVideo()
})

watch(tab, () => {
  stopGalleryVideo()
  // Each album has its own length; an index kept from the previous one would
  // point past the end and show an empty stage.
  index.value = 0
})

// Switching rooms while the album is open has to swap the room album too,
// otherwise the grid keeps showing the previous room's pictures.
watch(
  () => ui.groupRoomId,
  () => {
    if (ui.isOpen('gallery')) void loadGroup()
  },
)

watch(
  () => ui.isOpen('gallery'),
  async (open) => {
    if (!open) {
      closeLightbox()
      stopGalleryVideo()
      return
    }
    await Promise.all([loadFiles(), loadGroup(), pictures.ensureLoaded()])
    sceneId.value = null

    // A caller can ask for one attachment by url; honour that and switch tab,
    // otherwise the panel would open on a different album than requested.
    const target = ui.galleryTarget
    ui.galleryTarget = ''
    if (!target) {
      index.value = 0
      return
    }
    const inGroup = groupItems.value.findIndex((it) => it.url === target)
    if (inGroup >= 0) {
      tab.value = 'group'
      index.value = inGroup
      return
    }
    const at = items.value.findIndex((it) => it.url === target)
    if (at >= 0) {
      tab.value = 'files'
      index.value = at
    }
  },
  { immediate: true },
)
</script>

<template>
  <aside class="panel album" :class="{ show: ui.isOpen('gallery') }">
    <div class="d-head album-head">
      <span class="d-kicker">{{ t('picture.kicker') }}</span>
      <span class="d-title">{{ t('picture.title') }}</span>
      <button class="d-close" aria-label="close" @click="ui.closePanel('gallery')">
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>

    <div class="album-tabs">
      <button class="album-tab" :class="{ on: tab === 'scene' }" @click="tab = 'scene'">
        <span>{{ t('picture.tabScenes') }}</span>
        <span class="album-tab-n">{{ pictures.doneItems.length }}</span>
      </button>
      <button class="album-tab" :class="{ on: tab === 'files' }" @click="tab = 'files'">
        <span>{{ t('picture.tabFiles') }}</span>
        <span class="album-tab-n">{{ fileItems.length }}</span>
      </button>
      <button class="album-tab" :class="{ on: tab === 'group' }" @click="tab = 'group'">
        <span>{{ t('picture.tabGroup') }}</span>
        <span class="album-tab-n">{{ groupItems.length }}</span>
      </button>
    </div>

    <div class="d-body album-body">
      <!-- generated scenes -->
      <template v-if="tab === 'scene'">
        <div v-if="pictures.generating" class="album-flag">
          <span class="album-pulse"></span>{{ t('picture.generating') }}
        </div>
        <p v-if="fileError || pictures.error" class="album-error">{{ fileError || pictures.error }}</p>

        <div v-if="!pictures.doneItems.length && !pictures.generating" class="album-empty">
          <div class="album-empty-mark">
            <svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">
              <rect x="3.5" y="5" width="17" height="14" rx="1.5" />
              <path d="M3.5 15l4.5-4 4 3.5 3-2.5 5.5 4.5" />
              <circle cx="9" cy="9.5" r="1.3" />
            </svg>
          </div>
          <p>{{ t('picture.emptyScenes') }}</p>
        </div>

        <template v-else>
          <div class="album-stage">
            <img
              v-if="sceneUrl"
              :src="sceneUrl"
              :alt="scene?.prompt || 'scene'"
              @click="openLightbox(sceneUrl, scene?.prompt || 'scene')"
            />
            <div v-else class="album-loading">{{ t('picture.loading') }}</div>
          </div>

          <div v-if="scene" class="album-detail">
            <div class="album-detail-head">
              <span class="album-badge">{{ sourceLabel(scene.source) }}</span>
              <span class="album-dim">{{ sceneMeta }}</span>
              <span class="album-dim">{{ scene.provider }}</span>
            </div>
            <p class="album-prompt">{{ scene.prompt }}</p>
            <div class="album-detail-foot">
              <span class="album-dim">{{ formatTime(scene.created_at) }}</span>
              <span v-if="scene.status === 'failed'" class="album-fail">{{ scene.error }}</span>
            </div>
            <div class="album-acts">
              <button class="album-btn" @click="download(scene.url, `scene-${scene.id}.png`)">
                {{ t('picture.download') }}
              </button>
              <button
                v-if="scene.status === 'failed'"
                class="album-btn"
                @click="retryScene"
              >
                {{ t('picture.retry') }}
              </button>
              <button class="album-btn danger" @click="askDeleteScene">{{ t('picture.delete') }}</button>
            </div>
          </div>

          <div class="album-grid">
            <button
              v-for="p in pictures.doneItems"
              :key="p.id"
              class="album-cell"
              :class="{ on: scene?.id === p.id }"
              @click="pickScene(p.id)"
            >
              <img :src="sceneUrlMap[p.url]" :alt="p.prompt" />
            </button>
          </div>
        </template>
      </template>

      <!-- attachments: the single thread's files, and the open room's pictures -->
      <template v-else-if="tab === 'files' || tab === 'group'">
        <p v-if="fileError" class="album-error">{{ fileError }}</p>

        <div v-if="!items.length" class="album-empty">
          <div class="album-empty-mark">
            <svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 16V4" /><path d="M7 9l5-5 5 5" />
              <path d="M4 16v3.5h16V16" />
            </svg>
          </div>
          <p>{{ tab === 'group' ? t('picture.emptyGroup') : t('picture.emptyFiles') }}</p>
        </div>

        <template v-else>
          <div class="album-stage">
            <template v-if="fileIsVideo">
              <div v-if="videoPlaying" class="album-video-wrap">
                <video
                  ref="videoEl"
                  class="album-video-player"
                  :src="videoSrc"
                  controls
                  playsinline
                  preload="metadata"
                ></video>
                <button
                  type="button"
                  class="album-video-stop"
                  :data-tip="t('card.assetStopVideo')"
                  data-tip-float
                  :aria-label="t('card.assetStopVideo')"
                  @click="stopGalleryVideo"
                >
                  <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
                </button>
              </div>
              <button
                v-else
                type="button"
                class="album-video-trigger"
                :disabled="videoLoading"
                @click="playGalleryVideo"
              >
                <img v-if="fileCurrent" class="album-video-poster" :src="fileCurrent" :alt="fileName" />
                <span class="album-video-overlay">
                  <span class="album-video-play" aria-hidden="true">
                    <svg viewBox="0 0 24 24"><path d="M8 5.5v13l11-6.5z" /></svg>
                  </span>
                  <span class="album-video-label">{{
                    videoLoading ? t('picture.loading') : t('card.assetPlayVideo')
                  }}</span>
                </span>
              </button>
            </template>
            <img
              v-else-if="fileCurrent"
              :src="fileCurrent"
              :alt="fileName"
              @click="openLightbox(fileCurrent, fileName)"
            />
            <div v-else class="album-loading">{{ t('picture.loading') }}</div>
          </div>

          <div class="album-detail">
            <div class="album-detail-head">
              <span class="album-badge">{{
                tab === 'group' ? t('picture.tabGroup') : t('picture.tabFiles')
              }}</span>
              <span class="album-dim">{{ fileMeta }}</span>
              <span class="album-dim">{{ index + 1 }} / {{ items.length }}</span>
            </div>
            <p class="album-prompt album-prompt-soft">{{ fileName }}</p>
            <div class="album-acts">
              <button v-if="tab !== 'group'" class="album-btn" @click="quoteFile">
                {{ t('picture.quote') }}
              </button>
              <button
                class="album-btn"
                @click="fileItem && download(fileItem.url, fileItem.name)"
              >
                {{ t('picture.download') }}
              </button>
              <button class="album-btn danger" @click="askDeleteFile">{{ t('picture.delete') }}</button>
            </div>
          </div>

          <div class="album-grid">
            <button
              v-for="(it, i) in items"
              :key="it.url"
              class="album-cell"
              :class="{ on: i === index }"
              @click="index = i"
            >
              <img v-if="thumbPathOf(it) && urlMap[thumbPathOf(it)]" :src="urlMap[thumbPathOf(it)]" :alt="it.name" />
              <span v-else class="album-cell-blank">{{ isVideoItem(it) ? 'VID' : '' }}</span>
              <span v-if="isVideoItem(it)" class="album-cell-video" aria-hidden="true">▶</span>
            </button>
          </div>

          <div class="album-nav">
            <button class="album-nav-btn" :aria-label="t('picture.prev')" @click="goFile(-1)">
              <svg viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7" /></svg>
            </button>
            <button class="album-nav-btn" :aria-label="t('picture.next')" @click="goFile(1)">
              <svg viewBox="0 0 24 24"><path d="M9 5l7 7-7 7" /></svg>
            </button>
          </div>
        </template>
      </template>
    </div>

    <Teleport to="body">
      <div
        v-if="lightboxOpen"
        class="album-lightbox"
        role="dialog"
        aria-modal="true"
        @wheel.prevent="onLightboxWheel"
      >
        <div class="album-lb-bar" @click.stop>
          <button
            type="button"
            class="album-lb-btn"
            :disabled="zoom <= MIN_ZOOM"
            :aria-label="t('picture.zoomOut')"
            :data-tip="t('picture.zoomOut')"
            data-tip-float
            @click="zoomBy(-ZOOM_STEP)"
          >
            <svg viewBox="0 0 24 24"><path d="M6 12h12" /></svg>
          </button>
          <button
            type="button"
            class="album-lb-btn album-lb-pct"
            :data-tip="t('picture.zoomReset')"
            data-tip-float
            @click="resetZoom"
          >
            {{ Math.round(zoom * 100) }}%
          </button>
          <button
            type="button"
            class="album-lb-btn"
            :disabled="zoom >= MAX_ZOOM"
            :aria-label="t('picture.zoomIn')"
            :data-tip="t('picture.zoomIn')"
            data-tip-float
            @click="zoomBy(ZOOM_STEP)"
          >
            <svg viewBox="0 0 24 24"><path d="M12 6v12M6 12h12" /></svg>
          </button>
        </div>

        <button
          type="button"
          class="album-lb-close"
          :aria-label="t('picture.closeViewer')"
          :data-tip="t('picture.closeViewer')"
          data-tip-float
          @click="closeLightbox"
        >
          <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>

        <div class="album-lb-stage" @click="onStageBackdropClick">
          <img
            class="album-lb-img"
            :class="{ zoomed: zoom > 1, dragging }"
            :src="lightboxSrc"
            :alt="lightboxAlt"
            :style="lightboxStyle"
            draggable="false"
            @click.stop
            @dblclick.stop="onLightboxDoubleClick"
            @pointerdown="onLightboxPointerDown"
            @pointermove="onLightboxPointerMove"
            @pointerup="onLightboxPointerUp"
            @pointercancel="onLightboxPointerUp"
          />
        </div>
      </div>
    </Teleport>
  </aside>
</template>

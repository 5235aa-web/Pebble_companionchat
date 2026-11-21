<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import ChatBeat from './ChatBeat.vue'
import RichMessage from './RichMessage.vue'
import InlineVideoCard from './InlineVideoCard.vue'
import { useChatStore } from '@/stores/chat'
import { useSessionStore } from '@/stores/session'
import { useUiStore } from '@/stores/ui'
import { useCardAudioStore } from '@/stores/cardAudio'
import { useFileUrls } from '@/composables/useFileUrl'
import { extractInlineMediaFromText } from '@/utils/inlineMedia'
import { useI18n } from '@/i18n'
import { summaryText } from '@/data/chat'

const chat = useChatStore()
const session = useSessionStore()
const ui = useUiStore()
const cardAudio = useCardAudioStore()
const { t } = useI18n()

const greetingMedia = computed(() => {
  const raw = session.emptyLine
  if (!raw) return { images: [], htmlImages: [], audios: [], videos: [], cleanedText: '' }
  return extractInlineMediaFromText(raw, (refText) => session.findAssetByRef(refText))
})

const greetingImageMap = useFileUrls(
  () =>
    [...greetingMedia.value.images, ...greetingMedia.value.htmlImages]
      .map((a) => a.rel_path)
      .filter(Boolean),
  512,
)

const greetingAssetUrls = computed<Record<number, string>>(() => {
  const out: Record<number, string> = {}
  for (const a of greetingMedia.value.htmlImages) {
    const url = greetingImageMap.value[a.rel_path]
    if (url) out[a.id] = url
  }
  return out
})

function openGreetingImage(src: string) {
  ui.galleryTarget = src
  ui.openPanel('gallery')
}

const scroller = ref<HTMLElement | null>(null)
const threadEl = ref<HTMLElement | null>(null)
let stickToBottom = true

function atBottom(): boolean {
  const el = scroller.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight < 60
}

function scrollToEnd(smooth = false) {
  const el = scroller.value
  if (!el) return
  if (smooth) {
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  } else {
    // Assign scrollTop directly: scroll-behavior:smooth is set on .scroller, so
    // behavior:'auto' would animate and the stream would outrun it.
    el.scrollTop = el.scrollHeight
  }
  chat.showJump = false
}

/**
 * Scrollbar fade.
 *
 * The native thumb stays hidden while the thread is at rest so the reading
 * column has no permanent grey rail down its edge. Any wheel, drag or keyboard
 * scroll brings it back, and it fades out again once the gesture settles.
 */
const scrolling = ref(false)
let scrollbarTimer = 0

function markScrolling() {
  scrolling.value = true
  window.clearTimeout(scrollbarTimer)
  scrollbarTimer = window.setTimeout(() => (scrolling.value = false), 1200)
}

function onWheel(e: WheelEvent) {
  if (e.deltaY < 0) stickToBottom = false
}

/**
 * Throttle to one sync per frame.
 *
 * `atBottom()` reads scrollHeight, which forces the browser to flush pending
 * layout. Running that on every scroll event during a fast wheel gesture is
 * the classic layout-thrash pattern; at most one read per frame is enough to
 * decide whether the jump button belongs on screen.
 */
let raf = 0
function onScroll() {
  markScrolling()
  if (raf) return
  raf = requestAnimationFrame(() => {
    raf = 0
    const bottom = atBottom()
    chat.showJump = !bottom
    stickToBottom = bottom
  })
}

let resizeObserver: ResizeObserver | null = null

onUnmounted(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  if (raf) cancelAnimationFrame(raf)
  window.clearTimeout(scrollbarTimer)
})

watch(
  () => chat.beats.length,
  async () => {
    stickToBottom = true
    await nextTick()
    scrollToEnd(true)
  },
)

watch(
  () => chat.scrollRequest,
  () => {
    stickToBottom = true
    scrollToEnd(true)
  },
)

onMounted(async () => {
  await nextTick()
  scrollToEnd()
  if (threadEl.value && typeof ResizeObserver !== 'undefined') {
    // Follow height growth once per rendered frame rather than probing
    // scrollHeight synchronously on every incoming token.
    resizeObserver = new ResizeObserver(() => {
      if (stickToBottom && chat.busy) scrollToEnd()
    })
    resizeObserver.observe(threadEl.value)
  }
})
</script>

<template>
  <main
    ref="scroller"
    class="scroller"
    :class="{ scrolling }"
    @scroll.passive="onScroll"
    @wheel.passive="onWheel"
  >
    <div ref="threadEl" class="thread">
      <div class="axis"></div>

      <div v-if="!chat.beats.length" class="empty-stage show">
        <div class="es-mark">&ldquo;</div>
        <div class="es-head">
          <div v-if="chat.activeCast?.name" class="es-name">{{ chat.activeCast?.name }}</div>
          <div v-if="session.greetings.length > 1" class="es-greet-nav">
            <button
              type="button"
              class="es-greet-btn"
              :aria-label="t('card.greetingPrev')"
              @click="session.cycleGreeting(-1)"
            >
              <svg viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6" /></svg>
            </button>
            <span class="es-greet-idx">
              {{ session.greetingIndex + 1 }} / {{ session.greetings.length }}
            </span>
            <button
              type="button"
              class="es-greet-btn"
              :aria-label="t('card.greetingNext')"
              @click="session.cycleGreeting(1)"
            >
              <svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6" /></svg>
            </button>
          </div>
        </div>
        <div v-if="greetingMedia.images.length" class="attach-row es-media">
          <div
            v-for="a in greetingMedia.images"
            :key="a.id"
            class="attach-thumb"
            @click="openGreetingImage(a.rel_path)"
          >
            <img v-if="greetingImageMap[a.rel_path]" :src="greetingImageMap[a.rel_path]" :alt="a.name" />
          </div>
        </div>
        <div v-if="greetingMedia.videos.length" class="inline-video-list es-media">
          <InlineVideoCard
            v-for="video in greetingMedia.videos"
            :key="video.id"
            :asset="video"
          />
        </div>
        <div v-if="greetingMedia.audios.length" class="inline-audio-row es-media">
          <button
            v-for="item in greetingMedia.audios"
            :key="item.asset.id"
            type="button"
            class="inline-audio-pill"
            :class="{
              active: item.isBgm
                ? cardAudio.bgmAssetId === item.asset.id && cardAudio.bgmPlaying
                : cardAudio.sfxAssetId === item.asset.id && cardAudio.sfxPlaying,
            }"
            @click="item.isBgm ? cardAudio.toggleBgm(item.asset) : cardAudio.playSfx(item.asset)"
          >
            <span>♪ {{ item.asset.name }}</span>
          </button>
        </div>
        <RichMessage
          v-if="greetingMedia.cleanedText"
          :content="greetingMedia.cleanedText"
          variant="greeting"
          :asset-urls="greetingAssetUrls"
        />
        <p
          v-else-if="!greetingMedia.images.length && !greetingMedia.audios.length && !greetingMedia.videos.length"
          class="es-line"
        >
          {{ session.emptyLine || t('empty.noMessages') }}
        </p>
        <div v-if="session.emptyHints.length" class="es-hints">
          <button
            v-for="h in session.emptyHints"
            :key="h"
            class="es-hint"
            @click="chat.send(h)"
          >
            {{ h }}
          </button>
        </div>
      </div>

      <div v-if="summaryText" class="summary-beat">
        <div class="summary-label">{{ t('thread.previously') }}</div>
        <div class="summary-text">{{ summaryText }}</div>
      </div>

      <template v-for="(beat, i) in chat.beats" :key="beat.uid">
        <ChatBeat :beat="beat" :index="i" />
        <div v-if="i === 0" class="scene-break lit">
          <span class="sb-rule"></span>
          <span class="sb-text">{{ session.sceneBreak }}</span>
          <span class="sb-rule"></span>
        </div>
      </template>

      <div v-if="chat.pending" class="writing">
        <span class="node"></span>
        <div class="writing-row">
          <div class="book">
            <span class="book-spine"></span>
            <div class="leaf"><i></i></div>
            <div class="leaf"><i></i></div>
            <div class="leaf"><i></i></div>
          </div>
          <span class="writing-label">{{ t('thread.writing', { name: (chat.activeCast?.name || '').split(' ')[0] }) }}</span>
        </div>
      </div>
    </div>


  </main>
</template>

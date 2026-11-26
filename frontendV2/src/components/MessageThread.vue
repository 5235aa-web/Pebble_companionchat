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
import { applyDisplayRegex } from '@/utils/displayRegex'
import {
  extractInlineMediaFromText,
  normalizeAssetTagRefs,
  rewriteCssAssetUrls,
  type InlineMediaTextResult,
} from '@/utils/inlineMedia'
import { useI18n } from '@/i18n'
import { summaryText } from '@/data/chat'

const chat = useChatStore()
const session = useSessionStore()
const ui = useUiStore()
const cardAudio = useCardAudioStore()
const { t } = useI18n()

const EMPTY_GREETING_MEDIA: InlineMediaTextResult = {
  images: [],
  htmlImages: [],
  audios: [],
  videos: [],
  cleanedText: '',
}
const EMPTY_ASSET_URLS: Record<number, string> = {}

// Only parse and fetch media for the greeting currently shown on the empty stage.
const greetingVisible = computed(() => !chat.beats.length && !chat.loading)

const greetingMedia = computed<InlineMediaTextResult>(() => {
  if (!greetingVisible.value) return EMPTY_GREETING_MEDIA
  const raw = normalizeAssetTagRefs(session.emptyLine, session.assetNames)
  if (!raw) return EMPTY_GREETING_MEDIA
  const displayed = applyDisplayRegex(raw, session.displayRegexRules, {
    placement: 2,
    charName: session.act,
    userName: session.userName,
    variables: session.branchVariables,
    seed: `${session.activeCompanionId}:${session.activeBranch}`,
    assetNames: session.assetNames,
  })
  return extractInlineMediaFromText(displayed, (refText) => session.findAssetByRef(refText))
})

const resolvedCardCssInfo = computed(() =>
  rewriteCssAssetUrls(session.cardCss || '', (refText) => session.findAssetByRef(refText)),
)

const greetingImageMap = useFileUrls(
  () => {
    if (!greetingVisible.value) return []
    return [
      ...greetingMedia.value.images,
      ...greetingMedia.value.htmlImages,
      ...resolvedCardCssInfo.value.assets,
    ]
      .map((a) => a.rel_path)
      .filter(Boolean)
  },
  512,
)

const greetingAssetUrls = computed<Record<number, string>>(() => {
  const allAssets = [...greetingMedia.value.htmlImages, ...resolvedCardCssInfo.value.assets]
  if (!greetingVisible.value || !allAssets.length) {
    return EMPTY_ASSET_URLS
  }
  const out: Record<number, string> = {}
  for (const a of allAssets) {
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
let scrollMode: 'none' | 'bottom' | 'beat' = 'none'
let activeJumpId: number | null = null
let settleUntil = 0
let settleTimer = 0
let lastBottomScrollTop = 0

function atBottom(): boolean {
  const el = scroller.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight < 60
}

function clearSettleTimer() {
  if (settleTimer) {
    window.clearTimeout(settleTimer)
    settleTimer = 0
  }
}

function computeBeatScrollTop(id: number): number | null {
  const el = scroller.value
  if (!el) return null
  const target = el.querySelector(`[data-beat-id="${id}"]`) as HTMLElement | null
  if (!target) return null
  const sRect = el.getBoundingClientRect()
  const eRect = target.getBoundingClientRect()
  const maxTop = Math.max(0, el.scrollHeight - el.clientHeight)
  const offset =
    eRect.height > el.clientHeight * 0.72
      ? 36
      : Math.max(24, (el.clientHeight - eRect.height) / 2)
  const rawTop = el.scrollTop + (eRect.top - sRect.top) - offset
  return Math.max(0, Math.min(maxTop, rawTop))
}

function finishScrollSettle() {
  clearSettleTimer()
  const el = scroller.value
  if (!el) {
    scrollMode = 'none'
    return
  }
  if (scrollMode === 'bottom' && stickToBottom) {
    if (!atBottom()) {
      el.scrollTop = el.scrollHeight
    }
    chat.showJump = false
  } else if (scrollMode === 'beat' && activeJumpId !== null) {
    const top = computeBeatScrollTop(activeJumpId)
    if (top !== null && Math.abs(el.scrollTop - top) > 24) {
      el.scrollTop = top
    }
    const bottom = atBottom()
    chat.showJump = !bottom
    stickToBottom = bottom
  }
  if (performance.now() >= settleUntil) {
    scrollMode = 'none'
    activeJumpId = null
  }
}

function scheduleSettleCheck(ms = 900) {
  clearSettleTimer()
  settleTimer = window.setTimeout(() => {
    settleTimer = 0
    finishScrollSettle()
  }, ms)
}

function scrollToEnd(smooth = false, settleWindowMs = 1500) {
  const el = scroller.value
  if (!el) return
  stickToBottom = true
  scrollMode = 'bottom'
  activeJumpId = null
  lastBottomScrollTop = el.scrollTop
  settleUntil = performance.now() + settleWindowMs
  if (smooth) {
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    scheduleSettleCheck(950)
  } else {
    // Assign scrollTop directly: scroll-behavior:smooth is set on .scroller, so
    // behavior:'auto' would animate and the stream would outrun it.
    el.scrollTop = el.scrollHeight
    lastBottomScrollTop = el.scrollTop
    scheduleSettleCheck(350)
  }
  chat.showJump = false
}

function scrollToBeat(id: number, smooth = true) {
  const el = scroller.value
  if (!el) return
  const top = computeBeatScrollTop(id)
  if (top === null) return
  stickToBottom = false
  scrollMode = 'beat'
  activeJumpId = id
  settleUntil = performance.now() + 1500
  if (smooth) {
    el.scrollTo({ top, behavior: 'smooth' })
    scheduleSettleCheck(950)
  } else {
    el.scrollTop = top
    scheduleSettleCheck(350)
  }
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
  if (e.deltaY < 0) {
    stickToBottom = false
    scrollMode = 'none'
    activeJumpId = null
    settleUntil = 0
    clearSettleTimer()
  } else if (scrollMode === 'beat') {
    scrollMode = 'none'
    activeJumpId = null
    settleUntil = 0
    clearSettleTimer()
  }
}

function onTouchMove() {
  if (scrollMode === 'beat') {
    scrollMode = 'none'
    activeJumpId = null
    settleUntil = 0
    clearSettleTimer()
  }
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
    const el = scroller.value
    if (scrollMode === 'bottom' && stickToBottom && performance.now() < settleUntil && el) {
      if (el.scrollTop >= lastBottomScrollTop - 4) {
        lastBottomScrollTop = el.scrollTop
        chat.showJump = false
        return
      }
      // User dragged scrollbar / paged upward during programmatic scroll.
      stickToBottom = false
      scrollMode = 'none'
      settleUntil = 0
      clearSettleTimer()
    }
    const bottom = atBottom()
    chat.showJump = !bottom
    if (scrollMode !== 'beat') {
      stickToBottom = bottom
    }
  })
}

function onScrollEnd() {
  if (scrollMode === 'none') return
  finishScrollSettle()
}

let resizeObserver: ResizeObserver | null = null

onUnmounted(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  clearSettleTimer()
  if (raf) cancelAnimationFrame(raf)
  window.clearTimeout(scrollbarTimer)
})

watch(
  () => chat.beats.length,
  async (len, prevLen) => {
    stickToBottom = true
    await nextTick()
    scrollToEnd(Boolean(prevLen && len > prevLen))
  },
)

watch(
  () => chat.scrollRequest,
  () => {
    stickToBottom = true
    scrollToEnd(true)
  },
)

watch(
  () => chat.jumpRequest,
  async (req) => {
    if (!req) return
    await nextTick()
    scrollToBeat(req.id, true)
  },
)

onMounted(async () => {
  await nextTick()
  scrollToEnd()
  if (threadEl.value && typeof ResizeObserver !== 'undefined') {
    // Follow height growth once per rendered frame rather than probing
    // scrollHeight synchronously on every incoming token or decoding image.
    resizeObserver = new ResizeObserver(() => {
      const el = scroller.value
      if (!el) return
      if (stickToBottom && chat.busy) {
        el.scrollTop = el.scrollHeight
        chat.showJump = false
        return
      }
      if (scrollMode === 'bottom' && stickToBottom && performance.now() < settleUntil) {
        el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
        scheduleSettleCheck(700)
        return
      }
      if (scrollMode === 'beat' && activeJumpId !== null && performance.now() < settleUntil) {
        const top = computeBeatScrollTop(activeJumpId)
        if (top !== null && Math.abs(el.scrollTop - top) > 12) {
          el.scrollTo({ top, behavior: 'smooth' })
          scheduleSettleCheck(700)
        }
      }
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
    @scrollend="onScrollEnd"
    @wheel.passive="onWheel"
    @touchmove.passive="onTouchMove"
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
          :card-css="resolvedCardCssInfo.css"
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

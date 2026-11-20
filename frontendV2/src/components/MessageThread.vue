<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import ChatBeat from './ChatBeat.vue'
import { useChatStore } from '@/stores/chat'
import { useSessionStore } from '@/stores/session'
import { useI18n } from '@/i18n'
import { summaryText } from '@/data/chat'

const chat = useChatStore()
const session = useSessionStore()
const { t } = useI18n()

const scroller = ref<HTMLElement | null>(null)

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
    chat.showJump = !atBottom()
  })
}

onUnmounted(() => {
  if (raf) cancelAnimationFrame(raf)
  window.clearTimeout(scrollbarTimer)
})

watch(
  () => chat.beats.length,
  async () => {
    await nextTick()
    scrollToEnd(true)
  },
)

watch(
  // Two cheap signals instead of joining the whole reply on every delta: the
  // line count changes when a paragraph closes, the tail changes while one is
  // still being written.
  () => {
    const last = chat.beats[chat.beats.length - 1]
    return `${last?.lines.length ?? 0}\u0000${last?.tail ?? ''}`
  },
  async () => {
    if (atBottom()) {
      await nextTick()
      scrollToEnd()
    }
  },
)

watch(
  () => chat.scrollRequest,
  () => scrollToEnd(true),
)

onMounted(async () => {
  await nextTick()
  scrollToEnd()
})
</script>

<template>
  <main ref="scroller" class="scroller" :class="{ scrolling }" @scroll.passive="onScroll">
    <div class="thread">
      <div class="axis"></div>

      <div v-if="!chat.beats.length" class="empty-stage show">
        <div class="es-mark">&ldquo;</div>
        <div v-if="chat.activeCast?.name" class="es-name">{{ chat.activeCast?.name }}</div>
        <p class="es-line">{{ session.emptyLine || t('empty.noMessages') }}</p>
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

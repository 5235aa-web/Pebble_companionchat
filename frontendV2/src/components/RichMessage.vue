<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { renderRichMessage, splitStableStreamingPrefix } from '@/utils/richText'

const props = withDefaults(
  defineProps<{
    content: string
    streaming?: boolean
    variant?: 'beat' | 'greeting'
    assetUrls?: Record<number, string>
  }>(),
  {
    streaming: false,
    variant: 'beat',
    assetUrls: undefined,
  },
)

const html = ref('')
const prefixHtml = ref('')
const tailHtml = ref('')

let rafId = 0
let timerId = 0
let lastRenderAt = 0
let lastPrefixSource = ''
let lastPrefixAssets: Record<number, string> | undefined

function clearPending(): void {
  if (timerId) {
    window.clearTimeout(timerId)
    timerId = 0
  }
  if (rafId) {
    cancelAnimationFrame(rafId)
    rafId = 0
  }
}

function renderStreamFrame(): void {
  lastRenderAt = performance.now()
  const [prefix, tail] = splitStableStreamingPrefix(props.content)
  if (prefix) {
    if (prefix !== lastPrefixSource || props.assetUrls !== lastPrefixAssets) {
      lastPrefixSource = prefix
      lastPrefixAssets = props.assetUrls
      prefixHtml.value = renderRichMessage(prefix, false, props.assetUrls)
    }
    tailHtml.value = renderRichMessage(tail, true, props.assetUrls)
    html.value = ''
  } else {
    lastPrefixSource = ''
    lastPrefixAssets = undefined
    prefixHtml.value = ''
    tailHtml.value = ''
    html.value = renderRichMessage(props.content, true, props.assetUrls)
  }
}

function syncNow(): void {
  clearPending()
  lastRenderAt = performance.now()
  lastPrefixSource = ''
  lastPrefixAssets = undefined
  prefixHtml.value = ''
  tailHtml.value = ''
  html.value = renderRichMessage(props.content, props.streaming, props.assetUrls)
}

watch(
  [() => props.content, () => props.streaming, () => props.assetUrls],
  ([, isStreaming]) => {
    if (!isStreaming) {
      syncNow()
      return
    }
    if (timerId || rafId) return
    const len = props.content.length
    const minInterval = len > 6000 ? 110 : len > 2500 ? 75 : 48
    const wait = Math.max(0, minInterval - (performance.now() - lastRenderAt))
    const queueFrame = () => {
      rafId = requestAnimationFrame(() => {
        rafId = 0
        if (!props.streaming) {
          syncNow()
          return
        }
        renderStreamFrame()
      })
    }
    if (wait > 0) {
      timerId = window.setTimeout(() => {
        timerId = 0
        queueFrame()
      }, wait)
    } else {
      queueFrame()
    }
  },
  { immediate: true },
)

onUnmounted(() => {
  clearPending()
})
</script>

<template>
  <div
    v-if="streaming && prefixHtml"
    class="rich-msg"
    :class="[variant === 'greeting' ? 'greeting' : 'rich-beat', 'streaming']"
  >
    <div class="rich-prefix" v-html="prefixHtml"></div>
    <div class="rich-tail" v-html="tailHtml"></div>
  </div>
  <div
    v-else
    class="rich-msg"
    :class="[variant === 'greeting' ? 'greeting' : 'rich-beat', { streaming }]"
    v-html="html"
  ></div>
</template>

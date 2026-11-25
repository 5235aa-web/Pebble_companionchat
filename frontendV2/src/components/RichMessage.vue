<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import richMessageCss from '@/styles/22-rich-message.css?raw'
import {
  needsCardSandbox,
  renderRichMessage,
  sanitizeCardCss,
  splitStableStreamingPrefix,
} from '@/utils/richText'

const props = withDefaults(
  defineProps<{
    content: string
    streaming?: boolean
    variant?: 'beat' | 'greeting'
    role?: 'ai' | 'you' | 'narration' | string
    assetUrls?: Record<number, string>
    cardCss?: string
  }>(),
  {
    streaming: false,
    variant: 'beat',
    role: 'ai',
    assetUrls: undefined,
    cardCss: '',
  },
)

const html = ref('')
const prefixHtml = ref('')
const tailHtml = ref('')

const useSandbox = computed(() => needsCardSandbox(props.content, props.cardCss))

let rafId = 0
let timerId = 0
let lastRenderAt = 0
let lastPrefixSource = ''
let lastPrefixAssets: Record<number, string> | undefined
let lastPrefixSandbox = false

const sandboxHost = ref<HTMLElement | null>(null)
let shadowRoot: ShadowRoot | null = null
let shadowContainer: HTMLDivElement | null = null
let shadowPrefixEl: HTMLDivElement | null = null
let shadowTailEl: HTMLDivElement | null = null
let fallbackBaseStyleEl: HTMLStyleElement | null = null
let fallbackCardStyleEl: HTMLStyleElement | null = null
let cardSheet: CSSStyleSheet | null = null
let lastRenderedCardCss = ''
let lastShadowHtml = ''
let lastShadowPrefix = ''
let lastShadowTail = ''

const SHADOW_HOST_CSS = `
:host{
  display:block;
  position:relative;
  min-width:0;
}
.rich-msg{
  position:relative;
}
:host([data-role="ai"]) .say{
  font-size:18px;line-height:1.72;letter-spacing:.002em;
  color:var(--ink);margin:0 0 .62em;
}
:host([data-role="ai"]) .say:last-child{margin-bottom:0;}
:host([data-role="ai"]) .direction{
  font-size:16px;line-height:1.66;font-style:italic;
  color:var(--ink-3);
  border-left:1px solid var(--line-2);
  padding-left:17px;
  margin:0 0 .78em;
}
:host([data-role="you"]) .say{
  font-size:16px;line-height:1.76;color:var(--ink-2);margin:0 0 .5em;
}
:host([data-role="you"]) .direction{
  font-size:15px;line-height:1.7;font-style:italic;color:var(--ink-3);
  opacity:.8;border-left:none;padding-left:0;margin:0 0 .5em;
}
:host([data-role="narration"]) .say{
  font-size:15.5px;line-height:1.76;color:var(--ink-2);margin:0 0 .55em;
}
.caret{
  display:inline-block;width:2px;height:1.05em;vertical-align:text-bottom;
  margin-left:2px;background:var(--accent);animation:pebbleBlink 1s steps(1) infinite;
}
@keyframes pebbleBlink{50%{opacity:0;}}
`

function buildBaseShadowCss(): string {
  const adapted = richMessageCss
    .replace(/\.beat\[data-role="([^"]+)"\]\s+/g, ':host([data-role="$1"]) ')
    .replace(/html\[data-theme="day"\]\s+/g, ':host-context(html[data-theme="day"]) ')
  return `@layer pebble-base {\n${SHADOW_HOST_CSS}\n${adapted}\n}`
}

let cachedBaseSheet: CSSStyleSheet | null = null
let cachedBaseCssText = ''

function getBaseSheet(): CSSStyleSheet | null {
  if (typeof CSSStyleSheet === 'undefined') return null
  if (!cachedBaseSheet) {
    cachedBaseCssText = buildBaseShadowCss()
    try {
      cachedBaseSheet = new CSSStyleSheet()
      cachedBaseSheet.replaceSync(cachedBaseCssText)
    } catch {
      cachedBaseSheet = null
    }
  }
  return cachedBaseSheet
}

function ensureShadowRoot(): ShadowRoot | null {
  const host = sandboxHost.value
  if (!host) return null
  if (!shadowRoot) {
    shadowRoot = host.shadowRoot ?? host.attachShadow({ mode: 'open' })
  }
  if (!shadowContainer) {
    shadowContainer = document.createElement('div')
    shadowRoot.appendChild(shadowContainer)
  }
  return shadowRoot
}

function syncShadowStyles(root: ShadowRoot): void {
  const cleanedCardCss = sanitizeCardCss(props.cardCss || '', props.assetUrls)
  const baseSheet = getBaseSheet()
  const supportsAdopted = Boolean(baseSheet && 'adoptedStyleSheets' in root)

  if (supportsAdopted && baseSheet) {
    if (!cardSheet) {
      try {
        cardSheet = new CSSStyleSheet()
      } catch {
        cardSheet = null
      }
    }
    if (cardSheet) {
      if (cleanedCardCss !== lastRenderedCardCss) {
        lastRenderedCardCss = cleanedCardCss
        cardSheet.replaceSync(cleanedCardCss)
      }
      root.adoptedStyleSheets = cleanedCardCss ? [baseSheet, cardSheet] : [baseSheet]
      return
    }
  }

  if (!fallbackBaseStyleEl) {
    fallbackBaseStyleEl = document.createElement('style')
    fallbackBaseStyleEl.textContent = cachedBaseCssText || buildBaseShadowCss()
    root.prepend(fallbackBaseStyleEl)
  }
  if (cleanedCardCss) {
    if (!fallbackCardStyleEl) {
      fallbackCardStyleEl = document.createElement('style')
      root.insertBefore(fallbackCardStyleEl, shadowContainer)
    }
    if (cleanedCardCss !== lastRenderedCardCss) {
      lastRenderedCardCss = cleanedCardCss
      fallbackCardStyleEl.textContent = cleanedCardCss
    }
  } else if (fallbackCardStyleEl) {
    fallbackCardStyleEl.remove()
    fallbackCardStyleEl = null
    lastRenderedCardCss = ''
  }
}

function syncSandboxDom(): void {
  if (!useSandbox.value) return
  const root = ensureShadowRoot()
  if (!root || !shadowContainer) return

  syncShadowStyles(root)

  const cls = [
    'rich-msg',
    props.variant === 'greeting' ? 'greeting' : 'rich-beat',
    props.streaming ? 'streaming' : '',
  ]
    .filter(Boolean)
    .join(' ')
  if (shadowContainer.className !== cls) {
    shadowContainer.className = cls
  }

  if (props.streaming && prefixHtml.value) {
    if (!shadowPrefixEl || !shadowTailEl) {
      shadowContainer.innerHTML = ''
      shadowPrefixEl = document.createElement('div')
      shadowPrefixEl.className = 'rich-prefix'
      shadowTailEl = document.createElement('div')
      shadowTailEl.className = 'rich-tail'
      shadowContainer.append(shadowPrefixEl, shadowTailEl)
      lastShadowHtml = ''
      lastShadowPrefix = ''
      lastShadowTail = ''
    }
    if (prefixHtml.value !== lastShadowPrefix) {
      lastShadowPrefix = prefixHtml.value
      shadowPrefixEl.innerHTML = prefixHtml.value
    }
    if (tailHtml.value !== lastShadowTail) {
      lastShadowTail = tailHtml.value
      shadowTailEl.innerHTML = tailHtml.value
    }
    return
  }

  shadowPrefixEl = null
  shadowTailEl = null
  lastShadowPrefix = ''
  lastShadowTail = ''
  if (html.value !== lastShadowHtml) {
    lastShadowHtml = html.value
    shadowContainer.innerHTML = html.value
  }
}

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
  const sandbox = useSandbox.value
  const [prefix, tail] = splitStableStreamingPrefix(props.content)
  if (prefix) {
    if (
      prefix !== lastPrefixSource ||
      props.assetUrls !== lastPrefixAssets ||
      sandbox !== lastPrefixSandbox
    ) {
      lastPrefixSource = prefix
      lastPrefixAssets = props.assetUrls
      lastPrefixSandbox = sandbox
      prefixHtml.value = renderRichMessage(prefix, false, props.assetUrls, { sandbox })
    }
    tailHtml.value = renderRichMessage(tail, true, props.assetUrls, { sandbox })
    html.value = ''
  } else {
    lastPrefixSource = ''
    lastPrefixAssets = undefined
    lastPrefixSandbox = sandbox
    prefixHtml.value = ''
    tailHtml.value = ''
    html.value = renderRichMessage(props.content, true, props.assetUrls, { sandbox })
  }
  if (sandbox) void nextTick().then(syncSandboxDom)
}

function syncNow(): void {
  clearPending()
  lastRenderAt = performance.now()
  const sandbox = useSandbox.value
  lastPrefixSource = ''
  lastPrefixAssets = undefined
  lastPrefixSandbox = sandbox
  prefixHtml.value = ''
  tailHtml.value = ''
  html.value = renderRichMessage(props.content, props.streaming, props.assetUrls, { sandbox })
  if (sandbox) void nextTick().then(syncSandboxDom)
}

watch(
  [() => props.content, () => props.streaming, () => props.assetUrls, () => props.cardCss],
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

watch(
  [sandboxHost, () => props.cardCss, () => props.variant, () => props.role],
  ([host]) => {
    if (!host) {
      shadowRoot = null
      shadowContainer = null
      shadowPrefixEl = null
      shadowTailEl = null
      fallbackBaseStyleEl = null
      fallbackCardStyleEl = null
      lastRenderedCardCss = ''
      lastShadowHtml = ''
      lastShadowPrefix = ''
      lastShadowTail = ''
      return
    }
    if (useSandbox.value) syncSandboxDom()
  },
  { flush: 'post' },
)

onUnmounted(() => {
  clearPending()
  shadowRoot = null
  shadowContainer = null
  shadowPrefixEl = null
  shadowTailEl = null
  fallbackBaseStyleEl = null
  fallbackCardStyleEl = null
  cardSheet = null
})
</script>

<template>
  <div
    v-if="useSandbox"
    ref="sandboxHost"
    class="rich-sandbox-host"
    :data-role="role"
    :data-variant="variant"
  ></div>
  <div
    v-else-if="streaming && prefixHtml"
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

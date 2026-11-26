<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'

interface TipState {
  text: string
  left: number
  top: number
  below: boolean
  ready: boolean
}

const tip = ref<TipState | null>(null)
const tipEl = ref<HTMLElement | null>(null)

let activeAnchor: HTMLElement | null = null
let observer: MutationObserver | null = null
let seq = 0

function readTipText(el: HTMLElement): string {
  return el.getAttribute('data-tip')?.trim() ?? ''
}

function disconnectObserver(): void {
  if (observer) {
    observer.disconnect()
    observer = null
  }
}

function hideTip(): void {
  seq += 1
  activeAnchor = null
  disconnectObserver()
  tip.value = null
}

async function placeTip(anchor: HTMLElement, text: string): Promise<void> {
  const token = ++seq
  tip.value = {
    text,
    left: -9999,
    top: -9999,
    below: false,
    ready: false,
  }
  await nextTick()
  if (token !== seq || activeAnchor !== anchor || !anchor.isConnected) return
  const el = tipEl.value
  if (!el) return

  const rect = anchor.getBoundingClientRect()
  const tipW = el.offsetWidth
  const tipH = el.offsetHeight
  const gap = 10
  const margin = 8

  let below = false
  let top = rect.top - tipH - gap
  if (top < margin) {
    below = true
    top = rect.bottom + gap
  }
  top = Math.max(margin, Math.min(top, window.innerHeight - tipH - margin))

  const rawLeft = rect.left + rect.width / 2 - tipW / 2
  const left = Math.max(margin, Math.min(rawLeft, window.innerWidth - tipW - margin))

  tip.value = {
    text,
    left: Math.round(left),
    top: Math.round(top),
    below,
    ready: true,
  }
}

function showForAnchor(anchor: HTMLElement): void {
  const text = readTipText(anchor)
  if (!text) {
    hideTip()
    return
  }
  if (activeAnchor === anchor && tip.value?.text === text) return

  activeAnchor = anchor
  disconnectObserver()
  observer = new MutationObserver(() => {
    if (!activeAnchor || !activeAnchor.isConnected) {
      hideTip()
      return
    }
    const nextText = readTipText(activeAnchor)
    if (!nextText) {
      hideTip()
      return
    }
    if (nextText !== tip.value?.text) {
      void placeTip(activeAnchor, nextText)
    }
  })
  observer.observe(anchor, { attributes: true, attributeFilter: ['data-tip'] })
  void placeTip(anchor, text)
}

function findFloatingAnchor(target: EventTarget | null): HTMLElement | null {
  if (!(target instanceof Element)) return null
  return target.closest('[data-tip][data-tip-float]') as HTMLElement | null
}

function onMouseOver(e: MouseEvent): void {
  const anchor = findFloatingAnchor(e.target)
  if (!anchor) {
    if (activeAnchor) hideTip()
    return
  }
  showForAnchor(anchor)
}

function onMouseOut(e: MouseEvent): void {
  if (!activeAnchor) return
  const related = e.relatedTarget as Node | null
  if (related && activeAnchor.contains(related)) return
  const nextAnchor = findFloatingAnchor(e.relatedTarget)
  if (nextAnchor) {
    showForAnchor(nextAnchor)
    return
  }
  hideTip()
}

function onFocusIn(e: FocusEvent): void {
  const anchor = findFloatingAnchor(e.target)
  if (!anchor) return
  let isFocusVisible = true
  try {
    isFocusVisible = anchor.matches(':focus-visible')
  } catch {
    isFocusVisible = true
  }
  if (isFocusVisible) showForAnchor(anchor)
}

function onFocusOut(e: FocusEvent): void {
  if (!activeAnchor) return
  const related = e.relatedTarget as Node | null
  if (related && activeAnchor.contains(related)) return
  hideTip()
}

function onMouseDown(): void {
  if (activeAnchor) hideTip()
}

function onKeyDown(e: KeyboardEvent): void {
  if (e.key === 'Escape' && activeAnchor) hideTip()
}

onMounted(() => {
  document.addEventListener('mouseover', onMouseOver, true)
  document.addEventListener('mouseout', onMouseOut, true)
  document.addEventListener('focusin', onFocusIn, true)
  document.addEventListener('focusout', onFocusOut, true)
  document.addEventListener('mousedown', onMouseDown, true)
  document.addEventListener('scroll', hideTip, true)
  window.addEventListener('keydown', onKeyDown)
  window.addEventListener('blur', hideTip)
})

onUnmounted(() => {
  disconnectObserver()
  document.removeEventListener('mouseover', onMouseOver, true)
  document.removeEventListener('mouseout', onMouseOut, true)
  document.removeEventListener('focusin', onFocusIn, true)
  document.removeEventListener('focusout', onFocusOut, true)
  document.removeEventListener('mousedown', onMouseDown, true)
  document.removeEventListener('scroll', hideTip, true)
  window.removeEventListener('keydown', onKeyDown)
  window.removeEventListener('blur', hideTip)
})
</script>

<template>
  <Teleport to="body">
    <div
      v-if="tip"
      ref="tipEl"
      class="floating-tip"
      :class="{ below: tip.below, show: tip.ready }"
      :style="{
        left: `${tip.left}px`,
        top: `${tip.top}px`,
        visibility: tip.ready ? 'visible' : 'hidden',
      }"
      role="tooltip"
    >
      {{ tip.text }}
    </div>
  </Teleport>
</template>

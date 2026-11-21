<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useChatStore } from '@/stores/chat'
import { useSessionStore } from '@/stores/session'
import { useScriptStore } from '@/stores/script'
import { useI18n } from '@/i18n'

type JumpFilter = 'all' | 'you' | 'narration'

interface JumpEntry {
  uid: number
  id: number
  seq: number
  role: 'you' | 'narration'
  time: string
  preview: string
}

const chat = useChatStore()
const session = useSessionStore()
const script = useScriptStore()
const { t } = useI18n()

const rootEl = ref<HTMLElement | null>(null)
const popEl = ref<HTMLElement | null>(null)
const listEl = ref<HTMLElement | null>(null)

const open = ref(false)
const filter = ref<JumpFilter>('all')
const activeId = ref<number | null>(null)
const hovered = ref<{ entry: JumpEntry; top: number } | null>(null)

const inScene = computed(
  () => script.playing && script.session?.branch_id === session.activeBranch,
)

const entries = computed<JumpEntry[]>(() => {
  const out: JumpEntry[] = []
  let seq = 0
  for (const b of chat.beats) {
    if (b.role !== 'you' && b.role !== 'narration') continue
    seq++
    const raw = b.voice
      ? t('chat.voiceMessage')
      : b.lines
          .map((l) => l.text)
          .join(' ')
          .replace(/\s+/g, ' ')
          .trim() || (b.attachments?.length ? t('jump.attachmentOnly') : '')
    if (!raw) continue
    out.push({
      uid: b.uid,
      id: b.id,
      seq,
      role: b.role,
      time: b.time,
      preview: raw,
    })
  }
  return out
})

const visible = computed(() => !inScene.value && entries.value.length > 0)

const youCount = computed(() => entries.value.filter((e) => e.role === 'you').length)
const narrationCount = computed(() => entries.value.filter((e) => e.role === 'narration').length)

const filteredEntries = computed(() => {
  if (filter.value === 'all') return entries.value
  return entries.value.filter((e) => e.role === filter.value)
})

const activeSeqLabel = computed(() => {
  if (!entries.value.length) return '0'
  const hit = entries.value.find((e) => e.id === activeId.value)
  const n = hit ? hit.seq : entries.value[entries.value.length - 1].seq
  return String(n).padStart(2, '0')
})

function fmtSeq(n: number): string {
  return `#${String(n).padStart(2, '0')}`
}

let scrollLockUntil = 0
let scrollRaf = 0
let boundScroller: Element | null = null

function syncActiveFromScroll(): void {
  if (!visible.value || Date.now() < scrollLockUntil) return
  const scroller = boundScroller ?? document.querySelector('.scroller')
  if (!scroller) return
  const sRect = scroller.getBoundingClientRect()
  const anchorY = sRect.top + sRect.height * 0.32

  let bestId: number | null = null
  let bestDist = Infinity
  for (const entry of entries.value) {
    const el = document.querySelector(`[data-beat-id="${entry.id}"]`)
    if (!el) continue
    const r = el.getBoundingClientRect()
    const center = (r.top + Math.min(r.bottom, r.top + 140)) / 2
    const dist = Math.abs(center - anchorY)
    if (dist < bestDist) {
      bestDist = dist
      bestId = entry.id
    }
  }
  if (bestId !== null) activeId.value = bestId
}

function onThreadScroll(): void {
  if (scrollRaf) return
  scrollRaf = requestAnimationFrame(() => {
    scrollRaf = 0
    syncActiveFromScroll()
  })
}

function bindScroller(): void {
  const next = document.querySelector('.scroller')
  if (next === boundScroller) return
  if (boundScroller) boundScroller.removeEventListener('scroll', onThreadScroll)
  boundScroller = next
  if (boundScroller) boundScroller.addEventListener('scroll', onThreadScroll, { passive: true })
}

function jump(entry: JumpEntry): void {
  activeId.value = entry.id
  scrollLockUntil = Date.now() + 800
  hovered.value = null
  chat.jumpToBeat(entry.id)
}

function toggleDrawer(): void {
  open.value = !open.value
  hovered.value = null
  if (open.value) {
    void nextTick(() => {
      const row = listEl.value?.querySelector('.jr-row.active')
      row?.scrollIntoView({ block: 'nearest' })
    })
  }
}

function onHoverTick(entry: JumpEntry, ev: MouseEvent): void {
  if (open.value) return
  const target = ev.currentTarget as HTMLElement | null
  if (!target) return
  const rect = target.getBoundingClientRect()
  const clampedTop = Math.max(104, Math.min(window.innerHeight - 168, rect.top - 10))
  hovered.value = { entry, top: clampedTop }
}

function clearHover(): void {
  hovered.value = null
}

function onPointerDown(ev: PointerEvent): void {
  if (!open.value) return
  const target = ev.target as Node | null
  if (!target) return
  if (rootEl.value?.contains(target) || popEl.value?.contains(target)) return
  open.value = false
}

function onKeydown(ev: KeyboardEvent): void {
  if (ev.key === 'Escape' && open.value) open.value = false
}

watch(
  () => [session.activeCompanionId, session.activeBranch, inScene.value] as const,
  () => {
    open.value = false
    filter.value = 'all'
    hovered.value = null
    activeId.value = null
    void nextTick(() => {
      bindScroller()
      syncActiveFromScroll()
    })
  },
)

watch(
  () => entries.value.length,
  (len) => {
    if (!len) {
      open.value = false
      activeId.value = null
      return
    }
    if (activeId.value === null || !entries.value.some((e) => e.id === activeId.value)) {
      activeId.value = entries.value[entries.value.length - 1].id
    }
    void nextTick(() => {
      bindScroller()
      syncActiveFromScroll()
    })
  },
)

onMounted(() => {
  bindScroller()
  syncActiveFromScroll()
  window.addEventListener('pointerdown', onPointerDown)
  window.addEventListener('keydown', onKeydown)
})

onUnmounted(() => {
  if (boundScroller) boundScroller.removeEventListener('scroll', onThreadScroll)
  if (scrollRaf) cancelAnimationFrame(scrollRaf)
  window.removeEventListener('pointerdown', onPointerDown)
  window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div v-if="visible" ref="rootEl" class="jr" :class="{ open }">
    <button
      class="jr-badge"
      :class="{ active: open }"
      :aria-label="t('jump.title')"
      :aria-expanded="open"
      :data-tip="t('jump.title')"
      @click="toggleDrawer"
    >
      <span class="jr-badge-cur">{{ activeSeqLabel }}</span>
      <span class="jr-badge-all">{{ entries.length }}</span>
    </button>

    <div class="jr-track" @mouseleave="clearHover">
      <div class="jr-line"></div>
      <div class="jr-ticks">
        <button
          v-for="e in entries"
          :key="e.uid"
          class="jr-tick"
          :class="[
            `is-${e.role}`,
            { active: activeId === e.id, spot: chat.spotId === e.id },
          ]"
          :aria-label="`${fmtSeq(e.seq)} ${e.preview}`"
          @mouseenter="onHoverTick(e, $event)"
          @focus="clearHover"
          @click="jump(e)"
        >
          <span v-if="e.role === 'narration'" class="jr-diamond"></span>
          <span v-else class="jr-mark"></span>
        </button>
      </div>
    </div>

    <Transition name="jr-fade">
      <div
        v-if="hovered && !open"
        class="jr-tip"
        :class="`is-${hovered.entry.role}`"
        :style="{ top: `${hovered.top}px` }"
      >
        <div class="jr-tip-head">
          <span class="jr-tip-seq">{{ fmtSeq(hovered.entry.seq) }}</span>
          <span class="jr-tip-role">
            {{ hovered.entry.role === 'narration' ? t('jump.narrationTag') : t('jump.youTag') }}
          </span>
          <span v-if="hovered.entry.time" class="jr-tip-time">{{ hovered.entry.time }}</span>
        </div>
        <p class="jr-tip-text">{{ hovered.entry.preview }}</p>
      </div>
    </Transition>

    <Transition name="jr-pop-tr">
      <div v-if="open" ref="popEl" class="jr-pop">
        <div class="jr-pop-head">
          <div class="jr-pop-title-wrap">
            <span class="jr-pop-kicker">{{ t('jump.kicker') }}</span>
            <span class="jr-pop-title">{{ t('jump.title') }}</span>
          </div>
          <span class="jr-pop-count">{{ t('jump.count', { n: entries.length }) }}</span>
          <button class="jr-pop-close" aria-label="close" @click="open = false">&times;</button>
        </div>

        <div class="jr-filters">
          <button class="jr-flt" :class="{ on: filter === 'all' }" @click="filter = 'all'">
            {{ t('jump.all') }} · {{ entries.length }}
          </button>
          <button class="jr-flt" :class="{ on: filter === 'you' }" @click="filter = 'you'">
            {{ t('jump.you') }} · {{ youCount }}
          </button>
          <button
            class="jr-flt"
            :class="{ on: filter === 'narration' }"
            @click="filter = 'narration'"
          >
            {{ t('jump.narration') }} · {{ narrationCount }}
          </button>
        </div>

        <div ref="listEl" class="jr-list">
          <button
            v-for="e in filteredEntries"
            :key="e.uid"
            class="jr-row"
            :class="[
              `is-${e.role}`,
              { active: activeId === e.id, spot: chat.spotId === e.id },
            ]"
            @click="jump(e)"
          >
            <span class="jr-row-glyph" aria-hidden="true"></span>
            <div class="jr-row-body">
              <div class="jr-row-meta">
                <span class="jr-row-seq">{{ fmtSeq(e.seq) }}</span>
                <span class="jr-row-role">
                  {{ e.role === 'narration' ? t('jump.narrationTag') : t('jump.youTag') }}
                </span>
                <span v-if="e.time" class="jr-row-time">{{ e.time }}</span>
              </div>
              <p class="jr-row-text">{{ e.preview }}</p>
            </div>
          </button>

          <div v-if="!filteredEntries.length" class="jr-empty">
            {{ t('jump.emptyFilter') }}
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.jr {
  position: fixed;
  left: 8px;
  top: 104px;
  bottom: 96px;
  width: 46px;
  z-index: 5;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
  padding-left: 6px;
}

.jr-badge {
  flex: none;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  min-width: 26px;
  padding: 2px 3px 5px;
  background: transparent;
  border: 0;
  border-bottom: 1px solid var(--line);
  border-radius: 3px 3px 0 0;
  cursor: pointer;
  line-height: 1;
  transition: background .25s ease, border-color .25s ease;
}
.jr-badge:hover,
.jr-badge.active {
  background: rgb(var(--accent-rgb) / .08);
  border-bottom-color: rgb(var(--accent-rgb) / .55);
}
.jr-badge[data-tip]::after {
  left: calc(100% + 10px);
  right: auto;
  bottom: auto;
  top: 50%;
  transform: translateY(-50%) translateX(-4px);
}
.jr-badge[data-tip]:hover::after {
  transform: translateY(-50%) translateX(0);
}
.jr-badge-cur {
  font-family: var(--serif);
  font-size: 16px;
  color: var(--accent);
  letter-spacing: .02em;
}
.jr-badge-all {
  font-family: var(--mono);
  font-size: 8.5px;
  color: var(--ink-4);
  letter-spacing: .08em;
}

.jr-track {
  position: relative;
  flex: 1;
  min-height: 60px;
  width: 100%;
  display: flex;
  flex-direction: column;
}
.jr-line {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 7px;
  width: 1px;
  background: linear-gradient(180deg, transparent, var(--line) 6%, var(--line) 94%, transparent);
  pointer-events: none;
}
.jr-ticks {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  overflow-x: visible;
  scrollbar-width: none;
  padding: 4px 0;
}
.jr-ticks::-webkit-scrollbar {
  display: none;
}

.jr-tick {
  position: relative;
  flex: 1 0 10px;
  max-height: 22px;
  min-height: 9px;
  width: 30px;
  display: flex;
  align-items: center;
  padding: 0 0 0 2px;
  background: transparent;
  border: 0;
  cursor: pointer;
}

.jr-mark {
  display: block;
  width: 11px;
  height: 1.5px;
  border-radius: 1px;
  background: rgb(var(--ink-rgb) / .24);
  transition: width .24s var(--ease), background .24s ease, box-shadow .24s ease;
}
.jr-tick:hover .jr-mark {
  width: 18px;
  background: var(--accent);
}
.jr-tick.active .jr-mark,
.jr-tick.spot .jr-mark {
  width: 17px;
  height: 2px;
  background: var(--accent);
  box-shadow: 0 0 8px rgb(var(--accent-rgb) / .7);
}

.jr-diamond {
  display: block;
  margin-left: 3px;
  width: 5px;
  height: 5px;
  border: 1px solid rgb(var(--ink-rgb) / .38);
  background: var(--bg);
  transform: rotate(45deg);
  transition: transform .24s var(--ease), border-color .24s ease, background .24s ease, box-shadow .24s ease;
}
.jr-tick:hover .jr-diamond {
  border-color: var(--accent);
  background: rgb(var(--accent-rgb) / .28);
  transform: rotate(45deg) scale(1.22);
}
.jr-tick.active .jr-diamond,
.jr-tick.spot .jr-diamond {
  border-color: var(--accent);
  background: var(--accent);
  box-shadow: 0 0 8px rgb(var(--accent-rgb) / .75);
  transform: rotate(45deg) scale(1.18);
}

/* ---- Hover preview pill ---- */
.jr-tip {
  position: fixed;
  left: 58px;
  width: 240px;
  padding: 9px 12px;
  border: 1px solid var(--line-2);
  border-radius: 5px;
  background: rgb(var(--bg-rgb) / .92);
  backdrop-filter: blur(14px);
  box-shadow: 0 12px 30px rgb(var(--shadow-rgb) / .42);
  pointer-events: none;
  z-index: 9;
}
.jr-tip-head {
  display: flex;
  align-items: baseline;
  gap: 7px;
  margin-bottom: 4px;
}
.jr-tip-seq {
  font-family: var(--mono);
  font-size: 9px;
  color: var(--accent);
  letter-spacing: .08em;
}
.jr-tip-role {
  font-family: var(--mono);
  font-size: 8.5px;
  letter-spacing: .16em;
  color: var(--ink-3);
}
.jr-tip-time {
  margin-left: auto;
  font-family: var(--mono);
  font-size: 8.5px;
  color: var(--ink-4);
}
.jr-tip-text {
  margin: 0;
  font-family: var(--serif);
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--ink-2);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.jr-tip.is-narration .jr-tip-text {
  font-style: italic;
  color: var(--ink-3);
}

/* ---- Expandable index drawer ---- */
.jr-pop {
  position: fixed;
  left: 58px;
  top: 104px;
  width: 284px;
  max-height: calc(100vh - 208px);
  display: flex;
  flex-direction: column;
  border: 1px solid var(--line-2);
  border-radius: 6px;
  background: rgb(var(--bg-rgb) / .94);
  backdrop-filter: blur(18px);
  box-shadow: 0 20px 48px rgb(var(--shadow-rgb) / .48);
  z-index: 9;
  overflow: hidden;
}
.jr-pop-head {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 11px 12px 9px;
  border-bottom: 1px solid var(--line);
}
.jr-pop-title-wrap {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}
.jr-pop-kicker {
  font-family: var(--mono);
  font-size: 8px;
  letter-spacing: .22em;
  color: var(--accent-dim);
}
.jr-pop-title {
  font-family: var(--serif);
  font-size: 13.5px;
  color: var(--ink);
  font-weight: 500;
}
.jr-pop-count {
  margin-left: auto;
  font-family: var(--mono);
  font-size: 9.5px;
  color: var(--ink-4);
  letter-spacing: .06em;
}
.jr-pop-close {
  width: 22px;
  height: 22px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 3px;
  background: transparent;
  color: var(--ink-3);
  font-size: 15px;
  cursor: pointer;
  transition: color .2s ease, background .2s ease;
}
.jr-pop-close:hover {
  color: var(--ink);
  background: rgb(var(--ink-rgb) / .06);
}

.jr-filters {
  flex: none;
  display: flex;
  gap: 4px;
  padding: 7px 10px;
  border-bottom: 1px solid var(--line);
}
.jr-flt {
  flex: 1;
  padding: 4px 6px;
  border: 1px solid transparent;
  border-radius: 3px;
  background: transparent;
  color: var(--ink-3);
  font-family: var(--mono);
  font-size: 9.5px;
  letter-spacing: .04em;
  cursor: pointer;
  transition: color .2s ease, border-color .2s ease, background .2s ease;
}
.jr-flt:hover {
  color: var(--ink-2);
  background: rgb(var(--ink-rgb) / .04);
}
.jr-flt.on {
  color: var(--accent);
  border-color: rgb(var(--accent-rgb) / .32);
  background: rgb(var(--accent-rgb) / .08);
}

.jr-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 6px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.jr-row {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: 9px;
  width: 100%;
  padding: 8px 9px;
  border: 1px solid transparent;
  border-radius: 4px;
  background: transparent;
  text-align: left;
  cursor: pointer;
  transition: background .2s ease, border-color .2s ease, transform .2s var(--ease);
}
.jr-row:hover {
  background: rgb(var(--ink-rgb) / .045);
  border-color: var(--line);
}
.jr-row.active,
.jr-row.spot {
  background: rgb(var(--accent-rgb) / .09);
  border-color: rgb(var(--accent-rgb) / .28);
}

.jr-row-glyph {
  flex: none;
  margin-top: 5px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: rgb(var(--ink-rgb) / .25);
  transition: background .2s ease, box-shadow .2s ease;
}
.jr-row.is-narration .jr-row-glyph {
  border-radius: 1px;
  transform: rotate(45deg);
  background: transparent;
  border: 1px solid rgb(var(--ink-rgb) / .4);
}
.jr-row:hover .jr-row-glyph,
.jr-row.active .jr-row-glyph,
.jr-row.spot .jr-row-glyph {
  background: var(--accent);
  border-color: var(--accent);
  box-shadow: 0 0 8px rgb(var(--accent-rgb) / .65);
}

.jr-row-body {
  flex: 1;
  min-width: 0;
}
.jr-row-meta {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-bottom: 3px;
}
.jr-row-seq {
  font-family: var(--mono);
  font-size: 9px;
  color: var(--accent-dim);
  letter-spacing: .06em;
}
.jr-row-role {
  font-family: var(--mono);
  font-size: 8px;
  letter-spacing: .16em;
  color: var(--ink-4);
}
.jr-row.active .jr-row-seq,
.jr-row.spot .jr-row-seq {
  color: var(--accent);
}
.jr-row-time {
  margin-left: auto;
  font-family: var(--mono);
  font-size: 8.5px;
  color: var(--ink-4);
  opacity: .75;
}
.jr-row-text {
  margin: 0;
  font-family: var(--serif);
  font-size: 12.5px;
  line-height: 1.48;
  color: var(--ink-2);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}
.jr-row.is-narration .jr-row-text {
  font-style: italic;
  color: var(--ink-3);
}
.jr-row.active .jr-row-text,
.jr-row:hover .jr-row-text {
  color: var(--ink);
}

.jr-empty {
  padding: 22px 12px;
  text-align: center;
  font-family: var(--serif);
  font-size: 12px;
  color: var(--ink-4);
}

.jr-fade-enter-active,
.jr-fade-leave-active {
  transition: opacity .15s ease, transform .18s var(--ease);
}
.jr-fade-enter-from,
.jr-fade-leave-to {
  opacity: 0;
  transform: translateX(-4px);
}

.jr-pop-tr-enter-active,
.jr-pop-tr-leave-active {
  transition: opacity .22s ease, transform .24s var(--ease);
}
.jr-pop-tr-enter-from,
.jr-pop-tr-leave-to {
  opacity: 0;
  transform: translateX(-8px) scale(.98);
}

@media (max-width: 900px) {
  .jr,
  .jr-pop,
  .jr-tip {
    display: none;
  }
}
</style>

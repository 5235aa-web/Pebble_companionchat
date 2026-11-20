<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useScriptStore } from '@/stores/script'
import { useSessionStore } from '@/stores/session'
import { useChatStore } from '@/stores/chat'
import { useUiStore } from '@/stores/ui'
import { useI18n } from '@/i18n'

const script = useScriptStore()
const session = useSessionStore()
const chat = useChatStore()
const ui = useUiStore()
const { t, locale } = useI18n()

const open = ref(false)
/** Which scene the popover is showing. -1 means nothing is selected yet. */
const selected = ref(-1)
const busy = ref(false)
const editingKey = ref('')
const editValue = ref('')

const nodes = computed(() => script.play?.nodes ?? [])
const index = computed(() => script.currentIndex)
const total = computed(() => script.nodeCount)
const turnsDone = computed(() => Number(script.play?.session?.turns_done ?? 0))
/** Exchanges this scene plans to run; 0 means open-ended. */
const planned = computed(() => Number(nodes.value[index.value]?.turns ?? 0))
/**
 * Two conditions, both mandatory: the planned exchanges must be used up, and
 * the model must have sent its closing signal. Either one alone leaves the
 * scene unfinished.
 */
const done = computed(
  () => script.sceneDone && (planned.value <= 0 || turnsDone.value >= planned.value),
)
const hud = computed(() => script.hud)

const selectedNode = computed(() => nodes.value[selected.value] ?? null)
const isCurrent = computed(() => selected.value === index.value)
const isPast = computed(() => selected.value >= 0 && selected.value < index.value)
const isFuture = computed(() => selected.value > index.value)

/** The four core keys stay visible even before the model has filled them in. */
const hudRows = computed(() =>
  Object.entries({ mood: '', affection: 0, location: '', outfit: '', ...(hud.value ?? {}) }),
)

/**
 * Height weight for each scene: its planned exchange count. A scene with no
 * plan counts as one unit so it still occupies a visible slice.
 */
const weights = computed(() => nodes.value.map((n) => Math.max(1, Number(n.turns ?? 0))))

/** How much of one scene's slice is filled, from the top down. */
function fillPct(i: number): string {
  if (i < index.value) return '100%'
  if (i > index.value) return '0%'
  const plan = Number(nodes.value[i]?.turns ?? 0)
  if (plan <= 0) return done.value ? '100%' : '0%'
  return `${Math.min(100, (turnsDone.value / plan) * 100)}%`
}

function markState(i: number): 'done' | 'now' | 'next' {
  if (i < index.value) return 'done'
  if (i === index.value) return 'now'
  return 'next'
}

function openScene(i: number) {
  selected.value = i
  open.value = true
}

function askAdvance() {
  if (busy.value) return
  ui.askConfirm({
    title: t('script.advance'),
    text: t('script.confirmAdvance'),
    confirmLabel: t('script.advance'),
    danger: false,
    onConfirm: () => {
      void doAdvance()
    },
  })
}

async function doAdvance() {
  busy.value = true
  try {
    await script.advance()
    selected.value = script.currentIndex
    open.value = false
  } finally {
    busy.value = false
  }
}

function askRewind(i: number) {
  if (busy.value || i >= index.value) return
  ui.askConfirm({
    title: t('script.rewind'),
    text: t('script.rewindConfirm', { n: i + 1 }),
    danger: true,
    onConfirm: () => {
      void doRewind(i)
    },
  })
}

async function doRewind(i: number) {
  busy.value = true
  try {
    const payload = await script.rewind(i, locale.value)
    if (payload?.session) {
      await session.loadBranches()
      session.selectBranch(payload.session.branch_id)
      await chat.loadMessages()
      await session.loadTokens()
    }
    open.value = false
  } finally {
    busy.value = false
  }
}

function startEdit(key: string, value: unknown) {
  editingKey.value = key
  editValue.value = String(value ?? '')
}

async function commitEdit() {
  if (!editingKey.value) return
  const key = editingKey.value
  const next = { ...(hud.value ?? {}), [key]: editValue.value }
  editingKey.value = ''
  await script.setHud(next)
}

/** Open the scene: a narration line gives the model its cue to perform. */
function beginScene() {
  const title = script.currentNode?.title?.trim()
  if (!title) return
  open.value = false
  chat.send(`（${title}）`, [], 'narrator')
}

// The playthrough belongs to one branch: switching companion or branch must
// not leave a scene HUD hanging over an ordinary chat.
watch(
  () => session.activeCompanionId,
  (id) => {
    if (id) void script.loadActive(id)
    else script.clear()
  },
  { immediate: true },
)

const inScene = computed(
  () => script.playing && script.session?.branch_id === session.activeBranch,
)
</script>

<template>
  <div v-if="inScene" class="sp">
    <button
      class="sp-copy"
      :data-tip="t('script.copyOpen')"
      :aria-label="t('script.copyOpen')"
      @click="ui.openPanel('scene-copy')"
    >
      <svg viewBox="0 0 24 24">
        <path d="M9 4h8l3 3v12H9z" />
        <path d="M6 7v13h9" />
      </svg>
    </button>

    <button class="sp-badge" :aria-label="t('script.playing')" @click="openScene(index)">
      <span class="sp-badge-cur">{{ index + 1 }}</span>
      <span class="sp-badge-all">{{ total }}</span>
    </button>

    <div class="sp-track">
      <span class="sp-line"></span>
      <div
        v-for="(node, i) in nodes"
        :key="i"
        class="sp-seg"
        :style="{ flexGrow: String(weights[i]) }"
      >
        <span v-if="fillPct(i) !== '0%'" class="sp-seg-fill" :style="{ height: fillPct(i) }"></span>
        <button
          class="sp-seg-hit"
          :aria-label="node.title || `#${i + 1}`"
          @click="openScene(i)"
        >
          <span class="sp-mark" :class="markState(i)"></span>
          <span class="sp-tip">{{ i + 1 }}. {{ node.title }}</span>
        </button>
      </div>
    </div>

    <button
      class="sp-advance"
      :aria-label="t('script.advance')"
      :disabled="busy"
      @click="askAdvance"
    >
      <svg viewBox="0 0 24 24">
        <path d="M12 5v13M6.5 12.5L12 18l5.5-5.5" />
      </svg>
    </button>

    <div v-if="open" class="sp-pop">
      <div class="sp-pop-head">
        <span class="sp-pop-title">
          {{ t('script.progress', { n: selected + 1, total }) }}
        </span>
        <button class="sp-pop-close" @click="open = false">×</button>
      </div>
      <div class="sp-pop-scene">{{ selectedNode?.title }}</div>
      <p class="sp-pop-text">{{ selectedNode?.instruction }}</p>

      <template v-if="isCurrent">
        <div class="sp-hud-title">{{ t('script.hud') }}</div>
        <div class="sp-hud">
          <div v-for="[key, value] in hudRows" :key="key" class="sp-hud-row">
            <span class="sp-hud-k">{{ key }}</span>
            <input
              v-if="editingKey === key"
              v-model="editValue"
              class="sp-hud-input"
              @keydown.enter="commitEdit"
              @blur="commitEdit"
            />
            <button v-else class="sp-hud-v" @click="startEdit(key, value)">
              {{ value === '' ? '—' : value }}
            </button>
          </div>
        </div>

        <div class="sp-pop-foot">
          <button
            v-if="!chat.beats.length"
            class="mini-btn primary"
            :disabled="busy"
            @click="beginScene"
          >
            {{ t('script.start') }}
          </button>
          <template v-else>
            <span class="sp-state" :class="{ ready: done }">
              {{ done ? t('script.sceneDone') : t('script.uncompleted') }}
            </span>
            <button class="mini-btn primary" :disabled="busy" @click="askAdvance">
              {{ t('script.advance') }}
            </button>
          </template>
        </div>
      </template>

      <div v-else-if="isFuture" class="sp-hint">{{ t('script.notStarted') }}</div>

      <button v-else-if="isPast" class="mini-btn" :disabled="busy" @click="askRewind(selected)">
        {{ t('script.replayHere') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.sp {
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
.sp-copy {
  flex: none;
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  color: var(--ink-4);
  border: 1px solid var(--line);
  border-radius: 3px;
  transition: color .3s ease, border-color .3s ease;
}
.sp-copy:hover {
  color: var(--accent);
  border-color: var(--line-2);
}
/* The rail hugs the window edge, so a centred tip gets clipped on the left.
   This one opens to the right of the button instead. */
.sp-copy[data-tip]::after {
  left: calc(100% + 10px);
  right: auto;
  bottom: auto;
  top: 50%;
  transform: translateY(-50%) translateX(-4px);
}
.sp-copy[data-tip]:hover::after {
  transform: translateY(-50%) translateX(0);
}
.sp-copy svg {
  width: 14px;
  height: 14px;
  stroke: currentColor;
  fill: none;
  stroke-width: 1.5;
  stroke-linecap: round;
}
.sp-badge {
  flex: none;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  padding: 0 2px 5px;
  background: transparent;
  border: 0;
  border-bottom: 1px solid var(--line);
  cursor: pointer;
  line-height: 1;
}
.sp-badge-cur {
  font-family: var(--serif);
  font-size: 18px;
  color: var(--accent);
  letter-spacing: .02em;
}
.sp-badge-all {
  font-family: var(--mono);
  font-size: 8.5px;
  color: var(--ink-4);
  letter-spacing: .08em;
}
.sp-track {
  position: relative;
  flex: 1;
  min-height: 60px;
  width: 100%;
  display: flex;
  flex-direction: column;
}
.sp-line {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 7px;
  width: 1px;
  background: linear-gradient(180deg, transparent, var(--line) 6%, var(--line) 94%, transparent);
}
/* One scene per segment. flexGrow carries the planned exchange count, so a
   long scene owns a longer slice of the rail. */
.sp-seg {
  position: relative;
  flex-basis: 0;
  min-height: 14px;
  display: flex;
  flex-direction: column;
  border-top: 1px solid rgb(var(--ink-rgb) / .08);
}
.sp-seg:first-child { border-top: 0; }
.sp-seg-fill {
  position: absolute;
  left: 7px;
  top: 0;
  width: 1px;
  background: linear-gradient(180deg, var(--accent), var(--accent-dim));
  transition: height .55s var(--ease);
}
.sp-seg-fill::after {
  content: '';
  position: absolute;
  left: -2px;
  bottom: 0;
  width: 5px;
  height: 1px;
  background: var(--accent);
  box-shadow: 0 0 7px rgb(var(--accent-rgb) / .75);
}
.sp-seg-hit {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: flex-start;
  padding: 0;
  background: transparent;
  border: 0;
  cursor: pointer;
}
.sp-mark {
  margin-top: -0.5px;
  margin-left: 2px;
  width: 11px;
  height: 1px;
  background: rgb(var(--ink-rgb) / .2);
  transition: width .28s var(--ease), background .28s ease, box-shadow .28s ease;
}
.sp-mark.done { width: 13px; background: var(--accent-dim); }
.sp-mark.now {
  width: 17px;
  margin-left: 0;
  height: 1.5px;
  background: var(--accent);
  box-shadow: 0 0 8px rgb(var(--accent-rgb) / .6);
}
.sp-seg-hit:hover .sp-mark { width: 17px; background: var(--accent); }
.sp-tip {
  position: absolute;
  left: 40px;
  top: 0;
  max-width: 180px;
  padding: 3px 7px;
  border: 1px solid var(--line);
  border-radius: 3px;
  background: var(--bg-2);
  color: var(--ink-2);
  font-family: var(--serif);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  opacity: 0;
  pointer-events: none;
  transition: opacity .16s ease;
}
.sp-seg-hit:hover .sp-tip { opacity: 1; }
.sp-advance {
  flex: none;
  margin-left: 0;
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border: 1px solid var(--accent);
  border-radius: 50%;
  background: rgb(var(--accent-rgb) / .08);
  color: var(--accent);
  cursor: pointer;
  box-shadow: 0 0 12px rgb(var(--accent-rgb) / .22);
  animation: sp-breathe 2.8s ease-in-out infinite;
}
.sp-advance:disabled { opacity: .4; animation: none; }
.sp-advance svg {
  width: 15px;
  height: 15px;
  stroke: currentColor;
  fill: none;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
@keyframes sp-breathe {
  0%, 100% { opacity: .55; box-shadow: 0 0 8px rgb(var(--accent-rgb) / .16); }
  50% { opacity: 1; box-shadow: 0 0 16px rgb(var(--accent-rgb) / .4); }
}
.sp-pop {
  position: fixed;
  left: 60px;
  top: 104px;
  width: 250px;
  max-height: 62vh;
  overflow-y: auto;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--bg-2);
  z-index: 8;
}
.sp-pop-head { display: flex; align-items: center; justify-content: space-between; }
.sp-pop-title { font-family: var(--mono); font-size: 10px; color: var(--ink-3); }
.sp-pop-close { background: none; border: 0; color: var(--ink-3); cursor: pointer; font-size: 14px; }
.sp-pop-scene { margin-top: 6px; font-family: var(--serif); font-size: 14px; color: var(--ink-1); }
.sp-pop-text { margin: 6px 0 10px; font-size: 12px; line-height: 1.55; color: var(--ink-2); white-space: pre-wrap; }
.sp-hud-title { font-family: var(--mono); font-size: 10px; color: var(--ink-3); margin-bottom: 4px; }
.sp-hud { display: flex; flex-direction: column; gap: 3px; }
.sp-hud-row { display: flex; align-items: baseline; gap: 8px; font-size: 12px; }
.sp-hud-k { color: var(--ink-3); min-width: 56px; }
.sp-hud-v { flex: 1; text-align: left; background: none; border: 0; padding: 0; color: var(--ink-1); cursor: text; }
.sp-hud-input {
  flex: 1; min-width: 0; padding: 1px 4px;
  border: 1px solid var(--line); border-radius: 3px;
  background: var(--bg); color: var(--ink-1); font-size: 12px;
}
.sp-pop-foot {
  margin-top: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.sp-state { font-family: var(--mono); font-size: 10px; color: var(--ink-3); letter-spacing: .04em; }
.sp-state.ready { color: var(--accent); }
.sp-hint { margin-top: 8px; font-size: 11px; color: var(--ink-3); }

@media (max-width: 900px) {
  .sp { display: none; }
}
</style>

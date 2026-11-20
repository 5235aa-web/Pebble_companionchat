<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import type { Beat } from '@/data/chat'
import { useChatStore } from '@/stores/chat'
import { useUiStore } from '@/stores/ui'
import { useI18n } from '@/i18n'
import { useTtsStore } from '@/stores/tts'
import { useFileUrls } from '@/composables/useFileUrl'
import { shouldStream, streamSampleRate, synthesize, synthesizeStream } from '@/api/tts'
import { PcmPlayer } from '@/lib/pcmPlayer'
import { swipeMessage } from '@/api/companion'
import { useSessionStore } from '@/stores/session'
import { useCallStore } from '@/stores/call'
import { parseLines } from '@/stores/chat'

const props = defineProps<{ beat: Beat; index: number }>()
const chat = useChatStore()
const ui = useUiStore()
const tts = useTtsStore()

const attachMap = useFileUrls(() => (props.beat.attachments || []).map((a) => a.src), 1024)
const session = useSessionStore()
const call = useCallStore()
const { t } = useI18n()

const lit = ref(false)
const thinkingOpen = ref(false)
const editing = ref(false)
const draft = ref('')
const copied = ref(false)

const swipeCur = ref((props.beat.swipeIndex ?? 0) + 1)
const swipeTotal = computed(() => Math.max(1, props.beat.swipes?.length ?? 1))

/** Swiping asks the backend to switch the stored variant, then shows it. */
async function swipe(step: number) {
  const next = swipeCur.value + step
  if (next < 1 || next > swipeTotal.value) return
  const cid = session.activeCompanionId
  const bid = session.activeBranch
  if (!cid || !bid) return
  try {
    const r = await swipeMessage(cid, bid, props.beat.id, next - 1)
    swipeCur.value = next
    props.beat.lines = parseLines(r.content)
    // A swipe replaces the whole reply, so any half-written tail from the
    // original stream no longer belongs to what is on screen.
    props.beat.tail = undefined
    props.beat.swipeIndex = r.swipe_id
  } catch {
    // leave the current variant in place
  }
}

const isAI = computed(() => props.beat.role === 'ai')

/* ---- generation footprint (model, duration, tokens) */

/**
 * The record for the variant on screen. Messages generated before this feature
 * existed have none, and the footer simply does not render.
 */
const generation = computed(() => {
  const list = props.beat.generations
  if (!list?.length) return null
  return list[props.beat.swipeIndex ?? 0] ?? list[0] ?? null
})

function fmtDuration(ms?: number | null): string {
  if (typeof ms !== 'number' || ms <= 0) return ''
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)}s`
}

function fmtTokens(n?: number | null): string {
  if (typeof n !== 'number' || n <= 0) return ''
  return n < 1000 ? `${n} tok` : `${(n / 1000).toFixed(1)}k tok`
}

const metaLine = computed(() => {
  const g = generation.value
  if (!g) return ''
  return [g.model, fmtDuration(g.elapsed_ms), fmtTokens(g.completion_tokens)]
    .filter(Boolean)
    .join(' · ')
})

/** Detail on hover: what the headline number leaves out. */
const metaTip = computed(() => {
  const g = generation.value
  if (!g) return ''
  const bits: string[] = []
  if (g.provider) bits.push(g.provider)
  if (typeof g.prompt_tokens === 'number' && g.prompt_tokens > 0) {
    bits.push(t('beat.promptTokens', { n: g.prompt_tokens }))
  }
  if (typeof g.total_tokens === 'number' && g.total_tokens > 0) {
    bits.push(t('beat.totalTokens', { n: g.total_tokens }))
  }
  const ttft = fmtDuration(g.ttft_ms)
  if (ttft) bits.push(t('beat.firstToken', { t: ttft }))
  if (g.tokens_source === 'estimated') bits.push(t('beat.estimated'))
  return bits.join(' · ')
})

/**
 * Auto read-aloud handoff. The store queues the uid of the reply that just
 * finished; whichever beat owns it takes the cue and clears it, so the message
 * is read exactly once even though every beat watches the same value.
 */
watch(
  () => chat.autoReadUid,
  (uid) => {
    if (uid === null || uid !== props.beat.uid) return
    chat.autoReadUid = null
    // While a call is up it owns playback and reads the reply itself.
    if (call.active) return
    if (!playing.value) void readAloud()
  },
)

/** Summary body as plain text: the card preserves the model's line breaks. */
const summaryText = computed(() => props.beat.lines.map((l) => l.text).join('\n'))

/**
 * Collapsed by default. A folded summary can be a few hundred words, and it is
 * history rather than conversation — keeping it out of the DOM until asked for
 * costs nothing and keeps the thread's paint area small. `summaryText` is only
 * evaluated while open, since computed is lazy.
 */
const summaryOpen = ref(false)
const marked = computed(() => props.beat.marked === true)

onMounted(() => {
  // Stagger only the history that is already there. A beat you just sent (it
  // still carries a negative temp id) must appear at once: waiting behind the
  // index-based delay made the message show up after the reply had started.
  const fresh = props.beat.id < 0
  setTimeout(() => (lit.value = true), fresh ? 0 : Math.min(140 + props.index * 115, 600))
})

const playing = ref(false)
let audioEl: HTMLAudioElement | null = null
let pcm: PcmPlayer | null = null
let abortRead: AbortController | null = null

/** Halt whatever is currently speaking and clear the playing state. */
function stopSpeech() {
  abortRead?.abort()
  abortRead = null
  if (pcm) {
    void pcm.close()
    pcm = null
  }
  audioEl?.pause()
  audioEl = null
  playing.value = false
}

/**
 * Speak this beat with whichever TTS engine the panel has selected.
 *
 * Engines with a streaming endpoint are played as the audio arrives, which puts
 * the first syllable well under a second away instead of waiting for the whole
 * clip. The rest still go through the blocking path and a blob URL.
 */
async function readAloud() {
  if (playing.value) {
    stopSpeech()
    return
  }
  // The character on stage, not the row: a reply from an earlier character in
  // the same thread should still be read in whoever's voice the thread belongs
  // to. Falls back to the panel's config when the character has none.
  const cfg = tts.activeFor(chat.activeCast?.ttsVoice)
  if (!cfg) return
  const text = props.beat.lines.map((l) => l.text).join('\n').trim()
  if (!text) return
  playing.value = true

  if (shouldStream(cfg, tts.specs)) {
    const player = new PcmPlayer(streamSampleRate(cfg, tts.specs))
    pcm = player
    const ac = new AbortController()
    abortRead = ac
    try {
      await player.start()
      await synthesizeStream(cfg, text, (chunk) => player.push(chunk), ac.signal)
      await player.drain()
    } catch {
      // Aborts land here too; either way the audio stops.
    } finally {
      if (pcm === player) {
        pcm = null
        abortRead = null
        playing.value = false
        void player.close()
      }
    }
    return
  }

  try {
    const blob = await synthesize(cfg, text)
    const url = URL.createObjectURL(blob)
    audioEl = new Audio(url)
    audioEl.onended = () => {
      playing.value = false
      URL.revokeObjectURL(url)
    }
    await audioEl.play()
  } catch {
    playing.value = false
  }
}

function openImage(src: string) {
  ui.galleryTarget = src
  ui.openPanel('gallery')
}

// A beat can be unmounted mid-sentence (branch switch, compaction, reload).
// Leaving the AudioContext open would keep the sound going with no button left
// to stop it.
onUnmounted(() => {
  if (playing.value) stopSpeech()
})

function startEdit() {
  draft.value = props.beat.lines.map((l) => l.text).join('\n')
  editing.value = true
}

function saveEdit() {
  // Editing a user message is not a cosmetic fix: the model already answered
  // the old wording, so the whole tail of the thread is re-asked. The store
  // drops those beats and streams a fresh reply.
  chat.reedit(props.beat.id, draft.value)
  editing.value = false
}

function cancelEdit() {
  editing.value = false
}

function copyLine() {
  const text = props.beat.lines.map((l) => l.text).join('\n')
  void navigator.clipboard?.writeText(text)
  copied.value = true
  setTimeout(() => (copied.value = false), 1200)
}
</script>

<template>
  <div v-if="beat.role === 'summary'" class="summary-card" :class="{ open: summaryOpen }">
    <button class="sc-head" type="button" @click="summaryOpen = !summaryOpen">
      <span class="sc-kicker">{{ t('compact.cardTitle') }}</span>
      <span class="sc-meta">
        <template v-if="beat.folded">{{ beat.folded }} {{ t('compact.msgUnit') }}</template>
        <svg class="sc-chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6" /></svg>
      </span>
    </button>
    <p v-if="summaryOpen" class="sc-body">{{ summaryText }}</p>
  </div>

  <div v-else-if="beat.role === 'scene-summary'" class="scene-roll" :class="{ open: summaryOpen }">
    <button class="sr-head" type="button" @click="summaryOpen = !summaryOpen">
      <span class="sr-kicker">{{ beat.summaryLabel }}</span>
      <span class="sr-meta">
        <span class="sr-caption">{{ t('script.recap') }}</span>
        <svg class="sr-chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6" /></svg>
      </span>
    </button>
    <p v-if="summaryOpen" class="sr-body">{{ summaryText }}</p>
  </div>

  <article
    v-else
    class="beat"
    :class="{ lit, spot: chat.spotId === beat.id }"
    :data-role="beat.role"
    :data-beat-id="beat.id"
  >
    <span class="node"></span>
    <div class="beat-body">
      <header class="beat-head">
        <span class="speaker">{{ beat.speaker }}</span>
        <span class="beat-time">{{ beat.time }}</span>
      </header>

      <div v-if="beat.thinking" class="thinking" :class="{ open: thinkingOpen }">
        <button class="thinking-head" @click="thinkingOpen = !thinkingOpen">
          <svg class="chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6" /></svg>
          <span>{{ t('beat.thinking') }}</span>
        </button>
        <div class="thinking-body">
          <p>{{ beat.thinking }}</p>
        </div>
      </div>

      <div v-if="editing" class="edit-wrap">
        <textarea v-model="draft" class="edit-area" aria-label="edit message" rows="6"></textarea>
        <div class="edit-actions">
          <button class="mini-btn" @click="cancelEdit">{{ t('beat.cancel') }}</button>
          <button class="mini-btn primary" @click="saveEdit">{{ t('beat.save') }}</button>
        </div>
      </div>

      <div v-else>
        <div v-if="beat.attachments && beat.attachments.length" class="attach-row">
          <div
            v-for="a in beat.attachments"
            :key="a.src"
            class="attach-thumb"
            @click="openImage(a.src)"
          >
            <img :src="attachMap[a.src]" :alt="a.alt" />
          </div>
        </div>
        <div v-if="beat.voice" class="beat-lines">
          <p class="say voice-bubble">{{ t('chat.voiceMessage') }}</p>
        </div>
        <div v-else class="beat-lines">
          <p v-for="(line, i) in beat.lines" :key="i" :class="line.kind">{{ line.text }}</p>
          <!-- The unterminated tail of a live reply. Kept as its own paragraph
               so a delta rewrites one text node instead of the whole list. -->
          <p v-if="beat.tail" class="say tail">{{ beat.tail }}<span class="caret"></span></p>
        </div>
      </div>

      <footer v-if="!editing && beat.role !== 'narration'" class="beat-tools">
        <!-- Same reveal as the buttons: invisible until the message is hovered.
             Short by design; the tooltip carries the breakdown. -->
        <span v-if="metaLine" class="beat-meta" :data-tip="metaTip || undefined">{{ metaLine }}</span>
        <button
          v-if="isAI"
          class="tool"
          :class="{ on: playing }"
          :data-tip="playing ? t('tool.stopRead') : t('tool.readAloud')"
          @click="readAloud"
        >
          <svg v-if="playing" viewBox="0 0 24 24">
            <rect x="7" y="7" width="10" height="10" rx="1" />
          </svg>
          <svg v-else viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9H4z" /><path d="M17 8a5 5 0 010 8" /></svg>
        </button>
        <button class="tool" :data-tip="t('tool.copy')" @click="copyLine">
          <svg v-if="!copied" viewBox="0 0 24 24">
            <rect x="9" y="9" width="11" height="11" rx="1" />
            <path d="M5 15V4h11" />
          </svg>
          <svg v-else viewBox="0 0 24 24"><path d="M5 12l5 5L19 7" /></svg>
        </button>
        <button class="tool" :class="{ on: marked }" :data-tip="t('tool.mark')" @click="chat.toggleMark(beat.id)">
          <svg viewBox="0 0 24 24" :style="marked ? { fill: 'currentColor' } : undefined">
            <path d="M12 3.5l2.5 5.4 5.8.8-4.2 4 1 5.8-5.1-2.8-5.1 2.8 1-5.8-4.2-4 5.8-.8z" />
          </svg>
        </button>
        
        <button v-if="!isAI" class="tool" :data-tip="t('tool.edit')" @click="startEdit">
          <svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16v4z" /></svg>
        </button>
        <button v-if="isAI" class="tool" :data-tip="t('tool.regenerate')" @click="chat.regenerate(beat.id)">
          <svg viewBox="0 0 24 24"><path d="M20 12a8 8 0 11-2.4-5.7" /><path d="M20 4v4h-4" /></svg>
        </button>
        <button class="tool danger" :data-tip="t('tool.delete')" @click="chat.removeBeat(beat.id)">
          <svg viewBox="0 0 24 24">
            <path d="M4 7h16M9 7V5a1 1 0 011-1h4a1 1 0 011 1v2M6 7l1 13a1 1 0 001 1h8a1 1 0 001-1l1-13" />
          </svg>
        </button>

        <div v-if="beat.swipe" class="swipe">
          <button class="swipe-btn" data-sw="-1" @click="swipe(-1)">
            <svg viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6" /></svg>
          </button>
          <span class="swipe-n">{{ swipeCur }} / {{ swipeTotal }}</span>
          <button class="swipe-btn" data-sw="1" @click="swipe(1)">
            <svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6" /></svg>
          </button>
        </div>
      </footer>
    </div>
  </article>
</template>

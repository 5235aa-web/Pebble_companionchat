import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { Beat, BeatLine, GenerationMeta } from '@/data/chat'
import {
  deleteCompanion,
  deleteSingleMessage,
  getMessages,
  updateMessage,
} from '@/api/companion'
import {
  audioStream,
  chatStream,
  continueStream,
  regenerateStream,
  reeditStream,
  type AttachmentRef,
  type ChatChunk,
  type LlmConfig,
} from '@/api/stream'
import { useSessionStore } from './session'
import { useLlmStore } from './llm'
import { useScriptStore } from './script'
import type { SceneSummary } from '@/api/script'
import { compactBranch } from '@/api/compaction'
import { formatClock } from '@/utils/time'
import { resolvePlaceholders } from '@/utils/text'

const MAX_CONTEXT = 16384

/** Message clock. See utils/time for why the zone has to be added by hand. */
function nowTime(iso?: string): string {
  return formatClock(iso || new Date().toISOString())
}

/**
 * Generation records are stored per swipe. Older rows predate the field, so a
 * missing or malformed value has to read as "no info" rather than break the
 * thread; the footer simply stays hidden.
 */
/**
 * Put each segment card back where it belongs.
 *
 * A card stores the id of the last message it folded, and uncompressed messages
 * keep their ids, so "after the last message no newer than that id" restores the
 * original position without depending on the folded rows still being rendered.
 */
function mergeSceneCards(
  list: Beat[],
  cards: SceneSummary[],
  macro?: (t: string) => string,
): Beat[] {
  if (!cards.length) return list
  // A separate counter from the store's: this runs outside the store setup.
  let uid = -2_000_000
  const out: Beat[] = []
  let i = 0
  for (const card of cards) {
    while (i < list.length && Number(list[i].id ?? 0) <= card.message_to_id) {
      out.push(list[i])
      i += 1
    }
    out.push({
      uid: uid--,
      id: -card.id,
      role: 'scene-summary',
      speaker: '',
      time: nowTime(card.created_at),
      thinking: null,
      lines: parseLines(card.content, macro),
      swipe: null,
      summaryLabel: `Scene ${card.scene_from}-${card.scene_to}`,
    })
  }
  while (i < list.length) {
    out.push(list[i])
    i += 1
  }
  return out
}

function pickGenerations(meta: Record<string, unknown> | undefined): GenerationMeta[] | undefined {
  const raw = meta?.generation
  return Array.isArray(raw) && raw.length ? (raw as GenerationMeta[]) : undefined
}

/** A line that opens with a parenthesis is a stage direction, not speech. */
export function parseLines(text: string, macro?: (t: string) => string): BeatLine[] {
  const source = macro ? macro(text) : text
  return source
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .map<BeatLine>((l) => ({ kind: l.startsWith('(') ? 'direction' : 'say', text: l }))
}

/**
 * Move newly completed lines from the buffer into `target.lines`.
 *
 * Re-parsing the whole buffer on every delta is quadratic: an 800-character
 * reply delivered in 200 chunks re-splits those 800 characters 200 times. Only
 * text past the last newline is touched here, and it stays in `tail` until a
 * newline closes it, so a delta costs the characters that just arrived.
 *
 * `lines` is mutated in place on purpose. Replacing the array would change the
 * prop identity every beat downstream watches and re-create every paragraph
 * element instead of appending one.
 *
 * Returns the number of characters now committed.
 */
export function commitLines(target: Beat, full: string, committed: number): number {
  const cut = full.lastIndexOf('\n')
  if (cut < committed) {
    // Nothing closed yet: show the growing tail without committing it.
    target.tail = full.slice(committed).trim() || undefined
    return committed
  }
  for (const raw of full.slice(committed, cut).split('\n')) {
    const text = raw.trim()
    if (!text) continue
    target.lines.push({ kind: text.startsWith('(') ? 'direction' : 'say', text })
  }
  target.tail = full.slice(cut + 1).trim() || undefined
  return cut + 1
}

export function llmConfig(): LlmConfig {
  const llm = useLlmStore()
  const p = llm.currentProvider
  const params = llm.activeParams
  return {
    provider_type: p?.provider ?? '',
    api_key: p?.apiKey ?? '',
    base_url: p?.baseUrl ?? '',
    model: llm.requestModel,
    temperature: params.temperature,
    top_p: params.topP,
    max_tokens: params.maxTokens,
    // These used to be configured in the panel and never sent, so every slider
    // below Temperature was decoration. The backend decides per provider which
    // of them may go on the wire.
    top_k: params.topK,
    min_p: params.minP,
    repeat_penalty: params.repeatPenalty,
    frequency_penalty: params.frequencyPenalty,
    presence_penalty: params.presencePenalty,
  }
}

export const useChatStore = defineStore('chat', () => {
  const session = useSessionStore()
  const script = useScriptStore()

  const beats = ref<Beat[]>([])
  const busy = ref(false)
  const pending = ref(false)
  const narrationMode = ref(false)
  /**
   * Read every new reply aloud once it finishes streaming.
   *
   * Turning it on deliberately does not touch the reply already on screen: the
   * toggle takes effect from the next completed reply onwards.
   */
  const autoRead = ref(false)
  /** uid of the beat waiting to be read; cleared by the reader when consumed. */
  const autoReadUid = ref<number | null>(null)
  const emptyStage = ref(false)
  const loading = ref(false)
  /** True when the thread is scrolled away from the newest beat. */
  const showJump = ref(false)
  /** Bumped when something outside the thread asks it to scroll down. */
  const scrollRequest = ref(0)
  const error = ref('')

  let controller: AbortController | null = null
  let tempId = -1
  let nextUid = 1
  let buffer = ''

  const cast = computed(() => session.companions)
  const activeCast = computed(() => session.activeCast)
  const lastBeat = computed(() => beats.value[beats.value.length - 1] ?? null)

  /**
   * Cards are written with {{char}} / {{user}}; the thread has to show the names
   * this chat actually uses. Every place that renders stored text goes through
   * here, so an imported card reads normally instead of showing placeholders.
   */
  function withNames(text: string): string {
    return resolvePlaceholders(text, session.activeCast?.name ?? '', session.userName)
  }

  function reset() {
    // Abort first: a stream still writing into a cleared list would push its
    // beats back in after the reset.
    controller?.abort()
    controller = null
    beats.value = []
    busy.value = false
    pending.value = false
    buffer = ''
  }

  async function loadMessages() {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) {
      reset()
      return
    }
    loading.value = true
    error.value = ''
    try {
      // Segment cards live outside the message table; load them with the
      // branch so the thread can put each one back where it belongs.
      const sc = script.session
      if (sc && sc.branch_id === bid) await script.loadSummaries(sc.id)

      const list = await getMessages(cid, bid)
      const name = activeCast.value?.name ?? ''
      beats.value = list
        // Narration belongs in the thread, and so do compaction summaries:
        // they render as their own card rather than being dropped.
        .map<Beat>((m) => {
          if (m.message_type === 'compaction_summary') {
            const meta = (m.metadata ?? {}) as Record<string, unknown>
            return {
              uid: nextUid++,
              id: m.id,
              role: 'summary',
              speaker: '',
              time: nowTime(m.created_at),
              thinking: null,
              // metadata.summary holds the raw body; the content column wraps
              // it in markers that only the model needs to see.
              lines: parseLines(withNames(String(meta.summary ?? m.content))),
              swipe: null,
              folded: Number(meta.messages_compacted ?? 0),
              summaryVersion: Number(meta.compaction_version ?? 1),
            }
          }
          const narration = m.message_type === 'narration'
          const you = m.role === 'user' && !narration
          return {
            uid: nextUid++,
            id: m.id,
            role: narration ? 'narration' : you ? 'you' : 'ai',
            speaker: narration ? 'Narration' : you ? 'You' : name,
            time: nowTime(m.created_at),
            thinking: null,
            lines: parseLines(withNames(m.content)),
            swipe: m.swipes?.length ? m.swipes[m.swipe_id ?? 0] ?? null : null,
            swipes: m.swipes ?? [],
            swipeIndex: m.swipe_id ?? 0,
            generations: pickGenerations(m.metadata),
            voice: Boolean(m.metadata?.voice),
            marked: m.is_important,
            attachments: (m.metadata?.attachments as Array<Record<string, unknown>> | undefined)
              ?.filter((a) => a && a.kind === 'image' && typeof a.url === 'string')
              .map((a) => ({ src: String(a.url), alt: String(a.name ?? 'image') })),
          }
        })
      beats.value = mergeSceneCards(
        beats.value,
        sc && sc.branch_id === bid ? script.summaries : [],
        withNames,
      )
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
      beats.value = []
    } finally {
      loading.value = false
    }
  }

  function stop() {
    controller?.abort()
    controller = null
    busy.value = false
    pending.value = false
  }

  function pushUserBeat(text: string): Beat {
    beats.value.push({
      uid: nextUid++,
      id: tempId--,
      role: narrationMode.value ? 'narration' : 'you',
      speaker: narrationMode.value ? 'Narration' : 'You',
      time: nowTime(),
      thinking: null,
      lines: parseLines(withNames(text)),
      swipe: null,
    })
    return beats.value[beats.value.length - 1]
  }

  function pushAiBeat(): Beat {
    beats.value.push({
      uid: nextUid++,
      id: tempId--,
      role: 'ai',
      speaker: activeCast.value?.name ?? '',
      time: nowTime(),
      thinking: null,
      lines: [],
      swipe: null,
    })
    // Return the reactive proxy, not the literal: mutating the raw object
    // bypasses Vue's tracking and the stream only appears at onComplete.
    return beats.value[beats.value.length - 1]
  }

  function attach(target: Beat, userBeat?: Beat) {
    // Characters of `buffer` already folded into lines. Each delta then only
    // examines what arrived since, instead of re-splitting the whole reply.
    let committed = 0
    /**
     * Move the unterminated tail into `lines`.
     *
     * Runs on completion and on abort: an interrupted reply keeps whatever it
     * had produced, and leaving it in `tail` would hide it from copy, export
     * and TTS, all of which read `lines`.
     */
    const foldTail = () => {
      if (!target.tail) return
      target.lines.push({
        kind: target.tail.startsWith('(') ? 'direction' : 'say',
        text: target.tail,
      })
      target.tail = undefined
    }
    return {
      onChunk(chunk: ChatChunk) {
        if (chunk.type === 'content') {
          pending.value = false
          buffer += chunk.content ?? ''
          committed = commitLines(target, buffer, committed)
        } else if (chunk.type === 'stt_transcript') {
          // The engine's transcript for this voice turn. Kept on the beat so
          // edit and copy see the real words; the bubble still renders the
          // placeholder because the beat is flagged as voice.
          if (userBeat && typeof chunk.content === 'string') {
            userBeat.lines = parseLines(chunk.content)
          }
        } else if (chunk.type === 'thinking') {
          pending.value = false
          target.thinking = (target.thinking ?? '') + (chunk.content ?? '')
        } else if (chunk.type === 'done') {
          // Adopt the stored id so later actions hit the real row, without
          // re-fetching: a reload here repaints the whole thread mid-view.
          if (typeof chunk.message_id === 'number') target.id = chunk.message_id
          // Same for the user's own message. It was rendered before the request
          // with a temporary negative id, while search results and memory
          // sources carry the database id; without this the row can never be
          // scrolled to.
          if (typeof chunk.user_message_id === 'number' && userBeat) {
            userBeat.id = chunk.user_message_id
          }
          // The memories that went into this reply, for the rail's dots. An
          // empty array is meaningful here: it clears the previous reply's set.
          if (Array.isArray(chunk.memories)) session.setInPlay(chunk.memories)
          // The beat on screen holds one reply, so its record lands in slot 0;
          // a reload replaces this with the full per-swipe list. An empty reply
          // is never stored, so it gets no footer either.
          if (chunk.generation && typeof chunk.message_id === 'number' && chunk.message_id > 0) {
            target.generations = [chunk.generation]
          }
          pending.value = false
        } else if (chunk.type === 'script_state') {
          // The scene HUD and the completion flag, stripped from the reply by
          // the backend and delivered as its own frame.
          script.applyState(
            (chunk.hud as Record<string, unknown>) ?? {},
            Boolean(chunk.scene_done),
            Number(chunk.turns_done ?? 0),
          )
        } else if (chunk.type === 'error') {
          error.value = chunk.message ?? 'stream error'
        }
      },
      onError(err: Error) {
        error.value = err.message
        foldTail()
        stop()
      },
      onComplete() {
        stop()
        foldTail()
        void session.loadTokens(useLlmStore().tokenQuery)
        // The branch menu shows a message count per branch, so it is stale as
        // soon as a reply lands. Cheap to re-read, and the v-for keys keep the
        // menu from repainting.
        void session.loadBranches()
        // Queue the reply that just finished. Setting this here — rather than
        // when the toggle flips — is what keeps the switch from replaying the
        // message already on screen: only replies completed after the switch
        // was turned on ever reach this point with autoRead already true.
        if (autoRead.value && target.lines.length) autoReadUid.value = target.uid
      },
    }
  }

  function send(
    rawText: string,
    attachments: AttachmentRef[] = [],
    senderOverride?: 'user' | 'narrator',
  ) {
    if (busy.value) {
      stop()
      return
    }
    const text = rawText.trim()
    if (!text) return
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) {
      error.value = 'no companion or branch selected'
      return
    }

    const userBeat = pushUserBeat(text)
    const target = pushAiBeat()
    buffer = ''
    busy.value = true
    pending.value = true
    error.value = ''

    controller = chatStream(
      cid,
      bid,
      text,
      llmConfig(),
      false,
      attach(target, userBeat),
      attachments,
      senderOverride ?? (narrationMode.value ? 'narrator' : 'user'),
      script.session && script.session.branch_id === bid ? script.session.id : 0,
    )
  }

  /**
   * Voice input. The audio goes up whole; the backend transcribes it, stores the
   * transcript as this turn's user message, then answers in the same stream.
   */
  function sendAudio(audio: Blob) {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid || busy.value) return
    // The transcript is produced by the backend, so this beat starts empty and
    // flagged as voice: the bubble renders the placeholder, while the stored
    // transcript still reaches the model on every later turn. Without this push
    // the turn had no user message at all until the thread reloaded.
    const userBeat = pushUserBeat('')
    userBeat.voice = true
    const target = pushAiBeat()
    buffer = ''
    busy.value = true
    pending.value = true
    error.value = ''
    controller = audioStream(
      cid,
      bid,
      audio,
      llmConfig(),
      false,
      attach(target, userBeat),
      narrationMode.value ? 'narrator' : 'user',
    )
  }

  function continueChat() {
    if (busy.value) return
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) return

    const target = pushAiBeat()
    buffer = ''
    busy.value = true
    pending.value = true
    error.value = ''

    controller = continueStream(cid, bid, llmConfig(), false, attach(target))
  }

  function regenerate(id: number) {
    if (busy.value) return
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) return
    const idx = beats.value.findIndex((b) => b.id === id)
    if (idx < 0) return

    beats.value.splice(idx, 1)
    const target = pushAiBeat()
    buffer = ''
    busy.value = true
    pending.value = true
    error.value = ''

    controller = regenerateStream(cid, bid, id, llmConfig(), false, attach(target))
  }

  /**
   * Rewrite a user message and re-ask the model.
   *
   * Everything after the edited beat is discarded — both here and on the
   * server — because those turns answered the old wording. The reply is then
   * streamed into a fresh AI beat.
   */
  function reedit(id: number, text: string) {
    if (busy.value) return
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) return
    const idx = beats.value.findIndex((b) => b.id === id)
    if (idx < 0) return

    const edited = beats.value[idx]
    edited.lines = parseLines(text)
    beats.value.splice(idx + 1)

    const target = pushAiBeat()
    buffer = ''
    busy.value = true
    pending.value = true
    error.value = ''

    controller = reeditStream(cid, bid, id, text, llmConfig(), false, attach(target))
  }

  /**
   * The beat the user just jumped to, so the thread can flash it. Cleared after
   * the highlight plays; keeping it would leave a stale class on the element
   * and a second jump to the same message would not re-animate.
   */
  const spotId = ref<number | null>(null)
  let spotTimer = 0

  async function compactNow(): Promise<{ ok: boolean; message: string }> {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid || busy.value) return { ok: false, message: '' }
    busy.value = true
    try {
      const r = await compactBranch(cid, bid, {
        preserve_recent: 5,
        max_summary_tokens: 2000,
        preserve_important: true,
        max_important_messages: 20,
      })
      await loadMessages()
      await session.loadTokens()
      if (r && r.success === false) return { ok: false, message: r.error || '' }
      return { ok: true, message: '' }
    } catch (e) {
      return { ok: false, message: e instanceof Error ? e.message : String(e) }
    } finally {
      busy.value = false
    }
  }

  /** Scroll the thread to one beat, used by search results. */
  function jumpToBeat(id: number) {
    const el = document.querySelector(`[data-beat-id="${id}"]`)
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    window.clearTimeout(spotTimer)
    spotId.value = null
    // One frame between clearing and setting: Vue only restarts a CSS animation
    // when the class is removed and re-added across a render.
    requestAnimationFrame(() => {
      spotId.value = id
      spotTimer = window.setTimeout(() => (spotId.value = null), 4500)
    })
  }

  async function toggleMark(id: number) {
    const b = beats.value.find((x) => x.id === id)
    if (!b) return
    const next = !b.marked
    b.marked = next
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid || id < 0) return
    try {
      await updateMessage(cid, bid, id, { is_important: next })
    } catch (e) {
      b.marked = !next
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  async function removeBeat(id: number) {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    beats.value = beats.value.filter((b) => b.id !== id)
    if (!cid || !bid || id < 0) return
    try {
      await deleteSingleMessage(cid, bid, id)
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  async function updateBeat(id: number, text: string) {
    const b = beats.value.find((x) => x.id === id)
    if (!b) return
    b.lines = parseLines(text)
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid || id < 0) return
    try {
      await updateMessage(cid, bid, id, { content: text })
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  async function removeCast(id: string) {
    try {
      await deleteCompanion(id)
      await session.loadCompanions()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  function newBranch() {
    emptyStage.value = true
    void session.newBranch()
  }

  return {
    beats,
    cast,
    busy,
    pending,
    narrationMode,
    autoRead,
    autoReadUid,
    emptyStage,
    loading,
    showJump,
    scrollRequest,
    error,
    lastBeat,
    activeCast,
    loadMessages,
    send,
    continueChat,
    sendAudio,
    regenerate,
    reedit,
    stop,
    toggleMark,
    jumpToBeat,
    spotId,
    compactNow,
    removeBeat,
    updateBeat,
    removeCast,
    newBranch,
    reset,
  }
})

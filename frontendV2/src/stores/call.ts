/**
 * Voice call.
 *
 * One utterance at a time, but the transcript arrives while the user is still
 * talking: the microphone streams 16 kHz PCM over `/ws/call`, the backend pumps
 * it into audio.cpp's streaming ASR, and deltas come back as SSE. The user ends
 * the turn by saying the end keyword — silence is never interpreted, so pausing
 * to think is free. The final text then goes through the normal `chat.send()`
 * path — messages, persistence, memory and tools all behave exactly as they do
 * for typed input. The reply is then spoken with the same TTS config the panel
 * uses, and the call goes back to listening.
 *
 * Phases: idle → listening → transcribing → thinking → speaking → listening.
 */
import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import { CALL_WS_URL } from '@/api/client'
import { shouldStream, streamSampleRate, synthesize, synthesizeStream } from '@/api/tts'
import { CallMic } from '@/lib/callAudio'
import { PcmPlayer } from '@/lib/pcmPlayer'
import { loadVad, resetVad, voiceProbability } from '@/lib/vad'
import { useChatStore } from './chat'
import { useTtsStore } from './tts'

export type CallPhase = 'idle' | 'listening' | 'transcribing' | 'thinking' | 'speaking'

/** 32 ms per frame from the worklet. */
const FRAME_MS = 32
/**
 * The word that ends an utterance.
 *
 * Turn-taking is explicit: the user says this word when the sentence is done, so
 * nothing has to guess from silence. That is the point — a five-second pause to
 * think and a finished sentence look identical to a VAD, and only the speaker
 * knows which one it is. Matched as a whole word against the normalised
 * transcript, so it cannot fire inside a longer word; empty disables it.
 */
const END_KEYWORD = 'Beamish'
/**
 * Spellings the ASR plausibly returns for the same sound.
 *
 * The model has never read this word in a Chinese roleplay transcript, so it
 * transliterates by ear: a short vowel comes back "bimish", a long one
 * "beemish", and a Mandarin-biased decode may simply write 比米什. All of them
 * mean stop. One substituted letter is covered by the distance check below.
 */
const END_WORDS = ['beamish', 'bimish', '比米什']
/** Barge-in needs to clear a higher bar so the speakers cannot trigger it. */
const BARGE_THRESHOLD = 0.05
/** Continuous voice during playback that starts lowering the reply. */
const DUCK_MS = 200
/** Continuous voice during playback that takes the turn over. */
const BARGE_MS = 600
/**
 * Speech probability above which a frame counts as the user talking.
 *
 * Higher than the model's own 0.5 because a false barge-in is worse than a late
 * one: the assistant stops mid-sentence for no reason. Missing it only means
 * waiting for the next frame.
 */
const VAD_THRESHOLD = 0.65
/** Consecutive speech frames needed: two is 64 ms, enough to skip a click. */
const VAD_FRAMES = 2
/** Give up on a transcript that never arrives. */
const FINAL_TIMEOUT_MS = 8000

/** Lowercase, drop punctuation, collapse whitespace: matching ignores typography. */
function normalizeSpeech(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

/**
 * True when the end keyword has been spoken.
 *
 * The transcript is cumulative, so the keyword can sit anywhere in the text and
 * still end the turn. Whole-word comparison keeps a short keyword from firing
 * on a longer word that merely contains it.
 */
/**
 * True when two words are at most one edit apart.
 *
 * Deliberately not a general Levenshtein: only distance <= 1 is interesting,
 * and the guard against ordinary words depends on it staying that tight.
 * "beamish" and "blemish" are two edits apart, so a looser check would fire on
 * someone describing a blemish.
 */
function withinOneEdit(a: string, b: string): boolean {
  if (a === b) return true
  const [short, long] = a.length <= b.length ? [a, b] : [b, a]
  if (long.length - short.length > 1) return false
  let i = 0
  let j = 0
  let skipped = false
  while (i < short.length && j < long.length) {
    if (short[i] === long[j]) {
      i++
      j++
      continue
    }
    if (skipped) return false
    skipped = true
    if (short.length === long.length) i++
    j++
  }
  return true
}

function isEndWord(word: string): boolean {
  return END_WORDS.some((w) => withinOneEdit(word, w))
}

/** Han characters do not need word boundaries; latin ones do. */
function isHan(word: string): boolean {
  return /[\u3400-\u9fff]/.test(word)
}

/**
 * True when the end keyword has been spoken.
 *
 * The transcript is cumulative, so the keyword can sit anywhere in it. Words
 * are compared one at a time, which is what keeps a short keyword from firing
 * on a longer word that merely contains the letters.
 */
function hitEndKeyword(text: string | undefined): boolean {
  if (!text) return false
  // Chinese has no word spaces, so 比米什 can sit inside a longer run.
  if (END_WORDS.some((w) => isHan(w) && text.includes(w))) return true
  const words = normalizeSpeech(text).split(' ').filter(Boolean)
  if (words.some(isEndWord)) return true
  // An unfamiliar name sometimes comes back split in two ("beam ish", "beam
  // is"). Only the trailing pair is joined: "a beam is here" ends in "is here".
  if (words.length < 2) return false
  return withinOneEdit(words[words.length - 2] + words[words.length - 1], END_WORDS[0])
}

/**
 * Cut the keyword and everything after it.
 *
 * The user stops talking on the word, so anything past it is a stray syllable
 * the ASR had already picked up. The scan runs forward and cuts at the first
 * hit: repeating the keyword is common, and every copy has to go.
 */
function stripEndKeyword(text: string): string {
  if (!text) return text
  for (const w of END_WORDS) {
    if (!isHan(w)) continue
    const at = text.indexOf(w)
    if (at >= 0) return text.slice(0, at).trim().replace(/[\s,.;:!?，。；：！？、-]+$/, '')
  }
  const hits = [...text.matchAll(/[\p{L}\p{N}]+/gu)]
  // Forward scan, cut at the first hit. Backward scanning kept only the last
  // one, so "…very stupid. Beamish. Beamish" lost its tail and still carried
  // the first keyword into the message. The ASR repeats words, so a single
  // occurrence is not the case worth designing around.
  for (let k = 0; k < hits.length; k++) {
    if (!isEndWord(hits[k][0].toLowerCase())) continue
    const cut = hits[k].index ?? 0
    return text.slice(0, cut).trim().replace(/[\s,.;:!?，。；：！？、-]+$/, '')
  }
  // Split form: the trailing pair joined is the keyword.
  const parts = text.trim().split(/\s+/)
  if (parts.length >= 2) {
    const joined = (parts[parts.length - 2] + parts[parts.length - 1])
      .toLowerCase()
      .replace(/[^\p{L}\p{N}]/gu, '')
    if (withinOneEdit(joined, END_WORDS[0])) return parts.slice(0, -2).join(' ').trim()
  }
  return text
}

const MIN_FIRST_SENTENCE = 8
const MIN_NEXT_SENTENCE = 16
const MAX_VAD_QUEUE = 12

/** Split ready sentences out of *buf* for LLM->TTS pipelining, returning sentences and consumed length. */
function takeSpeakableSentences(
  buf: string,
  flushAll: boolean,
  isFirst: boolean,
): { sentences: string[]; consumed: number } {
  const sentences: string[] = []
  let start = 0
  const minLen = () => (isFirst && sentences.length === 0 ? MIN_FIRST_SENTENCE : MIN_NEXT_SENTENCE)
  const zhEnd = new Set(['。', '！', '？', '；'])
  const enEnd = new Set(['.', '!', '?'])
  const closers = new Set(['”', '’', '」', '』', '"', "'", ')', '）'])

  for (let i = 0; i < buf.length; i++) {
    const ch = buf[i]
    let breakEnd = -1
    if (ch === '\n') {
      breakEnd = i + 1
    } else if (zhEnd.has(ch)) {
      let end = i + 1
      while (end < buf.length && (closers.has(buf[end]) || buf[end] === ' ')) end++
      breakEnd = end
    } else if (enEnd.has(ch)) {
      let end = i + 1
      while (end < buf.length && closers.has(buf[end])) end++
      // Avoid breaking inside abbreviations/decimals unless followed by whitespace or CJK/newline.
      if (end < buf.length && (buf[end] === ' ' || buf[end] === '\t' || buf[end] === '\n')) {
        while (end < buf.length && (buf[end] === ' ' || buf[end] === '\t')) end++
        breakEnd = end
      }
    }
    if (breakEnd > start) {
      const piece = buf.slice(start, breakEnd).trim()
      if (piece.length >= minLen()) {
        sentences.push(piece)
        start = breakEnd
        i = breakEnd - 1
      }
    }
  }

  if (flushAll && start < buf.length) {
    const tail = buf.slice(start).trim()
    if (tail) sentences.push(tail)
    start = buf.length
  }
  return { sentences, consumed: start }
}

/** Match backend _filter_tts_text so pure stage-direction chunks are skipped cleanly. */
function hasSpeakableText(text: string, stripStageDirections: boolean): boolean {
  let cleaned = text.replace(/[（(][^）)]*[）)]/g, '')
  if (stripStageDirections) cleaned = cleaned.replace(/\*[^*\n]*\*/g, '')
  return cleaned.trim().length > 0
}

interface ServerFrame {
  type?: string
  text?: string
  message?: string
}

export const useCallStore = defineStore('call', () => {
  const chat = useChatStore()
  const tts = useTtsStore()

  const active = ref(false)
  const phase = ref<CallPhase>('idle')
  /** True while the backend is still starting the ASR engine. */
  const warming = ref(false)
  const level = ref(0)
  const seconds = ref(0)
  /**
   * Latest partial transcript from the streaming ASR. Not rendered anywhere:
   * the reply lands in the thread, and a second copy on the panel would just be
   * the same sentence twice. Kept because it is the only running record of what
   * the engine heard while the user is still speaking.
   */
  const partial = ref('')
  const errMsg = ref('')

  let mic: CallMic | null = null
  let ws: WebSocket | null = null
  let pcm: PcmPlayer | null = null
  let audioEl: HTMLAudioElement | null = null
  let abortCtl: AbortController | null = null
  let raf = 0
  let timer = 0
  let errTimer = 0

  /** Barge-in accumulator, reset per utterance. */
  let bargeMs = 0
  /** Consecutive frames the VAD called speech. */
  let voiceFrames = 0
  /** Ordered VAD frame queue + draining flag so recurrent state stays sequential. */
  const vadQueue: Float32Array[] = []
  let vadBusy = false
  /** Monotonic turn counter; bumps on barge-in or hangup to stop in-flight cascades cleanly. */
  let activeTurnId = 0
  let wakeCascade: (() => void) | null = null
  /** True between `start` and `stop` on the socket. */
  let streaming = false
  /** The backend only accepts PCM once it has opened the ASR stream. */
  let wsReady = false
  /** Set while a transcript is in flight, so a late frame is not lost. */
  let awaitingFinal = false
  let finalTimer = 0

  const canSpeak = () => tts.active !== null

  function showErr(message: string): void {
    errMsg.value = message
    window.clearTimeout(errTimer)
    errTimer = window.setTimeout(() => (errMsg.value = ''), 5000)
  }

  function send(frame: Record<string, unknown>): void {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(frame))
  }

  function resetUtterance(): void {
    partial.value = ''
    bargeMs = 0
    voiceFrames = 0
    vadQueue.length = 0
    resetVad()
  }

  /** Ask the backend for a fresh ASR session. */
  function openUtterance(): void {
    if (!active.value || streaming) return
    resetUtterance()
    streaming = true
    wsReady = false
    warming.value = true
    awaitingFinal = false
    send({ type: 'start' })
  }

  /** Close the current utterance and wait for its final transcript. */
  function closeUtterance(): void {
    if (!streaming) return
    streaming = false
    wsReady = false
    awaitingFinal = true
    phase.value = 'transcribing'
    send({ type: 'stop' })
    window.clearTimeout(finalTimer)
    finalTimer = window.setTimeout(() => {
      if (!awaitingFinal) return
      awaitingFinal = false
      showErr('语音识别超时')
      resumeListening()
    }, FINAL_TIMEOUT_MS)
  }

  function resumeListening(): void {
    if (!active.value) return
    phase.value = 'listening'
    resetUtterance()
    openUtterance()
  }

  // ---------- server frames ----------
  function onServerFrame(raw: string): void {
    let frame: ServerFrame
    try {
      frame = JSON.parse(raw)
    } catch {
      return
    }
    if (frame.type === 'ready') {
      wsReady = true
      warming.value = false
      return
    }
    if (frame.type === 'partial') {
      partial.value = frame.text ?? ''
      // The speaker owns the turn: the keyword ends it the moment it shows up.
      // `closeUtterance` is idempotent, so the following frames that still
      // carry the keyword are harmless.
      if (hitEndKeyword(frame.text)) {
        closeUtterance()
      } else if (import.meta.env.DEV) {
        // The only way to see what the engine actually heard: without it a
        // missed keyword is indistinguishable from a broken stream.
        console.debug('[call] heard:', frame.text)
      }
      return
    }
    if (frame.type === 'final') {
      window.clearTimeout(finalTimer)
      if (!awaitingFinal) return
      awaitingFinal = false
      const text = stripEndKeyword((frame.text ?? '').trim())
      if (!text) {
        resumeListening()
        return
      }
      partial.value = ''
      void askAi(text)
      return
    }
    if (frame.type === 'error') {
      window.clearTimeout(finalTimer)
      awaitingFinal = false
      showErr(frame.message || '语音识别失败')
      resumeListening()
    }
  }

  // ---------- pipeline ----------
  async function askAi(text: string): Promise<void> {
    if (!active.value) return
    const turnId = ++activeTurnId
    phase.value = 'thinking'
    const before = chat.beats.length
    chat.send(text)

    const aiBeat = chat.beats.slice(before).find((b) => b.role === 'ai')
    if (!aiBeat) {
      resumeListening()
      return
    }

    const cfg = tts.activeFor(chat.activeCast?.ttsVoice)
    if (!cfg) {
      await new Promise<void>((resolve) => {
        if (!chat.busy) {
          resolve()
          return
        }
        const stop = watch(
          () => chat.busy,
          (b) => {
            if (!b) {
              stop()
              resolve()
            }
          },
        )
      })
      if (active.value && turnId === activeTurnId) resumeListening()
      return
    }

    const stripStage = cfg.speakStageDirections === false
    const queue: string[] = []
    let cursor = 0
    let llmDone = !chat.busy
    let firstQueued = true

    const notifyQueue = (): void => {
      const w = wakeCascade
      wakeCascade = null
      w?.()
    }

    const pullSentences = (): void => {
      const live = [...aiBeat.lines.map((l) => l.text), aiBeat.tail ?? '']
        .filter(Boolean)
        .join('\n')
      const flush = !chat.busy
      if (live.length > cursor || flush) {
        const slice = live.slice(cursor)
        const { sentences, consumed } = takeSpeakableSentences(slice, flush, firstQueued)
        cursor += consumed
        for (const s of sentences) {
          if (hasSpeakableText(s, stripStage)) {
            queue.push(s)
            firstQueued = false
          }
        }
      }
      if (!chat.busy) llmDone = true
      notifyQueue()
    }

    const stopWatch = watch(
      [() => aiBeat.lines.length, () => aiBeat.tail, () => chat.busy],
      () => pullSentences(),
    )
    pullSentences()

    const waitNext = (): Promise<void> =>
      new Promise((resolve) => {
        wakeCascade = resolve
      })

    try {
      while (active.value && turnId === activeTurnId && queue.length === 0 && !llmDone) {
        await waitNext()
      }
      if (!active.value || turnId !== activeTurnId || queue.length === 0) {
        return
      }

      phase.value = 'speaking'
      bargeMs = 0
      const ac = new AbortController()
      abortCtl = ac

      if (shouldStream(cfg, tts.specs)) {
        const player = new PcmPlayer(streamSampleRate(cfg, tts.specs))
        pcm = player
        await player.start()
        while (active.value && turnId === activeTurnId) {
          if (queue.length > 0) {
            const sentence = queue.shift()!
            await synthesizeStream(cfg, sentence, (chunk) => player.push(chunk), ac.signal)
          } else if (llmDone) {
            break
          } else {
            await waitNext()
          }
        }
        if (active.value && turnId === activeTurnId) {
          await player.drain()
        }
      } else {
        while (active.value && turnId === activeTurnId) {
          if (queue.length > 0) {
            const sentence = queue.shift()!
            const blob = await synthesize(cfg, sentence)
            if (!active.value || turnId !== activeTurnId) break
            await playBlob(blob)
          } else if (llmDone) {
            break
          } else {
            await waitNext()
          }
        }
      }
    } catch {
      /* aborted or failed: either way the call moves on */
    } finally {
      stopWatch()
      wakeCascade = null
      if (pcm) {
        const player = pcm
        pcm = null
        void player.close()
      }
      abortCtl = null
      if (audioEl) {
        audioEl.pause()
        audioEl = null
      }
      if (active.value && turnId === activeTurnId) resumeListening()
    }
  }

  function playBlob(blob: Blob): Promise<void> {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(blob)
      const audio = new Audio(url)
      audioEl = audio
      const done = (): void => {
        audio.onended = null
        audio.onerror = null
        URL.revokeObjectURL(url)
        resolve()
      }
      audio.onended = done
      audio.onerror = done
      void audio.play().catch(done)
    })
  }

  // ---------- barge-in ----------
  /** s16le from the worklet → the [-1, 1] floats the model expects. */
  function toSamples(pcm: Uint8Array): Float32Array {
    const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength)
    const out = new Float32Array(pcm.byteLength >> 1)
    for (let i = 0; i < out.length; i++) out[i] = view.getInt16(i * 2, true) / 32768
    return out
  }

  function interrupt(): void {
    if (phase.value !== 'speaking') return
    activeTurnId += 1
    wakeCascade?.()
    wakeCascade = null
    vadQueue.length = 0
    abortCtl?.abort()
    pcm?.abort()
    audioEl?.pause()
    if (chat.busy) chat.stop()
    resumeListening()
  }

  /** Manually finish the current spoken turn without waiting for the end keyword. */
  function endTurn(): void {
    if (!active.value || phase.value !== 'listening' || !streaming) return
    closeUtterance()
  }

  async function drainVadQueue(): Promise<void> {
    if (vadBusy) return
    vadBusy = true
    try {
      while (vadQueue.length > 0 && active.value && phase.value === 'speaking') {
        const samples = vadQueue.shift()!
        const prob = await voiceProbability(samples)
        if (!active.value || phase.value !== 'speaking') break
        // Model not loaded yet: the energy timer keeps barge-in alive.
        if (prob === null) {
          if (bargeMs >= BARGE_MS) {
            bargeMs = 0
            interrupt()
            break
          }
          continue
        }
        voiceFrames = prob >= VAD_THRESHOLD ? voiceFrames + 1 : 0
        if (voiceFrames >= VAD_FRAMES) {
          voiceFrames = 0
          bargeMs = 0
          interrupt()
          break
        }
      }
    } catch {
      /* keep barge-in non-fatal */
    } finally {
      vadBusy = false
    }
  }

  // ---------- audio frames ----------
  function onFrame(frame: { pcm: Uint8Array; level: number }): void {
    if (!active.value) return
    const speaking = phase.value === 'speaking'
    if (!speaking) level.value = frame.level

    if (speaking) {
      // Keep the microphone live while the assistant talks: that is the only
      // way to hear an interruption. Loudness still drives the volume dip,
      // because a dip on a false positive costs nothing. Whether the user is
      // really speaking is the VAD's call — a fan and a quiet voice are the
      // same loudness and opposite answers.
      if (frame.level >= BARGE_THRESHOLD) {
        bargeMs += FRAME_MS
        if (bargeMs >= DUCK_MS) pcm?.duck()
      } else {
        bargeMs = Math.max(0, bargeMs - FRAME_MS)
        if (bargeMs < DUCK_MS) pcm?.unduck()
      }
      if (vadQueue.length >= MAX_VAD_QUEUE) {
        vadQueue.length = 0
        resetVad()
      }
      vadQueue.push(toSamples(frame.pcm))
      void drainVadQueue()
      return
    }

    if (phase.value === 'thinking') return

    // Until the backend has opened the ASR stream there is nowhere to put the
    // audio. Whether the utterance is over is decided by the end keyword in the
    // transcript, never by how long a pause lasted.
    if (!wsReady) return

    if (streaming && ws && ws.readyState === WebSocket.OPEN) ws.send(frame.pcm)
  }

  // ---------- lifecycle ----------
  async function start(): Promise<void> {
    if (active.value) return
    if (!canSpeak()) {
      showErr('请先在后台设置里配置音色')
      return
    }
    // Without a keyword nothing can end a turn, and the call would record until
    // the user hangs up. Refuse instead of pretending to work.
    if (!END_WORDS.length) {
      showErr('通话需要先配置结束关键词')
      return
    }
    active.value = true
    phase.value = 'listening'
    // Warm the VAD while the microphone opens: the model and its wasm runtime
    // take a few hundred milliseconds, and the first interruption would
    // otherwise fall back to the energy timer for its whole duration.
    void loadVad().catch(() => {})
    level.value = 0
    seconds.value = 0
    partial.value = ''
    errMsg.value = ''
    bargeMs = 0
    streaming = false
    awaitingFinal = false

    timer = window.setInterval(() => {
      seconds.value += 1
    }, 1000)

    try {
      mic = await CallMic.open()
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      showErr(
        /permission|denied/i.test(message)
          ? '麦克风权限被拒绝，请在系统设置里允许后重试'
          : '无法访问麦克风',
      )
      hangup()
      return
    }

    try {
      ws = new WebSocket(CALL_WS_URL)
    } catch {
      showErr('无法连接通话服务')
      hangup()
      return
    }
    ws.onmessage = (event) => {
      if (typeof event.data === 'string') onServerFrame(event.data)
    }
    ws.onerror = () => showErr('通话连接中断')
    ws.onclose = () => {
      if (active.value) showErr('通话连接已关闭')
    }
    ws.onopen = () => {
      if (!active.value) return
      openUtterance()
    }

    mic.onFrame = onFrame
    tick()
  }

  function tick(): void {
    raf = requestAnimationFrame(tick)
    if (phase.value === 'speaking' && pcm) level.value = pcm.getLevel()
  }

  function hangup(): void {
    active.value = false
    phase.value = 'idle'
    activeTurnId += 1
    wakeCascade?.()
    wakeCascade = null
    vadQueue.length = 0
    streaming = false
    awaitingFinal = false
    window.clearInterval(timer)
    timer = 0
    window.clearTimeout(errTimer)
    window.clearTimeout(finalTimer)
    if (raf) cancelAnimationFrame(raf)
    raf = 0

    mic?.close()
    mic = null
    abortCtl?.abort()
    abortCtl = null
    pcm?.abort()
    pcm = null
    if (audioEl) {
      audioEl.pause()
      audioEl = null
    }
    if (ws) {
      try {
        ws.send(JSON.stringify({ type: 'close' }))
      } catch {
        /* socket already gone */
      }
      ws.close()
      ws = null
    }
    level.value = 0
    partial.value = ''
    warming.value = false
  }

  return {
    active,
    phase,
    warming,
    level,
    seconds,
    partial,
    errMsg,
    /** Shown in the panel; empty when no keyword is configured yet. */
    endKeyword: END_KEYWORD,
    start,
    hangup,
    interrupt,
    endTurn,
  }
})

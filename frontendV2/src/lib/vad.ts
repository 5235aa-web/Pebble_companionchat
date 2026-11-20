/**
 * Silero VAD v5, running in the browser over ONNX Runtime Web.
 *
 * The voice call needs to know whether the user is *speaking*, not whether the
 * microphone is loud. A fan and a quiet voice sit at the same RMS in opposite
 * classes, which is why an energy threshold misfires in both directions. This
 * model answers the question directly, from the spectral shape of the frame.
 *
 * Used only for barge-in. Turn-taking is the end keyword, never a model.
 *
 * Both interface generations are supported: v5 takes 512 new samples plus the
 * 64 that ended the previous frame, with a recurrent state; v4 takes the frame
 * alone with an LSTM pair. The shape is read from the session's input names.
 */
import * as ort from 'onnxruntime-web'

const MODEL_URL = '/models/silero_vad.onnx'
/** New samples per call. Matches the worklet's frame, so nothing is buffered. */
const FRAME = 512
/** v5 carries the previous frame's tail as leading context. */
const CONTEXT = 64
const SAMPLE_RATE = 16000
const STATE_SIZE = 128
const RNN_SIZE = 64

let session: ort.InferenceSession | null = null
let loading: Promise<void> | null = null
let v5 = false
/** v5 recurrent state, 2 x 1 x 128. */
let state: ort.Tensor | null = null
/** v4 LSTM pair, 2 x 1 x 64 each. */
let h: ort.Tensor | null = null
let c: ort.Tensor | null = null
let context = new Float32Array(CONTEXT)

function zeros(shape: number[]): ort.Tensor {
  const size = shape.reduce((a, b) => a * b, 1)
  return new ort.Tensor('float32', new Float32Array(size), shape)
}

export function vadReady(): boolean {
  return session !== null
}

/**
 * Load the model once. Safe to call repeatedly: concurrent callers share the
 * same promise, and a failure clears it so the next attempt can retry.
 */
export function loadVad(): Promise<void> {
  if (session) return Promise.resolve()
  if (loading) return loading
  loading = (async () => {
    // One thread: the Tauri webview has no cross-origin isolation, so a
    // threaded wasm build cannot spawn its pool and would fail at startup.
    ort.env.wasm.numThreads = 1
    ort.env.wasm.wasmPaths = '/ort/'
    const s = await ort.InferenceSession.create(MODEL_URL)
    v5 = s.inputNames.includes('state')
    state = v5 ? zeros([2, 1, STATE_SIZE]) : null
    h = v5 ? null : zeros([2, 1, RNN_SIZE])
    c = v5 ? null : zeros([2, 1, RNN_SIZE])
    session = s
  })().catch((err) => {
    loading = null
    session = null
    throw err
  })
  return loading
}

/** Forget everything heard so far. Call when a new utterance starts. */
export function resetVad(): void {
  context = new Float32Array(CONTEXT)
  if (v5) {
    state = zeros([2, 1, STATE_SIZE])
  } else {
    h = zeros([2, 1, RNN_SIZE])
    c = zeros([2, 1, RNN_SIZE])
  }
}

/**
 * Probability that *frame* is speech, in [0, 1], or null before the model has
 * loaded. Frames must be fed in order: the model carries state across calls, so
 * dropping or repeating one degrades the next several judgements.
 */
export async function voiceProbability(frame: Float32Array): Promise<number | null> {
  const s = session
  if (!s) return null

  const input = new Float32Array(CONTEXT + FRAME)
  input.set(context, 0)
  input.set(frame.subarray(0, FRAME), CONTEXT)
  // The next call needs the tail of this frame, not its head.
  context = frame.slice(FRAME - CONTEXT, FRAME)

  const feeds: Record<string, ort.Tensor> = {
    input: new ort.Tensor('float32', input, [1, CONTEXT + FRAME]),
  }
  if (v5) {
    feeds.state = state ?? zeros([2, 1, STATE_SIZE])
    feeds.sr = new ort.Tensor('int64', BigInt64Array.from([BigInt(SAMPLE_RATE)]), [1])
  } else {
    feeds.h = h ?? zeros([2, 1, RNN_SIZE])
    feeds.c = c ?? zeros([2, 1, RNN_SIZE])
  }

  const out = await s.run(feeds)
  const next = out.stateN ?? out.hn
  if (v5 && next) {
    state = next
  } else {
    if (out.hn) h = out.hn
    if (out.cn) c = out.cn
  }
  const data = out.output?.data as Float32Array | undefined
  return data && data.length ? data[0] : null
}

/**
 * Microphone capture for the voice call.
 *
 * The worklet owns the 16 kHz decimation and posts 512-sample Float32 frames
 * (32 ms); this class turns them into s16le for the socket and reports a
 * per-frame level so the panel's waveform follows the voice.
 *
 * The node has to be connected to something or the audio graph never pulls it,
 * so it runs into a zero-gain sink: the graph stays alive and the microphone is
 * never played back through the speakers.
 */
const FRAME_SAMPLES = 512

export interface CallMicFrame {
  /** 16 kHz mono s16le, ready to go on the wire. */
  pcm: Uint8Array
  /** RMS of the frame, 0..1. */
  level: number
}

function toPcm16(frame: Float32Array): Uint8Array {
  const out = new Uint8Array(frame.length * 2)
  const view = new DataView(out.buffer)
  for (let i = 0; i < frame.length; i++) {
    const s = Math.max(-1, Math.min(1, frame[i]))
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true)
  }
  return out
}

function rms(frame: Float32Array): number {
  let sum = 0
  for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i]
  return Math.sqrt(sum / frame.length)
}

export class CallMic {
  private ctx: AudioContext
  private stream: MediaStream
  private source: MediaStreamAudioSourceNode
  private node: AudioWorkletNode
  private sink: GainNode

  onFrame: ((frame: CallMicFrame) => void) | null = null

  private constructor(
    ctx: AudioContext,
    stream: MediaStream,
    source: MediaStreamAudioSourceNode,
    node: AudioWorkletNode,
    sink: GainNode,
  ) {
    this.ctx = ctx
    this.stream = stream
    this.source = source
    this.node = node
    this.sink = sink
  }

  static async open(): Promise<CallMic> {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    })

    const Ctx =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    const ctx = new Ctx()
    try {
      // The worklet ships in public/ so the packaged shell serves it from the
      // same origin; BASE_URL keeps that true under a non-root base.
      await ctx.audioWorklet.addModule(`${import.meta.env.BASE_URL}call-mic-worklet.js`)
      if (ctx.state === 'suspended') await ctx.resume()

      const source = ctx.createMediaStreamSource(stream)
      const node = new AudioWorkletNode(ctx, 'call-mic', {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        channelCount: 1,
        processorOptions: { frame: FRAME_SAMPLES },
      })
      const sink = ctx.createGain()
      sink.gain.value = 0

      source.connect(node)
      node.connect(sink)
      sink.connect(ctx.destination)

      const mic = new CallMic(ctx, stream, source, node, sink)
      node.port.onmessage = (event: MessageEvent<Float32Array>) => {
        const data = event.data
        if (!data || !data.length) return
        mic.onFrame?.({ pcm: toPcm16(data), level: rms(data) })
      }
      return mic
    } catch (err) {
      stream.getTracks().forEach((track) => track.stop())
      void ctx.close()
      throw err
    }
  }

  /** Level of the microphone right now, for a waveform that keeps breathing. */
  get sampleRate(): number {
    return this.ctx.sampleRate
  }

  close(): void {
    this.onFrame = null
    this.node.port.onmessage = null
    try {
      this.source.disconnect()
      this.node.disconnect()
      this.sink.disconnect()
    } catch {
      /* already torn down */
    }
    this.stream.getTracks().forEach((track) => track.stop())
    void this.ctx.close().catch(() => undefined)
  }
}

/**
 * Plays s16le PCM as it arrives.
 *
 * The TTS stream endpoint sends raw samples rather than a container, so there is
 * no file to hand to `<audio>`. Each chunk becomes an AudioBuffer scheduled at a
 * running cursor: starting every buffer at `currentTime` would overlap them and
 * click, and starting at zero each time would cut the previous one off.
 */

/** Lead-in before the first buffer, so a slow chunk cannot underrun. */
const LEAD_S = 0.08

export class PcmPlayer {
  private ctx: AudioContext
  private readonly rate: number
  /** Absolute context time at which the queued audio runs out. */
  private cursor = 0
  private closed = false
  /** Live sources, so a barge-in can cut the sentence mid-word. */
  private sources = new Set<AudioBufferSourceNode>()
  /** Taps the same signal the speakers get, for the call panel's waveform. */
  private analyser: AnalyserNode
  private levelBuf: Uint8Array
  /** Sits between the sources and the speakers so a barge-in can dip the volume. */
  private gain: GainNode
  private ducked = false
  /** Leftover trailing byte when a transport chunk ends mid-sample (odd byte count). */
  private carryByte: number | null = null

  constructor(sampleRate: number) {
    this.rate = sampleRate
    // Ask for the engine's own rate so no resampling happens between the chunk
    // and the speaker. Where the platform ignores the hint the buffer still
    // plays at the right pitch, because an AudioBuffer carries its own rate.
    this.ctx = new AudioContext({ sampleRate })
    this.gain = this.ctx.createGain()
    this.analyser = this.ctx.createAnalyser()
    this.analyser.fftSize = 256
    this.gain.connect(this.analyser)
    this.analyser.connect(this.ctx.destination)
    this.levelBuf = new Uint8Array(this.analyser.fftSize)
  }

  /**
   * Lower the reply while the user might be starting to talk. Not a stop: if
   * the sound turns out to be a cough, `unduck()` restores it mid-sentence.
   */
  duck(level = 0.18): void {
    if (this.closed || this.ducked) return
    this.ducked = true
    this.gain.gain.setTargetAtTime(level, this.ctx.currentTime, 0.06)
  }

  /** Bring the reply back up after a false alarm. */
  unduck(): void {
    if (this.closed || !this.ducked) return
    this.ducked = false
    this.gain.gain.setTargetAtTime(1, this.ctx.currentTime, 0.09)
  }

  /**
   * Chrome starts an AudioContext suspended unless it was created inside a
   * gesture. Read-aloud is normally a click, but auto read-aloud fires from a
   * stream callback, so the context has to be nudged awake explicitly.
   */
  async start(): Promise<void> {
    if (this.ctx.state === 'suspended') {
      try {
        await this.ctx.resume()
      } catch {
        /* stays suspended; playback will simply be silent */
      }
    }
  }

  push(pcm: Uint8Array): void {
    if (this.closed || pcm.length === 0) return
    const hasCarry = this.carryByte !== null ? 1 : 0
    const totalBytes = hasCarry + pcm.length
    const samples = totalBytes >> 1
    if (samples === 0) {
      // Single byte arrived with no prior carry; hold it for the next chunk.
      this.carryByte = pcm[0]
      return
    }

    const buffer = this.ctx.createBuffer(1, samples, this.rate)
    const out = buffer.getChannelData(0)
    let srcOffset = 0
    let outIdx = 0
    if (this.carryByte !== null) {
      let s = this.carryByte | (pcm[0] << 8)
      if (s >= 0x8000) s -= 0x10000
      out[0] = s / 32768
      this.carryByte = null
      srcOffset = 1
      outIdx = 1
    }
    const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength)
    for (; srcOffset + 1 < pcm.length; srcOffset += 2, outIdx += 1) {
      out[outIdx] = view.getInt16(srcOffset, true) / 32768
    }
    if (srcOffset < pcm.length) {
      this.carryByte = pcm[srcOffset]
    }

    const src = this.ctx.createBufferSource()
    src.buffer = buffer
    src.connect(this.gain)
    src.onended = () => {
      this.sources.delete(src)
      try {
        src.disconnect()
      } catch {
        /* already gone */
      }
    }
    this.sources.add(src)
    // Never schedule in the past: that drops the head of the buffer.
    const at = Math.max(this.ctx.currentTime + LEAD_S, this.cursor)
    src.start(at)
    this.cursor = at + buffer.duration
  }

  /** Current output level, 0..1, for the panel's waveform. */
  getLevel(): number {
    if (this.closed) return 0
    this.analyser.getByteTimeDomainData(this.levelBuf as Uint8Array<ArrayBuffer>)
    let sum = 0
    for (let i = 0; i < this.levelBuf.length; i++) {
      const v = (this.levelBuf[i] - 128) / 128
      sum += v * v
    }
    return Math.sqrt(sum / this.levelBuf.length)
  }

  /**
   * Cut playback immediately. A barge-in must silence the assistant at once;
   * waiting for the scheduled buffers to drain would talk over the user.
   */
  abort(): void {
    if (this.closed) return
    this.closed = true
    this.carryByte = null
    for (const src of this.sources) {
      try {
        src.onended = null
        src.stop()
      } catch {
        /* not started or already stopped */
      }
      try {
        src.disconnect()
      } catch {
        /* ignore */
      }
    }
    this.sources.clear()
    void this.ctx.close().catch(() => undefined)
  }

  /** Resolve once everything already scheduled has finished playing. */
  async drain(): Promise<void> {
    if (this.closed) return
    const left = this.cursor - this.ctx.currentTime
    if (left > 0) {
      await new Promise((resolve) => window.setTimeout(resolve, left * 1000))
    }
  }

  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    this.carryByte = null
    try {
      await this.ctx.close()
    } catch {
      /* already closed */
    }
  }
}

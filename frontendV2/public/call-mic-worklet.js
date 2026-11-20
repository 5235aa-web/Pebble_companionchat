/**
 * Microphone capture for the voice call.
 *
 * Runs on the audio thread and posts 512-sample Float32 frames (32 ms at
 * 16 kHz) back to the caller. Whatever rate the AudioContext actually runs at
 * is decimated to 16 kHz by keeping every Nth sample: ASR does not need
 * band-limited resampling, and a boxcar filter here would only add latency.
 */
const TARGET_RATE = 16000
const FRAME = 512

class CallMic extends AudioWorkletProcessor {
  constructor() {
    super()
    this.step = Math.max(1, sampleRate / TARGET_RATE)
    this.acc = 0
    this.sum = 0
    this.count = 0
    this.out = new Float32Array(FRAME)
    this.n = 0
    this.muted = false
    this.port.onmessage = (event) => {
      if (event.data === 'mute') this.muted = true
      else if (event.data === 'unmute') this.muted = false
    }
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0]
    if (!channel || channel.length === 0) return true
    if (this.muted) return true

    for (let i = 0; i < channel.length; i++) {
      // Boxcar-average across each decimation window to curb aliasing on 44.1/48 kHz mics.
      this.sum += channel[i]
      this.count += 1
      this.acc += 1
      if (this.acc < this.step) continue
      this.acc -= this.step
      this.out[this.n++] = this.sum / this.count
      this.sum = 0
      this.count = 0
      if (this.n === FRAME) {
        this.port.postMessage(this.out.slice())
        this.n = 0
      }
    }
    return true
  }
}

registerProcessor('call-mic', CallMic)

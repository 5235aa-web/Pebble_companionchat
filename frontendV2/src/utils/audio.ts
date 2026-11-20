/**
 * MediaRecorder hands back webm/opus; the STT endpoint wants PCM. Decode, mix
 * to mono, resample to 16 kHz and wrap a WAV header around the samples.
 */

const TARGET_RATE = 16000

export async function blobToWav(blob: Blob): Promise<Blob> {
  const bytes = await blob.arrayBuffer()
  const Ctx = window.AudioContext || (window as any).webkitAudioContext
  const ctx = new Ctx()
  try {
    const decoded = await ctx.decodeAudioData(bytes)
    const mono = mixToMono(decoded)
    const samples = resample(mono, decoded.sampleRate, TARGET_RATE)
    return encodeWav(samples, TARGET_RATE)
  } finally {
    void ctx.close()
  }
}

function mixToMono(buf: AudioBuffer): Float32Array {
  const ch = buf.numberOfChannels
  if (ch === 1) return buf.getChannelData(0).slice()
  const out = new Float32Array(buf.length)
  for (let c = 0; c < ch; c++) {
    const data = buf.getChannelData(c)
    for (let i = 0; i < data.length; i++) out[i] += data[i] / ch
  }
  return out
}

function resample(input: Float32Array, from: number, to: number): Float32Array {
  if (from === to) return input
  const ratio = from / to
  const length = Math.round(input.length / ratio)
  const out = new Float32Array(length)
  for (let i = 0; i < length; i++) {
    const pos = i * ratio
    const left = Math.floor(pos)
    const right = Math.min(left + 1, input.length - 1)
    const frac = pos - left
    out[i] = input[left] * (1 - frac) + input[right] * frac
  }
  return out
}

function encodeWav(samples: Float32Array, rate: number): Blob {
  const buffer = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buffer)
  const writeString = (offset: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i))
  }
  writeString(0, 'RIFF')
  view.setUint32(4, 36 + samples.length * 2, true)
  writeString(8, 'WAVE')
  writeString(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, rate, true)
  view.setUint32(28, rate * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  writeString(36, 'data')
  view.setUint32(40, samples.length * 2, true)
  let offset = 44
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true)
    offset += 2
  }
  return new Blob([view], { type: 'audio/wav' })
}

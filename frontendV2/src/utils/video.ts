import { blobToWav } from './audio'

export const VIDEO_EXTENSIONS = new Set([
  'mp4',
  'm4v',
  'webm',
  'mov',
  'mkv',
  'avi',
  'flv',
  'wmv',
  'ts',
])

export const MAX_VIDEO_ATTACH_BYTES = 100 * 1024 * 1024
const MAX_BROWSER_AUDIO_BYTES = 45 * 1024 * 1024
const MAX_EXTRACT_FRAMES = 180
const MAX_FRAME_DIM = 768

export interface ExtractedVideoFrame {
  time: number
  data_url: string
}

export interface ExtractedVideoPayload {
  frames: ExtractedVideoFrame[]
  duration: number
  width: number
  height: number
  wavBlob?: Blob
}

export function isVideoFile(file: Pick<File, 'name' | 'type'>): boolean {
  if ((file.type || '').startsWith('video/')) return true
  const ext = (file.name.split('.').pop() || '').toLowerCase()
  return VIDEO_EXTENSIONS.has(ext)
}

export function formatVideoDuration(seconds?: number): string {
  const total = Math.max(0, Math.round(Number(seconds || 0)))
  if (!total) return ''
  const s = total % 60
  const m = Math.floor(total / 60) % 60
  const h = Math.floor(total / 3600)
  if (h > 0) {
    return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  }
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function sampleTimestamps(duration: number, maxFrames = MAX_EXTRACT_FRAMES): number[] {
  if (!Number.isFinite(duration) || duration <= 0.4) return [0]
  let target: number
  if (duration <= 10.0) {
    target = Math.round(duration * 4.0)
  } else if (duration <= 80.0) {
    target = 40 + Math.round((duration - 10.0) * 2.0)
  } else {
    target = maxFrames
  }
  const count = Math.max(2, Math.min(maxFrames, target))
  const margin = Math.min(0.15, duration * 0.02)
  const start = margin
  const end = Math.max(start, duration - margin)
  if (count <= 1 || end <= start) return [Number(start.toFixed(2))]
  const step = (end - start) / (count - 1)
  return Array.from({ length: count }, (_, i) => Number((start + i * step).toFixed(2)))
}

function waitForEvent(
  target: EventTarget,
  okEvent: string,
  errEvent: string,
  timeoutMs: number,
): Promise<void> {
  return new Promise((resolve, reject) => {
    let timer = 0
    const cleanup = () => {
      window.clearTimeout(timer)
      target.removeEventListener(okEvent, onOk)
      target.removeEventListener(errEvent, onErr)
    }
    const onOk = () => {
      cleanup()
      resolve()
    }
    const onErr = () => {
      cleanup()
      reject(new Error(`video ${errEvent}`))
    }
    timer = window.setTimeout(() => {
      cleanup()
      reject(new Error(`video ${okEvent} timeout`))
    }, timeoutMs)
    target.addEventListener(okEvent, onOk, { once: true })
    target.addEventListener(errEvent, onErr, { once: true })
  })
}

async function extractFramesInBrowser(
  file: File,
): Promise<{ frames: ExtractedVideoFrame[]; duration: number; width: number; height: number }> {
  const url = URL.createObjectURL(file)
  const video = document.createElement('video')
  video.muted = true
  video.playsInline = true
  video.preload = 'auto'
  video.src = url

  try {
    await waitForEvent(video, 'loadeddata', 'error', 8000)
    const width = Number(video.videoWidth || 0)
    const height = Number(video.videoHeight || 0)
    const duration = Number.isFinite(video.duration) ? Number(video.duration) : 0
    if (!width || !height) {
      return { frames: [], duration: 0, width: 0, height: 0 }
    }

    const scale = Math.min(1, MAX_FRAME_DIM / Math.max(width, height))
    const cw = Math.max(1, Math.round(width * scale))
    const ch = Math.max(1, Math.round(height * scale))
    const canvas = document.createElement('canvas')
    canvas.width = cw
    canvas.height = ch
    const ctx = canvas.getContext('2d')
    if (!ctx) return { frames: [], duration, width, height }

    const timestamps = sampleTimestamps(duration)
    const frames: ExtractedVideoFrame[] = []
    for (const ts of timestamps) {
      try {
        if (Math.abs((video.currentTime || 0) - ts) > 0.04) {
          const seeked = waitForEvent(video, 'seeked', 'error', 2500)
          video.currentTime = ts
          await seeked
        }
        ctx.drawImage(video, 0, 0, cw, ch)
        const dataUrl = canvas.toDataURL('image/jpeg', 0.78)
        if (dataUrl.startsWith('data:image/')) {
          frames.push({ time: ts, data_url: dataUrl })
        }
      } catch {
        // Skip a single frame seek glitch; FFmpeg fallback covers full failure.
      }
    }

    return {
      frames,
      duration: Number(duration.toFixed(2)),
      width,
      height,
    }
  } catch {
    return { frames: [], duration: 0, width: 0, height: 0 }
  } finally {
    video.pause()
    video.removeAttribute('src')
    video.load()
    URL.revokeObjectURL(url)
  }
}

export async function extractVideoAudioWav(file: File): Promise<Blob | undefined> {
  if (file.size > MAX_BROWSER_AUDIO_BYTES) return undefined
  try {
    return await Promise.race<Blob | undefined>([
      blobToWav(file),
      new Promise<undefined>((resolve) => window.setTimeout(() => resolve(undefined), 15000)),
    ])
  } catch {
    // Silent video or unsupported browser audio codec: backend FFmpeg handles fallback.
    return undefined
  }
}

/**
 * Extract representative keyframes and optional 16 kHz mono WAV from a local video
 * in the browser. Failures return empty frames so the backend can fall back to FFmpeg.
 */
export async function extractVideoPayload(
  file: File,
  options: { includeAudio?: boolean } = {},
): Promise<ExtractedVideoPayload> {
  const [visual, wavBlob] = await Promise.all([
    extractFramesInBrowser(file),
    options.includeAudio ? extractAudioAudioSafely(file) : Promise.resolve(undefined),
  ])
  return {
    ...visual,
    wavBlob,
  }
}

function extractAudioAudioSafely(file: File): Promise<Blob | undefined> {
  return extractVideoAudioWav(file)
}

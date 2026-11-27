import { onUnmounted, ref } from 'vue'
import { blobToWav } from '@/utils/audio'

export interface UseVoiceRecorderOptions {
  onError?: (err: unknown) => void
  minBytes?: number
}

function formatRecTime(sec: number): string {
  const m = String(Math.floor(sec / 60)).padStart(2, '0')
  const s = String(sec % 60).padStart(2, '0')
  return `${m}:${s}`
}

/**
 * Shared one-shot microphone recorder for single and group chat composers.
 * Records browser audio via MediaRecorder and encodes it to 16 kHz mono WAV.
 */
export function useVoiceRecorder(opts: UseVoiceRecorderOptions = {}) {
  const recording = ref(false)
  const recTime = ref('00:00')
  let recTimer = 0
  let recSec = 0
  let recorder: MediaRecorder | null = null
  let recChunks: Blob[] = []
  let recStream: MediaStream | null = null

  function clearTimer() {
    if (recTimer) {
      clearInterval(recTimer)
      recTimer = 0
    }
    recSec = 0
    recTime.value = '00:00'
  }

  function releaseStream() {
    recStream?.getTracks().forEach((tr) => tr.stop())
    recStream = null
  }

  async function start(): Promise<boolean> {
    if (recording.value) return true
    try {
      recStream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (err) {
      opts.onError?.(err)
      return false
    }
    recChunks = []
    recorder = new MediaRecorder(recStream)
    recorder.ondataavailable = (ev) => {
      if (ev.data.size > 0) recChunks.push(ev.data)
    }
    recorder.start()
    recording.value = true
    recSec = 0
    recTime.value = '00:00'
    recTimer = window.setInterval(() => {
      recSec++
      recTime.value = formatRecTime(recSec)
    }, 1000)
    return true
  }

  function stop(): Promise<Blob | null> {
    recording.value = false
    clearTimer()
    const rec = recorder
    recorder = null
    if (!rec) {
      releaseStream()
      return Promise.resolve(null)
    }
    return new Promise((resolve) => {
      rec.onstop = async () => {
        releaseStream()
        const raw = new Blob(recChunks, { type: rec.mimeType || 'audio/webm' })
        recChunks = []
        if (raw.size < (opts.minBytes ?? 1200)) {
          resolve(null)
          return
        }
        try {
          resolve(await blobToWav(raw))
        } catch (err) {
          opts.onError?.(err)
          resolve(null)
        }
      }
      rec.stop()
    })
  }

  function cancel(): void {
    recording.value = false
    clearTimer()
    const rec = recorder
    recorder = null
    if (!rec) {
      releaseStream()
      recChunks = []
      return
    }
    rec.onstop = () => {
      releaseStream()
      recChunks = []
    }
    rec.stop()
  }

  onUnmounted(() => {
    clearTimer()
    if (recorder && recorder.state !== 'inactive') {
      recorder.onstop = () => {
        releaseStream()
        recChunks = []
      }
      recorder.stop()
      recorder = null
    } else {
      releaseStream()
    }
  })

  return {
    recording,
    recTime,
    start,
    stop,
    cancel,
  }
}

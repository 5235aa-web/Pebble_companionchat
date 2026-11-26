import { ref } from 'vue'

/**
 * Shared real-time speech output level (0..1) and formant vowel shape hints
 * (`wide` for I/E vowels, `round` for O/U vowels) fed by active TTS/call audio players.
 */
export interface SpeechSpectrum {
  level: number
  wide: number
  round: number
}

const liveSpeechLevel = ref(0)
let liveWide = 0
let liveRound = 0
let activeSourceCount = 0

export function setSpeechLevel(level: number): void {
  liveSpeechLevel.value = Math.max(0, Math.min(1, level))
  if (liveSpeechLevel.value <= 0.001) {
    liveWide = 0
    liveRound = 0
  }
}

export function setSpeechSpectrum(level: number, wide: number, round: number): void {
  liveSpeechLevel.value = Math.max(0, Math.min(1, level))
  liveWide = Math.max(0, Math.min(1, wide))
  liveRound = Math.max(0, Math.min(1, round))
}

export function beginSpeechSource(): () => void {
  activeSourceCount += 1
  return () => {
    activeSourceCount = Math.max(0, activeSourceCount - 1)
    if (activeSourceCount === 0) {
      liveSpeechLevel.value = 0
      liveWide = 0
      liveRound = 0
    }
  }
}

export function getSpeechLevel(): number {
  return liveSpeechLevel.value
}

export function getSpeechSpectrum(): SpeechSpectrum {
  return {
    level: liveSpeechLevel.value,
    wide: liveWide,
    round: liveRound,
  }
}

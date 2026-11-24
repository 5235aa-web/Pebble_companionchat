import { ref } from 'vue'

/**
 * Shared real-time speech output level (0..1) fed by active TTS/call audio
 * players so the 2D puppet's mouth follows live voice automatically.
 */
const liveSpeechLevel = ref(0)
let activeSourceCount = 0

export function setSpeechLevel(level: number): void {
  liveSpeechLevel.value = Math.max(0, Math.min(1, level))
}

export function beginSpeechSource(): () => void {
  activeSourceCount += 1
  return () => {
    activeSourceCount = Math.max(0, activeSourceCount - 1)
    if (activeSourceCount === 0) liveSpeechLevel.value = 0
  }
}

export function getSpeechLevel(): number {
  return liveSpeechLevel.value
}

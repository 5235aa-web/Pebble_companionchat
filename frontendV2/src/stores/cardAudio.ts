import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import type { CompanionAsset } from '@/api/companion'
import { fileUrl } from '@/api/client'
import { useSessionStore } from './session'
import { useCallStore } from './call'

/**
 * Independent playback store for CHARX background music (BGM) and sound
 * effects (SFX), kept separate from TTS speech and realtime voice calls.
 */
export const useCardAudioStore = defineStore('cardAudio', () => {
  const session = useSessionStore()
  const call = useCallStore()

  const bgmAssetId = ref<number | null>(null)
  const bgmName = ref('')
  const bgmPlaying = ref(false)
  const sfxAssetId = ref<number | null>(null)
  const sfxPlaying = ref(false)
  const volume = ref(0.55)
  const muted = ref(false)

  let bgmEl: HTMLAudioElement | null = null
  let sfxEl: HTMLAudioElement | null = null

  function stopBgm() {
    if (bgmEl) {
      bgmEl.pause()
      bgmEl.src = ''
      bgmEl = null
    }
    bgmPlaying.value = false
    bgmAssetId.value = null
    bgmName.value = ''
  }

  function stopSfx() {
    if (sfxEl) {
      sfxEl.pause()
      sfxEl.src = ''
      sfxEl = null
    }
    sfxPlaying.value = false
    sfxAssetId.value = null
  }

  function stopAll() {
    stopBgm()
    stopSfx()
  }

  async function toggleBgm(asset: CompanionAsset) {
    if (bgmAssetId.value === asset.id && bgmPlaying.value) {
      stopBgm()
      return
    }
    await playBgmAsset(asset)
  }

  async function playBgmAsset(asset: CompanionAsset) {
    if (bgmAssetId.value === asset.id && bgmPlaying.value) return
    stopBgm()
    const url = await fileUrl(asset.rel_path, 0)
    if (!url) return
    const audio = new Audio(url)
    audio.loop = true
    audio.volume = muted.value ? 0 : volume.value
    bgmEl = audio
    bgmAssetId.value = asset.id
    bgmName.value = asset.name || 'BGM'
    bgmPlaying.value = true
    try {
      await audio.play()
    } catch {
      stopBgm()
    }
  }

  async function playSfx(asset: CompanionAsset) {
    if (sfxAssetId.value === asset.id && sfxPlaying.value) {
      stopSfx()
      return
    }
    stopSfx()
    const url = await fileUrl(asset.rel_path, 0)
    if (!url) return
    const audio = new Audio(url)
    audio.loop = false
    audio.volume = muted.value ? 0 : Math.min(1, volume.value + 0.15)
    sfxEl = audio
    sfxAssetId.value = asset.id
    sfxPlaying.value = true
    audio.onended = () => {
      if (sfxEl === audio) stopSfx()
    }
    try {
      await audio.play()
    } catch {
      stopSfx()
    }
  }

  function setVolume(v: number) {
    volume.value = Math.max(0, Math.min(1, v))
    if (bgmEl) bgmEl.volume = muted.value ? 0 : volume.value
    if (sfxEl) sfxEl.volume = muted.value ? 0 : Math.min(1, volume.value + 0.15)
  }

  function toggleMute() {
    muted.value = !muted.value
    setVolume(volume.value)
  }

  watch(
    () => session.activeCompanionId,
    () => {
      stopAll()
    },
  )

  watch(
    () => call.active,
    (active) => {
      if (active) stopAll()
    },
  )

  return {
    bgmAssetId,
    bgmName,
    bgmPlaying,
    sfxAssetId,
    sfxPlaying,
    volume,
    muted,
    toggleBgm,
    playBgmAsset,
    playSfx,
    stopBgm,
    stopSfx,
    stopAll,
    setVolume,
    toggleMute,
  }
})

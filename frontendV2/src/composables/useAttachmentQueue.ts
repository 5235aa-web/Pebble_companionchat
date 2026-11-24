import { computed, type Ref } from 'vue'
import { fileUrl } from '@/api/client'
import {
  getAttachmentCapabilities,
  parseAttachment,
  transcribeVideoAttachment,
  type AttachmentParseStage,
  type PickedAttachment,
} from '@/api/gallery'
import { useUiStore } from '@/stores/ui'
import { useI18n } from '@/i18n'
import { isVideoFile, MAX_VIDEO_ATTACH_BYTES } from '@/utils/video'

export const VIDEO_ACCEPT =
  'video/*,.mp4,.m4v,.webm,.mov,.mkv,.avi,.flv,.wmv,.ts'
export const MEDIA_ACCEPT = `image/*,${VIDEO_ACCEPT}`

const MAX_ATTACH_FILES = 10
const MAX_ATTACH_BYTES = 50 * 1024 * 1024

let nextChipId = 1

export function allocateChipId(): number {
  return nextChipId++
}

export function revokeOwnedChipUrls(list: PickedAttachment[]): void {
  for (const c of list) {
    if (c.ownBlobUrl && c.src.startsWith('blob:')) {
      URL.revokeObjectURL(c.src)
    }
  }
}

export function useAttachmentQueue(
  chips: Ref<PickedAttachment[]>,
  options: { mediaOnly?: () => boolean } = {},
) {
  const ui = useUiStore()
  const { t } = useI18n()

  const rawFiles = new Map<number, File>()

  const uploading = computed(() =>
    chips.value.some((c) => Boolean(c.uploading) || Boolean(c.transcribing)),
  )

  function removeChip(id: number): void {
    rawFiles.delete(id)
    const removed = chips.value.filter((c) => c.id === id)
    revokeOwnedChipUrls(removed)
    chips.value = chips.value.filter((c) => c.id !== id)
  }

  function clearChips(): void {
    rawFiles.clear()
    revokeOwnedChipUrls(chips.value)
    chips.value = []
  }

  async function addFiles(files: File[]): Promise<void> {
    if (!files.length) return

    const mediaOnly = Boolean(options.mediaOnly?.())
    const accepted = mediaOnly
      ? files.filter((f) => f.type.startsWith('image/') || isVideoFile(f))
      : files

    if (!accepted.length) {
      if (mediaOnly) {
        ui.askConfirm({
          title: t('composer.attach'),
          text: t('composer.mediaOnlyHint'),
          onConfirm: () => {},
        })
      }
      return
    }

    if (chips.value.length + accepted.length > MAX_ATTACH_FILES) {
      ui.askConfirm({
        title: t('composer.attach'),
        text: t('composer.attachLimit', { n: MAX_ATTACH_FILES }),
        onConfirm: () => {},
      })
      return
    }

    const tooLarge = accepted.find(
      (f) => f.size > (isVideoFile(f) ? MAX_VIDEO_ATTACH_BYTES : MAX_ATTACH_BYTES),
    )
    if (tooLarge) {
      ui.askConfirm({
        title: t('composer.attach'),
        text: isVideoFile(tooLarge)
          ? t('composer.videoTooLarge', { name: tooLarge.name })
          : t('composer.attachTooLarge', { name: tooLarge.name }),
        onConfirm: () => {},
      })
      return
    }

    const caps = await getAttachmentCapabilities()

    for (const file of accepted) {
      const isImgGuess = file.type.startsWith('image/')
      const isVidGuess = isVideoFile(file)
      const localSrc = isImgGuess ? URL.createObjectURL(file) : ''
      const id = allocateChipId()
      const ext = (file.name.split('.').pop() || '').toUpperCase().slice(0, 4)

      if (isVidGuess) {
        rawFiles.set(id, file)
      }

      chips.value = [
        ...chips.value,
        {
          id,
          name: file.name,
          ext,
          src: localSrc,
          ownBlobUrl: Boolean(localSrc),
          uploading: true,
          stage: 'uploading',
          sttEnabled: isVidGuess ? caps.videoAutoStt : undefined,
          kind: isImgGuess ? 'photo' : isVidGuess ? 'video' : 'paper',
          info: {
            kind: isImgGuess ? 'image' : isVidGuess ? 'video' : 'document',
            name: file.name,
            size: file.size,
          },
        },
      ]

      try {
        const info = await parseAttachment(
          file,
          (stage: AttachmentParseStage) => {
            const item = chips.value.find((c) => c.id === id)
            if (item) item.stage = stage
          },
          { enableStt: caps.videoAutoStt },
        )

        // If the user removed this pending chip before parsing finished, discard it cleanly.
        const current = chips.value.find((c) => c.id === id)
        if (!current) {
          rawFiles.delete(id)
          if (localSrc) URL.revokeObjectURL(localSrc)
          continue
        }

        const isImage = info.kind === 'image' || isImgGuess
        const isVideo = info.kind === 'video' || isVidGuess

        let src = localSrc
        let ownBlobUrl = Boolean(localSrc)
        if (isVideo && info.poster_url) {
          try {
            src = await fileUrl(info.poster_url, 256)
            ownBlobUrl = false
          } catch {
            src = ''
            ownBlobUrl = false
          }
        } else if (isImage && !src && info.url) {
          try {
            src = await fileUrl(info.url, 256)
            ownBlobUrl = false
          } catch {
            src = ''
            ownBlobUrl = false
          }
        }

        current.kind = isImage ? 'photo' : isVideo ? 'video' : 'paper'
        current.src = src
        current.ownBlobUrl = ownBlobUrl
        current.info = info
        if (isVideo) {
          current.sttEnabled = caps.videoAutoStt
          current.cachedTranscript = info.transcript || ''
        }
        current.uploading = false
        current.stage = undefined
      } catch (err) {
        removeChip(id)
        ui.askConfirm({
          title: t('composer.attach'),
          text: err instanceof Error ? err.message : String(err),
          onConfirm: () => {},
        })
      }
    }
  }

  async function toggleVideoStt(id: number): Promise<void> {
    const chip = chips.value.find((c) => c.id === id)
    if (!chip || chip.kind !== 'video' || chip.uploading || chip.transcribing) return

    if (chip.sttEnabled) {
      chip.sttEnabled = false
      if (chip.info.transcript) {
        chip.cachedTranscript = chip.info.transcript
      }
      chip.info = { ...chip.info, transcript: '' }
      return
    }

    if (chip.cachedTranscript !== undefined && chip.cachedTranscript !== '') {
      chip.sttEnabled = true
      chip.info = { ...chip.info, transcript: chip.cachedTranscript }
      return
    }

    if (!chip.info.url) return
    chip.transcribing = true
    try {
      const text = await transcribeVideoAttachment(chip.info.url, rawFiles.get(id))
      const current = chips.value.find((c) => c.id === id)
      if (!current) return
      current.cachedTranscript = text
      current.sttEnabled = true
      current.info = { ...current.info, transcript: text }
    } catch (err) {
      ui.askConfirm({
        title: t('composer.attach'),
        text: err instanceof Error ? err.message : String(err),
        onConfirm: () => {},
      })
    } finally {
      const current = chips.value.find((c) => c.id === id)
      if (current) current.transcribing = false
    }
  }

  return {
    uploading,
    addFiles,
    removeChip,
    clearChips,
    toggleVideoStt,
  }
}

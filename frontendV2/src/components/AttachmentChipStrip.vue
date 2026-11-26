<script setup lang="ts">
import type { PickedAttachment } from '@/api/gallery'
import { useI18n } from '@/i18n'
import { formatVideoDuration } from '@/utils/video'

defineProps<{
  chips: PickedAttachment[]
}>()

const emit = defineEmits<{
  remove: [id: number]
  toggleStt: [id: number]
}>()

const { t } = useI18n()

function videoMetaLabel(c: PickedAttachment): string {
  if (c.uploading) {
    return c.stage === 'extracting' ? t('composer.videoExtracting') : t('composer.videoUploading')
  }
  const frames = c.info.frames?.length ?? 0
  return frames > 0 ? t('composer.videoFrames', { n: frames }) : c.ext || 'VID'
}

function sttToggleLabel(c: PickedAttachment): string {
  if (c.transcribing) return t('composer.videoSttRunning')
  if (c.sttEnabled) {
    return c.info.transcript?.trim() ? t('composer.videoSttOn') : t('composer.videoSttNone')
  }
  return t('composer.videoSttOff')
}
</script>

<template>
  <div v-if="chips.length" class="chips">
    <template v-for="c in chips" :key="c.id">
      <div v-if="c.kind === 'photo'" class="chip" :class="{ up: c.uploading }">
        <div class="chip-photo">
          <img v-if="c.src" :src="c.src" alt="" />
          <div v-else class="chip-video-blank">{{ c.ext || 'IMG' }}</div>
          <span class="cap">{{ c.name }}</span>
          <div v-if="c.uploading" class="chip-scan"></div>
        </div>
        <button class="chip-x" aria-label="remove attachment" @click="emit('remove', c.id)">
          <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>

      <div v-else-if="c.kind === 'video'" class="chip" :class="{ up: c.uploading }">
        <div class="chip-video">
          <div class="chip-video-thumb">
            <img v-if="c.src" :src="c.src" alt="" />
            <div v-else class="chip-video-empty">
              <svg viewBox="0 0 24 24">
                <rect x="3" y="5" width="18" height="14" rx="2" />
                <path d="M10 9l5 3-5 3z" />
              </svg>
            </div>
            <span class="chip-video-badge">
              ▶ {{ formatVideoDuration(c.info.duration) || c.ext || 'VID' }}
            </span>
            <div v-if="c.uploading || c.transcribing" class="chip-scan"></div>
          </div>
          <div class="chip-video-foot">
            <span class="chip-video-name" :data-tip="c.name || undefined" data-tip-float>{{ c.name }}</span>
            <div class="chip-video-bar">
              <span class="chip-video-meta">{{ videoMetaLabel(c) }}</span>
              <button
                v-if="!c.uploading"
                type="button"
                class="chip-video-stt"
                :class="{ on: c.sttEnabled, busy: c.transcribing }"
                :disabled="c.transcribing"
                :data-tip="t('composer.videoSttTip')"
                data-tip-float
                @click.stop="emit('toggleStt', c.id)"
              >
                {{ sttToggleLabel(c) }}
              </button>
            </div>
          </div>
        </div>
        <button class="chip-x" aria-label="remove attachment" @click="emit('remove', c.id)">
          <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>

      <div v-else class="chip" :class="{ up: c.uploading }">
        <div class="chip-paper">
          <span class="ext">{{ c.ext }}</span>
          <span class="nm">{{ c.uploading ? t('composer.videoUploading') : c.name }}</span>
          <div v-if="c.uploading" class="chip-scan"></div>
        </div>
        <button class="chip-x" aria-label="remove attachment" @click="emit('remove', c.id)">
          <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>
    </template>
  </div>
</template>

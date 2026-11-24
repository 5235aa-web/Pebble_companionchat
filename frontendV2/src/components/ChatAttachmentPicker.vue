<script setup lang="ts">
/**
 * The attachment tray the group composer uses.
 *
 * It renders the same chips, under the same stored classes, that the single
 * thread has always used, so a picture or video sent into a room looks
 * exactly like one sent to a companion.
 */
import { computed, ref } from 'vue'
import type { PickedAttachment } from '@/api/gallery'
import { MEDIA_ACCEPT, useAttachmentQueue } from '@/composables/useAttachmentQueue'
import AttachmentChipStrip from './AttachmentChipStrip.vue'

const props = defineProps<{
  modelValue: PickedAttachment[]
  /** Rooms carry images and videos; documents have no reader there. */
  imageOnly?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [PickedAttachment[]]
}>()

const chipsModel = computed({
  get: () => props.modelValue,
  set: (v: PickedAttachment[]) => emit('update:modelValue', v),
})

const { uploading, addFiles, removeChip, toggleVideoStt } = useAttachmentQueue(chipsModel, {
  mediaOnly: () => Boolean(props.imageOnly),
})

const input = ref<HTMLInputElement | null>(null)

function pick(): void {
  input.value?.click()
}

async function onPicked(e: Event): Promise<void> {
  const el = e.target as HTMLInputElement
  const files = Array.from(el.files ?? [])
  el.value = ''
  if (files.length) await addFiles(files)
}

defineExpose({ pick, addFiles, uploading })
</script>

<template>
  <AttachmentChipStrip :chips="modelValue" @remove="removeChip" @toggle-stt="toggleVideoStt" />
  <input
    ref="input"
    type="file"
    :accept="imageOnly ? MEDIA_ACCEPT : undefined"
    multiple
    hidden
    @change="onPicked"
  />
</template>

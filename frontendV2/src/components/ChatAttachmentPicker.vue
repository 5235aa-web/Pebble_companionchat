<script setup lang="ts">
/**
 * The attachment tray the group composer uses.
 *
 * It renders the same chips, under the same stored classes, that the single
 * thread has always used, so a picture sent into a room looks exactly like one
 * sent to a companion. Ownership stays with the caller: this component only
 * uploads, lists and removes; sending is the parent's job.
 */
import { ref } from 'vue'
import { useUiStore } from '@/stores/ui'
import { useI18n } from '@/i18n'
import { parseAttachment, type PickedAttachment } from '@/api/gallery'

const props = defineProps<{
  modelValue: PickedAttachment[]
  /** Rooms only carry pictures; a document would reach no reader there. */
  imageOnly?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [PickedAttachment[]]
}>()

const ui = useUiStore()
const { t } = useI18n()

const MAX_FILES = 10
const MAX_BYTES = 50 * 1024 * 1024

const input = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
let seq = 1

function pick(): void {
  input.value?.click()
}

async function onPicked(e: Event): Promise<void> {
  const el = e.target as HTMLInputElement
  const files = Array.from(el.files ?? [])
  el.value = ''
  if (!files.length) return
  if (files.length > MAX_FILES) {
    ui.askConfirm({
      title: t('composer.attach'),
      text: t('composer.attachLimit', { n: MAX_FILES }),
      onConfirm: () => {},
    })
    return
  }
  const tooLarge = files.find((f) => f.size > MAX_BYTES)
  if (tooLarge) {
    ui.askConfirm({
      title: t('composer.attach'),
      text: t('composer.attachTooLarge', { name: tooLarge.name }),
      onConfirm: () => {},
    })
    return
  }
  uploading.value = true
  try {
    const added: PickedAttachment[] = []
    for (const file of files) {
      const isImage = file.type.startsWith('image/')
      if (props.imageOnly && !isImage) continue
      const info = await parseAttachment(file)
      added.push({
        id: seq++,
        name: file.name,
        ext: (file.name.split('.').pop() || '').toUpperCase().slice(0, 4),
        src: isImage ? URL.createObjectURL(file) : '',
        kind: isImage ? 'photo' : 'paper',
        info,
      })
    }
    if (added.length) emit('update:modelValue', [...props.modelValue, ...added])
  } catch (err) {
    ui.askConfirm({
      title: t('composer.attach'),
      text: err instanceof Error ? err.message : String(err),
      onConfirm: () => {},
    })
  } finally {
    uploading.value = false
  }
}

function remove(id: number): void {
  emit(
    'update:modelValue',
    props.modelValue.filter((c) => c.id !== id),
  )
}

defineExpose({ pick })
</script>

<template>
  <div v-if="modelValue.length" class="chips">
    <template v-for="c in modelValue" :key="c.id">
      <div v-if="c.kind === 'photo'" class="chip">
        <div class="chip-photo">
          <img :src="c.src" alt="" />
          <span class="cap">{{ c.name }}</span>
        </div>
        <button class="chip-x" aria-label="remove attachment" @click="remove(c.id)">
          <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>
      <div v-else class="chip">
        <div class="chip-paper">
          <span class="ext">{{ c.ext }}</span>
          <span class="nm">{{ c.name }}</span>
        </div>
        <button class="chip-x" aria-label="remove attachment" @click="remove(c.id)">
          <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>
    </template>
  </div>
  <input
    ref="input"
    type="file"
    :accept="imageOnly ? 'image/*' : undefined"
    multiple
    hidden
    @change="onPicked"
  />
</template>

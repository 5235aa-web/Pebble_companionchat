<script setup lang="ts">
import { computed, ref } from 'vue'
import type { PuppetAnchors } from '@/api/inochi2d'
import { useInochi2dStore } from '@/stores/inochi2d'
import { useI18n } from '@/i18n'
import UiSelect, { type UiOption } from './ui/UiSelect.vue'

const props = defineProps<{
  scaleX: number
  scaleY: number
}>()

const inochi = useInochi2dStore()
const { t, locale } = useI18n()

type AnchorKey =
  | 'headPivot'
  | 'eyeL'
  | 'eyeR'
  | 'mouthCenter'
  | 'chestCenter'
  | 'shoulderL'
  | 'shoulderR'
  | 'waistCenter'

const ANCHOR_META: { key: AnchorKey; color: string; zh: string; en: string }[] = [
  { key: 'headPivot', color: '#ff6b6b', zh: '头枢轴', en: 'Head' },
  { key: 'eyeL', color: '#4ecdc4', zh: '左眼', en: 'Eye L' },
  { key: 'eyeR', color: '#4ecdc4', zh: '右眼', en: 'Eye R' },
  { key: 'mouthCenter', color: '#ffe66d', zh: '嘴部', en: 'Mouth' },
  { key: 'chestCenter', color: '#ff9f43', zh: '胸腔', en: 'Chest' },
  { key: 'shoulderL', color: '#a29bfe', zh: '左肩', en: 'Sh L' },
  { key: 'shoulderR', color: '#a29bfe', zh: '右肩', en: 'Sh R' },
  { key: 'waistCenter', color: '#55efc4', zh: '腰枢轴', en: 'Waist' },
]

const rootRef = ref<HTMLElement | null>(null)
const draggingKey = ref<AnchorKey | null>(null)
const saving = ref(false)

const subjects = computed(() => inochi.puppet?.subjects ?? [])
const subjectOptions = computed<UiOption[]>(() =>
  subjects.value.map((sub, idx) => ({
    value: String(idx),
    label: sub.label || sub.id,
  })),
)
const activeSubject = computed(() => {
  const list = subjects.value
  if (!list.length) return null
  const idx = Math.max(0, Math.min(list.length - 1, inochi.rigSubjectIndex))
  return list[idx]
})

const currentAnchors = computed<PuppetAnchors | null>(() => {
  if (activeSubject.value) return activeSubject.value.anchors
  return inochi.puppet?.anchors ?? null
})

function anchorPos(key: AnchorKey): [number, number] | null {
  const a = currentAnchors.value
  if (!a) return null
  const raw = a[key]
  if (Array.isArray(raw) && raw.length === 2) return [raw[0], raw[1]]
  if (key === 'shoulderL') return [Math.min(0.92, a.chestCenter[0] + 0.14), Math.min(0.85, a.headPivot[1] + 0.08)]
  if (key === 'shoulderR') return [Math.max(0.08, a.chestCenter[0] - 0.14), Math.min(0.85, a.headPivot[1] + 0.08)]
  if (key === 'waistCenter') return [a.chestCenter[0], Math.min(0.95, a.chestCenter[1] + 0.24)]
  return null
}

function toStagePercent(cx: number, cy: number): { left: string; top: string } {
  const nx = (cx - 0.5) * 2 * props.scaleX
  const ny = (cy - 0.5) * 2 * props.scaleY
  return {
    left: `${(nx * 0.5 + 0.5) * 100}%`,
    top: `${(ny * 0.5 + 0.5) * 100}%`,
  }
}

function clientToChar(clientX: number, clientY: number): [number, number] | null {
  const el = rootRef.value
  if (!el) return null
  const rect = el.getBoundingClientRect()
  if (rect.width <= 0 || rect.height <= 0) return null
  const nx = ((clientX - rect.left) / rect.width) * 2 - 1
  const ny = ((clientY - rect.top) / rect.height) * 2 - 1
  const cx = Math.max(0.02, Math.min(0.98, 0.5 + nx / (2 * Math.max(0.1, props.scaleX))))
  const cy = Math.max(0.02, Math.min(0.98, 0.5 + ny / (2 * Math.max(0.1, props.scaleY))))
  return [Number(cx.toFixed(4)), Number(cy.toFixed(4))]
}

function startDrag(key: AnchorKey, e: PointerEvent): void {
  e.stopPropagation()
  e.preventDefault()
  draggingKey.value = key

  const onMove = (ev: PointerEvent) => {
    const pt = clientToChar(ev.clientX, ev.clientY)
    if (!pt || !currentAnchors.value) return
    const nextAnchors: PuppetAnchors = { ...currentAnchors.value, [key]: pt }
    if (activeSubject.value && subjects.value.length) {
      const nextSubs = subjects.value.map((s) =>
        s.id === activeSubject.value?.id ? { ...s, anchors: nextAnchors } : s,
      )
      inochi.previewRigPatch(
        inochi.rigSubjectIndex === 0 ? nextAnchors : undefined,
        nextSubs,
      )
    } else {
      inochi.previewRigPatch(nextAnchors)
    }
  }

  const onUp = () => {
    draggingKey.value = null
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
  }

  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
}

async function saveRig(): Promise<void> {
  saving.value = true
  try {
    await inochi.commitRig()
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div ref="rootRef" class="puppet-rig-overlay">
    <div class="puppet-rig-bar" @pointerdown.stop>
      <span class="puppet-rig-badge">{{ t('live2d.rigModeTitle') }}</span>
      <div v-if="subjects.length > 1" class="puppet-rig-sel">
        <UiSelect
          :model-value="String(inochi.rigSubjectIndex)"
          :options="subjectOptions"
          @update:model-value="inochi.rigSubjectIndex = Number($event) || 0"
        />
      </div>
      <button class="mini-btn" type="button" :disabled="saving" @click="saveRig">
        {{ saving ? '...' : t('live2d.rigSave') }}
      </button>
      <button class="mini-btn" type="button" @click="inochi.rigEditMode = false">
        {{ t('live2d.rigDone') }}
      </button>
    </div>

    <template v-for="item in ANCHOR_META" :key="item.key">
      <button
        v-if="anchorPos(item.key)"
        type="button"
        class="puppet-rig-handle"
        :class="{ dragging: draggingKey === item.key }"
        :style="{
          ...toStagePercent(anchorPos(item.key)![0], anchorPos(item.key)![1]),
          '--rig-color': item.color,
        }"
        @pointerdown="startDrag(item.key, $event)"
      >
        <span class="puppet-rig-dot"></span>
        <span class="puppet-rig-lbl">{{ locale === 'en' ? item.en : item.zh }}</span>
      </button>
    </template>
  </div>
</template>

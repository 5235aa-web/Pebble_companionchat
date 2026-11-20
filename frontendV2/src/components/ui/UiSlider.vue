<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{
  label: string
  modelValue: number
  min?: number
  max: number
  step?: number
}>()

const emit = defineEmits<{ 'update:modelValue': [number] }>()

const track = ref<HTMLElement | null>(null)

const ratio = computed(() => {
  const min = props.min ?? 0
  const span = props.max - min || 1
  return Math.min(1, Math.max(0, (props.modelValue - min) / span))
})

const display = computed(() => {
  if (props.max > 100) return String(Math.round(props.modelValue))
  return props.modelValue.toFixed(2)
})

function apply(clientX: number) {
  const el = track.value
  if (!el) return
  const r = el.getBoundingClientRect()
  const raw = (clientX - r.left) / r.width
  const clamped = Math.min(1, Math.max(0, raw))
  const min = props.min ?? 0
  let next = min + clamped * (props.max - min)
  if (props.step) next = Math.round(next / props.step) * props.step
  emit('update:modelValue', next)
}

function onDown(e: MouseEvent) {
  e.preventDefault()
  apply(e.clientX)
  const move = (ev: MouseEvent) => apply(ev.clientX)
  const up = () => {
    document.removeEventListener('mousemove', move)
    document.removeEventListener('mouseup', up)
  }
  document.addEventListener('mousemove', move)
  document.addEventListener('mouseup', up)
}
</script>

<template>
  <div class="sl">
    <span class="sl-name">{{ label }}</span>
    <div ref="track" class="sl-track" :style="{ '--v': ratio }" @mousedown="onDown">
      <span class="sl-fill"></span>
      <span class="sl-knob"></span>
    </div>
    <span class="sl-val">{{ display }}</span>
  </div>
</template>

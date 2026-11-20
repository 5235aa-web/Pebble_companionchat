<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

/**
 * The panel's dropdown.
 *
 * A native `<select>` is not an option here: on Windows the popup list is drawn
 * by the OS, so `option { background }` and the hover row are ignored — it shows
 * a white list with a system-blue highlight no matter what the stylesheet says.
 * This component draws both the field and the list, so the theme applies to all
 * of it.
 *
 * The list is teleported to `<body>` and positioned from the trigger's rect:
 * the backstage body scrolls, and an absolutely positioned popup would be
 * clipped by that container.
 */

export type UiOption = { value: string; label: string }

const props = withDefaults(
  defineProps<{
    modelValue: string
    options: UiOption[]
    placeholder?: string
    disabled?: boolean
  }>(),
  { placeholder: '', disabled: false },
)

const emit = defineEmits<{ (e: 'update:modelValue', value: string): void }>()

const open = ref(false)
const active = ref(-1)
const root = ref<HTMLElement | null>(null)
const button = ref<HTMLButtonElement | null>(null)
const list = ref<HTMLElement | null>(null)
const pos = ref({ left: 0, top: 0, bottom: 0, width: 0, maxHeight: 264, up: false })

const current = computed(
  () => props.options.find((o) => o.value === props.modelValue)?.label ?? props.placeholder,
)

function place() {
  const rect = button.value?.getBoundingClientRect()
  if (!rect) return
  const gap = 4
  const margin = 8
  const spaceBelow = Math.max(0, window.innerHeight - rect.bottom - gap - margin)
  const spaceAbove = Math.max(0, rect.top - gap - margin)
  const up = spaceBelow < 180 && spaceAbove > spaceBelow
  const maxHeight = Math.min(264, Math.max(96, up ? spaceAbove : spaceBelow))
  const width = Math.min(rect.width, window.innerWidth - margin * 2)
  const left = Math.min(Math.max(margin, rect.left), window.innerWidth - width - margin)
  pos.value = {
    left,
    top: rect.bottom + gap,
    bottom: window.innerHeight - rect.top + gap,
    width,
    maxHeight,
    up,
  }
}

async function toggle() {
  if (props.disabled) return
  if (open.value) {
    open.value = false
    return
  }
  place()
  active.value = props.options.findIndex((o) => o.value === props.modelValue)
  open.value = true
  await nextTick()
  scrollActive()
}

function close() {
  open.value = false
}

function pick(option: UiOption) {
  emit('update:modelValue', option.value)
  close()
}

function move(step: number) {
  const total = props.options.length
  if (!total) return
  active.value = (active.value + step + total) % total
  void nextTick(scrollActive)
}

function scrollActive() {
  const el = list.value?.querySelector<HTMLElement>(`[data-index="${active.value}"]`)
  el?.scrollIntoView({ block: 'nearest' })
}

function onKey(event: KeyboardEvent) {
  if (!open.value) {
    if (event.key === 'Enter' || event.key === ' ' || event.key === 'ArrowDown') {
      event.preventDefault()
      void toggle()
    }
    return
  }
  if (event.key === 'Escape') {
    event.preventDefault()
    close()
    button.value?.focus()
  } else if (event.key === 'ArrowDown') {
    event.preventDefault()
    move(1)
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    move(-1)
  } else if (event.key === 'Enter') {
    event.preventDefault()
    const option = props.options[active.value]
    if (option) pick(option)
  } else if (event.key === 'Home') {
    event.preventDefault()
    active.value = 0
    void nextTick(scrollActive)
  } else if (event.key === 'End') {
    event.preventDefault()
    active.value = props.options.length - 1
    void nextTick(scrollActive)
  }
}

function onPointerDown(event: PointerEvent) {
  const target = event.target as Node
  if (root.value?.contains(target) || list.value?.contains(target)) return
  close()
}

/** The popup is fixed-positioned, so scrolling the panel would leave it behind,
 *  but scrolling inside the popup list itself must stay open. */
function onScroll(event: Event) {
  if (!open.value) return
  const target = event.target
  if (target instanceof Node && list.value?.contains(target)) return
  close()
}

function onResize() {
  if (open.value) close()
}

onMounted(() => {
  document.addEventListener('pointerdown', onPointerDown, true)
  window.addEventListener('scroll', onScroll, true)
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onPointerDown, true)
  window.removeEventListener('scroll', onScroll, true)
  window.removeEventListener('resize', onResize)
})

watch(
  () => props.options,
  () => {
    if (open.value) active.value = -1
  },
)
</script>

<template>
  <div ref="root" class="ui-select" :class="{ 'is-open': open, 'is-disabled': disabled }">
    <button
      ref="button"
      type="button"
      class="ui-select-btn"
      :disabled="disabled"
      :aria-expanded="open"
      aria-haspopup="listbox"
      @click="toggle"
      @keydown="onKey"
    >
      <span class="ui-select-value">{{ current }}</span>
      <span class="ui-select-chevron" aria-hidden="true"></span>
    </button>

    <Teleport to="body">
      <div
        v-if="open"
        ref="list"
        class="ui-select-list"
        role="listbox"
        :style="{
          left: pos.left + 'px',
          top: pos.up ? 'auto' : pos.top + 'px',
          bottom: pos.up ? pos.bottom + 'px' : 'auto',
          width: pos.width + 'px',
          maxHeight: pos.maxHeight + 'px',
        }"
      >
        <button
          v-for="(option, index) in options"
          :key="option.value"
          type="button"
          class="ui-select-item"
          :class="{ 'is-on': option.value === modelValue, 'is-active': index === active }"
          :data-index="index"
          role="option"
          :aria-selected="option.value === modelValue"
          @mouseenter="active = index"
          @click="pick(option)"
        >
          {{ option.label }}
        </button>
        <p v-if="!options.length" class="ui-select-empty">{{ placeholder }}</p>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.ui-select {
  position: relative;
  width: 100%;
  min-width: 0;
}

.ui-select-btn {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 5px 0 9px;
  background: none;
  border: none;
  border-bottom: 1px solid var(--line-2);
  color: var(--ink-2);
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.06em;
  text-align: left;
  cursor: pointer;
  transition:
    border-color 0.3s ease,
    color 0.3s ease;
}

.ui-select-btn:hover {
  border-bottom-color: rgb(var(--accent-rgb) / 0.5);
  color: var(--ink);
}

.ui-select.is-open .ui-select-btn {
  border-bottom-color: rgb(var(--accent-rgb) / 0.6);
  color: var(--ink);
}

.ui-select.is-disabled .ui-select-btn {
  cursor: default;
  opacity: 0.5;
}

.ui-select-value {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ui-select-chevron {
  flex: none;
  width: 5px;
  height: 5px;
  margin-right: 3px;
  border-right: 1px solid var(--ink-3);
  border-bottom: 1px solid var(--ink-3);
  transform: rotate(45deg) translate(-1px, -1px);
  transition:
    border-color 0.3s ease,
    transform 0.3s ease;
}

.ui-select-btn:hover .ui-select-chevron,
.ui-select.is-open .ui-select-chevron {
  border-color: var(--accent);
}

.ui-select.is-open .ui-select-chevron {
  transform: rotate(225deg) translate(-1px, -1px);
}

.ui-select-list {
  position: fixed;
  z-index: 6000;
  max-height: 264px;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 4px 0;
  background: var(--bg);
  border: 1px solid var(--line-2);
  box-shadow: 0 18px 40px rgb(var(--shadow-rgb) / 0.45);
}

.ui-select-item {
  display: block;
  width: 100%;
  padding: 7px 11px;
  background: none;
  border: none;
  color: var(--ink-2);
  font-family: var(--mono);
  font-size: 9.5px;
  letter-spacing: 0.05em;
  text-align: left;
  cursor: pointer;
  transition:
    background 0.18s ease,
    color 0.18s ease;
}

.ui-select-item.is-active {
  background: rgb(var(--accent-rgb) / 0.14);
  color: var(--ink);
}

.ui-select-item.is-on {
  color: var(--accent);
}

.ui-select-item.is-on.is-active {
  background: rgb(var(--accent-rgb) / 0.22);
}

.ui-select-empty {
  padding: 8px 11px;
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.06em;
  color: var(--ink-3);
}
</style>

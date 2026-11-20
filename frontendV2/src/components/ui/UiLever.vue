<script setup lang="ts">
defineProps<{ modelValue: boolean; label: string }>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()
</script>

<template>
  <button class="lever" :class="{ on: modelValue }" @click="emit('update:modelValue', !modelValue)">
    <i></i><span>{{ label }}</span>
  </button>
</template>

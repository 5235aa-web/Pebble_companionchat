<script setup lang="ts">
import { useUiStore } from '@/stores/ui'
import { useI18n } from '@/i18n'

const ui = useUiStore()
const { t } = useI18n()
</script>

<template>
  <div class="drop-ov" :class="{ show: ui.dropActive }">
    <div class="drop-card">
      <svg viewBox="0 0 24 24">
        <path d="M12 16V4M7 9l5-5 5 5" />
        <path d="M4 15v4a1 1 0 001 1h14a1 1 0 001-1v-4" />
      </svg>
      <div class="drop-title">{{ t('drop.title') }}</div>
      <div class="drop-hint">{{ t('drop.hint') }}</div>
    </div>
  </div>
</template>

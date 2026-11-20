<script setup lang="ts">
import { useUiStore } from '@/stores/ui'
import { useI18n } from '@/i18n'

const ui = useUiStore()
const { t } = useI18n()
</script>

<template>
  <div class="dialog-ov" :class="{ show: ui.isOpen('dialog') }" @click.self="ui.closeDialog()">
    <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlgTitle">
      <div class="dlg-kicker">{{ t('dialog.confirm') }}</div>
      <div id="dlgTitle" class="dlg-title">{{ ui.dialogTitle }}</div>
      <p class="dlg-text">{{ ui.dialogText }}</p>
      <div class="dlg-actions">
        <button v-if="!ui.dialogAlert" class="mini-btn" @click="ui.closeDialog()">{{ t('dialog.cancel') }}</button>
        <button
          v-if="!ui.dialogAlert && ui.dialogSecondaryLabel"
          class="mini-btn"
          @click="ui.runSecondary()"
        >
          {{ ui.dialogSecondaryLabel }}
        </button>
        <button
          class="mini-btn"
          :class="ui.dialogDanger ? 'danger' : 'primary'"
          @click="ui.confirmDialog()"
        >
          {{ ui.dialogAlert ? t('dialog.close') : (ui.dialogConfirmLabel || t('dialog.delete')) }}
        </button>
      </div>
    </div>
  </div>
</template>

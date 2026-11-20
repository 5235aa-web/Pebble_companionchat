<script setup lang="ts">
import { ref, watch } from 'vue'
import { useUiStore } from '@/stores/ui'
import { useI18n } from '@/i18n'
import { getGlobalPrompts, saveGlobalPrompts } from '@/api/settings'

const ui = useUiStore()
const { t } = useI18n()

const advanced = ref('')
const system = ref('')
const postHistory = ref('')
const busy = ref(false)
const notice = ref('')

async function load() {
  try {
    const r = await getGlobalPrompts()
    advanced.value = r.advanced || ''
    system.value = r.system || ''
    postHistory.value = r.postHistory || ''
    notice.value = ''
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e)
  }
}

watch(
  () => ui.isOpen('rules'),
  (open) => {
    if (open) void load()
  },
  { immediate: true },
)

async function save() {
  if (busy.value) return
  busy.value = true
  notice.value = ''
  try {
    await saveGlobalPrompts({
      advanced: advanced.value,
      system: system.value,
      postHistory: postHistory.value,
    })
    notice.value = t('rules.saved')
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

function clearAll() {
  ui.askConfirm({
    title: t('rules.clearTitle'),
    text: t('rules.clearConfirm'),
    confirmLabel: t('rules.clear'),
    danger: true,
    onConfirm: () => {
      advanced.value = ''
      system.value = ''
      postHistory.value = ''
      void save()
    },
  })
}
</script>

<template>
  <aside class="panel" :class="{ show: ui.isOpen('rules') }">
    <div class="d-head">
      <span class="d-kicker">{{ t('rules.kicker') }}</span>
      <span class="d-title">{{ t('rules.title') }}</span>
      <button class="d-close" @click="ui.closePanel('rules')">
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>

    <div class="d-body">
      <p class="rl-intro">{{ t('rules.intro') }}</p>

      <div class="pf-field">
        <label>{{ t('rules.advanced') }}</label>
        <p class="rl-hint">{{ t('rules.advancedHint') }}</p>
        <textarea v-model="advanced" class="pf-area" rows="4" spellcheck="false" />
      </div>

      <div class="pf-field">
        <label>{{ t('rules.system') }}</label>
        <p class="rl-hint">{{ t('rules.systemHint') }}</p>
        <textarea v-model="system" class="pf-area" rows="4" spellcheck="false" />
      </div>

      <div class="pf-field">
        <label>{{ t('rules.postHistory') }}</label>
        <p class="rl-hint">{{ t('rules.postHistoryHint') }}</p>
        <textarea v-model="postHistory" class="pf-area" rows="4" spellcheck="false" />
      </div>
    </div>

    <div class="d-foot">
      <button class="mini-btn primary" :disabled="busy" @click="save">
        {{ t('rules.save') }}
      </button>
      <button class="mini-btn danger" @click="clearAll">{{ t('rules.clear') }}</button>
      <span v-if="notice" class="rl-notice">{{ notice }}</span>
    </div>
  </aside>
</template>

<style scoped>
.rl-intro {
  margin: 0 0 16px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--ink-4);
}
.rl-hint {
  margin: 0 0 6px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--ink-4);
}
.rl-notice {
  margin-left: auto;
  align-self: center;
  font-family: var(--mono);
  font-size: 10px;
  color: var(--accent);
}
</style>

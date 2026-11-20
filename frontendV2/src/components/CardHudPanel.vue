<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { useCardHudStore } from '@/stores/cardHud'
import { useCardAudioStore } from '@/stores/cardAudio'
import { useI18n } from '@/i18n'

const cardHud = useCardHudStore()
const cardAudio = useCardAudioStore()
const { t } = useI18n()

const editingKey = ref('')
const editDraft = ref('')
const editInput = ref<HTMLInputElement | null>(null)

const affectionPct = computed(() => {
  const v = cardHud.affectionValue
  if (v === null) return 0
  return Math.max(0, Math.min(100, v))
})

const hasAnyValue = computed(
  () =>
    Boolean(cardHud.moodText) ||
    cardHud.affectionValue !== null ||
    Boolean(cardHud.locationText) ||
    Boolean(cardHud.outfitText) ||
    cardHud.extraEntries.length > 0,
)

const pillSummary = computed(() => {
  const bits: string[] = []
  if (cardHud.moodText) bits.push(cardHud.moodText)
  if (cardHud.affectionValue !== null) bits.push(`♥ ${cardHud.affectionValue}`)
  if (!bits.length) bits.push(t('hud.title'))
  return bits.join(' · ')
})

async function startEdit(key: string, current: unknown) {
  editingKey.value = key
  editDraft.value = current === null || current === undefined ? '' : String(current)
  await nextTick()
  editInput.value?.focus()
  editInput.value?.select()
}

async function commitEdit() {
  const key = editingKey.value
  if (!key) return
  editingKey.value = ''
  let val: unknown = editDraft.value.trim()
  if (key === 'affection' && val !== '' && Number.isFinite(Number(val))) {
    val = Number(val)
  }
  await cardHud.saveHudFields({ [key]: val })
}

function cancelEdit() {
  editingKey.value = ''
}
</script>

<template>
  <div class="chud" :class="{ open: cardHud.open, active: cardHud.effectiveEnabled }">
    <!-- Collapsed frosted pill -->
    <button
      v-if="!cardHud.open"
      type="button"
      class="chud-pill"
      @click="cardHud.open = true"
    >
      <span class="chud-dot" :class="{ on: cardHud.effectiveEnabled }"></span>
      <span class="chud-pill-text">{{ pillSummary }}</span>
      <span v-if="cardHud.affectionDelta !== null" class="chud-delta" :class="{ neg: cardHud.affectionDelta < 0 }">
        {{ cardHud.affectionDelta > 0 ? `+${cardHud.affectionDelta}` : cardHud.affectionDelta }}
      </span>
      <span v-if="cardAudio.bgmPlaying" class="chud-eq" aria-hidden="true">
        <i></i><i></i><i></i>
      </span>
      <svg class="chud-chev" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
    </button>

    <!-- Expanded frosted glass theatre HUD -->
    <div v-else class="chud-card">
      <div class="chud-shimmer" aria-hidden="true"></div>
      <header class="chud-head">
        <div class="chud-title-wrap">
          <span class="chud-dot" :class="{ on: cardHud.effectiveEnabled }"></span>
          <span class="chud-kicker">{{ t('hud.title') }}</span>
        </div>
        <div class="chud-head-actions">
          <button
            type="button"
            class="chud-switch"
            :class="{ on: cardHud.effectiveEnabled }"
            :title="t('hud.toggleTip')"
            @click="cardHud.setEnabled(!cardHud.effectiveEnabled)"
          >
            {{ cardHud.effectiveEnabled ? t('hud.on') : t('hud.off') }}
          </button>
          <button
            v-if="hasAnyValue"
            type="button"
            class="chud-icon-btn"
            :title="t('hud.resetTip')"
            @click="cardHud.reset()"
          >
            <svg viewBox="0 0 24 24"><path d="M4 12a8 8 0 102.4-5.7M4 4v4h4" /></svg>
          </button>
          <button
            type="button"
            class="chud-icon-btn"
            aria-label="collapse hud"
            @click="cardHud.open = false"
          >
            <svg viewBox="0 0 24 24"><path d="M6 15l6-6 6 6" /></svg>
          </button>
        </div>
      </header>

      <div class="chud-body">
        <p v-if="!cardHud.effectiveEnabled && !hasAnyValue" class="chud-hint">
          {{ t('hud.pausedHint') }}
        </p>

        <!-- Hero row: Mood & Affection -->
        <div class="chud-hero">
          <div class="chud-stat">
            <span class="chud-label">{{ t('hud.mood') }}</span>
            <input
              v-if="editingKey === 'mood'"
              ref="editInput"
              v-model="editDraft"
              class="chud-input"
              @keydown.enter="commitEdit"
              @keydown.esc="cancelEdit"
              @blur="commitEdit"
            />
            <button
              v-else
              type="button"
              class="chud-val chud-mood-val"
              @click="startEdit('mood', cardHud.moodText)"
            >
              {{ cardHud.moodText || t('hud.emptyValue') }}
            </button>
          </div>

          <div class="chud-stat chud-aff">
            <div class="chud-aff-top">
              <span class="chud-label">{{ t('hud.affection') }}</span>
              <span v-if="cardHud.affectionDelta !== null" class="chud-delta" :class="{ neg: cardHud.affectionDelta < 0 }">
                {{ cardHud.affectionDelta > 0 ? `+${cardHud.affectionDelta}` : cardHud.affectionDelta }}
              </span>
            </div>
            <input
              v-if="editingKey === 'affection'"
              ref="editInput"
              v-model="editDraft"
              class="chud-input"
              @keydown.enter="commitEdit"
              @keydown.esc="cancelEdit"
              @blur="commitEdit"
            />
            <button
              v-else
              type="button"
              class="chud-val chud-aff-num"
              @click="startEdit('affection', cardHud.affectionValue ?? 0)"
            >
              {{ cardHud.affectionValue !== null ? cardHud.affectionValue : t('hud.emptyValue') }}
            </button>
            <div class="chud-bar" aria-hidden="true">
              <i :style="{ width: `${affectionPct}%` }"></i>
            </div>
          </div>
        </div>

        <!-- Location & Outfit & Extra fields -->
        <div class="chud-rows">
          <div class="chud-row">
            <span class="chud-label">{{ t('hud.location') }}</span>
            <input
              v-if="editingKey === 'location'"
              ref="editInput"
              v-model="editDraft"
              class="chud-input"
              @keydown.enter="commitEdit"
              @keydown.esc="cancelEdit"
              @blur="commitEdit"
            />
            <button
              v-else
              type="button"
              class="chud-val"
              @click="startEdit('location', cardHud.locationText)"
            >
              {{ cardHud.locationText || t('hud.emptyValue') }}
            </button>
          </div>

          <div class="chud-row">
            <span class="chud-label">{{ t('hud.outfit') }}</span>
            <input
              v-if="editingKey === 'outfit'"
              ref="editInput"
              v-model="editDraft"
              class="chud-input"
              @keydown.enter="commitEdit"
              @keydown.esc="cancelEdit"
              @blur="commitEdit"
            />
            <button
              v-else
              type="button"
              class="chud-val"
              @click="startEdit('outfit', cardHud.outfitText)"
            >
              {{ cardHud.outfitText || t('hud.emptyValue') }}
            </button>
          </div>

          <div v-for="[k, v] in cardHud.extraEntries" :key="k" class="chud-row">
            <span class="chud-label">{{ k }}</span>
            <input
              v-if="editingKey === k"
              ref="editInput"
              v-model="editDraft"
              class="chud-input"
              @keydown.enter="commitEdit"
              @keydown.esc="cancelEdit"
              @blur="commitEdit"
            />
            <button v-else type="button" class="chud-val" @click="startEdit(k, v)">
              {{ String(v) }}
            </button>
          </div>
        </div>

        <!-- Stage expression & BGM chips -->
        <div v-if="cardHud.activeExpressionLabel || cardAudio.bgmPlaying" class="chud-media">
          <div v-if="cardHud.activeExpressionLabel" class="chud-chip">
            <span class="chud-chip-k">{{ t('hud.expression') }}</span>
            <span class="chud-chip-v">{{ cardHud.activeExpressionLabel }}</span>
            <button
              type="button"
              class="chud-chip-act"
              @click="cardHud.clearStageOverride()"
            >
              {{ t('hud.expressionReset') }}
            </button>
          </div>
          <div v-if="cardAudio.bgmPlaying" class="chud-chip">
            <span class="chud-chip-k">{{ t('hud.bgm') }}</span>
            <span class="chud-chip-v">♪ {{ cardAudio.bgmName }}</span>
            <button type="button" class="chud-chip-act" @click="cardAudio.stopBgm()">
              {{ t('card.assetStop') }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chud {
  position: absolute;
  top: 14px;
  left: 14px;
  z-index: 6;
  max-width: calc(100% - 28px);
  pointer-events: none;
}

.chud-pill,
.chud-card {
  pointer-events: auto;
  background: rgb(var(--bg-rgb) / 0.72);
  backdrop-filter: blur(18px) saturate(1.25);
  -webkit-backdrop-filter: blur(18px) saturate(1.25);
  border: 1px solid rgb(var(--accent-rgb) / 0.24);
  box-shadow: 0 12px 32px -12px rgb(var(--shadow-rgb) / 0.55);
}

.chud-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-radius: 999px;
  color: var(--ink-2);
  transition: border-color 0.25s ease, transform 0.25s var(--ease), background-color 0.25s ease;
}
.chud-pill:hover {
  border-color: rgb(var(--accent-rgb) / 0.55);
  transform: translateY(-1px);
}
.chud-pill-text {
  font-family: var(--serif);
  font-size: 12px;
  letter-spacing: 0.02em;
  max-width: 190px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chud-chev {
  width: 12px;
  height: 12px;
  stroke: var(--ink-4);
  fill: none;
  stroke-width: 1.8;
  flex: none;
}

.chud-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ink-4);
  flex: none;
  transition: background-color 0.3s ease, box-shadow 0.3s ease;
}
.chud-dot.on {
  background: var(--accent);
  box-shadow: 0 0 8px rgb(var(--accent-rgb) / 0.7);
}

.chud-eq {
  display: inline-flex;
  align-items: flex-end;
  gap: 2px;
  height: 10px;
}
.chud-eq i {
  display: block;
  width: 2px;
  background: var(--accent);
  border-radius: 1px;
  animation: chudEq 1s ease-in-out infinite alternate;
}
.chud-eq i:nth-child(2) {
  animation-delay: 0.25s;
}
.chud-eq i:nth-child(3) {
  animation-delay: 0.5s;
}
@keyframes chudEq {
  from { height: 3px; opacity: 0.55; }
  to { height: 10px; opacity: 1; }
}

.chud-card {
  position: relative;
  width: min(312px, calc(100vw - 40px));
  border-radius: 12px;
  overflow: hidden;
  animation: chudIn 0.22s var(--ease) both;
}
@keyframes chudIn {
  from { opacity: 0; transform: translateY(-4px) scale(0.985); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

.chud-shimmer {
  height: 1.5px;
  background: linear-gradient(90deg, var(--accent), rgb(var(--accent-rgb) / 0.15) 75%, transparent);
}

.chud-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 12px 8px;
  border-bottom: 1px solid var(--line);
}
.chud-title-wrap {
  display: flex;
  align-items: center;
  gap: 7px;
}
.chud-kicker {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--accent-dim);
}
.chud-head-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

.chud-switch {
  font-family: var(--mono);
  font-size: 8.5px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--line-2);
  color: var(--ink-4);
  transition: color 0.2s ease, border-color 0.2s ease, background-color 0.2s ease;
}
.chud-switch.on {
  color: var(--accent);
  border-color: rgb(var(--accent-rgb) / 0.55);
  background: rgb(var(--accent-rgb) / 0.1);
}

.chud-icon-btn {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  color: var(--ink-4);
  transition: color 0.2s ease, background-color 0.2s ease;
}
.chud-icon-btn:hover {
  color: var(--ink);
  background: rgb(var(--ink-rgb) / 0.06);
}
.chud-icon-btn svg {
  width: 13px;
  height: 13px;
  stroke: currentColor;
  fill: none;
  stroke-width: 1.8;
}

.chud-body {
  padding: 10px 12px 12px;
  max-height: 300px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.chud-hint {
  margin: 0;
  font-family: var(--serif);
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--ink-3);
}

.chud-hero {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.chud-stat {
  padding: 8px 10px;
  border-radius: 8px;
  background: rgb(var(--ink-rgb) / 0.035);
  border: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.chud-aff-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
}
.chud-label {
  font-family: var(--mono);
  font-size: 8px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ink-4);
}
.chud-val {
  text-align: left;
  font-family: var(--serif);
  font-size: 13px;
  line-height: 1.35;
  color: var(--ink);
  cursor: text;
  word-break: break-word;
}
.chud-mood-val {
  font-size: 13.5px;
  color: var(--accent);
}
.chud-aff-num {
  font-family: var(--mono);
  font-size: 14px;
  font-variant-numeric: tabular-nums;
}

.chud-bar {
  height: 3px;
  border-radius: 999px;
  background: rgb(var(--ink-rgb) / 0.1);
  overflow: hidden;
  margin-top: 2px;
}
.chud-bar i {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, rgb(var(--accent-rgb) / 0.65), var(--accent));
  transition: width 0.55s var(--ease);
}

.chud-delta {
  font-family: var(--mono);
  font-size: 9px;
  padding: 1px 5px;
  border-radius: 999px;
  color: var(--accent);
  background: rgb(var(--accent-rgb) / 0.14);
}
.chud-delta.neg {
  color: var(--ember);
  background: rgb(var(--ember-rgb) / 0.14);
}

.chud-rows {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.chud-row {
  display: grid;
  grid-template-columns: 62px 1fr;
  align-items: baseline;
  gap: 8px;
  padding: 3px 2px;
  border-bottom: 1px dashed var(--line);
}
.chud-row:last-child {
  border-bottom: none;
}

.chud-input {
  width: 100%;
  min-width: 0;
  background: rgb(var(--ink-rgb) / 0.06);
  border: 1px solid rgb(var(--accent-rgb) / 0.45);
  border-radius: 4px;
  padding: 2px 6px;
  color: var(--ink);
  font-family: var(--serif);
  font-size: 12.5px;
}
.chud-input:focus {
  outline: none;
}

.chud-media {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding-top: 4px;
  border-top: 1px solid var(--line);
}
.chud-chip {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 4px 8px;
  border-radius: 7px;
  background: rgb(var(--accent-rgb) / 0.07);
  border: 1px solid rgb(var(--accent-rgb) / 0.2);
}
.chud-chip-k {
  font-family: var(--mono);
  font-size: 8px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--accent-dim);
  flex: none;
}
.chud-chip-v {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--serif);
  font-size: 12px;
  color: var(--ink-2);
}
.chud-chip-act {
  font-family: var(--mono);
  font-size: 8.5px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink-4);
  flex: none;
}
.chud-chip-act:hover {
  color: var(--accent);
}
</style>

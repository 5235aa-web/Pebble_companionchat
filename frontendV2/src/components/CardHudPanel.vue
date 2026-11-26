<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { CompanionAsset } from '@/api/companion'
import { useCardHudStore } from '@/stores/cardHud'
import { useCardAudioStore } from '@/stores/cardAudio'
import { useSessionStore } from '@/stores/session'
import { useI18n } from '@/i18n'

const cardHud = useCardHudStore()
const cardAudio = useCardAudioStore()
const session = useSessionStore()
const { t } = useI18n()

const editingKey = ref('')
const editDraft = ref('')
const editInput = ref<HTMLInputElement | null>(null)

const addingField = ref(false)
const newFieldName = ref('')
const newFieldValue = ref('')
const newFieldInput = ref<HTMLInputElement | null>(null)

type MediaPickerKind = '' | 'expression' | 'bgm'
const openPicker = ref<MediaPickerKind>('')
const pickerQuery = ref('')
const thoughtRevealed = ref(false)

watch(
  () => cardHud.thoughtText,
  () => {
    thoughtRevealed.value = false
  },
)

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
    cardHud.extraEntries.length > 0 ||
    cardHud.pinnedVarEntries.length > 0,
)

const hasMediaAssets = computed(
  () => session.imageAssets.length > 0 || session.audioAssets.length > 0,
)

const pillSummary = computed(() => {
  const bits: string[] = []
  if (cardHud.moodText) bits.push(cardHud.moodText)
  if (cardHud.affectionValue !== null) bits.push(`♥ ${cardHud.affectionValue}`)
  if (!bits.length) bits.push(t('hud.title'))
  return bits.join(' · ')
})

const rawPickerAssets = computed<CompanionAsset[]>(() => {
  if (openPicker.value === 'expression') return cardHud.expressionCandidates
  if (openPicker.value === 'bgm') return session.audioAssets
  return []
})

const filteredPickerAssets = computed<CompanionAsset[]>(() => {
  const list = rawPickerAssets.value
  const q = pickerQuery.value.trim().toLowerCase()
  const matched = q ? list.filter((a) => a.name.toLowerCase().includes(q)) : list
  return matched.slice(0, 60)
})

const volumePct = computed(() => Math.round((cardAudio.muted ? 0 : cardAudio.volume) * 100))

function onVolumeInput(ev: Event) {
  const raw = Number((ev.target as HTMLInputElement).value)
  if (!Number.isFinite(raw)) return
  if (cardAudio.muted && raw > 0) cardAudio.toggleMute()
  cardAudio.setVolume(raw / 100)
}

function togglePicker(kind: Exclude<MediaPickerKind, ''>) {
  if (openPicker.value === kind) {
    openPicker.value = ''
    pickerQuery.value = ''
  } else {
    openPicker.value = kind
    pickerQuery.value = ''
  }
}

async function pickExpression(asset: CompanionAsset | null) {
  await cardHud.setManualExpression(asset)
  openPicker.value = ''
  pickerQuery.value = ''
}

async function pickBgm(asset: CompanionAsset | null) {
  if (!asset) {
    cardAudio.stopBgm()
  } else {
    await cardAudio.playBgmAsset(asset)
  }
  openPicker.value = ''
  pickerQuery.value = ''
}

async function openAddField() {
  addingField.value = true
  newFieldName.value = ''
  newFieldValue.value = ''
  await nextTick()
  newFieldInput.value?.focus()
}

async function submitAddField() {
  const key = newFieldName.value.trim()
  if (!key) return
  const val = newFieldValue.value.trim()
  addingField.value = false
  newFieldName.value = ''
  newFieldValue.value = ''
  await cardHud.saveHudFields({ [key]: val })
}

function cancelAddField() {
  addingField.value = false
  newFieldName.value = ''
  newFieldValue.value = ''
}

function isNumericStr(raw: string): boolean {
  const s = raw.trim()
  return Boolean(s) && /^-?\d+(?:\.\d+)?$/.test(s)
}

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
  if (key.startsWith('var:')) {
    const varName = key.slice(4)
    await cardHud.savePinnedVar(varName, editDraft.value.trim())
    return
  }
  let val: unknown = editDraft.value.trim()
  if (key === 'affection' && val !== '' && Number.isFinite(Number(val))) {
    val = Number(val)
  }
  await cardHud.saveHudFields({ [key]: val })
}

async function nudgePinnedVar(varKey: string, currentVal: string, delta: number) {
  const num = Number(currentVal.trim())
  if (!Number.isFinite(num)) return
  const next = Math.round((num + delta) * 100) / 100
  await cardHud.savePinnedVar(varKey, String(next))
}

function cancelEdit() {
  editingKey.value = ''
}
</script>

<template>
  <div
    class="chud"
    :class="{ open: cardHud.open, active: cardHud.effectiveEnabled, compact: cardHud.compact }"
    :style="cardHud.moodRgb ? { '--chud-mood-rgb': cardHud.moodRgb } : undefined"
  >
    <!-- Collapsed frosted pill with micro-telemetry -->
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
      <span v-if="cardHud.locationText" class="chud-pill-meta" :data-tip="cardHud.locationText || undefined" data-tip-float>
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path d="M8 14s4.5-4.1 4.5-7.6A4.5 4.5 0 003.5 6.4C3.5 9.9 8 14 8 14z" />
          <circle cx="8" cy="6.4" r="1.5" />
        </svg>
        <span>{{ cardHud.locationText }}</span>
      </span>
      <span v-if="cardHud.outfitText" class="chud-pill-meta chud-pill-outfit" :data-tip="cardHud.outfitText || undefined" data-tip-float>
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path d="M5.6 2.6a2.4 2.4 0 004.8 0l2.6 1.5-1.2 3.1-1.6-.7V13.5H5.8V6.5l-1.6.7L3 4.1z" />
        </svg>
        <span>{{ cardHud.outfitText }}</span>
      </span>
      <span v-if="cardAudio.bgmPlaying" class="chud-eq" aria-hidden="true">
        <i></i><i></i><i></i>
      </span>
      <span
        v-if="cardHud.thoughtEnabled && cardHud.thoughtText"
        class="chud-pill-thought"
        :data-tip="t('hud.thought')"
        data-tip-float
        aria-hidden="true"
      >
        <svg viewBox="0 0 16 16"><path d="M8 3.2c-3 0-5.4 1.9-5.4 4.3 0 1.4.8 2.6 2 3.4l-.6 2.1 2.3-1.1c.5.1 1.1.2 1.7.2 3 0 5.4-1.9 5.4-4.3S11 3.2 8 3.2z" /></svg>
      </span>
      <svg class="chud-chev" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
      <span v-if="cardHud.affectionValue !== null" class="chud-pill-bar" aria-hidden="true">
        <i :style="{ width: `${affectionPct}%` }"></i>
      </span>
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
            :data-tip="t('hud.toggleTip')"
            data-tip-float
            @click="cardHud.setEnabled(!cardHud.effectiveEnabled)"
          >
            {{ cardHud.effectiveEnabled ? t('hud.on') : t('hud.off') }}
          </button>
          <button
            type="button"
            class="chud-icon-btn"
            :class="{ on: cardHud.thoughtEnabled }"
            :data-tip="cardHud.thoughtEnabled ? t('hud.thoughtOffTip') : t('hud.thoughtOnTip')"
            data-tip-float
            @click="cardHud.setThoughtEnabled(!cardHud.thoughtEnabled)"
          >
            <svg viewBox="0 0 24 24">
              <path d="M12 4.5c-4.7 0-8.5 3-8.5 6.8 0 2.2 1.3 4.2 3.3 5.4L5.8 20l3.7-1.7c.8.2 1.6.3 2.5.3 4.7 0 8.5-3 8.5-6.8S16.7 4.5 12 4.5z" />
            </svg>
          </button>
          <button
            type="button"
            class="chud-icon-btn"
            :class="{ on: cardHud.compact }"
            :data-tip="cardHud.compact ? t('hud.compactOff') : t('hud.compactOn')"
            data-tip-float
            @click="cardHud.toggleCompact()"
          >
            <svg viewBox="0 0 24 24">
              <rect x="4" y="5" width="7" height="5" rx="1" />
              <rect x="13" y="5" width="7" height="5" rx="1" />
              <rect x="4" y="13" width="16" height="6" rx="1" />
            </svg>
          </button>
          <button
            v-if="hasAnyValue"
            type="button"
            class="chud-icon-btn"
            :data-tip="t('hud.resetTip')"
            data-tip-float
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

          <div v-for="[k, v] in cardHud.extraEntries" :key="k" class="chud-row chud-extra-row">
            <span class="chud-label" :data-tip="String(k)" data-tip-float>{{ k }}</span>
            <input
              v-if="editingKey === k"
              ref="editInput"
              v-model="editDraft"
              class="chud-input"
              @keydown.enter="commitEdit"
              @keydown.esc="cancelEdit"
              @blur="commitEdit"
            />
            <div v-else class="chud-extra-val">
              <button type="button" class="chud-val" @click="startEdit(k, v)">
                {{ String(v) }}
              </button>
              <button
                type="button"
                class="chud-unpin chud-field-del"
                :data-tip="t('hud.fieldRemove')"
                data-tip-float
                @click="cardHud.removeHudField(k)"
              >
                ×
              </button>
            </div>
          </div>

          <!-- Inline Custom Field Creator -->
          <div v-if="addingField" class="chud-add-box">
            <input
              ref="newFieldInput"
              v-model="newFieldName"
              class="chud-input chud-add-k"
              :placeholder="t('hud.fieldName')"
              @keydown.enter="submitAddField"
              @keydown.esc="cancelAddField"
            />
            <input
              v-model="newFieldValue"
              class="chud-input chud-add-v"
              :placeholder="t('hud.fieldVal')"
              @keydown.enter="submitAddField"
              @keydown.esc="cancelAddField"
            />
            <button type="button" class="chud-add-go" @click="submitAddField">
              {{ t('hud.fieldAdd') }}
            </button>
            <button type="button" class="chud-unpin" @click="cancelAddField">×</button>
          </div>
          <button
            v-else
            type="button"
            class="chud-add-trigger"
            @click="openAddField"
          >
            + {{ t('hud.addField') }}
          </button>
        </div>

        <!-- Selective Unspoken Thought Frosted Veil -->
        <button
          v-if="cardHud.thoughtEnabled && cardHud.thoughtText"
          type="button"
          class="chud-thought"
          :class="{ revealed: thoughtRevealed }"
          @click="thoughtRevealed = !thoughtRevealed"
        >
          <span class="chud-thought-head">
            <span class="chud-thought-k">{{ t('hud.thought') }}</span>
            <span class="chud-thought-hint">
              {{ thoughtRevealed ? t('hud.thoughtHide') : t('hud.thoughtPeek') }}
            </span>
          </span>
          <span class="chud-thought-body">{{ cardHud.thoughtText }}</span>
        </button>

        <!-- Pinned CBS Story Variables (synced with Scene Rules Workshop) -->
        <div v-if="cardHud.pinnedVarEntries.length" class="chud-pvars">
          <div class="chud-section-k">{{ t('hud.pinnedVars') }}</div>
          <div v-for="pv in cardHud.pinnedVarEntries" :key="pv.key" class="chud-row chud-pvar-row">
            <span class="chud-label" :data-tip="pv.key || undefined" data-tip-float>{{ pv.key }}</span>
            <input
              v-if="editingKey === `var:${pv.key}`"
              ref="editInput"
              v-model="editDraft"
              class="chud-input"
              @keydown.enter="commitEdit"
              @keydown.esc="cancelEdit"
              @blur="commitEdit"
            />
            <div v-else class="chud-pvar-ctrl">
              <button
                type="button"
                class="chud-val"
                @click="startEdit(`var:${pv.key}`, pv.value)"
              >
                {{ pv.value || t('hud.emptyValue') }}
              </button>
              <div v-if="isNumericStr(pv.value)" class="chud-step-grp">
                <button type="button" class="chud-step" @click="nudgePinnedVar(pv.key, pv.value, -1)">−</button>
                <button type="button" class="chud-step" @click="nudgePinnedVar(pv.key, pv.value, 1)">+</button>
              </div>
              <button
                type="button"
                class="chud-unpin"
                :data-tip="t('hud.unpinVar')"
                data-tip-float
                @click="cardHud.togglePinVar(pv.key)"
              >
                ×
              </button>
            </div>
          </div>
        </div>

        <!-- Stage Micro-Console: expression, scene, BGM & volume -->
        <div v-if="hasMediaAssets" class="chud-media">
          <div v-if="session.imageAssets.length" class="chud-chip" :class="{ active: openPicker === 'expression' }">
            <span class="chud-chip-k">{{ t('hud.expression') }}</span>
            <button
              type="button"
              class="chud-chip-pick"
              :data-tip="t('hud.switchMedia')"
              data-tip-float
              @click="togglePicker('expression')"
            >
              <span class="chud-chip-v" :class="{ muted: !cardHud.activeExpressionLabel }">
                {{ cardHud.activeExpressionLabel || t('hud.expressionDefault') }}
              </span>
              <svg class="chud-chip-chev" viewBox="0 0 16 16" aria-hidden="true">
                <path d="M4.5 6.5L8 10l3.5-3.5" />
              </svg>
            </button>
            <button
              v-if="cardHud.activeExpressionLabel"
              type="button"
              class="chud-chip-act"
              @click="cardHud.setManualExpression(null)"
            >
              {{ t('hud.expressionReset') }}
            </button>
          </div>

          <div v-if="session.audioAssets.length" class="chud-chip" :class="{ active: openPicker === 'bgm' }">
            <span class="chud-chip-k">{{ t('hud.bgm') }}</span>
            <button
              type="button"
              class="chud-chip-pick"
              :data-tip="t('hud.switchMedia')"
              data-tip-float
              @click="togglePicker('bgm')"
            >
              <span class="chud-chip-v" :class="{ muted: !cardAudio.bgmPlaying }">
                {{ cardAudio.bgmPlaying ? `♪ ${cardAudio.bgmName}` : t('hud.bgmEmpty') }}
              </span>
              <svg class="chud-chip-chev" viewBox="0 0 16 16" aria-hidden="true">
                <path d="M4.5 6.5L8 10l3.5-3.5" />
              </svg>
            </button>
            <button
              v-if="cardAudio.bgmPlaying"
              type="button"
              class="chud-chip-act"
              @click="cardAudio.stopBgm()"
            >
              {{ t('card.assetStop') }}
            </button>
          </div>

          <!-- Volume & Mute Bar -->
          <div v-if="session.audioAssets.length" class="chud-vol-row">
            <button
              type="button"
              class="chud-vol-btn"
              :class="{ muted: cardAudio.muted }"
              :data-tip="t('hud.mute')"
              data-tip-float
              @click="cardAudio.toggleMute()"
            >
              <svg v-if="cardAudio.muted" viewBox="0 0 16 16" aria-hidden="true">
                <path d="M2.5 6.2h2.4l3.3-2.9v9.4L4.9 9.8H2.5z" />
                <path d="M10.5 6l3 4m0-4l-3 4" />
              </svg>
              <svg v-else viewBox="0 0 16 16" aria-hidden="true">
                <path d="M2.5 6.2h2.4l3.3-2.9v9.4L4.9 9.8H2.5z" />
                <path d="M10.6 5.6a3.5 3.5 0 010 4.8m1.9-6.6a6 6 0 010 8.4" />
              </svg>
            </button>
            <input
              type="range"
              class="chud-vol-range"
              min="0"
              max="100"
              :value="volumePct"
              :aria-label="t('hud.volume')"
              @input="onVolumeInput"
            />
            <span class="chud-vol-pct">{{ volumePct }}%</span>
          </div>

          <!-- Inline Searchable Media Picker -->
          <div v-if="openPicker" class="chud-picker">
            <input
              v-if="rawPickerAssets.length > 6"
              v-model="pickerQuery"
              class="chud-input chud-picker-q"
              :placeholder="t('hud.filterAssets')"
            />
            <div class="chud-picker-list">
              <button
                v-if="openPicker === 'expression'"
                type="button"
                class="chud-pick-item"
                :class="{ on: !cardHud.activeExpressionLabel }"
                @click="pickExpression(null)"
              >
                <span>{{ t('hud.expressionDefault') }}</span>
              </button>
              <button
                v-else-if="openPicker === 'bgm' && cardAudio.bgmPlaying"
                type="button"
                class="chud-pick-item"
                @click="pickBgm(null)"
              >
                <span>{{ t('card.assetStop') }}</span>
              </button>

              <template v-for="asset in filteredPickerAssets" :key="asset.id">
                <div v-if="openPicker === 'bgm'" class="chud-pick-audio-row">
                  <button
                    type="button"
                    class="chud-pick-item"
                    :class="{ on: cardAudio.bgmAssetId === asset.id && cardAudio.bgmPlaying }"
                    @click="pickBgm(asset)"
                  >
                    <span class="chud-pick-name">{{ asset.name }}</span>
                    <span v-if="asset.asset_type" class="chud-pick-tag">{{ asset.asset_type }}</span>
                  </button>
                  <button
                    type="button"
                    class="chud-pick-sfx"
                    @click="cardAudio.playSfx(asset)"
                  >
                    {{ t('hud.playSfx') }}
                  </button>
                </div>
                <button
                  v-else
                  type="button"
                  class="chud-pick-item"
                  :class="{
                    on: openPicker === 'expression' && cardHud.stageExpressionRelPath === asset.rel_path,
                  }"
                  @click="pickExpression(asset)"
                >
                  <span class="chud-pick-name">{{ asset.name }}</span>
                  <span v-if="asset.asset_type" class="chud-pick-tag">{{ asset.asset_type }}</span>
                </button>
              </template>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chud {
  --chud-mood-rgb: var(--accent-rgb);
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
  background: rgb(var(--bg-rgb) / 0.9);
  backdrop-filter: blur(16px) saturate(1.18);
  -webkit-backdrop-filter: blur(16px) saturate(1.18);
  border: 1px solid rgb(var(--chud-mood-rgb) / 0.3);
  box-shadow:
    0 12px 32px -14px rgb(var(--shadow-rgb) / 0.52),
    inset 0 1px 0 rgb(255 255 255 / 0.08);
  transition: border-color 0.9s ease;
}
:global(html[data-theme="day"]) .chud-pill,
:global(html[data-theme="day"]) .chud-card {
  background: rgb(var(--bg-rgb) / 0.9);
}

.chud-pill {
  position: relative;
  overflow: hidden;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  max-width: 100%;
  padding: 6px 12px 7px;
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
  max-width: 136px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chud-pill-meta {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-family: var(--serif);
  font-size: 11px;
  color: var(--ink-3);
  padding-left: 6px;
  border-left: 1px solid var(--line-2);
  min-width: 0;
  max-width: 82px;
}
.chud-pill-outfit {
  max-width: 68px;
}
.chud-pill-meta svg {
  width: 11px;
  height: 11px;
  stroke: var(--accent-dim);
  fill: none;
  stroke-width: 1.65;
  flex: none;
}
.chud-pill-meta span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chud-pill-bar {
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: 0;
  height: 2px;
  border-radius: 999px;
  background: rgb(var(--ink-rgb) / 0.08);
  overflow: hidden;
}
.chud-pill-bar i {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, rgb(var(--chud-mood-rgb) / 0.55), rgb(var(--chud-mood-rgb)));
  transition: width 0.5s var(--ease), background 0.9s ease;
}
.chud-pill-thought {
  position: relative;
  display: inline-grid;
  place-items: center;
  width: 17px;
  height: 17px;
  border-radius: 50%;
  color: var(--accent);
  flex: none;
}
.chud-pill-thought::before {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: 50%;
  background: radial-gradient(circle, rgb(var(--accent-rgb) / 0.36), transparent 72%);
  pointer-events: none;
  animation: chudThoughtHalo 2.6s ease-out infinite;
}
.chud-pill-thought svg {
  position: relative;
  z-index: 1;
  width: 11px;
  height: 11px;
  stroke: currentColor;
  fill: rgb(var(--accent-rgb) / 0.16);
  stroke-width: 1.75;
  filter: drop-shadow(0 0 4px rgb(var(--accent-rgb) / 0.58));
  animation: chudThoughtBeat 2.6s ease-in-out infinite;
}
@keyframes chudThoughtBeat {
  0%, 100% { transform: scale(1); opacity: 0.88; }
  30% { transform: scale(1.16); opacity: 1; }
  52% { transform: scale(1.03); opacity: 0.94; }
  70% { transform: scale(1.1); opacity: 1; }
}
@keyframes chudThoughtHalo {
  0% {
    transform: scale(0.85);
    opacity: 0.85;
    box-shadow: 0 0 2px 0 rgb(var(--accent-rgb) / 0.62);
  }
  75%, 100% {
    transform: scale(1.75);
    opacity: 0;
    box-shadow: 0 0 10px 4px rgb(var(--accent-rgb) / 0);
  }
}
@media (prefers-reduced-motion: reduce) {
  .chud-pill-thought::before,
  .chud-pill-thought svg {
    animation: none;
  }
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
  transition: background-color 0.9s ease, box-shadow 0.9s ease;
}
.chud-dot.on {
  background: rgb(var(--chud-mood-rgb));
  box-shadow: 0 0 8px rgb(var(--chud-mood-rgb) / 0.68);
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
  width: min(292px, calc(100vw - 40px));
  border-radius: 12px;
  overflow: hidden;
  animation: chudIn 0.22s var(--ease) both;
}
.chud.compact .chud-card {
  width: min(252px, calc(100vw - 40px));
}
@keyframes chudIn {
  from { opacity: 0; transform: translateY(-4px) scale(0.985); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

.chud-shimmer {
  height: 2px;
  background: linear-gradient(
    90deg,
    rgb(var(--chud-mood-rgb)),
    rgb(var(--chud-mood-rgb) / 0.24) 72%,
    transparent
  );
  transition: background 0.9s ease;
}

.chud-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 9px 11px 7px;
  border-bottom: 1px solid var(--line);
}
.chud.compact .chud-head {
  padding: 7px 10px 6px;
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
.chud-icon-btn.on {
  color: var(--accent);
  background: rgb(var(--accent-rgb) / 0.12);
}
.chud-icon-btn svg {
  width: 13px;
  height: 13px;
  stroke: currentColor;
  fill: none;
  stroke-width: 1.8;
}

.chud-body {
  padding: 9px 11px 11px;
  max-height: 310px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.chud.compact .chud-body {
  padding: 7px 9px 9px;
  max-height: 256px;
  gap: 6px;
}
.chud.compact .chud-stat {
  padding: 5px 8px;
  gap: 2px;
}
.chud.compact .chud-val {
  font-size: 12px;
}
.chud.compact .chud-rows {
  gap: 3px;
}
.chud.compact .chud-row {
  grid-template-columns: 52px 1fr;
  padding: 2px 1px;
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
.chud-pvars {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding-top: 6px;
  border-top: 1px solid var(--line);
}
.chud-section-k {
  font-family: var(--mono);
  font-size: 8px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--accent-dim);
}
.chud-pvar-row {
  grid-template-columns: 76px 1fr;
  align-items: center;
}
.chud-pvar-row .chud-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chud-pvar-ctrl {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.chud-pvar-ctrl .chud-val {
  flex: 1;
  min-width: 0;
}
.chud-step-grp {
  display: inline-flex;
  align-items: center;
  border: 1px solid var(--line-2);
  border-radius: 5px;
  overflow: hidden;
  flex: none;
}
.chud-step {
  width: 18px;
  height: 18px;
  display: grid;
  place-items: center;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--ink-3);
  transition: background-color 0.15s ease, color 0.15s ease;
}
.chud-step:hover {
  background: rgb(var(--accent-rgb) / 0.14);
  color: var(--accent);
}
.chud-unpin {
  font-size: 13px;
  line-height: 1;
  padding: 0 3px;
  color: var(--ink-4);
  flex: none;
}
.chud-extra-val {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}
.chud-extra-val .chud-val {
  flex: 1;
  min-width: 0;
}
.chud-field-del {
  opacity: 0.45;
  transition: opacity 0.15s ease, color 0.15s ease;
}
.chud-extra-row:hover .chud-field-del {
  opacity: 1;
}
.chud-add-trigger {
  align-self: flex-start;
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.08em;
  color: var(--ink-4);
  padding: 2px 6px;
  border-radius: 5px;
  border: 1px dashed var(--line-2);
  transition: color 0.2s ease, border-color 0.2s ease;
}
.chud-add-trigger:hover {
  color: var(--accent);
  border-color: rgb(var(--accent-rgb) / 0.45);
}
.chud-add-box {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 2px;
}
.chud-add-k {
  flex: 1.05;
  font-size: 11px;
  padding: 2px 5px;
}
.chud-add-v {
  flex: 1;
  font-size: 11px;
  padding: 2px 5px;
}
.chud-add-go {
  flex: none;
  font-family: var(--mono);
  font-size: 9.5px;
  padding: 3px 7px;
  border-radius: 4px;
  background: rgb(var(--accent-rgb) / 0.18);
  border: 1px solid rgb(var(--accent-rgb) / 0.42);
  color: var(--accent);
}

.chud-chip.active {
  border-color: rgb(var(--accent-rgb) / 0.48);
  background: rgb(var(--accent-rgb) / 0.11);
}
.chud-chip-pick {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 4px;
  text-align: left;
  cursor: pointer;
}
.chud-chip-v.muted {
  color: var(--ink-4);
}
.chud-chip-chev {
  width: 11px;
  height: 11px;
  stroke: var(--ink-4);
  fill: none;
  stroke-width: 1.8;
  flex: none;
  transition: transform 0.2s ease;
}
.chud-chip.active .chud-chip-chev {
  transform: rotate(180deg);
  stroke: var(--accent);
}
.chud-vol-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 2px 4px;
}
.chud-vol-btn {
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  border-radius: 5px;
  color: var(--ink-3);
  flex: none;
}
.chud-vol-btn:hover {
  color: var(--accent);
}
.chud-vol-btn.muted {
  color: var(--ember);
}
.chud-vol-btn svg {
  width: 13px;
  height: 13px;
  stroke: currentColor;
  fill: none;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.chud-vol-range {
  -webkit-appearance: none;
  appearance: none;
  flex: 1;
  min-width: 0;
  height: 2px;
  background: rgb(var(--ink-rgb) / 0.16);
  border-radius: 999px;
  outline: none;
  cursor: pointer;
}
.chud-vol-range::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 3px rgb(var(--accent-rgb) / 0.16);
  cursor: pointer;
}
.chud-vol-range::-moz-range-thumb {
  width: 9px;
  height: 9px;
  border: none;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 3px rgb(var(--accent-rgb) / 0.16);
  cursor: pointer;
}
.chud-vol-pct {
  font-family: var(--mono);
  font-size: 9px;
  color: var(--ink-4);
  width: 28px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.chud-picker {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 6px;
  border-radius: 8px;
  background: rgb(var(--bg-rgb) / 0.72);
  border: 1px solid var(--line-2);
}
.chud-picker-q {
  font-size: 11px;
  padding: 3px 6px;
}
.chud-picker-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 128px;
  overflow-y: auto;
}
.chud-pick-audio-row {
  display: flex;
  align-items: center;
  gap: 4px;
}
.chud-pick-audio-row .chud-pick-item {
  flex: 1;
}
.chud-pick-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  width: 100%;
  padding: 4px 7px;
  border-radius: 5px;
  font-family: var(--serif);
  font-size: 11.5px;
  color: var(--ink-2);
  text-align: left;
  transition: background-color 0.15s ease, color 0.15s ease;
}
.chud-pick-item:hover {
  background: rgb(var(--ink-rgb) / 0.06);
  color: var(--ink);
}
.chud-pick-item.on {
  background: rgb(var(--accent-rgb) / 0.16);
  color: var(--accent);
}
.chud-pick-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chud-pick-tag {
  font-family: var(--mono);
  font-size: 8px;
  text-transform: uppercase;
  color: var(--ink-4);
  flex: none;
}
.chud-pick-sfx {
  font-family: var(--mono);
  font-size: 8.5px;
  padding: 3px 6px;
  border-radius: 4px;
  border: 1px solid var(--line-2);
  color: var(--ink-3);
  flex: none;
}
.chud-pick-sfx:hover {
  color: var(--accent);
  border-color: rgb(var(--accent-rgb) / 0.45);
}
.chud-unpin:hover {
  color: var(--ember);
}
.chud-thought {
  display: flex;
  flex-direction: column;
  gap: 3px;
  width: 100%;
  text-align: left;
  padding: 6px 9px;
  border-radius: 0 8px 8px 0;
  border-left: 2px solid rgb(var(--chud-mood-rgb) / 0.65);
  background: rgb(var(--chud-mood-rgb) / 0.065);
  cursor: pointer;
  transition: background-color 0.25s ease, border-color 0.9s ease;
}
.chud-thought:hover {
  background: rgb(var(--chud-mood-rgb) / 0.1);
}
.chud-thought-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.chud-thought-k {
  font-family: var(--mono);
  font-size: 8px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: rgb(var(--chud-mood-rgb));
}
.chud-thought-hint {
  font-family: var(--mono);
  font-size: 8px;
  color: var(--ink-4);
}
.chud-thought-body {
  font-family: var(--serif);
  font-style: italic;
  font-size: 11.5px;
  line-height: 1.48;
  color: var(--ink-2);
  filter: blur(4.4px);
  opacity: 0.62;
  user-select: none;
  transition: filter 0.35s ease, opacity 0.35s ease;
}
.chud-thought:hover .chud-thought-body {
  filter: blur(2.6px);
  opacity: 0.74;
}
.chud-thought.revealed .chud-thought-body {
  filter: blur(0);
  opacity: 0.96;
  user-select: text;
}
</style>

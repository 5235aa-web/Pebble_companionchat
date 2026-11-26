<script setup lang="ts">
import { computed, ref } from 'vue'
import type { PuppetEmotion } from '@/api/inochi2d'
import { useInochi2dStore } from '@/stores/inochi2d'
import { useSessionStore } from '@/stores/session'
import { useUiStore } from '@/stores/ui'
import { useI18n } from '@/i18n'
import UiSlider from './ui/UiSlider.vue'
import UiLever from './ui/UiLever.vue'

const inochi = useInochi2dStore()
const session = useSessionStore()
const ui = useUiStore()
const { t, locale } = useI18n()

const tab = computed({
  get: () => inochi.editorTab,
  set: (v) => {
    inochi.editorTab = v
  },
})

const resolution = ref(768)
const steps = ref(20)
const fileInputRef = ref<HTMLInputElement | null>(null)
const packFilesInputRef = ref<HTMLInputElement | null>(null)
const buildErr = ref('')

// Pack / incremental outfit builder state
type PackItem = {
  key: string
  label: string
  file?: File
  assetId?: number
  sourceName: string
}

const packMode = ref<'outfit' | 'base'>('outfit')
const outfitLabel = ref('')
const packItems = ref<PackItem[]>([])
const baseIndex = ref(0)
const saveToAssets = ref(true)
const assetPickerOpen = ref(false)

// Non-modal draggable positioning anchored to the left of the right-hand ScenePanel
const offsetX = ref(0)
const offsetY = ref(0)

function onDragStart(e: PointerEvent): void {
  if ((e.target as HTMLElement | null)?.closest('button')) return
  const startX = e.clientX
  const startY = e.clientY
  const baseX = offsetX.value
  const baseY = offsetY.value
  const onMove = (ev: PointerEvent) => {
    offsetX.value = baseX + (ev.clientX - startX)
    offsetY.value = baseY + (ev.clientY - startY)
  }
  const onUp = () => {
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
  }
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
}

const popupStyle = computed(() => ({
  transform: `translate3d(${offsetX.value}px, ${offsetY.value}px, 0)`,
}))

const emotions: { id: PuppetEmotion; label: string; labelEn: string }[] = [
  { id: 'neutral', label: '默认', labelEn: 'Neutral' },
  { id: 'happy', label: '开心', labelEn: 'Happy' },
  { id: 'shy', label: '害羞', labelEn: 'Shy' },
  { id: 'surprised', label: '惊讶', labelEn: 'Surprised' },
  { id: 'sad', label: '委屈', labelEn: 'Sad' },
]

const displayedEmotions = computed(() => {
  const list = inochi.puppet?.expressions
  if (list && list.length > 0) {
    return list.map((e) => ({
      id: e.id,
      emotionKey: e.emotionKey,
      label: e.label,
      labelEn: e.labelEn || e.label,
    }))
  }
  return emotions.map((e) => ({ ...e, emotionKey: e.id }))
})

const activeEmotionId = computed(() => {
  const cur = inochi.config.emotion
  const list = displayedEmotions.value
  const exact = list.find((e) => e.id === cur)
  if (exact) return exact.id
  const byKey = list.find((e) => e.emotionKey && e.emotionKey === cur)
  return byKey ? byKey.id : cur
})

const displayedOutfits = computed(() => {
  return (inochi.puppet?.outfits ?? []).map((o) => ({
    id: o.id,
    label: o.label,
    labelEn: o.labelEn || o.label,
  }))
})

const activeOutfitId = computed(() => {
  const list = displayedOutfits.value
  if (!list.length) return ''
  const cur = inochi.config.outfit
  return cur && list.some((o) => o.id === cur) ? cur : list[0].id
})

const hiddenSet = computed(() => new Set(inochi.config.hiddenLayers || []))
const orderedLayers = computed(() => {
  const list = inochi.puppet?.layers ?? []
  const oid = activeOutfitId.value
  return list
    .filter((l) => !l.outfit || !oid || l.outfit === oid)
    .sort((a, b) => b.zIndex - a.zIndex)
})

const effectivePackMode = computed<'outfit' | 'base'>(() => {
  if (packMode.value === 'outfit' && !inochi.puppet) return 'base'
  return packMode.value
})

function layerTitle(label: string, labelEn: string): string {
  return locale.value === 'en' ? labelEn : label
}

function setAllClothesVisible(visible: boolean): void {
  const list = orderedLayers.value
  const hidden = new Set(inochi.config.hiddenLayers || [])
  for (const l of list) {
    if (l.category === 'clothes' || l.category === 'accessory') {
      if (visible) hidden.delete(l.id)
      else hidden.add(l.id)
    }
  }
  inochi.updateConfig({ hiddenLayers: Array.from(hidden) })
}

async function rebuildFromPortrait(): Promise<void> {
  buildErr.value = ''
  try {
    await inochi.startBuild(undefined, resolution.value, steps.value)
  } catch (e) {
    buildErr.value = e instanceof Error ? e.message : String(e)
  }
}

async function onUploadPicked(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  buildErr.value = ''
  try {
    await inochi.startBuildFromFile(file, resolution.value, steps.value)
  } catch (err) {
    buildErr.value = err instanceof Error ? err.message : String(err)
  }
}

function stripExt(name: string): string {
  const idx = name.lastIndexOf('.')
  return idx > 0 ? name.slice(0, idx) : name
}

function autoPickBaseIndex(items: PackItem[]): void {
  const wideEye = items.findIndex((i) => /惊讶2|惊讶|surprise/i.test(i.label))
  if (wideEye >= 0) {
    baseIndex.value = wideEye
    return
  }
  const normal = items.findIndex((i) => /正常|normal|default/i.test(i.label))
  if (normal >= 0) {
    baseIndex.value = normal
    return
  }
  if (baseIndex.value >= items.length) baseIndex.value = 0
}

function onPackFilesPicked(e: Event): void {
  const input = e.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  if (!files.length) return
  const additions: PackItem[] = files.map((file, idx) => ({
    key: `f-${Date.now()}-${idx}-${file.name}`,
    label: stripExt(file.name),
    file,
    sourceName: file.name,
  }))
  if (effectivePackMode.value === 'outfit') {
    packItems.value = [additions[0]]
    baseIndex.value = 0
  } else {
    packItems.value = [...packItems.value, ...additions]
    autoPickBaseIndex(packItems.value)
  }
}

function toggleAssetForPack(assetId: number, assetName: string): void {
  const existingIdx = packItems.value.findIndex((x) => x.assetId === assetId)
  if (existingIdx >= 0) {
    packItems.value = packItems.value.filter((_, i) => i !== existingIdx)
    if (baseIndex.value >= packItems.value.length) baseIndex.value = Math.max(0, packItems.value.length - 1)
    return
  }
  const item: PackItem = {
    key: `a-${assetId}`,
    label: stripExt(assetName),
    assetId,
    sourceName: assetName,
  }
  if (effectivePackMode.value === 'outfit') {
    packItems.value = [item]
    baseIndex.value = 0
    assetPickerOpen.value = false
  } else {
    packItems.value = [...packItems.value, item]
    autoPickBaseIndex(packItems.value)
  }
}

function removePackItem(index: number): void {
  packItems.value = packItems.value.filter((_, i) => i !== index)
  if (baseIndex.value >= packItems.value.length) {
    baseIndex.value = Math.max(0, packItems.value.length - 1)
  }
}

async function openAssetChooser(): Promise<void> {
  const id = session.activeCompanionId
  if (id && !session.imageAssets.length) {
    await session.loadCompanionAssets(id).catch(() => undefined)
  }
  assetPickerOpen.value = !assetPickerOpen.value
}

async function runPackBuild(): Promise<void> {
  if (!packItems.value.length) return
  buildErr.value = ''
  const orderedAssets = packItems.value.filter((x) => x.assetId !== undefined)
  const orderedFiles = packItems.value.filter((x) => x.file !== undefined)
  const reordered = [...orderedAssets, ...orderedFiles]
  const chosenKey = packItems.value[baseIndex.value]?.key
  const mappedBaseIdx = Math.max(
    0,
    reordered.findIndex((x) => x.key === chosenKey),
  )

  try {
    await inochi.startBuildPack({
      mode: effectivePackMode.value,
      outfitLabel: outfitLabel.value.trim(),
      baseIndex: mappedBaseIdx,
      labels: reordered.map((x) => x.label.trim() || stripExt(x.sourceName)),
      assetIds: orderedAssets.map((x) => x.assetId as number),
      files: orderedFiles.map((x) => x.file as File),
      saveToAssets: saveToAssets.value,
      resolution: resolution.value,
      steps: steps.value,
    })
    packItems.value = []
    if (effectivePackMode.value === 'outfit') outfitLabel.value = ''
    assetPickerOpen.value = false
  } catch (err) {
    buildErr.value = err instanceof Error ? err.message : String(err)
  }
}

async function onDeleteOutfit(oid: string): Promise<void> {
  buildErr.value = ''
  try {
    await inochi.removeOutfit(oid)
  } catch (err) {
    buildErr.value = err instanceof Error ? err.message : String(err)
  }
}
</script>

<template>
  <aside
    v-if="inochi.editorOpen && inochi.activeMode === 'live2d'"
    class="puppet-float"
    :style="popupStyle"
    role="dialog"
    aria-modal="false"
  >
    <div class="puppet-float-head" @pointerdown="onDragStart">
      <div class="puppet-float-titles">
        <span class="d-kicker">LIVE2D</span>
        <span class="d-title">{{ t('live2d.editorTitle') }}</span>
      </div>
      <button
        class="d-close"
        type="button"
        :aria-label="t('live2d.closeEditor')"
        @click="inochi.closeEditor()"
      >
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>

    <div class="tabs puppet-float-tabs">
      <button class="tab" :class="{ on: tab === 'motion' }" @click="tab = 'motion'">
        {{ t('live2d.tabMotion') }}
      </button>
      <button class="tab" :class="{ on: tab === 'layers' }" @click="tab = 'layers'">
        {{ t('live2d.tabLayers') }} ({{ orderedLayers.length }})
      </button>
      <button class="tab" :class="{ on: tab === 'build' }" @click="tab = 'build'">
        {{ t('live2d.tabBuild') }}
      </button>
    </div>

    <div class="puppet-float-body">
      <template v-if="tab === 'motion'">
        <div class="puppet-row-lever">
          <UiLever
            :model-value="inochi.config.motionEnabled"
            :label="t('live2d.motionEnable')"
            @update:model-value="inochi.updateConfig({ motionEnabled: $event })"
          />
          <UiLever
            :model-value="inochi.config.eyeTrack"
            :label="t('live2d.eyeTrack')"
            @update:model-value="inochi.updateConfig({ eyeTrack: $event })"
          />
        </div>

        <template v-if="displayedOutfits.length > 1">
          <div class="puppet-sec-label">{{ t('live2d.outfit') }}</div>
          <div class="puppet-emo-grid">
            <button
              v-for="o in displayedOutfits"
              :key="o.id"
              type="button"
              class="puppet-emo-btn"
              :class="{ on: activeOutfitId === o.id }"
              @click="inochi.updateConfig({ outfit: o.id })"
            >
              {{ locale === 'en' ? o.labelEn : o.label }}
            </button>
          </div>
        </template>

        <div class="puppet-sec-label">{{ t('live2d.emotion') }}</div>
        <div class="puppet-emo-grid">
          <button
            v-for="e in displayedEmotions"
            :key="e.id"
            type="button"
            class="puppet-emo-btn"
            :class="{ on: activeEmotionId === e.id }"
            @click="inochi.updateConfig({ emotion: e.id })"
          >
            {{ locale === 'en' ? e.labelEn : e.label }}
          </button>
        </div>

        <UiSlider
          :label="t('live2d.breathAmp')"
          :model-value="inochi.config.breathAmp"
          :min="0"
          :max="2"
          :step="0.05"
          @update:model-value="inochi.updateConfig({ breathAmp: $event })"
        />
        <UiSlider
          :label="t('live2d.breathSpeed')"
          :model-value="inochi.config.breathSpeed"
          :min="0.3"
          :max="2.2"
          :step="0.05"
          @update:model-value="inochi.updateConfig({ breathSpeed: $event })"
        />
        <UiSlider
          :label="t('live2d.swayAmp')"
          :model-value="inochi.config.swayAmp"
          :min="0"
          :max="2"
          :step="0.05"
          @update:model-value="inochi.updateConfig({ swayAmp: $event })"
        />
        <UiSlider
          :label="t('live2d.headParallax')"
          :model-value="inochi.config.headParallax"
          :min="0"
          :max="2"
          :step="0.05"
          @update:model-value="inochi.updateConfig({ headParallax: $event })"
        />
        <UiSlider
          :label="t('live2d.blinkRate')"
          :model-value="inochi.config.blinkRate"
          :min="0"
          :max="2.5"
          :step="0.05"
          @update:model-value="inochi.updateConfig({ blinkRate: $event })"
        />
        <UiSlider
          :label="t('live2d.physicsStrength')"
          :model-value="inochi.config.physicsStrength"
          :min="0"
          :max="2"
          :step="0.05"
          @update:model-value="inochi.updateConfig({ physicsStrength: $event })"
        />
        <UiSlider
          :label="t('live2d.physicsDamping')"
          :model-value="inochi.config.physicsDamping"
          :min="0.1"
          :max="1.5"
          :step="0.05"
          @update:model-value="inochi.updateConfig({ physicsDamping: $event })"
        />
        <UiSlider
          :label="t('live2d.mouthSens')"
          :model-value="inochi.config.mouthSens"
          :min="0"
          :max="3"
          :step="0.05"
          @update:model-value="inochi.updateConfig({ mouthSens: $event })"
        />
        <UiSlider
          :label="t('live2d.testMouth')"
          :model-value="inochi.config.testMouth"
          :min="0"
          :max="1"
          :step="0.02"
          @update:model-value="inochi.updateConfig({ testMouth: $event })"
        />

        <div class="puppet-float-actions">
          <button class="sc-open" type="button" @click="inochi.resetConfig()">
            {{ t('live2d.resetParams') }}
          </button>
        </div>
      </template>

      <template v-else-if="tab === 'layers'">
        <div class="puppet-layer-toolbar">
          <button class="sc-open" type="button" @click="setAllClothesVisible(true)">
            {{ t('live2d.showClothes') }}
          </button>
          <button class="sc-open" type="button" @click="setAllClothesVisible(false)">
            {{ t('live2d.hideClothes') }}
          </button>
        </div>

        <p v-if="!orderedLayers.length" class="cp-hint">{{ t('live2d.noLayers') }}</p>

        <div v-else class="puppet-layer-list">
          <label
            v-for="layer in orderedLayers"
            :key="layer.id"
            class="puppet-layer-item"
            :class="{ hidden: hiddenSet.has(layer.id) }"
          >
            <input
              type="checkbox"
              :checked="!hiddenSet.has(layer.id)"
              @change="inochi.toggleLayerVisibility(layer.id)"
            />
            <span class="puppet-layer-name">{{ layerTitle(layer.label, layer.labelEn) }}</span>
            <span class="puppet-layer-tag">{{ layer.tag }}</span>
          </label>
        </div>
      </template>

      <template v-else>
        <!-- Environment & model weights diagnostics -->
        <div v-if="inochi.env" class="puppet-env-card" :class="{ warn: !inochi.env.ready }">
          <div class="puppet-env-head">
            <span class="puppet-env-badge">
              {{ inochi.env.ready ? t('live2d.envReady') : t('live2d.envMissing') }}
            </span>
            <div class="puppet-env-tools">
              <button class="mini-btn" type="button" @click="inochi.refreshEnv()">
                {{ t('live2d.recheckEnv') }}
              </button>
              <button class="mini-btn" type="button" @click="ui.openBackstageTab('paths')">
                {{ t('live2d.openPathSettings') }}
              </button>
            </div>
          </div>
          <div v-if="inochi.env.ready && inochi.env.runtime" class="puppet-env-meta">
            {{ inochi.env.runtime.gpuName || inochi.env.runtime.backend }}
            <template v-if="inochi.env.runtime.vramGb"> · {{ inochi.env.runtime.vramGb }}GB</template>
            · {{ inochi.env.runtime.dtype }}
            <template v-if="inochi.env.runtime.offloadDefault"> · offload</template>
          </div>
          <ul v-else-if="!inochi.env.ready" class="puppet-env-list">
            <li v-if="!inochi.env.pythonPath">
              Python venv -> {{ inochi.env.expectedVenvDir || '~/.pebblecompanion/inochi2d-venv' }}
            </li>
            <li v-else-if="inochi.env.missingDeps?.length">
              pip: {{ inochi.env.requirementsPath || 'backend/Inochi2D/requirements.txt' }}
              ({{ inochi.env.missingDeps.slice(0, 4).join(', ') }})
            </li>
            <li v-if="!inochi.env.seethroughPath">See-through: backend/Inochi2D/see-through</li>
            <li v-if="!inochi.env.layerdiffPath">
              <a
                class="puppet-hf-link"
                href="https://huggingface.co/layerdifforg/seethroughv0.0.2_layerdiff3d"
                target="_blank"
                rel="noopener noreferrer"
              >LayerDiff3D</a>
              → {{ inochi.env.expectedLayerdiffDir }}
            </li>
            <li v-if="!inochi.env.marigoldPath">
              <a
                class="puppet-hf-link"
                href="https://huggingface.co/24yearsold/seethroughv0.0.1_marigold"
                target="_blank"
                rel="noopener noreferrer"
              >Marigold</a>
              → {{ inochi.env.expectedMarigoldDir }}
            </li>
          </ul>
        </div>

        <p class="cp-hint">{{ t('live2d.buildHint') }}</p>
        <UiSlider
          :label="t('live2d.resolution')"
          :model-value="resolution"
          :min="512"
          :max="1280"
          :step="128"
          @update:model-value="resolution = $event"
        />
        <UiSlider
          :label="t('live2d.steps')"
          :model-value="steps"
          :min="12"
          :max="36"
          :step="2"
          @update:model-value="steps = $event"
        />

        <div class="puppet-float-actions">
          <button
            class="sc-gen"
            type="button"
            :disabled="inochi.job.status === 'running' || !session.activeCompanionId || (inochi.env ? !inochi.env.ready : false)"
            @click="rebuildFromPortrait"
          >
            <span>{{ t('live2d.rebuildPortrait') }}</span>
          </button>
          <button
            class="sc-open"
            type="button"
            :disabled="inochi.job.status === 'running' || !session.activeCompanionId || (inochi.env ? !inochi.env.ready : false)"
            @click="fileInputRef?.click()"
          >
            <span>{{ t('live2d.pickImage') }}</span>
          </button>
        </div>

        <!-- Multi-image expression pack & incremental outfit builder -->
        <div class="puppet-pack-box">
          <div class="puppet-sec-label">{{ t('live2d.packTitle') }}</div>

          <div v-if="inochi.puppet" class="puppet-pack-modes">
            <button
              type="button"
              class="puppet-emo-btn"
              :class="{ on: effectivePackMode === 'outfit' }"
              @click="packMode = 'outfit'; packItems = packItems.slice(0, 1); baseIndex = 0"
            >
              {{ t('live2d.modeAddOutfit') }}
            </button>
            <button
              type="button"
              class="puppet-emo-btn"
              :class="{ on: effectivePackMode === 'base' }"
              @click="packMode = 'base'"
            >
              {{ t('live2d.modeRebuildPack') }}
            </button>
          </div>

          <p class="cp-hint">
            {{ effectivePackMode === 'outfit' ? t('live2d.addOutfitHint') : t('live2d.rebuildPackHint') }}
          </p>

          <input
            v-if="effectivePackMode === 'outfit'"
            v-model="outfitLabel"
            class="pf-input puppet-pack-input"
            :placeholder="t('live2d.outfitNamePlaceholder')"
          />

          <div class="puppet-float-actions">
            <button
              type="button"
              class="sc-open"
              :disabled="inochi.job.status === 'running' || !session.activeCompanionId"
              @click="packFilesInputRef?.click()"
            >
              {{ t('live2d.pickPackFiles') }}
            </button>
            <button
              type="button"
              class="sc-open"
              :disabled="inochi.job.status === 'running' || !session.activeCompanionId"
              @click="openAssetChooser"
            >
              {{ t('live2d.pickFromAssets') }}
            </button>
          </div>

          <!-- Character media assets picker -->
          <div v-if="assetPickerOpen" class="puppet-asset-picker">
            <p v-if="!session.imageAssets.length" class="cp-hint">{{ t('live2d.noCardImageAssets') }}</p>
            <div v-else class="puppet-asset-chips">
              <button
                v-for="a in session.imageAssets.slice(0, 48)"
                :key="a.id"
                type="button"
                class="puppet-emo-btn"
                :class="{ on: packItems.some((p) => p.assetId === a.id) }"
                @click="toggleAssetForPack(a.id, a.name)"
              >
                {{ a.name }}
              </button>
            </div>
          </div>

          <!-- Staged pack items with inline label + wide-eyed base selector -->
          <div v-if="packItems.length" class="puppet-pack-list">
            <div v-for="(item, idx) in packItems" :key="item.key" class="puppet-pack-row">
              <button
                v-if="effectivePackMode === 'base'"
                type="button"
                class="puppet-pack-base-btn"
                :class="{ on: baseIndex === idx }"
                :data-tip="t('live2d.setBaseTooltip')"
                data-tip-float
                @click="baseIndex = idx"
              >
                {{ baseIndex === idx ? '★' : '☆' }}
              </button>
              <input
                v-model="item.label"
                class="pf-input puppet-pack-label-in"
                :data-tip="item.sourceName || undefined"
                data-tip-float
              />
              <button type="button" class="pf-asset-btn danger" @click="removePackItem(idx)">×</button>
            </div>
          </div>

          <label v-if="packItems.some((i) => i.file)" class="puppet-pack-save">
            <input v-model="saveToAssets" type="checkbox" />
            <span>{{ t('live2d.savePackToAssets') }}</span>
          </label>

          <button
            v-if="packItems.length"
            class="sc-gen puppet-pack-go"
            type="button"
            :disabled="inochi.job.status === 'running' || !session.activeCompanionId || (inochi.env ? !inochi.env.ready : false)"
            @click="runPackBuild"
          >
            <span>
              {{ effectivePackMode === 'outfit' ? t('live2d.startAddOutfit') : t('live2d.startPackBuild', { count: packItems.length }) }}
            </span>
          </button>

          <!-- Manage existing outfits -->
          <div v-if="displayedOutfits.length > 1" class="puppet-outfit-manage">
            <div class="puppet-sec-label">{{ t('live2d.manageOutfits') }}</div>
            <div class="puppet-emo-grid">
              <span v-for="o in displayedOutfits" :key="o.id" class="puppet-outfit-chip">
                <span>{{ locale === 'en' ? o.labelEn : o.label }}</span>
                <button
                  type="button"
                  class="pf-asset-btn danger"
                  :disabled="inochi.job.status === 'running'"
                  @click="onDeleteOutfit(o.id)"
                >×</button>
              </span>
            </div>
          </div>
        </div>

        <p v-if="buildErr || inochi.job.error" class="scene-error">
          {{ buildErr || inochi.job.error }}
        </p>
        <input
          ref="fileInputRef"
          type="file"
          accept=".png,.jpg,.jpeg,.webp,.bmp"
          style="display: none"
          @change="onUploadPicked"
        />
        <input
          ref="packFilesInputRef"
          type="file"
          accept=".png,.jpg,.jpeg,.webp,.bmp"
          :multiple="effectivePackMode === 'base'"
          style="display: none"
          @change="onPackFilesPicked"
        />
      </template>
    </div>
  </aside>
</template>

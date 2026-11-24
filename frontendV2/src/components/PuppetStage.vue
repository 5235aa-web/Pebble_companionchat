<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useInochi2dStore } from '@/stores/inochi2d'
import { useSessionStore } from '@/stores/session'
import { useChatStore } from '@/stores/chat'
import { useUiStore } from '@/stores/ui'
import { useI18n } from '@/i18n'
import { Puppet2DRenderer, type HitZone } from '@/lib/puppet2d'
import { useFileUrl } from '@/composables/useFileUrl'

const props = defineProps<{
  activeSlideUrl?: string
}>()

const inochi = useInochi2dStore()
const session = useSessionStore()
const chat = useChatStore()
const ui = useUiStore()
const { t } = useI18n()

const canvasRef = ref<HTMLCanvasElement | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
const ready = ref(false)
const localError = ref('')
const canvasCursor = ref('')

let renderer: Puppet2DRenderer | null = null
let activePointerDown = false

const who = computed(() => chat.activeCast)
const sourcePreviewPath = computed(() => props.activeSlideUrl || who.value?.avatar || '')
const sourcePreviewUrl = useFileUrl(() => sourcePreviewPath.value, 0)

const stageLabel = computed(() => {
  const s = inochi.job.stage
  if (s === 'loading') return t('live2d.stageLoading')
  if (s === 'layerdiff') return t('live2d.stageLayerdiff')
  if (s === 'depth') return t('live2d.stageDepth')
  if (s === 'extract') return t('live2d.stageExtract')
  if (s === 'puppet') return t('live2d.stagePuppet')
  if (s === 'postprocess') return t('live2d.stagePostprocess')
  return t('live2d.building')
})

function ensureRenderer(): void {
  if (renderer || !canvasRef.value) return
  try {
    renderer = new Puppet2DRenderer(canvasRef.value, inochi.config, (ok) => {
      ready.value = ok
    })
  } catch (e) {
    localError.value = e instanceof Error ? e.message : String(e)
  }
}

watch(
  () => inochi.puppet,
  (manifest) => {
    ready.value = false
    if (!manifest) return
    ensureRenderer()
    if (renderer) {
      void renderer.loadManifest(manifest)
    }
  },
  { immediate: true },
)

watch(
  () => inochi.config,
  (cfg) => {
    renderer?.setConfig(cfg)
  },
  { deep: true },
)

function cursorForZone(zone: HitZone | null, pressed: boolean): string {
  if (zone === 'crown' || zone === 'cheek') return pressed ? 'grabbing' : 'grab'
  if (zone === 'chest') return 'pointer'
  return ''
}

function toCanvasNormalized(e: PointerEvent): { nx: number; ny: number } | null {
  const el = canvasRef.value
  if (!el) return null
  const rect = el.getBoundingClientRect()
  if (rect.width <= 0 || rect.height <= 0) return null
  return {
    nx: ((e.clientX - rect.left) / rect.width) * 2 - 1,
    ny: ((e.clientY - rect.top) / rect.height) * 2 - 1,
  }
}

function onCanvasPointerDown(e: PointerEvent): void {
  if (!renderer || (e.pointerType === 'mouse' && e.button !== 0)) return
  const pt = toCanvasNormalized(e)
  if (!pt) return
  activePointerDown = true
  try {
    ;(e.currentTarget as HTMLElement | null)?.setPointerCapture?.(e.pointerId)
  } catch {
    /* ignore capture errors on synthetic events */
  }
  const zone = renderer.handlePointerDown(pt.nx, pt.ny)
  canvasCursor.value = cursorForZone(zone, true)
}

function onCanvasPointerMove(e: PointerEvent): void {
  if (!renderer) return
  const pt = toCanvasNormalized(e)
  if (!pt) return
  const zone = renderer.setPointer(pt.nx, pt.ny, { isInsideStage: true })
  canvasCursor.value = cursorForZone(zone, activePointerDown)
}

function onCanvasPointerUp(e: PointerEvent): void {
  activePointerDown = false
  renderer?.handlePointerUp()
  try {
    ;(e.currentTarget as HTMLElement | null)?.releasePointerCapture?.(e.pointerId)
  } catch {
    /* ignore */
  }
  const pt = toCanvasNormalized(e)
  canvasCursor.value = pt && renderer ? cursorForZone(renderer.hitTest(pt.nx, pt.ny), false) : ''
}

function onCanvasPointerLeave(): void {
  if (!activePointerDown) {
    canvasCursor.value = ''
  }
}

function onWindowPointerMove(e: PointerEvent): void {
  if (!renderer || !ready.value || activePointerDown) return
  if (e.target === canvasRef.value) return
  const pt = toCanvasNormalized(e)
  if (!pt) return
  renderer.setPointer(pt.nx, pt.ny, { isInsideStage: false })
}

function onWindowPointerUp(): void {
  if (!activePointerDown) return
  activePointerDown = false
  renderer?.handlePointerUp()
  canvasCursor.value = ''
}

function onWindowBlur(): void {
  activePointerDown = false
  canvasCursor.value = ''
  renderer?.setPointerIdle()
}

function onDocumentMouseLeave(): void {
  if (!activePointerDown) {
    renderer?.setPointerIdle()
  }
}

async function buildFromCurrent(): Promise<void> {
  localError.value = ''
  try {
    await inochi.startBuild(sourcePreviewPath.value || undefined)
  } catch (e) {
    localError.value = e instanceof Error ? e.message : String(e)
  }
}

function triggerFilePick(): void {
  fileInputRef.value?.click()
}

async function onFilePicked(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  localError.value = ''
  try {
    await inochi.startBuildFromFile(file)
  } catch (err) {
    localError.value = err instanceof Error ? err.message : String(err)
  }
}

onMounted(() => {
  void inochi.ensureLoaded()
  if (inochi.puppet) {
    ensureRenderer()
    if (renderer) void renderer.loadManifest(inochi.puppet)
  }
  window.addEventListener('pointermove', onWindowPointerMove, { passive: true })
  window.addEventListener('pointerup', onWindowPointerUp)
  window.addEventListener('blur', onWindowBlur)
  document.documentElement.addEventListener('mouseleave', onDocumentMouseLeave)
})

onBeforeUnmount(() => {
  window.removeEventListener('pointermove', onWindowPointerMove)
  window.removeEventListener('pointerup', onWindowPointerUp)
  window.removeEventListener('blur', onWindowBlur)
  document.documentElement.removeEventListener('mouseleave', onDocumentMouseLeave)
  renderer?.destroy()
  renderer = null
})
</script>

<template>
  <div class="puppet-stage">
    <img
      v-if="sourcePreviewUrl"
      :src="sourcePreviewUrl"
      alt=""
      aria-hidden="true"
      class="scene-backdrop"
    />

    <canvas
      v-show="inochi.puppet && ready"
      ref="canvasRef"
      class="puppet-canvas"
      :style="canvasCursor ? { cursor: canvasCursor } : undefined"
      @pointerdown="onCanvasPointerDown"
      @pointermove="onCanvasPointerMove"
      @pointerup="onCanvasPointerUp"
      @pointercancel="onCanvasPointerUp"
      @pointerleave="onCanvasPointerLeave"
    ></canvas>

    <!-- Fallback / initial empty state before decomposition -->
    <div v-if="!inochi.puppet || !ready" class="puppet-empty">
      <img
        v-if="sourcePreviewUrl"
        :src="sourcePreviewUrl"
        :alt="who?.name || 'portrait'"
        class="puppet-preview-img"
      />
      <div v-if="!inochi.puppet && inochi.job.status !== 'running'" class="puppet-cta">
        <div class="puppet-cta-title">{{ t('live2d.emptyTitle') }}</div>
        <p class="puppet-cta-hint">{{ t('live2d.emptyHint') }}</p>
        <div class="puppet-cta-actions">
          <button
            class="sc-gen"
            type="button"
            :disabled="!session.activeCompanionId || !sourcePreviewPath || (inochi.env ? !inochi.env.ready : false)"
            @click.stop="buildFromCurrent"
          >
            <span>{{ t('live2d.buildNow') }}</span>
          </button>
          <button
            class="sc-open"
            type="button"
            :disabled="!session.activeCompanionId || (inochi.env ? !inochi.env.ready : false)"
            @click.stop="triggerFilePick"
          >
            <span>{{ t('live2d.pickImage') }}</span>
          </button>
          <button
            class="sc-open"
            type="button"
            :disabled="!session.activeCompanionId"
            @click.stop="inochi.openEditor('build')"
          >
            <span>{{ t('live2d.multiBuildBtn') }}</span>
          </button>
        </div>
        <div v-if="inochi.env && !inochi.env.ready" class="puppet-env-alert">
          <div class="puppet-env-alert-title">{{ t('live2d.envMissing') }}</div>
          <ul class="puppet-env-list">
            <li v-if="!inochi.env.pythonPath">Python venv: backend/Inochi2D/.venv</li>
            <li v-else-if="inochi.env.missingDeps?.length">
              pip: {{ inochi.env.requirementsPath || 'backend/Inochi2D/requirements.txt' }}
            </li>
            <li v-if="!inochi.env.seethroughPath">Repo: backend/Inochi2D/see-through</li>
            <li v-if="!inochi.env.layerdiffPath">
              LayerDiff3D -> {{ inochi.env.expectedLayerdiffDir }}
            </li>
            <li v-if="!inochi.env.marigoldPath">
              Marigold -> {{ inochi.env.expectedMarigoldDir }}
            </li>
          </ul>
          <div class="puppet-cta-actions">
            <button class="sc-open" type="button" @click.stop="ui.openBackstageTab('paths')">
              {{ t('live2d.openPathSettings') }}
            </button>
            <button class="sc-open" type="button" @click.stop="inochi.refreshEnv()">
              {{ t('live2d.recheckEnv') }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Building overlay -->
    <div v-if="inochi.job.status === 'running'" class="scene-busy puppet-busy">
      <span class="scene-busy-dot"></span>
      <span>{{ stageLabel }}</span>
      <span class="scene-busy-bar">
        <i :style="{ width: Math.max(6, inochi.job.progress) + '%' }"></i>
      </span>
      <button class="scene-cancel" type="button" @click.stop="inochi.cancelBuild()">
        {{ t('picture.cancel') }}
      </button>
    </div>

    <div v-if="localError || inochi.job.error" class="puppet-err-bar">
      {{ localError || inochi.job.error }}
    </div>

    <input
      ref="fileInputRef"
      type="file"
      accept=".png,.jpg,.jpeg,.webp,.bmp"
      style="display: none"
      @change="onFilePicked"
    />
  </div>
</template>

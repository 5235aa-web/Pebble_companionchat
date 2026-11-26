<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { isCharxCard } from '@/data/chat'
import { useUiStore } from '@/stores/ui'
import { useSessionStore } from '@/stores/session'
import { useFileUrl } from '@/composables/useFileUrl'
import { useChatStore } from '@/stores/chat'
import { usePictureStore } from '@/stores/pictures'
import { useCardAudioStore } from '@/stores/cardAudio'
import { useCardHudStore } from '@/stores/cardHud'
import { useScriptStore } from '@/stores/script'
import { useInochi2dStore } from '@/stores/inochi2d'
import { availableCompanionLocales, type CompanionLocale } from '@/utils/companionTranslation'
import CardHudPanel from './CardHudPanel.vue'
import SceneMotionCanvas from './SceneMotionCanvas.vue'
import PuppetStage from './PuppetStage.vue'
import { useI18n } from '@/i18n'

const ui = useUiStore()
const session = useSessionStore()
const chat = useChatStore()
const pictures = usePictureStore()
const cardAudio = useCardAudioStore()
const cardHud = useCardHudStore()
const script = useScriptStore()
const inochi = useInochi2dStore()
const { t } = useI18n()

const inScene = computed(
  () => Boolean(script.playing && script.session?.branch_id === session.activeBranch),
)

const expanded = ref(false)
const who = computed(() => chat.activeCast)
const availableLocales = computed(() => availableCompanionLocales(who.value?.translations))
const activeCardLocale = computed<CompanionLocale>(() => who.value?.activeLocale ?? '')
const isCharx = computed(() => Boolean(who.value && isCharxCard(who.value)))
const showCardHud = computed(
  () =>
    Boolean(who.value) &&
    !inScene.value &&
    ui.view !== 'group' &&
    (isCharx.value || (who.value?.assetCount ?? 0) > 0 || session.companionAssets.length > 0),
)

type Slide = {
  key: string
  url: string
  kind: 'picture' | 'avatar'
}

const MAX_STAGE_ASSET_SLIDES = 24

function stageAssetRank(assetType: string): number {
  const t = assetType.toLowerCase()
  if (t === 'background') return 0
  if (t === 'emotion') return 1
  if (t === 'portrait' || t === 'icon') return 2
  return 3
}

/**
 * The stage is a strip, not a single slot.
 *
 * The character's own portrait is always its last slide. It is the one image
 * that always exists, so a generated picture must never replace it: generating
 * adds a slide in front, and the strip reads "newest picture ... older
 * pictures, portrait". Deleting every picture then leaves the stage exactly as
 * it looked before the feature existed.
 *
 * On massive 2,000+ sprite CHARX cards, keep the manual strip bounded while
 * guaranteeing any active HUD expression override is always present.
 */
const slides = computed<Slide[]>(() => {
  // CHARX cards already bring curated portraits/expressions; keep generated
  // pictures in the album gallery instead of pushing them onto the stage strip.
  const out: Slide[] = isCharx.value
    ? []
    : pictures.doneItems.map((p) => ({
        key: `p${p.id}`,
        url: p.url,
        kind: 'picture',
      }))
  const seenUrls = new Set(out.map((s) => s.url))
  const avatar = who.value?.avatar
  if (avatar && !seenUrls.has(avatar)) {
    out.push({ key: 'avatar', url: avatar, kind: 'avatar' })
    seenUrls.add(avatar)
  }

  const overridePath = cardHud.stageExpressionRelPath
  if (overridePath && !seenUrls.has(overridePath)) {
    const matched = session.imageAssets.find((a) => a.rel_path === overridePath)
    out.push({
      key: matched ? `asset-${matched.id}` : 'hud-override',
      url: overridePath,
      kind: 'avatar',
    })
    seenUrls.add(overridePath)
  }

  const orderedAssets = [...session.imageAssets].sort(
    (a, b) => stageAssetRank(a.asset_type) - stageAssetRank(b.asset_type),
  )
  let addedAssets = 0
  for (const a of orderedAssets) {
    if (addedAssets >= MAX_STAGE_ASSET_SLIDES) break
    if (!a.rel_path || seenUrls.has(a.rel_path)) continue
    out.push({ key: `asset-${a.id}`, url: a.rel_path, kind: 'avatar' })
    seenUrls.add(a.rel_path)
    addedAssets += 1
  }
  return out
})

const slideIndex = ref(0)
const slide = computed(() => slides.value[slideIndex.value] ?? null)
const canSlide = computed(() => slides.value.length > 1)

// Must come after `who` and `slide`: the composable reads it immediately, and
// touching a const before it is initialised throws a TDZ error that takes the
// whole app down at mount time. 0 asks for the original bytes, because the
// scene is the one image drawn large enough for a downscaled copy to look soft.
const sceneUrl = useFileUrl(() => slide.value?.url, 0)
const backdropSceneUrl = useFileUrl(() => cardHud.stageSceneRelPath || undefined, 0)

/**
 * The image actually on screen.
 *
 * `sceneUrl` is empty until the bytes arrive (a portrait is several MB over the
 * socket), so the stage keeps showing the previous picture and swaps only once
 * the next one is ready. Without this the transition would flash a broken image
 * and then pop. `shownKey` drives the cross-fade.
 */
const shownKey = ref('')
const shownUrl = ref('')
const shownKind = ref<'picture' | 'avatar'>('avatar')
watch(
  [slide, sceneUrl],
  ([next, url]) => {
    if (!next || !url) return
    shownKey.value = next.key
    shownUrl.value = url
    shownKind.value = next.kind
  },
  { immediate: true },
)

const shownBackdropKey = ref('')
const shownBackdropUrl = ref('')
watch(
  [() => cardHud.stageSceneRelPath, backdropSceneUrl],
  ([relPath, url]) => {
    if (!relPath) {
      shownBackdropKey.value = ''
      shownBackdropUrl.value = ''
      return
    }
    if (!url) return
    shownBackdropKey.value = relPath
    shownBackdropUrl.value = url
  },
  { immediate: true },
)

const drag = ref(0)
const genError = ref('')
const suppressClick = ref(false)
const dragging = ref(false)
let startX = 0
let pointerId: number | null = null
let captured = false

const motionReady = ref(false)
const isAnimatedSlide = computed(() => /\.gif(?:$|[?#])/i.test(slide.value?.url || ''))
const motionActive = computed(() => ui.sceneMotion && Boolean(shownUrl.value) && !isAnimatedSlide.value)
const motionIntensity = computed(() => (ui.sceneMotionLevel === 'subtle' ? 0.48 : 1))

watch(motionActive, (active) => {
  if (!active) motionReady.value = false
})

const sceneHovering = ref(false)
const scenePointerX = ref(0)
const scenePointerY = ref(0)

function onScenePointerMove(event: PointerEvent) {
  if (!motionActive.value) return
  const el = event.currentTarget as HTMLElement | null
  if (!el) return
  const rect = el.getBoundingClientRect()
  if (rect.width <= 0 || rect.height <= 0) return
  const nx = ((event.clientX - rect.left) / rect.width) * 2 - 1
  const ny = ((event.clientY - rect.top) / rect.height) * 2 - 1
  scenePointerX.value = Math.max(-1, Math.min(1, nx))
  scenePointerY.value = Math.max(-1, Math.min(1, ny))
  sceneHovering.value = true
}

function onScenePointerLeave() {
  sceneHovering.value = false
  scenePointerX.value = 0
  scenePointerY.value = 0
}

const errorText = computed(() => genError.value || pictures.error)
const photoStyle = computed(
  () => ({ '--scene-drag': `${drag.value}px` }) as Record<string, string>,
)
const slideAlt = computed(() =>
  shownKind.value === 'avatar' ? who.value?.name || 'portrait' : t('picture.title'),
)

// A finished picture is the newest thing the user wants to see, so landing on
// it is the whole point of "the new picture replaces what is on screen".
watch(
  () => pictures.doneItems[0]?.id,
  (id, prev) => {
    if (id && id !== prev && !isCharx.value) slideIndex.value = 0
  },
)

// Switching character or branch rebuilds the strip; start from its head.
watch(
  () => [session.activeCompanionId, session.activeBranch],
  () => {
    slideIndex.value = 0
  },
)

// Deleting the picture that happened to be showing must not leave the index
// pointing at nothing: fall back to the last slide, which is the portrait.
watch(
  () => slides.value.length,
  (n) => {
    if (slideIndex.value >= n) slideIndex.value = Math.max(0, n - 1)
  },
)

// When the Stage HUD matches an expression or scene portrait, switch the
// stage slide to that image automatically; clearing returns to main portrait.
// Also watches `slides.length` with `immediate: true` so switching views or
// async asset/picture loading never drops an active override.
watch(
  () => [cardHud.stageExpressionRelPath, cardHud.stageExpressionSeq, slides.value.length] as const,
  ([relPath], prev) => {
    if (!slides.value.length) return
    if (relPath) {
      const idx = slides.value.findIndex((s) => s.url === relPath)
      if (idx >= 0) slideIndex.value = idx
      return
    }
    const wasActive = Boolean(prev?.[0])
    if (wasActive) {
      const avatarIdx = slides.value.findIndex((s) => s.key === 'avatar')
      slideIndex.value = avatarIdx >= 0 ? avatarIdx : 0
    }
  },
  { immediate: true },
)

function goSlide(step: number) {
  const n = slides.value.length
  if (n < 2) return
  slideIndex.value = (slideIndex.value + step + n) % n
}

function onPointerDown(event: PointerEvent) {
  if (!canSlide.value) return
  // The arrows live inside the stage, so a press on one must not start a drag.
  if ((event.target as HTMLElement | null)?.closest('button')) return
  pointerId = event.pointerId
  startX = event.clientX
  dragging.value = false
  drag.value = 0
  captured = false
}

function onPointerMove(event: PointerEvent) {
  if (pointerId === null || event.pointerId !== pointerId) return
  const delta = event.clientX - startX
  if (Math.abs(delta) > 6) {
    if (!captured) {
      // Capture only once a real drag has begun. Capturing on pointerdown made
      // the browser attribute the following click to this container (the common
      // ancestor of the press and the captured release), so tapping an arrow
      // opened the album instead of switching slides.
      ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
      captured = true
    }
    dragging.value = true
  }
  // Resist past the edges so the last picture does not slide into nothing.
  drag.value = delta * 0.45
}

function onPointerUp(event: PointerEvent) {
  if (pointerId === null || event.pointerId !== pointerId) return
  const delta = drag.value
  pointerId = null
  drag.value = 0
  dragging.value = false
  if (captured) {
    captured = false
    try {
      ;(event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId)
    } catch {
      /* the pointer may already be gone; nothing to release */
    }
  }
  if (Math.abs(delta) >= 20) {
    goSlide(delta < 0 ? 1 : -1)
    suppressClick.value = true
    window.setTimeout(() => {
      suppressClick.value = false
    }, 0)
  }
}

function onPhotoClick() {
  // Clicking the live2d canvas interacts with the puppet instead of opening the profile panel.
  if (inochi.activeMode === 'live2d') return
  // A drag ends with a click on the stage; that one must not open anything.
  if (suppressClick.value) return
  ui.openPanel('profile')
}

async function generateNow() {
  genError.value = ''
  try {
    await pictures.generate()
  } catch (e) {
    genError.value = e instanceof Error ? e.message : String(e)
  }
}

/**
 * Sampling progress for the running job, or null when there is nothing to show.
 *
 * Only ComfyUI reports steps; cloud providers have no counter, so they keep the
 * plain spinner instead of a bar stuck at zero.
 */
const busyPercent = computed<number | null>(() => {
  const item = pictures.runningItem
  if (!item || item.provider !== 'comfyui') return null
  return Math.max(0, Math.min(99, item.progress ?? 0))
})

async function cancelGeneration() {
  const item = pictures.runningItem
  if (!item) return
  genError.value = ''
  try {
    await pictures.cancel(item.id)
  } catch (e) {
    genError.value = e instanceof Error ? e.message : String(e)
  }
}

onMounted(() => {
  void pictures.ensureLoaded()
})
</script>

<template>
  <aside
    class="scene"
    :class="{ expanded }"
    :style="showCardHud && cardHud.moodRgb ? { '--scene-mood-rgb': cardHud.moodRgb } : undefined"
    @pointermove="onScenePointerMove"
    @pointerleave="onScenePointerLeave"
  >
    <CardHudPanel v-if="showCardHud" />
    <div
      class="scene-photo"
      :class="{ 'has-picture': !!shownUrl, dragging, 'motion-ready': motionActive && motionReady }"
      :style="photoStyle"
      @click="onPhotoClick"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="onPointerUp"
      @pointerleave="onPointerUp"
    >
      <PuppetStage v-if="inochi.activeMode === 'live2d'" :active-slide-url="slide?.url" />
      <template v-else>
        <div v-if="!shownUrl && !shownBackdropUrl" class="scene-glow"></div>
        <Transition name="scene-backdrop-swap">
          <img
            v-if="shownBackdropUrl"
            :key="'scene-' + shownBackdropKey"
            :src="shownBackdropUrl"
            alt=""
            aria-hidden="true"
            class="scene-backdrop scene-backdrop-live"
          />
          <img
            v-else-if="shownUrl"
            :key="shownKey + '-bg'"
            :src="shownUrl"
            alt=""
            aria-hidden="true"
            class="scene-backdrop"
          />
        </Transition>
        <div v-if="shownBackdropUrl" class="scene-backdrop-scrim" aria-hidden="true"></div>
        <div v-if="showCardHud && cardHud.moodRgb" class="scene-mood-aura" aria-hidden="true"></div>
        <Transition name="scene-swap">
          <img
            v-if="shownUrl"
            :key="shownKey"
            :src="shownUrl"
            :alt="slideAlt"
            class="scene-main"
            :class="{ 'scene-gen': shownKind === 'picture' }"
          />
        </Transition>
        <SceneMotionCanvas
          v-if="motionActive"
          :src="shownUrl"
          :pointer-x="scenePointerX"
          :pointer-y="scenePointerY"
          :hovering="sceneHovering"
          :dragging="dragging"
          :intensity="motionIntensity"
          @ready="motionReady = $event"
        />
        <div v-if="!shownUrl" class="scene-glyph">{{ who?.letter }}</div>

        <button
          v-if="canSlide"
          class="scene-nav prev"
          :aria-label="t('picture.prev')"
          @pointerdown.stop
          @click.stop="goSlide(-1)"
        >
          <svg viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7" /></svg>
        </button>
        <button
          v-if="canSlide"
          class="scene-nav next"
          :aria-label="t('picture.next')"
          @pointerdown.stop
          @click.stop="goSlide(1)"
        >
          <svg viewBox="0 0 24 24"><path d="M9 5l7 7-7 7" /></svg>
        </button>

        <div v-if="pictures.generating" class="scene-busy">
          <span class="scene-busy-dot"></span>
          <span>{{ t('picture.generating') }}</span>
          <span v-if="busyPercent !== null" class="scene-busy-bar">
            <i :style="{ width: busyPercent + '%' }"></i>
          </span>
          <button
            class="scene-cancel"
            type="button"
            :data-tip="t('picture.cancelTip')"
            @click.stop="cancelGeneration"
          >
            {{ t('picture.cancel') }}
          </button>
        </div>
        <div v-else-if="slides.length" class="scene-count">
          {{ slideIndex + 1 }} / {{ slides.length }}
        </div>
      </template>
    </div>

    <div v-if="who" class="scene-info">
      <div class="sc-head-row">
        <div class="sc-name">{{ who?.name }}</div>
        <div
          v-if="availableLocales.length"
          class="sc-lang-switch"
          role="group"
          :data-tip="t('card.localeSwitchTip')"
          data-tip-float
        >
          <button
            type="button"
            class="sc-lang-btn"
            :class="{ on: activeCardLocale === '' }"
            @click="session.setCompanionLocale(who.id, '')"
          >
            {{ t('card.langOriginal') }}
          </button>
          <button
            v-for="loc in availableLocales"
            :key="loc"
            type="button"
            class="sc-lang-btn"
            :class="{ on: activeCardLocale === loc }"
            @click="session.setCompanionLocale(who.id, loc)"
          >
            {{ loc === 'zh' ? t('card.langZh') : t('card.langEn') }}
          </button>
        </div>
      </div>
      <div class="sc-role">{{ who?.role }}</div>
      <p class="sc-desc">{{ who?.desc }}</p>

      <div class="scene-tools">
        <div class="scene-mode-switch" role="group" aria-label="Stage mode">
          <button
            class="scene-mode-btn"
            :class="{ on: inochi.activeMode === 'scene' }"
            type="button"
            @click="inochi.setMode('scene')"
          >
            {{ t('live2d.modeScene') }}
          </button>
          <button
            class="scene-mode-btn"
            :class="{ on: inochi.activeMode === 'live2d' }"
            type="button"
            @click="inochi.setMode('live2d')"
          >
            {{ t('live2d.modeLive2d') }}
          </button>
        </div>

        <button
          v-if="inochi.activeMode === 'live2d'"
          class="sc-open sc-live2d-edit"
          :class="{ active: inochi.editorOpen }"
          type="button"
          @click="inochi.toggleEditor()"
        >
          <span>{{ t('live2d.editParams') }}</span>
        </button>

        <template v-if="inochi.activeMode === 'scene'">
          <button
            class="sc-gen"
            :class="{ busy: pictures.generating }"
            :disabled="pictures.generating"
            @click="generateNow"
          >
            <svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">
              <path d="M4 8h3l1.5-2h7L17 8h3v11H4z" />
              <circle cx="12" cy="13.2" r="3.2" />
            </svg>
            <span>{{ pictures.generating ? t('picture.generating') : t('picture.generate') }}</span>
          </button>
          <button class="sc-open" :aria-label="t('picture.openAlbum')" @click="ui.openPanel('gallery')">
            <svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">
              <rect x="3.5" y="5" width="17" height="14" rx="1.5" />
              <path d="M3.5 15l4.5-4 4 3.5 3-2.5 5.5 4.5" />
              <circle cx="9" cy="9.5" r="1.3" />
            </svg>
            <span>{{ pictures.doneItems.length }}</span>
          </button>
          <button
            class="sc-open sc-motion-toggle"
            :class="{ active: ui.sceneMotion }"
            type="button"
            :aria-pressed="ui.sceneMotion"
            :data-tip="t('scene.motionTip')"
            data-tip-float
            @click="ui.toggleSceneMotion()"
          >
            <span class="sc-motion-dot" aria-hidden="true"></span>
            <span>{{ t('scene.motion') }}</span>
          </button>
          <button
            v-if="ui.sceneMotion"
            class="sc-open sc-motion-level"
            type="button"
            :data-tip="t('scene.motionLevelTip')"
            data-tip-float
            @click="ui.toggleSceneMotionLevel()"
          >
            <span>{{ ui.sceneMotionLevel === 'vivid' ? t('scene.motionVivid') : t('scene.motionSubtle') }}</span>
          </button>
        </template>
      </div>

      <div v-if="cardAudio.bgmPlaying" class="scene-tools">
        <button class="sc-open" type="button" @click="cardAudio.stopBgm()">
          <span>♪ {{ cardAudio.bgmName }} · {{ t('card.assetStop') }}</span>
        </button>
      </div>

      <p v-if="errorText" class="scene-error">{{ errorText }}</p>

      <button class="sc-more" @click="expanded = !expanded">
        <span>{{ t('scene.characterCard') }}</span>
        <svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>
      <div class="sc-card">
        <div class="sc-card-inner">
          <div class="sc-row"><span class="sc-k">{{ t('scene.tags') }}</span><span class="sc-v">{{ who?.tags }}</span></div>
          <div class="sc-row"><span class="sc-k">{{ t('scene.acts') }}</span><span class="sc-v">{{ who?.acts }}</span></div>
          <div class="sc-row"><span class="sc-k">{{ t('scene.lore') }}</span><span class="sc-v">{{ who?.lore }}</span></div>
          <div class="sc-row">
            <span class="sc-k">{{ t('scene.lastSeen') }}</span><span class="sc-v">{{ who?.lastSeen }}</span>
          </div>
          <div class="sc-row"><span class="sc-k">{{ t('scene.branch') }}</span><span class="sc-v">{{ session.branchTitle || t('branch.main') }}</span></div>
        </div>
      </div>
    </div>
    <div v-else class="scene-info">
      <div class="sc-name">{{ t('empty.noCharacter') }}</div>
      <p class="sc-desc">{{ t('empty.noCharacterHint') }}</p>
    </div>
  </aside>
</template>

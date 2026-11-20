<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import AppRail from './components/AppRail.vue'
import MessageThread from './components/MessageThread.vue'
import ChatComposer from './components/ChatComposer.vue'
import ScenePanel from './components/ScenePanel.vue'
import CastStrip from './components/CastStrip.vue'
import CastDrawer from './components/CastDrawer.vue'
import ScriptPanel from './components/ScriptPanel.vue'
import ScriptProgress from './components/ScriptProgress.vue'
import ThreadJumpRail from './components/ThreadJumpRail.vue'
import SceneCopyPanel from './components/SceneCopyPanel.vue'
import LorePanel from './components/LorePanel.vue'
import RulesPanel from './components/RulesPanel.vue'
import MemoryPanel from './components/MemoryPanel.vue'
import BackstagePanel from './components/BackstagePanel.vue'
import CompactPanel from './components/CompactPanel.vue'
import ImageGallery from './components/ImageGallery.vue'
import CharacterEditor from './components/CharacterEditor.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'
import DropOverlay from './components/DropOverlay.vue'
import CallPanel from './components/CallPanel.vue'
import GroupStage from './components/GroupStage.vue'
import { useUiStore } from './stores/ui'
import { useSessionStore } from './stores/session'
import { useChatStore } from './stores/chat'
import { useLlmStore } from './stores/llm'
import { usePictureStore } from './stores/pictures'
import { useUpdaterStore } from './stores/updater'
import { onSocketReady } from './api/client'
import { isDesktop } from './lib/window'

const ui = useUiStore()
const session = useSessionStore()
const chat = useChatStore()
const llm = useLlmStore()
const pictures = usePictureStore()
const updater = useUpdaterStore()

const glowX = ref(-400)
const glowY = ref(-400)
const glowOn = ref(0)
const curtainGone = ref(false)
const overtureGone = ref(false)

let curtainTimer = 0
let overtureTimer = 0

function onMove(e: MouseEvent) {
  glowX.value = e.clientX
  glowY.value = e.clientY
  glowOn.value = 1
}

onMounted(async () => {
  window.addEventListener('mousemove', onMove)

  // Arm the curtain timers before the first request, not after. `.curtain` is a
  // full-screen cover, and it only leaves when these fire: if the initial load
  // threw, the cover would stay up forever and the window would look blank.
  curtainTimer = window.setTimeout(() => (curtainGone.value = true), 1200)
  overtureTimer = window.setTimeout(() => (overtureGone.value = true), 1800)

  await bootstrap()
  // The first attempt almost always loses the race against the sidecar: the
  // webview loads before the backend is listening. Re-run the load on every
  // successful connect so a late start still fills the window.
  stopReady = onSocketReady(() => {
    void bootstrap()
  })
})

let stopReady: (() => void) | null = null
let booting = false

/**
 * Loads everything the window needs. Safe to call repeatedly: every store
 * replaces its collection rather than appending, and concurrent calls are
 * collapsed so a burst of reconnects cannot stack up duplicate requests.
 *
 * The steps are sequential on purpose. loadMessages reads the companion and
 * branch that loadCompanions selects, so running them together would leave the
 * thread reset whenever the cast finished last.
 */
async function bootstrap(): Promise<void> {
  if (booting) return
  booting = true
  try {
    try {
      await session.loadCompanions()
    } catch {
      // The store records the error for the UI; a failure here must not stop
      // the remaining steps from being attempted.
    }
    try {
      await chat.loadMessages()
    } catch {
      // ditto
    }
    try {
      await session.loadTokens(llm.tokenQuery)
    } catch {
      // Token accounting is advisory.
    }
    if (isDesktop() && !updater.hasChecked) {
      void updater.check(true)
    }
  } finally {
    booting = false
  }
}

watch(
  () => [session.activeCompanionId, session.activeBranch],
  async () => {
    await chat.loadMessages()
    await session.loadTokens(llm.tokenQuery)
  },
)

// A reply may have queued a picture on the backend. Poll for a couple of
// minutes afterwards so the stage fills on its own; the store stops as soon as
// nothing is in flight.
watch(
  () => chat.busy,
  (busy) => {
    if (busy) return
    const cfg = pictures.config
    if (!cfg?.enabled || !cfg?.autoChat) return
    void pictures.refresh()
    pictures.kick(90000)
  },
)

onUnmounted(() => {
  window.removeEventListener('mousemove', onMove)
  clearTimeout(curtainTimer)
  clearTimeout(overtureTimer)
  if (stopReady) stopReady()
})
</script>

<template>
  <div class="stage-light" aria-hidden="true"></div>
  <div class="grain" aria-hidden="true"></div>
  <div
    class="cursor-glow"
    aria-hidden="true"
    :style="{
      transform: `translate3d(${glowX}px, ${glowY}px, 0)`,
      opacity: String(glowOn),
    }"
  ></div>
  <div v-if="!curtainGone" class="curtain" aria-hidden="true"></div>
  <div v-if="!overtureGone" class="overture" aria-hidden="true"></div>
  <template v-if="session.activeCompanionId">
    <div :key="`c${session.activeCompanionId}`" class="curtain curtain-flash" aria-hidden="true"></div>
    <div :key="`o${session.activeCompanionId}`" class="overture" aria-hidden="true"></div>
  </template>
  <!-- Switching between the single thread and the group room replays the same
       pair: a quick cover blink plus a light sweep. Re-keying the elements is
       what restarts the animations; no timers involved. -->
  <template v-if="ui.view">
    <div :key="`vc${ui.view}`" class="curtain curtain-flash" aria-hidden="true"></div>
    <div :key="`vo${ui.view}`" class="overture" aria-hidden="true"></div>
  </template>

  <div class="stage" :class="{ 'group-mode': ui.view === 'group' }">
    <AppRail />
    <template v-if="ui.view === 'single'">
      <MessageThread />
      <ChatComposer />
      <ScenePanel />
      <CastStrip />
      <ScriptProgress />
      <ThreadJumpRail />
      <CallPanel />
    </template>
    <GroupStage v-else />
  </div>

  <ImageGallery />
  <CharacterEditor />
  <ConfirmDialog />
  <DropOverlay />

  <div class="scrim" :class="{ show: ui.scrimVisible() }" @click="ui.closeAll()"></div>

  <CastDrawer />
  <LorePanel />
  <RulesPanel />
  <MemoryPanel />
  <BackstagePanel />
  <CompactPanel />
  <ScriptPanel />
  <SceneCopyPanel />
</template>

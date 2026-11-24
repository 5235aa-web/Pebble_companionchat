<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useChatStore } from '@/stores/chat'
import { useUiStore } from '@/stores/ui'
import { useSessionStore } from '@/stores/session'
import { useScriptStore } from '@/stores/script'
import { useCallStore } from '@/stores/call'
import { useI18n } from '@/i18n'
import { clearBranchMessages } from '@/api/companion'
import { fileUrl } from '@/api/client'
import { attachmentToPayload, type PickedAttachment } from '@/api/gallery'
import { blobToWav } from '@/utils/audio'
import {
  allocateChipId,
  revokeOwnedChipUrls,
  useAttachmentQueue,
} from '@/composables/useAttachmentQueue'
import AttachmentChipStrip from './AttachmentChipStrip.vue'

const chat = useChatStore()
const ui = useUiStore()
const session = useSessionStore()
const script = useScriptStore()
const call = useCallStore()
const { t } = useI18n()

const placeholder = computed(() =>
  chat.narrationMode ? t('composer.placeholderNarration') : t('composer.placeholder'),
)

// Inside a playthrough the narration button drives a different path: the cue is
// consumed by the next reply and never replayed as history.
const inScript = computed(
  () => Boolean(script.session && script.session.branch_id === session.activeBranch),
)

const input = ref<HTMLTextAreaElement | null>(null)

const chips = ref<PickedAttachment[]>([])
const { uploading, addFiles, removeChip, clearChips, toggleVideoStt } = useAttachmentQueue(chips)

const attachInput = ref<HTMLInputElement | null>(null)
const dragOver = ref(false)
let dragDepth = 0

const recording = ref(false)
const recTime = ref('00:00')
let recTimer = 0
let recSec = 0
let recorder: MediaRecorder | null = null
let recChunks: Blob[] = []
let recStream: MediaStream | null = null

function autoGrow() {
  const el = input.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 170) + 'px'
}

function doSend() {
  if (chat.busy) {
    chat.stop()
    return
  }
  if (uploading.value) return
  const el = input.value
  if (!el) return
  const val = el.value
  if (!val.trim() && !chips.value.length) return
  chat.send(
    val,
    chips.value.map((c) => attachmentToPayload(c.info)),
  )
  el.value = ''
  clearChips()
  autoGrow()
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    doSend()
  }
}

function pickAttach() {
  attachInput.value?.click()
}

async function onAttachPicked(e: Event) {
  const el = e.target as HTMLInputElement
  const files = Array.from(el.files ?? [])
  el.value = ''
  await addFiles(files)
}

function onPaste(e: ClipboardEvent) {
  const items = Array.from(e.clipboardData?.items ?? [])
  const files = items
    .filter((it) => it.kind === 'file')
    .map((it) => it.getAsFile())
    .filter((f): f is File => Boolean(f))
  if (!files.length) return
  e.preventDefault()
  void addFiles(files)
}

function hasDroppedFiles(e: DragEvent): boolean {
  const dt = e.dataTransfer
  if (!dt) return false
  if (Array.from(dt.types ?? []).includes('Files')) return true
  return Array.from(dt.items ?? []).some((item) => item.kind === 'file')
}

function extractDroppedFiles(dt: DataTransfer | null): File[] {
  if (!dt) return []
  if (dt.files?.length) return Array.from(dt.files)
  if (!dt.items?.length) return []
  const out: File[] = []
  for (const item of Array.from(dt.items)) {
    if (item.kind === 'file') {
      const file = item.getAsFile()
      if (file) out.push(file)
    }
  }
  return out
}

function onDragEnter(e: DragEvent) {
  if (!hasDroppedFiles(e)) return
  e.preventDefault()
  dragDepth += 1
  dragOver.value = true
}

function onDragOver(e: DragEvent) {
  if (!hasDroppedFiles(e)) return
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
  dragOver.value = true
}

function onDragLeave() {
  if (dragDepth === 0) return
  dragDepth -= 1
  if (dragDepth === 0) dragOver.value = false
}

function onDrop(e: DragEvent) {
  dragDepth = 0
  dragOver.value = false
  if (!hasDroppedFiles(e)) return
  e.preventDefault()
  const files = extractDroppedFiles(e.dataTransfer)
  if (files.length) void addFiles(files)
}

function preventWindowFileDrag(e: DragEvent) {
  if (!hasDroppedFiles(e)) return
  e.preventDefault()
}

function resetDragState(e?: DragEvent) {
  if (e && hasDroppedFiles(e)) e.preventDefault()
  dragDepth = 0
  dragOver.value = false
}

/** An image or video quoted from the gallery arrives already stored: no re-upload. */
watch(
  () => ui.quotedImage,
  async (q) => {
    if (!q) return
    ui.quotedImage = null
    if (chips.value.some((c) => c.info.url === q.url)) return
    const isVideo = q.kind === 'video'
    const thumbPath = isVideo ? q.poster_url || '' : q.url
    let src = ''
    if (thumbPath) {
      try {
        src = await fileUrl(thumbPath, 256)
      } catch {
        src = ''
      }
    }
    chips.value.push({
      id: allocateChipId(),
      kind: isVideo ? 'video' : 'photo',
      name: q.name,
      ext: isVideo ? (q.name.split('.').pop() || 'VID').toUpperCase().slice(0, 4) : 'IMG',
      src,
      ownBlobUrl: false,
      sttEnabled: isVideo ? Boolean(q.transcript?.trim()) : undefined,
      cachedTranscript: isVideo ? q.transcript || '' : undefined,
      info: {
        kind: isVideo ? 'video' : 'image',
        name: q.name,
        url: q.url,
        poster_url: q.poster_url,
        duration: q.duration,
        mime: q.mime,
        size: q.size,
        width: q.width,
        height: q.height,
        frames: q.frames,
        transcript: q.transcript,
      },
    })
  },
)

/** The call and the one-shot recorder both own the microphone; only one runs. */
function toggleCall() {
  if (call.active) {
    call.hangup()
    return
  }
  if (recording.value) stopRec()
  void call.start()
}

async function toggleRec() {  if (recording.value) {
    stopRec()
    return
  }
  if (chat.busy) return
  try {
    recStream = await navigator.mediaDevices.getUserMedia({ audio: true })
  } catch (err) {
    ui.askConfirm({
      title: t('composer.voiceInput'),
      text: err instanceof Error ? err.message : String(err),
      onConfirm: () => {},
    })
    return
  }
  recChunks = []
  recorder = new MediaRecorder(recStream)
  recorder.ondataavailable = (ev) => {
    if (ev.data.size > 0) recChunks.push(ev.data)
  }
  recorder.start()
  recording.value = true
  recSec = 0
  recTime.value = '00:00'
  recTimer = window.setInterval(() => {
    recSec++
    recTime.value = '00:' + String(recSec).padStart(2, '0')
  }, 1000)
}

function stopRec() {
  recording.value = false
  clearInterval(recTimer)
  recTimer = 0
  recSec = 0
  recTime.value = '00:00'
  const rec = recorder
  recorder = null
  if (!rec) {
    recStream?.getTracks().forEach((tr) => tr.stop())
    recStream = null
    return
  }
  rec.onstop = async () => {
    recStream?.getTracks().forEach((tr) => tr.stop())
    recStream = null
    const raw = new Blob(recChunks, { type: rec.mimeType || 'audio/webm' })
    recChunks = []
    if (raw.size < 1200) return
    try {
      const wav = await blobToWav(raw)
      chat.sendAudio(wav)
    } catch (err) {
      ui.askConfirm({
        title: t('composer.voiceInput'),
        text: err instanceof Error ? err.message : String(err),
        onConfirm: () => {},
      })
    }
  }
  rec.stop()
}

onMounted(() => {
  window.addEventListener('dragover', preventWindowFileDrag)
  window.addEventListener('drop', resetDragState)
  window.addEventListener('dragend', resetDragState)
})

onUnmounted(() => {
  window.removeEventListener('dragover', preventWindowFileDrag)
  window.removeEventListener('drop', resetDragState)
  window.removeEventListener('dragend', resetDragState)
  revokeOwnedChipUrls(chips.value)
  clearInterval(recTimer)
  recStream?.getTracks().forEach((tr) => tr.stop())
})

function askClear() {
  ui.askConfirm({
    title: t('dialog.clearConversation.title'),
    text: t('dialog.clearConversation.text'),
    onConfirm: async () => {
      const cid = session.activeCompanionId
      const bid = session.activeBranch
      if (cid && bid) {
        try {
          await clearBranchMessages(cid, bid)
        } catch {
          /* fall through to the local reset below */
        }
      }
      chat.reset()
    },
  })
}

function toggleNarration() {
  chat.narrationMode = !chat.narrationMode
  nextTick(() => input.value?.focus())
}
</script>

<template>
  <footer
    class="composer"
    :class="{ recording, dragging: dragOver }"
    @dragenter="onDragEnter"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <button
      class="to-present"
      :class="{ show: chat.showJump }"
      :data-tip="t('thread.jumpToPresent')"
      @click="chat.scrollRequest++"
    >
      <svg viewBox="0 0 24 24"><path d="M12 5v14M6 13l6 6 6-6" /></svg>
    </button>
    <div class="attach-strip">
      <AttachmentChipStrip :chips="chips" @remove="removeChip" @toggle-stt="toggleVideoStt" />
    </div>

    <div class="composer-inner">
      <div v-if="dragOver" class="composer-drop-hint">{{ t('composer.dropHint') }}</div>
      <button class="c-btn danger" :data-tip="t('composer.clear')" @click="askClear">
        <svg viewBox="0 0 24 24">
          <path d="M4 7h16M9 7V5a1 1 0 011-1h4a1 1 0 011 1v2M6 7l1 13a1 1 0 001 1h8a1 1 0 001-1l1-13" />
        </svg>
      </button>
      <input ref="attachInput" type="file" multiple hidden @change="onAttachPicked" />
      <button class="c-btn" :data-tip="t('composer.attach')" @click="pickAttach">
        <svg viewBox="0 0 24 24">
          <rect x="3" y="5" width="18" height="14" rx="1" />
          <circle cx="8.5" cy="10" r="1.6" />
          <path d="M21 16l-5-4-4 3-3-2-6 5" />
        </svg>
      </button>
      <button class="c-btn" :data-tip="t('composer.voiceInput')" @click="toggleRec">
        <svg viewBox="0 0 24 24">
          <rect x="9" y="3" width="6" height="11" rx="3" />
          <path d="M5 11a7 7 0 0014 0M12 18v3" />
        </svg>
      </button>
      <button
        class="c-btn"
        :class="{ active: call.active }"
        :data-tip="t('composer.call')"
        @click="toggleCall"
      >
        <svg viewBox="0 0 24 24">
          <path d="M5 4h4l2 5-2.5 1.5a11 11 0 005 5L15 13l5 2v4a1 1 0 01-1 1A16 16 0 014 5a1 1 0 011-1z" />
        </svg>
      </button>
      <button
        class="c-btn"
        :class="{ active: chat.autoRead }"
        :data-tip="t('composer.autoRead')"
        @click="chat.autoRead = !chat.autoRead"
      >
        <svg viewBox="0 0 24 24">
          <path d="M4 9v6h4l5 4V5L8 9H4z" />
          <path d="M16.5 8.5a5 5 0 010 7" />
          <path d="M19.5 5.5a9 9 0 010 13" />
        </svg>
      </button>

      <textarea
        ref="input"
        id="input"
        name="message"
        aria-label="message"
        rows="1"
        :placeholder="placeholder"
        @input="autoGrow"
        @keydown="onKeydown"
        @paste="onPaste"
      ></textarea>

      <div class="rec">
        <span class="rec-bar" style="animation-delay: 0s"></span>
        <span class="rec-bar" style="animation-delay: 0.1s"></span>
        <span class="rec-bar" style="animation-delay: 0.2s"></span>
        <span class="rec-bar" style="animation-delay: 0.3s"></span>
        <span class="rec-bar" style="animation-delay: 0.15s"></span>
        <span class="rec-bar" style="animation-delay: 0.05s"></span>
        <span class="rec-bar" style="animation-delay: 0.25s"></span>
        <span class="rec-t">{{ recTime }}</span>
      </div>

      <button class="c-btn" :data-tip="t('composer.continue')" @click="chat.continueChat()">
        <svg viewBox="0 0 24 24"><path d="M4 12h11M11 8l4 4-4 4M19 5v14" /></svg>
      </button>
      <button
        class="c-btn"
        :class="{ active: chat.narrationMode, 'narration-script': chat.narrationMode && inScript }"
        :data-tip="t('composer.narration')"
        @click="toggleNarration"
      >
        <svg viewBox="0 0 24 24"><path d="M10 8.5H6v4.5h4v-2c0-1 .3-1.8 1.4-2.5" /><path d="M19 8.5h-4v4.5h4v-2c0-1 .3-1.8 1.4-2.5" /></svg>
      </button>

      <button
        class="send"
        :class="{ busy: chat.busy }"
        :disabled="!chat.busy && uploading"
        :data-tip="chat.busy ? t('composer.stop') : t('composer.send')"
        @click="doSend"
      >
        <svg v-if="!chat.busy" viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6" /></svg>
        <svg v-else viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="1" /></svg>
      </button>
    </div>
  </footer>
</template>

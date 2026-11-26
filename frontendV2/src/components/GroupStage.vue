<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useSessionStore } from '@/stores/session'
import { useUiStore } from '@/stores/ui'
import { llmConfig } from '@/stores/chat'
import { useFileUrls } from '@/composables/useFileUrl'
import { useI18n } from '@/i18n'
import ChatAttachmentPicker from './ChatAttachmentPicker.vue'
import InlineVideoCard from './InlineVideoCard.vue'
import { attachmentToPayload, type PickedAttachment } from '@/api/gallery'
import { isCharxCard, type BeatLine } from '@/data/chat'
import {
  addGroupMember,
  clearGroupMessages,
  createGroupSession,
  deleteGroupSession,
  getGroupSession,
  groupContext,
  groupRerollStream,
  groupStream,
  deleteGroupMessage,
  listGroupSessions,
  patchGroupSession,
  removeGroupMember,
  setGroupFocus,
  type GroupAttachment,
  type GroupDetail,
  type GroupCompaction,
  type GroupMember,
  type GroupMemory,
  type GroupMessage,
  type GroupSession,
} from '@/api/group'

/**
 * The group stage: up to three companions in one room.
 *
 * Layout follows the member count rather than a fixed three: two people get a
 * narrower fan and a gentler tilt. The cards sit behind the text, so the thread
 * reads straight through them instead of competing for a column.
 */
const ROOM_KEY = 'dialogue.groupRoom'
const MAX_MEMBERS = 3

const session = useSessionStore()
const ui = useUiStore()
const { t } = useI18n()

const room = ref<GroupSession | null>(null)
/** Every room on disk, newest first: the left column lists them. */
const rooms = ref<GroupSession[]>([])
const members = ref<GroupMember[]>([])
const messages = ref<GroupMessage[]>([])
/** What the room remembers. The delete prompt names the count before it goes. */
const memories = ref<GroupMemory[]>([])
const draft = ref('')
/** Pictures picked for the next line. Cleared when it is sent. */
const chips = ref<PickedAttachment[]>([])
const picker = ref<InstanceType<typeof ChatAttachmentPicker> | null>(null)
/** On: the next send is a stage direction rather than something the user says. */
const narrationMode = ref(false)
const busy = ref(false)
const error = ref('')
const loading = ref(true)
/** The reply being written right now, kept apart from the stored messages. */
const live = ref<{ speakerId: string; speaker: string; text: string } | null>(null)
/** Explicit next speaker chosen by clicking a card while composing. */
const selectedSpeakerId = ref('')
/** The newest summary of the earlier turns, if the room has been compacted. */
const compaction = ref<GroupCompaction | null>(null)

const composer = ref<HTMLTextAreaElement | null>(null)
/** Bumped on every room change; the curtain element keys off it to replay. */
const roomEpoch = ref(0)

const castById = computed(() => new Map(session.companions.map((c) => [c.id, c])))

/**
 * Portraits are stored paths, not URLs: the shell has no HTTP file access, so
 * every image has to be resolved through the socket. Without this the `<img>`
 * points at a path the webview cannot read and renders as a broken icon.
 */
const memberPortraits = useFileUrls(
  () =>
    members.value
      .map((m) => castById.value.get(m.companion_id)?.avatar || '')
      .filter((a): a is string => Boolean(a)),
  420,
)

const castPortraits = useFileUrls(
  () => session.companions.map((c) => c.avatar).filter((a): a is string => Boolean(a)),
  96,
)

interface CardView {
  key: string
  name: string
  letter: string
  avatar: string
  role: string
  desc: string
  speaking: boolean
  focused: boolean
}

const cards = computed<CardView[]>(() =>
  members.value.map((m) => {
    const cast = castById.value.get(m.companion_id)
    const name = m.name || cast?.name || ''
    const speaking = Boolean(
      live.value &&
        (live.value.speakerId
          ? live.value.speakerId === m.companion_id
          : live.value.speaker === name),
    )
    const focused = Boolean(m.focus) || selectedSpeakerId.value === m.companion_id
    return {
      key: m.companion_id,
      name,
      letter: cast?.letter || (name || '?').slice(0, 1).toUpperCase(),
      avatar: cast?.avatar || '',
      role: cast?.role || '',
      desc: cast?.desc || m.description || '',
      speaking,
      focused,
    }
  }),
)

const selectedSpeaker = computed(
  () => members.value.find((m) => m.companion_id === selectedSpeakerId.value) ?? null,
)

/**
 * Empty seats for a room with nobody in it yet.
 *
 * Without them the stage opens blank — no cards, no frame, and no hint that the
 * fan of three is where characters will appear. Three dashed slots say "this is
 * the table, invite someone" without inventing members that do not exist.
 */
const emptySlots = computed(() => (cards.value.length ? 0 : 3))

const SLOT_LEFT = ['20%', '49%', '78%']
const SLOT_ROT = [-6, 0, 6]

function slotStyle(index: number): Record<string, string> {
  const i = Math.min(index, SLOT_LEFT.length - 1)
  const rot = SLOT_ROT[i]
  const bump = +(13.5 * Math.abs(Math.sin((rot * Math.PI) / 180))).toFixed(2)
  return {
    left: SLOT_LEFT[i],
    transform: `translateX(-50%) translateY(${bump}vw) rotate(${rot}deg)`,
    height: 'calc(27vw * 1.55)',
  }
}

/**
 * Fan geometry. The tilt raises a card's top corner by half a width times
 * sin(angle); the vertical nudge cancels that so every card's highest point
 * lands on the same line.
 *
 * Two-card rooms get a much wider spread than the three-card fan. With two, the
 * reading column sits between them and the veil only has to cover empty space:
 * at 14/86 both cards clear the column entirely on a normal desktop width. The
 * tilt drops to 3 degrees for the same reason — further out, a strong angle
 * only pushes a card back toward the middle.
 */
const layouts = computed(() => {
  const n = cards.value.length
  if (n <= 1) return [{ left: '50%', rot: 0 }]
  if (n === 2) {
    return [
      { left: '14%', rot: -3 },
      { left: '86%', rot: 3 },
    ]
  }
  return [
    { left: '20%', rot: -6 },
    { left: '49%', rot: 0 },
    { left: '78%', rot: 6 },
  ]
})

function cardStyle(index: number): Record<string, string> {
  const p = layouts.value[index] ?? layouts.value[0]
  const bump = +(13.5 * Math.abs(Math.sin((p.rot * Math.PI) / 180))).toFixed(2)
  return {
    left: p.left,
    transform: `translateX(-50%) translateY(${bump}vw) rotate(${p.rot}deg)`,
    height: 'calc(27vw * 1.55)',
  }
}

/** Which line was just copied, so its button can show a tick for a moment. */
const copiedId = ref(0)

/**
 * The clock face for a stored line.
 *
 * SQLite's CURRENT_TIMESTAMP is UTC and carries no zone marker, so it reads
 * back as local time unless the zone is stated. Appending `Z` makes the browser
 * convert it, which is what a user expects from a timestamp.
 */
function clockOf(stamp?: string): string {
  if (!stamp) return ''
  const iso = stamp.includes('T') ? stamp : stamp.replace(' ', 'T')
  const d = new Date(iso.endsWith('Z') ? iso : `${iso}Z`)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

async function copyMessage(m: GroupMessage): Promise<void> {
  try {
    await navigator.clipboard?.writeText(m.content)
    copiedId.value = m.id
    window.setTimeout(() => {
      if (copiedId.value === m.id) copiedId.value = 0
    }, 1200)
  } catch {
    /* the clipboard can be denied; there is nothing useful to show for it */
  }
}

async function removeMessage(m: GroupMessage): Promise<void> {
  const roomId = room.value?.id
  if (!roomId) return
  const previous = messages.value
  // Optimistic: the line leaves at once and returns if the request fails, so a
  // slow round trip does not look like a broken button.
  messages.value = messages.value.filter((x) => x.id !== m.id)
  try {
    await deleteGroupMessage(roomId, m.id)
  } catch (e) {
    messages.value = previous
    error.value = e instanceof Error ? e.message : String(e)
  }
}

/** A message's own line breaks, with bracketed lines treated as action. */
/**
 * Messages the summary has not already covered.
 *
 * The room pays for the summary once. Without this cut the transcript stays on
 * screen in full and the reader sees a summary of text that is still right
 * there under it — and the context meter never drops.
 */
const visibleMessages = computed(() => {
  const boundary = Number(compaction.value?.messages_compacted ?? 0)
  if (!boundary) return messages.value
  // Negative ids are the optimistic lines pushed on send — not stored yet, so
  // they cannot be "covered by the summary". Comparing them against a positive
  // boundary hid the user's own message until the server echoed it back, which
  // read as a flash right after the reply finished.
  return messages.value.filter((m) => m.id < 0 || m.id > boundary)
})

const summaryOpen = ref(false)

/** Pictures a stored line carries. They never reach the model. */
function imagesOf(m: GroupMessage): GroupAttachment[] {
  return (m.attachments ?? []).filter((a) => a.kind === 'image' && !!a.url)
}

function videosOf(m: GroupMessage): GroupAttachment[] {
  return (m.attachments ?? []).filter((a) => a.kind === 'video' && !!a.url)
}

const threadShots = useFileUrls(
  () => messages.value.flatMap((m) => imagesOf(m).map((a) => String(a.url))),
  0,
)

function shotUrl(url: string): string {
  return threadShots.value[url] || ''
}

/** Open the album on this room's page, positioned on the clicked picture. */
function openShot(url: string): void {
  ui.galleryTarget = url
  ui.openPanel('gallery')
}

/**
 * Parsed lines, cached per (kind, content).
 *
 * The template calls this for every message on every render, and a streaming
 * delta re-renders the list. Without the cache a sixty-message room re-splits
 * all sixty lines per frame. Bounded so a long session cannot grow it forever.
 */
const lineCache = new Map<string, BeatLine[]>()
const LINE_CACHE_MAX = 400

function linesOf(content: string, kind = 'chat'): BeatLine[] {
  const key = `${kind}\u0000${content}`
  const hit = lineCache.get(key)
  if (hit) return hit
  const out: BeatLine[] =
    kind === 'narration'
      ? [{ kind: 'direction', text: content }]
      : content
          .split('\n')
          .map((l) => l.trim())
          .filter(Boolean)
          .map((l) => ({ kind: l.startsWith('(') ? 'direction' : 'say', text: l }))
  lineCache.set(key, out)
  if (lineCache.size > LINE_CACHE_MAX) {
    const oldest = lineCache.keys().next().value
    if (oldest !== undefined) lineCache.delete(oldest)
  }
  return out
}

function idOf(m: GroupMessage): number {
  return m.id
}

async function apply(detail: GroupDetail, toBottom = false): Promise<void> {
  room.value = detail.session
  members.value = detail.members ?? []
  if (
    selectedSpeakerId.value &&
    !members.value.some((m) => m.companion_id === selectedSpeakerId.value)
  ) {
    selectedSpeakerId.value = ''
  }
  messages.value = detail.messages ?? []
  memories.value = detail.memories ?? []
  compaction.value = detail.compaction ?? null
  ui.setGroupRoom(detail.session.id)
  // Dots describe one reply; switching rooms means they no longer describe
  // anything the user is looking at.
  ui.setGroupInPlay([])
  await nextTick()
  // Only when the room itself changes. A reload after a reply must leave the
  // reader where they are — they may have scrolled up to re-read a line, and
  // yanking them back to the bottom is exactly what that breaks.
  if (toBottom) {
    scrollToEnd()
    // A second pass on the next frame: the thread is measured before the cards
    // and the composer settle, and a scrollHeight read too early is the height
    // of a shorter thread — which is how a room opens a thousand pixels short
    // of its own bottom.
    await nextTick()
    scrollToEnd()
  }
  syncScrollState()
  void refreshContext()
}

/**
 * Report what the next turn will cost. The rail shows it in place of the
 * companion's numbers while the group view is open; both describe "how much of
 * the window is in use", they just count different transcripts.
 */
async function refreshContext(): Promise<void> {
  if (!room.value) {
    ui.setGroupTokens(0)
    return
  }
  const cfg = llmConfig()
  try {
    const c = await groupContext(room.value.id, cfg.model || '', cfg.provider_type || '')
    ui.setGroupTokens(c.tokens || 0)
  } catch {
    /* advisory: a failed count must not disturb the room */
  }
}

async function openRoom(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const stored = Number(localStorage.getItem(ROOM_KEY) || 0)
    if (stored) {
      try {
        await apply(await getGroupSession(stored), true)
        return
      } catch {
        // The room was deleted on another view; fall through and make a new one.
      }
    }
    const { session: created } = await createGroupSession({ name: 'New room', scene: '' })
    localStorage.setItem(ROOM_KEY, String(created.id))
    await apply({ session: created, members: [], messages: [], memories: [], compaction: null }, true)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
    // After `loading` drops, not inside apply(): while the flag is up the
    // transcript renders as a one-line placeholder, so the height measured then
    // belongs to that placeholder. Scrolling there and then letting the real
    // messages render is how a room opens at its top instead of its bottom.
    await nextTick()
    scrollToEnd()
  }
}

async function reload(): Promise<void> {
  if (!room.value) return
  try {
    await apply(await getGroupSession(room.value.id))
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

/* ---- mentioning (@ to address members in this room) ---- */

const mentionOpen = ref(false)
const mentionQuery = ref('')
const mentionIndex = ref(0)
const mentionStart = ref(-1)

/** Present room members filtered by what was typed after `@`. */
const mentionCandidates = computed(() => {
  const q = mentionQuery.value.trim().toLowerCase()
  return members.value.filter((m) => {
    const cast = castById.value.get(m.companion_id)
    const name = (m.name || cast?.name || '').toLowerCase()
    const role = (cast?.role || '').toLowerCase()
    return !q || name.includes(q) || role.includes(q)
  })
})

/* ---- slash commands (/invite, /remove) & invite drawer ---- */

const inviteOpen = ref(false)
const slashIndex = ref(0)

const SLASH_COMMANDS = [
  { cmd: '/invite', hint: 'bring a character into the room' },
  { cmd: '/remove', hint: 'take a character out of the room' },
] as const

const parsedSlash = computed(() => {
  const v = draft.value.trimStart()
  if (!v.startsWith('/')) return null
  const match = v.match(/^\/([a-z]*)(?:\s+(.*))?$/i)
  if (!match) return null
  const cmd = match[1].toLowerCase()
  const hasSpace = match[2] !== undefined
  const arg = (match[2] || '').trim().toLowerCase()
  if (cmd === 'invite') return { stage: 'invite' as const, arg }
  if (cmd === 'remove') return { stage: 'members' as const, arg }
  if (!hasSpace) {
    const matches = SLASH_COMMANDS.filter((c) => c.cmd.startsWith(`/${cmd}`))
    if (matches.length) return { stage: 'commands' as const, arg: `/${cmd}` }
  }
  return null
})

const slashStage = computed<'commands' | 'invite' | 'members' | null>(
  () => parsedSlash.value?.stage ?? null,
)

const filteredCommands = computed(() => {
  const prefix = parsedSlash.value?.stage === 'commands' ? parsedSlash.value.arg : '/'
  return SLASH_COMMANDS.filter((c) => c.cmd.startsWith(prefix))
})

/** Eligible cast members (excluding charx cards and those already in room), filtered by `/invite <name>`. */
const inviteCandidates = computed(() => {
  const joined = new Set(members.value.map((m) => m.companion_id))
  const q = parsedSlash.value?.stage === 'invite' ? parsedSlash.value.arg : ''
  return session.companions
    .filter((c) => !joined.has(c.id) && !isCharxCard(c))
    .filter(
      (c) =>
        !q ||
        c.name.toLowerCase().includes(q) ||
        (c.role || '').toLowerCase().includes(q),
    )
})

/** Present members filtered by optional `/remove <name>`. */
const removeCandidates = computed(() => {
  const q = parsedSlash.value?.stage === 'members' ? parsedSlash.value.arg : ''
  return members.value.filter((m) => {
    if (!q) return true
    const cast = castById.value.get(m.companion_id)
    return (
      m.name.toLowerCase().includes(q) ||
      (cast?.role || '').toLowerCase().includes(q)
    )
  })
})

watch([slashStage, inviteOpen, () => parsedSlash.value?.arg], () => {
  slashIndex.value = 0
})

/**
 * Grow the draft box with its content, up to a ceiling.
 *
 * `height = 'auto'` first: scrollHeight reports the height the content needs
 * only when the box is not already stretched, otherwise it never shrinks back
 * after a line is deleted. Same shape as the single-thread composer, same 170px
 * cap — past that the box scrolls instead of eating the transcript.
 */
function autoGrow(): void {
  const el = composer.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 170)}px`
}

function onDraftInput(): void {
  if (inviteOpen.value && draft.value.trim()) inviteOpen.value = false
  detectMention()
  autoGrow()
}

function detectMention(): void {
  const el = composer.value
  if (!el) return
  const caret = el.selectionStart ?? draft.value.length
  const before = draft.value.slice(0, caret)
  const match = before.match(/(?:^|[^A-Za-z0-9._%+-])@([^\s@]*)$/)
  if (match) {
    mentionOpen.value = true
    mentionStart.value = before.lastIndexOf('@')
    mentionQuery.value = match[1]
    mentionIndex.value = 0
  } else {
    mentionOpen.value = false
  }
}

function insertMention(member: GroupMember): void {
  const el = composer.value
  const caret = el?.selectionStart ?? draft.value.length
  const start =
    mentionStart.value >= 0 ? mentionStart.value : draft.value.lastIndexOf('@', caret)
  if (start < 0) return
  const name = member.name || castById.value.get(member.companion_id)?.name || ''
  if (!name) return
  const head = draft.value.slice(0, start)
  const tail = draft.value.slice(caret).replace(/^\s*/, '')
  const inserted = `@${name} `
  draft.value = head + inserted + tail
  mentionOpen.value = false
  mentionQuery.value = ''
  mentionStart.value = -1
  nextTick(() => {
    autoGrow()
    if (el) {
      const pos = (head + inserted).length
      el.focus()
      el.setSelectionRange(pos, pos)
    }
  })
}

async function invite(companionId: string): Promise<void> {
  if (!room.value) return
  const target = castById.value.get(companionId)
  if (target && isCharxCard(target)) {
    inviteOpen.value = false
    return
  }
  if (members.value.length >= MAX_MEMBERS) {
    error.value = `A room holds at most ${MAX_MEMBERS} characters.`
    inviteOpen.value = false
    return
  }
  try {
    await addGroupMember(room.value.id, companionId)
    inviteOpen.value = false
    if (slashStage.value === 'invite') {
      draft.value = ''
      nextTick(() => autoGrow())
    }
    await reload()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

function applyOptimisticFocus(companionId: string): void {
  for (const m of members.value) {
    m.focus = m.companion_id === companionId ? 1 : 0
  }
}

async function onCardClick(companionId: string): Promise<void> {
  if (!room.value || busy.value) return
  const hasDraft = Boolean(draft.value.trim() || chips.value.length)
  if (!hasDraft) {
    applyOptimisticFocus(companionId)
    void setGroupFocus(room.value.id, companionId).catch(() => {})
    promptSpeaker(companionId)
    return
  }
  if (selectedSpeakerId.value === companionId) {
    selectedSpeakerId.value = ''
  } else {
    selectedSpeakerId.value = companionId
    applyOptimisticFocus(companionId)
    void setGroupFocus(room.value.id, companionId).catch(() => {})
  }
  nextTick(() => composer.value?.focus())
}

/**
 * Clicking an empty seat or invite action opens the cast invite picker
 * without clobbering any message draft.
 */
function openInvitePicker(): void {
  if (members.value.length >= MAX_MEMBERS) return
  mentionOpen.value = false
  inviteOpen.value = !inviteOpen.value
  slashIndex.value = 0
}

function pickSlashCommand(cmd: string): void {
  draft.value = cmd
  inviteOpen.value = false
  nextTick(() => {
    autoGrow()
    composer.value?.focus()
  })
}

async function removeViaSlash(companionId: string): Promise<void> {
  if (!room.value) return
  try {
    await removeGroupMember(room.value.id, companionId)
    if (selectedSpeakerId.value === companionId) selectedSpeakerId.value = ''
    draft.value = ''
    nextTick(() => autoGrow())
    await reload()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

/** Portrait and initial for a member, resolved once for both pickers. */
function portraitOf(companionId: string): string {
  const cast = castById.value.get(companionId)
  return cast?.avatar ? castPortraits.value[cast.avatar] || '' : ''
}

function letterOf(companionId: string, name: string): string {
  const cast = castById.value.get(companionId)
  return cast?.letter || (name || '?').slice(0, 1).toUpperCase()
}

/* ---- sending ---- */

/* ---- streaming throttle ---- */

/**
 * One update per frame.
 *
 * A local model emits deltas faster than the browser paints; appending each one
 * straight to the reactive object re-renders the transcript dozens of times a
 * second for no visible gain. The same rAF throttle the single thread uses on
 * its scroll handler applies here to the text itself.
 */
let deltaBuffer = ''
let deltaRaf = 0
let liveSpeaker = ''
let liveSpeakerId = ''
let activeStream: AbortController | null = null

function flushDelta(): void {
  deltaRaf = 0
  if (!deltaBuffer) return
  const buffered = deltaBuffer
  deltaBuffer = ''
  if (!live.value) live.value = { speakerId: liveSpeakerId, speaker: liveSpeaker, text: '' }
  live.value.text += buffered
}

function pushDelta(text: string): void {
  deltaBuffer += text
  if (!deltaRaf) deltaRaf = requestAnimationFrame(flushDelta)
}

function clearDelta(): void {
  if (deltaRaf) cancelAnimationFrame(deltaRaf)
  deltaRaf = 0
  deltaBuffer = ''
}

/* ---- scroll: follow the stream, respect the reader ---- */

const scroller = ref<HTMLElement | null>(null)
const thread = ref<HTMLElement | null>(null)
const hoveredCardId = ref('')
const hoveredCardPos = ref<{ x: number; y: number } | null>(null)
const cardTipEl = ref<HTMLElement | null>(null)
const showJump = ref(false)
/** Whether the view should keep tracking the bottom as text arrives. */
let followBottom = true
let scrollRaf = 0
const FOLLOW_SLACK = 120

const hoveredCardTip = computed(() => {
  if (!hoveredCardId.value) return ''
  const card = cards.value.find((c) => c.key === hoveredCardId.value)
  if (!card) return ''
  return draft.value.trim() || chips.value.length
    ? t('group.cardSelectTip', { name: card.name })
    : t('group.cardSpeakTip', { name: card.name })
})

const cardTipStyle = computed(() => {
  const pos = hoveredCardPos.value
  if (!pos || !hoveredCardTip.value) return {}
  const tipW = cardTipEl.value?.offsetWidth ?? 220
  const tipH = cardTipEl.value?.offsetHeight ?? 30
  const margin = 8
  const left = Math.max(margin, Math.min(pos.x - tipW / 2, window.innerWidth - tipW - margin))
  let top = pos.y - tipH - 14
  if (top < margin) top = Math.min(pos.y + 18, window.innerHeight - tipH - margin)
  return { left: `${Math.round(left)}px`, top: `${Math.round(top)}px` }
})

/**
 * `.gc-scroll` covers the whole stage above the composer so wheel scrolling and
 * the right-edge scrollbar work anywhere, which places it in front of `.gc-hand`.
 * Only hit-test down to `.gc-card` in the exposed stage margins to the left and
 * right of `.gc-thread` so text selection, spacing clicks, and the middle card
 * behind the reading column never intercept thread interactions.
 */
const STAGE_CLICK_DRAG_PX = 5
let stageDown = { x: 0, y: 0, allowCard: false, hadSelection: false }

function isInsideThreadColumn(clientX: number): boolean {
  const el = thread.value
  if (!el) return false
  const rect = el.getBoundingClientRect()
  return clientX >= rect.left && clientX <= rect.right
}

function canHitStageCard(target: EventTarget | null, clientX: number): boolean {
  const el = scroller.value
  if (!el || target !== el) return false
  const rect = el.getBoundingClientRect()
  if (clientX > rect.left + el.clientWidth) return false
  return !isInsideThreadColumn(clientX)
}

function findCardAt(clientX: number, clientY: number): string {
  const hits = document.elementsFromPoint(clientX, clientY)
  for (const el of hits) {
    if (!(el instanceof HTMLElement)) continue
    const card = el.closest('.gc-card[data-card-key]') as HTMLElement | null
    if (card?.dataset.cardKey) return card.dataset.cardKey
  }
  return ''
}

function onStageMouseDown(e: MouseEvent): void {
  stageDown = {
    x: e.clientX,
    y: e.clientY,
    allowCard:
      e.button === 0 &&
      cards.value.length > 0 &&
      canHitStageCard(e.target, e.clientX),
    hadSelection: Boolean(window.getSelection()?.toString()),
  }
}

function onStageMouseMove(e: MouseEvent): void {
  if (!cards.value.length || !canHitStageCard(e.target, e.clientX)) {
    if (hoveredCardId.value) hoveredCardId.value = ''
    hoveredCardPos.value = null
    return
  }
  const id = findCardAt(e.clientX, e.clientY)
  hoveredCardId.value = id
  hoveredCardPos.value = id ? { x: e.clientX, y: e.clientY } : null
}

function onStageClick(e: MouseEvent): void {
  if (!cards.value.length || !canHitStageCard(e.target, e.clientX)) return
  if (!stageDown.allowCard || stageDown.hadSelection) return
  if (Math.hypot(e.clientX - stageDown.x, e.clientY - stageDown.y) > STAGE_CLICK_DRAG_PX) return
  if (window.getSelection()?.toString()) return
  const key = findCardAt(e.clientX, e.clientY)
  if (key) void onCardClick(key)
}

/**
 * The scroll container.
 *
 * Prefers the template ref, but falls back to a query: if that binding is ever
 * missed, every helper below becomes a silent no-op, and a silent no-op is
 * exactly what makes a control look broken while the DOM is perfectly fine.
 */
function getScroller(): HTMLElement | null {
  return scroller.value ?? (document.querySelector('.gc-scroll') as HTMLElement | null)
}

function gapFromBottom(): number {
  const el = getScroller()
  if (!el) return 0
  return el.scrollHeight - el.scrollTop - el.clientHeight
}

function scrollToEnd(smooth = false): void {
  const el = getScroller()
  if (!el) return
  if (smooth) {
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  } else {
    // Assign scrollTop directly: the stylesheet no longer sets
    // scroll-behavior:smooth, so this lands immediately instead of animating
    // toward a target that keeps moving as the reply grows.
    el.scrollTop = el.scrollHeight
  }
  followBottom = true
  syncScrollState()
}

/**
 * One measurement per frame.
 *
 * `scrollHeight` forces a layout flush, so reading it on every scroll event
 * during a fast wheel gesture is the classic thrash. The slack matters too: a
 * growing reply pushes the bottom away by a few pixels every frame, and that
 * must not read as "the user scrolled up and wants to stay there".
 */
/** One measurement, no throttle. Also called wherever the view moves without a
 *  scroll event — opening a room, or a reload — otherwise the button stays
 *  hidden while the reader is a thousand pixels up. */
function syncScrollState(): void {
  const gap = gapFromBottom()
  showJump.value = gap > FOLLOW_SLACK
  followBottom = gap <= FOLLOW_SLACK
}

function onScroll(): void {
  // Read synchronously, not inside the rAF. A streaming delta lands on the very
  // next frame, and if it still sees the old "following" flag it scrolls the
  // view back down before the throttled read notices the wheel — the reader then
  // has to fight the view for several scrolls to get away from the bottom.
  followBottom = gapFromBottom() <= FOLLOW_SLACK
  if (scrollRaf) return
  scrollRaf = requestAnimationFrame(() => {
    scrollRaf = 0
    showJump.value = gapFromBottom() > FOLLOW_SLACK
  })
}

watch(
  () => live.value?.text ?? '',
  async () => {
    if (!followBottom) return
    await nextTick()
    scrollToEnd()
  },
)

watch(
  () => messages.value.length,
  async (next, prev) => {
    // Only growth, and only while the reader is following. A reload swaps the
    // whole array — sometimes to a different length — and that must never move
    // the view; neither must a reply arriving while someone is reading back.
    if (next <= prev || !followBottom) return
    await nextTick()
    scrollToEnd()
  },
)

const RELAY_KEY = 'dialogue.groupRelay'
const savedRelay = Number(localStorage.getItem(RELAY_KEY) || 0)
const relayRounds = ref<0 | 1 | 2>(savedRelay === 1 || savedRelay === 2 ? savedRelay : 0)
const relayRemaining = ref(0)
let relayTimer = 0
let abortedByUser = false

function cycleRelay(): void {
  const next = ((relayRounds.value + 1) % 3) as 0 | 1 | 2
  relayRounds.value = next
  localStorage.setItem(RELAY_KEY, String(next))
}

const lastVisibleId = computed(() => {
  const list = visibleMessages.value
  return list.length ? list[list.length - 1].id : 0
})

const rerollMenuId = ref(0)

function otherMembersOf(speakerId: string | null): GroupMember[] {
  return members.value.filter((m) => m.companion_id !== (speakerId || ''))
}

onUnmounted(() => {
  if (scrollRaf) cancelAnimationFrame(scrollRaf)
  if (deltaRaf) cancelAnimationFrame(deltaRaf)
  if (relayTimer) clearTimeout(relayTimer)
  activeStream?.abort()
  activeStream = null
})

// The album can delete one of this room's pictures while the transcript still
// holds the old attachment list. Reload so the thumbnail goes with it.
watch(
  () => ui.groupEpoch,
  () => {
    if (room.value) void reload()
  },
)

function cancelRelay(): void {
  if (relayTimer) {
    clearTimeout(relayTimer)
    relayTimer = 0
  }
  relayRemaining.value = 0
}

function stopStream(): void {
  abortedByUser = true
  cancelRelay()
  if (!activeStream) {
    busy.value = false
    live.value = null
    return
  }
  activeStream.abort()
  activeStream = null
  clearDelta()
  busy.value = false
  live.value = null
  void reload()
}

function makeTurnHandlers(opts: {
  onSpeaker?: (chunk: { user_message?: { id: number } }) => void
  onDone?: (chunk: { user_message_id?: number }) => void
  onRollback?: () => void
  allowRelay?: boolean
}) {
  let turnFailed = false
  return {
    onChunk: (chunk: import('@/api/stream').ChatChunk) => {
      if (chunk.type === 'speaker') {
        liveSpeaker = chunk.speaker?.name ?? ''
        liveSpeakerId = chunk.speaker?.companion_id ?? ''
        opts.onSpeaker?.(chunk)
        clearDelta()
        live.value = { speakerId: liveSpeakerId, speaker: liveSpeaker, text: '' }
      } else if (chunk.type === 'content') {
        pushDelta(chunk.content ?? '')
      } else if (chunk.type === 'error') {
        turnFailed = true
        cancelRelay()
        clearDelta()
        opts.onRollback?.()
        // Busy room, model failure, or an empty answer — all arrive here and
        // all have to be visible, otherwise the turn looks like it silently died.
        error.value = chunk.message ?? 'The turn failed.'
        live.value = null
      } else if (chunk.type === 'done') {
        clearDelta()
        // Which memories the room was thinking of — the rail draws them.
        ui.setGroupInPlay(chunk.memories ?? [])
        opts.onDone?.(chunk)
        if (chunk.message) {
          const stored = chunk.message as unknown as GroupMessage
          if (live.value) live.value = null
          messages.value.push(stored)
        }
      }
    },
    onError: (e: Error) => {
      turnFailed = true
      cancelRelay()
      clearDelta()
      opts.onRollback?.()
      error.value = e.message
      busy.value = false
      live.value = null
      activeStream = null
    },
    onComplete: () => {
      clearDelta()
      busy.value = false
      live.value = null
      activeStream = null
      const shouldRelay =
        Boolean(opts.allowRelay) &&
        relayRemaining.value > 0 &&
        !turnFailed &&
        !abortedByUser &&
        members.value.length > 1 &&
        Boolean(room.value)
      void reload().then(() => {
        if (shouldRelay && !busy.value && !abortedByUser) {
          relayRemaining.value -= 1
          relayTimer = window.setTimeout(() => {
            relayTimer = 0
            if (!busy.value && !abortedByUser) {
              runTurn({ text: '', allowRelay: true })
            }
          }, 220)
        }
      })
    },
  }
}

function runTurn(opts: {
  text: string
  narrating?: boolean
  picked?: PickedAttachment[]
  speakerId?: string
  restoreDraftOnError?: string
  allowRelay?: boolean
}): void {
  if (!room.value || busy.value) return
  if (!members.value.length) {
    error.value = 'Invite someone to the room first.'
    cancelRelay()
    return
  }
  const roomId = room.value.id
  const narrating = Boolean(opts.narrating)
  const picked = narrating ? [] : (opts.picked ?? [])
  const text = opts.text
  const attachments = picked.map((c) => attachmentToPayload(c.info))

  const hasContent = Boolean(text || attachments.length)
  const optimistic: GroupMessage | null = hasContent
    ? {
        id: -Date.now(),
        session_id: roomId,
        speaker_id: narrating ? 'narrator' : 'user',
        speaker_name: narrating ? 'Narration' : 'You',
        role: 'user',
        kind: narrating ? 'narration' : 'chat',
        content: text,
        attachments,
      }
    : null

  if (optimistic) {
    messages.value.push(optimistic)
    void nextTick(() => composer.value?.scrollIntoView({ block: 'end' }))
  }

  let persistedUserMsg = false
  rerollMenuId.value = 0
  busy.value = true
  error.value = ''
  liveSpeaker = ''
  liveSpeakerId = ''
  live.value = null

  const rollbackOptimistic = () => {
    if (optimistic && !persistedUserMsg && optimistic.id < 0) {
      messages.value = messages.value.filter((m) => m.id !== optimistic.id)
      if (opts.restoreDraftOnError && !draft.value) {
        draft.value = opts.restoreDraftOnError
        nextTick(() => autoGrow())
      }
    }
  }

  activeStream = groupStream(
    roomId,
    text,
    llmConfig(),
    makeTurnHandlers({
      allowRelay: opts.allowRelay,
      onSpeaker: (chunk) => {
        const stored = chunk.user_message
        if (!stored) return
        if (optimistic) {
          persistedUserMsg = true
          optimistic.id = Number(stored.id)
          return
        }
        // A card click carries no draft, so the line the server stored has no
        // optimistic twin. Push it now, or the room starts the reply before it
        // shows who was called on.
        messages.value.push(stored as unknown as GroupMessage)
      },
      onDone: (chunk) => {
        if (optimistic && chunk.user_message_id && optimistic.id < 0) {
          persistedUserMsg = true
          optimistic.id = chunk.user_message_id
        }
      },
      onRollback: rollbackOptimistic,
    }),
    narrating ? 'narrator' : 'user',
    attachments,
    opts.speakerId,
  )
}

function rerollLast(messageId: number, speakerId?: string): void {
  if (!room.value || busy.value) return
  const roomId = room.value.id
  const idx = messages.value.findIndex((m) => m.id === messageId)
  if (idx < 0) return
  const removed = messages.value[idx]
  messages.value = messages.value.filter((m) => m.id !== messageId)

  abortedByUser = false
  cancelRelay()
  rerollMenuId.value = 0
  busy.value = true
  error.value = ''
  liveSpeaker = ''
  liveSpeakerId = ''
  live.value = null

  let replaced = false
  activeStream = groupRerollStream(
    roomId,
    messageId,
    llmConfig(),
    makeTurnHandlers({
      onSpeaker: () => {
        replaced = true
      },
      onRollback: () => {
        if (!replaced && !messages.value.some((m) => m.id === removed.id)) {
          messages.value = [...messages.value, removed]
        }
      },
    }),
    { speakerId },
  )
}

function send(): void {
  if (busy.value || relayRemaining.value > 0) {
    stopStream()
    return
  }
  // A half-typed command is not a message: Enter must not post "/remove" as
  // dialogue while the picker is still open.
  if (slashStage.value) return
  const narrating = narrationMode.value
  if (picker.value?.uploading) return
  // A stage direction carries text only: the backend stores no attachments for
  // one, so a picture picked while the mode is on stays in the tray.
  const picked = narrating ? [] : chips.value
  const rawDraft = draft.value
  const text = rawDraft.trim()
  if ((!text && !picked.length) || !room.value) return

  const speakerId = selectedSpeakerId.value || undefined
  draft.value = ''
  if (!narrating) chips.value = []
  mentionOpen.value = false
  inviteOpen.value = false
  selectedSpeakerId.value = ''
  // The box was grown for the old text; without this the empty draft keeps the
  // tall height until the next keystroke.
  nextTick(() => autoGrow())

  abortedByUser = false
  relayRemaining.value = members.value.length > 1 ? relayRounds.value : 0

  runTurn({
    text,
    narrating,
    picked,
    speakerId,
    restoreDraftOnError: rawDraft,
    allowRelay: true,
  })
}

function onComposerDrop(e: DragEvent): void {
  const files = Array.from(e.dataTransfer?.files ?? [])
  if (!files.length) return
  e.preventDefault()
  void picker.value?.addFiles(files)
}

function onComposerPaste(e: ClipboardEvent): void {
  const files = Array.from(e.clipboardData?.items ?? [])
    .filter((it) => it.kind === 'file')
    .map((it) => it.getAsFile())
    .filter((f): f is File => Boolean(f))
  if (!files.length) return
  e.preventDefault()
  void picker.value?.addFiles(files)
}

function promptSpeaker(companionId: string): void {
  if (!room.value || busy.value) return
  abortedByUser = false
  cancelRelay()
  mentionOpen.value = false
  inviteOpen.value = false
  selectedSpeakerId.value = ''
  runTurn({ text: '', speakerId: companionId })
}

/** Turn the next send into a stage direction; the mode stays on until toggled
 *  off, so a run of directions does not need the button pressed each time. */
function toggleNarration(): void {
  narrationMode.value = !narrationMode.value
  nextTick(() => composer.value?.focus())
}

function onKeydown(e: KeyboardEvent): void {
  if (mentionOpen.value && mentionCandidates.value.length) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      mentionIndex.value = (mentionIndex.value + 1) % mentionCandidates.value.length
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      mentionIndex.value =
        (mentionIndex.value - 1 + mentionCandidates.value.length) %
        mentionCandidates.value.length
      return
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault()
      const pick = mentionCandidates.value[mentionIndex.value]
      if (pick) insertMention(pick)
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      mentionOpen.value = false
      return
    }
  }

  if (inviteOpen.value) {
    const list = inviteCandidates.value
    if (e.key === 'ArrowDown' && list.length) {
      e.preventDefault()
      slashIndex.value = (slashIndex.value + 1) % list.length
      return
    }
    if (e.key === 'ArrowUp' && list.length) {
      e.preventDefault()
      slashIndex.value = (slashIndex.value - 1 + list.length) % list.length
      return
    }
    if (e.key === 'Enter' && list.length) {
      e.preventDefault()
      const pick = list[slashIndex.value]
      if (pick) void invite(pick.id)
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      inviteOpen.value = false
      return
    }
  }

  if (slashStage.value) {
    const stage = slashStage.value
    const count =
      stage === 'commands'
        ? filteredCommands.value.length
        : stage === 'invite'
          ? inviteCandidates.value.length
          : removeCandidates.value.length

    if (e.key === 'ArrowDown' && count) {
      e.preventDefault()
      slashIndex.value = (slashIndex.value + 1) % count
      return
    }
    if (e.key === 'ArrowUp' && count) {
      e.preventDefault()
      slashIndex.value = (slashIndex.value - 1 + count) % count
      return
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault()
      if (stage === 'commands') {
        const pick = filteredCommands.value[slashIndex.value]
        if (pick) pickSlashCommand(pick.cmd)
      } else if (stage === 'invite') {
        const pick = inviteCandidates.value[slashIndex.value]
        if (pick) void invite(pick.id)
      } else if (stage === 'members') {
        const pick = removeCandidates.value[slashIndex.value]
        if (pick) void removeViaSlash(pick.companion_id)
      }
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      draft.value = ''
      nextTick(() => autoGrow())
      return
    }
  }

  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    mentionOpen.value = false
    send()
  }
}

/* ---- rooms ---- */

async function loadRooms(): Promise<void> {
  try {
    const r = await listGroupSessions()
    rooms.value = r.sessions ?? []
  } catch {
    rooms.value = []
  }
}

async function switchRoom(id: number): Promise<void> {
  if (room.value?.id === id) return
  abortedByUser = true
  cancelRelay()
  if (activeStream) {
    activeStream.abort()
    activeStream = null
  }
  clearDelta()
  busy.value = false
  selectedSpeakerId.value = ''
  rerollMenuId.value = 0
  mentionOpen.value = false
  inviteOpen.value = false
  sceneOpen.value = false
  userNoteOpen.value = false
  localStorage.setItem(ROOM_KEY, String(id))
  live.value = null
  error.value = ''
  loading.value = true
  roomEpoch.value += 1
  try {
    await apply(await getGroupSession(id), true)
    await loadRooms()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
    // Same reason as openRoom: the transcript only has its real height once the
    // placeholder is gone.
    await nextTick()
    scrollToEnd()
  }
}

async function renameRoom(event: Event): Promise<void> {
  const name = ((event.target as HTMLInputElement).value || '').trim()
  if (!room.value || !name || name === room.value.name) return
  try {
    await patchGroupSession(room.value.id, { name })
    room.value = { ...room.value, name }
    await loadRooms()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

/* ---- user note & room scene ---- */

/**
 * The room's half of the user card: how the three of them know you here.
 * Merged after the card's own `user_info` when a prompt is built, so it can be
 * edited without touching every character card.
 */
const userNoteOpen = ref(false)
const userNoteDraft = ref('')

function openUserNote(): void {
  sceneOpen.value = false
  userNoteDraft.value = room.value?.user_note || ''
  userNoteOpen.value = !userNoteOpen.value
}

async function saveUserNote(): Promise<void> {
  if (!room.value) return
  const id = room.value.id
  const note = userNoteDraft.value
  try {
    await patchGroupSession(id, { user_note: note })
    room.value = { ...room.value, user_note: note }
    userNoteOpen.value = false
    void refreshContext()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

const sceneOpen = ref(false)
const sceneDraft = ref('')

function openScene(): void {
  userNoteOpen.value = false
  sceneDraft.value = room.value?.scene || ''
  sceneOpen.value = !sceneOpen.value
}

async function saveScene(): Promise<void> {
  if (!room.value) return
  const id = room.value.id
  const scene = sceneDraft.value
  try {
    await patchGroupSession(id, { scene })
    room.value = { ...room.value, scene }
    sceneOpen.value = false
    void refreshContext()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

/**
 * Hover label for a spine.
 *
 * `data-tip` cannot be used here: its `::after` sits outside the element, and
 * the spine column has to clip (it scrolls), so the label would be cut off the
 * moment it appeared. A fixed-position layer escapes the clip and keeps the
 * same look.
 */
const hoveredRoom = ref<{ name: string; y: number } | null>(null)

function showRoomTip(r: GroupSession, e: MouseEvent): void {
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
  hoveredRoom.value = { name: r.name, y: Math.round(rect.top + rect.height / 2) }
}

/**
 * Nothing to clear means no lines, not no cast: clearing keeps the cast, so a
 * room full of characters with an empty transcript still has nothing to empty.
 */
const emptyRoom = computed(() => !!room.value && !messages.value.length)

/**
 * One button, two ways out: keep the room and empty it, or remove it entirely.
 *
 * Clearing used to take the cast with it, which made it indistinguishable from
 * deleting a room for anyone who had just cast the characters. Now clearing
 * only drops the lines and deleting is always available, empty room or not.
 */
function askClearRoom(): void {
  if (!room.value) return
  const id = room.value.id
  const hasContent = !emptyRoom.value
  const extras: string[] = []
  if (memories.value.length) {
    extras.push(memories.value.length + (memories.value.length === 1 ? ' memory' : ' memories'))
  }
  if (compaction.value) extras.push('its summary')
  const archive = extras.length ? ', together with ' + extras.join(' and ') : ''
  ui.askConfirm({
    title: hasContent ? 'Clear the chat, or delete the room?' : 'Delete this room?',
    text: hasContent
      ? 'Clearing removes every line and keeps the cast. Deleting removes the room itself' + archive + '. This cannot be undone.'
      : 'Deleting removes the room' + archive + '. This cannot be undone.',
    confirmLabel: 'Delete the room',
    secondaryLabel: hasContent ? 'Clear the chat' : '',
    onSecondary: hasContent ? () => void clearRoom(id) : undefined,
    onConfirm: () => void deleteRoom(id),
  })
}

async function deleteRoom(id: number): Promise<void> {
  try {
    await deleteGroupSession(id)
    if (Number(localStorage.getItem(ROOM_KEY) || 0) === id) {
      localStorage.removeItem(ROOM_KEY)
    }
    await loadRooms()
    const next = rooms.value[0]
    if (next) await switchRoom(next.id)
    else await openRoom()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function clearRoom(id: number): Promise<void> {
  try {
    await clearGroupMessages(id)
    await reload()
    await loadRooms()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

function askNewRoom(): void {
  ui.askConfirm({
    title: 'Open a new room?',
    text: 'This room stays exactly as it is. The new one starts empty.',
    confirmLabel: 'New room',
    danger: false,
    onConfirm: () => void newRoom(),
  })
}

async function newRoom(): Promise<void> {
  try {
    const { session: fresh } = await createGroupSession({})
    await loadRooms()
    await switchRoom(fresh.id)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

onMounted(async () => {
  await loadRooms()
  await openRoom()
})
</script>

<template>
  <section class="group-stage">
    <aside class="gc-rooms">
      <button
        v-for="r in rooms"
        :key="r.id"
        type="button"
        class="gc-room"
        :class="{ on: r.id === room?.id }"
        @mouseenter="showRoomTip(r, $event)"
        @mouseleave="hoveredRoom = null"
        @click="switchRoom(r.id)"
      >
        {{ r.name }}
      </button>
    </aside>

    <div
      v-if="hoveredRoom"
      class="gc-room-tip"
      :style="{ top: `${hoveredRoom.y}px` }"
      aria-hidden="true"
    >
      {{ hoveredRoom.name }}
    </div>

    <div class="gc-main">
      <div :key="roomEpoch" class="gc-veil" aria-hidden="true"></div>
      <header class="gc-bar">
        <input
          class="gc-title"
          :value="room?.name || ''"
          spellcheck="false"
          aria-label="Room name"
          @change="renameRoom"
          @keydown.enter.prevent="($event.target as HTMLInputElement).blur()"
        />
        <button
          type="button"
          class="gc-bar-btn"
          :data-tip="t('group.newRoom')"
          aria-label="New room"
          @click="askNewRoom"
        >
          <svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>
        </button>
      </header>

      <div class="gc-hand" :class="{ empty: !cards.length }" aria-hidden="false">
      <button
        v-for="(card, i) in cards"
        :key="card.key"
        type="button"
        class="gc-card"
        :data-card-key="card.key"
        :class="{
          on: card.focused,
          speaking: card.speaking,
          hovered: hoveredCardId === card.key,
        }"
        :style="cardStyle(i)"
        :data-tip="
          draft.trim() || chips.length
            ? t('group.cardSelectTip', { name: card.name })
            : t('group.cardSpeakTip', { name: card.name })
        "
        data-tip-float
        @click="onCardClick(card.key)"
      >
        <span class="gc-portrait">
          <img
            v-if="card.avatar && memberPortraits[card.avatar]"
            :src="memberPortraits[card.avatar]"
            :alt="card.name"
          />
          <span v-else class="gc-glyph">{{ card.letter }}</span>
        </span>
        <span class="gc-foot">
          <span class="gc-name">{{ card.name }}</span>
          <i v-if="card.role" class="gc-role">{{ card.role }}</i>
        </span>
      </button>
      <div
        v-for="s in emptySlots"
        :key="`slot${s}`"
        class="gc-card gc-slot"
        :style="slotStyle(s - 1)"
        @click="openInvitePicker"
        aria-hidden="true"
      >
        <span class="gc-slot-mark">+</span>
      </div>
    </div>

    <div
      ref="scroller"
      class="gc-scroll"
      :class="{ 'card-hover': Boolean(hoveredCardId) }"
      @scroll.passive="onScroll"
      @mousedown="onStageMouseDown"
      @mousemove="onStageMouseMove"
      @mouseleave="hoveredCardId = ''; hoveredCardPos = null"
      @click="onStageClick"
    >
      <div ref="thread" class="gc-thread">
        <span class="gc-axis" aria-hidden="true"></span>

        <div v-if="loading" class="gc-empty">Opening the room…</div>

        <div v-else-if="!cards.length" class="gc-empty">
          <p class="gc-empty-title">{{ t('group.emptyTitle') }}</p>
          <p class="gc-empty-hint">
            {{ t('group.emptyHintLead') }}<button type="button" class="gc-empty-cmd" @click="openInvitePicker">/invite</button>{{ t('group.emptyHintTail', { invite: '+ ' + t('group.invite') }) }}
          </p>
        </div>

        <template v-else>
          <!-- The room's own summary: the part of the evening everyone still
               carries, folded into one note above the retained tail. -->
          <article v-if="compaction" class="gc-beat gc-beat-summary" data-role="summary">
            <span class="gc-node" aria-hidden="true"></span>
            <button type="button" class="gc-summary-head" @click="summaryOpen = !summaryOpen">
              <span class="gc-summary-k">Earlier in this room</span>
              <span class="gc-summary-n">{{ compaction.covered_count ?? 0 }} lines</span>
            </button>
            <p v-if="summaryOpen" class="gc-summary-text">{{ compaction.summary }}</p>
          </article>

          <article v-for="m in visibleMessages" :key="idOf(m)" class="gc-beat" :data-role="m.kind === 'narration' ? 'narration' : m.role">
            <span class="gc-node" aria-hidden="true"></span>
            <header class="gc-head">
              <span class="gc-speaker">{{ m.speaker_name || 'You' }}</span>
              <span v-if="clockOf(m.created_at)" class="gc-time">{{ clockOf(m.created_at) }}</span>
              <span class="gc-tools">
                <template v-if="m.role === 'assistant' && m.id === lastVisibleId && !busy && relayRemaining === 0">
                  <template v-if="rerollMenuId === m.id && otherMembersOf(m.speaker_id).length">
                    <button
                      v-for="om in otherMembersOf(m.speaker_id)"
                      :key="om.companion_id"
                      type="button"
                      class="gc-tool-chip"
                      @click="rerollLast(m.id, om.companion_id)"
                    >
                      {{ om.name }}
                    </button>
                  </template>
                  <button
                    v-if="otherMembersOf(m.speaker_id).length"
                    type="button"
                    class="gc-tool"
                    :class="{ on: rerollMenuId === m.id }"
                    :data-tip="t('group.switchSpeaker')"
                    :aria-label="t('group.switchSpeaker')"
                    @click="rerollMenuId = rerollMenuId === m.id ? 0 : m.id"
                  >
                    <svg viewBox="0 0 24 24">
                      <path d="M16 3l4 4-4 4" />
                      <path d="M20 7H9a4 4 0 00-4 4v1" />
                      <path d="M8 21l-4-4 4-4" />
                      <path d="M4 17h11a4 4 0 004-4v-1" />
                    </svg>
                  </button>
                  <button
                    type="button"
                    class="gc-tool"
                    :data-tip="t('group.reroll')"
                    :aria-label="t('group.reroll')"
                    @click="rerollLast(m.id)"
                  >
                    <svg viewBox="0 0 24 24">
                      <path d="M3 12a9 9 0 0115-6.7L21 8" />
                      <path d="M21 3v5h-5" />
                      <path d="M21 12a9 9 0 01-15 6.7L3 16" />
                    </svg>
                  </button>
                </template>
                <button
                  type="button"
                  class="gc-tool"
                  :data-tip="t('tool.copy')"
                  aria-label="Copy this line"
                  @click="copyMessage(m)"
                >
                  <svg v-if="copiedId !== m.id" viewBox="0 0 24 24">
                    <rect x="9" y="9" width="11" height="11" rx="1" />
                    <path d="M5 15V4h11" />
                  </svg>
                  <svg v-else viewBox="0 0 24 24"><path d="M5 12l5 5L19 7" /></svg>
                </button>
                <button
                  type="button"
                  class="gc-tool danger"
                  :data-tip="t('group.deleteMessage')"
                  aria-label="Delete this line"
                  @click="removeMessage(m)"
                >
                  <svg viewBox="0 0 24 24">
                    <path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13" />
                  </svg>
                </button>
              </span>
            </header>
            <p
              v-for="(line, li) in linesOf(m.content, m.kind)"
              :key="li"
              :class="line.kind === 'direction' ? 'gc-direction' : 'gc-say'"
            >
              {{ line.text }}
            </p>
            <div v-if="imagesOf(m).length" class="gc-shots">
              <button
                v-for="a in imagesOf(m)"
                :key="a.url"
                type="button"
                class="gc-shot"
                @click="openShot(String(a.url))"
              >
                <img :src="shotUrl(String(a.url))" :alt="a.name || 'image'" />
              </button>
            </div>
            <div v-if="videosOf(m).length" class="inline-video-list">
              <InlineVideoCard
                v-for="v in videosOf(m)"
                :key="String(v.url)"
                compact
                :video="{
                  name: v.name,
                  url: String(v.url),
                  posterUrl: v.poster_url,
                  duration: v.duration,
                  sizeBytes: v.size,
                }"
                :gallery-target="String(v.url)"
              />
            </div>
          </article>

          <article v-if="live" class="gc-beat gc-live" data-role="ai">
            <span class="gc-node" aria-hidden="true"></span>
            <header class="gc-head">
              <span class="gc-speaker">{{ live.speaker }}</span>
            </header>
            <template v-for="(line, i) in linesOf(live.text)" :key="i">
              <p v-if="line.kind === 'say'" class="gc-say">{{ line.text }}</p>
              <p v-else class="gc-direction">{{ line.text }}</p>
            </template>
          </article>

          <div v-if="(busy || relayRemaining > 0) && !live" class="gc-pending">
            <span class="gc-beat-node"></span>
            <span>{{ relayRemaining > 0 && !busy ? t('group.relaying') : 'someone is about to speak' }}</span>
          </div>
        </template>

        <p v-if="error" class="gc-error">{{ error }}</p>
      </div>
    </div>

    <footer
      class="gc-composer"
      @dragover.prevent
      @drop="onComposerDrop"
    >
      <ChatAttachmentPicker ref="picker" v-model="chips" image-only />
      <div class="gc-composer-inner">
        <div v-if="userNoteOpen" class="gc-note">
          <div class="gc-note-k">About you, in this room</div>
          <textarea
            v-model="userNoteDraft"
            class="gc-note-input"
            rows="3"
            placeholder="Name, background, how the others should treat you…"
          ></textarea>
          <div class="gc-note-foot">
            <button type="button" class="mini-btn primary" @click="saveUserNote">Save</button>
            <button type="button" class="mini-btn" @click="userNoteOpen = false">Cancel</button>
          </div>
        </div>

        <div v-if="sceneOpen" class="gc-note">
          <div class="gc-note-k">{{ t('group.sceneTitle') }}</div>
          <textarea
            v-model="sceneDraft"
            class="gc-note-input"
            rows="3"
            :placeholder="t('group.scenePlaceholder')"
          ></textarea>
          <div class="gc-note-foot">
            <button type="button" class="mini-btn primary" @click="saveScene">Save</button>
            <button type="button" class="mini-btn" @click="sceneOpen = false">Cancel</button>
          </div>
        </div>

        <div v-if="slashStage === 'commands'" class="gc-mention">
          <button
            v-for="(c, i) in filteredCommands"
            :key="c.cmd"
            type="button"
            class="gc-mention-item"
            :class="{ on: i === slashIndex }"
            @click="pickSlashCommand(c.cmd)"
          >
            <span class="gc-cmd">{{ c.cmd }}</span>
            <span class="gc-mention-role">{{ c.hint }}</span>
          </button>
        </div>

        <div v-else-if="slashStage === 'invite' || inviteOpen" class="gc-mention">
          <div v-if="members.length >= MAX_MEMBERS" class="gc-mention-empty">
            A room holds at most {{ MAX_MEMBERS }} characters.
          </div>
          <div v-else-if="!inviteCandidates.length" class="gc-mention-empty">
            {{
              parsedSlash?.stage === 'invite' && parsedSlash.arg
                ? 'No matching character card to invite.'
                : session.companions.some((c) => !isCharxCard(c))
                  ? 'Everyone in the cast is already here.'
                  : 'No standard character cards available (charx cards are excluded).'
            }}
          </div>
          <template v-else>
            <button
              v-for="(c, i) in inviteCandidates"
              :key="c.id"
              type="button"
              class="gc-mention-item"
              :class="{ on: i === slashIndex }"
              @click="invite(c.id)"
            >
              <span class="gc-avatar">
                <img v-if="c.avatar && castPortraits[c.avatar]" :src="castPortraits[c.avatar]" :alt="c.name" />
                <i v-else>{{ c.letter }}</i>
              </span>
              <span class="gc-mention-name">{{ c.name }}</span>
              <span v-if="c.role" class="gc-mention-role">{{ c.role }}</span>
            </button>
          </template>
        </div>

        <div v-else-if="slashStage === 'members'" class="gc-mention">
          <div v-if="!removeCandidates.length" class="gc-mention-empty">
            {{ members.length ? 'No matching member in the room.' : 'Nobody in the room yet.' }}
          </div>
          <button
            v-for="(m, i) in removeCandidates"
            :key="m.companion_id"
            type="button"
            class="gc-mention-item"
            :class="{ on: i === slashIndex }"
            @click="removeViaSlash(m.companion_id)"
          >
            <span class="gc-avatar">
              <img v-if="portraitOf(m.companion_id)" :src="portraitOf(m.companion_id)" :alt="m.name" />
              <i v-else>{{ letterOf(m.companion_id, m.name) }}</i>
            </span>
            <span class="gc-mention-name">{{ m.name }}</span>
            <span class="gc-mention-role">remove</span>
          </button>
        </div>

        <div v-if="mentionOpen" class="gc-mention">
          <div v-if="!mentionCandidates.length" class="gc-mention-empty">
            {{ members.length ? 'No matching member in this room.' : 'Nobody in the room yet — type /invite first.' }}
          </div>
          <button
            v-for="(m, i) in mentionCandidates"
            :key="m.companion_id"
            type="button"
            class="gc-mention-item"
            :class="{ on: i === mentionIndex }"
            @click="insertMention(m)"
          >
            <span class="gc-avatar">
              <img v-if="portraitOf(m.companion_id)" :src="portraitOf(m.companion_id)" :alt="m.name" />
              <i v-else>{{ letterOf(m.companion_id, m.name) }}</i>
            </span>
            <span class="gc-mention-name">{{ m.name }}</span>
            <span v-if="castById.get(m.companion_id)?.role" class="gc-mention-role">
              {{ castById.get(m.companion_id)?.role }}
            </span>
          </button>
        </div>

        <textarea
          ref="composer"
          v-model="draft"
          class="gc-input"
          rows="1"
          :placeholder="
            narrationMode
              ? t('composer.placeholderNarration')
              : 'Say something, @ to address, or /invite'
          "
          @input="onDraftInput"
          @keydown="onKeydown"
          @paste="onComposerPaste"
        ></textarea>

        <button
          type="button"
          class="gc-cbtn gc-note-btn"
          :class="{ on: Boolean(room?.scene?.trim()) }"
          :data-tip="t('group.scene')"
          :aria-label="t('group.scene')"
          @click="openScene"
        >
          <svg viewBox="0 0 24 24">
            <rect x="3.5" y="4.5" width="17" height="15" rx="1.5" />
            <path d="M3.5 16l4.5-4.5 3.5 3.5 4-5 5 6" />
          </svg>
        </button>

        <button
          type="button"
          class="gc-cbtn gc-note-btn"
          :class="{ on: Boolean(room?.user_note?.trim()) }"
          :data-tip="t('group.userNote')"
          aria-label="Your details in this room"
          @click="openUserNote"
        >
          <svg viewBox="0 0 24 24">
            <circle cx="12" cy="8.4" r="3.3" />
            <path d="M5.2 19.4c0-3.3 3-5.9 6.8-5.9s6.8 2.6 6.8 5.9" />
          </svg>
        </button>

        <button
          type="button"
          class="gc-cbtn gc-clear"
          :data-tip="t('group.clearOrDelete')"
          :aria-label="t('group.clearOrDelete')"
          @click="askClearRoom"
        >
          <svg viewBox="0 0 24 24">
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>

        <button
          type="button"
          class="gc-cbtn"
          :class="{ on: narrationMode }"
          :data-tip="t('composer.narration')"
          aria-label="Narration mode"
          @click="toggleNarration"
        >
          <svg viewBox="0 0 24 24">
            <path d="M10 8.5H6v4.5h4v-2c0-1 .3-1.8 1.4-2.5" />
            <path d="M19 8.5h-4v4.5h4v-2c0-1 .3-1.8 1.4-2.5" />
          </svg>
        </button>

        <button
          type="button"
          class="gc-cbtn"
          :data-tip="t('composer.attach')"
          aria-label="Attach media"
          @click="picker?.pick()"
        >
          <svg viewBox="0 0 24 24">
            <rect x="3.5" y="5" width="17" height="14" rx="2" />
            <circle cx="9" cy="10" r="1.6" />
            <path d="M21 16l-5-4-4 3-3-2-6 5" />
          </svg>
        </button>

        <button
          type="button"
          class="gc-cbtn gc-send"
          :class="{ busy: busy || relayRemaining > 0 }"
          :disabled="!busy && relayRemaining === 0 && Boolean(picker?.uploading)"
          :data-tip="busy || relayRemaining > 0 ? t('group.stop') : t('composer.send')"
          :aria-label="busy || relayRemaining > 0 ? t('group.stop') : t('composer.send')"
          @click="send"
        >
          <svg v-if="!busy && relayRemaining === 0" viewBox="0 0 24 24">
            <path d="M4 12h15M13 6l6 6-6 6" />
          </svg>
          <svg v-else viewBox="0 0 24 24">
            <rect x="7" y="7" width="10" height="10" rx="1" />
          </svg>
        </button>
      </div>

      <button
        v-if="showJump"
        type="button"
        class="gc-jump"
        :data-tip="t('group.toBottom')"
        aria-label="Scroll to the newest line"
        @click="scrollToEnd(true)"
      >
        <svg viewBox="0 0 24 24"><path d="M12 5v14M6 13l6 6 6-6" /></svg>
      </button>

      <div v-if="cards.length" class="gc-cast-row">
        <button
          v-for="card in cards"
          :key="card.key"
          type="button"
          class="gc-cast-pill"
          :class="{ on: card.focused, speaking: card.speaking }"
          :disabled="busy"
          :data-tip="
            draft.trim() || chips.length
              ? t('group.cardSelectTip', { name: card.name })
              : t('group.cardSpeakTip', { name: card.name })
          "
          data-tip-float
          @click="onCardClick(card.key)"
        >
          <span class="gc-cast-dot" aria-hidden="true"></span>
          <span>{{ card.name }}</span>
        </button>
      </div>

      <div class="gc-hint">
        <span>{{ t('group.mentionHint') }}</span>
        <span class="gc-dot">·</span>
        <span>{{ members.length }}/{{ MAX_MEMBERS }}</span>
        <template v-if="members.length > 1">
          <span class="gc-dot">·</span>
          <button
            type="button"
            class="gc-hint-btn"
            :class="{ on: relayRounds > 0 }"
            :data-tip="t('group.relayTip')"
            data-tip-float
            @click="cycleRelay"
          >
            {{ t('group.relayLabel') }}: {{ relayRounds ? `+${relayRounds}` : t('group.relayOff') }}
          </button>
        </template>
        <template v-if="members.length < MAX_MEMBERS">
          <span class="gc-dot">·</span>
          <button type="button" class="gc-hint-btn" @click="openInvitePicker">
            + {{ t('group.invite') }}
          </button>
        </template>
        <template v-if="selectedSpeaker">
          <span class="gc-dot">·</span>
          <button
            type="button"
            class="gc-target-pill"
            @click="selectedSpeakerId = ''"
          >
            {{ t('group.nextSpeaker', { name: selectedSpeaker.name }) }} ×
          </button>
        </template>
      </div>
    </footer>
    </div>

    <Teleport to="body">
      <div
        v-if="hoveredCardTip && hoveredCardPos"
        ref="cardTipEl"
        class="floating-tip show"
        :style="cardTipStyle"
        aria-hidden="true"
      >
        {{ hoveredCardTip }}
      </div>
    </Teleport>
  </section>
</template>

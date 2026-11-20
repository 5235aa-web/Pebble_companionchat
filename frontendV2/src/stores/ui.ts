import { defineStore } from 'pinia'
import { ref } from 'vue'

export type ThemeName = 'night' | 'day'
export type AccentName = 'amber' | 'ember' | 'celadon' | 'moon' | 'plum' | 'jade' | 'rose'
export type FontStyle = 'wenkai' | 'song' | 'hei'
/**
 * Which stage is on screen. One app, two views: the single-companion thread and
 * the group room. The rail, the theme and every panel stay where they are; only
 * the area below the rail is swapped.
 */
export type AppView = 'single' | 'group'
export type PanelId =
  | 'drawer'
  | 'lore'
  | 'memory'
  | 'backstage'
  | 'compact'
  | 'rules'
  | 'gallery'
  | 'script'
  | 'scene-copy'
  | 'profile'
  | 'dialog'
  | null

const THEME_KEY = 'dialogue.theme'
const FONT_KEY = 'dialogue.font'
const ACCENT_KEY = 'dialogue.accent'
const VIEW_KEY = 'dialogue.view'

function readStored(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeStored(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* storage may be unavailable, the theme still applies for this session */
  }
}

export const useUiStore = defineStore('ui', () => {
  const root = document.documentElement
  const theme = ref<ThemeName>((readStored(THEME_KEY) as ThemeName) || 'night')
  const accent = ref<AccentName>((readStored(ACCENT_KEY) as AccentName) || 'amber')
  const fontStyle = ref<FontStyle>('wenkai')
  const panel = ref<PanelId>(null)
  const view = ref<AppView>(readStored(VIEW_KEY) === 'group' ? 'group' : 'single')
  /**
   * Token footprint of the open room's next turn. Reported by the group stage
   * because only it knows which room is open; the rail only displays it.
   */
  const groupTokens = ref(0)

  function setGroupTokens(n: number): void {
    groupTokens.value = Math.max(0, Math.round(n))
  }

  /**
   * The room the group view is showing. The album needs it to know whose
   * pictures to list; every other consumer gets it from the stage directly.
   */
  const groupRoomId = ref(0)

  function setGroupRoom(id: number): void {
    groupRoomId.value = Math.max(0, Math.round(id))
  }

  /**
   * Bumped when a room's pictures change outside the stage — the album deletes
   * one while the transcript is still holding the old attachment list. The
   * stage watches this and reloads, otherwise the thumbnail points at a file
   * that no longer exists.
   */
  const groupEpoch = ref(0)

  function bumpGroupEpoch(): void {
    groupEpoch.value += 1
  }

  /**
   * Which memories fed the room's latest reply. Same shape as the single
   * thread's in-play entries so the rail draws both with one component; the
   * data source differs, the presentation does not.
   */
  const groupInPlay = ref<
    { id: number; label: string; weight: number; key: string }[]
  >([])

  function setGroupInPlay(
    list: { id: number; label: string; weight: number; key: string }[],
  ): void {
    groupInPlay.value = Array.isArray(list) ? list : []
  }

  /**
   * A memory the user asked to locate from the rail. The memory panel reads
   * this, switches to the right tab and filters to it — the panel is not open
   * yet when the click happens, so the request has to survive until it is.
   */
  const memoryFocus = ref<{ text: string; tab: 'single' | 'group' } | null>(null)

  function focusMemory(text: string, tab: 'single' | 'group'): void {
    memoryFocus.value = { text, tab }
  }
  const dropActive = ref(false)
  /** Gallery image to jump to when the panel opens. */
  const galleryTarget = ref('')
  /**
   * True while the profile panel is editing a character that does not exist yet.
   * Nothing is written to the database until the card is saved, so abandoning
   * the editor leaves no trace in the cast list.
   */
  const newCastDraft = ref(false)
  /** Image handed from the gallery to the composer as a pending attachment. */
  const quotedImage = ref<{ url: string; name: string } | null>(null)

  const dialogTitle = ref('')
  const dialogText = ref('')
  /** Empty means "use the localised Delete". */
  const dialogConfirmLabel = ref('')
  /**
   * A second, non-destructive choice next to the confirm button. Empty hides
   * it: most dialogs ask one question and only need yes or no.
   */
  const dialogSecondaryLabel = ref('')
  /** Destructive actions paint the confirm button red; others stay neutral. */
  const dialogDanger = ref(true)
  /** True when the dialog is a notice: one button, no question to answer. */
  const dialogAlert = ref(false)
  let dialogConfirm: (() => void) | null = null
  let dialogSecondaryConfirm: (() => void) | null = null

  /** A notice the user can only acknowledge. */
  function askAlert(opts: { title: string; text: string; confirmLabel?: string }) {
    dialogTitle.value = opts.title
    dialogText.value = opts.text
    dialogConfirmLabel.value = opts.confirmLabel ?? ''
    dialogSecondaryLabel.value = ''
    dialogDanger.value = false
    dialogAlert.value = true
    dialogConfirm = null
    dialogSecondaryConfirm = null
    panel.value = 'dialog'
  }

  function askConfirm(opts: {
    title: string
    text: string
    onConfirm: () => void
    confirmLabel?: string
    danger?: boolean
    /** Optional lighter alternative, shown between Cancel and Confirm. */
    secondaryLabel?: string
    onSecondary?: () => void
  }) {
    dialogTitle.value = opts.title
    dialogText.value = opts.text
    dialogConfirmLabel.value = opts.confirmLabel ?? ''
    dialogSecondaryLabel.value = opts.secondaryLabel ?? ''
    dialogDanger.value = opts.danger ?? true
    dialogAlert.value = false
    dialogConfirm = opts.onConfirm
    dialogSecondaryConfirm = opts.onSecondary ?? null
    panel.value = 'dialog'
  }

  function closeDialog() {
    dialogConfirm = null
    dialogSecondaryConfirm = null
    dialogSecondaryLabel.value = ''
    dialogAlert.value = false
    panel.value = null
  }

  function confirmDialog() {
    const fn = dialogConfirm
    closeDialog()
    fn?.()
  }

  function runSecondary() {
    const fn = dialogSecondaryConfirm
    closeDialog()
    fn?.()
  }

  root.dataset.theme = theme.value
  root.dataset.accent = accent.value

  function commit(t: ThemeName, a: AccentName) {
    theme.value = t
    accent.value = a
    root.dataset.theme = t
    root.dataset.accent = a
    writeStored(THEME_KEY, t)
    writeStored(ACCENT_KEY, a)
  }

  /** Circular reveal starting from the clicked control, falling back to a plain swap. */
  function transitionTo(t: ThemeName, a: AccentName, origin?: HTMLElement | null) {
    if (origin) {
      const r = origin.getBoundingClientRect()
      root.style.setProperty('--ox', r.left + r.width / 2 + 'px')
      root.style.setProperty('--oy', r.top + r.height / 2 + 'px')
    }
    const doc = document as Document & {
      startViewTransition?: (cb: () => void) => { ready: Promise<void> }
    }
    if (typeof doc.startViewTransition === 'function') {
      doc.startViewTransition(() => commit(t, a))
    } else {
      commit(t, a)
    }
  }

  function toggleTheme(origin?: HTMLElement | null) {
    transitionTo(theme.value === 'day' ? 'night' : 'day', accent.value, origin)
  }

  function setAccent(a: AccentName, origin?: HTMLElement | null) {
    transitionTo(theme.value, a, origin)
  }

  function openPanel(id: Exclude<PanelId, null>) {
    panel.value = panel.value === id ? null : id
  }

  /**
   * Swap the stage. Persisted so a reload comes back to the view in use, the
   * same way theme and accent are remembered.
   */
  function setView(next: AppView): void {
    view.value = next
    writeStored(VIEW_KEY, next)
  }

  function toggleView(): void {
    setView(view.value === 'group' ? 'single' : 'group')
  }

  /** Open the card editor on a blank card. The character is created on save. */
  function startNewCast() {
    newCastDraft.value = true
    panel.value = 'profile'
  }

  function clearNewCast() {
    newCastDraft.value = false
  }

  function closeAll() {
    panel.value = null
  }

  function applyFont(f: FontStyle) {
    fontStyle.value = f
    document.documentElement.dataset.font = f
    writeStored(FONT_KEY, f)
  }

  const fontOrder: FontStyle[] = ['wenkai', 'song', 'hei']

  function cycleFont() {
    const i = fontOrder.indexOf(fontStyle.value)
    applyFont(fontOrder[(i + 1) % fontOrder.length])
  }

  applyFont((readStored(FONT_KEY) as FontStyle) || 'wenkai')

  function closePanel(id: Exclude<PanelId, null>) {
    if (panel.value === id) panel.value = null
  }

  function isOpen(id: Exclude<PanelId, null>) {
    return panel.value === id
  }

  /** Modal-style panels draw the scrim; the drawer stack does too. */
  function scrimVisible() {
    return panel.value !== null
  }

  return {
    theme,
    accent,
    view,
    setView,
    toggleView,
    groupTokens,
    setGroupTokens,
    groupRoomId,
    setGroupRoom,
    groupEpoch,
    bumpGroupEpoch,
    groupInPlay,
    setGroupInPlay,
    memoryFocus,
    focusMemory,
    fontStyle,
    panel,
    dropActive,
    galleryTarget,
    quotedImage,
    newCastDraft,
    startNewCast,
    clearNewCast,
    dialogTitle,
    dialogText,
    dialogConfirmLabel,
    dialogSecondaryLabel,
    dialogDanger,
    dialogAlert,
    askAlert,
    askConfirm,
    confirmDialog,
    runSecondary,
    closeDialog,
    toggleTheme,
    setAccent,
    applyFont,
    cycleFont,
    openPanel,
    closePanel,
    closeAll,
    isOpen,
    scrimVisible,
  }
})

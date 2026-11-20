<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useUiStore, type AccentName, type FontStyle } from '@/stores/ui'
import type { Branch } from '@/data/chat'
import { useI18n, type LabelKey } from '@/i18n'
import { useSessionStore, type InPlayEntry } from '@/stores/session'
import { useChatStore, llmConfig } from '@/stores/chat'
import { useScriptStore } from '@/stores/script'
import { useLlmStore } from '@/stores/llm'
import { useMemoryStore } from '@/stores/memory'
import { searchMessages } from '@/api/companion'
import { closeWindow, isDesktop, minimizeWindow, toggleMaximizeWindow } from '@/lib/window'

const ui = useUiStore()
const { t, locale, cycleLocale } = useI18n()
const session = useSessionStore()
const chat = useChatStore()
const script = useScriptStore()
const llm = useLlmStore()
const memory = useMemoryStore()

/** True while the open thread is a scene being played. */
const inScene = computed(
  () => Boolean(script.session && script.session.branch_id === session.activeBranch),
)

/**
 * The context meter follows the open view. Both numbers answer the same
 * question — how much of the window is in use — but they count different
 * transcripts, so the group view reports its own.
 */
const ctxUsed = computed(() => (ui.view === 'group' ? ui.groupTokens : session.tokensUsed))
const ctxTotal = computed(() => session.tokensTotal)
const ctxPct = computed(() =>
  ctxTotal.value > 0 ? Math.min(100, (ctxUsed.value / ctxTotal.value) * 100) : 0,
)
const ctxPctText = computed(() => ctxPct.value.toFixed(2))

/**
 * Jump from a dot to the memory behind it.
 *
 * The keyword the dot carries is enough to isolate that row in the panel, so
 * the click lands on one memory rather than on the whole list.
 */
/**
 * The dots follow the view, like the context meter: the single thread's hits
 * come from the chat stream, a room's from its own turn. Same component, two
 * sources, never mixed.
 */
const inPlayList = computed(() =>
  ui.view === 'group' ? ui.groupInPlay : session.inPlay,
)

function openMemory(e: { id: number; label: string; weight?: number; key?: string }) {
  if (ui.view === 'group') {
    ui.focusMemory(e.label, 'group')
    ui.openPanel('memory')
    return
  }
  memory.query = e.key || e.label.slice(0, 16)
  ui.openPanel('memory')
}

const menuOpen = ref(false)
const accentOpen = ref(false)
const searchOpen = ref(false)
const searchInput = ref<HTMLInputElement | null>(null)

const accents: AccentName[] = ['amber', 'ember', 'celadon', 'moon', 'plum', 'jade', 'rose']
function accentTip(a: AccentName): string {
  return t(('accent.' + a) as LabelKey)
}

function pickAccent(a: AccentName) {
  ui.setAccent(a)
  accentOpen.value = false
}

const desktop = isDesktop()

const fontNames: Record<FontStyle, string> = {
  wenkai: 'WenKai',
  song: 'Songti',
  hei: 'Sans',
}
const fontTip = computed(() => fontNames[ui.fontStyle])
const langTip = computed(() => (locale.value === 'en' ? '中文' : 'English'))

const searchQuery = ref('')
const searchResults = ref<Array<{ id: number; role: string; content: string }>>([])
const searching = ref(false)
let searchTimer = 0

/** Debounced keyword search over this branch's messages. */
watch(searchQuery, (raw) => {
  clearTimeout(searchTimer)
  const q = raw.trim()
  if (!q) {
    searchResults.value = []
    return
  }
  searchTimer = window.setTimeout(async () => {
    const cid = session.activeCompanionId
    const bid = session.activeBranch
    if (!cid || !bid) return
    searching.value = true
    try {
      searchResults.value = await searchMessages(cid, bid, q)
    } catch {
      searchResults.value = []
    } finally {
      searching.value = false
    }
  }, 250)
})

const compactBusy = ref(false)

/**
 * Red mark next to the context bar. A local server degrades long before a cloud
 * window does, so the two families get different thresholds. The provider comes
 * from the reply that just finished, which is the model actually in use.
 */
const contextWarn = computed(() => {
  const used = session.tokensUsed
  if (used <= 0) return false
  const beat = [...chat.beats]
    .reverse()
    .find((b) => b.role === 'ai' && b.generations?.length)
  const provider = String(beat?.generations?.[0]?.provider ?? '')
  const local = provider ? provider === 'llama-cpp' : llm.isLocal
  return used >= (local ? 60_000 : 200_000)
})

function compactError(code?: string, detail?: string): string {
  if (code === 'not_enough_messages') return t('script.compactTooShort')
  if (code === 'empty_summary' || code === 'empty_transcript') return t('script.compactEmpty')
  return detail || t('script.compactFailedHint')
}

async function runSceneCompact(allowTrim = false) {
  compactBusy.value = true
  try {
    const r = await script.compactPlan(llmConfig(), allowTrim)
    if (r.ok) {
      const base = t('script.compactSummary', {
        count: r.compressed ?? 0,
        from: r.scene_from ?? 0,
        to: r.scene_to ?? 0,
      })
      ui.askAlert({
        title: t('script.compactDone'),
        text: r.trimmed ? `${base} ${t('script.compactTrimmed')}` : base,
      })
      await chat.loadMessages()
      await session.loadTokens()
      return
    }
    if (r.error === 'needs_trim') {
      // Nothing was sent yet: the user decides between dropping the opening of
      // the segment and switching to a model with a larger window.
      ui.askConfirm({
        title: t('script.trimTitle'),
        text: t('script.trimConfirm', { tokens: r.tokens ?? 0, limit: r.limit ?? 0 }),
        confirmLabel: t('script.trimGo'),
        danger: true,
        onConfirm: () => {
          void runSceneCompact(true)
        },
      })
      return
    }
    ui.askAlert({
      title: t('script.compactFailed'),
      text:
        r.error === 'llm_unavailable'
          ? t('script.compactOffline')
          : compactError(r.error, r.detail),
    })
  } catch (e) {
    ui.askAlert({
      title: t('script.compactFailed'),
      text: e instanceof Error ? e.message : String(e),
    })
  } finally {
    compactBusy.value = false
  }
}

function askCompact() {
  if (inScene.value) {
    ui.askConfirm({
      title: t('script.compactTitle'),
      text: t('script.compactConfirm'),
      confirmLabel: t('script.compactGo'),
      // Folding played scenes away changes what the model reads next; the
      // confirm is red so it never passes for a routine click.
      danger: true,
      onConfirm: () => {
        void runSceneCompact()
      },
    })
    return
  }
  ui.askConfirm({
    title: t('compact.confirmTitle'),
    text: t('compact.confirmText'),
    confirmLabel: t('compact.confirmGo'),
    // Compacting is not destructive: it keeps the recent turns and every
    // starred message, and the summary is written as a normal message.
    danger: false,
    onConfirm: () => {
      void chat.compactNow().then((r) => {
        // The drawer cannot show an error it was never told about: `error` is
        // local state inside CompactPanel. Report it here, and leave the panel
        // closed instead of springing open an empty one.
        if (r.ok) return
        ui.askAlert({
          title: t('script.compactFailed'),
          text: r.message || t('script.compactFailedHint'),
        })
      })
    },
  })
}

function closeSearch() {
  searchOpen.value = false
  searchQuery.value = ''
  searchResults.value = []
}

function toggleSearch() {
  searchOpen.value = !searchOpen.value
  if (searchOpen.value) {
    requestAnimationFrame(() => searchInput.value?.focus())
  }
}

function toggleMenu() {
  menuOpen.value = !menuOpen.value
}

function pickBranch(id: string) {
  session.selectBranch(id)
  menuOpen.value = false
}

function addBranch() {
  session.newBranch()
  menuOpen.value = false
}

const editingId = ref('')
const draftName = ref('')

function startRename(b: Branch) {
  // The main branch is renameable too; only deletion is blocked.
  editingId.value = b.id
  draftName.value = b.name
}

function cancelRename() {
  editingId.value = ''
  draftName.value = ''
}

function commitRename(id: string) {
  if (!editingId.value) return
  const name = draftName.value
  editingId.value = ''
  void session.renameBranchLocal(id, name)
}

function askDeleteBranch(id: string) {
  if (id === 'main') return
  ui.askConfirm({
    title: t('dialog.deleteBranch.title'),
    text: t('dialog.deleteBranch.text'),
    onConfirm: () => session.removeBranch(id),
  })
}
</script>

<template>
  <header class="rail" data-tauri-drag-region>
    <div class="rail-l">
      <span class="rail-act">Pebble</span>
      <div class="pb-branch" @click.stop="toggleMenu">
        <span class="rail-title">{{ session.branchTitle || t('branch.main') }}</span>
        <svg
          class="pb-caret"
          viewBox="0 0 24 24"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <path d="M6 9l6 6 6-6" />
        </svg>
        <div class="menu" :class="{ show: menuOpen }" @click.stop>
          <div class="menu-label">{{ t('rail.branches') }}</div>
          <div
            v-for="b in session.branches"
            :key="b.id"
            class="menu-item"
            :class="{ active: b.active }"
            role="button"
            tabindex="0"
            @click="pickBranch(b.id)"
            @keydown.enter="pickBranch(b.id)"
          >
            <span class="mi-dot"></span>
            <input
              v-if="editingId === b.id"
              v-model="draftName"
              class="mi-input"
              @click.stop
              @keydown.enter.prevent="commitRename(b.id)"
              @keydown.esc.prevent="cancelRename"
              @blur="commitRename(b.id)"
            />
            <span v-else class="mi-name">{{ b.name || t('branch.main') }}</span>
            <span class="mi-meta">{{ b.count }}</span>
            <button
              class="mi-act"
              :aria-label="t('rail.renameBranch')"
              @click.stop="startRename(b)"
            >
              <svg viewBox="0 0 24 24"><path d="M4 20h4L18 10l-4-4L4 16v4z" /><path d="M14 6l4 4" /></svg>
            </button>
            <button
              v-if="b.id !== 'main'"
              class="mi-act mi-act-del"
              :aria-label="t('rail.deleteBranch')"
              @click.stop="askDeleteBranch(b.id)"
            >
              <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
            </button>
          </div>
          <div class="menu-sep"></div>
          <button class="menu-item ghost" @click="addBranch">{{ t('rail.newBranch') }}</button>
        </div>
      </div>
    </div>

    <div class="rail-mid">
      <div v-if="session.scenario" class="kv kv-scenario">
        <span class="kv-k">{{ t('rail.scenario') }}</span><span class="kv-v">{{ session.scenario }}</span>
      </div>
      <div v-if="session.model" class="kv">
        <span class="kv-k">{{ t('rail.model') }}</span><span class="kv-v">{{ session.model }}</span>
      </div>
      <div v-if="session.voice" class="kv kv-voice">
        <span class="kv-k">{{ t('rail.voice') }}</span><span class="kv-v">{{ session.voice }}</span>
      </div>
      <div v-if="inPlayList.length" class="inplay">
        <span class="inplay-k">{{ t('rail.inPlay') }}</span>
        <span
          v-for="e in inPlayList"
          :key="e.id"
          class="ip-dot"
          role="button"
          tabindex="0"
          :style="{ '--w': e.weight ?? 0.55 }"
          :aria-label="e.label"
          @click="openMemory(e)"
          @keydown.enter.prevent="openMemory(e)"
        ><i></i><span class="ip-tip">{{ e.label }}</span></span>
      </div>
    </div>

    <div class="rail-r">
      <div v-if="ctxTotal || inScene" class="rail-ctx">
        <span v-if="ctxTotal" class="rail-ctx-n">
          <b>{{ ctxUsed.toLocaleString('en-US').replace(',', ' ') }}</b> /
          {{ ctxTotal.toLocaleString('en-US').replace(',', ' ') }}
        </span>
        <span v-if="ctxTotal" class="rail-ctx-bar"><i :style="{ width: ctxPct + '%' }"></i></span>
        <span v-if="ctxTotal" class="rail-ctx-pct">{{ ctxPctText }}%</span>
        <span v-if="contextWarn" class="ctx-warn" :data-tip="t('rail.contextWarn')">!</span>
        <button
          class="ctx-btn"
          :class="{ spinning: compactBusy }"
          :disabled="compactBusy"
          :data-tip="compactBusy ? t('script.compacting') : t('rail.compactNow')"
          @click="askCompact"
        >
          <svg viewBox="0 0 24 24">
            <path d="M20 12a8 8 0 11-2.4-5.7" />
            <path d="M20 4v4h-4" />
          </svg>
        </button>
        <span v-if="compactBusy" class="ctx-bubble">{{ t('script.compactingNear') }}</span>
      </div>

      <div class="search-wrap">
      <div class="pb-search" :class="{ open: searchOpen }">
        <svg viewBox="0 0 24 24">
          <circle cx="11" cy="11" r="6" />
          <path d="M20 20l-4.2-4.2" />
        </svg>
        <input
          ref="searchInput"
          v-model="searchQuery"
          name="search"
          aria-label="search messages"
          :placeholder="t('rail.searchPlaceholder')"
          @keydown.esc="closeSearch"
        />
      </div>
      <div v-if="searchOpen && searchQuery.trim()" class="search-drop">
        <button
          v-for="r in searchResults"
          :key="r.id"
          class="sd-row"
          @click="chat.jumpToBeat(r.id)"
        >
          <span class="sd-badge" :class="r.role">{{ r.role === 'user' ? 'YOU' : 'AI' }}</span>
          <span class="sd-text">{{ r.content }}</span>
        </button>
        <div v-if="!searchResults.length" class="sd-empty">
          {{ searching ? t('rail.searching') : t('rail.noMatch') }}
        </div>
      </div>
      </div>
      <button
        class="pb-btn view-switch"
        :class="{ on: ui.view === 'group' }"
        :data-tip="ui.view === 'group' ? t('rail.singleView') : t('rail.groupView')"
        @click="ui.toggleView()"
      >
        <svg viewBox="0 0 24 24">
          <circle cx="9" cy="8.2" r="3.1" />
          <path d="M3.2 19.4c0-3.1 2.6-5.6 5.8-5.6s5.8 2.5 5.8 5.6" />
          <path d="M15.6 5.4a3 3 0 010 5.7" />
          <path d="M17.2 13.9c2.1.5 3.6 2.4 3.6 4.6" />
        </svg>
      </button>
      <span class="rail-sep" aria-hidden="true"></span>
      <button class="pb-btn" :data-tip="t('rail.search')" @click="toggleSearch">
        <svg viewBox="0 0 24 24">
          <circle cx="11" cy="11" r="6" />
          <path d="M20 20l-4.2-4.2" />
        </svg>
      </button>
      <button class="pb-btn" :data-tip="t('rail.cast')" @click="ui.openPanel('drawer')">
        <svg viewBox="0 0 24 24">
          <path d="M4 5a2 2 0 012-2h13v18H6a2 2 0 01-2-2V5z" />
          <path d="M8 3v18" />
        </svg>
      </button>
      <button class="pb-btn" :data-tip="t('rail.script')" @click="ui.openPanel('script')">
        <svg viewBox="0 0 24 24">
          <path d="M4 6h9M4 10h6M4 14h4" />
          <path d="M13.5 20l1-3.6 5.2-5.2a2 2 0 00-2.8-2.8l-5.2 5.2L10.7 17l2.8 3z" />
          <path d="M12.2 13.9l2.8 2.8" />
        </svg>
      </button>
      <button class="pb-btn" :data-tip="t('rail.memory')" @click="ui.openPanel('memory')">
        <svg viewBox="0 0 24 24">
          <path d="M12 3a4.5 4.5 0 00-4.5 4.5A3.5 3.5 0 006 11a3.5 3.5 0 001.5 2.9A4 4 0 0012 21z" />
          <path d="M12 3a4.5 4.5 0 014.5 4.5A3.5 3.5 0 0118 11a3.5 3.5 0 01-1.5 2.9A4 4 0 0112 21z" />
          <path d="M12 3v18" />
        </svg>
      </button>
      <!-- Lore (world book) is hidden on purpose. This project is a card-driven
           single-character chat: the character card already carries all the
           setting it needs, and the memory engine covers "what happened between
           us" automatically. World book has never been used here, and its
           multi-book path is still unverified, so the entry point is commented
           out rather than deleted. Uncomment this block if a multi-character or
           large-world use case ever shows up; the panel, the API layer and the
           backend are all intact.
      <button class="pb-btn" :data-tip="t('rail.lore')" @click="ui.openPanel('lore')">
        <svg viewBox="0 0 24 24">
          <path d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z" />
          <path d="M12 12l8-4.5M12 12v9M12 12L4 7.5" />
        </svg>
      </button>
      -->
      <button class="pb-btn" :data-tip="t('rail.rules')" @click="ui.openPanel('rules')">
        <svg viewBox="0 0 24 24">
          <path d="M6 4h7l5 5v11H6z" />
          <path d="M13 4v5h5" />
          <path d="M9 13h6M9 16h4" />
        </svg>
      </button>
      <button class="pb-btn" :data-tip="t('rail.images')" @click="ui.openPanel('gallery')">
        <svg viewBox="0 0 24 24">
          <rect x="3" y="5" width="18" height="14" rx="1" />
          <circle cx="8.5" cy="10" r="1.6" />
          <path d="M21 16l-5-4-4 3-3-2-6 5" />
        </svg>
      </button>
      <button class="pb-btn" :data-tip="t('rail.compaction')" @click="ui.openPanel('compact')">
        <svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h10M4 17h13" /></svg>
      </button>
      <button class="pb-btn" :data-tip="t('rail.backstage')" @click="ui.openPanel('backstage')">
        <svg viewBox="0 0 24 24">
          <path d="M6 3.5v5.5M6 15v5.5M12 3.5v8.5M12 18v2.5M18 3.5v3.5M18 12.5v8" />
          <circle cx="6" cy="12" r="2" />
          <circle cx="12" cy="15.5" r="2" />
          <circle cx="18" cy="10" r="2" />
        </svg>
      </button>

      <div class="dial-h">
        <button class="dial-btn lang-btn" :data-tip="langTip" @click="cycleLocale()">
          {{ locale === 'en' ? 'EN' : '中' }}
        </button>
        <button class="dial-btn" :data-tip="fontTip" @click="ui.cycleFont()">
          <svg viewBox="0 0 24 24">
            <path d="M5 18.5L10.2 5.5h1.6L17 18.5" />
            <path d="M7.6 13.6h6.8" />
          </svg>
        </button>
        <button class="dial-btn" :data-tip="t('rail.dayNight')" @click="ui.toggleTheme($event.currentTarget as HTMLElement)">
          <svg class="ico-moon" viewBox="0 0 24 24">
            <path d="M20 14.5A8.5 8.5 0 019.5 4a8.5 8.5 0 1010.5 10.5z" />
          </svg>
          <svg class="ico-sun" viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="4.2" />
            <path
              d="M12 2.6v2.2M12 19.2v2.2M2.6 12h2.2M19.2 12h2.2M5.4 5.4l1.6 1.6M17 17l1.6 1.6M18.6 5.4L17 7M7 17l-1.6 1.6"
            />
          </svg>
        </button>
        <div class="accent-wrap">
          <button class="dial-btn accent-btn" :data-tip="t('rail.accent')" @click="accentOpen = !accentOpen">
            <span class="accent-dot"></span>
          </button>
          <div v-if="accentOpen" class="accent-menu">
            <button
              v-for="a in accents"
              :key="a"
              class="accent-item"
              :class="{ on: ui.accent === a }"
              @click="pickAccent(a)"
            >
              <span class="swatch" :data-accent="a"></span>
              <span class="accent-name">{{ accentTip(a) }}</span>
            </button>
          </div>
        </div>

        <div class="win-ctrls" :class="{ off: !desktop }">
          <button class="win-btn" :disabled="!desktop" aria-label="minimize" @click="minimizeWindow()">
            <svg viewBox="0 0 24 24"><path d="M6 12h12" /></svg>
          </button>
          <button class="win-btn" :disabled="!desktop" aria-label="maximize" @click="toggleMaximizeWindow()">
            <svg viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="1" /></svg>
          </button>
          <button class="win-btn close" :disabled="!desktop" aria-label="close" @click="closeWindow()">
            <svg viewBox="0 0 24 24"><path d="M7 7l10 10M17 7L7 17" /></svg>
          </button>
        </div>
      </div>
    </div>
  </header>
</template>

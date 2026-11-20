<script setup lang="ts">
import { useUiStore } from '@/stores/ui'
import { computed, ref, watch } from 'vue'
import { useSessionStore } from '@/stores/session'
import { useI18n } from '@/i18n'
import { compactBranch, getCompactedMessages, getCompactionHistory } from '@/api/compaction'
import { useChatStore } from '@/stores/chat'
import { llmConfig } from '@/stores/chat'
import type { CompactionRecord } from '@/data/compaction'
import {
  compactedGroupMessages,
  compactGroup,
  listGroupCompactions,
  type GroupCompaction,
  type GroupMessage,
} from '@/api/group'
import { formatDateTime } from '@/utils/time'

const ui = useUiStore()
const session = useSessionStore()
const chat = useChatStore()
const { t } = useI18n()

const records = ref<CompactionRecord[]>([])
const loading = ref(false)
const error = ref('')

/**
 * Two unrelated histories share this panel: the companion's compactions and the
 * room's. Switching the tab swaps the source; nothing is merged, and each view
 * keeps the opposite mode's compactions hidden.
 */
const isGroupView = computed(() => ui.view === 'group')
const tab = ref<'single' | 'group'>(ui.view === 'group' ? 'group' : 'single')
const groupRecords = ref<GroupCompaction[]>([])
const groupLoading = ref(false)

const roomId = computed(() => ui.groupRoomId)
const singleRecords = computed(() => (isGroupView.value ? [] : records.value))
const visibleGroupRecords = computed(() => (isGroupView.value ? groupRecords.value : []))

async function loadGroupRecords(): Promise<void> {
  const targetRoomId = roomId.value
  if (!isGroupView.value || !targetRoomId) {
    groupRecords.value = []
    return
  }
  groupLoading.value = true
  try {
    const r = await listGroupCompactions(targetRoomId)
    if (!isGroupView.value || roomId.value !== targetRoomId) return
    groupRecords.value = r.compactions ?? []
  } catch {
    if (roomId.value === targetRoomId) groupRecords.value = []
  } finally {
    if (roomId.value === targetRoomId || !isGroupView.value) {
      groupLoading.value = false
    }
  }
}

watch(
  () => [ui.isOpen('compact'), tab.value, isGroupView.value] as const,
  ([open, which, inGroup]) => {
    if (open && which === 'group' && inGroup) void loadGroupRecords()
  },
  { immediate: true },
)

function fmtTokens(n: number): string {
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k'
  return String(n)
}

async function load() {
  records.value = []
  if (isGroupView.value) return
  const cid = session.activeCompanionId
  const bid = session.activeBranch
  if (!cid || !bid) return
  loading.value = true
  error.value = ''
  try {
    const list = await getCompactionHistory(cid, bid)
    if (isGroupView.value || session.activeCompanionId !== cid || session.activeBranch !== bid) return
    records.value = list.map((r) => ({
      id: r.id,
      time: formatDateTime(r.created_at),
      count: r.message_count,
      saved: fmtTokens(Math.max(0, r.tokens_before - r.tokens_after)),
      preview: r.summary || '',
    }))
  } catch (e) {
    if (!isGroupView.value) error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

watch(
  () => [session.activeCompanionId, session.activeBranch, ui.isOpen('compact'), isGroupView.value] as const,
  ([, , open, inGroup]) => {
    if (open && !inGroup) void load()
  },
  { immediate: true },
)

const showRaw = ref(false)
const rawMessages = ref<Array<{ id: number; role: string; content: string }>>([])

async function toggleRaw() {
  if (isGroupView.value) return
  if (showRaw.value) {
    showRaw.value = false
    return
  }
  const cid = session.activeCompanionId
  const bid = session.activeBranch
  if (!cid || !bid) return
  try {
    const raw = await getCompactedMessages(cid, bid)
    if (isGroupView.value || session.activeCompanionId !== cid || session.activeBranch !== bid) return
    rawMessages.value = raw
    showRaw.value = true
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

const busy = ref(false)
const notice = ref('')

/* ---- room side ---- */
/* Same two controls as the companion side, pointed at the room's own tables:
   compact on demand, and read back the turns a summary covers. */
const groupShowRaw = ref(false)
const groupRawMessages = ref<GroupMessage[]>([])
const groupBusy = ref(false)
const groupNotice = ref('')

watch(
  () => ui.view,
  (view) => {
    tab.value = view === 'group' ? 'group' : 'single'
    if (view === 'group') {
      records.value = []
      showRaw.value = false
      rawMessages.value = []
      notice.value = ''
    } else {
      groupRecords.value = []
      groupShowRaw.value = false
      groupRawMessages.value = []
      groupNotice.value = ''
    }
    error.value = ''
  },
)

watch(
  () => ui.isOpen('compact'),
  (open) => {
    if (open) tab.value = isGroupView.value ? 'group' : 'single'
  },
)

watch(roomId, (id) => {
  groupRecords.value = []
  groupShowRaw.value = false
  groupRawMessages.value = []
  groupNotice.value = ''
  error.value = ''
  if (ui.isOpen('compact') && isGroupView.value && tab.value === 'group' && id) {
    void loadGroupRecords()
  }
})

watch(
  () => [session.activeCompanionId, session.activeBranch] as const,
  () => {
    records.value = []
    showRaw.value = false
    rawMessages.value = []
    notice.value = ''
    error.value = ''
  },
)

async function toggleGroupRaw(): Promise<void> {
  if (!isGroupView.value) return
  if (groupShowRaw.value) {
    groupShowRaw.value = false
    return
  }
  const targetRoomId = roomId.value
  if (!targetRoomId) return
  try {
    const r = await compactedGroupMessages(targetRoomId)
    if (!isGroupView.value || roomId.value !== targetRoomId) return
    groupRawMessages.value = r.messages ?? []
    groupShowRaw.value = true
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function compactGroupNow(): Promise<void> {
  const targetRoomId = roomId.value
  if (!isGroupView.value || !targetRoomId || groupBusy.value) return
  groupBusy.value = true
  groupNotice.value = ''
  try {
    const r = await compactGroup(targetRoomId, llmConfig())
    if (roomId.value !== targetRoomId) return
    groupNotice.value = `${r.messages_compacted} messages folded into a summary.`
    await loadGroupRecords()
  } catch (e) {
    if (roomId.value === targetRoomId) {
      groupNotice.value = e instanceof Error ? e.message : String(e)
    }
  } finally {
    groupBusy.value = false
  }
}

async function compactNow() {
  if (isGroupView.value) return
  const cid = session.activeCompanionId
  const bid = session.activeBranch
  if (!cid || !bid || busy.value) return
  busy.value = true
  notice.value = ''
  error.value = ''
  try {
    const r = await compactBranch(cid, bid, {
      preserve_recent: 5,
      max_summary_tokens: 2000,
      preserve_important: true,
      max_important_messages: 20,
    })
    if (r && r.success === false) notice.value = r.error || ''
    await load()
    await chat.loadMessages()
    await session.loadTokens()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

const totalSaved = computed(() =>
  singleRecords.value.reduce((sum, r) => sum + Number(r.saved.replace(/[^\d.]/g, '') || 0), 0),
)

function fmt(n: number) {
  return n.toLocaleString('en-US').replace(',', ' ')
}

</script>

<template>
  <aside class="panel" :class="{ show: ui.isOpen('compact') }">
    <div class="d-head">
      <span class="d-kicker">{{ t('compact.title') }}</span>
      <span class="d-title">{{ t('compact.title') }}</span>
      <button class="d-close" @click="ui.closePanel('compact')">
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>

    <div class="d-tabs">
      <button class="d-tab" :class="{ on: tab === 'single' }" @click="tab = 'single'">
        {{ t('panel.single') }}
      </button>
      <button class="d-tab" :class="{ on: tab === 'group' }" @click="tab = 'group'">
        {{ t('panel.group') }}
      </button>
    </div>

    <div v-if="tab === 'single'" class="d-body">
      <p v-if="isGroupView" class="mem-note">{{ t('compact.isolated') }}</p>
      <template v-else>
        <div class="cmp-stat">
          <div><span class="cmp-k">{{ t('compact.compactions') }}</span><span class="cmp-v">{{ singleRecords.length }}</span></div>
          <div><span class="cmp-k">{{ t('compact.tokensSaved') }}</span><span class="cmp-v">{{ fmt(totalSaved) }}</span></div>
          <div><span class="cmp-k">{{ t('compact.now') }}</span><span class="cmp-v">{{ fmt(session.tokensUsed) }}</span></div>
        </div>

        <div v-for="r in singleRecords" :key="r.id" class="cmp-item">
          <div class="cmp-head">
            <span class="cmp-time">{{ r.time }}</span>
            <span class="cmp-n">{{ r.count }} {{ t('compact.msgUnit') }} · {{ r.saved }} {{ t('compact.savedUnit') }}</span>
          </div>
          <p class="cmp-preview">{{ r.preview }}</p>
        </div>
        <div v-if="showRaw" class="raw-list">
          <div v-for="m in rawMessages" :key="m.id" class="raw-row">
            <span class="raw-role">{{ m.role }}</span>
            <p class="raw-text">{{ m.content }}</p>
          </div>
          <div v-if="!rawMessages.length" class="eb-hint">{{ t('empty.noCompaction') }}</div>
        </div>
        <div v-if="notice" class="empty-block"><div class="eb-hint">{{ notice }}</div></div>
        <div v-if="!singleRecords.length" class="empty-block">
          <div class="eb-title">{{ t('empty.noCompaction') }}</div>
          <div class="eb-hint">{{ t('empty.noCompactionHint') }}</div>
        </div>
      </template>
    </div>

    <div v-else class="d-body">
      <p v-if="!isGroupView" class="mem-note">{{ t('compact.isolatedGroup') }}</p>
      <template v-else>
        <p class="mem-note">{{ t('compact.groupNote') }}</p>
        <div v-if="groupLoading" class="empty-block">
          <div class="eb-title">{{ t('memory.loading') }}</div>
        </div>
        <div v-else-if="!visibleGroupRecords.length" class="empty-block">
          <div class="eb-title">{{ t('empty.noCompaction') }}</div>
          <div class="eb-hint">{{ t('compact.groupEmptyHint') }}</div>
        </div>
        <div v-for="c in visibleGroupRecords" :key="c.id" class="grp-row">
          <div class="grp-meta">
            <span class="grp-who">{{ c.covered_count ?? c.messages_compacted }} {{ t('compact.msgUnit') }}</span>
            <span v-if="c.created_at" class="grp-time">{{ c.created_at }}</span>
          </div>
          <p class="grp-text">{{ c.summary }}</p>
        </div>

        <div v-if="groupShowRaw" class="raw-list">
          <div v-for="m in groupRawMessages" :key="m.id" class="raw-row">
            <span class="raw-role">{{ m.speaker_name || m.role }}</span>
            <p class="raw-text">{{ m.content }}</p>
          </div>
          <div v-if="!groupRawMessages.length" class="eb-hint">{{ t('empty.noCompaction') }}</div>
        </div>
        <div v-if="groupNotice" class="empty-block"><div class="eb-hint">{{ groupNotice }}</div></div>
      </template>
    </div>

    <div v-if="tab === 'single' && !isGroupView" class="d-foot">
      <button class="mini-btn primary" :disabled="busy" @click="compactNow">{{ t('compact.compactNow') }}</button>
      <button class="mini-btn" @click="toggleRaw">{{ t('compact.viewRaw') }}</button>
    </div>
    <div v-else-if="tab === 'group' && isGroupView" class="d-foot">
      <button class="mini-btn primary" :disabled="groupBusy || !roomId" @click="compactGroupNow">
        {{ groupBusy ? t('compact.compacting') : t('compact.compactNow') }}
      </button>
      <button class="mini-btn" :disabled="!roomId" @click="toggleGroupRaw">{{ t('compact.viewRaw') }}</button>
    </div>
  </aside>
</template>

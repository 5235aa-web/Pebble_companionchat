<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useUiStore } from '@/stores/ui'
import { useScriptStore } from '@/stores/script'
import { useSessionStore } from '@/stores/session'
import { useChatStore } from '@/stores/chat'
import { useI18n } from '@/i18n'

const ui = useUiStore()
const script = useScriptStore()
const session = useSessionStore()
const chat = useChatStore()
const { t, locale } = useI18n()

const draftTitle = ref('')
const draftPremise = ref('')
const draftNodes = ref<Array<{ title: string; instruction: string; turns: number }>>([])
const busy = ref(false)
const notice = ref('')

const selectedId = computed(() => script.current?.id ?? 0)
const canStart = computed(() => Boolean(session.activeCompanionId) && draftNodes.value.length > 0)

function syncDraft() {
  draftTitle.value = script.current?.title ?? ''
  draftPremise.value = script.current?.premise ?? ''
  draftNodes.value = script.nodes.map((n) => ({
    title: n.title,
    instruction: n.instruction,
    turns: Number(n.turns ?? 0),
  }))
}

async function load() {
  busy.value = true
  try {
    await script.loadScripts()
    if (script.scripts.length && !script.current) {
      await pick(script.scripts[0].id)
    } else {
      syncDraft()
    }
  } finally {
    busy.value = false
  }
}

async function pick(id: number) {
  busy.value = true
  notice.value = ''
  try {
    await script.open(id)
    syncDraft()
  } finally {
    busy.value = false
  }
}

async function create() {
  busy.value = true
  notice.value = ''
  try {
    const id = await script.create(t('script.new'), '')
    syncDraft()
    await pick(id)
  } finally {
    busy.value = false
  }
}

function addNode() {
  draftNodes.value.push({ title: '', instruction: '', turns: 0 })
}

function removeNode(index: number) {
  draftNodes.value.splice(index, 1)
}

function moveNode(index: number, delta: number) {
  const target = index + delta
  if (target < 0 || target >= draftNodes.value.length) return
  const list = draftNodes.value
  const [item] = list.splice(index, 1)
  list.splice(target, 0, item)
}

async function save() {
  if (!selectedId.value) return
  busy.value = true
  notice.value = ''
  try {
    await script.saveMeta(draftTitle.value.trim(), draftPremise.value)
    script.nodes = draftNodes.value.map((n) => ({ ...n }))
    await script.persistNodes()
    syncDraft()
    notice.value = t('script.saved')
  } finally {
    busy.value = false
  }
}

async function removeScript() {
  if (!selectedId.value) return
  ui.askConfirm({
    title: t('script.delete'),
    text: t('script.deleteConfirm'),
    onConfirm: () => {
      void (async () => {
        await script.remove(selectedId.value)
        if (script.scripts.length) await pick(script.scripts[0].id)
        else syncDraft()
      })()
    },
  })
}

async function start() {
  if (!selectedId.value || !session.activeCompanionId || !draftNodes.value.length) return
  busy.value = true
  notice.value = ''
  try {
    // Unsaved edits would be lost: the session reads scenes from the database.
    await save()
    const r = await script.start(session.activeCompanionId, locale.value)
    if (!r?.session) {
      notice.value = t('script.noCompanion')
      return
    }
    await session.loadBranches()
    session.selectBranch(r.session.branch_id)
    await chat.loadMessages()
    await session.loadTokens()
    ui.closePanel('script')
  } catch (err) {
    notice.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <aside class="panel" :class="{ show: ui.isOpen('script') }">
    <div class="d-head">
      <span class="d-kicker">{{ t('script.title') }}</span>
      <span class="d-title">{{ t('script.title') }}</span>
      <button class="d-close" @click="ui.closePanel('script')">
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>

    <div class="d-body sc-body">
      <div class="sc-layout">
        <div class="sc-list">
          <button class="mini-btn primary sc-new" :disabled="busy" @click="create">
            {{ t('script.new') }}
          </button>
          <button
            v-for="s in script.scripts"
            :key="s.id"
            class="sc-item"
            :class="{ on: s.id === selectedId }"
            @click="pick(s.id)"
          >
            <span class="sc-item-title">{{ s.title }}</span>
            <span class="sc-item-meta">{{ s.node_count ?? 0 }}</span>
          </button>
          <div v-if="!script.scripts.length" class="eb-hint">{{ t('script.empty') }}</div>
        </div>

        <div v-if="selectedId" class="sc-edit">
          <label class="pf-label">{{ t('script.name') }}</label>
          <input v-model="draftTitle" class="pf-input" :placeholder="t('script.name')" />

          <label class="pf-label">{{ t('script.premise') }}</label>
          <textarea v-model="draftPremise" class="pf-area" rows="3" :placeholder="t('script.premise')"></textarea>

          <div class="sc-scenes-head">
            <span class="pf-label">{{ t('script.scenes') }}</span>
            <button class="mini-btn" @click="addNode">{{ t('script.addScene') }}</button>
          </div>

          <div v-for="(node, i) in draftNodes" :key="i" class="sc-node">
            <div class="sc-node-head">
              <span class="sc-node-n">{{ i + 1 }}</span>
              <input v-model="node.title" class="pf-input sc-node-title" :placeholder="t('script.sceneTitle')" />
              <input
                v-model.number="node.turns"
                class="pf-input sc-node-turns"
                type="number"
                min="0"
                max="200"
                :aria-label="t('script.turns')"
                :placeholder="t('script.turns')"
              />
              <button class="sc-mini" :disabled="i === 0" :aria-label="t('script.moveUp')" @click="moveNode(i, -1)">↑</button>
              <button class="sc-mini" :disabled="i === draftNodes.length - 1" :aria-label="t('script.moveDown')" @click="moveNode(i, 1)">↓</button>
              <button class="sc-mini" :aria-label="t('script.remove')" @click="removeNode(i)">×</button>
            </div>
            <textarea
              v-model="node.instruction"
              class="pf-area"
              rows="3"
              :placeholder="t('script.sceneInstruction')"
            ></textarea>
          </div>

          <div v-if="!draftNodes.length" class="eb-hint">{{ t('script.empty') }}</div>
          <div v-if="!session.activeCompanionId" class="eb-hint">{{ t('script.noCompanion') }}</div>
          <div v-if="notice" class="eb-hint">{{ notice }}</div>
        </div>

        <div v-else class="sc-edit sc-edit-empty">
          <p class="sc-empty-hint">{{ t('script.noScriptHint') }}</p>
        </div>
      </div>
    </div>

    <div class="d-foot">
      <button class="mini-btn primary" :disabled="busy || !selectedId" @click="start">
        {{ t('script.start') }}
      </button>
      <button class="mini-btn" :disabled="busy || !selectedId" @click="save">{{ t('script.save') }}</button>
      <button class="mini-btn" :disabled="busy || !selectedId" @click="removeScript">{{ t('script.delete') }}</button>
    </div>
  </aside>
</template>

<style scoped>
.sc-body { padding-top: 4px; }
.sc-edit-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 200px;
  text-align: center;
}
.sc-empty-hint {
  max-width: 280px;
  margin: 0;
  font-size: 12.5px;
  line-height: 1.9;
  color: var(--ink-4);
}
.sc-edit .pf-label { display: block; margin: 8px 0 4px; font-family: var(--mono); font-size: 10px; color: var(--ink-3); }
.sc-layout { display: flex; gap: 14px; min-height: 0; }
.sc-list { width: 168px; flex: none; display: flex; flex-direction: column; gap: 6px; }
.sc-new { width: 100%; }
.sc-item {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 7px 9px; border: 1px solid transparent; border-radius: 3px;
  background: rgb(var(--ink-rgb) / .02); color: var(--ink-2);
  font-family: var(--serif); font-size: 13px; text-align: left; cursor: pointer;
}
.sc-item.on { border-color: var(--line); color: var(--ink-1); background: rgb(var(--ink-rgb) / .05); }
.sc-item-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sc-item-meta { font-family: var(--mono); font-size: 10px; color: var(--ink-3); }
.sc-edit { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 6px; overflow-y: auto; }
.sc-scenes-head { display: flex; align-items: center; justify-content: space-between; margin-top: 8px; }
.sc-node { border: 1px solid var(--line); border-radius: 3px; padding: 8px; display: flex; flex-direction: column; gap: 6px; }
.sc-node-head { display: flex; align-items: center; gap: 6px; }
.sc-node-n { font-family: var(--mono); font-size: 11px; color: var(--ink-3); width: 16px; flex: none; }
.sc-node-title { flex: 1; }
.sc-node-turns { width: 54px; flex: none; text-align: center; }
.sc-mini {
  width: 22px; height: 22px; flex: none; border: 1px solid var(--line); border-radius: 3px;
  background: transparent; color: var(--ink-2); cursor: pointer; font-size: 12px; line-height: 1;
}
.sc-mini:disabled { opacity: .35; cursor: default; }
</style>

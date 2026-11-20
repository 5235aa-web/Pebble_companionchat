<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { getSessionNodes, saveSessionNodes, type ScriptNode } from '@/api/script'
import { useScriptStore } from '@/stores/script'
import { useSessionStore } from '@/stores/session'
import { useUiStore } from '@/stores/ui'
import { useI18n } from '@/i18n'

const script = useScriptStore()
const session = useSessionStore()
const ui = useUiStore()
const { t } = useI18n()

const nodes = ref<ScriptNode[]>([])
const busy = ref(false)
const notice = ref('')

const show = computed(() => ui.isOpen('scene-copy'))
/** Played scenes plus the running one: only what comes after them may change. */
const locked = computed(() => script.currentIndex + 1)

watch(show, (open) => {
  if (open) void load()
})

async function load() {
  const s = script.session
  if (!s) return
  busy.value = true
  notice.value = ''
  try {
    const r = await getSessionNodes(s.id)
    nodes.value = (r.nodes ?? []).map((n) => ({
      id: n.id,
      title: n.title,
      instruction: n.instruction,
      turns: Number(n.turns ?? 0),
    }))
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

function addScene() {
  nodes.value.push({ title: '', instruction: '', turns: 0 })
}

function removeScene(i: number) {
  if (i < locked.value) return
  nodes.value.splice(i, 1)
}

async function save() {
  const s = script.session
  if (!s) return
  busy.value = true
  notice.value = ''
  try {
    const r = await saveSessionNodes(s.id, nodes.value)
    // Take the rows back so a second save updates the same scenes instead of
    // inserting the list again.
    nodes.value = (r.nodes ?? []).map((n) => ({
      id: n.id,
      title: n.title,
      instruction: n.instruction,
      turns: Number(n.turns ?? 0),
    }))
    // The rail reads the session payload, so it has to pick up the new copy.
    await script.loadActive(session.activeCompanionId)
    notice.value = t('script.copySaved')
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <aside class="panel cp" :class="{ show }">
    <div class="d-head">
      <span class="d-kicker">{{ t('script.copyKicker') }}</span>
      <span class="d-title">{{ script.currentNode?.title || t('script.copyTitle') }}</span>
      <button class="d-close" @click="ui.closePanel('scene-copy')">
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>

    <div class="d-body cp-body">
      <p class="cp-hint">{{ t('script.copyHint') }}</p>

      <div v-for="(node, i) in nodes" :key="i" class="cp-node" :class="{ locked: i < locked }">
        <div class="cp-head">
          <span class="cp-n">{{ i + 1 }}</span>
          <input
            v-model="node.title"
            class="pf-input cp-title"
            :disabled="i < locked"
            :placeholder="t('script.sceneTitle')"
          />
          <input
            v-model.number="node.turns"
            class="pf-input cp-turns"
            type="number"
            min="0"
            max="200"
            :disabled="i < locked"
            :aria-label="t('script.turns')"
            :placeholder="t('script.turns')"
          />
          <button
            class="cp-mini"
            :disabled="i < locked"
            :aria-label="t('script.remove')"
            @click="removeScene(i)"
          >
            ×
          </button>
        </div>
        <textarea
          v-model="node.instruction"
          class="pf-area"
          rows="3"
          :disabled="i < locked"
          :placeholder="t('script.sceneInstruction')"
        ></textarea>
      </div>

      <div v-if="!nodes.length" class="eb-hint">{{ t('script.empty') }}</div>

      <div v-if="notice" class="eb-hint">{{ notice }}</div>
    </div>

    <div class="d-foot cp-foot">
      <button class="mini-btn" @click="addScene">{{ t('script.addScene') }}</button>
      <button class="mini-btn primary" :disabled="busy" @click="save">
        {{ t('script.copySave') }}
      </button>
    </div>
  </aside>
</template>

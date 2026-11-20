<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { useUiStore } from '@/stores/ui'
import { useSessionStore } from '@/stores/session'
import { useI18n } from '@/i18n'
import type { CharacterCard } from '@/data/character'
import {
  createCompanion,
  exportCompanion,
  getCompanion,
  updateCompanion,
  uploadCompanionAvatar,
  type Companion,
} from '@/api/companion'
import { useFileUrl } from '@/composables/useFileUrl'
import { getTtsVoices } from '@/api/tts'

const ui = useUiStore()
const session = useSessionStore()
const { t } = useI18n()

const empty: CharacterCard = {
  avatar: '',
  name: '',
  role: '',
  tags: [],
  description: '',
  personality: '',
  scenario: '',
  firstMes: '',
  mesExample: '',
  advancedPrompt: '',
  systemPrompt: '',
  postHistory: '',
  userInfo: '',
  creatorNotes: '',
  raw: '',
  ttsVoiceRef: '',
  ttsVoiceId: '',
}

const card = reactive<CharacterCard>({ ...empty })

// Declared after `card`: the composable reads the source immediately, and a
// reference before the const is initialised throws a TDZ error at mount time.
const avatarUrl = useFileUrl(() => card.avatar, 1024)
const newTag = ref('')
const rawOpen = ref(false)
const saving = ref(false)
const error = ref('')

/**
 * Reference clips available to the cloning engines. Loaded once per open rather
 * than per character: the list is a property of the voice library, not of the
 * card being edited.
 */
const voiceClips = ref<string[]>([])

const collapsed = reactive({
  identity: false,
  voice: false,
  tts: true,
  hood: true,
})

const fromCompanion = (c: Companion): CharacterCard => ({
  avatar: c.avatar_path || '',
  name: c.name || '',
  role: c.creator || '',
  tags: [...(c.tags ?? [])],
  description: c.description || '',
  personality: c.personality || '',
  scenario: c.scenario || '',
  firstMes: c.first_mes || '',
  mesExample: c.mes_example || '',
  advancedPrompt: c.advanced_prompt || '',
  systemPrompt: c.system_prompt || '',
  postHistory: c.post_history_instructions || '',
  userInfo: c.user_info || '',
  creatorNotes: c.creator_notes || '',
  raw: c.raw_card_data || '',
  ttsVoiceRef: typeof c.tts_config?.voiceRef === 'string' ? c.tts_config.voiceRef : '',
  ttsVoiceId: typeof c.tts_config?.voiceId === 'string' ? c.tts_config.voiceId : '',
})

async function load() {
  void loadVoiceClips()
  // A draft is a card that does not exist yet: blank it out instead of loading
  // whichever companion happens to be selected behind the panel.
  if (ui.newCastDraft) {
    Object.assign(card, empty)
    error.value = ''
    return
  }
  const id = session.activeCompanionId
  if (!id) {
    Object.assign(card, empty)
    return
  }
  try {
    const c = await getCompanion(id)
    Object.assign(card, c ? fromCompanion(c) : empty)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

/** Failure here is not worth surfacing: the field simply stays a text input. */
async function loadVoiceClips() {
  try {
    voiceClips.value = await getTtsVoices()
  } catch {
    voiceClips.value = []
  }
}

watch(
  () => [session.activeCompanionId, ui.isOpen('profile')],
  () => {
    if (ui.isOpen('profile')) {
      void load()
    } else if (ui.newCastDraft) {
      // The panel closed without saving: drop the draft so the next open starts
      // from the selected companion again.
      ui.clearNewCast()
    }
  },
  { immediate: true },
)

async function save() {
  const draft = ui.newCastDraft
  const id = draft ? 'card-' + Date.now().toString(36) : session.activeCompanionId
  if (!id) return
  if (draft && !card.name.trim()) {
    error.value = t('card.nameRequired')
    return
  }
  saving.value = true
  error.value = ''
  try {
    const fields = {
      name: card.name,
      description: card.description,
      personality: card.personality,
      scenario: card.scenario,
      first_mes: card.firstMes,
      mes_example: card.mesExample,
      advanced_prompt: card.advancedPrompt,
      system_prompt: card.systemPrompt,
      post_history_instructions: card.postHistory,
      user_info: card.userInfo,
      creator_notes: card.creatorNotes,
      tags: card.tags,
      creator: card.role,
      // Voice identity lives on the companion row so it travels with the card
      // and survives a restart; the engine itself stays in the TTS panel.
      tts_config: {
        voiceRef: card.ttsVoiceRef.trim(),
        voiceId: card.ttsVoiceId.trim(),
      },
    }
    // A draft only reaches the database here, so backing out of the editor
    // leaves the cast list untouched.
    if (draft) await createCompanion(id, fields)
    else await updateCompanion(id, fields)
    ui.clearNewCast()
    await session.loadCompanions()
    if (draft) await session.selectCompanion(id)
    ui.closePanel('profile')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

function download(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

const fileInput = ref<HTMLInputElement | null>(null)

function pickAvatar() {
  fileInput.value?.click()
}

async function onAvatarPicked(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  const id = session.activeCompanionId
  if (!file || !id) return
  try {
    const r = await uploadCompanionAvatar(id, file)
    card.avatar = r.avatar_path
    await session.loadCompanions()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function exportPng() {
  const id = session.activeCompanionId
  if (!id) return
  try {
    download(await exportCompanion(id, 'png'), (card.name || id) + '.png')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function exportJson() {
  const id = session.activeCompanionId
  if (!id) return
  try {
    download(await exportCompanion(id, 'json'), (card.name || id) + '.json')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

function removeTag(tag: string) {
  card.tags = card.tags.filter((x) => x !== tag)
}

function addTag() {
  const v = newTag.value.trim()
  if (!v) return
  card.tags.push(v)
  newTag.value = ''
}
</script>

<template>
  <div class="profile" :class="{ show: ui.isOpen('profile') }">
    <div class="pf">
      <div class="pf-head">
        <div class="pf-avatar-wrap">
          <img v-if="card.avatar" class="pf-avatar-big" :src="avatarUrl" :alt="card.name" />
          <div v-else class="pf-avatar-big">{{ (card.name || '?').charAt(0) }}</div>
          <input ref="fileInput" type="file" accept="image/*" hidden @change="onAvatarPicked" />
          <!-- Uploading needs a stored companion, so a draft offers it after save. -->
          <button v-if="!ui.newCastDraft" class="pf-avatar-btn" @click="pickAvatar">
            {{ t('card.change') }}
          </button>
        </div>
        <div class="pf-id">
          <input v-model="card.name" class="pf-name" aria-label="character name" />
          <div class="pf-role">{{ card.role }}</div>
        </div>
        <div v-if="!ui.newCastDraft" class="pf-exports">
          <button class="mini-btn" @click="exportPng">{{ t('card.exportPng') }}</button>
          <button class="mini-btn" @click="exportJson">{{ t('card.exportJson') }}</button>
        </div>
        <button class="d-close" @click="ui.closePanel('profile')">
          <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>

      <div class="pf-body">
        <!-- Identity -->
        <div class="pf-group" :class="{ collapsed: collapsed.identity }">
          <div class="pf-glabel" data-group @click="collapsed.identity = !collapsed.identity">
            <svg class="caret" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
            <span>{{ t('card.identity') }}</span>
          </div>
          <div class="pf-fields">
            <div class="pf-field">
              <label>{{ t('card.description') }}</label>
              <textarea v-model="card.description" class="pf-area" rows="3"></textarea>
            </div>
            <div class="pf-field">
              <label>{{ t('card.personality') }}</label>
              <textarea v-model="card.personality" class="pf-area" rows="2"></textarea>
            </div>
            <div class="pf-field">
              <label>{{ t('card.scenario') }}</label>
              <textarea v-model="card.scenario" class="pf-area" rows="2"></textarea>
            </div>
            <div class="pf-field">
              <label>{{ t('card.tags') }}</label>
              <div class="pf-tags">
                <span v-for="t in card.tags" :key="t" class="pf-tag">
                  {{ t }}
                  <button aria-label="remove tag" @click="removeTag(t)">
                    <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
                  </button>
                </span>
                <input
                  v-model="newTag"
                  class="pf-tag-input"
                  :placeholder="t('card.addTag')"
                  aria-label="add tag"
                  @keydown.enter="addTag"
                />
              </div>
            </div>
          </div>
        </div>

        <!-- Voice -->
        <div class="pf-group" :class="{ collapsed: collapsed.voice }">
          <div class="pf-glabel" data-group @click="collapsed.voice = !collapsed.voice">
            <svg class="caret" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
            <span>{{ t('card.voice') }}</span>
          </div>
          <div class="pf-fields">
            <div class="pf-field">
              <label>{{ t('card.firstMessage') }}</label>
              <textarea v-model="card.firstMes" class="pf-area" rows="3"></textarea>
            </div>
            <div class="pf-field">
              <label>{{ t('card.conversationExamples') }}</label>
              <textarea v-model="card.mesExample" class="pf-area" rows="4"></textarea>
            </div>
            <div class="pf-field">
              <label>{{ t('card.advancedPrompt') }}</label>
              <textarea v-model="card.advancedPrompt" class="pf-area" rows="3"></textarea>
            </div>
          </div>
        </div>

        <!-- TTS voice -->
        <div class="pf-group" :class="{ collapsed: collapsed.tts }">
          <div class="pf-glabel" data-group @click="collapsed.tts = !collapsed.tts">
            <svg class="caret" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
            <span>{{ t('card.ttsVoice') }}</span>
          </div>
          <div class="pf-fields">
            <div class="pf-field">
              <label>{{ t('card.voiceClip') }}</label>
              <select v-model="card.ttsVoiceRef" class="pf-input">
                <option value="">{{ t('card.voiceInherit') }}</option>
                <option v-for="v in voiceClips" :key="v" :value="v">{{ v }}</option>
              </select>
            </div>
            <div class="pf-field">
              <label>{{ t('card.voiceId') }}</label>
              <input v-model="card.ttsVoiceId" class="pf-input" :placeholder="t('card.voiceIdHint')" />
            </div>
          </div>
        </div>

        <!-- Under the hood -->
        <div class="pf-group" :class="{ collapsed: collapsed.hood }">
          <div class="pf-glabel" data-group @click="collapsed.hood = !collapsed.hood">
            <svg class="caret" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>
            <span>{{ t('card.underTheHood') }}</span>
          </div>
          <div class="pf-fields">
            <div class="pf-field">
              <label>{{ t('card.systemInstructions') }}</label>
              <textarea v-model="card.systemPrompt" class="pf-area" rows="3"></textarea>
            </div>
            <div class="pf-field">
              <label>{{ t('card.postHistory') }}</label>
              <textarea v-model="card.postHistory" class="pf-area" rows="2"></textarea>
            </div>
            <div class="pf-field">
              <label>{{ t('card.userInfo') }}</label>
              <textarea v-model="card.userInfo" class="pf-area" rows="2"></textarea>
            </div>
            <div class="pf-field">
              <label>{{ t('card.creatorNotes') }}</label>
              <textarea v-model="card.creatorNotes" class="pf-area" rows="2"></textarea>
            </div>
            <div class="pf-field">
              <button class="pf-raw-toggle" @click="rawOpen = !rawOpen">{{ t('card.rawJson') }}</button>
              <div class="pf-raw" :class="{ show: rawOpen }">
                <textarea v-model="card.raw" spellcheck="false" aria-label="raw card json"></textarea>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="pf-foot">
        <button class="mini-btn danger" @click="ui.openPanel('dialog')">{{ t('card.delete') }}</button>
        <div class="pf-right">
          <button class="mini-btn" @click="ui.closePanel('profile')">{{ t('card.cancel') }}</button>
          <button class="mini-btn primary" :disabled="saving" @click="save">{{ t('card.save') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

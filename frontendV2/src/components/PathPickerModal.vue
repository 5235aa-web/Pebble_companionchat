<script setup lang="ts">
import { ref, watch } from 'vue'
import { browsePath, type BrowseEntry } from '@/api/app'
import { useI18n } from '@/i18n'

const props = defineProps<{
  open: boolean
  title?: string
  initialPath?: string
  ext?: string
  dirsOnly?: boolean
  allowDir?: boolean
}>()

const emit = defineEmits<{
  close: []
  select: [path: string]
}>()

const { t } = useI18n()
const currentPath = ref('')
const parentPath = ref('')
const entries = ref<BrowseEntry[]>([])
const loading = ref(false)
const error = ref('')

async function loadDir(target: string) {
  loading.value = true
  error.value = ''
  try {
    const res = await browsePath(target, props.ext || '', Boolean(props.dirsOnly))
    currentPath.value = res.path
    parentPath.value = res.parent
    entries.value = res.entries
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    // Fall back to root/drive list if initialPath no longer exists
    if (target) {
      try {
        const res = await browsePath('', props.ext || '', Boolean(props.dirsOnly))
        currentPath.value = res.path
        parentPath.value = res.parent
        entries.value = res.entries
      } catch {
        /* keep original error */
      }
    }
  } finally {
    loading.value = false
  }
}

watch(
  () => props.open,
  (v) => {
    if (v) void loadDir(props.initialPath || '')
  },
)

function onClickEntry(entry: BrowseEntry) {
  if (entry.isDir) {
    void loadDir(entry.path)
  } else if (!props.dirsOnly) {
    emit('select', entry.path)
    emit('close')
  }
}

function chooseCurrentDir() {
  if (!currentPath.value) return
  emit('select', currentPath.value)
  emit('close')
}
</script>

<template>
  <div class="dialog-ov" :class="{ show: open }" @click.self="emit('close')">
    <div class="dialog path-dlg">
      <div class="dlg-kicker">{{ title || t('backstage.browseTitle') }}</div>
      <div class="path-dlg-bar">
        <button class="mini-btn" :disabled="loading || !currentPath" @click="loadDir(parentPath)">
          ↑ {{ t('backstage.browseUp') }}
        </button>
        <input
          v-model="currentPath"
          class="num-in path path-dlg-in"
          :placeholder="t('backstage.browseRootHint')"
          @keydown.enter.prevent="loadDir(currentPath)"
        />
        <button class="mini-btn" :disabled="loading" @click="loadDir(currentPath)">
          {{ t('backstage.browseGo') }}
        </button>
      </div>

      <div v-if="error" class="path-err">{{ error }}</div>

      <div class="path-dlg-list">
        <div v-if="loading" class="path-dlg-empty">{{ t('backstage.browseLoading') }}</div>
        <div v-else-if="!entries.length" class="path-dlg-empty">{{ t('backstage.browseEmpty') }}</div>
        <button
          v-for="e in entries"
          :key="e.path"
          class="path-dlg-row"
          :class="{ dir: e.isDir }"
          @click="onClickEntry(e)"
        >
          <span class="path-dlg-tag">{{ e.isDir ? 'DIR' : 'FILE' }}</span>
          <span class="path-dlg-name">{{ e.name }}</span>
          <span v-if="!e.isDir && e.sizeMb !== undefined" class="path-dlg-size">
            {{ e.sizeMb >= 1024 ? `${(e.sizeMb / 1024).toFixed(2)} GB` : `${e.sizeMb} MB` }}
          </span>
        </button>
      </div>

      <div class="dlg-actions">
        <button class="mini-btn" @click="emit('close')">{{ t('dialog.cancel') }}</button>
        <button
          v-if="dirsOnly || allowDir"
          class="mini-btn primary"
          :disabled="!currentPath"
          @click="chooseCurrentDir"
        >
          {{ t('backstage.browseChooseDir') }}
        </button>
      </div>
    </div>
  </div>
</template>

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  checkAppUpdate,
  getAppUpdateStatus,
  launchAppUpdate,
  startAppUpdateInstall,
  type UpdateCheckResult,
  type UpdateInstallStatus,
} from '@/api/app'
import { closeWindow, isDesktop } from '@/lib/window'

const EMPTY_STATUS: UpdateInstallStatus = {
  phase: 'idle',
  downloadedBytes: 0,
  totalBytes: 0,
  percent: 0,
  installerPath: '',
  assetName: '',
  error: '',
}

export const useUpdaterStore = defineStore('updater', () => {
  const panelOpen = ref(false)
  const checking = ref(false)
  const hasChecked = ref(false)
  const info = ref<UpdateCheckResult | null>(null)
  const checkError = ref('')
  const installStatus = ref<UpdateInstallStatus>({ ...EMPTY_STATUS })
  let pollTimer = 0

  const hasUpdate = computed(() => Boolean(info.value?.ok && info.value?.hasUpdate))
  const isBusy = computed(
    () =>
      installStatus.value.phase === 'downloading' ||
      installStatus.value.phase === 'launching',
  )

  function stopPolling(): void {
    if (pollTimer) {
      window.clearInterval(pollTimer)
      pollTimer = 0
    }
  }

  async function check(silent = false): Promise<void> {
    if (checking.value) return
    checking.value = true
    if (!silent) checkError.value = ''
    try {
      const res = await checkAppUpdate()
      info.value = res
      hasChecked.value = true
      if (!res.ok && !silent) {
        checkError.value = res.detail || res.error || 'check_failed'
      } else if (res.ok) {
        checkError.value = ''
      }
    } catch (err) {
      if (!silent) {
        checkError.value = err instanceof Error ? err.message : String(err)
      }
    } finally {
      checking.value = false
    }
  }

  function openPanel(): void {
    panelOpen.value = true
    if (!hasChecked.value || !info.value?.ok) {
      void check(false)
    }
  }

  function closePanel(): void {
    panelOpen.value = false
  }

  function togglePanel(): void {
    if (panelOpen.value) {
      closePanel()
    } else {
      openPanel()
    }
  }

  async function runInstall(): Promise<void> {
    const current = info.value
    if (!current || !current.hasUpdate) return

    const fallbackUrl = current.downloadUrl || current.releaseUrl
    if (!isDesktop()) {
      if (fallbackUrl) {
        window.open(fallbackUrl, '_blank', 'noopener,noreferrer')
      }
      return
    }

    if (!current.downloadUrl) {
      if (current.releaseUrl) {
        window.open(current.releaseUrl, '_blank', 'noopener,noreferrer')
      }
      return
    }

    stopPolling()
    try {
      installStatus.value = await startAppUpdateInstall(
        current.downloadUrl,
        current.assetName || '',
      )
      pollTimer = window.setInterval(() => {
        void pollProgress()
      }, 350)
    } catch (err) {
      installStatus.value = {
        ...EMPTY_STATUS,
        phase: 'error',
        error: err instanceof Error ? err.message : String(err),
      }
    }
  }

  async function pollProgress(): Promise<void> {
    try {
      const st = await getAppUpdateStatus()
      installStatus.value = st
      if (st.phase === 'ready') {
        stopPolling()
        await launchAppUpdate()
        installStatus.value = { ...st, phase: 'launching' }
        window.setTimeout(() => {
          void closeWindow()
        }, 350)
      } else if (st.phase === 'error' || st.phase === 'idle') {
        stopPolling()
      }
    } catch (err) {
      stopPolling()
      installStatus.value = {
        ...installStatus.value,
        phase: 'error',
        error: err instanceof Error ? err.message : String(err),
      }
    }
  }

  return {
    panelOpen,
    checking,
    hasChecked,
    info,
    checkError,
    installStatus,
    hasUpdate,
    isBusy,
    check,
    openPanel,
    closePanel,
    togglePanel,
    runInstall,
  }
})

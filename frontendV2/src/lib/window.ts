/**
 * Window controls for the packaged desktop build.
 *
 * A plain browser has no window chrome to drive, so every call degrades to a
 * no-op when Tauri is absent. We reach Tauri through its injected internals
 * rather than importing `@tauri-apps/api`: that package is only needed for the
 * desktop build, and a static import would break `vite dev` in a browser.
 */

interface TauriInternals {
  invoke: (command: string, args?: Record<string, unknown>) => Promise<unknown>
}

function internals(): TauriInternals | undefined {
  return (window as unknown as { __TAURI_INTERNALS__?: TauriInternals }).__TAURI_INTERNALS__
}

/** True when running inside the Tauri shell. */
export const isDesktop = (): boolean => Boolean(internals())

async function call(command: string): Promise<void> {
  const api = internals()
  if (!api) return
  try {
    await api.invoke(command, { label: 'main' })
  } catch (err) {
    console.warn(`[window] ${command} failed`, err)
  }
}

export const minimizeWindow = (): Promise<void> => call('plugin:window|minimize')
export const toggleMaximizeWindow = (): Promise<void> => call('plugin:window|toggle_maximize')
export const closeWindow = (): Promise<void> => call('plugin:window|close')

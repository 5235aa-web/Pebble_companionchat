import { onScopeDispose, ref, watch, type Ref } from 'vue'
import { fileUrl, onSocketReady } from '@/api/client'

/**
 * Resolve a stored path to a displayable URL.
 *
 * The packaged shell cannot load images over HTTP, so every `<img>` pointing at
 * an upload has to go through the socket instead. Wrapping that in a composable
 * keeps templates readable and makes the loading state explicit: the URL stays
 * empty until the bytes arrive.
 *
 * `width` asks the backend for a scaled copy. Uploads are stored at full size,
 * so anything drawn small should pass the width it is actually drawn at.
 */
export function useFileUrl(
  source: Ref<string | undefined> | (() => string | undefined),
  width = 256,
) {
  const url = ref('')
  const read = typeof source === 'function' ? source : () => source.value

  const load = async (path: string | undefined) => {
    if (!path) {
      url.value = ''
      return
    }
    try {
      url.value = await fileUrl(path, width)
    } catch {
      url.value = ''
    }
  }

  watch(read, load, { immediate: true })

  // The first read can run before the socket is up, which would leave the image
  // permanently blank. Re-read on every (re)connect.
  onScopeDispose(
    onSocketReady(() => {
      void load(read())
    }),
  )

  return url
}

/** Same idea for a list, keyed by the original path. */
export function useFileUrls(source: Ref<string[]> | (() => string[]), width = 256) {
  const map = ref<Record<string, string>>({})
  const read = typeof source === 'function' ? source : () => source.value

  const load = async (paths: string[]) => {
    const next: Record<string, string> = {}
    await Promise.all(
      (paths || []).map(async (path) => {
        if (!path) return
        try {
          next[path] = await fileUrl(path, width)
        } catch {
          next[path] = ''
        }
      }),
    )
    map.value = next
  }

  watch(read, load, { immediate: true, deep: true })

  onScopeDispose(
    onSocketReady(() => {
      void load(read())
    }),
  )

  return map
}

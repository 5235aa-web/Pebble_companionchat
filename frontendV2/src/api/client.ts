/**
 * API transport.
 *
 * In the packaged shell the webview cannot make HTTP requests at all: every
 * fetch and XHR fails with a bare "Failed to fetch" while the same URL answers
 * fine in Edge, and a WebSocket from the same webview works. So all JSON
 * traffic is tunnelled through one socket to `/ws`. The backend forwards each
 * frame into the FastAPI app through an ASGI transport, which means no endpoint
 * was rewritten and the browser build still uses plain fetch via the Vite proxy.
 */

const isTauri = (): boolean =>
  typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window

/** Base for `<img src>` and audio playback, which still load over HTTP. */
export const getBaseURL = (): string => {
  if (typeof window !== 'undefined') {
    if (isTauri()) return 'http://127.0.0.1:8002'
    if ((window as any).api) return 'http://localhost:8002'
    if (window.location.protocol === 'file:') return 'http://localhost:8002'
  }
  return '/api'
}

export const getDirectBase = (): string => {
  if (typeof window !== 'undefined') {
    if (isTauri()) return 'http://127.0.0.1:8002'
    if ((window as any).api) return 'http://localhost:8002'
    if (window.location.protocol === 'file:') return 'http://localhost:8002'
  }
  return import.meta.env.DEV ? 'http://localhost:8002' : getBaseURL()
}

export class ApiError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(`HTTP ${status}: ${detail}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/** The socket is used wherever fetch is known to be unusable. */
export const useSocket = (): boolean =>
  typeof window !== 'undefined' &&
  (isTauri() || window.location.protocol === 'file:')

const WS_URL = 'ws://127.0.0.1:8002/ws'

/**
 * The call panel streams raw PCM over its own socket: /ws is request/response
 * keyed by id and cannot carry a long-lived upload. See backend/ws_call.py.
 */
export const CALL_WS_URL = `${WS_URL.replace(/\/ws$/, '')}/ws/call`

function concat(parts: Uint8Array[]): Uint8Array {
  let total = 0
  for (const p of parts) total += p.length
  const out = new Uint8Array(total)
  let off = 0
  for (const p of parts) {
    out.set(p, off)
    off += p.length
  }
  return out
}

interface Pending {
  chunks: string[]
  onChunk?: (text: string) => void
  resolve: (value: string) => void
  reject: (err: unknown) => void
  /** Set for binary requests: the decoded bytes instead of a UTF-8 string. */
  raw?: Uint8Array[]
  /** Set for streaming binary requests: called per frame as bytes arrive. */
  onBinary?: (bytes: Uint8Array) => void
}

/**
 * Backoff for reconnects. The shell starts the webview before the backend
 * sidecar is listening, so the very first attempt is expected to fail; without
 * a retry chain nothing calls back in and the window stays empty forever.
 */
const RETRY_DELAYS_MS = [200, 400, 800, 1600, 3200]

class Socket {
  private ws: WebSocket | null = null
  private opening: Promise<void> | null = null
  private nextId = 1
  private lastError = ''
  private pending = new Map<number, Pending>()
  private retryTimer = 0
  private retryAttempt = 0
  private readyHandlers = new Set<() => void>()
  private readyWaiters = new Set<() => void>()

  connect(): Promise<void> {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) return Promise.resolve()
    if (this.opening) return this.opening
    this.opening = new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(WS_URL)
      ws.onopen = () => {
        this.ws = ws
        this.opening = null
        this.lastError = ''
        this.retryAttempt = 0
        resolve()
        // Wake anything that was waiting for a usable socket, then fire the
        // reload handlers. Both run after the promise settles so a caller that
        // resolves and immediately sends is not racing them.
        const waiters = [...this.readyWaiters]
        this.readyWaiters.clear()
        queueMicrotask(() => {
          for (const fn of waiters) fn()
          for (const fn of this.readyHandlers) fn()
        })
      }
      ws.onerror = () => {
        this.lastError = 'onerror'
        this.opening = null
        reject(new ApiError(0, 'websocket unavailable'))
        this.scheduleRetry()
      }
      ws.onclose = (ev) => {
        this.lastError = `close ${ev.code}${ev.reason ? ' ' + ev.reason : ''}`
        this.ws = null
        this.opening = null
        for (const [, p] of this.pending) p.reject(new ApiError(0, 'socket closed'))
        this.pending.clear()
        this.scheduleRetry()
      }
      ws.onmessage = (ev) => this.receive(String(ev.data))
    })
    return this.opening
  }

  /**
   * Fires every time the socket becomes usable, including after a reconnect.
   * Callers use it to re-run whatever load failed during startup.
   */
  onReady(fn: () => void): () => void {
    this.readyHandlers.add(fn)
    return () => {
      this.readyHandlers.delete(fn)
    }
  }

  /**
   * Resolve once a socket is usable, waiting through the retry chain.
   *
   * Every request goes through this instead of failing on the first attempt.
   * The window opens before the sidecar listens, so a caller that gives up on
   * the first refusal leaves its panel permanently empty: nothing else would
   * ever ask again.
   */
  private waitForOpen(timeoutMs = 20000): Promise<void> {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) return Promise.resolve()
    return new Promise<void>((resolve, reject) => {
      const done = () => {
        window.clearTimeout(timer)
        this.readyWaiters.delete(done)
        resolve()
      }
      const timer = window.setTimeout(() => {
        this.readyWaiters.delete(done)
        reject(new ApiError(0, 'socket unavailable'))
      }, timeoutMs)
      this.readyWaiters.add(done)
      // Keep the retry chain alive even when this is the only pending caller.
      void this.connect().catch(() => undefined)
    })
  }

  private scheduleRetry(): void {
    if (this.retryTimer) return
    const delay = RETRY_DELAYS_MS[Math.min(this.retryAttempt, RETRY_DELAYS_MS.length - 1)]
    this.retryAttempt += 1
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = 0
      // A failed attempt re-arms the timer through onerror; success ends it.
      void this.connect().catch(() => undefined)
    }, delay)
  }

  private receive(raw: string): void {
    let frame: Record<string, unknown>
    try {
      frame = JSON.parse(raw)
    } catch {
      return
    }
    const id = frame.id
    if (typeof id !== 'number') return
    const p = this.pending.get(id)
    if (!p) return

    if (frame.type === 'chunk') {
      const bytes = this.toBytes(String(frame.data ?? ''))
      if (p.raw) {
        p.raw.push(bytes)
        p.onBinary?.(bytes)
      } else {
        const text = new TextDecoder().decode(bytes)
        p.chunks.push(text)
        p.onChunk?.(text)
      }
    } else if (frame.type === 'end') {
      this.pending.delete(id)
      const status = Number(frame.status ?? 200)
      if (status >= 400) {
        const text = new TextDecoder().decode(
          p.raw ? concat(p.raw) : new TextEncoder().encode(p.chunks.join('')),
        )
        p.reject(new ApiError(status, text.slice(0, 300)))
        return
      }
      if (p.raw) {
        // Binary callers get the bytes; the string path below would mangle them.
        p.resolve(concat(p.raw) as unknown as string)
      } else {
        p.resolve(p.chunks.join(''))
      }
    } else if (frame.type === 'error') {
      this.pending.delete(id)
      p.reject(new ApiError(Number(frame.status ?? 500), String(frame.detail ?? 'error')))
    }
  }

  /** The bridge ships raw bytes as latin-1, a 1:1 mapping onto bytes. */
  private toBytes(data: string): Uint8Array {
    const bytes = new Uint8Array(data.length)
    for (let i = 0; i < data.length; i += 1) bytes[i] = data.charCodeAt(i) & 0xff
    return bytes
  }

  send(
    method: string,
    path: string,
    body?: unknown,
    onChunk?: (text: string) => void,
    signal?: AbortSignal,
  ): Promise<string> {
    return this.connect()
      .catch(() => this.waitForOpen())
      .then(
        () =>
          new Promise<string>((resolve, reject) => {
            if (signal?.aborted) {
              const err = new Error('aborted')
              err.name = 'AbortError'
              reject(err)
              return
            }
            const id = this.nextId
            this.nextId += 1
            this.pending.set(id, { chunks: [], onChunk, resolve, reject })
            if (signal) {
              signal.addEventListener(
                'abort',
                () => {
                  this.pending.delete(id)
                  if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                    this.ws.send(JSON.stringify({ id, type: 'cancel' }))
                  }
                  const err = new Error('aborted')
                  err.name = 'AbortError'
                  reject(err)
                },
                { once: true },
              )
            }
            this.ws!.send(JSON.stringify({ id, method, path, body }))
          }),
      )
  }

  /** Fetch a stored file. Images cannot be loaded over HTTP in the shell. */
  async binary(path: string, body?: unknown, method = 'GET'): Promise<Uint8Array> {
    await this.connect().catch(() => this.waitForOpen())
    return new Promise<Uint8Array>((resolve, reject) => {
      const id = this.nextId
      this.nextId += 1
      const p: Pending = {
        chunks: [],
        raw: [],
        resolve: () => resolve(concat(p.raw!)),
        reject: reject as (err: unknown) => void,
      }
      this.pending.set(id, p)
      this.ws!.send(JSON.stringify({ id, method, path, body }))
    })
  }

  /**
   * POST and hand each response frame to *onChunk* as it arrives.
   *
   * Used for streamed audio: the whole point is to start playing before the
   * response exists, so buffering it first would defeat the purpose. Aborting
   * drops the pending entry, which stops the bridge from pulling further frames
   * off the engine.
   */
  async binaryStream(
    path: string,
    body: unknown,
    onChunk: (bytes: Uint8Array) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    await this.connect().catch(() => this.waitForOpen())
    return new Promise<void>((resolve, reject) => {
      const id = this.nextId
      this.nextId += 1
      const p: Pending = {
        chunks: [],
        raw: [],
        onBinary: onChunk,
        resolve: () => resolve(),
        reject: reject as (err: unknown) => void,
      }
      this.pending.set(id, p)
      if (signal) {
        if (signal.aborted) {
          this.pending.delete(id)
          reject(new ApiError(0, 'aborted'))
          return
        }
        signal.addEventListener(
          'abort',
          () => {
            this.pending.delete(id)
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
              this.ws.send(JSON.stringify({ id, type: 'cancel' }))
            }
            reject(new ApiError(0, 'aborted'))
          },
          { once: true },
        )
      }
      this.ws!.send(JSON.stringify({ id, method: 'POST', path, body }))
    })
  }
}

const socket = new Socket()

/** Subscribe to every successful (re)connect. Returns an unsubscribe function. */
export const onSocketReady = (fn: () => void): (() => void) => socket.onReady(fn)

/**
 * POST a JSON body and read the raw response bytes.
 *
 * TTS returns audio, not JSON, so it cannot go through `send`. The shell has no
 * usable HTTP path for loopback, so the same socket carries the bytes.
 */
export const binaryRequest = (path: string, body: unknown): Promise<Uint8Array> =>
  socket.binary(path, body, 'POST')

/** Fetch raw bytes over the `/ws` bridge via GET (used for Tauri card/asset downloads). */
export const binaryGetRequest = (path: string): Promise<Uint8Array> =>
  socket.binary(path, undefined, 'GET')

/**
 * POST and consume the response as it streams in.
 *
 * Used for TTS: the audio is raw PCM and the first chunk is audible long before
 * the last one exists. A non-socket transport would have to buffer everything.
 */
export const binaryStreamRequest = (
  path: string,
  body: unknown,
  onChunk: (bytes: Uint8Array) => void,
  signal?: AbortSignal,
): Promise<void> => socket.binaryStream(path, body, onChunk, signal)


/** Browser path: plain fetch, proxied by Vite in dev. */
async function httpRequest(path: string, init: RequestInit): Promise<string> {
  let res: Response
  try {
    res = await fetch(getBaseURL() + path, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        ...(init.headers as Record<string, string> | undefined),
      },
    })
  } catch (err) {
    throw new ApiError(0, err instanceof Error ? err.message : 'network error')
  }
  const text = await res.text()
  if (!res.ok) throw new ApiError(res.status, text.slice(0, 300))
  return text
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method || 'GET').toUpperCase()

  let body: unknown
  if (typeof init.body === 'string' && init.body) {
    try {
      body = JSON.parse(init.body)
    } catch {
      body = init.body
    }
  }

  const text = useSocket()
    ? await socket.send(method, path, body)
    : await httpRequest(path, init)

  if (!text) return undefined as T
  try {
    return JSON.parse(text) as T
  } catch {
    return text as unknown as T
  }
}

export const get = <T>(path: string): Promise<T> => request<T>(path)

export const post = <T>(path: string, body?: unknown): Promise<T> =>
  request<T>(path, {
    method: 'POST',
    body: body === undefined ? undefined : JSON.stringify(body),
  })

export const put = <T>(path: string, body?: unknown): Promise<T> =>
  request<T>(path, {
    method: 'PUT',
    body: body === undefined ? undefined : JSON.stringify(body),
  })

export const patch = <T>(path: string, body?: unknown): Promise<T> =>
  request<T>(path, {
    method: 'PATCH',
    body: body === undefined ? undefined : JSON.stringify(body),
  })

export const del = <T>(path: string): Promise<T> => request<T>(path, { method: 'DELETE' })

/**
 * Multipart upload. The socket protocol carries JSON only, so this stays on
 * fetch; it is used by the STT upload path, which is unavailable in the shell
 * until it is tunnelled too.
 */
export const postForm = async <T>(
  path: string,
  form: FormData,
  init: RequestInit = {},
): Promise<T> => {
  const res = await fetch(getBaseURL() + path, { ...init, method: 'POST', body: form })
  const text = await res.text()
  if (!res.ok) throw new ApiError(res.status, text.slice(0, 300))
  return (text ? JSON.parse(text) : undefined) as T
}

/** Streams a response body chunk by chunk through the same socket. */
export async function streamRequest(
  path: string,
  body: unknown,
  onChunk: (text: string) => void,
  init: RequestInit = {},
): Promise<void> {
  if (useSocket()) {
    await socket.send('POST', path, body, onChunk, init.signal ?? undefined)
    return
  }
  const res = await fetch(getDirectBase() + path, {
    ...init,
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok || !res.body) {
    throw new ApiError(res.status, (await res.text()).slice(0, 300))
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    onChunk(decoder.decode(value, { stream: true }))
  }
}

/** Turn a stored relative path into something an <img> can load. */
export function getFileUrl(filePath: string): string {
  if (!filePath) return ''
  if (filePath.startsWith('data:') || filePath.startsWith('blob:')) return filePath
  if (/^https?:\/\//.test(filePath)) return filePath
  const base = getBaseURL()
  return filePath.startsWith('/') ? `${base}${filePath}` : `${base}/${filePath}`
}

const MAX_BLOB_CACHE = 160
const blobCache = new Map<string, string>()

function rememberBlobUrl(key: string, url: string): void {
  if (blobCache.has(key)) {
    blobCache.delete(key)
  } else if (blobCache.size >= MAX_BLOB_CACHE) {
    const oldestKey = blobCache.keys().next().value
    if (oldestKey !== undefined) {
      const oldestUrl = blobCache.get(oldestKey)
      blobCache.delete(oldestKey)
      if (oldestUrl) URL.revokeObjectURL(oldestUrl)
    }
  }
  blobCache.set(key, url)
}

/**
 * MIME for a stored file, derived from its extension.
 *
 * A Blob built without a type carries no Content-Type, and an <img> given such
 * a blob URL has nothing to decode against: avatars and scene photos stayed
 * blank while gallery entries worked, because those are data: URLs and never
 * reach this path.
 */
const MIME_BY_EXT: Record<string, string> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  webp: 'image/webp',
  gif: 'image/gif',
  avif: 'image/avif',
  bmp: 'image/bmp',
  svg: 'image/svg+xml',
  mp3: 'audio/mpeg',
  wav: 'audio/wav',
  ogg: 'audio/ogg',
  m4a: 'audio/mp4',
  flac: 'audio/flac',
  mp4: 'video/mp4',
  m4v: 'video/mp4',
  webm: 'video/webm',
  mov: 'video/quicktime',
  mkv: 'video/x-matroska',
}

function mimeFor(path: string): string {
  const clean = path.split('?')[0].split('#')[0]
  const dot = clean.lastIndexOf('.')
  if (dot < 0) return 'application/octet-stream'
  return MIME_BY_EXT[clean.slice(dot + 1).toLowerCase()] ?? 'application/octet-stream'
}

/**
 * Resolve a stored path to something an `<img>` can display.
 *
 * In the shell this fetches the bytes over the socket and hands back a blob
 * URL, because HTTP image loads fail there too. Cached per path: the same
 * avatar is rendered in several places.
 */
export async function fileUrl(path: string, width = 0): Promise<string> {
  if (!path) return ''
  if (path.startsWith('data:') || path.startsWith('blob:')) return path
  if (/^https?:\/\//.test(path)) return path
  if (!useSocket()) {
    const base = getBaseURL() + (path.startsWith('/') ? path : `/${path}`)
    return width > 0 ? `${base}?w=${width}` : base
  }

  const clean = path.startsWith('/') ? path.slice(1) : path
  const mime = mimeFor(clean)
  const isSvg = mime === 'image/svg+xml'
  // A non-zero width asks the backend for a scaled copy. Uploads are stored at
  // full size (one avatar here is 21 MB) and a cast card draws at 40 px, so the
  // original costs orders of magnitude more to ship over the socket. SVGs stay
  // vector and keep image/svg+xml rather than going through WebP rasterization.
  const request = width > 0 && !isSvg ? `/thumbs/${clean}?w=${width}` : `/${clean}`
  const key = `${isSvg ? 0 : width}:${clean}`

  const hit = blobCache.get(key)
  if (hit) {
    // Refresh LRU position without recreating the URL.
    blobCache.delete(key)
    blobCache.set(key, hit)
    return hit
  }

  const bytes = await socket.binary(request)
  // Scaled copies come back as WebP regardless of the source format, so the
  // Blob type has to follow the request rather than the path's extension.
  const type = width > 0 && !isSvg ? 'image/webp' : mime
  const url = URL.createObjectURL(new Blob([bytes as BlobPart], { type }))
  rememberBlobUrl(key, url)
  return url
}

/** Encode a Uint8Array chunk to base64 safely without overflowing the call stack. */
export function bytesToBase64(bytes: Uint8Array): string {
  let binary = ''
  const step = 0x4000
  for (let i = 0; i < bytes.length; i += step) {
    const sub = bytes.subarray(i, Math.min(i + step, bytes.length))
    binary += String.fromCharCode(...sub)
  }
  return btoa(binary)
}

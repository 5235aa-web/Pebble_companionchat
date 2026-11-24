import { ApiError, del, get, postForm } from './client'
import { extractVideoAudioWav, extractVideoPayload, isVideoFile } from '@/utils/video'

const C = '/companions'

export interface VideoFrameRef {
  time: number
  url: string
}

export interface BranchImage {
  kind?: 'image' | 'video'
  url: string
  poster_url?: string
  name: string
  mime: string
  size: number
  width: number
  height: number
  duration?: number
  frames?: VideoFrameRef[]
  transcript?: string
  message_id: number
  created_at: string
}

export const getBranchImages = (
  companionId: string,
  branchId: string,
): Promise<{ images: BranchImage[]; count: number }> =>
  get(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/images`,
  )

export const deleteBranchImage = (
  companionId: string,
  branchId: string,
  url: string,
): Promise<{ status: string; url: string; file_deleted: boolean; cleared_messages: number }> =>
  del(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/images?url=${encodeURIComponent(url)}`,
  )

export interface AttachmentInfo {
  kind: 'image' | 'document' | 'audio' | 'video' | 'text' | 'unknown'
  path?: string
  url?: string
  poster_url?: string
  name?: string
  mime?: string
  mime_type?: string
  size?: number
  content?: string
  width?: number
  height?: number
  duration?: number
  frames?: VideoFrameRef[]
  transcript?: string
  text?: string
  chars?: number
  truncated?: boolean
}

export type AttachmentParseStage = 'extracting' | 'uploading'

interface SupportedAttachmentsResponse {
  image: string[]
  video?: string[]
  document: string[]
  ffmpegAvailable?: boolean
  videoAutoStt?: boolean
}

let cachedCaps: { ffmpegAvailable: boolean; videoAutoStt: boolean; at: number } | null = null
const CAPS_CACHE_TTL_MS = 30_000

export function invalidateAttachmentCaps(): void {
  cachedCaps = null
}

export async function getAttachmentCapabilities(): Promise<{
  ffmpegAvailable: boolean
  videoAutoStt: boolean
}> {
  if (cachedCaps && Date.now() - cachedCaps.at < CAPS_CACHE_TTL_MS) {
    return {
      ffmpegAvailable: cachedCaps.ffmpegAvailable,
      videoAutoStt: cachedCaps.videoAutoStt,
    }
  }
  try {
    const res = await get<SupportedAttachmentsResponse>(`${C}/attachments/supported`)
    const caps = {
      ffmpegAvailable: Boolean(res?.ffmpegAvailable),
      videoAutoStt: Boolean(res?.videoAutoStt),
    }
    cachedCaps = { ...caps, at: Date.now() }
    return caps
  } catch {
    return { ffmpegAvailable: false, videoAutoStt: false }
  }
}

async function uploadVideoWithBrowserExtraction(
  file: File,
  enableStt: boolean,
  onStage?: (stage: AttachmentParseStage) => void,
): Promise<AttachmentInfo> {
  onStage?.('extracting')
  const extracted = await extractVideoPayload(file, { includeAudio: enableStt })
  const fd = new FormData()
  fd.append('file', file)
  fd.append('enable_stt', enableStt ? 'true' : 'false')
  if (extracted.frames.length) {
    fd.append(
      'frames_file',
      new Blob([JSON.stringify(extracted.frames)], { type: 'application/json' }),
      'frames.json',
    )
  }
  if (extracted.duration > 0) fd.append('duration', String(extracted.duration))
  if (extracted.width > 0) fd.append('width', String(extracted.width))
  if (extracted.height > 0) fd.append('height', String(extracted.height))
  if (enableStt && extracted.wavBlob) {
    fd.append('audio_wav', extracted.wavBlob, 'audio.wav')
  }
  onStage?.('uploading')
  return postForm(`${C}/attachments/parse`, fd)
}

export const parseAttachment = async (
  file: File,
  onStage?: (stage: AttachmentParseStage) => void,
  options?: { enableStt?: boolean },
): Promise<AttachmentInfo> => {
  if (!isVideoFile(file)) {
    onStage?.('uploading')
    const fd = new FormData()
    fd.append('file', file)
    return postForm(`${C}/attachments/parse`, fd)
  }

  const caps = await getAttachmentCapabilities()
  const enableStt = options?.enableStt ?? caps.videoAutoStt

  // Prefer backend FFmpeg when available; automatically fall back to browser
  // frame/audio sampling if FFmpeg is missing or fails at runtime (HTTP 422).
  if (caps.ffmpegAvailable) {
    onStage?.('uploading')
    const fd = new FormData()
    fd.append('file', file)
    fd.append('enable_stt', enableStt ? 'true' : 'false')
    try {
      return await postForm(`${C}/attachments/parse`, fd)
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 422)) {
        throw err
      }
      cachedCaps = { ffmpegAvailable: false, videoAutoStt: caps.videoAutoStt, at: Date.now() }
    }
  }

  return uploadVideoWithBrowserExtraction(file, enableStt, onStage)
}

export const transcribeVideoAttachment = async (
  url: string,
  file?: File,
): Promise<string> => {
  const caps = await getAttachmentCapabilities()
  if (caps.ffmpegAvailable) {
    const fd = new FormData()
    fd.append('url', url)
    try {
      const res = await postForm<{ transcript?: string }>(`${C}/attachments/video/transcribe`, fd)
      return res?.transcript ?? ''
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 422) || !file) {
        throw err
      }
      cachedCaps = { ffmpegAvailable: false, videoAutoStt: caps.videoAutoStt, at: Date.now() }
    }
  }

  const fd = new FormData()
  fd.append('url', url)
  if (file) {
    const wav = await extractVideoAudioWav(file)
    if (wav) fd.append('audio_wav', wav, 'audio.wav')
  }
  const res = await postForm<{ transcript?: string }>(`${C}/attachments/video/transcribe`, fd)
  return res?.transcript ?? ''
}

/**
 * A file the composer uploaded and is holding for the next send.
 *
 * `src` is a local object URL for the thumbnail; `info.url` is the stored copy
 * the message and the album point at. `ownBlobUrl` distinguishes URLs created
 * directly via `URL.createObjectURL(file)` from cached `fileUrl()` results.
 */
export interface PickedAttachment {
  id: number
  name: string
  ext: string
  src: string
  ownBlobUrl?: boolean
  uploading?: boolean
  stage?: AttachmentParseStage
  sttEnabled?: boolean
  transcribing?: boolean
  cachedTranscript?: string
  kind: 'photo' | 'video' | 'paper'
  info: AttachmentInfo
}

export function attachmentToPayload(info: AttachmentInfo) {
  return {
    kind: info.kind,
    name: info.name,
    url: info.url,
    poster_url: info.poster_url,
    path: info.path,
    mime: info.mime || info.mime_type,
    size: info.size,
    width: info.width,
    height: info.height,
    duration: info.duration,
    frames: info.frames,
    transcript: info.transcript,
    content: info.content,
    text: info.text,
    chars: info.chars,
    truncated: info.truncated,
  }
}

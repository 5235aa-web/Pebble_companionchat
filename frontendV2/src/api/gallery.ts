import { del, get, postForm } from './client'

const C = '/companions'

export interface BranchImage {
  url: string
  name: string
  mime: string
  size: number
  width: number
  height: number
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
  name?: string
  mime_type?: string
  size?: number
  content?: string
  width?: number
  height?: number
  text?: string
  chars?: number
  truncated?: boolean
}

export const parseAttachment = (file: File): Promise<AttachmentInfo> => {
  const fd = new FormData()
  fd.append('file', file)
  return postForm(`${C}/attachments/parse`, fd)
}

/**
 * A file the composer uploaded and is holding for the next send.
 *
 * `src` is a local object URL for the thumbnail; `info.url` is the stored copy
 * the message and the album point at.
 */
export interface PickedAttachment {
  id: number
  name: string
  ext: string
  src: string
  kind: 'photo' | 'paper'
  info: AttachmentInfo
}

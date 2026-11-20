import type { CompanionAsset } from '@/api/companion'
import type { BeatLine } from '@/data/chat'

const INLINE_MEDIA_RE =
  /\{\{\s*(img|image|asset|audio|bgm)\s*::\s*([^}]+?)\s*\}\}|!\[[^\]]*\]\(([^)]+)\)|((?:embeded|embedded):\/\/[^\s"'<>)]+)/gi

export interface InlineMediaResult {
  images: CompanionAsset[]
  audios: Array<{ asset: CompanionAsset; isBgm: boolean }>
  cleanedLines: Array<{ kind: 'say' | 'direction'; text: string }>
}

/**
 * Extract CHARX inline media macros ({{img::...}}, {{audio::...}}, {{bgm::...}},
 * embeded://..., Markdown images) from message/greeting lines using `lookup`.
 */
export function resolveInlineMedia(
  lines: BeatLine[],
  lookup: (ref: string) => CompanionAsset | undefined,
): InlineMediaResult {
  const images: CompanionAsset[] = []
  const audios: Array<{ asset: CompanionAsset; isBgm: boolean }> = []
  const seenIds = new Set<number>()
  const cleanedLines: Array<{ kind: 'say' | 'direction'; text: string }> = []

  for (const line of lines) {
    const text = line.text
      .replace(
        INLINE_MEDIA_RE,
        (
          whole,
          macroKind?: string,
          macroArg?: string,
          mdTarget?: string,
          embedUri?: string,
        ) => {
          const ref = (macroArg || mdTarget || embedUri || '').trim()
          const asset = ref ? lookup(ref) : undefined
          if (!asset) return whole
          if (!seenIds.has(asset.id)) {
            seenIds.add(asset.id)
            const mime = asset.mime_type || ''
            if (mime.startsWith('image/')) {
              images.push(asset)
            } else if (mime.startsWith('audio/')) {
              const isBgm =
                (macroKind || '').toLowerCase() === 'bgm' || asset.asset_type === 'bgm'
              audios.push({ asset, isBgm })
            }
          }
          return ''
        },
      )
      .trim()
    if (text) cleanedLines.push({ kind: line.kind, text })
  }

  return { images, audios, cleanedLines }
}

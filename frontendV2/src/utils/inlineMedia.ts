import type { CompanionAsset } from '@/api/companion'
import type { BeatLine } from '@/data/chat'

const INLINE_MEDIA_RE =
  /\{\{\s*(img|image|asset|audio|bgm|video)\s*::\s*([^}]+?)\s*\}\}|!\[[^\]]*\]\(([^)]+)\)|((?:embeded|embedded):\/\/[^\s"'<>)]+)/gi

export interface InlineMediaBaseResult {
  images: CompanionAsset[]
  audios: Array<{ asset: CompanionAsset; isBgm: boolean }>
  videos: CompanionAsset[]
}

export interface InlineMediaResult extends InlineMediaBaseResult {
  cleanedLines: Array<{ kind: 'say' | 'direction'; text: string }>
}

export interface InlineMediaTextResult extends InlineMediaBaseResult {
  htmlImages: CompanionAsset[]
  cleanedText: string
}

interface MediaCollector {
  images: CompanionAsset[]
  audios: Array<{ asset: CompanionAsset; isBgm: boolean }>
  videos: CompanionAsset[]
  recordAsset: (asset: CompanionAsset, macroKind?: string) => void
  stripLine: (rawLine: string) => { text: string; hadToken: boolean }
}

function createCollector(
  lookup: (ref: string) => CompanionAsset | undefined,
): MediaCollector {
  const images: CompanionAsset[] = []
  const audios: Array<{ asset: CompanionAsset; isBgm: boolean }> = []
  const videos: CompanionAsset[] = []
  const seenIds = new Set<number>()

  const recordAsset = (asset: CompanionAsset, macroKind?: string): void => {
    if (seenIds.has(asset.id)) return
    seenIds.add(asset.id)
    const mime = asset.mime_type || ''
    if (mime.startsWith('image/')) {
      images.push(asset)
    } else if (mime.startsWith('audio/')) {
      const isBgm =
        (macroKind || '').toLowerCase() === 'bgm' || asset.asset_type === 'bgm'
      audios.push({ asset, isBgm })
    } else if (mime.startsWith('video/') || asset.asset_type === 'video') {
      videos.push(asset)
    }
  }

  const stripLine = (rawLine: string): { text: string; hadToken: boolean } => {
    let hadToken = false
    const text = rawLine.replace(
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
        hadToken = true
        recordAsset(asset, macroKind)
        return ''
      },
    )
    return { text, hadToken }
  }

  return { images, audios, videos, recordAsset, stripLine }
}

/**
 * Extract CHARX inline media macros ({{img::...}}, {{audio::...}}, {{bgm::...}},
 * {{video::...}}, embeded://..., Markdown images) from message/greeting lines.
 */
export function resolveInlineMedia(
  lines: BeatLine[],
  lookup: (ref: string) => CompanionAsset | undefined,
): InlineMediaResult {
  const collector = createCollector(lookup)
  const cleanedLines: Array<{ kind: 'say' | 'direction'; text: string }> = []

  for (const line of lines) {
    const cleaned = collector.stripLine(line.text).text.trim()
    if (cleaned) cleanedLines.push({ kind: line.kind, text: cleaned })
  }

  return {
    images: collector.images,
    audios: collector.audios,
    videos: collector.videos,
    cleanedLines,
  }
}

const HTML_VIDEO_TAG_RE = /<video\b[^>]*>[\s\S]*?<\/video>|<video\b[^>]*>/gi
const HTML_IMG_TAG_RE = /<img\b[^>]*>/gi
const HTML_ATTR_SRC_RE = /(\bsrc\s*=\s*)(?:"([^"]*)"|'([^']*)')/gi
const HTML_IMG_SRC_RE = /(\bsrc\s*=\s*)(?:"([^"]*)"|'([^']*)')/i
// RisuAI shorthand tag syntax used by CHARX cards such as `<img="Godel_curious">`.
const HTML_IMG_SHORTHAND_RE =
  /^<img\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))([\s\S]*?)>$/i
const IMG_MACRO_SRC_RE = /^\{\{\s*(?:img|image|asset)\s*::\s*([^}]+?)\s*\}\}$/i
const VIDEO_MACRO_SRC_RE = /^\{\{\s*(?:video|asset)\s*::\s*([^}]+?)\s*\}\}$/i

function escapeHtmlAttr(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

/**
 * Extract CHARX inline media macros from unsplit message text while preserving
 * Markdown blank lines, indentation, and inline HTML `<img src="...">` layout.
 */
export function extractInlineMediaFromText(
  text: string,
  lookup: (ref: string) => CompanionAsset | undefined,
): InlineMediaTextResult {
  const collector = createCollector(lookup)
  if (!text) return { images: [], htmlImages: [], audios: [], videos: [], cleanedText: '' }

  const htmlImages: CompanionAsset[] = []
  const seenHtmlIds = new Set<number>()
  const stashedTags: string[] = []

  const resolveHtmlImageAsset = (rawSrc: string): CompanionAsset | undefined => {
    const trimmed = rawSrc.trim()
    if (!trimmed || /^(?:https?:|data:|blob:)/i.test(trimmed)) return undefined
    const macroMatch = trimmed.match(IMG_MACRO_SRC_RE)
    const ref = (macroMatch ? macroMatch[1] : trimmed).trim()
    const asset = ref ? lookup(ref) : undefined
    if (!asset || !(asset.mime_type || '').startsWith('image/')) return undefined
    if (!seenHtmlIds.has(asset.id)) {
      seenHtmlIds.add(asset.id)
      htmlImages.push(asset)
    }
    return asset
  }

  // Extract inline HTML <video> blocks that point to bundled video assets so
  // they render through InlineVideoCard instead of being stripped by DOMPurify.
  const sanitized = text.replace(/\u0000/g, '').replace(HTML_VIDEO_TAG_RE, (block) => {
    let matchedVideo = false
    for (const m of block.matchAll(HTML_ATTR_SRC_RE)) {
      const rawSrc = (m[2] ?? m[3] ?? '').trim()
      if (!rawSrc || /^(?:https?:|data:|blob:)/i.test(rawSrc)) continue
      const macroMatch = rawSrc.match(VIDEO_MACRO_SRC_RE)
      const ref = (macroMatch ? macroMatch[1] : rawSrc).trim()
      const asset = ref ? lookup(ref) : undefined
      if (asset && ((asset.mime_type || '').startsWith('video/') || asset.asset_type === 'video')) {
        collector.recordAsset(asset, 'video')
        matchedVideo = true
      }
    }
    return matchedVideo ? '' : block
  })

  // Protect HTML <img> tags before line-level macro extraction so `embeded://`,
  // `{{img::...}}`, or shorthand `<img="asset">` resolves in place instead of being blanked.
  const withPlaceholders = sanitized.replace(HTML_IMG_TAG_RE, (tag) => {
    let updatedTag = tag
    const shorthand = tag.match(HTML_IMG_SHORTHAND_RE)
    if (shorthand) {
      let rawRef = (shorthand[1] ?? shorthand[2] ?? '').trim()
      if (!rawRef && shorthand[3]) {
        rawRef = shorthand[3].replace(/\/+$/, '').trim()
      }
      const extraAttrs = (shorthand[4] ?? '').replace(/\/\s*$/, '').trim()
      const suffix = extraAttrs ? ` ${extraAttrs}` : ''
      if (/^(?:https?:|data:image\/)/i.test(rawRef)) {
        updatedTag = `<img src="${escapeHtmlAttr(rawRef)}"${suffix}>`
      } else {
        const asset = resolveHtmlImageAsset(rawRef)
        if (asset) {
          const altAttr = /\balt\s*=/i.test(extraAttrs)
            ? ''
            : ` alt="${escapeHtmlAttr(asset.name || rawRef)}"`
          updatedTag = `<img src="pebble-asset:${asset.id}"${altAttr}${suffix}>`
        }
      }
    } else {
      updatedTag = tag.replace(HTML_IMG_SRC_RE, (attrWhole, prefix: string, dq?: string, sq?: string) => {
        const asset = resolveHtmlImageAsset(dq ?? sq ?? '')
        if (!asset) return attrWhole
        const quote = dq !== undefined ? '"' : "'"
        return `${prefix}${quote}pebble-asset:${asset.id}${quote}`
      })
    }
    const idx = stashedTags.length
    stashedTags.push(updatedTag)
    return `\u0000PEBBLE_IMG_${idx}\u0000`
  })

  const outLines: string[] = []
  for (const rawLine of withPlaceholders.split('\n')) {
    if (!rawLine.trim()) {
      outLines.push('')
      continue
    }
    const { text: stripped, hadToken } = collector.stripLine(rawLine)
    if (hadToken && !stripped.trim()) continue
    outLines.push(hadToken ? stripped.replace(/[ \t]+$/g, '') : stripped)
  }

  const restored = outLines
    .join('\n')
    .replace(/\u0000PEBBLE_IMG_(\d+)\u0000/g, (_, idx) => stashedTags[Number(idx)] ?? '')
  const cleanedText = restored.replace(/\n{3,}/g, '\n\n').trim()
  return {
    images: collector.images,
    htmlImages,
    audios: collector.audios,
    videos: collector.videos,
    cleanedText,
  }
}

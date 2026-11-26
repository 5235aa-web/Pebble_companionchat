import type { CompanionAsset } from '@/api/companion'
import type { BeatLine } from '@/data/chat'

const INLINE_MEDIA_RE =
  /\{\{\s*(img|image|asset|raw|audio|bgm|video)\s*::\s*([^}]+?)\s*\}\}|!\[[^\]]*\]\(([^)]+)\)|((?:embeded|embedded):\/\/[^\s"'<>)]+)/gi

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
const HTML_AUDIO_TAG_RE = /<audio\b[^>]*>[\s\S]*?<\/audio>|<audio\b[^>]*>/gi
const HTML_STYLE_TAG_RE = /(<style\b[^>]*>)([\s\S]*?)(<\/style>)/gi
const HTML_INLINE_STYLE_ATTR_RE = /(\bstyle\s*=\s*)(?:"([^"]*)"|'([^']*)')/gi
const HTML_IMG_TAG_RE = /<img\b[^>]*>/gi
const HTML_ATTR_SRC_RE = /(\bsrc\s*=\s*)(?:"([^"]*)"|'([^']*)')/gi
const HTML_IMG_SRC_RE = /(\bsrc\s*=\s*)(?:"([^"]*)"|'([^']*)')/i
// RisuAI shorthand tag syntax used by CHARX cards such as `<img="Godel_curious">`.
const HTML_IMG_SHORTHAND_RE =
  /^<img\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))([\s\S]*?)>$/i
const IMG_MACRO_SRC_RE = /^\{\{\s*(?:img|image|asset|raw)\s*::\s*([^}]+?)\s*\}\}$/i
const VIDEO_MACRO_SRC_RE = /^\{\{\s*(?:video|asset|raw)\s*::\s*([^}]+?)\s*\}\}$/i
const AUDIO_MACRO_SRC_RE = /^\{\{\s*(?:audio|bgm|asset|raw)\s*::\s*([^}]+?)\s*\}\}$/i
const CSS_URL_RE = /url\s*\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*))\s*\)/gi

export function rewriteCssAssetUrls(
  css: string,
  lookup: (ref: string) => CompanionAsset | undefined,
): { css: string; assets: CompanionAsset[] } {
  if (!css || !/url\s*\(/i.test(css)) return { css, assets: [] }
  const assets: CompanionAsset[] = []
  const seenIds = new Set<number>()
  const rewritten = css.replace(CSS_URL_RE, (whole, dq?: string, sq?: string, uq?: string) => {
    const raw = (dq ?? sq ?? uq ?? '').trim()
    if (!raw || /^(?:https?:|data:|blob:|pebble-asset:)/i.test(raw)) return whole
    const macro = raw.match(IMG_MACRO_SRC_RE)
    const ref = (macro ? macro[1] : raw).trim()
    const asset = ref ? lookup(ref) : undefined
    if (!asset || !(asset.mime_type || '').startsWith('image/')) return whole
    if (!seenIds.has(asset.id)) {
      seenIds.add(asset.id)
      assets.push(asset)
    }
    return `url("pebble-asset:${asset.id}")`
  })
  return { css: rewritten, assets }
}

const IMG_TAG_SHORTHAND_REF_RE = /(<img\s*=\s*)(?:"([^"]+)"|'([^']+)'|([^\s"'=<>`]+))/gi
const ASSET_MACRO_REF_RE = /(\{\{\s*(?:img|image|asset|raw)\s*::\s*)([^}]+?)(\s*\}\})/gi
const MALFORMED_IMG_TAG_RE =
  /<(?:(?:mg|mgr|imb|imgr|im|img)\s*\(\s*(?:"([^"\n<>]*)"|'([^'\n<>]*)'|([^()\n<>]+?))\s*\)\s*\/?|(?:mg|mgr|imb|imgr|im)\s*=\s*(?:"([^"\n<>]*)"|'([^'\n<>]*)'|([^\s"'=<>`]+))(?:\s*\/?))>/gi
const PRECEDING_VALID_IMAGE_RE =
  /(?:<img\b[^>]*>|\{\{\s*(?:img|image|asset|raw)\s*::[^}]+\}\}|!\[[^\]]*\]\([^)]+\))\s*$/i
const FENCED_CODE_BLOCK_RE =
  /(^|\n)([ \t]*)(`{3,}|~{3,})[^\n]*\n[\s\S]*?\n\2\3[ \t]*(?=\n|$)/g
const COMPACT_SEP_RE = /[\s_-]+/g
const NON_REF_PREFIX_RE = /^(?:https?:|data:|blob:|pebble-asset:)/i

function editDistance(a: string, b: string): number {
  if (a === b) return 0
  if (a.length < b.length) [a, b] = [b, a]
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i)
  for (let i = 1; i <= a.length; i += 1) {
    const curr = [i]
    const ca = a[i - 1]
    for (let j = 1; j <= b.length; j += 1) {
      const cost = ca === b[j - 1] ? 0 : 1
      curr.push(Math.min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost))
    }
    prev = curr
  }
  return prev[b.length]
}

function commonPrefixLen(a: string, b: string): number {
  const limit = Math.min(a.length, b.length)
  let i = 0
  while (i < limit && a[i] === b[i]) i += 1
  return i
}

export function resolveFuzzyAssetName(ref: string, assetNames: string[]): string | null {
  const cleaned = (ref || '').trim()
  if (
    !cleaned ||
    cleaned.length < 4 ||
    cleaned.includes('{{') ||
    cleaned.includes('<') ||
    cleaned.includes('>') ||
    NON_REF_PREFIX_RE.test(cleaned)
  ) {
    return null
  }

  const lowMap = new Map<string, string>()
  const compactMap = new Map<string, string | null>()
  const candidates: Array<[string, string, string]> = []
  for (const name of assetNames) {
    const s = (name || '').trim()
    if (!s) continue
    if (s === cleaned) return s
    const low = s.toLowerCase()
    if (!lowMap.has(low)) {
      lowMap.set(low, s)
      const compact = low.replace(COMPACT_SEP_RE, '')
      candidates.push([low, s, compact])
      if (compact) {
        if (!compactMap.has(compact)) {
          compactMap.set(compact, s)
        } else if (compactMap.get(compact) !== s) {
          compactMap.set(compact, null)
        }
      }
    }
  }

  const q = cleaned.toLowerCase()
  const exactLow = lowMap.get(q)
  if (exactLow) return exactLow

  const qCompact = q.replace(COMPACT_SEP_RE, '')
  if (qCompact.length >= 4) {
    const exactCompact = compactMap.get(qCompact)
    if (exactCompact) return exactCompact
  }

  let bestScore = 0
  let bestPrefix = -1
  let bestName: string | null = null
  let tied = false

  for (const [cLow, cOrig, cCompact] of candidates) {
    if (cLow.length < 4) continue
    let candScore = 0
    let candPrefix = -1

    const maxLen = Math.max(q.length, cLow.length)
    const maxDist = Math.max(3, Math.floor(cLow.length / 4))
    if (Math.abs(q.length - cLow.length) <= maxDist) {
      const dist = editDistance(q, cLow)
      if (dist <= maxDist) {
        const sim = 1 - dist / maxLen
        if (sim >= 0.75) {
          candScore = sim
          candPrefix = commonPrefixLen(q, cLow)
        }
      }
    }

    if (qCompact.length >= 4 && cCompact.length >= 4) {
      const maxCmpLen = Math.max(qCompact.length, cCompact.length)
      const maxCmpDist = Math.max(2, Math.floor(cCompact.length / 4))
      if (Math.abs(qCompact.length - cCompact.length) <= maxCmpDist) {
        const distCmp = editDistance(qCompact, cCompact)
        if (distCmp <= maxCmpDist) {
          const simCmp = 1 - distCmp / maxCmpLen
          if (simCmp >= 0.78 && simCmp > candScore) {
            candScore = simCmp
            candPrefix = commonPrefixLen(qCompact, cCompact)
          }
        }
      }
    }

    if (candScore <= 0) continue
    if (candScore > bestScore + 1e-9 || (Math.abs(candScore - bestScore) <= 1e-9 && candPrefix > bestPrefix)) {
      bestScore = candScore
      bestPrefix = candPrefix
      bestName = cOrig
      tied = false
    } else if (Math.abs(candScore - bestScore) <= 1e-9 && candPrefix === bestPrefix) {
      tied = true
    }
  }

  return tied ? null : bestName
}

function normalizeAssetSegment(text: string, assetNames: string[]): string {
  let out = text.replace(
    IMG_TAG_SHORTHAND_REF_RE,
    (whole, prefix: string, dq?: string, sq?: string, uq?: string) => {
      const rawRef = dq ?? sq ?? uq ?? ''
      const quote = sq !== undefined ? "'" : '"'
      const fixed = resolveFuzzyAssetName(rawRef, assetNames)
      if (!fixed || (fixed === rawRef && uq === undefined)) return whole
      return `${prefix}${quote}${fixed}${quote}`
    },
  )
  out = out.replace(
    ASSET_MACRO_REF_RE,
    (whole, prefix: string, rawRef: string, suffix: string) => {
      const fixed = resolveFuzzyAssetName(rawRef, assetNames)
      return !fixed || fixed === rawRef ? whole : `${prefix}${fixed}${suffix}`
    },
  )

  MALFORMED_IMG_TAG_RE.lastIndex = 0
  if (MALFORMED_IMG_TAG_RE.test(out)) {
    MALFORMED_IMG_TAG_RE.lastIndex = 0
    const parts: string[] = []
    let cursor = 0
    let removedAny = false
    let match: RegExpExecArray | null
    while ((match = MALFORMED_IMG_TAG_RE.exec(out)) !== null) {
      parts.push(out.slice(cursor, match.index))
      cursor = match.index + match[0].length
      const preceding = parts.join('')
      const rawRef = (match[1] ?? match[2] ?? match[3] ?? match[4] ?? match[5] ?? match[6] ?? '').trim()
      if (PRECEDING_VALID_IMAGE_RE.test(preceding)) {
        removedAny = true
        continue
      }
      const fixed = resolveFuzzyAssetName(rawRef, assetNames)
      if (fixed) {
        parts.push(`<img="${fixed}">`)
      } else {
        removedAny = true
      }
    }
    parts.push(out.slice(cursor))
    out = parts.join('')
    if (removedAny) {
      out = out.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').replace(/\s+$/, '')
    }
  }

  return out
}

/**
 * Fix high-confidence LLM typos in `<img="...">` / `{{img/raw::...}}` tags and
 * repair or strip malformed `<mg(...)>` / `<mgr(...)>` / `<imb=...>` tags
 * against the companion's bundled asset names before Display Regex runs.
 */
export function normalizeAssetTagRefs(text: string, assetNames: string[]): string {
  if (!text || !assetNames.length) return text
  const stashed: string[] = []
  const masked = text.replace(FENCED_CODE_BLOCK_RE, (whole) => {
    const idx = stashed.length
    stashed.push(whole)
    return `\u0000PEBBLE_FENCE_${idx}\u0000`
  })
  const out = normalizeAssetSegment(masked, assetNames)
  if (!stashed.length) return out
  return out.replace(/\u0000PEBBLE_FENCE_(\d+)\u0000/g, (_, idx) => stashed[Number(idx)] ?? '')
}

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

  const recordHtmlImage = (asset: CompanionAsset) => {
    if (!seenHtmlIds.has(asset.id)) {
      seenHtmlIds.add(asset.id)
      htmlImages.push(asset)
    }
  }

  const resolveHtmlImageAsset = (rawSrc: string): CompanionAsset | undefined => {
    const trimmed = rawSrc.trim()
    if (!trimmed || /^(?:https?:|data:|blob:)/i.test(trimmed)) return undefined
    const macroMatch = trimmed.match(IMG_MACRO_SRC_RE)
    const ref = (macroMatch ? macroMatch[1] : trimmed).trim()
    const asset = ref ? lookup(ref) : undefined
    if (!asset || !(asset.mime_type || '').startsWith('image/')) return undefined
    recordHtmlImage(asset)
    return asset
  }

  // Extract inline HTML <video> and <audio> blocks that point to bundled assets
  // so they play via Pebble's media cards/pills instead of being stripped by DOMPurify.
  const sanitized = text
    .replace(/\u0000/g, '')
    .replace(HTML_VIDEO_TAG_RE, (block) => {
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
    .replace(HTML_AUDIO_TAG_RE, (block) => {
      let matchedAudio = false
      for (const m of block.matchAll(HTML_ATTR_SRC_RE)) {
        const rawSrc = (m[2] ?? m[3] ?? '').trim()
        if (!rawSrc || /^(?:https?:|data:|blob:)/i.test(rawSrc)) continue
        const macroMatch = rawSrc.match(AUDIO_MACRO_SRC_RE)
        const ref = (macroMatch ? macroMatch[1] : rawSrc).trim()
        const asset = ref ? lookup(ref) : undefined
        if (asset && (asset.mime_type || '').startsWith('audio/')) {
          collector.recordAsset(asset, macroMatch && /bgm/i.test(rawSrc) ? 'bgm' : 'audio')
          matchedAudio = true
        }
      }
      return matchedAudio ? '' : block
    })
    .replace(HTML_STYLE_TAG_RE, (_, openTag: string, cssBody: string, closeTag: string) => {
      const res = rewriteCssAssetUrls(cssBody, lookup)
      for (const a of res.assets) recordHtmlImage(a)
      return `${openTag}${res.css}${closeTag}`
    })
    .replace(HTML_INLINE_STYLE_ATTR_RE, (whole, prefix: string, dq?: string, sq?: string) => {
      const val = dq ?? sq ?? ''
      if (!/url\s*\(/i.test(val)) return whole
      const res = rewriteCssAssetUrls(val, lookup)
      if (!res.assets.length) return whole
      for (const a of res.assets) recordHtmlImage(a)
      const quote = dq !== undefined ? '"' : "'"
      return `${prefix}${quote}${res.css}${quote}`
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

import type { CompanionAsset } from '@/api/companion'
import type { BeatLine } from '@/data/chat'
import type { DisplayRegexRule } from './displayRegex'

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

const IMG_TAG_SHORTHAND_REF_RE = /(<img\s*=\s*)(?:"([^"]+)"|'([^']+)'|([^"'\n=<>`]+?)(?=\s*\/?>))/gi
const HTML_IMG_SRC_REF_RE = /(<img\b[^>]*?\bsrc\s*=\s*)(?:"([^"\n<>]+)"|'([^'\n<>]+)')/gi
const ASSET_MACRO_REF_RE = /(\{\{\s*(?:img|image|asset|raw)\s*::\s*)([^}]+?)(\s*\}\})/gi
const MALFORMED_IMG_TAG_RE =
  /<(([a-zA-Z][a-zA-Z0-9_-]{0,15})\s*\(\s*(?:"([^"\n<>]*)"|'([^'\n<>]*)'|([^()\n<>]+?))\s*\)\s*\/?|([a-zA-Z][a-zA-Z0-9_-]{1,15})\s*=\s*(?:"([^"\n<>]*)"|'([^'\n<>]*)'|([^"'\n=<>`]+?))(?:\s*\/?)|([a-zA-Z][a-zA-Z0-9_-]{1,15})\s+\bsrc\s*=\s*(?:"([^"\n<>]*)"|'([^'\n<>]*)')(?:\s*\/?)|([a-zA-Z][a-zA-Z0-9_-]{1,15})\s+(?:"([^"\n<>]+)"|'([^'\n<>]+)')\s*\/?)>/g
const MALFORMED_MACRO_TAG_RE =
  /\{\{\s*(mg|mgr|imb|imgr|im|irm|firm|fimg|ig|pic|cg|emote|sprite)\s*::\s*([^}\n]+?)\s*\}\}/gi
const INLINE_BACKTICK_TAG_RE = /`(<[^`\n<>]+>|\{\{\s*[a-zA-Z0-9_-]+\s*::[^`\n{}]+\}\})`/g
const PRECEDING_VALID_IMAGE_RE =
  /(?:<img\b[^>]*>|\{\{\s*(?:img|image|asset|raw)\s*::[^}]+\}\}|!\[[^\]]*\]\([^)]+\))\s*$/i
const FENCED_CODE_BLOCK_RE =
  /(^|\n)([ \t]*)(`{3,}|~{3,})[^\n]*\n[\s\S]*?\n\2\3[ \t]*(?=\n|$)/g
const COMPACT_SEP_RE = /[\s_-]+/g
const SNAKE_SEP_RE = /[\s-]+/g
const NON_PAREN_PROTOCOLS = new Set(['http', 'https', 'ftp', 'mailto'])
const HTML_SRC_TAGS = new Set([
  'img', 'audio', 'video', 'source', 'track', 'iframe', 'embed', 'input', 'script', 'frame',
])
const STANDARD_HTML_TAGS = new Set([
  'a', 'abbr', 'address', 'area', 'article', 'aside', 'audio', 'b', 'base', 'bdi', 'bdo',
  'blockquote', 'body', 'br', 'button', 'canvas', 'caption', 'center', 'cite', 'code', 'col',
  'colgroup', 'data', 'datalist', 'dd', 'del', 'details', 'dfn', 'dialog', 'div', 'dl', 'dt',
  'em', 'embed', 'fieldset', 'figcaption', 'figure', 'font', 'footer', 'form', 'frame',
  'frameset', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'head', 'header', 'hr', 'html', 'i',
  'iframe', 'input', 'ins', 'kbd', 'label', 'legend', 'li', 'link', 'main', 'map', 'mark',
  'meta', 'meter', 'nav', 'noscript', 'object', 'ol', 'optgroup', 'option', 'output', 'p',
  'param', 'picture', 'pre', 'progress', 'q', 'rp', 'rt', 'ruby', 's', 'samp', 'script',
  'section', 'select', 'small', 'source', 'span', 'strong', 'style', 'sub', 'summary', 'sup',
  'svg', 'table', 'tbody', 'td', 'template', 'textarea', 'tfoot', 'th', 'thead', 'time',
  'title', 'tr', 'track', 'u', 'ul', 'var', 'video', 'wbr',
])
const KNOWN_IMAGE_TAG_TYPOS = new Set([
  'mg', 'mgr', 'imb', 'imgr', 'im', 'irm', 'firm', 'fimg', 'ig',
  'img_', 'image', 'pic', 'cg', 'em', 'emo', 'emote', 'sprite', 'asset',
])
const BUILTIN_CBS_MACROS = new Set([
  'if', 'if_pure', 'each', 'when', 'getvar', 'setvar', 'addvar', 'gettempvar', 'settempvar',
  'tempvar', 'addtempvar', 'getglobalvar', 'slot', 'hud', 'array', 'makearray', 'spread',
  'equal', 'not_equal', 'notequal', 'greater', 'less', 'greater_equal', 'greaterequal',
  'less_equal', 'lessequal', 'and', 'or', 'not', 'replace', 'lower', 'upper', 'trim',
  'length', 'array_length', 'contains', 'startswith', 'endswith', 'split', 'join', 'calc',
  'random', 'pick', 'comment', 'raw', 'img', 'image', 'asset', 'audio', 'bgm', 'video',
])
const PATTERN_HTML_TAG_RE = /\\?<([a-zA-Z][a-zA-Z0-9_-]{0,23})(?=\\s|\\b|[\s=>/("'])/g
const PATTERN_MACRO_TAG_RE = /(?:\\?\{){2}\s*([a-zA-Z][a-zA-Z0-9_-]{0,23})\s*::/g
const IMAGE_REPLACE_HINT_RE =
  /<img\b|\{\{\s*(?:raw|img|image|asset)\s*::|(?:embeded|embedded):\/\//i
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

  const qSnake = q.replace(SNAKE_SEP_RE, '_').replace(/^_+|_+$/g, '')
  if (qSnake.length >= 4) {
    const suffixHits = candidates
      .filter(([cLow]) => cLow.endsWith('_' + qSnake) || cLow.endsWith('-' + qSnake))
      .map(([, cOrig]) => cOrig)
    if (suffixHits.length === 1) return suffixHits[0]
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

export interface ProtectedAssetTags {
  htmlTags: Set<string>
  macroTags: Set<string>
  primaryHtml: string
  primaryMacro: string
}

export interface NormalizeAssetTagOptions {
  rules?: DisplayRegexRule[]
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * Derive custom HTML/macro tag names declared by enabled card regex rules so
 * card-specific tags such as `<pimg="...">` or `{{pic::...}}` are preserved.
 */
export function extractProtectedAssetTags(rules?: DisplayRegexRule[]): ProtectedAssetTags {
  const htmlTags = new Set<string>()
  const macroTags = new Set<string>()
  const imageHtmlCandidates: string[] = []
  const imageMacroCandidates: string[] = []

  if (!rules?.length) {
    return { htmlTags, macroTags, primaryHtml: 'img', primaryMacro: 'img' }
  }

  for (const rule of rules) {
    if (!rule || rule.enabled === false || !rule.pattern) continue
    const pattern = String(rule.pattern)
    const isImageRule = IMAGE_REPLACE_HINT_RE.test(String(rule.replace ?? ''))

    PATTERN_HTML_TAG_RE.lastIndex = 0
    let hm: RegExpExecArray | null
    while ((hm = PATTERN_HTML_TAG_RE.exec(pattern)) !== null) {
      const start = hm.index
      if (start >= 2 && pattern.slice(start - 2, start) === '(?') continue
      const tag = hm[1].toLowerCase()
      if (STANDARD_HTML_TAGS.has(tag)) continue
      htmlTags.add(tag)
      if (isImageRule && !imageHtmlCandidates.includes(tag)) {
        imageHtmlCandidates.push(tag)
      }
    }

    PATTERN_MACRO_TAG_RE.lastIndex = 0
    let mm: RegExpExecArray | null
    while ((mm = PATTERN_MACRO_TAG_RE.exec(pattern)) !== null) {
      const macro = mm[1].toLowerCase()
      if (BUILTIN_CBS_MACROS.has(macro)) continue
      macroTags.add(macro)
      if (isImageRule && !imageMacroCandidates.includes(macro)) {
        imageMacroCandidates.push(macro)
      }
    }
  }

  let primaryHtml = 'img'
  if (imageHtmlCandidates.includes('img') || (htmlTags.has('img') && !imageHtmlCandidates.length)) {
    primaryHtml = 'img'
  } else if (imageHtmlCandidates.length) {
    primaryHtml = imageHtmlCandidates[0]
  }
  const primaryMacro = imageMacroCandidates.length ? imageMacroCandidates[0] : 'img'
  return { htmlTags, macroTags, primaryHtml, primaryMacro }
}

function isLikelyImageTagTypo(tagLow: string, protectedHtml: Set<string>): boolean {
  if (
    !tagLow ||
    STANDARD_HTML_TAGS.has(tagLow) ||
    HTML_SRC_TAGS.has(tagLow) ||
    protectedHtml.has(tagLow)
  ) {
    return false
  }
  if (KNOWN_IMAGE_TAG_TYPOS.has(tagLow)) return true
  if (editDistance(tagLow, 'img') <= 1) return true
  for (const ptag of protectedHtml) {
    if (ptag.length >= 3 && editDistance(tagLow, ptag) <= 1) return true
  }
  return false
}

function isPrecedingValidImage(preceding: string, protectedTags: ProtectedAssetTags): boolean {
  if (PRECEDING_VALID_IMAGE_RE.test(preceding)) return true
  const customHtml = [...protectedTags.htmlTags].filter((t) => t !== 'img').sort()
  if (customHtml.length) {
    const alt = customHtml.map(escapeRegExp).join('|')
    if (new RegExp(`<(?:${alt})\\b[^>]*>\\s*$`, 'i').test(preceding)) return true
  }
  if (protectedTags.macroTags.size) {
    const altM = [...protectedTags.macroTags].sort().map(escapeRegExp).join('|')
    if (new RegExp(`\\{\\{\\s*(?:${altM})\\s*::[^}]+\\}\\}\\s*$`, 'i').test(preceding)) {
      return true
    }
  }
  return false
}

function repairDeclaredTagRefs(
  text: string,
  assetNames: string[],
  protectedTags: ProtectedAssetTags,
): string {
  const fixShorthand = (
    whole: string,
    prefix: string,
    dq?: string,
    sq?: string,
    uq?: string,
  ): string => {
    const rawRef = (dq ?? sq ?? uq ?? '').trim()
    const quote = sq !== undefined ? "'" : '"'
    const fixed = resolveFuzzyAssetName(rawRef, assetNames)
    if (!fixed || (fixed === rawRef && uq === undefined)) return whole
    return `${prefix}${quote}${fixed}${quote}`
  }

  const fixHtmlSrc = (whole: string, prefix: string, dq?: string, sq?: string): string => {
    const rawRef = (dq ?? sq ?? '').trim()
    if (!rawRef || rawRef.includes('{{') || NON_REF_PREFIX_RE.test(rawRef)) return whole
    const quote = sq !== undefined ? "'" : '"'
    const fixed = resolveFuzzyAssetName(rawRef, assetNames)
    if (!fixed || fixed === rawRef) return whole
    return `${prefix}${quote}${fixed}${quote}`
  }

  const fixMacro = (whole: string, prefix: string, rawRef: string, suffix: string): string => {
    const fixed = resolveFuzzyAssetName(rawRef, assetNames)
    return !fixed || fixed === rawRef ? whole : `${prefix}${fixed}${suffix}`
  }

  let out = text.replace(IMG_TAG_SHORTHAND_REF_RE, fixShorthand)
  out = out.replace(HTML_IMG_SRC_REF_RE, fixHtmlSrc)
  out = out.replace(ASSET_MACRO_REF_RE, fixMacro)

  const customHtml = [...protectedTags.htmlTags].filter((t) => t !== 'img').sort()
  if (customHtml.length) {
    const alt = customHtml.map(escapeRegExp).join('|')
    const customShorthandRe = new RegExp(
      `(<(?:${alt})\\s*=\\s*)(?:"([^"]+)"|'([^']+)'|([^"'\\n=<>\\\`]+?)(?=\\s*/?>))`,
      'gi',
    )
    const customSrcRe = new RegExp(
      `(<(?:${alt})\\b[^>]*?\\bsrc\\s*=\\s*)(?:"([^"\\n<>]+)"|'([^'\\n<>]+)')`,
      'gi',
    )
    out = out.replace(customShorthandRe, fixShorthand)
    out = out.replace(customSrcRe, fixHtmlSrc)
  }

  if (protectedTags.macroTags.size) {
    const altM = [...protectedTags.macroTags].sort().map(escapeRegExp).join('|')
    const customMacroRe = new RegExp(`(\\{\\{\\s*(?:${altM})\\s*::\\s*)([^}]+?)(\\s*\\}\\})`, 'gi')
    out = out.replace(customMacroRe, fixMacro)
  }

  return out
}

function normalizeAssetSegment(
  text: string,
  assetNames: string[],
  protectedTags: ProtectedAssetTags,
): string {
  let out = text.replace(INLINE_BACKTICK_TAG_RE, (whole, inner: string) => {
    IMG_TAG_SHORTHAND_REF_RE.lastIndex = 0
    HTML_IMG_SRC_REF_RE.lastIndex = 0
    ASSET_MACRO_REF_RE.lastIndex = 0
    MALFORMED_MACRO_TAG_RE.lastIndex = 0
    if (
      IMG_TAG_SHORTHAND_REF_RE.test(inner) ||
      HTML_IMG_SRC_REF_RE.test(inner) ||
      ASSET_MACRO_REF_RE.test(inner) ||
      MALFORMED_MACRO_TAG_RE.test(inner)
    ) {
      return inner
    }
    if (protectedTags.macroTags.size) {
      const altM = [...protectedTags.macroTags].sort().map(escapeRegExp).join('|')
      if (new RegExp(`^\\{\\{\\s*(?:${altM})\\s*::\\s*[^}\\n]+\\s*\\}\\}$`, 'i').test(inner)) {
        return inner
      }
    }
    MALFORMED_IMG_TAG_RE.lastIndex = 0
    const mBad = MALFORMED_IMG_TAG_RE.exec(inner)
    if (mBad && mBad[0] === inner) {
      const tagName = (mBad[2] ?? mBad[6] ?? mBad[10] ?? mBad[13] ?? '').toLowerCase()
      if (
        protectedTags.htmlTags.has(tagName) ||
        isLikelyImageTagTypo(tagName, protectedTags.htmlTags)
      ) {
        return inner
      }
    }
    return whole
  })

  out = repairDeclaredTagRefs(out, assetNames, protectedTags)

  let removedAny = false

  MALFORMED_IMG_TAG_RE.lastIndex = 0
  if (MALFORMED_IMG_TAG_RE.test(out)) {
    MALFORMED_IMG_TAG_RE.lastIndex = 0
    const parts: string[] = []
    let cursor = 0
    let match: RegExpExecArray | null
    while ((match = MALFORMED_IMG_TAG_RE.exec(out)) !== null) {
      parts.push(out.slice(cursor, match.index))
      cursor = match.index + match[0].length
      const parenTag = match[2]
      const equalsTag = match[6]
      const srcTag = match[10]
      const quotedTag = match[13]
      const tagName = parenTag ?? equalsTag ?? srcTag ?? quotedTag ?? ''
      const tagLow = tagName.toLowerCase()

      if (equalsTag && tagLow === 'img') {
        parts.push(match[0])
        continue
      }
      if (protectedTags.htmlTags.has(tagLow)) {
        parts.push(match[0])
        continue
      }
      if (parenTag && NON_PAREN_PROTOCOLS.has(tagLow)) {
        parts.push(match[0])
        continue
      }
      if (srcTag && HTML_SRC_TAGS.has(tagLow)) {
        parts.push(match[0])
        continue
      }
      if (STANDARD_HTML_TAGS.has(tagLow)) {
        parts.push(match[0])
        continue
      }

      const rawRef = (
        match[3] ??
        match[4] ??
        match[5] ??
        match[7] ??
        match[8] ??
        match[9] ??
        match[11] ??
        match[12] ??
        match[14] ??
        match[15] ??
        ''
      ).trim()
      const isTypo =
        isLikelyImageTagTypo(tagLow, protectedTags.htmlTags) ||
        (Boolean(quotedTag) && tagLow === 'img')
      const fixed = resolveFuzzyAssetName(rawRef, assetNames)

      const preceding = parts.join('')
      if ((isTypo || fixed !== null) && isPrecedingValidImage(preceding, protectedTags)) {
        removedAny = true
        continue
      }

      if (fixed) {
        if (isTypo) {
          parts.push(`<${protectedTags.primaryHtml}="${fixed}">`)
          continue
        }
        if (equalsTag) {
          const isQuoted = match[7] !== undefined || match[8] !== undefined
          if (isQuoted || /[\s_/-]/.test(rawRef)) {
            parts.push(`<${protectedTags.primaryHtml}="${fixed}">`)
            continue
          }
        } else if (parenTag) {
          parts.push(`<${protectedTags.primaryHtml}="${fixed}">`)
          continue
        }
      }

      if (!isTypo) {
        parts.push(match[0])
        continue
      }

      removedAny = true
    }
    parts.push(out.slice(cursor))
    out = parts.join('')
  }

  MALFORMED_MACRO_TAG_RE.lastIndex = 0
  if (MALFORMED_MACRO_TAG_RE.test(out)) {
    MALFORMED_MACRO_TAG_RE.lastIndex = 0
    const parts: string[] = []
    let cursor = 0
    let match: RegExpExecArray | null
    while ((match = MALFORMED_MACRO_TAG_RE.exec(out)) !== null) {
      parts.push(out.slice(cursor, match.index))
      cursor = match.index + match[0].length
      const macroName = (match[1] ?? '').trim().toLowerCase()
      if (protectedTags.macroTags.has(macroName)) {
        parts.push(match[0])
        continue
      }
      const preceding = parts.join('')
      const rawRef = (match[2] ?? '').trim()
      if (isPrecedingValidImage(preceding, protectedTags)) {
        removedAny = true
        continue
      }
      const fixed = resolveFuzzyAssetName(rawRef, assetNames)
      if (fixed) {
        parts.push(`{{${protectedTags.primaryMacro}::${fixed}}}`)
      } else {
        removedAny = true
      }
    }
    parts.push(out.slice(cursor))
    out = parts.join('')
  }

  if (removedAny) {
    out = out.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').replace(/\s+$/, '')
  }

  return out
}

/**
 * Repair fuzzy asset refs inside valid/declared tags after Display Regex runs,
 * without stripping or rewriting any custom HTML/tags generated by the card.
 */
export function repairAssetTagRefs(
  text: string,
  assetNames: string[],
  options?: NormalizeAssetTagOptions,
): string {
  if (!text || !assetNames.length) return text
  const protectedTags = extractProtectedAssetTags(options?.rules)
  const stashed: string[] = []
  const masked = text.replace(FENCED_CODE_BLOCK_RE, (whole) => {
    const idx = stashed.length
    stashed.push(whole)
    return `\u0000PEBBLE_FENCE_${idx}\u0000`
  })
  const out = repairDeclaredTagRefs(masked, assetNames, protectedTags)
  if (!stashed.length) return out
  return out.replace(/\u0000PEBBLE_FENCE_(\d+)\u0000/g, (_, idx) => stashed[Number(idx)] ?? '')
}

/**
 * Fix high-confidence LLM typos in declared `<img="...">` / custom card tags /
 * `{{img/raw::...}}` and repair or strip malformed LLM image tags before Display Regex runs.
 */
export function normalizeAssetTagRefs(
  text: string,
  assetNames: string[],
  options?: NormalizeAssetTagOptions,
): string {
  if (!text || !assetNames.length) return text
  const protectedTags = extractProtectedAssetTags(options?.rules)
  const stashed: string[] = []
  const masked = text.replace(FENCED_CODE_BLOCK_RE, (whole) => {
    const idx = stashed.length
    stashed.push(whole)
    return `\u0000PEBBLE_FENCE_${idx}\u0000`
  })
  const out = normalizeAssetSegment(masked, assetNames, protectedTags)
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

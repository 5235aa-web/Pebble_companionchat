import { marked } from 'marked'
import DOMPurify from 'dompurify'

const SVG_ALLOWED_TAGS = [
  'svg', 'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'ellipse',
  'g', 'defs', 'lineargradient', 'radialgradient', 'stop',
]

const BASE_ALLOWED_TAGS = [
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'p', 'br', 'hr', 'blockquote', 'pre', 'code',
  'span', 'div', 'section', 'article', 'header', 'footer', 'aside',
  'strong', 'b', 'em', 'i', 'u', 's', 'del', 'ins', 'mark',
  'small', 'sub', 'sup', 'kbd', 'abbr', 'cite', 'q', 'ruby', 'rt', 'rp',
  'time', 'wbr',
  'ul', 'ol', 'li', 'dl', 'dt', 'dd',
  'details', 'summary',
  'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption', 'colgroup', 'col',
  'a', 'figure', 'figcaption', 'img',
  ...SVG_ALLOWED_TAGS,
]

const SANDBOX_ALLOWED_TAGS = [...BASE_ALLOWED_TAGS, 'style', 'input', 'label']

const BASE_FORBID_TAGS = [
  'script', 'style', 'iframe', 'frame', 'frameset', 'object', 'embed',
  'applet', 'base', 'head', 'title', 'link', 'meta', 'form', 'input', 'textarea', 'button',
  'select', 'option', 'math', 'video', 'audio', 'source', 'canvas',
  'foreignobject', 'animate', 'animatetransform', 'animatemotion', 'set', 'use',
  'noscript', 'template', 'slot',
]

const SANDBOX_FORBID_TAGS = [
  'script', 'iframe', 'frame', 'frameset', 'object', 'embed',
  'applet', 'base', 'head', 'title', 'link', 'meta', 'form', 'textarea', 'button',
  'select', 'option', 'math', 'video', 'audio', 'source', 'canvas',
  'foreignobject', 'animate', 'animatetransform', 'animatemotion', 'set', 'use',
  'noscript', 'template', 'slot',
]

const SVG_ALLOWED_ATTR = [
  'viewbox', 'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin',
  'stroke-dasharray', 'd', 'cx', 'cy', 'r', 'rx', 'ry', 'x', 'y', 'x1', 'y1', 'x2', 'y2',
  'points', 'transform', 'opacity', 'offset', 'stop-color', 'stop-opacity',
  'xmlns', 'preserveaspectratio', 'gradientunits',
]

const BASE_ALLOWED_ATTR = [
  'class', 'style', 'href', 'target', 'rel', 'title', 'alt', 'src',
  'width', 'height', 'colspan', 'rowspan', 'align', 'valign',
  'open', 'start', 'type', 'reversed',
  ...SVG_ALLOWED_ATTR,
]

const SANDBOX_ALLOWED_ATTR = [
  ...BASE_ALLOWED_ATTR,
  'id', 'name', 'for', 'checked', 'disabled', 'readonly', 'value',
  'min', 'max', 'step', 'placeholder', 'role', 'tabindex',
]

const BASE_FORBID_ATTR = ['id', 'name', 'srcdoc', 'formaction', 'xlink:href']
const SANDBOX_FORBID_ATTR = ['srcdoc', 'formaction', 'xlink:href']
const SAFE_INPUT_TYPES = new Set(['checkbox', 'radio', 'range', 'text', 'number'])

const ALLOWED_URI_REGEXP =
  /^(?:(?:https?|mailto):|data:image\/(?:png|jpe?g|gif|webp|avif);base64,|pebble-asset:\d+|#)/i

const SAFE_IMG_SRC_RE =
  /^(?:https?:\/\/|data:image\/(?:png|jpe?g|gif|webp|avif);base64,|pebble-asset:\d+)/i

const SAFE_CSS_URL_RE =
  /^(?:https?:\/\/|data:image\/(?:png|jpe?g|gif|webp|avif);base64,|blob:|\/(?!\/))/i

const SAFE_STYLE_PROPS = new Set([
  'color',
  'background',
  'background-color',
  'background-image',
  'background-size',
  'background-position',
  'background-repeat',
  'background-clip',
  'border',
  'border-top',
  'border-right',
  'border-bottom',
  'border-left',
  'border-color',
  'border-style',
  'border-width',
  'border-radius',
  'border-collapse',
  'border-spacing',
  'box-sizing',
  'position',
  'top',
  'right',
  'bottom',
  'left',
  'z-index',
  'transform',
  'transform-origin',
  'aspect-ratio',
  'object-fit',
  'object-position',
  'padding',
  'padding-top',
  'padding-right',
  'padding-bottom',
  'padding-left',
  'margin',
  'margin-top',
  'margin-right',
  'margin-bottom',
  'margin-left',
  'font-size',
  'font-weight',
  'font-style',
  'font-family',
  'line-height',
  'letter-spacing',
  'text-align',
  'text-decoration',
  'text-transform',
  'text-shadow',
  'text-overflow',
  'display',
  'flex',
  'flex-direction',
  'flex-wrap',
  'flex-grow',
  'flex-shrink',
  'flex-basis',
  'gap',
  'row-gap',
  'column-gap',
  'align-items',
  'align-self',
  'justify-content',
  'justify-self',
  'place-items',
  'grid-template-columns',
  'grid-column',
  'grid-row',
  'width',
  'max-width',
  'min-width',
  'height',
  'max-height',
  'min-height',
  'opacity',
  'box-shadow',
  'filter',
  'backdrop-filter',
  'overflow',
  'overflow-x',
  'overflow-y',
  'white-space',
  'word-break',
  'overflow-wrap',
  'vertical-align',
  'list-style',
  'visibility',
  'pointer-events',
  'cursor',
  'user-select',
  'transition',
])

const UNSAFE_STYLE_VALUE_RE =
  /expression\s*\(|@import|javascript:|vbscript:|behavior\s*:|-moz-binding/i

function resolveCssUrlValues(raw: string, assetUrls?: Record<number, string>): string {
  return raw.replace(
    /url\s*\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*))\s*\)/gi,
    (_, dq?: string, sq?: string, uq?: string) => {
      const target = (dq ?? sq ?? uq ?? '').trim()
      let resolved = target
      if (/^pebble-asset:\d+$/i.test(target)) {
        const id = Number(target.slice('pebble-asset:'.length))
        resolved = (Number.isInteger(id) && assetUrls?.[id]) || ''
      }
      if (resolved && SAFE_CSS_URL_RE.test(resolved)) {
        const clean = resolved.replace(/["'()\\]/g, '')
        return `url("${clean}")`
      }
      return 'none'
    },
  )
}

/**
 * Sanitize card-level CSS (`backgroundHTML` or message `<style>` blocks) so it
 * can execute safely inside a per-message Shadow DOM sandbox without `@import`,
 * script expressions, external trackers, or viewport-escaping fixed overlays.
 */
export function sanitizeCardCss(raw: string, assetUrls?: Record<number, string>): string {
  if (!raw || !raw.trim()) return ''
  const withoutDangerous = raw
    .replace(/<\/?style\b[^>]*>/gi, '')
    .replace(/<\/?script\b[^>]*>/gi, '')
    .replace(/@import\b[^;\n]*(?:;|$)/gi, '')
    .replace(/expression\s*\([^)]*\)/gi, 'none')
    .replace(/(?:javascript|vbscript)\s*:/gi, '')
    .replace(/-moz-binding\s*:[^;]*(?:;|$)/gi, '')
    .replace(/behavior\s*:[^;]*(?:;|$)/gi, '')
    .replace(/(\bposition\s*:\s*)fixed\b/gi, '$1absolute')
    .replace(/(^|[},\s])(?::root|html|body)(?=\s*[{,.:#[\s])/gi, '$1:host')
  return resolveCssUrlValues(withoutDangerous, assetUrls).trim()
}

function sanitizeInlineStyle(raw: string, assetUrls?: Record<number, string>): string {
  const kept: string[] = []
  for (const decl of raw.split(';')) {
    const idx = decl.indexOf(':')
    if (idx <= 0) continue
    const prop = decl.slice(0, idx).trim().toLowerCase()
    let val = decl.slice(idx + 1).trim()
    if (!prop || !val) continue
    if (!SAFE_STYLE_PROPS.has(prop)) continue
    if (UNSAFE_STYLE_VALUE_RE.test(val)) continue
    if (/url\s*\(/i.test(val)) {
      if (prop !== 'background' && prop !== 'background-image') continue
      val = resolveCssUrlValues(val, assetUrls)
    }
    kept.push(`${prop}: ${val}`)
  }
  return kept.join('; ')
}

let purifyHookInstalled = false
let activeSanitizeAssetUrls: Record<number, string> | undefined

function ensurePurifyHooks(): void {
  if (purifyHookInstalled) return
  purifyHookInstalled = true
  DOMPurify.addHook(
    'uponSanitizeAttribute',
    (node: Element, data: { attrName: string; attrValue: string; keepAttr: boolean }) => {
    const attr = data.attrName.toLowerCase()
    if (attr === 'style') {
      const clean = sanitizeInlineStyle(data.attrValue, activeSanitizeAssetUrls)
      if (!clean) {
        data.keepAttr = false
      } else {
        data.attrValue = clean
      }
      return
    }
    if (attr === 'src' && node.nodeName === 'IMG') {
      if (!SAFE_IMG_SRC_RE.test(data.attrValue.trim())) {
        data.keepAttr = false
      }
      return
    }
    if (attr === 'type' && node.nodeName === 'INPUT') {
      const kind = data.attrValue.trim().toLowerCase()
      if (!SAFE_INPUT_TYPES.has(kind)) {
        data.attrValue = 'checkbox'
      }
    }
  })
}

function classifyParagraph(p: HTMLParagraphElement): boolean {
  const lead = (p.textContent ?? '').trimStart()
  if (!lead && !p.querySelector('img, br, hr')) {
    p.remove()
    return false
  }
  p.classList.add(lead.startsWith('(') || lead.startsWith('（') ? 'direction' : 'say')
  return true
}

function trimEdgeWhitespace(nodes: Node[]): Node[] {
  const list = [...nodes]
  while (list.length) {
    const first = list[0]
    if (first.nodeType === Node.TEXT_NODE) {
      const trimmed = (first.textContent ?? '').replace(/^\s+/, '')
      if (!trimmed) {
        list.shift()
        continue
      }
      first.textContent = trimmed
    }
    break
  }
  while (list.length) {
    const last = list[list.length - 1]
    if (last.nodeType === Node.TEXT_NODE) {
      const trimmed = (last.textContent ?? '').replace(/\s+$/, '')
      if (!trimmed) {
        list.pop()
        continue
      }
      last.textContent = trimmed
    }
    break
  }
  return list
}

function transformParagraphs(root: DocumentFragment): void {
  const paragraphs = Array.from(root.querySelectorAll('p'))
  for (const p of paragraphs) {
    const hasTopBr = Array.from(p.childNodes).some((n) => n.nodeName === 'BR')
    if (!hasTopBr) {
      classifyParagraph(p)
      continue
    }

    const segments: Node[][] = [[]]
    for (const child of Array.from(p.childNodes)) {
      if (child.nodeName === 'BR') {
        segments.push([])
      } else {
        segments[segments.length - 1].push(child)
      }
    }

    const replacement: HTMLParagraphElement[] = []
    for (const seg of segments) {
      const cleaned = trimEdgeWhitespace(seg)
      if (!cleaned.length) continue
      const nextP = document.createElement('p')
      for (const node of cleaned) nextP.appendChild(node)
      if (classifyParagraph(nextP)) {
        replacement.push(nextP)
      }
    }

    if (replacement.length) {
      p.replaceWith(...replacement)
    } else {
      p.remove()
    }
  }
}

const PRUNABLE_IMAGE_SHELL_TAGS = new Set([
  'TD',
  'TH',
  'TR',
  'TBODY',
  'THEAD',
  'TFOOT',
  'TABLE',
  'FIGURE',
  'P',
  'DIV',
  'SPAN',
])
const NON_EMPTY_MEDIA_SELECTOR = 'img,svg,video,audio,input,button,hr,iframe'

function removeImgAndEmptyShells(img: Element): void {
  let cur: Element | null = img.parentElement
  img.remove()
  while (cur && PRUNABLE_IMAGE_SHELL_TAGS.has(cur.nodeName)) {
    const parent: Element | null = cur.parentElement
    if ((cur.textContent ?? '').trim() !== '' || cur.querySelector(NON_EMPTY_MEDIA_SELECTOR)) {
      break
    }
    cur.remove()
    cur = parent
  }
}

function hardenLinksAndMedia(root: DocumentFragment, assetUrls?: Record<number, string>): void {
  for (const a of root.querySelectorAll('a')) {
    a.setAttribute('target', '_blank')
    a.setAttribute('rel', 'noopener noreferrer')
  }
  for (const img of Array.from(root.querySelectorAll('img'))) {
    const src = (img.getAttribute('src') ?? '').trim()
    if (src.startsWith('pebble-asset:')) {
      const id = Number(src.slice('pebble-asset:'.length))
      const resolved = Number.isInteger(id) ? assetUrls?.[id] : undefined
      if (!resolved) {
        removeImgAndEmptyShells(img)
        continue
      }
      img.setAttribute('src', resolved)
    } else if (!src) {
      removeImgAndEmptyShells(img)
      continue
    }
    img.setAttribute('decoding', 'async')
    img.classList.add('rich-img')
  }
}

function appendStreamingCaret(root: DocumentFragment): void {
  const caret = document.createElement('span')
  caret.className = 'caret'
  caret.setAttribute('aria-hidden', 'true')

  const findInlineTarget = (el: Element | null): Element | null => {
    if (!el) return null
    const tag = el.nodeName
    if (tag === 'P' || /^H[1-6]$/.test(tag)) return el
    if (tag === 'LI' || tag === 'BLOCKQUOTE') {
      const nested = el.lastElementChild
      if (nested && (nested.nodeName === 'P' || nested.nodeName === 'UL' || nested.nodeName === 'OL')) {
        return findInlineTarget(nested) ?? el
      }
      return el
    }
    if (tag === 'UL' || tag === 'OL') {
      return findInlineTarget(el.lastElementChild)
    }
    return null
  }

  let lastEl = root.lastElementChild
  while (lastEl && !(lastEl.textContent ?? '').trim() && !lastEl.querySelector('img')) {
    lastEl = lastEl.previousElementSibling
  }
  const target = findInlineTarget(lastEl)
  if (target) {
    target.appendChild(caret)
  } else {
    root.appendChild(caret)
  }
}

const renderCache = new Map<string, string>()
const RENDER_CACHE_MAX = 300

export interface RenderRichOptions {
  sandbox?: boolean
}

const SANDBOX_TRIGGER_RE = /<(?:style|input|label)\b/i

export function needsCardSandbox(rawOrHtml: string, cardCss?: string): boolean {
  if (cardCss && cardCss.trim()) return true
  return SANDBOX_TRIGGER_RE.test(rawOrHtml || '')
}

/**
 * Render Markdown + controlled HTML for single-chat messages and greetings.
 *
 * Preserves Pebble's signature `(stage direction)` styling on plain lines while
 * safely supporting GFM Markdown and sanitized CHARX HTML cards. When `sandbox`
 * is enabled (used inside Shadow DOM), `<style>` and safe `<input>`/`<label>`
 * controls are sanitized and preserved.
 */
export function renderRichMessage(
  raw: string,
  streaming = false,
  assetUrls?: Record<number, string>,
  options?: RenderRichOptions,
): string {
  const trimmed = raw ?? ''
  if (!trimmed.trim()) {
    return streaming ? '<span class="caret" aria-hidden="true"></span>' : ''
  }

  const sandbox = Boolean(options?.sandbox)
  const canCache = !streaming && !sandbox && !trimmed.includes('pebble-asset:')
  if (canCache) {
    const hit = renderCache.get(trimmed)
    if (hit !== undefined) return hit
  }

  ensurePurifyHooks()

  // While streaming, hold back an unclosed trailing `<tag...` fragment so raw
  // markup attributes do not flicker across token boundaries.
  const source = streaming
    ? trimmed.replace(/<[a-zA-Z!/][^>\n]*$/, '')
    : trimmed

  const parsed = marked.parse(source, {
    gfm: true,
    breaks: true,
    async: false,
  }) as string

  let clean: string
  activeSanitizeAssetUrls = assetUrls
  try {
    clean = DOMPurify.sanitize(parsed, {
      ALLOWED_TAGS: sandbox ? SANDBOX_ALLOWED_TAGS : BASE_ALLOWED_TAGS,
      FORBID_TAGS: sandbox ? SANDBOX_FORBID_TAGS : BASE_FORBID_TAGS,
      FORBID_CONTENTS: sandbox ? ['script', 'title'] : ['script', 'style', 'title'],
      ALLOWED_ATTR: sandbox ? SANDBOX_ALLOWED_ATTR : BASE_ALLOWED_ATTR,
      FORBID_ATTR: sandbox ? SANDBOX_FORBID_ATTR : BASE_FORBID_ATTR,
      ALLOW_DATA_ATTR: false,
      ALLOWED_URI_REGEXP,
    })
  } finally {
    activeSanitizeAssetUrls = undefined
  }

  const tpl = document.createElement('template')
  tpl.innerHTML = clean
  if (sandbox) {
    for (const styleEl of tpl.content.querySelectorAll('style')) {
      const cleanedCss = sanitizeCardCss(styleEl.textContent ?? '', assetUrls)
      if (!cleanedCss) {
        styleEl.remove()
      } else {
        styleEl.textContent = cleanedCss
      }
    }
  }
  transformParagraphs(tpl.content)
  hardenLinksAndMedia(tpl.content, assetUrls)
  if (streaming) appendStreamingCaret(tpl.content)

  const out = tpl.innerHTML

  if (canCache) {
    renderCache.set(trimmed, out)
    if (renderCache.size > RENDER_CACHE_MAX) {
      const oldest = renderCache.keys().next().value
      if (oldest !== undefined) renderCache.delete(oldest)
    }
  }

  return out
}

const BLOCK_HTML_OPEN_RE =
  /<(details|div|section|article|table|blockquote|pre|ul|ol|dl|figure)\b[^>]*>/gi
const BLOCK_HTML_CLOSE_RE =
  /<\/(details|div|section|article|table|blockquote|pre|ul|ol|dl|figure)\s*>/gi
const CONTINUATION_LINE_RE =
  /^(?:[ \t]{4}|>|[-*+]\s|\d+[.)]\s|\||[=-]{2,}\s*$|\[[^\]]+\]:\s)/

/**
 * Safely split a long streaming Markdown/HTML reply into `[stablePrefix, activeTail]`
 * at a top-level blank line outside code fences, block HTML tags, lists, and
 * blockquotes. Allows the stable prefix to stay cached in its own DOM subtree
 * while only the trailing block re-renders during streaming.
 */
export function splitStableStreamingPrefix(source: string): [string, string] {
  if (!source || source.length < 900) return ['', source]

  const lines = source.split('\n')
  if (lines.length < 6) return ['', source]

  let fenceChar = ''
  let fenceLen = 0
  let htmlDepth = 0
  let charPos = 0
  const safeBreaks: number[] = []

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i]
    const trimmed = line.trim()

    const fenceMatch = trimmed.match(/^(`{3,}|~{3,})/)
    if (fenceMatch) {
      const marker = fenceMatch[1]
      if (!fenceChar) {
        fenceChar = marker[0]
        fenceLen = marker.length
      } else if (marker[0] === fenceChar && marker.length >= fenceLen && trimmed === marker) {
        fenceChar = ''
        fenceLen = 0
      }
    } else if (!fenceChar) {
      const opens = line.match(BLOCK_HTML_OPEN_RE)?.length ?? 0
      const closes = line.match(BLOCK_HTML_CLOSE_RE)?.length ?? 0
      htmlDepth = Math.max(0, htmlDepth + opens - closes)
    }

    const lineEnd = charPos + line.length + 1
    if (!fenceChar && htmlDepth === 0 && !trimmed && i > 0 && i + 1 < lines.length) {
      const nextLine = lines[i + 1]
      if (nextLine.trim() && !CONTINUATION_LINE_RE.test(nextLine)) {
        safeBreaks.push(lineEnd)
      }
    }
    charPos = lineEnd
  }

  // Keep at least the trailing ~320 chars in the active tail so inline context stays smooth.
  const maxPrefixEnd = source.length - 320
  let splitAt = 0
  for (let i = safeBreaks.length - 1; i >= 0; i -= 1) {
    if (safeBreaks[i] <= maxPrefixEnd) {
      splitAt = safeBreaks[i]
      break
    }
  }

  if (splitAt <= 0) return ['', source]
  return [source.slice(0, splitAt).trimEnd(), source.slice(splitAt)]
}

/** Extract readable plain text from Markdown/HTML messages for TTS read-aloud. */
export function plainTextForSpeech(raw: string): string {
  if (!raw) return ''
  const tpl = document.createElement('template')
  tpl.innerHTML = renderRichMessage(raw, false)
  for (const el of tpl.content.querySelectorAll('style')) {
    el.remove()
  }
  for (const el of tpl.content.querySelectorAll(
    'p, h1, h2, h3, h4, h5, h6, li, blockquote, tr, dt, dd, summary, pre',
  )) {
    el.append('\n')
  }
  return (tpl.content.textContent ?? '').replace(/\n{3,}/g, '\n\n').trim()
}

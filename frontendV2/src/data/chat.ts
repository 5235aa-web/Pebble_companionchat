export type BeatRole = 'ai' | 'you' | 'narration' | 'summary' | 'scene-summary'

export interface BeatLine {
  kind: 'say' | 'direction'
  text: string
}

/**
 * What one generation cost, stored per swipe on the message row.
 *
 * Token counts come from the provider's usage chunk and are labelled
 * `estimated` when the endpoint did not send one and the backend counted the
 * text itself: the footer should not claim precision it does not have.
 */
export interface GenerationMeta {
  model: string
  provider: string
  elapsed_ms: number
  ttft_ms?: number | null
  prompt_tokens?: number | null
  completion_tokens?: number | null
  total_tokens?: number | null
  context_tokens?: number | null
  tokens_source?: 'usage' | 'estimated' | 'none' | string
  status?: string
  at?: string
}

export interface BeatAttachment {
  src: string
  alt: string
}

export interface Beat {
  /** Stable render key: never changes, unlike id which the server assigns. */
  uid: number
  id: number
  role: BeatRole
  speaker: string
  time: string
  thinking: string | null
  lines: BeatLine[]
  /**
   * Unsplit message source, preserving blank lines and indentation so the
   * single-chat Markdown / controlled HTML renderer can parse block syntax.
   */
  raw?: string
  /**
   * The part of a streaming reply that has no newline after it yet.
   *
   * Kept apart from `lines` so a delta only rewrites this one string instead of
   * rebuilding the whole line array. Rendered as one extra paragraph.
   */
  tail?: string
  swipe: string | null
  /** All alternate replies plus which one is showing. */
  swipes?: string[]
  swipeIndex?: number
  marked?: boolean
  attachments?: BeatAttachment[]
  /**
   * Voice turn. The stored content is still the transcript — that is what the
   * model reads on every later turn — but the bubble shows a placeholder so the
   * user does not see their own speech written out.
   */
  voice?: boolean
  /** How many messages a compaction summary folded away. */
  folded?: number
  /** Summary version, shown in the card header. */
  summaryVersion?: number
  /** Scene range a script-mode segment card covers, e.g. "Scene 1-3". */
  summaryLabel?: string
  /** Per-variant generation info, indexed exactly like `swipes`. */
  generations?: GenerationMeta[]
}

export interface CastMember {
  id: string
  avatar: string
  letter: string
  name: string
  role: string
  desc: string
  meta: string
  tags: string
  acts: string
  lore: string
  lastSeen: string
  branch: string
  active: boolean
  pinned?: boolean
  assetCount?: number
  cardFormat?: string
  /**
   * Voice this character speaks in, stored on the companion row as
   * `tts_config`. Only identity is kept here; the engine and its sampling
   * parameters stay global, so switching engines in the panel still changes
   * everyone at once.
   */
  ttsVoice?: { ref: string; id: string }
}

/** True when this cast member was imported from a CHARX package. */
export function isCharxCard(c: Pick<CastMember, 'cardFormat' | 'assetCount'>): boolean {
  return c.cardFormat === 'charx' || (!c.cardFormat && (c.assetCount ?? 0) > 0)
}

export interface Branch {
  id: string
  name: string
  count: number
  active: boolean
}

/**
 * All of the following are empty on purpose: this build ships the shell only.
 * Every value here is meant to be replaced by a backend response.
 */

export const summaryText = ''

export const initialBeats: Beat[] = []

export const initialCast: CastMember[] = []

export const branches: Branch[] = []

export const replies: string[] = []

export const modelList: string[] = []

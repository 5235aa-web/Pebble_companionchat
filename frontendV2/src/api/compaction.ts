import { del, get, post } from './client'

const C = '/companions'

export interface TokenStats {
  companion_id: string
  branch_id: string
  model: string
  provider_type?: string
  message_count: number
  token_count: number
  system_tokens?: number
  history_tokens?: number
  exact?: boolean
  method?: string
  measured?: boolean
  measured_at?: number | null
  context_window: number
  threshold: number
  percentage: number
  should_compact: boolean
  status?: string
}

export interface CompactionRecord {
  id: number
  branch_id: string
  message_count: number
  tokens_before: number
  tokens_after: number
  summary: string
  created_at: string
}

export interface CompactionStats {
  total_compactions: number
  tokens_saved: number
  last_compaction_at?: string | null
}

export const getTokenStats = (
  companionId: string,
  branchId: string,
  model = 'gpt-4',
  providerType = '',
  baseUrl = '',
  maxContext = 0,
): Promise<TokenStats> => {
  const qs = new URLSearchParams({
    model,
    provider_type: providerType,
    base_url: baseUrl,
    max_context: String(maxContext),
  })
  return get(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/tokens?${qs}`,
  )
}

export interface CompactOptions {
  preserve_recent?: number
  max_summary_tokens?: number
  preserve_important?: boolean
  max_important_messages?: number
}

/** The backend body repeats the ids: CompactRequest requires companion_id. */
export const compactBranch = (
  companionId: string,
  branchId: string,
  opts: CompactOptions = {},
): Promise<{ success?: boolean; error?: string }> =>
  post(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/compact`,
    { companion_id: companionId, branch_id: branchId, ...opts },
  )

export const getCompactionHistory = async (
  companionId: string,
  branchId: string,
): Promise<CompactionRecord[]> => {
  const r = await get<{ history?: CompactionRecord[]; records?: CompactionRecord[] }>(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/compaction-history`,
  )
  return r.history ?? r.records ?? []
}

export const getCompactionStats = (
  companionId: string,
  branchId: string,
): Promise<CompactionStats> =>
  get(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/compaction-stats`,
  )

export const getCompactedMessages = async (
  companionId: string,
  branchId: string,
): Promise<Array<{ id: number; role: string; content: string; created_at: string }>> => {
  const r = await get<{
    messages?: Array<{ id: number; role: string; content: string; created_at: string }>
    batches?: Array<{
      batch_id: string
      message_count: number
      messages?: Array<{ id: number; role: string; content: string; created_at: string }>
    }>
  }>(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/compacted-messages`,
  )
  if (r.messages) return r.messages
  // The endpoint groups rows by compaction batch; the panel shows one flat list,
  // so the groups are merged here instead of teaching it the batch shape.
  return (r.batches ?? []).flatMap((b) => b.messages ?? [])
}

export const getLatestSummary = async (
  companionId: string,
  branchId: string,
): Promise<string> => {
  const r = await get<{ summary?: string }>(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/latest-summary`,
  )
  return r.summary ?? ''
}

export const deleteCompactionRecord = (
  companionId: string,
  branchId: string,
  recordId: number,
): Promise<unknown> =>
  del(
    `${C}/${encodeURIComponent(companionId)}/branches/${encodeURIComponent(branchId)}/compaction-history/${recordId}`,
  )

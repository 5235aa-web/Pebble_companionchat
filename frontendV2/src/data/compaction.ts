export interface CompactionRecord {
  id: number
  time: string
  count: number
  saved: string
  preview: string
}

/** Empty on purpose: history comes from the compaction backend. */
export const compactionRecords: CompactionRecord[] = []

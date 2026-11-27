import type { WorldBookEntryCategory, WorldBookEntryTranslations } from '@/api/worldbook'

export interface LoreEntry {
  id: number
  name: string
  badge: string | null
  category: WorldBookEntryCategory
  open: boolean
  on: boolean
  comment: string
  content: string
  translations?: WorldBookEntryTranslations
  keys: string[]
  secKeys: string[]
  position: string
  depth: string
  prob: string
  constant: boolean
  selective: boolean
  enabled: boolean
  logic: 'AND' | 'OR'
  draftKey: string
  draftSecKey: string
}

/** Empty on purpose: entries come from the world-book backend. */
export const initialLore: LoreEntry[] = []

export interface LoreEntry {
  id: number
  name: string
  badge: string | null
  open: boolean
  on: boolean
  comment: string
  content: string
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

import { get, put } from './client'

/**
 * Rules shared by every companion.
 *
 * These are the three card fields that steer a reply — advanced prompt, system
 * instruction, post-history instructions — lifted to the app level, so a rule
 * like "never write my lines" is stated once instead of in every card.
 */
export interface GlobalPrompts {
  advanced: string
  system: string
  postHistory: string
}

export const getGlobalPrompts = (): Promise<GlobalPrompts> => get('/settings/global-prompts')

export const saveGlobalPrompts = (payload: GlobalPrompts): Promise<GlobalPrompts> =>
  put('/settings/global-prompts', payload)

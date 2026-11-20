export interface CharacterCard {
  avatar: string
  name: string
  role: string
  tags: string[]
  description: string
  personality: string
  scenario: string
  firstMes: string
  alternateGreetings: string[]
  mesExample: string
  advancedPrompt: string
  systemPrompt: string
  postHistory: string
  userInfo: string
  creatorNotes: string
  raw: string
  /** A clip in the shared voice library; used by the cloning engines. */
  ttsVoiceRef: string
  /** A built-in voice id; used by MiniMax, ElevenLabs and Ming3's tts task. */
  ttsVoiceId: string
}

/** Empty on purpose: the card is filled from the character backend. */
export const characterCard: CharacterCard = {
  avatar: '',
  name: '',
  role: '',
  tags: [],
  description: '',
  personality: '',
  scenario: '',
  firstMes: '',
  alternateGreetings: [],
  mesExample: '',
  advancedPrompt: '',
  systemPrompt: '',
  postHistory: '',
  userInfo: '',
  creatorNotes: '',
  raw: '',
  ttsVoiceRef: '',
  ttsVoiceId: '',
}

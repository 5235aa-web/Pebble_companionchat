import type { PuppetActionId } from '@/api/inochi2d'

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v))
}

function smoothstep(t: number): number {
  const x = clamp(t, 0, 1)
  return x * x * (3 - 2 * x)
}

export interface ActiveActionClip {
  id: PuppetActionId
  elapsed: number
  duration: number
  intensity: number
}

export interface ActionPose {
  headX: number
  headY: number
  headDipX: number
  headDipY: number
  headTilt: number
  headScale: number
  bodyX: number
  bodyY: number
  bodyTilt: number
  scale: number
  armInward: number
  armSpread: number
  armLift: number
  blushBoost: number
  squintBoost: number
  blinkForce: number
  pupilBoost: number
}

export const ZERO_ACTION_POSE: ActionPose = {
  headX: 0,
  headY: 0,
  headDipX: 0,
  headDipY: 0,
  headTilt: 0,
  headScale: 0,
  bodyX: 0,
  bodyY: 0,
  bodyTilt: 0,
  scale: 0,
  armInward: 0,
  armSpread: 0,
  armLift: 0,
  blushBoost: 0,
  squintBoost: 0,
  blinkForce: 0,
  pupilBoost: 0,
}

export const ACTION_DURATIONS: Record<PuppetActionId, number> = {
  nod: 1.1,
  shake: 1.15,
  lean_in: 2.2,
  hug_lean: 2.6,
  cheer: 1.55,
  shy_hide: 2.1,
  sigh: 1.85,
  startle: 1.15,
  bow: 1.85,
}

export function sampleActionClip(clip: ActiveActionClip | null): ActionPose {
  if (!clip) return ZERO_ACTION_POSE
  const p = clamp(clip.elapsed / Math.max(0.1, clip.duration), 0, 1)
  const k = clip.intensity
  const bell = Math.sin(p * Math.PI)
  const plateau = smoothstep(Math.min(1, p / 0.22)) * smoothstep(Math.min(1, (1 - p) / 0.24))

  switch (clip.id) {
    case 'nod': {
      const wave = Math.sin(p * Math.PI * 3.2) * bell
      const dip = Math.max(0, wave)
      return {
        ...ZERO_ACTION_POSE,
        headY: dip * 0.52 * k,
        headDipY: dip * 0.011 * k,
        bodyY: dip * 0.010 * k,
        blinkForce: p > 0.12 && p < 0.44 ? Math.sin(((p - 0.12) / 0.32) * Math.PI) * 0.85 : 0,
      }
    }
    case 'shake': {
      const wave = Math.sin(p * Math.PI * 4.0) * bell
      return {
        ...ZERO_ACTION_POSE,
        headX: wave * 0.72 * k,
        headDipX: wave * 0.014 * k,
        headTilt: -wave * 0.065 * k,
        bodyTilt: wave * 0.015 * k,
      }
    }
    case 'lean_in': {
      return {
        ...ZERO_ACTION_POSE,
        scale: plateau * 0.16 * k,
        headY: -plateau * 0.16 * k,
        headTilt: plateau * 0.036 * k,
        bodyY: -plateau * 0.008 * k,
        pupilBoost: plateau * 0.07 * k,
      }
    }
    case 'hug_lean': {
      const openEnv =
        p < 0.36
          ? smoothstep(Math.min(1, p / 0.15)) * (1 - smoothstep(clamp((p - 0.18) / 0.18, 0, 1)))
          : 0
      const wrapIn = smoothstep(clamp((p - 0.20) / 0.20, 0, 1))
      const wrapOut = 1 - smoothstep(clamp((p - 0.80) / 0.20, 0, 1))
      const wrapEnv = wrapIn * wrapOut
      const closeEnv = smoothstep(Math.min(1, p / 0.24)) * wrapOut
      const snuggleSway = Math.sin((p - 0.24) * Math.PI * 3.0) * wrapEnv

      return {
        ...ZERO_ACTION_POSE,
        scale: (openEnv * 0.08 + closeEnv * 0.18) * k,
        headY: (-openEnv * 0.12 + wrapEnv * 0.18) * k,
        headDipX: wrapEnv * 0.008 * k,
        headDipY: wrapEnv * 0.006 * k,
        headTilt: (wrapEnv * 0.088 + snuggleSway * 0.038) * k,
        bodyTilt: (snuggleSway * 0.028 + wrapEnv * 0.018) * k,
        armSpread: openEnv * 0.16 * k,
        armInward: wrapEnv * 0.16 * k,
        armLift: (-openEnv * 0.014 - wrapEnv * 0.014) * k,
        blushBoost: closeEnv * 0.82 * k,
        squintBoost: wrapEnv * 0.48 * k,
        blinkForce: wrapEnv * 0.32 * k,
      }
    }
    case 'cheer': {
      const hop = Math.abs(Math.sin(p * Math.PI * 3.0)) * bell
      const rock = Math.sin(p * Math.PI * 3.0) * bell
      return {
        ...ZERO_ACTION_POSE,
        bodyY: -hop * 0.024 * k,
        headY: -hop * 0.18 * k,
        headTilt: rock * 0.072 * k,
        bodyTilt: rock * 0.032 * k,
        armSpread: (bell * 0.12 + hop * 0.06) * k,
        armLift: -hop * 0.018 * k,
        squintBoost: bell * 0.30 * k,
        blushBoost: bell * 0.25 * k,
      }
    }
    case 'shy_hide': {
      return {
        ...ZERO_ACTION_POSE,
        headX: -plateau * 0.34 * k,
        headDipX: -plateau * 0.010 * k,
        headDipY: plateau * 0.006 * k,
        headY: plateau * 0.22 * k,
        headTilt: plateau * 0.088 * k,
        bodyTilt: plateau * 0.028 * k,
        armInward: plateau * 0.12 * k,
        blushBoost: plateau * 0.92 * k,
        squintBoost: plateau * 0.34 * k,
      }
    }
    case 'sigh': {
      const rise = smoothstep(Math.min(1, p / 0.24)) * (1 - smoothstep(clamp((p - 0.24) / 0.22, 0, 1)))
      const drop = p > 0.20 ? Math.sin(((p - 0.20) / 0.80) * Math.PI) : 0
      return {
        ...ZERO_ACTION_POSE,
        headY: (-rise * 0.12 + drop * 0.32) * k,
        headDipY: drop * 0.008 * k,
        headTilt: -drop * 0.045 * k,
        bodyY: (-rise * 0.008 + drop * 0.016) * k,
        blinkForce: drop * 0.62 * k,
      }
    }
    case 'startle': {
      const snap = Math.exp(-p * 4.5) * Math.sin(Math.min(1, p * 4.8) * Math.PI * 0.5)
      return {
        ...ZERO_ACTION_POSE,
        scale: -snap * 0.06 * k,
        headY: -snap * 0.38 * k,
        bodyY: -snap * 0.020 * k,
        armSpread: snap * 0.16 * k,
        armLift: -snap * 0.018 * k,
        blinkForce: -snap * 0.16 * k,
        pupilBoost: -snap * 0.15 * k,
      }
    }
    case 'bow': {
      const down = smoothstep(clamp(p / 0.28, 0, 1))
      const up = 1 - smoothstep(clamp((p - 0.68) / 0.32, 0, 1))
      const env = down * up
      return {
        ...ZERO_ACTION_POSE,
        scale: env * 0.045 * k,
        headY: env * 0.42 * k,
        headDipY: env * 0.010 * k,
        headScale: env * 0.025 * k,
        bodyY: env * 0.036 * k,
        bodyTilt: env * 0.022 * k,
        armInward: env * 0.06 * k,
        blinkForce: env * 0.90 * k,
      }
    }
  }
}

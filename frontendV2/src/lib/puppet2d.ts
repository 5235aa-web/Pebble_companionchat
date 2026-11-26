import type {
  PuppetActionId,
  PuppetAnchors,
  PuppetConfig,
  PuppetExpression,
  PuppetLayer,
  PuppetManifest,
  PuppetSubject,
  RigLayerPatch,
} from '@/api/inochi2d'
import { fileUrl } from '@/api/client'
import { getSpeechSpectrum } from './speechMonitor'
import {
  ACTION_DURATIONS,
  ZERO_ACTION_POSE,
  sampleActionClip,
  type ActionPose,
  type ActiveActionClip,
} from './puppetActions'

const VERT_SRC = `#version 300 es
precision highp float;

in vec2 a_pos;
in vec2 a_uv;

out vec2 v_uv;

void main() {
  v_uv = a_uv;
  gl_Position = vec4(a_pos, 0.0, 1.0);
}
`

const FRAG_SRC = `#version 300 es
precision highp float;

in vec2 v_uv;
uniform sampler2D u_tex;
uniform float u_alpha;
uniform float u_blush;
uniform vec4 u_layerRect;
uniform vec2 u_cheekR;
uniform vec2 u_cheekL;
uniform vec4 u_cheekAxes;
uniform vec3 u_faceMood;
uniform vec4 u_eyeMood;
uniform vec4 u_eyeAnchors;
uniform float u_mouthOpen;

out vec4 outColor;

float cheekGlow(vec2 p, vec2 c, vec2 uHat, vec2 vHat, float rx, float ry) {
  vec2 d = p - c;
  float du = dot(d, uHat) / max(0.001, rx);
  float dv = dot(d, vHat) / max(0.001, ry);
  float dist2 = du * du + dv * dv;
  return exp(-dist2 * 2.1) * smoothstep(1.35, 0.25, sqrt(dist2));
}

void main() {
  vec4 c = texture(u_tex, v_uv);
  if (c.a > 0.01 && (u_blush > 0.005 || u_faceMood.x > 0.005 || u_faceMood.y > 0.005)) {
    vec2 charPos = vec2(
      u_layerRect.x + v_uv.x * u_layerRect.z,
      u_layerRect.y + v_uv.y * u_layerRect.w
    );
    vec2 aspect = vec2(u_cheekAxes.w, 1.0);
    vec2 pIso = charPos * aspect;
    vec2 uHat = u_cheekAxes.xy;
    vec2 vHat = vec2(-uHat.y, uHat.x);
    float dIso = max(0.001, u_cheekAxes.z);
    float skinMask = smoothstep(0.32, 0.65, c.r) * smoothstep(0.18, 0.45, min(c.g, c.b));

    if (u_blush > 0.005) {
      float rx = dIso * 0.40;
      float ry = dIso * 0.24;
      float g = max(
        cheekGlow(pIso, u_cheekR * aspect, uHat, vHat, rx, ry),
        cheekGlow(pIso, u_cheekL * aspect, uHat, vHat, rx, ry)
      );
      vec2 stripeDir = normalize(0.85 * uHat - 0.52 * vHat);
      float stripePhase = dot(pIso, stripeDir) / max(0.001, dIso * 0.048);
      float stripes = smoothstep(0.35, 0.88, sin(stripePhase * 6.28318));
      float tint = clamp((g + g * g * stripes * 0.26 * u_faceMood.z) * u_blush * skinMask, 0.0, 0.68);
      vec3 rose = vec3(1.02, 0.68, 0.72);
      c.rgb = mix(c.rgb, c.rgb * rose + vec3(0.055, -0.025, -0.015), tint);
    }

    if (u_faceMood.y > 0.005) {
      vec2 lidR = u_eyeAnchors.xy * aspect + vHat * (0.14 * dIso);
      vec2 lidL = u_eyeAnchors.zw * aspect + vHat * (0.14 * dIso);
      float lidGlow = max(
        cheekGlow(pIso, lidR, uHat, vHat, dIso * 0.24, dIso * 0.09),
        cheekGlow(pIso, lidL, uHat, vHat, dIso * 0.24, dIso * 0.09)
      );
      float lidTint = clamp(lidGlow * u_faceMood.y * skinMask * 0.44, 0.0, 0.44);
      c.rgb = mix(c.rgb, c.rgb * vec3(1.04, 0.72, 0.76) + vec3(0.04, -0.02, -0.01), lidTint);

      // Delicate translucent corner tear bead on sad > 0.35
      float tearFade = smoothstep(0.35, 0.85, u_faceMood.y);
      if (tearFade > 0.005) {
        vec2 tearR = u_eyeAnchors.xy * aspect - uHat * (0.18 * dIso) + vHat * (0.115 * dIso);
        vec2 tearL = u_eyeAnchors.zw * aspect + uHat * (0.18 * dIso) + vHat * (0.115 * dIso);
        float dropR = cheekGlow(pIso, tearR, uHat, vHat, dIso * 0.042, dIso * 0.056);
        float dropL = cheekGlow(pIso, tearL, uHat, vHat, dIso * 0.042, dIso * 0.056);
        float drop = max(dropR, dropL) * tearFade;
        float specR = cheekGlow(pIso, tearR - vHat * (0.014 * dIso), uHat, vHat, dIso * 0.016, dIso * 0.016);
        float specL = cheekGlow(pIso, tearL - vHat * (0.014 * dIso), uHat, vHat, dIso * 0.016, dIso * 0.016);
        float spec = max(specR, specL) * tearFade;
        c.rgb = mix(c.rgb, c.rgb * vec3(0.92, 0.95, 1.03) + vec3(0.06, 0.08, 0.11), clamp(drop * 0.55, 0.0, 0.55));
        c.rgb = mix(c.rgb, vec3(0.98, 0.99, 1.0), clamp(spec * 0.85, 0.0, 0.85));
      }
    }

    if (u_faceMood.x > 0.005) {
      vec2 eyeMid = 0.5 * (u_eyeAnchors.xy + u_eyeAnchors.zw) * aspect;
      float aboveEyes = dot(eyeMid - pIso, vHat) / dIso;
      float gloomBand = smoothstep(-0.08, 0.46, aboveEyes);
      float faceTone = smoothstep(0.18, 0.45, c.r);
      float gloomAmt = clamp(gloomBand * u_faceMood.x * faceTone * 0.46, 0.0, 0.46);
      c.rgb = mix(c.rgb, c.rgb * vec3(0.68, 0.70, 0.88), gloomAmt);
    }
  }

  if (c.a > 0.15 && (u_eyeMood.x > 0.005 || u_eyeMood.y > 0.005 || u_eyeMood.w > 0.005)) {
    float xArch = sin(clamp(v_uv.x, 0.0, 1.0) * 3.14159);
    // Sad iris catchlight suppression ("empty / hopeless eyes")
    if (u_eyeMood.w > 0.005) {
      float lum = dot(c.rgb, vec3(0.299, 0.587, 0.114));
      float maxC = max(c.r, max(c.g, c.b));
      float minC = min(c.r, min(c.g, c.b));
      float sat = maxC - minC;
      float specMask = smoothstep(0.42, 0.76, lum) * smoothstep(0.30, 0.08, sat) * smoothstep(0.68, 0.22, v_uv.y);
      c.rgb = mix(c.rgb, c.rgb * 0.30, clamp(specMask * u_eyeMood.w * 0.88, 0.0, 0.88));
      c.rgb = mix(c.rgb, vec3(lum) * vec3(0.84, 0.86, 0.94), clamp(u_eyeMood.w * 0.30, 0.0, 0.32));
    }
    if (u_eyeMood.x > 0.005) {
      float lowerBand = smoothstep(0.52, 0.78, v_uv.y) * smoothstep(0.98, 0.74, v_uv.y);
      float shimmer = 0.86 + 0.14 * sin(u_eyeMood.z * 3.8 + v_uv.x * 6.0);
      float tear = clamp(lowerBand * xArch * shimmer * u_eyeMood.x * 0.42, 0.0, 0.45);
      c.rgb = mix(c.rgb, vec3(0.96, 0.98, 1.0), tear);
    }
    if (u_eyeMood.y > 0.005) {
      float midBand = smoothstep(0.18, 0.48, v_uv.y) * smoothstep(0.85, 0.52, v_uv.y);
      float pulse = 0.90 + 0.10 * sin(u_eyeMood.z * 4.5);
      float lift = clamp(midBand * xArch * pulse * u_eyeMood.y * 0.16, 0.0, 0.18);
      c.rgb = min(vec3(1.0), c.rgb * (1.0 + lift) + vec3(0.035, 0.028, 0.02) * lift);
    }
  }

  // Procedural oral cavity interior when mouth opens (speech, testMouth, or surprised/happy)
  if (u_mouthOpen > 0.03 && c.a > 0.05) {
    vec2 mUV = (v_uv - vec2(0.5, 0.50)) / vec2(0.34, 0.16 + 0.24 * u_mouthOpen);
    float r2 = dot(mUV, mUV);
    float cavMask = smoothstep(1.05, 0.58, r2) * smoothstep(0.03, 0.20, u_mouthOpen) * smoothstep(0.08, 0.45, c.a);
    if (cavMask > 0.002) {
      float tongue = smoothstep(0.05, 0.58, mUV.y);
      vec3 cavCol = mix(vec3(0.24, 0.06, 0.09), vec3(0.62, 0.22, 0.26), tongue);
      float teeth = smoothstep(-0.18, -0.54, mUV.y) * smoothstep(0.85, 0.22, abs(mUV.x)) * smoothstep(0.28, 0.55, u_mouthOpen);
      cavCol = mix(cavCol, vec3(0.95, 0.91, 0.89), teeth * 0.72);
      float rim = smoothstep(0.66, 0.90, r2) * (1.0 - smoothstep(0.90, 1.08, r2));
      cavCol = mix(cavCol, vec3(0.18, 0.08, 0.08), rim * 0.55);
      c.rgb = mix(c.rgb, cavCol, cavMask);
    }
  }

  outColor = vec4(c.rgb, c.a * u_alpha);
}
`

interface LayerRuntime {
  meta: PuppetLayer
  tex: WebGLTexture
  loaded: boolean
  isBlink: boolean
  expressionId?: string
  depthGrid: Float32Array
  springX: number
  springY: number
  velX: number
  velY: number
}

const GRID_X = 18
const GRID_Y = 22
const DEPTH_VERT_COUNT = (GRID_X + 1) * (GRID_Y + 1)

function buildSyntheticDepthGrid(meta: PuppetLayer): Float32Array {
  const grid = new Float32Array(DEPTH_VERT_COUNT)
  const base = clamp(meta.depth ?? 0.5, 0.05, 0.95)
  const rig = meta.rig
  let amp = 0.06
  if (rig === 'face' || rig === 'head') amp = 0.14
  else if (rig === 'hair_front') amp = 0.11
  else if (rig === 'torso') amp = 0.12
  else if (rig === 'arm_l' || rig === 'arm_r') amp = 0.08
  else if (rig === 'hair_back') amp = -0.08

  let ptr = 0
  for (let gy = 0; gy <= GRID_Y; gy++) {
    const v = gy / GRID_Y
    const dy = (v - 0.5) * 2
    for (let gx = 0; gx <= GRID_X; gx++) {
      const u = gx / GRID_X
      const dx = (u - 0.5) * 2
      const dome = Math.max(0, 1 - (dx * dx * 0.85 + dy * dy * 0.75))
      // Smaller depth value = closer to camera
      grid[ptr++] = clamp(base - dome * amp, 0, 1)
    }
  }
  return grid
}

function compileShader(gl: WebGL2RenderingContext, type: number, src: string): WebGLShader {
  const s = gl.createShader(type)!
  gl.shaderSource(s, src)
  gl.compileShader(s)
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
    const info = gl.getShaderInfoLog(s) || 'shader compile error'
    gl.deleteShader(s)
    throw new Error(info)
  }
  return s
}

function createProgram(gl: WebGL2RenderingContext): WebGLProgram {
  const vs = compileShader(gl, gl.VERTEX_SHADER, VERT_SRC)
  const fs = compileShader(gl, gl.FRAGMENT_SHADER, FRAG_SRC)
  const prog = gl.createProgram()!
  gl.attachShader(prog, vs)
  gl.attachShader(prog, fs)
  gl.linkProgram(prog)
  gl.deleteShader(vs)
  gl.deleteShader(fs)
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    const info = gl.getProgramInfoLog(prog) || 'program link error'
    gl.deleteProgram(prog)
    throw new Error(info)
  }
  return prog
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v))
}

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t
}

function smoothstep(t: number): number {
  const x = clamp(t, 0, 1)
  return x * x * (3 - 2 * x)
}

type EmotionGroup = 'neutral' | 'happy' | 'shy' | 'surprised' | 'angry' | 'sad'

interface ProceduralEmotionWeights {
  happy: number
  shy: number
  surprised: number
  angry: number
  sad: number
}

interface EmotionDynamics {
  group: EmotionGroup
  breathFreq: number
  breathAmp: number
  inhaleFrac: number
  exhalePower: number
  headX: number
  headY: number
  headTilt: number
  postureX: number
  postureY: number
  swayMul: number
  swayFreqMul: number
  impulseX: number
  impulseY: number
  impulseZ: number
  gasp: number
  microBlink: boolean
}

const EMOTION_PROFILES: Record<string, EmotionDynamics> = {
  neutral: {
    group: 'neutral',
    breathFreq: 1.0,
    breathAmp: 1.0,
    inhaleFrac: 0.40,
    exhalePower: 1.35,
    headX: 0,
    headY: 0,
    headTilt: 0,
    postureX: 0,
    postureY: 0,
    swayMul: 1.0,
    swayFreqMul: 1.0,
    impulseX: 0,
    impulseY: -0.025,
    impulseZ: 0,
    gasp: 0,
    microBlink: true,
  },
  happy: {
    group: 'happy',
    breathFreq: 1.15,
    breathAmp: 1.08,
    inhaleFrac: 0.37,
    exhalePower: 1.25,
    headX: 0.04,
    headY: -0.10,
    headTilt: 0.024,
    postureX: 0.002,
    postureY: -0.0035,
    swayMul: 1.15,
    swayFreqMul: 1.10,
    impulseX: 0.02,
    impulseY: -0.075,
    impulseZ: 0.14,
    gasp: 0.06,
    microBlink: true,
  },
  得意: {
    group: 'happy',
    breathFreq: 1.06,
    breathAmp: 1.05,
    inhaleFrac: 0.38,
    exhalePower: 1.30,
    headX: 0.10,
    headY: -0.18,
    headTilt: -0.032,
    postureX: 0.003,
    postureY: -0.0055,
    swayMul: 1.05,
    swayFreqMul: 0.96,
    impulseX: 0.04,
    impulseY: -0.085,
    impulseZ: -0.14,
    gasp: 0.04,
    microBlink: true,
  },
  shy: {
    group: 'shy',
    breathFreq: 1.26,
    breathAmp: 1.18,
    inhaleFrac: 0.36,
    exhalePower: 1.45,
    headX: -0.08,
    headY: 0.15,
    headTilt: 0.052,
    postureX: -0.003,
    postureY: 0.005,
    swayMul: 0.85,
    swayFreqMul: 0.88,
    impulseX: -0.035,
    impulseY: 0.070,
    impulseZ: 0.20,
    gasp: 0.10,
    microBlink: true,
  },
  亲密: {
    group: 'shy',
    breathFreq: 1.18,
    breathAmp: 1.16,
    inhaleFrac: 0.38,
    exhalePower: 1.50,
    headX: -0.04,
    headY: 0.09,
    headTilt: 0.044,
    postureX: -0.002,
    postureY: 0.003,
    swayMul: 0.92,
    swayFreqMul: 0.84,
    impulseX: -0.02,
    impulseY: 0.055,
    impulseZ: 0.17,
    gasp: 0.08,
    microBlink: true,
  },
  surprised: {
    group: 'surprised',
    breathFreq: 1.42,
    breathAmp: 1.24,
    inhaleFrac: 0.31,
    exhalePower: 1.20,
    headX: 0,
    headY: -0.22,
    headTilt: -0.022,
    postureX: 0,
    postureY: -0.007,
    swayMul: 0.86,
    swayFreqMul: 1.16,
    impulseX: 0,
    impulseY: -0.11,
    impulseZ: -0.16,
    gasp: 0.34,
    microBlink: false,
  },
  angry: {
    group: 'angry',
    breathFreq: 1.38,
    breathAmp: 1.42,
    inhaleFrac: 0.34,
    exhalePower: 1.12,
    headX: 0.03,
    headY: 0.18,
    headTilt: -0.022,
    postureX: 0,
    postureY: 0.0045,
    swayMul: 0.70,
    swayFreqMul: 1.14,
    impulseX: 0.035,
    impulseY: 0.090,
    impulseZ: -0.14,
    gasp: 0.14,
    microBlink: true,
  },
  发怒: {
    group: 'angry',
    breathFreq: 1.52,
    breathAmp: 1.54,
    inhaleFrac: 0.32,
    exhalePower: 1.08,
    headX: 0.05,
    headY: 0.24,
    headTilt: -0.030,
    postureX: 0,
    postureY: 0.0065,
    swayMul: 0.64,
    swayFreqMul: 1.22,
    impulseX: -0.045,
    impulseY: 0.115,
    impulseZ: -0.18,
    gasp: 0.18,
    microBlink: false,
  },
  sad: {
    group: 'sad',
    breathFreq: 0.80,
    breathAmp: 1.28,
    inhaleFrac: 0.35,
    exhalePower: 1.90,
    headX: -0.06,
    headY: 0.20,
    headTilt: -0.035,
    postureX: -0.002,
    postureY: 0.006,
    swayMul: 0.66,
    swayFreqMul: 0.72,
    impulseX: -0.02,
    impulseY: 0.070,
    impulseZ: -0.11,
    gasp: 0,
    microBlink: true,
  },
}

function resolveEmotionDynamics(emotion: string, emotionKey?: string): EmotionDynamics {
  return (
    EMOTION_PROFILES[emotion] ||
    (emotionKey ? EMOTION_PROFILES[emotionKey] : undefined) ||
    EMOTION_PROFILES.neutral
  )
}

/**
 * Asymmetric physiological breath cycle in [0..1]: crisp inhale, slower
 * relaxed exhale, plus a gentle multi-breath depth swell so idle motion never loops mechanically.
 */
function shapeBreathWave(
  phase: number,
  simTime: number,
  inhaleFrac: number,
  exhalePower: number,
): number {
  const twoPi = Math.PI * 2
  const cycle = ((phase / twoPi) % 1 + 1) % 1
  const inh = clamp(inhaleFrac, 0.24, 0.48)
  let base: number
  if (cycle < inh) {
    const u = cycle / inh
    base = 0.5 - 0.5 * Math.cos(Math.PI * Math.pow(u, 0.92))
  } else {
    const u = (cycle - inh) / (1 - inh)
    base = Math.pow(0.5 + 0.5 * Math.cos(Math.PI * u), exhalePower)
  }
  const slowDepth = 0.88 + 0.12 * Math.sin(simTime * 0.29)
  return clamp(base * slowDepth, 0, 1)
}

const EXPR_Z_SPLIT = 18.1
const EXPR_TRANSITION_SEC = 0.26
const REPLACED_BASE_RIGS = new Set([
  'eye_l',
  'eye_r',
  'iris_l',
  'iris_r',
  'brow_l',
  'brow_r',
  'mouth',
])

export type HitZone = 'crown' | 'cheek' | 'chest' | 'arm_l' | 'arm_r' | 'body'

interface SkeletalAnchors {
  waistCenter: [number, number]
  shoulderL: [number, number]
  shoulderR: [number, number]
  armSpanL: number
  armSpanR: number
}

interface LayerDrawContext {
  scaleX: number
  scaleY: number
  cbW: number
  cbH: number
  headPivotX: number
  headPivotY: number
  chestX: number
  chestY: number
  mouthX: number
  mouthY: number
  waistX: number
  waistY: number
  shoulderLX: number
  shoulderLY: number
  shoulderRX: number
  shoulderRY: number
  armSpanL: number
  armSpanR: number
  cosT: number
  sinT: number
  cosZ: number
  sinZ: number
  bodyCosZ: number
  bodySinZ: number
  bodyLeanX: number
  bodyLeanY: number
  headScale: number
  stageScale: number
  depthRelief: number
  limbMotion: number
  armInward: number
  armSpread: number
  armLift: number
  armReactL: number
  armReactR: number
  breathWave: number
  breathLagWave: number
  chestRecoil: number
  blushLevel: number
  cheekR: [number, number]
  cheekL: [number, number]
  cheekAxes: [number, number, number, number]
  bodySwayX: number
  headOffsetX: number
  headOffsetY: number
  blinkAmt: number
  blinkClose: number
  hasClosedBlink: boolean
  hasDiffExpr: boolean
  emo: ProceduralEmotionWeights
  faceMood: [number, number, number]
  eyeMood: [number, number, number, number]
  pupilScale: number
  anchors: PuppetManifest['anchors']
}

/**
 * Real-time WebGL2 2D triangular-mesh puppet renderer for decomposed
 * See-through character layers.
 */
export class Puppet2DRenderer {
  private readonly canvas: HTMLCanvasElement
  private readonly gl: WebGL2RenderingContext
  private readonly program: WebGLProgram
  private readonly posLoc: number
  private readonly uvLoc: number
  private readonly texLoc: WebGLUniformLocation
  private readonly alphaLoc: WebGLUniformLocation
  private readonly blushLoc: WebGLUniformLocation
  private readonly layerRectLoc: WebGLUniformLocation
  private readonly cheekRLoc: WebGLUniformLocation
  private readonly cheekLLoc: WebGLUniformLocation
  private readonly cheekAxesLoc: WebGLUniformLocation
  private readonly faceMoodLoc: WebGLUniformLocation
  private readonly eyeMoodLoc: WebGLUniformLocation
  private readonly eyeAnchorsLoc: WebGLUniformLocation
  private readonly mouthOpenLoc: WebGLUniformLocation
  private readonly vboPos: WebGLBuffer
  private readonly vboUv: WebGLBuffer
  private readonly ibo: WebGLBuffer
  private readonly indexCount: number
  private readonly posBuffer: Float32Array
  private readonly uvBuffer: Float32Array

  private manifest: PuppetManifest | null = null
  private layers: LayerRuntime[] = []
  private config: PuppetConfig
  private rafId = 0
  private lastTs = 0
  private simTime = 0
  private swayTime = 0

  private targetPointerX = 0
  private targetPointerY = 0
  private targetGazeX = 0
  private targetGazeY = 0
  private pointerActive = false
  private pointerInsideStage = false
  private pointerDown = false
  private lastPointerMoveSimTime = -10
  private lastStageX = 0
  private lastStageY = 0
  private lastStrokeSignX = 0
  private lastStrokeSignY = 0
  private strokeFlipCount = 0
  private strokeFlipWindow = 0
  private hoverPetHold = 0
  private petIntensity = 0
  private petGoalX = 0
  private petGoalY = 0
  private petGoalTilt = 0
  private petCurX = 0
  private petCurY = 0
  private petCurTilt = 0
  private petHairForceX = 0
  private petHairForceY = 0

  private gazeFocusTimer = 0
  private focusTargetX = 0
  private focusTargetY = 0
  private idleGlanceTimer = 2.4
  private idleGlanceX = 0
  private idleGlanceY = 0

  private chestRecoil = 0
  private chestRecoilVel = 0
  private breathHoldTimer = 0
  private breathHoldLevel = 0

  private blushLevel = 0
  private blushGoal = 0
  private blushHoldTimer = 0
  private petDuration = 0

  private dodgeTimer = 0
  private dodgeGoalX = 0
  private dodgeGoalY = 0
  private dodgeGoalTilt = 0
  private dodgeGoalAngleX = 0
  private dodgeCurX = 0
  private dodgeCurY = 0
  private dodgeCurTilt = 0
  private dodgeCurAngleX = 0

  private tempReactionEmotion = ''
  private tempReactionTimer = 0
  private chestTapStreakTimer = 0
  private chestTapCount = 0

  private curAngleX = 0
  private curAngleY = 0
  private curAngleZ = 0
  private curEyeX = 0
  private curEyeY = 0
  private curMouth = 0
  private curMouthWide = 0
  private curMouthRound = 0

  private breathPhase = 0
  private curBreathFreq = 1
  private curBreathAmpMul = 1
  private curInhaleFrac = 0.4
  private curExhalePower = 1.35
  private breathWave = 0.45
  private breathLagWave = 0.45
  private breathImpulse = 0

  private curEmoX = 0
  private curEmoY = 0
  private curEmoTilt = 0
  private curPostureX = 0
  private curPostureY = 0
  private curSwayMul = 1
  private curSwayFreqMul = 1

  private reactX = 0
  private reactY = 0
  private reactZ = 0
  private reactVelX = 0
  private reactVelY = 0
  private reactVelZ = 0

  private lastEmotionId: string | null = null
  private targetExprId = ''
  private exprWeights = new Map<string, number>([['', 1]])
  private emoWeights: ProceduralEmotionWeights = {
    happy: 0,
    shy: 0,
    surprised: 0,
    angry: 0,
    sad: 0,
  }

  private blinkPhase = 0
  private nextBlinkAt = 2.6
  private blinkValue = 0
  private pupilScale = 1.0
  private pupilVel = 0

  private activeAction: ActiveActionClip | null = null
  private actionPose: ActionPose = { ...ZERO_ACTION_POSE }
  private armReactL = 0
  private armReactVelL = 0
  private armReactR = 0
  private armReactVelR = 0
  private skel: SkeletalAnchors = {
    waistCenter: [0.5, 0.72],
    shoulderL: [0.64, 0.42],
    shoulderR: [0.36, 0.42],
    armSpanL: 0.25,
    armSpanR: 0.25,
  }
  private subjectSkels = new Map<string, SkeletalAnchors>()

  private destroyed = false
  private onReady?: (ready: boolean) => void

  constructor(
    canvas: HTMLCanvasElement,
    initialConfig: PuppetConfig,
    onReady?: (ready: boolean) => void,
  ) {
    this.canvas = canvas
    this.config = { ...initialConfig }
    this.onReady = onReady

    const gl = canvas.getContext('webgl2', {
      alpha: true,
      premultipliedAlpha: true,
      antialias: true,
    })
    if (!gl) {
      throw new Error('WebGL2 is not supported in this browser/webview')
    }
    this.gl = gl
    this.program = createProgram(gl)
    this.posLoc = gl.getAttribLocation(this.program, 'a_pos')
    this.uvLoc = gl.getAttribLocation(this.program, 'a_uv')
    this.texLoc = gl.getUniformLocation(this.program, 'u_tex')!
    this.alphaLoc = gl.getUniformLocation(this.program, 'u_alpha')!
    this.blushLoc = gl.getUniformLocation(this.program, 'u_blush')!
    this.layerRectLoc = gl.getUniformLocation(this.program, 'u_layerRect')!
    this.cheekRLoc = gl.getUniformLocation(this.program, 'u_cheekR')!
    this.cheekLLoc = gl.getUniformLocation(this.program, 'u_cheekL')!
    this.cheekAxesLoc = gl.getUniformLocation(this.program, 'u_cheekAxes')!
    this.faceMoodLoc = gl.getUniformLocation(this.program, 'u_faceMood')!
    this.eyeMoodLoc = gl.getUniformLocation(this.program, 'u_eyeMood')!
    this.eyeAnchorsLoc = gl.getUniformLocation(this.program, 'u_eyeAnchors')!
    this.mouthOpenLoc = gl.getUniformLocation(this.program, 'u_mouthOpen')!

    const vertCount = (GRID_X + 1) * (GRID_Y + 1)
    this.posBuffer = new Float32Array(vertCount * 2)
    this.uvBuffer = new Float32Array(vertCount * 2)

    let ptr = 0
    for (let gy = 0; gy <= GRID_Y; gy++) {
      const v = gy / GRID_Y
      for (let gx = 0; gx <= GRID_X; gx++) {
        const u = gx / GRID_X
        this.uvBuffer[ptr++] = u
        this.uvBuffer[ptr++] = v
      }
    }

    const indices: number[] = []
    for (let gy = 0; gy < GRID_Y; gy++) {
      for (let gx = 0; gx < GRID_X; gx++) {
        const i0 = gy * (GRID_X + 1) + gx
        const i1 = i0 + 1
        const i2 = i0 + (GRID_X + 1)
        const i3 = i2 + 1
        indices.push(i0, i2, i1, i1, i2, i3)
      }
    }
    this.indexCount = indices.length

    this.vboPos = gl.createBuffer()!
    this.vboUv = gl.createBuffer()!
    this.ibo = gl.createBuffer()!

    gl.bindBuffer(gl.ARRAY_BUFFER, this.vboUv)
    gl.bufferData(gl.ARRAY_BUFFER, this.uvBuffer, gl.STATIC_DRAW)

    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.ibo)
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint16Array(indices), gl.STATIC_DRAW)

    gl.enable(gl.BLEND)
    gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA)

    this.lastTs = performance.now()
    this.loop(this.lastTs)
  }

  setConfig(next: PuppetConfig): void {
    this.config = { ...next }
  }

  triggerAction(id: PuppetActionId, intensity = 1): void {
    if (!this.config.motionEnabled) return
    const duration = ACTION_DURATIONS[id] ?? 1.4
    this.activeAction = {
      id,
      elapsed: 0,
      duration,
      intensity: clamp(intensity, 0.2, 1.5),
    }
    if (id === 'startle') {
      this.breathImpulse = Math.max(this.breathImpulse, 0.36 * intensity)
      this.pupilVel -= 0.7 * intensity
    } else if (id === 'sigh') {
      this.breathImpulse = Math.max(this.breathImpulse, 0.22 * intensity)
    } else if (id === 'hug_lean' || id === 'shy_hide') {
      this.blushGoal = Math.max(this.blushGoal, id === 'shy_hide' ? 0.9 : 0.7)
      this.blushHoldTimer = Math.max(this.blushHoldTimer, duration * 0.95)
    }
  }

  private resolveSkeletalAnchors(
    anchors: PuppetAnchors,
    layers: PuppetLayer[],
  ): SkeletalAnchors {
    const a = anchors
    const armL = layers.find((l) => l.rig === 'arm_l')
    const armR = layers.find((l) => l.rig === 'arm_r')
    const torso = layers.find((l) => l.rig === 'torso' && l.tag.toLowerCase() !== 'back collar')
    const lower = layers.find((l) => l.rig === 'lower')

    const shoulderL: [number, number] = a.shoulderL
      ? [a.shoulderL[0], a.shoulderL[1]]
      : armL
        ? [armL.nx + armL.nw * 0.22, armL.ny + armL.nh * 0.14]
        : [clamp(a.chestCenter[0] + 0.14, 0.08, 0.92), clamp(a.headPivot[1] + 0.08, 0.08, 0.85)]

    const shoulderR: [number, number] = a.shoulderR
      ? [a.shoulderR[0], a.shoulderR[1]]
      : armR
        ? [armR.nx + armR.nw * 0.78, armR.ny + armR.nh * 0.14]
        : [clamp(a.chestCenter[0] - 0.14, 0.08, 0.92), clamp(a.headPivot[1] + 0.08, 0.08, 0.85)]

    const waistCenter: [number, number] = a.waistCenter
      ? [a.waistCenter[0], a.waistCenter[1]]
      : torso
        ? [torso.nx + torso.nw * 0.5, lower ? (torso.ny + torso.nh + lower.ny) * 0.5 : torso.ny + torso.nh]
        : [a.chestCenter[0], clamp(a.chestCenter[1] + 0.24, 0.45, 0.96)]

    const armSpanL = armL ? Math.max(0.12, Math.hypot(armL.nw * 0.85, armL.nh * 0.85)) : 0.24
    const armSpanR = armR ? Math.max(0.12, Math.hypot(armR.nw * 0.85, armR.nh * 0.85)) : 0.24
    return { waistCenter, shoulderL, shoulderR, armSpanL, armSpanR }
  }

  getStageScale(): { scaleX: number; scaleY: number } {
    return this.getViewportScale()
  }

  updateLiveRig(
    anchorsPatch?: Partial<PuppetAnchors>,
    subjectsPatch?: PuppetSubject[],
    layerPatches?: RigLayerPatch[],
  ): void {
    const m = this.manifest
    if (!m) return
    if (anchorsPatch) {
      m.anchors = { ...m.anchors, ...anchorsPatch }
      this.skel = this.resolveSkeletalAnchors(m.anchors, m.layers)
    }
    if (subjectsPatch) {
      m.subjects = subjectsPatch
      this.rebuildSubjectSkels(m)
    }
    if (layerPatches?.length) {
      const byId = new Map(layerPatches.map((p) => [p.id, p]))
      for (const rt of this.layers) {
        const p = byId.get(rt.meta.id)
        if (!p) continue
        if (p.zIndex !== undefined) rt.meta.zIndex = p.zIndex
        if (p.parallaxX !== undefined) rt.meta.parallaxX = p.parallaxX
        if (p.parallaxY !== undefined) rt.meta.parallaxY = p.parallaxY
        if (p.physics !== undefined) rt.meta.physics = p.physics
        if (p.rig !== undefined) rt.meta.rig = p.rig
        if (p.subjectId !== undefined) rt.meta.subjectId = p.subjectId || undefined
      }
      this.layers.sort((a, b) => a.meta.zIndex - b.meta.zIndex)
    }
  }

  private rebuildSubjectSkels(manifest: PuppetManifest): void {
    this.subjectSkels.clear()
    for (const sub of manifest.subjects ?? []) {
      const subLayers = manifest.layers.filter((l) => l.subjectId === sub.id)
      this.subjectSkels.set(
        sub.id,
        this.resolveSkeletalAnchors(sub.anchors, subLayers.length ? subLayers : manifest.layers),
      )
    }
  }

  private getViewportScale(): { scaleX: number; scaleY: number } {
    const cb = this.manifest?.contentBounds
    const w = Math.max(1, this.canvas.width || this.canvas.clientWidth)
    const h = Math.max(1, this.canvas.height || this.canvas.clientHeight)
    const boxAspect = w / h
    const charAspect = cb ? Math.max(1, cb.width) / Math.max(1, cb.height) : 1
    let scaleX = 0.92
    let scaleY = 0.92
    if (charAspect > boxAspect) {
      scaleY = scaleX * (boxAspect / charAspect)
    } else {
      scaleX = scaleY * (charAspect / boxAspect)
    }
    return { scaleX, scaleY }
  }

  private stageToCharCoords(nx: number, ny: number): { cx: number; cy: number } {
    const { scaleX, scaleY } = this.getViewportScale()
    return {
      cx: 0.5 + nx / (2 * Math.max(0.1, scaleX)),
      cy: 0.5 + ny / (2 * Math.max(0.1, scaleY)),
    }
  }

  hitTest(nx: number, ny: number): HitZone | null {
    const m = this.manifest
    if (!m) return null
    const { cx, cy } = this.stageToCharCoords(nx, ny)
    if (cx < 0.06 || cx > 0.94 || cy < 0.02 || cy > 0.99) return null

    const a = m.anchors
    const eyeMidX = (a.eyeL[0] + a.eyeR[0]) * 0.5
    const eyeMidY = (a.eyeL[1] + a.eyeR[1]) * 0.5
    const eyeDist = Math.max(0.08, Math.hypot(a.eyeL[0] - a.eyeR[0], a.eyeL[1] - a.eyeR[1]))

    if (
      cy <= eyeMidY - eyeDist * 0.1 &&
      cy >= Math.max(0.02, a.faceCenter[1] - eyeDist * 1.45) &&
      Math.abs(cx - a.faceCenter[0]) <= eyeDist * 1.38
    ) {
      return 'crown'
    }
    if (cy <= a.headPivot[1] + 0.035 && Math.abs(cx - a.faceCenter[0]) <= eyeDist * 1.32) {
      return 'cheek'
    }
    // Check articulated arm layers before falling back to chest/body
    const outfits = m.outfits ?? []
    const activeOutfitId =
      (this.config.outfit && outfits.some((o) => o.id === this.config.outfit)
        ? this.config.outfit
        : outfits[0]?.id) || ''
    for (const rigName of ['arm_l', 'arm_r'] as const) {
      const arm = m.layers.find(
        (l) =>
          l.rig === rigName &&
          (!l.outfit || !activeOutfitId || l.outfit === activeOutfitId),
      )
      if (
        arm &&
        cx >= arm.nx - 0.01 &&
        cx <= arm.nx + arm.nw + 0.01 &&
        cy >= arm.ny + arm.nh * 0.18 &&
        cy <= arm.ny + arm.nh + 0.02 &&
        Math.abs(cx - a.chestCenter[0]) > 0.08
      ) {
        return rigName
      }
    }
    if (
      cy > a.headPivot[1] + 0.035 &&
      cy <= a.chestCenter[1] + 0.22 &&
      Math.abs(cx - a.chestCenter[0]) <= 0.24
    ) {
      return 'chest'
    }
    if (cx >= 0.14 && cx <= 0.86 && cy >= 0.04 && cy <= 0.98) {
      return 'body'
    }
    return null
  }

  setPointer(
    nx: number,
    ny: number,
    options?: { isInsideStage?: boolean },
  ): HitZone | null {
    const isInside = options?.isInsideStage ?? true
    this.pointerActive = true
    this.pointerInsideStage = isInside

    const m = this.manifest
    let relGazeX = clamp(nx, -1, 1)
    let relGazeY = clamp(ny, -1, 1)
    if (m) {
      const { scaleX, scaleY } = this.getViewportScale()
      const eyeMidX = (m.anchors.eyeL[0] + m.anchors.eyeR[0]) * 0.5
      const eyeMidY = (m.anchors.eyeL[1] + m.anchors.eyeR[1]) * 0.5
      const eyeNdcX = (eyeMidX - 0.5) * 2 * scaleX
      const eyeNdcY = (eyeMidY - 0.5) * 2 * scaleY
      relGazeX = (nx - eyeNdcX) / Math.max(0.35, scaleX)
      relGazeY = (ny - eyeNdcY) / Math.max(0.35, scaleY)
    }

    if (isInside) {
      this.targetPointerX = clamp(nx, -1, 1)
      this.targetPointerY = clamp(ny, -1, 1)
      this.targetGazeX = clamp(relGazeX, -1.15, 1.15)
      this.targetGazeY = clamp(relGazeY, -1.15, 1.15)
    } else {
      // Gentle attentive glance toward cursor elsewhere in the app (e.g. chat box)
      this.targetPointerX = clamp(nx * 0.42, -0.58, 0.58)
      this.targetPointerY = clamp(ny * 0.32, -0.42, 0.42)
      this.targetGazeX = clamp(relGazeX * 0.55, -0.78, 0.78)
      this.targetGazeY = clamp(relGazeY * 0.42, -0.58, 0.58)
    }

    const dx = nx - this.lastStageX
    const dy = ny - this.lastStageY
    this.lastStageX = nx
    this.lastStageY = ny

    const moveDist = Math.hypot(dx, dy)
    if (moveDist > 0.008) {
      this.lastPointerMoveSimTime = this.simTime
    }

    const zone = isInside || this.pointerDown ? this.hitTest(nx, ny) : null

    // Petting / stroking detection on head or cheek
    if (this.config.motionEnabled && (zone === 'crown' || zone === 'cheek' || this.pointerDown)) {
      const canHoverRub = zone === 'crown' || zone === 'cheek'
      if (!this.pointerDown && canHoverRub && moveDist > 0.005) {
        const sx = Math.abs(dx) > 0.006 ? Math.sign(dx) : 0
        const sy = Math.abs(dy) > 0.006 ? Math.sign(dy) : 0
        let flipped = false
        if (sx !== 0 && this.lastStrokeSignX !== 0 && sx !== this.lastStrokeSignX) flipped = true
        if (sy !== 0 && this.lastStrokeSignY !== 0 && sy !== this.lastStrokeSignY) flipped = true
        if (sx !== 0) this.lastStrokeSignX = sx
        if (sy !== 0) this.lastStrokeSignY = sy
        if (flipped) {
          this.strokeFlipCount += 1
          this.strokeFlipWindow = 0.75
          if (this.strokeFlipCount >= 2) {
            this.hoverPetHold = 0.65
          }
        }
      }

      if (this.pointerDown || this.hoverPetHold > 0) {
        const mRef = m?.anchors
        const refX = mRef ? mRef.faceCenter[0] : 0.5
        const { cx, cy } = this.stageToCharCoords(nx, ny)
        const offsetX = clamp(cx - refX, -0.25, 0.25)
        const sign = zone === 'crown' ? 1 : -1

        this.petGoalX = clamp(this.petGoalX + dx * 0.015, -0.006, 0.006)
        this.petGoalY = zone === 'crown' ? 0.0045 : clamp(0.002 + (cy - 0.25) * 0.01, -0.004, 0.005)
        this.petGoalTilt = clamp(offsetX * 0.14 * sign + dx * 0.35, -0.065, 0.065)
        this.petHairForceX = clamp(this.petHairForceX + dx * 0.42, -0.055, 0.055)
        this.petHairForceY = clamp(this.petHairForceY + dy * 0.32, -0.045, 0.045)
        if (this.pointerDown && canHoverRub) {
          this.hoverPetHold = Math.max(this.hoverPetHold, 0.25)
        }
      }
    }

    return zone
  }

  handlePointerDown(nx: number, ny: number): HitZone | null {
    this.pointerActive = true
    this.pointerInsideStage = true
    this.pointerDown = true
    this.lastStageX = nx
    this.lastStageY = ny
    this.lastPointerMoveSimTime = this.simTime

    const zone = this.hitTest(nx, ny)
    if (!this.config.motionEnabled || !zone) return zone

    const m = this.manifest
    if (m) {
      const { scaleX, scaleY } = this.getViewportScale()
      const eyeMidX = (m.anchors.eyeL[0] + m.anchors.eyeR[0]) * 0.5
      const eyeMidY = (m.anchors.eyeL[1] + m.anchors.eyeR[1]) * 0.5
      const eyeNdcX = (eyeMidX - 0.5) * 2 * scaleX
      const eyeNdcY = (eyeMidY - 0.5) * 2 * scaleY
      this.focusTargetX = clamp((nx - eyeNdcX) / Math.max(0.35, scaleX), -1.15, 1.15)
      this.focusTargetY = clamp((ny - eyeNdcY) / Math.max(0.35, scaleY), -1.15, 1.15)
      this.gazeFocusTimer = 0.65
    }

    const { cx, cy } = this.stageToCharCoords(nx, ny)
    const center = m ? m.anchors.faceCenter : [0.5, 0.25]
    const dx = cx - center[0]
    const dy = cy - center[1]
    const dir = dx >= 0 ? 1 : -1

    if (zone === 'crown') {
      // Gentle head-pat bounce, hair spring ruffle & warm soft blush
      this.reactVelY += 0.125
      this.reactVelZ -= dx * 0.85
      this.petHairForceY += 0.03
      this.petHairForceX += dx * 0.09
      this.hoverPetHold = Math.max(this.hoverPetHold, 0.35)
      this.blushGoal = Math.max(this.blushGoal, 0.62)
      this.blushHoldTimer = Math.max(this.blushHoldTimer, 1.6)
      if (this.config.blinkRate > 0.02 && this.blinkPhase === 0) {
        this.blinkPhase = 0.001
      }
    } else if (zone === 'cheek') {
      // Shy poke: deepen cheek blush + shy dodge/tilt + temporary shy expression
      this.reactVelX -= dir * 0.085
      this.reactVelY += clamp(dy * 0.25, -0.05, 0.08)
      this.reactVelZ += dir * 0.22
      this.petHairForceX -= dir * 0.03
      this.dodgeTimer = 1.05
      this.dodgeGoalX = -dir * 0.0085
      this.dodgeGoalY = 0.0045
      this.dodgeGoalTilt = dir * 0.058
      this.dodgeGoalAngleX = -dir * 0.24
      this.blushGoal = Math.max(this.blushGoal, 0.95)
      this.blushHoldTimer = Math.max(this.blushHoldTimer, 2.1)
      const curId = this.resolveActiveExpression(this.config.emotion)?.id
      const shyPref = curId === '害羞' ? ['亲密', '害羞'] : ['害羞', '亲密']
      const shyExpr = this.pickReactionExpr(shyPref, 'shy')
      if (shyExpr) {
        this.triggerTempExpression(shyExpr, 1.25)
      }
      if (this.config.blinkRate > 0.02 && this.blinkPhase === 0) {
        this.blinkPhase = 0.001
      }
    } else if (zone === 'chest') {
      // Breath pause + startle recoil + momentary surprised/angry expression feedback
      this.breathHoldTimer = 0.55
      this.breathHoldLevel = Math.max(this.breathWave, 0.85 * this.config.breathAmp)
      this.chestRecoilVel += 0.145
      this.reactVelY -= 0.13
      this.reactVelZ -= dir * 0.14
      this.petHairForceY -= 0.028
      this.dodgeTimer = 0.85
      this.dodgeGoalX = -dir * 0.004
      this.dodgeGoalY = -0.0075
      this.dodgeGoalTilt = -dir * 0.036
      this.dodgeGoalAngleX = 0
      this.chestTapCount = this.chestTapStreakTimer > 0 ? this.chestTapCount + 1 : 1
      this.chestTapStreakTimer = 2.6
      const useAngry = this.chestTapCount % 2 === 0 || this.config.emotion === 'surprised'
      const reactExpr = useAngry
        ? this.pickReactionExpr(['生气', '生气2', '发怒'], 'angry')
        : this.pickReactionExpr(['惊讶1', '惊讶2'], 'surprised')
      if (reactExpr) {
        this.triggerTempExpression(reactExpr, useAngry ? 1.4 : 1.3)
      }
    } else if (zone === 'arm_l' || zone === 'arm_r') {
      // Interactive hand/arm tap: elastic arm swing + friendly head glance
      if (zone === 'arm_l') {
        this.armReactVelL += 0.34
      } else {
        this.armReactVelR -= 0.34
      }
      this.reactVelX += dir * 0.04
      this.reactVelZ += dir * 0.12
      this.blushGoal = Math.max(this.blushGoal, 0.38)
      this.blushHoldTimer = Math.max(this.blushHoldTimer, 1.1)
    } else {
      // Subtle body reaction
      this.chestRecoilVel += 0.055
      this.reactVelX -= dir * 0.045
      this.reactVelZ += dir * 0.1
    }

    return zone
  }

  handlePointerUp(): void {
    this.pointerDown = false
    this.strokeFlipCount = 0
  }

  setPointerIdle(): void {
    this.pointerActive = false
    this.pointerInsideStage = false
    this.pointerDown = false
    this.hoverPetHold = 0
    this.targetPointerX = 0
    this.targetPointerY = 0
    this.targetGazeX = 0
    this.targetGazeY = 0
  }

  private makeRuntime(meta: PuppetLayer, isBlink = false, expressionId?: string): LayerRuntime {
    const gl = this.gl
    const tex = gl.createTexture()!
    gl.bindTexture(gl.TEXTURE_2D, tex)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
    gl.texImage2D(
      gl.TEXTURE_2D,
      0,
      gl.RGBA,
      1,
      1,
      0,
      gl.RGBA,
      gl.UNSIGNED_BYTE,
      new Uint8Array([0, 0, 0, 0]),
    )
    return {
      meta,
      tex,
      loaded: false,
      isBlink,
      expressionId,
      depthGrid: buildSyntheticDepthGrid(meta),
      springX: 0,
      springY: 0,
      velX: 0,
      velY: 0,
    }
  }

  private async loadDepthGrid(layer: LayerRuntime, manifest: PuppetManifest): Promise<void> {
    if (!layer.meta.depthRelPath) return
    try {
      const dUrl = await fileUrl(layer.meta.depthRelPath, 0)
      if (!dUrl || this.destroyed || this.manifest !== manifest) return
      const img = await new Promise<HTMLImageElement>((resolve, reject) => {
        const el = new Image()
        el.crossOrigin = 'anonymous'
        el.onload = () => resolve(el)
        el.onerror = () => reject(new Error('depth load failed'))
        el.src = dUrl
      })
      if (this.destroyed || this.manifest !== manifest) return
      const w = GRID_X + 1
      const h = GRID_Y + 1
      const c = document.createElement('canvas')
      c.width = w
      c.height = h
      const ctx2d = c.getContext('2d')
      if (!ctx2d) return
      ctx2d.drawImage(img, 0, 0, w, h)
      const data = ctx2d.getImageData(0, 0, w, h).data
      const grid = new Float32Array(w * h)
      for (let i = 0; i < w * h; i++) {
        grid[i] = data[i * 4] / 255
      }
      layer.depthGrid = grid
    } catch {
      /* keep synthetic depth grid cleanly */
    }
  }

  private async loadRuntimeTexture(layer: LayerRuntime, manifest: PuppetManifest): Promise<void> {
    try {
      const url = await fileUrl(layer.meta.relPath, 0)
      if (!url || this.destroyed || this.manifest !== manifest) return
      const img = await new Promise<HTMLImageElement>((resolve, reject) => {
        const el = new Image()
        el.crossOrigin = 'anonymous'
        el.onload = () => resolve(el)
        el.onerror = () => reject(new Error(`failed to load ${layer.meta.relPath}`))
        el.src = url
      })
      if (this.destroyed || this.manifest !== manifest) return
      const gl = this.gl
      gl.bindTexture(gl.TEXTURE_2D, layer.tex)
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, 0)
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img)
      layer.loaded = true
      if (layer.meta.depthRelPath) {
        void this.loadDepthGrid(layer, manifest)
      }
    } catch {
      /* skip broken layer cleanly */
    }
  }

  async loadManifest(manifest: PuppetManifest): Promise<void> {
    this.clearLayers()
    this.manifest = manifest
    this.skel = this.resolveSkeletalAnchors(manifest.anchors, manifest.layers)
    this.rebuildSubjectSkels(manifest)
    const runtimes: LayerRuntime[] = []

    for (const meta of manifest.layers) {
      runtimes.push(this.makeRuntime(meta, false, undefined))
    }
    for (const meta of manifest.blinkLayers ?? []) {
      runtimes.push(this.makeRuntime(meta, true, undefined))
    }
    for (const expr of manifest.expressions ?? []) {
      for (const meta of expr.layers ?? []) {
        runtimes.push(this.makeRuntime(meta, false, expr.id))
      }
    }
    runtimes.sort((a, b) => a.meta.zIndex - b.meta.zIndex)
    this.layers = runtimes

    // Load base & blink textures first so puppet paints promptly, then load expression diffs in parallel
    const core = runtimes.filter((l) => !l.expressionId)
    const exprOverlays = runtimes.filter((l) => Boolean(l.expressionId))

    await Promise.all(core.map((l) => this.loadRuntimeTexture(l, manifest)))
    if (!this.destroyed && this.manifest === manifest) {
      this.onReady?.(core.some((l) => l.loaded))
    }
    if (exprOverlays.length && !this.destroyed && this.manifest === manifest) {
      await Promise.all(exprOverlays.map((l) => this.loadRuntimeTexture(l, manifest)))
    }
  }

  private clearLayers(): void {
    for (const l of this.layers) {
      this.gl.deleteTexture(l.tex)
    }
    this.layers = []
    this.lastEmotionId = null
    this.lastConfigEmotion = null
    this.tempReactionEmotion = ''
    this.tempReactionTimer = 0
    this.chestTapStreakTimer = 0
    this.chestTapCount = 0
    this.targetExprId = ''
    this.exprWeights.clear()
    this.exprWeights.set('', 1)
    this.emoWeights = { happy: 0, shy: 0, surprised: 0, angry: 0, sad: 0 }
    this.pupilScale = 1.0
    this.pupilVel = 0
    this.chestRecoil = 0
    this.chestRecoilVel = 0
    this.breathHoldTimer = 0
    this.breathHoldLevel = 0
    this.blushLevel = 0
    this.blushGoal = 0
    this.blushHoldTimer = 0
    this.petDuration = 0
    this.dodgeTimer = 0
    this.dodgeCurX = 0
    this.dodgeCurY = 0
    this.dodgeCurTilt = 0
    this.dodgeCurAngleX = 0
    this.petIntensity = 0
    this.petHairForceX = 0
    this.petHairForceY = 0
    this.gazeFocusTimer = 0
    this.activeAction = null
    this.actionPose = { ...ZERO_ACTION_POSE }
    this.armReactL = 0
    this.armReactVelL = 0
    this.armReactR = 0
    this.armReactVelR = 0
    this.setPointerIdle()
    this.onReady?.(false)
  }

  private lastConfigEmotion: string | null = null

  private resolveActiveExpression(emotionId = this.config.emotion): PuppetExpression | null {
    const exprs = this.manifest?.expressions ?? []
    return (
      exprs.find((e) => e.id === emotionId) ||
      exprs.find((e) => e.emotionKey && e.emotionKey === emotionId) ||
      null
    )
  }

  private pickReactionExpr(preferredIds: string[], emotionKey: string): string {
    const exprs = this.manifest?.expressions ?? []
    for (const id of preferredIds) {
      const hit = exprs.find((e) => e.id === id)
      if (hit) return hit.id
    }
    const byKey = exprs.find((e) => e.emotionKey === emotionKey)
    return byKey ? byKey.id : emotionKey
  }

  private triggerTempExpression(emotionId: string, durationSec: number): void {
    this.tempReactionEmotion = emotionId
    this.tempReactionTimer = Math.max(this.tempReactionTimer, durationSec)
  }

  private resolveTargetExprId(activeExpr: PuppetExpression | null): string {
    if (!activeExpr || activeExpr.isBase) return ''
    const hasLoaded = this.layers.some((l) => l.expressionId === activeExpr.id && l.loaded)
    return hasLoaded ? activeExpr.id : ''
  }

  private triggerEmotionReaction(dyn: EmotionDynamics, prevEmotion: string): void {
    const cfg = this.config
    if (!cfg.motionEnabled) return

    this.reactVelX += dyn.impulseX
    this.reactVelY += dyn.impulseY
    this.reactVelZ += dyn.impulseZ
    if (dyn.gasp > 0) {
      this.breathImpulse = Math.max(this.breathImpulse, dyn.gasp)
    }
    if (dyn.group === 'sad') {
      // Nudge breath cycle slightly toward the sigh exhale peak
      const twoPi = Math.PI * 2
      const cycle = ((this.breathPhase / twoPi) % 1 + 1) % 1
      if (cycle < dyn.inhaleFrac) {
        this.breathPhase += (dyn.inhaleFrac - cycle) * twoPi * 0.65
      }
    }
    if (dyn.group === 'surprised') {
      // Startle opens eyes cleanly & snaps pupil inward instead of blinking shut
      this.blinkPhase = 0
      this.blinkValue = -0.06
      this.pupilVel -= 0.72
    } else if (
      dyn.microBlink &&
      cfg.blinkRate > 0.02 &&
      this.blinkPhase === 0 &&
      prevEmotion !== cfg.emotion
    ) {
      this.blinkPhase = 0.001
    }
  }

  private loop = (ts: number): void => {
    if (this.destroyed) return
    const dt = clamp((ts - this.lastTs) / 1000, 0.001, 0.05)
    this.lastTs = ts
    this.stepSimulation(dt)
    this.renderFrame()
    this.rafId = window.requestAnimationFrame(this.loop)
  }

  private stepSimulation(dt: number): void {
    const cfg = this.config
    if (this.lastConfigEmotion === null) {
      this.lastConfigEmotion = cfg.emotion
    } else if (this.lastConfigEmotion !== cfg.emotion) {
      this.lastConfigEmotion = cfg.emotion
      this.tempReactionTimer = 0
      this.tempReactionEmotion = ''
    }
    if (this.tempReactionTimer > 0) {
      this.tempReactionTimer = Math.max(0, this.tempReactionTimer - dt)
      if (this.tempReactionTimer === 0) {
        this.tempReactionEmotion = ''
      }
    }
    if (this.chestTapStreakTimer > 0) {
      this.chestTapStreakTimer = Math.max(0, this.chestTapStreakTimer - dt)
      if (this.chestTapStreakTimer === 0) {
        this.chestTapCount = 0
      }
    }

    const effectiveEmotion =
      this.tempReactionTimer > 0 && this.tempReactionEmotion
        ? this.tempReactionEmotion
        : cfg.emotion
    const activeExpr = this.resolveActiveExpression(effectiveEmotion)
    const resolvedTargetId = this.resolveTargetExprId(activeExpr)
    const dyn = resolveEmotionDynamics(effectiveEmotion, activeExpr?.emotionKey)

    if (this.lastEmotionId === null) {
      this.lastEmotionId = effectiveEmotion
    } else if (this.lastEmotionId !== effectiveEmotion) {
      const prev = this.lastEmotionId
      this.lastEmotionId = effectiveEmotion
      this.triggerEmotionReaction(dyn, prev)
    }

    // Advance per-expression crossfade weights even when ambient motion is disabled
    this.targetExprId = resolvedTargetId
    if (!this.exprWeights.has(resolvedTargetId)) {
      this.exprWeights.set(resolvedTargetId, 0)
    }
    const fadeStep = dt / EXPR_TRANSITION_SEC
    for (const [id, w] of Array.from(this.exprWeights.entries())) {
      const nextW = id === resolvedTargetId ? Math.min(1, w + fadeStep) : Math.max(0, w - fadeStep)
      if (nextW <= 0.005 && id !== resolvedTargetId) {
        this.exprWeights.delete(id)
      } else {
        this.exprWeights.set(id, nextW)
      }
    }

    // Smoothly blend procedural emotion weights for single-image / fallback expressions
    const emoBlendRate = 1 - Math.exp(-dt * 10.0)
    const keys: (keyof ProceduralEmotionWeights)[] = ['happy', 'shy', 'surprised', 'angry', 'sad']
    for (const k of keys) {
      this.emoWeights[k] = lerp(this.emoWeights[k], dyn.group === k ? 1 : 0, emoBlendRate)
    }

    if (!cfg.motionEnabled) {
      this.curAngleX = lerp(this.curAngleX, 0, 0.14)
      this.curAngleY = lerp(this.curAngleY, 0, 0.14)
      this.curAngleZ = lerp(this.curAngleZ, 0, 0.14)
      this.curEyeX = lerp(this.curEyeX, 0, 0.14)
      this.curEyeY = lerp(this.curEyeY, 0, 0.14)
      this.curPostureX = lerp(this.curPostureX, 0, 0.14)
      this.curPostureY = lerp(this.curPostureY, 0, 0.14)
      this.reactX = lerp(this.reactX, 0, 0.2)
      this.reactY = lerp(this.reactY, 0, 0.2)
      this.reactZ = lerp(this.reactZ, 0, 0.2)
      this.reactVelX = 0
      this.reactVelY = 0
      this.reactVelZ = 0
      this.chestRecoil = lerp(this.chestRecoil, 0, 0.2)
      this.chestRecoilVel = 0
      this.breathHoldTimer = 0
      this.blushLevel = lerp(this.blushLevel, 0, 0.16)
      this.blushHoldTimer = 0
      this.dodgeTimer = 0
      this.dodgeCurX = lerp(this.dodgeCurX, 0, 0.16)
      this.dodgeCurY = lerp(this.dodgeCurY, 0, 0.16)
      this.dodgeCurTilt = lerp(this.dodgeCurTilt, 0, 0.16)
      this.dodgeCurAngleX = lerp(this.dodgeCurAngleX, 0, 0.16)
      this.petDuration = 0
      this.petIntensity = lerp(this.petIntensity, 0, 0.2)
      this.petCurX = lerp(this.petCurX, 0, 0.2)
      this.petCurY = lerp(this.petCurY, 0, 0.2)
      this.petCurTilt = lerp(this.petCurTilt, 0, 0.2)
      this.petHairForceX = 0
      this.petHairForceY = 0
      this.hoverPetHold = 0
      this.gazeFocusTimer = 0
      this.breathWave = lerp(this.breathWave, 0, 0.18)
      this.breathLagWave = lerp(this.breathLagWave, 0, 0.14)
      this.breathImpulse = 0
      this.blinkValue = 0
      this.curMouth = lerp(this.curMouth, cfg.testMouth || 0, 0.25)
      return
    }

    this.simTime += dt

    // Interactive petting, hover-rub decay, and tap recoil spring
    if (this.strokeFlipWindow > 0) {
      this.strokeFlipWindow = Math.max(0, this.strokeFlipWindow - dt)
      if (this.strokeFlipWindow === 0) this.strokeFlipCount = 0
    }
    if (this.hoverPetHold > 0) {
      this.hoverPetHold = Math.max(0, this.hoverPetHold - dt)
    }
    if (this.gazeFocusTimer > 0) {
      this.gazeFocusTimer = Math.max(0, this.gazeFocusTimer - dt)
    }

    const isPetting = this.hoverPetHold > 0
    this.petIntensity = lerp(this.petIntensity, isPetting ? 1 : 0, 1 - Math.exp(-dt * (isPetting ? 8.5 : 3.2)))
    this.petGoalX *= Math.exp(-dt * 4.5)
    if (isPetting) {
      this.petDuration += dt
      const zone = this.hitTest(this.lastStageX, this.lastStageY)
      const blushTarget = zone === 'cheek' ? 0.95 : 0.72
      this.blushGoal = Math.max(this.blushGoal, blushTarget)
      this.blushHoldTimer = Math.max(this.blushHoldTimer, 1.45)
      if (zone === 'cheek') {
        const refX = this.manifest ? this.manifest.anchors.faceCenter[0] : 0.5
        const { cx } = this.stageToCharCoords(this.lastStageX, this.lastStageY)
        const side = cx >= refX ? 1 : -1
        this.dodgeTimer = Math.max(this.dodgeTimer, 0.48)
        this.dodgeGoalX = -side * 0.0065
        this.dodgeGoalY = 0.0038
        this.dodgeGoalTilt = side * 0.048
        this.dodgeGoalAngleX = -side * 0.18
      }
      if (this.petDuration > 0.35 && this.tempReactionTimer < 0.75) {
        const curId = this.resolveActiveExpression(this.config.emotion)?.id
        const pref = curId === '害羞' ? ['亲密', '害羞'] : ['害羞', '亲密']
        const exprId = this.pickReactionExpr(pref, 'shy')
        if (exprId) {
          this.triggerTempExpression(exprId, 0.95)
        }
      }
    } else {
      this.petGoalY = 0
      this.petGoalTilt = 0
      if (this.petIntensity < 0.05) {
        this.petDuration = 0
      }
    }
    const petSmooth = 1 - Math.exp(-dt * (isPetting ? 9 : 4.5))
    this.petCurX = lerp(this.petCurX, isPetting ? this.petGoalX : 0, petSmooth)
    this.petCurY = lerp(this.petCurY, isPetting ? this.petGoalY : 0, petSmooth)
    this.petCurTilt = lerp(this.petCurTilt, isPetting ? this.petGoalTilt : 0, petSmooth)
    this.petHairForceX *= Math.exp(-dt * 6.5)
    this.petHairForceY *= Math.exp(-dt * 6.5)

    // Blush glow hold & gradual fade (combine interactive click/pet blush + ambient shy/happy blush)
    if (this.blushHoldTimer > 0) {
      this.blushHoldTimer = Math.max(0, this.blushHoldTimer - dt)
    } else {
      this.blushGoal = 0
    }
    const hasDiffActive = this.targetExprId !== ''
    const blushPulse = 1 + 0.055 * Math.sin(this.simTime * 2.4) * this.emoWeights.shy
    const ambientBlush = !hasDiffActive
      ? (this.emoWeights.shy * 0.72 + this.emoWeights.happy * 0.22) * blushPulse
      : 0
    this.blushLevel = lerp(
      this.blushLevel,
      Math.max(this.blushGoal, ambientBlush),
      1 - Math.exp(-dt * (this.blushHoldTimer > 0 ? 7.5 : 3.2)),
    )

    // Sustained dodge & head-tilt pose hold
    if (this.dodgeTimer > 0) {
      this.dodgeTimer = Math.max(0, this.dodgeTimer - dt)
    } else {
      this.dodgeGoalX = 0
      this.dodgeGoalY = 0
      this.dodgeGoalTilt = 0
      this.dodgeGoalAngleX = 0
    }
    const dodgeSmooth = 1 - Math.exp(-dt * (this.dodgeTimer > 0 ? 11 : 4.2))
    this.dodgeCurX = lerp(this.dodgeCurX, this.dodgeGoalX, dodgeSmooth)
    this.dodgeCurY = lerp(this.dodgeCurY, this.dodgeGoalY, dodgeSmooth)
    this.dodgeCurTilt = lerp(this.dodgeCurTilt, this.dodgeGoalTilt, dodgeSmooth)
    this.dodgeCurAngleX = lerp(this.dodgeCurAngleX, this.dodgeGoalAngleX, dodgeSmooth)

    this.chestRecoilVel += (-this.chestRecoil * 48 - this.chestRecoilVel * 9.5) * dt
    this.chestRecoil = clamp(this.chestRecoil + this.chestRecoilVel * dt, -0.015, 0.015)

    // Smoothly adapt posture & breath rhythm targets to current emotion
    const emoSmooth = 1 - Math.exp(-dt * 4.8)
    this.curBreathFreq = lerp(this.curBreathFreq, dyn.breathFreq, emoSmooth)
    this.curBreathAmpMul = lerp(this.curBreathAmpMul, dyn.breathAmp, emoSmooth)
    this.curInhaleFrac = lerp(this.curInhaleFrac, dyn.inhaleFrac, emoSmooth)
    this.curExhalePower = lerp(this.curExhalePower, dyn.exhalePower, emoSmooth)
    this.curEmoX = lerp(this.curEmoX, dyn.headX, emoSmooth)
    this.curEmoY = lerp(this.curEmoY, dyn.headY, emoSmooth)
    this.curEmoTilt = lerp(this.curEmoTilt, dyn.headTilt, emoSmooth)
    this.curPostureX = lerp(this.curPostureX, dyn.postureX, emoSmooth)
    this.curPostureY = lerp(this.curPostureY, dyn.postureY, emoSmooth)
    this.curSwayMul = lerp(this.curSwayMul, dyn.swayMul, emoSmooth)
    this.curSwayFreqMul = lerp(this.curSwayFreqMul, dyn.swayFreqMul, emoSmooth)

    // Phase-integrated asymmetric breathing + startled breath hold / catch
    this.breathImpulse *= Math.exp(-dt * 4.0)
    let targetBreath: number
    if (this.breathHoldTimer > 0) {
      this.breathHoldTimer = Math.max(0, this.breathHoldTimer - dt)
      targetBreath = this.breathHoldLevel
    } else {
      this.breathPhase += dt * 1.55 * cfg.breathSpeed * this.curBreathFreq
      const rawBreath = shapeBreathWave(
        this.breathPhase,
        this.simTime,
        this.curInhaleFrac,
        this.curExhalePower,
      )
      targetBreath = clamp(rawBreath * this.curBreathAmpMul + this.breathImpulse, 0, 1.45) * cfg.breathAmp
    }
    this.breathWave = lerp(this.breathWave, targetBreath, 1 - Math.exp(-dt * (this.breathHoldTimer > 0 ? 22 : 12)))
    this.breathLagWave = lerp(this.breathLagWave, targetBreath, 1 - Math.exp(-dt * 4.8))

    // Spring-damped head reaction impulse on emotion change & taps
    const reactStiff = 36
    const reactDamp = 8.4
    this.reactVelX += (-this.reactX * reactStiff - this.reactVelX * reactDamp) * dt
    this.reactVelY += (-this.reactY * reactStiff - this.reactVelY * reactDamp) * dt
    this.reactVelZ += (-this.reactZ * reactStiff - this.reactVelZ * reactDamp) * dt
    this.reactX = clamp(this.reactX + this.reactVelX * dt, -0.02, 0.02)
    this.reactY = clamp(this.reactY + this.reactVelY * dt, -0.02, 0.02)
    this.reactZ = clamp(this.reactZ + this.reactVelZ * dt, -0.1, 0.1)

    // Idle eye micro-glance when pointer has left or sat still
    const pointerStale =
      !this.pointerActive || (!this.pointerDown && !isPetting && this.simTime - this.lastPointerMoveSimTime > 4.2)
    if (pointerStale || !cfg.eyeTrack) {
      this.idleGlanceTimer -= dt
      if (this.idleGlanceTimer <= 0) {
        this.idleGlanceTimer = 2.6 + Math.random() * 2.8
        this.idleGlanceX = (Math.random() - 0.5) * 0.42
        this.idleGlanceY = (Math.random() - 0.5) * 0.2
      }
    }

    // Advance active keyframe action clip + arm springs
    if (this.activeAction) {
      this.activeAction.elapsed += dt
      if (this.activeAction.elapsed >= this.activeAction.duration) {
        this.activeAction = null
        this.actionPose = { ...ZERO_ACTION_POSE }
      } else {
        this.actionPose = sampleActionClip(this.activeAction)
      }
    } else {
      this.actionPose = { ...ZERO_ACTION_POSE }
    }

    this.armReactVelL += (-this.armReactL * 42 - this.armReactVelL * 7.5) * dt
    this.armReactL = clamp(this.armReactL + this.armReactVelL * dt, -0.14, 0.14)
    this.armReactVelR += (-this.armReactR * 42 - this.armReactVelR * 7.5) * dt
    this.armReactR = clamp(this.armReactR + this.armReactVelR * dt, -0.14, 0.14)

    // Natural phase-continuous idle sway mixed with pointer tracking and emotional posture
    this.swayTime += dt * this.curSwayFreqMul
    const idleX = Math.sin(this.swayTime * 0.72) * 0.14 * cfg.swayAmp * this.curSwayMul
    const idleY = Math.cos(this.swayTime * 1.15) * 0.08 * cfg.swayAmp * this.curSwayMul
    const headTrackFade = pointerStale ? 0.35 : 1.0
    const focusPullX = this.gazeFocusTimer > 0 ? this.focusTargetX * 0.35 : 0
    const focusPullY = this.gazeFocusTimer > 0 ? this.focusTargetY * 0.25 : 0
    const act = this.actionPose
    const targetX = clamp(
      (this.targetPointerX * headTrackFade + focusPullX) * cfg.headParallax +
        idleX +
        this.curEmoX +
        this.dodgeCurAngleX +
        act.headX,
      -1.25,
      1.25,
    )
    const targetY = clamp(
      (this.targetPointerY * headTrackFade + focusPullY) * cfg.headParallax +
        idleY +
        this.curEmoY +
        act.headY,
      -1.25,
      1.25,
    )

    const headSmooth = 1 - Math.exp(-dt * (this.pointerInsideStage || this.activeAction ? 7.8 : 5.4))
    this.curAngleX = lerp(this.curAngleX, targetX, headSmooth)
    this.curAngleY = lerp(this.curAngleY, targetY, headSmooth)

    const breathTilt = (this.breathLagWave - 0.45 * cfg.breathAmp) * 0.0085
    const targetZ =
      this.curAngleX * 0.032 +
      Math.sin(this.swayTime * 0.65) * 0.013 * cfg.swayAmp * this.curSwayMul +
      this.curEmoTilt +
      this.reactZ +
      this.petCurTilt +
      this.dodgeCurTilt +
      breathTilt +
      act.headTilt
    this.curAngleZ = lerp(this.curAngleZ, targetZ, headSmooth)

    // Eyes lead the head: crisp eye-origin gaze focus or calm ambient glance
    let eyeTargetX: number
    let eyeTargetY: number
    if (!cfg.eyeTrack) {
      eyeTargetX = this.idleGlanceX * 0.7
      eyeTargetY = this.idleGlanceY * 0.7
    } else if (this.gazeFocusTimer > 0) {
      eyeTargetX = clamp(this.focusTargetX, -1.15, 1.15)
      eyeTargetY = clamp(this.focusTargetY, -1.15, 1.15)
    } else if (!pointerStale) {
      eyeTargetX = clamp(this.targetGazeX, -1.15, 1.15)
      eyeTargetY = clamp(this.targetGazeY, -1.15, 1.15)
    } else {
      eyeTargetX = lerp(this.targetGazeX * 0.25, this.idleGlanceX, 0.75)
      eyeTargetY = lerp(this.targetGazeY * 0.2, this.idleGlanceY, 0.75)
    }
    const eyeSmooth = 1 - Math.exp(-dt * (this.gazeFocusTimer > 0 || this.pointerInsideStage ? 18 : 10))
    this.curEyeX = lerp(this.curEyeX, eyeTargetX, eyeSmooth)
    this.curEyeY = lerp(this.curEyeY, eyeTargetY, eyeSmooth)

    // Spring-damped pupil dilation/contraction + emotion-driven blink rhythm
    const targetPupil = !hasDiffActive
      ? clamp(
          1 -
            this.emoWeights.surprised * 0.12 -
            this.emoWeights.angry * 0.06 +
            this.emoWeights.happy * 0.04 +
            this.emoWeights.shy * 0.03 +
            act.pupilBoost,
          0.76,
          1.12,
        )
      : clamp(1 + act.pupilBoost, 0.76, 1.12)
    this.pupilVel += ((targetPupil - this.pupilScale) * 95 - this.pupilVel * 11) * dt
    this.pupilScale = clamp(this.pupilScale + this.pupilVel * dt, 0.76, 1.12)

    if (cfg.blinkRate > 0.02) {
      const blinkFreqMul = clamp(
        1 + this.emoWeights.shy * 0.55 - this.emoWeights.angry * 0.35 - this.emoWeights.surprised * 0.45,
        0.45,
        1.7,
      )
      const blinkSpeedMul = clamp(
        1 + this.emoWeights.shy * 0.18 + this.emoWeights.angry * 0.22 - this.emoWeights.sad * 0.32,
        0.58,
        1.35,
      )
      this.nextBlinkAt -= dt * cfg.blinkRate * blinkFreqMul
      if (this.nextBlinkAt <= 0 && this.blinkPhase === 0) {
        this.blinkPhase = 0.001
      }
      if (this.blinkPhase > 0) {
        this.blinkPhase += dt * 5.2 * blinkSpeedMul
        if (this.blinkPhase >= 1) {
          this.blinkPhase = 0
          this.blinkValue = 0
          this.nextBlinkAt =
            this.emoWeights.shy > 0.5 && Math.random() < 0.28
              ? 0.16
              : (1.8 + Math.random() * 3.2) / blinkFreqMul
        } else {
          this.blinkValue = Math.sin(this.blinkPhase * Math.PI)
        }
      }
    } else {
      this.blinkValue = 0
      this.blinkPhase = 0
    }

    // Mouth open + vowel formant shape driven by live TTS/Call audio OR manual editor test slider
    const spec = getSpeechSpectrum()
    const audioLevel = clamp(spec.level * 3.4 * cfg.mouthSens, 0, 1)
    const targetMouth = Math.max(clamp(cfg.testMouth || 0, 0, 1), audioLevel)
    const mouthRate = 1 - Math.exp(-dt * 24)
    this.curMouth = lerp(this.curMouth, targetMouth, mouthRate)
    this.curMouthWide = lerp(this.curMouthWide, audioLevel > 0.04 ? spec.wide : 0, mouthRate)
    this.curMouthRound = lerp(this.curMouthRound, audioLevel > 0.04 ? spec.round : 0, mouthRate)

    // Spring-chain physics per layer (excited by sway, breeze, and head reaction impulses)
    const stiffness = 28 * Math.max(0.2, cfg.physicsStrength)
    const damping = clamp(3.5 + cfg.physicsDamping * 7.0, 2.0, 14.0)
    const breezeX = Math.sin(this.simTime * 1.9) * 0.004 * cfg.physicsStrength
    const breezeY = Math.cos(this.simTime * 1.4) * 0.002 * cfg.physicsStrength

    for (const l of this.layers) {
      const w = l.meta.physics
      if (w <= 0.001) {
        l.springX = 0
        l.springY = 0
        continue
      }
      const rig = l.meta.rig
      const isHairOrEar = rig === 'hair_front' || rig === 'hair_back' || rig === 'head'
      const hairX = isHairOrEar ? this.petHairForceX : 0
      const hairY = isHairOrEar ? this.petHairForceY : this.chestRecoilVel * 0.12
      const goalX =
        (-this.curAngleX * 0.018 - this.reactVelX * 0.14 - this.reactZ * 0.12 + breezeX + hairX) *
        w *
        cfg.physicsStrength
      const goalY =
        (this.curAngleY * 0.010 + this.reactVelY * 0.10 + breezeY + hairY) * w * cfg.physicsStrength
      const ax = (goalX - l.springX) * stiffness - l.velX * damping
      const ay = (goalY - l.springY) * stiffness - l.velY * damping
      l.velX += ax * dt
      l.velY += ay * dt
      l.springX = clamp(l.springX + l.velX * dt, -0.08, 0.08)
      l.springY = clamp(l.springY + l.velY * dt, -0.06, 0.06)
    }
  }

  private drawLayer(layer: LayerRuntime, opacityScale: number, ctx: LayerDrawContext): void {
    const gl = this.gl
    const rig = layer.meta.rig
    const isEyeOpenRig = rig === 'eye_l' || rig === 'eye_r' || rig === 'iris_l' || rig === 'iris_r'
    const isClosedBlinkRig = rig === 'blink_l' || rig === 'blink_r'

    let layerAlpha = opacityScale
    if (ctx.hasClosedBlink) {
      if (isEyeOpenRig) {
        layerAlpha *= clamp(1.0 - (ctx.blinkClose - 0.25) / 0.28, 0.0, 1.0)
      } else if (isClosedBlinkRig) {
        layerAlpha *= clamp((ctx.blinkClose - 0.35) / 0.25, 0.0, 1.0)
      }
    }
    if (layerAlpha <= 0.01) return

    const isHeadRig =
      rig === 'head' ||
      rig === 'face' ||
      isEyeOpenRig ||
      isClosedBlinkRig ||
      rig === 'brow_l' ||
      rig === 'brow_r' ||
      rig === 'mouth' ||
      rig === 'hair_front' ||
      rig === 'hair_back'

    const eyeCenter =
      rig === 'eye_r' || rig === 'iris_r' || rig === 'blink_r'
        ? ctx.anchors.eyeR
        : ctx.anchors.eyeL

    const isUpperBodyRig =
      rig === 'torso' || rig === 'neck' || rig === 'arm_l' || rig === 'arm_r' || isHeadRig

    let ptr = 0
    for (let gy = 0; gy <= GRID_Y; gy++) {
      const v = gy / GRID_Y
      for (let gx = 0; gx <= GRID_X; gx++) {
        const u = gx / GRID_X
        const vertIdx = gy * (GRID_X + 1) + gx
        const vertDepth = layer.depthGrid[vertIdx] ?? layer.meta.depth ?? 0.5
        let x = layer.meta.nx + u * layer.meta.nw
        let y = layer.meta.ny + v * layer.meta.nh

        // 1. Breathing expansion + tap recoil on torso / upper-body layers
        if (rig === 'torso' || rig === 'neck' || rig === 'arm_l' || rig === 'arm_r') {
          const dy = y - ctx.chestY
          const dx = x - ctx.chestX
          const falloff = Math.exp(-(dx * dx) / 0.09 - (dy * dy) / 0.06)
          y -= (ctx.breathWave * 0.0085 - ctx.chestRecoil) * falloff
          x += dx * ctx.breathWave * 0.016 * falloff
        }
        if (isUpperBodyRig) {
          const topWeight = clamp(1.0 - y * 0.85, 0, 1)
          x += ctx.bodySwayX * topWeight
          y -= ctx.breathWave * 0.004 * topWeight
        }

        // 1b. Articulated shoulder/arm rotation & lower-body waist-pinned drape sway
        const isUnsplitHandwear = layer.meta.tag.toLowerCase() === 'handwear'
        if (rig === 'arm_l' || rig === 'arm_r') {
          const isLeftArm = isUnsplitHandwear ? x >= ctx.chestX : rig === 'arm_l'
          const shX = isLeftArm ? ctx.shoulderLX : ctx.shoulderRX
          const shY = isLeftArm ? ctx.shoulderLY : ctx.shoulderRY
          const span = isLeftArm ? ctx.armSpanL : ctx.armSpanR
          const ax = x - shX
          const ay = y - shY
          const reach = clamp(Math.hypot(ax, ay) / Math.max(0.08, span), 0, 1.25)
          const bendW = smoothstep(reach)
          const sideSign = isLeftArm ? 1 : -1
          const breathArmAngle =
            sideSign * ((ctx.breathLagWave - 0.45) * 0.024 + Math.sin(this.swayTime * 0.92) * 0.016) * ctx.limbMotion
          const emoArmAngle =
            sideSign *
            (ctx.emo.shy * 0.05 - ctx.emo.happy * 0.032 - ctx.emo.surprised * 0.04 + ctx.emo.sad * 0.02) *
            ctx.limbMotion
          const actArmAngle =
            sideSign * (ctx.armInward - ctx.armSpread) +
            (isLeftArm ? ctx.armReactL : ctx.armReactR)
          const theta = (breathArmAngle + emoArmAngle + actArmAngle) * bendW
          const cA = Math.cos(theta)
          const sA = Math.sin(theta)
          x = shX + ax * cA - ay * sA
          y = shY + ax * sA + ay * cA + ctx.armLift * bendW
        } else if (rig === 'lower') {
          const belowWaist = clamp((y - ctx.waistY) / Math.max(0.15, 1.0 - ctx.waistY), 0, 1)
          const drapeW = Math.pow(belowWaist, 1.3)
          x -= ctx.bodySwayX * 0.35 * drapeW
        }

        // Keep top of neck smoothly welded under jaw when head posture/reaction shifts
        if (rig === 'neck') {
          const neckFollow = clamp(1.0 - v * 0.85, 0.1, 1.0)
          x += ctx.headOffsetX * neckFollow * 0.72
          y += ctx.headOffsetY * neckFollow * 0.82
        }

        // 2. Emotional head rotation + posture offset + perspective scale + 2.5D parallax
        if (isHeadRig) {
          const rx = x - ctx.headPivotX
          const ry = y - ctx.headPivotY
          const rotWeight = rig === 'hair_back' ? clamp(1.0 - v * 0.45, 0.35, 1.0) : 1.0
          const rCos = lerp(1, ctx.cosZ, rotWeight)
          const rSin = lerp(0, ctx.sinZ, rotWeight)
          const hScale = 1 + ctx.headScale * (rig === 'hair_back' ? 0.65 : 1.0)
          x = ctx.headPivotX + (rx * rCos - ry * rSin) * hScale
          y = ctx.headPivotY + (rx * rSin + ry * rCos) * hScale

          const postureWeight = rig === 'hair_back' ? clamp(1.0 - v * 0.35, 0.65, 1.0) : 1.0
          x += ctx.headOffsetX * postureWeight + this.curAngleX * layer.meta.parallaxX * 0.032
          y += ctx.headOffsetY * postureWeight + this.curAngleY * layer.meta.parallaxY * 0.025
        }

        // 3. Tilt-aligned eye blink, gaze & procedural expression shaping
        if (isEyeOpenRig) {
          const hScale = 1 + ctx.headScale
          const ex = ctx.headPivotX + (eyeCenter[0] - ctx.headPivotX) * hScale + ctx.headOffsetX
          const ey = ctx.headPivotY + (eyeCenter[1] - ctx.headPivotY) * hScale + ctx.headOffsetY
          const px = (x - ex) * ctx.cbW
          const py = (y - ey) * ctx.cbH
          let uLoc = px * ctx.cosT + py * ctx.sinT
          let vLoc = -px * ctx.sinT + py * ctx.cosT

          const eyeHalfW = Math.max(8, layer.meta.nw * ctx.cbW * 0.5)
          const eyeHalfH = Math.max(6, layer.meta.nh * ctx.cbH * 0.5)
          const uNorm = clamp(Math.abs(uLoc) / eyeHalfW, 0, 1)
          const isLeftSide = rig === 'eye_l' || rig === 'iris_l'
          const innerSign = isLeftSide ? clamp(-uLoc / eyeHalfW, -1, 1) : clamp(uLoc / eyeHalfW, -1, 1)

          if (!ctx.hasDiffExpr) {
            // Happy: warm curved smiling arch (lower rim rises gently into crescent)
            if (ctx.emo.happy > 0.005) {
              const arch = Math.cos(uNorm * Math.PI * 0.5)
              const rimBias = vLoc > 0 ? 1.3 : 0.9
              vLoc -= arch * 0.0038 * rimBias * ctx.cbH * ctx.emo.happy
            }
            // Spring-damped pupil contraction/dilation + subtle surprised tremor on iris
            if (rig === 'iris_l' || rig === 'iris_r') {
              uLoc *= ctx.pupilScale
              vLoc *= ctx.pupilScale
              if (ctx.emo.surprised > 0.005) {
                uLoc += Math.sin(this.simTime * 28) * 0.00038 * ctx.cbW * ctx.emo.surprised
              }
            }
            // Angry: fierce upper-lid press toward inner corner
            if (ctx.emo.angry > 0.005) {
              const topWeight = clamp((1 - vLoc / eyeHalfH) * 0.5, 0, 1)
              vLoc += topWeight * (0.0022 + Math.max(0, innerSign) * 0.0028) * ctx.cbH * ctx.emo.angry
            }
            // Sad: outer corner droop + subtle watery iris tremor
            if (ctx.emo.sad > 0.005) {
              vLoc += Math.max(0, -innerSign) * 0.0034 * ctx.cbH * ctx.emo.sad
              if (rig === 'iris_l' || rig === 'iris_r') {
                uLoc += Math.sin(this.simTime * 18) * 0.00055 * ctx.cbW * ctx.emo.sad
              }
            }
            // Shy: gentle upper lid lower
            if (ctx.emo.shy > 0.005) {
              const topWeight = clamp(-vLoc / eyeHalfH, 0, 1)
              vLoc += topWeight * 0.0018 * ctx.cbH * ctx.emo.shy
            }
          }

          const vPivot = ctx.cbH * 0.0105
          const squishRate = ctx.hasClosedBlink ? 0.38 : 0.86
          vLoc = vPivot + (vLoc - vPivot) * (1 - clamp(ctx.blinkAmt, -0.14, 0.94) * squishRate)

          x = ex + (uLoc * ctx.cosT - vLoc * ctx.sinT) / ctx.cbW
          y = ey + (uLoc * ctx.sinT + vLoc * ctx.cosT) / ctx.cbH

          if (rig === 'iris_l' || rig === 'iris_r') {
            const shyGlanceX = !ctx.hasDiffExpr ? ctx.emo.shy * 0.0022 : 0
            const shyGlanceY = !ctx.hasDiffExpr ? ctx.emo.shy * 0.0016 : 0
            x += this.curEyeX * 0.0065 + shyGlanceX
            y += this.curEyeY * 0.0045 + shyGlanceY
          }
        } else if (isClosedBlinkRig) {
          const settle = (1.0 - layerAlpha) * -0.002 + ctx.blinkAmt * 0.0012
          x += (-ctx.sinT * settle * ctx.cbH) / ctx.cbW
          y += ctx.cosT * settle
        }

        // 4. Tilt-aligned eyebrow emotion deformation (fallback when no diff expression overlay)
        if (!ctx.hasDiffExpr && (rig === 'brow_l' || rig === 'brow_r')) {
          const hScale = 1 + ctx.headScale
          const baseBx = layer.meta.nx + layer.meta.nw * 0.5
          const baseBy = layer.meta.ny + layer.meta.nh * 0.5
          const bx = ctx.headPivotX + (baseBx - ctx.headPivotX) * hScale + ctx.headOffsetX
          const by = ctx.headPivotY + (baseBy - ctx.headPivotY) * hScale + ctx.headOffsetY
          const bpx = (x - bx) * ctx.cbW
          const bpy = (y - by) * ctx.cbH
          let uLoc = bpx * ctx.cosT + bpy * ctx.sinT
          let vLoc = -bpx * ctx.sinT + bpy * ctx.cosT

          const innerSign = rig === 'brow_l' ? 1 - 2 * u : 2 * u - 1
          const arch = Math.sin(u * Math.PI)
          vLoc -= (0.0038 + 0.0032 * arch) * ctx.cbH * ctx.emo.happy
          vLoc -= (0.0008 + innerSign * 0.0042) * ctx.cbH * ctx.emo.shy
          vLoc -= (0.0075 + 0.0045 * arch) * ctx.cbH * ctx.emo.surprised
          vLoc += (0.0035 + innerSign * 0.0075) * ctx.cbH * ctx.emo.angry
          uLoc += (rig === 'brow_l' ? -1 : 1) * 0.0028 * ctx.cbW * ctx.emo.angry
          vLoc += (0.0022 - innerSign * 0.0068) * ctx.cbH * ctx.emo.sad

          x = bx + (uLoc * ctx.cosT - vLoc * ctx.sinT) / ctx.cbW
          y = by + (uLoc * ctx.sinT + vLoc * ctx.cosT) / ctx.cbH
        }

        // 5. Tilt-aligned mouth lip-sync + fallback emotion curve
        if (rig === 'mouth') {
          const hScale = 1 + ctx.headScale
          const mx = ctx.headPivotX + (ctx.mouthX - ctx.headPivotX) * hScale + ctx.headOffsetX
          const my = ctx.headPivotY + (ctx.mouthY - ctx.headPivotY) * hScale + ctx.headOffsetY
          const mpx = (x - mx) * ctx.cbW
          const mpy = (y - my) * ctx.cbH
          let uLoc = mpx * ctx.cosT + mpy * ctx.sinT
          let vLoc = -mpx * ctx.sinT + mpy * ctx.cosT
          const mouthHalfW = Math.max(8, layer.meta.nw * ctx.cbW * 0.5)
          const uNorm = clamp(Math.abs(uLoc) / mouthHalfW, 0, 1.2)
          const emoOpen = !ctx.hasDiffExpr ? ctx.emo.surprised * 0.45 + ctx.emo.happy * 0.08 : 0
          const open = Math.max(this.curMouth, emoOpen)

          if (vLoc > -0.002 * ctx.cbH) {
            const lowerFactor = clamp(
              (vLoc + 0.002 * ctx.cbH) / Math.max(8, layer.meta.nh * ctx.cbH * 0.7),
              0,
              1.4,
            )
            vLoc += open * 0.014 * ctx.cbH * lowerFactor
          } else {
            vLoc -= open * 0.0025 * ctx.cbH
          }
          // Formant vowel width modulation (I/E wider, O/U rounder)
          uLoc *= 1 + (this.curMouthWide * 0.14 - this.curMouthRound * 0.20) * clamp(this.curMouth, 0, 1)

          if (!ctx.hasDiffExpr) {
            if (ctx.emo.happy > 0.005) {
              uLoc *= 1 + 0.09 * ctx.emo.happy
              vLoc -= uNorm * uNorm * 0.24 * mouthHalfW * ctx.emo.happy
            }
            if (ctx.emo.shy > 0.005) {
              uLoc *= 1 - 0.12 * ctx.emo.shy
              vLoc -= uNorm * uNorm * 0.08 * mouthHalfW * ctx.emo.shy
            }
            if (ctx.emo.surprised > 0.005) {
              uLoc *= 1 - 0.18 * ctx.emo.surprised
            }
            if (ctx.emo.angry > 0.005) {
              vLoc += uNorm * uNorm * 0.16 * mouthHalfW * ctx.emo.angry
            }
            if (ctx.emo.sad > 0.005) {
              vLoc +=
                uNorm * uNorm * 0.24 * mouthHalfW * ctx.emo.sad +
                Math.sin(this.simTime * 16) * 0.00045 * ctx.cbH * ctx.emo.sad
            }
          }

          x = mx + (uLoc * ctx.cosT - vLoc * ctx.sinT) / ctx.cbW
          y = my + (uLoc * ctx.sinT + vLoc * ctx.cosT) / ctx.cbH
        }

        // 6. Spring-chain secondary physics (hair, ribbons, earrings)
        // Isolated Plan 2 guard: restrain outward-left upper front-hair swing over occluded eye zone
        if (layer.meta.physics > 0.001) {
          let sx = layer.springX
          let sy = layer.springY
          let pinFactor: number
          if (rig === 'hair_front') {
            pinFactor = Math.pow(v, 1.75)
            // Anchor upper-temple bangs while letting lower tips swing freely
            const templeSideMask = u < 0.52 && v < 0.72 ? clamp((u - 0.15) * 0.4 + v * 0.55, 0.2, 1.0) : 1.0
            sx = clamp(sx, -0.022, 0.045) * templeSideMask
          } else if (rig === 'hair_back') {
            pinFactor = Math.pow(v, 1.35)
          } else {
            pinFactor = Math.sin(v * Math.PI * 0.5)
          }
          x += sx * pinFactor
          y += sy * pinFactor
        }

        // 7. Per-vertex Marigold 2.5D depth relief + waist-pivoted upper-body lean + camera zoom
        if (ctx.depthRelief > 0.005) {
          const baseD = layer.meta.depth ?? 0.5
          // Smaller depth = closer to camera -> shifts with head/pointer angle
          const relD = clamp(baseD - vertDepth, -0.45, 0.45)
          const reliefAmp = isHeadRig ? 0.028 : 0.018
          x += this.curAngleX * relD * reliefAmp * ctx.depthRelief
          y += this.curAngleY * relD * reliefAmp * 0.78 * ctx.depthRelief
        }

        if (isUpperBodyRig) {
          const wx = x - ctx.waistX
          const wy = y - ctx.waistY
          // Rigid rotation around waistCenter preserves 1:1 facial & body proportions (no vertical squash)
          x = ctx.waistX + wx * ctx.bodyCosZ - wy * ctx.bodySinZ + ctx.bodyLeanX
          y = ctx.waistY + wx * ctx.bodySinZ + wy * ctx.bodyCosZ + ctx.bodyLeanY
        } else {
          y += ctx.bodyLeanY * 0.65
        }

        if (Math.abs(ctx.stageScale - 1) > 0.0005) {
          const zCx = ctx.chestX
          const zCy = lerp(ctx.chestY, ctx.headPivotY, 0.45)
          x = zCx + (x - zCx) * ctx.stageScale
          y = zCy + (y - zCy) * ctx.stageScale
        }

        const ndcX = (x - 0.5) * 2 * ctx.scaleX
        const ndcY = (0.5 - y) * 2 * ctx.scaleY
        this.posBuffer[ptr++] = ndcX
        this.posBuffer[ptr++] = ndcY
      }
    }

    gl.bindBuffer(gl.ARRAY_BUFFER, this.vboPos)
    gl.bufferData(gl.ARRAY_BUFFER, this.posBuffer, gl.DYNAMIC_DRAW)
    gl.enableVertexAttribArray(this.posLoc)
    gl.vertexAttribPointer(this.posLoc, 2, gl.FLOAT, false, 0, 0)

    gl.activeTexture(gl.TEXTURE0)
    gl.bindTexture(gl.TEXTURE_2D, layer.tex)
    gl.uniform1f(this.alphaLoc, layerAlpha)

    const isFace = rig === 'face'
    const isIris = rig === 'iris_l' || rig === 'iris_r'
    const isEyePart = rig === 'eye_l' || rig === 'eye_r' || isIris
    const applyBlush = isFace && ctx.blushLevel > 0.005
    const applyFaceMood = isFace && (ctx.faceMood[0] > 0.005 || ctx.faceMood[1] > 0.005)
    const sadIrisDim = isIris && !ctx.hasDiffExpr ? ctx.emo.sad : 0
    const applyEyeMood =
      isEyePart && (ctx.eyeMood[0] > 0.005 || ctx.eyeMood[1] > 0.005 || sadIrisDim > 0.005)
    const mouthOpenVal =
      rig === 'mouth'
        ? clamp(
            Math.max(this.curMouth, !ctx.hasDiffExpr ? ctx.emo.surprised * 0.45 + ctx.emo.happy * 0.08 : 0),
            0,
            1,
          )
        : 0

    gl.uniform1f(this.blushLoc, applyBlush ? ctx.blushLevel : 0)
    if (applyBlush || applyFaceMood) {
      gl.uniform4f(this.layerRectLoc, layer.meta.nx, layer.meta.ny, layer.meta.nw, layer.meta.nh)
      gl.uniform2f(this.cheekRLoc, ctx.cheekR[0], ctx.cheekR[1])
      gl.uniform2f(this.cheekLLoc, ctx.cheekL[0], ctx.cheekL[1])
      gl.uniform4f(
        this.cheekAxesLoc,
        ctx.cheekAxes[0],
        ctx.cheekAxes[1],
        ctx.cheekAxes[2],
        ctx.cheekAxes[3],
      )
      gl.uniform4f(
        this.eyeAnchorsLoc,
        ctx.anchors.eyeR[0],
        ctx.anchors.eyeR[1],
        ctx.anchors.eyeL[0],
        ctx.anchors.eyeL[1],
      )
    }
    gl.uniform3f(
      this.faceMoodLoc,
      isFace ? ctx.faceMood[0] : 0,
      isFace ? ctx.faceMood[1] : 0,
      isFace ? ctx.faceMood[2] : 0,
    )
    gl.uniform4f(
      this.eyeMoodLoc,
      applyEyeMood ? ctx.eyeMood[0] : 0,
      applyEyeMood ? ctx.eyeMood[1] : 0,
      ctx.eyeMood[2],
      sadIrisDim,
    )
    gl.uniform1f(this.mouthOpenLoc, mouthOpenVal)
    gl.drawElements(gl.TRIANGLES, this.indexCount, gl.UNSIGNED_SHORT, 0)
  }

  private renderFrame(): void {
    const gl = this.gl
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const cssW = Math.max(1, this.canvas.clientWidth)
    const cssH = Math.max(1, this.canvas.clientHeight)
    const targetW = Math.round(cssW * dpr)
    const targetH = Math.round(cssH * dpr)
    if (this.canvas.width !== targetW || this.canvas.height !== targetH) {
      this.canvas.width = targetW
      this.canvas.height = targetH
    }

    gl.viewport(0, 0, targetW, targetH)
    gl.clearColor(0, 0, 0, 0)
    gl.clear(gl.COLOR_BUFFER_BIT)

    const m = this.manifest
    if (!m || !this.layers.length) return

    const cb = m.contentBounds
    const { scaleX, scaleY } = this.getViewportScale()

    const hidden = new Set(this.config.hiddenLayers || [])
    const anchors = m.anchors
    const cfg = this.config
    const bodySwayX = cfg.motionEnabled
      ? Math.sin(this.swayTime * 0.68) * 0.008 * cfg.swayAmp * this.curSwayMul
      : 0

    gl.useProgram(this.program)
    gl.uniform1i(this.texLoc, 0)

    gl.bindBuffer(gl.ARRAY_BUFFER, this.vboUv)
    gl.enableVertexAttribArray(this.uvLoc)
    gl.vertexAttribPointer(this.uvLoc, 2, gl.FLOAT, false, 0, 0)
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.ibo)

    const cbW = Math.max(1, cb.width)
    const cbH = Math.max(1, cb.height)

    // Compute per-expression opacity weights for seamless crossfades without skin bleed
    const rawBaseW = clamp(this.exprWeights.get('') ?? 0, 0, 1)
    let baseFeatureAlpha: number
    if (this.targetExprId === '') {
      baseFeatureAlpha = clamp(rawBaseW * 1.55, 0, 1)
    } else {
      baseFeatureAlpha = 1 - smoothstep(clamp((1 - rawBaseW - 0.32) / 0.68, 0, 1))
    }

    const hasDiffExpr = Array.from(this.exprWeights.keys()).some(
      (id) => id !== '' && (this.exprWeights.get(id) ?? 0) > 0.01,
    )
    const emo = this.emoWeights
    const squint = !hasDiffExpr
      ? emo.happy * 0.2 + emo.shy * 0.16 + emo.angry * 0.08 - emo.surprised * 0.14
      : 0

    const breathLift = cfg.motionEnabled
      ? -(this.breathLagWave - 0.45 * cfg.breathAmp) * 0.0032
      : 0
    const act = cfg.motionEnabled ? this.actionPose : ZERO_ACTION_POSE
    const petSquint = cfg.motionEnabled ? this.petIntensity * 0.13 : 0
    const effectiveBlush = clamp(
      (cfg.motionEnabled
        ? this.blushLevel
        : !hasDiffExpr
          ? emo.shy * 0.68 + emo.happy * 0.2
          : 0) + act.blushBoost,
      0,
      1,
    )
    const gloomLevel = !hasDiffExpr ? clamp(emo.angry * 0.72 + emo.sad * 0.58, 0, 1) : 0
    const sadFlush = !hasDiffExpr ? emo.sad : 0
    const blushStripe = clamp((effectiveBlush - 0.35) / 0.5, 0, 1)
    const tearGlint = !hasDiffExpr ? clamp(emo.sad * 0.85 + emo.shy * 0.2, 0, 1) : 0
    const happySparkle = !hasDiffExpr ? clamp(emo.happy + act.squintBoost * 0.4, 0, 1) : 0

    const aspect = cbW / cbH
    const limbMotion = cfg.motionEnabled ? clamp(cfg.limbMotion ?? 1.0, 0, 2.0) : 0
    const depthRelief = cfg.motionEnabled ? clamp(cfg.depthRelief ?? 1.0, 0, 2.5) : 0
    const bodyTiltZ = cfg.motionEnabled
      ? act.bodyTilt + this.curAngleX * 0.012 * limbMotion + this.dodgeCurTilt * 0.32
      : 0

    const makeDrawCtx = (subAnchors: PuppetAnchors, subSkel: SkeletalAnchors): LayerDrawContext => {
      const eSpanX = (subAnchors.eyeL[0] - subAnchors.eyeR[0]) * cbW
      const eSpanY = (subAnchors.eyeL[1] - subAnchors.eyeR[1]) * cbH
      const hTilt =
        subAnchors.headTilt !== undefined
          ? subAnchors.headTilt
          : eSpanX * eSpanX + eSpanY * eSpanY > 1
            ? Math.atan2(eSpanY, eSpanX)
            : 0
      const eRx = subAnchors.eyeR[0] * aspect
      const eRy = subAnchors.eyeR[1]
      const eLx = subAnchors.eyeL[0] * aspect
      const eLy = subAnchors.eyeL[1]
      const dX = eLx - eRx
      const dY = eLy - eRy
      const dI = Math.max(0.05, Math.hypot(dX, dY))
      const uX = dX / dI
      const uY = dY / dI
      const vX = -uY
      const vY = uX
      return {
        scaleX,
        scaleY,
        cbW,
        cbH,
        headPivotX: subAnchors.headPivot[0],
        headPivotY: subAnchors.headPivot[1],
        chestX: subAnchors.chestCenter[0],
        chestY: subAnchors.chestCenter[1],
        mouthX: subAnchors.mouthCenter[0],
        mouthY: subAnchors.mouthCenter[1],
        waistX: subSkel.waistCenter[0],
        waistY: subSkel.waistCenter[1],
        shoulderLX: subSkel.shoulderL[0],
        shoulderLY: subSkel.shoulderL[1],
        shoulderRX: subSkel.shoulderR[0],
        shoulderRY: subSkel.shoulderR[1],
        armSpanL: subSkel.armSpanL,
        armSpanR: subSkel.armSpanR,
        cosT: Math.cos(hTilt),
        sinT: Math.sin(hTilt),
        cosZ: Math.cos(this.curAngleZ),
        sinZ: Math.sin(this.curAngleZ),
        bodyCosZ: Math.cos(bodyTiltZ),
        bodySinZ: Math.sin(bodyTiltZ),
        bodyLeanX: cfg.motionEnabled ? act.bodyX + this.dodgeCurX * 0.45 : 0,
        bodyLeanY: cfg.motionEnabled ? act.bodyY + this.dodgeCurY * 0.35 : 0,
        headScale: cfg.motionEnabled ? act.headScale : 0,
        stageScale: 1 + act.scale,
        depthRelief,
        limbMotion,
        armInward: act.armInward * limbMotion,
        armSpread: act.armSpread * limbMotion,
        armLift: act.armLift * limbMotion,
        armReactL: cfg.motionEnabled ? this.armReactL : 0,
        armReactR: cfg.motionEnabled ? this.armReactR : 0,
        breathWave: this.breathWave,
        breathLagWave: this.breathLagWave,
        chestRecoil: cfg.motionEnabled ? this.chestRecoil : 0,
        blushLevel: effectiveBlush,
        cheekR: [(eRx + uX * (0.04 * dI) + vX * (0.34 * dI)) / aspect, eRy + uY * (0.04 * dI) + vY * (0.34 * dI)],
        cheekL: [(eLx - uX * (0.04 * dI) + vX * (0.34 * dI)) / aspect, eLy - uY * (0.04 * dI) + vY * (0.34 * dI)],
        cheekAxes: [uX, uY, dI, aspect],
        bodySwayX,
        headOffsetX: cfg.motionEnabled ? this.curPostureX + this.reactX + this.petCurX + this.dodgeCurX + act.headDipX : 0,
        headOffsetY: cfg.motionEnabled ? this.curPostureY + this.reactY + this.petCurY + this.dodgeCurY + breathLift + act.headDipY : 0,
        blinkAmt: clamp(this.blinkValue + squint + petSquint + act.squintBoost + act.blinkForce, -0.14, 1.0),
        blinkClose: this.blinkPhase > 0 || act.blinkForce > 0.25 ? clamp(Math.max(this.blinkValue, act.blinkForce), 0, 1) : 0,
        hasClosedBlink: this.layers.some((l) => l.isBlink && l.loaded),
        hasDiffExpr,
        emo,
        faceMood: [gloomLevel, sadFlush, blushStripe],
        eyeMood: [tearGlint, happySparkle, this.simTime, 0],
        pupilScale: this.pupilScale,
        anchors: subAnchors,
      }
    }

    const ctx = makeDrawCtx(anchors, this.skel)
    const subjectCtxMap = new Map<string, LayerDrawContext>()
    for (const sub of m.subjects ?? []) {
      const subSkel = this.subjectSkels.get(sub.id) ?? this.skel
      subjectCtxMap.set(sub.id, makeDrawCtx(sub.anchors, subSkel))
    }

    const outfits = m.outfits ?? []
    const activeOutfitId =
      (cfg.outfit && outfits.some((o) => o.id === cfg.outfit) ? cfg.outfit : outfits[0]?.id) || ''

    const ctxForLayer = (layer: LayerRuntime): LayerDrawContext =>
      (layer.meta.subjectId && subjectCtxMap.get(layer.meta.subjectId)) || ctx

    const drawBaseLayer = (layer: LayerRuntime) => {
      if (!layer.isBlink && layer.meta.outfit && activeOutfitId && layer.meta.outfit !== activeOutfitId) {
        return
      }
      if (!layer.isBlink && hidden.has(layer.meta.id)) return
      let alpha = 1.0
      if (!layer.isBlink && REPLACED_BASE_RIGS.has(layer.meta.rig)) {
        if (baseFeatureAlpha <= 0.01) return
        alpha = baseFeatureAlpha
      }
      this.drawLayer(layer, alpha, ctxForLayer(layer))
    }

    // Pass 1: base & under-expression layers (0 .. <18.1)
    for (const layer of this.layers) {
      if (!layer.loaded || layer.expressionId !== undefined) continue
      if (layer.meta.zIndex >= EXPR_Z_SPLIT) break
      drawBaseLayer(layer)
    }

    // Pass 2: outgoing diff expressions (fade smoothly underneath incoming target)
    for (const [exprId, w] of this.exprWeights.entries()) {
      if (!exprId || exprId === this.targetExprId || w <= 0.01) continue
      const alpha = this.targetExprId === '' ? smoothstep(w) : 1 - Math.pow(1 - w, 2.0)
      if (alpha <= 0.01) continue
      for (const layer of this.layers) {
        if (layer.loaded && layer.expressionId === exprId) {
          this.drawLayer(layer, alpha, ctxForLayer(layer))
        }
      }
    }

    // Pass 3: incoming / active target diff expression on top
    if (this.targetExprId) {
      const targetAlpha = smoothstep(this.exprWeights.get(this.targetExprId) ?? 0)
      if (targetAlpha > 0.01) {
        for (const layer of this.layers) {
          if (layer.loaded && layer.expressionId === this.targetExprId) {
            this.drawLayer(layer, targetAlpha, ctxForLayer(layer))
          }
        }
      }
    }

    // Pass 4: blink overlays (18.6+) and front hair / high accessories (19+)
    for (const layer of this.layers) {
      if (!layer.loaded || layer.expressionId !== undefined) continue
      if (layer.meta.zIndex < EXPR_Z_SPLIT) continue
      drawBaseLayer(layer)
    }
  }

  destroy(): void {
    this.destroyed = true
    if (this.rafId) window.cancelAnimationFrame(this.rafId)
    this.clearLayers()
    const gl = this.gl
    gl.deleteBuffer(this.vboPos)
    gl.deleteBuffer(this.vboUv)
    gl.deleteBuffer(this.ibo)
    gl.deleteProgram(this.program)
  }
}

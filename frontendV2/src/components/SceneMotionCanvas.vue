<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    src: string
    pointerX?: number
    pointerY?: number
    hovering?: boolean
    dragging?: boolean
    intensity?: number
  }>(),
  {
    pointerX: 0,
    pointerY: 0,
    hovering: false,
    dragging: false,
    intensity: 1,
  },
)

const emit = defineEmits<{
  ready: [value: boolean]
}>()

const canvasRef = ref<HTMLCanvasElement | null>(null)

const GRID_X = 44
const GRID_Y = 58
const MASK_SIZE = 96
const SWAP_DURATION_MS = 520

const VERTEX_SHADER_SRC = `
attribute vec2 a_pos;
varying vec2 v_uv;

uniform float u_time;
uniform float u_intensity;
uniform vec2 u_anchor;
uniform vec2 u_pointer;
uniform vec2 u_canvasSize;
uniform vec2 u_currSize;

vec2 toImgUv(vec2 uv, vec2 boxSize, vec2 texSize) {
  float boxAspect = max(boxSize.x, 1.0) / max(boxSize.y, 1.0);
  float texAspect = max(texSize.x, 1.0) / max(texSize.y, 1.0);
  vec2 scale = texAspect > boxAspect
    ? vec2(1.0, texAspect / boxAspect)
    : vec2(boxAspect / texAspect, 1.0);
  return (uv - 0.5) * scale + 0.5;
}

void main() {
  v_uv = a_pos;

  vec2 imgUv = toImgUv(a_pos, u_canvasSize, u_currSize);
  float inBounds = step(0.0, imgUv.x) * step(imgUv.x, 1.0) * step(0.0, imgUv.y) * step(imgUv.y, 1.0);
  vec2 clampedUv = clamp(imgUv, 0.0, 1.0);

  // Strictly lock all four real borders of the uncropped image to zero displacement.
  float edgeX = sin(3.14159265 * clampedUv.x);
  float edgeY = sin(3.14159265 * clampedUv.y);
  float edgeLock = inBounds * pow(max(edgeX, 0.0), 0.72) * pow(max(edgeY, 0.0), 0.85);

  vec2 disp = vec2(0.0);

  // 1. Torso / chest breathing anchored below detected subject focus
  vec2 chestCenter = vec2(u_anchor.x, clamp(u_anchor.y + 0.24, 0.45, 0.74));
  vec2 dChest = vec2((clampedUv.x - chestCenter.x) / 0.28, (clampedUv.y - chestCenter.y) / 0.20);
  float chestMask = exp(-dot(dChest, dChest));
  float phase = u_time * 1.48;
  float breathWave = sin(phase) * 0.5 + sin(phase * 2.0 - 1.5707963) * 0.09;
  disp.y += chestMask * breathWave * 0.015;
  disp.x += (clampedUv.x - chestCenter.x) * chestMask * breathWave * 0.022;

  // 2. Gentle upper-body / head figure-eight idle sway
  float headMask = exp(
    -pow((clampedUv.x - u_anchor.x) / 0.36, 2.0)
    -pow((clampedUv.y - u_anchor.y) / 0.30, 2.0)
  );
  disp.x += sin(u_time * 0.72) * 0.0085 * headMask;
  disp.y += cos(u_time * 1.44) * 0.0038 * headMask;

  // 3. Soft hair & cloth breeze while shielding facial center rigidity
  float faceDist2 =
    pow((clampedUv.x - u_anchor.x) / 0.145, 2.0) +
    pow((clampedUv.y - u_anchor.y) / 0.135, 2.0);
  float faceProtect = 1.0 - 0.85 * exp(-faceDist2);
  float drapeMask = exp(-pow((clampedUv.x - u_anchor.x) / 0.45, 2.0))
    * smoothstep(0.08, 0.88, clampedUv.y)
    * faceProtect;
  float wave = sin(clampedUv.y * 8.2 - u_time * 1.85 + clampedUv.x * 3.1);
  disp.x += wave * 0.0056 * drapeMask;
  disp.y += cos(clampedUv.x * 6.0 + u_time * 1.25) * 0.0024 * drapeMask;

  // 4. Subtle macro mesh perspective lean synced with cursor parallax
  vec2 ambientPtr = vec2(sin(u_time * 0.42) * 0.18, cos(u_time * 0.33) * 0.11);
  vec2 ptr = u_pointer + ambientPtr;
  float leanMask = exp(
    -pow((clampedUv.x - u_anchor.x) / 0.46, 2.0)
    -pow((clampedUv.y - 0.48) / 0.48, 2.0)
  );
  disp.x -= ptr.x * 0.010 * leanMask;
  disp.y += ptr.y * 0.007 * leanMask;

  vec2 ndc = vec2(a_pos.x * 2.0 - 1.0, 1.0 - a_pos.y * 2.0);
  ndc += disp * edgeLock * u_intensity;
  gl_Position = vec4(ndc, 0.0, 1.0);
}
`

const FRAGMENT_SHADER_SRC = `
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif

varying vec2 v_uv;

uniform sampler2D u_texCurr;
uniform sampler2D u_texPrev;
uniform sampler2D u_maskCurr;
uniform sampler2D u_maskPrev;
uniform vec2 u_canvasSize;
uniform vec2 u_currSize;
uniform vec2 u_prevSize;
uniform float u_mix;
uniform float u_time;
uniform float u_intensity;
uniform vec2 u_pointer;

vec2 containUv(vec2 uv, vec2 boxSize, vec2 texSize, out float inBounds) {
  float boxAspect = max(boxSize.x, 1.0) / max(boxSize.y, 1.0);
  float texAspect = max(texSize.x, 1.0) / max(texSize.y, 1.0);
  vec2 scale = texAspect > boxAspect
    ? vec2(1.0, texAspect / boxAspect)
    : vec2(boxAspect / texAspect, 1.0);
  vec2 mapped = (uv - 0.5) * scale + 0.5;
  vec2 edgeDist = min(mapped, 1.0 - mapped);
  inBounds = smoothstep(0.0, 0.003, min(edgeDist.x, edgeDist.y));
  return clamp(mapped, 0.001, 0.999);
}

// Sample full uncropped image + depth-aware 2.5D parallax & foreground rim signal
vec4 sampleLayer(
  sampler2D tex,
  sampler2D mask,
  vec2 texSize,
  vec2 parallaxDir,
  out float outDepth,
  out float outRim,
  out float outBounds
) {
  float bounds = 0.0;
  vec2 baseUv = containUv(v_uv, u_canvasSize, texSize, bounds);
  float imgEdgeFade = sin(3.14159265 * baseUv.x) * sin(3.14159265 * baseUv.y);
  vec2 parallaxDelta = parallaxDir * imgEdgeFade;

  float d0 = texture2D(mask, baseUv).r;
  vec2 halfShift = parallaxDelta * (d0 - 0.14) * 0.5;
  vec2 midUv = clamp(baseUv + halfShift, 0.002, 0.998);
  float d1 = texture2D(mask, midUv).r;
  float depth = mix(d0, d1, 0.65);

  vec2 shiftedUv = clamp(baseUv + parallaxDelta * (depth - 0.14), 0.002, 0.998);
  vec4 col = texture2D(tex, shiftedUv);

  float eps = 0.016;
  float mx = texture2D(mask, clamp(shiftedUv + vec2(eps, 0.0), 0.002, 0.998)).r
           - texture2D(mask, clamp(shiftedUv - vec2(eps, 0.0), 0.002, 0.998)).r;
  float my = texture2D(mask, clamp(shiftedUv + vec2(0.0, eps), 0.002, 0.998)).r
           - texture2D(mask, clamp(shiftedUv - vec2(0.0, eps), 0.002, 0.998)).r;
  outDepth = depth;
  outRim = clamp(length(vec2(mx, my)) * 2.2, 0.0, 1.0);
  outBounds = bounds;
  return col;
}

float hash21(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}

// Analytical drifting bokeh / air dust motes layer
float bokehField(vec2 uv, float scale, float speed, float soft, float threshold) {
  vec2 p = uv * scale;
  p.y += u_time * speed;
  p.x += sin(u_time * speed * 1.4 + p.y * 0.7) * 0.25;
  vec2 id = floor(p);
  vec2 gv = fract(p) - 0.5;

  float acc = 0.0;
  for (int oy = -1; oy <= 1; oy++) {
    for (int ox = -1; ox <= 1; ox++) {
      vec2 offs = vec2(float(ox), float(oy));
      vec2 cell = id + offs;
      float rnd = hash21(cell);
      if (rnd > threshold) {
        float rnd2 = hash21(cell + 17.3);
        vec2 pos = offs + vec2(rnd - 0.5, rnd2 - 0.5) * 0.65;
        pos.x += sin(u_time * 0.8 + rnd * 6.2831) * 0.16;
        pos.y += cos(u_time * 0.6 + rnd2 * 6.2831) * 0.14;
        float dist = length(gv - pos);
        float radius = mix(0.06, 0.22, fract(rnd * 13.7));
        float twinkle = 0.55 + 0.45 * sin(u_time * (1.2 + rnd2) + rnd * 12.0);
        acc += smoothstep(radius + soft, radius * 0.25, dist) * twinkle;
      }
    }
  }
  return clamp(acc, 0.0, 1.0);
}

vec3 grade(vec3 rgb) {
  float luma = dot(rgb, vec3(0.2126, 0.7152, 0.0722));
  vec3 sat = mix(vec3(luma), rgb, 0.94);
  vec3 con = (sat - 0.5) * 1.03 + 0.5;
  return clamp(con, 0.0, 1.0);
}

void main() {
  vec2 ambientPtr = vec2(sin(u_time * 0.42) * 0.18, cos(u_time * 0.33) * 0.11);
  vec2 ptr = u_pointer + ambientPtr;

  vec2 parallaxDir = vec2(-ptr.x, -ptr.y) * 0.022 * u_intensity;

  float depthCurr = 0.0;
  float rimCurr = 0.0;
  float boundsCurr = 0.0;
  vec4 curr = sampleLayer(
    u_texCurr,
    u_maskCurr,
    u_currSize,
    parallaxDir,
    depthCurr,
    rimCurr,
    boundsCurr
  );

  float alphaCurr = curr.a * boundsCurr;
  float depth = depthCurr;
  float rim = rimCurr;
  vec3 baseRgb = curr.rgb;
  float outAlpha = alphaCurr;

  if (u_mix < 0.999) {
    float depthPrev = 0.0;
    float rimPrev = 0.0;
    float boundsPrev = 0.0;
    vec4 prev = sampleLayer(
      u_texPrev,
      u_maskPrev,
      u_prevSize,
      parallaxDir,
      depthPrev,
      rimPrev,
      boundsPrev
    );
    float alphaPrev = prev.a * boundsPrev;
    float t = smoothstep(0.0, 1.0, u_mix);
    float wPrev = (1.0 - t) * alphaPrev;
    float wCurr = t * alphaCurr;
    float wSum = max(wPrev + wCurr, 0.0001);
    baseRgb = (prev.rgb * wPrev + curr.rgb * wCurr) / wSum;
    outAlpha = wPrev + wCurr;
    depth = (depthPrev * wPrev + depthCurr * wCurr) / wSum;
    rim = (rimPrev * wPrev + rimCurr * wCurr) / wSum;
  }

  vec3 color = grade(baseRgb);

  // Subtle ambient directional sheen & rim highlight responding to tilt + breath
  float breathGlow = 0.5 + 0.5 * sin(u_time * 1.48);
  vec2 lightDir = normalize(vec2(0.45 - ptr.x * 0.65, -0.7 + ptr.y * 0.35));
  float sheenBand = smoothstep(-0.3, 0.9, dot(v_uv - 0.5, lightDir));
  vec3 warmAir = vec3(1.0, 0.93, 0.82);
  color += warmAir * sheenBand * (0.018 + 0.012 * breathGlow) * u_intensity;
  color += warmAir * rim * sheenBand * 0.085 * u_intensity;

  // Three-plane 2.5D atmospheric particles across the full uncropped image
  float aspect = max(u_canvasSize.x, 1.0) / max(u_canvasSize.y, 1.0);
  vec2 bokehUv = vec2(v_uv.x * aspect, v_uv.y);

  float backBokeh = bokehField(bokehUv + ptr * 0.025 * u_intensity, 3.6, 0.045, 0.16, 0.80);
  color += vec3(0.95, 0.88, 0.78) * backBokeh * (1.0 - depth * 0.78) * 0.11 * u_intensity;

  float frontMotes = bokehField(bokehUv + ptr * 0.11 * u_intensity + vec2(3.1, 1.7), 7.5, 0.095, 0.05, 0.84);
  color += vec3(1.0, 0.96, 0.88) * frontMotes * 0.16 * u_intensity;

  gl_FragColor = vec4(clamp(color, 0.0, 1.0), clamp(outAlpha, 0.0, 1.0));
}
`

let gl: WebGLRenderingContext | null = null
let program: WebGLProgram | null = null
let vbo: WebGLBuffer | null = null
let ibo: WebGLBuffer | null = null
let indexCount = 0

let texCurr: WebGLTexture | null = null
let texPrev: WebGLTexture | null = null
let maskCurr: WebGLTexture | null = null
let maskPrev: WebGLTexture | null = null

let analysisCanvas: HTMLCanvasElement | null = null
let analysisCtx: CanvasRenderingContext2D | null = null

let uTimeLoc: WebGLUniformLocation | null = null
let uIntensityLoc: WebGLUniformLocation | null = null
let uAnchorLoc: WebGLUniformLocation | null = null
let uPointerLoc: WebGLUniformLocation | null = null
let uCanvasSizeLoc: WebGLUniformLocation | null = null
let uCurrSizeLoc: WebGLUniformLocation | null = null
let uPrevSizeLoc: WebGLUniformLocation | null = null
let uMixLoc: WebGLUniformLocation | null = null
let uTexCurrLoc: WebGLUniformLocation | null = null
let uTexPrevLoc: WebGLUniformLocation | null = null
let uMaskCurrLoc: WebGLUniformLocation | null = null
let uMaskPrevLoc: WebGLUniformLocation | null = null

let currSize: [number, number] = [1, 1]
let prevSize: [number, number] = [1, 1]
let currImgAnchor: [number, number] = [0.5, 0.34]
let smoothAnchorX = 0.5
let smoothAnchorY = 0.34
let smoothIntensity = 1

let hasCurr = false
let hasPrev = false
let swapStartMs = 0
let rafId: number | null = null
let startTimeMs = 0
let loadSeq = 0
let resizeObserver: ResizeObserver | null = null
let canvasCssW = 1
let canvasCssH = 1
let smoothPtrX = 0
let smoothPtrY = 0
let lastFrameMs = 0

function smoothstep(edge0: number, edge1: number, x: number): number {
  const t = Math.max(0, Math.min(1, (x - edge0) / (edge1 - edge0)))
  return t * t * (3 - 2 * t)
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v))
}

/**
 * Heuristic foreground/background separation + subject centroid on a tiny 96x96
 * sample grid. Zero ML models: combines border backdrop color distance, local
 * Sobel edge richness, and portrait spatial priors in <2 ms.
 */
function analyzePortraitMask(img: HTMLImageElement): {
  bytes: Uint8Array
  anchor: [number, number]
} {
  const S = MASK_SIZE
  const bytes = new Uint8Array(S * S)

  try {
    if (!analysisCanvas) {
      analysisCanvas = document.createElement('canvas')
      analysisCanvas.width = S
      analysisCanvas.height = S
      analysisCtx = analysisCanvas.getContext('2d', { willReadFrequently: true })
    }
    if (!analysisCtx) throw new Error('no 2d context')
    analysisCtx.clearRect(0, 0, S, S)
    analysisCtx.drawImage(img, 0, 0, S, S)
    const pixels = analysisCtx.getImageData(0, 0, S, S).data

    // Sample representative backdrop colors along top and upper side edges
    const bgSamples: [number, number, number][] = []
    const topXs = [6, 20, 36, 52, 68, 84]
    for (const x of topXs) {
      const i = (1 * S + x) * 4
      bgSamples.push([pixels[i], pixels[i + 1], pixels[i + 2]])
    }
    const sideYs = [14, 32, 52]
    for (const y of sideYs) {
      const il = (y * S + 1) * 4
      const ir = (y * S + (S - 2)) * 4
      bgSamples.push([pixels[il], pixels[il + 1], pixels[il + 2]])
      bgSamples.push([pixels[ir], pixels[ir + 1], pixels[ir + 2]])
    }

    const luma = new Float32Array(S * S)
    for (let i = 0; i < S * S; i++) {
      const p = i * 4
      luma[i] = pixels[p] * 0.299 + pixels[p + 1] * 0.587 + pixels[p + 2] * 0.114
    }

    let bufA = new Float32Array(S * S)
    let bufB = new Float32Array(S * S)

    for (let y = 0; y < S; y++) {
      const v = y / (S - 1)
      const ym = Math.max(0, y - 1)
      const yp = Math.min(S - 1, y + 1)
      const vPrior = smoothstep(0.02, 0.20, v) * (1 - 0.12 * smoothstep(0.86, 1.0, v))

      for (let x = 0; x < S; x++) {
        const u = x / (S - 1)
        const idx = y * S + x
        const p = idx * 4
        const r = pixels[p]
        const g = pixels[p + 1]
        const b = pixels[p + 2]
        const alpha = pixels[p + 3] / 255

        let minDistSq = 1e9
        for (let s = 0; s < bgSamples.length; s++) {
          const [br, bg, bb] = bgSamples[s]
          const dr = r - br
          const dg = g - bg
          const db = b - bb
          const d2 = dr * dr + dg * dg + db * db
          if (d2 < minDistSq) minDistSq = d2
        }
        const distNorm = Math.sqrt(minDistSq) / 441.673

        const xm = Math.max(0, x - 1)
        const xp = Math.min(S - 1, x + 1)
        const gx = Math.abs(luma[y * S + xp] - luma[y * S + xm])
        const gy = Math.abs(luma[yp * S + x] - luma[ym * S + x])
        const edgeNorm = (gx + gy) / 510

        const uPrior = Math.exp(-Math.pow((u - 0.5) / 0.42, 2))
        const prior = uPrior * vPrior
        const raw =
          (0.64 * smoothstep(0.05, 0.30, distNorm) + 0.36 * smoothstep(0.025, 0.17, edgeNorm)) *
          (0.22 + 0.78 * prior) *
          alpha
        bufA[idx] = clamp(raw, 0, 1)
      }
    }

    // Three passes of 3x3 box blur fill solid character interiors cleanly
    for (let pass = 0; pass < 3; pass++) {
      for (let y = 0; y < S; y++) {
        const ym = Math.max(0, y - 1)
        const yp = Math.min(S - 1, y + 1)
        for (let x = 0; x < S; x++) {
          const xm = Math.max(0, x - 1)
          const xp = Math.min(S - 1, x + 1)
          const sum =
            bufA[ym * S + xm] +
            bufA[ym * S + x] +
            bufA[ym * S + xp] +
            bufA[y * S + xm] +
            bufA[y * S + x] +
            bufA[y * S + xp] +
            bufA[yp * S + xm] +
            bufA[yp * S + x] +
            bufA[yp * S + xp]
          bufB[y * S + x] = sum / 9
        }
      }
      const tmp = bufA
      bufA = bufB
      bufB = tmp
    }

    let wSum = 0
    let wxSum = 0
    let wySum = 0
    for (let y = 0; y < S; y++) {
      const v = y / (S - 1)
      for (let x = 0; x < S; x++) {
        const u = x / (S - 1)
        const idx = y * S + x
        const val = smoothstep(0.10, 0.68, bufA[idx])
        bytes[idx] = Math.round(val * 255)

        if (u >= 0.22 && u <= 0.78 && v >= 0.14 && v <= 0.58) {
          const w = val * val
          wSum += w
          wxSum += u * w
          wySum += v * w
        }
      }
    }

    const anchor: [number, number] =
      wSum > 0.5
        ? [clamp(wxSum / wSum, 0.34, 0.66), clamp(wySum / wSum, 0.22, 0.44)]
        : [0.5, 0.34]
    return { bytes, anchor }
  } catch {
    for (let y = 0; y < S; y++) {
      const v = y / (S - 1)
      for (let x = 0; x < S; x++) {
        const u = x / (S - 1)
        const val = Math.exp(-Math.pow((u - 0.5) / 0.34, 2) - Math.pow((v - 0.48) / 0.38, 2))
        bytes[y * S + x] = Math.round(val * 255)
      }
    }
    return { bytes, anchor: [0.5, 0.34] }
  }
}

function compileShader(context: WebGLRenderingContext, type: number, source: string): WebGLShader | null {
  const shader = context.createShader(type)
  if (!shader) return null
  context.shaderSource(shader, source)
  context.compileShader(shader)
  if (!context.getShaderParameter(shader, context.COMPILE_STATUS)) {
    context.deleteShader(shader)
    return null
  }
  return shader
}

function createEmptyTexture(context: WebGLRenderingContext): WebGLTexture | null {
  const tex = context.createTexture()
  if (!tex) return null
  context.bindTexture(context.TEXTURE_2D, tex)
  context.texParameteri(context.TEXTURE_2D, context.TEXTURE_WRAP_S, context.CLAMP_TO_EDGE)
  context.texParameteri(context.TEXTURE_2D, context.TEXTURE_WRAP_T, context.CLAMP_TO_EDGE)
  context.texParameteri(context.TEXTURE_2D, context.TEXTURE_MIN_FILTER, context.LINEAR)
  context.texParameteri(context.TEXTURE_2D, context.TEXTURE_MAG_FILTER, context.LINEAR)
  const px = new Uint8Array([0, 0, 0, 0])
  context.texImage2D(context.TEXTURE_2D, 0, context.RGBA, 1, 1, 0, context.RGBA, context.UNSIGNED_BYTE, px)
  return tex
}

function createEmptyMaskTexture(context: WebGLRenderingContext): WebGLTexture | null {
  const tex = context.createTexture()
  if (!tex) return null
  context.bindTexture(context.TEXTURE_2D, tex)
  context.texParameteri(context.TEXTURE_2D, context.TEXTURE_WRAP_S, context.CLAMP_TO_EDGE)
  context.texParameteri(context.TEXTURE_2D, context.TEXTURE_WRAP_T, context.CLAMP_TO_EDGE)
  context.texParameteri(context.TEXTURE_2D, context.TEXTURE_MIN_FILTER, context.LINEAR)
  context.texParameteri(context.TEXTURE_2D, context.TEXTURE_MAG_FILTER, context.LINEAR)
  const px = new Uint8Array([128])
  context.pixelStorei(context.UNPACK_ALIGNMENT, 1)
  context.texImage2D(
    context.TEXTURE_2D,
    0,
    context.LUMINANCE,
    1,
    1,
    0,
    context.LUMINANCE,
    context.UNSIGNED_BYTE,
    px,
  )
  return tex
}

function buildGridBuffers(context: WebGLRenderingContext): boolean {
  const cols = GRID_X + 1
  const rows = GRID_Y + 1
  const verts = new Float32Array(cols * rows * 2)
  let ptr = 0
  for (let y = 0; y < rows; y++) {
    const v = y / GRID_Y
    for (let x = 0; x < cols; x++) {
      verts[ptr++] = x / GRID_X
      verts[ptr++] = v
    }
  }

  const indices = new Uint16Array(GRID_X * GRID_Y * 6)
  let ip = 0
  for (let y = 0; y < GRID_Y; y++) {
    for (let x = 0; x < GRID_X; x++) {
      const i0 = y * cols + x
      const i1 = i0 + 1
      const i2 = i0 + cols
      const i3 = i2 + 1
      indices[ip++] = i0
      indices[ip++] = i2
      indices[ip++] = i1
      indices[ip++] = i1
      indices[ip++] = i2
      indices[ip++] = i3
    }
  }
  indexCount = indices.length

  vbo = context.createBuffer()
  ibo = context.createBuffer()
  if (!vbo || !ibo) return false

  context.bindBuffer(context.ARRAY_BUFFER, vbo)
  context.bufferData(context.ARRAY_BUFFER, verts, context.STATIC_DRAW)

  context.bindBuffer(context.ELEMENT_ARRAY_BUFFER, ibo)
  context.bufferData(context.ELEMENT_ARRAY_BUFFER, indices, context.STATIC_DRAW)
  return true
}

function syncCanvasSize() {
  const canvas = canvasRef.value
  if (!canvas || !gl) return
  const rect = canvas.getBoundingClientRect()
  const w = Math.max(1, Math.round(rect.width))
  const h = Math.max(1, Math.round(rect.height))
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  const pw = Math.max(1, Math.round(w * dpr))
  const ph = Math.max(1, Math.round(h * dpr))
  canvasCssW = w
  canvasCssH = h
  if (canvas.width !== pw || canvas.height !== ph) {
    canvas.width = pw
    canvas.height = ph
  }
  gl.viewport(0, 0, pw, ph)
}

function drawFrame(now: number) {
  rafId = null
  if (!gl || !program || !hasCurr || document.hidden) return

  if (!startTimeMs) startTimeMs = now
  const dt = lastFrameMs ? Math.min(0.05, Math.max(0.001, (now - lastFrameMs) / 1000)) : 0.016
  lastFrameMs = now

  const ptrLerp = 1 - Math.exp(-dt * 7.5)
  const targetX = props.dragging ? smoothPtrX : props.hovering ? props.pointerX : 0
  const targetY = props.dragging ? smoothPtrY : props.hovering ? props.pointerY : 0
  smoothPtrX += (targetX - smoothPtrX) * ptrLerp
  smoothPtrY += (targetY - smoothPtrY) * ptrLerp

  const slowLerp = 1 - Math.exp(-dt * 4.0)
  smoothAnchorX += (currImgAnchor[0] - smoothAnchorX) * slowLerp
  smoothAnchorY += (currImgAnchor[1] - smoothAnchorY) * slowLerp
  smoothIntensity += (props.intensity - smoothIntensity) * slowLerp

  const elapsedSec = ((now - startTimeMs) / 1000) % 6283.1853
  const mixVal = hasPrev ? Math.min(1, (now - swapStartMs) / SWAP_DURATION_MS) : 1
  if (mixVal >= 1) hasPrev = false

  gl.clearColor(0, 0, 0, 0)
  gl.clear(gl.COLOR_BUFFER_BIT)
  gl.useProgram(program)
  gl.uniform1f(uTimeLoc, elapsedSec)
  gl.uniform1f(uIntensityLoc, smoothIntensity)
  gl.uniform2f(uAnchorLoc, smoothAnchorX, smoothAnchorY)
  gl.uniform2f(uPointerLoc, smoothPtrX, smoothPtrY)
  gl.uniform2f(uCanvasSizeLoc, canvasCssW, canvasCssH)
  gl.uniform2f(uCurrSizeLoc, currSize[0], currSize[1])
  gl.uniform2f(uPrevSizeLoc, prevSize[0], prevSize[1])
  gl.uniform1f(uMixLoc, mixVal)

  gl.activeTexture(gl.TEXTURE0)
  gl.bindTexture(contextTex(gl), texCurr)
  gl.uniform1i(uTexCurrLoc, 0)

  gl.activeTexture(gl.TEXTURE1)
  gl.bindTexture(contextTex(gl), texPrev)
  gl.uniform1i(uTexPrevLoc, 1)

  gl.activeTexture(gl.TEXTURE2)
  gl.bindTexture(contextTex(gl), maskCurr)
  gl.uniform1i(uMaskCurrLoc, 2)

  gl.activeTexture(gl.TEXTURE3)
  gl.bindTexture(contextTex(gl), maskPrev)
  gl.uniform1i(uMaskPrevLoc, 3)

  gl.drawElements(gl.TRIANGLES, indexCount, gl.UNSIGNED_SHORT, 0)

  rafId = window.requestAnimationFrame(drawFrame)
}

function contextTex(context: WebGLRenderingContext): number {
  return context.TEXTURE_2D
}

function ensureRunning() {
  if (rafId === null && hasCurr && gl && !document.hidden) {
    lastFrameMs = 0
    rafId = window.requestAnimationFrame(drawFrame)
  }
}

function stopLoop() {
  if (rafId !== null) {
    window.cancelAnimationFrame(rafId)
    rafId = null
  }
}

function loadTextureFromUrl(url: string) {
  if (!gl || !url) return
  const token = ++loadSeq
  const img = new Image()
  img.crossOrigin = 'anonymous'

  img.onload = () => {
    if (token !== loadSeq || !gl) return
    const w = img.naturalWidth || img.width || 1
    const h = img.naturalHeight || img.height || 1
    const { bytes, anchor } = analyzePortraitMask(img)

    if (hasCurr && texCurr && texPrev && maskCurr && maskPrev) {
      const swappedTex = texPrev
      texPrev = texCurr
      texCurr = swappedTex

      const swappedMask = maskPrev
      maskPrev = maskCurr
      maskCurr = swappedMask

      prevSize = [currSize[0], currSize[1]]
      hasPrev = true
      swapStartMs = performance.now()
    } else {
      hasPrev = false
    }

    if (!texCurr) texCurr = createEmptyTexture(gl)
    if (!maskCurr) maskCurr = createEmptyMaskTexture(gl)
    if (!texCurr || !maskCurr) {
      emit('ready', false)
      return
    }

    gl.bindTexture(gl.TEXTURE_2D, texCurr)
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, 0)
    try {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img)
    } catch {
      emit('ready', false)
      return
    }

    gl.bindTexture(gl.TEXTURE_2D, maskCurr)
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1)
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, 0)
    gl.texImage2D(
      gl.TEXTURE_2D,
      0,
      gl.LUMINANCE,
      MASK_SIZE,
      MASK_SIZE,
      0,
      gl.LUMINANCE,
      gl.UNSIGNED_BYTE,
      bytes,
    )

    currSize = [w, h]
    currImgAnchor = anchor
    syncCanvasSize()
    if (!hasCurr) {
      smoothAnchorX = anchor[0]
      smoothAnchorY = anchor[1]
      smoothIntensity = props.intensity
    }
    hasCurr = true
    emit('ready', true)
    ensureRunning()
  }

  img.onerror = () => {
    if (token !== loadSeq) return
    if (!hasCurr) emit('ready', false)
  }

  img.src = url
}

function initWebGl(): boolean {
  const canvas = canvasRef.value
  if (!canvas) return false
  const context = canvas.getContext('webgl', {
    alpha: true,
    antialias: true,
    depth: false,
    stencil: false,
    premultipliedAlpha: false,
    powerPreference: 'default',
  })
  if (!context) return false

  const vs = compileShader(context, context.VERTEX_SHADER, VERTEX_SHADER_SRC)
  const fs = compileShader(context, context.FRAGMENT_SHADER, FRAGMENT_SHADER_SRC)
  if (!vs || !fs) {
    if (vs) context.deleteShader(vs)
    if (fs) context.deleteShader(fs)
    return false
  }

  const prog = context.createProgram()
  if (!prog) return false
  context.attachShader(prog, vs)
  context.attachShader(prog, fs)
  context.linkProgram(prog)
  context.deleteShader(vs)
  context.deleteShader(fs)
  if (!context.getProgramParameter(prog, context.LINK_STATUS)) {
    context.deleteProgram(prog)
    return false
  }

  gl = context
  program = prog
  if (!buildGridBuffers(context)) return false

  texCurr = createEmptyTexture(context)
  texPrev = createEmptyTexture(context)
  maskCurr = createEmptyMaskTexture(context)
  maskPrev = createEmptyMaskTexture(context)
  if (!texCurr || !texPrev || !maskCurr || !maskPrev) return false

  const aPosLoc = context.getAttribLocation(prog, 'a_pos')
  context.bindBuffer(context.ARRAY_BUFFER, vbo)
  context.enableVertexAttribArray(aPosLoc)
  context.vertexAttribPointer(aPosLoc, 2, context.FLOAT, false, 0, 0)

  uTimeLoc = context.getUniformLocation(prog, 'u_time')
  uIntensityLoc = context.getUniformLocation(prog, 'u_intensity')
  uAnchorLoc = context.getUniformLocation(prog, 'u_anchor')
  uPointerLoc = context.getUniformLocation(prog, 'u_pointer')
  uCanvasSizeLoc = context.getUniformLocation(prog, 'u_canvasSize')
  uCurrSizeLoc = context.getUniformLocation(prog, 'u_currSize')
  uPrevSizeLoc = context.getUniformLocation(prog, 'u_prevSize')
  uMixLoc = context.getUniformLocation(prog, 'u_mix')
  uTexCurrLoc = context.getUniformLocation(prog, 'u_texCurr')
  uTexPrevLoc = context.getUniformLocation(prog, 'u_texPrev')
  uMaskCurrLoc = context.getUniformLocation(prog, 'u_maskCurr')
  uMaskPrevLoc = context.getUniformLocation(prog, 'u_maskPrev')

  syncCanvasSize()
  return true
}

function cleanupWebGl() {
  stopLoop()
  loadSeq += 1
  if (gl) {
    if (texCurr) gl.deleteTexture(texCurr)
    if (texPrev) gl.deleteTexture(texPrev)
    if (maskCurr) gl.deleteTexture(maskCurr)
    if (maskPrev) gl.deleteTexture(maskPrev)
    if (vbo) gl.deleteBuffer(vbo)
    if (ibo) gl.deleteBuffer(ibo)
    if (program) gl.deleteProgram(program)
  }
  gl = null
  program = null
  vbo = null
  ibo = null
  texCurr = null
  texPrev = null
  maskCurr = null
  maskPrev = null
  hasCurr = false
  hasPrev = false
}

function onVisibilityChange() {
  if (document.hidden) {
    stopLoop()
  } else {
    ensureRunning()
  }
}

function onContextLost(e: Event) {
  e.preventDefault()
  stopLoop()
  emit('ready', false)
}

onMounted(() => {
  const canvas = canvasRef.value
  if (!canvas || !initWebGl()) {
    emit('ready', false)
    return
  }

  canvas.addEventListener('webglcontextlost', onContextLost)
  document.addEventListener('visibilitychange', onVisibilityChange)

  resizeObserver = new ResizeObserver(() => {
    syncCanvasSize()
  })
  resizeObserver.observe(canvas)

  if (props.src) {
    loadTextureFromUrl(props.src)
  }
})

watch(
  () => props.src,
  (nextSrc) => {
    if (!nextSrc || !gl) return
    loadTextureFromUrl(nextSrc)
  },
)

onBeforeUnmount(() => {
  const canvas = canvasRef.value
  if (canvas) canvas.removeEventListener('webglcontextlost', onContextLost)
  document.removeEventListener('visibilitychange', onVisibilityChange)
  resizeObserver?.disconnect()
  resizeObserver = null
  cleanupWebGl()
})
</script>

<template>
  <canvas ref="canvasRef" class="scene-motion-canvas" aria-hidden="true"></canvas>
</template>

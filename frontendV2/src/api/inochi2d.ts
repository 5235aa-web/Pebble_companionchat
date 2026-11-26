import { get, getDirectBase, post, put } from './client'

export interface PuppetLayer {
  id: string
  tag: string
  label: string
  labelEn: string
  category: string
  rig: string
  role?: string
  outfit?: string
  subjectId?: string
  x: number
  y: number
  width: number
  height: number
  nx: number
  ny: number
  nw: number
  nh: number
  zIndex: number
  depth: number
  depthRelPath?: string
  parallaxX: number
  parallaxY: number
  physics: number
  visible: boolean
  relPath: string
}

export interface PuppetAnchors {
  headPivot: [number, number]
  faceCenter: [number, number]
  eyeL: [number, number]
  eyeR: [number, number]
  headTilt?: number
  mouthCenter: [number, number]
  mouthSize: [number, number]
  chestCenter: [number, number]
  shoulderL?: [number, number]
  shoulderR?: [number, number]
  waistCenter?: [number, number]
}

export interface PuppetSubject {
  id: string
  label: string
  anchors: PuppetAnchors
}

export interface PuppetExpression {
  id: string
  label: string
  labelEn?: string
  emotionKey?: string
  isBase?: boolean
  layers: PuppetLayer[]
}

export interface PuppetOutfit {
  id: string
  label: string
  labelEn?: string
}

export interface PuppetPoseSlot {
  id: string
  label: string
  labelEn?: string
  triggerMoods: string[]
  triggerKeywords: string[]
  isDefault?: boolean
  createdAt?: number
}

export interface PuppetPosesIndex {
  activePoseId: string
  poses: PuppetPoseSlot[]
}

export interface PuppetManifest {
  version: number
  format?: string
  canvas: { width: number; height: number }
  contentBounds: { x: number; y: number; width: number; height: number }
  anchors: PuppetAnchors
  subjects?: PuppetSubject[]
  source: string
  layers: PuppetLayer[]
  blinkLayers?: PuppetLayer[]
  expressions?: PuppetExpression[]
  outfits?: PuppetOutfit[]
}

export type PuppetEmotion = string

export type PuppetActionId =
  | 'nod'
  | 'shake'
  | 'lean_in'
  | 'hug_lean'
  | 'cheer'
  | 'shy_hide'
  | 'sigh'
  | 'startle'
  | 'bow'

export interface PuppetConfig {
  motionEnabled: boolean
  breathAmp: number
  breathSpeed: number
  swayAmp: number
  headParallax: number
  depthRelief: number
  limbMotion: number
  eyeTrack: boolean
  blinkRate: number
  mouthSens: number
  physicsStrength: number
  physicsDamping: number
  autoChatSync: boolean
  emotion: PuppetEmotion
  outfit?: string
  testMouth: number
  hiddenLayers: string[]
}

export interface PuppetJobState {
  status: 'idle' | 'running' | 'done' | 'error'
  stage: string
  progress: number
  error: string
}

export interface InochiRuntimeInfo {
  backend: 'cuda' | 'rocm' | 'mps' | 'cpu' | 'unknown'
  gpuName: string
  vramGb: number
  dtype: string
  torchVersion: string
  offloadDefault: boolean
}

export interface InochiEnvStatus {
  ready: boolean
  pythonPath: string
  configuredPythonPath?: string
  expectedVenvDir?: string
  seethroughPath: string
  modelsRoot?: string
  layerdiffPath: string
  marigoldPath: string
  expectedLayerdiffDir?: string
  expectedMarigoldDir?: string
  requirementsPath?: string
  missingDeps?: string[]
  runtime?: InochiRuntimeInfo
  missing: string[]
}

export interface PuppetPayload {
  companionId: string
  env: InochiEnvStatus
  job: PuppetJobState
  puppet: PuppetManifest | null
  posesIndex?: PuppetPosesIndex
  config: PuppetConfig
}

export interface RigLayerPatch {
  id: string
  zIndex?: number
  parallaxX?: number
  parallaxY?: number
  physics?: number
  rig?: string
  subjectId?: string
}

export function getInochiStatus(refresh = false): Promise<InochiEnvStatus> {
  return get<InochiEnvStatus>(`/inochi2d/status${refresh ? '?refresh=1' : ''}`)
}

export function getPuppet(companionId: string, poseId = ''): Promise<PuppetPayload> {
  const pq = poseId ? `&poseId=${encodeURIComponent(poseId)}` : ''
  return get<PuppetPayload>(`/inochi2d/puppet?companionId=${encodeURIComponent(companionId)}${pq}`)
}

export function buildPuppet(req: {
  companionId: string
  sourceRelPath?: string
  resolution?: number
  steps?: number
  seed?: number
  noCompleteEye?: boolean
  poseId?: string
  poseLabel?: string
}): Promise<{ job: PuppetJobState }> {
  return post<{ job: PuppetJobState }>('/inochi2d/build', req)
}

export async function buildPuppetUpload(
  companionId: string,
  file: File,
  resolution = 768,
  steps = 20,
  poseId = '',
  poseLabel = '',
): Promise<{ job: PuppetJobState }> {
  const fd = new FormData()
  fd.append('companionId', companionId)
  fd.append('resolution', String(resolution))
  fd.append('steps', String(steps))
  if (poseId) fd.append('poseId', poseId)
  if (poseLabel) fd.append('poseLabel', poseLabel)
  fd.append('file', file)
  const resp = await fetch(`${getDirectBase()}/inochi2d/build-upload`, {
    method: 'POST',
    body: fd,
  })
  if (!resp.ok) {
    const txt = await resp.text().catch(() => resp.statusText)
    throw new Error(txt || `HTTP ${resp.status}`)
  }
  return (await resp.json()) as { job: PuppetJobState }
}

export async function buildPuppetPack(opts: {
  companionId: string
  mode: 'base' | 'outfit'
  outfitId?: string
  outfitLabel?: string
  poseId?: string
  poseLabel?: string
  baseIndex?: number
  labels?: string[]
  assetIds?: number[]
  files?: File[]
  saveToAssets?: boolean
  resolution?: number
  steps?: number
  noCompleteEye?: boolean
}): Promise<{ job: PuppetJobState }> {
  const fd = new FormData()
  fd.append('companionId', opts.companionId)
  fd.append('mode', opts.mode)
  if (opts.outfitId) fd.append('outfitId', opts.outfitId)
  if (opts.outfitLabel) fd.append('outfitLabel', opts.outfitLabel)
  if (opts.poseId) fd.append('poseId', opts.poseId)
  if (opts.poseLabel) fd.append('poseLabel', opts.poseLabel)
  fd.append('baseIndex', String(opts.baseIndex ?? 0))
  fd.append('labelsJson', JSON.stringify(opts.labels ?? []))
  fd.append('assetIdsJson', JSON.stringify(opts.assetIds ?? []))
  fd.append('saveToAssets', String(opts.saveToAssets ?? true))
  fd.append('resolution', String(opts.resolution ?? 768))
  fd.append('steps', String(opts.steps ?? 20))
  fd.append('noCompleteEye', String(Boolean(opts.noCompleteEye)))
  for (const f of opts.files ?? []) {
    fd.append('files', f)
  }
  const resp = await fetch(`${getDirectBase()}/inochi2d/build-pack`, {
    method: 'POST',
    body: fd,
  })
  if (!resp.ok) {
    const txt = await resp.text().catch(() => resp.statusText)
    throw new Error(txt || `HTTP ${resp.status}`)
  }
  return (await resp.json()) as { job: PuppetJobState }
}

export async function deletePuppetOutfit(
  companionId: string,
  outfitId: string,
): Promise<{ puppet: PuppetManifest; config: PuppetConfig }> {
  const resp = await fetch(
    `${getDirectBase()}/inochi2d/outfit?companionId=${encodeURIComponent(companionId)}&outfitId=${encodeURIComponent(outfitId)}`,
    { method: 'DELETE' },
  )
  if (!resp.ok) {
    const txt = await resp.text().catch(() => resp.statusText)
    throw new Error(txt || `HTTP ${resp.status}`)
  }
  return (await resp.json()) as { puppet: PuppetManifest; config: PuppetConfig }
}

export function savePuppetConfig(
  companionId: string,
  config: Partial<PuppetConfig>,
): Promise<{ config: PuppetConfig }> {
  return put<{ config: PuppetConfig }>('/inochi2d/config', { companionId, config })
}

export function cancelPuppetBuild(companionId: string): Promise<{ job: PuppetJobState }> {
  return post<{ job: PuppetJobState }>(
    `/inochi2d/cancel?companionId=${encodeURIComponent(companionId)}`,
    {},
  )
}

export function selectPuppetPose(
  companionId: string,
  poseId: string,
): Promise<{ posesIndex: PuppetPosesIndex; puppet: PuppetManifest | null }> {
  return post<{ posesIndex: PuppetPosesIndex; puppet: PuppetManifest | null }>(
    '/inochi2d/poses/select',
    { companionId, poseId },
  )
}

export function updatePuppetPoseMeta(req: {
  companionId: string
  poseId: string
  label?: string
  triggerMoods?: string[]
  triggerKeywords?: string[]
}): Promise<{ posesIndex: PuppetPosesIndex }> {
  return post<{ posesIndex: PuppetPosesIndex }>('/inochi2d/poses/meta', req)
}

export async function deletePuppetPose(
  companionId: string,
  poseId: string,
): Promise<{ posesIndex: PuppetPosesIndex; puppet: PuppetManifest | null }> {
  const resp = await fetch(
    `${getDirectBase()}/inochi2d/poses?companionId=${encodeURIComponent(companionId)}&poseId=${encodeURIComponent(poseId)}`,
    { method: 'DELETE' },
  )
  if (!resp.ok) {
    const txt = await resp.text().catch(() => resp.statusText)
    throw new Error(txt || `HTTP ${resp.status}`)
  }
  return (await resp.json()) as { posesIndex: PuppetPosesIndex; puppet: PuppetManifest | null }
}

async function importPuppetFile(
  endpoint: string,
  companionId: string,
  file: File,
  poseId = '',
  poseLabel = '',
): Promise<{ puppet: PuppetManifest; posesIndex: PuppetPosesIndex }> {
  const fd = new FormData()
  fd.append('companionId', companionId)
  if (poseId) fd.append('poseId', poseId)
  if (poseLabel) fd.append('poseLabel', poseLabel)
  fd.append('file', file)
  const resp = await fetch(`${getDirectBase()}${endpoint}`, {
    method: 'POST',
    body: fd,
  })
  if (!resp.ok) {
    const txt = await resp.text().catch(() => resp.statusText)
    throw new Error(txt || `HTTP ${resp.status}`)
  }
  return (await resp.json()) as { puppet: PuppetManifest; posesIndex: PuppetPosesIndex }
}

export function importPsdPuppet(
  companionId: string,
  file: File,
  poseId = '',
  poseLabel = '',
): Promise<{ puppet: PuppetManifest; posesIndex: PuppetPosesIndex }> {
  return importPuppetFile('/inochi2d/import-psd', companionId, file, poseId, poseLabel)
}

export function importInxPuppet(
  companionId: string,
  file: File,
  poseId = '',
  poseLabel = '',
): Promise<{ puppet: PuppetManifest; posesIndex: PuppetPosesIndex }> {
  return importPuppetFile('/inochi2d/import-inx', companionId, file, poseId, poseLabel)
}

export function savePuppetRig(req: {
  companionId: string
  poseId?: string
  anchors?: Partial<PuppetAnchors>
  subjects?: PuppetSubject[]
  layers?: RigLayerPatch[]
}): Promise<{ puppet: PuppetManifest }> {
  return put<{ puppet: PuppetManifest }>('/inochi2d/rig', req)
}

export function matchCompanionLive2dCue(
  companionId: string,
  req: { text?: string; mood?: string; expression?: string },
): Promise<{ matchedPoseId: string | null; posesIndex: PuppetPosesIndex; puppet: PuppetManifest | null }> {
  return post(`/companions/${encodeURIComponent(companionId)}/live2d/cue`, req)
}

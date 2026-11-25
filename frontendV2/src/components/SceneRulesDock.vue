<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import {
  compileSceneRule,
  explainSceneRules,
  type SceneRuleCompileResult,
} from '@/api/companion'
import { useFileUrls } from '@/composables/useFileUrl'
import { useI18n } from '@/i18n'
import { useChatStore } from '@/stores/chat'
import { useCardHudStore } from '@/stores/cardHud'
import { useSessionStore } from '@/stores/session'
import { useUiStore } from '@/stores/ui'
import { evaluateCbs, type CbsOptions } from '@/utils/cbs'
import {
  applyDisplayRegex,
  type DisplayRegexRule,
  type ScenePromptRule,
} from '@/utils/displayRegex'
import {
  extractInlineMediaFromText,
  normalizeAssetTagRefs,
  rewriteCssAssetUrls,
} from '@/utils/inlineMedia'
import {
  buildOfflineBlockRule,
  buildOfflinePromptRule,
  IMMERSIVE_RECIPES,
  type BlockMatchMode,
  type BlockRuleScope,
  type BlockVisualAction,
  type ImmersiveRecipe,
  type PromptConditionMode,
} from '@/utils/sceneRuleRecipes'
import RichMessage from './RichMessage.vue'
import UiSelect, { type UiOption } from './ui/UiSelect.vue'

const session = useSessionStore()
const chat = useChatStore()
const cardHud = useCardHudStore()
const ui = useUiStore()
const { locale } = useI18n()

const isZh = computed(() => locale.value !== 'en')
const activeTab = ref<'create' | 'rules' | 'recipes'>('create')
const createMode = ref<'nl' | 'blocks'>('nl')
const blockKind = ref<'format' | 'prompt'>('format')

const nlModeOptions = computed<UiOption[]>(() =>
  isZh.value
    ? [
        { value: 'auto', label: '自动识别类型' },
        { value: 'display', label: '仅改视觉显示' },
        { value: 'scene_prompt', label: '剧情/时间/语气提示 (0正则)' },
        { value: 'output', label: '净化 AI 回复' },
        { value: 'prompt', label: '发给 AI 前精简历史（不改屏幕）' },
        { value: 'input', label: '改写我的输入' },
      ]
    : [
        { value: 'auto', label: 'Auto detect type' },
        { value: 'display', label: 'Visual display only' },
        { value: 'scene_prompt', label: 'Scene prompt (no regex)' },
        { value: 'output', label: 'Clean AI output' },
        { value: 'prompt', label: 'Context-only history rewrite' },
        { value: 'input', label: 'Rewrite user input' },
      ],
)

const blockScopeOptions = computed<UiOption[]>(() =>
  isZh.value
    ? [
        { value: 'display', label: '仅改视觉显示' },
        { value: 'output', label: '净化 AI 回复原文' },
        { value: 'prompt', label: '发给 AI 前精简历史（不改屏幕）' },
      ]
    : [
        { value: 'display', label: 'Visual Display' },
        { value: 'output', label: 'Clean AI Reply' },
        { value: 'prompt', label: 'Context-only (AI prompt)' },
      ],
)

const blockMatchOptions = computed<UiOption[]>(() =>
  isZh.value
    ? [
        { value: 'delimited', label: '成对符号包裹（如 【...】）' },
        { value: 'line_prefix', label: '整行开头匹配' },
        { value: 'kv_line', label: '属性行（字段: 值）' },
        { value: 'literal', label: '固定词语' },
      ]
    : [
        { value: 'delimited', label: 'Wrapped delimiters' },
        { value: 'line_prefix', label: 'Line prefix' },
        { value: 'kv_line', label: 'Key: Value line' },
        { value: 'literal', label: 'Exact phrase' },
      ],
)

const blockActionOptions = computed<UiOption[]>(() =>
  isZh.value
    ? [
        { value: 'aside', label: '转为斜体心声条' },
        { value: 'card', label: '装入圆角提示卡片' },
        { value: 'badge', label: '变成高亮微标签' },
        { value: 'hide', label: '隐藏不显示' },
        { value: 'replace_text', label: '替换为其他文字' },
      ]
    : [
        { value: 'aside', label: 'Italic aside' },
        { value: 'card', label: 'Rounded info card' },
        { value: 'badge', label: 'Pill badge' },
        { value: 'hide', label: 'Hide completely' },
        { value: 'replace_text', label: 'Replace with text' },
      ],
)

const promptConditionOptions = computed<UiOption[]>(() =>
  isZh.value
    ? [
        { value: 'idle', label: '离开超过指定分钟' },
        { value: 'chat_turn', label: '对话达到指定轮数' },
        { value: 'time_window', label: '每天特定时段' },
        { value: 'hud_affection', label: 'HUD 好感度达到指定值' },
        { value: 'hud_field', label: 'HUD 地点 / 心情 / 着装包含关键词' },
        { value: 'always', label: '每轮对话始终生效' },
      ]
    : [
        { value: 'idle', label: 'Idle minutes >=' },
        { value: 'chat_turn', label: 'Chat turn >=' },
        { value: 'time_window', label: 'Time window (hour)' },
        { value: 'hud_affection', label: 'HUD affection >=' },
        { value: 'hud_field', label: 'HUD field contains keyword' },
        { value: 'always', label: 'Always active' },
      ],
)

const hudFieldOptions = computed<UiOption[]>(() =>
  isZh.value
    ? [
        { value: 'location', label: '地点 (location)' },
        { value: 'mood', label: '心情 (mood)' },
        { value: 'outfit', label: '着装 (outfit)' },
      ]
    : [
        { value: 'location', label: 'Location' },
        { value: 'mood', label: 'Mood' },
        { value: 'outfit', label: 'Outfit' },
      ],
)

const saving = ref(false)
const busyCompile = ref(false)
const busyExplain = ref(false)
const statusMsg = ref('')
const errorMsg = ref('')

/* ---- Natural Language Compiler State ---- */
const nlInstruction = ref('')
const nlPreferredMode = ref<'auto' | 'display' | 'scene_prompt' | 'output' | 'prompt' | 'input'>('auto')
const sampleText = ref('')
const compileResult = ref<SceneRuleCompileResult | null>(null)

/* ---- Offline Block Builder State ---- */
const formatBlock = reactive({
  name: '',
  scope: 'display' as BlockRuleScope,
  matchMode: 'delimited' as BlockMatchMode,
  startToken: '【心声：',
  endToken: '】',
  action: 'aside' as BlockVisualAction,
  replaceText: '',
})

const promptBlock = reactive({
  name: '',
  conditionMode: 'idle' as PromptConditionMode,
  threshold: 120,
  nightStartHour: 22,
  nightEndHour: 5,
  hudField: 'location' as 'location' | 'mood' | 'outfit',
  hudMatch: '',
  placement: 'system' as 'system' | 'post_history',
  prompt: '',
})

/* ---- Live Sandbox Simulation State ---- */
const simChatIndex = ref(12)
const simIdleMinutes = ref(15)
const simAffection = ref(50)
const varDraft = ref<Record<string, string>>({})
const newVarKey = ref('')
const newVarVal = ref('')
const expandedCodeIds = ref<Record<string, boolean>>({})

/* ---- Live Context Helpers ---- */
const latestAiReply = computed(() => {
  for (let i = chat.beats.length - 1; i >= 0; i -= 1) {
    const b = chat.beats[i]
    if (b.role === 'ai') {
      return typeof b.raw === 'string' ? b.raw : b.lines.map((l) => l.text).join('\n')
    }
  }
  return ''
})

function pickDefaultSample(): string {
  return (
    latestAiReply.value ||
    session.emptyLine ||
    '她轻轻垂下眼帘，指尖摩挲着温热的杯壁。\n【心声：其实已经等了好久……】'
  )
}

function useSampleFromLatestAi() {
  sampleText.value = latestAiReply.value || pickDefaultSample()
}

function useSampleFromGreeting() {
  sampleText.value = session.emptyLine || pickDefaultSample()
}

watch(
  () => session.activeCompanionId,
  () => {
    compileResult.value = null
    errorMsg.value = ''
    statusMsg.value = ''
    expandedCodeIds.value = {}
    sampleText.value = pickDefaultSample()
  },
  { immediate: true },
)

watch(
  () => session.branchVariables,
  (vars) => {
    varDraft.value = { ...(vars ?? {}) }
  },
  { immediate: true, deep: true },
)

watch(
  () => [ui.sceneDockOpen, chat.beats.length, cardHud.affectionValue] as const,
  ([open]) => {
    if (open && !sampleText.value.trim()) {
      sampleText.value = pickDefaultSample()
    }
    simChatIndex.value = Math.max(0, chat.beats.length - 1)
    if (cardHud.affectionValue !== null) {
      simAffection.value = Math.max(0, Math.min(100, Math.round(cardHud.affectionValue)))
    }
  },
)

const totalActiveRules = computed(() => {
  const s = session.sceneRules.filter((r) => r.enabled !== false).length
  const d = session.displayRegexRules.filter((r) => r.enabled !== false).length
  const o = session.outputRegexRules.filter((r) => r.enabled !== false).length
  const p = session.promptRegexRules.filter((r) => r.enabled !== false).length
  const i = session.inputRegexRules.filter((r) => r.enabled !== false).length
  return s + d + o + p + i
})

const hasRulesConfigured = computed(
  () =>
    session.sceneRules.length +
      session.displayRegexRules.length +
      session.outputRegexRules.length +
      session.promptRegexRules.length +
      session.inputRegexRules.length >
    0,
)

const currentTimeClock = computed(() => {
  const d = new Date()
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
})

/* ---- Unified Rule List for Active Companion ---- */
type UnifiedRuleKind = 'scene_prompt' | 'display' | 'output' | 'prompt' | 'input'

interface UnifiedRuleRow {
  kind: UnifiedRuleKind
  index: number
  id: string
  name: string
  summary: string
  enabled: boolean
  origin?: string
  raw: ScenePromptRule | DisplayRegexRule
}

const REGEX_KIND_BUCKETS: ReadonlyArray<{
  kind: Exclude<UnifiedRuleKind, 'scene_prompt'>
  prefix: string
  fallbackLabel: string
  list: () => DisplayRegexRule[]
}> = [
  { kind: 'display', prefix: 'disp', fallbackLabel: '排版规则', list: () => session.displayRegexRules },
  { kind: 'output', prefix: 'out', fallbackLabel: '净化规则', list: () => session.outputRegexRules },
  { kind: 'prompt', prefix: 'proc', fallbackLabel: '上下文精简', list: () => session.promptRegexRules },
  { kind: 'input', prefix: 'in', fallbackLabel: '输入改写', list: () => session.inputRegexRules },
]

const unifiedRules = computed<UnifiedRuleRow[]>(() => {
  const out: UnifiedRuleRow[] = []
  session.sceneRules.forEach((r, idx) => {
    out.push({
      kind: 'scene_prompt',
      index: idx,
      id: r.id || `scene_${idx}`,
      name: r.name || `剧情规则 ${idx + 1}`,
      summary: r.summary || r.prompt,
      enabled: r.enabled !== false,
      origin: r.origin,
      raw: r,
    })
  })
  for (const bucket of REGEX_KIND_BUCKETS) {
    bucket.list().forEach((r, idx) => {
      out.push({
        kind: bucket.kind,
        index: idx,
        id: r.id || `${bucket.prefix}_${idx}`,
        name: r.name || `${bucket.fallbackLabel} ${idx + 1}`,
        summary: r.summary || '',
        enabled: r.enabled !== false,
        origin: r.origin,
        raw: r,
      })
    })
  }
  return out
})

const unexplainedCount = computed(
  () => unifiedRules.value.filter((r) => !r.summary.trim()).length,
)

function kindBadgeLabel(kind: UnifiedRuleKind): string {
  if (kind === 'scene_prompt') return isZh.value ? '剧情提示 · 0正则' : 'Scene Prompt'
  if (kind === 'display') return isZh.value ? '仅改显示' : 'Visual Display'
  if (kind === 'output') return isZh.value ? '净化回复' : 'Output Clean'
  if (kind === 'prompt') return isZh.value ? '仅洗上下文' : 'Prompt Context'
  return isZh.value ? '输入改写' : 'Input Rewrite'
}

/* ---- Mutations Persisted Immediately to Current Companion ---- */
async function persistRuleCollections(patch: {
  scene_rules?: ScenePromptRule[]
  display_regex?: DisplayRegexRule[]
  output_regex?: DisplayRegexRule[]
  prompt_regex?: DisplayRegexRule[]
  input_regex?: DisplayRegexRule[]
}) {
  if (!session.activeCompanionId) return
  saving.value = true
  errorMsg.value = ''
  try {
    await session.saveSceneRules(patch)
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

const REGEX_PAYLOAD_KEY: Record<
  Exclude<UnifiedRuleKind, 'scene_prompt'>,
  'display_regex' | 'output_regex' | 'prompt_regex' | 'input_regex'
> = {
  display: 'display_regex',
  output: 'output_regex',
  prompt: 'prompt_regex',
  input: 'input_regex',
}

function getRegexBucket(kind: Exclude<UnifiedRuleKind, 'scene_prompt'>): DisplayRegexRule[] {
  if (kind === 'display') return session.displayRegexRules
  if (kind === 'output') return session.outputRegexRules
  if (kind === 'prompt') return session.promptRegexRules
  return session.inputRegexRules
}

async function addCompiledRule(kind: UnifiedRuleKind, rule: ScenePromptRule | DisplayRegexRule) {
  if (kind === 'scene_prompt') {
    await persistRuleCollections({ scene_rules: [...session.sceneRules, rule as ScenePromptRule] })
    return
  }
  const key = REGEX_PAYLOAD_KEY[kind]
  await persistRuleCollections({ [key]: [...getRegexBucket(kind), rule as DisplayRegexRule] })
}

async function toggleUnifiedRule(row: UnifiedRuleRow) {
  const nextEnabled = !row.enabled
  if (row.kind === 'scene_prompt') {
    await persistRuleCollections({
      scene_rules: session.sceneRules.map((r, i) =>
        i === row.index ? { ...r, enabled: nextEnabled } : r,
      ),
    })
    return
  }
  const key = REGEX_PAYLOAD_KEY[row.kind]
  await persistRuleCollections({
    [key]: getRegexBucket(row.kind).map((r, i) =>
      i === row.index ? { ...r, enabled: nextEnabled } : r,
    ),
  })
}

async function deleteUnifiedRule(row: UnifiedRuleRow) {
  if (row.kind === 'scene_prompt') {
    await persistRuleCollections({
      scene_rules: session.sceneRules.filter((_, i) => i !== row.index),
    })
    return
  }
  const key = REGEX_PAYLOAD_KEY[row.kind]
  await persistRuleCollections({
    [key]: getRegexBucket(row.kind).filter((_, i) => i !== row.index),
  })
}

async function updateAdvancedRuleField(row: UnifiedRuleRow, patch: Record<string, unknown>) {
  if (row.kind === 'scene_prompt') {
    await persistRuleCollections({
      scene_rules: session.sceneRules.map((r, i) =>
        i === row.index ? ({ ...r, ...patch } as ScenePromptRule) : r,
      ),
    })
    return
  }
  const key = REGEX_PAYLOAD_KEY[row.kind]
  await persistRuleCollections({
    [key]: getRegexBucket(row.kind).map((r, i) =>
      i === row.index ? ({ ...r, ...patch } as DisplayRegexRule) : r,
    ),
  })
}

/* ---- AI Compile & Explain Actions ---- */
async function runAiCompile() {
  const cid = session.activeCompanionId
  const inst = nlInstruction.value.trim()
  if (!cid || !inst) return
  busyCompile.value = true
  errorMsg.value = ''
  statusMsg.value = ''
  try {
    const res = await compileSceneRule(cid, {
      instruction: inst,
      sample_text: sampleText.value,
      preferred_mode: nlPreferredMode.value,
      branch_id: session.activeBranch || 'main',
    })
    compileResult.value = res
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    busyCompile.value = false
  }
}

async function applyCompiledDraft() {
  const res = compileResult.value
  if (!res) return
  await addCompiledRule(res.rule_kind, res.rule)
  statusMsg.value = isZh.value ? `已将「${res.rule.name || '新规则'}」应用到当前角色` : 'Applied rule to current companion'
  nlInstruction.value = ''
  compileResult.value = null
  activeTab.value = 'rules'
}

async function explainImportedRules() {
  const cid = session.activeCompanionId
  const targets = unifiedRules.value.filter((r) => !r.summary.trim())
  if (!cid || !targets.length) return
  busyExplain.value = true
  errorMsg.value = ''
  try {
    const res = await explainSceneRules(
      cid,
      targets.map((r) => ({
        id: r.id,
        kind: r.kind,
        name: r.name,
        pattern: 'pattern' in r.raw ? r.raw.pattern : undefined,
        replace: 'replace' in r.raw ? r.raw.replace : undefined,
        condition_cbs: 'condition_cbs' in r.raw ? r.raw.condition_cbs : undefined,
        prompt: 'prompt' in r.raw ? r.raw.prompt : undefined,
      })),
      isZh.value ? 'zh' : 'en',
    )
    const map = new Map(res.explanations.map((e) => [e.id, e]))
    const nextScene = session.sceneRules.map((r, i) => {
      const ex = map.get(r.id || `scene_${i}`)
      return ex ? { ...r, name: ex.name || r.name, summary: ex.summary || r.summary } : r
    })
    const nextDisp = session.displayRegexRules.map((r, i) => {
      const ex = map.get(r.id || `disp_${i}`)
      return ex ? { ...r, name: ex.name || r.name, summary: ex.summary || r.summary } : r
    })
    const nextOut = session.outputRegexRules.map((r, i) => {
      const ex = map.get(r.id || `out_${i}`)
      return ex ? { ...r, name: ex.name || r.name, summary: ex.summary || r.summary } : r
    })
    const nextPrompt = session.promptRegexRules.map((r, i) => {
      const ex = map.get(r.id || `proc_${i}`)
      return ex ? { ...r, name: ex.name || r.name, summary: ex.summary || r.summary } : r
    })
    const nextIn = session.inputRegexRules.map((r, i) => {
      const ex = map.get(r.id || `in_${i}`)
      return ex ? { ...r, name: ex.name || r.name, summary: ex.summary || r.summary } : r
    })
    await persistRuleCollections({
      scene_rules: nextScene,
      display_regex: nextDisp,
      output_regex: nextOut,
      prompt_regex: nextPrompt,
      input_regex: nextIn,
    })
    statusMsg.value = isZh.value ? '已完成现有规则白话解读' : 'Translated rules into plain summaries'
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    busyExplain.value = false
  }
}

/* ---- Offline Block Builder ---- */
const offlineFormatPreviewRule = computed(() =>
  buildOfflineBlockRule({
    name: formatBlock.name,
    scope: formatBlock.scope,
    matchMode: formatBlock.matchMode,
    startToken: formatBlock.startToken,
    endToken: formatBlock.endToken,
    action: formatBlock.action,
    replaceText: formatBlock.replaceText,
  }),
)

const offlinePromptPreviewRule = computed(() =>
  buildOfflinePromptRule({
    name: promptBlock.name,
    conditionMode: promptBlock.conditionMode,
    threshold: promptBlock.threshold,
    nightStartHour: promptBlock.nightStartHour,
    nightEndHour: promptBlock.nightEndHour,
    hudField: promptBlock.hudField,
    hudMatch: promptBlock.hudMatch,
    placement: promptBlock.placement,
    prompt: promptBlock.prompt,
  }),
)

async function addOfflineBlock() {
  if (blockKind.value === 'format') {
    const rule = offlineFormatPreviewRule.value
    if (!rule) return
    await addCompiledRule(formatBlock.scope, rule)
    statusMsg.value = isZh.value ? `已添加格式规则「${rule.name}」` : 'Added formatting rule'
    formatBlock.name = ''
    activeTab.value = 'rules'
  } else {
    const rule = offlinePromptPreviewRule.value
    if (!rule) return
    await addCompiledRule('scene_prompt', rule)
    statusMsg.value = isZh.value ? `已添加剧情规则「${rule.name}」` : 'Added scene prompt rule'
    promptBlock.name = ''
    promptBlock.prompt = ''
    activeTab.value = 'rules'
  }
}

/* ---- Immersive Recipes Toggle ---- */
function isRecipeInstalled(recipe: ImmersiveRecipe): boolean {
  if (recipe.kind === 'scene_prompt') {
    return session.sceneRules.some((r) => r.recipe_id === recipe.id || r.id === recipe.id)
  }
  return getRegexBucket(recipe.kind).some((r) => r.recipe_id === recipe.id || r.id === recipe.id)
}

async function toggleRecipe(recipe: ImmersiveRecipe) {
  const installed = isRecipeInstalled(recipe)
  if (recipe.kind === 'scene_prompt') {
    const next = installed
      ? session.sceneRules.filter((r) => r.recipe_id !== recipe.id && r.id !== recipe.id)
      : [...session.sceneRules, { ...(recipe.rule as ScenePromptRule) }]
    await persistRuleCollections({ scene_rules: next })
    return
  }
  const key = REGEX_PAYLOAD_KEY[recipe.kind]
  const cur = getRegexBucket(recipe.kind)
  const next = installed
    ? cur.filter((r) => r.recipe_id !== recipe.id && r.id !== recipe.id)
    : [...cur, { ...(recipe.rule as DisplayRegexRule) }]
  await persistRuleCollections({ [key]: next })
}

/* ---- Branch Variables Quick Editor ---- */
async function saveVariablesNow() {
  saving.value = true
  errorMsg.value = ''
  try {
    await session.saveBranchVariables({ ...varDraft.value })
    statusMsg.value = isZh.value ? '宏观变量已保存，即时生效' : 'Variables saved'
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

async function addVariablePair() {
  const k = newVarKey.value.trim()
  if (!k) return
  varDraft.value = { ...varDraft.value, [k]: newVarVal.value }
  newVarKey.value = ''
  newVarVal.value = ''
  await saveVariablesNow()
}

async function removeVariableKey(key: string) {
  const copy = { ...varDraft.value }
  delete copy[key]
  varDraft.value = copy
  await saveVariablesNow()
}

async function resetVarsNow() {
  await session.resetBranchVariablesLocal()
  statusMsg.value = isZh.value ? '已重置为角色卡初始变量' : 'Reset variables to card defaults'
}

/* ---- Real RichMessage Preview Pipeline ---- */
const previewCbsOpts = computed<CbsOptions>(() => {
  const mins = Math.max(0, Math.floor(simIdleMinutes.value || 0))
  const dur = mins >= 60 ? `${Math.floor(mins / 60)}h ${mins % 60}m` : `${mins}m`
  return {
    charName: session.act,
    userName: session.userName,
    variables: varDraft.value,
    seed: `${session.activeCompanionId}:${session.activeBranch}`,
    assetNames: session.assetNames,
    chatIndex: simChatIndex.value,
    lastMessageId: simChatIndex.value,
    idleDuration: dur,
    idleMinutes: mins,
    hud: {
      ...cardHud.hud,
      affection: simAffection.value,
    },
  }
})

function renderSampleWithRules(
  rawSource: string,
  extraDisplayRules: DisplayRegexRule[] = [],
  extraOutputRules: DisplayRegexRule[] = [],
) {
  const source = rawSource || ''
  if (!source.trim()) return { cleanedText: '', assetUrls: {} as Record<number, string>, cardCss: '' }
  const outRules = [...session.outputRegexRules, ...extraOutputRules]
  const afterOut = applyDisplayRegex(source, outRules, { ...previewCbsOpts.value, placement: 2 })
  const afterCbs = evaluateCbs(afterOut, previewCbsOpts.value)
  const norm = normalizeAssetTagRefs(afterCbs, session.assetNames)
  const dispRules = [...session.displayRegexRules, ...extraDisplayRules]
  const afterDisp = applyDisplayRegex(norm, dispRules, { ...previewCbsOpts.value, placement: 2 })
  const media = extractInlineMediaFromText(afterDisp, (ref) => session.findAssetByRef(ref))
  return {
    cleanedText: media.cleanedText,
    mediaAssets: media.htmlImages,
  }
}

const liveCompiledPreview = computed(() => {
  const res = compileResult.value
  if (!res) return null
  if (res.rule_kind === 'scene_prompt') {
    return {
      isPrompt: true,
      text: res.preview_after || '',
    }
  }
  if (res.rule_kind === 'prompt' || res.rule_kind === 'input') {
    return {
      isPrompt: true,
      text: res.preview_after || sampleText.value,
    }
  }
  const r = res.rule as DisplayRegexRule
  const disp = res.rule_kind === 'display' ? [r] : []
  const outp = res.rule_kind === 'output' ? [r] : []
  const rendered = renderSampleWithRules(sampleText.value, disp, outp)
  return {
    isPrompt: false,
    text: rendered.cleanedText,
  }
})

const offlinePreviewRendered = computed(() => {
  if (blockKind.value === 'prompt') {
    const r = offlinePromptPreviewRule.value
    if (!r) return null
    const evalText = evaluateCbs(r.prompt, previewCbsOpts.value)
    const condEval = r.condition_cbs
      ? evaluateCbs(r.condition_cbs, previewCbsOpts.value).trim().toLowerCase()
      : '1'
    const active = Boolean(condEval && !['0', 'false', 'null', 'undefined', 'none', 'nan'].includes(condEval))
    return { isPrompt: true, active, text: evalText }
  }
  const r = offlineFormatPreviewRule.value
  if (!r) return null
  if (formatBlock.scope === 'prompt') {
    const afterPrompt = applyDisplayRegex(sampleText.value, [r], {
      ...previewCbsOpts.value,
      placement: 2,
    })
    return { isPrompt: true, active: true, text: afterPrompt }
  }
  const rendered = renderSampleWithRules(
    sampleText.value,
    formatBlock.scope === 'display' ? [r] : [],
    formatBlock.scope === 'output' ? [r] : [],
  )
  return { isPrompt: false, active: true, text: rendered.cleanedText }
})

const sandboxPreview = computed(() =>
  renderSampleWithRules(sampleText.value),
)

const resolvedCardCssInfo = computed(() =>
  rewriteCssAssetUrls(session.cardCss || '', (refText) => session.findAssetByRef(refText)),
)

const previewImageMap = useFileUrls(() => {
  if (!ui.sceneDockOpen) return []
  const assets = [
    ...(sandboxPreview.value.mediaAssets ?? []),
    ...resolvedCardCssInfo.value.assets,
  ]
  return assets.map((a) => a.rel_path)
})

const previewAssetUrls = computed<Record<number, string>>(() => {
  const out: Record<number, string> = {}
  const assets = [
    ...(sandboxPreview.value.mediaAssets ?? []),
    ...resolvedCardCssInfo.value.assets,
  ]
  for (const a of assets) {
    const u = previewImageMap.value[a.rel_path]
    if (u) out[a.id] = u
  }
  return out
})

const activePromptInjections = computed(() =>
  session.sceneRules
    .filter((r) => r.enabled !== false)
    .map((r) => {
      const condRaw = (r.condition_cbs || '').trim()
      const condRes = condRaw ? evaluateCbs(condRaw, previewCbsOpts.value).trim().toLowerCase() : '1'
      const triggered = Boolean(
        condRes && !['0', 'false', 'null', 'undefined', 'none', 'nan'].includes(condRes),
      )
      return {
        id: r.id,
        name: r.name,
        triggered,
        text: triggered ? evaluateCbs(r.prompt, previewCbsOpts.value) : '',
      }
    }),
)
</script>

<template>
  <section
    class="scene-dock-wrap"
    :class="{ open: ui.sceneDockOpen }"
    :aria-hidden="!ui.sceneDockOpen"
  >
    <div class="scene-dock-clip">
      <div class="scene-dock">
        <!-- Dock Header -->
        <header class="sd-head">
          <div class="sd-brand">
            <span class="sd-title">
              <svg viewBox="0 0 24 24" class="sd-ico">
                <path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9L12 3z" />
                <path d="M18.5 15.5l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9.9-2.1z" />
              </svg>
              {{ isZh ? '沉浸规则工坊' : 'Scene Rules' }}
            </span>
            <span class="sd-char-pill" :title="session.activeCompanionId">
              {{ session.act || '—' }}
            </span>
            <span class="sd-count-pill">
              {{ totalActiveRules }} {{ isZh ? '条生效' : 'active' }}
            </span>
          </div>

          <nav class="sd-tabs" role="tablist">
            <button
              type="button"
              class="sd-tab"
              :class="{ active: activeTab === 'create' }"
              @click="activeTab = 'create'"
            >
              {{ isZh ? '人话生成' : 'Create' }}
            </button>
            <button
              type="button"
              class="sd-tab"
              :class="{ active: activeTab === 'rules' }"
              @click="activeTab = 'rules'"
            >
              {{ isZh ? '当前规则与变量' : 'Rules & Vars' }}
              <span v-if="hasRulesConfigured" class="sd-tab-num">{{ unifiedRules.length }}</span>
            </button>
            <button
              type="button"
              class="sd-tab"
              :class="{ active: activeTab === 'recipes' }"
              @click="activeTab = 'recipes'"
            >
              {{ isZh ? '灵感配方 · 试跑' : 'Recipes & Test' }}
            </button>
          </nav>

          <div class="sd-actions">
            <button
              type="button"
              class="sd-link"
              :title="isZh ? '打开完整角色卡编辑器' : 'Open full character editor'"
              @click="ui.openPanel('profile')"
            >
              {{ isZh ? '完整角色卡' : 'Card Editor' }}
            </button>
            <button
              type="button"
              class="sd-close"
              :aria-label="isZh ? '收起规则工坊' : 'Close Scene Rules'"
              @click="ui.setSceneDockOpen(false)"
            >
              ×
            </button>
          </div>
        </header>

        <div v-if="errorMsg || statusMsg" class="sd-banner" :class="{ err: Boolean(errorMsg) }">
          <span>{{ errorMsg || statusMsg }}</span>
          <button type="button" class="sd-banner-x" @click="errorMsg = ''; statusMsg = ''">×</button>
        </div>

        <!-- Body -->
        <div class="sd-body">
          <!-- TAB 1: NATURAL LANGUAGE / OFFLINE BLOCK CREATOR -->
          <div v-if="activeTab === 'create'" class="sd-pane sd-create">
            <div class="sd-subswitch">
              <button
                type="button"
                class="sd-pill-btn"
                :class="{ active: createMode === 'nl' }"
                @click="createMode = 'nl'"
              >
                {{ isZh ? '自然语言描述（AI 编译 + 示例自检）' : 'Natural Language (AI)' }}
              </button>
              <button
                type="button"
                class="sd-pill-btn"
                :class="{ active: createMode === 'blocks' }"
                @click="createMode = 'blocks'"
              >
                {{ isZh ? '离线积木（免 AI 即配）' : 'Offline Blocks' }}
              </button>
            </div>

            <!-- Submode A: Natural Language Compiler -->
            <div v-if="createMode === 'nl'" class="sd-grid-2">
              <div class="sd-col">
                <label class="sd-label">
                  {{ isZh ? '用大白话告诉我想实现什么效果' : 'Describe what you want in plain language' }}
                </label>
                <textarea
                  v-model="nlInstruction"
                  class="sd-textarea"
                  rows="3"
                  :placeholder="
                    isZh
                      ? '例1：把【心声：...】改成浅灰斜体气泡并去掉方括号\n例2：超过2小时没聊时，让角色主动温柔提起好久不见'
                      : 'e.g. Render [thought: ...] as a soft italic card without brackets'
                  "
                ></textarea>

                <div class="sd-row-bar">
                  <div class="sd-select-wrap">
                    <UiSelect v-model="nlPreferredMode" :options="nlModeOptions" />
                  </div>
                  <button
                    type="button"
                    class="sd-primary"
                    :disabled="busyCompile || !nlInstruction.trim()"
                    @click="runAiCompile"
                  >
                    {{
                      busyCompile
                        ? isZh
                          ? '生成并试跑中…'
                          : 'Compiling…'
                        : isZh
                          ? '生成并试跑'
                          : 'Compile & Test'
                    }}
                  </button>
                </div>

                <div class="sd-sample-box">
                  <div class="sd-sample-head">
                    <span class="sd-mini-label">
                      {{ isZh ? '参考示例消息（用于自动验收是否命中）' : 'Sample message for auto-verification' }}
                    </span>
                    <div class="sd-mini-btns">
                      <button type="button" class="sd-mini" @click="useSampleFromLatestAi">
                        {{ isZh ? '抓取最新回复' : 'Latest AI' }}
                      </button>
                      <button type="button" class="sd-mini" @click="useSampleFromGreeting">
                        {{ isZh ? '抓取开场白' : 'Greeting' }}
                      </button>
                    </div>
                  </div>
                  <textarea
                    v-model="sampleText"
                    class="sd-textarea sd-textarea-sm"
                    rows="2"
                  ></textarea>
                </div>
              </div>

              <!-- Live Compiled Output Card -->
              <div class="sd-col sd-preview-pane">
                <div class="sd-preview-head">
                  <span>{{ isZh ? '编译结果与实时气泡预览' : 'Compiled Preview' }}</span>
                  <span v-if="compileResult?.matched_sample === true" class="sd-ok-badge">
                    {{ isZh ? '✓ 已命中示例' : '✓ Matched' }}
                  </span>
                  <span v-else-if="compileResult?.matched_sample === false" class="sd-warn-badge">
                    {{ isZh ? '未命中当前示例，请微调示例文本或重试' : 'No match on sample' }}
                  </span>
                </div>

                <div v-if="!compileResult" class="sd-empty-hint">
                  <p>
                    {{
                      isZh
                        ? '左侧用自然语言描述你想加的排版样式、去出戏清理，或者时间/轮数剧情触发；点击「生成并试跑」后，这里会立刻渲染真实效果给你确认。'
                        : 'Describe any formatting, cleanup, or time/turn behavior on the left and click Compile & Test.'
                    }}
                  </p>
                </div>

                <div v-else class="sd-compiled-card">
                  <div class="sd-rule-top">
                    <span class="sd-scope-tag">{{ kindBadgeLabel(compileResult.rule_kind) }}</span>
                    <strong>{{ compileResult.rule.name }}</strong>
                    <span v-if="compileResult.repaired" class="sd-repaired">
                      {{ isZh ? '已自动修正' : 'Auto-repaired' }}
                    </span>
                  </div>
                  <p class="sd-summary-text">{{ compileResult.rule.summary }}</p>

                  <div class="sd-bubble-preview">
                    <div v-if="liveCompiledPreview?.isPrompt" class="sd-prompt-preview">
                      <span class="sd-mini-label">{{ isZh ? '将注入给角色的指令：' : 'Injected prompt:' }}</span>
                      <p>{{ liveCompiledPreview.text }}</p>
                    </div>
                    <RichMessage
                      v-else-if="liveCompiledPreview"
                      :content="liveCompiledPreview.text"
                      :asset-urls="previewAssetUrls"
                      :card-css="resolvedCardCssInfo.css"
                      role="ai"
                    />
                  </div>

                  <div class="sd-apply-row">
                    <button type="button" class="sd-primary" :disabled="saving" @click="applyCompiledDraft">
                      {{ isZh ? '应用到当前角色' : 'Apply to Character' }}
                    </button>
                  </div>
                </div>
              </div>
            </div>

            <!-- Submode B: Offline Semantic Blocks -->
            <div v-else class="sd-grid-2">
              <div class="sd-col">
                <div class="sd-mini-btns" style="margin-bottom: 6px;">
                  <button
                    type="button"
                    class="sd-pill-btn"
                    :class="{ active: blockKind === 'format' }"
                    @click="blockKind = 'format'"
                  >
                    {{ isZh ? '格式气泡 / 净化块' : 'Format / Cleanup Block' }}
                  </button>
                  <button
                    type="button"
                    class="sd-pill-btn"
                    :class="{ active: blockKind === 'prompt' }"
                    @click="blockKind = 'prompt'"
                  >
                    {{ isZh ? '时间 / 轮数剧情块 (0正则)' : 'Story Condition Block' }}
                  </button>
                </div>

                <template v-if="blockKind === 'format'">
                  <div class="sd-form-grid">
                    <div class="sd-mini-label">
                      <span>{{ isZh ? '作用范围' : 'Scope' }}</span>
                      <UiSelect v-model="formatBlock.scope" :options="blockScopeOptions" />
                    </div>
                    <div class="sd-mini-label">
                      <span>{{ isZh ? '匹配方式' : 'Match Pattern' }}</span>
                      <UiSelect v-model="formatBlock.matchMode" :options="blockMatchOptions" />
                    </div>
                  </div>

                  <div class="sd-form-grid">
                    <label class="sd-mini-label">
                      {{ formatBlock.matchMode === 'delimited' ? (isZh ? '左标记' : 'Start') : (isZh ? '关键词/前缀' : 'Token') }}
                      <input v-model="formatBlock.startToken" class="sd-input" />
                    </label>
                    <label v-if="formatBlock.matchMode === 'delimited'" class="sd-mini-label">
                      {{ isZh ? '右标记' : 'End' }}
                      <input v-model="formatBlock.endToken" class="sd-input" />
                    </label>
                  </div>

                  <div class="sd-form-grid">
                    <div class="sd-mini-label">
                      <span>{{ isZh ? '展示效果' : 'Visual Effect' }}</span>
                      <UiSelect v-model="formatBlock.action" :options="blockActionOptions" />
                    </div>
                    <label v-if="formatBlock.action === 'replace_text'" class="sd-mini-label">
                      {{ isZh ? '替换为' : 'Replacement' }}
                      <input v-model="formatBlock.replaceText" class="sd-input" />
                    </label>
                    <label v-else class="sd-mini-label">
                      {{ isZh ? '规则名称（可选）' : 'Title (optional)' }}
                      <input v-model="formatBlock.name" class="sd-input" :placeholder="isZh ? '自动命名' : 'Auto'" />
                    </label>
                  </div>
                </template>

                <template v-else>
                  <div class="sd-form-grid">
                    <div class="sd-mini-label">
                      <span>{{ isZh ? '触发条件' : 'Trigger When' }}</span>
                      <UiSelect v-model="promptBlock.conditionMode" :options="promptConditionOptions" />
                    </div>
                    <label v-if="promptBlock.conditionMode === 'idle'" class="sd-mini-label">
                      {{ isZh ? '离开分钟数' : 'Minutes' }}
                      <input v-model.number="promptBlock.threshold" type="number" min="1" class="sd-input" />
                    </label>
                    <label v-else-if="promptBlock.conditionMode === 'chat_turn'" class="sd-mini-label">
                      {{ isZh ? '第几轮起' : 'From turn' }}
                      <input v-model.number="promptBlock.threshold" type="number" min="1" class="sd-input" />
                    </label>
                    <label v-else-if="promptBlock.conditionMode === 'hud_affection'" class="sd-mini-label">
                      {{ isZh ? '好感度达到 (0-100)' : 'Affection >=' }}
                      <input v-model.number="promptBlock.threshold" type="number" min="0" max="100" class="sd-input" />
                    </label>
                    <div v-else-if="promptBlock.conditionMode === 'hud_field'" class="sd-form-grid">
                      <div class="sd-mini-label">
                        <span>{{ isZh ? 'HUD 字段' : 'HUD Field' }}</span>
                        <UiSelect v-model="promptBlock.hudField" :options="hudFieldOptions" />
                      </div>
                      <label class="sd-mini-label">
                        {{ isZh ? '包含关键词' : 'Keyword' }}
                        <input
                          v-model="promptBlock.hudMatch"
                          class="sd-input"
                          :placeholder="isZh ? '如 雨夜 / 卧室 / 害羞' : 'e.g. rain / blush'"
                        />
                      </label>
                    </div>
                    <div v-else-if="promptBlock.conditionMode === 'time_window'" class="sd-form-grid">
                      <label class="sd-mini-label">
                        {{ isZh ? '起始小时' : 'From hr' }}
                        <input v-model.number="promptBlock.nightStartHour" type="number" min="0" max="23" class="sd-input" />
                      </label>
                      <label class="sd-mini-label">
                        {{ isZh ? '结束小时' : 'To hr' }}
                        <input v-model.number="promptBlock.nightEndHour" type="number" min="0" max="23" class="sd-input" />
                      </label>
                    </div>
                  </div>
                  <label class="sd-mini-label">
                    {{ isZh ? '触发后告诉角色的话（可自动感知角色名、时间、离开时长）' : 'Instruction sent to character when triggered' }}
                    <textarea
                      v-model="promptBlock.prompt"
                      class="sd-textarea"
                      rows="2"
                      :placeholder="isZh ? '例：已经过了好一会儿，开口时自然撒个娇或温柔问候一下。' : 'e.g. It has been a while; greet the user warmly.'"
                    ></textarea>
                  </label>
                </template>

                <div class="sd-apply-row">
                  <button
                    type="button"
                    class="sd-primary"
                    :disabled="saving || (blockKind === 'format' ? !offlineFormatPreviewRule : !offlinePromptPreviewRule)"
                    @click="addOfflineBlock"
                  >
                    {{ isZh ? '一键添加规则' : 'Add Block Rule' }}
                  </button>
                </div>
              </div>

              <div class="sd-col sd-preview-pane">
                <div class="sd-preview-head">
                  <span>{{ isZh ? '离线积木即时预览' : 'Instant Block Preview' }}</span>
                </div>
                <div class="sd-bubble-preview">
                  <div v-if="offlinePreviewRendered?.isPrompt" class="sd-prompt-preview">
                    <span class="sd-mini-label">
                      {{ offlinePreviewRendered.active ? (isZh ? '当前模拟状态下会触发：' : 'Active now:') : (isZh ? '当前条件未满足（会在满足时生效）：' : 'Standby until triggered:') }}
                    </span>
                    <p>{{ offlinePreviewRendered.text }}</p>
                  </div>
                  <RichMessage
                    v-else-if="offlinePreviewRendered"
                    :content="offlinePreviewRendered.text"
                    :asset-urls="previewAssetUrls"
                    :card-css="resolvedCardCssInfo.css"
                    role="ai"
                  />
                  <div v-else class="sd-empty-hint">{{ isZh ? '填写左侧积木即可预览效果' : 'Fill fields to preview' }}</div>
                </div>
              </div>
            </div>
          </div>

          <!-- TAB 2: ACTIVE RULES & CARD MACRO VARIABLES -->
          <div v-else-if="activeTab === 'rules'" class="sd-pane sd-rules-pane">
            <!-- Live Context Telemetry Strip -->
            <div class="sd-telemetry">
              <span class="sd-tel-item"><code v-pre>{{chat_index}}</code> = {{ simChatIndex }}</span>
              <span class="sd-tel-item"><code v-pre>{{time}}</code> = {{ currentTimeClock }}</span>
              <span class="sd-tel-item"><code v-pre>{{assetlist}}</code> = {{ session.assetNames.length }} {{ isZh ? '项' : 'items' }}</span>
              <span v-if="cardHud.affectionValue !== null" class="sd-tel-item">
                <code v-pre>{{hud_affection}}</code> = {{ cardHud.affectionValue }}
              </span>
              <span v-if="cardHud.moodText" class="sd-tel-item">
                <code v-pre>{{hud_mood}}</code> = {{ cardHud.moodText }}
              </span>
              <button
                v-if="unexplainedCount > 0"
                type="button"
                class="sd-mini sd-ai-btn"
                :disabled="busyExplain"
                @click="explainImportedRules"
              >
                {{
                  busyExplain
                    ? isZh ? 'AI 解读中…' : 'Explaining…'
                    : isZh ? `AI 解读 ${unexplainedCount} 条规则` : `Explain ${unexplainedCount} rules`
                }}
              </button>
            </div>

            <div class="sd-split-rules">
              <!-- Unified Rule Cards -->
              <div class="sd-rule-list">
                <div v-if="!unifiedRules.length" class="sd-empty-hint">
                  <p>
                    {{
                      isZh
                        ? '当前角色还没有沉浸规则。切到「人话生成」说一句你想改的效果，或到「灵感配方」一键启用模板。'
                        : 'No rules yet for this character. Use Create or Recipes to add one effortlessly.'
                    }}
                  </p>
                </div>

                <article
                  v-for="row in unifiedRules"
                  :key="`${row.kind}-${row.id}-${row.index}`"
                  class="sd-rule-card"
                  :class="{ off: !row.enabled }"
                >
                  <div class="sd-rule-row">
                    <label class="sd-check">
                      <input
                        class="sd-check-input"
                        type="checkbox"
                        :checked="row.enabled"
                        @change="toggleUnifiedRule(row)"
                      />
                      <span class="sd-check-box" :class="{ on: row.enabled }" aria-hidden="true">
                        <svg viewBox="0 0 16 16"><path d="M3.6 8.4l2.7 2.7 6.1-6.4" /></svg>
                      </span>
                      <span class="sd-rule-name">{{ row.name }}</span>
                    </label>
                    <span class="sd-scope-tag">{{ kindBadgeLabel(row.kind) }}</span>
                    <button
                      type="button"
                      class="sd-mini"
                      @click="expandedCodeIds[row.id] = !expandedCodeIds[row.id]"
                    >
                      {{ expandedCodeIds[row.id] ? (isZh ? '收起源码' : 'Hide') : (isZh ? '源码' : 'Code') }}
                    </button>
                    <button
                      type="button"
                      class="sd-del"
                      :aria-label="isZh ? '删除规则' : 'Delete rule'"
                      @click="deleteUnifiedRule(row)"
                    >
                      ×
                    </button>
                  </div>
                  <p v-if="row.summary" class="sd-rule-desc">{{ row.summary }}</p>
                  <p v-else class="sd-rule-desc muted">
                    {{ isZh ? '来自角色卡导入规则（可点右上角 AI 解读成大白话）' : 'Imported rule (click AI Explain to summarize)' }}
                  </p>

                  <!-- Collapsed Technical Source Drawer -->
                  <div v-if="expandedCodeIds[row.id]" class="sd-code-drawer">
                    <template v-if="row.kind === 'scene_prompt'">
                      <label class="sd-mini-label">
                        condition_cbs
                        <input
                          class="sd-input mono"
                          :value="(row.raw as ScenePromptRule).condition_cbs"
                          @change="updateAdvancedRuleField(row, { condition_cbs: ($event.target as HTMLInputElement).value })"
                        />
                      </label>
                      <label class="sd-mini-label">
                        prompt
                        <textarea
                          class="sd-textarea sd-textarea-sm mono"
                          rows="2"
                          :value="(row.raw as ScenePromptRule).prompt"
                          @change="updateAdvancedRuleField(row, { prompt: ($event.target as HTMLTextAreaElement).value })"
                        ></textarea>
                      </label>
                    </template>
                    <template v-else>
                      <label class="sd-mini-label">
                        pattern / flags
                        <div class="sd-row-bar">
                          <input
                            class="sd-input mono"
                            :value="(row.raw as DisplayRegexRule).pattern"
                            @change="updateAdvancedRuleField(row, { pattern: ($event.target as HTMLInputElement).value })"
                          />
                          <input
                            class="sd-input mono"
                            style="width: 58px; flex: none;"
                            :value="(row.raw as DisplayRegexRule).flags || 'g'"
                            @change="updateAdvancedRuleField(row, { flags: ($event.target as HTMLInputElement).value })"
                          />
                        </div>
                      </label>
                      <label class="sd-mini-label">
                        replace
                        <textarea
                          class="sd-textarea sd-textarea-sm mono"
                          rows="2"
                          :value="(row.raw as DisplayRegexRule).replace"
                          @change="updateAdvancedRuleField(row, { replace: ($event.target as HTMLTextAreaElement).value })"
                        ></textarea>
                      </label>
                    </template>
                  </div>
                </article>
              </div>

              <!-- Branch Macro Variables Quick Editor -->
              <aside class="sd-vars-box">
                <div class="sd-sample-head">
                  <span class="sd-mini-label">{{ isZh ? '角色宏观变量 (CBS)' : 'Macro Variables' }}</span>
                  <button type="button" class="sd-mini" @click="resetVarsNow">
                    {{ isZh ? '重置默认' : 'Reset' }}
                  </button>
                </div>
                <div v-if="!Object.keys(varDraft).length" class="sd-var-empty">
                  {{ isZh ? '暂无变量（可直接新增键值对，如 心情 = 开心）' : 'No variables set yet' }}
                </div>
                <div v-else class="sd-var-list">
                  <div v-for="(_, k) in varDraft" :key="String(k)" class="sd-var-row">
                    <span class="sd-var-k" :title="String(k)">{{ k }}</span>
                    <input
                      v-model="varDraft[k]"
                      class="sd-input sd-var-v"
                      @change="saveVariablesNow"
                    />
                    <button
                      type="button"
                      class="sd-pin"
                      :class="{ pinned: cardHud.isVarPinned(String(k)) }"
                      :title="
                        cardHud.isVarPinned(String(k))
                          ? isZh ? '已钉选到 Stage HUD（点击取消）' : 'Pinned to Stage HUD (click to unpin)'
                          : isZh ? '钉选到 Stage HUD 实时追踪' : 'Pin to Stage HUD'
                      "
                      @click="cardHud.togglePinVar(String(k))"
                    >
                      <svg viewBox="0 0 16 16" aria-hidden="true">
                        <path d="M5.5 2.5h5l-.8 3.6 2 2v1.2H8.6v4.2L8 14.5l-.6-1V9.3H4.3V8.1l2-2z" />
                      </svg>
                    </button>
                    <button type="button" class="sd-del" @click="removeVariableKey(String(k))">×</button>
                  </div>
                </div>
                <div class="sd-var-add">
                  <input v-model="newVarKey" class="sd-input" :placeholder="isZh ? '变量名' : 'Key'" />
                  <input v-model="newVarVal" class="sd-input" :placeholder="isZh ? '值' : 'Value'" />
                  <button type="button" class="sd-mini" @click="addVariablePair">+</button>
                </div>
              </aside>
            </div>
          </div>

          <!-- TAB 3: IMMERSIVE RECIPES & LIVE PLAYGROUND -->
          <div v-else class="sd-pane sd-recipes-pane">
            <div class="sd-grid-2">
              <div class="sd-col sd-recipe-list">
                <article
                  v-for="recipe in IMMERSIVE_RECIPES"
                  :key="recipe.id"
                  class="sd-recipe-card"
                  :class="{ on: isRecipeInstalled(recipe) }"
                >
                  <div class="sd-recipe-top">
                    <div>
                      <strong>{{ isZh ? recipe.titleZh : recipe.titleEn }}</strong>
                      <span class="sd-scope-tag" style="margin-left: 6px;">
                        {{ kindBadgeLabel(recipe.kind) }}
                      </span>
                    </div>
                    <button
                      type="button"
                      class="sd-pill-btn"
                      :class="{ active: isRecipeInstalled(recipe) }"
                      :disabled="saving"
                      @click="toggleRecipe(recipe)"
                    >
                      {{ isRecipeInstalled(recipe) ? (isZh ? '✓ 已启用' : '✓ Enabled') : (isZh ? '+ 一键启用' : '+ Enable') }}
                    </button>
                  </div>
                  <p class="sd-rule-desc">{{ isZh ? recipe.descZh : recipe.descEn }}</p>
                </article>
              </div>

              <!-- Live Test Playground -->
              <div class="sd-col sd-preview-pane">
                <div class="sd-preview-head">
                  <span>{{ isZh ? '角色即时试跑台' : 'Live Sandbox' }}</span>
                  <div class="sd-mini-btns">
                    <button type="button" class="sd-mini" @click="useSampleFromLatestAi">
                      {{ isZh ? '最新回复' : 'Latest AI' }}
                    </button>
                    <button type="button" class="sd-mini" @click="useSampleFromGreeting">
                      {{ isZh ? '开场白' : 'Greeting' }}
                    </button>
                  </div>
                </div>
                <div class="sd-sim-sliders">
                  <label class="sd-mini-label">
                    {{ isZh ? `轮数 ${simChatIndex}` : `Turn ${simChatIndex}` }}
                    <input v-model.number="simChatIndex" type="range" min="0" max="60" />
                  </label>
                  <label class="sd-mini-label">
                    {{ isZh ? `离线 ${simIdleMinutes}m` : `Idle ${simIdleMinutes}m` }}
                    <input v-model.number="simIdleMinutes" type="range" min="0" max="360" step="15" />
                  </label>
                  <label class="sd-mini-label">
                    {{ isZh ? `好感 ${simAffection}` : `Affection ${simAffection}` }}
                    <input v-model.number="simAffection" type="range" min="0" max="100" />
                  </label>
                </div>
                <textarea v-model="sampleText" class="sd-textarea sd-textarea-sm" rows="2"></textarea>

                <div v-if="activePromptInjections.length" class="sd-prompt-chips">
                  <span
                    v-for="inj in activePromptInjections"
                    :key="inj.id"
                    class="sd-inj-pill"
                    :class="{ live: inj.triggered }"
                    :title="inj.text || (isZh ? '当前未触发' : 'Not triggered yet')"
                  >
                    {{ inj.triggered ? '●' : '○' }} {{ inj.name }}
                  </span>
                </div>

                <div class="sd-bubble-preview">
                  <RichMessage
                    :content="sandboxPreview.cleanedText"
                    :asset-urls="previewAssetUrls"
                    :card-css="resolvedCardCssInfo.css"
                    role="ai"
                  />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

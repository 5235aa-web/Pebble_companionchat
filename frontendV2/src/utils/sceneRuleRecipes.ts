/**
 * Immersive Scene Rule recipes & offline zero-regex semantic block builders.
 *
 * Lets users create rich visual formatting, output cleanup, and context-aware
 * storytelling rules without writing regular expressions by hand.
 */

import type { DisplayRegexRule, ScenePromptRule } from './displayRegex'

export type RecipeRuleKind = 'scene_prompt' | 'display' | 'output' | 'prompt'

export interface ImmersiveRecipe {
  id: string
  category: 'context' | 'visual' | 'cleanup'
  kind: RecipeRuleKind
  titleZh: string
  titleEn: string
  descZh: string
  descEn: string
  previewSample: string
  rule: ScenePromptRule | DisplayRegexRule
}

export const IMMERSIVE_RECIPES: ImmersiveRecipe[] = [
  {
    id: 'recipe_idle_reunion',
    category: 'context',
    kind: 'scene_prompt',
    titleZh: '久别重逢 · 时间流逝感',
    titleEn: 'Idle Time Awareness',
    descZh: '离开超过 2 小时后再开口时，提醒角色自然感知时间差（不写正则）。',
    descEn: 'When idle >= 120m, subtly hint the character to notice how long you were away.',
    previewSample: '{{idle_duration}}',
    rule: {
      id: 'recipe_idle_reunion',
      name: '久别重逢时间感知',
      summary: '超过 2 小时未对话时，提醒角色自然流露久别重逢的感觉',
      enabled: true,
      trigger_type: 'idle',
      condition_cbs: '{{greater_equal::{{idle_minutes}}::120}}',
      prompt:
        'It has been {{idle_duration}} since {{user}} last spoke with you. Naturally acknowledge the passage of time with warmth or playful teasing—without breaking character or sounding robotic.',
      placement: 'system',
      origin: 'recipe',
      recipe_id: 'recipe_idle_reunion',
    },
  },
  {
    id: 'recipe_late_night_mood',
    category: 'context',
    kind: 'scene_prompt',
    titleZh: '深夜静谧 · 时段氛围',
    titleEn: 'Late-Night Atmosphere',
    descZh: '每晚 22:00–05:00 自动为角色注入更轻柔、亲密的夜谈叙事氛围。',
    descEn: 'Between 22:00 and 05:00 local time, nudge responses toward softer late-night intimacy.',
    previewSample: '{{time}}',
    rule: {
      id: 'recipe_late_night_mood',
      name: '深夜静谧氛围',
      summary: '22:00 到次日 05:00 自动调整为轻声夜谈语境',
      enabled: true,
      trigger_type: 'time_window',
      condition_cbs: '{{or::{{greater_equal::{{hour}}::22}}::{{less::{{hour}}::5}}}}',
      prompt:
        'Current local time is {{time}}. Keep the pacing unhurried, softer, and slightly more intimate to match the quiet late-night atmosphere.',
      placement: 'system',
      origin: 'recipe',
      recipe_id: 'recipe_late_night_mood',
    },
  },
  {
    id: 'recipe_bond_milestone',
    category: 'context',
    kind: 'scene_prompt',
    titleZh: '关系升温 · 轮数阶段成长',
    titleEn: 'Relationship Progression',
    descZh: '对话达到第 20 轮后，让角色在互动中显得更加熟稔放松。',
    descEn: 'After 20 turns of conversation, soften formalities and increase familiarity.',
    previewSample: 'Turn {{chat_index}}',
    rule: {
      id: 'recipe_bond_milestone',
      name: '长对话熟稔阶段',
      summary: '聊天满 20 轮后，角色流露更高的熟悉感与默契',
      enabled: true,
      trigger_type: 'chat_turn',
      condition_cbs: '{{greater_equal::{{chat_index}}::20}}',
      prompt:
        'You and {{user}} have already shared {{chat_index}} turns together. Speak with the relaxed familiarity, inside-joke comfort, and ease of someone who genuinely knows {{user}} well.',
      placement: 'system',
      origin: 'recipe',
      recipe_id: 'recipe_bond_milestone',
    },
  },
  {
    id: 'recipe_asset_guard',
    category: 'context',
    kind: 'scene_prompt',
    titleZh: '表情/立绘防出错护栏',
    titleEn: 'Asset Name Guardrail',
    descZh: '把可用资产列表注入提示词，避免 AI 编造不存在的立绘名导致报错。',
    descEn: 'Shows the model your bundled asset list so it only picks valid expressions/media.',
    previewSample: '{{assetlist}}',
    rule: {
      id: 'recipe_asset_guard',
      name: '立绘资产名称防错',
      summary: '将当前可用资产名列表提供给角色，减少错写或漏图',
      enabled: true,
      trigger_type: 'always',
      condition_cbs: '{{not_equal::{{assetlist}}::[]}}',
      prompt:
        'When using bundled expression/media tags, ONLY choose exact names from this available list: {{assetlist}}.',
      placement: 'post_history',
      origin: 'recipe',
      recipe_id: 'recipe_asset_guard',
    },
  },
  {
    id: 'recipe_hud_affection_warmth',
    category: 'context',
    kind: 'scene_prompt',
    titleZh: '好感度联动 · 心动升温',
    titleEn: 'HUD Affection Progression',
    descZh: '当 Stage HUD 好感度达到 65 以上时，角色自动流露更明显的亲昵与偏爱（免写正则）。',
    descEn: 'When Stage HUD affection >= 65, naturally soften distance and deepen warmth.',
    previewSample: '♥ {{hud_affection}} · {{hud_mood}}',
    rule: {
      id: 'recipe_hud_affection_warmth',
      name: '好感度升温联动',
      summary: 'HUD 好感度 ≥ 65 时，让角色自然表达更多偏爱与亲昵',
      enabled: true,
      trigger_type: 'variable',
      condition_cbs: '{{greater_equal::{{hud_affection}}::65}}',
      prompt:
        'Live relationship affection is {{hud_affection}}/100 and current mood is {{hud_mood}}. Let genuine closeness, lingering warmth, and quiet fondness show naturally in your wording and body language.',
      placement: 'system',
      origin: 'recipe',
      recipe_id: 'recipe_hud_affection_warmth',
    },
  },
  {
    id: 'recipe_hud_scene_presence',
    category: 'context',
    kind: 'scene_prompt',
    titleZh: '场景着装 · 在场细节呼应',
    titleEn: 'HUD Scene & Outfit Grounding',
    descZh: '把 HUD 当前追踪的地点与着装融入动作细节里，让每次互动更贴合此时此地。',
    descEn: 'Weaves live HUD location and outfit into sensory details so scenes stay grounded.',
    previewSample: '{{hud_location}} · {{hud_outfit}}',
    rule: {
      id: 'recipe_hud_scene_presence',
      name: '当前地点与着装在场感',
      summary: '在对话动作中自然呼应 HUD 的当前地点与着装',
      enabled: true,
      trigger_type: 'always',
      condition_cbs: '{{or::{{hud_location}}::{{hud_outfit}}}}',
      prompt:
        'Keep physical details grounded in the active scene: location is "{{hud_location}}", outfit is "{{hud_outfit}}". Reference lighting, posture, or clothing textures subtly when fitting.',
      placement: 'system',
      origin: 'recipe',
      recipe_id: 'recipe_hud_scene_presence',
    },
  },
  {
    id: 'recipe_inner_thought_card',
    category: 'visual',
    kind: 'display',
    titleZh: '心声 / 内心戏卡片化',
    titleEn: 'Inner Thought Aside Card',
    descZh: '把【心声：...】或 [心声: ...] 自动渲染为柔和的斜体心声气泡，并隐藏方括号。',
    descEn: 'Turns 【心声：...】 / [心声: ...] / [thought: ...] into a soft italic aside card.',
    previewSample: '她笑了笑，低头搅动着杯子里的咖啡。\n【心声：其实刚才等了你好久呢。】',
    rule: {
      id: 'recipe_inner_thought_card',
      name: '心声斜体气泡',
      summary: '将【心声：...】或 [thought: ...] 渲染为柔和斜体心声卡片',
      origin: 'recipe',
      recipe_id: 'recipe_inner_thought_card',
      pattern: '(?:【|\\[)\\s*(?:心声|内心|thought)\\s*[:：]\\s*([\\s\\S]*?)\\s*(?:】|\\])',
      flags: 'gi',
      replace:
        '<div style="opacity:0.82;font-style:italic;padding:6px 11px;border-left:2px solid currentColor;background:rgba(255,255,255,0.04);border-radius:0 6px 6px 0;margin:6px 0;">$1</div>',
      placement: [1, 2],
      enabled: true,
    },
  },
  {
    id: 'recipe_status_badge',
    category: 'visual',
    kind: 'display',
    titleZh: '状态 / 情绪微标签',
    titleEn: 'Mood & Status Pill',
    descZh: '把 [情绪: 开心] 或 【状态：微醺】 转换成精致的小胶囊徽章。',
    descEn: 'Turns [情绪: ...] / 【状态：...】 / [mood: ...] into an inline pill badge.',
    previewSample: '【情绪：有点小开心】“今天怎么突然想到来找我啦？”',
    rule: {
      id: 'recipe_status_badge',
      name: '情绪状态微标签',
      summary: '将【情绪/状态：...】美化成精致的圆角徽章',
      origin: 'recipe',
      recipe_id: 'recipe_status_badge',
      pattern: '(?:【|\\[)\\s*(?:情绪|状态|心情|mood|status)\\s*[:：]\\s*([^\\]】\\n]+)\\s*(?:】|\\])',
      flags: 'gi',
      replace:
        '<span style="display:inline-block;padding:2px 9px;border-radius:999px;font-size:0.82em;background:rgba(224,163,94,0.16);border:1px solid rgba(224,163,94,0.36);margin:2px 6px 2px 0;">$1</span>',
      placement: [1, 2],
      enabled: true,
    },
  },
  {
    id: 'recipe_strip_ooc',
    category: 'cleanup',
    kind: 'output',
    titleZh: '净化出戏 OOC 旁注',
    titleEn: 'Strip AI OOC Notes',
    descZh: '自动删掉 AI 回复末尾多余的 （注：...） 或 [OOC: ...] 解释性文字。',
    descEn: 'Removes trailing (注：...) or [OOC: ...] meta commentary before saving replies.',
    previewSample: '“明天见哦。”\n（注：以上对话符合角色温柔体贴的性格设定。）',
    rule: {
      id: 'recipe_strip_ooc',
      name: '自动清理 OOC 注释',
      summary: '在保存回复前移除（注：...）与 [OOC: ...] 等出戏文字',
      origin: 'recipe',
      recipe_id: 'recipe_strip_ooc',
      pattern: '(?:\\r?\\n)*(?:（|\\(|\\[|【)\\s*(?:注|附注|OOC|System\\s*Note|提示)\\s*[:：][\\s\\S]*?(?:）|\\)|\\]|】)\\s*$',
      flags: 'gi',
      replace: '',
      placement: [2],
      enabled: true,
    },
  },
  {
    id: 'recipe_prompt_strip_status',
    category: 'cleanup',
    kind: 'prompt',
    titleZh: '发给 AI 前剥离历史 HTML/思考块',
    titleEn: 'Strip Old HTML/Think Before AI',
    descZh: '屏幕上照常保留样式和思考/折叠块，仅在较早历史发给 AI 时剔除冗余标签，省 Token 且防模型复读标签。',
    descEn: 'Keeps rich HTML/details on screen, but strips them from older turns (depth >= 2) before sending to the AI.',
    previewSample: '<details><summary>状态</summary>心情: 平静</details>\n“今晚风好大。”',
    rule: {
      id: 'recipe_prompt_strip_status',
      name: '省Token精简历史上下文',
      summary: '仅在发给 AI 的较早历史（深度≥2）中剥离 <details>/<style>/<think> 等标签，界面照常显示',
      origin: 'recipe',
      recipe_id: 'recipe_prompt_strip_status',
      pattern: '<(?:details|style|think)\\b[^>]*>[\\s\\S]*?<\\/(?:details|style|think)>',
      flags: 'gi',
      replace: '',
      placement: [1, 2],
      min_depth: 2,
      enabled: true,
    },
  },
]

export type BlockMatchMode = 'delimited' | 'line_prefix' | 'kv_line' | 'literal'
export type BlockVisualAction = 'hide' | 'aside' | 'card' | 'badge' | 'replace_text'
export type BlockRuleScope = 'display' | 'output' | 'prompt'

export interface OfflineBlockInput {
  name: string
  scope: BlockRuleScope
  matchMode: BlockMatchMode
  startToken: string
  endToken: string
  action: BlockVisualAction
  replaceText?: string
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * Deterministically builds a safe Regex+HTML Display/Output rule from plain
 * visual block choices — works 100% offline with zero LLM or manual regex.
 */
export function buildOfflineBlockRule(input: OfflineBlockInput): DisplayRegexRule | null {
  const start = input.startToken.trim()
  const end = input.endToken.trim()
  if (!start) return null

  let pattern = ''
  let flags = 'g'
  let matchDesc = ''

  if (input.matchMode === 'delimited') {
    const close = end || start
    pattern = `${escapeRegex(start)}\\s*([\\s\\S]*?)\\s*${escapeRegex(close)}`
    flags = 'gi'
    matchDesc = `${start}...${close}`
  } else if (input.matchMode === 'line_prefix') {
    pattern = `^${escapeRegex(start)}(.*)$`
    flags = 'gim'
    matchDesc = `以「${start}」开头的行`
  } else if (input.matchMode === 'kv_line') {
    pattern = `^${escapeRegex(start)}\\s*[:：]\\s*(.+)$`
    flags = 'gim'
    matchDesc = `「${start}：值」属性行`
  } else {
    pattern = `(${escapeRegex(start)})`
    flags = 'g'
    matchDesc = `「${start}」`
  }

  let replace = ''
  let actionDesc = ''
  if (input.action === 'hide') {
    replace = ''
    actionDesc = '隐藏原文'
  } else if (input.action === 'aside') {
    replace =
      '<div style="opacity:0.82;font-style:italic;padding:5px 10px;border-left:2px solid currentColor;background:rgba(255,255,255,0.04);border-radius:0 6px 6px 0;margin:5px 0;">$1</div>'
    actionDesc = '转为斜体旁白条'
  } else if (input.action === 'card') {
    replace =
      '<div style="padding:8px 12px;border-radius:8px;background:rgba(255,255,255,0.05);border:1px solid rgba(160,150,130,0.22);margin:6px 0;">$1</div>'
    actionDesc = '装入圆角卡片'
  } else if (input.action === 'badge') {
    replace =
      '<span style="display:inline-block;padding:2px 9px;border-radius:999px;font-size:0.82em;background:rgba(224,163,94,0.16);border:1px solid rgba(224,163,94,0.36);margin:2px 6px 2px 0;">$1</span>'
    actionDesc = '变为徽章标签'
  } else {
    replace = input.replaceText ?? ''
    actionDesc = replace ? `替换为「${replace}」` : '清空替换'
  }

  const title = input.name.trim() || `${matchDesc} → ${actionDesc}`
  return {
    id: `blk_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`,
    name: title.slice(0, 32),
    summary: `匹配 ${matchDesc}，${actionDesc}`,
    origin: 'builder',
    pattern,
    flags,
    replace,
    placement: input.scope === 'output' ? [2] : [1, 2],
    enabled: true,
  }
}

export type PromptConditionMode =
  | 'always'
  | 'idle'
  | 'chat_turn'
  | 'time_window'
  | 'hud_affection'
  | 'hud_field'

export interface OfflinePromptBlockInput {
  name: string
  conditionMode: PromptConditionMode
  threshold: number
  nightStartHour?: number
  nightEndHour?: number
  hudField?: 'location' | 'mood' | 'outfit'
  hudMatch?: string
  placement: 'system' | 'post_history'
  prompt: string
}

/** Build a zero-regex contextual Scene Prompt Rule from visual presets. */
export function buildOfflinePromptRule(input: OfflinePromptBlockInput): ScenePromptRule | null {
  const body = input.prompt.trim()
  if (!body) return null

  let trigger_type: ScenePromptRule['trigger_type'] = 'always'
  let condition_cbs = ''
  let condSummary = '每轮生效'

  if (input.conditionMode === 'idle') {
    const mins = Math.max(1, Math.round(input.threshold || 60))
    trigger_type = 'idle'
    condition_cbs = `{{greater_equal::{{idle_minutes}}::${mins}}}`
    condSummary = `离开 ≥ ${mins} 分钟触发`
  } else if (input.conditionMode === 'chat_turn') {
    const turn = Math.max(1, Math.round(input.threshold || 10))
    trigger_type = 'chat_turn'
    condition_cbs = `{{greater_equal::{{chat_index}}::${turn}}}`
    condSummary = `聊满 ${turn} 轮后触发`
  } else if (input.conditionMode === 'time_window') {
    const startH = Math.max(0, Math.min(23, Math.round(input.nightStartHour ?? 22)))
    const endH = Math.max(0, Math.min(23, Math.round(input.nightEndHour ?? 5)))
    trigger_type = 'time_window'
    if (startH > endH) {
      condition_cbs = `{{or::{{greater_equal::{{hour}}::${startH}}}::{{less::{{hour}}::${endH}}}}}`
    } else {
      condition_cbs = `{{and::{{greater_equal::{{hour}}::${startH}}}::{{less::{{hour}}::${endH}}}}}`
    }
    condSummary = `${String(startH).padStart(2, '0')}:00–${String(endH).padStart(2, '0')}:00 时段触发`
  } else if (input.conditionMode === 'hud_affection') {
    const aff = Math.max(0, Math.min(100, Math.round(input.threshold ?? 60)))
    trigger_type = 'variable'
    condition_cbs = `{{greater_equal::{{hud_affection}}::${aff}}}`
    condSummary = `HUD 好感度 ≥ ${aff} 触发`
  } else if (input.conditionMode === 'hud_field') {
    const field = input.hudField || 'location'
    const kw = (input.hudMatch || '').trim()
    if (!kw) return null
    trigger_type = 'variable'
    condition_cbs = `{{contains::{{lower::{{hud_${field}}}}}::${kw.toLowerCase()}}}`
    const fieldLabel = field === 'mood' ? '心情' : field === 'outfit' ? '着装' : '地点'
    condSummary = `HUD ${fieldLabel}含「${kw}」触发`
  }

  return {
    id: `pr_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`,
    name: input.name.trim() || condSummary,
    summary: `${condSummary}：${body.slice(0, 48)}`,
    enabled: true,
    trigger_type,
    condition_cbs,
    prompt: body,
    placement: input.placement,
    origin: 'builder',
  }
}

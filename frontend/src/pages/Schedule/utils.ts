// ═══ 赛程列表页纯工具函数 (2026-08-31 自 index.tsx 拆分, 无 React 依赖) ═══
import { MAX_MIN } from './types'

// ═══ 2026-08-29 复原(方案A) 适配层 ═══
// /api/terminal/analyze 返回的是 _live_predict 全链路结果 (方向/OIP比分/OU/决策同响应),
// 与重建后接的 /api/live-goal-probe/analyze (score_hint) 字段不同。此处做单一适配,
// 把前者映射成 score_hint 形状, 使渲染层零改动。
//   · 比分 ← oip.top3_scores (OIP 泊松比分模型)
//   · 方向 ← direction (反抽水取赔率 argmax, 一级信号)
//   · 决策 ← decision / decision_text (价值层 EV 判定, PASS=全方向负EV)
export function adaptLivePredict(raw: any, currentScore?: string): any {
  if (!raw || raw.error) return null
  const norm = (s: any) => String(s ?? '').replace(':', '-')
  const dirOf = (s: any): 'home' | 'draw' | 'away' | null => {
    const p = norm(s).split('-')
    const h = parseInt(p[0] ?? '0', 10)
    const a = parseInt(p[1] ?? '0', 10)
    if (Number.isNaN(h) || Number.isNaN(a)) return null
    return h > a ? 'home' : a > h ? 'away' : 'draw'
  }
  const top: string[] = Array.isArray(raw?.oip?.top3_scores) ? raw.oip.top3_scores : []
  const probs: number[] = Array.isArray(raw?.oip?.top3_prob) ? raw.oip.top3_prob : []
  const top3 = top.map((s, i) => ({ score: norm(s), prob: probs[i] ?? 0 }))
  const score = top3[0]?.score || null
  // 方向冲突: 主推比分方向与当前比分领先方相反 → 诚实标注分歧(非错误)
  const dScore = dirOf(score)
  const dCur = dirOf(currentScore)
  const rollConflict = !!(dScore && dCur && currentScore && dScore !== dCur)
  // 2026-08-30: top1 = 当前比分时, 不再称为"主推"以免视觉错觉(模型仅表示"最可能不变")
  const top1EqualsCurrent = !!(score && currentScore && norm(score) === norm(currentScore))
  const ou = raw?.sub_markets?.ou
  return {
    ...raw,                                    // 保留 _live_predict 原始字段供其它卡片复用
    score_hint: {
      score,
      score_opening: top3[1]?.score ?? null,
      top3,
      top3_opening: top3,
      winner_label: raw?.direction ?? null,
      basis: raw?.decision_text || '',
      opening_basis: `模型 ${raw?.model_type ?? '-'} · 校准 ${raw?.model_calibrated_on ?? '-'}`,
      roll_verification: ou
        ? `OU ${ou.ou_line}: 模型 P(over)=${Number(ou.model_p_over ?? 0).toFixed(3)} vs 市场 ${Number(ou.market_p_over ?? 0).toFixed(3)}`
        : null,
      roll_conflict: rollConflict,
      opening_conflict: false,
      lead_prior_note: null,
      found: !!score,
      // 2026-08-30: 比分分析器三级判定 (定方向/软加权/观望) 原样透传
      score_analysis: raw?.score_analysis ?? null,
      // 2026-08-30: top1 等于当前比分时改措辞, 避免"主推=当前比分"的视觉错觉
      top1_equals_current: top1EqualsCurrent,
    },
  }
}

// ═══ 实时比赛计时(锚定 kickoff GMT+8, 与实时比分页同源, 误差<1分钟) ═══
// 之前锚定采集库 minute 字段(全量轮询 60s), 误差可逼近 1 分钟; 改为以开赛时间(kickoff)
// 为固定基准, 用本地时钟实时推算, 误差仅=网络/时钟抖动(秒级)。
export function parseKickoffGMT8(kickoff: string | null | undefined): number | null {
  if (!kickoff) return null
  let s = kickoff.trim().replace(' ', 'T')
  // 无时区标记 → 视为 GMT+8 (乐鱼/雷速 feed 的 commence_time/kickoff 均为北京时间本地时)
  if (!/[+-]\d{2}:?\d{2}$/.test(s) && !s.endsWith('Z')) {
    s += '+08:00'
  }
  const t = new Date(s).getTime()
  return isNaN(t) ? null : t
}

// 以 kickoff(GMT+8) 为固定基准实时推算比赛已进行的分钟数; kickoff 缺失时回退 feed minute
export function computeLiveMinute(
  kickoff: string | null | undefined,
  now: number,
  fallbackMinute?: number | null,
): number | null {
  const ko = parseKickoffGMT8(kickoff)
  if (ko == null) return fallbackMinute ?? null
  const elapsedMin = (now - ko) / 60000
  if (elapsedMin < -5) return 0 // 尚未开赛
  return elapsedMin
}

// 统一的比赛分钟解析: feed 分钟为锚点 + 本地增量, 仅当"数据新鲜 + feed 卡45 + kickoff 落
// 在(45,125]"时才用 kickoff 估算顶替(解决 obscure 联赛下半场卡 45)。僵尸(last_seen 陈旧)
// 一律不回推, 且结果封顶 125。refTime = 最近一次拉取时间戳(ms)。
export function resolveDisplayMinute(
  m: { minute: number | null; kickoff: string | null | undefined },
  now: number,
  refTime: number,
): number | null {
  const feedMin = m.minute != null ? m.minute : null
  const kickoffMin = computeLiveMinute(m.kickoff, now, null)
  if (feedMin != null) {
    // 2026-08-30 对齐采集器: m.minute 已是后端 resolve_true_minute 精算值(扣中场),
    // 只做本地增量推进; 删掉旧的"卡45用不扣中场的 kickoff 覆盖"逻辑(会把正确的
    // 45 换成错误的 60)。
    let v = feedMin + (refTime > 0 ? (now - refTime) / 60000 : 0)
    return Math.min(MAX_MIN, Math.max(0, v))
  }
  return kickoffMin != null ? Math.min(MAX_MIN, Math.max(0, kickoffMin)) : null
}

export function formatMatchTime(min: number | null, isHalftime = false): string {
  if (isHalftime) return '中场休息'
  if (min == null) return ''
  if (min < 0.5) return '开球'
  if (min < 45) {
    const sec = Math.round((min % 1) * 60)
    return `上半场 ${String(Math.floor(min)).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
  }
  if (min < 60) return `上半场 ${Math.floor(min)}+'`
  if (min < 90) {
    const sec = Math.round((min % 1) * 60)
    return `下半场 ${String(Math.floor(min)).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
  }
  return `下半场 ${Math.floor(min)}+'`
}

export function formatKickoffShort(kickoff: string | null | undefined): string {
  if (!kickoff) return '--:--'
  const s = kickoff.trim()
  const t = s.indexOf(' ')
  const hm = t > 0 ? s.slice(t + 1) : s
  return hm.length >= 5 ? hm.slice(0, 5) : hm
}

export function formatAgeMs(ageMs: number): string {
  const ageSec = Math.max(0, Math.floor(ageMs / 1000))
  if (ageSec < 60) return `${ageSec} 秒前`
  const min = Math.floor(ageSec / 60)
  const sec = ageSec % 60
  if (min < 60) return `${min} 分 ${sec} 秒前`
  const h = Math.floor(min / 60)
  const m = min % 60
  return `${h} 小时 ${m} 分前`
}

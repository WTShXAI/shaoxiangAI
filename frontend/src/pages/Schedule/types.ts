// ═══ 赛程列表页类型契约 (2026-08-31 自 index.tsx 拆分) ═══

export interface LiveMatch {
  match_key: string
  home: string
  away: string
  league: string
  score: string
  minute: number
  kickoff: string
  last_seen?: number
  // 后端已按破蛋/进球潜力排序并附带信号
  half_signal?: string
  half_direction?: 'OVER' | 'UNDER' | null
  half_prob?: number
  full_signal?: string
  full_direction?: 'OVER' | 'UNDER' | null
  full_prob?: number
  // 实时 1X2 (模型对决用)
  odds_h?: number; odds_d?: number; odds_a?: number
  // 开盘 1X2 (模型对决静态段用)
  opening_h?: number; opening_d?: number; opening_a?: number
  // OU 大小球 (手动预测回填, 对齐 NFTB 入参)
  ou_line?: number; ou_over?: number; ou_under?: number
  ou_op_line?: number; ou_op_over?: number; ou_op_under?: number
  // CS 波胆矩阵 ([[score, odds], ...])
  cs_top?: [string, number][] | null
  // 乐鱼扩展赛事内容 (伤病/休整/情报, 来自 content_collector)
  meta?: {
    injuries: { side: string; player: string; pos?: string; reason?: string }[]
    injuries_count: number
    rest_days: number | null
    news: boolean
  } | null
}

export interface ProbeSide {
  prob: number
  signal: string
  direction: 'OVER' | 'UNDER' | null
  line: number
  target_total: number
  over_odds: number | null
  under_odds: number | null
  delta: number | null
  // 2026-08-28: 区分即时盘口 vs 联赛先验兜底; league_prior 时显示琥珀徽标
  data_source?: 'live_odds' | 'league_prior'
  no_odds_reason?: string | null
}

export interface LineDropWindow {
  label: string
  n: number
  hit: number
  acc: number
  roi: number
  avg_under: number
}

export interface LineDropData {
  detected: boolean
  drop?: number
  open_total?: number
  current_total?: number
  window?: string | null
  note?: string
  verdict?: string
  history?: {
    early_window?: LineDropWindow
    late_window?: LineDropWindow
    baseline?: { acc: number; n: number }
  }
}

export interface FulltimeOutcome {
  direction: string | null
  confidence: number | null
  expected_total: number | null
  expected_score: string | null
  expected_home_goals: number | null
  expected_away_goals: number | null
  ah_side: string | null
  reasons: string[]
  note: string
}

export interface ProbeData {
  match_key: string
  current_score: string
  current_minute: number
  league: string
  fav_odds: number | null
  half: ProbeSide
  full: ProbeSide
  reasons: string[]
  warning: string
  line_drop?: LineDropData | null
  fulltime?: FulltimeOutcome | null
}

export const POLL_INTERVAL = 5000 // 5 秒轮询(与当前采集频率匹配) — 列表 + 轻量 probe
export const HEAVY_POLL_INTERVAL = 30000 // 30 秒重分析轮询 (_live_predict 全链路/4盘口/天眼, 2026-08-31 拆分)

// 足球真实最大比赛时间(90min + 中场15 + 补时), 封顶 125。任何估算不得超过此值,
// 否则已完场却卡在 live 的僵尸(开赛数小时)会被 kickoff 回推算出 270+ 分钟。
export const MAX_MIN = 125

// ── 模型来源色点 (2026-08-30 用户需求: 红蓝绿区分结果来源) ──
//   红 = 模型预测(_live_predict 的比分/方向/OU)
//   蓝 = 市场/庄家客观读数(赔率、去水概率、跟随盘口)
//   绿 = 滚球实时(probe 破蛋、降盘漂移)
export const SOURCE_COLORS = {
  model: '#f87171',   // 红
  market: '#60a5fa',  // 蓝
  live: '#4ade80',    // 绿
} as const
export type SourceKind = keyof typeof SOURCE_COLORS
export const SOURCE_LABEL: Record<SourceKind, string> = {
  model: '模型预测(_live_predict)',
  market: '市场/庄家读数',
  live: '滚球实时(probe)',
}

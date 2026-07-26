// ============================================
// 哨响AI - 核心类型定义
// ============================================
// 比赛状态
export type MatchStatus = 'upcoming' | 'live' | 'finished' | 'postponed'
// 预测结果
export type PredictionResult = 'home' | 'draw' | 'away'
// 联赛
export interface League {
  code: string
  name: string
  country: string
  logo?: string
}
// 球队
export interface Team {
  id: string
  name: string
  shortName: string
  logo?: string
  rank?: number
  form?: string[]
}
// 比赛
export interface Match {
  id: string
  homeTeam: Team
  awayTeam: Team
  league: League
  kickoff: string
  status: MatchStatus
  homeScore?: number
  awayScore?: number
  venue?: string
  // 半场比分
  halftimeHome?: number
  halftimeAway?: number
  // 赔率
  homeOdds?: number
  drawOdds?: number
  awayOdds?: number
  // 预测
  prediction?: string  // 'H' | 'D' | 'A'
  confidence?: number
}
// 比分预测
export interface ScorePrediction {
  home: number
  away: number
}
// 概率
export interface Probabilities {
  home: number
  draw: number
  away: number
}
// 盘口
export interface Handicap {
  line: number
  homeOdds: number
  awayOdds: number
}
// 大小球
export interface OverUnder {
  line: number
  overOdds: number
  underOdds: number
}
// 完整预测
export interface Prediction {
  matchId: string
  result: PredictionResult
  probabilities: Probabilities
  score: ScorePrediction
  confidence: number
  handicap?: Handicap
  overUnder?: OverUnder
  modelVersion: string
  timestamp: string
  analysis?: string
  // ── P0修复新增字段 (FullLinkagePipeline v6.0) ──
  /** 一致性校验报告 (7条校验自动化) */
  consistency?: {
    passed: boolean
    checks: Array<{
      name: string
      passed: boolean
      detail?: string
    }>
  }
  /** 让2球不穿律是否触发 */
  hcp2_law_applied?: boolean | null
  /** 短路机制状态 */
  short_circuit?: boolean
  /** P0触发标记列表 */
  p0_triggers?: string[]
  /** 最优波胆 "2-0" */
  best_score?: string
  /** 备选波胆列表 */
  alt_scores?: string[]
  /** D-Gate 风控结果 */
  dgate_result?: {
    risk_tag?: string
    draw_alert?: boolean
    imp?: number
  }
  /** OU联动推理 */
  ou_linkage?: {
    ou_class?: string
    top_scores?: Array<{ score: string; prob: number }>
  }
  /** TaoGe策略决策 */
  taoge_strategy?: {
    primary?: string
    secondary?: string
    direction?: string
  }
  // ── WC校准 OU/让球建议 (v7.1 rules-layer 新增, 经 bridge /predict/single 透传) ──
  /** WC实测校准大小球建议 */
  ou_recommend?: {
    recommend?: string
    line?: number
    expected_total?: number
    confidence?: number
    wc_calibrated?: boolean
    note?: string
  }
  /** WC校准让球建议 */
  hcp_recommend?: {
    recommend?: string
    hcp?: number
    confidence?: number
    wc_calibrated?: boolean
    note?: string
  }
}
// 预测统计
export interface PredictionStats {
  totalPredictions: number
  todayAccuracy: number
  overallAccuracy: number
  hotLeagues: { league: string; count: number }[]
  recentResults: { date: string; correct: number; total: number }[]
}
// 模型版本
export interface ModelVersion {
  id: string
  name: string
  version: string
  accuracy: number
  deployedAt: string
  status: 'active' | 'inactive' | 'rollback'
  metrics: Record<string, number>
}
// 模型对比
export interface ModelComparison {
  models: ModelVersion[]
  metrics: { name: string; values: number[] }[]
}
// 训练状态
export interface TrainingStatus {
  id: string
  status: 'idle' | 'running' | 'completed' | 'failed'
  progress: number
  currentEpoch: number
  totalEpochs: number
  loss: number
  accuracy: number
  startedAt: string
  estimatedEnd?: string
}
// 系统健康
export interface SystemHealth {
  status: 'healthy' | 'degraded' | 'down'
  uptime: number
  apiLatency: number
  predictionLatency: number
  modelHealth: 'healthy' | 'degraded' | 'down'
  databaseHealth: 'healthy' | 'degraded' | 'down'
  memoryUsage: number
  cpuUsage: number
}
// 告警
export interface Alert {
  id: string
  severity: 'critical' | 'warning' | 'info'
  title: string
  message: string
  timestamp: string
  acknowledged: boolean
}
// 指标摘要
export interface MetricsSummary {
  apiRequestsPerMin: number
  avgResponseTime: number
  predictionRequestsPerMin: number
  errorRate: number
  activeUsers: number
}
// 用户
export interface User {
  id: string
  username: string
  email: string
  role: 'admin' | 'analyst' | 'viewer'
  avatar?: string
}
// 球队特征
export interface TeamFeatures {
  teamName: string
  attack: number
  defense: number
  midfield: number
  stamina: number
  morale: number
  homeAdvantage: number
  formTrend: number[]
  goalStats: {
    avgScored: number
    avgConceded: number
    cleanSheetRate: number
    scoringRate: number
  }
}
// 实时赛程预测数据 (来自完整管线 /fixtures/upcoming?include_predictions=true)
export interface FixturePrediction {
  mode: 'full_pipeline' | 'simplified'
  warning?: boolean
  probabilities: { H: number; D: number; A: number }
  prediction: string
  top_scores: { score: string; prob: number; outcome: string }[]
  direction: 'SAME' | 'DRAW_DIVERGE' | 'OPPOSITE'
  draw_signal: number
  trap_level: string
  risk_tag: string
  odds_used: { H: number; D: number; A: number; OU: number }
  expected_goals?: { home: number; away: number; total: number; ou_line: number }
}
// 实时赛程 (来自 /fixtures/upcoming, football-data.org 实时API)
export interface Fixture {
  id: number
  home: string
  away: string
  time: string         // UTC ISO
  time_local: string   // 北京时间 HH:MM
  date_local: string   // 本地日期 MM-DD
  day_of_week: string  // 周几
  group: string        // 小组赛分组, 淘汰赛为空
  stage: string        // 赛事阶段
  status: string       // TIMED / IN_PLAY / FINISHED 等
  score_home?: number | null
  score_away?: number | null
  is_finished?: boolean
  prediction?: FixturePrediction
}
export interface FixturesResponse {
  matches: Fixture[]
  days: number
  upcoming_count: number
  finished_count: number
  cutoff: string
  today: Fixture[]
  tomorrow: Fixture[]
  error?: string
}
// ═══ 34 联赛赛程 类型 ═══
export interface LeagueCatalogEntry {
  sport_key: string
  name: string
  available: boolean
  fixture_count: number
}
export interface LeagueCategory {
  category: string
  leagues: LeagueCatalogEntry[]
}
export interface LeaguesResponse {
  categories: LeagueCategory[]
  total_leagues: number
}
export interface FixtureEntry {
  id: string
  home: string
  away: string
  commence_time: string
  league?: string
  sport?: string
  // 1X2 全场
  odds_h?: number
  odds_d?: number
  odds_a?: number
  // 初盘赔率 (固定, 第一条采集, 永不漂移)
  opening_h?: number
  opening_d?: number
  opening_a?: number
  // AH/OU 初盘
  ah_op_home?: number
  ah_op_away?: number
  ou_op_over?: number
  ou_op_under?: number
  // 全场让球 (AH)
  ah_line?: string
  ah_home?: number
  ah_away?: number
  // 全场大小 (OU)
  ou_line?: string
  ou_over?: number
  ou_under?: number
  // 半场 1X2
  h1_odds_h?: number
  h1_odds_d?: number
  h1_odds_a?: number
  // 半场让球
  h_ah_line?: string
  h_ah_home?: number
  h_ah_away?: number
  // 半场大小
  h_ou_line?: string
  h_ou_over?: number
  h_ou_under?: number
  // 状态
  match_state?: number
  score_home?: number
  score_away?: number
  score_inferred?: boolean  // drift 推断比分, 非 leyu 直接推送
  match_minute?: string | number
  kickoff_countdown?: string
  kickoff_ms?: number
  bookmakers_count?: number
  sport_key?: string
  // Req2: 赔率快照 + 漂移 (后端 _capture_initial_odds 注入)
  _snapshot?: {
    initial: { odds_h?: number; odds_d?: number; odds_a?: number;
                ah_line?: string; ah_home?: number; ah_away?: number;
                ou_line?: string; ou_over?: number; ou_under?: number } | null
    drift: Partial<Record<'odds_h'|'odds_d'|'odds_a'|'ah_home'|'ah_away'|'ou_over'|'ou_under', number>> | null
    has_snapshot: boolean
  } | null
}
export interface LeagueFixturesResponse {
  sport_key: string
  name: string
  category: string
  fixtures: FixtureEntry[]
  cached: boolean
  cache_age_s?: number
  stale?: boolean
  note?: string
  error?: string
}
// ── 模拟投注 (paper betting) ──
export type BetSide = 'H' | 'D' | 'A'
export interface BetRecord {
  bet_id: number
  match_id?: number | null
  home_team: string
  away_team: string
  league?: string
  match_date?: string | null
  bet_type?: string        // 'recommendation' | 'paper_bet'
  source?: string          // 'prediction' | 'manual'
  predicted_result?: BetSide | null
  confidence?: number
  home_odds?: number
  draw_odds?: number
  away_odds?: number
  kelly?: number
  expected_value?: number
  actual_result?: BetSide | null
  is_correct?: number | null   // 0/1/null
  actual_score?: string | null
  resolved_at?: string | null
  created_at?: string
}
export interface PlaceBetRequest {
  home_team: string
  away_team: string
  league?: string
  home_odds: number
  draw_odds: number
  away_odds: number
  bet_side: BetSide
  stake_amount?: number    // 不传则后端按半凯利建议
  confidence?: number
}
export interface PlaceBetResponse {
  bet_id: number
  bet_side: BetSide
  odds: number
  stake_amount: number
  kelly_half: number
  implied_prob: number
  message?: string
  error?: string
}
export interface BetListResponse {
  bets: BetRecord[]
  total: number
  limit: number
  offset: number
  error?: string
}
// API响应包装
export interface ApiResponse<T> {
  success: boolean
  data: T
  message?: string
  timestamp: string
}


// ============================================
// 量化投注系统类型 (真实数据驱动)
// ============================================
export interface QuantAccount {
  init_bankroll: number
  equity: number
  peak: number
  return_pct: number
  bets: number
  wins: number
  losses: number
  win_rate: number
  pnl_total: number
  max_drawdown_pct: number
  sharpe: number
}
export interface QuantStrategy {
  id: string
  name: string
  desc: string
  enabled: boolean
}
export interface QuantPosition {
  oid: string
  home: string
  away: string
  direction: string
  odds: number
  stake: number
  win: boolean
  pnl: number
  equity_after: number
}
export interface QuantOrder {
  oid: string
  mid: string
  home: string
  away: string
  market: string
  selection: string
  direction: string
  odds: number
  stake: number
  equity_before: number
  model_prob: number
  edge_pct: number
  ev_pct: number
  confidence: number
  mode: string
  strategy_id?: string
  strategy_name?: string
  created_at: string
  settled: boolean
  win?: boolean | null
  pnl?: number | null
  equity_after?: number | null
}
export interface QuantSignal {
  ts: string
  level: string  // order/settle/loss/scan/replay/analyze/info
  msg: string
  [key: string]: any
}
export interface OptionValuation {
  market: string
  selection: string
  odds: number | null
  model_prob: number
  market_prob: number | null
  edge_pct: number
  ev_pct: number
  kelly_half: number
  decision: string  // BET/EVAL/SCAN/PASS
}
export interface ScanResult {
  mid: string
  home: string
  away: string
  league: string
  is_multi_book: boolean
  expected_goals?: { lh: number; la: number; total: number } | null
  top_scores?: { score: string; prob: number }[] | null
  options: OptionValuation[]
  bet_candidates: OptionValuation[]
  n_options: number
  n_bets: number
  best_option: OptionValuation | null
}
export interface QuantSnapshot {
  account: QuantAccount
  equity_curve: { step: number; equity: number; bankroll?: number; note?: string }[]
  positions: QuantPosition[]
  pending: QuantOrder[]
  recent_orders: QuantOrder[]
  signals: QuantSignal[]
  recent_scans: ScanResult[]
  strategies: QuantStrategy[]
  auto_mode: boolean
  bet_count: number
}

// ============================================
// 赛事终端类型 (全链路分析 _live_predict 输出)
// ============================================
export interface TerminalMatch {
  home: string
  away: string
  league: string
  sport_key: string
  commence_time: string
  odds_h: number
  odds_d: number
  odds_a: number
  bookmakers_count: number
  bookmakers?: string[]
}
/** 价值层单行 (H/D/A 每方向) */
export interface ValueLayerRow {
  outcome: string
  odds: number
  market_prob: number
  model_prob: number
  edge: number
  edge_pct: number
  ev: number
  ev_pct: number
  kelly_full: number
  kelly_half: number
  stake_unit: number
}
/** 波胆交叉标注 (单比分 × 让球 × 大小球) */
export interface ScoreAnnotated {
  score: string
  prob: number
  prob_eff: number
  direction: 'H' | 'D' | 'A'
  handicap?: string | null   // 赢/输/走/半赢/半输
  ou?: string | null          // 大/小/走
  fair_decimal?: number | null
  fair_eff_decimal?: number | null
  long_tail?: boolean         // 长尾负EV
}
/** 市场结构波胆三角定位候选 (OU×AH×1X2×CS 取交集, 可审计) */
export interface CsTriangulation {
  winner?: 'home' | 'draw' | 'away'
  ah_favorite?: 'home' | 'away' | null
  ou_branch?: string
  cs_coverage?: string
  method?: string
  ranked?: string[]
  notes?: string[]
  candidates?: Array<{
    score: string; hg: number; ag: number; reason: string
    cs_prob?: number | null; poisson?: number | null; blend?: number
  }>
}
export interface OperatorView {
  rules?: string[]
  verdict?: string
  stake_hint?: string
  trap?: { score?: number; level?: string; tags?: string[] }
}
/** 单条策略方向信号 (面板提示级, 不改 verdict / 不自动下注) */
export interface StrategySignal {
  name: string
  direction: string
  strength: number        // 0-1 强度
  metric: string
  note: string
  confidence: 'high' | 'low'
}

/** _live_predict 决策卡片 (terminal/analyze 返回) */
export interface TerminalDecisionCard {
  fixture: { home: string; away: string; commence_time: string; sport_key: string }
  odds: { oh: number; od: number; oa: number }
  market_prob: { h: number; d: number; a: number }
  overround?: number
  direction: string
  market_conf?: number
  decision: string  // BET / PASS
  decision_text: string
  best_direction?: string
  best_edge_pct?: number
  rows?: ValueLayerRow[]
  softline?: any | null
  books_count: number
  draw_alert: boolean
  operator_view?: OperatorView
  sub_markets?: { ou?: any; draw?: any; correct_score?: any }
  oip?: {
    lambda_h: number; lambda_a: number
    top3_scores: string[]; top3_prob: number[]
    top5_scores: string[]; top5_prob: number[]
    over15?: number; over25?: number; over35?: number
    /** 波胆×让球×大小球交叉标注 */
    scores_annotated?: ScoreAnnotated[]
    /** 操盘纪律标记 */
    discipline?: { multi_direction: boolean; direction_count: { H: number; D: number; A: number }; best_direction: string }
    ah_line?: number | string | null
    ou_line?: number | string | null
    /** 市场结构波胆三角定位 (OU×AH×1X2×CS 取交集) */
    cs_triangulation?: CsTriangulation | null
  }
  handicap?: any
  value_layer?: any
  /** 三方向策略信号 (tier 感知: 仅 obscure 低级别联赛层触发; 面板提示级, 不改 verdict) */
  strategy_signals?: StrategySignal[]
  /** 信号域: obscure(触发) / main / cup(不触发) */
  strategy_tier?: 'obscure' | 'main' | 'cup'
  /** In-play 条件波胆信息 (null=赛前/未裁剪) */
  inplay?: {
    current_score: string
    elapsed: number
    time_ratio: number
    original_lambda_h: number
    original_lambda_a: number
    remaining_lambda_h: number
    remaining_lambda_a: number
    note: string
  } | null
  /** 多庄 sharp/retail 共识 (leisu 数据可用时) */
  multibook_consensus?: {
    n_books: number; n_sharp: number; has_true_sharp: boolean
    sharp_books: string[]
    sharp_consensus: { h: number; d: number; a: number }
    retail_mean: { h: number; d: number; a: number }
    value_side: { outcome: string; pp: number }
    fade_side: { outcome: string; pp: number }
    max_spread_pp: number
    divergences?: { book: string; outcome: string; retail_over: boolean; pp: number }[]
  } | null
  /** 操盘手逆转信号 (初盘→live赔率漂移) */
  operator_signals?: {
    reversal_risk: number
    operator_reliability: number
    direction: 'home' | 'draw' | 'away'
    drift_draw_down: boolean
    drift_significant: boolean
    favorite_flip: boolean
    spread_change: number
    signals: string[]
    delta: { h: number; d: number; a: number }
  } | null
  error?: string
}

// ═══ 实时比分 (bridge_service /api/live-scores, /api/live-score/{mid}) ═══
// 数据源: live_scores DB 表 (_poll_live_details 后台线程写入)。
// mststi: 1=上半场 2=中场 3=下半场 4=加时 5=点球 -1=结束
export interface LiveScoreMatch {
  mid: string
  home: string
  away: string
  league: string
  mststi: number
  score_home: number
  score_away: number
  match_minute: string | number
  mlet?: string
  events?: any[]
  snapshot_at: number
  is_live: boolean
  // 实时赔率 (DB 列可能为 NULL)
  odds_h?: number | null
  odds_d?: number | null
  odds_a?: number | null
  // 初盘 (固定, 第一条采集)
  opening_h?: number | null
  opening_d?: number | null
  opening_a?: number | null
  // AH 初盘
  ah_op_home?: number | null
  ah_op_away?: number | null
  // OU 初盘
  ou_op_over?: number | null
  ou_op_under?: number | null
  ou_line?: string | null
  ou_over?: number | null
  ou_under?: number | null
  ah_line?: string | null
  ah_home?: number | null
  ah_away?: number | null
}
export interface LiveScoresResponse {
  matches: LiveScoreMatch[]
  count: number
  error?: string
}
export interface ScoreHistoryEntry {
  ts: number
  score_home: number
  score_away: number
  match_minute: string | number
  mststi: number
}
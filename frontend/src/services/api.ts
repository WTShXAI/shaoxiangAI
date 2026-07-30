import axios from 'axios'
import type {
  ApiResponse,
  FixturesResponse,
  LeaguesResponse,
  LeagueFixturesResponse,
  LiveScoreMatch,
  LiveScoresResponse,
  ScoreHistoryEntry,
} from '@/types'
import { useAppStore } from '@/store'

// ── 运行时客户端 (单一真相源) ──
// 系统唯一后端 = bridge_service (:9000)。所有真实端点都建在它之上, 无 /api/v1 前缀:
//   /api/leagues, /api/leagues/{sport_key}/fixtures, /api/match-results,
//   /api/terminal/*, /api/quant*, /api/portfolio ...
// 历史遗留: 一批指向 /api/v1/* (predict / models / training / monitor / fixtures /
// matches / features / alerts / historical / auth / data-quality / bets ...) 的 service,
// 其对应后端路由从未实现 → 前端调用必 404。已于 2026-07-17 架构/前端统一性审计中整体移除。
// 哨响AI 前端 ↔ bridge_service 全部走同源 (空 baseURL, axios 自动补当前 origin)。
// 这样可以彻底规避 CORS:
//   - 浏览器从 http://127.0.0.1:9000 加载前端, 直接请求 /api/... = 同源
//   - 不再依赖 bridge 的 CORS 中间件正确性
// 保留 VITE_BRIDGE_URL 是为了 Docker/远程部署场景: 跨机访问时填入 http://<host>:9000。
const _BRIDGE_URL = ((import.meta as any).env?.VITE_BRIDGE_URL || '').trim()
if (!_BRIDGE_URL) {
  // 同源部署, 静默即可 — 这是默认期望
} else {
  console.info('[env] VITE_BRIDGE_URL 已配置, 跨源访问:', _BRIDGE_URL)
}

// 统一 axios 工厂 (所有客户端经此创建, 杜绝散落重复实例)
function createClient(baseURL: string) {
  // H4(2026-07-30): 若配置了 VITE_API_KEY 则注入 X-API-Key 头 (生产鉴权).
  // 默认开发环境无此变量, 不注入, 保持兼容.
  const apiKey = (import.meta as any).env?.VITE_API_KEY
  return axios.create({
    baseURL,
    timeout: 30000,
    headers: {
      'Content-Type': 'application/json',
      ...(apiKey ? { 'X-API-Key': apiKey } : {}),
    },
  })
}

const bridgeApi = createClient(_BRIDGE_URL || '')
bridgeApi.interceptors.request.use((config) => {
  const token = localStorage.getItem('auth_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// ============================================
// 联赛赛程服务 (34联赛) — bridge_service:9000
// ============================================
export const leagueScheduleService = {
  // 获取联赛目录 (按分类分组); days=N 只保留近 N 天内(默认 2)有赛程的联赛
  getLeagues: (days: number = 2) =>
    bridgeApi.get<ApiResponse<LeaguesResponse>>(`/api/leagues?days=${days}`),
  // 获取指定联赛赛程 (sport_key 含中文, 必须 encodeURIComponent)
  getFixtures: (sportKey: string) =>
    bridgeApi.get<ApiResponse<LeagueFixturesResponse>>(`/api/leagues/${encodeURIComponent(sportKey)}/fixtures`),
}

// ============================================
// 赛果查询服务 — bridge_service:9000  /api/match-results
// 数据源 football_data.db (matches + historical_matches UNION)
// ============================================
export interface MatchResult {
  home: string; away: string; league: string; date: string
  home_score: number; away_score: number; result: string
  ht_h: number | null; ht_a: number | null; source: string
}
export const matchResultService = {
  getResults: (params?: { league?: string; q?: string; date_from?: string; date_to?: string; limit?: number }) =>
    bridgeApi.get<ApiResponse<{ results: MatchResult[]; total: number }> | { error: string; results: never[] }>('/api/match-results', { params }),
}

// ============================================
// 实时比分服务 — bridge_service:9000  /api/live-scores(++) — 已有端点
// 数据源 live_scores DB 表; 5s TTL 单场刷新端点专为前端实时轮询设计
// ============================================
export const liveScoreService = {
  /** 最近 180s 内更新的全部进行中比赛 (mststi>0), 含实时赔率 */
  getLiveMatches: (limit: number = 5000) =>
    bridgeApi.get<ApiResponse<LiveScoresResponse>>('/api/live-scores', { params: { limit } }),
  /** 单场比分时序快照 (用于折线图/事件回放) */
  getScoreHistory: (mid: string, limit: number = 60) =>
    bridgeApi.get<ApiResponse<{ mid: string; history: ScoreHistoryEntry[]; count: number }>>(
      `/api/live-score/${encodeURIComponent(mid)}`, { params: { limit } }),
  /** 单场实时刷新 (5s TTL 缓存, 进球后 5-15s 反映到 UI) */
  getLiveUpdate: (mid: string, force: boolean = false) =>
    bridgeApi.get<ApiResponse<any>>(`/api/live-update/${encodeURIComponent(mid)}`, { params: { force } }),
}

// ============================================
// 量化投注系统 (真实数据) — bridge_service:9000  /api/quant/*
// 真实行情(live_odds_raw/odds_features) + 全市场扫描 + 历史回放 + 策略层
// ============================================
export interface ScanSingleRequest {
  home: string; away: string; h: number; d: number; a: number; league?: string
  score_odds?: Record<string, number>
  total_goals_odds?: Record<string, number>
  handicap_odds?: { line: number; home: number; draw: number; away: number }
  ou_odds?: { line: number; over: number; under: number }
}
export const quantService = {
  snapshot: () => bridgeApi.get<ApiResponse<any>>('/api/quant/snapshot'),
  scanCycle: (mode: string = 'sim', limit: number = 20) =>
    bridgeApi.post<ApiResponse<any>>('/api/quant/scan/auto', { action: 'cycle', mode, limit }),
  autoMode: (on: boolean) =>
    bridgeApi.post<ApiResponse<any>>('/api/quant/scan/auto', { action: on ? 'on' : 'off' }),
  scanSingle: (data: ScanSingleRequest) =>
    bridgeApi.post<ApiResponse<any>>('/api/quant/scan/single', data),
  historyReplay: (nMatches: number = 100) =>
    bridgeApi.post<ApiResponse<any>>('/api/quant/history/replay', { n_matches: nMatches }),
  confirmAll: () => bridgeApi.post<ApiResponse<any>>('/api/quant/order/confirm-all'),
  confirmOne: (oid: string, actual: string = 'D') =>
    bridgeApi.post<ApiResponse<any>>('/api/quant/order/confirm', { oid, actual }),
  toggleStrategy: (strategyId: string, enabled: boolean) =>
    bridgeApi.post<ApiResponse<any>>('/api/quant/strategy/toggle', { strategy_id: strategyId, enabled }),
  reset: (bankroll?: number) =>
    bridgeApi.post<ApiResponse<any>>('/api/quant/reset', bankroll ? { bankroll } : {}),
}

// ── 回测组合绩效 (live_pilot_guardian --portfolio) ──
export const portfolioService = {
  /** 获取组合管理器完整快照: 资金曲线+持仓+绩效指标 */
  snapshot: () => bridgeApi.get<{
    sharpe_ratio?: number
    max_drawdown_pct?: number
    calmar_ratio?: number
    win_rate_pct?: number
    profit_loss_ratio?: number
    expected_value?: number
    total_trades?: number
    total_roi_pct?: number
    portfolio_snapshot?: {
      initial_equity: number
      current_equity: number
      total_roi_pct: number
      positions: any[]
      equity_curve: { timestamp: string; equity: number; pnl: number; note: string }[]
    }
  }>('/api/portfolio'),
}

// ============================================
// 赛事终端服务 — bridge_service:9000  /api/terminal/*
// 赛事列表 + 全链路分析(_live_predict 11 层编排)
// ============================================
export const terminalService = {
  /** 当天可决策赛事列表 (live_odds_raw, ≥2 庄) */
  getMatches: () => bridgeApi.get<ApiResponse<any>>('/api/terminal/matches'),
  /** 全链路分析 — 直接用盘口赔率(赛事列表同源), 不调 The Odds API
   *  @param liveScore 可选 in-play 当前比分, 传入时后端启用条件 Poisson 裁剪 */
  analyze: async (home: string, away: string, sportKey: string = 'soccer_fifa_world_cup',
            odds?: { h: number; d: number; a: number },
            handicap?: { ah_line?: number | string; ah_home?: number; ah_away?: number;
                         ou_line?: number | string; ou_over?: number; ou_under?: number },
            liveScore?: { homeGoals?: number; awayGoals?: number; elapsed?: number }) => {
    const body: Record<string, any> = { home, away, sport_key: sportKey }
    if (odds) Object.assign(body, { odds_h: odds.h, odds_d: odds.d, odds_a: odds.a })
    if (handicap) Object.assign(body, {
      ah_line: handicap.ah_line, ah_home: handicap.ah_home, ah_away: handicap.ah_away,
      ou_line: handicap.ou_line, ou_over: handicap.ou_over, ou_under: handicap.ou_under,
    })
    if (liveScore) Object.assign(body, {
      home_goals: liveScore.homeGoals, away_goals: liveScore.awayGoals, elapsed: liveScore.elapsed,
    })
    const resp = await bridgeApi.post<ApiResponse<any>>('/api/terminal/analyze', body)
    // 赛事模型路由回填: model_type / model_calibrated_on 由后端 _live_predict 单一真相源给出,
    // 前端绝不自己分类, 仅透传展示 (ApiResponse.data = _live_predict result dict)。
    const payload = (resp.data as any)?.data
    if (payload) {
      useAppStore.getState().setModelType((payload as any).model_type ?? null)
      useAppStore.getState().setModelCalibratedOn((payload as any).model_calibrated_on ?? null)
    }
    return resp
  },
  /** 实时赔率匹配 (三级回退: live_odds_raw → The Odds API → 提示) */
  getMatchOdds: (home: string, away: string) =>
    bridgeApi.get<ApiResponse<any>>('/api/match-odds', { params: { home, away } }),
  /** HTTP 降级版赔率摄入 (浏览器插件 DOM 抓取 → 喂进 live_odds_raw) */
  ingest: (data: { home: string; away: string; source?: string; h: number; d: number; a: number; score?: string; minute?: number }) =>
    bridgeApi.post<ApiResponse<any>>('/api/terminal/ingest', data),
}

// ============================================
// 概率排名编排器 — bridge_service:9000  /api/predict/ranked
// 三市场 (1X2/OU/CS) 各自锚定操盘手赔率去水 → 跨市场按概率降序统一排名 (OU不特权)
// 前端 MatchAnalysisModal 并行调用, 渲染"概率排名总览"面板 (analysis/markets/combined_top)
// 仅取 home/away + 1X2 + OU; 操盘手CS赔率(op_cs)由后端自动从 GQ.db 回退
// ============================================
export const rankedService = {
  predict: (home: string, away: string,
            odds: { h: number; d: number; a: number },
            handicap?: { ou_line?: number | string; ou_over?: number; ou_under?: number }) => {
    const body: Record<string, any> = { home, away, oh: odds.h, od: odds.d, oa: odds.a }
    if (handicap) {
      if (handicap.ou_line != null && handicap.ou_line !== '') body.ou_line = Number(handicap.ou_line)
      if (handicap.ou_over != null) body.over_water = handicap.ou_over
      if (handicap.ou_under != null) body.under_water = handicap.ou_under
    }
    return bridgeApi.post<ApiResponse<any>>('/api/predict/ranked', body)
  },
}

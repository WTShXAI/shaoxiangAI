import axios from 'axios'
import type { ApiResponse } from '@/types'
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


// Bridge 标准响应包 (后端真实返回 shape: {ok, data, error?}, 与历史遗留 ApiResponse 不同)
export interface BridgeResponse<T> {
  ok: boolean
  data: T
  error?: string
}

// ============================================
// 滚球破蛋神器 — bridge_service:9000 /api/live-goal-probe*
// 走统一 axios 客户端: 自动注入 VITE_API_KEY / auth, 支持 VITE_BRIDGE_URL 远程部署.
// ============================================
export const liveGoalProbeService = {
  /** 当前进行中比赛列表, 后端已按破蛋/进球潜力排序。支持 offset 分页 (live 场次多时避免被截断) */
  getMatches: (limit: number = 50, offset: number = 0) =>
    bridgeApi.get<BridgeResponse<{ matches: any[]; max_last_seen: number | null; server_now: number | null; total_live: number; total_scheduled: number; offset: number; limit: number }>>('/api/live-goal-probe/matches', { params: { limit, offset }, timeout: 60000 }),
  /** 对指定比赛输出半场/全场破蛋概率与信号方向 */
  getProbe: (match_key: string, score: string, minute: number, is_halftime: boolean = false) =>
    bridgeApi.get<BridgeResponse<any>>('/api/live-goal-probe', { params: { match_key, score, minute, is_halftime }, timeout: 60000 }),
  /** 6 维盘口聚合 (滚球实时): 1X2/AH/OU × 全场/半场, 含当前 line/odds + 相对开盘 drift */
  getLiveOdds: (match_key: string) =>
    bridgeApi.get<BridgeResponse<any>>(`/api/live-odds/${encodeURIComponent(match_key)}`, { timeout: 60000 }),
  /** 历史回测摘要 (风险披露) */
  getBacktest: () =>
    bridgeApi.get<BridgeResponse<any>>('/api/live-goal-probe/backtest'),
  /** 操盘手结论卡(实时页) — 取初盘1X2 → _live_predict → 蒸馏一行结论 */
  getLiveOperatorCard: (match_key: string, home?: string, away?: string, league?: string) =>
    bridgeApi.get<BridgeResponse<any>>('/api/live/operator-card', {
      params: { match_key, home: home || undefined, away: away || undefined, league: league || undefined },
    }),
  /** CS 波胆诱导标记 — 检测庄家是否用低赔波胆簇引导资金 */
  getInduceFlag: (match_key: string, actual_score?: string) =>
    bridgeApi.get<BridgeResponse<any>>('/api/cs/induce-flag', {
      params: { match_key, actual_score: actual_score || undefined },
    }),
  /** CS 信任卡 — 结构校准分布 + 庄家盘口对照 + 诱导标记; current_score+minute → 滚球即时盘模式 */
  getCsTrustCard: (match_key: string, current_score?: string, minute?: number) =>
    bridgeApi.get<BridgeResponse<any>>('/api/cs/trust-card', {
      params: {
        match_key,
        current_score: current_score || undefined,
        minute: minute || undefined,
      },
    }),
  /** 分钟级数据流: 盘口+比分时间线 / 进球事件 / 逐分钟剩余破蛋曲线 */
  getMinuteStream: (match_key: string, line: number = 2.5, league?: string, opening_total?: number) =>
    bridgeApi.get<BridgeResponse<any>>('/api/match-minute-stream', {
      params: { match_key, line, league: league || undefined, opening_total: opening_total ?? undefined },
      timeout: 60000,
    }),
  /** 把当前可见比赛注册为采集器秒级焦点 (前端不读返回, 失败静默) */
  registerFocus: (match_keys: string[], ttl_seconds: number = 60) =>
    bridgeApi.post<BridgeResponse<{ success: boolean; count: number }>>(
      '/api/focus',
      { match_keys, ttl_seconds },
    ),
  /** 自主巡航 Agent 告警列表(最新在前) — 后台 Agent 循环产生 */
  getAgentAlerts: (limit: number = 50) =>
    bridgeApi.get<BridgeResponse<any>>('/api/agent/alerts', { params: { limit } }),
  /** 决策智能体卡片: 消费模型数据(开盘盘口结构) → 输出 决策/方案/合理比分。
   *  本地 qwen3 仅作背后推理引擎; 卡片展示智能体的「决策和方案」, 非模型闲聊。 */
  getAnalyze: (match_key: string, score: string = '0-0', minute: number = 0, is_halftime: boolean = false) =>
    bridgeApi.get<BridgeResponse<any>>('/api/live-goal-probe/analyze', {
      params: { match_key, score, minute, is_halftime },
      timeout: 60000,
    }),
  /** 决策仲裁层 (C 完整档): 聚合 决策智能体/操盘手卡/OU决策 多路信号,
   *  产出 signal_consensus / discrepancy / closing_line_value / confidence_interval。
   *  over/under 可选, 传入则补 OU discrepancy(devig fair vs implied)。 */
  getConsensus: (
    match_key: string, score: string = '0-0', minute: number = 0, is_halftime: boolean = false,
    home?: string, away?: string, league?: string,
    over?: number, under?: number, line: number = 2.5,
    opening_total?: number, current_total?: number,
  ) =>
    bridgeApi.get<BridgeResponse<any>>('/api/live-goal-probe/consensus', {
      params: {
        match_key, score, minute, is_halftime,
        home: home || undefined, away: away || undefined, league: league || undefined,
        over: over ?? undefined, under: under ?? undefined, line,
        opening_total: opening_total ?? undefined, current_total: current_total ?? undefined,
      },
      timeout: 60000,
    }),
  /** 动态滚球决策系统 (Live Momentum Trader) 统一裁决卡:
   *  聚合 决策智能体/操盘手/OU决策/信号仲裁/回测 五部分, 消除"多卡互相矛盾"体感。
   *  替代原 ModelAnalysisCard + ArbitrationCard。所有建仓措辞标注"分析参考·需人工审批"。
   *  live_home/live_draw/live_away 与 ah_home/ah_away 为可选真实盘口赔率(用于 AH↔1X2 市场对照)。 */
  getMomentum: (
    match_key: string, score: string = '0-0', minute: number = 0, is_halftime: boolean = false,
    home?: string, away?: string, league?: string,
    over?: number, under?: number, line: number = 2.5,
    opening_total?: number, current_total?: number,
    live_home?: number, live_draw?: number, live_away?: number,
    ah_home?: number, ah_away?: number,
  ) =>
    bridgeApi.get<BridgeResponse<any>>('/api/live-goal-probe/momentum', {
      params: {
        match_key, score, minute, is_halftime,
        home: home || undefined, away: away || undefined, league: league || undefined,
        over: over ?? undefined, under: under ?? undefined, line,
        opening_total: opening_total ?? undefined, current_total: current_total ?? undefined,
        live_home: live_home ?? undefined, live_draw: live_draw ?? undefined,
        live_away: live_away ?? undefined, ah_home: ah_home ?? undefined, ah_away: ah_away ?? undefined,
      },
      timeout: 60000,
    }),
}

// ============================================
// 赛事终端服务 — bridge_service:9000 /api/terminal/analyze
// 全链路分析 (_live_predict 11 层编排)
// 2026-08-29 复原: 8-27 前端重建前, 赛程页面接的就是这个模型。
//   重建(commit 0571e07c)后改用 /api/live-goal-probe/analyze (cross_score) 出比分,
//   实测不可信(干净数据 top1 仅 7.6%) → 用户拍板复原回 _live_predict。
// 契约: 前端绝不自己分类模型, model_type / model_calibrated_on 由后端单一真相源给出。
// ============================================
export const terminalService = {
  /** 全链路分析 — 直接用盘口赔率(赛事列表同源), 不调 The Odds API
   *  @param liveScore 可选 in-play 当前比分, 传入时后端启用条件 Poisson 裁剪 */
  analyze: (
    home: string,
    away: string,
    sportKey: string = 'soccer_fifa_world_cup',
    odds?: { h: number; d: number; a: number },
    handicap?: {
      ah_line?: number | string; ah_home?: number; ah_away?: number
      ou_line?: number | string; ou_over?: number; ou_under?: number
    },
    liveScore?: { homeGoals?: number; awayGoals?: number; elapsed?: number },
  ) => {
    const body: Record<string, any> = { home, away, sport_key: sportKey }
    if (odds) Object.assign(body, { odds_h: odds.h, odds_d: odds.d, odds_a: odds.a })
    if (handicap) Object.assign(body, {
      ah_line: handicap.ah_line, ah_home: handicap.ah_home, ah_away: handicap.ah_away,
      ou_line: handicap.ou_line, ou_over: handicap.ou_over, ou_under: handicap.ou_under,
    })
    if (liveScore) Object.assign(body, {
      home_goals: liveScore.homeGoals, away_goals: liveScore.awayGoals, elapsed: liveScore.elapsed,
    })
    return bridgeApi.post<BridgeResponse<any>>('/api/terminal/analyze', body, { timeout: 90000 })
  },
}


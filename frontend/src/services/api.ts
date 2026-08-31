import axios from 'axios'
import type {
  ApiResponse, LeaguesResponse, LeagueFixturesResponse, LiveScoresResponse, ScoreHistoryEntry,
} from '@/types'
import { useAppStore } from '@/store'

// ── 运行时客户端 (单一真相源) ──
// 系统唯一后端 = bridge_service (:9000)。所有真实端点都建在它之上, 无 /api/v1 前缀:
//   /api/leagues, /api/leagues/{sport_key}/fixtures,
//   /api/terminal/*, /api/quant*, /api/portfolio ...
// 历史遗留: 一批指向 /api/v1/* (predict / models / training / monitor / fixtures /
// matches / features / alerts / historical / auth / data-quality / bets ...) 的 service,
// 其对应后端路由从未实现 → 前端调用必 404。已于 2026-07-17 架构/前端统一性审计中整体移除。
// 哨响AI 前端 ↔ bridge_service 全部走同源 (空 baseURL, axios 自动补当前 origin)。
// 这样可以彻底规避 CORS:
//   - 浏览器从 http://127.0.0.1:9000 加载前端, 直接请求 /api/... = 同源
//   - 不再依赖 bridge 的 CORS 中间件正确性
// 保留 VITE_BRIDGE_URL 是为了 Docker/远程部署场景: 跨机访问时填入 http://<host>:9000。
const _BRIDGE_URL = (import.meta.env?.VITE_BRIDGE_URL || '').trim()
if (!_BRIDGE_URL) {
  // 同源部署, 静默即可 — 这是默认期望
} else {
  console.info('[env] VITE_BRIDGE_URL 已配置, 跨源访问:', _BRIDGE_URL)
}

// 统一 axios 工厂 (所有客户端经此创建, 杜绝散落重复实例)
function createClient(baseURL: string) {
  // H4(2026-07-30): 若配置了 VITE_API_KEY 则注入 X-API-Key 头 (生产鉴权).
  // 默认开发环境无此变量, 不注入, 保持兼容.
  const apiKey = import.meta.env?.VITE_API_KEY
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

// ── 统一错误归一化 (v7.5, 2026-08-31) ──
// 所有经 bridgeApi 的失败请求统一转成 ApiError, 调用方 catch 即得友好 message + 可编程 code。
// 成功路径不改动 (保持现有 res.data / res.data.data 契约)。
export type ApiErrorCode =
  | 'ERR_NETWORK' | 'ERR_TIMEOUT' | 'ERR_SERVER' | 'ERR_UNAUTHORIZED'
  | 'ERR_NOT_FOUND' | 'ERR_HTTP' | 'ERR_CANCELED' | 'ERR_UNKNOWN'

export class ApiError extends Error {
  readonly code: ApiErrorCode
  readonly status?: number
  readonly raw?: unknown
  constructor(message: string, code: ApiErrorCode = 'ERR_UNKNOWN', status?: number, raw?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.raw = raw
  }
}

/** 从任意异常归一化为 ApiError (幂等: 已是 ApiError 原样返回) */
export function normalizeApiError(e: unknown): ApiError {
  if (e instanceof ApiError) return e
  if (axios.isAxiosError(e)) {
    const status = e.response?.status
    const body = e.response?.data as Record<string, unknown> | undefined
    // 兼容多种后端报错字段: {error}/{message}/{msg}/{detail}
    const serverMsg =
      (typeof body?.error === 'string' && body.error) ||
      (typeof body?.message === 'string' && body.message) ||
      (typeof body?.msg === 'string' && body.msg) ||
      (typeof body?.detail === 'string' && body.detail) ||
      (typeof body?.detail === 'object' && body.detail ? JSON.stringify(body.detail) : '')
    if (e.code === 'ECONNABORTED') return new ApiError('请求超时，请稍后重试', 'ERR_TIMEOUT', status, e)
    if (axios.isCancel(e)) return new ApiError('请求已取消', 'ERR_CANCELED', undefined, e)
    if (!e.response) return new ApiError(`网络连接失败：${e.message || '无法连接到服务'}`, 'ERR_NETWORK', undefined, e)
    if (status && status >= 500) return new ApiError(serverMsg || `服务端错误 (${status})`, 'ERR_SERVER', status, e)
    if (status === 401) return new ApiError(serverMsg || '登录已失效，请重新登录', 'ERR_UNAUTHORIZED', status, e)
    if (status === 404) return new ApiError(serverMsg || '接口不存在 (404)', 'ERR_NOT_FOUND', status, e)
    return new ApiError(serverMsg || `请求失败 (${status ?? '未知状态'})`, 'ERR_HTTP', status, e)
  }
  if (e instanceof Error) return new ApiError(e.message || '未知错误', 'ERR_UNKNOWN', undefined, e)
  return new ApiError(String(e), 'ERR_UNKNOWN', undefined, e)
}

// 统一错误拦截: 失败侧归一化 (成功侧原样透传, 不改变任何调用方契约)
bridgeApi.interceptors.response.use(
  (res) => res,
  (error) => Promise.reject(normalizeApiError(error)),
)

/** 请求取消辅助: 页面/组件卸载时 abort 进行中请求, 防 setState on unmounted (v7.5) */
export function makeAbortSignal(): AbortSignal {
  return new AbortController().signal
}
export function isCancelError(e: unknown): boolean {
  return e instanceof ApiError && e.code === 'ERR_CANCELED'
}

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

// 电子盘/虚拟盘过滤 (2026-08-30): 前端兜底拦截, 与后端 NON_FOOTBALL_LEAGUE_KEYWORDS 同源.
// 后端 live_goal_probe_matches_api 已在列表层过滤 (bridge_service.py:4249), 但前端 service 层再挡一道,
// 任何走 getMatches 的页面都不会渲染电子盘/8分钟盘 (防后端改动或新取数口子漏出).
export const VIRTUAL_LEAGUE_KEYWORDS = [
  '8分钟', '瓦尔哈拉', '瓦尔基里', '电子', '电竞', '电子竞技',
  'FIFAe', 'eSports', 'Esports', 'VS-', '梦幻对垒',
]
export function isVirtualLeague(league?: string | null): boolean {
  if (!league) return false
  return VIRTUAL_LEAGUE_KEYWORDS.some(kw => league.includes(kw))
}

// 主流联赛白名单 (2026-08-31 涛哥反馈): LeagueSchedule 滚球页被 U21/U20/女子/友谊赛
// 等低级别联赛淹没, 模型在这些样本上 league_scoring 先验不足, 校准不可靠.
// 任何走 getMatches 的页面不再渲染, 与后端过滤双保险.
// 反向匹配: 不在白名单 = 冷门全剔. 解决采集器 league="其他"(717场未填) 漏过关键词匹配的问题.
export const MAIN_LEAGUE_WHITELIST = [
  '英超','西甲','意甲','德甲','法甲','欧冠','中超','亚冠','世界杯',
  '欧联','欧国联','世预赛','英冠','意乙','西乙','德乙','法乙',
  '葡超','巴甲','阿甲','荷甲','比甲','苏超','瑞超','挪超',
  '欧协联','欧洲杯','联合会杯','亚冠联',
]
// 子串白名单要排除的冷门标签 (防 "英超U21" 被误判为"英超"主流)
export const MAIN_LEAGUE_BLOCKWORDS = [
  'U21','U19','U20','U23','U17','U18',
  '女子','女足','女甲','女乙','女杯','WOMEN','Women',
  '友谊','热身','球会友谊','邀请赛','俱乐部赛','热身赛',
  '预备队','后备','二队','B队','青训','梯队',
  '少年','学生','校园','业余','地区','城市','省级','市级',
]
export function isMainLeague(league?: string | null, sportKey?: string | null): boolean {
  const u = ((league || '') + '|' + (sportKey || ''))
  if (MAIN_LEAGUE_BLOCKWORDS.some(kw => u.includes(kw))) return false  // 子串冷门先排除
  return MAIN_LEAGUE_WHITELIST.some(m => u.includes(m))
}
export function isObscureLeague(league?: string | null, sportKey?: string | null): boolean {
  // 无联赛名视为冷门 (采集器 league 字段缺失场景)
  if (!league && !sportKey) return true
  // 虚拟盘(电子竞技/8分钟) 一律剔
  if (isVirtualLeague(league) || isVirtualLeague(sportKey)) return true
  // 反向匹配: 不在主流白名单 = 冷门
  return !isMainLeague(league, sportKey)
}

export const liveGoalProbeService = {
  /** 当前进行中比赛列表, 后端已按破蛋/进球潜力排序。支持 offset 分页 (live 场次多时避免被截断)。
   *  前端兜底: 剔除电子盘/虚拟盘 (isVirtualLeague), 与后端过滤双保险. */
  getMatches: (limit: number = 50, offset: number = 0) =>
    bridgeApi
      .get<BridgeResponse<{ matches: any[]; max_last_seen: number | null; server_now: number | null; total_live: number; total_scheduled: number; offset: number; limit: number }>>('/api/live-goal-probe/matches', { params: { limit, offset }, timeout: 60000 })
      .then((res) => {
        const d = res.data?.data
        if (d && Array.isArray(d.matches)) {
          d.matches = d.matches.filter((m: any) => !isVirtualLeague(m?.league) && !isObscureLeague(m?.league, m?.sport_key))
        }
        return res
      }),
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
    signal?: AbortSignal,
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
    return bridgeApi.post<BridgeResponse<any>>('/api/terminal/analyze', body, { timeout: 90000, signal })
  },
}

// ============================================
// 联赛赛程服务 (34联赛) — bridge_service:9000
// 2026-08-31 多页导航恢复: 自 0571e07c^ 还原 (实时比分页 LiveScores 依赖)
// ============================================
export const leagueScheduleService = {
  // 获取联赛目录 (按分类分组); days=N 只保留近 N 天内(默认 7)有赛程的联赛
  getLeagues: (days: number = 7) =>
    bridgeApi.get<ApiResponse<LeaguesResponse>>(`/api/leagues?days=${days}`),
  // 获取指定联赛赛程 (sport_key 含中文, 必须 encodeURIComponent)
  getFixtures: (sportKey: string) =>
    bridgeApi.get<ApiResponse<LeagueFixturesResponse>>(`/api/leagues/${encodeURIComponent(sportKey)}/fixtures`),
  // 全量赛程聚合 (一次返回所有联赛 fixtures, 避免前端逐联赛并发触发全局限流)
  getAllFixtures: (days: number = 7, signal?: AbortSignal) =>
    bridgeApi.get<ApiResponse<any>>(`/api/all-fixtures?days=${days}`, { signal }),
}

// ============================================
// 实时比分服务 — bridge_service:9000  /api/live-scores(++)
// 数据源 live_scores DB 表; 5s TTL 单场刷新端点专为前端实时轮询设计
// ============================================
export const liveScoreService = {
  /** 最近 180s 内更新的全部进行中比赛 (mststi>0), 含实时赔率 */
  getLiveMatches: (limit: number = 5000, signal?: AbortSignal) =>
    bridgeApi.get<ApiResponse<LiveScoresResponse>>('/api/live-scores', { params: { limit }, signal }),
  /** 单场比分时序快照 (用于折线图/事件回放) */
  getScoreHistory: (mid: string, limit: number = 60, signal?: AbortSignal) =>
    bridgeApi.get<ApiResponse<{ mid: string; history: ScoreHistoryEntry[]; count: number }>>(
      `/api/live-score/${encodeURIComponent(mid)}`, { params: { limit }, signal }),
  /** 单场实时刷新 (5s TTL 缓存, 进球后 5-15s 反映到 UI) */
  getLiveUpdate: (mid: string, force: boolean = false, signal?: AbortSignal) =>
    bridgeApi.get<ApiResponse<any>>(`/api/live-update/${encodeURIComponent(mid)}`, { params: { force }, signal }),
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
            handicap?: { ou_line?: number | string; ou_over?: number; ou_under?: number },
            signal?: AbortSignal) => {
    const body: Record<string, any> = { home, away, oh: odds.h, od: odds.d, oa: odds.a }
    if (handicap) {
      if (handicap.ou_line != null && handicap.ou_line !== '') body.ou_line = Number(handicap.ou_line)
      if (handicap.ou_over != null) body.over_water = handicap.ou_over
      if (handicap.ou_under != null) body.under_water = handicap.ou_under
    }
    return bridgeApi.post<ApiResponse<any>>('/api/predict/ranked', body, { signal })
  },
}

// ============================================
// 世界级分析器 — bridge_service:9000  GET /api/world-analyze
// 市场锚+模型矩阵+一致性+Edge三件套+漂移+联赛背景+诚实边界 (IR-20: 分析非预测)
// 缺 1X2 赔率时后端自动按对阵回填 events.db; 开盘价可选(无则漂移缺失)
// ============================================
export interface WorldAnalyzerParams {
  home: string
  away: string
  league?: string
  h?: number; d?: number; a?: number
  ou_line?: number; ou_over?: number; ou_under?: number
  ah_line?: number; ah_home?: number; ah_away?: number
  op_h?: number; op_d?: number; op_a?: number
  kickoff?: string
}
export const worldAnalyzerService = {
  analyze: (p: WorldAnalyzerParams) =>
    bridgeApi.get<ApiResponse<any>>('/api/world-analyze', { params: p, timeout: 90000 }),
}


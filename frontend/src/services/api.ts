import axios from 'axios'
import type {
  ApiResponse,
  Match,
  Prediction,
  PredictionStats,
  ModelVersion,
  ModelComparison,
  TrainingStatus,
  SystemHealth,
  Alert,
  MetricsSummary,
  User,
  TeamFeatures,
  League,
  FixturesResponse,
  LeaguesResponse,
  LeagueFixturesResponse,
  BetRecord,
  PlaceBetRequest,
  PlaceBetResponse,
  BetListResponse,
  TerminalMatch,
  DecisionCard,
  DataGrowthStats,
  TerminalMatchesResponse,
  TerminalIngestRequest,
} from '@/types'
import { normalizePrediction, normalizeDecisionCard } from './bridgeAdapter'

// ── 运行时环境变量校验 (E4 P0-8) ──
// 约定: 生产部署必须注入 VITE_BRIDGE_URL / VITE_API_URL; 缺失则告警并回退 dev 默认。
const _BRIDGE_URL = (import.meta as any).env?.VITE_BRIDGE_URL
const _API_URL = (import.meta as any).env?.VITE_API_URL
if (!_BRIDGE_URL) {
  console.warn(
    '[env] VITE_BRIDGE_URL 未配置 → bridge_service 请求将回退 http://localhost:9000，' +
    '生产部署会失效。请在 .env 设置 VITE_BRIDGE_URL (如 http://<host>:9000)。'
  )
}

// 前缀约定 (文档化, 勿随意改名, 否则后端路由整片 404):
//   - 主 API (FastAPI 后端):  baseURL = VITE_API_URL || '/api/v1'  → 路由 /api/v1/*
//   - bridge_service:         baseURL = VITE_BRIDGE_URL || http://localhost:9000
//                             → 其路由本身带 /api 前缀 (e.g. /api/leagues, /api/terminal/*)
// 统一 axios 工厂 (E4 P1-13: 所有客户端经此创建, 杜绝散落重复实例)
function createClient(baseURL: string) {
  return axios.create({
    baseURL,
    timeout: 30000,
    headers: {
      'Content-Type': 'application/json',
    },
  })
}

const api = createClient(_API_URL || '/api/v1')

// ── 预测服务专用实例 → bridge_service:9000 (FullLinkagePipeline v6.0) ──
const bridgeApi = createClient(_BRIDGE_URL || 'http://localhost:9000')
bridgeApi.interceptors.request.use((config) => {
  const token = localStorage.getItem('auth_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})
// 请求拦截器 - 添加认证Token
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('auth_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})
// 响应拦截器 - 统一错误处理
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('auth_token')
      // 不再强制跳转，由各页面自行处理 401 错误
    }
    return Promise.reject(error)
  }
)
// ============================================
// 预测服务
// ============================================
export const predictionService = {
  // 下一场比赛预测
  getNextMatch: () =>
    api.get<ApiResponse<{ match: Match; prediction: Prediction }>>('/predict/next-match'),
  // 单场比赛预测 — 走 bridge_service (v7.1 双引擎)
  predictSingle: async (data: { home_team?: string; homeTeam?: string; away_team?: string; awayTeam?: string; league?: string; odds_h?: number; odds_d?: number; odds_a?: number; hcp?: number; ou_line?: number; stage?: string; competition?: string }) => {
    const resp = await api.post<ApiResponse<Prediction>>('/predict/single', data)
    // E4 P0-8: 隔离引擎易变字段, 缺省补稳定默认 → 引擎输出变动不崩
    resp.data = { ...resp.data, data: normalizePrediction(resp.data?.data) }
    return resp
  },
  // 批量预测
  predictBatch: (matches: { homeTeam: string; awayTeam: string; league?: string }[]) =>
    api.post<ApiResponse<Prediction[]>>('/predict/batch', { matches }),
  // 预测历史
  getPredictionHistory: (params?: { page?: number; limit?: number; league?: string }) =>
    api.get<ApiResponse<Prediction[]>>('/predict/history', { params }),
  // 预测统计
  getPredictionStats: () =>
    api.get<ApiResponse<PredictionStats>>('/predict/stats'),
  // 生成报告
  generateReport: (data: { matchIds: string[]; format?: string }) =>
    api.post<ApiResponse<{ url: string }>>('/predict/report', data),
  // V4引擎预测 — 走 bridge_service (v7.1)
  predictV4: async (data: { homeTeam: string; awayTeam: string; league?: string; odds_h?: number; odds_d?: number; odds_a?: number; hcp?: number; ou_line?: number; stage?: string; competition?: string }) => {
    const resp = await api.post<ApiResponse<Prediction>>('/predict/single', {
      home_team: data.homeTeam,
      away_team: data.awayTeam,
      league: data.league,
      odds_h: data.odds_h,
      odds_d: data.odds_d,
      odds_a: data.odds_a,
      hcp: data.hcp,
      ou_line: data.ou_line,
      stage: data.stage || 'knockout',
      competition: data.competition || 'wc',
    })
    // E4 P0-8: 隔离引擎易变字段
    resp.data = { ...resp.data, data: normalizePrediction(resp.data?.data) }
    return resp
  },
}
// ============================================
// 模型管理服务
// ============================================
export const modelService = {
  // 模型版本列表
  getVersions: () =>
    api.get<ApiResponse<ModelVersion[]>>('/models/versions'),
  // 模型详情
  getVersionDetail: (id: string) =>
    api.get<ApiResponse<ModelVersion>>(`/models/versions/${id}`),
  // 模型对比
  compareModels: (params: { modelA: string; modelB: string }) =>
    api.get<ApiResponse<ModelComparison>>('/models/compare', { params }),
  // 模型信息
  getModelInfo: () =>
    api.get<ApiResponse<{ name: string; version: string; description: string }>>('/models/info'),
  // 部署模型
  deployModel: (modelId: string) =>
    api.post<ApiResponse<{ success: boolean }>>('/models/deploy', { model_id: modelId }),
  // 回滚模型
  rollbackModel: (versionId: string) =>
    api.post<ApiResponse<{ success: boolean }>>('/models/rollback', { version_id: versionId }),
  // 自动提升
  autoPromote: () =>
    api.post<ApiResponse<{ promoted: boolean; modelId?: string }>>('/models/auto-promote'),
}
// ============================================
// 训练服务
// ============================================
export const trainingService = {
  // 启动训练
  startTraining: (config?: Record<string, unknown>) =>
    api.post<ApiResponse<{ trainingId: string }>>('/training/start', config || {}),
  // 训练状态
  getTrainingStatus: () =>
    api.get<ApiResponse<TrainingStatus>>('/training/status'),
  // 训练历史
  getTrainingHistory: () =>
    api.get<ApiResponse<TrainingStatus[]>>('/training/history'),
}
// ============================================
// 比赛数据服务
// ============================================
export const matchService = {
  // 比赛列表
  getMatches: (params?: { league?: string; status?: string; date?: string; limit?: number; offset?: number }) =>
    api.get<ApiResponse<Match[]>>('/matches/list', { params }),
  // 比赛比分
  getScores: (params?: { league?: string; date?: string }) =>
    api.get<ApiResponse<Match[]>>('/matches/scores', { params }),
}
// ============================================
// 实时赛程服务 (football-data.org 实时API, 后端1小时缓存)
// ============================================
export const fixtureService = {
  // 未来7天世界杯赛程 + 完整预测管线嵌入
  getUpcoming: (days: number = 7) =>
    api.get<FixturesResponse>(`/fixtures/upcoming?days=${days}&include_predictions=true`),
}
// ============================================
// 特征服务
// ============================================
export const featureService = {
  // 球队特征
  getTeamFeatures: (teamName: string) =>
    api.get<ApiResponse<TeamFeatures>>(`/features/teams/${encodeURIComponent(teamName)}`),
}
// ============================================
// 监控服务
// ============================================
export const monitorService = {
  // 健康检查
  getHealth: () =>
    api.get<ApiResponse<SystemHealth>>('/monitor/health'),
  // 系统信息
  getSystemInfo: () =>
    api.get<ApiResponse<{ version: string; uptime: number; pythonVersion: string }>>('/monitor/system'),
  // 模型健康
  getModelHealth: () =>
    api.get<ApiResponse<{ status: string; lastPrediction: string; accuracy: number }>>('/monitor/model-health'),
  // 指标摘要
  getMetricsSummary: () =>
    api.get<ApiResponse<MetricsSummary>>('/monitor/metrics/summary'),
}
// ============================================
// 告警服务
// ============================================
export const alertService = {
  // 告警列表
  getAlerts: (params?: { severity?: string; acknowledged?: boolean }) =>
    api.get<ApiResponse<Alert[]>>('/alerts/alerts', { params }),
}
// ============================================
// 历史数据服务
// ============================================
export const historicalService = {
  // 联赛列表
  getLeagues: () =>
    api.get<ApiResponse<League[]>>('/historical/leagues'),
  // 联赛比赛数据
  getLeagueMatches: (leagueCode: string) =>
    api.get<ApiResponse<Match[]>>(`/historical/${leagueCode}/matches`),
}
// ============================================
// 认证服务
// ============================================
export const authService = {
  // 登录
  login: (credentials: { username: string; password: string }) =>
    api.post<ApiResponse<{ token: string; user: User }>>('/auth/login', credentials),
  // 当前用户
  getCurrentUser: () =>
    api.get<ApiResponse<User>>('/auth/me'),
}
// ============================================
// 数据质量服务
// ============================================
export const dataQualityService = {
  // 数据质量报告
  getReports: () =>
    api.get<ApiResponse<{ reports: unknown[] }>>('/data-quality/reports'),
}
// ============================================
// 联赛赛程服务 (34联赛) — bridge_service:9000
// 注意: bridge_service 路由是 /api/leagues (无 /v1 前缀), 须用 bridgeApi 实例
// ============================================
export const leagueScheduleService = {
  // 获取联赛目录 (按分类分组)
  getLeagues: () =>
    bridgeApi.get<ApiResponse<LeaguesResponse>>('/api/leagues'),
  // 获取指定联赛赛程
  getFixtures: (sportKey: string) =>
    bridgeApi.get<ApiResponse<LeagueFixturesResponse>>(`/api/leagues/${sportKey}/fixtures`),
}
// ============================================
// 模拟投注服务 (paper betting) — bridge_service:9000
// ============================================
export const betService = {
  // 查询投注记录 (支持分页 + 状态过滤)
  getBets: (params?: { limit?: number; offset?: number; status?: 'resolved' | 'pending' | '' }) =>
    bridgeApi.get<ApiResponse<BetListResponse>>('/api/bets', { params }),
  // 手动模拟下注 (赛程页内嵌触发)
  placeBet: (data: PlaceBetRequest) =>
    bridgeApi.post<ApiResponse<PlaceBetResponse>>('/api/bets', data),
}
// ============================================
// 赔率 Widget 服务 — bridge_service:9000
// API key 由后端注入, 前端只拿拼好的 URL 嵌 iframe
// ============================================
export const widgetService = {
  getWidgetUrl: (sportKey: string, params?: { bookmakerKeys?: string; oddsFormat?: string; markets?: string }) =>
    bridgeApi.get<ApiResponse<{ widget_url?: string; sport_key?: string; error?: string }>>('/api/widget-url', {
      params: { sport_key: sportKey, ...params },
    }),
}
// ============================================
// 操盘终端服务 (OperatorTerminal) — bridge_service:9000
// ============================================
export const terminalService = {
  // 当天可决策比赛列表 (有多庄赔率的)
  getMatches: () =>
    bridgeApi.get<ApiResponse<TerminalMatchesResponse>>('/api/terminal/matches'),
  // 指定比赛实时拉取多庄 → 决策卡片
  analyze: async (home: string, away: string, sportKey: string = 'soccer_fifa_world_cup') => {
    const resp = await bridgeApi.post<ApiResponse<DecisionCard>>('/api/terminal/analyze', { home, away, sport_key: sportKey })
    // E4 P0-8: 隔离引擎易变字段, 缺省补稳定默认
    resp.data = { ...resp.data, data: normalizeDecisionCard(resp.data?.data) }
    return resp
  },
  // 数据增长统计
  getGrowthStats: () =>
    bridgeApi.get<ApiResponse<DataGrowthStats>>('/api/data-growth/stats'),
  // 插件赔率摄入 (HTTP降级版)
  ingest: (data: TerminalIngestRequest) =>
    bridgeApi.post<ApiResponse<{ status: string; match: string; books: number; direction?: string; decision?: string }>>('/api/terminal/ingest', data),
}
export default api
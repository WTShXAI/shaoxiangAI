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
} from '@/types'
const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
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
      window.location.href = '/login'
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
  // 单场比赛预测
  predictSingle: (data: { home_team?: string; homeTeam?: string; away_team?: string; awayTeam?: string; league?: string }) =>
    api.post<ApiResponse<Prediction>>('/predict/single', data),
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
  // V4引擎预测
  predictV4: (data: { homeTeam: string; awayTeam: string; league?: string }) =>
    api.post<ApiResponse<Prediction>>('/predict/v4', data),
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
  // 今天+明天的世界杯赛程 (前端快速按钮)
  getUpcoming: () =>
    api.get<FixturesResponse>('/fixtures/upcoming'),
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
export default api
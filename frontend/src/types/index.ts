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
// API响应包装
export interface ApiResponse<T> {
  success: boolean
  data: T
  message?: string
  timestamp: string
}
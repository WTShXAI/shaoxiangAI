import { create } from 'zustand'
import type { User, Alert, SystemHealth, MetricsSummary, PredictionStats } from '@/types'
// ============================================
// 应用状态
// ============================================
interface AppState {
  // 侧边栏
  sidebarCollapsed: boolean
  toggleSidebar: () => void
  // 用户
  user: User | null
  setUser: (user: User | null) => void
  // 系统状态
  systemHealth: SystemHealth | null
  setSystemHealth: (health: SystemHealth | null) => void
  // 告警
  alerts: Alert[]
  setAlerts: (alerts: Alert[]) => void
  unacknowledgedCount: number
  // 指标摘要
  metricsSummary: MetricsSummary | null
  setMetricsSummary: (metrics: MetricsSummary | null) => void
  // 预测统计
  predictionStats: PredictionStats | null
  setPredictionStats: (stats: PredictionStats | null) => void
  // 主题
  theme: 'dark' | 'light'
  setTheme: (theme: 'dark' | 'light') => void
  // 赛事引擎 (全局切换, 旧开关: 保留但默认 league, 不再用于模型判断)
  competition: 'wc' | 'league'
  setCompetition: (competition: 'wc' | 'league') => void
  // 赛事模型路由 (后端单一真相源回填, 前端绝不自己分类)
  modelType: 'cup' | 'league' | null
  setModelType: (modelType: 'cup' | 'league' | null) => void
  modelCalibratedOn: string | null
  setModelCalibratedOn: (modelCalibratedOn: string | null) => void
}
export const useAppStore = create<AppState>((set, get) => ({
  sidebarCollapsed: false,
  toggleSidebar: () => set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
  user: null,
  setUser: (user) => set({ user }),
  systemHealth: null,
  setSystemHealth: (systemHealth) => set({ systemHealth }),
  alerts: [],
  setAlerts: (alerts) =>
    set({
      alerts,
      unacknowledgedCount: alerts.filter((a) => !a.acknowledged).length,
    }),
  unacknowledgedCount: 0,
  metricsSummary: null,
  setMetricsSummary: (metricsSummary) => set({ metricsSummary }),
  predictionStats: null,
  setPredictionStats: (predictionStats) => set({ predictionStats }),
  theme: 'dark',
  setTheme: (theme) => set({ theme }),
  competition: 'league',
  setCompetition: (competition) => set({ competition }),
  modelType: null,
  setModelType: (modelType) => set({ modelType }),
  modelCalibratedOn: null,
  setModelCalibratedOn: (modelCalibratedOn) => set({ modelCalibratedOn }),
}))
// (已清理：预测大厅相关死切片)
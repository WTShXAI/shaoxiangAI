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
}))
// ============================================
// 预测大厅状态
// ============================================
interface PredictionHallState {
  selectedLeague: string | null
  setSelectedLeague: (league: string | null) => void
  timeFilter: 'today' | 'week' | 'month'
  setTimeFilter: (filter: 'today' | 'week' | 'month') => void
  searchQuery: string
  setSearchQuery: (query: string) => void
}
export const usePredictionHallStore = create<PredictionHallState>((set) => ({
  selectedLeague: null,
  setSelectedLeague: (league) => set({ selectedLeague: league }),
  timeFilter: 'today',
  setTimeFilter: (timeFilter) => set({ timeFilter }),
  searchQuery: '',
  setSearchQuery: (searchQuery) => set({ searchQuery }),
}))
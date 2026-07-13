import { createBrowserRouter, Navigate } from 'react-router-dom'
import { lazy, Suspense } from 'react'
import AppLayout from '@/components/layout/AppLayout'

// E4 P1-13: 路由级代码分割 — 各页独立 chunk, echarts/framer 不再进首包
const PredictionHall = lazy(() => import('@/pages/PredictionHall'))
const MatchAnalysis = lazy(() => import('@/pages/MatchAnalysis'))
const ModelManagement = lazy(() => import('@/pages/ModelManagement'))
const SystemMonitor = lazy(() => import('@/pages/SystemMonitor'))
const DataExplorer = lazy(() => import('@/pages/DataExplorer'))
const LeagueSchedule = lazy(() => import('@/pages/LeagueSchedule'))
const OperatorTerminal = lazy(() => import('@/pages/OperatorTerminal'))

const PageFallback = () => (
  <div className="flex items-center justify-center h-full min-h-[40vh] text-ink-secondary text-sm">
    加载中…
  </div>
)

const withSuspense = (node: React.ReactNode) => <Suspense fallback={<PageFallback />}>{node}</Suspense>

const router = createBrowserRouter([
  {
    path: '/',
    element: <AppLayout />,
    children: [
      { index: true, element: withSuspense(<PredictionHall />) },
      { path: 'match-analysis', element: withSuspense(<MatchAnalysis />) },
      { path: 'operator-terminal', element: withSuspense(<OperatorTerminal />) },
      { path: 'model-management', element: withSuspense(<ModelManagement />) },
      { path: 'system-monitor', element: withSuspense(<SystemMonitor />) },
      { path: 'data-explorer', element: withSuspense(<DataExplorer />) },
      { path: 'league-schedule', element: withSuspense(<LeagueSchedule />) },
      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
])
export default router

import { createBrowserRouter, Navigate } from 'react-router-dom'
import { lazy, Suspense } from 'react'
import AppLayout from '@/components/layout/AppLayout'

// E4 P1-13: 路由级代码分割 — 各页独立 chunk, echarts/framer 不再进首包
const LeagueSchedule = lazy(() => import('@/pages/LeagueSchedule'))
const MatchResults = lazy(() => import('@/pages/MatchResults'))
const QuantDemo = lazy(() => import('@/pages/QuantDemo'))
const LiveScores = lazy(() => import('@/pages/LiveScores'))

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
      { index: true, element: withSuspense(<LeagueSchedule />) },
      { path: 'live-scores', element: withSuspense(<LiveScores />) },
      { path: 'match-results', element: withSuspense(<MatchResults />) },
      { path: 'quant-demo', element: withSuspense(<QuantDemo />) },
      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
])
export default router

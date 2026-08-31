import { createBrowserRouter, Navigate } from 'react-router-dom'
import { lazy, Suspense } from 'react'
import AppLayout from '@/components/layout/AppLayout'
import Skeleton from '@/components/shared/Skeleton'

// v7.6 多页导航恢复 (2026-08-31): 单页架构改回多页, Sidebar 提供入口。
// / → 赛程列表 (详情自动跑 7 个模型: 破蛋/合理比分/动态决策/信号仲裁/CS信任卡/庄家诱导/模型对决)
// /live-scores → 实时比分 (LiveScores, 自 0571e07c^ 恢复)
// /timeline → 时间线 (Timeline, 自 0571e07c^ 恢复)
// /world-analyzer → 世界级分析器 (GET /api/world-analyze, 市场锚+模型矩阵+Edge三件套)
const Schedule = lazy(() => import('@/pages/Schedule'))
const LiveScores = lazy(() => import('@/pages/LiveScores'))
const Timeline = lazy(() => import('@/pages/Timeline'))
const WorldAnalyzer = lazy(() => import('@/pages/WorldAnalyzer'))

const PageFallback = () => (
  <div className="p-6 space-y-4 max-w-[1000px]">
    <Skeleton variant="line" className="w-1/3" />
    <Skeleton variant="card" />
    <Skeleton variant="card" />
  </div>
)

const withSuspense = (node: React.ReactNode) => <Suspense fallback={<PageFallback />}>{node}</Suspense>

const router = createBrowserRouter([
  {
    path: '/',
    element: <AppLayout />,
    children: [
      { index: true, element: withSuspense(<Schedule />) },
      { path: 'live-scores', element: withSuspense(<LiveScores />) },
      { path: 'timeline', element: withSuspense(<Timeline />) },
      { path: 'world-analyzer', element: withSuspense(<WorldAnalyzer />) },
      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
])

export default router

import { createBrowserRouter, Navigate } from 'react-router-dom'
import { lazy, Suspense } from 'react'
import AppLayout from '@/components/layout/AppLayout'

// 2026-08-28 用户拍板: 进入前端直接显示赛程列表 (滚球神器/仪表盘磁贴入口全部移除)。
// / → 赛程列表 (详情自动跑 7 个模型: 破蛋/合理比分/动态决策/信号仲裁/CS信任卡/庄家诱导/模型对决)。
const Schedule = lazy(() => import('@/pages/Schedule'))

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
      { index: true, element: withSuspense(<Schedule />) },
      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
])

export default router

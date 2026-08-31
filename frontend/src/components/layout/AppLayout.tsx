import { Outlet, useLocation } from 'react-router-dom'
import { useEffect } from 'react'
import { motion } from 'framer-motion'
import Sidebar from './Sidebar'
import TopBar from './TopBar'
import AgeGate from '@/components/compliance/AgeGate'
import ComplianceFooter from '@/components/compliance/ComplianceFooter'
import { useAppStore } from '@/store'

// 路由 → 文档标题 (v7.5): 切换页面时浏览器标签页同步更新
const ROUTE_TITLES: Record<string, string> = {
  '/': '赛程 · 滚球决策',
  '/live-scores': '实时比分',
  '/timeline': '今日时间轴',
  '/world-analyzer': '世界级分析器',
}
const BASE_TITLE = '哨响AI | 足球预测系统'

// v7.6 多页导航恢复 (2026-08-31): Sidebar(4页面) + TopBar + 内容区 + 合规页脚。
// Sidebar 为 fixed 定位, 内容区用 marginLeft 跟随折叠/展开 (展开 240px / 折叠 72px)。
export default function AppLayout() {
  const { sidebarCollapsed } = useAppStore()
  const { pathname } = useLocation()

  // 路由标题同步 (子路径如 /world-analyzer 精确匹配, 未知路径用默认)
  useEffect(() => {
    const title = ROUTE_TITLES[pathname]
    document.title = title ? `${title} · ${BASE_TITLE}` : BASE_TITLE
  }, [pathname])

  return (
    <div className="min-h-screen flex flex-col">
      <AgeGate />
      <Sidebar />
      <motion.div
        initial={false}
        animate={{ marginLeft: sidebarCollapsed ? 72 : 240 }}
        className="transition-all duration-300 flex flex-col min-h-screen"
      >
        <TopBar />
        <motion.main
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.3 }}
          className="flex-1 p-6 max-w-[1400px] w-full mx-auto"
        >
          <Outlet />
        </motion.main>
        <ComplianceFooter />
      </motion.div>
    </div>
  )
}

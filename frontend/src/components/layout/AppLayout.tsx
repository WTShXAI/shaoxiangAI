import { Outlet } from 'react-router-dom'
import { motion } from 'framer-motion'
import TopBar from './TopBar'
import AgeGate from '@/components/compliance/AgeGate'
import ComplianceFooter from '@/components/compliance/ComplianceFooter'

// v7.5 重构: 仪表盘单页架构, 不再使用左侧导航栏 (Sidebar 已删除)。
// 保留 AgeGate(18+ 合规) + TopBar(系统状态/主题) + 内容区 + 合规页脚。
export default function AppLayout() {
  return (
    <div className="min-h-screen flex flex-col">
      <AgeGate />
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
    </div>
  )
}

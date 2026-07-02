import { useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useAppStore } from '@/store'
import { monitorService, alertService } from '@/services/api'
export default function TopBar() {
  const { systemHealth, setSystemHealth, alerts, setAlerts, unacknowledgedCount, metricsSummary, setMetricsSummary } = useAppStore()
  // 轮询系统状态
  useEffect(() => {
    const fetchData = async () => {
      try {
        const [healthRes, alertsRes, metricsRes] = await Promise.all([
          monitorService.getHealth(),
          alertService.getAlerts({ acknowledged: false }),
          monitorService.getMetricsSummary(),
        ])
        setSystemHealth(healthRes.data)
        setAlerts(alertsRes.data.alerts || [])
        setMetricsSummary(metricsRes.data)
      } catch {
        // 静默失败，保持上次数据
      }
    }
    fetchData()
    const interval = setInterval(fetchData, 15000)
    return () => clearInterval(interval)
  }, [setSystemHealth, setAlerts, setMetricsSummary])
  const healthColor = systemHealth?.status === 'healthy' ? 'bg-pitch-500' : systemHealth?.status === 'degraded' ? 'bg-ember-500' : 'bg-danger-500'
  return (
    <header className="h-14 border-b border-white/[0.04] bg-[#0a0e0f]/60 backdrop-blur-xl flex items-center justify-between px-6">
      {/* 左侧：面包屑/页面标题 */}
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${healthColor} shadow-[0_0_8px_var(--tw-shadow-color)]`} />
          <span className="text-xs text-white/40 font-medium">
            {systemHealth?.status === 'healthy' ? '系统正常' : systemHealth?.status === 'degraded' ? '性能下降' : '异常'}
          </span>
        </div>
        <div className="w-px h-4 bg-white/[0.06]" />
        <span className="text-xs text-white/30">
          {metricsSummary?.apiRequestsPerMin ? `${metricsSummary.apiRequestsPerMin} req/min` : '-- req/min'}
        </span>
      </div>
      {/* 右侧：告警 + 用户 */}
      <div className="flex items-center gap-4">
        {/* 告警按钮 */}
        <button className="relative p-2 rounded-xl hover:bg-white/[0.04] transition-colors group">
          <svg className="w-5 h-5 text-white/40 group-hover:text-white/60" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5">
            <path strokeLinecap="round" strokeLinejoin="round" d="M14.857 17.082a23.848 23.848 0 005.454-1.31A8.967 8.967 0 0118 9.75v-.7V9A6 6 0 006 9v.75a8.967 8.967 0 01-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 01-5.714 0m5.714 0a3 3 0 11-5.714 0" />
          </svg>
          <AnimatePresence>
            {unacknowledgedCount > 0 && (
              <motion.span
                initial={{ scale: 0 }}
                animate={{ scale: 1 }}
                exit={{ scale: 0 }}
                className="absolute -top-0.5 -right-0.5 w-4 h-4 bg-danger-500 rounded-full text-[9px] font-bold text-white flex items-center justify-center"
              >
                {unacknowledgedCount > 9 ? '9+' : unacknowledgedCount}
              </motion.span>
            )}
          </AnimatePresence>
        </button>
        {/* 用户头像 */}
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-pitch-500/20 to-frost-500/20 border border-white/[0.06] flex items-center justify-center">
            <span className="text-xs font-bold text-pitch-400">S</span>
          </div>
        </div>
      </div>
    </header>
  )
}
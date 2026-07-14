import { useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useAppStore } from '@/store'
import { monitorService, alertService } from '@/services/api'

export default function TopBar() {
  const { systemHealth, setSystemHealth, alerts, setAlerts, unacknowledgedCount,
          metricsSummary, setMetricsSummary, competition, setCompetition } = useAppStore()

  useEffect(() => {
    const fetch = async () => {
      try {
        const [h, a, m] = await Promise.all([
          monitorService.getHealth(), alertService.getAlerts({ acknowledged: false }), monitorService.getMetricsSummary(),
        ])
        setSystemHealth((h.data as any)?.data || (h.data as any))
        setAlerts((a.data as any)?.data || [])
        setMetricsSummary((m.data as any)?.data || (m.data as any))
      } catch {}
    }
    fetch()
    const i = setInterval(fetch, 15000)
    return () => clearInterval(i)
  }, [setSystemHealth, setAlerts, setMetricsSummary])

  const statusColor =
    systemHealth?.status === 'healthy' ? 'bg-field-500' :
    systemHealth?.status === 'degraded' ? 'bg-ember-500' : 'bg-danger-500'

  return (
    <header className="h-14 border-b border-surface-border bg-surface-canvas/80 backdrop-blur-sm flex items-center justify-between px-5">
      {/* Left: Status + Competition */}
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2">
          <span className={`w-1.5 h-1.5 rounded-full ${statusColor}`} />
          <span className="text-xs text-ink-muted">
            {systemHealth?.status === 'healthy' ? '系统正常' :
             systemHealth?.status === 'degraded' ? '性能下降' : '异常'}
          </span>
        </div>
        <span className="w-px h-3.5 bg-white/[0.08]" />

        {/* Competition toggle — new design */}
        <div className="inline-flex bg-white/[0.04] rounded-md p-0.5 border border-white/[0.06]">
          <button
            onClick={() => setCompetition('wc')}
            className={`relative px-2.5 py-1 rounded text-[11px] font-medium transition-colors duration-150 ${
              competition === 'wc'
                ? 'text-field-400 bg-field-500/12'
                : 'text-ink-muted hover:text-ink-secondary'
            }`}
          >世界杯</button>
          <button
            onClick={() => setCompetition('league')}
            className={`relative px-2.5 py-1 rounded text-[11px] font-medium transition-colors duration-150 ${
              competition === 'league'
                ? 'text-frost-400 bg-frost-500/12'
                : 'text-ink-muted hover:text-ink-secondary'
            }`}
          >五大联赛</button>
        </div>
      </div>

      {/* Right */}
      <div className="flex items-center gap-3">
        <button className="relative p-1.5 rounded-md hover:bg-white/[0.04] transition-colors">
          <svg className="w-4 h-4 text-ink-muted hover:text-ink-secondary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5">
            <path strokeLinecap="round" strokeLinejoin="round" d="M14.857 17.082a23.848 23.848 0 005.454-1.31A8.967 8.967 0 0118 9.75v-.7V9A6 6 0 006 9v.75a8.967 8.967 0 01-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 01-5.714 0m5.714 0a3 3 0 11-5.714 0" />
          </svg>
          <AnimatePresence>
            {unacknowledgedCount > 0 && (
              <motion.span initial={{ scale: 0 }} animate={{ scale: 1 }} exit={{ scale: 0 }}
                className="absolute -top-0.5 -right-0.5 w-3.5 h-3.5 bg-danger-500 rounded-full text-[8px] font-bold text-white flex items-center justify-center"
              >{unacknowledgedCount > 9 ? '9+' : unacknowledgedCount}</motion.span>
            )}
          </AnimatePresence>
        </button>

        <div className="w-7 h-7 rounded-md bg-gradient-to-br from-field-500/20 to-frost-500/20 border border-white/[0.06] flex items-center justify-center">
          <span className="text-[11px] font-semibold text-field-400">S</span>
        </div>
      </div>
    </header>
  )
}

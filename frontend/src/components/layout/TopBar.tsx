import { useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useAppStore } from '@/store'

// 与 services/api.ts 保持一致: 默认同源(空串), 规避 localhost vs 127.0.0.1 的 CORS 跨域;
// 仅 Docker/远程跨机部署时才设 VITE_BRIDGE_URL=http://<host>:9000。
// (此前硬编码 localhost:9000, 浏览器经 127.0.0.1:9000 加载时 /health 被 CORS 打死,
//  导致 TopBar 健康点长期误显红色"异常"。)
const BRIDGE_URL = ((import.meta as any).env?.VITE_BRIDGE_URL || '').trim()

export default function TopBar() {
  const { systemHealth, setSystemHealth, alerts, setAlerts, unacknowledgedCount,
          metricsSummary, setMetricsSummary,
          modelType, modelCalibratedOn,
          theme, setTheme } = useAppStore()

  useEffect(() => {
    const poll = async () => {
      // bridge_service /health (主 API /api/v1/* 不可用时静默降级)
      try {
        const ctrl = new AbortController()
        const t = setTimeout(() => ctrl.abort(), 3000)
        const r = await window.fetch(`${BRIDGE_URL}/health`, { signal: ctrl.signal })
        clearTimeout(t)
        const d = await r.json()
        setSystemHealth({ status: d?.ok ? 'healthy' : 'degraded', ...(typeof d === 'object' ? d : {}) })
      } catch { /* bridge 未就绪时静默 */ }
    }
    poll()
    const i = setInterval(poll, 15000)
    return () => clearInterval(i)
  }, [setSystemHealth])

  // 主题: 从 localStorage 恢复并应用到 <html data-theme>, 避免首屏闪烁
  useEffect(() => {
    const saved = (localStorage.getItem('sx-theme') as 'dark' | 'light' | null) || 'dark'
    setTheme(saved)
    document.documentElement.dataset.theme = saved
  }, [setTheme])

  const toggleTheme = () => {
    const next = theme === 'dark' ? 'light' : 'dark'
    setTheme(next)
    document.documentElement.dataset.theme = next
    localStorage.setItem('sx-theme', next)
  }

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

        {/* 赛事模型路由: 后端按赛事类型自动匹配杯赛/联赛, 前端绝不自己分类 (读 store) */}
        {(() => {
          let label: string
          if (modelType === 'cup' && modelCalibratedOn === 'world_cup') label = '当前模型：杯赛（世界杯校准）'
          else if (modelType === 'cup' && modelCalibratedOn === 'none') label = '当前模型：杯赛（沿用世界杯参数，未独立校准）'
          else if (modelType === 'league') label = '当前模型：联赛'
          else label = '当前模型：自动'
          return (
            <div className="inline-flex items-center bg-white/[0.04] rounded-md px-2.5 py-1 border border-white/[0.06]">
              <span className="text-[11px] font-medium text-ink-secondary">{label}</span>
            </div>
          )
        })()}
      </div>

      {/* Right */}
      <div className="flex items-center gap-3">
        <button onClick={toggleTheme} title={theme === 'dark' ? '切换到白天模式' : '切换到黑夜模式'}
          className="p-1.5 rounded-md hover:bg-ink-primary/[0.06] transition-colors">
          {theme === 'dark' ? (
            <svg className="w-4 h-4 text-ink-secondary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5">
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z" />
            </svg>
          ) : (
            <svg className="w-4 h-4 text-ink-secondary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5">
              <circle cx="12" cy="12" r="4.5" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 2v2.5M12 19.5V22M4.22 4.22l1.77 1.77M18.01 18.01l1.77 1.77M2 12h2.5M19.5 12H22M4.22 19.78l1.77-1.77M18.01 5.99l1.77-1.77" />
            </svg>
          )}
        </button>
        <button className="relative p-1.5 rounded-md hover:bg-ink-primary/[0.06] transition-colors">
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

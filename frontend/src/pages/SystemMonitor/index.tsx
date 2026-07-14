import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { monitorService, alertService } from '@/services/api'
import type { SystemHealth, Alert, MetricsSummary } from '@/types'
function HealthCard({ title, status, value, unit, icon }: {
  title: string; status: 'healthy' | 'degraded' | 'down'; value: string; unit?: string; icon: React.ReactNode
}) {
  return (
    <div className="glass-card p-5">
      <div className="flex items-center justify-between mb-3">
        <span className="text-[10px] font-medium uppercase tracking-wider text-white/30">{title}</span>
        <span className={`status-dot-${status}`} />
      </div>
      <div className="flex items-end gap-1">
        <span className="text-2xl font-bold font-display text-white">{value}</span>
        {unit && <span className="text-xs text-white/30 mb-1">{unit}</span>}
      </div>
      <div className="mt-2">{icon}</div>
    </div>
  )
}
export default function SystemMonitor() {
  const [health, setHealth] = useState<SystemHealth | null>(null)
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [metrics, setMetrics] = useState<MetricsSummary | null>(null)
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    const fetchData = async () => {
      try {
        setLoading(true)

        // 各 API 独立调用，一个失败不影响其他
        try {
          const healthRes = await monitorService.getHealth()
          setHealth((healthRes.data as any)?.data || healthRes.data as any)
        } catch {
          setHealth(null)
        }

        try {
          const alertsRes = await alertService.getAlerts()
          setAlerts((alertsRes.data as any)?.data || (alertsRes.data as any)?.alerts || [])
        } catch {
          setAlerts([])
        }

        try {
          const metricsRes = await monitorService.getMetricsSummary()
          setMetrics((metricsRes.data as any)?.data || metricsRes.data as any)
        } catch {
          setMetrics(null)
        }
      } finally {
        setLoading(false)
      }
    }
    fetchData()
    const interval = setInterval(fetchData, 10000)
    return () => clearInterval(interval)
  }, [])
  return (
    <div className="space-y-6">
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
        <h1 className="text-2xl font-black font-display text-white tracking-tight">系统监控</h1>
        <p className="text-sm text-white/40 mt-1">实时指标 · 健康状态 · 告警管理</p>
      </motion.div>
      {/* 健康状态卡片组 */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1 }}
        className="grid grid-cols-2 md:grid-cols-4 gap-4"
      >
        <HealthCard
          title="系统状态"
          status={health?.status ?? 'healthy'}
          value={health?.status === 'healthy' ? '正常' : health?.status === 'degraded' ? '降级' : '异常'}
          icon={<div className="text-xs text-white/20">运行 {health ? `${Math.floor(health.uptime / 3600)}h` : '--'}</div>}
        />
        <HealthCard
          title="API延迟"
          status={health?.apiLatency && health.apiLatency < 100 ? 'healthy' : health?.apiLatency && health.apiLatency < 200 ? 'degraded' : 'down'}
          value={health?.apiLatency?.toString() ?? '--'}
          unit="ms"
          icon={<div className="text-xs text-white/20">预测 {health?.predictionLatency ?? '--'}ms</div>}
        />
        <HealthCard
          title="模型健康"
          status={health?.modelHealth ?? 'healthy'}
          value={health?.modelHealth === 'healthy' ? '正常' : health?.modelHealth === 'degraded' ? '降级' : '异常'}
          icon={<div className="text-xs text-white/20">数据库 {health?.databaseHealth === 'healthy' ? '正常' : '异常'}</div>}
        />
        <HealthCard
          title="资源使用"
          status={health?.cpuUsage && health.cpuUsage < 70 ? 'healthy' : 'degraded'}
          value={`${health?.cpuUsage ?? '--'}/${health?.memoryUsage ?? '--'}`}
          unit="% CPU/内存"
          icon={<div className="text-xs text-white/20">请求量 {metrics?.apiRequestsPerMin ?? '--'}/min</div>}
        />
      </motion.div>
      {/* 指标详情 + 告警列表 */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* 实时指标 */}
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.2 }}
          className="glass-card p-6"
        >
          <h3 className="text-xs font-bold uppercase tracking-widest text-white/30 mb-4">实时指标</h3>
          <div className="space-y-4">
            {[
              { label: 'API请求量', value: metrics?.apiRequestsPerMin ?? 0, unit: 'req/min', max: 200 },
              { label: '平均响应时间', value: metrics?.avgResponseTime ?? 0, unit: 'ms', max: 200 },
              { label: '预测请求量', value: metrics?.predictionRequestsPerMin ?? 0, unit: 'req/min', max: 100 },
              { label: '错误率', value: metrics?.errorRate ?? 0, unit: '%', max: 5 },
            ].map((item) => {
              const pct = item.label === '错误率' ? (item.value as number) / item.max * 100 : (item.value as number) / item.max * 100
              return (
                <div key={item.label}>
                  <div className="flex justify-between text-xs mb-1">
                    <span className="text-white/50">{item.label}</span>
                    <span className="text-white/70 font-mono">{item.value}{item.unit}</span>
                  </div>
                  <div className="h-1.5 rounded-full bg-white/5">
                    <motion.div
                      initial={{ width: 0 }}
                      animate={{ width: `${Math.min(pct, 100)}%` }}
                      transition={{ duration: 1, ease: 'easeOut' }}
                      className={`h-full rounded-full ${
                        pct > 80 ? 'bg-danger-500' : pct > 60 ? 'bg-ember-500' : 'bg-pitch-500'
                      }`}
                    />
                  </div>
                </div>
              )
            })}
          </div>
        </motion.div>
        {/* 告警列表 */}
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.3 }}
          className="glass-card p-6"
        >
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-xs font-bold uppercase tracking-widest text-white/30">告警列表</h3>
            <span className="text-[10px] text-white/30">{(alerts || []).filter(a => !a.acknowledged).length} 条未处理</span>
          </div>
          <div className="space-y-2">
            <AnimatePresence>
              {(alerts || []).map((alert, i) => (
                <motion.div
                  key={alert.id}
                  initial={{ opacity: 0, x: -10 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: i * 0.05 }}
                  className={`p-3 rounded-xl border ${
                    alert.severity === 'critical' ? 'border-danger-500/20 bg-danger-500/5' :
                    alert.severity === 'warning' ? 'border-ember-500/20 bg-ember-500/5' :
                    'border-frost-500/20 bg-frost-500/5'
                  }`}
                >
                  <div className="flex items-start gap-3">
                    <span className={`w-1.5 h-1.5 rounded-full mt-1.5 ${
                      alert.severity === 'critical' ? 'bg-danger-500' :
                      alert.severity === 'warning' ? 'bg-ember-500' : 'bg-frost-500'
                    }`} />
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-medium text-white/70">{alert.title}</p>
                      <p className="text-[10px] text-white/30 mt-0.5">{alert.message}</p>
                      <p className="text-[9px] text-white/20 mt-1">{alert.timestamp}</p>
                    </div>
                    {!alert.acknowledged && (
                      <span className="text-[9px] font-medium text-danger-400 px-1.5 py-0.5 rounded bg-danger-500/10">未处理</span>
                    )}
                  </div>
                </motion.div>
              ))}
            </AnimatePresence>
          </div>
        </motion.div>
      </div>
    </div>
  )
}
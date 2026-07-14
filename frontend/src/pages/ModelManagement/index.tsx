import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { modelService } from '@/services/api'
import type { ModelVersion } from '@/types'
export default function ModelManagement() {
  const [models, setModels] = useState<ModelVersion[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedModels, setSelectedModels] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const fetchModels = async () => {
      try {
        setLoading(true)
        const res = await modelService.getVersions()
        const raw = (res.data as any)?.data || res.data as any
        // 后端返回 model_id/model_type/registered_at，前端用 id/name/deployedAt，做映射
        const modelsList = raw?.models || (Array.isArray(raw) ? raw : []) || []
        setModels(modelsList.map((m: any) => ({
          ...m,
          id: m.model_id || m.id,
          name: m.name || m.model_type || m.model_id || '未命名',
          version: m.version || m.model_id || '--',
          deployedAt: m.deployedAt || m.registered_at || '--',
          accuracy: m.metrics?.accuracy ?? m.accuracy ?? 0,
        })))
      } catch {
        // API失败时保持空列表，不模拟假数据
        console.error('获取模型版本失败')
        setModels([])
      } finally {
        setLoading(false)
      }
    }
    fetchModels()
  }, [])
  const fmtPct = (v: number | undefined): string => {
    if (v == null || Number.isNaN(v)) return '--'
    return `${(v * 100).toFixed(1)}%`
  }
  const barWidth = (v: number | undefined): string => {
    if (v == null || Number.isNaN(v)) return '0%'
    return `${Math.min(Math.max(v * 100, 0), 100)}%`
  }
  const handleDeploy = async (modelId: string) => {
    try {
      setError(null)
      await modelService.deployModel(modelId)
      setModels((prev) =>
        prev.map((m) => ({
          ...m,
          status: m.id === modelId ? 'active' as const : 'inactive' as const,
        }))
      )
    } catch (err: any) {
      console.error('模型部署失败:', err)
      const status = err?.response?.status
      if (status === 403) {
        setError('部署失败：需要管理员权限。请使用 admin 账号登录（默认密码: 哨响AI2026）')
      } else if (status === 401) {
        setError('部署失败：未登录，请先登录后再操作')
      } else {
        setError('部署失败，请检查后端服务是否运行')
      }
    }
  }
  const handleRollback = async (versionId: string) => {
    try {
      setError(null)
      await modelService.rollbackModel(versionId)
      // 刷新列表
      const res = await modelService.getVersions()
      setModels((res.data as any)?.models || [])
    } catch (err: any) {
      console.error('模型回滚失败:', err)
      const status = err?.response?.status
      if (status === 403) {
        setError('回滚失败：需要管理员权限。请使用 admin 账号登录')
      } else if (status === 401) {
        setError('回滚失败：未登录，请先登录后再操作')
      } else {
        setError('回滚失败，请检查后端服务是否运行')
      }
    }
  }
  return (
    <div className="space-y-6">
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
        <h1 className="text-2xl font-black font-display text-white tracking-tight">模型管理</h1>
        <p className="text-sm text-white/40 mt-1">模型版本管理 · 部署与回滚 · 性能对比</p>
      </motion.div>
      {error && (
        <motion.div
          initial={{ opacity: 0, y: -5 }}
          animate={{ opacity: 1, y: 0 }}
          className="bg-danger-500/10 border border-danger-500/20 rounded-xl px-4 py-3 flex items-center gap-2"
        >
          <span className="w-1.5 h-1.5 rounded-full bg-danger-500" />
          <span className="text-xs text-danger-400">{error}</span>
          <button onClick={() => setError(null)} className="ml-auto text-white/30 hover:text-white/60 text-xs">✕</button>
        </motion.div>
      )}
      {/* 模型版本列表 */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1 }}
        className="glass-card p-6"
      >
        <div className="flex items-center justify-between mb-6">
          <h3 className="text-xs font-bold uppercase tracking-widest text-white/30">模型版本</h3>
          <button className="btn-primary text-xs">+ 注册新模型</button>
        </div>
        {loading ? (
          <div className="space-y-3">
            {[1, 2, 3].map((i) => (
              <div key={i} className="skeleton h-16 w-full" />
            ))}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>模型名称</th>
                  <th>版本</th>
                  <th>准确率</th>
                  <th>部署时间</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <AnimatePresence>
                  {models.map((model, i) => (
                    <motion.tr
                      key={model.id}
                      initial={{ opacity: 0, y: 10 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: i * 0.05 }}
                    >
                      <td className="font-medium text-white/80">{model.name}</td>
                      <td><span className="font-mono text-xs text-white/40">{model.version}</span></td>
                      <td>
                        <div className="flex items-center gap-2">
                          <span className={`font-bold font-display ${
                            (model.accuracy || 0) > 0.74 ? 'text-pitch-400' : (model.accuracy || 0) > 0.70 ? 'text-ember-400' : 'text-white/60'
                          }`}>
                            {fmtPct(model.accuracy)}
                          </span>
                          <div className="w-16 h-1 rounded-full bg-white/5">
                            <div className={`h-full rounded-full ${
                              (model.accuracy || 0) > 0.74 ? 'bg-pitch-400' : (model.accuracy || 0) > 0.70 ? 'bg-ember-400' : 'bg-white/20'
                            }`} style={{ width: barWidth(model.accuracy) }} />
                          </div>
                        </div>
                      </td>
                      <td className="text-xs text-white/40">{model.deployedAt}</td>
                      <td>
                        <span className={`badge ${
                          model.status === 'active' ? 'badge-green' :
                          model.status === 'rollback' ? 'badge-red' : 'badge-blue'
                        }`}>
                          {model.status === 'active' ? '生产中' : model.status === 'rollback' ? '已回滚' : '未激活'}
                        </span>
                      </td>
                      <td>
                        <div className="flex gap-2">
                          <button
                            onClick={() => handleDeploy(model.id)}
                            disabled={model.status === 'active'}
                            className="btn-ghost text-xs disabled:opacity-30"
                          >
                            部署
                          </button>
                          <button
                            onClick={() => handleRollback(model.id)}
                            className="btn-ghost text-xs text-danger-400 hover:text-danger-300"
                          >
                            回滚
                          </button>
                        </div>
                      </td>
                    </motion.tr>
                  ))}
                </AnimatePresence>
              </tbody>
            </table>
          </div>
        )}
      </motion.div>
      {/* 模型对比区域 */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.2 }}
        className="glass-card p-6"
      >
        <h3 className="text-xs font-bold uppercase tracking-widest text-white/30 mb-4">性能对比</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          {['准确率', '精确率', '召回率', 'F1分数'].map((metric, i) => (
            <div key={metric} className="p-4 rounded-xl bg-white/[0.02] border border-white/[0.04]">
              <p className="stat-label mb-2">{metric}</p>
              <div className="space-y-2">
                {models.slice(0, 3).map((model) => {
                  const metricKey = { '准确率': 'accuracy', '精确率': 'precision', '召回率': 'recall', 'F1分数': 'f1' }[metric] as string
                  let val: number = 0
                  if (metric === '准确率') {
                    val = model.accuracy ?? 0
                  } else {
                    val = model.metrics?.[metricKey] ?? 0
                  }
                  const isValid = typeof val === 'number' && !Number.isNaN(val)
                  return (
                    <div key={model.id} className="flex items-center gap-2">
                      <span className="text-[10px] text-white/30 w-16 truncate">{model.name}</span>
                      <div className="flex-1 h-1.5 rounded-full bg-white/5">
                        <motion.div
                          initial={{ width: 0 }}
                          animate={{ width: isValid ? `${Math.min(Math.max(val * 100, 0), 100)}%` : '0%' }}
                          transition={{ delay: 0.3 + i * 0.1, duration: 0.8 }}
                          className="h-full rounded-full bg-gradient-to-r from-pitch-600 to-pitch-400"
                        />
                      </div>
                      <span className="text-[10px] font-mono text-white/40 w-8 text-right">
                        {isValid ? `${(val * 100).toFixed(1)}%` : '--'}
                      </span>
                    </div>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      </motion.div>
    </div>
  )
}
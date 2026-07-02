import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { modelService } from '@/services/api'
import type { ModelVersion } from '@/types'
export default function ModelManagement() {
  const [models, setModels] = useState<ModelVersion[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedModels, setSelectedModels] = useState<string[]>([])
  useEffect(() => {
    const fetchModels = async () => {
      try {
        setLoading(true)
        const res = await modelService.getVersions()
        setModels(res.data?.data || [])
      } catch {
        // 使用模拟数据
        setModels([
          { id: '1', name: 'XGBoost+Ridge', version: 'v4.1.0', accuracy: 0.742, deployedAt: '2026-06-28', status: 'active', metrics: { precision: 0.73, recall: 0.71, f1: 0.72 } },
          { id: '2', name: 'DrawExpert', version: 'v5.3', accuracy: 0.718, deployedAt: '2026-06-25', status: 'inactive', metrics: { precision: 0.70, recall: 0.68, f1: 0.69 } },
          { id: '3', name: 'Neural Network', version: 'v3.2', accuracy: 0.695, deployedAt: '2026-06-20', status: 'inactive', metrics: { precision: 0.68, recall: 0.66, f1: 0.67 } },
          { id: '4', name: 'D-Gate v5.3', version: 'v5.3', accuracy: 0.763, deployedAt: '2026-06-29', status: 'rollback', metrics: { precision: 0.75, recall: 0.74, f1: 0.74 } },
        ])
      } finally {
        setLoading(false)
      }
    }
    fetchModels()
  }, [])
  const handleDeploy = async (modelId: string) => {
    try {
      await modelService.deployModel(modelId)
      setModels((prev) =>
        prev.map((m) => ({
          ...m,
          status: m.id === modelId ? 'active' as const : 'inactive' as const,
        }))
      )
    } catch {
      // 模拟部署成功
      setModels((prev) =>
        prev.map((m) => ({
          ...m,
          status: m.id === modelId ? 'active' as const : (m.status === 'active' ? 'inactive' as const : m.status),
        }))
      )
    }
  }
  const handleRollback = async (versionId: string) => {
    try {
      await modelService.rollbackModel(versionId)
    } catch {
      // 模拟回滚
    }
  }
  return (
    <div className="space-y-6">
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
        <h1 className="text-2xl font-black font-display text-white tracking-tight">模型管理</h1>
        <p className="text-sm text-white/40 mt-1">模型版本管理 · 部署与回滚 · 性能对比</p>
      </motion.div>
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
                            model.accuracy > 0.74 ? 'text-pitch-400' : model.accuracy > 0.70 ? 'text-ember-400' : 'text-white/60'
                          }`}>
                            {(model.accuracy * 100).toFixed(1)}%
                          </span>
                          <div className="w-16 h-1 rounded-full bg-white/5">
                            <div className={`h-full rounded-full ${
                              model.accuracy > 0.74 ? 'bg-pitch-400' : model.accuracy > 0.70 ? 'bg-ember-400' : 'bg-white/20'
                            }`} style={{ width: `${model.accuracy * 100}%` }} />
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
                  const val = metric === '准确率' ? model.accuracy :
                    model.metrics?.[{ '准确率': 'accuracy', '精确率': 'precision', '召回率': 'recall', 'F1分数': 'f1' }[metric] as keyof typeof model.metrics] ?? 0
                  return (
                    <div key={model.id} className="flex items-center gap-2">
                      <span className="text-[10px] text-white/30 w-16 truncate">{model.name}</span>
                      <div className="flex-1 h-1.5 rounded-full bg-white/5">
                        <motion.div
                          initial={{ width: 0 }}
                          animate={{ width: `${(val as number) * 100}%` }}
                          transition={{ delay: 0.3 + i * 0.1, duration: 0.8 }}
                          className="h-full rounded-full bg-gradient-to-r from-pitch-600 to-pitch-400"
                        />
                      </div>
                      <span className="text-[10px] font-mono text-white/40 w-8 text-right">
                        {((val as number) * 100).toFixed(1)}%
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
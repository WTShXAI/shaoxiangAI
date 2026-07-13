import { useState, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { terminalService } from '@/services/api'
import type { TerminalMatch, DecisionCard, DataGrowthStats } from '@/types'

export default function OperatorTerminal() {
  const [matches, setMatches] = useState<TerminalMatch[]>([])
  const [selected, setSelected] = useState<TerminalMatch | null>(null)
  const [card, setCard] = useState<DecisionCard | null>(null)
  const [growth, setGrowth] = useState<DataGrowthStats | null>(null)
  const [loading, setLoading] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [error, setError] = useState('')

  // 加载当天比赛 + 数据增长统计
  const loadMatches = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [mRes, gRes] = await Promise.all([
        terminalService.getMatches(),
        terminalService.getGrowthStats(),
      ])
      const mData = mRes.data.data
      setMatches(mData?.matches || [])
      setGrowth(gRes.data.data || null)
    } catch (e: any) {
      setError('加载失败: ' + (e?.message || '网络错误'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { loadMatches() }, [loadMatches])

  // 分析指定比赛
  const analyze = async (m: TerminalMatch) => {
    setSelected(m)
    setAnalyzing(true)
    setError('')
    try {
      const res = await terminalService.analyze(m.home, m.away, m.sport_key)
      setCard(res.data.data || null)
    } catch (e: any) {
      setError('分析失败: ' + (e?.message || '网络错误'))
      setCard(null)
    } finally {
      setAnalyzing(false)
    }
  }

  const decisionColor = (d: string) => {
    if (d === 'BET') return 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20'
    if (d === 'SCAN') return 'text-amber-400 bg-amber-500/10 border-amber-500/20'
    return 'text-white/40 bg-white/[0.04] border-white/[0.08]'
  }

  return (
    <div className="space-y-5">
      {/* 页面标题 */}
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
        <h1 className="text-2xl font-black font-display text-white tracking-tight">操盘终端</h1>
        <p className="text-sm text-white/40 mt-1">多庄实时分析 · 跨庄价值层决策 · 仅建议不下单</p>
      </motion.div>

      {/* 状态栏 */}
      {growth && (
        <motion.div
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.1 }}
          className="flex flex-wrap gap-3"
        >
          {[
            { label: 'API配额', val: growth.quota_remaining ?? '?', unit: '次', warn: (growth.quota_remaining ?? 999) < 50 },
            { label: '今日采集', val: growth.today_collected, unit: '场' },
            { label: '活跃联赛', val: growth.active_leagues, unit: '个' },
            { label: '训练数据', val: `${(growth.odds_features_total / 10000).toFixed(1)}万`, unit: '行' },
            { label: '有赛果', val: growth.live_odds_raw_with_result, unit: '场' },
          ].map((s) => (
            <div key={s.label} className={`px-3 py-1.5 rounded-lg border text-xs font-medium ${
              s.warn ? 'border-amber-500/30 bg-amber-500/8 text-amber-400' : 'border-white/[0.08] bg-white/[0.03] text-white/60'
            }`}>
              <span className="text-white/30">{s.label}</span>{' '}
              <span className="text-white font-semibold">{s.val}</span>
              <span className="text-white/20 ml-0.5">{s.unit}</span>
            </div>
          ))}
          <button onClick={loadMatches} disabled={loading}
            className="px-3 py-1.5 rounded-lg border border-field-500/30 bg-field-500/8 text-field-400 text-xs font-medium hover:bg-field-500/15 transition-colors">
            {loading ? '刷新中...' : '🔄 刷新'}
          </button>
        </motion.div>
      )}

      <div className="flex gap-5 flex-col lg:flex-row">
        {/* 左栏: 比赛列表 */}
        <motion.div
          initial={{ opacity: 0, x: -10 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: 0.15 }}
          className="lg:w-[380px] flex-shrink-0 space-y-2"
        >
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-white/70">今日可决策比赛</h2>
            <span className="text-xs text-white/30">{matches.length} 场</span>
          </div>

          {loading && (
            <div className="space-y-2">
              {[1,2,3].map(i => (
                <div key={i} className="h-20 rounded-lg bg-white/[0.04] animate-pulse" />
              ))}
            </div>
          )}

          {!loading && matches.length === 0 && (
            <div className="text-center py-12 text-white/30 text-sm">
              今日无可决策比赛<br />
              <span className="text-xs">(需 ≥2 庄家赔率, 等待定时采集或手动拉取)</span>
            </div>
          )}

          <AnimatePresence>
            {matches.map((m, i) => (
              <motion.div
                key={`${m.home}-${m.away}`}
                initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.03 }}
                onClick={() => analyze(m)}
                className={`p-3 rounded-lg border cursor-pointer transition-all duration-200 ${
                  selected?.home === m.home && selected?.away === m.away
                    ? 'border-field-500/40 bg-field-500/6'
                    : 'border-white/[0.06] bg-white/[0.02] hover:bg-white/[0.05] hover:border-white/[0.12]'
                }`}
              >
                <div className="flex items-center justify-between">
                  <div className="font-medium text-sm text-white">
                    {m.home} <span className="text-white/20 text-xs mx-1">vs</span> {m.away}
                  </div>
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-field-500/15 text-field-400 font-medium">
                    {m.bookmakers_count}庄
                  </span>
                </div>
                <div className="flex items-center gap-3 mt-1.5">
                  <span className="text-[11px] text-white/25">{m.league}</span>
                  <span className="text-[11px] text-white/25">
                    {m.commence_time ? new Date(m.commence_time).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '--:--'}
                  </span>
                </div>
                <div className="flex gap-2 mt-1.5 text-[11px] text-white/35">
                  <span>H {m.odds_h?.toFixed(2) || '-'}</span>
                  <span>D {m.odds_d?.toFixed(2) || '-'}</span>
                  <span>A {m.odds_a?.toFixed(2) || '-'}</span>
                </div>
              </motion.div>
            ))}
          </AnimatePresence>
        </motion.div>

        {/* 右栏: 决策卡片 */}
        <motion.div
          initial={{ opacity: 0, x: 10 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: 0.2 }}
          className="flex-1 min-w-0"
        >
          {!selected && (
            <div className="flex items-center justify-center h-64 text-white/25 text-sm">
              选择左侧比赛查看多庄决策卡片
            </div>
          )}

          {analyzing && (
            <div className="flex items-center justify-center h-64 gap-2">
              <div className="w-4 h-4 rounded-full border-2 border-field-400 border-t-transparent animate-spin" />
              <span className="text-white/40 text-sm">实时拉取多庄赔率并分析...</span>
            </div>
          )}

          {error && (
            <div className="p-4 rounded-lg border border-red-500/20 bg-red-500/5 text-red-400 text-sm">
              ⚠️ {error}
            </div>
          )}

          {card && !analyzing && (
            <div className="space-y-4">
              {/* 决策标签 */}
              <div className="flex items-center gap-3">
                <h2 className="text-lg font-bold text-white">
                  {card.fixture.home} vs {card.fixture.away}
                </h2>
                <span className={`text-xs px-2.5 py-1 rounded-full border font-bold ${decisionColor(card.decision)}`}>
                  {card.decision === 'BET' ? '建仓' : card.decision === 'SCAN' ? '观察' : '观望'}
                </span>
                {card.books_count > 0 && (
                  <span className="text-[11px] text-white/30">{card.books_count}庄</span>
                )}
              </div>

              {/* 决策文本 */}
              {card.decision_text && (
                <p className="text-xs text-white/40 bg-white/[0.03] rounded-lg px-3 py-2 border border-white/[0.06]">
                  {card.decision_text}
                </p>
              )}

              {/* 赔率区 + 隐含概率条 */}
              <div className="grid grid-cols-3 gap-3">
                {[
                  { label: '主胜', key: 'h', odds: card.odds?.oh, prob: card.market_prob?.h },
                  { label: '平局', key: 'd', odds: card.odds?.od, prob: card.market_prob?.d },
                  { label: '客胜', key: 'a', odds: card.odds?.oa, prob: card.market_prob?.a },
                ].map((x) => (
                  <div key={x.key} className="p-3 rounded-lg border border-white/[0.06] bg-white/[0.02] text-center">
                    <div className="text-[11px] text-white/30">{x.label}</div>
                    <div className="text-lg font-bold text-white">{x.odds?.toFixed(2) || '-'}</div>
                    <div className="mt-1.5 h-1.5 rounded-full bg-white/[0.06] overflow-hidden">
                      <div
                        className="h-full rounded-full bg-field-500/60 transition-all duration-500"
                        style={{ width: `${(x.prob || 0) * 100}%` }}
                      />
                    </div>
                    <div className="text-[10px] text-white/25 mt-0.5">{((x.prob || 0) * 100).toFixed(1)}%</div>
                  </div>
                ))}
              </div>

              {/* EV/凯利 行 */}
              {card.rows && card.rows.length > 0 && (
                <div>
                  <h3 className="text-xs font-semibold text-white/40 mb-2">EV / 凯利 分析</h3>
                  <div className="space-y-1">
                    {card.rows.map((r, i) => (
                      <div key={i} className={`flex items-center justify-between px-3 py-1.5 rounded text-xs ${
                        card.best_direction === r.outcome ? 'bg-field-500/8 border border-field-500/15' : ''
                      }`}>
                        <span className="text-white/60 w-10">{r.outcome}</span>
                        <span className="text-white/40 w-16 text-right">@{r.odds?.toFixed(2)}</span>
                        <span className="text-white/40 w-20 text-right">
                          edge {r.edge_pct?.toFixed(1)}%
                        </span>
                        <span className={`w-16 text-right font-medium ${(r.ev || 0) > 0 ? 'text-emerald-400' : 'text-white/30'}`}>
                          EV {r.ev?.toFixed(1)}%
                        </span>
                        <span className="w-20 text-right text-white/30">
                          凯利 {(r.kelly_half || 0).toFixed(3)}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 跨庄信号 */}
              {card.softline && (
                <div className="p-3 rounded-lg border border-white/[0.06] bg-white/[0.02]">
                  <h3 className="text-xs font-semibold text-white/40 mb-1.5">跨庄信号</h3>
                  <div className="flex flex-wrap gap-2 text-[11px]">
                    {card.softline.disagreement_detected && (
                      <span className="px-2 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20">
                        庄家分歧
                      </span>
                    )}
                    {card.softline.softline_fade_applied && (
                      <span className="px-2 py-0.5 rounded bg-field-500/10 text-field-400 border border-field-500/20">
                        热门淡化 (0.41)
                      </span>
                    )}
                    {card.softline.softline_adjusted_probs && (
                      <span className="text-white/25">
                        adj: [{card.softline.softline_adjusted_probs.map((p: number) => (p * 100).toFixed(1) + '%').join(', ')}]
                      </span>
                    )}
                  </div>
                </div>
              )}

              {/* 操盘手视图 */}
              {card.operator_view && (
                <div className="p-3 rounded-lg border border-white/[0.06] bg-white/[0.02]">
                  <div className="flex items-center justify-between mb-2">
                    <h3 className="text-xs font-semibold text-white/40">操盘手规则触发</h3>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                      card.operator_view.stake_hint === '重仓' ? 'bg-emerald-500/15 text-emerald-400' :
                      card.operator_view.stake_hint === '回避' ? 'bg-red-500/15 text-red-400' :
                      'bg-white/[0.04] text-white/30'
                    }`}>
                      {card.operator_view.stake_hint}
                    </span>
                  </div>
                  <p className="text-xs text-white/50 mb-1.5">{card.operator_view.verdict}</p>
                  <div className="space-y-1">
                    {card.operator_view.rules_fired.map((r) => (
                      <div key={r.id} className="flex items-start gap-2 text-[11px]">
                        <span className={`flex-shrink-0 w-4 h-4 rounded flex items-center justify-center text-[9px] font-bold ${
                          r.color === 'red' ? 'bg-red-500/15 text-red-400' :
                          r.color === 'amber' ? 'bg-amber-500/15 text-amber-400' :
                          r.color === 'green' ? 'bg-emerald-500/15 text-emerald-400' :
                          'bg-field-500/15 text-field-400'
                        }`}>
                          {r.id.replace('R', '')}
                        </span>
                        <div>
                          <span className="text-white/60">{r.label}:</span>
                          <span className="text-white/35 ml-1">{r.detail}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                  <p className="text-[10px] text-white/20 mt-2">
                    主信号 {card.operator_view.primary_signal} · 置信 {card.operator_view.confidence_pct}%
                  </p>
                </div>
              )}

              {/* 平局预警 */}
              {card.draw_alert && (
                <div className="px-3 py-2 rounded-lg border border-amber-500/20 bg-amber-500/5 text-amber-400 text-xs">
                  ⚠️ 平局预警 (P平≥26%) — 考虑防平策略
                </div>
              )}

              {/* 子市场概览 */}
              {card.sub_markets && Object.keys(card.sub_markets).length > 0 && (
                <div className="p-3 rounded-lg border border-white/[0.06] bg-white/[0.02]">
                  <h3 className="text-xs font-semibold text-white/40 mb-1.5">子市场信号</h3>
                  <div className="flex flex-wrap gap-1.5 text-[10px]">
                    {Object.entries(card.sub_markets).map(([k, v]: [string, any]) => (
                      <span key={k} className="px-2 py-0.5 rounded bg-white/[0.04] text-white/40 border border-white/[0.06]">
                        {k}: {v?.decision || 'SCAN'}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </motion.div>
      </div>
    </div>
  )
}

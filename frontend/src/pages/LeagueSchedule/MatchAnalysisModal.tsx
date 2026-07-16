import { useState, useEffect } from 'react'
import { motion } from 'framer-motion'
import { terminalService } from '@/services/api'
import type { TerminalDecisionCard, ValueLayerRow } from '@/types'

// ── 辅助格式化 ──
const pct = (v: number | undefined, digits = 1) =>
  typeof v === 'number' && !isNaN(v) ? (v * 100).toFixed(digits) + '%' : '—'
const num = (v: number | undefined, digits = 2) =>
  typeof v === 'number' && !isNaN(v) ? v.toFixed(digits) : '—'
const edgeColor = (e: number) => e > 0.02 ? 'text-accent' : e > 0 ? 'text-ember-400' : 'text-white/30'

// ── 价值层三行 (H/D/A) ──
function ValueRows({ rows }: { rows: ValueLayerRow[] }) {
  if (!rows || !rows.length) return null
  const dirMap: Record<string, string> = { H: '主胜', D: '平局', A: '客胜' }
  return (
    <div className="space-y-1.5">
      {rows.map((r) => {
        const isBest = r.ev > 0
        return (
          <div key={r.outcome} className={`flex items-center gap-3 px-3 py-2 rounded-lg ${isBest ? 'bg-accent/8 border border-accent/15' : 'bg-white/[0.02]'}`}>
            <span className="text-sm text-white/80 w-12">{dirMap[r.outcome] || r.outcome}</span>
            <span className="font-mono text-sm text-white/60 w-14">@{num(r.odds)}</span>
            <span className="font-mono text-xs text-white/40 w-16">P{num(r.model_prob, 3)}</span>
            <span className={`font-mono text-sm font-semibold w-16 text-right ${edgeColor(r.edge)}`}>
              {r.edge_pct >= 0 ? '+' : ''}{num(r.edge_pct, 1)}%
            </span>
            <span className={`font-mono text-xs w-16 text-right ${r.ev > 0 ? 'text-accent' : 'text-white/30'}`}>
              EV{r.ev_pct >= 0 ? '+' : ''}{num(r.ev_pct, 1)}%
            </span>
            <span className="font-mono text-xs text-white/40 w-12 text-right">k{num(r.kelly_half, 3)}</span>
          </div>
        )
      })}
    </div>
  )
}

// ── 主弹窗 ──
export default function MatchAnalysisModal({
  home, away, sportKey, odds, onClose,
}: { home: string; away: string; sportKey: string; odds?: { h: number; d: number; a: number }; onClose: () => void }) {
  const [card, setCard] = useState<TerminalDecisionCard | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showSub, setShowSub] = useState(false)

  useEffect(() => {
    let alive = true
    setLoading(true); setError(null)
    terminalService.analyze(home, away, sportKey, odds)
      .then((res) => {
        if (!alive) return
        const d = (res.data as any)?.data || res.data
        if (d?.error) { setError(d.error) }
        else { setCard(d as TerminalDecisionCard) }
      })
      .catch((e) => { if (alive) setError(e?.response?.data?.detail || e?.message || '分析失败') })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [home, away, sportKey])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 backdrop-blur-sm p-4" onClick={onClose}>
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95 }}
        onClick={(e) => e.stopPropagation()}
        className="bg-accent-card border border-white/[0.08] rounded-xl w-full max-w-2xl max-h-[88vh] overflow-y-auto shadow-2xl"
      >
        {/* 弹窗头 */}
        <div className="sticky top-0 z-10 bg-accent-card/95 backdrop-blur px-6 py-4 border-b border-white/[0.06] flex items-center justify-between">
          <div>
            <div className="text-[10px] font-mono text-accent tracking-widest uppercase">LIVE DECODE</div>
            <h3 className="text-lg font-bold text-white mt-0.5">{home} <span className="text-white/30 mx-1 text-sm font-normal">vs</span> {away}</h3>
          </div>
          <button onClick={onClose} className="w-8 h-8 rounded-lg flex items-center justify-center text-white/40 hover:text-white hover:bg-white/[0.06]">
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2"><path strokeLinecap="round" d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>

        <div className="p-6 space-y-5">
          {/* Loading */}
          {loading && (
            <div className="py-16 text-center">
              <div className="inline-block w-8 h-8 border-2 border-accent/30 border-t-accent rounded-full animate-spin mb-3" />
              <p className="text-sm text-white/50">全链路分析中 · OIP波胆 / 让球 / 价值层 / 子市场…</p>
              <p className="text-[11px] text-white/30 mt-1">直接用盘口赔率计算,通常 1 秒内完成</p>
            </div>
          )}

          {/* Error */}
          {error && !loading && (
            <div className="py-12 text-center">
              <div className="text-3xl mb-3">⚠️</div>
              <p className="text-sm text-white/70 font-medium">分析失败</p>
              <p className="text-[11px] text-white/40 mt-2 max-w-sm mx-auto">{error}</p>
              <p className="text-[10px] text-white/25 mt-3">提示: 该比赛可能无盘口赔率数据,或赔率未采集</p>
            </div>
          )}

          {/* 决策卡 */}
          {card && !loading && !error && (
            <>
              {/* 决策结论条 */}
              <div className={`flex items-center justify-between rounded-xl px-5 py-4 ${
                card.decision === 'BET'
                  ? 'bg-accent/10 border border-accent/20'
                  : 'bg-white/[0.03] border border-white/[0.06]'
              }`}>
                <div>
                  <div className="text-[10px] font-mono text-white/40 uppercase tracking-wider">决策</div>
                  <div className="flex items-center gap-2 mt-1">
                    <span className={`text-2xl font-black ${card.decision === 'BET' ? 'text-accent' : 'text-white/50'}`}>
                      {card.decision}
                    </span>
                    <span className="text-sm text-white/60">· {card.direction}</span>
                    {card.best_edge_pct !== undefined && card.best_edge_pct > 0 && (
                      <span className="font-mono text-sm text-accent">edge +{num(card.best_edge_pct, 1)}%</span>
                    )}
                  </div>
                  <p className="text-[11px] text-white/40 mt-1">{card.decision_text}</p>
                </div>
                <div className="text-right">
                  <div className="text-[10px] font-mono text-white/40">覆盖庄家</div>
                  <div className="font-mono text-2xl font-bold text-white mt-1">{card.books_count}</div>
                  {card.draw_alert && <div className="text-[10px] text-ember-400 mt-1">⚠ 平局预警</div>}
                </div>
              </div>

              {/* 赔率 + 市场隐含 */}
              <div className="grid grid-cols-3 gap-2">
                {[
                  { l: '主胜', o: card.odds?.oh, p: card.market_prob?.h },
                  { l: '平局', o: card.odds?.od, p: card.market_prob?.d },
                  { l: '客胜', o: card.odds?.oa, p: card.market_prob?.a },
                ].map((x) => (
                  <div key={x.l} className="bg-accent-inner rounded-lg px-3 py-2.5 text-center">
                    <div className="text-[10px] text-white/40">{x.l}</div>
                    <div className="font-mono text-lg font-bold text-white mt-0.5">{num(x.o)}</div>
                    <div className="font-mono text-[10px] text-accent/70">{pct(x.p)}</div>
                  </div>
                ))}
              </div>

              {/* 价值层 */}
              {card.rows && card.rows.length > 0 && (
                <div>
                  <div className="text-[10px] font-mono text-accent tracking-widest uppercase mb-2">价值层 / VALUE LAYER</div>
                  <ValueRows rows={card.rows} />
                </div>
              )}

              {/* OIP 波胆 */}
              {card.oip && (
                <div>
                  <div className="text-[10px] font-mono text-accent tracking-widest uppercase mb-2">OIP 波胆模型 / SCORE</div>
                  <div className="grid grid-cols-2 gap-3">
                    <div className="bg-accent-inner rounded-lg px-3 py-2">
                      <div className="text-[10px] text-white/40">期望进球 λ</div>
                      <div className="font-mono text-sm text-white mt-0.5">
                        主 {num(card.oip.lambda_h, 2)} · 客 {num(card.oip.lambda_a, 2)}
                      </div>
                    </div>
                    <div className="bg-accent-inner rounded-lg px-3 py-2">
                      <div className="text-[10px] text-white/40">Top3 比分</div>
                      <div className="font-mono text-xs text-white/80 mt-0.5">
                        {(card.oip.top3_scores || []).map((s, i) => `${s}(${pct(card.oip!.top3_prob?.[i], 0)})`).join('  ')}
                      </div>
                    </div>
                  </div>
                  <div className="flex gap-2 mt-2">
                    {card.oip.over15 !== undefined && <span className="text-[10px] font-mono text-white/40 bg-white/[0.03] px-2 py-1 rounded">≥1.5 {pct(card.oip.over15, 0)}</span>}
                    {card.oip.over25 !== undefined && <span className="text-[10px] font-mono text-white/40 bg-white/[0.03] px-2 py-1 rounded">≥2.5 {pct(card.oip.over25, 0)}</span>}
                    {card.oip.over35 !== undefined && <span className="text-[10px] font-mono text-white/40 bg-white/[0.03] px-2 py-1 rounded">≥3.5 {pct(card.oip.over35, 0)}</span>}
                  </div>
                </div>
              )}

              {/* 操盘手视角 */}
              {card.operator_view && (
                <div>
                  <div className="text-[10px] font-mono text-accent tracking-widest uppercase mb-2">操盘手视角 / PLAYBOOK</div>
                  {card.operator_view.verdict && (
                    <p className="text-sm text-white/70 bg-accent-inner rounded-lg px-3 py-2 mb-2">{card.operator_view.verdict}</p>
                  )}
                  {card.operator_view.rules && card.operator_view.rules.length > 0 && (
                    <div className="space-y-1">
                      {card.operator_view.rules.slice(0, 5).map((r, i) => (
                        <div key={i} className="text-[11px] text-white/50 flex gap-2">
                          <span className="font-mono text-accent/60 flex-shrink-0">R{i + 1}</span>
                          <span>{r}</span>
                        </div>
                      ))}
                    </div>
                  )}
                  {card.operator_view.stake_hint && (
                    <div className="mt-2 inline-block text-xs font-medium text-ember-400 bg-ember-500/10 px-3 py-1 rounded-full">
                      注码建议: {card.operator_view.stake_hint}
                    </div>
                  )}
                  {card.operator_view.trap && card.operator_view.trap.score !== undefined && (
                    <div className={`mt-2 text-xs px-3 py-1.5 rounded-lg ${(card.operator_view.trap.score || 0) >= 70 ? 'bg-danger/15 text-danger-400' : 'bg-white/[0.03] text-white/40'}`}>
                      陷阱识别: {card.operator_view.trap.level || '低'} · score {card.operator_view.trap.score}
                    </div>
                  )}
                </div>
              )}

              {/* 子市场(折叠) */}
              {card.sub_markets && (card.sub_markets.ou || card.sub_markets.draw || card.sub_markets.correct_score) && (
                <div>
                  <button onClick={() => setShowSub(!showSub)}
                    className="w-full flex items-center justify-between text-[10px] font-mono text-white/40 hover:text-white/60 uppercase tracking-widest py-1">
                    <span>子市场 / SUB MARKETS (大小球 · 平局共识 · 波胆)</span>
                    <span>{showSub ? '▾' : '▸'}</span>
                  </button>
                  {showSub && (
                    <pre className="text-[10px] text-white/40 bg-accent-inner rounded-lg p-3 overflow-x-auto font-mono mt-1">
                      {JSON.stringify(card.sub_markets, null, 2)}
                    </pre>
                  )}
                </div>
              )}

              {/* softline (跨庄分歧) */}
              {card.softline && (
                <div className={`text-xs rounded-lg px-3 py-2 ${
                  card.softline.disagreement_detected ? 'bg-accent/8 text-accent' : 'bg-white/[0.02] text-white/40'
                }`}>
                  跨庄分歧: {card.softline.disagreement_detected ? '检测到结构性分歧' : '无显著分歧'}
                  {card.softline.clv_beat !== undefined && ` · CLV ${card.softline.clv_beat ? 'beat ✓' : 'lose'}`}
                </div>
              )}
            </>
          )}
        </div>
      </motion.div>
    </div>
  )
}

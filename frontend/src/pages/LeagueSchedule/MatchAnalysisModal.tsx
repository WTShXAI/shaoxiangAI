import { useState, useEffect, useRef } from 'react'
import { motion } from 'framer-motion'
import { terminalService } from '@/services/api'
import type { TerminalDecisionCard, ValueLayerRow } from '@/types'

// ── 辅助格式化 ──
const pct = (v: number | undefined, digits = 1) =>
  typeof v === 'number' && !isNaN(v) ? (v * 100).toFixed(digits) + '%' : '—'
const num = (v: number | undefined, digits = 2) =>
  typeof v === 'number' && !isNaN(v) ? v.toFixed(digits) : '—'
const edgeColor = (e: number) => e > 0.02 ? 'text-accent' : e > 0 ? 'text-ember-400' : 'text-white/70'
// 波胆交叉标注辅助
const dirLabel = (d: string) => d === 'H' ? '主' : d === 'D' ? '平' : d === 'A' ? '客' : d
const dirColor = (d: string) => d === 'H' ? 'text-frost-300' : d === 'A' ? 'text-ember-300' : 'text-white/85'
const hcColor = (h: string) => h === '赢' || h === '半赢' ? 'text-accent' : h === '走' ? 'text-white/70' : 'text-ember-400'
const ouColor = (o: string) => o === '大' ? 'text-pitch-300' : o === '小' ? 'text-frost-300' : 'text-white/70'

// ── 价值层三行 (H/D/A) ──
function ValueRows({ rows }: { rows: ValueLayerRow[] }) {
  if (!rows || !rows.length) return null
  const dirMap: Record<string, string> = { H: '主胜', D: '平局', A: '客胜' }
  return (
    <div className="space-y-1.5">
      {rows.map((r) => {
        const isBest = r.ev > 0
        return (
          <div key={r.outcome} className={`flex items-center gap-3 px-3 py-2 rounded-lg ${isBest ? 'bg-accent/10 border border-accent/20' : 'bg-white/[0.04]'}`}>
            <span className="text-sm text-white w-12">{dirMap[r.outcome] || r.outcome}</span>
            <span className="font-mono text-sm text-white/85 w-14">@{num(r.odds)}</span>
            <span className="font-mono text-xs text-white/70 w-16">P{num(r.model_prob, 3)}</span>
            <span className={`font-mono text-sm font-semibold w-16 text-right ${edgeColor(r.edge)}`}>
              {r.edge_pct >= 0 ? '+' : ''}{num(r.edge_pct, 1)}%
            </span>
            <span className={`font-mono text-xs w-16 text-right ${r.ev > 0 ? 'text-accent' : 'text-white/70'}`}>
              EV{r.ev_pct >= 0 ? '+' : ''}{num(r.ev_pct, 1)}%
            </span>
            <span className="font-mono text-xs text-white/70 w-12 text-right">k{num(r.kelly_half, 3)}</span>
          </div>
        )
      })}
    </div>
  )
}

// ── 单栏简版结论 (初始/实时对照用) ──
function VerdictMini({label, card}: {label:string; card: TerminalDecisionCard}) {
  return (
    <div className="bg-accent-inner rounded-lg px-3 py-2">
      <div className="text-[9px] text-white/70 font-mono uppercase tracking-wider">{label}</div>
      <div className="flex items-center gap-2 mt-1">
        <span className={`text-base font-black ${card.decision === 'BET' ? 'text-accent' : 'text-white/85'}`}>
          {card.decision || '—'}
        </span>
        <span className="text-[11px] text-white/85">{card.direction || ''}</span>
      </div>
      {card.best_edge_pct !== undefined && (
        <div className="font-mono text-[11px] text-white/85 mt-0.5">
          edge {card.best_edge_pct > 0 ? '+' : ''}{num(card.best_edge_pct, 1)}%
        </div>
      )}
    </div>
  )
}

// ── 主弹窗 ──
export default function MatchAnalysisModal({
  home, away, sportKey, odds, handicap, initialOdds, initialHandicap, focus, onClose, liveScore,
}: {
  home: string; away: string; sportKey: string
  odds?: { h: number; d: number; a: number }
  handicap?: { ah_line?: number | string; ah_home?: number; ah_away?: number;
               ou_line?: number | string; ou_over?: number; ou_under?: number }
  initialOdds?: { h: number; d: number; a: number }
  initialHandicap?: { ah_line?: number | string; ah_home?: number; ah_away?: number;
                      ou_line?: number | string; ou_over?: number; ou_under?: number }
  focus?: 'overview' | 'correct_score'; onClose: () => void
  liveScore?: { homeGoals?: number; awayGoals?: number; elapsed?: number }
}) {
  const [card, setCard] = useState<TerminalDecisionCard | null>(null)
  const [cardInitial, setCardInitial] = useState<TerminalDecisionCard | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // 波胆快捷入口 → 默认展开子市场 (含波胆)
  const [showSub, setShowSub] = useState(focus === 'correct_score')
  const oipRef = useRef<HTMLDivElement>(null)

  // 数据就绪 + 聚焦波胆 → 自动滚动到 OIP 波胆模型区
  useEffect(() => {
    if (card && !loading && focus === 'correct_score' && oipRef.current) {
      oipRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, [card, loading, focus])

  useEffect(() => {
    let alive = true
    setLoading(true); setError(null); setCard(null); setCardInitial(null)
    // 同源传入选中的让球/大小球盘口 → 后端波胆×让球×大小球交叉标注才准确
    const liveReq = terminalService.analyze(home, away, sportKey, odds, handicap, liveScore)
    const reqs: Promise<any>[] = [liveReq]
    // Req2: 有开盘快照 → 并行跑一次初始分析, 弹窗双栏对比
    if (initialOdds) reqs.push(terminalService.analyze(home, away, sportKey, initialOdds, initialHandicap))
    Promise.all(reqs.map(r => r.then((res) => {
      const d = (res.data as any)?.data || res.data
      return d?.error ? { __error: d.error } : (d as TerminalDecisionCard)
    }).catch((e) => ({ __error: e?.response?.data?.detail || e?.message || '分析失败' }))))
      .then(([live, initial]: any) => {
        if (!alive) return
        if (live?.__error) setError(live.__error)
        else setCard(live || null)
        setCardInitial(initial?.__error ? null : (initial || null))
      })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [home, away, sportKey, initialOdds, initialHandicap])

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
            <h3 className="text-lg font-bold text-white mt-0.5">{home} <span className="text-white/70 mx-1 text-sm font-normal">vs</span> {away}</h3>
          </div>
          <button onClick={onClose} className="w-8 h-8 rounded-lg flex items-center justify-center text-white/70 hover:text-white hover:bg-white/[0.06]">
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2"><path strokeLinecap="round" d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>

        <div className="p-6 space-y-5">
          {/* Loading */}
          {loading && (
            <div className="py-16 text-center">
              <div className="inline-block w-8 h-8 border-2 border-accent/30 border-t-accent rounded-full animate-spin mb-3" />
              <p className="text-sm text-white/75">全链路分析中 · OIP波胆 / 让球 / 价值层 / 子市场…</p>
              <p className="text-[11px] text-white/60 mt-1">直接用盘口赔率计算,通常 1 秒内完成</p>
            </div>
          )}

          {/* Error */}
          {error && !loading && (
            <div className="py-12 text-center">
              <div className="text-3xl mb-3">⚠️</div>
              <p className="text-sm text-white/85 font-medium">分析失败</p>
              <p className="text-[11px] text-white/70 mt-2 max-w-sm mx-auto">{error}</p>
              <p className="text-[10px] text-white/60 mt-3">提示: 该比赛可能无盘口赔率数据,或赔率未采集</p>
            </div>
          )}

          {/* Req2: 初始分析 vs 实时分析 双栏对照 */}
          {card && cardInitial && !loading && !error && (
            <div className="rounded-xl border border-white/[0.08] bg-white/[0.02] p-3">
              <div className="flex items-center justify-between mb-2">
                <div className="text-[10px] font-mono text-accent tracking-widest uppercase">初始 vs 实时 · 走势对照</div>
                {card.best_edge_pct !== undefined && cardInitial.best_edge_pct !== undefined && (() => {
                  const d = card.best_edge_pct - cardInitial.best_edge_pct
                  return (
                    <span className={`text-[10px] font-mono font-bold ${d > 0 ? 'text-accent' : d < 0 ? 'text-ember-500' : 'text-white/70'}`}>
                      edge {d >= 0 ? '+' : ''}{num(d, 1)}%
                    </span>
                  )
                })()}
              </div>
              <div className="grid grid-cols-2 gap-3">
                <VerdictMini label="初始分析" card={cardInitial} />
                <VerdictMini label="实时分析" card={card} />
              </div>
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
                  <div className="text-[10px] font-mono text-white/70 uppercase tracking-wider">决策</div>
                  <div className="flex items-center gap-2 mt-1">
                    <span className={`text-2xl font-black ${card.decision === 'BET' ? 'text-accent' : 'text-white/85'}`}>
                      {card.decision}
                    </span>
                    <span className="text-sm text-white/85">· {card.direction}</span>
                    {card.best_edge_pct !== undefined && card.best_edge_pct > 0 && (
                      <span className="font-mono text-sm text-accent">edge +{num(card.best_edge_pct, 1)}%</span>
                    )}
                  </div>
                  <p className="text-[11px] text-white/70 mt-1">{card.decision_text}</p>
                </div>
                <div className="text-right">
                  <div className="text-[10px] font-mono text-white/70">覆盖庄家</div>
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
                    <div className="text-[10px] text-white/70">{x.l}</div>
                    <div className="font-mono text-lg font-bold text-white mt-0.5">{num(x.o)}</div>
                    <div className="font-mono text-[10px] text-accent">{pct(x.p)}</div>
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

              {/* In-Play 条件波胆横幅 */}
              {card.inplay && (
                <div className="rounded-xl bg-pitch-500/10 border border-pitch-500/20 px-4 py-3">
                  <div className="flex items-center gap-2">
                    <span className="text-[10px] font-mono text-pitch-300 tracking-widest uppercase">⚡ IN-PLAY 条件波胆</span>
                    <span className="text-[11px] font-bold text-white">当前 {card.inplay.current_score} · {card.inplay.elapsed}′</span>
                    <span className="text-[10px] text-white/60">剩余 {((1 - card.inplay.time_ratio) * 100).toFixed(0)}%</span>
                  </div>
                  <p className="text-[10px] text-white/70 mt-1">{card.inplay.note}</p>
                </div>
              )}

              {/* OIP 波胆 */}
              {card.oip && (
                <div ref={oipRef} className={`scroll-mt-4 rounded-xl ${focus === 'correct_score' ? 'ring-1 ring-ember-500/30 bg-ember-500/[0.03] p-3 -mx-1' : ''}`}>
                  <div className="flex items-center gap-2 mb-2">
                    <div className="text-[10px] font-mono text-accent tracking-widest uppercase">
                      {card.inplay ? '⚡ 条件波胆 (In-Play)' : 'OIP 波胆模型 / SCORE'}
                    </div>
                    {focus === 'correct_score' && (
                      <span className="text-[9px] px-1.5 py-0.5 bg-ember-500/20 text-ember-300 rounded font-bold">波胆直达</span>
                    )}
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <div className="bg-accent-inner rounded-lg px-3 py-2">
                      <div className="text-[10px] text-white/70">
                        {card.inplay ? '期望进球 λ (剩余时间)' : '期望进球 λ'}
                      </div>
                      <div className="font-mono text-xs text-white mt-0.5">
                        {card.inplay ? (
                          <>主 <span className="text-accent">{num(card.inplay.remaining_lambda_h, 2)}</span> · 客 <span className="text-ember-400">{num(card.inplay.remaining_lambda_a, 2)}</span>
                            <div className="text-[9px] text-white/50 mt-0.5">全场: 主{num(card.inplay.original_lambda_h, 2)} / 客{num(card.inplay.original_lambda_a, 2)}</div>
                          </>
                        ) : (
                          <>主 {num(card.oip.lambda_h, 2)} · 客 {num(card.oip.lambda_a, 2)}</>
                        )}
                      </div>
                    </div>
                    <div className="bg-accent-inner rounded-lg px-3 py-2">
                      <div className="text-[10px] text-white/70">{card.inplay ? '条件 Top3 比分' : 'Top3 比分'}</div>
                      <div className="font-mono text-xs text-white/85 mt-0.5">
                        {(card.oip.top3_scores || []).map((s, i) => `${s}(${pct(card.oip!.top3_prob?.[i], 0)})`).join('  ')}
                      </div>
                    </div>
                  </div>
                  <div className="flex gap-2 mt-2">
                    {card.oip.over15 !== undefined && <span className="text-[10px] font-mono text-white/80 bg-white/[0.06] px-2 py-1 rounded">≥1.5 {pct(card.oip.over15, 0)}</span>}
                    {card.oip.over25 !== undefined && <span className="text-[10px] font-mono text-white/80 bg-white/[0.06] px-2 py-1 rounded">≥2.5 {pct(card.oip.over25, 0)}</span>}
                    {card.oip.over35 !== undefined && <span className="text-[10px] font-mono text-white/80 bg-white/[0.06] px-2 py-1 rounded">≥3.5 {pct(card.oip.over35, 0)}</span>}
                  </div>

                  {/* 波胆 × 让球 × 大小球 交叉标注表 */}
                  {card.oip.scores_annotated && card.oip.scores_annotated.length > 0 && (
                    <div className="mt-3">
                      <div className="text-[9px] text-white/70 mb-1.5 flex items-center gap-2">
                        <span>波胆 × 让球{card.oip.ah_line != null ? `(${card.oip.ah_line})` : ''} × 大小球{card.oip.ou_line != null ? `(${card.oip.ou_line})` : ''} 交叉</span>
                        {card.oip.discipline?.best_direction && (
                          <span className="text-[9px] text-accent">· 最强方向 {dirLabel(card.oip.discipline.best_direction)}</span>
                        )}
                      </div>
                      <div className="space-y-1">
                        {card.oip.scores_annotated.map((s) => (
                          <div key={s.score} className={`flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-[11px] font-mono ${
                            s.long_tail ? 'bg-white/[0.03] opacity-70' : 'bg-accent-inner'
                          } ${card.oip?.discipline?.best_direction && s.direction === card.oip.discipline.best_direction ? 'border-l-2 border-accent/60' : ''}`}>
                            <span className={`font-bold w-8 ${s.long_tail ? 'text-white/60' : 'text-white'}`}>{s.score}</span>
                            <span className="text-white/75 w-10">{pct(s.prob, 1)}</span>
                            <span className={`w-6 text-center ${dirColor(s.direction)}`}>{dirLabel(s.direction)}</span>
                            {s.handicap && (
                              <span className={`w-10 text-center ${hcColor(s.handicap)}`}>让{s.handicap}</span>
                            )}
                            {s.ou && (
                              <span className={`w-9 text-center ${ouColor(s.ou)}`}>{s.ou}</span>
                            )}
                            {s.fair_decimal && (
                              <span className="text-white/70 w-14 text-right">@{num(s.fair_decimal, 1)}</span>
                            )}
                            {s.long_tail && (
                              <span className="text-[8px] text-ember-400 ml-auto">长尾负EV</span>
                            )}
                          </div>
                        ))}
                      </div>
                      {card.oip.discipline?.multi_direction && (
                        <div className="mt-2 text-[10px] text-ember-400/80 bg-ember-500/[0.06] border border-ember-500/10 rounded-lg px-2.5 py-1.5">
                          ⚠ Top波胆跨多方向({card.oip.discipline.direction_count.H > 0 && `主${card.oip.discipline.direction_count.H} `}{card.oip.discipline.direction_count.D > 0 && `平${card.oip.discipline.direction_count.D} `}{card.oip.discipline.direction_count.A > 0 && `客${card.oip.discipline.direction_count.A}`}) · 撒网对冲=消耗价值 · 建议锁定<strong className="text-accent">{dirLabel(card.oip.discipline.best_direction)}</strong>方向
                        </div>
                      )}
                    </div>
                  )}
                  {/* 市场结构波胆三角定位 (OU×AH×1X2×CS 取交集, 可审计) */}
                  {card.oip?.cs_triangulation && (
                    <div className="mt-3 rounded-xl bg-accent/5 border border-accent/10 p-3">
                      <div className="flex items-center gap-2 mb-2 flex-wrap">
                        <div className="text-[10px] font-mono text-accent tracking-widest uppercase">市场结构波胆三角定位</div>
                        <span className="text-[9px] px-1.5 py-0.5 bg-accent/15 text-accent rounded font-bold">OU×AH×1X2×CS</span>
                        {card.oip.cs_triangulation.cs_coverage && (
                          <span className="text-[9px] text-white/70">{card.oip.cs_triangulation.cs_coverage}</span>
                        )}
                        {card.oip.cs_triangulation.winner && (
                          <span className="text-[9px] text-white/80">胜方·{dirLabel(card.oip.cs_triangulation.winner)}</span>
                        )}
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {(card.oip.cs_triangulation.ranked || []).slice(0, 8).map((sc, i) => {
                          const cand = (card.oip!.cs_triangulation!.candidates || []).find(c => c.score === sc)
                          const isTop = i === 0
                          return (
                            <div key={sc} className={`px-2 py-1 rounded-lg text-[11px] font-mono ${isTop ? 'bg-accent text-black font-bold' : 'bg-white/[0.06] text-white/90'}`}>
                              <span className="font-bold">{sc}</span>
                              {cand?.blend != null && <span className="text-[9px] ml-1 opacity-70">{pct(cand.blend, 0)}</span>}
                            </div>
                          )
                        })}
                      </div>
                      {card.oip.cs_triangulation.notes && card.oip.cs_triangulation.notes.length > 0 && (
                        <div className="mt-2 space-y-0.5">
                          {card.oip.cs_triangulation.notes.map((n, i) => (
                            <div key={i} className="text-[9px] text-white/70 leading-tight">· {n}</div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  {/* CS 实时赔率时间线 (初盘/中场/当前 + drift) — 仅 GQ 已采集比赛 */}
                  {card.oip?.cs_odds_timeline && (() => {
                    const tl = card.oip!.cs_odds_timeline!;
                    const lines = Object.entries(tl.open || tl.live || {})
                      .sort((a, b) => (a[1] as number) - (b[1] as number))
                      .slice(0, 6);
                    return (
                      <div className="mt-3 rounded-xl bg-white/[0.03] border border-white/10 p-3">
                        <div className="flex items-center gap-2 mb-2 flex-wrap">
                          <div className="text-[10px] font-mono text-accent tracking-widest uppercase">CS 实时赔率时间线</div>
                          <span className="text-[9px] px-1.5 py-0.5 bg-accent/15 text-accent rounded font-bold">初盘→中场→当前</span>
                          {tl.has_ht && <span className="text-[9px] text-white/70">含中场收盘</span>}
                          {tl.drift_summary?.lean && (
                            <span className={`text-[9px] px-1.5 py-0.5 rounded font-bold ${
                              tl.drift_summary.lean === 'follow_money' ? 'bg-field-500/15 text-field-400' :
                              tl.drift_summary.lean === 'fade' ? 'bg-danger-500/15 text-danger-400' :
                              'bg-white/10 text-white/60'}`}>
                              顺人性盘·{tl.drift_summary.lean === 'follow_money' ? '资金站实际侧' : tl.drift_summary.lean === 'fade' ? '资金逃离' : '均衡'}
                            </span>
                          )}
                        </div>
                        <div className="space-y-0.5">
                          {lines.map(([sc, op]) => {
                            const ht = (tl.ht_close as Record<string, number>)?.[sc];
                            const lv = (tl.live as Record<string, number>)?.[sc];
                            const drift = (tl.drift_live_open as Record<string, number>)?.[sc];
                            const htDrift = (tl.drift_ht_open as Record<string, number>)?.[sc];
                            const driftCls = drift == null ? 'text-white/40' : drift < 0 ? 'text-field-400' : drift > 0 ? 'text-danger-400' : 'text-white/50';
                            const driftTxt = drift == null ? '—' : `${drift > 0 ? '+' : ''}${num(drift, 2)}`;
                            const htDriftCls = htDrift == null ? 'text-white/40' : htDrift < 0 ? 'text-field-400' : htDrift > 0 ? 'text-danger-400' : 'text-white/50';
                            const htDriftTxt = htDrift == null ? '—' : `${htDrift > 0 ? '+' : ''}${num(htDrift, 2)}`;
                            return (
                              <div key={sc} className="flex items-center gap-2 px-2 py-1 rounded text-[11px] font-mono">
                                <span className="font-bold w-8 text-white">{sc}</span>
                                <span className="w-14 text-white/70">初 {num(op as number, 2)}</span>
                                <span className="w-14 text-white/70">中 {ht != null ? num(ht, 2) : '—'}</span>
                                <span className="w-14 text-accent font-bold">现 {lv != null ? num(lv, 2) : '—'}</span>
                                <span className={`w-14 text-right ${htDriftCls}`}>{htDriftTxt}</span>
                                <span className={`w-14 text-right ${driftCls}`}>{driftTxt}</span>
                              </div>
                            );
                          })}
                        </div>
                        <div className="mt-1.5 text-[9px] text-white/50">中漂=初盘→中场收盘 · 现漂=初盘→当前 · 绿↓=临场被看好(资金站该比分) · 红↑=被看衰 · 仅 GQ 已采集比赛</div>
                      </div>
                    );
                  })()}
                </div>
              )}

              {/* OIP 波胆 Top5 推荐 */}
              {card.oip?.top5_scores && card.oip.top5_scores.length > 0 && (
                <div className="rounded-xl bg-frost-500/[0.06] border border-frost-500/10 p-3">
                  <div className="flex items-center gap-2 mb-2">
                    <div className="text-[10px] font-mono text-frost-300 tracking-widest uppercase">OIP 波胆推荐 Top5</div>
                    {card.inplay && <span className="text-[8px] px-1.5 py-0.5 bg-pitch-500/20 text-pitch-300 rounded font-bold">IN-PLAY</span>}
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {(card.oip.top5_scores || []).map((s, i) => (
                      <div key={s} className={`px-2.5 py-1.5 rounded-lg text-[11px] font-mono ${
                        i === 0 ? 'bg-accent/20 border border-accent/30 text-white font-bold' :
                        i < 3 ? 'bg-white/[0.08] text-white/90' :
                        'bg-white/[0.04] text-white/70'
                      }`}>
                        <span className="font-bold">{s}</span>
                        <span className="text-[9px] ml-1 opacity-70">{pct(card.oip!.top5_prob?.[i], 0)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 多庄共识 (sharp vs retail) */}
              {card.multibook_consensus && (
                <div className="rounded-xl bg-accent/5 border border-accent/10 p-3">
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-2">
                      <div className="text-[10px] font-mono text-accent tracking-widest uppercase">多庄共识 · SHARP vs RETAIL</div>
                      <span className="text-[9px] text-white/70">{card.multibook_consensus.n_books}庄 · {card.multibook_consensus.n_sharp}sharp</span>
                    </div>
                    {card.multibook_consensus.max_spread_pp > 0 && (
                      <span className="text-[9px] font-mono text-ember-400">离散 {card.multibook_consensus.max_spread_pp}pp</span>
                    )}
                  </div>
                  {/* sharp共识 vs 零售均值 HDA bar */}
                  <div className="grid grid-cols-3 gap-2 mb-2">
                    {[
                      { l: '主', i: 'h', c: 'text-frost-300' },
                      { l: '平', i: 'd', c: 'text-white/85' },
                      { l: '客', i: 'a', c: 'text-ember-300' },
                    ].map((x) => {
                      const sv = (card.multibook_consensus!.sharp_consensus as any)[x.i] || 0
                      const rv = (card.multibook_consensus!.retail_mean as any)[x.i] || 0
                      return (
                        <div key={x.i} className="bg-accent-inner rounded-lg px-2 py-2 text-center">
                          <div className="text-[8px] text-white/70">{x.l}</div>
                          <div className={`font-mono text-sm font-bold ${x.c}`}>{sv}%</div>
                          <div className="font-mono text-[9px] text-white/50">零售 {rv}%</div>
                        </div>
                      )
                    })}
                  </div>
                  {/* 价值/该fade边 */}
                  <div className="flex items-center gap-3 text-[10px]">
                    <span className="text-accent">
                      价值侧: {card.multibook_consensus.value_side.outcome === 'H' ? '主胜' : card.multibook_consensus.value_side.outcome === 'D' ? '平局' : '客胜'} +{card.multibook_consensus.value_side.pp}pp
                    </span>
                    <span className="text-ember-400">
                      该fade: {card.multibook_consensus.fade_side.outcome === 'H' ? '主胜' : card.multibook_consensus.fade_side.outcome === 'D' ? '平局' : '客胜'} {card.multibook_consensus.fade_side.pp}pp
                    </span>
                  </div>
                  {/* 背离明细 */}
                  {card.multibook_consensus.divergences && card.multibook_consensus.divergences.length > 0 && (
                    <div className="mt-2 pt-2 border-t border-white/[0.06]">
                      <div className="text-[8px] text-white/60 mb-1">零售背离样本</div>
                      <div className="flex flex-wrap gap-1">
                        {card.multibook_consensus.divergences.map((d, i) => (
                          <span key={i} className={`text-[9px] px-1.5 py-0.5 rounded ${d.retail_over ? 'bg-ember-500/[0.15] text-ember-400' : 'bg-accent/10 text-accent'}`}>
                            {d.book} {d.outcome} {d.retail_over ? '超买' : '低估'} {d.pp}pp
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* 波胆推荐 */}
              {card.sub_markets?.correct_score?.rows && card.sub_markets.correct_score.rows.length > 0 && (
                <div>
                  <div className="flex items-center gap-2 mb-3">
                    <div className="text-[10px] font-mono text-accent tracking-widest uppercase">波胆推荐 / CORRECT SCORE</div>
                    {card.sub_markets.correct_score.edge_available && (
                      <span className="text-[8px] px-1.5 py-0.5 bg-accent/15 text-accent rounded font-bold">EDGE</span>
                    )}
                    <span className="text-[9px] text-white/70">{card.sub_markets.correct_score.decision}</span>
                  </div>
                  <div className="space-y-1.5">
                    {card.sub_markets.correct_score.rows
                      .filter((r: any) => typeof r.edge === 'number' ? r.edge > -0.02 : true)
                      .slice(0, 6)
                      .map((r: any, i: number) => {
                        const hasEdge = typeof r.edge === 'number' && r.edge > 0
                        const isTop = i === 0 && hasEdge
                        return (
                          <div key={r.score} className={`flex items-center gap-3 px-3 py-2.5 rounded-lg ${
                            isTop ? 'bg-accent/10 border border-accent/20' :
                            hasEdge ? 'bg-accent/5 border border-accent/10' :
                            'bg-white/[0.04]'
                          }`}>
                            <span className={`font-mono text-lg font-black w-14 ${hasEdge ? 'text-white' : 'text-white/85'}`}>
                              {r.score}
                            </span>
                            <div className="flex-1 flex items-center gap-4">
                              <div className="flex flex-col">
                                <span className="text-[9px] text-white/60">概率</span>
                                <span className="font-mono text-sm text-white">{pct(r.prob, 1)}</span>
                              </div>
                              <div className="flex flex-col">
                                <span className="text-[9px] text-white/60">有效概率</span>
                                <span className="font-mono text-sm text-white/90">{pct(r.prob_eff, 1)}</span>
                              </div>
                              <div className="flex flex-col">
                                <span className="text-[9px] text-white/60">fair</span>
                                <span className="font-mono text-sm text-white/80">@{num(r.fair_decimal, 1)}</span>
                              </div>
                            </div>
                            <div className="text-right">
                              {typeof r.edge === 'number' && r.edge !== false ? (
                                <>
                                  <div className={`font-mono text-sm font-bold ${r.edge > 0.02 ? 'text-accent' : r.edge > 0 ? 'text-pitch-400' : 'text-ember-500'}`}>
                                    {r.edge > 0 ? '+' : ''}{r.edge.toFixed(3)}
                                  </div>
                                  <div className="text-[9px] text-white/60">edge</div>
                                </>
                              ) : (
                                <span className="text-[10px] text-white/55">—</span>
                              )}
                            </div>
                            <div className="text-right min-w-[56px]">
                              {r.ev_pct !== undefined && r.ev_pct > 0 ? (
                                <span className="font-mono text-xs font-bold text-accent bg-accent/10 px-2 py-1 rounded">
                                  +{num(r.ev_pct, 1)}%
                                </span>
                              ) : r.ev_pct !== undefined ? (
                                <span className="font-mono text-xs text-ember-500">{num(r.ev_pct, 1)}%</span>
                              ) : null}
                            </div>
                          </div>
                        )
                      })}
                  </div>
                  {card.sub_markets.correct_score.rows.length > 6 && (
                    <div className="text-[10px] text-white/60 text-center mt-1">+{card.sub_markets.correct_score.rows.length - 6} 项更多 (展开子市场查看)</div>
                  )}
                </div>
              )}

              {/* 操盘手视角 */}
              {card.operator_view && (
                <div>
                  <div className="text-[10px] font-mono text-accent tracking-widest uppercase mb-2">操盘手视角 / PLAYBOOK</div>
                  {card.operator_view.verdict && (
                    <p className="text-sm text-white/85 bg-accent-inner rounded-lg px-3 py-2 mb-2">{card.operator_view.verdict}</p>
                  )}
                  {card.operator_view.rules && card.operator_view.rules.length > 0 && (
                    <div className="space-y-1">
                      {card.operator_view.rules.slice(0, 5).map((r, i) => (
                        <div key={i} className="text-[11px] text-white/75 flex gap-2">
                          <span className="font-mono text-accent flex-shrink-0">R{i + 1}</span>
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
                    <div className={`mt-2 text-xs px-3 py-1.5 rounded-lg ${(card.operator_view.trap.score || 0) >= 70 ? 'bg-danger/15 text-danger-400' : 'bg-white/[0.06] text-white/70'}`}>
                      陷阱识别: {card.operator_view.trap.level || '低'} · score {card.operator_view.trap.score}
                    </div>
                  )}
                </div>
              )}

              {/* 操盘手逆转信号 (drift 学习器) */}
              {card.operator_signals && (
                <div className={`rounded-xl border p-3 ${card.operator_signals.reversal_risk > 0.5 ? 'bg-ember-500/[0.06] border-ember-500/20' : 'bg-accent/5 border-accent/10'}`}>
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-2">
                      <div className="text-[10px] font-mono text-accent tracking-widest uppercase">操盘手逆转信号 · DRIFT LEARNER</div>
                      <span className={`text-[9px] px-1.5 py-0.5 rounded font-bold ${card.operator_signals.reversal_risk > 0.5 ? 'bg-ember-500/20 text-ember-300' : 'bg-accent/15 text-accent'}`}>
                        逆转 {Math.round(card.operator_signals.reversal_risk * 100)}%
                      </span>
                    </div>
                    <span className="text-[9px] text-white/60">可靠性 {Math.round(card.operator_signals.operator_reliability * 100)}%</span>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {card.operator_signals.drift_draw_down && (
                      <span className="text-[9px] px-1.5 py-0.5 rounded bg-frost-500/15 text-frost-300 font-bold">平赔↓(平局升)</span>
                    )}
                    {card.operator_signals.favorite_flip && (
                      <span className="text-[9px] px-1.5 py-0.5 rounded bg-ember-500/15 text-ember-300 font-bold">热门反转</span>
                    )}
                    {card.operator_signals.drift_significant && (
                      <span className="text-[9px] px-1.5 py-0.5 rounded bg-white/[0.08] text-white/70">显著漂移</span>
                    )}
                  </div>
                  <div className="mt-1.5 text-[10px] text-white/60">
                    方向: {card.operator_signals.direction === 'home' ? '偏向主队' : card.operator_signals.direction === 'draw' ? '偏向平局' : '偏向客队'} · Δ {card.operator_signals.delta.h.toFixed(2)}/{card.operator_signals.delta.d.toFixed(2)}/{card.operator_signals.delta.a.toFixed(2)}
                  </div>
                </div>
              )}

              {/* 方向信号 (三方向策略信号 · tier 感知 · 面板提示级) */}
              {card.strategy_signals !== undefined && (
                <div>
                  <div className="flex items-center gap-2 mb-2 flex-wrap">
                    <div className="text-[10px] font-mono text-accent tracking-widest uppercase">方向信号 / DIRECTION SIGNALS</div>
                    <span className={`text-[9px] px-1.5 py-0.5 rounded font-bold ${
                      card.strategy_tier === 'obscure' ? 'bg-accent/15 text-accent' :
                      card.strategy_tier === 'cup' ? 'bg-white/[0.06] text-white/60' : 'bg-white/[0.06] text-white/60'
                    }`}>
                      {card.strategy_tier === 'obscure' ? '低级别联赛层' : card.strategy_tier === 'cup' ? '杯赛/赛会' : '主流联赛'}
                    </span>
                    <span className="text-[9px] text-white/45">提示级 · 不改结论</span>
                  </div>
                  {card.strategy_signals && card.strategy_signals.length > 0 ? (
                    <div className="space-y-2">
                      {card.strategy_signals.map((s: any, i: number) => (
                        <div key={i} className="rounded-lg bg-white/[0.03] border border-white/10 px-3 py-2.5">
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-[12px] font-bold text-white">{s.name}</span>
                            <span className={`text-[8px] px-1.5 py-0.5 rounded font-bold ${
                              s.confidence === 'high' ? 'bg-accent/15 text-accent' : 'bg-ember-500/15 text-ember-300'
                            }`}>{s.confidence === 'high' ? '高置信' : '方向性'}</span>
                          </div>
                          <div className="text-[11px] text-accent mt-0.5">{s.direction}</div>
                          <div className="flex items-center gap-2 mt-1.5">
                            <div className="flex-1 h-1.5 rounded-full bg-white/[0.08] overflow-hidden">
                              <div className="h-full rounded-full bg-accent" style={{ width: `${Math.max(6, Math.round((s.strength || 0) * 100))}%` }} />
                            </div>
                            <span className="text-[9px] font-mono text-white/65 w-9 text-right">{Math.round((s.strength || 0) * 100)}%</span>
                          </div>
                          <div className="text-[9px] text-white/55 mt-1">📊 {s.metric}</div>
                          <div className="text-[9px] text-white/45 mt-0.5">{s.note}</div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="text-[11px] text-white/55 bg-white/[0.03] border border-white/[0.06] rounded-lg px-3 py-2.5">
                      {card.strategy_tier === 'obscure'
                        ? '当前盘口无明显方向性偏差'
                        : '主流联赛/杯赛不触发低级别联赛层信号（偏差规律不适用）'}
                    </div>
                  )}
                </div>
              )}

              {/* 子市场(折叠) */}
              {card.sub_markets && (card.sub_markets.ou || card.sub_markets.draw || card.sub_markets.correct_score) && (
                <div>
                  <button onClick={() => setShowSub(!showSub)}
                    className="w-full flex items-center justify-between text-[10px] font-mono text-white/70 hover:text-white uppercase tracking-widest py-1">
                    <span>子市场 / SUB MARKETS (大小球 · 平局共识 · 波胆)</span>
                    <span>{showSub ? '▾' : '▸'}</span>
                  </button>
                  {showSub && (
                    <div className='bg-accent-inner rounded-lg p-3 mt-1 space-y-3 text-[10px] font-mono'>
                      {card.sub_markets.ou && (
                        <div>
                          <div className='text-white/85 font-bold mb-1'>大小球 OU</div>
                          <div className='grid grid-cols-4 gap-2 text-white/85'>
                            <div>edge <span className='text-accent font-bold'>{card.sub_markets.ou.edge?.toFixed?.(3) ?? card.sub_markets.ou.edge}</span></div>
                            <div>fair <span className='text-white'>{card.sub_markets.ou.fair_decimal?.toFixed?.(2) ?? card.sub_markets.ou.fair_decimal}</span></div>
                            <div>off <span className='text-white'>{card.sub_markets.ou.fair_off_decimal?.toFixed?.(2) ?? card.sub_markets.ou.fair_off_decimal}</span></div>
                            <div>value <span className='text-pitch-400'>{card.sub_markets.ou.value?.toFixed?.(2) ?? card.sub_markets.ou.value}</span></div>
                          </div>
                        </div>
                      )}
                      {card.sub_markets.draw && (
                        <div>
                          <div className='text-white/85 font-bold mb-1'>平局共识 DRAW</div>
                          <div className='grid grid-cols-4 gap-2 text-white/85'>
                            <div>edge <span className='text-accent font-bold'>{card.sub_markets.draw.edge?.toFixed?.(3) ?? card.sub_markets.draw.edge}</span></div>
                            <div>fair <span className='text-white'>{card.sub_markets.draw.fair_decimal?.toFixed?.(2) ?? card.sub_markets.draw.fair_decimal}</span></div>
                            <div>value <span className='text-pitch-400'>{card.sub_markets.draw.value?.toFixed?.(2) ?? card.sub_markets.draw.value}</span></div>
                          </div>
                        </div>
                      )}
                      {card.sub_markets.correct_score?.rows && card.sub_markets.correct_score.rows.length > 0 && (
                        <div>
                          <div className='text-white/85 font-bold mb-1'>波胆 CORRECT SCORE</div>
                          <div className='space-y-0.5 max-h-40 overflow-y-auto'>
                            {card.sub_markets.correct_score.rows.slice(0, 20).map((r: any, i: number) => (
                              <div key={i} className='grid grid-cols-[40px_60px_50px_50px_60px] gap-1 text-white/85 hover:bg-white/[0.06] px-1 py-0.5 rounded'>
                                <span className='text-white font-bold'>{r.score}</span>
                                <span>p={r.prob?.toFixed?.(3) ?? r.prob}</span>
                                <span>pe={r.prob_eff?.toFixed?.(3) ?? r.prob_eff}</span>
                                <span>f={r.fair_decimal?.toFixed?.(1) ?? r.fair_decimal}</span>
                                <span className={typeof r.edge === 'number' && r.edge > 0 ? 'text-accent' : 'text-ember-500'}>e={typeof r.edge === 'number' ? r.edge.toFixed(3) : '--'}</span>
                              </div>
                            ))}
                            {card.sub_markets.correct_score.rows.length > 20 && (
                              <div className='text-white/60 italic text-center'>+{card.sub_markets.correct_score.rows.length - 20} 条更多...</div>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* softline (跨庄分歧) */}
              {card.softline && (
                <div className={`text-xs rounded-lg px-3 py-2 ${
                  card.softline.disagreement_detected ? 'bg-accent/10 text-accent' : 'bg-white/[0.04] text-white/75'
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

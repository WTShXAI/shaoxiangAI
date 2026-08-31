import type { TerminalDecisionCard, ValueLayerRow } from '@/types'
import { pct, num, edgeColor, dirColor, dirLabel, hcColor, ouColor } from './format'

// ── 价值层三行 (H/D/A) ──
export function ValueRows({ rows }: { rows: ValueLayerRow[] }) {
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
export function VerdictMini({ label, card }: { label: string; card: TerminalDecisionCard }) {
  return (
    <div className="bg-accent-inner rounded-lg px-3 py-2">
      <div className="text-[10px] text-white/70 font-mono uppercase tracking-wider">{label}</div>
      <div className="flex items-center gap-2 mt-1">
        <span className={`text-base font-black ${card.decision === 'BET' ? 'text-accent' : 'text-white/85'}`}>
          {card.decision || '—'}
        </span>
        <span className="text-[12px] text-white/85">{card.direction || ''}</span>
      </div>
      {card.best_edge_pct !== undefined && (
        <div className="font-mono text-[12px] text-white/85 mt-0.5">
          edge {card.best_edge_pct > 0 ? '+' : ''}{num(card.best_edge_pct, 1)}%
        </div>
      )}
    </div>
  )
}

// ── 决策结论条 ──
export function DecisionVerdictBar({ card }: { card: TerminalDecisionCard }) {
  return (
    <div className={`flex items-center justify-between rounded-xl px-5 py-4 ${
      card.decision === 'BET'
        ? 'bg-accent/10 border border-accent/20'
        : 'bg-white/[0.03] border border-white/[0.06]'
    }`}>
      <div>
        <div className="text-[11px] font-mono text-white/70 uppercase tracking-wider">全链路决策卡 · 决策</div>
        <div className="flex items-center gap-2 mt-1">
          <span className={`text-2xl font-black ${card.decision === 'BET' ? 'text-accent' : 'text-white/85'}`}>
            {card.decision}
          </span>
          <span className="text-sm text-white/85">· {card.direction}</span>
          {card.best_edge_pct !== undefined && card.best_edge_pct > 0 && (
            <span className="font-mono text-sm text-accent">edge +{num(card.best_edge_pct, 1)}%</span>
          )}
        </div>
        <p className="text-[12px] text-white/70 mt-1">{card.decision_text}</p>
      </div>
      <div className="text-right">
        <div className="text-[11px] font-mono text-white/70">覆盖庄家</div>
        <div className="font-mono text-2xl font-bold text-white mt-1">{card.books_count}</div>
        {card.draw_alert && <div className="text-[11px] text-ember-400 mt-1">⚠ 平局预警</div>}
      </div>
    </div>
  )
}

// ── 赔率 + 市场隐含 ──
export function MarketOddsGrid({ card }: { card: TerminalDecisionCard }) {
  return (
    <div className="grid grid-cols-3 gap-2">
      {[
        { l: '主胜', o: card.odds?.oh, p: card.market_prob?.h },
        { l: '平局', o: card.odds?.od, p: card.market_prob?.d },
        { l: '客胜', o: card.odds?.oa, p: card.market_prob?.a },
      ].map((x) => (
        <div key={x.l} className="bg-accent-inner rounded-lg px-3 py-2.5 text-center">
          <div className="text-[11px] text-white/70">{x.l}</div>
          <div className="font-mono text-lg font-bold text-white mt-0.5">{num(x.o)}</div>
          <div className="font-mono text-[11px] text-accent">{pct(x.p)}</div>
        </div>
      ))}
    </div>
  )
}

// ── 价值层 (含标题) ──
export function ValueLayerBlock({ card }: { card: TerminalDecisionCard }) {
  if (!card.rows || card.rows.length === 0) return null
  return (
    <div>
      <div className="text-[11px] font-mono text-accent tracking-widest uppercase mb-2">价值层 / VALUE LAYER</div>
      <ValueRows rows={card.rows} />
    </div>
  )
}

// ── In-Play 条件波胆横幅 ──
export function InplayBanner({ card }: { card: TerminalDecisionCard }) {
  if (!card.inplay) return null
  return (
    <div className="rounded-xl bg-pitch-500/10 border border-pitch-500/20 px-4 py-3">
      <div className="flex items-center gap-2">
        <span className="text-[11px] font-mono text-pitch-300 tracking-widest uppercase">⚡ IN-PLAY 条件波胆</span>
        <span className="text-[12px] font-bold text-white">当前 {card.inplay.current_score} · {card.inplay.elapsed}′</span>
        <span className="text-[11px] text-white/60">剩余 {((1 - card.inplay.time_ratio) * 100).toFixed(0)}%</span>
      </div>
      <p className="text-[11px] text-white/70 mt-1">{card.inplay.note}</p>
    </div>
  )
}

// ── OIP 波胆推荐 Top5 ──
export function Top5Recommend({ card }: { card: TerminalDecisionCard }) {
  if (!card.oip?.top5_scores || card.oip.top5_scores.length === 0) return null
  return (
    <div className="rounded-xl bg-frost-500/[0.06] border border-frost-500/10 p-3">
      <div className="flex items-center gap-2 mb-2">
        <div className="text-[11px] font-mono text-frost-300 tracking-widest uppercase">OIP 波胆推荐 Top5</div>
        {card.inplay && <span className="text-[10px] px-1.5 py-0.5 bg-pitch-500/20 text-pitch-300 rounded font-bold">IN-PLAY</span>}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {(card.oip.top5_scores || []).map((s, i) => (
          <div key={s} className={`px-2.5 py-1.5 rounded-lg text-[12px] font-mono ${
            i === 0 ? 'bg-accent/20 border border-accent/30 text-white font-bold' :
            i < 3 ? 'bg-white/[0.08] text-white/90' :
            'bg-white/[0.04] text-white/70'
          }`}>
            <span className="font-bold">{s}</span>
            <span className="text-[10px] ml-1 opacity-70">{pct(card.oip!.top5_prob?.[i], 0)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── 多庄共识 (sharp vs retail) ──
export function MultiBookConsensus({ card }: { card: TerminalDecisionCard }) {
  const mb = card.multibook_consensus
  if (!mb) return null
  return (
    <div className="rounded-xl bg-accent/5 border border-accent/10 p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <div className="text-[11px] font-mono text-accent tracking-widest uppercase">多庄共识 · SHARP vs RETAIL</div>
          <span className="text-[10px] text-white/70">{mb.n_books}庄 · {mb.n_sharp}sharp</span>
        </div>
        {mb.max_spread_pp > 0 && (
          <span className="text-[10px] font-mono text-ember-400">离散 {mb.max_spread_pp}pp</span>
        )}
      </div>
      {/* sharp共识 vs 零售均值 HDA bar */}
      <div className="grid grid-cols-3 gap-2 mb-2">
        {[
          { l: '主', i: 'h', c: 'text-frost-300' },
          { l: '平', i: 'd', c: 'text-white/85' },
          { l: '客', i: 'a', c: 'text-ember-300' },
        ].map((x) => {
          const sv = (mb.sharp_consensus as any)[x.i] || 0
          const rv = (mb.retail_mean as any)[x.i] || 0
          return (
            <div key={x.i} className="bg-accent-inner rounded-lg px-2 py-2 text-center">
              <div className="text-[10px] text-white/70">{x.l}</div>
              <div className={`font-mono text-sm font-bold ${x.c}`}>{sv}%</div>
              <div className="font-mono text-[10px] text-white/50">零售 {rv}%</div>
            </div>
          )
        })}
      </div>
      {/* 价值/该fade边 */}
      <div className="flex items-center gap-3 text-[11px]">
        <span className="text-accent">
          价值侧: {mb.value_side.outcome === 'H' ? '主胜' : mb.value_side.outcome === 'D' ? '平局' : '客胜'} +{mb.value_side.pp}pp
        </span>
        <span className="text-ember-400">
          该fade: {mb.fade_side.outcome === 'H' ? '主胜' : mb.fade_side.outcome === 'D' ? '平局' : '客胜'} {mb.fade_side.pp}pp
        </span>
      </div>
      {/* 背离明细 */}
      {mb.divergences && mb.divergences.length > 0 && (
        <div className="mt-2 pt-2 border-t border-white/[0.06]">
          <div className="text-[10px] text-white/60 mb-1">零售背离样本</div>
          <div className="flex flex-wrap gap-1">
            {mb.divergences.map((d, i) => (
              <span key={i} className={`text-[10px] px-1.5 py-0.5 rounded ${d.retail_over ? 'bg-ember-500/[0.15] text-ember-400' : 'bg-accent/10 text-accent'}`}>
                {d.book} {d.outcome} {d.retail_over ? '超买' : '低估'} {d.pp}pp
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ── 波胆推荐 ──
export function CorrectScoreRecommend({ card }: { card: TerminalDecisionCard }) {
  const rows = card.sub_markets?.correct_score?.rows
  if (!rows || rows.length === 0) return null
  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <div className="text-[11px] font-mono text-accent tracking-widest uppercase">波胆推荐 / CORRECT SCORE</div>
        {card.sub_markets?.correct_score?.edge_available && (
          <span className="text-[10px] px-1.5 py-0.5 bg-accent/15 text-accent rounded font-bold">EDGE</span>
        )}
        <span className="text-[10px] text-white/70">{card.sub_markets?.correct_score?.decision}</span>
      </div>
      <div className="space-y-1.5">
        {rows
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
                    <span className="text-[10px] text-white/60">概率</span>
                    <span className="font-mono text-sm text-white">{pct(r.prob, 1)}</span>
                  </div>
                  <div className="flex flex-col">
                    <span className="text-[10px] text-white/60">有效概率</span>
                    <span className="font-mono text-sm text-white/90">{pct(r.prob_eff, 1)}</span>
                  </div>
                  <div className="flex flex-col">
                    <span className="text-[10px] text-white/60">fair</span>
                    <span className="font-mono text-sm text-white/80">@{num(r.fair_decimal, 1)}</span>
                  </div>
                </div>
                <div className="text-right">
                  {typeof r.edge === 'number' && r.edge !== false ? (
                    <>
                      <div className={`font-mono text-sm font-bold ${r.edge > 0.02 ? 'text-accent' : r.edge > 0 ? 'text-pitch-400' : 'text-ember-500'}`}>
                        {r.edge > 0 ? '+' : ''}{r.edge.toFixed(3)}
                      </div>
                      <div className="text-[10px] text-white/60">edge</div>
                    </>
                  ) : (
                    <span className="text-[11px] text-white/55">—</span>
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
      {rows.length > 6 && (
        <div className="text-[11px] text-white/60 text-center mt-1">+{rows.length - 6} 项更多 (展开子市场查看)</div>
      )}
    </div>
  )
}

// ── 操盘手视角 ──
export function OperatorView({ card }: { card: TerminalDecisionCard }) {
  const ov = card.operator_view
  if (!ov) return null
  return (
    <div>
      <div className="text-[11px] font-mono text-accent tracking-widest uppercase mb-2">操盘手视角 / PLAYBOOK</div>
      {ov.verdict && (
        <p className="text-sm text-white/85 bg-accent-inner rounded-lg px-3 py-2 mb-2">{ov.verdict}</p>
      )}
      {ov.rules && ov.rules.length > 0 && (
        <div className="space-y-1">
          {ov.rules.slice(0, 5).map((r, i) => (
            <div key={i} className="text-[12px] text-white/75 flex gap-2">
              <span className="font-mono text-accent flex-shrink-0">R{i + 1}</span>
              <span>{r}</span>
            </div>
          ))}
        </div>
      )}
      {ov.stake_hint && (
        <div className="mt-2 inline-block text-xs font-medium text-ember-400 bg-ember-500/10 px-3 py-1 rounded-full">
          注码建议: {ov.stake_hint}
        </div>
      )}
      {ov.trap && ov.trap.score !== undefined && (
        <div className={`mt-2 text-xs px-3 py-1.5 rounded-lg ${(ov.trap.score || 0) >= 70 ? 'bg-danger/15 text-danger-400' : 'bg-white/[0.06] text-white/70'}`}>
          陷阱识别: {ov.trap.level || '低'} · score {ov.trap.score}
        </div>
      )}
    </div>
  )
}

// ── 操盘手逆转信号 (drift 学习器) ──
export function OperatorSignals({ card }: { card: TerminalDecisionCard }) {
  const os = card.operator_signals
  if (!os) return null
  return (
    <div className={`rounded-xl border p-3 ${os.reversal_risk > 0.5 ? 'bg-ember-500/[0.06] border-ember-500/20' : 'bg-accent/5 border-accent/10'}`}>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <div className="text-[11px] font-mono text-accent tracking-widest uppercase">操盘手逆转信号 · DRIFT LEARNER</div>
          <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold ${os.reversal_risk > 0.5 ? 'bg-ember-500/20 text-ember-300' : 'bg-accent/15 text-accent'}`}>
            逆转 {Math.round(os.reversal_risk * 100)}%
          </span>
        </div>
        <span className="text-[10px] text-white/60">可靠性 {Math.round(os.operator_reliability * 100)}%</span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {os.drift_draw_down && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-frost-500/15 text-frost-300 font-bold">平赔↓(平局升)</span>
        )}
        {os.favorite_flip && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-ember-500/15 text-ember-300 font-bold">热门反转</span>
        )}
        {os.drift_significant && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-white/[0.08] text-white/70">显著漂移</span>
        )}
      </div>
      <div className="mt-1.5 text-[11px] text-white/60">
        方向: {os.direction === 'home' ? '偏向主队' : os.direction === 'draw' ? '偏向平局' : '偏向客队'} · Δ {os.delta.h.toFixed(2)}/{os.delta.d.toFixed(2)}/{os.delta.a.toFixed(2)}
      </div>
    </div>
  )
}

// ── 方向信号 (三方向策略信号 · tier 感知 · 面板提示级) ──
export function StrategySignals({ card }: { card: TerminalDecisionCard }) {
  return (
    <div>
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        <div className="text-[11px] font-mono text-accent tracking-widest uppercase">方向信号 / DIRECTION SIGNALS</div>
        <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold ${
          card.strategy_tier === 'obscure' ? 'bg-accent/15 text-accent' :
          card.strategy_tier === 'cup' ? 'bg-white/[0.06] text-white/60' : 'bg-white/[0.06] text-white/60'
        }`}>
          {card.strategy_tier === 'obscure' ? '低级别联赛层' : card.strategy_tier === 'cup' ? '杯赛/赛会' : '主流联赛'}
        </span>
        <span className="text-[10px] text-white/45">提示级 · 不改结论</span>
      </div>
      {card.strategy_signals && card.strategy_signals.length > 0 ? (
        <div className="space-y-2">
          {card.strategy_signals.map((s: any, i: number) => (
            <div key={i} className={`rounded-lg border px-3 py-2.5 ${s.suppressed ? 'bg-ember-500/[0.04] border-ember-500/15 opacity-60' : 'bg-white/[0.03] border-white/10'}`}>
              <div className="flex items-center justify-between gap-2">
                <span className="text-[13px] font-bold text-white">{s.name}{s.suppressed && <span className="text-[10px] text-ember-400 ml-1">(已抑制)</span>}</span>
                <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold ${
                  s.suppressed ? 'bg-ember-500/15 text-ember-300' : s.confidence === 'high' ? 'bg-accent/15 text-accent' : 'bg-ember-500/15 text-ember-300'
                }`}>{s.suppressed ? '⚠️抑制' : s.confidence === 'high' ? '高置信' : '方向性'}</span>
              </div>
              <div className={`text-[12px] mt-0.5 ${s.suppressed ? 'text-white/50 line-through' : 'text-accent'}`}>{s.direction}</div>
              <div className="flex items-center gap-2 mt-1.5">
                <div className="flex-1 h-1.5 rounded-full bg-white/[0.08] overflow-hidden">
                  <div className={`h-full rounded-full ${s.suppressed ? 'bg-ember-500/40' : 'bg-accent'}`} style={{ width: `${Math.max(6, Math.round((s.strength || 0) * 100))}%` }} />
                </div>
                <span className="text-[10px] font-mono text-white/65 w-9 text-right">{Math.round((s.strength || 0) * 100)}%</span>
              </div>
              <div className="text-[10px] text-white/55 mt-1">📊 {s.metric}</div>
              {s.suppressed
                ? <div className="text-[10px] text-ember-400 mt-0.5">⚠️ {s.suppress_reason || '与其他信号冲突,经回测抑制'}</div>
                : <div className="text-[10px] text-white/45 mt-0.5">{s.note}</div>}
            </div>
          ))}
        </div>
      ) : (
        <div className="text-[12px] text-white/55 bg-white/[0.03] border border-white/[0.06] rounded-lg px-3 py-2.5">
          {card.strategy_tier === 'obscure'
            ? '当前盘口无明显方向性偏差'
            : '主流联赛/杯赛不触发低级别联赛层信号（偏差规律不适用）'}
        </div>
      )}
    </div>
  )
}

// ── 子市场(折叠) ──
export function SubMarkets({ card, showSub, setShowSub }: {
  card: TerminalDecisionCard
  showSub: boolean
  setShowSub: (v: boolean) => void
}) {
  const sm = card.sub_markets
  if (!sm || (!sm.ou && !sm.draw && !sm.correct_score)) return null
  return (
    <div>
      <button onClick={() => setShowSub(!showSub)}
        className="w-full flex items-center justify-between text-[11px] font-mono text-white/70 hover:text-white uppercase tracking-widest py-1">
        <span>子市场 / SUB MARKETS (大小球 · 平局共识 · 波胆)</span>
        <span>{showSub ? '▾' : '▸'}</span>
      </button>
      {showSub && (
        <div className='bg-accent-inner rounded-lg p-3 mt-1 space-y-3 text-[11px] font-mono'>
          {sm.ou && (
            <div>
              <div className='text-white/85 font-bold mb-1'>大小球 OU</div>
              <div className='grid grid-cols-4 gap-2 text-white/85'>
                <div>edge <span className='text-accent font-bold'>{sm.ou.edge?.toFixed?.(3) ?? sm.ou.edge}</span></div>
                <div>fair <span className='text-white'>{sm.ou.fair_decimal?.toFixed?.(2) ?? sm.ou.fair_decimal}</span></div>
                <div>off <span className='text-white'>{sm.ou.fair_off_decimal?.toFixed?.(2) ?? sm.ou.fair_off_decimal}</span></div>
                <div>value <span className='text-pitch-400'>{sm.ou.value?.toFixed?.(2) ?? sm.ou.value}</span></div>
              </div>
            </div>
          )}
          {sm.draw && (
            <div>
              <div className='text-white/85 font-bold mb-1'>平局共识 DRAW</div>
              <div className='grid grid-cols-4 gap-2 text-white/85'>
                <div>edge <span className='text-accent font-bold'>{sm.draw.edge?.toFixed?.(3) ?? sm.draw.edge}</span></div>
                <div>fair <span className='text-white'>{sm.draw.fair_decimal?.toFixed?.(2) ?? sm.draw.fair_decimal}</span></div>
                <div>value <span className='text-pitch-400'>{sm.draw.value?.toFixed?.(2) ?? sm.draw.value}</span></div>
              </div>
            </div>
          )}
          {sm.correct_score?.rows && sm.correct_score.rows.length > 0 && (
            <div>
              <div className='text-white/85 font-bold mb-1'>波胆 CORRECT SCORE</div>
              <div className='space-y-0.5 max-h-40 overflow-y-auto'>
                {sm.correct_score.rows.slice(0, 20).map((r: any, i: number) => (
                  <div key={i} className='grid grid-cols-[40px_60px_50px_50px_60px] gap-1 text-white/85 hover:bg-white/[0.06] px-1 py-0.5 rounded'>
                    <span className='text-white font-bold'>{r.score}</span>
                    <span>p={r.prob?.toFixed?.(3) ?? r.prob}</span>
                    <span>pe={r.prob_eff?.toFixed?.(3) ?? r.prob_eff}</span>
                    <span>f={r.fair_decimal?.toFixed?.(1) ?? r.fair_decimal}</span>
                    <span className={typeof r.edge === 'number' && r.edge > 0 ? 'text-accent' : 'text-ember-500'}>e={typeof r.edge === 'number' ? r.edge.toFixed(3) : '--'}</span>
                  </div>
                ))}
                {sm.correct_score.rows.length > 20 && (
                  <div className='text-white/60 italic text-center'>+{sm.correct_score.rows.length - 20} 条更多...</div>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── softline (跨庄分歧) ──
export function SoftlineBanner({ card }: { card: TerminalDecisionCard }) {
  if (!card.softline) return null
  return (
    <div className={`text-xs rounded-lg px-3 py-2 ${
      card.softline.disagreement_detected ? 'bg-accent/10 text-accent' : 'bg-white/[0.04] text-white/75'
    }`}>
      跨庄分歧: {card.softline.disagreement_detected ? '检测到结构性分歧' : '无显著分歧'}
      {card.softline.clv_beat !== undefined && ` · CLV ${card.softline.clv_beat ? 'beat ✓' : 'lose'}`}
    </div>
  )
}

// ── 交叉标注颜色辅助 (供 OIP 区块使用) ──
export { dirColor, dirLabel, hcColor, ouColor }

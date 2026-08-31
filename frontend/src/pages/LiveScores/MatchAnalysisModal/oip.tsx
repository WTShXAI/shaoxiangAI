import type { RefObject } from 'react'
import type { TerminalDecisionCard } from '@/types'
import { pct, num, dirLabel, dirColor, hcColor, ouColor } from './format'

// ── OIP 波胆大区块 (λ / Top3 / 交叉标注 / 三角定位 / CS 时间线) ──
export function OipCard({ card, focus, oipRef }: {
  card: TerminalDecisionCard
  focus?: 'overview' | 'correct_score'
  oipRef: RefObject<HTMLDivElement>
}) {
  if (!card.oip) return null
  return (
    <div ref={oipRef} className={`scroll-mt-4 rounded-xl ${focus === 'correct_score' ? 'ring-1 ring-ember-500/30 bg-ember-500/[0.03] p-3 -mx-1' : ''}`}>
      <div className="flex items-center gap-2 mb-2">
        <div className="text-[11px] font-mono text-accent tracking-widest uppercase">
          {card.inplay ? '⚡ 条件波胆 (In-Play)' : 'OIP 波胆模型 / SCORE'}
        </div>
        {focus === 'correct_score' && (
          <span className="text-[10px] px-1.5 py-0.5 bg-ember-500/20 text-ember-300 rounded font-bold">波胆直达</span>
        )}
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="bg-accent-inner rounded-lg px-3 py-2">
          <div className="text-[11px] text-white/70">
            {card.inplay ? '期望进球 λ (剩余时间)' : '期望进球 λ'}
          </div>
          <div className="font-mono text-xs text-white mt-0.5">
            {card.inplay ? (
              <>主 <span className="text-accent">{num(card.inplay.remaining_lambda_h, 2)}</span> · 客 <span className="text-ember-400">{num(card.inplay.remaining_lambda_a, 2)}</span>
                <div className="text-[10px] text-white/50 mt-0.5">全场: 主{num(card.inplay.original_lambda_h, 2)} / 客{num(card.inplay.original_lambda_a, 2)}</div>
              </>
            ) : (
              <>主 {num(card.oip.lambda_h, 2)} · 客 {num(card.oip.lambda_a, 2)}</>
            )}
          </div>
        </div>
        <div className="bg-accent-inner rounded-lg px-3 py-2">
          <div className="text-[11px] text-white/70">{card.inplay ? '条件 Top3 比分' : 'Top3 比分'}</div>
          <div className="font-mono text-xs text-white/85 mt-0.5">
            {(card.oip.top3_scores || []).map((s, i) => `${s}(${pct(card.oip!.top3_prob?.[i], 0)})`).join('  ')}
          </div>
        </div>
      </div>
      <div className="flex gap-2 mt-2">
        {card.oip.over15 !== undefined && <span className="text-[11px] font-mono text-white/80 bg-white/[0.06] px-2 py-1 rounded">≥1.5 {pct(card.oip.over15, 0)}</span>}
        {card.oip.over25 !== undefined && <span className="text-[11px] font-mono text-white/80 bg-white/[0.06] px-2 py-1 rounded">≥2.5 {pct(card.oip.over25, 0)}</span>}
        {card.oip.over35 !== undefined && <span className="text-[11px] font-mono text-white/80 bg-white/[0.06] px-2 py-1 rounded">≥3.5 {pct(card.oip.over35, 0)}</span>}
      </div>

      {/* 波胆 × 让球 × 大小球 交叉标注表 */}
      {card.oip.scores_annotated && card.oip.scores_annotated.length > 0 && (
        <div className="mt-3">
          <div className="text-[10px] text-white/70 mb-1.5 flex items-center gap-2">
            <span>波胆 × 让球{card.oip.ah_line != null ? `(${card.oip.ah_line})` : ''} × 大小球{card.oip.ou_line != null ? `(${card.oip.ou_line})` : ''} 交叉</span>
            {card.oip.discipline?.best_direction && (
              <span className="text-[10px] text-accent">· 最强方向 {dirLabel(card.oip.discipline.best_direction)}</span>
            )}
          </div>
          <div className="space-y-1">
            {card.oip.scores_annotated.map((s) => (
              <div key={s.score} className={`flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-[12px] font-mono ${
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
                  <span className="text-[10px] text-ember-400 ml-auto">长尾负EV</span>
                )}
              </div>
            ))}
          </div>
          {card.oip.discipline?.multi_direction && (
            <div className="mt-2 text-[11px] text-ember-400/80 bg-ember-500/[0.06] border border-ember-500/10 rounded-lg px-2.5 py-1.5">
              ⚠ Top波胆跨多方向({card.oip.discipline.direction_count.H > 0 && `主${card.oip.discipline.direction_count.H} `}{card.oip.discipline.direction_count.D > 0 && `平${card.oip.discipline.direction_count.D} `}{card.oip.discipline.direction_count.A > 0 && `客${card.oip.discipline.direction_count.A}`}) · 撒网对冲=消耗价值 · 建议锁定<strong className="text-accent">{dirLabel(card.oip.discipline.best_direction)}</strong>方向
            </div>
          )}
        </div>
      )}
      {/* 市场结构波胆三角定位 (OU×AH×1X2×CS 取交集, 可审计) */}
      {card.oip?.cs_triangulation && (
        <div className="mt-3 rounded-xl bg-accent/5 border border-accent/10 p-3">
          <div className="flex items-center gap-2 mb-2 flex-wrap">
            <div className="text-[11px] font-mono text-accent tracking-widest uppercase">市场结构波胆三角定位</div>
            <span className="text-[10px] px-1.5 py-0.5 bg-accent/15 text-accent rounded font-bold">OU×AH×1X2×CS</span>
            {card.oip.cs_triangulation.cs_coverage && (
              <span className="text-[10px] text-white/70">{card.oip.cs_triangulation.cs_coverage}</span>
            )}
            {card.oip.cs_triangulation.winner && (
              <span className="text-[10px] text-white/80">胜方·{dirLabel(card.oip.cs_triangulation.winner)}</span>
            )}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {(card.oip.cs_triangulation.ranked || []).slice(0, 8).map((sc, i) => {
              const cand = (card.oip!.cs_triangulation!.candidates || []).find(c => c.score === sc)
              const isTop = i === 0
              return (
                <div key={sc} className={`px-2 py-1 rounded-lg text-[12px] font-mono ${isTop ? 'bg-accent text-black font-bold' : 'bg-white/[0.06] text-white/90'}`}>
                  <span className="font-bold">{sc}</span>
                  {cand?.blend != null && <span className="text-[10px] ml-1 opacity-70">{pct(cand.blend, 0)}</span>}
                </div>
              )
            })}
          </div>
          {card.oip.cs_triangulation.notes && card.oip.cs_triangulation.notes.length > 0 && (
            <div className="mt-2 space-y-0.5">
              {card.oip.cs_triangulation.notes.map((n, i) => (
                <div key={i} className="text-[10px] text-white/70 leading-tight">· {n}</div>
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
              <div className="text-[11px] font-mono text-accent tracking-widest uppercase">CS 实时赔率时间线</div>
              <span className="text-[10px] px-1.5 py-0.5 bg-accent/15 text-accent rounded font-bold">初盘→中场→当前</span>
              {tl.has_ht && <span className="text-[10px] text-white/70">含中场收盘</span>}
              {tl.drift_summary?.lean && (
                <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold ${
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
                  <div key={sc} className="flex items-center gap-2 px-2 py-1 rounded text-[12px] font-mono">
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
            <div className="mt-1.5 text-[10px] text-white/50">中漂=初盘→中场收盘 · 现漂=初盘→当前 · 绿↓=临场被看好(资金站该比分) · 红↑=被看衰 · 仅 GQ 已采集比赛</div>
          </div>
        );
      })()}
    </div>
  )
}

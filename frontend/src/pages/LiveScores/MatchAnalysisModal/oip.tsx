import type { RefObject } from 'react'
import type { TerminalDecisionCard } from '@/types'
import { pct, num } from './format'

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
          {card.inplay ? '⚡ OIP 波胆引擎 · λ机制' : 'OIP 波胆引擎 · λ机制'}
        </div>
        <span className="text-[10px] px-1.5 py-0.5 bg-ember-500/15 text-ember-300 rounded font-bold">波胆基座</span>
      </div>
      {/* λ 期望进球 — OIP 引擎核心输入 (波胆机制, 非波胆精选) */}
      <div className="bg-accent-inner rounded-lg px-3 py-2 mb-2">
        <div className="text-[11px] text-white/70">
          {card.inplay ? '期望进球 λ (剩余时间)' : '期望进球 λ (OIP 引擎)'}
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
      <div className="flex gap-2">
        {card.oip.over15 !== undefined && <span className="text-[11px] font-mono text-white/80 bg-white/[0.06] px-2 py-1 rounded">≥1.5 {pct(card.oip.over15, 0)}</span>}
        {card.oip.over25 !== undefined && <span className="text-[11px] font-mono text-white/80 bg-white/[0.06] px-2 py-1 rounded">≥2.5 {pct(card.oip.over25, 0)}</span>}
        {card.oip.over35 !== undefined && <span className="text-[11px] font-mono text-white/80 bg-white/[0.06] px-2 py-1 rounded">≥3.5 {pct(card.oip.over35, 0)}</span>}
      </div>
      <div className="mt-2 text-[10px] text-white/50 leading-snug">
        波胆精选由上方「黄金神瞳 CS 信任卡」独家给出 (cs_trust_model: OIP 为基座 + 实证频率 + 诱导标记, 准确率最高); 本块仅展示 λ 机制与庄家 CS 实时赔率, 不重复列比分。
      </div>

      {/* CS 实时赔率时间线 (初盘/中场/当前 + drift) — 仅 GQ 已采集比赛 */}
      {card.oip?.cs_odds_timeline && (() => {
        const tl = card.oip!.cs_odds_timeline!
        const lines = Object.entries(tl.open || tl.live || {})
          .sort((a, b) => (a[1] as number) - (b[1] as number))
          .slice(0, 6)
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
                const ht = (tl.ht_close as Record<string, number>)?.[sc]
                const lv = (tl.live as Record<string, number>)?.[sc]
                const drift = (tl.drift_live_open as Record<string, number>)?.[sc]
                const htDrift = (tl.drift_ht_open as Record<string, number>)?.[sc]
                const driftCls = drift == null ? 'text-white/40' : drift < 0 ? 'text-field-400' : drift > 0 ? 'text-danger-400' : 'text-white/50'
                const driftTxt = drift == null ? '—' : `${drift > 0 ? '+' : ''}${num(drift, 2)}`
                const htDriftCls = htDrift == null ? 'text-white/40' : htDrift < 0 ? 'text-field-400' : htDrift > 0 ? 'text-danger-400' : 'text-white/50'
                const htDriftTxt = htDrift == null ? '—' : `${htDrift > 0 ? '+' : ''}${num(htDrift, 2)}`
                return (
                  <div key={sc} className="flex items-center gap-2 px-2 py-1 rounded text-[12px] font-mono">
                    <span className="font-bold w-8 text-white">{sc}</span>
                    <span className="w-14 text-white/70">初 {num(op as number, 2)}</span>
                    <span className="w-14 text-white/70">中 {ht != null ? num(ht, 2) : '—'}</span>
                    <span className="w-14 text-accent font-bold">现 {lv != null ? num(lv, 2) : '—'}</span>
                    <span className={`w-14 text-right ${htDriftCls}`}>{htDriftTxt}</span>
                    <span className={`w-14 text-right ${driftCls}`}>{driftTxt}</span>
                  </div>
                )
              })}
            </div>
            <div className="mt-1.5 text-[10px] text-white/50">中漂=初盘→中场收盘 · 现漂=初盘→当前 · 绿↓=临场被看好(资金站该比分) · 红↑=被看衰 · 仅 GQ 已采集比赛</div>
          </div>
        )
      })()}
    </div>
  )
}

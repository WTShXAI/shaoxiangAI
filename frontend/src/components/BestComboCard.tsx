/**
 * 4 盘口综合诚实分析卡 (2026-08-31, IR-20/IR-21/IR-30)
 *
 * 消费 /api/best-combo/analyze 返回的 analysis.best_combo.analyze_best_combo 结果。
 * 四条件: 胜平负 / 大小球 / 让球 / 波胆。所有"edge"标注来自收盘现实价压测证据;
 * 候选信号明确标注"未部署/未真实下注"。波胆=概率分布, 非单点预测(IR-03)。
 */

const VERDICT_STYLE: Record<string, string> = {
  候选信号: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300',
  弱候选: 'border-amber-500/30 bg-amber-500/[0.06] text-amber-300/90',
  分析信号: 'border-sky-500/30 bg-sky-500/[0.06] text-sky-300/90',
  概率分布: 'border-field-500/30 bg-field-500/[0.06] text-field-300/90',
  无信号: 'border-white/10 bg-white/5 text-ink-muted',
}

// ── 类型契约: /api/best-combo/analyze → analysis.best_combo.analyze_best_combo (v7.5 收敛) ──
export interface BestComboMarket {
  verdict: string
  label: string
  note?: string
  // ① 1X2
  probs?: [number, number, number]
  direction?: string
  // ② OU
  line?: number
  p_over?: number
  imp_over?: number
  side?: 'over' | 'under'
  edge?: number
  // ③ AH
  poisson_home_cover?: number
  imp_home_cover?: number
  // ④ CS
  top_scorelines?: { score: string; prob: number }[]
}
export interface BestComboResult {
  x2: BestComboMarket | null
  ou: BestComboMarket | null
  ah: BestComboMarket | null
  cs: BestComboMarket | null
  honesty?: string
}

export default function BestComboCard({ result }: { result: BestComboResult | null | undefined }) {
  if (!result) return null

  const x2 = result.x2
  const ou = result.ou
  const ah = result.ah
  const cs = result.cs

  return (
    <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/[0.04] p-4">
      <div className="flex items-center justify-between mb-2 flex-wrap gap-1.5">
        <span className="text-[12px] font-semibold text-emerald-300">4 盘口综合 · 诚实分析面板</span>
        <span className="text-[10px] text-ink-muted font-mono">来源: 开盘赔率 + 模型(收盘压测证据)</span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {/* ① 胜平负 */}
        <div className="rounded-lg border border-sky-500/20 bg-surface-card/40 p-2.5">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-[10px] font-semibold text-sky-300">① 胜平负 (1X2)</span>
            {x2 && <span className={`text-[9px] px-1.5 py-0.5 rounded border ${VERDICT_STYLE[x2.verdict] || VERDICT_STYLE['分析信号']}`}>{x2.verdict}</span>}
          </div>
          {x2 ? (
            <>
              <div className="flex justify-between text-[11px] font-mono mb-1">
                <span className="text-ink-primary">主 {((x2.probs?.[0] ?? 0)*100).toFixed(1)}%</span>
                <span className="text-ink-primary">平 {((x2.probs?.[1] ?? 0)*100).toFixed(1)}%</span>
                <span className="text-ink-primary">客 {((x2.probs?.[2] ?? 0)*100).toFixed(1)}%</span>
              </div>
              <div className="text-[10px] text-ink-muted">方向倾向: <b className="text-sky-300">{x2.direction}</b> · {x2.label}</div>
            </>
          ) : <div className="text-[10px] text-ink-muted">无 1X2 赔率</div>}
        </div>

        {/* ② 大小球 */}
        <div className="rounded-lg border border-emerald-500/20 bg-surface-card/40 p-2.5">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-[10px] font-semibold text-emerald-300">② 大小球 (OU)</span>
            {ou && <span className={`text-[9px] px-1.5 py-0.5 rounded border ${VERDICT_STYLE[ou.verdict] || VERDICT_STYLE['无信号']}`}>{ou.verdict}</span>}
          </div>
          {ou ? (
            <>
              <div className="text-[10px] font-mono text-ink-primary">盘口 {ou.line} · 模型P(大) {((ou.p_over ?? 0)*100).toFixed(1)}% / 市场 {((ou.imp_over ?? 0)*100).toFixed(1)}%</div>
              <div className="text-[10px] text-ink-muted mt-0.5">
                {ou.verdict === '候选信号'
                  ? <>低线窄策略 → 倾向 <b className="text-emerald-300">{ou.side}</b> (edge {((ou.edge ?? 0)*100).toFixed(1)}%, 前向监控中, 未部署)</>
                  : <>无信号: {ou.label}</>}
              </div>
            </>
          ) : <div className="text-[10px] text-ink-muted">无大小球赔率</div>}
        </div>

        {/* ③ 让球 */}
        <div className="rounded-lg border border-amber-500/20 bg-surface-card/40 p-2.5">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-[10px] font-semibold text-amber-300">③ 让球 (AH)</span>
            {ah && <span className={`text-[9px] px-1.5 py-0.5 rounded border ${VERDICT_STYLE[ah.verdict] || VERDICT_STYLE['弱候选']}`}>{ah.verdict}</span>}
          </div>
          {ah ? (
            <>
              <div className="text-[10px] font-mono text-ink-primary">盘口 {ah.line} · 泊松主覆 {((ah.poisson_home_cover ?? 0)*100).toFixed(1)}% / 市场 {((ah.imp_home_cover ?? 0)*100).toFixed(1)}%</div>
              <div className="text-[10px] text-ink-muted mt-0.5">{ah.label} · {ah.note}</div>
            </>
          ) : <div className="text-[10px] text-ink-muted">AH 数据未提供(赛事列表仅含1X2+OU) · 弱候选, 待接入</div>}
        </div>

        {/* ④ 波胆 */}
        <div className="rounded-lg border border-field-500/20 bg-surface-card/40 p-2.5">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-[10px] font-semibold text-field-300">④ 波胆 (CS)</span>
            {cs && <span className={`text-[9px] px-1.5 py-0.5 rounded border ${VERDICT_STYLE[cs.verdict] || VERDICT_STYLE['概率分布']}`}>{cs.verdict}</span>}
          </div>
          {cs ? (
            <>
              <div className="space-y-0.5">
                {(cs.top_scorelines || []).slice(0, 5).map((r) => (
                  <div key={r.score} className="flex justify-between text-[10px] font-mono">
                    <span className="text-ink-primary">{r.score}</span>
                    <span className="text-field-300/90">{(r.prob*100).toFixed(1)}%</span>
                  </div>
                ))}
              </div>
              <div className="text-[10px] text-ink-muted mt-0.5">{cs.label}</div>
            </>
          ) : <div className="text-[10px] text-ink-muted">无波胆概率</div>}
        </div>
      </div>

      {/* 诚实边界注记 */}
      <div className="text-[10px] text-ink-muted/70 mt-2 leading-relaxed">
        {result.honesty} · 唯一通过收盘现实价+CI 审视的是 OU 低线(2.0–2.75)窄策略(前向监控中);
        让球仅真实但薄弱的狗覆盖偏差; 波胆=概率分布非单点预测(庄家诱导器)。建仓须人工审批(IR-21)。
      </div>
    </div>
  )
}

/**
 * 开盘天眼 +EV 裁判卡 (2026-08-31 部署, IR-20/IR-21/IR-30)
 *
 * 消费 /api/open-eye/recommend 返回的 pipeline.open_eye_predictor.recommend 结果。
 * 诚实边界: 仅建议不自动下注; 不标稳赢; 覆盖不足(未知/冷门队)或 edge<=0 -> PASS。
 */

const SIDE_LABEL: Record<string, string> = { H: '主胜', D: '平局', A: '客胜' }

// ── 类型契约: /api/open-eye/recommend → pipeline.open_eye_predictor.recommend (v7.5 收敛) ──
export interface OpenEyeRecommend {
  ok: boolean
  reason?: string
  side?: string
  odds?: number
  edge_pp?: number
  model_prob?: number
  market_implied?: number
  kelly_frac?: number
  prob_hda?: [number, number, number]
  price_source?: 'gq_lookup' | string
  compliant?: string
}

export default function OpenEyeCard({ result }: { result: OpenEyeRecommend | null | undefined }) {
  if (!result) return null

  // PASS / 异常: 覆盖门不达标 或 无赔率
  if (!result.ok) {
    return (
      <div className="rounded-xl border border-white/10 bg-white/5 p-4">
        <div className="flex items-center justify-between mb-2 flex-wrap gap-1.5">
          <span className="text-[12px] font-semibold text-indigo-300">开盘天眼 · +EV 裁判</span>
          <span className="text-[10px] text-ink-muted font-mono">来源: 独立实力特征 + 开盘盘口</span>
        </div>
        <div className="rounded-lg border border-white/10 bg-surface-card/40 p-2.5">
          <span className="text-[10px] px-1.5 py-0.5 rounded border border-white/10 bg-white/5 text-ink-muted">天眼 PASS</span>
          <div className="text-[11px] text-ink-muted mt-1.5">{result.reason || '本场不满足天眼可下注边界'}</div>
        </div>
        <div className="text-[10px] text-ink-muted/70 mt-2 leading-relaxed">
          天眼仅对"两队均有可靠独立实力历史"的比赛有效(覆盖门); 未知/冷门队自动 PASS。仅建议, 不自动下注(IR-21)。
        </div>
      </div>
    )
  }

  const side = SIDE_LABEL[result.side || ''] || result.side || '-'
  const edgePos = (result.edge_pp ?? 0) > 0
  return (
    <div className="rounded-xl border border-indigo-500/30 bg-indigo-500/[0.04] p-4">
      <div className="flex items-center justify-between mb-2 flex-wrap gap-1.5">
        <span className="text-[12px] font-semibold text-indigo-300">开盘天眼 · +EV 裁判</span>
        <span className="text-[10px] text-ink-muted font-mono">
          来源: 独立实力特征 + {result.price_source === 'gq_lookup' ? 'GQ盘口' : '当前盘口'}
        </span>
      </div>

      {/* 主判定 */}
      <div className="rounded-lg border border-indigo-500/20 bg-surface-card/40 p-2.5">
        <div className="flex items-center justify-between mb-1.5">
          <span className="text-[10px] font-semibold text-indigo-300">建议边 (EYE_OPEN_RESID)</span>
          <span className={`text-[9px] px-1.5 py-0.5 rounded border ${edgePos ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300' : 'border-white/10 bg-white/5 text-ink-muted'}`}>
            {edgePos ? '有 +EV' : '无 edge'}
          </span>
        </div>
        <div className="flex justify-between items-baseline text-[11px] font-mono mb-1">
          <span className="text-ink-primary">倾向 <b className="text-indigo-300">{side}</b> @ {Number(result.odds ?? 0).toFixed(2)}</span>
          <span className={edgePos ? 'text-emerald-300' : 'text-ink-muted'}>edge {(result.edge_pp ?? 0) > 0 ? '+' : ''}{result.edge_pp ?? 0} pp</span>
        </div>
        <div className="flex justify-between text-[11px] font-mono mb-1">
          <span className="text-ink-primary">模型P {((result.model_prob ?? 0) * 100).toFixed(1)}%</span>
          <span className="text-ink-primary">市场隐含 {((result.market_implied ?? 0) * 100).toFixed(1)}%</span>
        </div>
        <div className="text-[10px] text-ink-muted">1/4-Kelly 建议注码比例: <b className="text-indigo-300">{((result.kelly_frac ?? 0) * 100).toFixed(2)}%</b> 本金 (单注封顶 10%)</div>
      </div>

      {/* 三方向概率 */}
      <div className="rounded-lg border border-white/10 bg-surface-card/40 p-2.5 mt-2">
        <div className="text-[10px] font-semibold text-ink-muted mb-1">模型三方向概率 (H/D/A)</div>
        <div className="flex justify-between text-[11px] font-mono">
          <span className="text-ink-primary">主 {((result.prob_hda?.[0] ?? 0) * 100).toFixed(1)}%</span>
          <span className="text-ink-primary">平 {((result.prob_hda?.[1] ?? 0) * 100).toFixed(1)}%</span>
          <span className="text-ink-primary">客 {((result.prob_hda?.[2] ?? 0) * 100).toFixed(1)}%</span>
        </div>
      </div>

      <div className="text-[10px] text-ink-muted/70 mt-2 leading-relaxed">
        {result.compliant} · 天眼在"两队已知"干净子集验证 ROI +10.05% (CI 正, pos_ev=True); 未知队子集为负 → 自动 PASS。建仓须人工审批(IR-21), 不得标注稳赢(IR-30)。
      </div>
    </div>
  )
}

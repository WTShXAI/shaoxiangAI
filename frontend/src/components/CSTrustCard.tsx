/**
 * CS 信任卡 (08-22 v1 + 08-25 第三栏真实历史频率形态)
 *
 * 三栏: ①结构模型(1X2+OU+AH 三盘交叉拟合 λμ → 全比分分布, 覆盖100%)
 *       ②庄家 CS 线(隐含分布, 仅覆盖已列比分 ~25 项)
 *       ③历史实证(真实完赛库每比分线占比, 禁硬编码)
 * 主推背离检测(ALIGNED/DIVERGED) + 诱导标记 + trust_score(相对庄家线可信度, 非预测胜率) + 免责(IR-03)。
 * 注: 纯开盘三盘拟合形态(2026-08-22 建立, 08-25 接真实历史频率), 不含滚球即时盘模式。
 */

interface TrustRow { score: string; prob: number; hist_freq?: number | null }

// ── 类型契约: /api/cs/trust-card 返回 (v7.5 收敛) ──
export interface CsTrustCardData {
  found?: boolean
  our_top5?: TrustRow[]
  book_top5?: TrustRow[]
  alignment?: string
  induce_level?: 'NONE' | 'YELLOW' | 'RED'
  trust_score?: number | null
  fit_sources?: string[]
  book_listed_count?: number
  favorite_score?: string
  historical_freq_total_matches?: number
  induce_reasons?: string[]
  margin?: number | null
  trust_notes?: string[]
  historical_cheapest_hit_rate: number
}
export interface CsInduceData {
  verdict?: string
  level?: string
}

export default function CSTrustCard({ trustCard, induce }: { trustCard: CsTrustCardData | null | undefined; induce?: CsInduceData | null }) {
  if (!trustCard || (!trustCard.found && !(trustCard.our_top5?.length))) return null

  const ourTop5: TrustRow[] = trustCard.our_top5 || []
  const bookTop5: TrustRow[] = trustCard.book_top5 || []
  const alignment = trustCard.alignment
  const induceLevel = trustCard.induce_level || 'NONE'

  // 历史实证栏: 直接用结构 top5 每行挂的 hist_freq (与结构同口径对比)
  const histRows: TrustRow[] = ourTop5
    .filter((r) => r.hist_freq != null)
    .map((r) => ({ score: r.score, prob: r.hist_freq as number }))

  const Bar = ({ pct, color }: { pct: number; color: string }) => (
    <div className="h-1.5 rounded bg-white/[0.05] overflow-hidden">
      <div className={`h-full ${color}`} style={{ width: `${Math.min(pct * (ourTop5[0] ? 100 / (ourTop5[0].prob * 100) : 1), 100)}%` }} />
    </div>
  )

  return (
    <div className="rounded-xl border border-ember-500/30 bg-ember-500/[0.04] p-4">
      {/* 头部: 标题 + 主推背离/信任分徽标 */}
      <div className="flex items-center justify-between mb-2 flex-wrap gap-1.5">
        <div className="flex items-center gap-2">
          <span className="text-[12px] font-semibold text-ember-300">CS 波胆 · 三盘交叉信任卡</span>
          {alignment && (
            <span className={`text-[10px] px-1.5 py-0.5 rounded border font-semibold ${
              alignment === 'ALIGNED' ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'
              : alignment === 'DIVERGED' ? 'border-red-500/40 bg-red-500/15 text-red-300'
              : 'border-white/10 bg-white/5 text-ink-muted'}`}
            >
              {alignment === 'ALIGNED' ? '主推一致' : alignment === 'DIVERGED' ? '主推背离 ⚠' : alignment}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 text-[10px] text-ink-muted">
          {trustCard.trust_score != null && <span>信任分 <b className="text-ink-primary">{trustCard.trust_score}</b></span>}
          {(trustCard.fit_sources || []).length > 0 && (
            <span className="font-mono">{(trustCard.fit_sources || []).join('+')}</span>
          )}
        </div>
      </div>

      {/* 三栏: 结构 / 庄家 / 历史实证 */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {/* ① 结构模型 */}
        <div className="rounded-lg border border-ember-500/20 bg-surface-card/40 p-2.5">
          <div className="text-[10px] font-semibold text-ember-300 mb-1.5">① 结构模型 <span className="text-ink-muted font-normal">覆盖100%</span></div>
          <div className="space-y-1">
            {ourTop5.map((r) => (
              <div key={r.score} className="text-[10px]">
                <div className="flex justify-between">
                  <span className="font-mono text-ink-primary">{r.score}</span>
                  <span className="text-ember-300/90 font-mono">{(r.prob * 100).toFixed(1)}%</span>
                </div>
                <Bar pct={r.prob} color="bg-ember-400/70" />
              </div>
            ))}
            {ourTop5.length === 0 && <div className="text-[10px] text-ink-muted">无初盘 1X2, 无法拟合</div>}
          </div>
        </div>
        {/* ② 庄家 CS 线 */}
        <div className="rounded-lg border border-frost-500/20 bg-surface-card/40 p-2.5">
          <div className="text-[10px] font-semibold text-frost-300 mb-1.5">
            ② 庄家 CS 线 <span className="text-ink-muted font-normal">{trustCard.book_listed_count ? `${trustCard.book_listed_count} 项` : '未开盘'}</span>
          </div>
          <div className="space-y-1">
            {bookTop5.map((r) => (
              <div key={r.score} className="text-[10px]">
                <div className="flex justify-between">
                  <span className="font-mono text-ink-primary">{r.score}{trustCard.favorite_score === r.score && <span className="text-rose-300 ml-1" title="庄家赔率最低 = 主推比分">←主推</span>}</span>
                  <span className="text-frost-300/90 font-mono">{(r.prob * 100).toFixed(1)}%</span>
                </div>
                <Bar pct={r.prob} color="bg-frost-400/70" />
              </div>
            ))}
            {bookTop5.length === 0 && (
              <div className="text-[10px] text-ink-muted leading-relaxed">
                本场庄家未开 CS 矩阵<br />(开盘结构诚实推导, 不伪造庄家线)
              </div>
            )}
          </div>
        </div>
        {/* ③ 历史实证 */}
        <div className="rounded-lg border border-field-500/20 bg-surface-card/40 p-2.5">
          <div className="text-[10px] font-semibold text-field-300 mb-1.5">
            ③ 历史实证 <span className="text-ink-muted font-normal">{trustCard.historical_freq_total_matches ? `${trustCard.historical_freq_total_matches} 场` : ''}</span>
          </div>
          <div className="space-y-1">
            {histRows.map((r) => (
              <div key={r.score} className="text-[10px]">
                <div className="flex justify-between">
                  <span className="font-mono text-ink-primary">{r.score}</span>
                  <span className="text-field-300/90 font-mono">{(r.prob * 100).toFixed(1)}%</span>
                </div>
                <Bar pct={r.prob} color="bg-field-400/70" />
              </div>
            ))}
            {histRows.length === 0 && <div className="text-[10px] text-ink-muted">历史库加载中/不可用</div>}
          </div>
        </div>
      </div>

      {/* 诱导标记 */}
      {(induceLevel !== 'NONE' || induce) && (
        <div className={`mt-2 text-[11px] px-3 py-2 rounded-lg border ${
          induceLevel === 'RED' ? 'border-red-500/40 bg-red-500/[0.08] text-red-300'
          : induceLevel === 'YELLOW' ? 'border-amber-500/30 bg-amber-500/[0.05] text-amber-300/90'
          : 'border-white/10 bg-white/[0.03] text-ink-muted'
        }`}>
          <span className="font-semibold">
            庄家诱导标记: {induceLevel === 'RED' ? 'RED ⚠' : induceLevel === 'YELLOW' ? 'YELLOW' : 'NONE'}
          </span>
          {(trustCard.induce_reasons || []).map((s: string, i: number) => (
            <span key={i} className="ml-2">{s}</span>
          ))}
          {trustCard.margin != null && <span className="ml-2 font-mono">CS抽水 {(trustCard.margin * 100).toFixed(0)}%</span>}
          {induce?.verdict && <span className="ml-2">{induce.verdict}</span>}
        </div>
      )}

      {/* 信任注记 + 免责 */}
      {(trustCard.trust_notes || []).length > 0 && (
        <div className="mt-1.5 space-y-0.5">
          {(trustCard.trust_notes || []).map((n: string, i: number) => (
            <div key={i} className="text-[10px] text-ink-muted">· {n}</div>
          ))}
        </div>
      )}
      <div className="text-[10px] text-ink-muted/70 mt-1.5">
        分布校准(覆盖全比分) · 主推背离 = 庄家主推不在结构高概率区(诱导嫌疑) · 最便宜波胆历史命中仅 {(trustCard.historical_cheapest_hit_rate * 100).toFixed(1)}% · 非投注建议 / 不预测单点比分
      </div>
    </div>
  )
}

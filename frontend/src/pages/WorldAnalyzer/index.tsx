import { useState, useCallback, useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { worldAnalyzerService } from '@/services/api'
import PageHeader from '@/components/layout/PageHeader'

// ═══ 类型 (后端 /api/world-analyze → pipeline/world_analyzer.analyze_match) ═══
interface WorldAnalyzeResult {
  version?: string
  generated_at?: string
  match?: { home?: string; away?: string; league?: string | null; kickoff?: string | null }
  market?: {
    '1x2_devig'?: [number, number, number] | null
    overround?: number | null
    ou_line?: number | string | null
    ou_over_devig?: number | null
    ah_line?: number | string | null
    ah_home_devig?: number | null
  }
  models?: {
    independent_1x2?: [number, number, number] | null
    fl_1x2?: [number, number, number] | null
    fused_1x2?: [number, number, number] | null
    fl_ah?: [number, number] | null
    poisson_over?: number | null
    cs_odds?: {
      qualified: boolean
      reason: string
      top5?: [string, number][] | null
      three_way?: [number, number, number] | null
    } | null
  }
  consensus?: {
    model_avg_1x2?: [number, number, number] | null
    lean?: string | null
    vs_market_pp?: [number, number, number] | null
    n_models?: number
  }
  edge_1x2?: { side?: string; win_rate?: number | null; implied?: number | null; edge_pp?: number | null } | null
  ou?: { model_p_over?: number; market_p_over?: number; edge_pp?: number; lean?: string } | null
  drift?: {
    drift_pp_home?: number
    drift_pp_draw?: number
    drift_pp_away?: number
    strongest_drift?: number
  } | null
  league_context?: { league?: string; n?: number; home_rate?: number; draw_rate?: number; away_rate?: number; avg_goals?: number } | null
  honest_flags?: string[]
  runtime_ms?: number
  error?: string
}

// ═══ 辅助格式化 ═══
const pct = (v: number | undefined | null, digits = 1) =>
  typeof v === 'number' && !isNaN(v) ? (v * 100).toFixed(digits) + '%' : '—'
const pp = (v: number | undefined | null) =>
  typeof v === 'number' && !isNaN(v) ? (v > 0 ? '+' : '') + v.toFixed(1) + 'pp' : '—'
const sideLabel = (s: string) => (s === 'H' ? '主胜' : s === 'D' ? '平局' : s === 'A' ? '客胜' : s)

// 概率条 (三方向)
function ProbBar({ p, color }: { p: number; color: string }) {
  return (
    <div className="h-1.5 rounded-full bg-ink-primary/[0.08] overflow-hidden flex-1">
      <div className={`h-full rounded-full ${color}`} style={{ width: `${Math.min(100, Math.max(2, p * 100))}%` }} />
    </div>
  )
}

// ═══ 页面 ═══
// 支持 query 参数回填 (赛程页"世界级分析 →"跳转): ?home=&away=&league=&kickoff=&h=&d=&a=&op_h=&op_d=&op_a=&ou_line=&ou_over=&ou_under=
export default function WorldAnalyzer() {
  const [sp] = useSearchParams()
  const qv = (k: string) => sp.get(k) ?? ''
  const [home, setHome] = useState(qv('home'))
  const [away, setAway] = useState(qv('away'))
  const [league, setLeague] = useState(qv('league'))
  const [h, setH] = useState(qv('h')); const [d, setD] = useState(qv('d')); const [a, setA] = useState(qv('a'))
  const [ouLine, setOuLine] = useState(qv('ou_line')); const [ouOver, setOuOver] = useState(qv('ou_over')); const [ouUnder, setOuUnder] = useState(qv('ou_under'))
  const [ahLine, setAhLine] = useState(qv('ah_line')); const [ahHome, setAhHome] = useState(qv('ah_home')); const [ahAway, setAhAway] = useState(qv('ah_away'))
  const [opH, setOpH] = useState(qv('op_h')); const [opD, setOpD] = useState(qv('op_d')); const [opA, setOpA] = useState(qv('op_a'))
  const [kickoff, setKickoff] = useState(qv('kickoff'))

  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<WorldAnalyzeResult | null>(null)

  const num = (s: string) => (s.trim() === '' ? undefined : Number(s))

  const run = useCallback(async () => {
    if (!home.trim() || !away.trim()) { setError('请填写主队与客队'); return }
    setLoading(true); setError(''); setResult(null)
    try {
      const res = await worldAnalyzerService.analyze({
        home: home.trim(), away: away.trim(),
        league: league.trim() || undefined,
        h: num(h), d: num(d), a: num(a),
        ou_line: num(ouLine), ou_over: num(ouOver), ou_under: num(ouUnder),
        ah_line: num(ahLine), ah_home: num(ahHome), ah_away: num(ahAway),
        op_h: num(opH), op_d: num(opD), op_a: num(opA),
        kickoff: kickoff.trim() || undefined,
      })
      const r = (res.data as any)?.data || res.data
      setResult(r as WorldAnalyzeResult)
    } catch (e: unknown) {
      // normalizeApiError: 网络/超时/5xx 统一转友好文案
      setError(e instanceof Error ? e.message : '分析失败')
    } finally {
      setLoading(false)
    }
  }, [home, away, league, h, d, a, ouLine, ouOver, ouUnder, ahLine, ahHome, ahAway, opH, opD, opA, kickoff])

  // query 回填 (赛程页跳转) → mount 后自动触发分析; 防重复只跑一次
  const autoRan = useRef(false)
  useEffect(() => {
    if (!autoRan.current && home.trim() && away.trim()) {
      autoRan.current = true
      run()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const fillPreset = () => {
    setHome('皇家马德里'); setAway('巴塞罗那'); setLeague('西甲')
    setH('1.90'); setD('3.50'); setA('4.20')
    setOuLine('2.75'); setOuOver('1.85'); setOuUnder('1.95')
    setAhLine('-0.75'); setAhHome('1.90'); setAhAway('1.95')
    setOpH('2.10'); setOpD('3.40'); setOpA('3.60')
  }

  const m = result?.match
  const mk = result?.market
  const md = result?.models
  const cs = result?.consensus
  const edge = result?.edge_1x2
  const ou = result?.ou
  const dr = result?.drift
  const lg = result?.league_context

  return (
    <div className="space-y-4">
      <PageHeader
        title="世界级分析器"
        subtitle="市场锚 + 模型矩阵 + 一致性 + Edge三件套 + 漂移 + 联赛背景 · 分析非预测 (IR-20)"
      />

      {/* 输入区 */}
      <div className="bg-accent-card border border-surface-border rounded-xl p-4 space-y-3">
        <div className="flex items-center gap-3 flex-wrap">
          <input value={home} onChange={e => setHome(e.target.value)} placeholder="主队 (必填)" className="px-3 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary placeholder:text-ink-disabled w-44" />
          <span className="text-ink-muted text-sm">VS</span>
          <input value={away} onChange={e => setAway(e.target.value)} placeholder="客队 (必填)" className="px-3 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary placeholder:text-ink-disabled w-44" />
          <input value={league} onChange={e => setLeague(e.target.value)} placeholder="联赛 (可选)" className="px-3 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary placeholder:text-ink-disabled w-36" />
          <input value={kickoff} onChange={e => setKickoff(e.target.value)} placeholder="开赛时间 (可选)" className="px-3 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary placeholder:text-ink-disabled w-44" />
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          <div className="text-[11px] text-ink-muted">1X2 当前赔率 (缺省自动回填)</div>
          <div className="text-[11px] text-ink-muted">大小球 (线/大/小)</div>
          <div className="text-[11px] text-ink-muted">让球 (线/主/客)</div>
          <div className="text-[11px] text-ink-muted">开盘价 主/平/客 (可选)</div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          <div className="flex gap-2">
            <input value={h} onChange={e => setH(e.target.value)} placeholder="主" className="px-2 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary w-full" />
            <input value={d} onChange={e => setD(e.target.value)} placeholder="平" className="px-2 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary w-full" />
            <input value={a} onChange={e => setA(e.target.value)} placeholder="客" className="px-2 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary w-full" />
          </div>
          <div className="flex gap-2">
            <input value={ouLine} onChange={e => setOuLine(e.target.value)} placeholder="线" className="px-2 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary w-full" />
            <input value={ouOver} onChange={e => setOuOver(e.target.value)} placeholder="大" className="px-2 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary w-full" />
            <input value={ouUnder} onChange={e => setOuUnder(e.target.value)} placeholder="小" className="px-2 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary w-full" />
          </div>
          <div className="flex gap-2">
            <input value={ahLine} onChange={e => setAhLine(e.target.value)} placeholder="线" className="px-2 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary w-full" />
            <input value={ahHome} onChange={e => setAhHome(e.target.value)} placeholder="主" className="px-2 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary w-full" />
            <input value={ahAway} onChange={e => setAhAway(e.target.value)} placeholder="客" className="px-2 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary w-full" />
          </div>
          <div className="flex gap-2">
            <input value={opH} onChange={e => setOpH(e.target.value)} placeholder="主" className="px-2 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary w-full" />
            <input value={opD} onChange={e => setOpD(e.target.value)} placeholder="平" className="px-2 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary w-full" />
            <input value={opA} onChange={e => setOpA(e.target.value)} placeholder="客" className="px-2 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary w-full" />
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button onClick={run} disabled={loading}
            className="px-4 py-1.5 rounded-md bg-field-500 hover:bg-field-600 disabled:opacity-50 text-white text-sm font-medium transition-colors">
            {loading ? '分析中…' : '开始分析'}
          </button>
          <button onClick={fillPreset} className="px-3 py-1.5 rounded-md bg-accent-inner border border-surface-border text-ink-secondary text-xs hover:text-ink-primary transition-colors">
            填入示例 (皇马 vs 巴萨)
          </button>
          {error && <span className="text-danger-400 text-xs">{error}</span>}
        </div>
      </div>

      {/* 结果区 */}
      {result?.error && (
        <div className="bg-danger-500/10 border border-danger-500/30 rounded-xl p-4 text-danger-400 text-sm">⚠ {result.error}</div>
      )}

      {result && !result.error && (
        <div className="space-y-3">
          {/* 头部 */}
          <div className="bg-accent-card border border-surface-border rounded-xl px-4 py-3 flex items-center justify-between flex-wrap gap-2">
            <div>
              <div className="text-[15px] font-semibold text-ink-primary">
                {m?.home || '—'} <span className="text-ink-muted font-normal">vs</span> {m?.away || '—'}
              </div>
              <div className="text-[11px] text-ink-muted mt-0.5">
                {m?.league || '无联赛'} {m?.kickoff ? `· ${m.kickoff}` : ''} · v{result.version} · {result.runtime_ms}ms
              </div>
            </div>
            <div className="text-[11px] text-ink-disabled">分析非预测, 不构成下注建议 (IR-20)</div>
          </div>

          {/* 市场锚 */}
          <div className="bg-accent-card border border-surface-border rounded-xl p-4">
            <div className="text-[13px] font-semibold text-ink-primary mb-2">市场锚 (去水隐含概率)</div>
            {mk?.['1x2_devig'] ? (
              <div className="space-y-1.5">
                {[['主胜', mk['1x2_devig'][0], 'bg-field-500'], ['平局', mk['1x2_devig'][1], 'bg-frost-400'], ['客胜', mk['1x2_devig'][2], 'bg-ember-400']].map(([label, p, c]) => (
                  <div key={label as string} className="flex items-center gap-2 text-xs">
                    <span className="text-ink-muted w-8 flex-shrink-0">{label as string}</span>
                    <ProbBar p={p as number} color={c as string} />
                    <span className="text-ink-primary w-14 text-right">{pct(p as number)}</span>
                  </div>
                ))}
                <div className="text-[11px] text-ink-muted pt-1">抽水 {mk?.overround != null ? (mk.overround * 100).toFixed(1) + '%' : '—'}{mk?.overround != null && mk.overround > 0.15 ? ' ⚠ 过高, 价值层打折 (IR-18)' : ''}</div>
              </div>
            ) : <div className="text-xs text-ink-muted">无 1X2 赔率 → 市场锚不可用</div>}
            {mk?.ou_line != null && mk?.ou_over_devig != null && (
              <div className="text-xs text-ink-muted mt-2">OU: 线 {mk.ou_line} · 大 {pct(mk.ou_over_devig)} / 小 {pct(1 - mk.ou_over_devig)}</div>
            )}
            {mk?.ah_line != null && mk?.ah_home_devig != null && (
              <div className="text-xs text-ink-muted mt-1">AH: 线 {mk.ah_line} · 主 {pct(mk.ah_home_devig)} / 客 {pct(1 - mk.ah_home_devig)}</div>
            )}
          </div>

          {/* 模型矩阵 + 一致性 */}
          <div className="grid md:grid-cols-2 gap-3">
            <div className="bg-accent-card border border-surface-border rounded-xl p-4">
              <div className="text-[13px] font-semibold text-ink-primary mb-2">模型矩阵</div>
              <div className="space-y-2 text-xs">
                {md?.independent_1x2 ? (
                  <div>
                    <div className="text-ink-muted mb-1">independent_1x2 (最强单模型)</div>
                    <div className="flex items-center gap-1.5">
                      {md.independent_1x2.map((p, i) => <ProbBar key={i} p={p} color={['bg-field-500', 'bg-frost-400', 'bg-ember-400'][i]} />)}
                      <span className="text-ink-primary w-24 text-right flex-shrink-0">{md.independent_1x2.map(v => pct(v)).join(' / ')}</span>
                    </div>
                  </div>
                ) : <div className="text-ink-muted">independent_1x2: 无该对阵特征, 跳过</div>}
                {md?.fl_1x2 && (
                  <div>
                    <div className="text-ink-muted mb-1">fl_1x2</div>
                    <div className="flex items-center gap-1.5">
                      {md.fl_1x2.map((p, i) => <ProbBar key={i} p={p} color={['bg-field-500', 'bg-frost-400', 'bg-ember-400'][i]} />)}
                      <span className="text-ink-primary w-24 text-right flex-shrink-0">{md.fl_1x2.map(v => pct(v)).join(' / ')}</span>
                    </div>
                  </div>
                )}
                {md?.fused_1x2 && (
                  <div>
                    <div className="text-ink-muted mb-1">fused_1x2</div>
                    <div className="flex items-center gap-1.5">
                      {md.fused_1x2.map((p, i) => <ProbBar key={i} p={p} color={['bg-field-500', 'bg-frost-400', 'bg-ember-400'][i]} />)}
                      <span className="text-ink-primary w-24 text-right flex-shrink-0">{md.fused_1x2.map(v => pct(v)).join(' / ')}</span>
                    </div>
                  </div>
                )}
                {md?.fl_ah && <div className="text-ink-muted">fl_ah (让球): 主 {pct(md.fl_ah[0])} / 客 {pct(md.fl_ah[1])}</div>}
                {md?.poisson_over != null && <div className="text-ink-muted">poisson OU P(大): {pct(md.poisson_over)}</div>}
                {md?.cs_odds && (
                  <div className={`rounded-md px-2 py-1.5 border ${md.cs_odds.qualified ? 'border-surface-border bg-surface-dark/40' : 'border-amber-500/30 bg-amber-500/[0.06]'}`}>
                    <div className="text-ink-muted">cs_odds (波胆 26 类)
                      {md.cs_odds.qualified
                        ? <span className="text-field-400 ml-1.5">✓ 已达标</span>
                        : <span className="text-amber-300 ml-1.5">⚠ 未达标</span>}
                    </div>
                    {md.cs_odds.top5 ? (
                      <div className="text-ink-primary mt-0.5">
                        top5: {md.cs_odds.top5.map(([s, p]) => `${s}(${pct(p)})`).join(' / ')}
                      </div>
                    ) : (
                      <div className="text-amber-300/90 mt-0.5">{md.cs_odds.reason}</div>
                    )}
                    {md.cs_odds.three_way && (
                      <div className="text-[10px] text-ink-muted mt-0.5">
                        三方向: 主 {pct(md.cs_odds.three_way[0])} / 平 {pct(md.cs_odds.three_way[1])} / 客 {pct(md.cs_odds.three_way[2])} · 不参与模型平均
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>

            <div className="bg-accent-card border border-surface-border rounded-xl p-4">
              <div className="text-[13px] font-semibold text-ink-primary mb-2">一致性 + Edge 三件套</div>
              {cs?.model_avg_1x2 ? (
                <>
                  <div className="text-xs text-ink-muted mb-1">
                    模型平均 (n={cs.n_models}): 主 {pct(cs.model_avg_1x2[0])} / 平 {pct(cs.model_avg_1x2[1])} / 客 {pct(cs.model_avg_1x2[2])} → 倾向 <span className="text-field-400 font-medium">{cs.lean}</span>
                  </div>
                  {cs.vs_market_pp && (
                    <div className="text-xs text-ink-muted mb-2">
                      模型−市场: 主 {pp(cs.vs_market_pp[0])} / 平 {pp(cs.vs_market_pp[1])} / 客 {pp(cs.vs_market_pp[2])}
                    </div>
                  )}
                </>
              ) : <div className="text-xs text-ink-muted mb-2">模型平均不可用</div>}
              {edge && edge.edge_pp != null ? (
                <div className="space-y-1 text-xs">
                  <div className="flex items-center gap-2">
                    <span className="text-ink-muted">方向</span>
                    <span className="text-ink-primary font-medium">{sideLabel(edge.side || '')}</span>
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${edge.edge_pp > 0 ? 'bg-field-500/15 text-field-400' : 'bg-danger-500/15 text-danger-400'}`}>
                      {edge.edge_pp > 0 ? '正EV方向' : '负EV'}
                    </span>
                  </div>
                  <div className="grid grid-cols-3 gap-2 text-center">
                    <div className="bg-accent-inner rounded-md py-1.5"><div className="text-[10px] text-ink-muted">win_rate</div><div className="text-ink-primary">{pct(edge.win_rate, 2)}</div></div>
                    <div className="bg-accent-inner rounded-md py-1.5"><div className="text-[10px] text-ink-muted">implied</div><div className="text-ink-primary">{pct(edge.implied, 2)}</div></div>
                    <div className="bg-accent-inner rounded-md py-1.5"><div className="text-[10px] text-ink-muted">edge</div><div className={edge.edge_pp > 0 ? 'text-field-400' : 'text-danger-400'}>{pp(edge.edge_pp)}</div></div>
                  </div>
                  <div className="text-[10px] text-ink-disabled pt-0.5">+EV 判据: 胜率 &gt; 隐含 + 抽水 (IR-17)</div>
                </div>
              ) : <div className="text-xs text-ink-muted">Edge 三件套不可用 (模型/赔率缺失)</div>}
            </div>
          </div>

          {/* OU + 漂移 */}
          <div className="grid md:grid-cols-2 gap-3">
            {ou && (
              <div className="bg-accent-card border border-surface-border rounded-xl p-4">
                <div className="text-[13px] font-semibold text-ink-primary mb-2">大小球判定</div>
                <div className="text-xs space-y-1">
                  <div className="text-ink-muted">模型 P(大) <span className="text-ink-primary">{pct(ou.model_p_over)}</span> vs 市场 <span className="text-ink-primary">{pct(ou.market_p_over)}</span> → <span className={`font-medium ${ou.lean === '大' ? 'text-field-400' : ou.lean === '小' ? 'text-danger-400' : 'text-ember-400'}`}>{ou.lean}</span> (edge {pp(ou.edge_pp)})</div>
                  <div className="text-[10px] text-ink-disabled">OU 全局无 edge 是长期真相; 此处仅为模型/市场分歧展示 (IR-03)</div>
                </div>
              </div>
            )}
            {dr && (
              <div className="bg-accent-card border border-surface-border rounded-xl p-4">
                <div className="text-[13px] font-semibold text-ink-primary mb-2">漂移 (开盘→当前)</div>
                <div className="text-xs space-y-1">
                  <div className="flex gap-3 text-ink-muted">
                    <span>主 {pp(dr.drift_pp_home)}</span>
                    <span>平 {pp(dr.drift_pp_draw)}</span>
                    <span>客 {pp(dr.drift_pp_away)}</span>
                  </div>
                  <div className="text-[10px] text-ink-disabled">三段框架初盘锚 · 读漂移方向判断庄家护盘/诱盘线索</div>
                </div>
              </div>
            )}
          </div>

          {/* 联赛背景 + 诚实边界 */}
          {lg && (
            <div className="bg-accent-card border border-surface-border rounded-xl p-4">
              <div className="text-[13px] font-semibold text-ink-primary mb-2">联赛背景 [{lg.league}]</div>
              <div className="text-xs text-ink-muted space-x-3">
                <span>样本 n={lg.n}</span>
                <span>主胜 {pct(lg.home_rate)}</span>
                <span>平 {pct(lg.draw_rate)}</span>
                <span>客胜 {pct(lg.away_rate)}</span>
                <span>场均 {lg.avg_goals?.toFixed(2)} 球</span>
              </div>
            </div>
          )}

          {result.honest_flags && result.honest_flags.length > 0 && (
            <div className="bg-ember-500/10 border border-ember-500/25 rounded-xl p-4">
              <div className="text-[13px] font-semibold text-ember-400 mb-1.5">诚实边界</div>
              <ul className="space-y-1">
                {result.honest_flags.map((f, i) => <li key={i} className="text-xs text-ember-300">⚠ {f}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

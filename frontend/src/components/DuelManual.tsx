import { useEffect, useRef, useState } from 'react'
import { liveGoalProbeService } from '@/services/api'

/* ═══ 手动预测对比 (移植自 harness 5173 → 9000 生产前端, 2026-08-28) ═══
 * 手动输入即时盘 1X2 (+ 可选初盘/比分/分钟) → 单场四方对比:
 * 本系统(live/static) / GitHub heuristic / 去水隐含基线 / 优化混合(w=0.6)
 * 下方附历史指标看板 (AUC/LogLoss/Brier/Acc, 来源 unified_corrected_duel_result.json)
 */

function NumberInput({ label, value, onChange, placeholder }: { label: string; value: string; onChange: (v: string) => void; placeholder?: string }) {
  return (
    <label className="flex flex-col gap-0.5">
      <span className="text-[10px] text-ink-muted">{label}</span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full text-[12px] px-2 py-1.5 rounded bg-surface-dark/80 border border-surface-border/40 text-ink-primary placeholder:text-ink-muted/40 focus:outline-none focus:border-frost-500/50"
      />
    </label>
  )
}

/* ── 四方对比 (harness 主题: 三色竖条 主绿/平橙/客蓝) ── */
const HARNESS_COLORS = { H: '#39d98a', D: '#ffb454', A: '#5aa0ff' }

function Bars({ p }: { p: any }) {
  if (!p) return <div className="text-[10px] text-ink-muted">预测失败 / 缺特征</div>
  const data = [
    { k: 'H', v: p.p_home, t: '主' },
    { k: 'D', v: p.p_draw, t: '平' },
    { k: 'A', v: p.p_away, t: '客' },
  ]
  const mx = Math.max(...data.map((d) => d.v))
  return (
    <div className="flex items-end gap-3 h-[92px]">
      {data.map((d) => (
        <div key={d.k} className="flex flex-col items-center flex-1 gap-0.5">
          <div className="text-[11px] font-mono font-bold" style={{ color: HARNESS_COLORS[d.k as keyof typeof HARNESS_COLORS] }}>
            {(d.v * 100).toFixed(1)}%
          </div>
          <div
            className="w-full rounded-t transition-all"
            style={{ height: `${(d.v / mx) * 56}px`, background: HARNESS_COLORS[d.k as keyof typeof HARNESS_COLORS] }}
          />
          <div className="text-[10px] text-ink-muted">{d.t}</div>
        </div>
      ))}
    </div>
  )
}

function CompareRows({ result }: { result: any }) {
  const items: [string, any, boolean][] = [
    [`本系统 · ${result.mode ?? ''}`, result.system, false],
    ['GitHub heuristic', result.github, false],
    ['去水隐含基线', result.baseline, false],
    ['优化混合 (推荐)', result.optimized, true],
  ]

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      {items.map(([name, p, hot]) => (
        <div key={name} className={`rounded-xl border p-3 ${hot ? 'border-field-500/60 bg-field-500/[0.10] shadow-[0_0_16px_rgba(90,160,255,0.12)]' : 'border-surface-border/40 bg-surface-dark/40'}`}>
          <div className="text-[11px] font-semibold text-ink-primary mb-2 flex items-center justify-between">
            <span>{name}</span>
            {hot && <span className="text-[10px] text-field-300">⭐</span>}
          </div>
          <Bars p={p} />
          {p?.argmax_cn && (
            <div className="mt-1.5 inline-flex px-2 py-0.5 rounded text-[10px] font-semibold"
              style={{
                color: HARNESS_COLORS[p.argmax as keyof typeof HARNESS_COLORS] ?? '#9aa7b4',
                border: `1px solid ${HARNESS_COLORS[p.argmax as keyof typeof HARNESS_COLORS] ?? '#444'}40`,
                background: `${HARNESS_COLORS[p.argmax as keyof typeof HARNESS_COLORS] ?? '#444'}18`,
              }}
            >
              {p.argmax_cn}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

/* ── 指标看板 (harness 主题: win 列绿字) ── */
function MetricsTable({ title, m }: { title: string; m: any }) {
  if (!m) return null
  const rows: [string, any, boolean][] = [
    ['本系统', m.system, true],
    ['GitHub', m.github, false],
    ['去水基线', m.naive, false],
  ]
  return (
    <div className="overflow-x-auto">
      <div className="text-[11px] font-semibold text-ink-primary mb-1">{title} (n={m.n ?? '—'})</div>
      <table className="w-full text-[11px] font-mono border-collapse">
        <thead>
          <tr className="text-ink-muted border-b border-white/[0.08]">
            <th className="text-left py-1.5 pr-2 font-medium">模型</th>
            <th className="text-right py-1.5 px-2 font-medium">AUC</th>
            <th className="text-right py-1.5 px-2 font-medium">LogLoss</th>
            <th className="text-right py-1.5 px-2 font-medium">Brier</th>
            <th className="text-right py-1.5 px-2 font-medium">Acc</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, mm, win]) => (
            <tr key={name} className="border-b border-white/[0.04] hover:bg-white/[0.02]">
              <td className={`py-1.5 pr-2 ${win ? 'text-field-300 font-semibold' : 'text-ink-secondary'}`}>{name}</td>
              <td className={`text-right py-1.5 px-2 font-bold ${win ? 'text-emerald-300' : 'text-ink-secondary'}`}>{mm?.auc?.toFixed(4) ?? '—'}</td>
              <td className="text-right py-1.5 px-2 text-ink-secondary">{mm?.logloss?.toFixed(3) ?? '—'}</td>
              <td className="text-right py-1.5 px-2 text-ink-secondary">{mm?.brier?.toFixed(3) ?? '—'}</td>
              <td className="text-right py-1.5 px-2 text-ink-secondary">{mm?.acc?.toFixed(3) ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* 手动预测对比入口初值 (来自赛程列表点击, 对齐 NFTB 入参) */
export interface DuelInitial {
  home?: number; draw?: number; away?: number
  openHome?: number; openDraw?: number; openAway?: number
  score?: string; minute?: number
  homeName?: string
  awayName?: string
  league?: string
  ouLine?: number; ouOver?: number; ouUnder?: number
  ouOpLine?: number; ouOpOver?: number; ouOpUnder?: number
  csTop?: [string, number][] | null
}

export default function DuelManual({ initial }: { initial?: DuelInitial | null }) {
  const [home, setHome] = useState('')
  const [draw, setDraw] = useState('')
  const [away, setAway] = useState('')
  const [openHome, setOpenHome] = useState('')
  const [openDraw, setOpenDraw] = useState('')
  const [openAway, setOpenAway] = useState('')
  const [score, setScore] = useState('')
  const [minute, setMinute] = useState('')
  const [homeName, setHomeName] = useState('')
  const [awayName, setAwayName] = useState('')
  const [league, setLeague] = useState('')
  const [ouLine, setOuLine] = useState('')
  const [ouOver, setOuOver] = useState('')
  const [ouUnder, setOuUnder] = useState('')
  const [csTop, setCsTop] = useState('')
  const [result, setResult] = useState<any>(null)
  const [metrics, setMetrics] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const predictedRef = useRef(false)
  const predictRef = useRef<() => void>(() => {})

  useEffect(() => {
    liveGoalProbeService.getDuelMetrics().then((r) => setMetrics((r as any)?.data?.data ?? (r as any)?.data)).catch(() => {})
  }, [])

  // 赛程列表点击 → 自动回填 + 自动预测
  useEffect(() => {
    if (!initial) return
    setHome(initial.home != null ? String(initial.home) : '')
    setDraw(initial.draw != null ? String(initial.draw) : '')
    setAway(initial.away != null ? String(initial.away) : '')
    setOpenHome(initial.openHome != null ? String(initial.openHome) : '')
    setOpenDraw(initial.openDraw != null ? String(initial.openDraw) : '')
    setOpenAway(initial.openAway != null ? String(initial.openAway) : '')
    setScore(initial.score ?? '')
    setMinute(initial.minute != null ? String(initial.minute) : '')
    setHomeName(initial.homeName ?? '')
    setAwayName(initial.awayName ?? '')
    setLeague(initial.league ?? '')
    setOuLine(initial.ouLine != null ? String(initial.ouLine) : '')
    setOuOver(initial.ouOver != null ? String(initial.ouOver) : '')
    setOuUnder(initial.ouUnder != null ? String(initial.ouUnder) : '')
    setCsTop(initial.csTop ? JSON.stringify(initial.csTop) : '')
    predictedRef.current = false
    // 回填后等 state 生效再自动预测
    const t = setTimeout(() => {
      if (!predictedRef.current) {
        predictedRef.current = true
        predictRef.current()
      }
    }, 50)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initial])

  // predict 最新引用 (避免 useEffect 闭包捕获旧 state)
  useEffect(() => {
    predictRef.current = predict
  })

  const predict = () => {
    if (!home || !draw || !away) {
      setErr('请填写即时盘 1X2 赔率（主/平/客）')
      return
    }
    setLoading(true)
    setErr(null)
    const body: any = { home: Number(home), draw: Number(draw), away: Number(away) }
    if (openHome || openDraw || openAway) {
      body.open_home = Number(openHome); body.open_draw = Number(openDraw); body.open_away = Number(openAway)
    }
    if (score) body.score = score
    if (minute) body.minute = Number(minute)
    if (ouLine) body.ou_line = Number(ouLine)
    if (ouOver) body.ou_over = Number(ouOver)
    if (ouUnder) body.ou_under = Number(ouUnder)
    if (csTop.trim()) {
      try { body.cs = JSON.parse(csTop) } catch { /* 非 JSON 忽略 */ }
    }
    if (homeName) body.home_name = homeName
    if (awayName) body.away_name = awayName
    if (league) body.league = league
    liveGoalProbeService.getDuelPredict(body)
      .then((r) => setResult((r as any)?.data?.data ?? (r as any)?.data))
      .catch((e) => setErr(String(e?.message ?? e)))
      .finally(() => setLoading(false))
  }

  return (
    <div className="space-y-4">
      {/* 输入表单 */}
      <div className="rounded-xl border border-surface-border/40 bg-surface-dark/30 p-4">
        <div className="text-[12px] font-semibold text-ink-primary mb-3">手动预测对比 · 输入盘口</div>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-2.5">
          <NumberInput label="主队 (自动回填)" value={homeName} onChange={setHomeName} placeholder="阿根廷" />
          <NumberInput label="客队 (自动回填)" value={awayName} onChange={setAwayName} placeholder="法国" />
          <NumberInput label="联赛 (自动回填)" value={league} onChange={setLeague} placeholder="WC2026" />
          <NumberInput label="滚球比分 (可选, 如 1-0)" value={score} onChange={setScore} placeholder="1-0" />
          <NumberInput label="即时盘 主 (H)" value={home} onChange={setHome} placeholder="2.10" />
          <NumberInput label="即时盘 平 (D)" value={draw} onChange={setDraw} placeholder="3.30" />
          <NumberInput label="即时盘 客 (A)" value={away} onChange={setAway} placeholder="3.50" />
          <NumberInput label="初盘 主 (可选)" value={openHome} onChange={setOpenHome} placeholder="1.90" />
          <NumberInput label="初盘 平 (可选)" value={openDraw} onChange={setOpenDraw} placeholder="3.40" />
          <NumberInput label="初盘 客 (可选)" value={openAway} onChange={setOpenAway} placeholder="4.20" />
          <NumberInput label="分钟 (可选, 填了切 in-play)" value={minute} onChange={setMinute} placeholder="67" />
          <NumberInput label="大小球线 (自动回填)" value={ouLine} onChange={setOuLine} placeholder="2.5" />
          <NumberInput label="大球赔率 (自动回填)" value={ouOver} onChange={setOuOver} placeholder="1.95" />
          <NumberInput label="小球赔率 (自动回填)" value={ouUnder} onChange={setOuUnder} placeholder="1.90" />
          <NumberInput label="波胆 CS (自动回填 JSON)" value={csTop} onChange={setCsTop} placeholder='[["1-1",8.3],["2-1",11]]' />
        </div>
        <div className="flex items-center gap-3 mt-3">
          <button className="btn-primary" onClick={predict} disabled={loading}>
            {loading ? '预测中…' : '开始对比预测'}
          </button>
          {err && <span className="text-[11px] text-danger-400">{err}</span>}
        </div>
        <div className="text-[10px] text-ink-muted mt-2">
          即时盘必填；初盘填了→静态模式(wi_1x2)；比分+分钟填了→滚球模式(live_1x2 + 0.6 混合)
        </div>
      </div>

      {/* 四方对比 */}
      {result && !result.error && (
        <div className="rounded-xl border border-field-500/40 bg-field-500/[0.05] p-4">
          <div className="text-[12px] font-semibold text-field-300 mb-3">单场四方对比 · {result.mode}</div>
          <CompareRows result={result} />
          <div className="text-[10px] text-ink-muted/70 mt-2">
            优化混合 = 0.6·滚球模型 + 0.4·去水基线（时序 holdout 锁定，滚球 AUC 0.8217 最优）· 分析参考，非投注建议
          </div>
        </div>
      )}
      {result?.error && <div className="text-[11px] text-danger-400">{result.error}</div>}

      {/* 三市场分析 (OU 大小球 + CS 合理比分, 对齐 NFTB) */}
      {(result?.ou_analysis || result?.cs_analysis) && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {result.ou_analysis && (
            <div className="rounded-xl border border-pitch-500/30 bg-pitch-500/[0.05] p-4">
              <div className="text-[12px] font-semibold text-pitch-300 mb-1.5">大小球分析 (OU)</div>
              <div className="flex items-baseline gap-2">
                <span className="text-lg font-bold text-ink-primary">
                  {result.ou_analysis.direction === 'OVER' ? '大球' : '小球'}
                </span>
                <span className="text-[11px] text-ink-muted">线 {result.ou_analysis.line}</span>
              </div>
              <div className="mt-1.5 h-2 bg-white/[0.06] rounded-full overflow-hidden flex">
                <div className="h-full bg-emerald-400/70" style={{ width: `${(result.ou_analysis.p_over * 100).toFixed(1)}%` }} title="大球概率" />
              </div>
              <div className="text-[10px] text-ink-muted mt-1">
                大球隐含 {(result.ou_analysis.p_over * 100).toFixed(1)}% · {result.ou_analysis.basis}
              </div>
            </div>
          )}
          {result.cs_analysis && (
            <div className="rounded-xl border border-ember-500/30 bg-ember-500/[0.05] p-4">
              <div className="text-[12px] font-semibold text-ember-300 mb-1.5">合理比分 (CS 诚实锚)</div>
              <div className="flex items-baseline gap-2">
                <span className="text-[26px] font-bold font-mono text-ember-400">{result.cs_analysis.score}</span>
                <span className="text-[11px] text-ink-secondary">{result.cs_analysis.winner_label}</span>
              </div>
              <div className="text-[10px] text-ink-muted mt-1">{result.cs_analysis.basis}</div>
            </div>
          )}
        </div>
      )}

      {/* NFTB 契约 (ranked_predictor 全链路: result/confidence/analysis 七段/operator/team_strength) */}
      {result?.nftb && !result.nftb.error && (
        <div className="rounded-xl border border-field-500/40 bg-field-500/[0.05] p-4">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[12px] font-semibold text-field-300">全链路研判 (ranked_predictor · 与 NFTB /predict 同契约)</span>
            <span className="text-[10px] px-2 py-0.5 rounded border border-field-500/40 bg-field-500/15 text-field-300">
              {result.nftb.source ?? 'shaoxiang'} · {result.nftb.confidence_tier ?? '—'} 把握
            </span>
          </div>
          {/* result + confidence + probabilities */}
          <div className="flex items-center gap-4 flex-wrap">
            <div className="text-center px-4 py-2 rounded-lg bg-surface-dark/60 border border-surface-border/40">
              <div className="text-[10px] text-ink-muted">方向</div>
              <div className={`text-xl font-bold ${result.nftb.result === 'H' ? 'text-emerald-300' : result.nftb.result === 'A' ? 'text-sky-300' : 'text-amber-300'}`}>
                {result.nftb.result === 'H' ? '主胜' : result.nftb.result === 'A' ? '客胜' : '平局'}
              </div>
            </div>
            <div className="text-center px-4 py-2 rounded-lg bg-surface-dark/60 border border-surface-border/40">
              <div className="text-[10px] text-ink-muted">把握度</div>
              <div className="text-xl font-bold font-mono text-ink-primary">{((result.nftb.confidence ?? 0) * 100).toFixed(1)}%</div>
            </div>
            <div className="flex-1 min-w-[160px]">
              <div className="text-[10px] text-ink-muted mb-1">1X2 概率</div>
              <div className="grid grid-cols-3 gap-1">
                {(['H', 'D', 'A'] as const).map((k, i) => {
                  const v = result.nftb.probabilities?.[k]
                  const pct = v != null ? Math.round(v * 100) : 0
                  const isMax = result.nftb.result === k
                  return (
                    <div key={k} className="relative h-5 rounded bg-white/[0.04] overflow-hidden">
                      <div className={`absolute inset-y-0 left-0 ${isMax ? 'bg-field-400/70' : 'bg-white/[0.08]'}`} style={{ width: `${pct}%` }} />
                      <span className="absolute inset-0 flex items-center justify-center text-[9px] font-mono">
                        {['主', '平', '客'][i]} {pct}%
                      </span>
                    </div>
                  )
                })}
              </div>
            </div>
            {result.nftb.team_strength && (
              <div className="text-center px-4 py-2 rounded-lg bg-surface-dark/60 border border-surface-border/40">
                <div className="text-[10px] text-ink-muted">球队评分</div>
                <div className="text-[11px] font-mono text-ink-primary">
                  主 {result.nftb.team_strength.home_pts ?? '—'} / 客 {result.nftb.team_strength.away_pts ?? '—'}
                </div>
                <div className="text-[10px] text-ink-muted">分差 {(result.nftb.team_strength.pts_diff ?? 0).toFixed(1)} · 信号 {result.nftb.team_strength.signal ?? '—'}</div>
              </div>
            )}
          </div>
          {/* operator_verdict 一行结论 */}
          {result.nftb.operator_verdict && (
            <div className="mt-3 text-[12px] text-amber-300 bg-amber-500/[0.06] border border-amber-500/20 rounded-lg px-3 py-2">
              {result.nftb.operator_verdict}
            </div>
          )}
          {/* 七段 analysis 折叠 */}
          {result.nftb.analysis && (
            <details className="mt-2">
              <summary className="text-[11px] text-ink-muted cursor-pointer select-none">展开七段研判（1X2 / OU / CS / 操盘手 / 风险 / 排名）</summary>
              <div className="mt-2 space-y-1.5">
                {(['verdict', '1x2', 'ou', 'cs', 'operator', 'risk', 'ranking'] as const).map((k) => (
                  result.nftb.analysis[k] ? (
                    <div key={k} className="text-[11px] text-ink-secondary">
                      <span className="text-ink-muted font-semibold mr-1.5">
                        {k === 'verdict' ? '总览' : k === '1x2' ? '1X2' : k === 'ou' ? '大小球' : k === 'cs' ? '波胆' : k === 'operator' ? '操盘手' : k === 'risk' ? '风险' : '排名'}
                      </span>
                      {result.nftb.analysis[k]}
                    </div>
                  ) : null
                ))}
              </div>
            </details>
          )}
        </div>
      )}
      {result?.nftb?.error && <div className="text-[11px] text-danger-400">{result.nftb.error}</div>}

      {/* 指标看板 */}
      {metrics?.ready ? (
        <div className="rounded-xl border border-surface-border/40 bg-surface-dark/30 p-4 space-y-4">
          <div className="text-[12px] font-semibold text-ink-primary">历史对决指标（时序 OOS）</div>
          <MetricsTable title="静态段 (football_data OOS)" m={metrics.static} />
          <MetricsTable title="滚球段 (events 时序窗口)" m={metrics.inplay} />
        </div>
      ) : (
        <div className="text-[10px] text-ink-muted">{metrics?.note ?? '指标加载中…'}</div>
      )}
    </div>
  )
}
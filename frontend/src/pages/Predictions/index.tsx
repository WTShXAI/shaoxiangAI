// ═══ 预测中心页 (2026-09-18 系统改造: 博彩量化 → 预测系统) ═══
// 数据: /api/predictions (逐场概率输出) + /api/predictions/calibration (系统校准指标)。
// 设计铁律: 只解释, 不喊单 — 展示概率/期望进球/总进球分布/与市场偏差, 无任何注码/凯利/下注语义。
import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import PageHeader from '@/components/layout/PageHeader'
import ApiError from '@/components/shared/ApiError'
import Skeleton from '@/components/shared/Skeleton'
import EmptyState from '@/components/shared/EmptyState'
import { predictionsService, type PredictionEntry, type PredictionCalibrationSection, type ReplayMatch, type ReplayPayload } from '@/services/api'

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

function inShanghai(d: Date): Date {
  return new Date(d.toLocaleString('en-US', { timeZone: 'Asia/Shanghai' }))
}
function shanghaiToday(): string {
  return inShanghai(new Date()).toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' })
}
function fmtKickoff(ko: string): string {
  if (!ko) return '--:--'
  // kickoff 为上海本地时间字面量, 与 isDone 同口径补 +08:00 (跨时区浏览器显示一致)
  const d = new Date(ko.replace(' ', 'T') + '+08:00')
  if (isNaN(d.getTime())) return ko
  return inShanghai(d).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}
function dateTabs(now: number): { key: string; label: string }[] {
  const today = inShanghai(new Date(now))
  today.setHours(0, 0, 0, 0)
  const out: { key: string; label: string }[] = []
  for (let i = -2; i <= 2; i++) {
    const d = new Date(today)
    d.setDate(today.getDate() + i)
    const key = d.toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' })
    const label = i === 0 ? '今天' : `${d.getMonth() + 1}.${d.getDate()} ${WEEKDAYS[d.getDay()]}`
    out.push({ key, label })
  }
  return out
}

function pct(v: number | null | undefined, digits = 0): string {
  return v == null ? '—' : `${(v * 100).toFixed(digits)}%`
}

/** 按联赛分组 (保持传入顺序 — 排序已由调用方完成) */
function groupByLeague(items: PredictionEntry[]): [string, PredictionEntry[]][] {
  const m = new Map<string, PredictionEntry[]>()
  for (const p of items) {
    const k = p.league || '其他'
    if (!m.has(k)) m.set(k, [])
    m.get(k)!.push(p)
  }
  return [...m.entries()]
}

/** 联赛分组卡片列表 */
function LeagueGroups({ groups, nowMs, verdicts }: { groups: [string, PredictionEntry[]][]; nowMs: number; verdicts?: Map<string, ReplayMatch> }) {
  return (
    <div className="space-y-5">
      {groups.map(([league, items]) => (
        <div key={league}>
          <div className="text-xs font-semibold text-ink-disabled mb-2 flex items-center gap-2">
            {league}
            <span className="text-ink-disabled/60 font-normal">{items.length} 场</span>
            <span className="flex-1 h-px bg-surface-border" />
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            {items.map((p) => (
              <PredictionCard key={p.match_key} p={p} done={isDone(p, nowMs)} verdict={verdicts?.get(p.match_key)} />
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

/**
 * 已结束判定: status=finished, 或开赛超3.5小时 (足球最长~3小时)。
 * 兜底采集器状态滞后 — 预测行开赛即冻结, 赛果未回补的场 status 会一直停在 live/scheduled。
 * kickoff 为上海本地时间字面量, 补 +08:00 转绝对时刻对比。
 */
function isDone(p: PredictionEntry, nowMs: number): boolean {
  if (p.status === 'finished') return true
  const ko = new Date(p.kickoff.replace(' ', 'T') + '+08:00')
  if (isNaN(ko.getTime())) return false
  return nowMs - ko.getTime() > 3.5 * 3600 * 1000
}

const OUTCOME_LABEL: Record<'home' | 'draw' | 'away', string> = { home: '主胜', draw: '平局', away: '客胜' }

const SOURCE_LABEL: Record<PredictionEntry['model_source'], string> = {
  candles_ensemble: 'K线集成',
  market_baseline: '市场基准',
}

const CONF_LABEL: Record<PredictionEntry['model_confidence'], string> = {
  high: '置信 高',
  medium: '置信 中',
  low: '置信 低',
  baseline: '基准',
}
const CONF_STYLE: Record<PredictionEntry['model_confidence'], string> = {
  high: 'bg-field-500/12 text-field-400 border-field-500/25',
  medium: 'bg-ember-500/12 text-ember-400 border-ember-500/25',
  low: 'bg-white/[0.04] text-ink-muted border-surface-border',
  baseline: 'bg-white/[0.04] text-ink-disabled border-surface-border',
}

/** 三段式 1X2 概率条 (模型概率) */
function ProbBar({ p }: { p: PredictionEntry }) {
  const segs = [
    { v: p.p_home, cls: 'bg-field-500', label: '主' },
    { v: p.p_draw, cls: 'bg-ink-disabled', label: '平' },
    { v: p.p_away, cls: 'bg-frost-500', label: '客' },
  ]
  return (
    <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-white/[0.05]">
      {segs.map((s, i) => (
        <div key={i} className={s.cls} style={{ width: `${Math.max(0, s.v) * 100}%` }} />
      ))}
    </div>
  )
}

/** 总进球分布 vs 实际 (分布证据, 已结算场显示) */
function TotalGoalsDistribution({ p, actualTotal }: { p: PredictionEntry; actualTotal: number }) {
  const dist = p.total_goals_distribution || {}
  const buckets = ['0', '1', '2', '3', '4', '5', '6', '7+']
  const max = Math.max(...buckets.map((b) => dist[b] || 0), 0.01)
  return (
    <div className="mt-2.5">
      <div className="text-[10px] text-ink-disabled mb-1">
        总进球分布 · 实际 <span className="text-field-400 font-semibold">{actualTotal} 球</span>
      </div>
      <div className="flex items-end gap-1 h-8">
        {buckets.map((b) => {
          const v = dist[b] || 0
          const isActual = actualTotal >= 7 ? b === '7+' : b === String(actualTotal)
          return (
            <div key={b} className="flex-1 flex flex-col items-center justify-end gap-0.5 h-full" title={`${b} 球 ${pct(v, 1)}`}>
              <div
                className={`w-full rounded-sm ${isActual ? 'bg-field-500' : 'bg-white/[0.12]'}`}
                style={{ height: `${Math.max(6, (v / max) * 100)}%` }}
              />
              <span className={`text-[9px] tabular-nums ${isActual ? 'text-field-400 font-semibold' : 'text-ink-disabled'}`}>{b}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function parseTotal(score: string): number | null {
  const m = /^(\d+)-(\d+)$/.exec(score.trim())
  return m ? Number(m[1]) + Number(m[2]) : null
}

function PredictionCard({ p, done, verdict }: { p: PredictionEntry; done?: boolean; verdict?: ReplayMatch }) {
  const mi = p.market_implied || ({} as PredictionEntry['market_implied'])
  const dHome = mi.home != null ? (p.p_home - mi.home) * 100 : null
  const dAway = mi.away != null ? (p.p_away - mi.away) * 100 : null
  const dDraw = mi.draw != null ? (p.p_draw - mi.draw) * 100 : null
  const actualTotal = verdict?.score ? parseTotal(verdict.score) : null
  return (
    <div className="rounded-xl border border-surface-border bg-surface-dark/60 p-4 hover:border-field-500/30 transition-colors">
      {/* 头行: 时间 + 对阵 + 徽标 */}
      <div className="flex items-center gap-2 flex-wrap mb-3">
        <span className="text-xs text-ink-disabled tabular-nums">{fmtKickoff(p.kickoff)}</span>
        <span className="text-sm font-semibold text-ink-primary">
          {p.home} <span className="text-ink-disabled font-normal mx-1">vs</span> {p.away}
        </span>
        {done && !verdict?.actual && !verdict?.score_dubious && (
          <span className="text-[10px] px-1.5 py-0.5 rounded border border-danger-500/30 text-danger-400 font-semibold">已结束</span>
        )}
        {verdict?.score_dubious && (
          <span className="text-[10px] px-1.5 py-0.5 rounded border border-ember-500/30 text-ember-400 font-semibold" title="终盘赔率 tick 早于开赛+95分钟, 疑似断流假0-0 — 不计入任何校准指标">
            已结束 · 比分存疑{verdict.score ? ` (${verdict.score}?)` : ''}
          </span>
        )}
        {verdict?.actual && (
          <>
            <span className="text-[10px] px-1.5 py-0.5 rounded border border-surface-border text-ink-muted tabular-nums">
              终场 {verdict.score} · {OUTCOME_LABEL[verdict.actual]}
            </span>
            <span className={`text-[10px] px-1.5 py-0.5 rounded border font-semibold tabular-nums ${
              verdict.hit ? 'border-field-500/40 text-field-400' : 'border-danger-500/30 text-danger-400'}`}>
              {verdict.hit ? '命中 ✓' : '未中 ✗'} · LL {verdict.ll?.toFixed(2)}
            </span>
          </>
        )}
        {p.league && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-white/[0.05] text-ink-muted">{p.league}</span>
        )}
        <span className="text-[10px] px-1.5 py-0.5 rounded border border-surface-border text-ink-muted">
          {SOURCE_LABEL[p.model_source] || p.model_source}
        </span>
        <span className={`text-[10px] px-1.5 py-0.5 rounded border ${CONF_STYLE[p.model_confidence]}`}>
          {CONF_LABEL[p.model_confidence]}
        </span>
      </div>

      {/* 模型 1X2 概率 */}
      <div className="grid grid-cols-3 gap-2 mb-1.5 text-center">
        <div>
          <div className="text-[10px] text-ink-disabled">主胜</div>
          <div className="text-base font-bold text-field-400 tabular-nums">{pct(p.p_home, 1)}</div>
        </div>
        <div>
          <div className="text-[10px] text-ink-disabled">平局</div>
          <div className="text-base font-bold text-ink-primary tabular-nums">{pct(p.p_draw, 1)}</div>
        </div>
        <div>
          <div className="text-[10px] text-ink-disabled">客胜</div>
          <div className="text-base font-bold text-frost-400 tabular-nums">{pct(p.p_away, 1)}</div>
        </div>
      </div>
      <ProbBar p={p} />
      {verdict?.actual && actualTotal != null && <TotalGoalsDistribution p={p} actualTotal={actualTotal} />}

      {/* 派生市场 + 期望进球 */}
      <div className="grid grid-cols-4 gap-2 mt-3 text-center">
        <div className="rounded-lg bg-white/[0.03] py-1.5">
          <div className="text-[10px] text-ink-disabled">期望进球</div>
          <div className="text-xs font-semibold text-ink-primary tabular-nums">
            {p.expected_home_goals.toFixed(2)} - {p.expected_away_goals.toFixed(2)}
          </div>
        </div>
        <div className="rounded-lg bg-white/[0.03] py-1.5">
          <div className="text-[10px] text-ink-disabled">大于2.5球</div>
          <div className="text-xs font-semibold text-ink-primary tabular-nums">{pct(p.over_2_5)}</div>
        </div>
        <div className="rounded-lg bg-white/[0.03] py-1.5">
          <div className="text-[10px] text-ink-disabled">双方进球</div>
          <div className="text-xs font-semibold text-ink-primary tabular-nums">{pct(p.btts)}</div>
          {mi.btts_p != null && (
            <div className={`text-[9px] tabular-nums ${Math.abs((p.btts - mi.btts_p) * 100) >= 3 ? 'text-ember-400' : 'text-ink-disabled'}`}>
              市 {pct(mi.btts_p)}
            </div>
          )}
        </div>
        <div className="rounded-lg bg-white/[0.03] py-1.5">
          <div className="text-[10px] text-ink-disabled">最可能比分</div>
          <div className="text-xs font-semibold text-ink-primary tabular-nums">
            {(p.top_scorelines || []).slice(0, 2).map(([s, v]) => `${s} ${pct(v)}`).join(' · ') || '—'}
          </div>
        </div>
      </div>

      {/* 市场对照 + 偏差说明 (只解释, 不喊单) */}
      <div className="mt-3 rounded-lg border border-surface-border/60 px-3 py-2 text-[11px] leading-relaxed">
        <span className="text-ink-disabled">市场隐含: </span>
        <span className="text-ink-muted tabular-nums">
          {mi.home != null ? `${pct(mi.home, 1)} / ${pct(mi.draw, 1)} / ${pct(mi.away, 1)}` : '—'}
          {mi.ou_line != null && ` · OU${mi.ou_line} 大 ${pct(mi.p_over)}`}
        </span>
        <span className="text-ink-disabled mx-2">|</span>
        <span className="text-ink-muted">
          偏差: 主 {dHome == null ? '—' : `${dHome >= 0 ? '+' : ''}${dHome.toFixed(1)}pp`} ·
          平 {dDraw == null ? '—' : `${dDraw >= 0 ? '+' : ''}${dDraw.toFixed(1)}pp`} ·
          客 {dAway == null ? '—' : `${dAway >= 0 ? '+' : ''}${dAway.toFixed(1)}pp`}
        </span>
        {p.deviation_note && (
          <div className="text-ink-muted mt-1">📝 {p.deviation_note}</div>
        )}
      </div>
    </div>
  )
}

/** 系统校准条: 模型 vs 市场 (LogLoss/Brier 口径) */
function CalibrationStrip() {
  const { data, isLoading } = useQuery({
    queryKey: ['predictions-calibration'],
    queryFn: ({ signal }) => predictionsService.calibration(signal),
    staleTime: 10 * 60 * 1000,
  })
  const cal = data?.data?.data as Record<string, any> | undefined
  const candles = (cal?.candles_1x2 || undefined) as PredictionCalibrationSection | undefined
  const delta = cal?.candles_vs_market_same_set as any | undefined
  if (isLoading) return <Skeleton rows={1} variant="card" />
  if (!candles || candles.n == null) return null
  return (
    <div className="mb-4 rounded-xl border border-surface-border bg-surface-dark/60 px-4 py-3 flex flex-wrap items-center gap-x-6 gap-y-1 text-xs">
      <span className="text-ink-disabled font-medium">系统校准 (K线集成 · n={candles.n})</span>
      <span className="text-ink-muted">LogLoss <b className="text-ink-primary tabular-nums">{candles.log_loss?.toFixed(4)}</b> <span className="text-ink-disabled">(随机 ln3≈1.099)</span></span>
      <span className="text-ink-muted">Brier <b className="text-ink-primary tabular-nums">{candles.brier_multiclass?.toFixed(4)}</b></span>
      {candles.accuracy != null && <span className="text-ink-muted">TOP1 <b className="text-ink-primary tabular-nums">{pct(candles.accuracy, 1)}</b></span>}
      {delta && (
        <span className={delta.log_loss_delta < 0 ? 'text-field-400' : 'text-ember-400'}>
          vs 市场: LogLoss {delta.log_loss_delta >= 0 ? '+' : ''}{delta.log_loss_delta?.toFixed(4)}
          {' '}({delta.log_loss_delta < 0 ? '概率质量优于市场' : '市场基准更优'})
        </span>
      )}
    </div>
  )
}

/** 当日复盘条 (沙盘回放): 开赛冻结预测 vs 实际赛果的当日诊断 */
function ReplayStrip({ replay }: { replay?: ReplayPayload }) {
  if (!replay || replay.n_settled === 0) return null
  const o = replay.overall
  const hits = o.accuracy != null ? Math.round(o.accuracy * o.n) : null
  return (
    <div className="mb-4 rounded-xl border border-field-500/20 bg-surface-dark/60 px-4 py-3 flex flex-wrap items-center gap-x-6 gap-y-1 text-xs">
      <span className="text-ink-disabled font-medium">当日复盘 <span className="text-ink-disabled/60 font-normal">(开赛冻结口径)</span></span>
      <span className="text-ink-muted">
        已结算 <b className="text-ink-primary tabular-nums">{replay.n_settled}</b>/{replay.n_total} 场
        {replay.n_unsettled > 0 && <span className="text-ink-disabled"> · {replay.n_unsettled} 场未完赛不计入</span>}
      </span>
      {o.log_loss != null && (
        <span className="text-ink-muted">LogLoss <b className="text-ink-primary tabular-nums">{o.log_loss.toFixed(4)}</b></span>
      )}
      {o.brier != null && (
        <span className="text-ink-muted">Brier <b className="text-ink-primary tabular-nums">{o.brier.toFixed(4)}</b></span>
      )}
      {o.accuracy != null && (
        <span className="text-ink-muted">
          TOP1 <b className="text-ink-primary tabular-nums">{pct(o.accuracy, 1)}</b>
          {hits != null && <span className="text-ink-disabled"> ({hits}/{o.n})</span>}
        </span>
      )}
      {Object.entries(replay.by_source)
        .filter(([, s]) => s.log_loss != null)
        .map(([src, s]) => (
          <span key={src} className="text-ink-disabled">
            {SOURCE_LABEL[src as PredictionEntry['model_source']] || src}:{' '}
            LL <span className="tabular-nums">{s.log_loss?.toFixed(4)}</span> · n={s.n}
            {s.accuracy != null && <span> · TOP1 {pct(s.accuracy, 0)}</span>}
          </span>
        ))}
    </div>
  )
}

export default function Predictions() {
  const [day, setDay] = useState(shanghaiToday())
  const [now] = useState(() => Date.now())
  const tabs = useMemo(() => dateTabs(now), [now])

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ['predictions', day],
    queryFn: ({ signal }) => predictionsService.getByDate(day, false, signal),
    staleTime: 5 * 60 * 1000,
  })

  // 沙盘回放: 该日冻结预测 vs 实际赛果 (复盘条 + 完赛卡判定/分布证据)
  const { data: replayData } = useQuery({
    queryKey: ['predictions-replay', day],
    queryFn: ({ signal }) => predictionsService.replay(day, signal),
    staleTime: 5 * 60 * 1000,
  })
  const replay = replayData?.data?.data as ReplayPayload | undefined
  const verdicts = useMemo(() => {
    const m = new Map<string, ReplayMatch>()
    replay?.matches?.forEach((x) => { if (x.actual) m.set(x.match_key, x) })
    return m
  }, [replay])

  const payload = data?.data?.data
  const list = (payload?.predictions || []) as PredictionEntry[]
  // 已结束与未结束分开: 未开赛/进行中在上 (升序), 已结束在底部独立区 (降序, 新完赛在前)。
  // isDone 时间兜底: 采集器状态滞后 (赛果未回补, status 停在 live) 的场按开赛时刻判定。
  const upcoming = useMemo(
    () => list.filter((p) => !isDone(p, now)).sort((a, b) => a.kickoff.localeCompare(b.kickoff)),
    [list, now])
  const finished = useMemo(
    () => list.filter((p) => isDone(p, now)).sort((a, b) => b.kickoff.localeCompare(a.kickoff)),
    [list, now])
  const groupedUpcoming = useMemo(() => groupByLeague(upcoming), [upcoming])
  const groupedFinished = useMemo(() => groupByLeague(finished), [finished])

  return (
    <div className="p-4 md:p-6 max-w-[1100px] mx-auto">
      <PageHeader
        title="预测中心"
        subtitle="概率输出 · 只解释, 不喊单 — 概率经历史赛果校准 (LogLoss / Brier 口径)"
        icon={
          <svg className="w-6 h-6 text-field-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.6">
            <path strokeLinecap="round" strokeLinejoin="round" d="M9.75 3.104v5.714a2.25 2.25 0 01-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 014.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0112 15a9.065 9.065 0 00-6.23.693L5 14.5m14.8.8l1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0112 21c-2.773 0-5.491-.235-8.135-.687-1.718-.293-2.3-2.379-1.067-3.61L5 14.5" />
          </svg>
        }
      />

      <CalibrationStrip />

      {/* 日期条 */}
      <div className="flex items-center gap-2 mb-4 flex-wrap">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setDay(t.key)}
            className={`px-3 py-1.5 rounded-md text-xs font-medium border transition-colors ${
              t.key === day
                ? 'text-field-400 bg-field-500/10 border-field-500/25'
                : 'text-ink-muted border-surface-border hover:text-ink-primary hover:bg-white/[0.04]'
            }`}
        >
          {t.label}
        </button>
        ))}
        <span className="flex-1" />
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="px-3 py-1.5 rounded-md text-xs text-ink-muted border border-surface-border hover:text-ink-primary hover:bg-white/[0.04] transition-colors disabled:opacity-50"
        >
          {isFetching ? '刷新中…' : '刷新'}
        </button>
      </div>

      <ReplayStrip replay={replay} />

      {isLoading ? (
        <Skeleton rows={6} />
      ) : isError ? (
        <ApiError message={(error as Error)?.message || '预测数据加载失败'} onRetry={() => refetch()} />
      ) : list.length === 0 ? (
        <EmptyState
          title="该日暂无预测数据"
          message={`预测表由 pipeline.predict_export 生成 (开赛前逐场落库, 开赛即冻结)。可运行: python -m pipeline.predict_export --date ${day}`}
        />
      ) : (
        <div className="space-y-6">
          {groupedUpcoming.length > 0 && <LeagueGroups groups={groupedUpcoming} nowMs={now} verdicts={verdicts} />}
          {groupedFinished.length > 0 && (
            <div>
              <div className="text-xs font-semibold text-danger-400/90 mb-2 flex items-center gap-2">
                已结束
                <span className="text-ink-disabled/60 font-normal">{finished.length} 场 · 新完赛在前</span>
                <span className="flex-1 h-px bg-surface-border" />
              </div>
              <LeagueGroups groups={groupedFinished} nowMs={now} verdicts={verdicts} />
            </div>
          )}
        </div>
      )}

      <p className="mt-6 text-[11px] text-ink-disabled text-center">
        概率预测 · 仅供分析参考 · 非投注建议 — 模型概率与市场隐含均为估计值, 完赛场按 LogLoss/Brier 持续校准
      </p>
    </div>
  )
}

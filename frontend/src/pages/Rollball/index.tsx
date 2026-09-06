import { useCallback, useEffect, useRef, useState } from 'react'
import PageHeader from '@/components/layout/PageHeader'
import MatchAnalysisModal from '@/pages/LiveScores/MatchAnalysisModal'
import { worldAnalyzerService, goldenEyeService, timelineService } from '@/services/api'

/**
 * 滚球分析仪表盘 (2026-08-30) — 四市场(胜平负/让球/大小球/比分) + 实时进度。
 * 数据源: /api/rollball/analyze 聚合端点(全部复用现成模型, 与详情页同源)。
 */

const POLL = 8000

interface RollballData {
  match_key: string
  status?: string
  minute?: number
  score?: string
  kickoff?: string
  league?: string
  home?: string
  away?: string
  goal_timeline?: { minute: number; score: string }[]
  opening_1x2?: { h: number; d: number; a: number; ph?: number; pd?: number; pa?: number; fav?: string } | null
  opening_ah?: { line: number; home: number; away: number; p_home_cover?: number | null } | null
  live_odds?: {
    x2?: { odds: number[]; p: number[]; fav: string } | null
    ou?: { line: number; over: number; under: number } | null
    ah?: { line: number; home: number; away: number } | null
  }
  ou?: {
    line?: number | null; direction?: string | null; prob?: number | null
    signal?: string | null; data_source?: string | null
    half?: { line?: number | null; direction?: string | null; prob?: number | null }
    anchor?: { open_line?: number; current_line?: number; open_total?: number; current_total?: number; drift_total?: number } | null
    verdict?: string | null
  }
  cs?: {
    found?: boolean
    mode?: string; score?: string; top5?: { score: string; prob: number }[]
    n_matched?: number; mean_dist?: number; basis?: string
    ou_align?: string; live_filter?: string
  }
  direction?: { winner?: string | null; label?: string | null; basis?: string | null; conflict?: boolean; opening_conflict?: boolean } | null
}

const pct = (v?: number | null, d = 0) => (v != null ? `${(v * 100).toFixed(d)}%` : '—')
const odds2 = (v?: number | null) => (v != null ? v.toFixed(2) : '—')
const dirCN = (d?: string | null) =>
  d === 'home' || d === '主胜' ? '主胜' : d === 'away' || d === '客胜' ? '客胜' : d === 'draw' || d === '平' ? '平局' : d === 'OVER' ? '大球' : d === 'UNDER' ? '小球' : d ?? '—'

function Card({ title, accent, children }: { title: string; accent?: string; children: React.ReactNode }) {
  return (
    <div className={`rounded-xl border p-4 bg-surface-dark/30 ${accent ?? 'border-surface-border/40'}`}>
      <div className="text-[12px] font-semibold text-ink-secondary mb-2">{title}</div>
      {children}
    </div>
  )
}

export default function Rollball() {
  const [matches, setMatches] = useState<any[]>([])
  const [sel, setSel] = useState<any>(null)
  const [data, setData] = useState<RollballData | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const [search, setSearch] = useState('')
  const [tab, setTab] = useState<'overview' | 'deep' | 'world' | 'ge' | 'timeline' | 'sixline'>('overview')
  const [sixData, setSixData] = useState<any>(null)
  const [sixErr, setSixErr] = useState('')
  const [worldData, setWorldData] = useState<any>(null)
  const [worldErr, setWorldErr] = useState('')
  const [geData, setGeData] = useState<any>(null)
  const [geErr, setGeErr] = useState('')
  const [tlData, setTlData] = useState<any>(null)
  const timer = useRef<number | null>(null)

  const loadMatches = useCallback(async () => {
    try {
      const r = await fetch('/api/live-goal-probe/matches?limit=200')
      const j = await r.json()
      const ms = ((j?.data ?? j)?.matches ?? []).filter(
        (m: any) => (m.league || '').toLowerCase().indexOf('vs-') < 0)
      setMatches(ms)
      setSel((s: any) => s || ms[0] || null)
    } catch { /* silent */ }
  }, [])

  const loadAnalyze = useCallback(async (m: any) => {
    if (!m) return
    setLoading(true)
    setErr('')
    try {
      const q = `match_key=${encodeURIComponent(m.match_key)}&score=${encodeURIComponent(m.score || '0-0')}&minute=${calibMinute(m)}`
      const r = await fetch(`/api/rollball/analyze?${q}`)
      const j = await r.json()
      if (j?.ok) setData(j.data)
      else setErr(j?.error || '分析失败')
    } catch (e: any) {
      setErr(e?.message || '网络错误')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadMatches()
    timer.current = window.setInterval(loadMatches, POLL)
    return () => { if (timer.current) window.clearInterval(timer.current) }
  }, [loadMatches])

  useEffect(() => {
    if (sel) loadAnalyze(sel)
  }, [sel, loadAnalyze])

  // ── 融合 Tab: 世界分析器 / 黄金神瞳 / 时间线 ──
  const loadWorld = useCallback(async (m: any) => {
    if (!m) return
    setWorldData(null); setWorldErr('')
    try {
      const p: any = { home: m.home, away: m.away, league: m.league, kickoff: m.kickoff }
      if (m.odds_h) { p.h = m.odds_h; p.d = m.odds_d; p.a = m.odds_a }
      if (m.ou_line) { p.ou_line = m.ou_line; p.ou_over = m.ou_over; p.ou_under = m.ou_under }
      const r = await worldAnalyzerService.analyze(p)
      setWorldData((r as any)?.data ?? r)
    } catch (e: any) { setWorldErr(e?.message || '分析失败') }
  }, [])

  const loadGoldenEye = useCallback(async (m: any) => {
    if (!m) return
    setGeData(null); setGeErr('')
    try {
      const r = await goldenEyeService.analyze({ home: m.home, away: m.away })
      setGeData((r as any)?.data ?? r)
    } catch (e: any) { setGeErr(e?.message || '分析失败') }
  }, [])

  const loadTimeline = useCallback(async () => {
    try {
      const r = await timelineService.getToday()
      setTlData((r as any)?.data ?? r)
    } catch { setTlData(null) }
  }, [])

  const loadSixline = useCallback(async (m: any) => {
    if (!m) return
    setSixData(null); setSixErr('')
    try {
      const [h, a] = String(m.score || '0-0').split('-')
      const q = `match_key=${encodeURIComponent(m.match_key)}&score=${encodeURIComponent(m.score || '0-0')}&minute=${calibMinute(m)}`
      const r = await fetch(`/api/sixline/analyze?${q}`)
      const j = await r.json()
      if (j?.ok) setSixData(j.data)
      else setSixErr(j?.error || '分析失败')
      void h; void a
    } catch (e: any) { setSixErr(e?.message || '网络错误') }
  }, [])

  useEffect(() => {
    if (!sel) return
    if (tab === 'world') loadWorld(sel)
    if (tab === 'ge') loadGoldenEye(sel)
    if (tab === 'timeline') loadTimeline()
    if (tab === 'sixline') loadSixline(sel)
  }, [sel, tab, loadWorld, loadGoldenEye, loadTimeline, loadSixline])

  // 通用数据面板: 标量键值 + 数组/对象摘要 (结构未知的服务响应统一展示)
  function DataPanel({ data, depth = 0 }: { data: any; depth?: number }) {
    if (data == null) return <span className="text-ink-muted">—</span>
    if (typeof data !== 'object') return <span className="text-ink-primary font-mono">{String(data)}</span>
    if (Array.isArray(data)) {
      return (
        <div className="space-y-1">
          {data.slice(0, 12).map((v, i) => (
            <div key={i} className="pl-2 border-l border-surface-border/40">
              <span className="text-[10px] text-ink-muted mr-1.5">#{i + 1}</span>
              <DataPanel data={v} depth={depth + 1} />
            </div>
          ))}
          {data.length > 12 && <div className="text-[10px] text-ink-muted">…共 {data.length} 项</div>}
        </div>
      )
    }
    const entries = Object.entries(data).filter(([, v]) => v != null)
    return (
      <div className="space-y-0.5">
        {entries.slice(0, 24).map(([k, v]) => (
          <div key={k} className="flex gap-2 text-[11px]">
            <span className="text-ink-muted shrink-0 w-32 truncate" title={k}>{k}</span>
            <span className="min-w-0"><DataPanel data={v} depth={depth + 1} /></span>
          </div>
        ))}
      </div>
    )
  }

  // 开赛延迟窗 + 分钟校准 (2026-08-31 用户需求):
  //   ① 开赛「延迟 3 分钟」后才加入列表(kickoff+3min ≤ now), 未开赛/刚开赛不显示
  //   ② 加入后比赛分钟用开赛时间墙钟校准 — 乐鱼 minute 有 45/90 占位垃圾与滞后,
  //      墙钟推算分钟(|采集−推算|>2 或采集缺失时)作为显示与分析入参的地面真相。
  // 足球时间三段映射: 墙钟流逝 → 比赛分钟 (上半场0-47 / 中场停表47-62 / 下半场62起从45'续走)。
  // 采集分钟仅当与墙钟一致(±2')时微调, 否则以墙钟为地面真相 (45/90 占位垃圾与滞后免疫)。
  const calibMinute = useCallback((m: any): number => {
    let probe: number | null = null
    if (m.kickoff) {
      const ko = new Date(String(m.kickoff).replace(' ', 'T')).getTime()
      if (!isNaN(ko)) {
        const elapsed = (Date.now() - ko) / 60000
        probe = elapsed <= 47 ? Math.max(0, Math.floor(elapsed))
              : elapsed <= 62 ? 45
              : Math.min(120, 45 + Math.floor(elapsed - 62))
      }
    }
    const raw = m.minute != null ? Math.max(0, Math.min(130, Number(m.minute))) : null
    if (probe == null) return raw ?? 0
    if (raw == null) return probe
    return Math.abs(raw - probe) > 2 ? probe : raw
  }, [])

  // 开赛「延迟 3 分钟」后才加入列表; 加入后分钟用 kickoff 墙钟校准 (calibMinute)。
  const filtered = matches.filter((m) => {
    if (search.trim()) {
      const q = search.trim().toLowerCase()
      if (!`${m.home} ${m.away} ${m.league}`.toLowerCase().includes(q)) return false
    }
    if (m.kickoff) {
      const ko = new Date(String(m.kickoff).replace(' ', 'T')).getTime()
      if (!isNaN(ko)) {
        const elapsedMin = (Date.now() - ko) / 60000
        if (elapsedMin < 3) return false   // 开赛未满 3 分钟 → 暂不加入
        if (elapsedMin > 130) return false // 完赛僵尸不占列表
      }
    } else if (!m.minute) {
      return false   // 无开赛时间且无分钟数 → 无法判定进度, 不加入
    }
    return true
  })

  const d = data
  const liveX2 = d?.live_odds?.x2
  const opX2 = d?.opening_1x2
  const ou = d?.ou
  const ahOpen = d?.opening_ah
  const ahLive = d?.live_odds?.ah
  const cs = d?.cs

  return (
    <div className="min-h-screen p-4 md:p-6">
      <PageHeader title="滚球分析" subtitle="胜平负 · 让球 · 大小球 · 比分 — 四市场同源分析, 结合实时进度 (进球轨迹 / 盘口漂移 / OU 对齐)" />

      <div className="mt-4 grid grid-cols-1 lg:grid-cols-[340px_1fr] gap-4">
        {/* 左: 比赛列表 */}
        <div className="rounded-xl border border-surface-border/40 bg-surface-dark/30 p-3">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[12px] font-semibold text-ink-secondary">进行中 / 未开赛</span>
            <span className="text-[10px] text-emerald-300">{matches.length} 场</span>
          </div>
          <input
            value={search} onChange={(e) => setSearch(e.target.value)}
            placeholder="搜队名 / 联赛"
            className="w-full mb-2 px-2.5 py-1.5 text-[12px] rounded-md bg-surface-card border border-surface-border/40 text-ink-primary placeholder:text-ink-disabled outline-none focus:border-field-500/50"
          />
          <div className="space-y-1 max-h-[70vh] overflow-y-auto">
            {filtered.map((m) => (
              <button
                key={m.match_key}
                onClick={() => setSel(m)}
                className={`w-full text-left px-2.5 py-2 rounded-lg border transition-colors ${
                  sel?.match_key === m.match_key
                    ? 'border-field-500/50 bg-field-500/[0.08]'
                    : 'border-surface-border/30 hover:bg-surface-hover/60'
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="text-[12px] text-ink-primary truncate">{m.home} vs {m.away}</span>
                  <span className="text-[12px] font-mono text-field-300 ml-2 shrink-0">{m.score}</span>
                </div>
                <div className="text-[10px] text-ink-muted mt-0.5">
                  {m.league} · {m.kickoff || m.minute ? `${calibMinute(m)}'` : '未开赛'}
                </div>
              </button>
            ))}
            {filtered.length === 0 && <div className="text-[11px] text-ink-muted p-2">无匹配比赛</div>}
          </div>
        </div>

        {/* 右: 分析面板 */}
        <div className="space-y-3">
          {!d && loading && <div className="text-[12px] text-ink-muted p-4">分析加载中…</div>}
          {err && <div className="text-[12px] text-rose-300 p-3 rounded-lg border border-rose-500/30 bg-rose-500/[0.05]">{err}</div>}
          {!d && !loading && !err && <div className="text-[12px] text-ink-muted p-4">← 左侧选择比赛开始分析</div>}

          {d && (
            <>
              {/* 实时进度 */}
              <div className="rounded-xl border border-surface-border/40 bg-surface-dark/30 p-4">
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <div>
                    <div className="text-[15px] font-semibold text-ink-primary">{d.home} vs {d.away}</div>
                    <div className="text-[10px] text-ink-muted mt-0.5">{d.league} · 开赛 {d.kickoff?.slice(0, 16).replace('T', ' ')}</div>
                  </div>
                  <div className="flex items-center gap-4">
                    <div className="text-center">
                      <div className="text-[10px] text-ink-muted">比分</div>
                      <div className="text-[22px] font-bold font-mono text-ink-primary leading-none mt-0.5">{d.score}</div>
                    </div>
                    <div className="text-center">
                      <div className="text-[10px] text-ink-muted">时间</div>
                      <div className="text-[22px] font-bold font-mono text-sky-300 leading-none mt-0.5">{sel ? `${calibMinute(sel)}'` : (d.minute != null && d.minute > 0 ? `${d.minute}'` : '—')}</div>
                    </div>
                  </div>
                </div>
                {/* 进球轨迹 */}
                {(d.goal_timeline?.length ?? 0) > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {d.goal_timeline!.map((g, i) => (
                      <span key={i} className="text-[10px] font-mono px-1.5 py-0.5 rounded border border-surface-border/40 bg-surface-card/60 text-ink-secondary">
                        {g.minute}′ {g.score}
                      </span>
                    ))}
                  </div>
                )}
              </div>

              {/* 融合 Tab 栏 */}
              <div className="flex items-center gap-1 rounded-lg bg-surface-dark/50 border border-surface-border/40 p-1 w-fit flex-wrap">
                {([
                  ['overview', '总览 · 四市场'],
                  ['deep', '全链路 7 模型'],
                  ['world', '世界分析器'],
                  ['ge', '黄金神瞳'],
                  ['timeline', '时间线'],
                  ['sixline', '六行框架'],
                ] as const).map(([id, label]) => (
                  <button
                    key={id}
                    onClick={() => setTab(id)}
                    className={`px-3 py-1.5 rounded-md text-[12px] font-medium transition-colors ${
                      tab === id ? 'bg-field-500/20 text-field-300 border border-field-500/30' : 'text-ink-muted hover:text-ink-primary'
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>

              {tab === 'deep' && sel && (
                <MatchAnalysisModal
                  home={sel.home}
                  away={sel.away}
                  sportKey="soccer"
                  league={sel.league}
                  kickoff={sel.kickoff}
                  matchKey={sel.match_key}
                  matchState={sel.minute != null && sel.minute > 0 ? 'live' : 'scheduled'}
                  liveScore={(() => {
                    const [h, a] = String(sel.score || '0-0').split('-').map((x: string) => parseInt(x, 10) || 0)
                    return { homeGoals: h, awayGoals: a, elapsed: calibMinute(sel) }
                  })()}
                  onClose={() => setTab('overview')}
                />
              )}

              {tab === 'sixline' && (
                <div className="rounded-xl border border-indigo-500/30 bg-indigo-500/[0.04] p-4 space-y-2">
                  <div className="flex items-center justify-between flex-wrap gap-2">
                    <span className="text-[12px] font-semibold text-indigo-300">六行分析框架 · 概率判断而非必中</span>
                    <button onClick={() => sel && loadSixline(sel)} className="text-[11px] px-2 py-1 rounded border border-surface-border/40 text-ink-secondary hover:text-ink-primary">重新分析</button>
                  </div>
                  {sixErr && <div className="text-[11px] text-rose-300">{sixErr}</div>}
                  {!sixData && !sixErr && <div className="text-[11px] text-ink-muted">分析中…</div>}
                  {sixData?.lines?.map((ln: any) => (
                    <div key={ln.no} className="rounded-lg border border-surface-border/30 bg-surface-card/40 px-3 py-2">
                      <span className="text-[10px] font-mono text-indigo-300 mr-2">行{ln.no}</span>
                      <span className="text-[12px] text-ink-primary">{ln.text}</span>
                    </div>
                  ))}
                  {sixData?.conclusion && (
                    <div className="text-[10px] text-ink-muted/70">
                      每场自动入台账(sixline_log), 赛后结算方向/比分池命中 — 支持 100+ 场复盘
                    </div>
                  )}
                </div>
              )}

              {tab === 'world' && (
                <div className="rounded-xl border border-surface-border/40 bg-surface-dark/30 p-4 space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-[12px] font-semibold text-ink-secondary">世界分析器 · 市场锚 + 模型矩阵 + Edge</span>
                    <button onClick={() => loadWorld(sel)} className="text-[11px] px-2 py-1 rounded border border-surface-border/40 text-ink-secondary hover:text-ink-primary">重新分析</button>
                  </div>
                  {worldErr && <div className="text-[11px] text-rose-300">{worldErr}</div>}
                  {!worldData && !worldErr && <div className="text-[11px] text-ink-muted">分析中…</div>}
                  {worldData != null && <DataPanel data={worldData} />}
                </div>
              )}

              {tab === 'ge' && (
                <div className="rounded-xl border border-surface-border/40 bg-surface-dark/30 p-4 space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-[12px] font-semibold text-ink-secondary">黄金神瞳 · 天眼/世界级/赛程OU/比分分布 三镜头</span>
                    <button onClick={() => loadGoldenEye(sel)} className="text-[11px] px-2 py-1 rounded border border-surface-border/40 text-ink-secondary hover:text-ink-primary">重新分析</button>
                  </div>
                  {geErr && <div className="text-[11px] text-rose-300">{geErr}</div>}
                  {!geData && !geErr && <div className="text-[11px] text-ink-muted">分析中…</div>}
                  {geData != null && <DataPanel data={geData} />}
                </div>
              )}

              {tab === 'timeline' && (
                <div className="rounded-xl border border-surface-border/40 bg-surface-dark/30 p-4 space-y-2">
                  <span className="text-[12px] font-semibold text-ink-secondary">时间线 · 当日赛程</span>
                  {tlData != null ? <DataPanel data={tlData} /> : <div className="text-[11px] text-ink-muted">加载中…</div>}
                </div>
              )}

              {/* 方向总判定 */}
              {tab === 'overview' && d.direction?.label && (
                <div className={`rounded-xl border p-3 ${d.direction.conflict ? 'border-amber-500/40 bg-amber-500/[0.05]' : 'border-emerald-500/40 bg-emerald-500/[0.06]'}`}>
                  <span className="text-[12px] text-ink-secondary">终场方向判定: </span>
                  <span className={`text-[14px] font-bold ${d.direction.conflict ? 'text-amber-300' : 'text-emerald-300'}`}>
                    {d.direction.label}
                  </span>
                  {d.direction.basis && <span className="text-[10px] text-ink-muted ml-2">{d.direction.basis}</span>}
                </div>
              )}

              {/* 四市场 */}
              <div className={`grid grid-cols-1 md:grid-cols-2 gap-3 ${tab === 'overview' ? '' : 'hidden'}`}>
                {/* 胜平负 */}
                <Card title="胜平负 1X2 (开盘 vs 即时)" accent="border-sky-500/30">
                  {opX2 && (
                    <div className="text-[11px] text-ink-muted mb-1.5">
                      开盘 {odds2(opX2.h)}/{odds2(opX2.d)}/{odds2(opX2.a)}
                      {opX2.ph != null && <span className="ml-1">(主 {pct(opX2.ph)} / 平 {pct(opX2.pd)} / 客 {pct(opX2.pa)})</span>}
                    </div>
                  )}
                  {liveX2 ? (
                    <div>
                      <div className="text-[11px] text-ink-muted mb-1">即时盘 {liveX2.odds.map(odds2).join(' / ')}</div>
                      <div className="flex gap-1.5">
                        {(['主胜', '平局', '客胜'] as const).map((lbl, i) => (
                          <div key={lbl} className={`flex-1 rounded-lg border px-2 py-1.5 text-center ${
                            liveX2.fav === ['home', 'draw', 'away'][i]
                              ? 'border-sky-500/50 bg-sky-500/[0.08]' : 'border-surface-border/30'
                          }`}>
                            <div className="text-[10px] text-ink-muted">{lbl}</div>
                            <div className={`text-[14px] font-mono font-bold ${liveX2.fav === ['home', 'draw', 'away'][i] ? 'text-sky-300' : 'text-ink-secondary'}`}>
                              {pct(liveX2.p[i], 1)}
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : <div className="text-[11px] text-ink-muted">暂无即时 1X2 盘口</div>}
                </Card>

                {/* 让球 */}
                <Card title="让球 AH (亚盘)" accent="border-violet-500/30">
                  <div className="space-y-1.5 text-[11px]">
                    {ahOpen ? (
                      <div className="flex justify-between">
                        <span className="text-ink-muted">开盘 {ahOpen.line > 0 ? `+${ahOpen.line}` : ahOpen.line}</span>
                        <span className="font-mono text-ink-primary">{odds2(ahOpen.home)} / {odds2(ahOpen.away)}</span>
                        {ahOpen.p_home_cover != null && <span className="text-ink-muted">主覆盖 {pct(ahOpen.p_home_cover)}</span>}
                      </div>
                    ) : <div className="text-ink-muted">无开盘让球</div>}
                    {ahLive && (
                      <div className="flex justify-between">
                        <span className="text-ink-muted">即时 {ahLive.line > 0 ? `+${ahLive.line}` : ahLive.line}</span>
                        <span className="font-mono text-field-300">{odds2(ahLive.home)} / {odds2(ahLive.away)}</span>
                      </div>
                    )}
                    {!ahOpen && !ahLive && <div className="text-ink-muted">暂无让球盘口</div>}
                  </div>
                </Card>

                {/* 大小球 */}
                <Card title="大小球 OU (破蛋)" accent="border-ember-500/30">
                  {ou && (ou.line != null || ou.data_source === 'live_odds') ? (
                    <div>
                      <div className="flex items-center gap-2 mb-1.5 flex-wrap">
                        <span className={`text-[13px] font-bold ${ou.direction === 'OVER' ? 'text-emerald-300' : 'text-amber-300'}`}>
                          {dirCN(ou.direction)}
                        </span>
                        {ou.line != null && <span className="text-[11px] font-mono text-ink-secondary">线 {ou.line}</span>}
                        {ou.prob != null && <span className="text-[11px] text-ink-muted">置信 {pct(ou.prob)}</span>}
                        {ou.verdict && <span className="text-[10px] text-ink-muted">{ou.verdict}</span>}
                      </div>
                      {ou && ou.data_source === 'live_odds' && d.live_odds?.ou && (
                        <div className="text-[11px] text-ink-muted mb-1">
                          即时线 {d.live_odds.ou.line}: 大 {odds2(d.live_odds.ou.over)} / 小 {odds2(d.live_odds.ou.under)}
                        </div>
                      )}
                      {ou.anchor?.open_line != null && (
                        <div className="text-[10px] text-ink-muted">
                          漂移: 开盘 {ou.anchor.open_line} → 当前 {ou.anchor.current_line ?? '—'}
                          {ou.anchor.drift_total != null && ` (总球 ${ou.anchor.drift_total > 0 ? '+' : ''}${ou.anchor.drift_total})`}
                        </div>
                      )}
                      {ou.data_source !== 'live_odds' && (
                        <div className="text-[10px] text-amber-300/80 mt-0.5">⚠ 非即时盘数据 ({ou.data_source})</div>
                      )}
                    </div>
                  ) : <div className="text-[11px] text-ink-muted">暂无 OU 盘口</div>}
                </Card>

                {/* 比分 */}
                <Card title="比分 CS (统一推荐)" accent="border-emerald-500/30">
                  {cs?.found ? (() => {
                    // 前端排除法兜底 (2026-08-31, 用户实测 札幌冈萨多 0-1 仍见 0-0/1-0):
                    // 后端比分滞后时 chips 可能含与当前比分矛盾的候选 — 渲染层按当前比分
                    // 再过滤一次, 主推自动顺延到第一个合法候选。
                    const cur = (d?.score || '').split('-')
                    const ch = parseInt(cur[0], 10)
                    const ca = parseInt(cur[1], 10)
                    const legal = (cs.top5 || []).filter((t) => {
                      if (isNaN(ch) || isNaN(ca)) return true
                      const seg = t.score.split('-')
                      return parseInt(seg[0], 10) >= ch && parseInt(seg[1], 10) >= ca
                    })
                    const list = legal.length ? legal : (cs.top5 || [])
                    const main = legal.length ? legal[0].score : cs.score
                    return (
                    <div>
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-[22px] font-bold font-mono text-emerald-300">{main}</span>
                        <span className="text-[10px] px-1 py-0.5 rounded bg-emerald-500/15 text-emerald-300 font-semibold">首选 TOP1</span>
                        {cs.mode === 'roll' && <span className="text-[10px] px-1 py-0.5 rounded bg-sky-500/15 text-sky-300">滚球态</span>}
                      </div>
                      {/* 推荐内容 = TOP1 + TOP3 (前三名, 回测 top3 含实际 95.1%@55-65'自洽样本) */}
                      <div className="mt-1">
                        <div className="text-[10px] text-ink-muted mb-1">推荐三选 (TOP3 · 滚球 60′ 回测含实际 95.1%)</div>
                        <div className="flex flex-wrap gap-1">
                          {list.slice(0, 3).map((t, i) => (
                            <span key={t.score} className={`text-[11px] font-mono px-2 py-1 rounded border ${
                              i === 0 ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-300' : 'border-field-500/30 bg-field-500/[0.05] text-ink-primary'
                            }`}>
                              <span className="text-[9px] text-ink-muted mr-1">TOP{i + 1}</span>
                              {t.score} <span className="text-ink-muted">{(t.prob * 100).toFixed(0)}%</span>
                            </span>
                          ))}
                          {list.length === 0 && <span className="text-[10px] text-ink-muted">当前比分下无合法候选</span>}
                        </div>
                      </div>
                      {list.length > 3 && (
                        <details className="mt-1">
                          <summary className="text-[10px] text-ink-muted cursor-pointer">完整分布 ({list.length} 项)</summary>
                          <div className="flex flex-wrap gap-1 mt-1">
                            {list.slice(3, 8).map((t) => (
                              <span key={t.score} className="text-[10px] font-mono px-1.5 py-0.5 rounded border border-white/10 bg-white/[0.04] text-ink-muted">
                                {t.score} {(t.prob * 100).toFixed(0)}%
                              </span>
                            ))}
                          </div>
                        </details>
                      )}
                      {cs.ou_align && <div className="text-[10px] text-sky-300/90 mt-1">{cs.ou_align}</div>}
                      {cs.n_matched != null && (
                        <div className="text-[10px] text-ink-muted mt-1">
                          DB 匹配 {cs.n_matched} 场(均距 {cs.mean_dist}){cs.live_filter ? ` · ${cs.live_filter}` : ''}
                        </div>
                      )}
                    </div>
                    )
                  })() : <div className="text-[11px] text-ink-muted">暂无比分推荐(无开盘三盘)</div>}
                </Card>
              </div>

              {/* 底注 */}
              <div className="text-[10px] text-ink-muted/70 px-1">
                全部结论与滚球详情页同源(probe / cross_score / unified_scoreline) · 非投注建议 · 数据每 {POLL / 1000}s 刷新
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

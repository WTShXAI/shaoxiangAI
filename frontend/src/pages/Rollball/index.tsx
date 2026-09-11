import { useCallback, useEffect, useRef, useState } from 'react'
import PageHeader from '@/components/layout/PageHeader'
import Skeleton from '@/components/shared/Skeleton'
import MatchAnalysisModal from '@/pages/LiveScores/MatchAnalysisModal'
import { worldAnalyzerService, goldenEyeService, timelineService } from '@/services/api'

/**
 * 滚球分析仪表盘 (2026-08-30) — 四市场(胜平负/让球/大小球/比分) + 实时进度。
 * 数据源: /api/rollball/analyze 聚合端点(全部复用现成模型, 与详情页同源)。
 *
 * UX 升级 (2026-09-08):
 *   ① 分析面板随 8s 轮询同步刷新(原只在切换比赛时拉一次 — 主面板比分/判定全程冻结),
 *      后台静默刷新不闪烁, 头部呼吸灯 + 「更新于 HH:MM:SS」明示数据新鲜度。
 *   ② 切换比赛立即清空旧面板(原停留上一场分析直到新响应到达 — 跨场误读风险)。
 *   ③ 进度卡: 比赛阶段徽章(上半场/中场/下半场) + 墙钟校准标记(分钟被纠偏时亮出)
 *      + 进球瞬间比分脉冲动画 + 进球时间轴可视化(主/客着色 · 中场线 · 当前分钟指针)。
 *   ④ 门控卡: 一致度进度条 + 信号着色; 头部常驻门控小结徽章(不用滚动即可见可信度)。
 *   ⑤ 四市场卡: OU 置信度概率条+信号徽章, 1X2 三向概率堆叠条+盘口转向提示,
 *      让球线移动指示。
 *   ⑥ 左列表: 比赛阶段状态点(滚球呼吸/中场/未开赛), 进行中按进度排序。
 */

const POLL = 8000
const DIR_CN: Record<string, string> = { home: '主胜', draw: '平', away: '客胜' }

/* 深挖 Tab 常见键名中文化 (未知键原样显示, 悬停看原文) */
const KEY_CN: Record<string, string> = {
  match_key: '比赛', home: '主队', away: '客队', league: '联赛', kickoff: '开赛',
  score: '比分', minute: '分钟', status: '状态', last_seen: '最近活跃',
  direction: '方向', prob: '概率', signal: '信号', line: '盘口线', verdict: '判定',
  top5: '前五比分', top3: '前三比分', n_matched: '匹配场数', mean_dist: '平均距离',
  odds: '赔率', h: '主胜赔', d: '平赔', a: '客胜赔', ou: '大小球', ah: '让球',
  ph: '主胜概率', pd: '平概率', pa: '客胜概率', fav: '热门',
  open_line: '开盘线', current_line: '当前线', drift_total: '总球漂移',
  half: '半场', full: '全场', half_signal: '半场信号', full_signal: '全场信号',
  ou_drift: 'OU漂移', priority: '优先级', expected_score: '预期比分',
  confidence: '置信度', conf: '置信度', edge: '优势', weight: '权重',
  agree: '一致', split: '分裂', level: '级别', pool: '比分池', fusion: '融合',
  risks: '风险', risk: '风险', fixture: '赛事', commence_time: '开赛时间',
  market_prob: '市场概率', value_layer: '价值层', strategy_tier: '策略层',
  injuries: '伤停', news: '新闻', preview: '前瞻', mode: '模式',
  basis: '依据', winner: '赢家', winner_label: '方向', winner_basis: '方向依据',
  is_scheduled: '未开赛', is_halftime: '中场', inducement: '诱导信号',
  current_score: '当前比分', current_minute: '当前分钟', total: '总球',
  cs_direction: '比分方向', ah_recommend: '让球推荐', aligned: '对齐',
  consensus: '共识', open_total: '开盘总球', pool_avg_total: '池均总球',
  anchor: '锚点', drift: '漂移', line_drop: '降盘', opening: '开盘',
  opening_1x2: '开盘1X2', opening_ah: '开盘让球', live_odds: '即时盘',
  goal_timeline: '进球轨迹', summary: '摘要', data_source: '数据源',
}

interface RollballData {
  match_key: string
  status?: string
  minute?: number
  score?: string
  score_known?: boolean
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
    direction?: { winner?: string; label?: string; prob?: number; basis?: string; top1_swapped?: boolean; note?: string } | null
  }
  direction?: { winner?: string | null; label?: string | null; basis?: string | null; conflict?: boolean; opening_conflict?: boolean } | null
  consensus_gate?: {
    level?: 'HIGH' | 'MED' | 'SPLIT' | 'NO_SIGNAL'
    verdict?: string
    majority?: string | null
    agree_ratio?: number
    signals?: Record<string, string>
    fusion?: { direction?: string; prob?: number; weights?: Record<string, number>; agree?: boolean; split?: boolean }
    cs_verify?: { pool_dir?: string; verdict?: string; match?: boolean }
  } | null
  final_direction?: { direction?: string; prob?: number; agree?: number; cs_verified?: boolean | null; level?: string } | null
  ht_freeze?: {
    ht_score: string
    ou: { line?: number | null; direction?: string | null; prob?: number | null }
    x2: { p_home?: number | null; p_draw?: number | null; p_away?: number | null; direction?: string | null }
    cs_top1?: string | null
    cs_top3?: string[]
    frozen_at?: number
  } | null
}

const pct = (v?: number | null, d = 0) => (v != null ? `${(v * 100).toFixed(d)}%` : '—')
const odds2 = (v?: number | null) => (v != null ? v.toFixed(2) : '—')
const dirCN = (d?: string | null) =>
  d === 'home' || d === '主胜' ? '主胜' : d === 'away' || d === '客胜' ? '客胜' : d === 'draw' || d === '平' ? '平局' : d === 'OVER' ? '大球' : d === 'UNDER' ? '小球' : d ?? '—'

function Card({ title, accent, children }: { title: string; accent?: string; children: React.ReactNode }) {
  return (
    <div className={`rounded-xl border p-3 bg-surface-dark/30 ${accent ?? 'border-surface-border/40'}`}>
      <div className="text-[12px] font-semibold text-ink-secondary mb-2">{title}</div>
      {children}
    </div>
  )
}

/* ── 比赛阶段 (墙钟口径, 与 calibMinute 三段映射同源) ── */
type PhaseKey = 'pre' | 'first' | 'ht' | 'second' | 'ft'
const PHASE_ORDER: Record<PhaseKey, number> = { second: 0, first: 1, ht: 2, pre: 3, ft: 4 }
function phaseInfo(m: any): { key: PhaseKey; label: string; live: boolean } {
  if (!m?.kickoff) return { key: 'pre', label: '未开赛', live: false }
  const ko = new Date(String(m.kickoff).replace(' ', 'T')).getTime()
  if (isNaN(ko)) return { key: 'pre', label: '未开赛', live: false }
  const elapsed = (Date.now() - ko) / 60000
  if (elapsed < 0) return { key: 'pre', label: '未开赛', live: false }
  if (elapsed <= 47) return { key: 'first', label: '上半场', live: true }
  if (elapsed <= 62) return { key: 'ht', label: '中场休息', live: false }
  if (elapsed <= 130) return { key: 'second', label: '下半场', live: true }
  return { key: 'ft', label: '已完赛', live: false }
}

/* 墙钟推算分钟 (三段映射), 无 kickoff 时 null — calibMinute 与列表排序共用 */
function wallclockMinute(m: any): number | null {
  if (!m?.kickoff) return null
  const ko = new Date(String(m.kickoff).replace(' ', 'T')).getTime()
  if (isNaN(ko)) return null
  const elapsed = (Date.now() - ko) / 60000
  if (elapsed <= 47) return Math.max(0, Math.floor(elapsed))
  if (elapsed <= 62) return 45
  return Math.min(120, 45 + Math.floor(elapsed - 62))
}

/* 列表排序: 下半场 > 上半场 > 中场 > 未开赛 > 完赛; 同阶段按比赛进度降序 */
function compareMatches(a: any, b: any): number {
  const pa = phaseInfo(a), pb = phaseInfo(b)
  if (PHASE_ORDER[pa.key] !== PHASE_ORDER[pb.key]) return PHASE_ORDER[pa.key] - PHASE_ORDER[pb.key]
  const ma = wallclockMinute(a) ?? Number(a.minute) ?? 0
  const mb = wallclockMinute(b) ?? Number(b.minute) ?? 0
  return mb - ma
}

function PhaseDot({ phase }: { phase: { key: PhaseKey; live: boolean } }) {
  if (phase.live) return <span className="w-1.5 h-1.5 rounded-full bg-field-400 animate-pulse shrink-0" />
  if (phase.key === 'ht') return <span className="w-1.5 h-1.5 rounded-full bg-ember-400 shrink-0" />
  return <span className="w-1.5 h-1.5 rounded-full bg-white/20 shrink-0" />
}

/* OU 信号 → 徽章 (颜色语义: 绿=强信号 / 黄=弱 / 蓝=已破 / 灰=无优势) */
const OU_SIG: Record<string, { label: string; cls: string }> = {
  STRONG_BREAK: { label: '强大球信号', cls: 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/25' },
  STRONG_HOLD: { label: '强小球信号', cls: 'bg-ember-500/15 text-ember-300 border border-ember-500/25' },
  WEAK_TREND: { label: '弱趋势', cls: 'bg-amber-500/10 text-amber-300 border border-amber-500/20' },
  ALREADY_BROKEN: { label: '已破线', cls: 'bg-sky-500/15 text-sky-300 border border-sky-500/25' },
  NO_EDGE: { label: '弱优势', cls: 'bg-white/[0.06] text-ink-secondary border border-white/10' },
}

/* 门控信号值着色 (主胜/客胜/平/大/小 各自语义色) */
const sigTone = (v: string) =>
  /主胜|home/i.test(v) ? 'text-sky-300' :
  /客胜|away/i.test(v) ? 'text-violet-300' :
  /平|draw/i.test(v) ? 'text-ink-secondary' :
  /OVER|大/i.test(v) ? 'text-emerald-300' :
  /UNDER|小/i.test(v) ? 'text-ember-300' : 'text-ink-muted'

export default function Rollball() {
  const [matches, setMatches] = useState<any[]>([])
  const [sel, setSel] = useState<any>(null)
  const [data, setData] = useState<RollballData | null>(null)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
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
  const selRef = useRef<any>(null)
  const loadingRef = useRef(false)

  /* 比分变动脉冲: 检测 d.score 前后差异, 定位进球方 */
  const [flash, setFlash] = useState<'home' | 'away' | null>(null)
  const prevScore = useRef('')
  useEffect(() => {
    const s = data?.score || ''
    const prev = prevScore.current
    prevScore.current = s
    if (!prev || !s || prev === s) return
    const [ph, pa] = prev.split('-').map((x) => parseInt(x, 10) || 0)
    const [h, a] = s.split('-').map((x) => parseInt(x, 10) || 0)
    if (h > ph || a > pa) {
      setFlash(h > ph ? 'home' : 'away')
      const t = window.setTimeout(() => setFlash(null), 1300)
      return () => window.clearTimeout(t)
    }
  }, [data?.score])

  const loadMatches = useCallback(async () => {
    try {
      const r = await fetch('/api/live-goal-probe/matches?limit=200')
      const j = await r.json()
      const ms = ((j?.data ?? j)?.matches ?? []).filter(
        (m: any) => (m.league || '').toLowerCase().indexOf('vs-') < 0)
      ms.sort(compareMatches)   // 进行中(按进度)在前 — 默认选中即列表首位
      setMatches(ms)
      setSel((s: any) => s || ms[0] || null)
    } catch { /* silent */ }
  }, [])

  const loadAnalyze = useCallback(async (m: any, background = false) => {
    if (!m || loadingRef.current) return
    loadingRef.current = true
    if (background) {
      setRefreshing(true)   // 后台刷新: 保留旧面板, 头部呼吸灯提示
    } else {
      setData(null)         // 切换比赛: 立即清空旧面板, 杜绝"看的是上一场"
      setLoading(true)
    }
    setErr('')
    try {
      const q = `match_key=${encodeURIComponent(m.match_key)}&score=${encodeURIComponent(m.score || '0-0')}&minute=${calibMinute(m)}`
      const r = await fetch(`/api/rollball/analyze?${q}`)
      const j = await r.json()
      if (j?.ok) {
        setData(j.data)
        setLastUpdated(new Date())
      } else if (!background) setErr(j?.error || '分析失败')
    } catch (e: any) {
      if (!background) setErr(e?.message || '网络错误')
    } finally {
      loadingRef.current = false
      setRefreshing(false)
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadMatches()
    timer.current = window.setInterval(() => {
      loadMatches()
      // ② 主面板随轮询同步刷新 — 比分/分钟/判定跟着比赛走, 而非冻结在选中瞬间
      if (selRef.current) loadAnalyze(selRef.current, true)
    }, POLL)
    return () => { if (timer.current) window.clearInterval(timer.current) }
  }, [loadMatches, loadAnalyze])

  useEffect(() => {
    selRef.current = sel
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
      const q = `match_key=${encodeURIComponent(m.match_key)}&score=${encodeURIComponent(m.score || '0-0')}&minute=${calibMinute(m)}`
      const r = await fetch(`/api/sixline/analyze?${q}`)
      const j = await r.json()
      if (j?.ok) setSixData(j.data)
      else setSixErr(j?.error || '分析失败')
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
            <span className="text-ink-secondary shrink-0 w-32 truncate" title={k}>{KEY_CN[k] || k}</span>
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
    const probe = wallclockMinute(m)
    const raw = m.minute != null ? Math.max(0, Math.min(130, Number(m.minute))) : null
    if (probe == null) return raw ?? 0
    if (raw == null) return probe
    return Math.abs(raw - probe) > 2 ? probe : raw
  }, [])

  // 墙钟校准是否实际纠偏过 (展示"墙钟校准"标记的系统优势: 分钟可信)
  const isCalibrated = useCallback((m: any): boolean => {
    if (!m?.kickoff || m.minute == null) return false
    const probe = wallclockMinute(m)
    if (probe == null) return false
    const raw = Math.max(0, Math.min(130, Number(m.minute)))
    return Math.abs(raw - probe) > 2
  }, [])

  // 开赛「延迟 3 分钟」后才加入列表(已按 compareMatches 排序: 进行中在前, 越接近终场越靠前)。
  const filtered = matches
    .filter((m) => {
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
    // 排序在 loadMatches 中已做(compareMatches) — 保证默认选中 = 列表首位

  const d = data
  const liveX2 = d?.live_odds?.x2
  const opX2 = d?.opening_1x2
  const ou = d?.ou
  const ahOpen = d?.opening_ah
  const ahLive = d?.live_odds?.ah
  const cs = d?.cs
  const gate = d?.consensus_gate
  const selPhase = sel ? phaseInfo(sel) : null

  /* 进球时间轴解析: 累计比分差分 → 进球方着色; 分钟按采集序单调钳制(历史 minute_at 垃圾免疫) */
  const timeline = (() => {
    const tl = d?.goal_timeline ?? []
    let prevM = 0
    let prevH = 0, prevA = 0
    return tl.map((g) => {
      const [h, a] = String(g.score).split('-').map((x) => parseInt(x, 10) || 0)
      const side = h > prevH ? 'home' : a > prevA ? 'away' : null
      prevH = Math.max(prevH, h); prevA = Math.max(prevA, a)
      const minute = Math.max(g.minute, prevM)
      prevM = minute
      return { ...g, minute, side }
    })
  })()
  const nowMin = sel ? calibMinute(sel) : 0

  return (
    <div className="min-h-screen p-3 md:p-4">
      <PageHeader compact title="滚球分析" subtitle="胜平负 · 让球 · 大小球 · 比分 — 四市场同源分析, 结合实时进度 (进球轨迹 / 盘口漂移 / OU 对齐)" />

      <div className="mt-2 grid grid-cols-1 lg:grid-cols-[340px_1fr] gap-3">
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
            {filtered.map((m) => {
              const ph = phaseInfo(m)
              const active = sel?.match_key === m.match_key
              return (
                <button
                  key={m.match_key}
                  onClick={() => setSel(m)}
                  className={`w-full text-left px-2.5 py-2 rounded-lg border transition-colors ${
                    active
                      ? 'border-field-500/50 bg-field-500/[0.08]'
                      : 'border-surface-border/30 hover:bg-surface-hover/60'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[12px] text-ink-primary truncate">{m.home} vs {m.away}</span>
                    {/* 比分缺失(断供场)显示 '—' 而非留白 — 诚实占位 */}
                    <span className={`text-[12px] font-mono ml-2 shrink-0 ${m.score ? 'text-field-300' : 'text-ink-disabled'}`}>
                      {m.score || '—'}
                    </span>
                  </div>
                  <div className="flex items-center gap-1.5 text-[10px] text-ink-muted mt-0.5">
                    <PhaseDot phase={ph} />
                    <span className="truncate">{m.league}</span>
                    {m.full_direction && (m.full_signal === 'STRONG_BREAK' || m.full_signal === 'STRONG_HOLD' || (m.full_prob != null && m.full_prob >= 0.56)) && (
                      <span className={`shrink-0 font-mono px-1 rounded ${m.full_direction === 'OVER' ? 'text-emerald-300/80 bg-emerald-500/[0.08]' : 'text-ember-300/80 bg-ember-500/[0.08]'}`}>
                        {m.full_direction === 'OVER' ? '大' : '小'} {m.full_prob != null ? (m.full_prob * 100).toFixed(0) + '%' : ''}
                      </span>
                    )}
                    <span className="ml-auto shrink-0 font-mono">
                      {ph.key === 'pre' ? '未开赛' : `${calibMinute(m)}'`}
                    </span>
                  </div>
                </button>
              )
            })}
            {filtered.length === 0 && <div className="text-[11px] text-ink-muted p-2">无匹配比赛</div>}
          </div>
        </div>

        {/* 右: 分析面板 */}
        <div className="space-y-2">
          {!d && loading && (
            <div className="space-y-2 p-1">
              <Skeleton variant="line" className="w-2/3" />
              <div className="grid grid-cols-2 gap-2">
                <Skeleton variant="card" /><Skeleton variant="card" />
              </div>
              <Skeleton variant="card" />
            </div>
          )}
          {err && <div className="text-[12px] text-rose-300 p-3 rounded-lg border border-rose-500/30 bg-rose-500/[0.05]">{err}</div>}
          {!d && !loading && !err && <div className="text-[12px] text-ink-muted p-4">← 左侧选择比赛开始分析</div>}

          {d && (
            <>
              {/* 实时进度 */}
              <div className="rounded-xl border border-surface-border/40 bg-surface-dark/30 p-4">
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-[15px] font-semibold text-ink-primary truncate">{d.home} vs {d.away}</span>
                      {/* 阶段徽章 */}
                      {selPhase && (selPhase.key !== 'pre' || d.status === 'scheduled') && (
                        <span className={`text-[10px] px-1.5 py-0.5 rounded border flex items-center gap-1 ${
                          selPhase.live ? 'border-field-500/30 bg-field-500/[0.08] text-field-300'
                            : selPhase.key === 'ht' ? 'border-ember-500/30 bg-ember-500/[0.08] text-ember-300'
                            : 'border-surface-border/40 bg-surface-card/60 text-ink-muted'
                        }`}>
                          {selPhase.live && <span className="w-1.5 h-1.5 rounded-full bg-field-400 animate-pulse" />}
                          {selPhase.label}
                        </span>
                      )}
                      {/* 墙钟校准标记: 分钟被墙钟纠偏时亮出 (feed 分钟不可信 → 系统已自动纠正) */}
                      {sel && isCalibrated(sel) && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded border border-frost-500/30 bg-frost-500/[0.08] text-frost-300"
                          title="采集分钟与真实时间背离, 已按开赛墙钟自动校准 — 免疫占位垃圾/滞后">
                          墙钟校准
                        </span>
                      )}
                      {/* 门控小结徽章: 不滚动即可见可信度 */}
                      {gate?.level && gate.level !== 'NO_SIGNAL' && (
                        <span className={`text-[10px] px-1.5 py-0.5 rounded border font-semibold ${
                          gate.level === 'HIGH' ? 'border-emerald-500/40 bg-emerald-500/[0.1] text-emerald-300'
                            : gate.level === 'SPLIT' ? 'border-rose-500/40 bg-rose-500/[0.1] text-rose-300'
                            : 'border-amber-500/30 bg-amber-500/[0.08] text-amber-300'
                        }`}>
                          {gate.level === 'HIGH' ? '✓ 四方向一致' : gate.level === 'SPLIT' ? '✗ 方向分歧' : '△ 多数一致'}
                        </span>
                      )}
                    </div>
                    <div className="text-[10px] text-ink-muted mt-0.5">{d.league} · 开赛 {d.kickoff?.slice(0, 16).replace('T', ' ')}</div>
                  </div>
                  <div className="flex items-center gap-4">
                    {/* 数据新鲜度: 呼吸灯 + 更新时间 (后台 8s 静默刷新可见) */}
                    <div className="text-right">
                      <div className="text-[10px] text-ink-muted flex items-center gap-1 justify-end">
                        <span className={`w-1.5 h-1.5 rounded-full ${refreshing ? 'bg-frost-400 animate-pulse' : 'bg-field-500'}`} />
                        {refreshing ? '刷新中…' : lastUpdated ? `更新于 ${lastUpdated.toLocaleTimeString('zh-CN', { hour12: false })}` : '—'}
                      </div>
                    </div>
                    <div className="text-center">
                      <div className="text-[10px] text-ink-muted">比分</div>
                      {/* 断供场: DB 无真实比分 → 显示 '—' + 断供徽章, 不把兜底 0-0 当真 */}
                      {d.score_known === false ? (
                        <>
                          <div className="text-[22px] font-bold font-mono text-ink-disabled leading-none mt-0.5" title="比分数据源断供(乐鱼对此联赛开赛后不再推流), 采集器每90s尝试外部救援">—</div>
                          <div className="text-[9px] text-amber-300/80 mt-0.5" title="乐鱼对此联赛开赛后不再推流比分; 采集器每90s尝试外部救援">比分断供</div>
                        </>
                      ) : (
                        <div className={`text-[22px] font-bold font-mono leading-none mt-0.5 ${flash ? 'animate-scorepulse ' + (flash === 'home' ? 'text-sky-300' : 'text-violet-300') : 'text-ink-primary'}`}>
                          {d.score}
                        </div>
                      )}
                    </div>
                    <div className="text-center">
                      <div className="text-[10px] text-ink-muted">时间</div>
                      <div className="text-[22px] font-bold font-mono text-sky-300 leading-none mt-0.5">{sel ? `${calibMinute(sel)}'` : (d.minute != null && d.minute > 0 ? `${d.minute}'` : '—')}</div>
                    </div>
                  </div>
                </div>
                {/* 进球时间轴: 主/客着色圆点沿分钟轴分布, 中场线 + 当前分钟指针 */}
                {timeline.length > 0 && (() => {
                  const pos = (m: number) => `${Math.min(100, Math.max(0, (Math.min(m, 95) / 95) * 100))}%`
                  return (
                    <div className="mt-4 px-1">
                      <div className="relative h-7">
                        <div className="absolute left-0 right-0 top-1/2 -translate-y-1/2 h-[3px] rounded-full bg-white/[0.07]" />
                        {/* 起止刻度 */}
                        <span className="absolute top-full -translate-x-1/2 text-[8px] text-ink-disabled" style={{ left: '0%' }}>0′</span>
                        <span className="absolute top-full -translate-x-full text-[8px] text-ink-disabled" style={{ left: '100%' }}>90′</span>
                        {/* 中场线 */}
                        <div className="absolute top-0 bottom-0 w-px bg-white/15" style={{ left: pos(45) }} title="中场 45'" />
                        <span className="absolute top-full -translate-x-1/2 text-[8px] text-ink-disabled" style={{ left: pos(45) }}>HT</span>
                        {/* 进球点 */}
                        {timeline.filter(g => g.side).map((g, i) => (
                          <div key={i} className="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 group"
                            style={{ left: pos(g.minute) }}>
                            <span className="relative block text-[11px] leading-none">
                              <span className={`absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-3 h-3 rounded-full ${g.side === 'home' ? 'bg-sky-400' : g.side === 'away' ? 'bg-violet-400' : 'bg-white/40'}`} />
                              <span className="relative" style={{ transform: 'translateY(-1px)' }}>⚽</span>
                            </span>
                            <span className="pointer-events-none absolute bottom-full mb-1 left-1/2 -translate-x-1/2 whitespace-nowrap text-[9px] font-mono px-1 py-0.5 rounded bg-surface-card border border-surface-border/60 text-ink-secondary opacity-0 group-hover:opacity-100 transition-opacity">
                              {g.minute}′ {g.score}
                            </span>
                          </div>
                        ))}
                        {/* 当前分钟指针 (滚球中) */}
                        {selPhase?.live && nowMin > 0 && nowMin <= 95 && (
                          <div className="absolute top-0 bottom-0 w-[2px] bg-field-400/70 rounded" style={{ left: pos(nowMin) }} title={`当前 ${nowMin}'`} />
                        )}
                      </div>
                      <div className="flex items-center gap-3 mt-3">
                        <span className="text-[10px] text-ink-muted flex items-center gap-1"><span className="relative inline-flex items-center justify-center"><span className="w-2.5 h-2.5 rounded-full bg-sky-400 inline-block" /><span className="absolute text-[8px]">⚽</span></span>{d.home}</span>
                        <span className="text-[10px] text-ink-muted flex items-center gap-1"><span className="relative inline-flex items-center justify-center"><span className="w-2.5 h-2.5 rounded-full bg-violet-400 inline-block" /><span className="absolute text-[8px]">⚽</span></span>{d.away}</span>
                        <span className="ml-auto text-[9px] text-ink-disabled">悬停进球点看比分</span>
                      </div>
                    </div>
                  )
                })()}
              </div>

              {/* 融合 Tab 栏 (吸顶: 深挖 tab 长内容滚动时保持可达) */}
              <div className="sticky top-0 z-10 flex items-center gap-1 rounded-lg bg-surface-dark/90 backdrop-blur border border-surface-border/40 p-1 w-fit flex-wrap shadow-sm">
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
                  {worldData != null && <div className="max-h-[62vh] overflow-y-auto pr-1"><DataPanel data={worldData} /></div>}
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
                  {geData != null && <div className="max-h-[62vh] overflow-y-auto pr-1"><DataPanel data={geData} /></div>}
                </div>
              )}

              {tab === 'timeline' && (
                <div className="rounded-xl border border-surface-border/40 bg-surface-dark/30 p-4 space-y-2">
                  <span className="text-[12px] font-semibold text-ink-secondary">时间线 · 当日赛程</span>
                  {tlData != null ? <div className="max-h-[62vh] overflow-y-auto pr-1"><DataPanel data={tlData} /></div> : <div className="text-[11px] text-ink-muted">加载中…</div>}
                </div>
              )}

              {/* 中场冻结判定 (2026-09-10 双锚点架构): 下半场的主判定。
                  HT 窗口用仅上半场信息冻结一次, 下半场不再随实时进球改写 —
                  判定可干净复盘重训。滚球实时卡降级为参考。 */}
              {tab === 'overview' && d?.ht_freeze && selPhase?.key === 'second' && (() => {
                const hf = d.ht_freeze
                const ouTxt = hf.ou.direction ? `${hf.ou.direction === 'OVER' ? '大' : '小'}${hf.ou.line != null ? hf.ou.line : ''}` : '—'
                const x2Txt = hf.x2.direction ? (DIR_CN[hf.x2.direction] ?? hf.x2.direction) : '—'
                return (
                  <div className="rounded-xl border border-field-500/50 bg-field-500/[0.07] p-3.5">
                    <div className="flex items-center justify-between flex-wrap gap-1.5">
                      <span className="text-[13px] font-bold text-field-300">
                        冻结 · 中场判定 (HT {hf.ht_score})
                      </span>
                      <span className="text-[10px] text-ink-muted">
                        {hf.frozen_at ? new Date(hf.frozen_at * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : ''} 冻结 · 下半场判定以此为准
                      </span>
                    </div>
                    <div className="mt-2 grid grid-cols-3 gap-2">
                      <div className="rounded-lg border border-surface-border/30 bg-surface-card/40 px-2 py-1.5 text-center">
                        <div className="text-[10px] text-ink-muted">OU 方向</div>
                        <div className={`text-[14px] font-bold font-mono ${hf.ou.direction === 'OVER' ? 'text-emerald-300' : 'text-amber-300'}`}>
                          {ouTxt}
                        </div>
                        {hf.ou.prob != null && <div className="text-[9px] text-ink-muted">置信 {(hf.ou.prob * 100).toFixed(0)}%</div>}
                      </div>
                      <div className="rounded-lg border border-surface-border/30 bg-surface-card/40 px-2 py-1.5 text-center">
                        <div className="text-[10px] text-ink-muted">1X2 方向</div>
                        <div className="text-[14px] font-bold font-mono text-sky-300">{x2Txt}</div>
                        {hf.x2.direction && hf.x2.p_home != null && hf.x2.p_draw != null && hf.x2.p_away != null && (
                          <div className="text-[9px] text-ink-muted">
                            {({ home: hf.x2.p_home, draw: hf.x2.p_draw, away: hf.x2.p_away } as Record<string, number | null>)[hf.x2.direction!] !== undefined
                              ? `${((( { home: hf.x2.p_home, draw: hf.x2.p_draw, away: hf.x2.p_away } as Record<string, number | null>)[hf.x2.direction!]) ?? 0) * 100 > 0 ? ((({ home: hf.x2.p_home, draw: hf.x2.p_draw, away: hf.x2.p_away } as Record<string, number | null>)[hf.x2.direction!])! * 100).toFixed(0) + '%' : ''}`
                              : ''}
                          </div>
                        )}
                      </div>
                      <div className="rounded-lg border border-surface-border/30 bg-surface-card/40 px-2 py-1.5 text-center">
                        <div className="text-[10px] text-ink-muted">CS 首选</div>
                        <div className="text-[14px] font-bold font-mono text-violet-300">{hf.cs_top1 ?? '—'}</div>
                        {(hf.cs_top3 ?? []).length > 1 && <div className="text-[9px] text-ink-muted">三选 {(hf.cs_top3 ?? []).join('/')}</div>}
                      </div>
                    </div>
                  </div>
                )
              })()}

              {/* 多方向一致性门控 (用户口径: 有分歧 → 结果不可信) */}
              {tab === 'overview' && gate?.level && gate.level !== 'NO_SIGNAL' && (() => {
                const g = gate
                const isHigh = g.level === 'HIGH'
                const isSplit = g.level === 'SPLIT'
                const style = isHigh
                  ? 'border-emerald-500/50 bg-emerald-500/[0.08]'
                  : isSplit
                    ? 'border-rose-500/50 bg-rose-500/[0.07]'
                    : 'border-amber-500/40 bg-amber-500/[0.05]'
                const txt = isHigh ? 'text-emerald-300' : isSplit ? 'text-rose-300' : 'text-amber-300'
                const sigs = Object.entries(g.signals || {})
                const agree = Math.max(0, Math.min(1, g.agree_ratio ?? 0))
                return (
                  <div className={`rounded-xl border p-3.5 ${style}`}>
                    <div className="flex items-center justify-between flex-wrap gap-1.5">
                      <span className={`text-[13px] font-bold ${txt}`}>
                        {isHigh ? '✓ 结论可信' : isSplit ? '✗ 比赛结果不可信' : '△ 结论较可信'}
                      </span>
                      <span className="text-[10px] text-ink-muted">方向一致度 {((g.agree_ratio ?? 0) * 100).toFixed(0)}%</span>
                    </div>
                    {/* 一致度进度条: 绿≥75 / 黄 50-75 / 红<50 */}
                    <div className="probability-bar mt-2">
                      <div className={`probability-fill ${agree >= 0.75 ? 'bg-field-500' : agree >= 0.5 ? 'bg-ember-500' : 'bg-danger-500'}`}
                        style={{ width: `${agree * 100}%` }} />
                    </div>
                    <div className="text-[11px] text-ink-secondary mt-2">{g.verdict}</div>
                    {(() => {
                      const fd = d.final_direction
                      const useFusion = !!fd?.direction
                      const structW = d.direction?.winner ?? null
                      const diverged = useFusion && structW && structW !== fd!.direction
                      const vLabel = useFusion
                        ? (DIR_CN[fd!.direction ?? ''] ?? fd!.direction)
                        : (d.direction?.label ?? null)
                      const vProb = useFusion ? fd!.prob : null
                      if (!vLabel) return null
                      return (
                        <div className="mt-2 flex items-center gap-2 flex-wrap">
                          <span className="text-[10px] text-ink-muted">终场方向判定:</span>
                          <span className={`text-[16px] font-bold font-mono ${diverged ? 'text-amber-300' : txt}`}>
                            {vLabel}
                          </span>
                          {vProb != null && <span className="text-[11px] text-ink-muted">置信 {((vProb ?? 0) * 100).toFixed(0)}%</span>}
                          {useFusion && <span className="text-[10px] px-1 py-0.5 rounded bg-field-500/15 text-field-300">四方向融合</span>}
                          {d.final_direction?.cs_verified != null && (
                            <span className={`text-[10px] px-1.5 py-0.5 rounded ${d.final_direction.cs_verified ? 'bg-emerald-500/15 text-emerald-300' : 'bg-amber-500/15 text-amber-300'}`}>
                              {d.final_direction.cs_verified ? '✓ 波胆验证通过' : '⚠ 波胆未确认'}
                            </span>
                          )}
                          {diverged && (
                            <span className="text-[10px] text-ink-muted" title="比分池结构方向与融合结论分歧 — 已按四方向加权裁决">
                              (结构方向 {DIR_CN[structW ?? ''] ?? structW} 与融合分歧)
                            </span>
                          )}
                          {!useFusion && d.direction?.basis && <span className="text-[10px] text-ink-muted">{d.direction.basis}</span>}
                        </div>
                      )
                    })()}
                    {sigs.length > 0 && (
                      <div className="mt-1.5 flex flex-wrap gap-1.5">
                        {sigs.map(([k, v]: [string, string]) => (
                          <span key={k} className="text-[10px] font-mono px-1.5 py-0.5 rounded border border-white/10 bg-white/[0.04]">
                            <span className="text-ink-muted">{k}: </span>
                            <span className={sigTone(v)}>{v}</span>
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                )
              })()}

              {/* 四市场 */}
              <div className={`grid grid-cols-1 md:grid-cols-2 gap-2 ${tab === 'overview' ? '' : 'hidden'}`}>
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
                      <div className="text-[11px] text-ink-muted mb-1 flex items-center gap-1.5 flex-wrap">
                        <span>即时盘 {liveX2.odds.map(odds2).join(' / ')}</span>
                        {/* 盘口转向提示: 开盘热门 ≠ 即时热门 */}
                        {opX2?.fav && liveX2.fav && opX2.fav !== liveX2.fav && (
                          <span className="text-[9px] px-1 py-0.5 rounded bg-amber-500/15 text-amber-300 border border-amber-500/20">
                            ⇄ 盘口转向 {dirCN(liveX2.fav)}
                          </span>
                        )}
                      </div>
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
                      {/* 三向概率堆叠条: 一眼看出市场倾斜 */}
                      <div className="flex h-1.5 rounded-full overflow-hidden mt-2 bg-white/[0.06]">
                        <div className="bg-sky-500/70 transition-all duration-500" style={{ width: `${Math.max(0, Math.min(1, liveX2.p[0])) * 100}%` }} />
                        <div className="bg-white/20 transition-all duration-500" style={{ width: `${Math.max(0, Math.min(1, liveX2.p[1])) * 100}%` }} />
                        <div className="bg-violet-500/70 transition-all duration-500" style={{ width: `${Math.max(0, Math.min(1, liveX2.p[2])) * 100}%` }} />
                      </div>
                    </div>
                  ) : <div className="text-[11px] text-ink-muted">暂无即时 1X2 盘口</div>}
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
                    const postponed = legal.length > 0 && cs.top5 != null && cs.top5.length > 0 && legal[0].score !== cs.top5[0].score
                    return (
                    <div>
                      <div className="flex items-center gap-2 mb-1 flex-wrap">
                        <span className="text-[22px] font-bold font-mono text-emerald-300">{main}</span>
                        <span className="text-[10px] px-1 py-0.5 rounded bg-emerald-500/15 text-emerald-300 font-semibold">首选 TOP1</span>
                        {cs.mode === 'roll' && <span className="text-[10px] px-1 py-0.5 rounded bg-sky-500/15 text-sky-300">滚球态</span>}
                        {cs.mode === 'prior' && (
                          <span className="text-[10px] px-1 py-0.5 rounded bg-ember-500/15 text-ember-300 border border-ember-500/20"
                            title="本场无开盘三盘, 以期望进球先验分析(诚实降级)">
                            先验分析
                          </span>
                        )}
                        {/* 仲裁方向徽章: 与首选比分恒一致 (2026-09-10 串联矛盾根治) */}
                        {cs.direction?.label && (
                          <span className="text-[10px] px-1 py-0.5 rounded bg-violet-500/15 text-violet-300 border border-violet-500/25"
                            title={cs.direction.basis || '比分池仲裁方向'}>
                            池方向 {cs.direction.label} {cs.direction.prob != null ? (cs.direction.prob * 100).toFixed(0) + '%' : ''}
                          </span>
                        )}
                        {postponed && (
                          <span className="text-[9px] px-1 py-0.5 rounded bg-amber-500/10 text-amber-300 border border-amber-500/20"
                            title={`后端首推 ${cs.top5?.[0]?.score} 与当前比分矛盾, 已按排除法顺延`}>
                            首推已顺延
                          </span>
                        )}
                      </div>
                      {cs.direction?.note && <div className="text-[10px] text-ink-muted mt-0.5">{cs.direction.note}</div>}
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
                        {/* TOP3 概率占比条 */}
                        {list.length >= 2 && (
                          <div className="flex h-1 rounded-full overflow-hidden mt-1.5 bg-white/[0.06]">
                            {list.slice(0, 3).map((t, i) => (
                              <div key={t.score}
                                className={i === 0 ? 'bg-emerald-500/80' : i === 1 ? 'bg-field-500/50' : 'bg-field-500/30'}
                                style={{ width: `${(t.prob / (list[0].prob + list[1].prob + (list[2]?.prob ?? 0))) * 100}%` }} />
                            ))}
                          </div>
                        )}
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
                      {/* 条件化推荐 (2026-09-10): OU 方向与首选比分类别分歧时,
                          按 OU 命中与否给出条件首选 — 张力转成信息而非矛盾 */}
                      {(() => {
                        const od = ou?.direction
                        const ol = ou?.line
                        if (!od || !ol || !list.length) return null
                        const sat = list.filter(t => {
                          const [a, b] = t.score.split('-').map(Number)
                          const tot = a + b
                          return od === 'OVER' ? tot > ol : tot < ol
                        })
                        const top = list[0]
                        const topSat = top && (() => {
                          const [a, b] = top.score.split('-').map(Number)
                          return od === 'OVER' ? a + b > ol : a + b < ol
                        })()
                        if (topSat || !sat.length) return null
                        return (
                          <div className="text-[10px] mt-1 text-ink-muted">
                            条件推荐: OU{od === 'OVER' ? '大' : '小'}命中 → 首选 {sat[0].score} ({(sat[0].prob * 100).toFixed(0)}%)
                            ；OU未中 → {top.score}
                          </div>
                        )
                      })()}
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
              {/* 让球 */}
                <Card title="让球 AH (亚盘)" accent="border-violet-500/30">
                  <div className="space-y-1.5 text-[11px]">
                    {ahOpen ? (
                      <div className="flex justify-between items-center">
                        <span className="text-ink-muted">开盘 {ahOpen.line > 0 ? `+${ahOpen.line}` : ahOpen.line}</span>
                        <span className="font-mono text-ink-primary">{odds2(ahOpen.home)} / {odds2(ahOpen.away)}</span>
                        {ahOpen.p_home_cover != null && <span className="text-ink-muted">主覆盖 {pct(ahOpen.p_home_cover)}</span>}
                      </div>
                    ) : <div className="text-ink-muted">无开盘让球</div>}
                    {ahLive && (
                      <div className="flex justify-between items-center">
                        <span className="text-ink-muted flex items-center gap-1">
                          即时 {ahLive.line > 0 ? `+${ahLive.line}` : ahLive.line}
                          {/* 线移动指示: 开盘 → 即时 发生升降盘 */}
                          {ahOpen != null && ahLive.line !== ahOpen.line && (
                            <span className="text-[9px] px-1 py-0.5 rounded bg-frost-500/15 text-frost-300 border border-frost-500/20">
                              {ahLive.line > (ahOpen?.line ?? ahLive.line) ? '↑' : '↓'} 升降盘
                            </span>
                          )}
                        </span>
                        <span className="font-mono text-field-300">{odds2(ahLive.home)} / {odds2(ahLive.away)}</span>
                      </div>
                    )}
                    {!ahOpen && !ahLive && <div className="text-ink-muted">暂无让球盘口</div>}
                  </div>
                </Card>

                {/* 大小球 */}
                <Card title="大小球 OU (破蛋)" accent="border-ember-500/30">
                  {ou && (ou.line != null || ou.data_source === 'live_odds' || ou.data_source === 'league_prior') ? (
                    <div>
                      <div className="flex items-center gap-2 mb-1.5 flex-wrap">
                        {/* 哨响理念: 永不观望 — NO_EDGE 也输出方向(弱优势标注), 概率照实展示 */}
                        <span className={`text-[13px] font-bold ${ou.direction === 'OVER' ? 'text-emerald-300' : 'text-amber-300'}`}>
                          {dirCN(ou.direction)}
                        </span>
                        {ou.line != null && <span className="text-[11px] font-mono text-ink-secondary">线 {ou.line}</span>}
                        {ou.signal && OU_SIG[ou.signal] && (
                          <span className={`text-[9px] px-1.5 py-0.5 rounded ${OU_SIG[ou.signal].cls}`}>{OU_SIG[ou.signal].label}</span>
                        )}
                        {ou.verdict && <span className="text-[10px] text-ink-muted">{ou.verdict}</span>}
                      </div>
                      {/* 置信度概率条 (绿=大 / 黄=小, 长度=置信) */}
                      {ou.prob != null && (
                        <div className="probability-bar mb-1.5">
                          <div className={`probability-fill ${ou.direction === 'OVER' ? 'bg-field-500' : 'bg-ember-500'}`}
                            style={{ width: `${Math.max(0, Math.min(1, ou.prob)) * 100}%` }} />
                        </div>
                      )}
                      {ou && ou.data_source === 'live_odds' && d.live_odds?.ou && (
                        <div className="text-[11px] text-ink-muted mb-1">
                          即时线 {d.live_odds.ou.line}: 大 {odds2(d.live_odds.ou.over)} / 小 {odds2(d.live_odds.ou.under)}
                        </div>
                      )}
                      {ou.anchor?.open_line != null && (
                        <div className="text-[10px] text-ink-muted flex items-center gap-1 flex-wrap">
                          {(ou.anchor.current_line ?? ou.anchor.open_line) !== ou.anchor.open_line && (
                            <span className={`px-1 rounded ${ou.anchor.current_line! < ou.anchor.open_line! ? 'bg-rose-500/15 text-rose-300' : 'bg-field-500/15 text-field-300'}`}
                              title="庄家调整总球线: 降线=防大, 升线=放大的信号">
                              {ou.anchor.current_line! < ou.anchor.open_line! ? '↓ 降盘' : '↑ 升盘'}
                            </span>
                          )}
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

                </div>

              {/* 底注 */}
              <div className="text-[10px] text-ink-muted/70 px-1">
                全部结论与滚球详情页同源(probe / cross_score / unified_scoreline) · 四方向一致性门控 · 分钟墙钟校准 ·
                非投注建议 · 列表与分析每 {POLL / 1000}s 自动刷新
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

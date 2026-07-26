import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { leagueScheduleService, liveScoreService } from '@/services/api'
import MatchAnalysisModal from '../LeagueSchedule/MatchAnalysisModal'
import PageHeader from '@/components/layout/PageHeader'
import type { FixtureEntry } from '@/types'

// ═══ 工具 (统一用 timeZone:'Asia/Shanghai', 不依赖本机时区, 任意机器都正确) ═══
function fmtClockGMT8(now: number) {
  return new Date(now).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}
function fmtGMT8(iso: string) {
  try {
    return new Date(iso).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false })
  } catch { return '' }
}
function fmtOdds(v: number | undefined | null) {
  return typeof v === 'number' && !isNaN(v) ? v.toFixed(2) : '——'
}
// mststi: 0=未开赛 1=上半场 2=中场 3=下半场 4=加时 5=点球 6+=异常(中断/延期/取消) <0=结束
// 注意:
//   1. feed 的 state 标记经常滞后 (实际已开赛但 state 仍=0)
//   2. match_minute='PA' 表示"待定/延期", 此时无论 state 是什么都不应显示进行中
//   3. state>=6 是异常状态 (非正常进行中), 不应显示比分
//   4. score 缺失 (null/undefined) 时不能显示比分
function stateOf(fx: FixtureEntry, now: number): { live: boolean; finished: boolean; pending: boolean; min: number | null; label: string } {
  let st = Number(fx.match_state ?? 0)
  if (isNaN(st)) st = 0
  const raw = String(fx.match_minute ?? '').replace(/[′'"]/g, '')
  const mm = parseInt(raw, 10)
  const minStr = isNaN(mm) ? '' : `${mm}'`
  const isPA = raw === 'PA' || raw === '中场' || raw === 'P'
  const isAbnormal = st >= 6   // 6+ = 中断/延期/取消等异常
  // PA 或异常状态 → 一律视为待定, 不显示进行中
  if (isPA || isAbnormal) {
    return { live: false, finished: false, pending: true, min: null, label: '待定' }
  }
  // feed 状态滞后兜底: state=0 但开赛已过 10-180min → 视为进行中
  if (st === 0 && fx.commence_time) {
    const elapsedMin = (now - new Date(fx.commence_time).getTime()) / 60000
    if (elapsedMin > 10 && elapsedMin < 180) {
      st = 1 // 视为上半场
    }
  }
  // 开赛已超 3h 且非结束 → 视为已结束
  if (st === 0 && fx.commence_time) {
    const elapsedMin = (now - new Date(fx.commence_time).getTime()) / 60000
    if (elapsedMin > 180) st = -1
  }
  let label = ''
  if (st === 1) label = `上半场 ${minStr || `~${Math.round((now - new Date(fx.commence_time).getTime()) / 60000)}'`}`.trim()
  else if (st === 2) label = '中场休息'
  else if (st === 3) label = `下半场 ${minStr}`.trim()
  else if (st === 4) label = `加时 ${minStr}`.trim()
  else if (st === 5) label = '点球大战'
  else if (st < 0) label = '已结束'
  return { live: st > 0, finished: st < 0, pending: false, min: isNaN(mm) ? null : mm, label }
}
// 倒计时 (距开赛)
function countdown(iso: string, now: number): string | null {
  const ko = new Date(iso).getTime()
  const remain = ko - now
  if (remain <= 0) return null
  const m = Math.floor(remain / 60000)
  const h = Math.floor(m / 60)
  const d = Math.floor(h / 24)
  if (d > 0) return `${d}天${h % 24}h`
  if (h > 0) return `${h}h${m % 60}m`
  return `${m}m`
}

// ═══ 虚盘过滤 (滤掉乐鱼混入的电竞模拟 + 8分钟虚拟杯) ═══
const FAKE_LEAGUE = /VS-|EAFC|PANDA|瓦尔哈拉|瓦尔基里|梦幻对垒|8分钟/

// ═══ 状态徽章 ═══
function StatusBadge({ fx, now }: { fx: FixtureEntry; now: number }) {
  const { live, finished, pending } = stateOf(fx, now)
  if (live) {
    return (
      <span className='inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold bg-field-500/15 text-field-400 border border-field-500/25'>
        <span className='w-1.5 h-1.5 rounded-full bg-field-500 animate-pulse' />进行中
      </span>
    )
  }
  if (finished) {
    return (
      <span className='inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold bg-frost-500/12 text-frost-400 border border-frost-500/20'>
        已结束
      </span>
    )
  }
  if (pending) {
    // feed 标记待定(PA) 或异常状态(state>=6) → 状态待定
    return (
      <span className='inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold bg-surface-border/60 text-ink-muted'>
        待定
      </span>
    )
  }
  // 正常未开赛: 看距开赛时间
  const cd = countdown(fx.commence_time, now)
  if (cd) {
    return (
      <span className='inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold bg-surface-border/60 text-ink-secondary'>
        距开赛 {cd}
      </span>
    )
  }
  // 开赛时间已过且非 PA → feed 状态滞后, 实际已开赛
  return (
    <span className='inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold bg-ember-500/15 text-ember-400 border border-ember-500/25'>
      <span className='w-1.5 h-1.5 rounded-full bg-ember-500 animate-pulse' />
      已开赛·数据滞后
    </span>
  )
}

// ═══ 1X2 赔率 chips ═══
function OddsPanel({ fx }: { fx: FixtureEntry }) {
  // ── 1X2 ──
  const h = fx.odds_h, d = fx.odds_d, a = fx.odds_a
  const oh = fx.opening_h, od_ = fx.opening_d, oa = fx.opening_a
  const has1x2 = typeof h === 'number' && typeof d === 'number' && typeof a === 'number'
  const has1x2Op = typeof oh === 'number' && typeof od_ === 'number' && typeof oa === 'number'
  // ── AH (让球) ──
  const ahL = fx.ah_line, ahH = fx.ah_home, ahA = fx.ah_away
  const ahOpH = fx.ah_op_home, ahOpA = fx.ah_op_away
  const hasAH = typeof ahH === 'number' && typeof ahA === 'number' && ahL != null
  const hasAHOp = typeof ahOpH === 'number' && typeof ahOpA === 'number'
  // ── OU (大小) ──
  const ouL = fx.ou_line, ouO = fx.ou_over, ouU = fx.ou_under
  const ouOpO = fx.ou_op_over, ouOpU = fx.ou_op_under
  const hasOU = typeof ouO === 'number' && typeof ouU === 'number' && ouL != null
  const hasOUOp = typeof ouOpO === 'number' && typeof ouOpU === 'number'

  if (!has1x2 && !has1x2Op && !hasAH && !hasAHOp && !hasOU && !hasOUOp) return null

  const C = (v: number | undefined | null) => typeof v === 'number' && !isNaN(v) ? String(Number(v.toFixed(2))) : '——'
  const G = (v: number | undefined | null, c: string) =>
    <span className={`font-mono ${c} font-bold tabular-nums`}>{C(v)}</span>

  const Lb = 'text-ink-muted/50 w-[26px] flex-shrink-0 text-right text-[10px]'
  const Mu = 'text-ink-muted/50'
  const Mm = 'text-ink-muted/60'

  return (
    <div className='flex flex-col gap-0.5'>
      {/* ═══ 1X2 初盘 ═══ */}
      {has1x2Op && <div className='flex items-center gap-1 text-[10px]'>
        <span className={Lb}>初</span>
        <span className='inline-flex gap-1 bg-surface-dark/40 rounded px-1.5 py-px'>
          <span className={Mu}>主</span><span className={`font-mono ${Mm} font-bold tabular-nums`}>{C(oh)}</span>
          <span className={Mu}>平</span><span className={`font-mono ${Mm} font-bold tabular-nums`}>{C(od_)}</span>
          <span className={Mu}>客</span><span className={`font-mono ${Mm} font-bold tabular-nums`}>{C(oa)}</span>
        </span>
      </div>}
      {/* ═══ 1X2 滚动 ═══ */}
      {has1x2 && <div className='flex items-center gap-1 text-[11px]'>
        <span className='text-ink-secondary w-[26px] flex-shrink-0 text-right text-[10px]'>{has1x2Op ? '滚' : '赔率'}</span>
        <span className='inline-flex gap-1 bg-surface-dark/50 rounded px-1.5 py-0.5'>
          <span className='text-ink-muted'>主</span>{G(h, 'text-pitch-400')}
          <span className='text-ink-muted'>平</span>{G(d, 'text-white/80')}
          <span className='text-ink-muted'>客</span>{G(a, 'text-ember-400')}
        </span>
      </div>}
      {/* ═══ AH (让球) 初盘 ═══ */}
      {hasAHOp && <div className='flex items-center gap-1 text-[10px]'>
        <span className={Lb}>初</span>
        <span className='inline-flex gap-1 bg-surface-dark/40 rounded px-1.5 py-px'>
          <span className={Mu}>让{ahL}</span>
          <span className={`font-mono ${Mm} font-bold tabular-nums`}>{C(ahOpH)}</span>
          <span className={`font-mono ${Mm} font-bold tabular-nums`}>{C(ahOpA)}</span>
        </span>
      </div>}
      {/* ═══ AH 滚动 ═══ */}
      {hasAH && <div className='flex items-center gap-1 text-[11px]'>
        <span className='text-ink-secondary w-[26px] flex-shrink-0 text-right text-[10px]'>{hasAHOp ? '滚' : '让球'}</span>
        <span className='inline-flex gap-1 bg-surface-dark/50 rounded px-1.5 py-0.5'>
          <span className='text-ink-muted'>让{ahL}</span>
          {G(ahH, 'text-pitch-400')}{G(ahA, 'text-ember-400')}
        </span>
      </div>}
      {/* ═══ OU (大小) 初盘 ═══ */}
      {hasOUOp && <div className='flex items-center gap-1 text-[10px]'>
        <span className={Lb}>初</span>
        <span className='inline-flex gap-1 bg-surface-dark/40 rounded px-1.5 py-px'>
          <span className={Mu}>大{ouL}</span>
          <span className={`font-mono ${Mm} font-bold tabular-nums`}>{C(ouOpO)}</span>
          <span className={`font-mono ${Mm} font-bold tabular-nums`}>{C(ouOpU)}</span>
        </span>
      </div>}
      {/* ═══ OU 滚动 ═══ */}
      {hasOU && <div className='flex items-center gap-1 text-[11px]'>
        <span className='text-ink-secondary w-[26px] flex-shrink-0 text-right text-[10px]'>{hasOUOp ? '滚' : '大小'}</span>
        <span className='inline-flex gap-1 bg-surface-dark/50 rounded px-1.5 py-0.5'>
          <span className='text-ink-muted'>大{ouL}</span>
          {G(ouO, 'text-pitch-400')}{G(ouU, 'text-ember-400')}
        </span>
      </div>}
    </div>
  )
}

// ═══ 单场卡片 ═══
function MatchCard({ fx, now, onAnalyze }: { fx: FixtureEntry; now: number; onAnalyze?: (home: string, away: string, sportKey?: string, odds?: { h: number; d: number; a: number }, liveScore?: { homeGoals: number; awayGoals: number; elapsed?: number }) => void }) {
  const { live, finished, pending, label } = stateOf(fx, now)
  // 比分必须双方都有有效数值才显示, 否则显示 vs (避免 0-0 误导)
  const hasScore = typeof fx.score_home === 'number' && typeof fx.score_away === 'number'
  const sh = hasScore ? (fx.score_home as number) : 0
  const sa = hasScore ? (fx.score_away as number) : 0
  const homeLead = live && hasScore && sh > sa
  const awayLead = live && hasScore && sa > sh
  // 只在 live/finished 且有真实比分时才显示比分
  const showScore = (live || finished) && hasScore
  const timeLabel = live ? label : finished ? '已结束' : pending ? '状态待定' : `${fmtGMT8(fx.commence_time)} 开赛`
  return (
    <div className={`rounded-lg border px-3 py-2 transition-colors duration-150 ${
      live
        ? 'border-field-500/20 bg-field-500/[0.04]'
        : finished
          ? 'border-frost-500/10 bg-surface-dark/30'
          : 'border-surface-border/40 bg-transparent hover:bg-surface-dark/20'
    }`}>
      {/* 头部: 状态徽章 + 联赛 + 阶段/时间 (紧凑一行) */}
      <div className='flex items-center gap-2 flex-wrap mb-1'>
        <StatusBadge fx={fx} now={now} />
        <span className='text-xs text-ink-muted max-w-[140px] truncate' title={fx.league || ''}>{fx.league || '其他'}</span>
        <span className='text-[10px] text-ink-muted ml-auto font-mono'>{timeLabel}</span>
      </div>

      {/* 比分主体 / 对阵 (密度提升: 字号 22→18, 间距收紧) */}
      <div className='flex items-center gap-2'>
        <span className={`flex-1 text-right text-sm font-bold truncate ${homeLead ? 'text-field-300' : 'text-ink-primary'}`}>{fx.home}</span>
        {showScore ? (
          <span className='flex items-center gap-1 flex-shrink-0 min-w-[60px] justify-center'>
            <span className={`text-[18px] font-black font-mono tabular-nums ${homeLead ? 'text-field-300' : 'text-ink-primary'} ${live ? 'animate-pulse' : ''}`}>{sh}</span>
            <span className='text-ink-muted/40 text-xs'>-</span>
            <span className={`text-[18px] font-black font-mono tabular-nums ${awayLead ? 'text-field-300' : 'text-ink-primary'} ${live ? 'animate-pulse' : ''}`}>{sa}</span>
            {fx.score_inferred && <span className='ml-1 text-[8px] px-1 py-0.5 rounded bg-ember-500/15 text-ember-400 font-bold'>推断</span>}
          </span>
        ) : (
          <span className='text-[11px] text-ink-muted flex-shrink-0 min-w-[60px] text-center'>vs</span>
        )}
        <span className={`flex-1 text-left text-sm font-bold truncate ${awayLead ? 'text-field-300' : 'text-ink-primary'}`}>{fx.away}</span>
      </div>

      {/* 赔率 + 分析入口 */}
      <div className='mt-1 flex items-start justify-between gap-2'>
        <OddsPanel fx={fx} />
        {typeof fx.odds_h === 'number' && typeof fx.odds_d === 'number' && typeof fx.odds_a === 'number' && onAnalyze && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              onAnalyze(
                fx.home, fx.away, fx.sport_key || fx.league,
                { h: fx.odds_h as number, d: fx.odds_d as number, a: fx.odds_a as number },
                (typeof fx.score_home === 'number' && typeof fx.score_away === 'number')
                  ? { homeGoals: fx.score_home, awayGoals: fx.score_away, elapsed: typeof fx.match_minute === 'number' ? fx.match_minute : undefined }
                  : undefined
              )
            }}
            className='flex-shrink-0 px-2.5 py-1 rounded text-[11px] bg-gradient-to-r from-frost-500 to-frost-600 text-white font-bold hover:opacity-90 transition-opacity'
          >分析</button>
        )}
      </div>
    </div>
  )
}

// ═══ 主页面 ═══
type FilterMode = 'all' | 'live' | 'today' | 'finished'

const MATCHES_CACHE_KEY = 'sx_ls_matches'
const MATCHES_CACHE_TTL = 5 * 60_000

function loadMatchesCache(): FixtureEntry[] | null {
  try {
    const raw = localStorage.getItem(MATCHES_CACHE_KEY)
    if (!raw) return null
    const { d, ts } = JSON.parse(raw)
    return Date.now() - ts < MATCHES_CACHE_TTL ? d as FixtureEntry[] : null
  } catch { return null }
}
function saveMatchesCache(data: FixtureEntry[]) {
  try { localStorage.setItem(MATCHES_CACHE_KEY, JSON.stringify({ d: data.slice(0, 200), ts: Date.now() })) } catch {}
}

export default function LiveScoresPage() {
  const [matches, setMatches] = useState<FixtureEntry[]>(() => loadMatchesCache() || [])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const [filter, setFilter] = useState<FilterMode>('today')
  const [leagueFilter, setLeagueFilter] = useState<string>('')
  const [analyze, setAnalyze] = useState<{ home: string; away: string; sportKey?: string; odds?: { h: number; d: number; a: number }; liveScore?: { homeGoals: number; awayGoals: number; elapsed?: number } } | null>(null)
  const onAnalyze = useCallback((h: string, a: string, sportKey?: string, odds?: { h: number; d: number; a: number }, liveScore?: { homeGoals: number; awayGoals: number; elapsed?: number }) => {
    setAnalyze({ home: h, away: a, sportKey, odds, liveScore })
  }, [])

  // 首次加载全量 (走 feed, 与联赛赛程页同源)
  const fetchAll = useCallback(async () => {
    try {
      const res = await leagueScheduleService.getLeagues()
      const d = (res.data as any)?.data || res.data
      const cats = d?.categories || []
      const leagues: { name: string; sport_key: string }[] = []
      for (const cat of cats) {
        for (const lg of cat.leagues || []) {
          if (lg.fixture_count > 0) leagues.push({ name: lg.name, sport_key: lg.sport_key })
        }
      }
      // 并行抓取 (上限 20 个联赛, 与 LeagueSchedule 一致)
      const toFetch = leagues.slice(0, 20)
      const results = await Promise.all(toFetch.map(lg =>
        leagueScheduleService.getFixtures(lg.sport_key)
          .then(r2 => {
            const d2 = (r2.data as any)?.data || r2.data
            return ((d2?.fixtures || []) as FixtureEntry[]).map(f => ({ ...f, league: f.league || lg.name }))
          }).catch(() => [] as FixtureEntry[])
      ))
      const all = results.flat().filter(f => !FAKE_LEAGUE.test(f.league || ''))
      // 排序: 进行中置顶(state 降序) → 未开赛(开赛时间升序) → 已结束(开赛时间降序)
      all.sort((a, b) => {
        const sa = Number(a.match_state ?? 0), sb = Number(b.match_state ?? 0)
        const la = sa > 0 ? 2 : sa < 0 ? 0 : 1
        const lb = sb > 0 ? 2 : sb < 0 ? 0 : 1
        if (la !== lb) return lb - la
        const ta = new Date(a.commence_time).getTime()
        const tb = new Date(b.commence_time).getTime()
        return la === 0 ? tb - ta : ta - tb  // 已结束取最近; 其余取最早
      })
saveMatchesCache(all)
      setMatches(all)
      setError('')
      setUpdatedAt(Date.now())
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '未知错误')
    } finally {
      setLoading(false)
    }
  }, [])

  // 首次 + 30s 全量刷新 (feed 有 60s 缓存, 30s 轮询足够)
  useEffect(() => {
    fetchAll()
    const id = setInterval(fetchAll, 30000)
    return () => clearInterval(id)
  }, [fetchAll])

  // 5s 轻量比分轮询: 只在有进行中比赛时, 走 /api/live-scores 合并最新比分
  // (后台采集线程若没跑则返回空 → 静默跳过, 不影响主流程)
  const liveKey = matches.some(f => stateOf(f, Date.now()).live)
  useEffect(() => {
    if (!liveKey) return
    const id = setInterval(async () => {
      try {
        const res = await liveScoreService.getLiveMatches(50)
        const arr = (res.data as any)?.data?.matches as any[] | undefined
        if (!arr || arr.length === 0) return
        const key = (h: string, a: string) => `${h}|${a}`
        const map: Map<string, any> = new Map(arr.map((m: any) => [key(m.home, m.away), m] as const))
        setMatches(prev => prev.map(f => {
          const m = map.get(key(f.home, f.away))
          if (!m) return f
          if (f.score_home === m.score_home && f.score_away === m.score_away
              && f.match_minute === m.match_minute) return f
          return { ...f, score_home: m.score_home, score_away: m.score_away,
                   match_minute: m.match_minute, match_state: m.mststi ?? f.match_state,
                   odds_h: m.odds_h, odds_d: m.odds_d, odds_a: m.odds_a,
                   opening_h: m.opening_h, opening_d: m.opening_d, opening_a: m.opening_a,
                   ah_line: m.ah_line, ah_home: m.ah_home, ah_away: m.ah_away,
                   ah_op_home: m.ah_op_home, ah_op_away: m.ah_op_away,
                   ou_line: m.ou_line, ou_over: m.ou_over, ou_under: m.ou_under,
                   ou_op_over: m.ou_op_over, ou_op_under: m.ou_op_under,
                   score_inferred: m.score_inferred }
        }))
      } catch { /* 静默 */ }
    }, 5000)
    return () => clearInterval(id)
  }, [liveKey])

  // 1s 实时时钟 (倒计时/开赛同步)
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  // 联赛列表 (去重, 按场数降序)
  const leagueOptions = useMemo(() => {
    const map = new Map<string, number>()
    for (const m of matches) {
      const k = m.league || '其他'
      map.set(k, (map.get(k) || 0) + 1)
    }
    return Array.from(map.entries()).sort((a, b) => b[1] - a[1])
  }, [matches])

  // 今日议程判定 (GMT+8, 用标准 timeZone 选项)
  const todayKey = useMemo(() => {
    return new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' }) // YYYY-MM-DD
  }, [updatedAt])
  const isToday = (iso: string) => {
    try {
      const day = new Date(iso).toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' })
      return day === todayKey
    } catch { return false }
  }

  // 过滤 (用增强后的 stateOf, 兜底 feed 状态滞后)
  const visible = useMemo(() => matches.filter(f => {
    const { live, finished } = stateOf(f, now)
    if (filter === 'live' && !live) return false
    if (filter === 'finished' && !finished) return false
    if (filter === 'today' && !live && !isToday(f.commence_time)) return false
    if (leagueFilter && (f.league || '其他') !== leagueFilter) return false
    return true
  }), [matches, filter, leagueFilter, now])

  const liveCount = matches.filter(f => stateOf(f, now).live).length
  const finishedCount = matches.filter(f => stateOf(f, now).finished).length
  const todayCount = matches.filter(f => isToday(f.commence_time)).length
  const leagueCount = leagueOptions.length

  if (loading && matches.length === 0) {
    return (
      <div className='flex items-center justify-center h-full min-h-[40vh] text-ink-secondary text-sm'>
        加载实时比分中…
      </div>
    )
  }
  if (error && matches.length === 0) {
    return (
      <div className='flex flex-col items-center justify-center h-full min-h-[40vh] gap-3'>
        <p className='text-ember-400 text-sm'>实时比分获取失败，请稍后重试</p>
        <p className='text-ink-muted text-xs'>{error}</p>
        <button onClick={fetchAll} className='px-4 py-1.5 rounded bg-surface-border/30 text-ink-secondary text-xs hover:bg-surface-border/50 transition-colors'>
          重新加载
        </button>
      </div>
    )
  }

  return (
    <div className='flex flex-col bg-surface-canvas overflow-hidden' style={{ height: 'calc(100vh - 104px)' }}>
      {/* 顶部: 标题 + 统计 + 过滤 (固定, 不滚动) */}
      {/* 顶部: 标题 + 统计 + 过滤 (固定, 不滚动) */}
      <div className='flex-shrink-0 bg-surface-panel border-b border-surface-border px-4 py-3'>
        {/* 标题 + 统计 (统一 PageHeader) */}
        <PageHeader
          title="实时比分"
          subtitle={`跨全部联赛 · 30s 刷新${updatedAt ? ` · ${new Date(updatedAt).toLocaleTimeString('zh-CN', { hour12: false })}` : ''}`}
          metrics={[
            { label: '进行中', value: liveCount, accent: 'field' },
            { label: '今日', value: todayCount },
            { label: '已结束', value: finishedCount, accent: 'frost' },
          ]}
        />

        {/* 状态过滤 chips + 时钟 */}
        <div className='flex items-center gap-1.5 mb-2'>
          {([['today', '今日'], ['live', '进行中'], ['finished', '已结束'], ['all', '全部']] as [FilterMode, string][]).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setFilter(key)}
              className={`px-2.5 py-1 rounded text-[11px] transition-colors flex-shrink-0 ${
                filter === key
                  ? 'bg-gradient-to-r from-frost-500 to-frost-600 text-white font-bold'
                  : 'bg-surface-hover text-ink-secondary hover:text-white border border-surface-border'
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {/* 联赛筛选 (横向滚动, 不换行避免高度跳动) */}
        {leagueOptions.length > 0 && (
          <div className='flex gap-1.5 overflow-x-auto pb-1' style={{ scrollbarWidth: 'thin' }}>
            <button
              onClick={() => setLeagueFilter('')}
              className={`px-2 py-0.5 rounded text-[10px] transition-colors flex-shrink-0 ${
                !leagueFilter ? 'bg-accent/20 text-accent font-bold' : 'bg-surface-hover text-ink-muted hover:text-white'
              }`}
            >
              全部联赛
            </button>
            {leagueOptions.map(([name, cnt]) => (
              <button
                key={name}
                onClick={() => setLeagueFilter(leagueFilter === name ? '' : name)}
                className={`px-2 py-0.5 rounded text-[10px] transition-colors flex items-center gap-1 flex-shrink-0 ${
                  leagueFilter === name ? 'bg-accent/20 text-accent font-bold' : 'bg-surface-hover text-ink-muted hover:text-white'
                }`}
                title={name}
              >
                <span className='max-w-[120px] truncate'>{name}</span>
                <span className='text-[9px] opacity-60'>{cnt}</span>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* 比赛列表 (可滚动) */}
      <div className='flex-1 overflow-y-auto px-4 py-3'>
        {visible.length === 0 ? (
          <div className='flex flex-col items-center justify-center py-16 text-center'>
            <div className='w-12 h-12 rounded-full bg-surface-hover border border-surface-border flex items-center justify-center mb-3'>
              <span className='text-ink-muted text-[20px]'>⚽</span>
            </div>
            <p className='text-[12px] text-ink-secondary'>
              {filter === 'live'
                ? '当前没有进行中的比赛'
                : filter === 'today'
                  ? '今日暂无赛程'
                  : filter === 'finished'
                    ? '暂无已结束比赛数据'
                    : '暂无比赛数据'}
            </p>
            <p className='text-[10px] text-ink-muted mt-1'>
              {filter === 'live'
                ? '比赛开打后会自动出现在这里 · 可切换"今日"查看赛程'
                : '切换其他筛选或稍后刷新'}
            </p>
          </div>
        ) : (
          <>
            <div className='text-[10px] text-ink-muted mb-2'>共 {visible.length} 场</div>
            <div className='space-y-2'>
              {visible.map(f => <MatchCard key={f.id || `${f.home}-${f.away}`} fx={f} now={now} onAnalyze={onAnalyze} />)}
            </div>
          </>
        )}
        <div className='mt-6 text-[10px] text-ink-disabled text-center'>
          数据来自乐鱼/雷速实时接口 · 与联赛赛程同源 · 仅供分析参考, 非投注建议
        </div>
      </div>
      {analyze && (
        <MatchAnalysisModal
          home={analyze.home} away={analyze.away} sportKey={analyze.sportKey || 'soccer'}
          odds={analyze.odds} liveScore={analyze.liveScore}
          onClose={() => setAnalyze(null)}
        />
      )}
    </div>
  )
}

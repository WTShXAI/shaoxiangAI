import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { leagueScheduleService, liveScoreService, isObscureLeague } from '@/services/api'
import MatchAnalysisModal from './MatchAnalysisModal'
import PageHeader from '@/components/layout/PageHeader'
import ApiError from '@/components/shared/ApiError'
import Skeleton from '@/components/shared/Skeleton'
import type { FixtureEntry } from '@/types'
import { stateOf, FAKE_LEAGUE, liveToFixture } from './fixtureUtils'
import { MatchCard } from './components'

// ═══ 主页面 ═══
type FilterMode = 'all' | 'live' | 'halftime' | 'today' | 'finished' | 'upcoming'

const MATCHES_CACHE_KEY = 'sx_ls_matches'
const MATCHES_CACHE_TTL = 5 * 60_000
const MATCHES_CACHE_SCHEMA_VER = 2  // 状态逻辑大改后 bump, 旧缓存自动废弃

function loadMatchesCache(): FixtureEntry[] | null {
  try {
    const raw = localStorage.getItem(MATCHES_CACHE_KEY)
    if (!raw) return null
    const { d, ts, v } = JSON.parse(raw)
    if (v !== MATCHES_CACHE_SCHEMA_VER) return null
    return Date.now() - ts < MATCHES_CACHE_TTL ? d as FixtureEntry[] : null
  } catch { return null }
}
function saveMatchesCache(data: FixtureEntry[]) {
  try { localStorage.setItem(MATCHES_CACHE_KEY, JSON.stringify({ d: data.slice(0, 600), ts: Date.now(), v: MATCHES_CACHE_SCHEMA_VER })) } catch {}
}

export default function LiveScoresPage() {
  const [matches, setMatches] = useState<FixtureEntry[]>(() => loadMatchesCache() || [])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const [filter, setFilter] = useState<FilterMode>('all')
  const [leagueFilter, setLeagueFilter] = useState<string>('')
  const [leagueSearch, setLeagueSearch] = useState('')
  const [analyze, setAnalyze] = useState<{ home: string; away: string; sportKey?: string; odds?: { h: number; d: number; a: number }; handicap?: { ah_line?: number | string; ah_home?: number; ah_away?: number; ou_line?: number | string; ou_over?: number; ou_under?: number }; liveScore?: { homeGoals: number; awayGoals: number; elapsed?: number } } | null>(null)
  const onAnalyze = useCallback((h: string, a: string, sportKey?: string, odds?: { h: number; d: number; a: number }, handicap?: { ah_line?: number | string; ah_home?: number; ah_away?: number; ou_line?: number | string; ou_over?: number; ou_under?: number }, liveScore?: { homeGoals: number; awayGoals: number; elapsed?: number }) => {
    setAnalyze({ home: h, away: a, sportKey, odds, handicap, liveScore })
  }, [])

  // 首次加载全量 (走 feed, 与联赛赛程页同源)
  const fetchAll = useCallback(async (signal?: AbortSignal) => {
    try {
      // 全量赛程聚合端点: 一次返回所有联赛 fixtures (绕开前端逐联赛并发抓取触发的全局限流 120/min,
      // 否则 days≥7 时 233 个请求远超限制, 大量联赛 fixtures 被 429 丢弃 → 赛事不全)
      const res = await leagueScheduleService.getAllFixtures(7, signal)
      const d = (res.data as any)?.data || res.data
      const all: FixtureEntry[] = ((d?.fixtures || []) as FixtureEntry[])
        .map(f => ({ ...f, league: f.league || f.sport_key }))
        .filter(f => !FAKE_LEAGUE.test(f.league || '') && !isObscureLeague(f.league, f.sport_key))
      // ── 合并 /api/live-scores 的真实进行中比赛 ──
      // feed 的 fixtures 对进行中比赛只给 match_state+分钟, 比分(score_home/away)为 NULL,
      // 真实比分必须来自 /api/live-scores。这里把实时比分/分钟/状态 MERGE 进已存在的比赛
      // (修正 feed 的 NULL 比分), 不在骨架里的才新增, 让"实时比分"页名副其实。
      try {
        const lr = await liveScoreService.getLiveMatches(8000, signal)
        const liveArr = (lr.data as any)?.data?.matches as any[] | undefined
        if (liveArr && liveArr.length) {
          const existMap = new Map<string, FixtureEntry>(all.map(f => [`${f.home}|${f.away}`, f]))
          for (const m of liveArr) {
            if (!m || m.mststi == null) continue
            const st = Number(m.mststi)
            if (st < 1 || st >= 6) continue            // 只处理进行中(含中场), 避免污染赛程/异常态
            const key = `${m.home}|${m.away}`
            const ex = existMap.get(key)
            if (ex) {
              // 合并实时比分/分钟/状态 (feed 的 NULL 比分在此被修正)
              if (m.score_home != null) ex.score_home = m.score_home
              if (m.score_away != null) ex.score_away = m.score_away
              if (m.match_minute != null) ex.match_minute = m.match_minute
              ex.match_state = m.mststi
              ex.score_inferred = m.score_inferred ?? ex.score_inferred
            } else {
              all.push(liveToFixture(m))
              existMap.set(key, m)
            }
          }
        }
      } catch { /* 静默: 采集线程未运行时不影响主流程 */ }
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
      // 取消(组件卸载)不报错
      if (e instanceof Error && e.name === 'CanceledError') return
      setError(e instanceof Error ? e.message : '未知错误')
    } finally {
      setLoading(false)
    }
  }, [])

  // 首次 + 30s 全量刷新 (feed 有 60s 缓存, 30s 轮询足够)
  useEffect(() => {
    const ctrl = new AbortController()
    fetchAll(ctrl.signal)
    const id = setInterval(() => fetchAll(ctrl.signal), 30000)
    return () => { clearInterval(id); ctrl.abort() }
  }, [fetchAll])

  // 5s 轻量比分轮询: 无条件运行, 走 /api/live-scores 合并最新比分
  // (与联赛赛程页一致: 不依赖 liveKey 门控, 否则采集线程未跑/初始无 live 时永远不刷新)
  // 后台采集线程若没跑则返回空 → 静默跳过, 不影响主流程。
  useEffect(() => {
    const ctrl = new AbortController()
    const id = setInterval(async () => {
      try {
        const res = await liveScoreService.getLiveMatches(5000, ctrl.signal)
        const arr = (res.data as any)?.data?.matches as any[] | undefined
        if (!arr || arr.length === 0) return
        const key = (h: string, a: string) => `${h}|${a}`
        const map: Map<string, any> = new Map(arr.map((m: any) => [key(m.home, m.away), m] as const))
        setMatches(prev => prev.map(f => {
          const m = map.get(key(f.home, f.away))
          if (!m) return f
          const currentSt = Number(f.match_state ?? 0)
          const incomingSt = Number(m.mststi ?? currentSt)
          // 防御 live feed 污染已结束比赛: 当前已是 finished(state<0) 时, 不再被改回 live(state>0)
          const shouldUpdateState = !(currentSt < 0 && incomingSt > 0)
          // 仅比分/分钟/状态变化才更新: 赔率/盘口继续由30s全量fetchAll刷新,
          // 避免 feed 缺字段时把 odds/ah/ou 覆盖成 None (回归防护)。
          if (f.score_home === m.score_home && f.score_away === m.score_away
              && f.match_minute === m.match_minute
              && (!shouldUpdateState || f.match_state === (m.mststi ?? f.match_state))) return f
          return { ...f, score_home: m.score_home, score_away: m.score_away,
                   match_minute: m.match_minute,
                   match_state: shouldUpdateState ? (m.mststi ?? f.match_state) : f.match_state,
                   score_inferred: m.score_inferred ?? f.score_inferred }
        }))
      } catch { /* 静默: 已取消/采集线程未运行均不影响主流程 */ }
    }, 5000)
    return () => { clearInterval(id); ctrl.abort() }
  }, [])

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

  // 联赛搜索过滤 (2026-08-01 前端优化: 联赛数多时输入关键词快速定位, 免横向长滚)
  const filteredLeagueOptions = useMemo(() => {
    const q = leagueSearch.trim().toLowerCase()
    if (!q) return leagueOptions
    return leagueOptions.filter(([name]) => name.toLowerCase().includes(q))
  }, [leagueOptions, leagueSearch])

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
    const { live, finished, halftime } = stateOf(f, now)
    if (filter === 'live' && !live) return false
    if (filter === 'finished' && !finished) return false
    if (filter === 'upcoming' && (live || finished || halftime)) return false
    if (filter === 'today' && !live && !halftime && !isToday(f.commence_time)) return false
    if (leagueFilter && (f.league || '其他') !== leagueFilter) return false
    return true
  }), [matches, filter, leagueFilter, now])

  // 分组渲染: 仅 "全部"/"今日" 视图下把 进行中 / 中场休息 / 未开赛 / 已结束 拆成独立区块
  const groups = useMemo(() => {
    if (filter !== 'all' && filter !== 'today') return null
    const live: any[] = []
    const halftime: any[] = []
    const upcoming: any[] = []
    const finished: any[] = []
    for (const f of visible) {
      const s = stateOf(f, now)
      if (s.live) live.push(f)
      else if (s.halftime) halftime.push(f)
      else if (s.finished) finished.push(f)
      else upcoming.push(f)
    }
    return [
      { key: 'live', title: '进行中', items: live },
      { key: 'halftime', title: '中场休息', items: halftime },
      { key: 'upcoming', title: '未开赛', items: upcoming },
      { key: 'finished', title: '已结束', items: finished },
    ]
  }, [visible, filter, now])

  const liveCount = matches.filter(f => stateOf(f, now).live).length
  const halftimeCount = matches.filter(f => stateOf(f, now).halftime).length
  const finishedCount = matches.filter(f => stateOf(f, now).finished).length
  const upcomingCount = matches.filter(f => { const s = stateOf(f, now); return !s.live && !s.halftime && !s.finished }).length
  const todayCount = matches.filter(f => isToday(f.commence_time)).length
  const leagueCount = leagueOptions.length

  if (loading && matches.length === 0) {
    return (
      <div className="max-w-[1200px] mx-auto space-y-3">
        <Skeleton variant="line" className="w-1/2" />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <Skeleton variant="card" />
          <Skeleton variant="card" />
          <Skeleton variant="card" />
          <Skeleton variant="card" />
        </div>
      </div>
    )
  }
  if (error && matches.length === 0) {
    return <ApiError message={error} onRetry={() => fetchAll()} />
  }

  return (
    <div className='flex flex-col bg-surface-canvas overflow-hidden' style={{ height: 'calc(100vh - 104px)' }}>
      {/* 顶部: 标题 + 统计 + 过滤 (固定, 不滚动) */}
      <div className='flex-shrink-0 bg-surface-panel border-b border-surface-border px-4 py-3'>
        {/* 标题 + 统计 (统一 PageHeader) */}
        <PageHeader
          title="实时比分"
          subtitle={`跨全部联赛 · 30s 刷新${updatedAt ? ` · ${new Date(updatedAt).toLocaleTimeString('zh-CN', { hour12: false })}` : ''}`}
          metrics={[
            { label: '进行中', value: liveCount, accent: 'field' },
            { label: '中场休息', value: halftimeCount },
            { label: '未开赛', value: upcomingCount },
            { label: '今日', value: todayCount },
            { label: '已结束', value: finishedCount, accent: 'frost' },
          ]}
        />

        {/* 状态过滤 chips + 时钟 */}
        <div className='flex items-center gap-1.5 mb-2'>
          {([['today', '今日'], ['live', '进行中'], ['halftime', '中场休息'], ['upcoming', '未开赛'], ['finished', '已结束'], ['all', '全部']] as [FilterMode, string][]).map(([key, label]) => (
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
          <div className='flex gap-1.5 overflow-x-auto pb-1 items-center' style={{ scrollbarWidth: 'thin' }}>
            <input
              value={leagueSearch}
              onChange={(e) => setLeagueSearch(e.target.value)}
              placeholder='搜联赛'
              className='px-2 py-0.5 rounded text-[10px] bg-surface-hover text-white placeholder-ink-muted w-20 flex-shrink-0 outline-none focus:ring-1 focus:ring-accent/50'
            />
            <button
              onClick={() => setLeagueFilter('')}
              className={`px-2 py-0.5 rounded text-[10px] transition-colors flex-shrink-0 ${
                !leagueFilter ? 'bg-accent/20 text-accent font-bold' : 'bg-surface-hover text-ink-muted hover:text-white'
              }`}
            >
              全部联赛
            </button>
            {filteredLeagueOptions.map(([name, cnt]) => (
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
                : filter === 'upcoming'
                  ? '暂无可查看的未开赛比赛'
                  : filter === 'today'
                    ? '今日暂无赛程'
                    : filter === 'finished'
                      ? '暂无已结束比赛数据'
                      : '暂无比赛数据'}
            </p>
            <p className='text-[10px] text-ink-muted mt-1'>
              {filter === 'live'
                ? '比赛开打后会自动出现在这里 · 可切换"今日"查看赛程'
                : filter === 'upcoming'
                  ? '已开赛或已结束的比赛不显示在此筛选'
                  : '切换其他筛选或稍后刷新'}
            </p>
          </div>
        ) : (
          groups ? (
            <div className='space-y-4'>
              {groups.map(g => g.items.length === 0 ? null : (
                <section key={g.key}>
                  <div className='flex items-center gap-2 mb-2'>
                    <span className='text-[11px] font-bold text-ink-secondary'>{g.title}</span>
                    <span className='text-[10px] text-ink-muted bg-surface-hover rounded px-1.5 py-0.5'>{g.items.length}</span>
                    <span className='flex-1 h-px bg-surface-border' />
                  </div>
                  <div className='space-y-2'>
                    {g.items.map(f => <MatchCard key={f.id || `${f.home}-${f.away}`} fx={f} now={now} onAnalyze={onAnalyze} />)}
                  </div>
                </section>
              ))}
            </div>
          ) : (
            <>
              <div className='text-[10px] text-ink-muted mb-2'>共 {visible.length} 场</div>
              <div className='space-y-2'>
                {visible.map(f => <MatchCard key={f.id || `${f.home}-${f.away}`} fx={f} now={now} onAnalyze={onAnalyze} />)}
              </div>
            </>
          )
        )}
        <div className='mt-6 text-[10px] text-ink-disabled text-center'>
          数据来自乐鱼/雷速实时接口 · 与联赛赛程同源 · 仅供分析参考, 非投注建议
        </div>
      </div>
      {analyze && (
        <MatchAnalysisModal
          home={analyze.home} away={analyze.away} sportKey={analyze.sportKey || 'soccer'}
          odds={analyze.odds} handicap={analyze.handicap} liveScore={analyze.liveScore}
          onClose={() => setAnalyze(null)}
        />
      )}
    </div>
  )
}

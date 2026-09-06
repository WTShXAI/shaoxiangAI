// ═══ 赛程页 (tc87 风格复刻 2026-09-02) ═══
// 参考用户提供的 tc87.vip/matches 截图：水平日期标签 + 联赛分类芯片 + 行式列表 + 完场比分。
// 数据: /api/all-fixtures(全量赛程, 已含 opening 1X2/AH/OU) + /api/live-scores(实时比分 merge)。
// 点整行打开 MatchAnalysisModal 全链路分析。
import { useState, useEffect, useCallback, useMemo } from 'react'
import { leagueScheduleService, liveScoreService } from '@/services/api'
import MatchAnalysisModal from '@/pages/LiveScores/MatchAnalysisModal'
import PageHeader from '@/components/layout/PageHeader'
import ApiError from '@/components/shared/ApiError'
import Skeleton from '@/components/shared/Skeleton'
import EmptyState from '@/components/shared/EmptyState'
import TeamLogo from '@/components/shared/TeamLogo'
import type { FixtureEntry } from '@/types'
import { stateOf, FAKE_LEAGUE, liveToFixture } from '@/pages/LiveScores/fixtureUtils'

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

// 统一 Asia/Shanghai 的日期/时间格式
function inShanghai(d: Date): Date {
  return new Date(d.toLocaleString('en-US', { timeZone: 'Asia/Shanghai' }))
}
function shanghaiDateKey(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  return inShanghai(d).toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' })
}
function fmtTime(iso: string): string {
  if (!iso) return '--:--'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return '--:--'
  return inShanghai(d).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}
function fmtTabDate(isoDay: string): string {
  const d = new Date(isoDay + 'T00:00:00')
  const sh = inShanghai(d)
  const m = sh.getMonth() + 1
  const day = sh.getDate()
  return `${m}.${day} ${WEEKDAYS[sh.getDay()]}`
}

function generateDateTabs(now: number): string[] {
  // 以今天为中心，前后各 2 天（与 tc87 截图的 5 日条一致）
  const today = inShanghai(new Date(now))
  today.setHours(0, 0, 0, 0)
  const days: string[] = []
  for (let i = -2; i <= 2; i++) {
    const d = new Date(today)
    d.setDate(today.getDate() + i)
    days.push(d.toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' }))
  }
  return days
}

function StatusBadge({ s, minute }: { s: ReturnType<typeof stateOf>; minute?: number | null }) {
  if (s.finished) {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] text-ink-muted">
        已结束
      </span>
    )
  }
  if (s.live || s.halftime) {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] text-ember-400">
        <span className="w-1.5 h-1.5 rounded-full bg-ember-500 animate-pulse" />
        {typeof minute === 'number' ? `${minute}'` : '滚球中'}
      </span>
    )
  }
  return <span className="text-[11px] text-ink-muted">未开赛</span>
}

export default function SchedulePage() {
  const [matches, setMatches] = useState<FixtureEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const [now, setNow] = useState(() => Date.now())

  const dateTabs = useMemo(() => generateDateTabs(now), [now])
  const [selectedDate, setSelectedDate] = useState(dateTabs[2]) // 默认今天
  const [selectedLeague, setSelectedLeague] = useState<string>('全部')

  const [analyze, setAnalyze] = useState<{
    home: string; away: string; sportKey?: string
    league?: string; kickoff?: string; matchState?: string | number
    odds?: { h: number; d: number; a: number }
    handicap?: { ah_line?: number | string; ah_home?: number; ah_away?: number; ou_line?: number | string; ou_over?: number; ou_under?: number }
    liveScore?: { homeGoals?: number; awayGoals?: number; elapsed?: number }
    matchKey?: string
  } | null>(null)

  // 若日期标签因 now 变化而重算，保持选中今天
  useEffect(() => {
    setSelectedDate(dateTabs[2])
  }, [dateTabs])

  // ── 取数 (与 LiveScores 同源: 全量赛程 + 实时比分 merge) ──
  const fetchAll = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await leagueScheduleService.getAllFixtures(7, signal)
      const d = (res.data as any)?.data || res.data
      const all: FixtureEntry[] = ((d?.fixtures || []) as FixtureEntry[])
        .map(f => ({ ...f, league: f.league || f.sport_key }))
        .filter(f => !FAKE_LEAGUE.test(f.league || ''))
      // merge /api/live-scores 真实进行中比分/分钟/状态
      try {
        const lr = await liveScoreService.getLiveMatches(8000, signal)
        const liveArr = (lr.data as any)?.data?.matches as any[] | undefined
        if (liveArr && liveArr.length) {
          const existMap = new Map(all.map(f => [`${f.home}|${f.away}`, f]))
          for (const m of liveArr) {
            if (!m || m.mststi == null) continue
            const st = Number(m.mststi)
            if (st < 1 || st >= 6) continue
            const key = `${m.home}|${m.away}`
            const ex = existMap.get(key)
            if (ex) {
              ex.score_home = m.score_home ?? ex.score_home
              ex.score_away = m.score_away ?? ex.score_away
              ex.match_minute = m.match_minute ?? ex.match_minute
              ex.match_state = m.mststi ?? ex.match_state
              ex.liveConfirmed = true
            } else {
              all.push(liveToFixture(m))
            }
          }
        }
      } catch { /* live 合并失败不阻断赛程 */ }
      setMatches(all)
      setUpdatedAt(Date.now())
      setError('')
    } catch (e: any) {
      setError(e?.message || '赛程加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const ac = new AbortController()
    fetchAll(ac.signal)
    const t = setInterval(() => fetchAll(), 30000)
    const clock = setInterval(() => setNow(Date.now()), 30000)
    return () => { ac.abort(); clearInterval(t); clearInterval(clock) }
  }, [fetchAll])

  // ── 联赛芯片 ──
  const leagueChips = useMemo(() => {
    const counts = new Map<string, number>()
    for (const f of matches) {
      const l = f.league || '其他'
      counts.set(l, (counts.get(l) || 0) + 1)
    }
    const sorted = Array.from(counts.entries()).sort((a, b) => b[1] - a[1]).map(([l]) => l)
    return ['全部', ...sorted]
  }, [matches])

  // ── 过滤 ──
  const filtered = useMemo(() => {
    const base = matches
      .filter(f => {
        const d = shanghaiDateKey(f.commence_time)
        return d === selectedDate
      })
      .filter(f => selectedLeague === '全部' || (f.league || '其他') === selectedLeague)
    // 按开赛时间升序
    return [...base].sort((a, b) => (a.commence_time || '').localeCompare(b.commence_time || ''))
  }, [matches, selectedDate, selectedLeague])

  const onAnalyze = useCallback((f: FixtureEntry) => {
    const s = stateOf(f, now)
    setAnalyze({
      home: f.home,
      away: f.away,
      sportKey: f.sport_key,
      league: f.league || f.sport_key,
      kickoff: f.commence_time,
      matchState: f.match_state,
      odds: { h: f.odds_h ?? 0, d: f.odds_d ?? 0, a: f.odds_a ?? 0 },
      handicap: {
        ah_line: f.ah_line, ah_home: f.ah_home, ah_away: f.ah_away,
        ou_line: f.ou_line, ou_over: f.ou_over, ou_under: f.ou_under,
      },
      liveScore: s.finished || s.live || s.halftime
        ? {
            homeGoals: f.score_home ?? 0,
            awayGoals: f.score_away ?? 0,
            elapsed: s.live || s.halftime ? (typeof f.match_minute === 'number' ? f.match_minute : Number(f.match_minute) || 0) : undefined,
          }
        : undefined,
      matchKey: f.match_key,
    })
  }, [now])

  return (
    <div className="min-h-screen p-4 md:p-6">
      <PageHeader
        title="赛程"
        subtitle="完赛比分与在售赔率同步中 · 数据源 乐鱼/GQ 实时采集"
      />

      {/* 联赛分类芯片 (tc87 风格) */}
      <div className="mt-4 -mx-4 md:-mx-6 px-4 md:px-6">
        <div className="flex items-center gap-2 overflow-x-auto pb-2 scrollbar-thin scrollbar-thumb-surface-border">
          {leagueChips.map(l => (
            <button
              key={l}
              onClick={() => setSelectedLeague(l)}
              className={`flex-shrink-0 px-3.5 py-1.5 rounded-full text-[12px] font-medium border whitespace-nowrap transition-colors ${
                selectedLeague === l
                  ? 'bg-field-500/20 text-field-400 border-field-500/40'
                  : 'bg-surface-dark/40 text-ink-secondary border-surface-border/40 hover:border-surface-border/60'
              }`}
            >
              {l}
            </button>
          ))}
        </div>
      </div>

      {/* 日期标签 (tc87 风格) */}
      <div className="mt-3 flex items-center justify-between border-b border-surface-border/30 pb-0">
        <div className="flex items-center gap-1">
          {dateTabs.map(d => {
            const active = d === selectedDate
            return (
              <button
                key={d}
                onClick={() => setSelectedDate(d)}
                className={`relative flex flex-col items-center justify-center min-w-[72px] px-3 py-2.5 text-[13px] transition-colors ${
                  active ? 'text-field-400 font-bold' : 'text-ink-muted hover:text-ink-secondary'
                }`}
              >
                <span className="text-[12px]">{fmtTabDate(d)}</span>
                {active && <span className="absolute bottom-0 left-2 right-2 h-0.5 bg-field-500 rounded-t" />}
              </button>
            )
          })}
        </div>
        <div className="text-[12px] text-ink-muted pr-2">
          共 <span className="text-ember-400 font-bold">{filtered.length}</span> 场
        </div>
      </div>

      {/* 错误 / 加载 / 空态 */}
      {error && (
        <div className="mt-4">
          <ApiError message={error} onRetry={() => fetchAll()} />
        </div>
      )}
      {loading && (
        <div className="mt-4 space-y-2">
          {[0, 1, 2, 3].map(i => <Skeleton key={i} variant="card" />)}
        </div>
      )}
      {!loading && !error && filtered.length === 0 && (
        <div className="mt-10">
          <EmptyState title="暂无比赛" message={`${selectedDate} 当前筛选条件下没有比赛。`} />
        </div>
      )}

      {/* 行式比赛列表 (tc87 风格) */}
      {!loading && !error && filtered.length > 0 && (
        <div className="mt-2 rounded-xl border border-surface-border/20 overflow-hidden">
          {filtered.map((f, idx) => {
            const s = stateOf(f, now)
            const showScore = s.finished || s.live || s.halftime
            return (
              <div
                key={f.id || `${f.home}|${f.away}|${idx}`}
                onClick={() => onAnalyze(f)}
                className="group flex items-stretch cursor-pointer border-b border-surface-border/10 last:border-0 bg-surface-dark/20 hover:bg-surface-dark/40 transition-colors"
              >
                {/* 左侧：时间 + 联赛 */}
                <div className="w-[72px] md:w-20 flex flex-col justify-center items-start pl-3 md:pl-4 py-3 shrink-0">
                  <span className="text-[13px] font-semibold text-ink-primary">{fmtTime(f.commence_time)}</span>
                  <span className="text-[11px] text-ink-muted truncate w-full">{f.league || f.sport_key || '其他'}</span>
                </div>

                {/* 中间：主客 + 比分/VS */}
                <div className="flex-1 flex items-center justify-center gap-2 md:gap-4 py-3 px-2 min-w-0">
                  <div className="flex-1 flex items-center justify-end gap-2 md:gap-3 min-w-0">
                    <span className="text-[13px] md:text-[14px] font-medium text-ink-primary truncate text-right hidden sm:block">{f.home}</span>
                    <span className="text-[12px] font-medium text-ink-primary truncate text-right sm:hidden">{f.home}</span>
                    <TeamLogo name={f.home} />
                  </div>

                  <div className="w-[56px] md:w-[72px] text-center shrink-0">
                    {showScore ? (
                      <span className="text-[16px] md:text-[18px] font-bold font-mono text-ink-primary tracking-wider">
                        {f.score_home ?? 0}:{f.score_away ?? 0}
                      </span>
                    ) : (
                      <span className="text-[13px] font-medium text-ink-muted">VS</span>
                    )}
                  </div>

                  <div className="flex-1 flex items-center justify-start gap-2 md:gap-3 min-w-0">
                    <TeamLogo name={f.away} />
                    <span className="text-[13px] md:text-[14px] font-medium text-ink-primary truncate hidden sm:block">{f.away}</span>
                    <span className="text-[12px] font-medium text-ink-primary truncate sm:hidden">{f.away}</span>
                  </div>
                </div>

                {/* 右侧：状态 + 分析入口 */}
                <div className="w-16 md:w-20 flex flex-col justify-center items-end pr-3 md:pr-4 py-3 shrink-0">
                  <StatusBadge s={s} minute={typeof f.match_minute === 'number' ? f.match_minute : Number(f.match_minute) || null} />
                  <span className="mt-1 text-[10px] text-field-500/80 opacity-0 group-hover:opacity-100 transition-opacity">分析</span>
                </div>
              </div>
            )
          })}
        </div>
      )}

      <div className="mt-3 text-[10px] text-ink-muted text-right">
        {updatedAt ? `更新 ${new Date(updatedAt).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit' })}` : '加载中'}
      </div>

      {/* 分析弹窗 */}
      {analyze && (
        <MatchAnalysisModal
          home={analyze.home}
          away={analyze.away}
          sportKey={analyze.sportKey || ''}
          league={analyze.league}
          kickoff={analyze.kickoff}
          matchState={analyze.matchState}
          odds={analyze.odds}
          handicap={analyze.handicap}
          liveScore={analyze.liveScore}
          matchKey={analyze.matchKey}
          onClose={() => setAnalyze(null)}
        />
      )}
    </div>
  )
}

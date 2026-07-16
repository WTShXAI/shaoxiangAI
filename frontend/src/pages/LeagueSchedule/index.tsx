import { useState, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { leagueScheduleService } from '@/services/api'
import type {
  LeaguesResponse,
  LeagueCatalogEntry,
  FixtureEntry,
} from '@/types'
import MatchAnalysisModal from './MatchAnalysisModal'

// ── 安全日期/时间格式化 ──
function fmtDate(iso: string): string {
  try {
    const d = new Date(iso)
    if (isNaN(d.getTime())) return '--'
    return d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
  } catch { return '--' }
}
function fmtTime(iso: string): string {
  try {
    const d = new Date(iso)
    if (isNaN(d.getTime())) return '--:--'
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  } catch { return '--:--' }
}
function fmtGMT8(iso: string): string {
  try {
    const d = new Date(iso)
    if (isNaN(d.getTime())) return ''
    // 显示 GMT+8 时间
    const utc = d.getTime() + d.getTimezoneOffset() * 60000
    const gmt8 = new Date(utc + 8 * 3600000)
    return gmt8.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  } catch { return '' }
}
function fmtOdds(v: number | undefined): string {
  return typeof v === 'number' && !isNaN(v) ? v.toFixed(2) : '-'
}
function fmtCountdown(ms: number | undefined): string {
  if (typeof ms !== 'number') return ''
  if (ms <= 0) return '已开赛'
  const m = Math.floor(ms / 60000)
  if (m < 60) return `${m}'`
  const h = Math.floor(m / 60)
  return `${h}h${m % 60}m`
}

// ============================================
// 子组件：联赛目录项
// ============================================
function LeagueRow({
  entry, active, onClick,
}: { entry: LeagueCatalogEntry; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`w-full flex items-center justify-between gap-2 px-3 py-2 rounded-lg text-left transition-colors duration-150 ${
        active
          ? 'bg-field-500/15 border border-field-500/25 text-field-300'
          : 'border border-transparent text-white/60 hover:text-white/90 hover:bg-white/[0.04]'
      }`}
    >
      <span className="text-sm font-medium truncate">{entry.name}</span>
      <span
        className={`text-[9px] px-1.5 py-0.5 rounded flex-shrink-0 ${
          entry.available
            ? 'bg-pitch-500/15 text-pitch-400/80'
            : 'bg-white/5 text-white/30'
        }`}
      >
        {entry.available ? '已接入' : '未接入'}
      </span>
    </button>
  )
}

// ============================================
// 子组件：单场比赛 - 6 列赔率卡片 (平台风格)
// ============================================

// 一列 (3 行 / 2 行数据)
function MarketColumn({
  title, line, homeLabel, drawLabel, awayLabel,
  home, draw, away, accent, kickoffMs, isLive,
}: {
  title: string
  line?: string
  homeLabel: string   // "主" / "主" / "Over"
  drawLabel?: string  // "平" / "Over" — 可选
  awayLabel: string   // "客" / "Under"
  home?: number
  draw?: number
  away?: number
  accent?: 'home' | 'fav' | 'over'
  kickoffMs?: number
  isLive?: boolean
}) {
  const accentColor = accent === 'home' ? 'text-frost-300' : accent === 'over' ? 'text-pitch-300' : 'text-white/90'
  return (
    <div className="min-w-[110px] flex-shrink-0 border-l border-white/[0.04] first:border-l-0">
      {/* 列头 */}
      <div className="px-2 py-2 text-center border-b border-white/[0.04]">
        <div className="text-[10px] font-bold text-white/60 tracking-wider uppercase">{title}</div>
        {line && <div className="text-[9px] text-frost-400 font-bold mt-0.5">{line}</div>}
      </div>
      {/* 数据行 */}
      <div className="px-2 py-1.5 text-center">
        <div className="text-[9px] text-frost-400 font-bold mb-0.5">{homeLabel}</div>
        <div className={`text-[15px] font-display font-black ${typeof home === 'number' ? accentColor : 'text-white/20'}`}>
          {fmtOdds(home)}
        </div>
      </div>
      {drawLabel !== undefined && (
        <div className="px-2 py-1.5 text-center border-t border-white/[0.03]">
          <div className="text-[9px] text-frost-400 font-bold mb-0.5">{drawLabel}</div>
          <div className={`text-[15px] font-display font-black ${typeof draw === 'number' ? 'text-ember-400' : 'text-white/20'}`}>
            {fmtOdds(draw)}
          </div>
        </div>
      )}
      <div className="px-2 py-1.5 text-center border-t border-white/[0.03]">
        <div className="text-[9px] text-frost-400 font-bold mb-0.5">{awayLabel}</div>
        <div className={`text-[15px] font-display font-black ${typeof away === 'number' ? accentColor : 'text-white/20'}`}>
          {fmtOdds(away)}
        </div>
      </div>
    </div>
  )
}

// 比赛主卡 (左右主队 + 6 列赔率) — 可点击触发分析
function MatchOddsCard({ fx, sportKey, onAnalyze }: {
  fx: FixtureEntry
  sportKey: string
  onAnalyze: (home: string, away: string, sportKey: string, odds?: { h: number; d: number; a: number }) => void
}) {
  const isLive = typeof fx.score_home === 'number' && (fx.score_home ?? 0) + (fx.score_away ?? 0) > 0
  const gmt8Time = fmtGMT8(fx.commence_time)
  const countdown = fmtCountdown(fx.kickoff_ms)

  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      onClick={() => onAnalyze(fx.home, fx.away, sportKey,
        (typeof fx.odds_h === 'number' && typeof fx.odds_d === 'number' && typeof fx.odds_a === 'number')
          ? { h: fx.odds_h, d: fx.odds_d, a: fx.odds_a } : undefined)}
      className="card overflow-hidden mb-3 cursor-pointer hover:border-accent/30 transition-colors duration-150 group"
    >
      {/* 顶部: 联赛 + 状态 + 分析按钮 */}
      <div className="px-4 py-2 bg-white/[0.02] border-b border-white/[0.04] flex items-center justify-between">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[10px] font-bold text-white/50 truncate">{fx.league || '其他'}</span>
          {isLive && (
            <span className="text-[9px] px-1.5 py-0.5 bg-ember-500/15 text-ember-400 rounded font-bold animate-pulse">
              ● 进行中 {fx.score_home}-{fx.score_away}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 text-[10px] text-white/40">
          {countdown && <span className="text-pitch-400 font-bold font-mono">{countdown}</span>}
          {gmt8Time && <span className="font-mono">{gmt8Time} GMT+8</span>}
          <span className="opacity-0 group-hover:opacity-100 transition-opacity text-accent font-medium bg-accent/10 px-2 py-0.5 rounded">
            🎯 分析
          </span>
        </div>
      </div>

      {/* 主体: 主队信息 + 6 列赔率 */}
      <div className="flex items-stretch">
        {/* 左: 主客队 */}
        <div className="flex-shrink-0 w-[180px] px-3 py-3 border-r border-white/[0.04]">
          <div className="flex items-center gap-2 mb-2">
            <span className="w-1.5 h-1.5 rounded-full bg-field-500 flex-shrink-0" />
            <span className="text-sm font-bold text-white truncate">{fx.home}</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="w-1.5 h-1.5 rounded-full bg-ember-500 flex-shrink-0" />
            <span className="text-sm font-bold text-white truncate">{fx.away}</span>
          </div>
          {isLive && (
            <div className="mt-2 text-[9px] text-ember-400/80 font-bold font-mono">{fx.match_minute}′</div>
          )}
        </div>

        {/* 6 列赔率 */}
        <div className="flex-1 flex overflow-x-auto scrollbar-thin">
          <MarketColumn
            title="全场独赢" homeLabel="主" drawLabel="平" awayLabel="客"
            home={fx.odds_h} draw={fx.odds_d} away={fx.odds_a} accent="home"
          />
          <MarketColumn
            title="全场让球" line={fx.ah_line} homeLabel="主" awayLabel="客"
            home={fx.ah_home} away={fx.ah_away} accent="home"
          />
          <MarketColumn
            title="全场大小" line={fx.ou_line} homeLabel="大" awayLabel="小"
            home={fx.ou_over} away={fx.ou_under} accent="over"
          />
          <MarketColumn
            title="半场独赢" homeLabel="主" drawLabel="平" awayLabel="客"
            home={fx.h1_odds_h} draw={fx.h1_odds_d} away={fx.h1_odds_a} accent="home"
          />
          <MarketColumn
            title="半场让球" line={fx.h_ah_line} homeLabel="主" awayLabel="客"
            home={fx.h_ah_home} away={fx.h_ah_away} accent="home"
          />
          <MarketColumn
            title="半场大小" line={fx.h_ou_line} homeLabel="大" awayLabel="小"
            home={fx.h_ou_over} away={fx.h_ou_under} accent="over"
          />
        </div>
      </div>

      {/* 底部: 子市场 tabs (纯展示占位, 实际分析走整卡点击) */}
      <div className="px-4 py-2 border-t border-white/[0.04] flex items-center gap-2 overflow-x-auto scrollbar-thin bg-white/[0.01]">
        {['角球', '15分钟', '波胆', '特色组合', '冠军'].map((name) => (
          <span key={name} className="text-[10px] px-2 py-1 rounded text-white/30 whitespace-nowrap">
            {name}
            {name === '波胆' && <span className="ml-1 text-[7px] px-1 bg-ember-500/60 text-white rounded font-bold align-top">HOT</span>}
          </span>
        ))}
        <span className="text-[9px] text-accent/60 ml-auto font-mono">点击卡片分析 →</span>
      </div>
    </motion.div>
  )
}

// ============================================
// 子组件：赛程面板 (loading / 空 / error / 数据 四态)
// ============================================
function FixturesPanel({
  selected, fixtures, loading, error, staleNote, onAnalyze,
}: {
  selected: LeagueCatalogEntry | null
  fixtures: FixtureEntry[]
  loading: boolean
  error: string | null
  staleNote: string | null
  onAnalyze: (home: string, away: string, sportKey: string, odds?: { h: number; d: number; a: number }) => void
}) {
  if (!selected) {
    return (
      <div className="card p-10 text-center">
        <div className="w-14 h-14 rounded-2xl bg-field-500/10 border border-field-500/15 flex items-center justify-center mx-auto mb-4">
          <svg className="w-6 h-6 text-field-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5">
            <path strokeLinecap="round" strokeLinejoin="round" d="M6.75 3v2.25M17.25 3v2.25M3 18.75V7.5a2.25 2.25 0 012.25-2.25h13.5A2.25 2.25 0 0121 7.5v11.25m-18 0A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75m-18 0v-7.5A2.25 2.25 0 015.25 9h13.5A2.25 2.25 0 0121 11.25v7.5" />
          </svg>
        </div>
        <p className="text-sm text-white/50 font-medium">从左侧选择一个联赛</p>
        <p className="text-[11px] text-white/30 mt-1">查看该联赛的实时赛程与 6 大市场赔率</p>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="space-y-3">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="card h-32 bg-white/[0.02] animate-pulse" />
        ))}
      </div>
    )
  }

  if (error) {
    const isSession = /0401013|账户|过期|会话|0400500|处理失败|expired/i.test(error)
    return (
      <div className="card p-6 text-center">
        <div className="w-12 h-12 rounded-xl bg-ember-500/10 border border-ember-500/15 flex items-center justify-center mx-auto mb-3">
          <svg className="w-5 h-5 text-ember-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
          </svg>
        </div>
        <p className="text-sm text-white/70 font-medium">{selected.name} · 实时赛程暂不可用</p>
        <p className="text-[11px] text-white/40 mt-2 max-w-md mx-auto">
          {isSession
            ? '数据源会话已过期（0401013/0400500）。需重新登录获取有效会话（更新 leisu_live.py DEEP_LINK 中的 sessionId 即可恢复）。'
            : `赛程获取失败：${error}`}
        </p>
      </div>
    )
  }

  if (fixtures.length === 0) {
    return (
      <div className="card p-10 text-center">
        <p className="text-sm text-white/50">该联赛当前窗口暂无赛程</p>
        <p className="text-[11px] text-white/30 mt-1">赛季可能休赛或尚未开赛</p>
      </div>
    )
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-3 px-1">
        <h3 className="text-xs font-bold uppercase tracking-widest text-white/50">
          {selected.name} · 实时赛程
        </h3>
        <span className="text-[10px] text-white/30">共 {fixtures.length} 场 · 含 6 大市场赔率</span>
      </div>
      {staleNote && (
        <div className="px-4 py-2 bg-ember-500/[0.06] border border-ember-500/10 rounded-lg text-[10px] text-ember-400/80 mb-3">
          ⚠️ {staleNote}
        </div>
      )}
      <div>
        {fixtures.map((f) => (
          <MatchOddsCard
            key={f.id || `${f.home}-${f.away}`}
            fx={f}
            sportKey={selected.sport_key}
            onAnalyze={onAnalyze}
          />
        ))}
      </div>
    </div>
  )
}

// ============================================
// 主页面：联赛赛程
// ============================================
export default function LeagueSchedule() {
  const [catalog, setCatalog] = useState<LeaguesResponse | null>(null)
  const [catalogLoading, setCatalogLoading] = useState(true)
  const [selected, setSelected] = useState<LeagueCatalogEntry | null>(null)
  const [fixtures, setFixtures] = useState<FixtureEntry[]>([])
  const [fixturesLoading, setFixturesLoading] = useState(false)
  const [fixturesError, setFixturesError] = useState<string | null>(null)
  const [staleNote, setStaleNote] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const [analyzing, setAnalyzing] = useState<{ home: string; away: string; sportKey: string; odds?: { h: number; d: number; a: number } } | null>(null)

  const onAnalyze = useCallback((home: string, away: string, sportKey: string, odds?: { h: number; d: number; a: number }) => {
    setAnalyzing({ home, away, sportKey, odds })
  }, [])

  const loadCatalog = useCallback(async () => {
    setCatalogLoading(true)
    try {
      const res = await leagueScheduleService.getLeagues()
      const data = (res.data as any)?.data || res.data
      setCatalog(data)
      setUpdatedAt(new Date())
    } catch (err) {
      console.error('联赛目录加载失败:', err)
    } finally {
      setCatalogLoading(false)
    }
  }, [])

  useEffect(() => { loadCatalog() }, [loadCatalog])

  const selectLeague = useCallback(async (entry: LeagueCatalogEntry) => {
    setSelected(entry)
    setFixturesLoading(true)
    setFixturesError(null)
    setStaleNote(null)
    try {
      const res = await leagueScheduleService.getFixtures(entry.sport_key)
      const data = (res.data as any)?.data || res.data
      if (data?.error) {
        setFixturesError(String(data.error))
        setFixtures([])
      } else {
        setFixtures(data?.fixtures || [])
      }
      if (data?.stale) setStaleNote(data.note || '返回缓存数据（可能已过期）')
    } catch (err) {
      console.error('赛程加载失败:', err)
      setFixturesError('赛程获取失败，请稍后重试')
      setFixtures([])
    } finally {
      setFixturesLoading(false)
    }
  }, [])

  const totalLeagues = catalog?.total_leagues ?? 0
  const categoryCount = catalog?.categories?.length ?? 0
  const timeStr = updatedAt
    ? updatedAt.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    : '--'

  return (
    <div className="space-y-6">
      {/* 标题栏 */}
      <motion.div
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        className="flex items-center justify-between"
      >
        <div>
          <h1 className="text-2xl font-black font-display text-white tracking-tight">联赛赛程</h1>
          <p className="text-sm text-white/40 mt-1">实时赛事 · 6 大市场赔率 (1X2 / 让球 / 大小 · 全场+半场) · 数据源 微瑞 live feed</p>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-4 bg-white/[0.03] rounded-xl px-4 py-2">
            <div className="text-center">
              <div className="text-lg font-black font-display text-field-400">
                {catalogLoading ? '--' : totalLeagues}
              </div>
              <div className="text-[9px] text-white/30 uppercase tracking-wider">联赛</div>
            </div>
            <div className="w-px h-7 bg-white/10" />
            <div className="text-center">
              <div className="text-lg font-black font-display text-white/70">
                {catalogLoading ? '--' : categoryCount}
              </div>
              <div className="text-[9px] text-white/30 uppercase tracking-wider">分组</div>
            </div>
          </div>
          <button
            onClick={loadCatalog}
            disabled={catalogLoading}
            className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-medium text-white/40 hover:text-field-400 hover:bg-field-500/10 transition-all disabled:opacity-40"
          >
            <svg className={`w-3.5 h-3.5 ${catalogLoading ? 'animate-spin' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
              <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992V4.356M19.671 16.5A8.25 8.25 0 005.672 8.023m-1.652 7.629v4.992h4.992M4.329 7.5A8.25 8.25 0 0018.328 15.977" />
            </svg>
            {catalogLoading ? '加载中' : `刷新 (${timeStr})`}
          </button>
        </div>
      </motion.div>

      {/* 主体 */}
      <div className="grid grid-cols-1 xl:grid-cols-[320px_1fr] gap-6">
        {/* 左侧联赛目录 */}
        <div className="card p-4 xl:max-h-[calc(100vh-180px)] xl:overflow-y-auto scrollbar-thin">
          {catalogLoading ? (
            <div className="space-y-2">
              {Array.from({ length: 8 }).map((_, i) => (
                <div key={i} className="h-9 bg-white/[0.02] rounded-lg animate-pulse" />
              ))}
            </div>
          ) : catalog && catalog.categories.length > 0 ? (
            <div className="space-y-4">
              {catalog.categories.map((cat) => (
                <div key={cat.category}>
                  <p className="text-[10px] font-bold text-white/40 uppercase tracking-wider mb-2 px-1">
                    {cat.category}
                  </p>
                  <div className="space-y-1">
                    {cat.leagues.map((entry) => (
                      <LeagueRow
                        key={entry.sport_key}
                        entry={entry}
                        active={selected?.sport_key === entry.sport_key}
                        onClick={() => selectLeague(entry)}
                      />
                    ))}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-white/30 text-center py-6">联赛目录加载失败</p>
          )}
        </div>

        {/* 右侧赛程面板 */}
        <FixturesPanel
          selected={selected}
          fixtures={fixtures}
          loading={fixturesLoading}
          error={fixturesError}
          staleNote={staleNote}
          onAnalyze={onAnalyze}
        />
      </div>

      {/* 赛事分析弹窗 (全链路决策卡) */}
      <AnimatePresence>
        {analyzing && (
          <MatchAnalysisModal
            home={analyzing.home}
            away={analyzing.away}
            sportKey={analyzing.sportKey}
            odds={analyzing.odds}
            onClose={() => setAnalyzing(null)}
          />
        )}
      </AnimatePresence>
    </div>
  )
}

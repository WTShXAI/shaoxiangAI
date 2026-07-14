import { useState, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { leagueScheduleService, betService } from '@/services/api'
import type { LeagueCategory, LeagueCatalogEntry, LeagueFixturesResponse, FixtureEntry, BetSide } from '@/types'

// ── 分类图标 ──
const CAT_ICONS: Record<string, string> = {
  '五大联赛': '⭐',
  '英格兰联赛': '🏴󠁧󠁢󠁥󠁮󠁧󠁿',
  '德国联赛': '🇩🇪',
  '北欧': '❄️',
  '美洲': '🌎',
  '亚洲/其他': '🌏',
  '杯赛/国际': '🏆',
}

function fmtTime(ts: string): string {
  try {
    const d = new Date(ts)
    if (isNaN(d.getTime())) return ts?.slice(0, 16) || '--'
    return d.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  } catch { return '--' }
}

// ── 单联赛项 ──
function LeagueItem({ league, selected, onSelect, loading }: {
  league: LeagueCatalogEntry; selected: boolean; onSelect: () => void; loading: boolean
}) {
  return (
    <button
      onClick={onSelect}
      disabled={loading}
      className={`flex items-center justify-between px-3 py-2 rounded-md text-sm transition-all duration-150 w-full text-left ${
        selected
          ? 'bg-field-500/12 border border-field-500/25 text-field-400 font-semibold'
          : 'text-white/70 hover:text-white hover:bg-white/[0.04] border border-transparent'
      }`}
    >
      <span>{league.name}</span>
      {league.fixture_count > 0 && (
        <span className="text-[10px] bg-pitch-500/15 text-pitch-400 px-1.5 py-0.5 rounded-full">
          {league.fixture_count}
        </span>
      )}
      {loading && selected && (
        <span className="w-3 h-3 border-2 border-field-400 border-t-transparent rounded-full animate-spin" />
      )}
    </button>
  )
}

// ── 分类卡片 ──
function CategoryCard({ category, selectedKey, onSelect, loading }: {
  category: LeagueCategory; selectedKey: string; onSelect: (sk: string) => void; loading: boolean
}) {
  const [open, setOpen] = useState(
    category.category === '五大联赛' || category.category === '杯赛/国际'
  )
  const icon = CAT_ICONS[category.category] || '⚽'

  return (
    <div className="card p-0 overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 w-full px-4 py-3 hover:bg-white/[0.02] transition-colors"
      >
        <span className="text-lg">{icon}</span>
        <span className="text-sm font-bold text-white/90 font-display">{category.category}</span>
        <span className="text-[10px] text-white/25 ml-auto">{category.leagues.length} 联赛</span>
        <svg className={`w-3 h-3 text-white/30 transition-transform duration-200 ${open ? 'rotate-180' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="px-3 pb-3 space-y-0.5">
              {category.leagues.map((lg) => (
                <LeagueItem
                  key={lg.sport_key}
                  league={lg}
                  selected={selectedKey === lg.sport_key}
                  onSelect={() => onSelect(lg.sport_key)}
                  loading={loading}
                />
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ── 凯利计算 (前端预览, 与后端 bet_core 一致: 半凯利 + 10%封顶) ──
function calcKelly(oh: number, od: number, oa: number, side: BetSide): { kelly: number; stake: number; prob: number } {
  const inv = 1/oh + 1/od + 1/oa
  const probs = { H: (1/oh)/inv, D: (1/od)/inv, A: (1/oa)/inv }
  const odds = { H: oh, D: od, A: oa }
  const p = probs[side]
  const b = odds[side] - 1
  const kellyFull = b > 0 ? (b * p - (1 - p)) / b : 0
  const kellyHalf = Math.max(0, kellyFull * 0.5)
  const stake = Math.round(3000 * Math.min(kellyHalf, 0.10) * 10) / 10
  return { kelly: kellyHalf, stake, prob: p }
}

// ── 下注面板 (行内展开) ──
function BetPanel({ fixture, leagueName, onPlace, busy }: {
  fixture: FixtureEntry; leagueName: string
  onPlace: (side: BetSide, stake: number) => Promise<void>
  busy: boolean
}) {
  const [side, setSide] = useState<BetSide>('H')
  const [stake, setStake] = useState(0)
  const oh = fixture.odds_h || 0, od = fixture.odds_d || 0, oa = fixture.odds_a || 0
  const k = oh > 1 ? calcKelly(oh, od, oa, side) : null
  const sides: { key: BetSide; label: string; odds?: number; color: string }[] = [
    { key: 'H', label: '主胜', odds: oh, color: 'field' },
    { key: 'D', label: '平局', odds: od, color: 'ember' },
    { key: 'A', label: '客胜', odds: oa, color: 'frost' },
  ]
  return (
    <div className="px-4 py-3 bg-white/[0.02] border-t border-white/[0.04]">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-[11px] text-white/40 font-medium">模拟下注</span>
        {/* 方向选择 */}
        <div className="flex gap-1">
          {sides.map(s => (
            <button key={s.key} onClick={() => { setSide(s.key); setStake(0) }}
              disabled={busy}
              className={`px-2 py-1 rounded text-[10px] font-bold transition-all ${
                side === s.key
                  ? s.color === 'field' ? 'bg-field-500/20 text-field-400 border border-field-500/30'
                    : s.color === 'ember' ? 'bg-ember-500/20 text-ember-400 border border-ember-500/30'
                    : 'bg-frost-500/20 text-frost-400 border border-frost-500/30'
                  : 'text-white/30 hover:text-white/60 border border-transparent'
              }`}>
              {s.label} {s.odds ? s.odds.toFixed(2) : '—'}
            </button>
          ))}
        </div>
        {/* 凯利建议 */}
        {k && k.kelly > 0 && (
          <span className="text-[10px] text-white/30">
            凯利建议 {Math.round(k.kelly * 100)}% → ¥{k.stake}
            <button onClick={() => setStake(k.stake)}
              className="ml-1 text-field-400 hover:text-field-300 underline">采用</button>
          </span>
        )}
        {k && k.kelly <= 0 && (
          <span className="text-[10px] text-white/20">无凯利优势 (负期望)</span>
        )}
        {/* 注码输入 */}
        <input type="number" value={stake || ''} placeholder="注码¥"
          onChange={e => setStake(parseFloat(e.target.value) || 0)}
          disabled={busy}
          className="w-20 px-2 py-1 text-xs bg-white/[0.04] border border-white/10 rounded text-white/80 placeholder:text-white/20 focus:outline-none focus:border-field-500/40" />
        <button
          onClick={() => onPlace(side, stake)}
          disabled={busy || stake <= 0}
          className="px-3 py-1 text-[11px] font-medium rounded bg-field-600 hover:bg-field-500 text-white transition-colors disabled:opacity-30 disabled:cursor-not-allowed">
          {busy ? '提交中...' : `下注 ${side}`}
        </button>
      </div>
    </div>
  )
}

// ── 赛程表格 ──
function FixtureTable({ data, loading, onPlaceBet, betBusy }: {
  data?: LeagueFixturesResponse; loading: boolean
  onPlaceBet: (fixture: FixtureEntry, side: BetSide, stake: number) => Promise<void>
  betBusy: number | null  // bet_id 正在下注中, null=空闲
}) {
  const [expandedRow, setExpandedRow] = useState<string | null>(null)
  const [placedFixtures, setPlacedFixtures] = useState<Record<string, { side: BetSide; stake: number }>>({})
  if (loading) {
    return (
      <div className="card p-8 flex items-center justify-center">
        <div className="flex items-center gap-3 text-white/40">
          <span className="w-5 h-5 border-2 border-field-400 border-t-transparent rounded-full animate-spin" />
          <span className="text-sm">正在拉取赛程...</span>
        </div>
      </div>
    )
  }
  if (!data || data.error) {
    return (
      <div className="card p-8 text-center text-white/30 text-sm">
        {data?.error || '请选择联赛查看赛程'}
      </div>
    )
  }
  const fixtures = data.fixtures || []
  if (fixtures.length === 0) {
    return (
      <div className="card p-8 text-center text-white/30 text-sm">
        暂时没有 {data.name} 的赛程数据
        <div className="text-[10px] mt-1">(可能非赛季期, 或 API 暂未覆盖)</div>
      </div>
    )
  }

  const hasOdds = fixtures.some((f: FixtureEntry) => f.odds_h)
  const leagueName = data.name || ''

  return (
    <div className="card p-0 overflow-hidden">
      <div className="flex items-center justify-between px-5 py-3 border-b border-white/[0.06]">
        <div>
          <span className="text-sm font-bold text-white/90 font-display">{data.name}</span>
          <span className="text-[10px] text-white/30 ml-2">共 {fixtures.length} 场</span>
        </div>
        <div className="flex items-center gap-2">
          {data.stale && <span className="text-[10px] text-amber-400/80">⚠ 过期缓存</span>}
          <span className={`text-[10px] px-1.5 py-0.5 rounded ${data.cached ? 'bg-pitch-500/15 text-pitch-400' : 'bg-field-500/15 text-field-400'}`}>
            {data.cached ? `缓存 ${data.cache_age_s ? Math.floor(data.cache_age_s / 60) + 'm' : ''}` : '实时'}
          </span>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-white/25 border-b border-white/[0.04]">
              <th className="text-left py-2 px-4 font-medium">时间</th>
              <th className="text-left py-2 px-4 font-medium">主队</th>
              <th className="text-center py-2 px-2 font-medium">VS</th>
              <th className="text-left py-2 px-4 font-medium">客队</th>
              {hasOdds && (
                <>
                  <th className="text-center py-2 px-2 font-medium">主胜</th>
                  <th className="text-center py-2 px-2 font-medium">平局</th>
                  <th className="text-center py-2 px-2 font-medium">客胜</th>
                  <th className="text-center py-2 px-2 font-medium">操作</th>
                </>
              )}
            </tr>
          </thead>
          <tbody>
            {fixtures.map((f: FixtureEntry, i: number) => {
              const rowKey = f.id || `row-${i}`
              const placed = placedFixtures[rowKey]
              return (
                <>
                  <motion.tr
                    key={rowKey}
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: i * 0.02, duration: 0.2 }}
                    className="border-b border-white/[0.02] hover:bg-white/[0.02] transition-colors"
                  >
                    <td className="py-2.5 px-4 text-white/50 whitespace-nowrap font-mono text-[11px]">
                      {fmtTime(f.commence_time)}
                    </td>
                    <td className="py-2.5 px-4 text-white/85 font-medium">{f.home}</td>
                    <td className="py-2.5 px-2 text-center text-white/15 font-bold text-[10px]">VS</td>
                    <td className="py-2.5 px-4 text-white/85 font-medium">{f.away}</td>
                    {hasOdds && (
                      <>
                        <td className="py-2.5 px-2 text-center text-white/60 font-mono">
                          {f.odds_h?.toFixed(2) || '—'}
                        </td>
                        <td className="py-2.5 px-2 text-center text-white/60 font-mono">
                          {f.odds_d?.toFixed(2) || '—'}
                        </td>
                        <td className="py-2.5 px-2 text-center text-white/60 font-mono">
                          {f.odds_a?.toFixed(2) || '—'}
                        </td>
                        <td className="py-2.5 px-2 text-center">
                          {placed ? (
                            <span className="text-[10px] text-field-400 font-medium">
                              ✓ {placed.side} ¥{placed.stake}
                            </span>
                          ) : f.odds_h && f.odds_h > 1 ? (
                            <button
                              onClick={() => setExpandedRow(expandedRow === rowKey ? null : rowKey)}
                              disabled={betBusy !== null}
                              className="text-[10px] px-2 py-0.5 rounded text-field-400/80 hover:text-field-400 hover:bg-field-500/10 transition-colors disabled:opacity-30"
                            >
                              {expandedRow === rowKey ? '收起' : '下注'}
                            </button>
                          ) : null}
                        </td>
                      </>
                    )}
                  </motion.tr>
                  {expandedRow === rowKey && f.odds_h && (
                    <tr key={`${rowKey}-bet`}>
                      <td colSpan={hasOdds ? 8 : 4} className="p-0">
                        <BetPanel
                          fixture={f}
                          leagueName={leagueName}
                          busy={betBusy === i}
                          onPlace={async (side, stakeAmt) => {
                            await onPlaceBet(f, side, stakeAmt)
                            setPlacedFixtures(prev => ({ ...prev, [rowKey]: { side, stake: stakeAmt } }))
                            setExpandedRow(null)
                          }}
                        />
                      </td>
                    </tr>
                  )}
                </>
              )
            })}
          </tbody>
        </table>
      </div>
      {data.note && (
        <div className="px-5 py-2 text-[10px] text-white/25 bg-white/[0.01] border-t border-white/[0.04]">
          {data.note}
        </div>
      )}
    </div>
  )
}


// ── 主页面 ──
export default function LeagueSchedule() {
  const [categories, setCategories] = useState<LeagueCategory[]>([])
  const [loadingLeagues, setLoadingLeagues] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [selectedKey, setSelectedKey] = useState<string>('soccer_fifa_world_cup')
  const [fixtureData, setFixtureData] = useState<LeagueFixturesResponse | undefined>()
  const [loadingFixture, setLoadingFixture] = useState(false)
  const [fixtureError, setFixtureError] = useState<string | null>(null)

  // 模拟投注状态
  const [betBusy, setBetBusy] = useState<number | null>(null)
  const [betMsg, setBetMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null)


  // 加载联赛目录
  const loadLeagues = useCallback(async () => {
    setLoadingLeagues(true)
    try {
      const res = await leagueScheduleService.getLeagues()
      setCategories(res.data?.data?.categories || [])
      setError(null)
    } catch (e: any) {
      setError(e?.message || '联赛目录加载失败')
    } finally {
      setLoadingLeagues(false)
    }
  }, [])

  useEffect(() => { loadLeagues() }, [loadLeagues])

  // 选中联赛时加载赛程
  const loadFixtures = useCallback(async (sk: string) => {
    setSelectedKey(sk)
    setLoadingFixture(true)
    setFixtureError(null)
    try {
      const res = await leagueScheduleService.getFixtures(sk)
      setFixtureData(res.data.data)
    } catch (e: any) {
      setFixtureError(e?.message || '赛程加载失败')
      setFixtureData(undefined)
    } finally {
      setLoadingFixture(false)
    }
  }, [])

  // 首次加载: 如果有可用联赛, 自动选第一个
  useEffect(() => {
    if (categories.length > 0 && !fixtureData && !loadingFixture) {
      // 优先级: 世界杯 > 第一个可用联赛
      const wc = categories
        .flatMap(c => c.leagues)
        .find(l => l.sport_key === 'soccer_fifa_world_cup')
      if (wc) {
        loadFixtures('soccer_fifa_world_cup')
      }
    }
  }, [categories, fixtureData, loadingFixture, loadFixtures])

  // 模拟下注处理
  const handlePlaceBet = useCallback(async (fixture: FixtureEntry, side: BetSide, stake: number) => {
    const fixtureIdx = fixtureData?.fixtures?.indexOf(fixture) ?? -1
    setBetBusy(fixtureIdx)
    setBetMsg(null)
    try {
      const res = await betService.placeBet({
        home_team: fixture.home,
        away_team: fixture.away,
        league: fixtureData?.name || '',
        home_odds: fixture.odds_h || 0,
        draw_odds: fixture.odds_d || 0,
        away_odds: fixture.odds_a || 0,
        bet_side: side,
        stake_amount: stake,
      })
      const d = res.data.data
      if (d.error) {
        setBetMsg({ type: 'err', text: d.error })
      } else {
        setBetMsg({ type: 'ok', text: d.message || `已下注 ${side} ¥${stake}` })
      }
    } catch (e: any) {
      setBetMsg({ type: 'err', text: e?.message || '下注失败' })
    } finally {
      setBetBusy(null)
      // 3秒后自动清除消息
      setTimeout(() => setBetMsg(null), 3000)
    }
  }, [fixtureData])

  const selectedLeague = categories
    .flatMap(c => c.leagues)
    .find(l => l.sport_key === selectedKey)

  return (
    <div className="space-y-4">
      {/* 页面标题 */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-bold text-white font-display">📅 联赛赛程</h2>
          <p className="text-xs text-white/25 mt-0.5">34 个联赛实时赛程 · 数据源 The Odds API</p>
        </div>
        <button
          onClick={loadLeagues}
          disabled={loadingLeagues}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md text-white/40 hover:text-white hover:bg-white/[0.04] transition-colors disabled:opacity-30"
        >
          <svg className={`w-3.5 h-3.5 ${loadingLeagues ? 'animate-spin' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
            <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182" />
          </svg>
          刷新目录
        </button>
      </div>

      {/* 主体: 左分类 + 右赛程 */}
      <div className="grid grid-cols-1 lg:grid-cols-[300px_1fr] gap-4">
        {/* 左栏: 联赛分类 */}
        <div className="space-y-2 max-h-[calc(100vh-180px)] overflow-y-auto pr-1">
          {loadingLeagues ? (
            <div className="card p-6 flex items-center justify-center">
              <div className="flex items-center gap-3 text-white/40">
                <span className="w-4 h-4 border-2 border-field-400 border-t-transparent rounded-full animate-spin" />
                <span className="text-sm">加载联赛目录...</span>
              </div>
            </div>
          ) : error ? (
            <div className="card p-4 text-center text-danger-400 text-sm">{error}</div>
          ) : (
            categories.map((cat) => (
              <CategoryCard
                key={cat.category}
                category={cat}
                selectedKey={selectedKey}
                onSelect={loadFixtures}
                loading={loadingFixture}
              />
            ))
          )}
        </div>

        {/* 右栏: 赛程表 */}
        <div className="max-h-[calc(100vh-180px)] overflow-y-auto">
          {betMsg && (
            <div className={`mb-2 px-3 py-2 rounded-md text-xs ${
              betMsg.type === 'ok'
                ? 'bg-field-500/10 text-field-400 border border-field-500/20'
                : 'bg-danger-500/10 text-danger-400 border border-danger-500/20'
            }`}>
              {betMsg.type === 'ok' ? '✓ ' : '✗ '}{betMsg.text}
            </div>
          )}
          {fixtureError ? (
            <div className="card p-4 text-center text-danger-400 text-sm">{fixtureError}</div>
          ) : (
            <AnimatePresence mode="wait">
              <motion.div
                key={selectedKey}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -8 }}
                transition={{ duration: 0.2 }}
              >
                <FixtureTable
                  data={fixtureData}
                  loading={loadingFixture}
                  onPlaceBet={handlePlaceBet}
                  betBusy={betBusy}
                />
              </motion.div>
            </AnimatePresence>
          )}
        </div>
      </div>
    </div>
  )
}

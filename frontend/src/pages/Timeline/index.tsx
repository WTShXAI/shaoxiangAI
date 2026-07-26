import { useState, useEffect, useCallback } from 'react'
import PageHeader from '@/components/layout/PageHeader'

// ═══ 类型 ═══
interface OddsItem {
  odds: number
  line?: number | null
  [k: string]: unknown
}
interface TimelineMatch {
  mid: string
  home: string
  away: string
  league: string
  kickoff_str: string   // "09:30"
  kickoff_ts: number     // epoch ms
  status: 'scheduled' | 'live' | 'finished'
  score: [number, number] | null
  minute: number
  odds: Record<string, OddsItem>
}
interface TimelineResponse {
  date: string
  tz: string
  count: number
  cached_at: string
  matches: TimelineMatch[]
}

// ═══ 虚盘过滤（乐鱼源混入的电竞模拟）═══
const FAKE_LEAGUE = /VS-|EAFC|PANDA/i

// ═══ 状态徽章 ═══
function StatusBadge({ status }: { status: string }) {
  const cfg: Record<string, { label: string; bg: string; dot?: string }> = {
    scheduled: { label: '待开赛', bg: 'bg-surface-border/60 text-ink-disabled' },
    live:      { label: '进行中', bg: 'bg-field-500/15 text-field-400 border border-field-500/25', dot: 'emerald' },
    finished:  { label: '已结束', bg: 'bg-frost-500/12 text-frost-400 border border-frost-500/20' },
  }
  const c = cfg[status] || cfg.scheduled
  const dotColor = c.dot === 'emerald' ? '#10b981' : '#6ee7b7'
  return (
    <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold ${c.bg}`}>
      {c.dot && <span className="w-1.5 h-1.5 rounded-full animate-pulse" style={{ backgroundColor: dotColor }} />}
      {c.label}
    </span>
  )
}

// ═══ 赔率摘要 chips ═══
function OddsChips({ odds }: { odds: Record<string, OddsItem> }) {
  if (!odds || Object.keys(odds).length === 0) return null
  // 1X2
  const h = odds['1X2/home'], d = odds['1X2/draw'], a = odds['1X2/away']
  const has1x2 = h && d && a
  // AH (取第一条)
  const ahKeys = Object.keys(odds).filter(k => k.startsWith('AH_'))
  const ahHome = ahKeys.length ? odds[ahKeys[0] + '/home'] : null
  const ahAway = ahKeys.length ? odds[ahKeys[0] + '/away'] : null
  // OU (取第一条)
  const ouKeys = Object.keys(odds).filter(k => k.startsWith('OU_'))
  const ouOver = ouKeys.length ? odds[ouKeys[0] + '/over'] : null
  const ouUnder = ouKeys.length ? odds[ouKeys[0] + '/under'] : null
  // CS top3
  const csTop = Object.keys(odds).filter(k => k.startsWith('CS/')).map(k => ({ key: k.replace('CS/', ''), ...odds[k] })).sort((x, y) => x.odds - y.odds).slice(0, 3)

  return (
    <div className="flex flex-wrap gap-1 mt-1">
      {has1x2 && (
        <span className="inline-flex gap-1 text-[11px] bg-surface-dark/50 rounded px-1.5 py-0.5">
          <span className="text-ink-muted">主</span><span className="font-mono text-ink-primary">{h.odds.toFixed(2)}</span>
          <span className="text-ink-muted">平</span><span className="font-mono text-ink-primary">{d.odds.toFixed(2)}</span>
          <span className="text-ink-muted">客</span><span className="font-mono text-ink-primary">{a.odds.toFixed(2)}</span>
        </span>
      )}
      {!has1x2 && ahHome && ahAway && (
        <span className="inline-flex gap-1 text-[11px] bg-surface-dark/50 rounded px-1.5 py-0.5">
          <span className="text-ink-muted">{ahKeys[0]?.replace('AH_', '')}</span>
          <span className="font-mono text-ink-primary">{ahHome.odds.toFixed(2)}</span>
          <span className="text-ink-muted">/</span>
          <span className="font-mono text-ink-primary">{ahAway.odds.toFixed(2)}</span>
        </span>
      )}
      {!has1x2 && ouOver && ouUnder && (
        <span className="inline-flex gap-1 text-[11px] bg-surface-dark/50 rounded px-1.5 py-0.5">
          <span className="text-ink-muted">O{ouKeys[0]?.replace('OU_', '')}</span>
          <span className="font-mono text-ink-primary">{ouOver.odds.toFixed(2)}</span>
          <span className="text-ink-muted">U</span>
          <span className="font-mono text-ink-primary">{ouUnder.odds.toFixed(2)}</span>
        </span>
      )}
      {csTop.map(cs => (
        <span key={cs.key} className="inline-flex gap-0.5 text-[11px] bg-surface-dark/50 rounded px-1.5 py-0.5">
          <span className="text-ink-muted">{cs.key}</span>
          <span className="font-mono text-field-400">{cs.odds.toFixed(1)}</span>
        </span>
      ))}
    </div>
  )
}

// ═══ 赔率详情展开区 ═══
function OddsDetail({ odds }: { odds: Record<string, OddsItem> }) {
  if (!odds || Object.keys(odds).length === 0) return <p className="text-ink-muted text-xs py-2">暂无赔率数据</p>
  const groups: Record<string, { label: string; items: { key: string; odds: number }[] }[]> = {}

  for (const [k, v] of Object.entries(odds)) {
    const parts = k.split('/')
    const market = parts[0], sel = parts.slice(1).join('/')
    if (!groups[market]) groups[market] = []
    // CS 按赔率升序
    if (market === 'CS') {
      groups[market].push({ label: sel, items: [{ key: sel, odds: v.odds }] })
    } else {
      let grp = groups[market].find(g => g.label === market)
      if (!grp) { grp = { label: market, items: [] }; groups[market].push(grp) }
      grp.items.push({ key: sel, odds: v.odds })
    }
  }
  // CS 展平为单条
  if (groups['CS']) {
    const csItems = groups['CS'].map(g => ({ key: g.label, odds: g.items[0]?.odds ?? 0 })).sort((a, b) => a.odds - b.odds)
    groups['CS'] = [{ label: 'CS', items: csItems.slice(0, 12) }]
  }

  const marketOrder = ['1X2', 'AH', 'OU', 'CS']
  return (
    <div className="mt-2 pt-2 border-t border-surface-border/30 grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
      {marketOrder.filter(m => groups[m]).map(market =>
        groups[market].map((grp, gi) => (
          <div key={`${market}-${gi}`} className="bg-surface-dark/40 rounded p-1.5">
            <div className="text-[10px] text-ink-muted font-bold mb-1">
              {market === '1X2' ? '独赢' : market === 'AH' ? '让球' : market === 'OU' ? '大小' : '波胆'}
              {grp.label !== market && ` ${grp.label.replace(/^AH_|^OU_/, '')}`}
            </div>
            {grp.items.map(it => (
              <div key={it.key} className="flex justify-between">
                <span className="text-ink-muted/70">{it.key === 'home' ? '主' : it.key === 'away' ? '客' : it.key === 'draw' ? '平' : it.key === 'over' ? '大' : it.key === 'under' ? '小' : it.key}</span>
                <span className="font-mono text-ink-primary font-bold">{it.odds.toFixed(2)}</span>
              </div>
            ))}
          </div>
        ))
      )}
    </div>
  )
}

// ═══ 主组件 ═══
const PAGE_SIZE = 30

export default function TimelinePage() {
  const [data, setData] = useState<TimelineResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [displayCount, setDisplayCount] = useState(PAGE_SIZE)

  const fetchTimeline = useCallback(async () => {
    try {
      const res = await fetch('/api/timeline/today')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const json: TimelineResponse = await res.json()
      if (json.error) { setError(json.error); return }
      // 过滤虚盘
      json.matches = json.matches.filter(m => !FAKE_LEAGUE.test(m.league))
      setData(json)
      setError('')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '未知错误')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchTimeline()
    const timer = setInterval(fetchTimeline, 75_000)
    return () => clearInterval(timer)
  }, [fetchTimeline])

  const toggleExpand = (mid: string) => setExpandedId(prev => prev === mid ? null : mid)

  if (loading && !data) {
    return <div className="flex items-center justify-center h-full min-h-[40vh] text-ink-secondary text-sm">加载中…</div>
  }
  if (error && !data) {
    return (
      <div className="flex flex-col items-center justify-center h-full min-h-[40vh] gap-3">
        <p className="text-ember-400 text-sm">时间轴获取失败，请稍后重试</p>
        <p className="text-ink-muted text-xs">{error}</p>
        <button onClick={fetchTimeline} className="px-4 py-1.5 rounded bg-surface-border/30 text-ink-secondary text-xs hover:bg-surface-border/50 transition-colors">重新加载</button>
      </div>
    )
  }
  if (!data) return null

  const scCount = data.matches.filter(m => m.status === 'scheduled').length
  const lvCount = data.matches.filter(m => m.status === 'live').length
  const fnCount = data.matches.filter(m => m.status === 'finished').length

  return (
    <div className="max-w-4xl mx-auto px-4 py-5">
      <PageHeader
        title="今日比赛时间轴"
        subtitle={`${data.date} · ${data.tz} · 共 ${data.count} 场${data.cached_at ? ` · 缓存于 ${data.cached_at.slice(11, 19)}` : ''}`}
        metrics={[
          { label: '待开赛', value: scCount },
          { label: '进行中', value: lvCount, accent: 'field' },
          { label: '已结束', value: fnCount, accent: 'frost' },
        ]}
      />

      {/* ── 时间轴 ── */}
      <div className="relative pl-8 border-l-2 border-surface-border/40">
        {data.matches.slice(0, displayCount).map(m => (
          <div key={m.mid} className="relative mb-2">
            {/* 节点圆点 */}
            <div
              className={`absolute -left-[1.15rem] top-3.5 w-2.5 h-2.5 rounded-full border-2 border-surface-dark ring-2 ring-surface-canvas ${
                m.status === 'live' ? 'bg-field-500' : m.status === 'finished' ? 'bg-frost-500' : 'bg-surface-border'
              }`}
            />

            {/* 卡片 */}
            <div
              onClick={() => toggleExpand(m.mid)}
              className={`ml-4 rounded-lg border px-3.5 py-2.5 cursor-pointer transition-colors duration-150 ${
                m.status === 'live'
                  ? 'border-field-500/15 bg-field-500/[0.03] hover:bg-field-500/[0.06]'
                  : m.status === 'finished'
                  ? 'border-frost-500/10 bg-surface-dark/30 hover:bg-surface-dark/50'
                  : 'border-surface-border/30 bg-transparent hover:bg-surface-dark/30'
              }`}
            >
              {/* 头部行: 时间 + 状态 + 对阵 */}
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-[15px] font-bold text-ink-primary tabular-nums min-w-[40px]">{m.kickoff_str}</span>
                <StatusBadge status={m.status} />
                <span className="text-xs text-ink-muted/60 max-w-[140px] truncate" title={m.league}>{m.league}</span>
                {m.home ? (
                  <span className="text-sm font-medium text-ink-primary ml-auto">{m.home} vs {m.away}</span>
                ) : (
                  <span className="text-xs text-ink-disabled ml-auto">对阵待开赛揭晓</span>
                )}
              </div>

              {/* 比分 / 进行中计时 */}
              {m.score && m.status !== 'scheduled' ? (
                <div className="flex items-center gap-2 mt-2">
                  <span className="text-[20px] font-black text-ink-primary tabular-nums tracking-widest">
                    {m.score[0]} : {m.score[1]}
                  </span>
                  {m.status === 'live' && m.minute > 0 && (
                    <span className="text-xs text-field-400 font-mono">{m.minute}&apos;</span>
                  )}
                </div>
              ) : m.status === 'live' ? (
                <p className="text-xs text-ink-muted mt-1">比分采集中…</p>
              ) : null}

              {/* 赔率摘要 */}
              <OddsChips odds={m.odds} />

              {/* 详情展开区 */}
              {expandedId === m.mid && (
                <OddsDetail odds={m.odds} />
              )}
            </div>
          </div>
        ))}
      </div>

      {/* 加载更多 / 收起 */}
      {data.matches.length > PAGE_SIZE && (
        <div className="mt-4 flex justify-center gap-2">
          {displayCount < data.matches.length && (
            <button
              onClick={() => setDisplayCount(c => c + PAGE_SIZE)}
              className="px-4 py-1.5 rounded-md text-xs bg-surface-border/40 text-ink-secondary hover:bg-surface-border/60 transition-colors"
            >
              加载更多 · 还有 {data.matches.length - displayCount} 场
            </button>
          )}
          {displayCount > PAGE_SIZE && (
            <button
              onClick={() => setDisplayCount(PAGE_SIZE)}
              className="px-3 py-1.5 rounded-md text-xs text-ink-disabled hover:text-ink-secondary transition-colors"
            >
              收起
            </button>
          )}
        </div>
      )}

      {/* 底部说明 */}
      <div className="mt-6 text-[10px] text-ink-disabled text-center">
        待开赛比赛对阵由乐鱼数据源提供，开赛后实时更新。金额单位均为模拟盘赔率参考，非真实投注建议。
      </div>
    </div>
  )
}

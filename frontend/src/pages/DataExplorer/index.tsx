import { useState, useEffect, useCallback } from 'react'
import { motion } from 'framer-motion'
import { matchService, historicalService } from '@/services/api'
import type { Match, League } from '@/types'

// 结果映射
const resultLabel: Record<string, string> = { 'H': '主胜', 'D': '平局', 'A': '客胜' }
const resultColor: Record<string, string> = {
  'H': 'bg-pitch-500/20 text-pitch-400',
  'D': 'bg-ember-500/20 text-ember-400',
  'A': 'bg-frost-500/20 text-frost-400',
}

// 可排序列定义
type SortKey = 'date' | 'league' | 'score' | 'odds_h' | 'none'
const SORT_LABELS: Record<SortKey, string> = {
  'date': '时间', 'league': '联赛', 'score': '比分', 'odds_h': '赔率', 'none': ''
}

export default function DataExplorer() {
  const [matches, setMatches] = useState<Match[]>([])
  const [leagues, setLeagues] = useState<League[]>([])
  const [loading, setLoading] = useState(true)
  const [searchQuery, setSearchQuery] = useState('')
  const [selectedLeague, setSelectedLeague] = useState<string>('all')
  const [selectedStatus, setSelectedStatus] = useState<string>('all')
  const [expandedRow, setExpandedRow] = useState<string | null>(null)
  const [wsConnected, setWsConnected] = useState(false)
  const [page, setPage] = useState(0)
  const [total, setTotal] = useState(0)
  const [sortKey, setSortKey] = useState<SortKey>('date')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [refreshing, setRefreshing] = useState(false)
  const pageSize = 50

  // 筛选变化时重置页码
  const handleLeagueChange = (code: string) => { setSelectedLeague(code); setPage(0) }
  const handleStatusChange = (code: string) => { setSelectedStatus(code); setPage(0) }

  // 排序切换
  const handleSort = (key: SortKey) => {
    if (key === 'none') return
    if (sortKey === key) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(key)
      setSortDir('desc')
    }
  }

  const fetchData = useCallback(async (showLoading = true) => {
    try {
      if (showLoading) setLoading(true); else setRefreshing(true)
      const [matchesRes, leaguesRes] = await Promise.all([
        matchService.getMatches({
          status: selectedStatus !== 'all' ? selectedStatus : undefined,
          league: selectedLeague !== 'all' ? selectedLeague : undefined,
          limit: pageSize,
          offset: page * pageSize,
        }),
        historicalService.getLeagues(),
      ])
      setMatches(matchesRes.data?.matches || matchesRes.data?.data || [])
      setTotal(matchesRes.data?.total || 0)
      setLeagues(leaguesRes.data?.data || [])
    } catch {
      if (!leagues.length) {
        setLeagues([
          { code: 'WC2026', name: '2026世界杯', country: '国际' },
          { code: 'EPL', name: '英超', country: '英格兰' },
          { code: 'LA_LIGA', name: '西甲', country: '西班牙' },
        ])
      }
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [selectedLeague, selectedStatus, page, pageSize, leagues.length])

  useEffect(() => {
    let ws: WebSocket | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let unmounted = false

    const connectWS = () => {
      if (unmounted) return
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const wsUrl = `${protocol}//${window.location.host}/ws/realtime`
      ws = new WebSocket(wsUrl)

      ws.onopen = () => { setWsConnected(true) }
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          if (data.type === 'match_update' && data.match) {
            setMatches(prev => prev.map(m => m.id === data.match.id ? { ...m, ...data.match } : m))
          }
          if (data.type === 'matches_list' && Array.isArray(data.matches)) {
            setMatches(data.matches)
          }
          if (data.type === 'odds_update' && Array.isArray(data.data)) {
            setMatches(prev => prev.map(m => {
              const up = data.data.find((o: any) => o.id === m.id)
              return up ? { ...m, ...up } : m
            }))
          }
        } catch { /* 非法JSON */ }
      }
      ws.onerror = () => {}
      ws.onclose = () => {
        setWsConnected(false)
        if (!unmounted) reconnectTimer = setTimeout(connectWS, 3000)
      }
    }

    fetchData()
    connectWS()

    return () => {
      unmounted = true
      ws?.close()
      if (reconnectTimer) clearTimeout(reconnectTimer)
    }
  }, [fetchData])

  // 前端搜索过滤（后端已做分页，这里仅做客户端搜索在当前页内过滤）
  const searchFiltered = (matches || []).filter((m) => {
    if (!searchQuery) return true
    return m.homeTeam.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      m.awayTeam.name.toLowerCase().includes(searchQuery.toLowerCase())
  })

  // 排序
  const sorted = [...searchFiltered].sort((a, b) => {
    let va: any, vb: any
    switch (sortKey) {
      case 'date':
        va = a.kickoff || ''; vb = b.kickoff || ''; break
      case 'league':
        va = a.league?.name || ''; vb = b.league?.name || ''; break
      case 'score':
        va = (a.homeScore ?? -1) + (a.awayScore ?? -1); vb = (b.homeScore ?? -1) + (b.awayScore ?? -1); break
      case 'odds_h':
        va = (a as any).home_odds ?? 0; vb = (b as any).home_odds ?? 0; break
      default: return 0
    }
    if (va < vb) return sortDir === 'asc' ? -1 : 1
    if (va > vb) return sortDir === 'asc' ? 1 : -1
    return 0
  })

  const getResult = (m: Match): string => {
    if (m.status !== 'finished' || m.homeScore == null || m.awayScore == null) return ''
    if (m.homeScore > m.awayScore) return 'H'
    if (m.homeScore === m.awayScore) return 'D'
    return 'A'
  }

  const fmtOdds = (v: number | undefined): string => {
    if (v == null || v === 0) return '--'
    return v.toFixed(2)
  }

  // 表头渲染
  const SortHeader = ({ label, key }: { label: string; key: SortKey }) => (
    <th
      onClick={() => handleSort(key)}
      className="py-3 px-3 text-white/30 font-medium uppercase tracking-wider cursor-pointer select-none hover:text-white/60 transition-colors"
    >
      <span className="inline-flex items-center gap-1">
        {label}
        {sortKey === key && (
          <span className="text-[10px]">{sortDir === 'asc' ? '↑' : '↓'}</span>
        )}
      </span>
    </th>
  )

  return (
    <div className="space-y-6">
      {/* 标题栏 */}
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-black font-display text-white tracking-tight">数据探索</h1>
            <p className="text-sm text-white/40 mt-1">比赛数据查询 · 比分详情 · 赔率分析</p>
          </div>
          <div className="flex items-center gap-3">
            {/* 刷新按钮 */}
            <button
              onClick={() => fetchData(false)}
              disabled={refreshing}
              className="p-1.5 rounded-lg bg-white/[0.04] hover:bg-white/[0.08] transition-colors"
              title="刷新数据"
            >
              <svg className={`w-4 h-4 text-white/40 ${refreshing ? 'animate-spin' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
            </button>
            {/* WS状态 */}
            <span className={`w-2.5 h-2.5 rounded-full ${wsConnected ? 'bg-green-500' : 'bg-red-500'}`} />
            <span className="text-xs text-white/40">
              {wsConnected ? '实时连接已建立' : '实时连接未建立'}
            </span>
          </div>
        </div>
      </motion.div>

      {/* 筛选栏 */}
      <motion.div
        initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1 }}
        className="glass-card p-4"
      >
        <div className="flex flex-wrap gap-3 items-center">
          <div className="flex-1 min-w-[180px]">
            <input
              type="text" placeholder="搜索球队名称..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full bg-white/[0.04] border border-white/[0.08] rounded-lg px-3 py-2 text-xs text-white/70 placeholder-white/20 outline-none focus:border-pitch-500/30 transition-colors"
            />
          </div>

          {/* 联赛筛选 — 滚动下拉 */}
          <select
            value={selectedLeague}
            onChange={(e) => handleLeagueChange(e.target.value)}
            className="bg-white/[0.04] border border-white/[0.08] rounded-lg px-3 py-2 text-xs text-white/70 outline-none focus:border-pitch-500/30 transition-colors appearance-none cursor-pointer min-w-[130px]"
          >
            <option value="all">全部联赛</option>
            {leagues.map((l) => (
              <option key={l.code} value={l.code}>{l.name}</option>
            ))}
          </select>

          {/* 状态筛选 */}
          <div className="flex gap-1.5">
            {[
              ['all', '全部'], ['finished', '已结束'], ['upcoming', '未开始'], ['live', '进行中']
            ].map(([val, label]) => (
              <button key={val} onClick={() => handleStatusChange(val)}
                className={`px-2.5 py-1 rounded-md text-[10px] font-medium transition-all ${
                  selectedStatus === val ? 'bg-frost-500/20 text-frost-400' : 'text-white/30 hover:text-white/60 hover:bg-white/[0.03]'
                }`}>{label}</button>
            ))}
          </div>
        </div>
        <div className="text-[10px] text-white/20 mt-2">
          共 {total || sorted.length} 场比赛
          {sortKey !== 'none' && <span className="ml-2">· 排序: {SORT_LABELS[sortKey]} {sortDir === 'asc' ? '↑' : '↓'}</span>}
        </div>
      </motion.div>

      {/* 数据表格 */}
      <motion.div
        initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.2 }}
        className="glass-card overflow-hidden"
      >
        {loading ? (
          <div className="p-6 space-y-3">
            {[1, 2, 3, 4, 5].map((i) => (
              <div key={i} className="h-10 bg-white/[0.02] rounded-lg animate-pulse" />
            ))}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-white/[0.06]">
                  <SortHeader label="联赛" key="league" />
                  <th className="text-right py-3 px-3 text-white/30 font-medium uppercase tracking-wider">主队</th>
                  <SortHeader label="比分" key="score" />
                  <th className="text-left py-3 px-3 text-white/30 font-medium uppercase tracking-wider">客队</th>
                  <th className="text-center py-3 px-2 text-white/30 font-medium uppercase tracking-wider">结果</th>
                  <SortHeader label="时间" key="date" />
                  <SortHeader label="赔率" key="odds_h" />
                  <th className="text-center py-3 px-2 text-white/30 font-medium uppercase tracking-wider">预测</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((match, i) => {
                  const result = getResult(match)
                  const isExpanded = expandedRow === match.id
                  const m = match as any
                  const hasOdds = m.home_odds != null || m.draw_odds != null || m.away_odds != null

                  return (
                    <>
                      <motion.tr
                        initial={{ opacity: 0, y: 5 }}
                        animate={{ opacity: 1, y: 0 }}
                        transition={{ delay: Math.min(i * 0.01, 0.3) }}
                        onClick={() => setExpandedRow(isExpanded ? null : match.id)}
                        className="border-b border-white/[0.03] cursor-pointer hover:bg-white/[0.02] transition-colors group"
                      >
                        <td className="py-2.5 px-3">
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-white/[0.04] text-white/40 whitespace-nowrap">
                            {match.league.name}
                          </span>
                        </td>

                        <td className="py-2.5 px-3 text-right">
                          <span className={`font-medium whitespace-nowrap ${
                            match.status === 'finished' && result === 'H' ? 'text-pitch-400' : 'text-white/80'
                          }`}>{match.homeTeam.name}</span>
                        </td>

                        <td className="py-2.5 px-2 text-center">
                          {match.status === 'finished' && match.homeScore != null ? (
                            <div>
                              <span className={`font-bold font-display text-sm ${
                                result === 'H' ? 'text-pitch-400' : result === 'D' ? 'text-ember-400' : 'text-frost-400'
                              }`}>
                                {match.homeScore} - {match.awayScore}
                              </span>
                              {(m.halftime_home != null || m.halftime_away != null) && (
                                <div className="text-[9px] text-white/20 mt-0.5">
                                  (HT {m.halftime_home ?? 0}-{m.halftime_away ?? 0})
                                </div>
                              )}
                            </div>
                          ) : match.status === 'live' ? (
                            <div className="flex items-center justify-center gap-1.5">
                              <span className="w-1.5 h-1.5 rounded-full bg-danger-500 animate-pulse" />
                              <span className="font-bold font-display text-sm text-white/80">
                                {match.homeScore ?? 0} - {match.awayScore ?? 0}
                              </span>
                            </div>
                          ) : (
                            <span className="font-bold font-display text-sm text-white/20">VS</span>
                          )}
                        </td>

                        <td className="py-2.5 px-3">
                          <span className={`font-medium whitespace-nowrap ${
                            match.status === 'finished' && result === 'A' ? 'text-frost-400' : 'text-white/80'
                          }`}>{match.awayTeam.name}</span>
                        </td>

                        <td className="py-2.5 px-2 text-center">
                          {result ? (
                            <span className={`text-[10px] px-2 py-0.5 rounded font-medium ${resultColor[result]}`}>
                              {resultLabel[result]}
                            </span>
                          ) : <span className="text-white/15">--</span>}
                        </td>

                        <td className="py-2.5 px-3 text-center whitespace-nowrap">
                          <span className="text-white/30">
                            {match.kickoff ? new Date(match.kickoff).toLocaleDateString('zh-CN', {
                              month: '2-digit', day: '2-digit',
                            }) : '--'}
                          </span>
                        </td>

                        <td className="py-2.5 px-3 text-center hidden md:table-cell">
                          {hasOdds ? (
                            <span className="font-mono text-white/25 text-[10px]">
                              {fmtOdds(m.home_odds)} / {fmtOdds(m.draw_odds)} / {fmtOdds(m.away_odds)}
                            </span>
                          ) : <span className="text-white/10">--</span>}
                        </td>

                        <td className="py-2.5 px-2 text-center">
                          {match.prediction ? (
                            <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${resultColor[match.prediction] || 'text-white/30'}`}>
                              {resultLabel[match.prediction] || match.prediction}
                              {match.confidence != null && (
                                <span className="ml-1 text-white/20">
                                  {match.confidence > 1 ? match.confidence : Math.round(match.confidence * 100)}%
                                </span>
                              )}
                            </span>
                          ) : <span className="text-white/15">--</span>}
                        </td>
                      </motion.tr>

                      {/* 展开详情行 */}
                      {isExpanded && (
                        <tr className="border-b border-white/[0.03] bg-white/[0.01]">
                          <td colSpan={8} className="p-4">
                            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                              {/* 比分详情 */}
                              <div className="bg-white/[0.03] rounded-lg p-3">
                                <div className="text-[10px] text-white/30 mb-2 uppercase tracking-wider">比分详情</div>
                                <div className="text-sm text-white/70 space-y-1">
                                  {match.status === 'finished' ? (
                                    <>
                                      <div>全场: <span className="font-mono text-white/90">{match.homeScore} - {match.awayScore}</span></div>
                                      {(m.halftime_home != null || m.halftime_away != null) && (
                                        <div>半场: <span className="font-mono text-white/90">{m.halftime_home ?? 0} - {m.halftime_away ?? 0}</span></div>
                                      )}
                                      <div className="text-[10px] text-white/20 mt-1">
                                        {match.kickoff ? new Date(match.kickoff).toLocaleString('zh-CN') : '--'}
                                      </div>
                                    </>
                                  ) : (
                                    <div className="text-white/20 text-xs">比赛尚未开始或进行中</div>
                                  )}
                                </div>
                              </div>

                              {/* 赔率信息 */}
                              <div className="bg-white/[0.03] rounded-lg p-3">
                                <div className="text-[10px] text-white/30 mb-2 uppercase tracking-wider">赔率 (1X2)</div>
                                {hasOdds ? (
                                  <div className="grid grid-cols-3 gap-2 text-center">
                                    <div>
                                      <div className="text-[10px] text-white/30">主胜</div>
                                      <div className="font-mono text-pitch-400 text-sm">{fmtOdds(m.home_odds)}</div>
                                    </div>
                                    <div>
                                      <div className="text-[10px] text-white/30">平局</div>
                                      <div className="font-mono text-ember-400 text-sm">{fmtOdds(m.draw_odds)}</div>
                                    </div>
                                    <div>
                                      <div className="text-[10px] text-white/30">客胜</div>
                                      <div className="font-mono text-frost-400 text-sm">{fmtOdds(m.away_odds)}</div>
                                    </div>
                                  </div>
                                ) : (
                                  <div className="text-white/20 text-xs">暂无赔率数据</div>
                                )}
                              </div>

                              {/* 预测详情 */}
                              <div className="bg-white/[0.03] rounded-lg p-3">
                                <div className="text-[10px] text-white/30 mb-2 uppercase tracking-wider">AI 预测</div>
                                {match.prediction ? (
                                  <div className="space-y-1.5">
                                    <div>
                                      <span className={`text-xs px-2 py-0.5 rounded font-medium ${resultColor[match.prediction] || 'text-white/30'}`}>
                                        {resultLabel[match.prediction] || match.prediction}
                                      </span>
                                      {match.confidence != null && (
                                        <span className="ml-2 text-xs text-white/40">
                                          置信度 {match.confidence > 1 ? match.confidence : Math.round(match.confidence * 100)}%
                                        </span>
                                      )}
                                    </div>
                                    <div className="text-[10px] text-white/20">
                                      {result && match.prediction === result ? '✅ 预测正确' :
                                       result && match.prediction !== result ? '❌ 预测失准' :
                                       '⏳ 等待赛果'}
                                    </div>
                                  </div>
                                ) : (
                                  <div className="text-white/20 text-xs">暂无预测数据</div>
                                )}
                              </div>
                            </div>
                          </td>
                        </tr>
                      )}
                    </>
                  )
                })}
              </tbody>
            </table>

            {sorted.length === 0 && (
              <div className="text-center py-16">
                <p className="text-white/20 text-sm">暂无匹配数据</p>
                <p className="text-white/10 text-xs mt-1">尝试调整筛选条件</p>
              </div>
            )}
          </div>
        )}

        {/* 分页控件 */}
        {!loading && total > pageSize && (
          <div className="flex items-center justify-between px-4 py-3 border-t border-white/[0.06]">
            <span className="text-xs text-white/30">
              共 {total} 场 · 第 {page * pageSize + 1}-{Math.min((page + 1) * pageSize, total)} 场
            </span>
            <div className="flex gap-1">
              <button disabled={page === 0} onClick={() => setPage(0)}
                className="px-2 py-1 text-[10px] rounded bg-white/[0.04] text-white/40 hover:text-white/70 disabled:opacity-30 disabled:cursor-not-allowed"
              >首页</button>
              <button disabled={page === 0} onClick={() => setPage(p => p - 1)}
                className="px-2 py-1 text-[10px] rounded bg-white/[0.04] text-white/40 hover:text-white/70 disabled:opacity-30 disabled:cursor-not-allowed"
              >上一页</button>
              <span className="px-2 py-1 text-[10px] text-white/40">{page + 1}/{Math.ceil(total / pageSize)}</span>
              <button disabled={(page + 1) * pageSize >= total} onClick={() => setPage(p => p + 1)}
                className="px-2 py-1 text-[10px] rounded bg-white/[0.04] text-white/40 hover:text-white/70 disabled:opacity-30 disabled:cursor-not-allowed"
              >下一页</button>
              <button disabled={(page + 1) * pageSize >= total}
                onClick={() => setPage(Math.floor((total - 1) / pageSize))}
                className="px-2 py-1 text-[10px] rounded bg-white/[0.04] text-white/40 hover:text-white/70 disabled:opacity-30 disabled:cursor-not-allowed"
              >末页</button>
            </div>
          </div>
        )}
      </motion.div>
    </div>
  )
}

import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { matchService, historicalService } from '@/services/api'
import type { Match, League } from '@/types'

// 结果映射
const resultLabel: Record<string, string> = { 'H': '主胜', 'D': '平局', 'A': '客胜' }
const resultColor: Record<string, string> = {
  'H': 'bg-pitch-500/20 text-pitch-400',
  'D': 'bg-ember-500/20 text-ember-400',
  'A': 'bg-frost-500/20 text-frost-400',
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

useEffect(() => {
  let ws: WebSocket | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let unmounted = false

  const connectWS = () => {
    if (unmounted) return

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.hostname}:9000/ws/realtime`
    ws = new WebSocket(wsUrl)

    ws.onopen = () => {
      console.log('✅ WebSocket 已连接')
      setWsConnected(true)
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        console.log('📡 实时数据:', data)

        if (data.type === 'match_update' && data.match) {
          // 合并更新：保留当前行的非推送字段（league/homeTeam 对象等），仅覆盖变化的字段
          setMatches(prev =>
            prev.map(m => m.id === data.match.id ? { ...m, ...data.match } : m)
          )
        }

        if (data.type === 'matches_list' && Array.isArray(data.matches)) {
          setMatches(data.matches)
        }
      } catch {
        // 非法 JSON，忽略该条消息
      }
    }

    ws.onerror = () => {
      console.warn('⚠️ WebSocket 连接异常')
    }

    ws.onclose = () => {
      console.log('❌ WebSocket 已断开')
      setWsConnected(false)

      // 3秒后自动重连
      if (!unmounted) {
        reconnectTimer = setTimeout(connectWS, 3000)
      }
    }
  }

  const fetchInitialData = async () => {
    try {
      setLoading(true)
      const [matchesRes, leaguesRes] = await Promise.all([
        matchService.getMatches(),
        historicalService.getLeagues(),
      ])
      setMatches(matchesRes.data?.data || [])
      setLeagues(leaguesRes.data?.data || [])
    } catch {
      setLeagues([
        { code: 'WC2026', name: '2026世界杯', country: '国际' },
        { code: 'EPL', name: '英超', country: '英格兰' },
        { code: 'LA_LIGA', name: '西甲', country: '西班牙' },
      ])
    } finally {
      setLoading(false)
    }
  }

  // 首次加载 HTTP 数据
  fetchInitialData()

  // 建立 WebSocket 实时连接
  connectWS()

  // 组件卸载时清理
  return () => {
    unmounted = true
    ws?.close()
    if (reconnectTimer) clearTimeout(reconnectTimer)
  }
}, [])

  const filteredMatches = (matches || []).filter((m) => {
    const matchesSearch = !searchQuery ||
      m.homeTeam.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      m.awayTeam.name.toLowerCase().includes(searchQuery.toLowerCase())
    const matchesLeague = selectedLeague === 'all' || m.league.code === selectedLeague
    const matchesStatus = selectedStatus === 'all' || 
      (selectedStatus === 'finished' && m.status === 'finished') ||
      (selectedStatus === 'upcoming' && m.status === 'upcoming') ||
      (selectedStatus === 'live' && m.status === 'live')
    return matchesSearch && matchesLeague && matchesStatus
  })

  // 获取结果标识
  const getResult = (m: Match): string => {
    if (m.status !== 'finished' || m.homeScore == null || m.awayScore == null) return ''
    if (m.homeScore > m.awayScore) return 'H'
    if (m.homeScore === m.awayScore) return 'D'
    return 'A'
  }

  // 获取赔率格式化
  const fmtOdds = (v: number | undefined): string => {
    if (v == null || v === 0) return '--'
    return v.toFixed(2)
  }

  return (
    <div className="space-y-6">
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-black font-display text-white tracking-tight">数据探索</h1>
            <p className="text-sm text-white/40 mt-1">比赛数据查询 · 比分详情 · 赔率分析</p>
          </div>
          <div className="flex items-center gap-2">
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

          {/* 联赛筛选 */}
          <div className="flex gap-1.5 flex-wrap">
            <button onClick={() => setSelectedLeague('all')}
              className={`px-2.5 py-1 rounded-md text-[10px] font-medium transition-all ${
                selectedLeague === 'all' ? 'bg-pitch-500/20 text-pitch-400' : 'text-white/30 hover:text-white/60 hover:bg-white/[0.03]'
              }`}>全部联赛</button>
            {leagues.slice(0, 6).map((l) => (
              <button key={l.code} onClick={() => setSelectedLeague(l.code)}
                className={`px-2.5 py-1 rounded-md text-[10px] font-medium transition-all ${
                  selectedLeague === l.code ? 'bg-pitch-500/20 text-pitch-400' : 'text-white/30 hover:text-white/60 hover:bg-white/[0.03]'
                }`}>{l.name}</button>
            ))}
          </div>

          {/* 状态筛选 */}
          <div className="flex gap-1.5">
            {[
              ['all', '全部'], ['finished', '已结束'], ['upcoming', '未开始'], ['live', '进行中']
            ].map(([val, label]) => (
              <button key={val} onClick={() => setSelectedStatus(val)}
                className={`px-2.5 py-1 rounded-md text-[10px] font-medium transition-all ${
                  selectedStatus === val ? 'bg-frost-500/20 text-frost-400' : 'text-white/30 hover:text-white/60 hover:bg-white/[0.03]'
                }`}>{label}</button>
            ))}
          </div>
        </div>
        <div className="text-[10px] text-white/20 mt-2">
          共 {filteredMatches.length} 场比赛
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
                  <th className="text-left py-3 px-4 text-white/30 font-medium uppercase tracking-wider">联赛</th>
                  <th className="text-right py-3 px-3 text-white/30 font-medium uppercase tracking-wider">主队</th>
                  <th className="text-center py-3 px-2 text-white/30 font-medium uppercase tracking-wider w-[100px]">比分</th>
                  <th className="text-left py-3 px-3 text-white/30 font-medium uppercase tracking-wider">客队</th>
                  <th className="text-center py-3 px-2 text-white/30 font-medium uppercase tracking-wider">结果</th>
                  <th className="text-center py-3 px-3 text-white/30 font-medium uppercase tracking-wider">时间</th>
                  <th className="text-center py-3 px-3 text-white/30 font-medium uppercase tracking-wider hidden md:table-cell">赔率(主/平/客)</th>
                  <th className="text-center py-3 px-2 text-white/30 font-medium uppercase tracking-wider">预测</th>
                </tr>
              </thead>
              <tbody>
                {filteredMatches.map((match, i) => {
                  const result = getResult(match)
                  const isExpanded = expandedRow === match.id
                  const hasOdds = (match as any).home_odds != null || (match as any).draw_odds != null || (match as any).away_odds != null

                  return (
                    <motion.tr
                      key={match.id}
                      initial={{ opacity: 0, y: 5 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: i * 0.015 }}
                      onClick={() => setExpandedRow(isExpanded ? null : match.id)}
                      className="border-b border-white/[0.03] cursor-pointer hover:bg-white/[0.02] transition-colors group"
                    >
                      {/* 联赛 */}
                      <td className="py-2.5 px-4">
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-white/[0.04] text-white/40 whitespace-nowrap">
                          {match.league.name}
                        </span>
                      </td>

                      {/* 主队 */}
                      <td className="py-2.5 px-3 text-right">
                        <span className={`font-medium whitespace-nowrap ${
                          match.status === 'finished' && result === 'H' ? 'text-pitch-400' : 'text-white/80'
                        }`}>{match.homeTeam.name}</span>
                      </td>

                      {/* 比分 */}
                      <td className="py-2.5 px-2 text-center">
                        {match.status === 'finished' && match.homeScore != null ? (
                          <div>
                            <span className={`font-bold font-display text-sm ${
                              result === 'H' ? 'text-pitch-400' :
                              result === 'D' ? 'text-ember-400' :
                              'text-frost-400'
                            }`}>
                              {match.homeScore} - {match.awayScore}
                            </span>
                            {/* 半场比分 (如果有数据) */}
                            {((match as any).halftime_home != null || (match as any).halftime_away != null) && (
                              <div className="text-[9px] text-white/20 mt-0.5">
                                (HT {(match as any).halftime_home ?? 0}-{(match as any).halftime_away ?? 0})
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

                      {/* 客队 */}
                      <td className="py-2.5 px-3">
                        <span className={`font-medium whitespace-nowrap ${
                          match.status === 'finished' && result === 'A' ? 'text-frost-400' : 'text-white/80'
                        }`}>{match.awayTeam.name}</span>
                      </td>

                      {/* 结果标签 */}
                      <td className="py-2.5 px-2 text-center">
                        {result ? (
                          <span className={`text-[10px] px-2 py-0.5 rounded font-medium ${resultColor[result]}`}>
                            {resultLabel[result]}
                          </span>
                        ) : (
                          <span className="text-white/15">--</span>
                        )}
                      </td>

                      {/* 时间 */}
                      <td className="py-2.5 px-3 text-center">
                        <span className="text-white/30 whitespace-nowrap">
                          {match.kickoff ? new Date(match.kickoff).toLocaleDateString('zh-CN', {
                            month: '2-digit', day: '2-digit',
                          }) : '--'}
                        </span>
                      </td>

                      {/* 赔率 (桌面端显示) */}
                      <td className="py-2.5 px-3 text-center hidden md:table-cell">
                        {hasOdds ? (
                          <span className="font-mono text-white/25 text-[10px]">
                            {fmtOdds((match as any).home_odds)} / {fmtOdds((match as any).draw_odds)} / {fmtOdds((match as any).away_odds)}
                          </span>
                        ) : (
                          <span className="text-white/10">--</span>
                        )}
                      </td>

                      {/* 预测 */}
                      <td className="py-2.5 px-2 text-center">
                        {match.prediction ? (
                          <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${resultColor[match.prediction] || 'text-white/30'}`}>
                            {resultLabel[match.prediction] || match.prediction}
                            {match.confidence != null && (
                              <span className="ml-1 text-white/20">{Math.round(match.confidence * 100) || match.confidence}%</span>
                            )}
                          </span>
                        ) : (
                          <span className="text-white/15">--</span>
                        )}
                      </td>
                    </motion.tr>
                  )
                })}
              </tbody>
            </table>

            {filteredMatches.length === 0 && (
              <div className="text-center py-16">
                <p className="text-white/20 text-sm">暂无匹配数据</p>
                <p className="text-white/10 text-xs mt-1">尝试调整筛选条件</p>
              </div>
            )}
          </div>
        )}
      </motion.div>
    </div>
  )
}

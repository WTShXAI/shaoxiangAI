import { useState, useCallback } from 'react'
import { motion } from 'framer-motion'
import { matchResultService } from '@/services/api'
import type { MatchResult as MatchResultRow } from '@/services/api'
import PageHeader from '@/components/layout/PageHeader'

// ── 格式化 ──
function fmtDate(d: string): string {
  if (!d) return '--'
  try {
    const dt = new Date(d)
    if (isNaN(dt.getTime())) return d.slice(0, 10)
    return dt.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
  } catch { return d.slice(0, 10) }
}
const resultBadge = (r: string) => {
  if (r === 'H') return { t: '主胜', c: 'bg-frost-500/15 text-frost-300' }
  if (r === 'D') return { t: '平局', c: 'bg-white/8 text-white/60' }
  if (r === 'A') return { t: '客胜', c: 'bg-ember-500/15 text-ember-300' }
  return { t: r || '--', c: 'bg-white/5 text-white/40' }
}
const srcLabel = (s: string) => s === 'recent' ? '近期' : '历史'

// ============================================
// 单场赛果行
// ============================================
function ResultRow({ r }: { r: MatchResultRow }) {
  const badge = resultBadge(r.result)
  const htStr = (r.ht_h !== null && r.ht_a !== null) ? `半场 ${r.ht_h}-${r.ht_a}` : null
  return (
    <div className="card px-4 py-3 mb-2 hover:border-white/10 transition-colors">
      <div className="flex items-center gap-4">
        {/* 日期 + 联赛 */}
        <div className="flex-shrink-0 w-16 text-center">
          <div className="text-xs font-mono text-white/60">{fmtDate(r.date)}</div>
          <div className="text-[9px] text-white/30 mt-0.5">{srcLabel(r.source)}</div>
        </div>
        {/* 比分主体 */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3">
            <span className="text-sm font-bold text-white truncate flex-1 text-right">{r.home}</span>
            <div className="flex items-center gap-2 flex-shrink-0">
              <span className="text-xl font-black font-display font-mono text-white">{r.home_score}</span>
              <span className="text-xs text-white/30">-</span>
              <span className="text-xl font-black font-display font-mono text-white">{r.away_score}</span>
            </div>
            <span className="text-sm font-bold text-white truncate flex-1">{r.away}</span>
          </div>
          <div className="flex items-center gap-2 mt-1">
            <span className="text-[10px] text-white/40 truncate">{r.league || '未知联赛'}</span>
            {htStr && <span className="text-[10px] text-white/30 font-mono">· {htStr}</span>}
          </div>
        </div>
        {/* 结果标签 */}
        <span className={`text-[10px] px-2 py-1 rounded font-bold flex-shrink-0 ${badge.c}`}>
          {badge.t}
        </span>
      </div>
    </div>
  )
}

// ============================================
// 主页面：赛果查询
// ============================================
export default function MatchResults() {
  const [q, setQ] = useState('')
  const [league, setLeague] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [results, setResults] = useState<MatchResultRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [searched, setSearched] = useState(false)

  const search = useCallback(async () => {
    setLoading(true); setError(null); setSearched(true)
    try {
      const res = await matchResultService.getResults({
        q: q.trim() || undefined,
        league: league.trim() || undefined,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        limit: 100,
      })
      const data = (res.data as any)?.data || res.data
      if (data?.error) { setError(data.error); setResults([]) }
      else { setResults(data?.results || []) }
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || '查询失败')
      setResults([])
    } finally {
      setLoading(false)
    }
  }, [q, league, dateFrom, dateTo])

  const reset = () => {
    setQ(''); setLeague(''); setDateFrom(''); setDateTo('')
    setResults([]); setError(null); setSearched(false)
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="赛果查询"
        subtitle="历史 + 近期已完赛比分 (含 2026 世界杯) · 数据源 football_data.db"
      />

      {/* 搜索栏 */}
      <div className="card p-4 space-y-3">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <label className="text-[10px] text-white/40 uppercase tracking-wider">队名 / 关键词</label>
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && search()}
              placeholder="如: 阿根廷, England, 巴西"
              className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-white/30 focus:border-accent/40 focus:outline-none"
            />
          </div>
          <div>
            <label className="text-[10px] text-white/40 uppercase tracking-wider">联赛</label>
            <input
              value={league}
              onChange={(e) => setLeague(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && search()}
              placeholder="如: 世界杯, 英超, La Liga"
              className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-white/30 focus:border-accent/40 focus:outline-none"
            />
          </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 items-end">
          <div>
            <label className="text-[10px] text-white/40 uppercase tracking-wider">开始日期</label>
            <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)}
              className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:border-accent/40 focus:outline-none" />
          </div>
          <div>
            <label className="text-[10px] text-white/40 uppercase tracking-wider">结束日期</label>
            <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)}
              className="w-full mt-1 bg-white/[0.03] border border-white/10 rounded-lg px-3 py-2 text-sm text-white focus:border-accent/40 focus:outline-none" />
          </div>
          <button onClick={search} disabled={loading}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-accent/15 text-accent border border-accent/25 hover:bg-accent/25 transition-colors disabled:opacity-40">
            {loading ? '查询中…' : '🔍 查询'}
          </button>
          <button onClick={reset}
            className="px-4 py-2 rounded-lg text-sm font-medium text-white/50 hover:text-white/80 hover:bg-white/[0.04] transition-colors">
            重置
          </button>
        </div>
      </div>

      {/* 结果 */}
      {loading && (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="card h-16 bg-white/[0.02] animate-pulse" />
          ))}
        </div>
      )}

      {error && !loading && (
        <div className="card p-6 text-center">
          <div className="text-3xl mb-2">⚠️</div>
          <p className="text-sm text-white/70">{error}</p>
        </div>
      )}

      {!loading && !error && searched && results.length === 0 && (
        <div className="card p-10 text-center">
          <p className="text-sm text-white/50">未找到匹配赛果</p>
          <p className="text-[11px] text-white/30 mt-1">尝试调整队名/联赛/日期范围</p>
        </div>
      )}

      {!loading && !error && results.length > 0 && (
        <div>
          <div className="flex items-center justify-between mb-3 px-1">
            <h3 className="text-xs font-bold uppercase tracking-widest text-white/50">赛果列表</h3>
            <span className="text-[10px] text-white/30">共 {results.length} 场 · 按日期倒序</span>
          </div>
          {results.map((r, i) => (
            <ResultRow key={`${r.date}-${r.home}-${r.away}-${i}`} r={r} />
          ))}
        </div>
      )}

      {!searched && !loading && (
        <div className="card p-10 text-center">
          <div className="w-14 h-14 rounded-2xl bg-field-500/10 border border-field-500/15 flex items-center justify-center mx-auto mb-4">
            <svg className="w-6 h-6 text-field-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5">
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" />
            </svg>
          </div>
          <p className="text-sm text-white/50 font-medium">输入条件查询已完赛比分</p>
          <p className="text-[11px] text-white/30 mt-1">支持队名、联赛、日期范围过滤 · 覆盖 2012 至今</p>
        </div>
      )}
    </div>
  )
}

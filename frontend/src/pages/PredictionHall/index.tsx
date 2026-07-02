import { useState, useEffect, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useAppStore } from '@/store'
import { predictionService, matchService } from '@/services/api'
import type { Match, Prediction, PredictionStats } from '@/types'
// ============================================
// 子组件：焦点战轮播
// ============================================
function FeaturedMatchCarousel({ matches }: { matches: Match[] }) {
  const [current, setCurrent] = useState(0)
  const featured = matches.slice(0, 5)
  useEffect(() => {
    if (featured.length === 0) return
    const timer = setInterval(() => setCurrent((c) => (c + 1) % featured.length), 5000)
    return () => clearInterval(timer)
  }, [featured.length])
  if (featured.length === 0) return null
  const match = featured[current]
  return (
    <div className="relative h-48 rounded-2xl overflow-hidden group">
      {/* 背景渐变 */}
      <div className="absolute inset-0 bg-gradient-to-r from-pitch-950/80 via-pitch-900/40 to-frost-950/80" />
      <div className="absolute inset-0 bg-[url('data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iNjAiIGhlaWdodD0iNjAiIHZpZXdCb3g9IjAgMCA2MCA2MCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48ZyBmaWxsPSJub25lIiBmaWxsLXJ1bGU9ImV2ZW5vZGQiPjxnIGZpbGw9IiNmZmYiIGZpbGwtb3BhY2l0eT0iMC4wMyI+PGNpcmNsZSBjeD0iMzAiIGN5PSIzMCIgcj0iMiIvPjwvZz48L2c+PC9zdmc+')] opacity-50" />
      {/* 内容 */}
      <div className="relative h-full flex items-center justify-between px-8">
        {/* 主队 */}
        <div className="text-center">
          <div className="w-16 h-16 rounded-full bg-white/[0.06] border border-white/[0.1] flex items-center justify-center mb-2 mx-auto">
            <span className="text-2xl font-black font-display text-white/80">{match.homeTeam.shortName}</span>
          </div>
          <p className="text-lg font-bold font-display text-white">{match.homeTeam.name}</p>
        </div>
        {/* 比分/VS */}
        <div className="text-center">
          <div className="text-5xl font-black font-display text-white/20 tracking-widest">VS</div>
          <div className="mt-2 flex items-center gap-2 justify-center">
            <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase bg-pitch-500/20 text-pitch-400 border border-pitch-500/20">
              焦点战
            </span>
            <span className="text-xs text-white/40">{new Date(match.kickoff).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</span>
          </div>
        </div>
        {/* 客队 */}
        <div className="text-center">
          <div className="w-16 h-16 rounded-full bg-white/[0.06] border border-white/[0.1] flex items-center justify-center mb-2 mx-auto">
            <span className="text-2xl font-black font-display text-white/80">{match.awayTeam.shortName}</span>
          </div>
          <p className="text-lg font-bold font-display text-white">{match.awayTeam.name}</p>
        </div>
      </div>
      {/* 指示点 */}
      <div className="absolute bottom-3 left-1/2 -translate-x-1/2 flex gap-1.5">
        {featured.map((_, i) => (
          <button
            key={i}
            onClick={() => setCurrent(i)}
            className={`w-1.5 h-1.5 rounded-full transition-all duration-300 ${
              i === current ? 'w-4 bg-pitch-400' : 'bg-white/20 hover:bg-white/40'
            }`}
          />
        ))}
      </div>
    </div>
  )
}
// ============================================
// 子组件：比赛卡片
// ============================================
function MatchCard({ match, prediction, index }: { match: Match; prediction?: Prediction; index: number }) {
  const [isHovered, setIsHovered] = useState(false)
  const homeProb = prediction?.probabilities.home ?? 0.33
  const drawProb = prediction?.probabilities.draw ?? 0.34
  const awayProb = prediction?.probabilities.away ?? 0.33
  const confidence = prediction?.confidence ?? 0
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.05, duration: 0.4 }}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      className="glass-card-hover p-4 cursor-pointer relative overflow-hidden group"
    >
      {/* 悬停光效 */}
      <motion.div
        initial={false}
        animate={{ opacity: isHovered ? 1 : 0 }}
        className="absolute -inset-1 bg-gradient-to-r from-pitch-500/5 via-transparent to-frost-500/5 blur-xl pointer-events-none"
      />
      {/* 联赛标签 */}
      <div className="flex items-center justify-between mb-3">
        <span className="text-[10px] font-medium uppercase tracking-wider text-white/30">{match.league.name}</span>
        <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${
          match.status === 'live' ? 'bg-danger-500/20 text-danger-400' :
          match.status === 'upcoming' ? 'bg-pitch-500/20 text-pitch-400' :
          'bg-white/5 text-white/30'
        }`}>
          {match.status === 'live' ? '● 进行中' : match.status === 'upcoming' ? '即将开始' : '已结束'}
        </span>
      </div>
      {/* 对阵信息 */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex-1 text-center">
          <p className="text-sm font-bold font-display text-white/90 truncate">{match.homeTeam.name}</p>
          <p className="text-[10px] text-white/30 mt-0.5">{match.homeTeam.form?.join(' ') || '--'}</p>
        </div>
        <div className="w-16 text-center">
          <div className="text-2xl font-black font-display text-white/60">
            {match.status === 'finished' ? `${match.homeScore ?? '-'} - ${match.awayScore ?? '-'}` : 'VS'}
          </div>
        </div>
        <div className="flex-1 text-center">
          <p className="text-sm font-bold font-display text-white/90 truncate">{match.awayTeam.name}</p>
          <p className="text-[10px] text-white/30 mt-0.5">{match.awayTeam.form?.join(' ') || '--'}</p>
        </div>
      </div>
      {/* 概率条 */}
      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-medium text-pitch-400 w-6 text-right">主{Math.round(homeProb * 100)}%</span>
          <div className="probability-bar flex-1">
            <motion.div
              initial={{ width: 0 }}
              animate={{ width: `${homeProb * 100}%` }}
              transition={{ delay: index * 0.05 + 0.3, duration: 0.8, ease: 'easeOut' }}
              className="probability-fill bg-gradient-to-r from-pitch-600 to-pitch-400"
            />
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-medium text-ember-400 w-6 text-right">平{Math.round(drawProb * 100)}%</span>
          <div className="probability-bar flex-1">
            <motion.div
              initial={{ width: 0 }}
              animate={{ width: `${drawProb * 100}%` }}
              transition={{ delay: index * 0.05 + 0.4, duration: 0.8, ease: 'easeOut' }}
              className="probability-fill bg-gradient-to-r from-ember-600 to-ember-400"
            />
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-medium text-frost-400 w-6 text-right">客{Math.round(awayProb * 100)}%</span>
          <div className="probability-bar flex-1">
            <motion.div
              initial={{ width: 0 }}
              animate={{ width: `${awayProb * 100}%` }}
              transition={{ delay: index * 0.05 + 0.5, duration: 0.8, ease: 'easeOut' }}
              className="probability-fill bg-gradient-to-r from-frost-600 to-frost-400"
            />
          </div>
        </div>
      </div>
      {/* 信心指数 */}
      <div className="mt-3 flex items-center justify-between">
        <span className="text-[10px] text-white/30">信心指数</span>
        <div className="flex items-center gap-1">
          {[1, 2, 3, 4, 5].map((star) => (
            <svg
              key={star}
              className={`w-3 h-3 ${star <= Math.round(confidence * 5) ? 'text-ember-400' : 'text-white/10'}`}
              fill="currentColor"
              viewBox="0 0 20 20"
            >
              <path d="M9.049 2.927c.3-.921 1.603-.921 1.902 0l1.07 3.292a1 1 0 00.95.69h3.462c.969 0 1.371 1.24.588 1.81l-2.8 2.034a1 1 0 00-.364 1.118l1.07 3.292c.3.921-.755 1.688-1.54 1.118l-2.8-2.034a1 1 0 00-1.175 0l-2.8 2.034c-.784.57-1.838-.197-1.539-1.118l1.07-3.292a1 1 0 00-.364-1.118L2.98 8.72c-.783-.57-.38-1.81.588-1.81h3.461a1 1 0 00.951-.69l1.07-3.292z" />
            </svg>
          ))}
        </div>
      </div>
      {/* 比分预测 */}
      {prediction?.score && (
        <div className="mt-2 text-center">
          <span className="text-[10px] text-white/20">预测比分：</span>
          <span className="text-xs font-bold font-display text-white/50">{prediction.score.home} - {prediction.score.away}</span>
        </div>
      )}
    </motion.div>
  )
}
// ============================================
// 子组件：统计概览面板
// ============================================
function StatsOverview({ stats }: { stats: PredictionStats | null }) {
  return (
    <div className="glass-card p-5 space-y-5">
      <h3 className="text-xs font-bold uppercase tracking-widest text-white/30">预测统计</h3>
      <div className="space-y-4">
        {/* 今日准确率 */}
        <div>
          <p className="stat-label">今日准确率</p>
          <div className="flex items-baseline gap-2">
            <span className="stat-value text-pitch-400">{stats ? `${Math.round(stats.todayAccuracy * 100)}%` : '--'}</span>
            <span className="text-xs text-white/30">总体 {stats ? `${Math.round(stats.overallAccuracy * 100)}%` : '--'}</span>
          </div>
          <div className="probability-bar mt-2">
            <motion.div
              initial={{ width: 0 }}
              animate={{ width: stats ? `${stats.todayAccuracy * 100}%` : '0%' }}
              transition={{ duration: 1, ease: 'easeOut' }}
              className="probability-fill bg-gradient-to-r from-pitch-600 to-pitch-400"
            />
          </div>
        </div>
        {/* 总预测数 */}
        <div>
          <p className="stat-label">总预测数</p>
          <span className="stat-value text-white/80">{stats?.totalPredictions?.toLocaleString() ?? '--'}</span>
        </div>
        {/* 热门赛事 */}
        <div>
          <p className="stat-label mb-2">热门赛事</p>
          <div className="space-y-1.5">
            {stats?.hotLeagues?.slice(0, 4).map((league) => (
              <div key={league.league} className="flex items-center justify-between text-xs">
                <span className="text-white/60">{league.league}</span>
                <span className="text-white/30">{league.count}场</span>
              </div>
            )) ?? <span className="text-xs text-white/20">暂无数据</span>}
          </div>
        </div>
      </div>
      {/* 近期表现 */}
      {stats?.recentResults && stats.recentResults.length > 0 && (
        <div>
          <p className="stat-label mb-2">近期表现</p>
          <div className="flex gap-1">
            {stats.recentResults.slice(-7).map((r, i) => {
              const rate = r.total > 0 ? r.correct / r.total : 0
              return (
                <div key={i} className="flex-1 text-center">
                  <div className={`h-12 rounded-lg ${rate > 0.6 ? 'bg-pitch-500/30' : rate > 0.3 ? 'bg-ember-500/20' : 'bg-danger-500/20'} flex items-center justify-center`}>
                    <span className="text-[10px] font-bold font-display">{Math.round(rate * 100)}%</span>
                  </div>
                  <span className="text-[8px] text-white/20 mt-0.5 block">{new Date(r.date).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
// ============================================
// 子组件：预测结果时间线
// ============================================
function PredictionTimeline({ predictions }: { predictions: Prediction[] }) {
  return (
    <div className="glass-card p-5">
      <h3 className="text-xs font-bold uppercase tracking-widest text-white/30 mb-4">预测时间线</h3>
      <div className="space-y-3">
        {predictions.slice(0, 6).map((pred, i) => (
          <motion.div
            key={i}
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: i * 0.05 }}
            className="flex items-center gap-3 p-2 rounded-xl hover:bg-white/[0.02] transition-colors"
          >
            <div className={`w-1.5 h-1.5 rounded-full ${
              pred.confidence > 0.7 ? 'bg-pitch-400' : pred.confidence > 0.4 ? 'bg-ember-400' : 'bg-danger-400'
            }`} />
            <div className="flex-1 min-w-0">
              <p className="text-xs text-white/60 truncate">
                预测 {pred.result === 'home' ? '主胜' : pred.result === 'draw' ? '平局' : '客胜'}
              </p>
              <p className="text-[10px] text-white/20">{new Date(pred.timestamp).toLocaleString('zh-CN')}</p>
            </div>
            <span className="text-[10px] font-medium text-white/30">{Math.round(pred.confidence * 100)}%</span>
          </motion.div>
        ))}
      </div>
    </div>
  )
}
// ============================================
// 主页面：预测大厅
// ============================================
export default function PredictionHall() {
  const [matches, setMatches] = useState<Match[]>([])
  const [predictions, setPredictions] = useState<Map<string, Prediction>>(new Map())
  const [stats, setStats] = useState<PredictionStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [timeFilter, setTimeFilter] = useState<'all' | 'today' | 'week'>('all')
  const { sidebarCollapsed } = useAppStore()
  useEffect(() => {
    const fetchData = async () => {
      try {
        setLoading(true)
        const [matchesRes, statsRes] = await Promise.all([
          matchService.getMatches(),
          predictionService.getPredictionStats(),
        ])
        setMatches(matchesRes.data?.data || [])
        setStats(statsRes.data?.data || null)
        // 为每场比赛获取预测（示例：取前8场）
        const predMap = new Map<string, Prediction>()
        const topMatches = (matchesRes.data?.data || []).slice(0, 8)
        await Promise.all(
          topMatches.map(async (match) => {
            try {
              const res = await predictionService.predictSingle({
                home_team: match.homeTeam.name,
                away_team: match.awayTeam.name,
                league: match.league.code,
              } as any)
              predMap.set(match.id, res.data?.data || null)
            } catch {
              // 个别预测失败不影响整体
            }
          })
        )
        setPredictions(predMap)
      } catch (err) {
        console.error('获取数据失败:', err)
      } finally {
        setLoading(false)
      }
    }
    fetchData()
    const interval = setInterval(fetchData, 30000)
    return () => clearInterval(interval)
  }, [])
  // 筛选比赛
  const filteredMatches = useMemo(() => {
    if (!matches || !Array.isArray(matches)) return []
    const now = new Date()
    return matches.filter((m) => {
      const matchDate = new Date(m.kickoff)
      const diffDays = (matchDate.getTime() - now.getTime()) / (1000 * 60 * 60 * 24)
      if (timeFilter === 'all') return true
      if (timeFilter === 'today') return diffDays >= -1 && diffDays <= 1
      if (timeFilter === 'week') return diffDays >= -1 && diffDays <= 7
      return true
    })
  }, [matches, timeFilter])
  return (
    <div className="space-y-6">
      {/* 页面标题 */}
      <motion.div
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        className="flex items-center justify-between"
      >
        <div>
          <h1 className="text-2xl font-black font-display text-white tracking-tight">
            预测大厅
          </h1>
          <p className="text-sm text-white/40 mt-1">实时比赛预测与数据分析</p>
        </div>
        <div className="flex items-center gap-2 bg-white/[0.03] rounded-xl p-1">
          {(['all', 'today', 'week'] as const).map((f) => (
            <button
              key={f}
              onClick={() => setTimeFilter(f)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all duration-200 ${
                timeFilter === f
                  ? 'bg-pitch-500/20 text-pitch-400 shadow-[0_0_12px_rgba(34,197,94,0.08)]'
                  : 'text-white/30 hover:text-white/60'
              }`}
            >
              {f === 'all' ? '全部' : f === 'today' ? '今日' : '本周'}
            </button>
          ))}
        </div>
      </motion.div>
      {/* 焦点战轮播 */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1 }}
      >
        <FeaturedMatchCarousel matches={matches} />
      </motion.div>
      {/* 主内容区域：比赛卡片网格 + 右侧面板 */}
      <div className="grid grid-cols-1 xl:grid-cols-4 gap-6">
        {/* 比赛卡片网格 */}
        <div className="xl:col-span-3">
          {loading ? (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {Array.from({ length: 6 }).map((_, i) => (
                <div key={i} className="glass-card p-4">
                  <div className="skeleton h-4 w-20 mb-4" />
                  <div className="flex items-center justify-between mb-4">
                    <div className="skeleton h-8 w-20" />
                    <div className="skeleton h-8 w-12" />
                    <div className="skeleton h-8 w-20" />
                  </div>
                  <div className="space-y-2">
                    <div className="skeleton h-2 w-full" />
                    <div className="skeleton h-2 w-full" />
                    <div className="skeleton h-2 w-full" />
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              <AnimatePresence>
                {filteredMatches.slice(0, 12).map((match, index) => (
                  <MatchCard
                    key={match.id}
                    match={match}
                    prediction={predictions.get(match.id)}
                    index={index}
                  />
                ))}
              </AnimatePresence>
            </div>
          )}
        </div>
        {/* 右侧面板 */}
        <div className="space-y-4">
          <StatsOverview stats={stats} />
          <PredictionTimeline predictions={Array.from(predictions.values())} />
        </div>
      </div>
    </div>
  )
}
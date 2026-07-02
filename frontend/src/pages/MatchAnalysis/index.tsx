import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { predictionService, matchService, featureService } from '@/services/api'
import type { Match, Prediction, TeamFeatures } from '@/types'
// ============================================
// 子组件：球队信息面板
// ============================================
function TeamInfoPanel({ team, side, features }: { team: { name: string; shortName: string; form?: string[] }; side: 'home' | 'away'; features?: TeamFeatures }) {
  const isHome = side === 'home'
  return (
    <motion.div
      initial={{ opacity: 0, x: isHome ? -20 : 20 }}
      animate={{ opacity: 1, x: 0 }}
      className={`flex-1 ${isHome ? 'text-right' : 'text-left'}`}
    >
      <div className={`inline-flex flex-col ${isHome ? 'items-end' : 'items-start'}`}>
        <div className={`w-20 h-20 rounded-2xl bg-gradient-to-br ${isHome ? 'from-pitch-500/20 to-pitch-700/20' : 'from-frost-500/20 to-frost-700/20'} border border-white/[0.06] flex items-center justify-center mb-3`}>
          <span className="text-3xl font-black font-display text-white/70">{team.shortName}</span>
        </div>
        <h2 className="text-xl font-bold font-display text-white">{team.name}</h2>
        {team.form && (
          <div className={`flex gap-1 mt-2 ${isHome ? 'justify-end' : 'justify-start'}`}>
            {team.form.map((result, i) => (
              <span key={i} className={`w-5 h-5 rounded text-[9px] font-bold flex items-center justify-center ${
                result === 'W' ? 'bg-pitch-500/30 text-pitch-400' :
                result === 'L' ? 'bg-danger-500/30 text-danger-400' :
                'bg-white/10 text-white/40'
              }`}>
                {result}
              </span>
            ))}
          </div>
        )}
        {features && (
          <div className="mt-3 space-y-1">
            <div className="flex items-center gap-2 text-[10px]">
              <span className="text-white/30">进攻</span>
              <div className="w-16 h-1 rounded-full bg-white/5">
                <div className={`h-full rounded-full ${isHome ? 'bg-pitch-400' : 'bg-frost-400'}`} style={{ width: `${features.attack * 10}%` }} />
              </div>
              <span className="text-white/50 font-mono">{features.attack.toFixed(1)}</span>
            </div>
            <div className="flex items-center gap-2 text-[10px]">
              <span className="text-white/30">防守</span>
              <div className="w-16 h-1 rounded-full bg-white/5">
                <div className={`h-full rounded-full ${isHome ? 'bg-pitch-400' : 'bg-frost-400'}`} style={{ width: `${features.defense * 10}%` }} />
              </div>
              <span className="text-white/50 font-mono">{features.defense.toFixed(1)}</span>
            </div>
          </div>
        )}
      </div>
    </motion.div>
  )
}
// ============================================
// 子组件：概率仪表盘
// ============================================
function ProbabilityGauge({ probabilities, result }: { probabilities: { home: number; draw: number; away: number }; result: string }) {
  const segments = [
    { label: '主胜', value: probabilities.home, color: 'from-pitch-600 to-pitch-400', result: 'home' },
    { label: '平局', value: probabilities.draw, color: 'from-ember-600 to-ember-400', result: 'draw' },
    { label: '客胜', value: probabilities.away, color: 'from-frost-600 to-frost-400', result: 'away' },
  ]
  return (
    <div className="glass-card p-6">
      <h3 className="text-xs font-bold uppercase tracking-widest text-white/30 mb-4">概率分布</h3>
      <div className="flex gap-2 h-32 items-end">
        {segments.map((seg, i) => (
          <div key={seg.label} className="flex-1 flex flex-col items-center gap-2">
            <motion.span
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 0.5 + i * 0.1 }}
              className={`text-xs font-bold font-display ${result === seg.result ? 'text-white' : 'text-white/30'}`}
            >
              {Math.round(seg.value * 100)}%
            </motion.span>
            <motion.div
              initial={{ height: 0 }}
              animate={{ height: `${seg.value * 100}%` }}
              transition={{ delay: 0.3 + i * 0.1, duration: 0.8, ease: 'easeOut' }}
              className={`w-full rounded-t-lg bg-gradient-to-t ${seg.color} ${
                result === seg.result ? 'shadow-[0_0_16px_rgba(34,197,94,0.2)]' : ''
              }`}
              style={{ minHeight: seg.value > 0 ? 4 : 0 }}
            />
            <span className="text-[10px] text-white/30 font-medium">{seg.label}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
// ============================================
// 子组件：比分预测
// ============================================
function ScorePredictionPanel({ score, confidence }: { score: { home: number; away: number }; confidence: number }) {
  return (
    <div className="glass-card p-6 text-center">
      <h3 className="text-xs font-bold uppercase tracking-widest text-white/30 mb-4">比分预测</h3>
      <div className="flex items-center justify-center gap-6">
        <div className="text-center">
          <p className="text-[10px] text-white/30 mb-1">主队</p>
          <motion.span
            initial={{ scale: 0 }}
            animate={{ scale: 1 }}
            transition={{ type: 'spring', stiffness: 200, damping: 15 }}
            className="text-6xl font-black font-display text-pitch-400"
          >
            {score.home}
          </motion.span>
        </div>
        <div className="text-4xl font-black font-display text-white/20">:</div>
        <div className="text-center">
          <p className="text-[10px] text-white/30 mb-1">客队</p>
          <motion.span
            initial={{ scale: 0 }}
            animate={{ scale: 1 }}
            transition={{ type: 'spring', stiffness: 200, damping: 15, delay: 0.2 }}
            className="text-6xl font-black font-display text-frost-400"
          >
            {score.away}
          </motion.span>
        </div>
      </div>
      <div className="mt-4 flex items-center justify-center gap-2">
        <span className="text-[10px] text-white/30">信心指数</span>
        <div className="flex gap-0.5">
          {[1, 2, 3, 4, 5].map((star) => (
            <svg
              key={star}
              className={`w-4 h-4 ${star <= Math.round(confidence * 5) ? 'text-ember-400' : 'text-white/10'}`}
              fill="currentColor"
              viewBox="0 0 20 20"
            >
              <path d="M9.049 2.927c.3-.921 1.603-.921 1.902 0l1.07 3.292a1 1 0 00.95.69h3.462c.969 0 1.371 1.24.588 1.81l-2.8 2.034a1 1 0 00-.364 1.118l1.07 3.292c.3.921-.755 1.688-1.54 1.118l-2.8-2.034a1 1 0 00-1.175 0l-2.8 2.034c-.784.57-1.838-.197-1.539-1.118l1.07-3.292a1 1 0 00-.364-1.118L2.98 8.72c-.783-.57-.38-1.81.588-1.81h3.461a1 1 0 00.951-.69l1.07-3.292z" />
            </svg>
          ))}
        </div>
      </div>
    </div>
  )
}
// ============================================
// 子组件：分析标签页
// ============================================
const tabs = [
  { id: 'features', label: '特征分析' },
  { id: 'history', label: '历史交锋' },
  { id: 'form', label: '球队状态' },
  { id: 'model', label: '模型决策' },
]
function AnalysisTabs({ homeFeatures, awayFeatures }: { homeFeatures?: TeamFeatures; awayFeatures?: TeamFeatures }) {
  const [activeTab, setActiveTab] = useState('features')
  return (
    <div className="glass-card p-6">
      {/* 标签页导航 */}
      <div className="flex gap-1 mb-6 bg-white/[0.02] rounded-xl p-1">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex-1 py-2 rounded-lg text-xs font-medium transition-all duration-200 ${
              activeTab === tab.id
                ? 'bg-pitch-500/15 text-pitch-400 shadow-[0_0_12px_rgba(34,197,94,0.06)]'
                : 'text-white/30 hover:text-white/60'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <AnimatePresence mode="wait">
        <motion.div
          key={activeTab}
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -10 }}
          transition={{ duration: 0.2 }}
        >
          {activeTab === 'features' && (
            <div className="space-y-4">
              <h4 className="text-sm font-bold text-white/70">球队能力对比</h4>
              <div className="grid grid-cols-2 gap-4">
                {homeFeatures && awayFeatures && (
                  <>
                    {[
                      { key: 'attack', label: '进攻能力', home: homeFeatures.attack, away: awayFeatures.attack },
                      { key: 'defense', label: '防守能力', home: homeFeatures.defense, away: awayFeatures.defense },
                      { key: 'midfield', label: '中场控制', home: homeFeatures.midfield, away: awayFeatures.midfield },
                      { key: 'stamina', label: '体能状态', home: homeFeatures.stamina, away: awayFeatures.stamina },
                      { key: 'morale', label: '士气指数', home: homeFeatures.morale, away: awayFeatures.morale },
                    ].map((item) => {
                      const total = item.home + item.away
                      const homePct = total > 0 ? (item.home / total) * 100 : 50
                      return (
                        <div key={item.key} className="space-y-1">
                          <div className="flex justify-between text-[10px]">
                            <span className="text-pitch-400 font-medium">{item.home.toFixed(1)}</span>
                            <span className="text-white/30">{item.label}</span>
                            <span className="text-frost-400 font-medium">{item.away.toFixed(1)}</span>
                          </div>
                          <div className="h-1.5 rounded-full bg-white/5 flex">
                            <motion.div
                              initial={{ width: 0 }}
                              animate={{ width: `${homePct}%` }}
                              transition={{ duration: 0.8, ease: 'easeOut' }}
                              className="h-full rounded-l-full bg-gradient-to-r from-pitch-600 to-pitch-400"
                            />
                            <motion.div
                              initial={{ width: 0 }}
                              animate={{ width: `${100 - homePct}%` }}
                              transition={{ duration: 0.8, ease: 'easeOut' }}
                              className="h-full rounded-r-full bg-gradient-to-r from-frost-600 to-frost-400"
                            />
                          </div>
                        </div>
                      )
                    })}
                  </>
                )}
              </div>
            </div>
          )}
          {activeTab === 'history' && (
            <div className="text-center py-8">
              <p className="text-sm text-white/30">历史交锋数据加载中...</p>
            </div>
          )}
          {activeTab === 'form' && (
            <div className="text-center py-8">
              <p className="text-sm text-white/30">球队状态数据加载中...</p>
            </div>
          )}
          {activeTab === 'model' && (
            <div className="text-center py-8">
              <p className="text-sm text-white/30">模型决策过程加载中...</p>
            </div>
          )}
        </motion.div>
      </AnimatePresence>
    </div>
  )
}
// ============================================
// 主页面：比赛分析
// ============================================
export default function MatchAnalysis() {
  const [match, setMatch] = useState<Match | null>(null)
  const [prediction, setPrediction] = useState<Prediction | null>(null)
  const [homeFeatures, setHomeFeatures] = useState<TeamFeatures | undefined>()
  const [awayFeatures, setAwayFeatures] = useState<TeamFeatures | undefined>()
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    const fetchData = async () => {
      try {
        setLoading(true)
        // 获取下一场比赛
        const nextMatchRes = await predictionService.getNextMatch()
        const p = nextMatchRes.data as any
        setPrediction(p)
        // 从预测结果构建 Match 对象
        const m: Match = {
          id: p.matchId || 'next-match',
          homeTeam: { id: '', name: p.home_team || '主队', shortName: (p.home_team || '主队').slice(0, 3) },
          awayTeam: { id: '', name: p.away_team || '客队', shortName: (p.away_team || '客队').slice(0, 3) },
          league: { code: '', name: p.league || '未知联赛', country: '' },
          kickoff: p.match_date || '',
          status: 'upcoming',
          venue: p.venue,
        }
        setMatch(m)
        // 获取球队特征
        try {
          const [homeRes, awayRes] = await Promise.all([
            featureService.getTeamFeatures(p.home_team),
            featureService.getTeamFeatures(p.away_team),
          ])
          setHomeFeatures(homeRes.data.data)
          setAwayFeatures(awayRes.data.data)
        } catch {
          // 特征数据可选
        }
      } catch (err) {
        console.error('获取比赛数据失败:', err)
      } finally {
        setLoading(false)
      }
    }
    fetchData()
  }, [])
  if (loading) {
    return (
      <div className="space-y-6">
        <div className="skeleton h-8 w-48" />
        <div className="skeleton h-48 w-full" />
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2"><div className="skeleton h-64" /></div>
          <div><div className="skeleton h-64" /></div>
        </div>
      </div>
    )
  }
  if (!match || !prediction) {
    return (
      <div className="text-center py-20">
        <p className="text-white/40">暂无比赛数据</p>
      </div>
    )
  }
  return (
    <div className="space-y-6">
      {/* 页面标题 */}
      <motion.div
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
      >
        <h1 className="text-2xl font-black font-display text-white tracking-tight">比赛分析</h1>
        <p className="text-sm text-white/40 mt-1">深度分析引擎 · 多维度决策支持</p>
      </motion.div>
      {/* 对阵双方信息 */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1 }}
        className="glass-card p-8"
      >
        <div className="flex items-center justify-between">
          <TeamInfoPanel team={match.homeTeam} side="home" features={homeFeatures} />
          
          {/* 中间信息 */}
          <div className="px-8 text-center">
            <div className="text-[10px] font-medium uppercase tracking-widest text-white/20 mb-2">{match.league.name}</div>
            <div className="text-4xl font-black font-display text-white/40 mb-2">VS</div>
            <div className="text-xs text-white/30">
              {new Date(match.kickoff).toLocaleDateString('zh-CN', {
                year: 'numeric', month: 'long', day: 'numeric',
                hour: '2-digit', minute: '2-digit'
              })}
            </div>
            {match.venue && <div className="text-[10px] text-white/20 mt-1">{match.venue}</div>}
          </div>
          <TeamInfoPanel team={match.awayTeam} side="away" features={awayFeatures} />
        </div>
      </motion.div>
      {/* 预测结果 + 概率 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <ProbabilityGauge probabilities={prediction.probabilities} result={prediction.result} />
        </div>
        <ScorePredictionPanel score={prediction.score} confidence={prediction.confidence} />
      </div>
      {/* 多维度分析标签页 */}
      <AnalysisTabs homeFeatures={homeFeatures} awayFeatures={awayFeatures} />
    </div>
  )
}
import { useState, useEffect, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { useAppStore } from '@/store'
import { predictionService, featureService, fixtureService } from '@/services/api'
import type { Match, Prediction, TeamFeatures } from '@/types'
import StarRating from '@/components/shared/StarRating'
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
        <div className={`w-20 h-20 rounded-2xl bg-gradient-to-br ${isHome ? 'from-field-500/15 to-field-700/10' : 'from-frost-500/15 to-frost-700/10'} border border-surface-border flex items-center justify-center mb-3`}>
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
// 子组件：比分预测（多比分推荐，与赛程表一致）
// ============================================
interface TopScoreItem {
  score: string
  prob: number        // 标准化后统一用 prob（后端可能是 probability 或 prob）
  outcome: string
}

/** 将后端 top_scores 条目标准化为 TopScoreItem（兼容 probability/prob 两种字段名） */
function normalizeTopScores(raw: any[]): TopScoreItem[] {
  if (!Array.isArray(raw)) return []
  return raw.map((s: any) => ({
    score: s.score || '',
    prob: (s.prob ?? s.probability ?? 0.05) as number,   // 兼容 probability/prob
    outcome: s.outcome || 'draw',
  }))
}

function ScorePredictionPanel({
  primaryScore,
  topScores,
  confidence,
}: {
  primaryScore: { home: number; away: number }
  topScores: TopScoreItem[]
  confidence: number
}) {
  const hasScores = topScores.length > 0

  return (
    <div className="glass-card p-6 text-center">
      <h3 className="text-xs font-bold uppercase tracking-widest text-white/30 mb-4">比分预测</h3>

      {/* 主推比分（首个推荐） */}
      <div className="flex items-center justify-center gap-6 mb-4">
        <div className="text-center">
          <p className="text-[10px] text-white/30 mb-1">主队</p>
          <motion.span
            initial={{ scale: 0 }}
            animate={{ scale: 1 }}
            transition={{ type: 'spring', stiffness: 200, damping: 15 }}
            className="text-6xl font-black font-display text-pitch-400"
          >
            {primaryScore.home}
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
            {primaryScore.away}
          </motion.span>
        </div>
      </div>

      {/* 推荐比分列表（与赛程表一致：Score + Prob） */}
      {hasScores && (
        <div className="border-t border-white/[0.06] pt-4">
          <p className="text-[10px] text-white/30 mb-2 uppercase tracking-wider">
            前 {Math.min(topScores.length, 4)} 比分推荐
          </p>
          <div className="grid grid-cols-2 gap-2">
            {topScores.slice(0, 4).map((s, idx) => {
              const isPrimary = idx === 0
              return (
                <motion.div
                  key={s.score + idx}
                  initial={{ opacity: 0, y: 5 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: 0.3 + idx * 0.06 }}
                  className={`rounded-lg px-3 py-2 text-center ${
                    isPrimary
                      ? 'bg-pitch-500/10 border border-pitch-500/20'
                      : 'bg-white/[0.02] border border-white/[0.04]'
                  }`}
                >
                  <span className={`text-sm font-bold font-display ${isPrimary ? 'text-white/90' : 'text-white/60'}`}>
                    {s.score}
                  </span>
                  <span className="text-[10px] text-white/30 ml-1.5">
                    {Math.round(s.prob * 100)}%
                  </span>
                  {isPrimary && (
                    <span className="block text-[9px] text-pitch-400/60 mt-0.5">首选</span>
                  )}
                </motion.div>
              )
            })}
          </div>
        </div>
      )}

      <div className="mt-4 flex items-center justify-center gap-2">
        <span className="text-[10px] text-white/30">信心指数</span>
        <StarRating confidence={confidence} size="md" />
      </div>

      {/* 无多比分时的降级展示 */}
      {!hasScores && (
        <p className="text-[10px] text-white/15 mt-1">仅主推比分</p>
      )}
    </div>
  )
}
// ============================================
// 子组件：分析标签页
// ============================================
const tabs = [
  { id: 'features', label: '特征分析' },
  // 历史交锋 / 球队状态 / 模型决策 后端暂未就绪，待后续接入后启用
]
function AnalysisTabs({ homeFeatures, awayFeatures }: { homeFeatures?: TeamFeatures; awayFeatures?: TeamFeatures }) {
  const [activeTab, setActiveTab] = useState('features')
  return (
    <div className="glass-card p-6">
      {/* 标签页导航 — 仅展示已就绪的标签 */}
      {tabs.length > 1 && (
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
      )}
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
        </motion.div>
      </AnimatePresence>
    </div>
  )
}
// ============================================
// 主页面：比赛分析 (支持 URL 参数: ?home=&away=&league=&kickoff=&matchId=)
// ============================================
export default function MatchAnalysis() {
  const [searchParams] = useSearchParams()
  const [match, setMatch] = useState<Match | null>(null)
  const [prediction, setPrediction] = useState<Prediction | null>(null)
  const [topScores, setTopScores] = useState<TopScoreItem[]>([])
  const [homeFeatures, setHomeFeatures] = useState<TeamFeatures | undefined>()
  const [awayFeatures, setAwayFeatures] = useState<TeamFeatures | undefined>()
  const [loading, setLoading] = useState(true)
  const { competition } = useAppStore()  // v7.1: 响应式赛事切换

  // 从 URL 参数提取比赛信息
  const queryMatch = useMemo(() => {
    const home = searchParams.get('home')
    const away = searchParams.get('away')
    return home && away
      ? {
          matchId: searchParams.get('matchId') || '',
          home,
          away,
          league: searchParams.get('league') || '未知联赛',
          kickoff: searchParams.get('kickoff') || new Date().toISOString(),
        }
      : null
  }, [searchParams])

  useEffect(() => {
    if (!queryMatch) {
      setLoading(false)
      return
    }
    const fetchData = async () => {
      try {
        setLoading(true)

        // 构建 Match 对象
        const m: Match = {
          id: queryMatch.matchId,
          homeTeam: { id: '', name: queryMatch.home, shortName: queryMatch.home.slice(0, 3) },
          awayTeam: { id: '', name: queryMatch.away, shortName: queryMatch.away.slice(0, 3) },
          league: { code: '', name: queryMatch.league, country: '' },
          kickoff: queryMatch.kickoff,
          status: 'upcoming',
        }
        setMatch(m)

        // 调用 predictSingle 获取预测
        try {
          const res = await predictionService.predictSingle({
            home_team: queryMatch.home,
            away_team: queryMatch.away,
            league: 'WC',
            competition,
          } as any)
          const raw = res.data as any
          if (raw && raw.probabilities) {
            // ── 优先从赛程管线取 top_scores（与预测大厅表格同源）──
            let scores: TopScoreItem[] = []
            const matchIdNum = parseInt(queryMatch.matchId, 10)
            if (!isNaN(matchIdNum) && matchIdNum > 0) {
              try {
                const fixRes = await fixtureService.getUpcoming()
                const fixtures = (fixRes.data as any)?.matches || []
                const matched = fixtures.find((f: any) => f.id === matchIdNum)
                if (matched?.prediction?.top_scores) {
                  scores = normalizeTopScores(matched.prediction.top_scores)
                }
              } catch { /* 降级 */ }
            }
            // 管线无数据时降级到 predictSingle 的 score_prediction
            if (!scores.length) {
              scores = normalizeTopScores(raw.score_prediction?.top_scores || raw.score?.top_scores || [])
            }
            setTopScores(scores)

            // 主推比分取第一个
            const topScore = scores[0]
            const primaryHome = topScore ? parseInt(topScore.score.split('-')[0]) : 0
            const primaryAway = topScore ? parseInt(topScore.score.split('-')[1]) : 0

            const predCode = raw.prediction || raw.result || 'D'
            const resultMap: Record<string, 'home' | 'draw' | 'away'> = { H: 'home', D: 'draw', A: 'away' }
            const p: Prediction = {
              matchId: queryMatch.matchId,
              result: resultMap[predCode] || 'draw',
              probabilities: raw.probabilities,
              score: { home: primaryHome, away: primaryAway },
              confidence: raw.confidence || 0,
              modelVersion: raw.prediction_mode || 'unknown',
              timestamp: raw.timestamp || new Date().toISOString(),
              // ── P0修复新增字段 ──
              analysis: raw.analysis,
              consistency: raw.consistency,
              hcp2_law_applied: raw.hcp2_law_applied,
              short_circuit: raw.short_circuit,
              p0_triggers: raw.p0_triggers,
              best_score: raw.best_score,
              alt_scores: raw.alt_scores,
              dgate_result: raw.dgate_result,
              ou_linkage: raw.ou_linkage,
              taoge_strategy: raw.taoge_strategy,
              ou_recommend: raw.ou_recommend,
              hcp_recommend: raw.hcp_recommend,
            }
            setPrediction(p)
          }
        } catch {
          console.error('预测获取失败')
        }

        // 获取球队特征
        try {
          const [homeRes, awayRes] = await Promise.all([
            featureService.getTeamFeatures(queryMatch.home),
            featureService.getTeamFeatures(queryMatch.away),
          ])
          const hf = (homeRes.data as any)?.features || (homeRes.data as any)?.data || homeRes.data
          const af = (awayRes.data as any)?.features || (awayRes.data as any)?.data || awayRes.data
          // 校验: 必须有 attack/defense 字段, 防止 HTML 字符串或无效对象流入
          if (hf && typeof hf.attack === 'number' && typeof hf.defense === 'number') setHomeFeatures(hf)
          if (af && typeof af.attack === 'number' && typeof af.defense === 'number') setAwayFeatures(af)
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
  }, [queryMatch, competition])  // v7.1: competition 切换时重新拉取

  // 无 query 参数 → 引导用户从大厅进入
  if (!queryMatch) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-center">
        <div className="w-16 h-16 rounded-full bg-white/[0.04] border border-white/[0.08] flex items-center justify-center mb-4">
          <svg className="w-8 h-8 text-white/20" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5">
            <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0" />
          </svg>
        </div>
        <h2 className="text-lg font-bold text-white/50 mb-2">请选择比赛</h2>
        <p className="text-sm text-white/30 max-w-xs">
          从 <a href="/" className="text-pitch-400 hover:text-pitch-300 underline underline-offset-2">预测大厅</a> 点击任意比赛卡片即可查看深度分析
        </p>
      </div>
    )
  }
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
              {(() => {
                const d = new Date(match.kickoff)
                return isNaN(d.getTime())
                  ? '比赛日期待定'
                  : d.toLocaleDateString('zh-CN', {
                      year: 'numeric', month: 'long', day: 'numeric',
                      hour: '2-digit', minute: '2-digit'
                    })
              })()}
            </div>
            {match.venue && <div className="text-[10px] text-white/20 mt-1">{match.venue}</div>}
          </div>
          <TeamInfoPanel team={match.awayTeam} side="away" features={awayFeatures} />
        </div>
      </motion.div>
      {/* 预测结果 + 概率 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
      {prediction.probabilities ? (
          <ProbabilityGauge probabilities={prediction.probabilities} result={prediction.result} />
        ) : (
          <div className="glass-card p-6 text-center">
            <h3 className="text-xs font-bold uppercase tracking-widest text-white/30 mb-4">概率分布</h3>
            <span className="text-xs text-white/15">暂无预测数据</span>
          </div>
        )}
        </div>
        <ScorePredictionPanel
          primaryScore={prediction.score}
          topScores={topScores}
          confidence={prediction.confidence}
        />
      </div>
      {/* P0 风控信息面板 (v7.1 重新设计) */}
      {prediction.consistency || prediction.p0_triggers?.length || prediction.hcp2_law_applied ? (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.25 }}
          className="card p-5"
        >
          <div className="flex items-center gap-2 mb-4">
            <span className="status-dot-healthy" />
            <h3 className="text-xs font-bold uppercase tracking-widest text-ink-muted">
              风控校验 · P0 管道
            </h3>
            {prediction.consistency && (
              <span className={`badge ml-auto ${prediction.consistency.passed ? 'badge-green' : 'badge-amber'}`}>
                {prediction.consistency.passed ? '通过' : '冲突'}
              </span>
            )}
          </div>
          <div className="grid grid-cols-2 lg:grid-cols-3 gap-2">
            {/* 一致性校验 */}
            {prediction.consistency && (
              <div className="bg-surface-panel rounded-lg px-3 py-2.5">
                <span className={`text-[11px] font-bold ${prediction.consistency.passed ? 'text-field-400' : 'text-ember-400'}`}>
                  {prediction.consistency.passed ? '✅' : '⚠️'} 一致性校验
                </span>
                <div className="text-micro text-ink-muted mt-0.5">
                  {prediction.consistency.checks?.length || 0} 条规则
                  {prediction.consistency.checks?.filter(c => !c.passed).length ? ` · ${prediction.consistency.checks.filter(c => !c.passed).length} 条冲突` : ' · 全部通过'}
                </div>
              </div>
            )}
            {/* P0 触发标记 */}
            {prediction.p0_triggers && prediction.p0_triggers.length > 0 && (
              <div className="bg-frost-500/[0.06] rounded-lg px-3 py-2.5">
                <span className="text-[11px] font-bold text-frost-400">🔧 P0 触发</span>
                <div className="text-micro text-ink-muted mt-0.5">{prediction.p0_triggers.join(' · ')}</div>
              </div>
            )}
            {/* 让2球不穿律 */}
            {prediction.hcp2_law_applied && (
              <div className="bg-violet-500/[0.06] rounded-lg px-3 py-2.5">
                <span className="text-[11px] font-bold text-violet-400">🛡️ 让2球不穿律</span>
                <div className="text-micro text-ink-muted mt-0.5">屠杀比分已过滤</div>
              </div>
            )}
            {/* 短路机制 */}
            {prediction.short_circuit && (
              <div className="bg-danger-500/[0.06] rounded-lg px-3 py-2.5">
                <span className="text-[11px] font-bold text-danger-400">⚡ 短路机制触发</span>
              </div>
            )}
            {/* D-Gate 风控 */}
            {prediction.dgate_result && (
              <div className="bg-surface-panel rounded-lg px-3 py-2.5">
                <span className="text-[11px] font-bold text-ink-secondary">D-Gate</span>
                <div className="text-micro text-ink-muted mt-0.5">
                  {prediction.dgate_result.risk_tag || 'clean'}
                  {prediction.dgate_result.draw_alert ? ' · draw_alert' : ''}
                </div>
              </div>
            )}
            {/* TaoGe 策略 */}
            {prediction.taoge_strategy && (
              <div className="bg-surface-panel rounded-lg px-3 py-2.5">
                <span className="text-[11px] font-bold text-ink-secondary">TaoGe</span>
                <div className="text-micro text-ink-muted mt-0.5">
                  {prediction.taoge_strategy.primary || ''} + {prediction.taoge_strategy.secondary || ''}
                </div>
              </div>
            )}
          </div>
        </motion.div>
      ) : null}
      {/* WC 校准市场建议 (v7.1 rules-layer: OU/让球 WC实测校准) */}
      {prediction.ou_recommend || prediction.hcp_recommend ? (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.3 }}
          className="card p-5"
        >
          <div className="flex items-center gap-2 mb-4">
            <span className="status-dot-healthy" />
            <h3 className="text-xs font-bold uppercase tracking-widest text-ink-muted">
              市场建议 · WC 校准
            </h3>
            <span className="badge badge-green ml-auto">实测校准</span>
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            {/* 大小球 OU */}
            {prediction.ou_recommend && (
              <div className="bg-surface-panel rounded-lg px-4 py-3">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-bold text-ink-secondary">⚽ 大小球 (OU)</span>
                  <span className={`badge ${prediction.ou_recommend.recommend === '大' ? 'badge-green' : 'badge-amber'}`}>
                    {prediction.ou_recommend.recommend}
                  </span>
                </div>
                <div className="flex items-end gap-4 mt-2">
                  <div>
                    <div className="text-micro text-ink-muted">盘口</div>
                    <div className="text-lg font-black font-display text-white">{prediction.ou_recommend.line}</div>
                  </div>
                  <div>
                    <div className="text-micro text-ink-muted">预期总球</div>
                    <div className="text-lg font-black font-display text-field-400">{prediction.ou_recommend.expected_total}</div>
                  </div>
                  <div className="ml-auto text-right">
                    <div className="text-micro text-ink-muted">置信度</div>
                    <div className="text-sm font-bold text-white/70">{Math.round((prediction.ou_recommend.confidence ?? 0) * 100)}%</div>
                  </div>
                </div>
                {prediction.ou_recommend.note && (
                  <div className="text-micro text-ink-muted mt-2 border-t border-white/[0.06] pt-2">{prediction.ou_recommend.note}</div>
                )}
              </div>
            )}
            {/* 让球 HCP */}
            {prediction.hcp_recommend && (
              <div className="bg-surface-panel rounded-lg px-4 py-3">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-bold text-ink-secondary">🛡️ 让球 (HCP)</span>
                  <span className="badge badge-green">{prediction.hcp_recommend.recommend}</span>
                </div>
                <div className="flex items-end gap-4 mt-2">
                  <div>
                    <div className="text-micro text-ink-muted">盘口</div>
                    <div className="text-lg font-black font-display text-white">{prediction.hcp_recommend.hcp}</div>
                  </div>
                  <div className="ml-auto text-right">
                    <div className="text-micro text-ink-muted">置信度</div>
                    <div className="text-sm font-bold text-white/70">{Math.round((prediction.hcp_recommend.confidence ?? 0) * 100)}%</div>
                  </div>
                </div>
                {prediction.hcp_recommend.note && (
                  <div className="text-micro text-ink-muted mt-2 border-t border-white/[0.06] pt-2">{prediction.hcp_recommend.note}</div>
                )}
              </div>
            )}
          </div>
        </motion.div>
      ) : null}
      {/* 多维度分析标签页 */}
      <AnalysisTabs homeFeatures={homeFeatures} awayFeatures={awayFeatures} />
    </div>
  )
}
import { useState, useEffect, useMemo, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { useAppStore } from '@/store'
import { predictionService, matchService, fixtureService } from '@/services/api'
import type { Match, Prediction, PredictionStats, Fixture, FixturePrediction } from '@/types'
import StarRating from '@/components/shared/StarRating'
// 安全日期格式化：处理可能无效的日期字符串
function safeFormatDate(dateStr: string): string {
  try {
    const d = new Date(dateStr)
    if (isNaN(d.getTime())) return '--'
    return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  } catch { return '--' }
}
// ============================================
// 子组件：实时赛程横幅 (今日/明日世界杯赛程, 手动刷新)
// ============================================
function FixturePill({ fixture, index }: { fixture: Fixture; index: number }) {
  const isLive = fixture.status === 'IN_PLAY' || fixture.status === 'PAUSED'
  const isFinished = fixture.status === 'FINISHED'
  const hasScore = fixture.score_home != null && fixture.score_away != null
  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ delay: index * 0.04, duration: 0.3 }}
      className="card p-3 min-w-[220px] flex-shrink-0 hover:border-field-500/20 transition-colors"
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-[10px] font-bold text-pitch-400 font-display">
          {fixture.time_local}
        </span>
        <span className={`text-[9px] font-medium px-1.5 py-0.5 rounded ${
          isLive ? 'bg-danger-500/20 text-danger-400 animate-pulse' :
          isFinished ? 'bg-white/5 text-white/30' :
          'bg-pitch-500/15 text-pitch-400/80'
        }`}>
          {isLive ? '● 进行中' : isFinished ? '已结束' : fixture.group ? `${fixture.group}组` : '即将开始'}
        </span>
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-bold font-display text-white/90 truncate flex-1 text-right">{fixture.home}</span>
        {hasScore ? (
          <span className="text-sm font-black font-display text-white/70 px-1">
            {fixture.score_home}-{fixture.score_away}
          </span>
        ) : (
          <span className="text-[10px] font-bold text-white/20 px-1">VS</span>
        )}
        <span className="text-xs font-bold font-display text-white/90 truncate flex-1">{fixture.away}</span>
      </div>
    </motion.div>
  )
}
function FixturesBanner() {
  const [today, setToday] = useState<Fixture[]>([])
  const [tomorrow, setTomorrow] = useState<Fixture[]>([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const [error, setError] = useState<string | null>(null)

  const fetchFixtures = async () => {
    setRefreshing(true)
    try {
      const res = await fixtureService.getUpcoming()
      const data = res.data
      setToday(data.today || [])
      setTomorrow(data.tomorrow || [])
      setUpdatedAt(new Date())
      setError(data.error || null)
    } catch (err) {
      console.error('获取赛程失败:', err)
      setError('赛程获取失败')
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }
  useEffect(() => { fetchFixtures() }, [])

  const total = today.length + tomorrow.length
  const timeStr = updatedAt ? updatedAt.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '--'

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.05 }}
      className="card p-4"
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-pitch-400 animate-pulse" />
          <h3 className="text-xs font-bold uppercase tracking-widest text-white/50">实时赛程 · 2026世界杯</h3>
          <span className="text-[10px] text-white/30">共 {total} 场</span>
          {error && <span className="text-[10px] text-ember-400/70">{error}</span>}
        </div>
        <button
          onClick={fetchFixtures}
          disabled={refreshing}
          className="flex items-center gap-1 px-2.5 py-1 rounded-lg text-[10px] font-medium text-white/40 hover:text-pitch-400 hover:bg-pitch-500/10 transition-all disabled:opacity-40"
        >
          <svg className={`w-3 h-3 ${refreshing ? 'animate-spin' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
            <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992V4.356M19.671 16.5A8.25 8.25 0 005.672 8.023m-1.652 7.629v4.992h4.992M4.329 7.5A8.25 8.25 0 0018.328 15.977" />
          </svg>
          <span>{refreshing ? '刷新中' : `刷新 (${timeStr})`}</span>
        </button>
      </div>
      {loading ? (
        <div className="flex gap-3 overflow-hidden">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="skeleton h-20 w-[220px] rounded-2xl flex-shrink-0" />
          ))}
        </div>
      ) : total === 0 ? (
        <p className="text-xs text-white/30 py-4 text-center">当前窗口暂无赛程（赛事可能已结束或尚未开始）</p>
      ) : (
        <div className="space-y-3">
          {today.length > 0 && (
            <div>
              <p className="text-[10px] font-bold text-white/40 mb-2 uppercase tracking-wider">今日 {today.length} 场</p>
              <div className="flex gap-3 overflow-x-auto pb-1 scrollbar-thin">
                {today.map((f, i) => <FixturePill key={f.id} fixture={f} index={i} />)}
              </div>
            </div>
          )}
          {tomorrow.length > 0 && (
            <div>
              <p className="text-[10px] font-bold text-white/40 mb-2 uppercase tracking-wider">明日 {tomorrow.length} 场</p>
              <div className="flex gap-3 overflow-x-auto pb-1 scrollbar-thin">
                {tomorrow.map((f, i) => <FixturePill key={f.id} fixture={f} index={i} />)}
              </div>
            </div>
          )}
        </div>
      )}
    </motion.div>
  )
}
// 结果映射
const resultLabel: Record<string, string> = { H: '主胜', D: '平局', A: '客胜', home: '主胜', draw: '平局', away: '客胜' }
const resultColor: Record<string, string> = {
  H: 'bg-pitch-500/20 text-pitch-400',
  D: 'bg-ember-500/20 text-ember-400',
  A: 'bg-frost-500/20 text-frost-400',
  home: 'bg-pitch-500/20 text-pitch-400',
  draw: 'bg-ember-500/20 text-ember-400',
  away: 'bg-frost-500/20 text-frost-400',
}
const directionLabel: Record<string, string> = {
  SAME: '✅ 同向',
  DRAW_DIVERGE: '⚠️ 平局分歧',
  OPPOSITE: '⚠️ 方向相反',
}

// 取 top-2 推荐比分字符串
function fmtTopScores(pred: FixturePrediction | undefined): string {
  if (!pred?.top_scores?.length) return '--'
  return pred.top_scores.slice(0, 2).map((s) => `${s.score}`).join(' / ')
}

// 比分方向是否与 1X2 一致（仅用于视觉提示）
function getDirectionClass(dir: string | undefined): string {
  if (dir === 'OPPOSITE') return 'text-ember-400'
  if (dir === 'DRAW_DIVERGE') return 'text-amber-400'
  return 'text-pitch-400'
}

// ============================================
// 子组件：完整赛程预测表 (全管线预测嵌入)
// ============================================

/** 阶段 → 中文映射 */
const stageLabel: Record<string, string> = {
  GROUP_STAGE: '小组赛',
  LAST_16: '1/8决赛',
  QUARTER_FINALS: '1/4决赛',
  SEMI_FINALS: '半决赛',
  FINAL: '决赛',
  THIRD_PLACE: '三四名决赛',
}

function FixturesTable({ fixtures, loading }: { fixtures: Fixture[]; loading: boolean }) {
  const navigate = useNavigate()
  // ---- loading 骨架 ----
  if (loading) {
    return (
      <div className="glass-card p-4 space-y-3">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="h-12 bg-white/[0.02] rounded-lg animate-pulse" />
        ))}
      </div>
    )
  }

  // ---- empty 占位 ----
  if (!fixtures.length) {
    return (
      <div className="glass-card p-6 text-center">
        <p className="text-sm text-white/30">未来窗口暂无赛程</p>
        <p className="text-[10px] text-white/20 mt-1">可点击上方刷新按钮重试</p>
      </div>
    )
  }

  // ---- error 降级（后端返回 error 字段时仍渲染表格，顶部提示） ----
  // fixtures 数据本身始终可用；error 信息由 FixturesBanner 展示。

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.15 }}
      className="card overflow-hidden"
    >
      {/* 表头 */}
      <div className="px-4 py-3 border-b border-surface-border flex items-center justify-between">
        <h3 className="text-xs font-bold uppercase tracking-widest text-white/50">完整赛程 · 预测管线</h3>
        <span className="text-[10px] text-white/30">共 {fixtures.length} 场</span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-surface-border">
              <th className="text-left py-3 px-3 text-white/30 font-medium uppercase tracking-wider">日期</th>
              <th className="text-left py-3 px-3 text-white/30 font-medium uppercase tracking-wider">阶段</th>
              <th className="text-right py-3 px-3 text-white/30 font-medium uppercase tracking-wider">主队</th>
              <th className="text-center py-3 px-2 text-white/30 font-medium uppercase tracking-wider">比分</th>
              <th className="text-left py-3 px-3 text-white/30 font-medium uppercase tracking-wider">客队</th>
              <th className="text-center py-3 px-3 text-white/30 font-medium uppercase tracking-wider">概率</th>
              <th className="text-center py-3 px-3 text-white/30 font-medium uppercase tracking-wider">推荐比分</th>
              <th className="text-center py-3 px-2 text-white/30 font-medium uppercase tracking-wider">风险</th>
            </tr>
          </thead>

          <tbody>
            {fixtures.map((f, i) => {
              const pred = f.prediction
              const isFinished = f.status === 'FINISHED' || f.is_finished === true
              const isLive = f.status === 'IN_PLAY' || f.status === 'PAUSED'
              const hasScore = f.score_home != null && f.score_away != null

              // 完赛结果判定
              const result: 'H' | 'D' | 'A' | undefined = hasScore
                ? (f.score_home! > f.score_away! ? 'H' : f.score_home === f.score_away ? 'D' : 'A')
                : undefined

              // 降级预测标记
              const isDegraded = pred?.warning === true || pred?.mode === 'simplified'

              return (
                <tr
                  key={f.id}
                  onClick={() => {
                    const params = new URLSearchParams({
                      matchId: f.id?.toString() || '',
                      home: f.home || '',
                      away: f.away || '',
                      league: f.group ? `${f.group}组` : (stageLabel[f.stage] || f.stage || '世界杯'),
                      kickoff: f.time,
                    })
                    navigate(`/match-analysis?${params.toString()}`)
                  }}
                  className={`border-b border-surface-border hover:bg-white/[0.04] cursor-pointer transition-colors ${
                    isDegraded ? 'bg-danger-500/[0.03]' : ''
                  }`}
                >
                  {/* 日期: date_local + day_of_week + time_local */}
                  <td className="py-2.5 px-3 whitespace-nowrap">
                    <div className="text-white/70">{f.date_local || '--'}</div>
                    <div className="text-[10px] text-white/30">{f.day_of_week} {f.time_local}</div>
                  </td>

                  {/* 阶段: stage / group */}
                  <td className="py-2.5 px-3">
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-white/[0.04] text-white/40 whitespace-nowrap">
                      {f.group
                        ? `${f.group}组`
                        : stageLabel[f.stage] || f.stage || '世界杯'}
                    </span>
                  </td>

                  {/* 主队: 已完赛主胜高亮 */}
                  <td className="py-2.5 px-3 text-right">
                    <span
                      className={`font-bold font-display whitespace-nowrap ${
                        isFinished && result === 'H' ? 'text-pitch-400' : 'text-white/80'
                      }`}
                    >
                      {f.home}
                    </span>
                  </td>

                  {/* 比分: 完赛→分数, 进行中→红色脉冲, 未开始→VS */}
                  <td className="py-2.5 px-2 text-center">
                    {isFinished && hasScore ? (
                      <span
                        className={`font-bold font-display text-sm ${
                          result === 'H'
                            ? 'text-pitch-400'
                            : result === 'D'
                            ? 'text-ember-400'
                            : 'text-frost-400'
                        }`}
                      >
                        {f.score_home} - {f.score_away}
                      </span>
                    ) : isLive ? (
                      <span className="inline-flex items-center gap-1.5 font-bold font-display text-sm text-danger-400 animate-pulse">
                        <span className="w-1.5 h-1.5 rounded-full bg-danger-400" />
                        {f.score_home ?? 0} - {f.score_away ?? 0}
                      </span>
                    ) : (
                      <span className="font-bold font-display text-sm text-white/20">VS</span>
                    )}
                  </td>

                  {/* 客队: 已完赛客胜高亮 */}
                  <td className="py-2.5 px-3">
                    <span
                      className={`font-bold font-display whitespace-nowrap ${
                        isFinished && result === 'A' ? 'text-frost-400' : 'text-white/80'
                      }`}
                    >
                      {f.away}
                    </span>
                  </td>

                  {/* 概率: H/D/A 三道概率条 */}
                  <td className="py-2.5 px-3 text-center">
                    {pred && !isFinished ? (
                      <div className="space-y-1 w-[140px] mx-auto">
                        {(() => { const h = (pred.probabilities?.H ?? 0.33) * 100; const d = (pred.probabilities?.D ?? 0.33) * 100; const a = (pred.probabilities?.A ?? 0.33) * 100; return (<>
                        <div className="flex items-center gap-1">
                          <span className="text-[9px] text-white/30 w-3">H</span>
                          <div className="flex-1 h-1.5 bg-white/5 rounded-full overflow-hidden">
                            <motion.div initial={{ width: 0 }} animate={{ width: `${Math.round(h)}%` }}
                              transition={{ delay: i * 0.015 + 0.2, duration: 0.5 }} className="h-full bg-pitch-500 rounded-full" />
                          </div>
                          <span className="text-[9px] text-white/40 w-8 text-right">{Math.round(h)}%</span>
                        </div>
                        <div className="flex items-center gap-1">
                          <span className="text-[9px] text-white/30 w-3">D</span>
                          <div className="flex-1 h-1.5 bg-white/5 rounded-full overflow-hidden">
                            <motion.div initial={{ width: 0 }} animate={{ width: `${Math.round(d)}%` }}
                              transition={{ delay: i * 0.015 + 0.25, duration: 0.5 }} className="h-full bg-ember-500 rounded-full" />
                          </div>
                          <span className="text-[9px] text-white/40 w-8 text-right">{Math.round(d)}%</span>
                        </div>
                        <div className="flex items-center gap-1">
                          <span className="text-[9px] text-white/30 w-3">A</span>
                          <div className="flex-1 h-1.5 bg-white/5 rounded-full overflow-hidden">
                            <motion.div initial={{ width: 0 }} animate={{ width: `${Math.round(a)}%` }}
                              transition={{ delay: i * 0.015 + 0.3, duration: 0.5 }} className="h-full bg-frost-500 rounded-full" />
                          </div>
                          <span className="text-[9px] text-white/40 w-8 text-right">{Math.round(a)}%</span>
                        </div>
                        </>)})()}
                      </div>
                    ) : (
                      <span className="text-white/15">--</span>
                    )}
                  </td>

                  {/* 推荐比分: top_scores[0], [1] → score + prob */}
                  <td className="py-2.5 px-3 text-center">
                    {pred && !isFinished && pred.top_scores?.length ? (
                      <div className="space-y-1">
                        {pred.top_scores.slice(0, 2).map((s, idx) => (
                          <div key={idx} className="text-xs font-bold font-display">
                            <span className="text-white/70">{s.score}</span>
                            <span className="text-[9px] text-white/30 ml-1">
                              ({Math.round(s.prob * 100)}%)
                            </span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <span className="text-white/15">--</span>
                    )}
                  </td>

                  {/* 风险: risk_tag / direction / 降级 / 已结束 */}
                  <td className="py-2.5 px-2 text-center">
                    <div className="flex flex-col items-center gap-1">
                      {isFinished ? (
                        <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-white/5 text-white/30 whitespace-nowrap">
                          已结束
                        </span>
                      ) : isDegraded ? (
                        <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-danger-500/15 text-danger-400 whitespace-nowrap animate-pulse">
                          ⚠️ 降级预测
                        </span>
                      ) : pred ? (
                        <>
                          {/* direction 标签 */}
                          {pred.direction && (
                            <span
                              className={`text-[10px] px-1.5 py-0.5 rounded font-medium whitespace-nowrap ${
                                pred.direction === 'SAME'
                                  ? 'bg-pitch-500/15 text-pitch-400/80'
                                  : pred.direction === 'OPPOSITE'
                                  ? 'bg-danger-500/15 text-danger-400'
                                  : 'bg-ember-500/15 text-ember-400'
                              }`}
                            >
                              {directionLabel[pred.direction] || pred.direction}
                            </span>
                          )}
                          {/* risk_tag 标签（clean 不展示） */}
                          {pred.risk_tag && pred.risk_tag !== 'clean' && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-ember-500/10 text-amber-400/80 whitespace-nowrap">
                              {pred.risk_tag}
                            </span>
                          )}
                          {/* 无风险信息兜底 */}
                          {!pred.direction && (!pred.risk_tag || pred.risk_tag === 'clean') && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-pitch-500/15 text-pitch-400/80 whitespace-nowrap">
                              已预测
                            </span>
                          )}
                        </>
                      ) : (
                        <span className="text-white/15">--</span>
                      )}
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </motion.div>
  )
}

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
    <div className="relative h-52 rounded-2xl overflow-hidden group border border-surface-border">
      {/* 背景渐变 — v7.1 NVIDIA风格 */}
      <div className="absolute inset-0 bg-gradient-to-br from-surface-dark via-surface-panel to-field-950/60" />
      <div className="absolute inset-0 bg-gradient-to-r from-field-500/[0.04] via-transparent to-frost-500/[0.04]" />
      {/* 网格纹理 */}
      <div className="absolute inset-0 bg-[url('data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iNjAiIGhlaWdodD0iNjAiIHZpZXdCb3g9IjAgMCA2MCA2MCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48ZyBmaWxsPSJub25lIiBmaWxsLXJ1bGU9ImV2ZW5vZGQiPjxnIGZpbGw9IiNmZmYiIGZpbGwtb3BhY2l0eT0iMC4wMiI+PGNpcmNsZSBjeD0iMzAiIGN5PSIzMCIgcj0iMiIvPjwvZz48L2c+PC9zdmc+')] opacity-50" />
      {/* 内容 */}
      <div className="relative h-full flex items-center justify-between px-10">
        {/* 主队 */}
        <div className="text-center flex-1">
          <div className="w-20 h-20 rounded-2xl bg-gradient-to-br from-field-500/10 to-field-700/5 border border-field-500/10 flex items-center justify-center mb-3 mx-auto backdrop-blur-sm">
            <span className="text-3xl font-black font-display text-ink-primary tracking-tight">{match.homeTeam?.shortName ?? '?'}</span>
          </div>
          <p className="text-base font-bold font-display text-ink-primary">{match.homeTeam?.name ?? '?'}</p>
        </div>
        {/* 比分/VS */}
        <div className="text-center px-6">
          <div className="text-6xl font-black font-display text-ink-disabled tracking-widest select-none">VS</div>
          <div className="mt-3 flex items-center gap-2 justify-center">
            <span className="badge-green text-[10px]">焦点战</span>
            <span className="text-xs text-ink-muted">{safeFormatDate(match.kickoff)}</span>
          </div>
        </div>
        {/* 客队 */}
        <div className="text-center flex-1">
          <div className="w-20 h-20 rounded-2xl bg-gradient-to-br from-frost-500/10 to-frost-700/5 border border-frost-500/10 flex items-center justify-center mb-3 mx-auto backdrop-blur-sm">
            <span className="text-3xl font-black font-display text-ink-primary tracking-tight">{match.awayTeam?.shortName ?? '?'}</span>
          </div>
          <p className="text-base font-bold font-display text-ink-primary">{match.awayTeam?.name ?? '?'}</p>
        </div>
      </div>
      {/* 指示点 */}
      <div className="absolute bottom-3 left-1/2 -translate-x-1/2 flex gap-2">
        {featured.map((_, i) => (
          <button
            key={i}
            onClick={() => setCurrent(i)}
            className={`rounded-full transition-all duration-300 ${
              i === current ? 'w-5 h-1.5 bg-field-400' : 'w-1.5 h-1.5 bg-white/15 hover:bg-white/30'
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
  const navigate = useNavigate()
  const [isHovered, setIsHovered] = useState(false)
  const hasPrediction = prediction != null && prediction.probabilities != null
  const homeProb = prediction?.probabilities.home ?? 0
  const drawProb = prediction?.probabilities.draw ?? 0
  const awayProb = prediction?.probabilities.away ?? 0
  const confidence = prediction?.confidence ?? 0

  const handleClick = () => {
    const params = new URLSearchParams({
      matchId: match.id,
      home: match.homeTeam?.name ?? '',
      away: match.awayTeam?.name ?? '',
      league: match.league?.name ?? '',
      kickoff: match.kickoff,
    })
    navigate(`/match-analysis?${params.toString()}`)
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.05, duration: 0.4 }}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      onClick={handleClick}
      className="card-hover p-4 cursor-pointer relative overflow-hidden group"
    >
      {/* 悬停光效 */}
      <motion.div
        initial={false}
        animate={{ opacity: isHovered ? 1 : 0 }}
        className="absolute -inset-1 bg-gradient-to-r from-field-500/[0.06] via-transparent to-frost-500/[0.06] blur-xl pointer-events-none"
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
      {/* 概率条 — 无预测时不显示假数据 */}
      {hasPrediction ? (
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
      ) : (
        <div className="py-3 text-center">
          <span className="text-[10px] text-white/15">暂无预测数据</span>
        </div>
      )}
      {/* 信心指数 */}
      <div className="mt-3 flex items-center justify-between">
        <span className="text-[10px] text-white/30">信心指数</span>
        <StarRating confidence={confidence} size="sm" />
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
            <span className="stat-value text-pitch-400">{stats?.todayAccuracy != null ? `${Math.round(stats.todayAccuracy * 100)}%` : '--'}</span>
            <span className="text-xs text-white/30">总体 {stats?.overallAccuracy != null ? `${Math.round(stats.overallAccuracy * 100)}%` : '--'}</span>
          </div>
          <div className="probability-bar mt-2">
            <motion.div
              initial={{ width: 0 }}
              animate={{ width: stats?.todayAccuracy != null ? `${stats.todayAccuracy * 100}%` : '0%' }}
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
  const [fixtures, setFixtures] = useState<Fixture[]>([])
  const [fixturesLoading, setFixturesLoading] = useState(true)
  const { sidebarCollapsed, competition } = useAppStore()

  // 获取完整赛程（含管线预测）
  const fetchFixtures = useCallback(async () => {
    try {
      setFixturesLoading(true)
      const res = await fixtureService.getUpcoming()
      const raw = res.data as any
      const data = raw?.data || raw
      setFixtures(data?.matches || (Array.isArray(data) ? data : []) || [])
    } catch (err) {
      console.error('获取赛程失败:', err)
      setFixtures([])
    } finally {
      setFixturesLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchFixtures()
    const interval = setInterval(fetchFixtures, 60000)
    return () => clearInterval(interval)
  }, [fetchFixtures])

  useEffect(() => {
    const fetchData = async () => {
      try {
        setLoading(true)
        const [matchesRes, statsRes] = await Promise.all([
          matchService.getMatches(),
          predictionService.getPredictionStats(),
        ])
        const matchesRaw = (matchesRes.data as any)?.data || matchesRes.data as any
        const rawMatches = (matchesRaw?.matches || []) as Match[]
        const validMatches = rawMatches.filter((m) => {
          const home = m.homeTeam?.name?.trim() || ''
          const away = m.awayTeam?.name?.trim() || ''
          return home && away && home !== '主队' && away !== '客队'
        })
        setMatches(validMatches)
        const rawStats = statsRes.data as any
        const statsData = rawStats?.data || rawStats
        setStats(statsData && typeof statsData === 'object' ? statsData : null)
        // 为每场比赛获取预测（示例：取前8场）
        const predMap = new Map<string, Prediction>()
        const topMatches = validMatches.slice(0, 8)
        await Promise.all(
          topMatches.map(async (match: any) => {
            try {
              const res = await predictionService.predictSingle({
                home_team: match.homeTeam.name,
                away_team: match.awayTeam.name,
                league: match.league.code,
                odds_h: match.homeOdds,
                odds_d: match.drawOdds,
                odds_a: match.awayOdds,
                stage: match.status === 'upcoming' ? 'knockout' : 'group',
                competition,
              } as any)
              const raw = res.data as any
              if (raw && raw.probabilities) {
                // 后端直接返回数据，无ApiResponse包装；需要规范化预测格式
                const topScore = raw.score_prediction?.top_scores?.[0]
                const predCode = raw.prediction || raw.result || 'D'
                const resultMap: Record<string, 'home' | 'draw' | 'away'> = { H: 'home', D: 'draw', A: 'away' }
                const pred: Prediction = {
                  matchId: match.id,
                  result: resultMap[predCode] || 'draw',
                  probabilities: raw.probabilities,
                  score: {
                    home: topScore ? parseInt(topScore.score.split('-')[0]) : 0,
                    away: topScore ? parseInt(topScore.score.split('-')[1]) : 0,
                  },
                  confidence: raw.confidence || 0,
                  modelVersion: raw.prediction_mode || 'unknown',
                  timestamp: raw.timestamp || new Date().toISOString(),
                }
                predMap.set(match.id, pred)
              }
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
  }, [competition])  // v7.1: competition 切换时重新拉取预测
  // 筛选比赛（仅杯赛+五大联赛，不删训练数据）
  const filteredMatches = useMemo(() => {
    if (!matches || !Array.isArray(matches)) return []
    const allowedLeagues = new Set([
      '世界杯', 'FIFA World Cup', 'WC',           // 世界杯
      '英超', 'Premier League', 'PL',             // 英超
      '西甲', 'La Liga', 'LL',                    // 西甲
      '意甲', 'Serie A', 'SA',                    // 意甲
      '德甲', 'Bundesliga', 'BL',                 // 德甲
      '法甲', 'Ligue 1', 'L1',                    // 法甲
    ])
    const now = new Date()
    return matches.filter((m) => {
      const leagueName = m.league?.name || ''
      const leagueCode = m.league?.code || ''
      // 联赛名或代码任一命中即保留
      const isTargetLeague = allowedLeagues.has(leagueName)
        || allowedLeagues.has(leagueCode)
        || leagueName.startsWith('世界杯')
        || leagueName.startsWith('FIFA')
      if (!isTargetLeague) return false

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
      {/* 实时赛程横幅 */}
      <FixturesBanner />
      {/* 完整赛程预测表 */}
      <FixturesTable fixtures={fixtures} loading={fixturesLoading} />
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
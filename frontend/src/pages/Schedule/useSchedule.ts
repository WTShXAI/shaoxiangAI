// ═══ 赛程列表页数据逻辑 Hook (2026-08-31 自 index.tsx 拆分) ═══
// 与 MatchAnalysisModal/useMatchAnalysis 同款模式: 状态+取数+轮询+过滤全部收进 hook,
// index.tsx 只做渲染组装。轮询拆分(2026-08-31 优化):
//   · 5s 轻轮询 = 比赛列表 + 破蛋 probe (与采集频率匹配)
//   · 30s 重轮询 = _live_predict 全链路 + 4盘口综合 + 开盘天眼 (解耦, 不再每 5s 压 4 接口)
import { useState, useEffect, useCallback, useRef } from 'react'
import { liveGoalProbeService, terminalService, bestComboService, openEyeService } from '@/services/api'
import type { LiveMatch, ProbeData } from './types'
import { MAX_MIN, POLL_INTERVAL, HEAVY_POLL_INTERVAL } from './types'
import { adaptLivePredict, resolveDisplayMinute } from './utils'

export function useSchedule() {
  const [matches, setMatches] = useState<LiveMatch[]>([])
  const [selected, setSelected] = useState<LiveMatch | null>(null)
  const [probe, setProbe] = useState<ProbeData | null>(null)
  const [anal, setAnal] = useState<any>(null)
  const [momentum, setMomentum] = useState<any>(null)
  const [consensus, setConsensus] = useState<any>(null)
  const [trustCard, setTrustCard] = useState<any>(null)
  const [induce, setInduce] = useState<any>(null)
  const [bestCombo, setBestCombo] = useState<any>(null)
  const [openEye, setOpenEye] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [lastUpdate, setLastUpdate] = useState<number | null>(null)
  const [maxLastSeen, setMaxLastSeen] = useState<number | null>(null)
  const [backtest, setBacktest] = useState<any>(null)
  const [now, setNow] = useState<number>(() => Date.now())
  const [search, setSearch] = useState('')
  const [onlyGoalless, setOnlyGoalless] = useState(false)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // 1 秒实时时钟(与实时比分页同源, 用于 kickoff 基准的时间走字)
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const fetchBacktest = useCallback(async () => {
    try {
      const res = await liveGoalProbeService.getBacktest()
      const j = res.data
      if (j.ok) setBacktest(j.data)
    } catch {
      /* 非关键, 忽略 */
    }
  }, [])

  const fetchMatches = useCallback(async () => {
    try {
      const res = await liveGoalProbeService.getMatches(50)
      const j = res.data
      if (!j.ok) throw new Error(j.error || '列表失败')
      const payload = j.data || { matches: [], max_last_seen: null }
      setMatches(payload.matches || [])
      setMaxLastSeen(payload.max_last_seen || null)
      setLastUpdate(Date.now())
      setError('')
    } catch (e: any) {
      setError(e?.response?.data?.error || e?.message || '获取比赛列表失败')
    }
  }, [])

  // 2026-08-29 复原(方案A) 适配层:
  //   /api/terminal/analyze(_live_predict) 是全链路模型 —— 方向/OIP比分/OU/决策在
  //   同一个响应里, 字段与重建后接的 /api/live-goal-probe/analyze(score_hint) 不同。
  //   这里做**单一适配**把前者映射成 score_hint 形状, 渲染层不动。
  //   比分取自 oip.top3_scores (OIP 泊松比分模型), 方向取自 direction (市场 argmax)。
  //
  // 2026-08-31 优化: 轮询拆分 —— 破蛋 probe(轻) 保持 5s, 重分析(analyze/best-combo/open-eye)
  //   独立 30s 轮询, 避免每 5s 对后端压 4 个接口(analyze 最长 90s timeout)。
  //   比赛分钟对齐采集器(2026-08-30 修复): 后端 list_live_matches 已用 resolve_true_minute
  //   精算分钟 —— kickoff 真实时钟 + 扣除中场 15 分钟 + feed 45/90 只当半场标识,
  //   所以前端**直接用 m.minute**。
  const computeProbeMinute = useCallback((m: LiveMatch): number => {
    const liveMin = m.minute != null ? m.minute : 0
    return Math.round(Math.min(MAX_MIN, Math.max(0, liveMin)))
  }, [])

  /** 轻量: 滚球破蛋 probe (5s 轮询) */
  const fetchProbeLight = useCallback(async (m: LiveMatch) => {
    const min = computeProbeMinute(m)
    try {
      const res = await liveGoalProbeService.getProbe(m.match_key, m.score, min)
      const probeRes = res?.data
      if (probeRes) {
        if (!probeRes.ok) { setProbe(null); return }
        setProbe(probeRes.data)
      } else {
        setProbe(null)
      }
    } catch {
      setProbe(null)
    }
  }, [computeProbeMinute])

  /** 重分析: _live_predict 全链路 + 4盘口综合 + 开盘天眼 (30s 轮询, 切换比赛立即) */
  const fetchProbeHeavy = useCallback(async (m: LiveMatch) => {
    setLoading(true)
    try {
      const min = computeProbeMinute(m)
      // 2026-08-29 复原(方案A): 模型只接 /api/terminal/analyze 的 _live_predict 单一真相源。
      //   重建后接的 /api/live-goal-probe/analyze(cross_score) 实测不可信 → 换回。
      //   momentum / consensus / trust-card / induce-flag / duel 按用户拍板暂不接入。
      const _sp = (m.score || '0-0').split('-')
      const _hg = parseInt(_sp[0] ?? '0', 10) || 0
      const _ag = parseInt(_sp[1] ?? '0', 10) || 0
      const [analRes, bcRes, oeRes] = await Promise.all([
        // sportKey 传空串: 后端会按队名自动反查真实联赛。
        // ⚠ 绝不能传默认 'soccer_fifa_world_cup' —— 后端据此判定 WC 并改用
        //   goal_scale=1.35(世界杯校准), 对普通联赛会系统性高估总进球。
        terminalService.analyze(
          m.home, m.away, '',
          (m.odds_h && m.odds_d && m.odds_a) ? { h: m.odds_h, d: m.odds_d, a: m.odds_a } : undefined,
          { ou_line: m.ou_line ?? undefined, ou_over: m.ou_over ?? undefined, ou_under: m.ou_under ?? undefined },
          min > 0 ? { homeGoals: _hg, awayGoals: _ag, elapsed: min } : undefined,
        ).then((r) => (r as any)?.data?.data ?? (r as any)?.data).catch(() => null),
        // 4 盘口诚实综合(2026-08-31): 胜平负/大小球/让球/波胆 候选信号, 来源开盘赔率+模型
        bestComboService.analyze({
          home: m.home, away: m.away, sport_key: '',
          odds_h: m.odds_h ?? undefined, odds_d: m.odds_d ?? undefined, odds_a: m.odds_a ?? undefined,
          ou_line: m.ou_line ?? undefined, ou_over: m.ou_over ?? undefined, ou_under: m.ou_under ?? undefined,
        }).then((r) => (r as any)?.data?.data?.result ?? null).catch(() => null),
        // 开盘天眼 +EV 裁判(2026-08-31): 独立实力特征 + 盘口, 覆盖门/无edge -> PASS
        openEyeService.recommend({
          home: m.home, away: m.away, sport_key: '',
          odds_h: m.odds_h ?? undefined, odds_d: m.odds_d ?? undefined, odds_a: m.odds_a ?? undefined,
        }).then((r) => (r as any)?.data?.data?.result ?? null).catch(() => null),
      ])
      setAnal(adaptLivePredict(analRes, m.score) || null)
      setBestCombo(bcRes || null)
      setOpenEye(oeRes || null)
      // 暂未接入的模型置空 (用户拍板: 其它模型先不接赛程页)
      setMomentum(null)
      setConsensus(null)
      setTrustCard(null)
      setInduce(null)
      setError('')
    } catch (e: any) {
      setError(e?.response?.data?.error || e?.message || '探测失败')
    } finally {
      setLoading(false)
    }
  }, [computeProbeMinute])

  // 5s 轮询: 比赛列表 + 轻量破蛋 probe (保持与采集频率匹配)
  useEffect(() => {
    fetchMatches()
    timerRef.current = setInterval(() => {
      fetchMatches()
      if (selected) fetchProbeLight(selected)
    }, POLL_INTERVAL)
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [fetchMatches, fetchProbeLight, selected])

  // 30s 重分析轮询: _live_predict 全链路 + 4盘口综合 + 开盘天眼
  // (与 5s 轻轮询解耦, 2026-08-31 优化; 切换比赛时下方 selected effect 立即触发)
  useEffect(() => {
    if (!selected) return
    fetchProbeHeavy(selected)
    const id = setInterval(() => { if (selected) fetchProbeHeavy(selected) }, HEAVY_POLL_INTERVAL)
    return () => clearInterval(id)
  }, [selected, fetchProbeHeavy])

  useEffect(() => {
    if (matches.length && !selected) {
      // 后端已按破蛋/进球潜力排序, 默认选中优先级最高的比赛
      setSelected(matches[0])
    }
  }, [matches, selected])

  // 切换比赛: 立即轻量 probe (重分析由上方 effect 兜底触发)
  useEffect(() => {
    if (selected) fetchProbeLight(selected)
  }, [selected, fetchProbeLight])

  useEffect(() => {
    fetchBacktest()
  }, [fetchBacktest])

  // 列表过滤: 搜索(队名/联赛) + 仅看0-0(破蛋核心场景) + 隐藏已完场僵尸
  const filteredMatches = matches.filter(m => {
    const displayMin = resolveDisplayMinute(m, now, lastUpdate ?? 0)
    const isFinished = displayMin != null && displayMin >= MAX_MIN - 1
    if (isFinished) return false
    if (onlyGoalless && m.score !== '0-0') return false
    if (search.trim()) {
      const q = search.trim().toLowerCase()
      const hay = `${m.home} ${m.away} ${m.league}`.toLowerCase()
      if (!hay.includes(q)) return false
    }
    return true
  })

  return {
    matches, selected, setSelected,
    probe, anal, momentum, consensus, trustCard, induce, bestCombo, openEye,
    loading, error, lastUpdate, maxLastSeen, backtest, now,
    search, setSearch, onlyGoalless, setOnlyGoalless,
    filteredMatches,
  }
}

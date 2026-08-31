import { useState, useEffect, useRef, useCallback } from 'react'
import { terminalService, rankedService } from '@/services/api'
import type { TerminalDecisionCard } from '@/types'

// ── 弹窗 props 契约 (LiveScores 页传入) ──
export interface HandicapLike {
  ah_line?: number | string; ah_home?: number; ah_away?: number
  ou_line?: number | string; ou_over?: number; ou_under?: number
}
export interface MatchAnalysisModalProps {
  home: string; away: string; sportKey: string
  odds?: { h: number; d: number; a: number }
  handicap?: HandicapLike
  initialOdds?: { h: number; d: number; a: number }
  initialHandicap?: HandicapLike
  focus?: 'overview' | 'correct_score'
  onClose: () => void
  liveScore?: { homeGoals?: number; awayGoals?: number; elapsed?: number }
}

// ── 数据逻辑: 全链路决策卡 + 概率排名编排器 + 25s 热刷新 + 卸载守卫 ──
export function useMatchAnalysis(props: MatchAnalysisModalProps) {
  const { home, away, sportKey, odds, handicap, initialOdds, initialHandicap, liveScore, focus } = props
  const [card, setCard] = useState<TerminalDecisionCard | null>(null)
  const [cardInitial, setCardInitial] = useState<TerminalDecisionCard | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // 概率排名总览 (ranked_predictor) — 独立 state, 不影响现有决策卡
  const [ranked, setRanked] = useState<any | null>(null)
  const [rankedError, setRankedError] = useState<string | null>(null)
  // 波胆快捷入口 → 默认展开子市场 (含波胆)
  const [showSub, setShowSub] = useState(focus === 'correct_score')
  // 主推视图 Tab: ranked(概率排名主推, 默认) / decision(全链路决策卡)
  const [activeTab, setActiveTab] = useState<'ranked' | 'decision'>('ranked')
  const oipRef = useRef<HTMLDivElement>(null)

  // 数据就绪 + 聚焦波胆 → 自动滚动到 OIP 波胆模型区
  useEffect(() => {
    if (card && !loading && focus === 'correct_score' && oipRef.current) {
      oipRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, [card, loading, focus])

  // 用 ref 持有最新 props, 避免 setInterval 闭包过期 (弹窗打开期间 liveScore/odds 会持续更新)
  const propsRef = useRef({ home, away, sportKey, odds, handicap, initialOdds, initialHandicap, liveScore })
  propsRef.current = { home, away, sportKey, odds, handicap, initialOdds, initialHandicap, liveScore }

  // 卸载守卫: 弹窗关闭后取消所有未完成的 setState, 防止 React 警告/内存泄漏
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  // 拉取全链路分析 + 概率排名 (silent=true 为热刷新, 不清空/不闪 loading)
  const runAnalysis = useCallback((silent = false) => {
    const { home, away, sportKey, odds, handicap, initialOdds, initialHandicap, liveScore } = propsRef.current
    if (!mountedRef.current) return
    if (!silent) {
      setLoading(true); setError(null); setCard(null); setCardInitial(null)
      setRanked(null); setRankedError(null)
    }
    // 同源传入选中的让球/大小球盘口 → 后端波胆×让球×大小球交叉标注才准确
    const liveReq = terminalService.analyze(home, away, sportKey, odds, handicap, liveScore)
    const reqs: Promise<any>[] = [liveReq]
    // Req2: 有开盘快照 → 并行跑一次初始分析, 弹窗双栏对比
    if (initialOdds) reqs.push(terminalService.analyze(home, away, sportKey, initialOdds, initialHandicap))

    // Req3: 概率排名编排器 (独立并行, 不影响现有决策卡/双栏对照)
    // 仅当本场有完整 1X2 + OU 盘口时调用; 操盘手CS赔率由后端自动从 GQ.db 回退
    if (odds && handicap && handicap.ou_line != null && handicap.ou_over != null && handicap.ou_under != null) {
      rankedService.predict(home, away, odds, handicap)
        .then((res) => {
          if (!mountedRef.current) return
          const d = (res.data as any)?.data || res.data
          if (d?.__error || d?.error) setRankedError(String(d.__error || d.error))
          else setRanked(d || null)
        })
        .catch((e: any) => { if (mountedRef.current) setRankedError(e?.response?.data?.detail || e?.message || '排名分析失败') })
    }
    Promise.all(reqs.map(r => r.then((res) => {
      const d = (res.data as any)?.data || res.data
      return d?.error ? { __error: d.error } : (d as TerminalDecisionCard)
    }).catch((e) => ({ __error: e?.response?.data?.detail || e?.message || '分析失败' }))))
      .then(([live, initial]: any) => {
        if (!mountedRef.current) return
        if (live?.__error) setError(live.__error)
        else setCard(live || null)
        setCardInitial(initial?.__error ? null : (initial || null))
      })
      .finally(() => { if (mountedRef.current && !silent) setLoading(false) })
  }, [])

  // 打开时拉一次; 比赛进行中(elapsed 有值)→ 每 25s 热刷新, 消除弹窗数据冻结导致的"信息滞后"
  useEffect(() => {
    runAnalysis(false)
    const isLive = propsRef.current.liveScore?.elapsed != null
    if (!isLive) return
    const t = setInterval(() => runAnalysis(true), 25000)
    return () => clearInterval(t)
  }, [runAnalysis, home, away, sportKey, initialOdds, initialHandicap, liveScore?.elapsed])

  return { card, cardInitial, loading, error, ranked, rankedError, showSub, setShowSub, activeTab, setActiveTab, oipRef }
}

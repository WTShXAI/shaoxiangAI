import { motion } from 'framer-motion'
import { useMatchAnalysis, type MatchAnalysisModalProps } from './useMatchAnalysis'
import TeamLogo from '@/components/shared/TeamLogo'
import CSTrustCard from '@/components/CSTrustCard'
import { fmtGMT8 } from '@/pages/LiveScores/fixtureUtils'
import {
  VerdictMini, DecisionVerdictBar, MarketOddsGrid, ValueLayerBlock, InplayBanner,
  MultiBookConsensus, OperatorView, OperatorCardSection,
  OperatorSignals, StrategySignals, SubMarkets, SoftlineBanner,
} from './sections'
import { OipCard } from './oip'
import { pct, num } from './format'

// 模型溯源徽标 — 回答"接入什么模型号"
function ModelBadge({ color, label, version }: { color: 'field' | 'pitch' | 'ember'; label: string; version: string }) {
  const dot = color === 'field' ? 'bg-field-500' : color === 'pitch' ? 'bg-pitch-500' : 'bg-ember-500'
  const box = color === 'field'
    ? 'bg-field-500/10 text-field-400 border-field-500/20'
    : color === 'pitch'
      ? 'bg-pitch-500/10 text-pitch-300 border-pitch-500/20'
      : 'bg-ember-500/10 text-ember-300 border-ember-500/20'
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md border text-[10px] ${box}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${dot}`} />
      <span className="font-medium">{label}</span>
      <span className="opacity-75 font-mono">{version}</span>
    </span>
  )
}

// 模型溯源清单 — 严格对齐 pipeline/model_catalog.py 台账 (2026-08-05 核实), 不再硬编码误导文案
// 三个区块各自真实调用的模型集合, 后端改编排时此处需同步
const MODEL_TRACING = [
  { color: 'pitch' as const, label: '概率排名主推', version: 'ranked_predictor',
    detail: 'M1 WI 主模型 + M2 Score(OIP) + M3 ReverseOdds + M5 FL 结构库 + M6 ValueSignal + M7 分析中心' },
  { color: 'field' as const, label: '全链路决策', version: 'unified_predictor v7.4',
    detail: '后端 _live_predict: M3 ReverseOdds 诱盘破解 + M4 DriftReliability 漂移 + OIP 波胆 + 价值层' },
  { color: 'ember' as const, label: 'CS 信任卡', version: 'cs_trust_model 黄金神瞳',
    detail: '波胆唯一权威模型: build_trust_card = OIP 基座 + 实证频率(10905+场) + 诱导标记 + 信任分; 弹窗波胆精选仅此一处' },
]

function statusFrom(props: { matchState?: string | number; kickoff?: string; liveScore?: { elapsed?: number } }) {
  const st = Number(props.matchState ?? 0)
  const elapsed = props.liveScore?.elapsed
  if (elapsed != null && st >= 0 && st < 6) {
    return { kind: 'live' as const, label: `进行中 ${elapsed}'`, showScore: true }
  }
  if (st < 0) {
    return { kind: 'finished' as const, label: '已结束', showScore: true }
  }
  if (st === 2) {
    return { kind: 'halftime' as const, label: '中场休息', showScore: true }
  }
  return { kind: 'upcoming' as const, label: (props.kickoff ? fmtGMT8(props.kickoff) : '') || '未开赛', showScore: false }
}

export default function MatchAnalysisModal(props: MatchAnalysisModalProps) {
  const { home, away, league, kickoff, onClose } = props
  const { card, cardInitial, loading, error, ranked, rankedError, rankedLoading, trustCard, trustCardError, trustCardLoading, showSub, setShowSub, oipRef, csRef } = useMatchAnalysis(props)
  const status = statusFrom(props)
  const homeGoals = props.liveScore?.homeGoals ?? 0
  const awayGoals = props.liveScore?.awayGoals ?? 0

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 backdrop-blur-sm p-3 md:p-4" onClick={onClose}>
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95 }}
        onClick={(e) => e.stopPropagation()}
        className="bg-surface-panel border border-surface-border rounded-2xl w-full max-w-2xl max-h-[90vh] overflow-y-auto shadow-2xl"
      >
        {/* 弹窗头：联赛 + 对阵信息 + 比分/状态 */}
        <div className="sticky top-0 z-10 bg-surface-panel/95 backdrop-blur px-4 md:px-6 pt-4 pb-3 border-b border-surface-border/40">
          <div className="flex items-start justify-between gap-3">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-2">
                <span className="px-2 py-0.5 rounded-full bg-field-500/15 text-field-400 text-[10px] font-bold border border-field-500/20">
                  {league || '赛程'}
                </span>
                <span className="text-[10px] text-ink-muted">{kickoff ? fmtGMT8(kickoff) : ''}</span>
              </div>
              <div className="flex items-center gap-3">
                <div className="flex-1 flex items-center justify-end gap-2 min-w-0">
                  <span className="text-[15px] md:text-base font-bold text-ink-primary truncate text-right hidden sm:block">{home}</span>
                  <span className="text-[13px] font-bold text-ink-primary truncate text-right sm:hidden">{home}</span>
                  <TeamLogo name={home} size="md" />
                </div>
                <div className="shrink-0 text-center px-2">
                  {status.showScore ? (
                    <div className="text-[22px] md:text-[26px] font-black font-mono text-ink-primary tracking-wider leading-none">
                      {homeGoals}:{awayGoals}
                    </div>
                  ) : (
                    <div className="text-[14px] font-bold text-ink-muted">VS</div>
                  )}
                  <div className={`mt-1 text-[10px] ${status.kind === 'live' ? 'text-ember-400' : 'text-ink-muted'}`}>
                    {status.kind === 'live' && <span className="inline-block w-1.5 h-1.5 rounded-full bg-ember-500 animate-pulse mr-1 align-middle" />}
                    {status.label}
                  </div>
                </div>
                <div className="flex-1 flex items-center justify-start gap-2 min-w-0">
                  <TeamLogo name={away} size="md" />
                  <span className="text-[15px] md:text-base font-bold text-ink-primary truncate hidden sm:block">{away}</span>
                  <span className="text-[13px] font-bold text-ink-primary truncate sm:hidden">{away}</span>
                </div>
              </div>
            </div>
            <button onClick={onClose} className="w-8 h-8 rounded-lg flex items-center justify-center text-ink-muted hover:text-ink-primary hover:bg-surface-dark/60 shrink-0">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2"><path strokeLinecap="round" d="M6 18L18 6M6 6l12 12" /></svg>
            </button>
          </div>

          {/* Tab 切换已合并为单张分析卡: 模型溯源 + 概率排名 + 全链路决策 同屏 */}
        </div>

        <div className="p-4 md:p-6 flex flex-col gap-5">
          {/* Loading */}
          {loading && (
            <div className="py-16 text-center">
              <div className="inline-block w-8 h-8 border-2 border-field-500/30 border-t-field-500 rounded-full animate-spin mb-3" />
              <p className="text-sm text-ink-secondary">全链路分析中 · OIP波胆 / 让球 / 价值层 / 子市场…</p>
              <p className="text-[12px] text-ink-muted mt-1">直接用盘口赔率计算，通常 1 秒内完成</p>
            </div>
          )}

          {/* Error */}
          {error && !loading && (
            <div className="py-12 text-center">
              <div className="text-3xl mb-3">⚠️</div>
              <p className="text-sm text-ink-primary font-medium">分析失败</p>
              <p className="text-[12px] text-ink-secondary mt-2 max-w-sm mx-auto">{error}</p>
              <p className="text-[11px] text-ink-muted mt-3">提示: 该比赛可能无盘口赔率数据，或赔率未采集</p>
            </div>
          )}

          {/* 单张分析卡: 模型溯源 + CS信任卡 + 概率排名 + 全链路决策 同屏, 不再分两卡 */}
          {!loading && !error && (
            <div className="flex flex-col gap-4">
              {/* 模型溯源 — 明示弹窗三路桥接的真实模型号 (对齐 pipeline/model_catalog.py 台账) */}
              <div className="rounded-lg bg-surface-dark/40 border border-surface-border/40 p-3">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-[10px] text-ink-muted font-medium">模型溯源</span>
                  <span className="text-[9px] text-ink-disabled font-mono">v7.4 pipeline · 真实编排</span>
                </div>
                <div className="space-y-1.5">
                  {MODEL_TRACING.map((m) => (
                    <div key={m.label} className="flex items-start gap-2">
                      <ModelBadge color={m.color} label={m.label} version={m.version} />
                      <span className="text-[10px] text-ink-muted leading-5 pt-0.5 flex-1 min-w-0">{m.detail}</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* CS 信任卡 (黄金神瞳 build_trust_card) — 波胆唯一权威模型, 与两路主分析解耦, 无盘口仍可回退纯 1X2/历史实证 */}
              <div ref={csRef}>
                {trustCardLoading && !trustCard && !trustCardError && (
                  <div className="flex items-center gap-2 text-[11px] text-ink-muted px-1 py-2">
                    <span className="inline-block w-3.5 h-3.5 border-2 border-ember-500/30 border-t-ember-500 rounded-full animate-spin" />
                    CS 信任卡计算中…
                  </div>
                )}
                <CSTrustCard trustCard={trustCard} />
                {trustCardError && !trustCard && (
                  <div className="text-[11px] text-ink-muted px-1">CS 信任卡: {trustCardError}</div>
                )}
              </div>

              {/* 概率排名主推 */}
              {rankedLoading ? (
                <div className="rounded-xl bg-field-500/[0.06] border border-field-500/20 p-3">
                  <div className="flex items-center gap-2 text-[11px] text-ink-muted">
                    <span className="inline-block w-3.5 h-3.5 border-2 border-field-500/30 border-t-field-500 rounded-full animate-spin" />
                    概率排名计算中…
                  </div>
                </div>
              ) : ranked ? (
                <div className="rounded-xl bg-field-500/[0.06] border border-field-500/20 p-3">
                  <div className="flex items-center gap-2 mb-2 flex-wrap">
                    <span className="text-[10px] px-1.5 py-0.5 bg-pitch-500/25 text-pitch-300 rounded font-bold">🏆 概率排名</span>
                    <ModelBadge color="pitch" label="ranked_predictor" version="M1 WI·M2 Score·M3 ReverseOdds·M5 FL·M6 Value·M7" />
                    <span className="text-[10px] px-1.5 py-0.5 bg-field-500/15 text-field-400 rounded font-bold">OU 不特权</span>
                    {ranked.operator_intent && (
                      <span className="text-[10px] text-ink-muted">操盘手意图: {ranked.operator_intent}</span>
                    )}
                  </div>
                  {/* combined_top 跨市场统一排名榜 */}
                  <div className="space-y-1 mb-3">
                    {(ranked.combined_top || []).map((item: any, i: number) => (
                      <div key={item[0]} className={`flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-[12px] font-mono ${i === 0 ? 'bg-field-500/15 border border-field-500/30 text-field-400 font-bold' : 'bg-surface-dark/40 text-ink-primary'}`}>
                        <span className="w-4 text-ink-muted">{i + 1}</span>
                        <span className="flex-1 truncate">{item[0]}</span>
                        <span>{pct(item[1], 1)}</span>
                      </div>
                    ))}
                  </div>
                  {/* 三市场概率小卡 */}
                  <div className="grid grid-cols-2 gap-2 mb-3">
                    <div className="bg-surface-dark/50 rounded-lg px-2 py-2">
                      <div className="text-[10px] text-ink-secondary">1X2 去水概率</div>
                      <div className="font-mono text-[12px] mt-0.5 text-ink-primary">{pct(ranked.markets?.['1x2']?.p_h)}/{pct(ranked.markets?.['1x2']?.p_d)}/{pct(ranked.markets?.['1x2']?.p_a)}</div>
                      <div className="text-[10px] text-ink-muted mt-0.5">主/平/客</div>
                    </div>
                    <div className="bg-surface-dark/50 rounded-lg px-2 py-2">
                      <div className="text-[10px] text-ink-secondary">大小球 ({ranked.markets?.ou?.line ?? '—'})</div>
                      <div className="font-mono text-[12px] mt-0.5 text-ink-primary">大{pct(ranked.markets?.ou?.p_over)}/小{pct(ranked.markets?.ou?.p_under)}</div>
                      <div className="text-[10px] text-ink-muted mt-0.5">方向稍后</div>
                    </div>
                  </div>
                  {/* 分析七段 */}
                  <div className="space-y-1.5">
                    <p className="text-[12px] text-ink-primary leading-snug">{ranked.analysis?.verdict}</p>
                    <div className="grid grid-cols-1 gap-y-1 text-[11px] text-ink-secondary">
                      <div><span className="text-field-400">1X2:</span> {ranked.analysis?.['1x2']}</div>
                      <div><span className="text-field-400">OU:</span> {ranked.analysis?.ou}</div>
                      <div><span className="text-field-400">CS:</span> {ranked.analysis?.cs}</div>
                      <div><span className="text-field-400">操盘手:</span> {ranked.analysis?.operator}</div>
                      <div className="text-ember-400/90"><span className="text-ember-400">风险:</span> {ranked.analysis?.risk}</div>
                    </div>
                  </div>
                </div>
              ) : rankedError ? (
                <div className="text-[11px] text-ink-muted px-1">概率排名分析: {rankedError}</div>
              ) : (
                <div className="text-[12px] text-ink-muted px-1 py-2">暂无概率排名数据 (本场可能无 1X2 初盘)</div>
              )}

              {/* 全链路决策 */}
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-[10px] px-1.5 py-0.5 bg-field-500/25 text-field-400 rounded font-bold">🧠 全链路决策</span>
                <ModelBadge color="field" label="unified_predictor" version="M3 ReverseOdds + M4 Drift" />
              </div>

              {/* Req2: 初始分析 vs 实时分析 双栏对照 */}
              {card && cardInitial && (
                <div className="rounded-xl border border-surface-border/40 bg-surface-dark/30 p-3">
                  <div className="flex items-center justify-between mb-2">
                    <div className="text-[11px] font-mono text-field-400 tracking-widest uppercase">初始 vs 实时 · 走势对照</div>
                    {card.best_edge_pct !== undefined && cardInitial.best_edge_pct !== undefined && (() => {
                      const d = card.best_edge_pct - cardInitial.best_edge_pct
                      return (
                        <span className={`text-[11px] font-mono font-bold ${d > 0 ? 'text-field-400' : d < 0 ? 'text-ember-500' : 'text-ink-muted'}`}>
                          edge {d >= 0 ? '+' : ''}{num(d, 1)}%
                        </span>
                      )
                    })()}
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <VerdictMini label="初始分析" card={cardInitial} />
                    <VerdictMini label="实时分析" card={card} />
                  </div>
                </div>
              )}

              {/* 决策卡 */}
              {card ? (
                <>
                  <DecisionVerdictBar card={card} />
                  <OperatorCardSection card={card} />
                  <MarketOddsGrid card={card} />
                  <ValueLayerBlock card={card} />
                  <InplayBanner card={card} />
                  <OipCard card={card} focus={props.focus} oipRef={oipRef} />
                  <MultiBookConsensus card={card} />
                  <OperatorView card={card} />
                  <OperatorSignals card={card} />
                  <StrategySignals card={card} />
                  <SubMarkets card={card} showSub={showSub} setShowSub={setShowSub} />
                  <SoftlineBanner card={card} />
                </>
              ) : (
                <div className="text-[12px] text-ink-muted px-1 py-2">暂无全链路决策数据</div>
              )}
            </div>
          )}
        </div>
      </motion.div>
    </div>
  )
}

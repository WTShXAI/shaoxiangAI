import { motion } from 'framer-motion'
import { useMatchAnalysis, type MatchAnalysisModalProps } from './useMatchAnalysis'
import {
  VerdictMini, DecisionVerdictBar, MarketOddsGrid, ValueLayerBlock, InplayBanner,
  Top5Recommend, MultiBookConsensus, CorrectScoreRecommend, OperatorView,
  OperatorSignals, StrategySignals, SubMarkets, SoftlineBanner,
} from './sections'
import { OipCard } from './oip'
import { pct, num, dirLabel2 } from './format'

export default function MatchAnalysisModal(props: MatchAnalysisModalProps) {
  const { home, away, onClose, focus } = props
  const { card, cardInitial, loading, error, ranked, rankedError, showSub, setShowSub, activeTab, setActiveTab, oipRef } = useMatchAnalysis(props)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 backdrop-blur-sm p-4" onClick={onClose}>
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95 }}
        onClick={(e) => e.stopPropagation()}
        className="bg-accent-card border border-white/[0.08] rounded-xl w-full max-w-2xl max-h-[88vh] overflow-y-auto shadow-2xl"
      >
        {/* 弹窗头 */}
        <div className="sticky top-0 z-10 bg-accent-card/95 backdrop-blur px-6 py-4 border-b border-white/[0.06] flex items-center justify-between">
          <div>
            <div className="text-[11px] font-mono text-accent tracking-widest uppercase">LIVE DECODE</div>
            <h3 className="text-lg font-bold text-white mt-0.5">{home} <span className="text-white/70 mx-1 text-sm font-normal">vs</span> {away}</h3>
          </div>
          <button onClick={onClose} className="w-8 h-8 rounded-lg flex items-center justify-center text-white/70 hover:text-white hover:bg-white/[0.06]">
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2"><path strokeLinecap="round" d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>

        <div className="p-6 flex flex-col gap-5">
          {/* Loading */}
          {loading && (
            <div className="py-16 text-center">
              <div className="inline-block w-8 h-8 border-2 border-accent/30 border-t-accent rounded-full animate-spin mb-3" />
              <p className="text-sm text-white/75">全链路分析中 · OIP波胆 / 让球 / 价值层 / 子市场…</p>
              <p className="text-[12px] text-white/60 mt-1">直接用盘口赔率计算,通常 1 秒内完成</p>
            </div>
          )}

          {/* Error */}
          {error && !loading && (
            <div className="py-12 text-center">
              <div className="text-3xl mb-3">⚠️</div>
              <p className="text-sm text-white/85 font-medium">分析失败</p>
              <p className="text-[12px] text-white/70 mt-2 max-w-sm mx-auto">{error}</p>
              <p className="text-[11px] text-white/60 mt-3">提示: 该比赛可能无盘口赔率数据,或赔率未采集</p>
            </div>
          )}

          {/* ── 主推视图 Tab 切换: 概率排名(默认) / 全链路决策卡 ── */}
          <div className="flex gap-2 border-b border-white/[0.06] pb-3">
            <button
              onClick={() => setActiveTab('ranked')}
              className={`px-4 py-2 rounded-lg text-[13px] font-bold transition-colors ${
                activeTab === 'ranked'
                  ? 'bg-accent/20 text-accent border border-accent/30'
                  : 'bg-white/[0.04] text-white/60 border border-transparent hover:text-white'
              }`}
            >
              🏆 概率排名主推
            </button>
            <button
              onClick={() => setActiveTab('decision')}
              className={`px-4 py-2 rounded-lg text-[13px] font-bold transition-colors ${
                activeTab === 'decision'
                  ? 'bg-accent/20 text-accent border border-accent/30'
                  : 'bg-white/[0.04] text-white/60 border border-transparent hover:text-white'
              }`}
            >
              全链路决策卡
            </button>
          </div>

          {/* Tab 内容: 概率排名主推 (ranked_predictor — 三市场概率排名, OU不特权) */}
          {activeTab === 'ranked' && (
            ranked ? (
              <div className="rounded-xl bg-accent/[0.06] border border-accent/25 p-3">
                <div className="flex items-center gap-2 mb-2 flex-wrap">
                  <span className="text-[10px] px-1.5 py-0.5 bg-pitch-500/25 text-pitch-300 rounded font-bold">🏆 主推视图</span>
                  <div className="text-[11px] font-mono text-accent tracking-widest uppercase">概率排名总览 / RANKED</div>
                  <span className="text-[10px] px-1.5 py-0.5 bg-accent/15 text-accent rounded font-bold">OU 不特权</span>
                  {ranked.operator_intent && (
                    <span className="text-[10px] text-white/60">操盘手意图: {ranked.operator_intent}</span>
                  )}
                </div>
                {/* combined_top 跨市场统一排名榜 */}
                <div className="space-y-1 mb-3">
                  {(ranked.combined_top || []).map((item: any, i: number) => (
                    <div key={item[0]} className={`flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-[12px] font-mono ${i === 0 ? 'bg-accent/15 border border-accent/30 text-accent font-bold' : 'bg-white/[0.04] text-white/85'}`}>
                      <span className="w-4 text-white/50">{i + 1}</span>
                      <span className="flex-1 truncate">{item[0]}</span>
                      <span>{pct(item[1], 1)}</span>
                    </div>
                  ))}
                </div>
                {/* 三市场概率小卡 */}
                <div className="grid grid-cols-3 gap-2 mb-3">
                  <div className="bg-accent-inner rounded-lg px-2 py-2">
                    <div className="text-[10px] text-white/70">1X2 去水概率</div>
                    <div className="font-mono text-[12px] mt-0.5">{pct(ranked.markets?.['1x2']?.p_h)}/{pct(ranked.markets?.['1x2']?.p_d)}/{pct(ranked.markets?.['1x2']?.p_a)}</div>
                    <div className="text-[10px] text-white/50 mt-0.5">主/平/客</div>
                  </div>
                  <div className="bg-accent-inner rounded-lg px-2 py-2">
                    <div className="text-[10px] text-white/70">大小球 ({ranked.markets?.ou?.line ?? '—'})</div>
                    <div className="font-mono text-[12px] mt-0.5">{dirLabel2(ranked.markets?.ou?.direction)}</div>
                    <div className="text-[10px] text-white/50 mt-0.5">大{pct(ranked.markets?.ou?.p_over)}/小{pct(ranked.markets?.ou?.p_under)}</div>
                  </div>
                  <div className="bg-accent-inner rounded-lg px-2 py-2">
                    <div className="text-[10px] text-white/70">波胆榜首</div>
                    <div className="font-mono text-[12px] mt-0.5">{ranked.markets?.cs?.ranked?.[0]?.[0] || '—'}</div>
                    <div className="text-[10px] text-white/50 mt-0.5">{pct(ranked.markets?.cs?.ranked?.[0]?.[1])}</div>
                  </div>
                </div>
                {/* 分析七段 */}
                <div className="space-y-1.5">
                  <p className="text-[12px] text-white/85 leading-snug">{ranked.analysis?.verdict}</p>
                  <div className="grid grid-cols-1 gap-y-1 text-[11px] text-white/70">
                    <div><span className="text-accent">1X2:</span> {ranked.analysis?.['1x2']}</div>
                    <div><span className="text-accent">OU:</span> {ranked.analysis?.ou}</div>
                    <div><span className="text-accent">CS:</span> {ranked.analysis?.cs}</div>
                    <div><span className="text-accent">操盘手:</span> {ranked.analysis?.operator}</div>
                    <div className="text-ember-400/90"><span className="text-ember-400">风险:</span> {ranked.analysis?.risk}</div>
                  </div>
                </div>
              </div>
            ) : rankedError ? (
              <div className="text-[11px] text-white/45 px-1">概率排名分析: {rankedError}</div>
            ) : (
              <div className="text-[12px] text-white/50 px-1 py-2">概率排名加载中…</div>
            )
          )}

          {/* Req2: 初始分析 vs 实时分析 双栏对照 (置于全链路决策卡 Tab 内) */}
          {card && cardInitial && !loading && !error && activeTab === 'decision' && (
            <div style={{ order: 3 }} className="rounded-xl border border-white/[0.08] bg-white/[0.02] p-3">
              <div className="flex items-center justify-between mb-2">
                <div className="text-[11px] font-mono text-accent tracking-widest uppercase">初始 vs 实时 · 走势对照</div>
                {card.best_edge_pct !== undefined && cardInitial.best_edge_pct !== undefined && (() => {
                  const d = card.best_edge_pct - cardInitial.best_edge_pct
                  return (
                    <span className={`text-[11px] font-mono font-bold ${d > 0 ? 'text-accent' : d < 0 ? 'text-ember-500' : 'text-white/70'}`}>
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
          {card && !loading && !error && activeTab === 'decision' && (
            <>
              <DecisionVerdictBar card={card} />
              <MarketOddsGrid card={card} />
              <ValueLayerBlock card={card} />
              <InplayBanner card={card} />
              <OipCard card={card} focus={focus} oipRef={oipRef} />
              <Top5Recommend card={card} />
              <MultiBookConsensus card={card} />
              <CorrectScoreRecommend card={card} />
              <OperatorView card={card} />
              <OperatorSignals card={card} />
              <StrategySignals card={card} />
              <SubMarkets card={card} showSub={showSub} setShowSub={setShowSub} />
              <SoftlineBanner card={card} />
            </>
          )}
        </div>
      </motion.div>
    </div>
  )
}

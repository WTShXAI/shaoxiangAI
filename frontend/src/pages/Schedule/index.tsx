// ═══ 赛程列表页 (2026-08-31 拆分: 组装层) ═══
// 数据逻辑 → useSchedule.ts | 展示组件 → components.tsx | 纯函数 → utils.ts | 类型 → types.ts
// 页面职责仅剩: 布局 + 世界级分析器跳转 + 合理比分大卡渲染
import { useNavigate } from 'react-router-dom'
import PageHeader from '@/components/layout/PageHeader'
import CSTrustCard from '@/components/CSTrustCard'
import BestComboCard from '@/components/BestComboCard'
import OpenEyeCard from '@/components/OpenEyeCard'
import { isModelQualified, gateSummary } from '@/lib/modelQualification'
import Skeleton from '@/components/shared/Skeleton'
import { useSchedule } from './useSchedule'
import { MatchListItem, SourceDot, SideCard, FreshnessBadge, LineDropCard } from './components'
import { formatAgeMs, formatKickoffShort, resolveDisplayMinute } from './utils'
import { MAX_MIN } from './types'

export default function SchedulePage() {
  const navigate = useNavigate()
  const {
    matches, selected, setSelected,
    probe, anal, momentum, consensus, trustCard, induce, bestCombo, openEye,
    loading, error, lastUpdate, maxLastSeen, backtest, now,
    search, setSearch, onlyGoalless, setOnlyGoalless,
    filteredMatches,
  } = useSchedule()

  return (
    <div className="min-h-screen p-4 md:p-6">
      <PageHeader title="赛程列表" subtitle="全比赛 · 点比赛自动跑分析模型（全链路 _live_predict + 滚球破蛋 probe）" />

      {/* 颜色图例 (2026-08-30): 红=模型预测 / 蓝=市场读数 / 绿=滚球实时 */}
      <div className="mt-3 flex items-center gap-4 text-[11px] text-ink-muted">
        <span className="flex items-center gap-1.5"><SourceDot kind="model" />模型预测(_live_predict)</span>
        <span className="flex items-center gap-1.5"><SourceDot kind="market" />市场/庄家读数</span>
        <span className="flex items-center gap-1.5"><SourceDot kind="live" />滚球实时(probe)</span>
      </div>

      {/* 模型达标闸门 (2026-08-31): 仅接入实证达标模型, 不达标不渲染 */}
      <div className="mt-2 flex items-center gap-2 text-[10px]">
        <span className="px-2 py-0.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 text-emerald-300">
          模型达标闸门 ✓ 已启用 · 达标 {gateSummary().passed}/{gateSummary().total} · 拦截 {gateSummary().blocked}
        </span>
        <span className="text-ink-muted/70">实盘ROI&gt;0 且验证样本达标才接入；不达标模型不渲染</span>
      </div>

      {/* 2026-08-28: 删除原"概率警报仪"黄色全级常驻警告 (采集器在跑+数据齐全时不应报警);
         回测/概率层说明保留在详情区(终场读数卡·置信度)  */}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mt-4">
        {/* 左侧比赛列表 */}
        <div className="lg:col-span-1 rounded-xl border border-surface-border/40 bg-surface-dark/30 overflow-hidden flex flex-col max-h-[calc(100vh-220px)]">
          <div className="px-3 py-2 border-b border-surface-border/40 bg-surface-dark/50 flex items-center justify-between">
            <span className="text-[12px] font-semibold text-ink-primary">进行中比赛</span>
            <div className="flex items-center gap-2">
              <FreshnessBadge maxLastSeen={maxLastSeen} now={now} />
              <span className="text-[10px] text-ink-muted">
                {lastUpdate ? `拉取于 ${formatAgeMs(now - lastUpdate)}` : '加载中...'}
              </span>
            </div>
          </div>
          <div className="px-2 py-1.5 border-b border-surface-border/30 bg-surface-dark/30 space-y-1.5">
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="搜索队名 / 联赛..."
              className="w-full text-[11px] px-2 py-1 rounded bg-surface-dark/80 border border-surface-border/40 text-ink-primary placeholder:text-ink-muted/50 focus:outline-none focus:border-frost-500/50"
            />
            <div className="flex items-center justify-between">
              <label className="flex items-center gap-1.5 text-[10px] text-ink-muted cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={onlyGoalless}
                  onChange={e => setOnlyGoalless(e.target.checked)}
                  className="accent-amber-500 w-3 h-3"
                />
                仅看 0-0 (破蛋)
              </label>
              <span className="text-[10px] text-ink-muted/60">
                {filteredMatches.length}/{matches.length} 场
              </span>
            </div>
          </div>
          <div className="overflow-y-auto flex-1 p-2 space-y-1.5">
            {filteredMatches.map(m => (
              <MatchListItem
                key={m.match_key}
                m={m}
                now={now}
                fetchTime={lastUpdate ?? 0}
                selected={selected?.match_key === m.match_key}
                onClick={() => setSelected(m)}
              />
            ))}
            {filteredMatches.length === 0 && (
              <div className="text-center text-ink-muted text-sm py-8">暂无匹配比赛</div>
            )}
          </div>
        </div>

        {/* 右侧探测结果 */}
        <div className="lg:col-span-2 space-y-4">
          {error && (
            <div className="rounded-lg border border-rose-500/30 bg-rose-500/[0.06] px-3 py-2 text-[12px] text-rose-300">
              {error}
            </div>
          )}

          {!selected && (
            <div className="text-center text-ink-muted text-sm py-12">请选择左侧比赛</div>
          )}

          {selected && (
            <>
              <div className="rounded-xl border border-surface-border/40 bg-surface-dark/30 p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <div className="text-lg font-bold text-ink-primary">
                      {selected.home} <span className="text-ink-muted font-normal">vs</span> {selected.away}
                    </div>
                    <div className="text-[11px] text-ink-muted mt-0.5">
                      {selected.league} · 开赛 {formatKickoffShort(selected.kickoff)}
                      {selected.kickoff && selected.kickoff.includes(' ') ? ` (${selected.kickoff.split(' ')[0]})` : ''}
                    </div>
                  </div>
                  <div className="flex items-center gap-3">
                    <div className="text-center px-4 py-2 rounded-lg bg-surface-dark/60 border border-surface-border/40">
                      <div className="text-[10px] text-ink-muted">比分</div>
                      <div className="text-xl font-mono font-bold text-ink-primary">{selected.score}</div>
                    </div>
                    <div className="text-center px-4 py-2 rounded-lg bg-surface-dark/60 border border-surface-border/40">
                      <div className="text-[10px] text-ink-muted">时间</div>
                      <div className="text-xl font-mono font-bold text-frost-400">
                        {(() => {
                          const min = resolveDisplayMinute(selected, now, lastUpdate ?? 0)
                          return Math.round(min ?? 0) + "'"
                        })()}
                      </div>
                    </div>
                  </div>
                </div>
                {/* 世界级分析器入口: 携带本场实时/开盘 1X2 + OU 跳转自动回填 */}
                <div className="mt-3 flex items-center gap-2">
                  <button
                    onClick={() => {
                      const s = selected
                      if (!s) return
                      const q = new URLSearchParams()
                      q.set('home', s.home)
                      q.set('away', s.away)
                      if (s.league) q.set('league', s.league)
                      if (s.kickoff) q.set('kickoff', s.kickoff)
                      if (s.odds_h != null) q.set('h', String(s.odds_h))
                      if (s.odds_d != null) q.set('d', String(s.odds_d))
                      if (s.odds_a != null) q.set('a', String(s.odds_a))
                      if (s.opening_h != null) q.set('op_h', String(s.opening_h))
                      if (s.opening_d != null) q.set('op_d', String(s.opening_d))
                      if (s.opening_a != null) q.set('op_a', String(s.opening_a))
                      if (s.ou_line != null) q.set('ou_line', String(s.ou_line))
                      if (s.ou_over != null) q.set('ou_over', String(s.ou_over))
                      if (s.ou_under != null) q.set('ou_under', String(s.ou_under))
                      navigate(`/world-analyzer?${q.toString()}`)
                    }}
                    className="px-3 py-1.5 rounded-lg bg-field-500/15 border border-field-500/40 text-field-400 text-xs font-medium hover:bg-field-500/25 transition-colors"
                    title="打开世界级分析器并自动回填本场赔率"
                  >
                    世界级分析 →
                  </button>
                  {probe?.fav_odds && (
                    <span className="text-[11px] text-ink-muted flex items-center gap-1.5">
                      <SourceDot kind="market" />1X2 热门赔率: <span className="font-mono text-ink-secondary">{probe.fav_odds.toFixed(2)}</span>
                    </span>
                  )}
                </div>
              </div>

              {/* 30px 合理比分大卡 (治 CS 矛盾: 矛盾警告置顶, 比分置下, 永久免责锚) */}
              {isModelQualified('live_predict_1x2') && (() => {
                const sh: any = anal?.score_hint
                const cur = selected?.score ?? ''
                // 2026-08-29 修复: 原 `sh.score !== cur` **字符串全等**比较 → 滚球阶段
                //   几乎恒为真(模型推的是终场比分, 当然 ≠ 当前比分), 红色警告变成常态噪音,
                //   且文案称 sh.score "未接 in-play 比分" 是事实错误 —— sh.score 恰恰是
                //   ② 滚球修正主推(已条件化), 未接 in-play 的是 sh.score_opening。
                //   改用后端语义标记 roll_conflict (主推方向与当前比分领先方相反) 判定,
                //   仅在**真反向**时报警; 无该字段(旧响应)时回退原逻辑。
                const contradict = !!(sh?.roll_conflict ?? (sh?.score && cur && sh.score !== cur))
                const openingConflict = !!sh?.opening_conflict
                if (!sh) return (
                  <div className="text-[11px] text-ink-muted bg-white/[0.03] rounded-lg p-2">
                    无合理比分（本场无真实开盘赔率，无法推导）
                  </div>
                )
                return (
                  <div className={`rounded-xl border p-4 ${
                    contradict
                      ? 'border-danger-500/60 bg-danger-500/[0.06] shadow-[0_0_18px_rgba(248,113,113,0.18)]'
                      : 'border-ember-500/30 bg-ember-500/[0.06]'
                  }`}>
                    <div className="flex items-center justify-between mb-2">
                      <span className={`text-[12px] font-semibold flex items-center gap-1.5 ${contradict ? 'text-danger-300' : 'text-ember-300'}`}>
                        {contradict && <span className="text-danger-400">⚠</span>}
                        <SourceDot kind="model" />
                        合理比分（开盘结构诚实锚）
                      </span>
                      <span className="text-[10px] text-ink-muted">30px · 仿 19:1x 原始版本</span>
                    </div>
                    {/* 矛盾警告置顶 (一眼看到) — 仅在方向真相反时 */}
                    {contradict && (
                      <div className="mb-2 text-[11px] text-danger-300 bg-danger-500/10 border border-danger-500/30 rounded-md px-2.5 py-1.5 leading-snug">
                        ⚠ 当前滚球 <b>{cur}</b> 的领先方与模型主推 <b>{sh.score}</b> <b>方向相反</b>——
                        模型跟随当前市场定价，未把领先方当既定结果（IR-25: 领先后收缩假设已两次证伪）。此处为<b>分歧提示</b>，非赛果预测。
                      </div>
                    )}
                    {/* 三级判定 (2026-08-30): 定方向/软加权/观望 */}
                    {sh.score_analysis && (() => {
                      const sa = sh.score_analysis
                      const lv = sa['级别']
                      const dir = sa['方向']
                      const conf = sa['置信度']
                      const note = sa['分歧标注']
                      const dirCN = dir === 'home' ? '主胜' : dir === 'away' ? '客胜' : dir === 'draw' ? '平局' : null
                      const lvStyle = lv === '定方向'
                        ? 'text-emerald-300 border-emerald-500/40 bg-emerald-500/10'
                        : lv === '软加权'
                          ? 'text-amber-300 border-amber-500/40 bg-amber-500/10'
                          : 'text-ink-muted border-surface-border/40 bg-surface-dark/40'
                      return (
                        <div className="mb-2 text-[11px] flex items-center gap-2 flex-wrap">
                          <span className={`px-1.5 py-0.5 rounded font-semibold border ${lvStyle}`}>{lv}</span>
                          {dirCN && <span>方向 <b className="text-ink-primary">{dirCN}</b></span>}
                          <span className="text-ink-muted">置信度 {Math.round((conf ?? 0) * 100)}%</span>
                          {note && <span className="text-amber-300">{note}</span>}
                        </div>
                      )
                    })()}
                    <div className="flex items-baseline gap-3">
                      <span className="text-[30px] leading-none font-bold text-ember-400 font-mono">{sh.score}</span>
                      {sh.winner_label && <span className="text-xs text-ink-secondary">{sh.winner_label}</span>}
                      {sh.score_opening && sh.score_opening !== sh.score && (
                        <span className="text-[13px] font-mono text-ink-muted ml-2">
                          (初盘/即时: {sh.score_opening})
                        </span>
                      )}
                    </div>
                    {sh.basis && <div className="text-[10px] text-ink-muted mt-1.5">{sh.basis}</div>}
                    {/* 双比分推荐: ①初盘+即时结构 ②滚球修正 (2026-08-28) */}
                    {(sh.score_opening || sh.opening_basis) && (
                      <div className="mt-1.5 grid grid-cols-2 gap-1.5">
                        <div className={`rounded-lg border px-2.5 py-1.5 ${
                          openingConflict
                            ? 'border-danger-500/40 bg-danger-500/[0.06]'
                            : 'border-frost-500/20 bg-frost-500/[0.04]'
                        }`}>
                          <div className="text-[9px] text-frost-300 mb-0.5">① 初盘+即时结构</div>
                          <div className="text-[13px] font-mono font-bold text-ink-primary">{sh.score_opening ?? '—'}</div>
                          {sh.opening_basis && <div className="text-[9px] text-ink-muted">{sh.opening_basis}</div>}
                          {/* 初盘结论与实时比分反向 → 明确标注, 不再裸显示造成误导 */}
                          {openingConflict && (
                            <div className="text-[9px] text-danger-300 mt-0.5">⚠ 与当前比分反向（初盘结论，非推荐）</div>
                          )}
                        </div>
                        <div className="rounded-lg border border-ember-500/20 bg-ember-500/[0.04] px-2.5 py-1.5">
                          <div className="text-[9px] text-ember-300 mb-0.5">
                            ② 滚球修正 ({sh.top1_equals_current ? 'top1 = 当前比分' : '主推'})
                          </div>
                          <div className="text-[13px] font-mono font-bold text-ember-400">{sh.score}</div>
                          {sh.top1_equals_current && (
                            <div className="text-[9px] text-ink-muted mt-0.5">
                              模型最可能维持该比分（不预测改变），非终场预测
                            </div>
                          )}
                          {sh.roll_verification && <div className="text-[9px] text-ink-muted">{sh.roll_verification}</div>}
                        </div>
                      </div>
                    )}
                    {/* 永久免责锚 (诚实边界): 开盘结构 ≠ 赛果预测 */}
                    <div className="text-[10px] text-ink-muted/70 mt-2 border-t border-white/[0.05] pt-1.5">
                      开局结构诚实锚 (开盘盘口去水 + OU 线定总球, 禁 CS 定价 · 08-23 决策), 分布概率而非命中预测
                    </div>
                  </div>
                )
              })()}

              {loading && !probe && (
                <div className="py-8">
                  <Skeleton variant="card" />
                </div>
              )}

              {probe && isModelQualified('probe_core_ou') && (
                <>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <SideCard title="半场破蛋" side={probe.half} currentTotal={selected?.score ? selected.score.split('-').reduce((a: number, x: string) => a + (parseInt(x, 10) || 0), 0) : null} />
                    <SideCard title="全场破蛋" side={probe.full} currentTotal={selected?.score ? selected.score.split('-').reduce((a: number, x: string) => a + (parseInt(x, 10) || 0), 0) : null} />
                  </div>

                  {/* FulltimeOutcomeCard 已删除 (用户指令 2026-08-31) */}

                  <LineDropCard ld={probe.line_drop} />

                  {/* 4 个模型结果 (赛程列表=全功能) */}
                  {momentum && (() => {
                    const ouVal = (momentum.part1_market_validation as any)?.ou_validation
                    const ouAvail = ouVal && ouVal.available
                    const phase = (momentum.part2_phase as any)?.label
                    const oneLine = (momentum.part5_execution as any)?.one_line_decision
                    const hasContent = oneLine || ouAvail || phase
                    if (!hasContent) {
                      return (
                        <div className="rounded-xl border border-white/[0.08] bg-white/[0.03] p-4">
                          <div className="text-[12px] font-semibold text-ink-muted mb-1">动态滚球决策 (Momentum)</div>
                          <div className="text-[11px] text-ink-muted">本场缺 OU 盘口数据或实时比分, 无法运行五段裁决</div>
                        </div>
                      )
                    }
                    return (
                    <div className="rounded-xl border border-pitch-500/30 bg-pitch-500/[0.05] p-4">
                      <div className="text-[12px] font-semibold text-pitch-300 mb-2">动态滚球决策 (Momentum · 五段统一裁决)</div>
                      {/* 一行决策 */}
                      <div className="text-[12px] text-ink-primary leading-snug">
                        {(momentum.part5_execution as any)?.one_line_decision ?? '—'}
                      </div>
                      {/* 阶段 + OU 市场校验 */}
                      <div className="grid grid-cols-2 md:grid-cols-3 gap-2 text-[10px] mt-2">
                        <div><span className="text-ink-muted">阶段</span> <span className="font-mono text-ink-primary">{(momentum.part2_phase as any)?.label ?? '—'}</span></div>
                        <div><span className="text-ink-muted">OU 抽水</span> <span className="font-mono text-ink-primary">{(momentum.part1_market_validation as any)?.ou_validation?.margin_ok ? '正常' : '异常'}</span></div>
                        <div><span className="text-ink-muted">隐含大球</span> <span className="font-mono text-ink-primary">
                          {(() => {
                            const ov = (momentum.part1_market_validation as any)?.ou_validation?.implied_over
                            return ov != null ? `${(ov * 100).toFixed(1)}%` : '—'
                          })()}
                        </span></div>
                      </div>
                      {/* 动态价值 sides (大球/小球) */}
                      {(() => {
                        const sides = (momentum.part4_dynamic_value as any)?.sides
                        if (!Array.isArray(sides) || sides.length === 0) return null
                        return (
                          <div className="grid grid-cols-2 gap-2 mt-2">
                            {sides.map((s: any) => (
                              <div key={s.side} className={`rounded-lg border p-2 ${s.side === 'over' ? 'border-emerald-500/30 bg-emerald-500/[0.06]' : 'border-rose-500/30 bg-rose-500/[0.06]'}`}>
                                <div className="text-[10px] text-ink-muted">{s.label} · 赔率 {s.odds}</div>
                                <div className="text-[11px] font-mono text-ink-primary">
                                  模型P {(s.model_p != null ? (s.model_p * 100).toFixed(1) : '—')}%
                                  {s.live_ev != null && <span className="text-ink-muted"> · EV {s.live_ev > 0 ? '+' : ''}{(s.live_ev * 100).toFixed(1)}%</span>}
                                </div>
                                {s.live_ev_lean && <div className="text-[9px] text-amber-300/80 mt-0.5">{s.live_ev_lean}</div>}
                              </div>
                            ))}
                          </div>
                        )
                      })()}
                      {/* 合理比分 ranked */}
                      {(() => {
                        const csr = (momentum.part5_execution as any)?.correct_scores_ranked
                        if (Array.isArray(csr) && csr.length > 0) {
                          return (
                            <div className="text-[10px] text-ink-muted mt-2">
                              合理比分: <span className="font-mono text-ink-primary">{csr.slice(0, 5).map((c: any) => (typeof c === 'string' ? c : c.score ?? JSON.stringify(c))).join(' / ')}</span>
                            </div>
                          )
                        }
                        return null
                      })()}
                      {(momentum.disclaimer as string) && <div className="text-[9px] text-ink-muted/60 mt-2">{momentum.disclaimer}</div>}
                    </div>
                    )
                  })}

                  {consensus && (
                    <div className="rounded-xl border border-frost-500/30 bg-frost-500/[0.05] p-4">
                      <div className="text-[12px] font-semibold text-frost-300 mb-2">信号仲裁 (Consensus · 治"5+ 路信号平铺")</div>
                      {(() => {
                        const sc = consensus.signal_consensus as any
                        if (!sc || sc.available === false) return <div className="text-[10px] text-ink-muted">{sc?.agreement ?? '无信号（数据不足）'}</div>
                        return (
                          <>
                            <div className="flex items-center gap-2">
                              <span className="text-[11px] text-ink-primary">{sc.agreement ?? '—'}</span>
                              {sc.primary_signal && (
                                <span className="text-[10px] px-2 py-0.5 rounded border border-frost-500/40 bg-frost-500/15 text-frost-300">
                                  {sc.primary_signal.label ?? sc.primary_signal.value ?? '—'}
                                </span>
                              )}
                            </div>
                            {sc.n_signals != null && <div className="text-[10px] text-ink-muted mt-1">信号数: {sc.n_signals}</div>}
                            {Array.isArray(sc.signals) && sc.signals.length > 0 && (
                              <div className="mt-1.5 space-y-0.5">
                                {sc.signals.slice(0, 5).map((sg: any, i: number) => (
                                  <div key={i} className="text-[10px] text-ink-secondary">• {sg.source ?? sg.label ?? JSON.stringify(sg).slice(0, 60)}</div>
                                ))}
                              </div>
                            )}
                          </>
                        )
                      })()}
                    </div>
                  )}

                  {/* CS 信任卡 (2026-08-28 重建 8/25 三栏形态: 结构/庄家/历史 + DB同结构匹配 + 滚球即时盘) */}
                  {isModelQualified('cs_trust') && <CSTrustCard trustCard={trustCard} induce={induce} />}

                  {/* 4 盘口综合诚实分析卡 (2026-08-31): 胜平负/大小球/让球/波胆 候选信号 */}
                  {isModelQualified('best_combo') && <BestComboCard result={bestCombo} />}
                  {isModelQualified('open_eye') && <OpenEyeCard result={openEye} />}

                  <div className="rounded-xl border border-surface-border/40 bg-surface-dark/30 p-4">
                    <div className="text-[12px] font-semibold text-ink-primary mb-2">判读理由</div>
                    <ul className="space-y-1.5">
                      {probe.reasons.map((r, i) => (
                        <li key={i} className="text-[12px] text-ink-secondary flex items-start gap-2">
                          <span className="text-field-400 mt-0.5">•</span>
                          {r}
                        </li>
                      ))}
                    </ul>
                  </div>

                  {probe.warning && (
                    <div className="text-[11px] text-amber-400/80 bg-amber-500/[0.05] border border-amber-500/20 rounded-lg px-3 py-2">
                      {probe.warning}
                    </div>
                  )}
                </>
              )}
            </>
          )}
        </div>

        {backtest && (
          <details className="mt-4 rounded-xl border border-surface-border/40 bg-surface-dark/30 p-4 group">
            <summary className="cursor-pointer text-[12px] font-semibold text-ink-primary select-none">
              回测结论与风险披露 (历史 {backtest.n_matches_with_odds} 场 · 开赛快照)
            </summary>
            <div className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-4 text-[12px]">
              <div className="rounded-lg border border-surface-border/30 bg-surface-dark/40 p-3">
                <div className="font-semibold text-ink-primary mb-1">全场破蛋 (OU)</div>
                <div className="text-ink-muted">可下注样本: <span className="text-ink-secondary">{backtest.full.n_bettable}</span></div>
                <div className="text-ink-muted">方向命中率: <span className="text-ink-secondary">{(backtest.full.direction_accuracy*100).toFixed(1)}%</span></div>
                <div className="text-ink-muted">盲跟低水 ROI: <span className={backtest.full.roi>=0?'text-emerald-300':'text-rose-300'}>{(backtest.full.roi*100).toFixed(1)}%</span> (盈亏平衡需 &gt; −11%)</div>
                <div className="text-ink-muted mt-1">结论: ≈跟随市场低水方, 仅回收部分抽水, <b className="text-amber-300">非稳定 +EV</b>。</div>
              </div>
              <div className="rounded-lg border border-surface-border/30 bg-surface-dark/40 p-3">
                <div className="font-semibold text-ink-primary mb-1">半场破蛋 (OU_1H)</div>
                <div className="text-ink-muted">可下注样本: <span className="text-ink-secondary">{backtest.half.n_bettable}</span></div>
                <div className="text-ink-muted">方向命中率: <span className="text-ink-secondary">{(backtest.half.direction_accuracy*100).toFixed(1)}%</span></div>
                <div className="text-ink-muted">盲跟低水 ROI: <span className={backtest.half.roi>=0?'text-emerald-300':'text-rose-300'}>{(backtest.half.roi*100).toFixed(1)}%</span></div>
                <div className="text-ink-muted mt-1">结论: <b className="text-rose-300">样本不足 / 置信低</b>, 勿据此下注。</div>
              </div>
            </div>
            <div className="mt-3 text-[11px] text-ink-muted leading-relaxed">
              方法: 取每场最早盘口快照(≈开赛, minute_at 最小) 喂入与线上完全一致的模型, 假定 0-0 / 0 分钟, 不含赔率动量项。
              绿色渲染 = 模型高概率方向(跟随盘口低水方, 即市场倾向), 并非"必胜"；且 45-60 秒轮询下无法捕捉 3 秒进球窗口。
              要获得真·秒级信号, 需将采集器升级到 3-5 秒轮询。
            </div>
          </details>
        )}
      </div>
    </div>
  )
}

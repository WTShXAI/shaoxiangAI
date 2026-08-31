// ═══ 赛程列表页展示组件 (2026-08-31 自 index.tsx 拆分, 纯展示无副作用) ═══
// 模式: 展示区块"收整卡 prop + 内部条件自守卫 return null"(与 MatchAnalysisModal/sections.tsx 同款)
import type { LiveMatch, LineDropData, ProbeSide, SourceKind } from './types'
import { MAX_MIN, SOURCE_COLORS, SOURCE_LABEL } from './types'
import { formatKickoffShort, formatMatchTime, resolveDisplayMinute } from './utils'

function SignalBadge({ signal, direction }: { signal: string; direction: string | null }) {
  const color =
    signal === 'STRONG_BREAK' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40' :
    signal === 'STRONG_HOLD' ? 'bg-rose-500/20 text-rose-300 border-rose-500/40' :
    signal === 'WEAK_TREND' ? 'bg-amber-500/20 text-amber-300 border-amber-500/40' :
    signal === 'ALREADY_BROKEN' ? 'bg-sky-500/20 text-sky-300 border-sky-500/40' :
    'bg-surface-border/40 text-ink-muted border-surface-border/30'
  const label =
    signal === 'STRONG_BREAK' ? '强烈看大' :
    signal === 'STRONG_HOLD' ? '强烈看小' :
    signal === 'WEAK_TREND' ? '轻微倾向' :
    signal === 'ALREADY_BROKEN' ? '已达成' :
    '无明确信号'
  return (
    <span className={`px-2.5 py-1 rounded-md text-[11px] font-semibold border ${color}`}>
      {label}{direction ? ` · ${direction === 'OVER' ? '大' : '小'}` : ''}
    </span>
  )
}

function ProbBar({ prob, direction }: { prob: number; direction: string | null }) {
  const pct = Math.round(prob * 100)
  return (
    <div className="mt-2">
      <div className="flex justify-between text-[11px] mb-1">
        <span className="text-ink-muted">不破蛋</span>
        <span className="text-ink-primary font-mono font-bold">{pct}%</span>
        <span className="text-ink-muted">破蛋</span>
      </div>
      <div className="h-2.5 bg-surface-border/40 rounded-full overflow-hidden flex">
        <div
          className="h-full bg-rose-400/70 transition-all"
          style={{ width: `${100 - pct}%` }}
        />
        <div
          className={`h-full transition-all ${direction === 'OVER' ? 'bg-emerald-400' : direction === 'UNDER' ? 'bg-rose-400' : 'bg-amber-400'}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

function MiniBadge({ side, signal, direction }: { side: string; signal?: string; direction?: string | null }) {
  if (!signal) return null
  const color =
    signal === 'STRONG_BREAK' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40' :
    signal === 'STRONG_HOLD' ? 'bg-rose-500/20 text-rose-300 border-rose-500/40' :
    signal === 'WEAK_TREND' ? 'bg-amber-500/20 text-amber-300 border-amber-500/40' :
    signal === 'ALREADY_BROKEN' ? 'bg-sky-500/20 text-sky-300 border-sky-500/40' :
    'bg-surface-border/40 text-ink-muted border-surface-border/30'
  const labelMap: Record<string, string> = {
    STRONG_BREAK: '看大',
    STRONG_HOLD: '看小',
    WEAK_TREND: '倾向',
    ALREADY_BROKEN: '已破',
    NO_EDGE: '观望',
  }
  const label = labelMap[signal] ?? signal
  return (
    <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold border ${color}`}>
      {side}·{label}{direction ? (direction === 'OVER' ? '大' : '小') : ''}
    </span>
  )
}

function MatchListItem({ m, selected, onClick, now, fetchTime }: { m: LiveMatch; selected: boolean; onClick: () => void; now: number; fetchTime: number }) {
  // 以 feed 的 minute 为真实比赛时间锚点，本地每秒平滑递增；kickoff 仅在 minute 缺失时兜底。
  // 低级联赛/延迟开球/比赛中断时，kickoff 挂钟时间会比有效比赛时间快，minute 更接近乐鱼显示。
  // 修正(2026-08-15): GQ feed 对 obscure 联赛的 minute 常在中场 45' 后卡死不再更新，
  // 导致所有比赛 stuck 在「上半场 45+'」。当 minute≈45 但 kickoff 显示已开赛超过 60 分钟时，
  // 改用 kickoff 估算真实时间，至少能正确区分上半场/下半场/已完场。
  // 统一用 resolveDisplayMinute: feed 分钟为锚点 + 本地增量, 仅新鲜数据且卡45时回推 kickoff,
  // 结果封顶 125 分钟, 已完场僵尸绝不显示 270+。
  const displayMin = resolveDisplayMinute(m, now, fetchTime)
  const isFinished = displayMin != null && displayMin >= MAX_MIN - 1
  const isGoalless = m.score === '0-0'
  return (
    <button
      onClick={onClick}
      className={`w-full text-left rounded-lg border px-3 py-2 transition-colors ${
        selected
          ? 'bg-field-500/15 border-field-500/40'
          : 'bg-surface-dark/40 border-surface-border/30 hover:bg-surface-border/30'
      } ${isFinished ? 'opacity-50' : ''}`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] text-ink-muted truncate">{m.league}</span>
        <div className="flex items-center gap-1.5 shrink-0">
          {isGoalless && !isFinished && (
            <span className="text-[9px] px-1 py-0.5 rounded bg-amber-500/20 text-amber-300 font-bold">0-0</span>
          )}
          {isFinished && (
            <span className="text-[9px] px-1 py-0.5 rounded bg-surface-border/40 text-ink-muted">完</span>
          )}
          <span className="text-[9px] font-mono text-ink-muted/70">{formatKickoffShort(m.kickoff)}</span>
          <span className="text-[10px] font-mono text-frost-400">{formatMatchTime(displayMin)}</span>
        </div>
      </div>
      <div className="text-[12px] text-ink-primary mt-0.5">
        {m.home} <span className="text-ink-muted">vs</span> {m.away}
      </div>
      <div className="flex items-center justify-between mt-1.5">
        <div className={`text-[13px] font-mono font-bold ${isGoalless ? 'text-amber-300' : 'text-ink-secondary'}`}>{m.score}</div>
        <div className="flex items-center gap-1">
          <MiniBadge side="半" signal={m.half_signal} direction={m.half_direction} />
          <MiniBadge side="全" signal={m.full_signal} direction={m.full_direction} />
        </div>
      </div>
    </button>
  )
}

function SourceDot({ kind }: { kind: SourceKind }) {
  return (
    <span
      className="inline-block w-2 h-2 rounded-full shrink-0 align-middle"
      style={{ backgroundColor: SOURCE_COLORS[kind] }}
      title={SOURCE_LABEL[kind]}
    />
  )
}

function SideCard({ title, side, currentTotal }: { title: string; side: ProbeSide; currentTotal?: number | null }) {
  // 2026-08-30: 当前总球已达 line 时,"小球"已无意义(盘口已死), 关闭绿色高亮 + 标注。
  // 例: 当前 0-3, line 1.5/2.5 → 总球 3 已超 → 终场只可能"大", 不再标"推荐方向"。
  const lineDead = currentTotal != null && side.line != null && currentTotal >= side.line
  const overActive = !lineDead && side.direction === 'OVER'
  const underActive = !lineDead && side.direction === 'UNDER'
  const isPrior = side.data_source === 'league_prior'  // 2026-08-28: 区分即时盘口 vs 联赛先验兜底
  return (
    <div className={`rounded-xl border p-4 ${isPrior ? 'border-amber-500/30 bg-amber-500/[0.04]' : 'border-surface-border/40 bg-surface-dark/30'}`}>
      <div className="flex items-center justify-between mb-2">
        <span className="text-[13px] font-semibold text-ink-primary flex items-center gap-1.5">
          <SourceDot kind="live" />{title}
        </span>
        <div className="flex items-center gap-1.5">
          {isPrior && (
            <span className="px-1.5 py-0.5 rounded text-[9px] font-mono border border-amber-500/40 bg-amber-500/15 text-amber-300">
              先验基线
            </span>
          )}
          <SignalBadge signal={side.signal} direction={side.direction} />
        </div>
      </div>
      <ProbBar prob={side.prob} direction={side.direction} />
      <div className="grid grid-cols-2 gap-3 mt-4">
        <div className={`rounded-lg border p-2.5 text-center transition-colors ${
          overActive ? 'border-emerald-500/50 bg-emerald-500/[0.08]' : 'border-surface-border/30 bg-surface-dark/40'
        }`}>
          <div className="text-[10px] text-ink-muted mb-0.5">大球 {side.line !== undefined ? `> ${side.line}` : ''}</div>
          <div className={`text-[15px] font-mono font-bold ${overActive ? 'text-emerald-300' : 'text-ink-secondary'}`}>
            {side.over_odds != null ? side.over_odds.toFixed(2) : '—'}
          </div>
          {overActive && <div className="text-[10px] text-emerald-400 mt-0.5">推荐方向</div>}
        </div>
        <div className={`rounded-lg border p-2.5 text-center transition-colors ${
          underActive ? 'border-emerald-500/50 bg-emerald-500/[0.08]' : 'border-surface-border/30 bg-surface-dark/40'
        }`}>
          <div className="text-[10px] text-ink-muted mb-0.5">小球 {side.line !== undefined ? `< ${side.line}` : ''}</div>
          <div className={`text-[15px] font-mono font-bold ${underActive ? 'text-emerald-300' : 'text-ink-secondary'}`}>
            {side.under_odds != null ? side.under_odds.toFixed(2) : '—'}
          </div>
          {underActive && <div className="text-[10px] text-emerald-400 mt-0.5">推荐方向</div>}
        </div>
      </div>
      {side.delta !== null && (
        <div className="mt-3 text-[11px] text-ink-muted">
          水位差 Δ={side.delta.toFixed(2)} · 目标总进球 ≥ {side.target_total}
        </div>
      )}
      {/* 2026-08-30: 当前总球已超盘口 → "小球"无意义(盘口已死), 关闭方向 + 诚实标注 */}
      {lineDead && side.line != null && currentTotal != null && (
        <div className="mt-2 text-[10px] text-ink-muted/80">
          ⚠ 当前总球 <b className="text-ink-secondary">{currentTotal}</b> 已达 line {side.line} → 盘口已死, 推荐方向关闭
        </div>
      )}
      {/* 2026-08-28: 诚实标注无即时盘口时的原因(WS 推流未到/联赛无 OU) */}
      {isPrior && side.no_odds_reason && (
        <div className="mt-2 text-[10px] text-amber-300/80">
          ⚠ {side.no_odds_reason}
        </div>
      )}
    </div>
  )
}

function FreshnessBadge({ maxLastSeen, now }: { maxLastSeen: number | null; now: number }) {
  if (!maxLastSeen) return null
  const ageSec = Math.max(0, Math.floor((now - maxLastSeen * 1000) / 1000))
  let color = 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40'
  let label = `数据更新于 ${ageSec} 秒前`
  if (ageSec >= 300) {
    color = 'bg-rose-500/15 text-rose-300 border-rose-500/40'
    // 滞后状态也带秒, 让 badge 每秒走字(否则只显示分钟, 1 分钟内看似卡住)
    label = `数据已滞后 ${Math.floor(ageSec / 60)} 分 ${ageSec % 60} 秒`
  } else if (ageSec >= 60) {
    color = 'bg-amber-500/15 text-amber-300 border-amber-500/40'
    label = `数据更新于 ${Math.floor(ageSec / 60)} 分 ${ageSec % 60} 秒前`
  }
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] font-semibold border ${color}`}>
      <span className="relative flex h-1.5 w-1.5">
        <span className={`absolute inline-flex h-full w-full rounded-full ${ageSec >= 300 ? 'bg-rose-400' : ageSec >= 60 ? 'bg-amber-400' : 'bg-emerald-400'} opacity-75 animate-ping`} />
        <span className={`relative inline-flex h-1.5 w-1.5 rounded-full ${ageSec >= 300 ? 'bg-rose-400' : ageSec >= 60 ? 'bg-amber-400' : 'bg-emerald-400'}`} />
      </span>
      {label}
    </span>
  )
}

function LineDropCard({ ld }: { ld: LineDropData | null | undefined }) {
  if (!ld) {
    return (
      <div className="rounded-xl border border-surface-border/40 bg-surface-dark/30 p-4">
        <div className="text-[12px] font-semibold text-ink-primary mb-1 flex items-center gap-1.5"><SourceDot kind="live" />降盘漂移观察</div>
        <div className="text-[11px] text-ink-muted">暂无盘口轨迹数据 (需历史 OU 快照)</div>
      </div>
    )
  }
  if (!ld.detected) {
    return (
      <div className="rounded-xl border border-surface-border/40 bg-surface-dark/30 p-4">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[12px] font-semibold text-ink-primary flex items-center gap-1.5"><SourceDot kind="live" />降盘漂移观察</span>
          <span className="px-2 py-0.5 rounded-md text-[10px] font-semibold border bg-surface-border/30 text-ink-muted border-surface-border/30">
            无降盘
          </span>
        </div>
        <div className="text-[11px] text-ink-muted">
          总球线 {ld.open_total ?? '—'} → {ld.current_total ?? '—'}
          {ld.drop !== undefined && (
            <span className={ld.drop >= 0 ? ' text-ink-secondary' : ' text-frost-400'}>
              {' '}({ld.drop >= 0 ? '升' : '降'}{Math.abs(ld.drop).toFixed(2)}球)
            </span>
          )}
        </div>
        <div className="mt-1 text-[10px] text-ink-muted/70">{ld.note}</div>
      </div>
    )
  }
  const hist = ld.window === 'late' ? ld.history?.late_window : ld.history?.early_window
  const roiPct = hist ? (hist.roi >= 0 ? '+' : '') + (hist.roi * 100).toFixed(0) + '%' : '—'
  return (
    <div className="rounded-xl border border-amber-500/40 bg-amber-500/[0.06] p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[12px] font-semibold text-amber-200 flex items-center gap-1.5"><SourceDot kind="live" />降盘漂移观察 ⚠ 非买入信号</span>
        <span className="px-2 py-0.5 rounded-md text-[10px] font-semibold border bg-amber-500/20 text-amber-200 border-amber-500/40">
          降 {ld.drop?.toFixed(2)} 球
        </span>
      </div>
      <div className="text-[12px] text-ink-secondary">
        总球线 <span className="font-mono">{ld.open_total}</span> →{' '}
        <span className="font-mono">{ld.current_total}</span>
        <span className="text-amber-300"> (降 {ld.drop?.toFixed(2)} 球)</span>
      </div>
      {hist && (
        <div className="mt-3 grid grid-cols-3 gap-2 text-center">
          <div className="rounded-lg bg-surface-dark/50 border border-surface-border/30 p-2">
            <div className="text-[10px] text-ink-muted">历史窗口</div>
            <div className="text-[11px] font-semibold text-ink-primary">{hist.label}</div>
          </div>
          <div className="rounded-lg bg-surface-dark/50 border border-surface-border/30 p-2">
            <div className="text-[10px] text-ink-muted">小命中率</div>
            <div className="text-[13px] font-mono font-bold text-amber-200">{(hist.acc * 100).toFixed(0)}%</div>
          </div>
          <div className="rounded-lg bg-surface-dark/50 border border-surface-border/30 p-2">
            <div className="text-[10px] text-ink-muted">历史 ROI</div>
            <div className={`text-[13px] font-mono font-bold ${hist.roi >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
              {roiPct}
            </div>
          </div>
        </div>
      )}
      <div className="mt-3 text-[10px] text-amber-300/80 leading-relaxed">
        {ld.verdict ?? '盘口已降盘, 历史上小方向略优但单庄无稳定 edge, 仅作方向参考。'}
      </div>
    </div>
  )
}

// 对外导出 (index.tsx 组装 + 页面渲染用)
export { SignalBadge, ProbBar, MiniBadge, MatchListItem, SourceDot, SideCard, FreshnessBadge, LineDropCard }

import { useState, useEffect, useCallback, useRef } from 'react'
import PageHeader from '@/components/layout/PageHeader'
import DuelManual from '@/components/DuelManual'
import CSTrustCard from '@/components/CSTrustCard'
import { liveGoalProbeService } from '@/services/api'

interface LiveMatch {
  match_key: string
  home: string
  away: string
  league: string
  score: string
  minute: number
  kickoff: string
  last_seen?: number
  // 后端已按破蛋/进球潜力排序并附带信号
  half_signal?: string
  half_direction?: 'OVER' | 'UNDER' | null
  half_prob?: number
  full_signal?: string
  full_direction?: 'OVER' | 'UNDER' | null
  full_prob?: number
  // 实时 1X2 (模型对决用)
  odds_h?: number; odds_d?: number; odds_a?: number
  // 开盘 1X2 (模型对决静态段用)
  opening_h?: number; opening_d?: number; opening_a?: number
  // OU 大小球 (手动预测回填, 对齐 NFTB 入参)
  ou_line?: number; ou_over?: number; ou_under?: number
  ou_op_line?: number; ou_op_over?: number; ou_op_under?: number
  // CS 波胆矩阵 ([[score, odds], ...])
  cs_top?: [string, number][] | null
  // 乐鱼扩展赛事内容 (伤病/休整/情报, 来自 content_collector)
  meta?: {
    injuries: { side: string; player: string; pos?: string; reason?: string }[]
    injuries_count: number
    rest_days: number | null
    news: boolean
  } | null
}

interface ProbeSide {
  prob: number
  signal: string
  direction: 'OVER' | 'UNDER' | null
  line: number
  target_total: number
  over_odds: number | null
  under_odds: number | null
  delta: number | null
  // 2026-08-28: 区分即时盘口 vs 联赛先验兜底; league_prior 时显示琥珀徽标
  data_source?: 'live_odds' | 'league_prior'
  no_odds_reason?: string | null
}

interface LineDropWindow {
  label: string
  n: number
  hit: number
  acc: number
  roi: number
  avg_under: number
}

interface LineDropData {
  detected: boolean
  drop?: number
  open_total?: number
  current_total?: number
  window?: string | null
  note?: string
  verdict?: string
  history?: {
    early_window?: LineDropWindow
    late_window?: LineDropWindow
    baseline?: { acc: number; n: number }
  }
}

interface FulltimeOutcome {
  direction: string | null
  confidence: number | null
  expected_total: number | null
  expected_score: string | null
  expected_home_goals: number | null
  expected_away_goals: number | null
  ah_side: string | null
  reasons: string[]
  note: string
}

interface ProbeData {
  match_key: string
  current_score: string
  current_minute: number
  league: string
  fav_odds: number | null
  half: ProbeSide
  full: ProbeSide
  reasons: string[]
  warning: string
  line_drop?: LineDropData | null
  fulltime?: FulltimeOutcome | null
}

const POLL_INTERVAL = 5000 // 5 秒轮询(与当前采集频率匹配)

// ═══ 实时比赛计时(锚定 kickoff GMT+8, 与实时比分页同源, 误差<1分钟) ═══
// 之前锚定采集库 minute 字段(全量轮询 60s), 误差可逼近 1 分钟; 改为以开赛时间(kickoff)
// 为固定基准, 用本地时钟实时推算, 误差仅=网络/时钟抖动(秒级)。
function parseKickoffGMT8(kickoff: string | null | undefined): number | null {
  if (!kickoff) return null
  let s = kickoff.trim().replace(' ', 'T')
  // 无时区标记 → 视为 GMT+8 (乐鱼/雷速 feed 的 commence_time/kickoff 均为北京时间本地时)
  if (!/[+-]\d{2}:?\d{2}$/.test(s) && !s.endsWith('Z')) {
    s += '+08:00'
  }
  const t = new Date(s).getTime()
  return isNaN(t) ? null : t
}

// 以 kickoff(GMT+8) 为固定基准实时推算比赛已进行的分钟数; kickoff 缺失时回退 feed minute
function computeLiveMinute(
  kickoff: string | null | undefined,
  now: number,
  fallbackMinute?: number | null,
): number | null {
  const ko = parseKickoffGMT8(kickoff)
  if (ko == null) return fallbackMinute ?? null
  const elapsedMin = (now - ko) / 60000
  if (elapsedMin < -5) return 0 // 尚未开赛
  return elapsedMin
}

// 足球真实最大比赛时间(90min + 中场15 + 补时), 封顶 125。任何估算不得超过此值,
// 否则已完场却卡在 live 的僵尸(开赛数小时)会被 kickoff 回推算出 270+ 分钟。
const MAX_MIN = 125

// 统一的比赛分钟解析: feed 分钟为锚点 + 本地增量, 仅当"数据新鲜 + feed 卡45 + kickoff 落
// 在(45,125]"时才用 kickoff 估算顶替(解决 obscure 联赛下半场卡 45)。僵尸(last_seen 陈旧)
// 一律不回推, 且结果封顶 125。refTime = 最近一次拉取时间戳(ms)。
function resolveDisplayMinute(m: LiveMatch, now: number, refTime: number): number | null {
  const feedMin = m.minute != null ? m.minute : null
  const kickoffMin = computeLiveMinute(m.kickoff, now, null)
  const fresh = (m.last_seen ? now - m.last_seen * 1000 : 1e12) < 30 * 60 * 1000
  if (feedMin != null) {
    let v = feedMin + (refTime > 0 ? (now - refTime) / 60000 : 0)
    if (Math.abs(feedMin - 45) < 1 && fresh && kickoffMin != null && kickoffMin > 50 && kickoffMin <= MAX_MIN) {
      v = kickoffMin
    }
    return Math.min(MAX_MIN, Math.max(0, v))
  }
  return kickoffMin != null ? Math.min(MAX_MIN, Math.max(0, kickoffMin)) : null
}

function formatMatchTime(min: number | null, isHalftime = false): string {
  if (isHalftime) return '中场休息'
  if (min == null) return ''
  if (min < 0.5) return '开球'
  if (min < 45) {
    const sec = Math.round((min % 1) * 60)
    return `上半场 ${String(Math.floor(min)).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
  }
  if (min < 60) return `上半场 ${Math.floor(min)}+'`
  if (min < 90) {
    const sec = Math.round((min % 1) * 60)
    return `下半场 ${String(Math.floor(min)).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
  }
  return `下半场 ${Math.floor(min)}+'`
}

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

function formatKickoffShort(kickoff: string | null | undefined): string {
  if (!kickoff) return '--:--'
  const s = kickoff.trim()
  const t = s.indexOf(' ')
  const hm = t > 0 ? s.slice(t + 1) : s
  return hm.length >= 5 ? hm.slice(0, 5) : hm
}

function MatchListItem({ m, selected, onClick, onDuel, now, fetchTime }: { m: LiveMatch; selected: boolean; onClick: () => void; onDuel: (m: LiveMatch) => void; now: number; fetchTime: number }) {
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
          <button
            onClick={(e) => { e.stopPropagation(); onDuel(m) }}
            className="px-1.5 py-0.5 rounded text-[9px] font-semibold border border-field-500/40 bg-field-500/15 text-field-300 hover:bg-field-500/30 transition-colors"
            title="跳转手动预测并自动回填该场赔率/比分/时间"
          >
            ▶ 预测
          </button>
          <MiniBadge side="半" signal={m.half_signal} direction={m.half_direction} />
          <MiniBadge side="全" signal={m.full_signal} direction={m.full_direction} />
        </div>
      </div>
    </button>
  )
}

function SideCard({ title, side }: { title: string; side: ProbeSide }) {
  const overActive = side.direction === 'OVER'
  const underActive = side.direction === 'UNDER'
  const isPrior = side.data_source === 'league_prior'  // 2026-08-28: 区分即时盘口 vs 联赛先验兜底
  return (
    <div className={`rounded-xl border p-4 ${isPrior ? 'border-amber-500/30 bg-amber-500/[0.04]' : 'border-surface-border/40 bg-surface-dark/30'}`}>
      <div className="flex items-center justify-between mb-2">
        <span className="text-[13px] font-semibold text-ink-primary">{title}</span>
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
      {/* 2026-08-28: 诚实标注无即时盘口时的原因(WS 推流未到/联赛无 OU) */}
      {isPrior && side.no_odds_reason && (
        <div className="mt-2 text-[10px] text-amber-300/80">
          ⚠ {side.no_odds_reason}
        </div>
      )}
    </div>
  )
}

function FulltimeOutcomeCard({ ft, fallbackScore }: { ft: FulltimeOutcome | null | undefined; fallbackScore?: string | null }) {
  if (!ft) return null
  const dirColor =
    ft.direction === '主胜' ? 'text-emerald-300' :
    ft.direction === '客胜' ? 'text-sky-300' :
    ft.direction === '平' ? 'text-amber-300' :
    'text-ink-primary'
  const confPct = ft.confidence != null ? Math.round(ft.confidence * 100) : null
  // 比分回退链: 庄家OU隐含比分(跟随盘口) → DB同结构匹配/结构模型 top1 → 开盘诚实锚。
  // OU 盘口缺失的场也能显示一个比分(来源标注诚实区分, 不伪造盘口推导)。
  const shownScore = ft.expected_score || fallbackScore || null
  const scoreLabel = ft.expected_score ? '庄家预期比分' : '结构预期比分'
  return (
    <div className="rounded-xl border border-field-500/40 bg-field-500/[0.06] p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[13px] font-semibold text-ink-primary">
          终场结果读数 <span className="text-[10px] text-ink-muted font-normal">(跟随盘口)</span>
        </span>
        {confPct != null && (
          <span className="px-2 py-0.5 rounded-md text-[10px] font-semibold border border-field-500/40 bg-field-500/15 text-field-300">
            置信 {confPct}%
          </span>
        )}
      </div>
      <div className="flex items-end gap-5">
        <div>
          <div className="text-[10px] text-ink-muted mb-0.5">最可能终场</div>
          <div className={`text-2xl font-bold font-mono ${dirColor}`}>{ft.direction ?? '—'}</div>
        </div>
        {shownScore && (
          <div>
            <div className="text-[10px] text-ink-muted mb-0.5">{scoreLabel}</div>
            <div className="text-2xl font-bold font-mono text-ink-primary">{shownScore}</div>
          </div>
        )}
        {ft.expected_total != null && (
          <div>
            <div className="text-[10px] text-ink-muted mb-0.5">隐含总球</div>
            <div className="text-lg font-mono text-ink-secondary">{ft.expected_total}</div>
          </div>
        )}
      </div>
      {confPct != null && (
        <div className="mt-3 h-2 bg-surface-border/40 rounded-full overflow-hidden">
          <div className="h-full bg-field-400" style={{ width: `${confPct}%` }} />
        </div>
      )}
      {ft.reasons && ft.reasons.length > 0 && (
        <ul className="mt-3 space-y-1.5">
          {ft.reasons.map((r, i) => (
            <li key={i} className="text-[12px] text-ink-secondary flex items-start gap-2">
              <span className="text-field-400 mt-0.5">•</span>{r}
            </li>
          ))}
        </ul>
      )}
      {ft.note && (
        <div className="mt-3 text-[11px] text-amber-400/80 bg-amber-500/[0.05] border border-amber-500/20 rounded-lg px-3 py-2">
          {ft.note}
        </div>
      )}
    </div>
  )
}

function formatAgeMs(ageMs: number): string {
  const ageSec = Math.max(0, Math.floor(ageMs / 1000))
  if (ageSec < 60) return `${ageSec} 秒前`
  const min = Math.floor(ageSec / 60)
  const sec = ageSec % 60
  if (min < 60) return `${min} 分 ${sec} 秒前`
  const h = Math.floor(min / 60)
  const m = min % 60
  return `${h} 小时 ${m} 分前`
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
        <div className="text-[12px] font-semibold text-ink-primary mb-1">降盘漂移观察</div>
        <div className="text-[11px] text-ink-muted">暂无盘口轨迹数据 (需历史 OU 快照)</div>
      </div>
    )
  }
  if (!ld.detected) {
    return (
      <div className="rounded-xl border border-surface-border/40 bg-surface-dark/30 p-4">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[12px] font-semibold text-ink-primary">降盘漂移观察</span>
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
        <span className="text-[12px] font-semibold text-amber-200">降盘漂移观察 ⚠ 非买入信号</span>
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

export default function SchedulePage() {
  const [matches, setMatches] = useState<LiveMatch[]>([])
  const [selected, setSelected] = useState<LiveMatch | null>(null)
  const [probe, setProbe] = useState<ProbeData | null>(null)
  const [anal, setAnal] = useState<any>(null)
  const [momentum, setMomentum] = useState<any>(null)
  const [consensus, setConsensus] = useState<any>(null)
  const [trustCard, setTrustCard] = useState<any>(null)
  const [induce, setInduce] = useState<any>(null)
  const [duel, setDuel] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [lastUpdate, setLastUpdate] = useState<number | null>(null)
  const [maxLastSeen, setMaxLastSeen] = useState<number | null>(null)
  const [backtest, setBacktest] = useState<any>(null)
  const [mode, setMode] = useState<'schedule' | 'duel'>('schedule')
  const [duelInitial, setDuelInitial] = useState<any>(null)

  // 点击"▶ 预测": 切到手动预测 tab, 自动回填该场 即时盘/初盘/比分/分钟/OU/CS/队名/联赛
  const goDuel = useCallback((m: LiveMatch) => {
    setDuelInitial({
      home: m.odds_h, draw: m.odds_d, away: m.odds_a,
      openHome: m.opening_h, openDraw: m.opening_d, openAway: m.opening_a,
      score: m.score, minute: m.minute ?? undefined,
      homeName: m.home, awayName: m.away,
      league: m.league,
      ouLine: m.ou_line, ouOver: m.ou_over, ouUnder: m.ou_under,
      ouOpLine: m.ou_op_line, ouOpOver: m.ou_op_over, ouOpUnder: m.ou_op_under,
      csTop: m.cs_top ?? undefined,
    })
    setMode('duel')
  }, [])
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

  const fetchProbe = useCallback(async (m: LiveMatch) => {
    setLoading(true)
    try {
      // 比赛分钟封顶 125: 仅当数据新鲜且 feed 卡45时回推 kickoff(避免僵尸算出 270+),
      // 其余按 feed 分钟, 且结果硬性封顶。
      const kickoffMinProbe = computeLiveMinute(m.kickoff, Date.now(), null)
      let liveMin = m.minute ?? 0
      if (m.minute != null && Math.abs(m.minute - 45) < 1 && kickoffMinProbe != null && kickoffMinProbe > 50 && kickoffMinProbe <= 125) {
        liveMin = kickoffMinProbe
      } else if (m.minute == null && kickoffMinProbe != null) {
        liveMin = Math.min(MAX_MIN, Math.max(0, kickoffMinProbe))
      }
      liveMin = Math.min(MAX_MIN, Math.max(0, liveMin))
      const min = Math.round(liveMin)
      // 并行拉取 6 个模型 (赛程列表=全功能): 基础分析/合理比分/动态决策/信号仲裁/CS 三栏/庄家诱导
      const [probeRes, analRes, momRes, conRes, trustRes, indRes] = await Promise.all([
        liveGoalProbeService.getProbe(m.match_key, m.score, min).then((r) => r.data).catch(() => null),
        liveGoalProbeService.getAnalyze(m.match_key, m.score, min, false).then((r) => (r as any)?.data?.data ?? (r as any)?.data).catch(() => null),
        liveGoalProbeService.getMomentum(m.match_key, m.score, min, false, m.home, m.away, m.league, m.ou_over, m.ou_under, m.ou_line ?? 2.5).then((r) => (r as any)?.data ?? r).catch(() => null),
        liveGoalProbeService.getConsensus(m.match_key, m.score, min, false, m.home, m.away, m.league, m.ou_over, m.ou_under, m.ou_line ?? 2.5).then((r) => (r as any)?.data ?? r).catch(() => null),
        // 双层解包: 端点返回 {ok, data: card}, axios .data 一层 → .data.data 才是 card
        liveGoalProbeService.getCsTrustCard(m.match_key, m.score, min > 0 ? min : undefined).then((r) => (r as any)?.data?.data ?? (r as any)?.data).catch(() => null),
        // live 场不传 actual_score(当前比分非终场, 8/22 铁律), 仅终场回检用
        liveGoalProbeService.getInduceFlag(m.match_key).then((r) => (r as any)?.data?.data ?? (r as any)?.data).catch(() => null),
      ])
      // 第 7 个: 模型对决 (四方对比, 需要实时 1X2 + 开盘 1X2)
      let duelRes: any = null
      if (m.odds_h && m.odds_d && m.odds_a) {
        duelRes = await liveGoalProbeService.getDuelPredict({
          home: m.odds_h, draw: m.odds_d, away: m.odds_a,
          score: m.score, minute: min > 0 ? min : undefined,
          open_home: m.opening_h ?? undefined, open_draw: m.opening_d ?? undefined,
          open_away: m.opening_a ?? undefined,
        }).then((r) => (r as any)?.data?.data ?? (r as any)?.data).catch(() => null)
      }
      if (probeRes) {
        if (!probeRes.ok) throw new Error(probeRes.error || '探测失败')
        setProbe(probeRes.data)
      } else {
        setProbe(null)
      }
      setAnal(analRes || null)
      setMomentum(momRes || null)
      setConsensus(conRes || null)
      setTrustCard(trustRes?.found === false ? null : (trustRes || null))
      setInduce(indRes || null)
      setDuel(duelRes || null)
      setError('')
    } catch (e: any) {
      setError(e?.response?.data?.error || e?.message || '探测失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchMatches()
    timerRef.current = setInterval(() => {
      fetchMatches()
      if (selected) fetchProbe(selected)
    }, POLL_INTERVAL)
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [fetchMatches, fetchProbe, selected])

  useEffect(() => {
    if (matches.length && !selected) {
      // 后端已按破蛋/进球潜力排序, 默认选中优先级最高的比赛
      setSelected(matches[0])
    }
  }, [matches, selected])

  useEffect(() => {
    if (selected) fetchProbe(selected)
  }, [selected, fetchProbe])

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

  return (
    <div className="min-h-screen p-4 md:p-6">
      <PageHeader title="赛程列表" subtitle="全比赛 · 点比赛自动跑 7 个模型（破蛋 / 合理比分 / 动态决策 / 信号仲裁 / CS 信任卡 / 庄家诱导 / 模型对决）" />

      {/* 顶部菜单: 赛程列表 | 手动预测对比 */}
      <div className="mt-4 flex items-center gap-1 rounded-lg bg-surface-dark/50 border border-surface-border/40 p-1 w-fit">
        {(['schedule', 'duel'] as const).map((m) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            className={`px-3 py-1.5 rounded-md text-[12px] font-medium transition-colors ${
              mode === m ? 'bg-field-500/20 text-field-300 border border-field-500/30' : 'text-ink-muted hover:text-ink-primary'
            }`}
          >
            {m === 'schedule' ? '赛程列表' : '手动预测对比'}
          </button>
        ))}
      </div>

      {mode === 'duel' ? (
        <div className="mt-4 max-w-[1000px]">
          <DuelManual initial={duelInitial} />
        </div>
      ) : (
      <>
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
                onDuel={goDuel}
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
                {probe?.fav_odds && (
                  <div className="mt-3 text-[11px] text-ink-muted">
                    1X2 热门赔率: <span className="font-mono text-ink-secondary">{probe.fav_odds.toFixed(2)}</span>
                  </div>
                )}
              </div>

              {/* 30px 合理比分大卡 (治 CS 矛盾: 矛盾警告置顶, 比分置下, 永久免责锚) */}
              {(() => {
                const sh: any = anal?.score_hint
                const cur = selected?.score ?? ''
                const contradict = !!(sh?.score && cur && sh.score !== cur)
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
                        合理比分（开盘结构诚实锚）
                      </span>
                      <span className="text-[10px] text-ink-muted">30px · 仿 19:1x 原始版本</span>
                    </div>
                    {/* 矛盾警告置顶 (一眼看到) */}
                    {contradict && (
                      <div className="mb-2 text-[11px] text-danger-300 bg-danger-500/10 border border-danger-500/30 rounded-md px-2.5 py-1.5 leading-snug">
                        ⚠ 当前滚球已 <b>{cur}</b>，合理比分 <b>{sh.score}</b> 来自开盘盘口、未接 in-play 比分——请以滚球为准，勿当赛果预测。
                      </div>
                    )}
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
                        <div className="rounded-lg border border-frost-500/20 bg-frost-500/[0.04] px-2.5 py-1.5">
                          <div className="text-[9px] text-frost-300 mb-0.5">① 初盘+即时结构</div>
                          <div className="text-[13px] font-mono font-bold text-ink-primary">{sh.score_opening ?? '—'}</div>
                          {sh.opening_basis && <div className="text-[9px] text-ink-muted">{sh.opening_basis}</div>}
                        </div>
                        <div className="rounded-lg border border-ember-500/20 bg-ember-500/[0.04] px-2.5 py-1.5">
                          <div className="text-[9px] text-ember-300 mb-0.5">② 滚球修正 (主推)</div>
                          <div className="text-[13px] font-mono font-bold text-ember-400">{sh.score}</div>
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
                <div className="text-center text-ink-muted text-sm py-8">探测中...</div>
              )}

              {probe && (
                <>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <SideCard title="半场破蛋" side={probe.half} />
                    <SideCard title="全场破蛋" side={probe.full} />
                  </div>

                  <FulltimeOutcomeCard
                    ft={probe.fulltime}
                    fallbackScore={(trustCard?.db_match?.score || trustCard?.our_top5?.[0]?.score || anal?.score_hint?.score || '').replace(':', '-') || null}
                  />

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
                  <CSTrustCard trustCard={trustCard} induce={induce} />

                  {/* 模型对决 (2026-08-28 整合自 8000 harness → 9000) */}
                  {duel && (
                    <div className="rounded-xl border border-field-500/40 bg-field-500/[0.05] p-4">
                      <div className="flex items-center justify-between mb-2">
                        <span className="text-[12px] font-semibold text-field-300">模型对决 · 单场四方对比</span>
                        <span className="text-[10px] text-ink-muted">{duel.mode ?? ''}</span>
                      </div>
                      <div className="space-y-2">
                        {(
                          [
                            ['本系统', duel.system],
                            ['GitHub', duel.github],
                            ['去水基线', duel.baseline],
                            ['优化混合', duel.optimized],
                          ] as [string, any][]
                        ).map(([name, p]) => (
                          <div key={name} className="flex items-center gap-2">
                            <span className="text-[10px] text-ink-muted w-14 shrink-0">{name}</span>
                            <div className="flex-1 grid grid-cols-3 gap-1">
                              {['主', '平', '客'].map((lbl, i) => {
                                const v = p ? (i === 0 ? p.p_home : i === 1 ? p.p_draw : p.p_away) : null
                                const pct = v != null ? Math.round(v * 100) : 0
                                const isMax = p && p.argmax === (['H', 'D', 'A'][i])
                                return (
                                  <div key={lbl} className="relative h-5 rounded bg-white/[0.04] overflow-hidden">
                                    <div className={`absolute inset-y-0 left-0 ${isMax ? 'bg-field-400/60' : 'bg-white/[0.08]'}`} style={{ width: `${pct}%` }} />
                                    <span className="absolute inset-0 flex items-center justify-center text-[9px] font-mono">
                                      {lbl} {v != null ? `${pct}%` : '—'}
                                    </span>
                                  </div>
                                )
                              })}
                            </div>
                            <span className="text-[10px] text-ink-secondary w-10 text-right shrink-0">{p?.argmax_cn ?? '—'}</span>
                          </div>
                        ))}
                      </div>
                      <div className="text-[10px] text-ink-muted mt-2">
                        优化混合 = 0.6·滚球模型 + 0.4·去水基线（时序 holdout 锁定，滚球 AUC 0.8217 最优）· 历史指标见对决看板
                      </div>
                    </div>
                  )}

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
      </>
      )}
    </div>
  )
}

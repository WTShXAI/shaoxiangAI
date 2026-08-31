import type { FixtureEntry } from '@/types'
import { stateOf, countdown, fmtGMT8 } from './fixtureUtils'

// ═══ 状态徽章 ═══
export function StatusBadge({ fx, now }: { fx: FixtureEntry; now: number }) {
  const { live, finished, pending, halftime } = stateOf(fx, now)
  if (live) {
    return (
      <span className='inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold bg-field-500/15 text-field-400 border border-field-500/25'>
        <span className='w-1.5 h-1.5 rounded-full bg-field-500 animate-pulse' />进行中
      </span>
    )
  }
  if (halftime) {
    return (
      <span className='inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-500/12 text-amber-400 border border-amber-500/20'>
        中场休息
      </span>
    )
  }
  if (finished) {
    return (
      <span className='inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold bg-frost-500/12 text-frost-400 border border-frost-500/20'>
        已结束
      </span>
    )
  }
  if (pending) {
    // feed 标记待定(PA) 或异常状态(state>=6) → 状态待定
    return (
      <span className='inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold bg-surface-border/60 text-ink-muted'>
        待定
      </span>
    )
  }
  // 正常未开赛: 看距开赛时间
  const cd = countdown(fx.commence_time, now)
  if (cd) {
    return (
      <span className='inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold bg-surface-border/60 text-ink-secondary'>
        距开赛 {cd}
      </span>
    )
  }
  // 开赛时间已过且非 PA → feed 状态滞后, 实际已开赛
  return (
    <span className='inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold bg-ember-500/15 text-ember-400 border border-ember-500/25'>
      <span className='w-1.5 h-1.5 rounded-full bg-ember-500 animate-pulse' />
      已开赛·数据滞后
    </span>
  )
}

// ═══ 1X2 赔率 chips ═══
export function OddsPanel({ fx }: { fx: FixtureEntry }) {
  // ── 1X2 ──
  const h = fx.odds_h, d = fx.odds_d, a = fx.odds_a
  const oh = fx.opening_h, od_ = fx.opening_d, oa = fx.opening_a
  const has1x2 = typeof h === 'number' && typeof d === 'number' && typeof a === 'number'
  const has1x2Op = typeof oh === 'number' && typeof od_ === 'number' && typeof oa === 'number'
  // ── AH (让球) ──
  const ahL = fx.ah_line, ahH = fx.ah_home, ahA = fx.ah_away
  const ahOpH = fx.ah_op_home, ahOpA = fx.ah_op_away
  const hasAH = typeof ahH === 'number' && typeof ahA === 'number' && ahL != null
  const hasAHOp = typeof ahOpH === 'number' && typeof ahOpA === 'number'
  // ── OU (大小) ──
  const ouL = fx.ou_line, ouO = fx.ou_over, ouU = fx.ou_under
  const ouOpO = fx.ou_op_over, ouOpU = fx.ou_op_under
  const hasOU = typeof ouO === 'number' && typeof ouU === 'number' && ouL != null
  const hasOUOp = typeof ouOpO === 'number' && typeof ouOpU === 'number'

  // OU 方向置信度(2026-08-01 用所有OU数据校准): 市场一边倒 + 盘口线极端 → 高置信
  // 移植自 pipeline/evaluation/ou_eval.ou_confidence; 分桶验证: <0.3=46.6% / 0.5-0.7=77% / >=0.7=97.7%
  const ouConfidence = (line: number | string | null, over: number, under: number): number | null => {
    if (line == null || !isFinite(over) || !isFinite(under) || over <= 1.01 || under <= 1.01) return null
    const ln = typeof line === 'string' ? parseFloat(line) : line
    if (!isFinite(ln)) return null
    const pOver = (1 / over) / (1 / over + 1 / under)
    const pFav = Math.max(pOver, 1 - pOver)
    const confMarket = (pFav - 0.5) * 2
    const confLine = Math.min(Math.abs(ln - 2.75) / 2.0, 1.0)
    return Math.round((0.5 * confMarket + 0.5 * confLine) * 1000) / 1000
  }
  const ouConf = hasOU ? ouConfidence(ouL, ouO, ouU) : null

  if (!has1x2 && !has1x2Op && !hasAH && !hasAHOp && !hasOU && !hasOUOp) return null

  const C = (v: number | undefined | null) => typeof v === 'number' && !isNaN(v) ? String(Number(v.toFixed(2))) : '——'
  const G = (v: number | undefined | null, c: string) =>
    <span className={`font-mono ${c} font-bold tabular-nums`}>{C(v)}</span>

  const Lb = 'text-ink-muted/50 w-[26px] flex-shrink-0 text-right text-[10px]'
  const Mu = 'text-ink-muted/50'
  const Mm = 'text-ink-muted/60'

  return (
    <div className='flex flex-col gap-0.5'>
      {/* ═══ 1X2 初盘 ═══ */}
      {has1x2Op && <div className='flex items-center gap-1 text-[10px]'>
        <span className={Lb}>初</span>
        <span className='inline-flex gap-1 bg-surface-dark/40 rounded px-1.5 py-px'>
          <span className={Mu}>主</span><span className={`font-mono ${Mm} font-bold tabular-nums`}>{C(oh)}</span>
          <span className={Mu}>平</span><span className={`font-mono ${Mm} font-bold tabular-nums`}>{C(od_)}</span>
          <span className={Mu}>客</span><span className={`font-mono ${Mm} font-bold tabular-nums`}>{C(oa)}</span>
        </span>
      </div>}
      {/* ═══ 1X2 滚动 ═══ */}
      {has1x2 && <div className='flex items-center gap-1 text-[11px]'>
        <span className='text-ink-secondary w-[26px] flex-shrink-0 text-right text-[10px]'>{has1x2Op ? '滚' : '赔率'}</span>
        <span className='inline-flex gap-1 bg-surface-dark/50 rounded px-1.5 py-0.5'>
          <span className='text-ink-muted'>主</span>{G(h, 'text-pitch-400')}
          <span className='text-ink-muted'>平</span>{G(d, 'text-white/80')}
          <span className='text-ink-muted'>客</span>{G(a, 'text-ember-400')}
        </span>
      </div>}
      {/* ═══ AH (让球) 初盘 ═══ */}
      {hasAHOp && <div className='flex items-center gap-1 text-[10px]'>
        <span className={Lb}>初</span>
        <span className='inline-flex gap-1 bg-surface-dark/40 rounded px-1.5 py-px'>
          <span className={Mu}>让{ahL}</span>
          <span className={`font-mono ${Mm} font-bold tabular-nums`}>{C(ahOpH)}</span>
          <span className={`font-mono ${Mm} font-bold tabular-nums`}>{C(ahOpA)}</span>
        </span>
      </div>}
      {/* ═══ AH 滚动 ═══ */}
      {hasAH && <div className='flex items-center gap-1 text-[11px]'>
        <span className='text-ink-secondary w-[26px] flex-shrink-0 text-right text-[10px]'>{hasAHOp ? '滚' : '让球'}</span>
        <span className='inline-flex gap-1 bg-surface-dark/50 rounded px-1.5 py-0.5'>
          <span className='text-ink-muted'>让{ahL}</span>
          {G(ahH, 'text-pitch-400')}{G(ahA, 'text-ember-400')}
        </span>
      </div>}
      {/* ═══ OU (大小) 初盘 ═══ */}
      {hasOUOp && <div className='flex items-center gap-1 text-[10px]'>
        <span className={Lb}>初</span>
        <span className='inline-flex gap-1 bg-surface-dark/40 rounded px-1.5 py-px'>
          <span className={Mu}>大{ouL}</span>
          <span className={`font-mono ${Mm} font-bold tabular-nums`}>{C(ouOpO)}</span>
          <span className={`font-mono ${Mm} font-bold tabular-nums`}>{C(ouOpU)}</span>
        </span>
      </div>}
      {/* ═══ OU 滚动 ═══ */}
      {hasOU && <div className='flex items-center gap-1 text-[11px]'>
        <span className='text-ink-secondary w-[26px] flex-shrink-0 text-right text-[10px]'>{hasOUOp ? '滚' : '大小'}</span>
        <span className='inline-flex gap-1 bg-surface-dark/50 rounded px-1.5 py-0.5'>
          <span className='text-ink-muted'>大{ouL}</span>
          {G(ouO, 'text-pitch-400')}{G(ouU, 'text-ember-400')}
        </span>
        {ouConf != null && ouConf >= 0.5 && (
          <span className='text-[9px] px-1 py-px rounded bg-emerald-500/20 text-emerald-300 font-bold'>有把握</span>
        )}
        {ouConf != null && ouConf < 0.3 && (
          <span className='text-[9px] px-1 py-px rounded bg-ink-muted/10 text-ink-muted/60'>模糊</span>
        )}
      </div>}
    </div>
  )
}

// ═══ 单场卡片分析回调契约 (LiveScores 主页面 → MatchAnalysisModal) ═══
export type AnalyzeHandler = (
  home: string, away: string, sportKey?: string,
  odds?: { h: number; d: number; a: number },
  handicap?: { ah_line?: number | string; ah_home?: number; ah_away?: number; ou_line?: number | string; ou_over?: number; ou_under?: number },
  liveScore?: { homeGoals: number; awayGoals: number; elapsed?: number },
) => void

// ═══ 单场卡片 ═══
export function MatchCard({ fx, now, onAnalyze }: { fx: FixtureEntry; now: number; onAnalyze?: AnalyzeHandler }) {
  const { live, finished, pending, halftime, label } = stateOf(fx, now)
  // 比分必须双方都有有效数值才显示, 否则显示 vs (避免 0-0 误导)
  const hasScore = typeof fx.score_home === 'number' && typeof fx.score_away === 'number'
  const sh = hasScore ? (fx.score_home as number) : 0
  const sa = hasScore ? (fx.score_away as number) : 0
  const homeLead = live && hasScore && sh > sa
  const awayLead = live && hasScore && sa > sh
  // 只在 live/finished 且有真实比分时才显示比分; 中场休息若有比分也展示(半场比分)
  const showScore = (live || finished || halftime) && hasScore
  const koClock = fmtGMT8(fx.commence_time)
  const timeLabel = live ? label : halftime ? '中场休息' : finished ? '已结束' : pending ? '状态待定' : (koClock ? `${koClock} 开赛` : '时间待定')
  return (
    <div className={`rounded-lg border px-3 py-2 transition-colors duration-150 ${
      live
        ? 'border-field-500/20 bg-field-500/[0.04]'
        : halftime
          ? 'border-amber-500/15 bg-amber-500/[0.04]'
          : finished
            ? 'border-frost-500/10 bg-surface-dark/30'
            : 'border-surface-border/40 bg-transparent hover:bg-surface-dark/20'
    }`}>
      {/* 头部: 状态徽章 + 联赛 + 阶段/时间 (紧凑一行) */}
      <div className='flex items-center gap-2 flex-wrap mb-1'>
        <StatusBadge fx={fx} now={now} />
        <span className='text-xs text-ink-muted max-w-[140px] truncate' title={fx.league || ''}>{fx.league || '其他'}</span>
        <span className='text-[10px] text-ink-muted ml-auto font-mono'>{timeLabel}</span>
      </div>

      {/* 比分主体 / 对阵 (密度提升: 字号 22→18, 间距收紧) */}
      <div className='flex items-center gap-2'>
        <span className={`flex-1 text-right text-sm font-bold truncate ${homeLead ? 'text-field-300' : 'text-ink-primary'}`}>{fx.home}</span>
        {showScore ? (
          <span className='flex items-center gap-1 flex-shrink-0 min-w-[60px] justify-center'>
            <span className={`text-[18px] font-black font-mono tabular-nums ${homeLead ? 'text-field-300' : 'text-ink-primary'} ${live ? 'animate-pulse' : ''}`}>{sh}</span>
            <span className='text-ink-muted/40 text-xs'>-</span>
            <span className={`text-[18px] font-black font-mono tabular-nums ${awayLead ? 'text-field-300' : 'text-ink-primary'} ${live ? 'animate-pulse' : ''}`}>{sa}</span>
            {fx.score_inferred && <span className='ml-1 text-[8px] px-1 py-0.5 rounded bg-ember-500/15 text-ember-400 font-bold'>推断</span>}
          </span>
        ) : (
          <span className='text-[11px] text-ink-muted flex-shrink-0 min-w-[60px] text-center'>vs</span>
        )}
        <span className={`flex-1 text-left text-sm font-bold truncate ${awayLead ? 'text-field-300' : 'text-ink-primary'}`}>{fx.away}</span>
      </div>

      {/* 赔率 + 分析入口 */}
      <div className='mt-1 flex items-start justify-between gap-2'>
        <OddsPanel fx={fx} />
        {typeof fx.odds_h === 'number' && typeof fx.odds_d === 'number' && typeof fx.odds_a === 'number' && onAnalyze && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              const hasOU = fx.ou_line != null && fx.ou_over != null && fx.ou_under != null
              const handicap = hasOU
                ? { ou_line: fx.ou_line, ou_over: fx.ou_over, ou_under: fx.ou_under,
                    ah_line: fx.ah_line ?? undefined, ah_home: fx.ah_home ?? undefined, ah_away: fx.ah_away ?? undefined }
                : undefined
              onAnalyze(
                fx.home, fx.away, fx.sport_key || fx.league,
                { h: fx.odds_h as number, d: fx.odds_d as number, a: fx.odds_a as number },
                handicap,
                (typeof fx.score_home === 'number' && typeof fx.score_away === 'number')
                  ? { homeGoals: fx.score_home, awayGoals: fx.score_away, elapsed: typeof fx.match_minute === 'number' ? fx.match_minute : undefined }
                  : undefined
              )
            }}
            className='flex-shrink-0 px-2.5 py-1 rounded text-[11px] bg-gradient-to-r from-frost-500 to-frost-600 text-white font-bold hover:opacity-90 transition-opacity'
          >分析</button>
        )}
      </div>
    </div>
  )
}

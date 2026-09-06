import type { FixtureEntry } from '@/types'

// ═══ 工具 (统一用 timeZone:'Asia/Shanghai', 不依赖本机时区, 任意机器都正确) ═══
export function fmtClockGMT8(now: number) {
  return new Date(now).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}
export function fmtGMT8(iso: string) {
  if (!iso) return '' // 空开赛时间 → 空串, 而非 "Invalid Date"
  try {
    if (isNaN(new Date(iso).getTime())) return '' // 无效日期 toLocaleTimeString 返回 "Invalid Date" 而非抛错, 需显式拦截
    return new Date(iso).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false })
  } catch { return '' }
}
export function fmtOdds(v: number | undefined | null) {
  return typeof v === 'number' && !isNaN(v) ? v.toFixed(2) : '——'
}

// 比赛状态机 (IR-24 实时比分=真相; 前端按时间兜底纠正僵尸卡盘)
// mststi: 0=未开赛 1=上半场 2=中场 3=下半场 4=加时 5=点球 6+=异常(中断/延期/取消) <0=结束
// 注意:
//   1. feed 的 state 标记经常滞后 (实际已开赛但 state 仍=0)
//   2. match_minute='PA' 表示"待定/延期", 此时无论 state 是什么都不应显示进行中
//   3. state>=6 是异常状态 (非正常进行中), 不应显示比分
//   4. score 缺失 (null/undefined) 时不能显示比分
export function stateOf(fx: FixtureEntry, now: number): { live: boolean; finished: boolean; pending: boolean; halftime: boolean; min: number | null; label: string } {
  let st = Number(fx.match_state ?? 0)
  if (isNaN(st)) st = 0
  // 前端兜底: 开赛时间在未来 → 不可能是进行中/已结束 (修正 GQ 偶发把未来赛程标成 live+minute=45)
  if (fx.commence_time) {
    const ko = new Date(fx.commence_time).getTime()
    if (ko > now + 60000) {
      return { live: false, finished: false, pending: false, halftime: false, min: null, label: `${fmtGMT8(fx.commence_time)} 开赛` }
    }
  }
  const raw = String(fx.match_minute ?? '').replace(/[′'"]/g, '')
  const mm = parseInt(raw, 10)
  const minStr = isNaN(mm) ? '' : `${mm}'`
  const isPA = raw === 'PA' || raw === '中场' || raw === 'P'
  const isAbnormal = st >= 6   // 6+ = 中断/延期/取消等异常
  // PA 或异常状态 → 一律视为待定, 不显示进行中
  if (isPA || isAbnormal) {
    return { live: false, finished: false, pending: true, halftime: false, min: null, label: '待定' }
  }
  // feed 状态滞后兜底: state=0 但开赛已过 10-180min → 视为进行中
  if (st === 0 && fx.commence_time) {
    const elapsedMin = (now - new Date(fx.commence_time).getTime()) / 60000
    if (elapsedMin > 10 && elapsedMin < 180) {
      st = 1 // 视为上半场
    }
  }
  // 开赛已超 150min → 视为已结束 (足球最长含加时点球≈150min; 超过仍标live=僵尸卡盘,
  // 前端按时间兜底纠正。原180min放宽是为兼容 feed 滞后, 现结合后端僵尸清理可收紧)
  if (fx.commence_time) {
    const elapsedMin = (now - new Date(fx.commence_time).getTime()) / 60000
    if (elapsedMin > 150 && st > 0) st = -1
  }
  // 后端快照时间兜底: snapshot_at 过老说明采集器已失联
  if (st > 0 && (fx as any).snapshot_at) {
    const ageMin = (now - Number((fx as any).snapshot_at) * 1000) / 60000
    if (ageMin > 60) st = -1
  }
  // 半场识别: 仅当后端明确返回中场状态(state=2) 或 minute 明确为 HT/中场/PB 时才是中场休息。
  // 不能把 integer 45 当成半场: 45 可能是上半场第45分钟、上半场补时(45+)或数据脏标,
  // 若直接判中场会误导用户以为比赛已暂停(如此前 2-1 时显示中场休息, 实际仍在踢成 2-3)。
  const isHalftime = st === 2 || raw === 'HT' || raw === '中场' || raw === 'PB'
  let label = ''
  if (isHalftime) label = '中场休息'
  else if (st === 1) label = `上半场 ${minStr || `~${Math.round((now - new Date(fx.commence_time).getTime()) / 60000)}'`}`.trim()
  else if (st === 3) label = `下半场 ${minStr}`.trim()
  else if (st === 4) label = `加时 ${minStr}`.trim()
  else if (st === 5) label = '点球大战'
  else if (st < 0) label = '已结束'
  return { live: st > 0 && !isHalftime, finished: st < 0, pending: false, halftime: isHalftime, min: isNaN(mm) ? null : mm, label }
}

// 倒计时 (距开赛)
export function countdown(iso: string, now: number): string | null {
  const ko = new Date(iso).getTime()
  if (isNaN(ko)) return null // 空/无效开赛时间 → 不显示倒计时 (修复 "距开赛 NaNm")
  const remain = ko - now
  if (remain <= 0) return null
  const m = Math.floor(remain / 60000)
  const h = Math.floor(m / 60)
  const d = Math.floor(h / 24)
  if (d > 0) return `${d}天${h % 24}h`
  if (h > 0) return `${h}h${m % 60}m`
  return `${m}m`
}

// ═══ 虚盘过滤 (滤掉乐鱼混入的电竞模拟 + 8分钟虚拟杯) ═══
export const FAKE_LEAGUE = /VS-|EAFC|PANDA|瓦尔哈拉|瓦尔基里|梦幻对垒|8分钟/

// ═══ /api/live-scores 真实比赛 → FixtureEntry (补齐 obscure 联赛被 slice(0,20) 截断的 live 比赛) ═══
export function liveToFixture(m: any): FixtureEntry {
  const commence = m.kickoff
    || (m.snapshot_at ? new Date(m.snapshot_at * 1000).toISOString() : new Date().toISOString())
  return {
    id: m.mid || `${m.home}|${m.away}`,
    home: m.home,
    away: m.away,
    commence_time: commence,
    league: m.league,
    sport_key: m.league,
    match_state: m.mststi ?? m.match_state ?? 0,
    match_minute: m.match_minute ?? '',
    score_home: m.score_home ?? null,
    score_away: m.score_away ?? null,
    score_inferred: m.score_inferred ?? false,
    odds_h: m.odds_h ?? null,
    odds_d: m.odds_d ?? null,
    odds_a: m.odds_a ?? null,
    opening_h: m.opening_h ?? null,
    opening_d: m.opening_d ?? null,
    opening_a: m.opening_a ?? null,
    ah_line: m.ah_line ?? null,
    ah_home: m.ah_home ?? null,
    ah_away: m.ah_away ?? null,
    ou_line: m.ou_line ?? null,
    ou_over: m.ou_over ?? null,
    ou_under: m.ou_under ?? null,
  } as FixtureEntry
}

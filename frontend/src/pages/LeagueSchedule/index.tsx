import { useState, useEffect, useCallback, useRef } from 'react'
import { motion } from 'framer-motion'
import { leagueScheduleService, liveScoreService, terminalService } from '@/services/api'
import type { LeaguesResponse, LeagueCatalogEntry, FixtureEntry, LiveScoreMatch } from '@/types'
import MatchAnalysisModal from './MatchAnalysisModal'

// ═══ 平台配色 (从 C:\Users\ShXAI\Documents\1 CSS 提取) ═══
// 底 #000c17, 卡 #001529, 边框 #1a2e42, 蓝 #1890ff, 红 #ff4d4f
// 绿(异常/直播高亮) #389e0d, 黄 #f59e0b, 紫 #531dab
// 文本 #6c7ba8 (次) / #414655 (三级)

function fmtDate(iso: string) { try { return new Date(iso).toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'}) } catch { return '--' } }
function fmtGMT8(iso: string) { try { return new Date(iso).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hour12:false}) } catch { return '' } }
// GMT+8 日期键 (YYYY-MM-DD), 用于"今天"判断
function gmt8DayKey(iso: string) { try { return new Date(iso).toLocaleDateString('en-CA',{timeZone:'Asia/Shanghai'}) } catch { return '' } }
// GMT+8 实时时钟 (HH:MM:SS)
function fmtClockGMT8(now: number) { return new Date(now).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) }
function fmtOdds(v: number|undefined) { return typeof v==='number' && !isNaN(v) ? v.toFixed(2) : '——' }
// Req2: 赔率漂移箭头 (↑=赔率升/资金撤出, ↓=赔率降/资金涌入)
function DriftArrow({field, snapshot}: {field:string; snapshot:FixtureEntry['_snapshot']}) {
  const delta = snapshot?.drift?.[field as keyof NonNullable<NonNullable<FixtureEntry['_snapshot']>['drift']>]
  if (delta == null || Math.abs(delta) < 0.01) return null
  const up = delta > 0
  return <span className={`text-[9px] font-bold ml-0.5 ${up ? 'text-ember-400' : 'text-accent'}`}>{up ? '↑' : '↓'}{Math.abs(delta).toFixed(2)}</span>
}
// ═══ 实时比赛计时器 (Req1: 比赛开始后走字同步) ═══
// 原理: feed 每 30s 返回 match_minute 作为锚点, 本地每秒递增。
//       支持上半场(0-45)/中场休息(45-45+15)/下半场(60-90+)/伤停补时
function useLiveMinute(matchMinute: string | number | undefined | null, matchState: number) {
  const [localMin, setLocalMin] = useState<number | null>(null)
  const anchorRef = useRef<{ ts: number; min: number } | null>(null)

  useEffect(() => {
    if (matchState <= 0 || matchMinute == null) {
      setLocalMin(null); anchorRef.current = null; return
    }
    // 解析 feed 返回的分钟 (可能是 "29'" 或 29 或 "HT" 或 "PA")
    const ms = String(matchMinute).replace(/[′'"]/g, '')
    if (ms === 'HT' || ms === '中场') { setLocalMin(45); return }
    if (ms === 'FT' || ms === '结束') { setLocalMin(90); return }
    if (ms === 'PA') { setLocalMin(null); return }
    const parsed = parseInt(ms, 10)
    if (isNaN(parsed)) { setLocalMin(null); return }

    // 新锚点: feed 分钟变了 → 重置
    if (!anchorRef.current || Math.abs(parsed - anchorRef.current.min) > 2) {
      anchorRef.current = { ts: Date.now(), min: parsed }
      setLocalMin(parsed)
      return
    }

    // 本地递增
    const elapsed = (Date.now() - anchorRef.current.ts) / 60000
    setLocalMin(Math.round((anchorRef.current.min + elapsed) * 10) / 10)
  }, [matchMinute, matchState])

  // 每秒 tick
  useEffect(() => {
    if (!anchorRef.current || localMin == null) return
    const id = setInterval(() => {
      const elapsed = (Date.now() - anchorRef.current!.ts) / 60000
      setLocalMin(Math.round((anchorRef.current!.min + elapsed) * 10) / 10)
    }, 1000)
    return () => clearInterval(id)
  }, [localMin != null]) // eslint-disable-line

  return localMin
}

function formatMatchTime(min: number | null, isHalftime = false): string {
  if (isHalftime) return `中场休息`
  if (min == null) return ''
  if (min < 0.5) return `开球`
  if (min < 45) return `上半场 ${Math.floor(min)}:${String(Math.round((min % 1) * 60)).padStart(2, '0')}`
  // 45-60 分钟可能是上半场补时、真正中场休息或下半场刚开始; 数据源无明确 HT 标记时,
  // 保守显示为"上半场 45+'", 避免把正在踢的比赛(如此前 2-1 阶段)误判为中场休息。
  if (min < 60) return `上半场 ${Math.floor(min)}+'`
  if (min < 90) return `下半场 ${Math.floor(min)}:${String(Math.round((min % 1) * 60)).padStart(2, '0')}`
  return `下半场 ${Math.floor(min)}'+`
}

const Lock = () => <span className='text-ink-muted text-[11px]'>——</span>

// 让球/大小球载荷 (前端 fixture → 分析端点透传, 与分析波胆×让球交叉标注一致)
type HandicapPayload = {
  ah_line?: number | string
  ah_home?: number
  ah_away?: number
  ou_line?: number | string
  ou_over?: number
  ou_under?: number
}
// 从 fixture 构让球载荷 (缺失字段留 undefined, 后端按 None 处理)
function buildHandicap(fx: FixtureEntry): HandicapPayload | undefined {
  if (fx.ah_line === undefined && fx.ou_line === undefined &&
      fx.ah_home === undefined && fx.ou_over === undefined) return undefined
  return {
    ah_line: fx.ah_line, ah_home: fx.ah_home, ah_away: fx.ah_away,
    ou_line: fx.ou_line, ou_over: fx.ou_over, ou_under: fx.ou_under,
  }
}
// Req2: 从开盘快照取初始赔率 (分析弹窗双栏对比用)
function buildInitialOdds(fx: FixtureEntry): {h:number;d:number;a:number} | undefined {
  const ini = fx._snapshot?.initial
  if (ini && ini.odds_h !== undefined && ini.odds_d !== undefined && ini.odds_a !== undefined)
    return { h: ini.odds_h, d: ini.odds_d, a: ini.odds_a }
  return undefined
}
function buildInitialHandicap(fx: FixtureEntry): HandicapPayload | undefined {
  const ini = fx._snapshot?.initial
  if (!ini) return undefined
  if (ini.ah_line === undefined && ini.ou_line === undefined &&
      ini.ah_home === undefined && ini.ou_over === undefined) return undefined
  return { ah_line: ini.ah_line, ah_home: ini.ah_home, ah_away: ini.ah_away,
           ou_line: ini.ou_line, ou_over: ini.ou_over, ou_under: ini.ou_under }
}
// 客队盘口镜像: 主队视角 line 翻符号 (主受让+ ⇒ 客让球-); 支持 split 盘 (0/0.5, -0/0.5)
function mirrorLine(line?: number | string): string {
  if (line == null) return ''
  const s = String(line)
  if (!s.includes('/')) {
    const core = s.replace(/^[+-]/, '')
    if (core === '0') return '0'
    return s.startsWith('-') ? core : '-' + core
  }
  const toks = s.split('/')
  const homeNeg = toks[0].startsWith('-')
  const mirrorNeg = !homeNeg   // 主 '-' ⇒ 客 '+'; 主 '+'/无 ⇒ 客 '-'
  return toks.map((t) => {
    const neg = t.startsWith('-'); const pos = t.startsWith('+')
    const core = t.replace(/^[+-]/, '')
    if (core === '0') return '0'
    if (neg) return core            // 翻转 -x ⇒ +x
    if (pos) return '-' + core      // 翻转 +x ⇒ -x
    return mirrorNeg ? '-' + core : '+' + core  // 无符号继承镜像符号
  }).join('/')
}

// ═══ 比赛展开详情 (Req4+Req2: 点击 ▾ 显示波胆/全盘口汇总 + 赔率漂移) ═══
function MatchDetailRow({fx}: {fx: FixtureEntry}) {
  const snap = fx._snapshot
  const hasDrift = snap && snap.drift && Object.keys(snap.drift).length > 0
  // Req3: 已结束比赛 → 自动记录赛果标签
  const isFinished = Number(fx.match_state ?? 0) < 0
  const resultLabel = isFinished && typeof fx.score_home === 'number' && typeof fx.score_away === 'number'
    ? (fx.score_home > fx.score_away ? '主胜' : fx.score_home === fx.score_away ? '平局' : '客胜')
    : null

  // 全部盘口汇总 (当前赔率 + 字段名用于漂移查询)
  const allMarkets = [
    { label: '1X2', items: [
      fx.odds_h != null ? ['主胜', fx.odds_h, 'odds_h'] : null,
      fx.odds_d != null ? ['平局', fx.odds_d, 'odds_d'] : null,
      fx.odds_a != null ? ['客胜', fx.odds_a, 'odds_a'] : null,
    ].filter(Boolean) as [string, number, string][] },
    { label: '全场让球', line: fx.ah_line, items: [
      fx.ah_home != null ? [`主 ${fx.ah_line||''}`, fx.ah_home, 'ah_home'] : null,
      fx.ah_away != null ? [`客 ${mirrorLine(fx.ah_line)}`, fx.ah_away, 'ah_away'] : null,
    ].filter(Boolean) as [string, number, string][] },
    { label: '全场大小', line: fx.ou_line, items: [
      fx.ou_over != null ? [`大 ${fx.ou_line||''}`, fx.ou_over, 'ou_over'] : null,
      fx.ou_under != null ? [`小 ${fx.ou_line||''}`, fx.ou_under, 'ou_under'] : null,
    ].filter(Boolean) as [string, number, string][] },
    { label: '半场让球', line: fx.h_ah_line, items: [
      fx.h_ah_home != null ? [`主 ${fx.h_ah_line||''}`, fx.h_ah_home, 'h_ah_home'] : null,
      fx.h_ah_away != null ? [`客 ${mirrorLine(fx.h_ah_line)}`, fx.h_ah_away, 'h_ah_away'] : null,
    ].filter(Boolean) as [string, number, string][] },
    { label: '半场大小', line: fx.h_ou_line, items: [
      fx.h_ou_over != null ? [`大 ${fx.h_ou_line||''}`, fx.h_ou_over, 'h_ou_over'] : null,
      fx.h_ou_under != null ? [`小 ${fx.h_ou_line||''}`, fx.h_ou_under, 'h_ou_under'] : null,
    ].filter(Boolean) as [string, number, string][] },
  ].filter(m => m.items.length > 0)

  return (
    <div className='grid grid-cols-[180px_repeat(4,minmax(0,1fr))_40px] border-b border-surface-border/30 bg-accent-inner/30'>
      <div className='px-3 py-2.5 border-r border-surface-border/30'>
        <span className='text-xs text-accent font-bold'>盘口详情</span>
        <div className='text-[11px] text-ink-muted mt-0.5'>{fx.league || ''}</div>
        {isFinished && resultLabel && (
          <span className='text-[10px] px-1.5 py-0.5 rounded bg-accent/20 text-accent font-bold mt-1 inline-block'>
            赛果 {fx.score_home}-{fx.score_away} · {resultLabel}
          </span>
        )}
        {hasDrift && <span className='text-[10px] px-1 py-0.5 rounded bg-ember-500/20 text-ember-400 font-bold mt-1 inline-block'>水位变动中</span>}
      </div>
      {allMarkets.map((market, mi) => (
        <div key={mi} className='px-2.5 py-2.5 border-r border-surface-border/20 flex flex-col gap-0.5'>
          <span className='text-[10px] text-accent/70 font-bold uppercase tracking-wider'>{market.label}
            {market.line ? ` (${market.line})` : ''}
          </span>
          {market.items.map(([label, val, field], ri) => {
            const isHome = label.startsWith('主') || label.startsWith('大')
            const isAway = label.startsWith('客') || label.startsWith('小')
            const colorClass = isHome ? 'text-pitch-400' : isAway ? 'text-ember-400' : 'text-white/60'
            const fld = field as 'odds_h'|'odds_d'|'odds_a'|'ah_home'|'ah_away'|'ou_over'|'ou_under'
            const delta = snap?.drift?.[fld]
            const opening = snap?.initial?.[fld]
            return (
              <div key={ri} className='flex flex-col gap-0.5 text-[11px]'>
                <div className='flex justify-between gap-2 items-baseline'>
                  <span className='text-ink-muted'>{label}</span>
                  <span className={`text-sm font-mono font-bold ${colorClass}`}>{fmtOdds(val)}</span>
                </div>
                {opening != null && (
                  <div className='flex justify-between gap-2'>
                    <span className='text-[10px] text-ink-muted/70'>开 {fmtOdds(opening)}</span>
                    {delta != null && Math.abs(delta) >= 0.01 && (
                      <span className={`text-[10px] font-black ${delta > 0 ? 'text-ember-400' : 'text-accent'}`}>
                        {delta > 0 ? '↑' : '↓'}{Math.abs(delta).toFixed(2)}
                      </span>
                    )}
                  </div>
                )}
              </div>
            )
          })}
          {market.items.length === 0 && <Lock/>}
        </div>
      ))}
      <div className='px-1.5 py-2' />
    </div>
  )
}

// ═══ 中栏: 比赛列表 (按联赛分组, 可折叠) ═══
function MatchRow({fx, onAnalyze, now, onToggleExpand, expanded, activeLiveId, onSelectLiveId}:{
  fx:FixtureEntry; now:number;
  onAnalyze:(h:string,a:string,sportKey?:string,odds?:{h:number;d:number;a:number},handicap?:HandicapPayload,initialOdds?:{h:number;d:number;a:number},initialHandicap?:HandicapPayload,liveScore?:{homeGoals:number;awayGoals:number;elapsed?:number})=>void;
  onToggleExpand?:()=>void; expanded?:boolean;
  activeLiveId?: string | null
  onSelectLiveId?: (id:string) => void
}) {
  const ms = Number(fx.match_state ?? 0)
  const isFinished = ms < 0
  const isLive = ms > 0
  const gmt8 = fmtGMT8(fx.commence_time)
  const ko = new Date(fx.commence_time).getTime()
  const remain = (isLive||isFinished) ? 0 : (ko-now)
  const cdStr = remain > 0 ? (()=>{const m=Math.floor(remain/60000); return m<60?`${m}m`:`${Math.floor(m/60)}h${m%60}m`})() : ''

  // 实时计时器 (Req1)
  const liveMin = useLiveMinute(fx.match_minute, ms)
  const timeLabel = isLive ? formatMatchTime(liveMin, ms === 2)
    : !isLive && !isFinished && remain > 0 ? `距开赛 ${cdStr}`
    : !isLive && !isFinished && remain <= 0 ? '即将开赛'
    : '已结束'

  const showScore = (isLive||isFinished) && typeof fx.score_home === 'number'

  // 5 大市场列
  const oneX2 = [
    fx.odds_h !== undefined ? ['主', fx.odds_h] : null,
    fx.odds_d !== undefined ? ['平', fx.odds_d] : null,
    fx.odds_a !== undefined ? ['客', fx.odds_a] : null,
  ].filter(Boolean) as [string, number][]
  const fullAH = [
    fx.ah_home !== undefined ? [`主 ${fx.ah_line||''}`, fx.ah_home] : null,
    fx.ah_away !== undefined ? [`客 ${mirrorLine(fx.ah_line)}`, fx.ah_away] : null,
  ].filter(Boolean) as [string, number][]
  const fullOU = [
    fx.ou_over !== undefined ? [`大 ${fx.ou_line||''}`, fx.ou_over] : null,
    fx.ou_under !== undefined ? [`小 ${fx.ou_line||''}`, fx.ou_under] : null,
  ].filter(Boolean) as [string, number][]
  const halfAH = [
    fx.h_ah_home !== undefined ? [`主 ${fx.h_ah_line||''}`, fx.h_ah_home] : null,
    fx.h_ah_away !== undefined ? [`客 ${mirrorLine(fx.h_ah_line)}`, fx.h_ah_away] : null,
  ].filter(Boolean) as [string, number][]
  const halfOU = [
    fx.h_ou_over !== undefined ? [`大 ${fx.h_ou_line||''}`, fx.h_ou_over] : null,
    fx.h_ou_under !== undefined ? [`小 ${fx.h_ou_line||''}`, fx.h_ou_under] : null,
  ].filter(Boolean) as [string, number][]

  const fid = fx.id || `${fx.home}-${fx.away}`
  return (
    <div
      onClick={() => isLive && onSelectLiveId?.(fid)}
      className={`grid grid-cols-[200px_1fr_1fr_1fr_1fr_56px] border-b transition-colors text-[11px] cursor-pointer ${
        activeLiveId === fid
          ? 'bg-accent/10 border-accent/30'
          : 'border-surface-border hover:bg-surface-hover/50'
      }`}>
      <div className='px-3 py-2 flex flex-col gap-0.5 border-r border-surface-border/50'>
        <div className='flex items-center gap-1.5'>
          <span className={`w-1 h-1 rounded-full ${isLive ? 'bg-ember-500 animate-pulse' : isFinished ? 'bg-ink-muted' : 'bg-frost-500'}`}/>
          {isLive && showScore && <span className='text-ember-500 font-bold animate-pulse'>{fx.score_home}-{fx.score_away}</span>}
        </div>
        <div className='flex items-center gap-1.5 text-ink-secondary'>
          <span className='font-mono text-[11px]'>{gmt8}</span>
          <span className={`text-[10px] ${isLive ? 'text-ember-500 font-bold' : remain > 0 && !isFinished ? 'text-field-500' : 'text-ink-muted'} ${isLive && liveMin != null ? 'animate-pulse' : ''}`}>
            {timeLabel}
          </span>
          {/* Req2: 赔率漂移摘要徽章 */}
          {fx._snapshot?.drift && Object.keys(fx._snapshot.drift).length > 0 && (
            <span className='text-[8px] px-1 py-0.5 rounded bg-ember-500/15 text-ember-400 font-bold animate-pulse'>
              水位变
            </span>
          )}
        </div>
        <div className='flex items-center gap-1 text-ink-secondary truncate'>
          <span className='truncate font-medium'>{fx.home}</span>
          <span className='text-ink-muted'>vs</span>
          <span className='truncate font-medium'>{fx.away}</span>
        </div>
      </div>
      {[oneX2, fullAH, fullOU, halfAH, halfOU].map((rows, ci) => (
        <div key={ci} className='px-2 py-2 flex flex-col gap-0.5 border-r border-surface-border/30 justify-center'>
          {rows.length === 0 ? (
            <>
              <div className='flex items-center justify-between gap-1 py-0.5'><Lock/></div>
              <div className='flex items-center justify-between gap-1 py-0.5'><Lock/></div>
            </>
          ) : rows.map(([label, val], ri) => {
            const isHome = label.startsWith('主') || label.startsWith('大')
            const isAway = label.startsWith('客') || label.startsWith('小')
            const colorClass = isHome ? 'text-pitch-400' : isAway ? 'text-ember-400' : 'text-ink-secondary'
            return (
              <div key={ri} className='flex items-center justify-between gap-1 py-0.5 leading-none'>
                <span className='text-[10px] text-ink-secondary truncate'>{label}</span>
                <span className={`text-[14px] font-black font-mono ${colorClass}`}>{fmtOdds(val)}</span>
              </div>
            )
          })}
        </div>
      ))}
      {/* 分析按钮 (Req5: 手动触发, 不再整行onClick) */}
      <div className='px-1.5 py-2 flex items-center justify-center border-l border-surface-border/30'>
        <button
          onClick={(e) => { e.stopPropagation(); onAnalyze(fx.home, fx.away, fx.sport_key, fx.odds_h !== undefined ? {h:fx.odds_h!, d:fx.odds_d!, a:fx.odds_a!} : undefined, buildHandicap(fx), buildInitialOdds(fx), buildInitialHandicap(fx), (typeof fx.score_home === 'number' && typeof fx.score_away === 'number') ? {homeGoals:fx.score_home!, awayGoals:fx.score_away!, elapsed: typeof fx.match_minute === 'number' ? fx.match_minute : undefined} : undefined) }}
          className='px-2 py-1 rounded bg-accent/15 hover:bg-accent/25 text-accent text-[10px] font-bold transition-colors'
        >分析</button>
        {onToggleExpand && (
          <button
            onClick={(e) => { e.stopPropagation(); onToggleExpand() }}
            className='ml-0.5 px-1.5 py-1 rounded bg-surface-hover hover:bg-white/[0.06] text-ink-muted text-[9px] transition-colors'
            title={expanded ? '收起详情' : '展开波胆/赔率'}
          >{expanded ? '▴' : '▾'}</button>
        )}
      </div>
    </div>
  )
}

// 联赛分类 (可折叠)
function LeagueGroup({name, count, fixtures, onAnalyze, now, defaultOpen=true, expandedIds, onToggleExpand, activeLiveId, onSelectLiveId}:{
  name:string; count:number; fixtures:FixtureEntry[]; onAnalyze:any; now:number;
  defaultOpen?:boolean; expandedIds?: Set<string>; onToggleExpand?(id:string):void;
  activeLiveId?: string | null; onSelectLiveId?: (id:string) => void
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className='mb-1.5'>
      <div onClick={()=>setOpen(!open)}
        className='flex items-center justify-between px-3 h-9 bg-surface-panel border border-surface-border rounded cursor-pointer hover:border-frost-500/30 transition-colors'>
        <div className='flex items-center gap-2'>
          <span className={`w-3 h-3 transition-transform ${open?'rotate-90':''}`}>
            <svg viewBox='0 0 24 24' fill='currentColor' className='text-accent'><path d='M8 5l8 7-8 7V5z'/></svg>
          </span>
          <span className='text-[12px] font-bold text-white'>{name}</span>
          <span className='text-[10px] px-1.5 py-0.5 bg-field-500/15 text-field-500 rounded font-bold'>{count} 场</span>
        </div>
        <span className='text-[10px] text-ink-muted'>全独赢 / 让球 / 大小 / 半场</span>
      </div>
      {open && (
        <div className='mt-0.5 bg-surface-canvas border-l border-r border-b border-surface-border/50 rounded-b'>
          {fixtures.length === 0 ? (
            <div className='px-3 py-2 text-[10px] text-ink-muted'>暂无赛程</div>
          ) : (
            <div>
              {fixtures.map(f => {
                const fid = f.id || `${f.home}-${f.away}`
                return (
                  <div key={fid}>
                    <MatchRow fx={f} onAnalyze={onAnalyze} now={now}
                      expanded={expandedIds?.has(fid)}
                      onToggleExpand={onToggleExpand ? (() => onToggleExpand(fid)) : undefined}
                      activeLiveId={activeLiveId}
                      onSelectLiveId={onSelectLiveId}
                    />
                    {expandedIds?.has(fid) && <MatchDetailRow fx={f} />}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ═══ 议程行 (紧凑单行: 时间 + 状态 + 对阵 + 1X2/比分 + 分析按钮) ═══
// 三态: 未开赛(倒计时+独赢) | 进行中(分钟+实时比分) | 已结束(终场比分)
function UpcomingRow({fx, now, onAnalyze, activeLiveId, onSelectLiveId}:{fx:FixtureEntry;now:number;onAnalyze:(h:string,a:string,sportKey?:string,odds?:{h:number;d:number;a:number},handicap?:HandicapPayload,initialOdds?:{h:number;d:number;a:number},initialHandicap?:HandicapPayload,liveScore?:{homeGoals:number;awayGoals:number;elapsed?:number})=>void; activeLiveId?: string | null; onSelectLiveId?: (id:string) => void}) {
  const ko = new Date(fx.commence_time).getTime()
  const remain = ko - now
  const cd = remain > 0 ? (()=>{const m=Math.floor(remain/60000); const h=Math.floor(m/60); return h>0?`${h}h${m%60}m`:`${m}m`})() : null
  const state = Number(fx.match_state ?? 0)
  const hasScore = typeof fx.score_home === 'number' && typeof fx.score_away === 'number'
  // 状态判定: 严格信任后端 match_state; 后端已综合 kickoff/status/minute/last_seen 做兜底,
  // 前端不再自行推断, 避免 finished(state=-1) 因 score!=0 被误判为 live。
  const isFinished = state < 0
  const isLive = state > 0 && !isFinished
  // 解析分钟: "45"→45, "45+2"→45, "HT"→45
  const parseMin = (m: any): number | null => {
    if (m == null) return null
    const s = String(m).replace(/[′'"]/g, '')
    if (s === 'HT' || s === '中场') return 45
    if (s === 'FT' || s === '结束') return 90
    const n = parseInt(s, 10)
    return isNaN(n) ? null : n
  }
  const minute = parseMin(fx.match_minute)
  const oneX2 = [
    fx.odds_h !== undefined ? ['主', fx.odds_h] : null,
    fx.odds_d !== undefined ? ['平', fx.odds_d] : null,
    fx.odds_a !== undefined ? ['客', fx.odds_a] : null,
  ].filter(Boolean) as [string, number][]
  const fid = fx.id || `${fx.home}-${fx.away}`
  return (
    <div
      onClick={() => isLive && onSelectLiveId?.(fid)}
      className={`flex items-center gap-3 px-3 py-2 border-b transition-colors cursor-pointer ${
        activeLiveId === fid
          ? 'bg-accent/10 border-accent/30'
          : isLive ? 'bg-field-500/[0.04] hover:bg-field-500/[0.08] border-surface-border/60' :
            isFinished ? 'bg-surface-hover/30 hover:bg-surface-hover/50 border-surface-border/60' :
            'hover:bg-surface-hover/50 border-surface-border/60'
      }`}>
      <div className='flex flex-col w-[78px] flex-shrink-0'>
        <span className='font-mono text-[12px] text-white font-bold'>{fmtGMT8(fx.commence_time)}</span>
        {isLive ? (
          <span className='text-[10px] text-field-400 font-bold animate-pulse'>
            进行中{minute != null ? ` ${minute}'` : ''}
          </span>
        ) : isFinished ? (
          <span className='text-[10px] text-frost-400'>已结束</span>
        ) : remain > 0 ? (
          <span className='text-[10px] text-field-500'>距开赛 {cd}</span>
        ) : (
          <span className='text-[10px] text-ember-500 animate-pulse'>即将开赛</span>
        )}
      </div>
      <span className='text-[9px] px-1.5 py-0.5 bg-surface-hover text-ink-muted rounded max-w-[78px] truncate flex-shrink-0'>{fx.league}</span>
      <div className='flex-1 flex items-center gap-1.5 text-[12px] min-w-0'>
        <span className={`truncate font-medium ${isLive && typeof fx.score_home==='number' && typeof fx.score_away==='number' && fx.score_home > fx.score_away ? 'text-field-300' : 'text-white'}`}>{fx.home}</span>
        <span className='text-ink-muted text-[10px]'>vs</span>
        <span className={`truncate font-medium ${isLive && typeof fx.score_home==='number' && typeof fx.score_away==='number' && fx.score_away > fx.score_home ? 'text-field-300' : 'text-white'}`}>{fx.away}</span>
      </div>
      {/* 中间区: 比分(进行中/已结束) 或 1X2赔率(未开赛) */}
      <div className='flex items-center gap-2 flex-shrink-0 min-w-[120px] justify-end'>
        {(isLive || isFinished) && hasScore ? (
          <span className='flex items-center gap-1.5'>
            <span className='text-[9px] text-ink-muted'>{isFinished ? '终' : 'LIVE'}</span>
            <span className='font-mono font-black text-[15px] text-white tabular-nums'>
              {fx.score_home} - {fx.score_away}
            </span>
          </span>
        ) : oneX2.length === 0 ? <Lock/> : (
          oneX2.map(([label,val]) => (
            <span key={label} className='flex items-center gap-0.5'>
              <span className='text-ink-muted text-[9px]'>{label}</span>
              <span className='font-mono font-bold text-[14px] text-ink-secondary'>{fmtOdds(val)}</span>
            </span>
          ))
        )}
      </div>
      {/* 分析按钮 (Req5) */}
      <button
        onClick={(e) => { e.stopPropagation(); onAnalyze(fx.home, fx.away, fx.sport_key, fx.odds_h !== undefined ? {h:fx.odds_h!, d:fx.odds_d!, a:fx.odds_a!} : undefined, buildHandicap(fx), buildInitialOdds(fx), buildInitialHandicap(fx), (typeof fx.score_home === 'number' && typeof fx.score_away === 'number') ? {homeGoals:fx.score_home!, awayGoals:fx.score_away!, elapsed: typeof fx.match_minute === 'number' ? fx.match_minute : undefined} : undefined) }}
        className='px-2.5 py-1 rounded bg-accent/15 hover:bg-accent/25 text-accent text-[10px] font-bold transition-colors flex-shrink-0'
      >分析</button>
    </div>
  )
}

// ═══ 左栏: 联赛筛选 (搜索 + 折叠0场 + 选中) ═══
function LeagueFilter({allLeagues, selectedLeague, onSelectLeague}:{
  allLeagues: {name:string; fixture_count:number; sport_key:string}[]
  selectedLeague: string
  onSelectLeague: (sport_key:string) => void
}) {
  const [q, setQ] = useState('')
  const [showEmpty, setShowEmpty] = useState(false)
  const active = allLeagues.filter(l => l.fixture_count > 0)
  const empty = allLeagues.filter(l => l.fixture_count === 0)
  const base = showEmpty ? allLeagues : active
  const list = q.trim()
    ? allLeagues.filter(l => l.name.toLowerCase().includes(q.trim().toLowerCase()))
    : base
  return (
    <aside className='w-[240px] flex-shrink-0 bg-surface-panel border-r border-surface-border flex flex-col'>
      {/* 搜索 */}
      <div className='p-3 border-b border-surface-border'>
        <div className='relative'>
          <svg className='w-3.5 h-3.5 text-ink-muted absolute left-2.5 top-1/2 -translate-y-1/2' fill='none' viewBox='0 0 24 24' stroke='currentColor' strokeWidth='2'>
            <path strokeLinecap='round' strokeLinejoin='round' d='M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z' />
          </svg>
          <input
            value={q}
            onChange={e => setQ(e.target.value)}
            placeholder='搜索联赛'
            className='w-full bg-surface-canvas border border-surface-border rounded-md pl-8 pr-2 py-1.5 text-[12px] text-ink-primary placeholder:text-ink-disabled focus:outline-none focus:border-frost-500/50'
          />
        </div>
      </div>
      {/* 全部 */}
      <div className='p-2 border-b border-surface-border/60'>
        <button onClick={()=>onSelectLeague('')}
          className={`w-full text-left px-2.5 py-1.5 rounded text-[12px] font-medium transition-colors ${
            selectedLeague === '' ? 'bg-gradient-to-r from-frost-500 to-frost-600 text-white' : 'text-ink-secondary hover:text-white hover:bg-white/[0.04]'
          }`}>全部联赛</button>
      </div>
      {/* 列表 */}
      <div className='flex-1 overflow-y-auto px-2 py-2 space-y-0.5'>
        {list.map(lg => (
          <button key={lg.sport_key} onClick={()=>onSelectLeague(lg.sport_key)}
            className={`w-full flex items-center justify-between gap-2 px-2.5 py-1.5 rounded text-[12px] transition-colors ${
              selectedLeague === lg.sport_key ? 'bg-frost-500/15 text-frost-400 border border-frost-500/30' : 'text-ink-secondary hover:text-white hover:bg-white/[0.04] border border-transparent'
            }`}>
            <span className='truncate'>{lg.name}</span>
            {lg.fixture_count > 0 && (
              <span className='text-[9px] px-1 rounded bg-field-500/15 text-field-500 flex-shrink-0'>{lg.fixture_count}</span>
            )}
          </button>
        ))}
        {empty.length > 0 && !q.trim() && (
          <button onClick={()=>setShowEmpty(v => !v)}
            className={`w-full text-left px-2.5 py-1.5 rounded text-[10px] transition-colors border border-dashed ${
              showEmpty ? 'text-ember-500 border-ember-500/40 bg-ember-500/10' : 'text-ink-muted border-surface-border/60 hover:text-ink-secondary'
            }`}>
            {showEmpty ? '收起无赛程联赛' : `+${empty.length} 个无赛程`}
          </button>
        )}
      </div>
      {/* 底栏统计 */}
      <div className='p-2 border-t border-surface-border text-[10px] text-ink-muted text-center'>
        {active.length} 个有赛程 · {empty.length} 个无
      </div>
    </aside>
  )
}

// MiniMetric — Dashboard 头用的紧凑指标卡
function MiniMetric({label, value, accent}:{label:string; value:number|string; accent?: 'field'|'frost'|'ember'|'danger'}) {
  const color = accent === 'field' ? 'text-field-400'
    : accent === 'frost' ? 'text-frost-400'
    : accent === 'ember' ? 'text-ember-400'
    : accent === 'danger' ? 'text-danger-400' : 'text-ink-primary'
  return (
    <div className='px-3 py-1.5 rounded-lg bg-surface-canvas border border-surface-border/60 flex flex-col justify-center min-w-[60px]'>
      <span className='text-[9px] text-ink-muted leading-none mb-1'>{label}</span>
      <span className={`text-[16px] font-bold leading-none ${color}`}>{value}</span>
    </div>
  )
}

// ═══ 联赛赛程 (顶部) — 联赛选择 + 比赛列表 ═══
function LeagueSchedulePanel({fixtures, allLeagues, onAnalyze, selectedLeague, onSelectLeague, viewMode, onViewModeChange, now, expandedIds, onToggleExpand, activeLiveId, onSelectLiveId}:{
  fixtures: FixtureEntry[]
  allLeagues: {name:string; fixture_count:number; sport_key:string}[]
  onAnalyze: any
  selectedLeague: string
  onSelectLeague: (sport_key:string) => void
  viewMode: 'league' | 'timeline'
  onViewModeChange: (v: 'league' | 'timeline') => void
  now: number
  expandedIds?: Set<string>
  onToggleExpand?: (id:string) => void
  activeLiveId?: string | null
  onSelectLiveId?: (id:string) => void
}) {
  const todayKey = gmt8DayKey(new Date(now).toISOString())
  // Dashboard 指标 (头部卡片用)
  const liveCount = fixtures.filter(f => Number(f.match_state) > 0).length
  const analyzableCount = fixtures.filter(f => f.odds_h != null && f.odds_d != null && f.odds_a != null).length
  const activeLeaguesCount = allLeagues.filter(l => l.fixture_count > 0).length

  // 今日议程: 今日 + match_state=0(未开赛) 或 state>0(进行中) 或 比分已确定(已结束)
  // 以前只取 state=0, 导致已开打的比赛进不了未开赛议程, 也看不到比分
  const baseList = selectedLeague
    ? fixtures.filter(f => f.sport_key === selectedLeague)
    : fixtures
  const upcoming = baseList
    .filter(f => gmt8DayKey(f.commence_time) === todayKey)
    .sort((a,b) => new Date(a.commence_time).getTime() - new Date(b.commence_time).getTime())

  // 联赛分组 (支持筛选); 未开赛议程在上方叠加, 联赛分组保留全部比赛
  const groupSrc = baseList
  const groups: Record<string, FixtureEntry[]> = {}
  for (const f of groupSrc) {
    const lg = f.league || '其他'
    if (!groups[lg]) groups[lg] = []
    groups[lg].push(f)
  }
  // 分组排序: 含进行中的联赛置顶 → 其余按场次数降序; 组内按开赛时间升序
  const groupNames = Object.keys(groups).sort((a,b) => {
    const la = groups[a].some(f => Number(f.match_state) > 0) ? 1 : 0
    const lb = groups[b].some(f => Number(f.match_state) > 0) ? 1 : 0
    if (la !== lb) return lb - la
    return groups[b].length - groups[a].length
  })
  for (const k of groupNames) groups[k].sort((a,b) => new Date(a.commence_time).getTime() - new Date(b.commence_time).getTime())
  const totalLeagues = allLeagues.length

  return (
    <div className='flex-1 flex flex-col bg-surface-canvas overflow-hidden'>
      {/* Dashboard 头: 页名 + 关键指标 (联赛选择已移至左栏 LeagueFilter) */}
      <div className='bg-surface-panel border-b border-surface-border p-3 flex-shrink-0'>
        <div className='flex items-center justify-between gap-3'>
          <div className='flex items-center gap-2 min-w-0'>
            <span className='w-1.5 h-1.5 rounded-full bg-field-300 flex-shrink-0'/>
            <div className='min-w-0'>
              <h2 className='text-[15px] font-bold text-white leading-none'>联赛赛程</h2>
              <p className='text-[10px] text-ink-muted mt-1 truncate'>{fixtures.length} 场 · {activeLeaguesCount} 个联赛有赛程</p>
            </div>
          </div>
          <div className='flex items-stretch gap-2 flex-shrink-0'>
            <div className='flex rounded bg-surface-dark/30 border border-surface-border/20 overflow-hidden'>
              <button onClick={() => onViewModeChange('league')}
                className={`px-2.5 py-1 text-[10px] font-bold transition-colors ${viewMode === 'league' ? 'bg-accent/20 text-accent' : 'text-ink-muted hover:text-ink-secondary'}`}>
                联赛
              </button>
              <button onClick={() => onViewModeChange('timeline')}
                className={`px-2.5 py-1 text-[10px] font-bold transition-colors ${viewMode === 'timeline' ? 'bg-accent/20 text-accent' : 'text-ink-muted hover:text-ink-secondary'}`}>
                时间
              </button>
            </div>
            <MiniMetric label='今日' value={upcoming.length} accent='frost' />
            <MiniMetric label='进行中' value={liveCount} accent='ember' />
            <MiniMetric label='可分析' value={analyzableCount} accent='field' />
          </div>
        </div>
      </div>

      {/* 未开赛议程 (今日) */}
      <div className='flex-1 overflow-y-auto px-2 py-2'>
        <div className='mb-2.5'>
          <div className='flex items-center justify-between px-3 h-9 bg-surface-panel border border-surface-border rounded mb-1'>
            <div className='flex items-center gap-2'>
              <span className='w-1.5 h-1.5 rounded-full bg-field-500 animate-pulse'/>
            <span className='text-[13px] font-bold text-white'>今日议程</span>
            <span className='text-[10px] px-1.5 py-0.5 bg-field-500/15 text-field-500 rounded font-bold'>今日 {upcoming.length} 场</span>
            </div>
            <span className='text-[10px] text-ink-muted font-mono'>⏰ {fmtClockGMT8(now)} GMT+8</span>
          </div>
          {upcoming.length === 0 ? (
            <div className='px-3 py-2 text-[10px] text-ink-muted bg-surface-canvas border border-surface-border/50 rounded'>今日暂无未开赛程</div>
          ) : (
            <div className='bg-surface-canvas border border-surface-border/50 rounded overflow-hidden'>
              {upcoming.map(f => <UpcomingRow key={f.id||`${f.home}-${f.away}`} fx={f} now={now} onAnalyze={onAnalyze} activeLiveId={activeLiveId} onSelectLiveId={onSelectLiveId}/>)}
            </div>
          )}
        </div>

        {/* 联赛分组视图 */}
        {viewMode === 'league' && (
          <>
            <div className='grid grid-cols-[200px_1fr_1fr_1fr_1fr_56px] bg-surface-panel border border-surface-border/50 rounded-t flex-shrink-0 mt-1'>
              <div className='px-3 py-2 text-[10px] text-ink-secondary font-bold uppercase tracking-wider border-r border-surface-border/50'>联赛 / 时间</div>
              {['全部独赢','全场让球','全场大小','半场让球','半场大小'].map(h => (
                <div key={h} className='px-2 py-2 text-center text-[10px] text-ink-secondary font-bold uppercase tracking-wider border-r border-surface-border/30 last:border-r-0'>{h}</div>
              ))}
              <div className='px-1.5 py-2 text-center text-[10px] text-ink-secondary font-bold uppercase tracking-wider'>操作</div>
            </div>
            {groupNames.length === 0 ? (
              <div className='p-12 text-center text-ink-secondary text-sm'>该联赛当前窗口暂无赛程</div>
            ) : (
              <>
                <div className='px-1 mb-1 text-[10px] text-ink-muted'>联赛赛程 ({groupSrc.length} 场)</div>
                {groupNames.map(name => (
                  <LeagueGroup key={name} name={name} count={groups[name].length} fixtures={groups[name]} onAnalyze={onAnalyze} now={now} expandedIds={expandedIds} onToggleExpand={onToggleExpand} activeLiveId={activeLiveId} onSelectLiveId={onSelectLiveId}/>
                ))}
              </>
            )}
          </>
        )}

        {/* 时间轴视图: 扁平按开赛时间排序 + 联赛色标 */}
        {viewMode === 'timeline' && (
          <div className='mt-1'>
            <div className='px-1 mb-1 text-[10px] text-ink-muted'>时间轴 ({baseList.length} 场 · 按开赛升序)</div>
            <div className='space-y-0.5'>
              {baseList.slice().sort((a,b)=>new Date(a.commence_time).getTime()-new Date(b.commence_time).getTime()).map(f => {
                const id = f.id || `${f.home}-${f.away}`
                const state = Number(f.match_state ?? 0)
                const isLive = state > 0
                const isFinished = state < 0
                const hasScore = typeof f.score_home === 'number' && typeof f.score_away === 'number'
                const hasOdds = f.odds_h != null && f.odds_d != null && f.odds_a != null
                return (
                  <div key={id} className={`flex items-center gap-2 px-3 py-1.5 rounded text-xs border-l-2 transition-colors ${
                    isLive ? 'border-l-ember-500 bg-ember-500/[0.04]' :
                    isFinished ? 'border-l-frost-500/60 bg-transparent' :
                    'border-l-transparent bg-transparent hover:bg-surface-dark/20'
                  }`}>
                    {/* 时间 */}
                    <span className='text-[10px] font-mono text-ink-disabled w-9 flex-shrink-0'>{fmtGMT8(f.commence_time)}</span>
                    {/* 状态圆点 */}
                    <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${isLive ? 'bg-ember-500 animate-pulse' : isFinished ? 'bg-ink-disabled' : 'bg-surface-border'}`} />
                    {/* 联赛 */}
                    <span className='text-[9px] text-ink-disabled flex-shrink-0 max-w-[60px] truncate'>{f.league}</span>
                    {/* 对阵 + 比分 */}
                    <span className='flex-1 flex items-center gap-1.5 min-w-0'>
                      <span className={`font-medium truncate ${isLive && hasScore && f.score_home! > f.score_away! ? 'text-field-300' : 'text-white'}`}>{f.home}</span>
                      {hasScore ? (
                        <span className={`font-mono font-bold flex-shrink-0 ${isLive ? 'text-white' : 'text-ink-secondary'}`}>{f.score_home}-{f.score_away}</span>
                      ) : (
                        <span className='text-ink-disabled flex-shrink-0'>vs</span>
                      )}
                      <span className={`font-medium truncate ${isLive && hasScore && f.score_away! > f.score_home! ? 'text-field-300' : 'text-white'}`}>{f.away}</span>
                      {isLive && f.match_minute && <span className='text-[9px] text-ember-400 font-mono flex-shrink-0'>{f.match_minute}&apos;</span>}
                    </span>
                    {/* 赔率微型 */}
                    {hasOdds && (
                      <span className='text-[9px] font-mono text-ink-muted flex-shrink-0 hidden sm:inline'>
                        <span>{fmtOdds(f.odds_h)}</span>
                        <span className='mx-0.5 text-ink-disabled'>/</span>
                        <span>{fmtOdds(f.odds_d)}</span>
                        <span className='mx-0.5 text-ink-disabled'>/</span>
                        <span>{fmtOdds(f.odds_a)}</span>
                      </span>
                    )}
                    {/* 分析按钮 */}
                    {hasOdds && (
                      <button onClick={(e) => { e.stopPropagation(); onAnalyze(f.home, f.away, f.sport_key, {h:f.odds_h!, d:f.odds_d!, a:f.odds_a!}, undefined, undefined, undefined, isLive && hasScore ? {homeGoals:f.score_home!, awayGoals:f.score_away!, elapsed: typeof f.match_minute === 'number' ? f.match_minute : undefined} : undefined) }}
                        className='text-[9px] px-2 py-0.5 rounded font-bold text-accent bg-accent/10 hover:bg-accent/20 transition-colors flex-shrink-0'>
                        分析
                      </button>
                    )}
                  </div>
                )
              })}
              {baseList.length === 0 && <div className='py-8 text-center text-ink-muted text-xs'>暂无比赛</div>}
            </div>
          </div>
        )}

      </div>
    </div>
  )
}

// ═══ 右栏: 实时面板 (全部进行中比赛 + 首场波胆 + 实时计时器) ═══
function LivePanel({fixtures, now, activeLiveId, onSelectLiveId, selectedLeague, onAnalyze}:{fixtures:FixtureEntry[]; now?: number; activeLiveId?: string | null; onSelectLiveId?: (id:string) => void; selectedLeague?: string; onAnalyze?: (h:string,a:string,sportKey?:string,odds?:{h:number;d:number;a:number},handicap?:HandicapPayload,initialOdds?:{h:number;d:number;a:number},initialHandicap?:HandicapPayload,liveScore?:{homeGoals:number;awayGoals:number;elapsed?:number})=>void}) {
  // 进行中比赛 (按状态码降序 → 同状态按开赛时间升序); 选中联赛时只取该联赛
  const liveAll = fixtures
    .filter(f => Number(f.match_state) > 0)
    .filter(f => !selectedLeague || f.sport_key === selectedLeague)
    .sort((a, b) => {
      const sa = Number(a.match_state) || 0, sb = Number(b.match_state) || 0
      if (sa !== sb) return sb - sa
      return new Date(a.commence_time).getTime() - new Date(b.commence_time).getTime()
    })
  // 优先使用用户选中的比赛; 若已非 live 或找不到则回退首场
  const activeFromId = activeLiveId ? liveAll.find(f => (f.id || `${f.home}-${f.away}`) === activeLiveId) : undefined
  const live = activeFromId || liveAll[0]
  const rest = liveAll.slice(1)
  // 实时计时器 (Req1) — 跟踪当前选中比赛
  const liveMs = live ? Number(live.match_state ?? 0) : 0
  const liveMin = useLiveMinute(live?.match_minute, liveMs)

  // 拉取当前比赛的波胆赔率 (用于右栏 6x6 网格)
  const [csOdds, setCsOdds] = useState<Record<string, number>>({})
  const [csLoading, setCsLoading] = useState(false)
  useEffect(() => {
    if (!live) { setCsOdds({}); return }
    const fid = live.id || `${live.home}-${live.away}`
    let cancelled = false
    const fetchOdds = async () => {
      setCsLoading(true)
      try {
        const res = await terminalService.analyze(
          live.home, live.away, live.sport_key || 'soccer',
          live.odds_h !== undefined ? {h: live.odds_h!, d: live.odds_d!, a: live.odds_a!} : undefined,
          buildHandicap(live),
          (typeof live.score_home === 'number' && typeof live.score_away === 'number')
            ? {homeGoals: live.score_home!, awayGoals: live.score_away!, elapsed: typeof live.match_minute === 'number' ? live.match_minute : undefined}
            : undefined
        )
        const payload = (res.data as any)?.data
        const map: Record<string, number> = {}
        // 优先取庄家实时 CS 赔率时间线
        const tl = payload?.oip?.cs_odds_timeline
        if (tl?.live) {
          Object.entries(tl.live).forEach(([score, odds]) => {
            const o = Number(odds)
            if (o > 0) map[score] = o
          })
        }
        // fallback: sub_markets.correct_score fair_decimal
        const rows = payload?.sub_markets?.correct_score?.rows
        if (rows && Object.keys(map).length === 0) {
          rows.forEach((r: any) => {
            if (r.score && typeof r.fair_decimal === 'number' && r.fair_decimal > 0) {
              map[r.score] = r.fair_decimal
            }
          })
        }
        if (!cancelled) setCsOdds(map)
      } catch (e) {
        // 静默失败, 网格显示 ——
      } finally {
        if (!cancelled) setCsLoading(false)
      }
    }
    fetchOdds()
    const timer = setInterval(fetchOdds, 15000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [live?.id, live?.home, live?.away])

  if (!live) {
    return (
      <div className='w-[280px] flex-shrink-0 bg-surface-panel border-l border-surface-border flex flex-col items-center justify-center text-center'>
        <div className='w-12 h-12 rounded-full bg-surface-hover border border-surface-border flex items-center justify-center mb-3'>
          <span className='text-ink-muted text-[20px]'>⏱</span>
        </div>
        <p className='text-[11px] text-ink-secondary'>暂无进行中的比赛</p>
        <p className='text-[9px] text-ink-muted mt-1'>有比赛开打时, 这里会显示比分和波胆</p>
      </div>
    )
  }
  const liveHasScore = typeof live.score_home === 'number' && typeof live.score_away === 'number'
  const isHomeLead = liveHasScore && live.score_home! > live.score_away!
  const isAwayLead = liveHasScore && live.score_away! > live.score_home!
  return (
    <div className='w-[280px] flex-shrink-0 bg-surface-panel border-l border-surface-border flex flex-col'>
      {/* 比赛头: 比分高亮 */}
      <div className='px-3 py-2 bg-gradient-to-r from-frost-700 to-frost-500 text-white'>
        <div className='flex items-center justify-between text-[10px] mb-1.5'>
          <span className='px-1.5 py-0.5 bg-white/20 rounded font-bold'>{live.league || '其他'}</span>
          <span className='text-[8px] flex items-center gap-1'>
            <span className='w-1 h-1 rounded-full bg-danger-500 animate-pulse'/>
            LIVE{liveAll.length > 1 ? ` +${liveAll.length - 1}` : ''}
          </span>
        </div>
        <div className='flex items-center justify-between mb-1'>
          <span className={`text-[12px] font-bold ${isHomeLead?'text-white':'text-white/80'}`}>{live.home}</span>
          <span className='flex items-center gap-1 text-[18px] font-black font-mono'>
            {liveHasScore ? (
              <>
                <span className={isHomeLead?'text-accent':'text-white'}>{live.score_home}</span>
                <span className='opacity-50 text-[12px]'>-</span>
                <span className={isAwayLead?'text-accent':'text-white'}>{live.score_away}</span>
              </>
            ) : (
              <span className='text-white/50 text-[14px] font-medium'>— : —</span>
            )}
          </span>
          <span className={`text-[12px] font-bold ${isAwayLead?'text-white':'text-white/80'}`}>{live.away}</span>
        </div>
        <div className='text-[10px] text-center text-white/70 font-mono'>
          {formatMatchTime(liveMin, liveMs === 2)}
        </div>
      </div>
      {/* 全场波胆 6x6 网格 */}
      <div className='px-3 py-2.5'>
        <div className='flex items-center justify-between mb-1.5'>
          <span className='text-[10px] text-accent font-bold'>全场波胆</span>
          <span className='text-[9px] text-ink-muted'>[全场波胆] ×</span>
        </div>
        <div className='grid grid-cols-6 gap-1 text-[9px]'>
          {Array.from({length:6}).map((_,r) =>
            Array.from({length:6}).map((_,c) => {
              const isCurrent = liveHasScore && r === live.score_home && c === live.score_away
              const scoreKey = `${r}-${c}`
              const odds = csOdds[scoreKey]
              const hasOdds = typeof odds === 'number' && odds > 0
              return (
                <div key={`${r}-${c}`} className={`flex flex-col items-center py-1.5 rounded transition-colors ${
                  isCurrent
                    ? 'bg-gradient-to-br from-field-300/30 to-frost-400/30 border border-field-300/60 text-accent font-bold'
                    : hasOdds
                      ? 'bg-surface-hover border border-surface-border/70 text-white'
                      : 'bg-surface-hover border border-surface-border/70 text-ink-muted'
                }`}>
                  <span className={`text-[9px] ${isCurrent?'text-accent font-bold':'text-ink-secondary'}`}>{r}:{c}</span>
                  <span className={`text-[9px] font-mono ${isCurrent?'text-accent':hasOdds?'text-white font-bold':'text-ink-muted'}`}>
                    {csLoading && !hasOdds ? '…' : hasOdds ? odds.toFixed(2) : '——'}
                  </span>
                </div>
              )
            })
          )}
        </div>
      </div>
      {/* 其余进行中比赛列表 (可滚动) */}
      {rest.length > 0 && (
        <div className='flex-1 flex flex-col min-h-0'>
          <div className='px-3 py-1.5 border-t border-surface-border/50 flex items-center justify-between'>
            <span className='text-[10px] text-accent font-bold'>其他进行中</span>
            <span className='text-[9px] px-1.5 py-0.5 bg-field-500/15 text-field-400 rounded font-bold'>{rest.length} 场</span>
          </div>
          <div className='flex-1 overflow-y-auto px-2 pb-2 space-y-1'>
            {rest.map(f => {
              const parseMin = (m: any): number | null => {
                const s = String(m ?? '').replace(/[′'"]/g, '')
                if (s === 'HT' || s === '中场') return 45
                const n = parseInt(s, 10)
                return isNaN(n) ? null : n
              }
              const min = parseMin(f.match_minute)
              const fHasScore = typeof f.score_home === 'number' && typeof f.score_away === 'number'
              const homeLead = fHasScore && f.score_home! > f.score_away!
              const awayLead = fHasScore && f.score_away! > f.score_home!
              const fid = f.id || `${f.home}-${f.away}`
              return (
                <div
                  key={fid}
                  onClick={() => onSelectLiveId?.(fid)}
                  className={`px-2 py-1.5 rounded border transition-colors cursor-pointer ${
                    activeLiveId === fid
                      ? 'bg-accent/15 border-accent/40'
                      : 'bg-surface-hover/40 border-surface-border/40 hover:bg-surface-hover/70'
                  }`}>
                  <div className='flex items-center gap-1 mb-0.5'>
                    <span className='w-1 h-1 rounded-full bg-field-500 animate-pulse'/>
                    <span className='text-[9px] text-ink-muted truncate flex-1'>{f.league || ''}</span>
                    {min != null && <span className='text-[9px] text-field-400 font-mono font-bold'>{min}'</span>}
                  </div>
                  <div className='flex items-center justify-between gap-1.5'>
                    <span className={`text-[10px] truncate flex-1 text-right ${homeLead?'text-field-300 font-bold':'text-white/90'}`}>{f.home}</span>
                    <span className='text-[12px] font-black font-mono tabular-nums text-white whitespace-nowrap'>
                      {fHasScore ? `${f.score_home}-${f.score_away}` : '— : —'}
                    </span>
                    <span className={`text-[10px] truncate flex-1 ${awayLead?'text-field-300 font-bold':'text-white/90'}`}>{f.away}</span>
                    {onAnalyze && typeof f.odds_h === 'number' && typeof f.odds_d === 'number' && typeof f.odds_a === 'number' && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          onAnalyze(
                            f.home, f.away, f.sport_key || f.league,
                            { h: f.odds_h as number, d: f.odds_d as number, a: f.odds_a as number },
                            undefined, undefined, undefined,
                            (typeof f.score_home === 'number' && typeof f.score_away === 'number')
                              ? { homeGoals: f.score_home, awayGoals: f.score_away, elapsed: typeof f.match_minute === 'number' ? f.match_minute : (parseMin(f.match_minute) ?? 0) }
                              : undefined
                          )
                        }}
                        className='flex-shrink-0 ml-1 px-1.5 py-0.5 rounded text-[9px] bg-gradient-to-r from-frost-500 to-frost-600 text-white font-bold hover:opacity-90 transition-opacity'
                      >分析</button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

// ═══ 主页面 ═══
export default function LeagueSchedule() {
  const [catalog, setCatalog] = useState<LeaguesResponse|null>(null)
  const [fixtures, setFixtures] = useState<FixtureEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [analyze, setAnalyze] = useState<{home:string;away:string;sportKey?:string;odds?:{h:number;d:number;a:number};handicap?:HandicapPayload;initialOdds?:{h:number;d:number;a:number};initialHandicap?:HandicapPayload;liveScore?:{homeGoals:number;awayGoals:number;elapsed?:number}} | null>(null)
  const [selectedLeague, setSelectedLeague] = useState<string>('')  // '' = 全部 (联赛默认)
  const [viewMode, setViewMode] = useState<'league' | 'timeline'>('league')
  const [updatedAt, setUpdatedAt] = useState<Date|null>(null)
  const [now, setNow] = useState<number>(() => Date.now())
  // 展开状态 (Req4: 每场比赛可展开/收起波胆详情)
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())
  // 右栏实时面板当前选中比赛 (默认 null = 自动取首场进行中)
  const [activeLiveId, setActiveLiveId] = useState<string | null>(null)

  const onAnalyze = useCallback((h:string,a:string,sportKey?:string,odds?:{h:number;d:number;a:number},handicap?:HandicapPayload,initialOdds?:{h:number;d:number;a:number},initialHandicap?:HandicapPayload,liveScore?:{homeGoals:number;awayGoals:number;elapsed:number}) => setAnalyze({home:h,away:a,sportKey,odds,handicap,initialOdds,initialHandicap,liveScore}),[])
  const toggleExpand = useCallback((id: string) => {
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }, [])

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const res = await leagueScheduleService.getLeagues()
      const d = (res.data as any)?.data || res.data
      setCatalog(d)
      // 抓所有有赛程的联赛的赛程
      const allLeagues: {name:string; fixture_count:number; sport_key:string}[] = []
      for (const cat of (d?.categories || [])) {
        for (const lg of cat.leagues || []) {
          allLeagues.push({name: lg.name, fixture_count: lg.fixture_count, sport_key: lg.sport_key})
        }
      }
      // 选头几个有赛程的联赛拉详情 (并行抓取, 上限提到 20 覆盖更多直播比赛)
      const toFetch = allLeagues.filter(l => l.fixture_count > 0).slice(0, 20)
      const results = await Promise.all(toFetch.map(lg =>
        leagueScheduleService.getFixtures(lg.sport_key)
          .then(r2 => {
            const d2 = (r2.data as any)?.data || r2.data
            return (d2?.fixtures || []).map((f: any) => ({ ...f, sport_key: lg.sport_key })) as FixtureEntry[]
          }).catch(() => [] as FixtureEntry[])
      ))
      setFixtures(results.flat())
      setUpdatedAt(new Date())
    } catch {} finally { setLoading(false) }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  // 30s 自动刷新数据
  useEffect(() => {
    const timer = setInterval(refresh, 30000)
    return () => clearInterval(timer)
  }, [refresh])

  // 5s 轻量比分轮询 (叠加在 30s 全量刷新上): 用 /api/live-scores 合并最新比分
  // 降级安全: 后台采集线程未跑 / 当前无直播 → 返回空数组 → 不合并, 页面仍正常。
  useEffect(() => {
    const id = setInterval(async () => {
      try {
        const res = await liveScoreService.getLiveMatches(50)
        const arr: LiveScoreMatch[] | undefined = (res.data as any)?.data?.matches
        if (!arr || arr.length === 0) return
        const key = (h: string, a: string) => `${h}|${a}`
        const map = new Map(arr.map(m => [key(m.home, m.away), m]))
        setFixtures(prev => prev.map(f => {
          const m = map.get(key(f.home, f.away))
          if (!m) return f
          // 仅在有实际变化时更新 (避免无谓重渲)
          if (f.score_home === m.score_home && f.score_away === m.score_away
              && f.match_minute === m.match_minute && f.match_state === m.mststi) return f
          return {
            ...f,
            score_home: m.score_home,
            score_away: m.score_away,
            match_minute: m.match_minute,
            match_state: m.mststi ?? f.match_state,
          }
        }))
      } catch { /* 轮询失败静默, 30s 全量刷新会兜底 */ }
    }, 5000)
    return () => clearInterval(id)
  }, [])

  // 1s 实时时钟 (倒计时/开赛同步)
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  // 提取所有联赛
  const allLeagues: {name:string;fixture_count:number;sport_key:string}[] = []
  if (catalog?.categories) {
    for (const cat of catalog.categories) {
      for (const lg of cat.leagues) {
        allLeagues.push({name: lg.name, fixture_count: lg.fixture_count, sport_key: lg.sport_key})
      }
    }
  }

  return (
    <div className='flex flex-col h-screen bg-surface-canvas overflow-hidden'>
      <div className='flex flex-1 overflow-hidden'>
        <LeagueFilter
          allLeagues={allLeagues}
          selectedLeague={selectedLeague}
          onSelectLeague={setSelectedLeague}
        />
        <LeagueSchedulePanel
          fixtures={fixtures}
          allLeagues={allLeagues}
          onAnalyze={onAnalyze}
          selectedLeague={selectedLeague}
          onSelectLeague={setSelectedLeague}
          viewMode={viewMode}
          onViewModeChange={setViewMode}
          now={now}
          expandedIds={expandedIds}
          onToggleExpand={toggleExpand}
          activeLiveId={activeLiveId}
          onSelectLiveId={setActiveLiveId}
        />
        <LivePanel fixtures={fixtures} now={now} activeLiveId={activeLiveId} onSelectLiveId={setActiveLiveId} selectedLeague={selectedLeague} onAnalyze={onAnalyze}/>
      </div>

      {analyze && (
        <MatchAnalysisModal
          home={analyze.home} away={analyze.away} sportKey={analyze.sportKey || 'soccer'}
          odds={analyze.odds} handicap={analyze.handicap}
          initialOdds={analyze.initialOdds} initialHandicap={analyze.initialHandicap}
          liveScore={analyze.liveScore}
          onClose={()=>setAnalyze(null)}/>
      )}
    </div>
  )
}

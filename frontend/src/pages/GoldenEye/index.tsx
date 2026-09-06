import { useState, useCallback, useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  goldenEyeService,
  unwrapData,
  type GoldenEyeItem,
  type GoldenEyeScoreLine,
  type GoldenEyeLivePayload,
} from '@/services/api'
import PageHeader from '@/components/layout/PageHeader'

// ═══ 辅助格式化 ═══
const pct = (v: number | undefined | null, digits = 1) =>
  typeof v === 'number' && !isNaN(v) ? (v * 100).toFixed(digits) + '%' : '—'
const pp = (v: number | undefined | null) =>
  typeof v === 'number' && !isNaN(v) ? (v > 0 ? '+' : '') + v.toFixed(1) + 'pp' : '—'
const sideLabel = (s?: string) =>
  s === 'H' ? '主胜' : s === 'D' ? '平局' : s === 'A' ? '客胜' : (s ?? '—')
const priceLabel = (s?: string) =>
  s === 'opening_gq' ? '开盘(GQ)' : s === 'provided' ? '即时' : s === 'none' ? '无盘' : s === 'error' ? '异常' : (s ?? '—')
const formatKickoff = (s?: string | null) => {
  if (!s || s.trim() === '') return null
  const d = new Date(s.replace(' ', 'T'))
  if (isNaN(d.getTime())) return s
  return d.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).replace(/\//g, '-')
}

function ProbBar({ p, color }: { p: number; color: string }) {
  return (
    <div className="h-1.5 rounded-full bg-ink-primary/[0.08] overflow-hidden flex-1">
      <div className={`h-full rounded-full ${color}`} style={{ width: `${Math.min(100, Math.max(2, p * 100))}%` }} />
    </div>
  )
}

function ScoreDistPanel({ dist, total }: { dist: GoldenEyeScoreLine[]; total?: number }) {
  if (!dist || dist.length === 0) return <div className="text-xs text-ink-muted">无比分分布</div>
  const top = dist.slice(0, 6)
  return (
    <div className="space-y-1.5">
      {top.map((s) => (
        <div key={s.score} className="flex items-center gap-2 text-xs">
          <span className="text-ink-muted w-9 flex-shrink-0 tabular-nums">{s.score}</span>
          <ProbBar p={s.prob} color="bg-ember-400" />
          <span className="text-ink-primary w-12 text-right flex-shrink-0 tabular-nums">{pct(s.prob)}</span>
        </div>
      ))}
      {typeof total === 'number' && (
        <div className="text-[10px] text-ink-disabled pt-0.5">前{top.length}项合计 {pct(total)} · 诚实分布, 非准确比分 (IR-30)</div>
      )}
    </div>
  )
}

// ═══ 全场扫描卡片 (+EV 信号优先) ═══
function MatchCard({ item, onSelect }: { item: GoldenEyeItem; onSelect: (item: GoldenEyeItem) => void }) {
  const ty = item.tianyan
  const we = item.world?.edge_1x2
  const hasEV = (ty.edge_pp ?? 0) > 0 || (we != null && (we.edge_pp ?? 0) >= 1)
  return (
    <button
      onClick={() => onSelect(item)}
      className="text-left w-full bg-accent-card border border-surface-border rounded-xl p-4 transition-colors hover:border-field-500/30"
    >
      <div className="flex items-center justify-between gap-2">
        <div className="text-[14px] font-semibold text-ink-primary">
          {item.home} <span className="text-ink-muted font-normal">vs</span> {item.away}
        </div>
        {item.divergence?.level === 'hard' ? (
          <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-amber-500/15 text-amber-300 flex-shrink-0">⚠️ 分歧·按世界级裁定</span>
        ) : hasEV ? (
          <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-amber-500/15 text-amber-300 flex-shrink-0">+EV 信号</span>
        ) : null}
      </div>
      <div className="text-[11px] text-ink-muted mt-0.5">
        {item.league || '无联赛'}
        {item.kickoff && ` · 开赛 ${formatKickoff(item.kickoff)}`}
        {' · 价源 '}{priceLabel(item.price_source)}
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-1 mt-2 text-xs">
        <div className="text-ink-muted">
          天眼:{' '}
          {ty.ok ? (
            <span className={ty.edge_pp && ty.edge_pp > 0 ? 'text-emerald-400 font-medium' : 'text-ink-primary'}>
              {sideLabel(ty.side)} {pp(ty.edge_pp)}
            </span>
          ) : (
            <span className="text-ink-disabled">PASS 未覆盖</span>
          )}
        </div>
        <div className="text-ink-muted">
          世界:{' '}
          {we && we.edge_pp != null ? (
            <span className={we.edge_pp >= 1 ? 'text-emerald-400 font-medium' : 'text-ink-primary'}>
              {sideLabel(we.side)} {pp(we.edge_pp)}
            </span>
          ) : (
            <span className="text-ink-disabled">诊断 n/a</span>
          )}
        </div>
      </div>
      {item.divergence?.level === 'soft' && (
        <div className="text-[10px] text-amber-300/90 mt-1">⚠️ 两镜分歧(soft): {item.divergence.note}</div>
      )}
      <div className="mt-2">
        <div className="text-[10px] text-ink-disabled mb-1">终场比分分布 (top3)</div>
        <ScoreDistPanel dist={item.score_dist.slice(0, 3)} />
      </div>
    </button>
  )
}

// ═══ 单场深读 (三镜头 + 比分分布) ═══
function DeepDive({ item, priceSource, onBack }: { item: GoldenEyeItem; priceSource?: string; onBack?: () => void }) {
  const ty = item.tianyan
  const we = item.world?.edge_1x2
  const hasEV = (ty.edge_pp ?? 0) > 0 || (we != null && (we.edge_pp ?? 0) >= 1)
  const wou = item.world?.ou
  const ou = item.ou
  return (
    <div className="space-y-3">
      <div
      className="bg-accent-card border border-surface-border rounded-xl px-4 py-3 flex items-center justify-between flex-wrap gap-2"
      >
        <div>
          {onBack && (
            <button
              onClick={onBack}
              className="group flex items-center gap-1 text-[11px] text-ink-muted hover:text-field-400 transition-colors mb-1"
            >
              <svg className="w-3.5 h-3.5 transition-transform group-hover:-translate-x-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5L8.25 12l7.5-7.5" />
              </svg>
              返回全场扫描
            </button>
          )}
          <div className="text-[15px] font-semibold text-ink-primary">
            {item.home} <span className="text-ink-muted font-normal">vs</span> {item.away}
          </div>
          <div className="text-[11px] text-ink-muted mt-0.5">
            {item.league || '无联赛'}
            {item.kickoff && ` · 开赛 ${formatKickoff(item.kickoff)}`}
            {' · 价源 '}{priceLabel(item.price_source || priceSource)}
            {' · 世界级 v'}{item.world?.version || '—'}
          </div>
        </div>
        {hasEV ? (
          <span className="px-2 py-1 rounded-md bg-amber-500/15 text-amber-300 text-xs font-medium">+EV 信号</span>
        ) : (
          <span className="px-2 py-1 rounded-md bg-surface-dark/60 text-ink-muted text-xs">无 +EV 信号</span>
        )}
      </div>

      {/* 两镜分歧横幅 (诚实边界 IR-21/IR-30) */}
      {item.divergence?.conflict && (
        <div className={`bg-accent-card border rounded-xl p-3 ${
          item.divergence.level === 'hard' ? 'border-amber-500/50 bg-amber-500/[0.06]' : 'border-amber-500/30'
        }`}>
          <div className="flex items-center gap-2 text-[13px] font-semibold text-amber-300">
            <span>⚠️ 两镜分歧</span>
            <span className="text-[10px] font-normal px-1.5 py-0.5 rounded bg-amber-500/15">
              {item.divergence.level === 'hard' ? '方向相反·各自+EV·不自动标 +EV 信号' : '方向不一致·以天眼+EV为准'}
            </span>
          </div>
          <div className="mt-1.5 text-[11px] text-ink-secondary leading-snug">
            天眼 → <span className="font-medium text-ink-primary">{sideLabel(item.divergence.tianyan_side ?? undefined)}</span>
            <span className="text-ember-400"> {pp(item.divergence.tianyan_edge_pp)}</span>
            {'  ·  '}
            世界级 → <span className="font-medium text-ink-primary">{sideLabel(item.divergence.world_side ?? undefined)}</span>
            <span className="text-ember-400"> {pp(item.divergence.world_edge_pp)}</span>
          </div>
          <div className="mt-1 text-[10px] text-ink-muted">{item.divergence.note}</div>
        </div>
      )}

      <div className="grid md:grid-cols-3 gap-3">
        {/* ① 天眼 +EV裁判 */}
        <div className={`bg-accent-card border rounded-xl p-4 ${ty.ok && ty.edge_pp && ty.edge_pp > 0 ? 'border-emerald-500/50' : 'border-surface-border'}`}>
          <div className="text-[13px] font-semibold text-ink-primary mb-2">① 天眼 +EV裁判</div>
          {ty.ok ? (
            <div className="space-y-2 text-xs">
              <div className="flex items-center gap-2">
                <span className="text-ink-muted">方向</span>
                <span className="text-ink-primary font-medium">{sideLabel(ty.side)}</span>
                <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${ty.edge_pp && ty.edge_pp > 0 ? 'bg-emerald-500/15 text-emerald-400' : 'bg-surface-dark/60 text-ink-muted'}`}>
                  {ty.edge_pp && ty.edge_pp > 0 ? '正EV' : '无edge'}
                </span>
              </div>
              <div className="grid grid-cols-3 gap-2 text-center">
                <div className="bg-accent-inner rounded-md py-1.5"><div className="text-[10px] text-ink-muted">模型概率</div><div className="text-ink-primary">{pct(ty.model_prob, 2)}</div></div>
                <div className="bg-accent-inner rounded-md py-1.5"><div className="text-[10px] text-ink-muted">隐含</div><div className="text-ink-primary">{pct(ty.market_implied, 2)}</div></div>
                <div className="bg-accent-inner rounded-md py-1.5"><div className="text-[10px] text-ink-muted">edge</div><div className={ty.edge_pp && ty.edge_pp > 0 ? 'text-emerald-400' : 'text-danger-400'}>{pp(ty.edge_pp)}</div></div>
              </div>
              {ty.compliant && <div className="text-[10px] text-ink-disabled">{ty.compliant}</div>}
            </div>
          ) : (
            <div className="text-xs text-ink-muted">{ty.reason || '天眼未覆盖, PASS (IR-30)'}</div>
          )}
        </div>

        {/* ② 世界级诊断 */}
        <div className={`bg-accent-card border rounded-xl p-4 ${we && we.edge_pp != null && we.edge_pp >= 1 ? 'border-emerald-500/50' : 'border-surface-border'}`}>
          <div className="text-[13px] font-semibold text-ink-primary mb-2">② 世界级诊断</div>
          {we && we.edge_pp != null ? (
            <div className="space-y-2 text-xs">
              <div className="text-ink-muted">倾向 <span className="text-field-400 font-medium">{item.world?.lean || sideLabel(we.side)}</span></div>
              <div className="grid grid-cols-3 gap-2 text-center">
                <div className="bg-accent-inner rounded-md py-1.5"><div className="text-[10px] text-ink-muted">win_rate</div><div className="text-ink-primary">{pct(we.win_rate, 2)}</div></div>
                <div className="bg-accent-inner rounded-md py-1.5"><div className="text-[10px] text-ink-muted">implied</div><div className="text-ink-primary">{pct(we.implied, 2)}</div></div>
                <div className="bg-accent-inner rounded-md py-1.5"><div className="text-[10px] text-ink-muted">edge</div><div className={we.edge_pp >= 1 ? 'text-emerald-400' : 'text-ink-primary'}>{pp(we.edge_pp)}</div></div>
              </div>
              <div className="text-[10px] text-ink-disabled">诊断层读数, 标 +EV 信号框仅天眼判定 (IR-20 分析非预测)</div>
            </div>
          ) : (
            <div className="text-xs text-ink-muted">世界级 Edge 三件套不可用</div>
          )}
        </div>

        {/* ③ 赛程OU / 盘口 */}
        <div className="bg-accent-card border border-surface-border rounded-xl p-4">
          <div className="text-[13px] font-semibold text-ink-primary mb-2">③ 赛程OU / 盘口</div>
          <div className="space-y-2 text-xs text-ink-muted">
            {wou && wou.model_p_over != null ? (
              <div>
                模型 P(大) <span className="text-ink-primary">{pct(wou.model_p_over)}</span> vs 市场{' '}
                <span className="text-ink-primary">{pct(wou.market_p_over)}</span> →{' '}
                <span className="font-medium">{wou.lean}</span> ({pp(wou.edge_pp)})
              </div>
            ) : (
              <div>OU 模型分歧: 全局无 edge (长期真相)</div>
            )}
            {ou ? (
              <div className="pt-1 space-y-0.5">
                {ou.ah_line != null && (
                  <div>
                    AH 线 {ou.ah_line} · 主 {ou.ah_home != null ? ou.ah_home : '—'}
                    {ou.ah_away != null ? ` / 客 ${ou.ah_away}` : ''}
                  </div>
                )}
                {ou.ou_line != null && (
                  <div>
                    OU 线 {ou.ou_line} · 大 {ou.ou_over != null ? ou.ou_over : '—'}
                    {ou.ou_under != null ? ` / 小 ${ou.ou_under}` : ''}
                  </div>
                )}
                {ou.ah_line == null && ou.ou_line == null && <div>无盘口线数据</div>}
              </div>
            ) : (
              <div className="pt-1">无盘口线 (单场模式)</div>
            )}
          </div>
        </div>
      </div>

      {/* 终场比分概率分布 */}
      <div className="bg-accent-card border border-surface-border rounded-xl p-4">
        <div className="flex items-center justify-between mb-2">
          <div className="text-[13px] font-semibold text-ink-primary">终场比分概率分布</div>
          <span className="text-[10px] text-ink-disabled">诚实分布, 非准确比分 (IR-30)</span>
        </div>
        <ScoreDistPanel dist={item.score_dist} total={item.score_total} />
      </div>

    </div>
  )
}

// ═══ 页面 ═══
// 支持 query 参数回填 (赛程页 "黄金神瞳 →" 跳转): ?home=&away=&league=&h=&d=&a=
export default function GoldenEye() {
  const [sp] = useSearchParams()
  const qv = (k: string) => sp.get(k) ?? ''
  const [home, setHome] = useState(qv('home'))
  const [away, setAway] = useState(qv('away'))
  const [league, setLeague] = useState(qv('league'))
  const [h, setH] = useState(qv('h'))
  const [d, setD] = useState(qv('d'))
  const [a, setA] = useState(qv('a'))

  const [single, setSingle] = useState<GoldenEyeItem | null>(null)
  const [singleSource, setSingleSource] = useState<string | undefined>(undefined)
  const [live, setLive] = useState<GoldenEyeItem[] | null>(null)
  const [liveMeta, setLiveMeta] = useState<{ count: number; covered: number }>({ count: 0, covered: 0 })
  const [loadingSingle, setLoadingSingle] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [error, setError] = useState('')
  const liveListRef = useRef<GoldenEyeItem[] | null>(null)

  const num = (s: string) => (s.trim() === '' ? undefined : Number(s))

  const analyze = useCallback(async () => {
    if (!home.trim() || !away.trim()) {
      setError('请填写主队与客队')
      return
    }
    setLoadingSingle(true)
    setError('')
    setLive(null)
    try {
      const res = await goldenEyeService.analyze({
        home: home.trim(),
        away: away.trim(),
        sport_key: '',
        odds_h: num(h),
        odds_d: num(d),
        odds_a: num(a),
      })
      const payload = unwrapData<{ result: GoldenEyeItem; price_source?: string; reason?: string }>(res)
      if (payload && payload.result) {
        setSingle(payload.result as GoldenEyeItem)
        setSingleSource(payload.price_source)
      } else if (payload && payload.reason) {
        setSingle(null)
        setError(payload.reason)
      } else {
        setSingle(null)
        setError('黄金神瞳无返回')
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '分析失败')
    } finally {
      setLoadingSingle(false)
    }
  }, [home, away, h, d, a])

  const scan = useCallback(async () => {
    setScanning(true)
    setError('')
    setSingle(null)
    try {
      const res = await goldenEyeService.live()
      const payload = unwrapData<GoldenEyeLivePayload>(res)
      if (payload && Array.isArray(payload.result)) {
        setLive(payload.result as GoldenEyeItem[])
        setLiveMeta({ count: payload.count ?? payload.result.length, covered: payload.tianyan_covered ?? 0 })
      } else {
        setLive([])
        setLiveMeta({ count: 0, covered: 0 })
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '扫描失败')
    } finally {
      setScanning(false)
    }
  }, [])

  // query 回填 (赛程页跳转) → mount 后自动分析; 防重复只跑一次
  const autoRan = useRef(false)
  useEffect(() => {
    if (!autoRan.current && home.trim() && away.trim()) {
      autoRan.current = true
      analyze()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])



  const fillPreset = () => {
    setHome('托特纳姆热刺')
    setAway('阿森纳')
    setLeague('英超')
    setH('2.40')
    setD('3.40')
    setA('2.90')
  }

  const onSelectLive = (item: GoldenEyeItem) => {
    liveListRef.current = live
    setHome(item.home)
    setAway(item.away)
    setLeague(item.league ?? '')
    setSingle(item)
    setLive(null)
    setError('')
  }

  const backToList = () => {
    setSingle(null)
    setLive(liveListRef.current)
    liveListRef.current = null
  }

  const evCount = (live ?? []).filter((it) => {
    const t = it.tianyan?.edge_pp ?? 0
    const w = it.world?.edge_1x2?.edge_pp ?? 0
    return t > 0 || w >= 1
  }).length

  return (
    <div className="space-y-4">
      <PageHeader
        title="黄金神瞳"
        subtitle="天眼 +EV裁判 · 世界级诊断 · 终场比分分布 · 全场三镜头诊断"
        icon={
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5">
            <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 12s3.75-7.5 9.75-7.5 9.75 7.5 9.75 7.5-3.75 7.5-9.75 7.5S2.25 12 2.25 12z" />
            <circle cx="12" cy="12" r="3" />
          </svg>
        }
        metrics={[
          { label: '+EV 信号', value: evCount, accent: 'field' },
          { label: '天眼覆盖', value: liveMeta.covered },
        ]}
      />

      {/* 输入区 */}
      <div className="bg-accent-card border border-surface-border rounded-xl p-4 space-y-3">
        <div className="flex items-center gap-3 flex-wrap">
          <input
            value={home}
            onChange={(e) => setHome(e.target.value)}
            placeholder="主队 (必填)"
            className="px-3 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary placeholder:text-ink-disabled w-44"
          />
          <span className="text-ink-muted text-sm">VS</span>
          <input
            value={away}
            onChange={(e) => setAway(e.target.value)}
            placeholder="客队 (必填)"
            className="px-3 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary placeholder:text-ink-disabled w-44"
          />
          <input
            value={league}
            onChange={(e) => setLeague(e.target.value)}
            placeholder="联赛 (可选)"
            className="px-3 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary placeholder:text-ink-disabled w-36"
          />
        </div>
        <div className="flex items-center gap-2">
          <div className="text-[11px] text-ink-muted">1X2 赔率 (缺省自动回填空盘):</div>
          <input value={h} onChange={(e) => setH(e.target.value)} placeholder="主" className="px-2 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary w-20" />
          <input value={d} onChange={(e) => setD(e.target.value)} placeholder="平" className="px-2 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary w-20" />
          <input value={a} onChange={(e) => setA(e.target.value)} placeholder="客" className="px-2 py-1.5 rounded-md bg-accent-inner border border-surface-border text-sm text-ink-primary w-20" />
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <button
            onClick={analyze}
            disabled={loadingSingle}
            className="px-4 py-1.5 rounded-md bg-field-500 hover:bg-field-600 disabled:opacity-50 text-white text-sm font-medium transition-colors"
          >
            {loadingSingle ? '分析中…' : '单场分析'}
          </button>
          <button
            onClick={scan}
            disabled={scanning}
            className="px-4 py-1.5 rounded-md bg-field-500 hover:bg-field-600 disabled:opacity-50 text-white text-sm font-medium transition-colors"
          >
            {scanning ? '扫描中…' : '扫描全场机会'}
          </button>
          <button
            onClick={fillPreset}
            className="px-3 py-1.5 rounded-md bg-accent-inner border border-surface-border text-ink-secondary text-xs hover:text-ink-primary transition-colors"
          >
            填入示例
          </button>
          {error && <span className="text-danger-400 text-xs">{error}</span>}
        </div>
      </div>

      {/* 单场深读 */}
      {single && !error && <DeepDive item={single} priceSource={singleSource} onBack={liveListRef.current ? backToList : undefined} />}

      {/* 全场列表 */}
      {live && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <div className="text-[13px] font-semibold text-ink-primary">
              全场三镜头扫描 ({live.length} 场 · +EV 信号 {evCount})
            </div>
            <div className="text-[10px] text-ink-disabled">列表优先 · 仅天眼验证+EV 或世界级诊断≥1pp 标 +EV 信号</div>
          </div>
          {live.length === 0 ? (
            <div className="text-xs text-ink-muted bg-accent-card border border-surface-border rounded-xl p-4">当前无进行中比赛含 1X2 盘口</div>
          ) : (
            <div className="grid md:grid-cols-2 gap-3">
              {live.map((it) => (
                <MatchCard key={it.match_key || `${it.home}-${it.away}`} item={it} onSelect={onSelectLive} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

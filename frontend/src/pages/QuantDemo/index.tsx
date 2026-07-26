import { useState, useEffect, useCallback, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import ReactECharts from 'echarts-for-react'
import { quantService, portfolioService, type ScanSingleRequest } from '@/services/api'
import type {
  QuantSnapshot, QuantAccount, OptionValuation, ScanResult,
} from '@/types'
import PageHeader from '@/components/layout/PageHeader'

// ── 资金曲线 (echarts, 含峰值线 + 基准线) ──
function EquityChart({ curve }: { curve: { step: number; equity: number }[] }) {
  if (!curve || curve.length < 2) {
    return <div className="text-sm text-white/30 h-48 flex items-center justify-center">跑几步后显示资金曲线</div>
  }
  const eqs = curve.map((c) => c.equity)
  let peak = eqs[0]
  const peaks = eqs.map((v) => { peak = Math.max(peak, v); return peak })
  const base = eqs[0]
  const up = eqs[eqs.length - 1] >= base
  const option = {
    grid: { left: 50, right: 16, top: 16, bottom: 28 },
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(20,24,32,.95)', borderColor: 'rgba(255,255,255,.1)', textStyle: { color: '#c9d1d9', fontSize: 11 } },
    xAxis: { type: 'category', data: curve.map((c) => `#${c.step}`), axisLine: { lineStyle: { color: '#30363d' } }, axisLabel: { color: '#8b949e', fontSize: 9, maxItems: 8 } },
    yAxis: { type: 'value', scale: true, axisLine: { show: false }, splitLine: { lineStyle: { color: '#21262d' } }, axisLabel: { color: '#8b949e', fontSize: 9, formatter: (v: number) => '¥' + Math.round(v) } },
    series: [
      { name: '权益', type: 'line', data: eqs, smooth: true, symbol: 'none',
        lineStyle: { color: up ? '#3fb950' : '#f85149', width: 2 },
        areaStyle: { color: up ? 'rgba(63,185,80,.12)' : 'rgba(248,81,73,.12)' } },
      { name: '峰值', type: 'line', data: peaks, symbol: 'none',
        lineStyle: { color: '#58a6ff', width: 1, type: 'dashed', opacity: 0.5 } },
    ],
  }
  return <ReactECharts option={option} style={{ height: 192 }} />
}

// ── 回测绩效卡 (PortfolioManager 真实数据: 16140场, 139注, ROI+83.76%) ──
function PortfolioCard() {
  const [data, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    portfolioService.snapshot()
      .then((r: any) => {
        const d = r.data || r
        if (d && d.total_trades != null && d.total_trades > 0) setData(d)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  if (loading) return null
  if (!data) return null

  const snap = data.portfolio_snapshot || {}
  const ec = snap.equity_curve || []
  const eqs = ec.map((e: any) => e.equity)
  const notes = ec.map((e: any) => {
    const n = e.note || ''
    return n.length > 30 ? n.slice(0, 28) + '…' : n
  })

  // ECharts option (真实资金曲线)
  const up = eqs.length > 1 && eqs[eqs.length - 1] >= eqs[0]
  let peak = eqs[0] || 0
  const peaks = eqs.map((v: number) => { peak = Math.max(peak, v); return peak })
  const chartOption = {
    grid: { left: 50, right: 16, top: 16, bottom: 28 },
    tooltip: { trigger: 'axis',
      backgroundColor: 'rgba(20,24,32,.95)', borderColor: 'rgba(255,255,255,.1)',
      textStyle: { color: '#c9d1d9', fontSize: 11 },
      formatter: (params: any[]) => {
        const p = params[0]
        return `<div>#${p.dataIndex + 1} <b>¥${Math.round(p.value).toLocaleString()}</b><br/>
          <span style="color:#8b949e;font-size:10px">${notes[p.dataIndex] || ''}</span></div>`
      } },
    xAxis: { type: 'category', data: ec.map(() => ''), show: false },
    yAxis: { type: 'value', scale: true, axisLine: { show: false },
      splitLine: { lineStyle: { color: '#21262d' } },
      axisLabel: { color: '#8b949e', fontSize: 9, formatter: (v: number) => '¥' + Math.round(v) } },
    series: [
      { name: '权益', type: 'line', data: eqs, smooth: true, symbol: 'none',
        lineStyle: { color: up ? '#3fb950' : '#f85149', width: 2 },
        areaStyle: { color: up ? 'rgba(63,185,80,.12)' : 'rgba(248,81,73,.12)' } },
      { name: '峰值', type: 'line', data: peaks, symbol: 'none',
        lineStyle: { color: '#58a6ff', width: 1, type: 'dashed', opacity: 0.5 } },
    ],
  }

  return (
    <div className="p-4 rounded-xl border border-frost-500/20 bg-frost-500/[0.03]">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-frost-300">📊 回测绩效 (live_pilot_guardian 🛡️)</h3>
        <span className="text-xs text-frost-200/50">{data.total_trades} 注 · {ec.length} 点</span>
      </div>

      {/* 指标卡片 */}
      <div className="grid grid-cols-3 md:grid-cols-7 gap-2 mb-3">
        {[
          { l: '总ROI', v: `${data.total_roi_pct > 0 ? '+' : ''}${data.total_roi_pct}%`,
            c: (data.total_roi_pct || 0) >= 0 ? 'text-field-400' : 'text-danger-400' },
          { l: '夏普', v: data.sharpe_ratio?.toFixed(2) || 'N/A',
            c: (data.sharpe_ratio || 0) >= 0 ? 'text-field-400' : 'text-danger-400' },
          { l: '最大回撤', v: `${data.max_drawdown_pct}%`,
            c: (data.max_drawdown_pct || 0) > 20 ? 'text-danger-400' : 'text-ember-400' },
          { l: '卡玛', v: data.calmar_ratio?.toFixed(2) || 'N/A', c: 'text-frost-300' },
          { l: '胜率', v: `${data.win_rate_pct}%`, c: 'text-white/70' },
          { l: '盈亏比', v: data.profit_loss_ratio?.toFixed(2) || 'N/A', c: 'text-frost-300' },
          { l: 'EV/注', v: data.expected_value != null ? `¥${data.expected_value.toFixed(0)}` : 'N/A',
            c: (data.expected_value || 0) > 0 ? 'text-field-400' : 'text-danger-400' },
        ].map((x) => (
          <div key={x.l} className="p-2 rounded-lg border border-white/[0.06] bg-white/[0.02]">
            <div className="text-[10px] text-white/30">{x.l}</div>
            <div className={`text-base font-bold tabular-nums ${x.c}`}>{x.v}</div>
          </div>
        ))}
      </div>

      {/* 资金曲线 */}
      {eqs.length > 1 && <ReactECharts option={chartOption} style={{ height: 160 }} />}
    </div>
  )
}

// ── 价值扫描卡片 ──
function ScanCard({ s }: { s: ScanResult }) {
  const eg = s.expected_goals
  const bets = s.bet_candidates || []
  const [open, setOpen] = useState(bets.length > 0)
  const shown = open ? s.options : s.options.slice(0, 5)
  return (
    <div className="rounded-lg border border-white/[0.06] bg-white/[0.02] overflow-hidden">
      <button onClick={() => setOpen(!open)} className="w-full flex items-center justify-between px-3 py-2 hover:bg-white/[0.03]">
        <span className="text-sm font-medium text-white/80">
          {s.home} <span className="text-white/30 mx-1">vs</span> {s.away}
          {s.league && <span className="text-white/30 text-xs ml-2">{s.league}</span>}
        </span>
        <span className="flex items-center gap-2 text-xs">
          {eg && <span className="text-frost-400">λ{eg.total}</span>}
          {bets.length > 0
            ? <span className="text-field-400 font-semibold">★{bets.length}价值</span>
            : <span className="text-white/30">{s.is_multi_book ? '全跳过' : '单庄·仅分析'}</span>}
          <span className="text-white/30">{open ? '▾' : '▸'}</span>
        </span>
      </button>
      {open && shown.length > 0 && (
        <div className="border-t border-white/[0.04] py-1">
          {shown.map((o, i) => <OptionRow key={i} o={o} />)}
        </div>
      )}
    </div>
  )
}

// ── 决策状态中文化 (后端可能返 BET/PASS/EVAL/SCAN 四种) ──
const decisionLabel = (d?: string): string => {
  switch (d) {
    case 'BET': return '投注'
    case 'PASS': return '跳过'
    case 'EVAL': return '分析'
    case 'SCAN': return '扫描'
    default: return d || '未知'
  }
}

function OptionRow({ o }: { o: OptionValuation }) {
  const decCls = o.decision === 'BET' ? 'bg-field-500/20 text-field-400'
    : o.decision === 'EVAL' ? 'bg-ember-500/15 text-ember-400'
    : o.decision === 'SCAN' ? 'bg-white/[0.06] text-white/40'
    : 'bg-white/[0.04] text-white/25'
  const edgeCls = o.edge_pct > 0 ? 'text-field-400' : 'text-white/30'
  const isBest = o.decision === 'BET'
  return (
    <div className={`flex items-center justify-between px-3 py-1 text-xs ${isBest ? 'bg-field-500/5' : ''}`}>
      <span className="flex items-center gap-2">
        <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${decCls}`}>{decisionLabel(o.decision)}</span>
        <span className="text-white/70">{o.selection}</span>
        {o.odds != null && <span className="text-white/40">@{o.odds.toFixed(2)}</span>}
        <span className="text-white/25 text-[10px]">{o.market}</span>
      </span>
      <span className="flex items-center gap-3">
        <span className="text-white/30">P{o.model_prob.toFixed(3)}</span>
        <span className={`${edgeCls} tabular-nums w-16 text-right`}>edge{o.edge_pct >= 0 ? '+' : ''}{o.edge_pct.toFixed(1)}%</span>
      </span>
    </div>
  )
}

// ── 单场分析弹窗 ──
function AnalyzeModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [form, setForm] = useState({
    home: '法国', away: '西班牙', league: '世界杯', h: '2.26', d: '3.10', a: '2.75',
    hcpLine: '-1', hcpH: '5.05', hcpD: '3.85', hcpA: '1.49',
    ouLine: '2.5', ouOver: '1.90', ouUnder: '1.90',
  })
  const [busy, setBusy] = useState(false)
  const f = (k: string) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value })
  const run = async () => {
    setBusy(true)
    const body: ScanSingleRequest = {
      home: form.home, away: form.away, league: form.league,
      h: +form.h, d: +form.d, a: +form.a,
    }
    if (form.hcpLine) body.handicap_odds = { line: +form.hcpLine, home: +form.hcpH, draw: +form.hcpD, away: +form.hcpA }
    if (form.ouLine) body.ou_odds = { line: +form.ouLine, over: +form.ouOver, under: +form.ouUnder }
    try { await quantService.scanSingle(body) } finally { setBusy(false); onClose(); onDone() }
  }
  const inputCls = "bg-white/[0.04] border border-white/[0.08] rounded px-2 py-1 text-sm text-white/80 w-20 focus:border-field-500 outline-none"
  const labelCls = "text-[11px] text-white/40 mb-1"
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm" onClick={onClose}>
      <motion.div initial={{ opacity: 0, scale: 0.96 }} animate={{ opacity: 1, scale: 1 }}
        onClick={(e) => e.stopPropagation()}
        className="bg-[#161b22] border border-white/[0.08] rounded-xl p-6 w-[560px] max-h-[85vh] overflow-y-auto">
        <h3 className="text-lg font-bold text-white mb-1">🎯 单场全市场分析</h3>
        <p className="text-xs text-white/40 mb-4">输入比赛赔率 → 系统用 OIP 模型扫描全市场价值(波胆/让球/大小球)</p>
        <div className="grid grid-cols-3 gap-3 mb-3">
          <div><div className={labelCls}>主队</div><input className={`${inputCls} w-full`} value={form.home} onChange={f('home')} /></div>
          <div><div className={labelCls}>客队</div><input className={`${inputCls} w-full`} value={form.away} onChange={f('away')} /></div>
          <div><div className={labelCls}>联赛</div><input className={`${inputCls} w-full`} value={form.league} onChange={f('league')} /></div>
        </div>
        <div className="grid grid-cols-3 gap-3 mb-4">
          <div><div className={labelCls}>主胜赔率</div><input className={inputCls} value={form.h} onChange={f('h')} /></div>
          <div><div className={labelCls}>平局赔率</div><input className={inputCls} value={form.d} onChange={f('d')} /></div>
          <div><div className={labelCls}>客胜赔率</div><input className={inputCls} value={form.a} onChange={f('a')} /></div>
        </div>
        <div className="text-xs text-white/40 mb-2">让球盘 (可选)</div>
        <div className="grid grid-cols-4 gap-2 mb-4">
          <div><div className={labelCls}>让球数</div><input className={inputCls} value={form.hcpLine} onChange={f('hcpLine')} /></div>
          <div><div className={labelCls}>主胜(让)</div><input className={inputCls} value={form.hcpH} onChange={f('hcpH')} /></div>
          <div><div className={labelCls}>平(让)</div><input className={inputCls} value={form.hcpD} onChange={f('hcpD')} /></div>
          <div><div className={labelCls}>客胜(让)</div><input className={inputCls} value={form.hcpA} onChange={f('hcpA')} /></div>
        </div>
        <div className="text-xs text-white/40 mb-2">大小球 (可选)</div>
        <div className="grid grid-cols-3 gap-2 mb-5">
          <div><div className={labelCls}>盘口</div><input className={inputCls} value={form.ouLine} onChange={f('ouLine')} /></div>
          <div><div className={labelCls}>大球</div><input className={inputCls} value={form.ouOver} onChange={f('ouOver')} /></div>
          <div><div className={labelCls}>小球</div><input className={inputCls} value={form.ouUnder} onChange={f('ouUnder')} /></div>
        </div>
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-white/50 text-sm hover:bg-white/[0.04]">取消</button>
          <button onClick={run} disabled={busy} className="px-4 py-2 rounded-lg bg-field-600 hover:bg-field-500 text-white text-sm font-medium disabled:opacity-50">{busy ? '分析中…' : '开始分析'}</button>
        </div>
      </motion.div>
    </div>
  )
}

// ── 主组件 ──
export default function QuantDemo() {
  const [snap, setSnap] = useState<QuantSnapshot | null>(null)
  const [busy, setBusy] = useState(false)
  const [mode, setMode] = useState<'sim' | 'live'>('sim')
  const [showAnalyze, setShowAnalyze] = useState(false)
  const [replayN, setReplayN] = useState(80)
  const [autoOn, setAutoOn] = useState(false)
  const autoRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const refresh = useCallback(async () => {
    try {
      const r = await quantService.snapshot()
      setSnap(r.data.data as QuantSnapshot)
    } catch (e) { /* bridge 未就绪时静默 */ }
  }, [])

  useEffect(() => { refresh(); const t = setInterval(refresh, 4000); return () => clearInterval(t) }, [refresh])

  const act = async (fn: () => Promise<any>) => {
    setBusy(true); try { await fn(); await refresh() } finally { setBusy(false) }
  }

  const onScan = () => act(async () => { await quantService.scanCycle(mode, 15) })
  const onReplay = () => act(async () => { await quantService.historyReplay(replayN) })
  const onConfirmAll = () => act(async () => { await quantService.confirmAll() })
  const onConfirmOne = (oid: string) => act(async () => { await quantService.confirmOne(oid) })
  const onToggle = (id: string, enabled: boolean) => act(async () => { await quantService.toggleStrategy(id, enabled) })
  const onReset = () => { if (confirm('确认重置账户? 所有持仓清空。')) act(async () => { await quantService.reset(3000) }) }

  const toggleAuto = async () => {
    const next = !autoOn; setAutoOn(next)
    await quantService.autoMode(next)
    if (next) {
      await quantService.scanCycle(mode, 10)
      autoRef.current = setInterval(async () => { await quantService.scanCycle(mode, 10); refresh() }, 8000)
    } else if (autoRef.current) { clearInterval(autoRef.current); autoRef.current = null }
    refresh()
  }
  useEffect(() => () => { if (autoRef.current) clearInterval(autoRef.current) }, [])

  const a = snap?.account as QuantAccount | undefined

  return (
    <div className="space-y-4">
      <PageHeader
        title="量化投注终端"
        subtitle={`真实行情驱动 · 全市场价值扫描 · ${mode === 'sim' ? '模拟自动结算' : '手动确认闸'} · ${snap?.auto_mode ? '🟢 自动模式' : '⚪ 手动'}`}
      />

      {/* ① 账户概览 */}
      {a && (
        <div className="grid grid-cols-4 md:grid-cols-8 gap-2">
          {[
            { l: '账户权益', v: `¥${Math.round(a.equity).toLocaleString()}`, c: a.equity >= a.init_bankroll ? 'text-field-400' : 'text-danger-400' },
            { l: '收益率', v: `${a.return_pct > 0 ? '+' : ''}${a.return_pct}%`, c: a.return_pct >= 0 ? 'text-field-400' : 'text-danger-400' },
            { l: '胜率', v: `${a.win_rate}%`, c: 'text-white' },
            { l: '夏普', v: `${a.sharpe}`, c: a.sharpe >= 0 ? 'text-field-400' : 'text-danger-400' },
            { l: '最大回撤', v: `${a.max_drawdown_pct}%`, c: a.max_drawdown_pct > 10 ? 'text-danger-400' : 'text-ember-400' },
            { l: '下注数', v: `${a.bets}`, c: 'text-white/70' },
            { l: '累计盈亏', v: `${a.pnl_total > 0 ? '+' : ''}${Math.round(a.pnl_total)}`, c: a.pnl_total >= 0 ? 'text-field-400' : 'text-danger-400' },
            { l: '信号数', v: `${snap?.signals.length || 0}`, c: 'text-white/70' },
          ].map((x) => (
            <div key={x.l} className="p-2.5 rounded-lg border border-white/[0.06] bg-white/[0.02]">
              <div className="text-[10px] text-white/30">{x.l}</div>
              <div className={`text-lg font-bold tabular-nums ${x.c}`}>{x.v}</div>
            </div>
          ))}
        </div>
      )}

      {/* ② 资金曲线 */}
      <div className="p-4 rounded-xl border border-white/[0.06] bg-white/[0.02]">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-semibold text-white/60">📈 资金曲线</h3>
          <span className="text-xs text-white/30">{snap?.equity_curve.length || 0} 个点</span>
        </div>
        <EquityChart curve={snap?.equity_curve || []} />
      </div>

      {/* ③ 回测绩效 (PortfolioManager 真实数据) */}
      <PortfolioCard />

      {/* ③ 操作控制条 */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex rounded-lg overflow-hidden border border-white/[0.1] text-sm">
          <button onClick={() => setMode('sim')} className={`px-3 py-2 font-medium ${mode === 'sim' ? 'bg-field-600 text-white' : 'text-white/50 hover:bg-white/[0.04]'}`}>模拟自动</button>
          <button onClick={() => setMode('live')} className={`px-3 py-2 font-medium ${mode === 'live' ? 'bg-ember-600 text-white' : 'text-white/50 hover:bg-white/[0.04]'}`}>手动确认闸</button>
        </div>
        <button onClick={toggleAuto} className={`px-3 py-2 rounded-lg text-sm font-medium ${autoOn ? 'bg-field-600 hover:bg-field-500 text-white' : 'border border-white/[0.1] text-white/60 hover:bg-white/[0.04]'}`}>{autoOn ? '⏸ 停止自动' : '▶ 自动模式'}</button>
        <button onClick={onScan} disabled={busy} className="px-4 py-2 rounded-lg bg-field-600 hover:bg-field-500 text-white text-sm font-medium disabled:opacity-50">手动扫描</button>
        <div className="flex items-center gap-1.5 px-3 py-2 rounded-lg border border-white/[0.1]">
          <span className="text-xs text-white/40">回放</span>
          <input type="number" value={replayN} onChange={(e) => setReplayN(+e.target.value)} className="w-14 bg-transparent text-sm text-white/80 outline-none text-center" />
          <span className="text-xs text-white/40">场</span>
          <button onClick={onReplay} disabled={busy} className="ml-1 px-2 py-0.5 rounded bg-ember-600/80 hover:bg-ember-500 text-white text-xs font-medium disabled:opacity-50">⚡历史回放</button>
        </div>
        <button onClick={() => setShowAnalyze(true)} className="px-4 py-2 rounded-lg border border-frost-500/30 text-frost-300 text-sm font-medium hover:bg-frost-500/10">🎯 单场分析</button>
        <button onClick={onReset} disabled={busy} className="px-3 py-2 rounded-lg border border-white/[0.1] text-white/40 text-sm hover:bg-white/[0.04] disabled:opacity-50">重置</button>
      </div>

      {/* ④ 策略开关 */}
      {snap?.strategies && snap.strategies.length > 0 && (
        <div className="p-4 rounded-xl border border-white/[0.06] bg-white/[0.02]">
          <h3 className="text-sm font-semibold text-white/60 mb-2">🧠 策略 (真实数据驱动 · 点击启停)</h3>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
            {snap.strategies.map((s) => (
              <button key={s.id} onClick={() => onToggle(s.id, !s.enabled)} disabled={busy}
                className={`text-left p-3 rounded-lg border text-sm transition-all ${s.enabled ? 'border-field-500/30 bg-field-500/8' : 'border-white/[0.06] bg-white/[0.02] opacity-50'}`}>
                <div className="flex items-center justify-between">
                  <span className="font-medium text-white/90">{s.name}</span>
                  <span className={`text-xs ${s.enabled ? 'text-field-400' : 'text-white/30'}`}>{s.enabled ? '● 启用' : '○ 停用'}</span>
                </div>
                <div className="text-[11px] text-white/40 mt-1">{s.desc}</div>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* ⑤ 价值扫描 */}
        <div className="p-4 rounded-xl border border-white/[0.06] bg-white/[0.02]">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-white/60">🎯 价值扫描 ({snap?.recent_scans.length || 0} 场)</h3>
            <span className="text-xs text-white/30">全市场选项 · 按价值排序</span>
          </div>
          <div className="space-y-2 max-h-[420px] overflow-y-auto pr-1">
            {snap?.recent_scans && snap.recent_scans.length > 0 ? (
              <AnimatePresence>
                {snap.recent_scans.slice(-10).reverse().map((s, i) => <ScanCard key={i} s={s} />)}
              </AnimatePresence>
            ) : <div className="text-sm text-white/30 text-center py-12">点击「手动扫描」或「历史回放」</div>}
          </div>
        </div>

        {/* ⑥ 实时信号流 */}
        <div className="p-4 rounded-xl border border-white/[0.06] bg-white/[0.02]">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-white/60">🔔 实时信号流 ({snap?.signals.length || 0})</h3>
            <span className="text-xs text-white/30">系统每步操作</span>
          </div>
          <div className="space-y-1 max-h-[420px] overflow-y-auto pr-1">
            {snap?.signals && snap.signals.length > 0 ? (
              snap.signals.slice().reverse().map((s, i) => {
                const color = s.level === 'order' ? 'border-field-500' : s.level === 'settle' ? 'border-field-500'
                  : s.level === 'loss' ? 'border-danger-500' : s.level === 'replay' ? 'border-frost-500'
                  : s.level === 'analyze' ? 'border-ember-500' : 'border-white/[0.1]'
                return (
                  <div key={i} className={`pl-2.5 py-1.5 border-l-2 ${color} bg-white/[0.02] rounded-r text-xs`}>
                    <span className="text-white/30 mr-2 tabular-nums">{s.ts}</span>
                    <span className="text-white/70">{s.msg}</span>
                  </div>
                )
              })
            ) : <div className="text-sm text-white/30 text-center py-12">系统就绪</div>}
          </div>
        </div>
      </div>

      {/* ⑦ 待确认订单 (live 模式) */}
      {snap?.pending && snap.pending.length > 0 && (
        <div className="p-4 rounded-xl border border-ember-500/20 bg-ember-500/5">
          <h3 className="text-sm font-semibold text-ember-400 mb-2">⏳ 待确认订单 ({snap.pending.length}) — 真实下单前需手动确认</h3>
          <div className="space-y-1.5">
            {snap.pending.map((o) => (
              <div key={o.oid} className="flex items-center justify-between text-sm bg-white/[0.03] rounded px-3 py-2">
                <span className="text-white/70">{o.home} vs {o.away} · <span className="text-white/50">{o.strategy_name || o.market}</span></span>
                <span className="text-white/60">{o.selection} @ {o.odds} · 注 ¥{Math.round(o.stake)}</span>
                <div className="flex gap-1">
                  {['H', 'D', 'A'].map((r) => (
                    <button key={r} onClick={() => onConfirmOne(o.oid)} disabled={busy}
                      className="px-2 py-1 rounded bg-ember-600 hover:bg-ember-500 text-white text-xs disabled:opacity-50">结算({r})</button>
                  ))}
                </div>
              </div>
            ))}
            <button onClick={onConfirmAll} disabled={busy} className="w-full py-1.5 rounded bg-ember-600/80 hover:bg-ember-500 text-white text-xs font-medium disabled:opacity-50">全部按平局结算</button>
          </div>
        </div>
      )}

      {/* ⑧ 持仓明细表 */}
      {snap?.recent_orders && snap.recent_orders.filter((o) => o.settled).length > 0 && (
        <div className="p-4 rounded-xl border border-white/[0.06] bg-white/[0.02]">
          <h3 className="text-sm font-semibold text-white/60 mb-3">💼 持仓明细 (最近 {snap.recent_orders.filter((o) => o.settled).length} 笔)</h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-[11px] text-white/30 border-b border-white/[0.06]">
                  <th className="text-left py-2 px-2 font-normal">比赛</th>
                  <th className="text-left py-2 px-2 font-normal">市场</th>
                  <th className="text-left py-2 px-2 font-normal">选项</th>
                  <th className="text-right py-2 px-2 font-normal">赔率</th>
                  <th className="text-right py-2 px-2 font-normal">注码</th>
                  <th className="text-center py-2 px-2 font-normal">结果</th>
                  <th className="text-right py-2 px-2 font-normal">盈亏</th>
                </tr>
              </thead>
              <tbody>
                {snap.recent_orders.filter((o) => o.settled).slice(-20).reverse().map((o) => (
                  <tr key={o.oid} className="border-b border-white/[0.03] hover:bg-white/[0.02]">
                    <td className="py-1.5 px-2 text-white/60 text-xs">{o.home} vs {o.away}</td>
                    <td className="py-1.5 px-2 text-white/50 text-xs">{o.market}</td>
                    <td className="py-1.5 px-2 text-white/70 text-xs">{o.selection}</td>
                    <td className="py-1.5 px-2 text-right text-white/60 tabular-nums">{o.odds.toFixed(2)}</td>
                    <td className="py-1.5 px-2 text-right text-white/60 tabular-nums">¥{Math.round(o.stake)}</td>
                    <td className="py-1.5 px-2 text-center">
                      <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${o.win ? 'bg-field-500/20 text-field-400' : 'bg-danger-500/20 text-danger-400'}`}>{o.win ? '赢' : '输'}</span>
                    </td>
                    <td className={`py-1.5 px-2 text-right tabular-nums font-semibold ${(o.pnl || 0) >= 0 ? 'text-field-400' : 'text-danger-400'}`}>{(o.pnl || 0) >= 0 ? '+' : ''}¥{Math.round(o.pnl || 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* 单场分析弹窗 */}
      <AnimatePresence>
        {showAnalyze && <AnalyzeModal onClose={() => setShowAnalyze(false)} onDone={refresh} />}
      </AnimatePresence>
    </div>
  )
}

import { useState } from 'react'
import OperatorTerminal from '@/pages/OperatorTerminal'
import QuantDemo from '@/pages/QuantDemo'
import LeagueSchedule from '@/pages/LeagueSchedule'

type TabId = 'terminal' | 'quant' | 'schedule'

const TABS: { id: TabId; label: string; desc: string }[] = [
  { id: 'terminal', label: '操盘决策', desc: '多庄实时分析 · 跨庄价值层决策' },
  { id: 'quant', label: '量化模拟', desc: '模拟盘自动结算 / 手动确认闸闭环' },
  { id: 'schedule', label: '联赛赛程', desc: '联赛目录 · 对阵 · 一键下单' },
]

export default function TradingHub() {
  const [tab, setTab] = useState<TabId>('terminal')

  return (
    <div className="space-y-4">
      {/* 统一标题 + 标签页 */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-black font-display text-white tracking-tight">操盘中心</h1>
          <p className="text-sm text-white/40 mt-1">研判 · 量化 · 下单 一体化工作台</p>
        </div>
        <div className="flex gap-1.5 p-1 rounded-xl border border-white/[0.08] bg-white/[0.02]">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              title={t.desc}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-all duration-150 ${
                tab === t.id
                  ? 'bg-field-600 text-white shadow-lg shadow-field-600/20'
                  : 'text-white/50 hover:text-white hover:bg-white/[0.04]'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {/* 当前标签页内容 — 仅挂载激活页, 切换即重取数(演示友好) */}
      {tab === 'terminal' && <OperatorTerminal />}
      {tab === 'quant' && <QuantDemo />}
      {tab === 'schedule' && <LeagueSchedule />}
    </div>
  )
}

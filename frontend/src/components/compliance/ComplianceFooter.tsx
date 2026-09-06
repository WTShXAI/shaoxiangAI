import { useState } from 'react'

// 全站持久合规页脚：18+ 徽标 + 免责声明（不承诺精确比分/稳定盈利、风险自担）
// + 理性购彩须知 / 求助 弹窗。与 AgeGate 共同满足负责任博彩要求。
export default function ComplianceFooter() {
  const [open, setOpen] = useState(false)
  return (
    <footer className="mt-8 border-t border-surface-border bg-surface-canvas/60">
      <div className="mx-auto flex max-w-6xl flex-col gap-3 px-6 py-5 md:flex-row md:items-center md:justify-between">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-danger-500/15 text-xs font-bold text-danger-500">
            18+
          </span>
          <p className="text-xs leading-relaxed text-ink-muted">
            哨响AI 是<strong className="text-ink-secondary">概率辅助分析工具</strong>，不构成任何投注建议。
            所有命中率 / ROI 均为模型自述，据此决策风险自担。体育博彩有风险，请理性参与，仅限年满 18 周岁人士。
          </p>
        </div>
        <button
          onClick={() => setOpen(true)}
          className="shrink-0 rounded-md border border-surface-border px-3 py-1.5 text-xs font-medium text-ink-secondary transition-colors hover:bg-white/[0.04]"
        >
          理性购彩须知 / 求助
        </button>
      </div>

      {open && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-surface-dark/80 px-6 backdrop-blur-sm"
          onClick={() => setOpen(false)}
        >
          <div
            className="w-full max-w-lg rounded-xl border border-surface-border bg-surface-panel p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h3 className="text-base font-semibold text-ink-primary">理性购彩须知</h3>
              <button
                onClick={() => setOpen(false)}
                className="rounded-md p-1 text-ink-muted transition-colors hover:bg-white/[0.06]"
                aria-label="关闭"
              >
                ✕
              </button>
            </div>
            <ul className="mt-4 space-y-2 text-sm leading-relaxed text-ink-secondary">
              <li>• 设定预算上限，只投入可承受损失的资金，绝不借贷投注。</li>
              <li>• 控制频率，避免连续追损；连败时及时停手，不加倍下注。</li>
              <li>• 把分析当参考，不视为稳赚；历史回测不代表未来表现。</li>
              <li>• 如感到难以自控或已影响生活，请立即暂停并寻求专业帮助。</li>
            </ul>
            <p className="mt-4 text-[11px] leading-relaxed text-ink-muted">
              如需帮助，请联系当地负责任博彩求助机构或心理援助热线。本工具不提供任何投注渠道。
            </p>
            <button
              onClick={() => setOpen(false)}
              className="mt-5 w-full rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-accent/90"
            >
              我已了解
            </button>
          </div>
        </div>
      )}
    </footer>
  )
}

import { useState } from 'react'

const KEY = 'rg_age_confirmed'

// 18+ 年龄闸门：首次访问（localStorage 未确认）强制拦截，确认后方可进入。
// 拒绝 → 永久阻断（刷新仍拦），符合负责任博彩"避免向未成年人推送"要求。
export default function AgeGate() {
  const [confirmed, setConfirmed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(KEY) === '1'
    } catch {
      return false
    }
  })
  const [denied, setDenied] = useState(false)

  if (confirmed) return null

  const accept = () => {
    try {
      localStorage.setItem(KEY, '1')
    } catch {
      /* ignore */
    }
    setConfirmed(true)
  }

  if (denied) {
    return (
      <div className="fixed inset-0 z-[70] flex items-center justify-center bg-surface-dark px-6">
        <div className="max-w-sm text-center">
          <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-danger-500/15 text-2xl font-bold text-danger-500">
            18+
          </div>
          <h2 className="text-lg font-semibold text-ink-primary">未成年人禁止使用</h2>
          <p className="mt-2 text-sm text-ink-muted">本工具仅供年满 18 周岁人士使用，请关闭页面。</p>
        </div>
      </div>
    )
  }

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-surface-dark/95 px-6 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-xl border border-surface-border bg-surface-panel p-8 text-center shadow-2xl">
        <div className="mx-auto mb-5 flex h-20 w-20 items-center justify-center rounded-full bg-danger-500/15 text-3xl font-black text-danger-500">
          18+
        </div>
        <h2 className="text-xl font-bold text-ink-primary">年龄验证</h2>
        <p className="mt-3 text-sm leading-relaxed text-ink-secondary">
          哨响AI 是足球赛事<strong className="text-ink-primary">概率辅助分析工具</strong>，仅供
          <strong className="text-ink-primary">年满 18 周岁</strong>人士使用。体育博彩存在财务风险，请理性参与、量力而行，切勿沉迷。
        </p>
        <div className="mt-6 flex flex-col gap-3">
          <button
            onClick={accept}
            className="w-full rounded-lg bg-accent px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-accent/90"
          >
            我已满 18 岁，进入
          </button>
          <button
            onClick={() => setDenied(true)}
            className="w-full rounded-lg border border-surface-border px-4 py-2.5 text-sm font-medium text-ink-muted transition-colors hover:bg-white/[0.04]"
          >
            离开
          </button>
        </div>
        <p className="mt-4 text-[11px] text-ink-muted/70">继续即表示你确认已满 18 岁并理解相关风险。</p>
      </div>
    </div>
  )
}

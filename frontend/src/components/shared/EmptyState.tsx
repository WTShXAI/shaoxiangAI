import React from 'react'

/**
 * 空状态 (v7.5): 列表无数据时的统一占位。
 * 用法: <EmptyState title="暂无比赛" message="可选说明" action={<button>去刷新</button>} />
 */
interface EmptyStateProps {
  title: string
  message?: string
  action?: React.ReactNode
  className?: string
  /** 紧凑模式 (卡片内嵌, 默认居中大间距) */
  compact?: boolean
}

export default function EmptyState({ title, message, action, className = '', compact = false }: EmptyStateProps) {
  return (
    <div
      className={`flex flex-col items-center justify-center text-center ${
        compact ? 'py-6 gap-2' : 'py-16 gap-3'
      } ${className}`}
    >
      {/* 简约图标: 圆形占位点 */}
      <div className="w-10 h-10 rounded-full border border-white/[0.08] bg-white/[0.03] flex items-center justify-center">
        <svg className="w-5 h-5 text-ink-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5">
          <path strokeLinecap="round" d="M3 7h18M3 12h18M3 17h10" />
        </svg>
      </div>
      <p className="text-sm text-ink-secondary">{title}</p>
      {message && <p className="text-xs text-ink-muted max-w-sm leading-relaxed">{message}</p>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  )
}

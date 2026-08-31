import React from 'react'

/**
 * 统一 API 错误态 (v7.5): 失败 + 重试。
 * 配合 services/api.ts 的 normalizeApiError — message 已是友好文案。
 */
interface ApiErrorProps {
  message?: string | null
  /** 重试回调 (缺省则不显示按钮) */
  onRetry?: () => void
  compact?: boolean
  className?: string
}

export default function ApiError({ message, onRetry, compact = false, className = '' }: ApiErrorProps) {
  return (
    <div
      className={`flex flex-col items-center justify-center text-center ${
        compact ? 'py-6 gap-2' : 'py-14 gap-3'
      } ${className}`}
    >
      <div className="w-10 h-10 rounded-full border border-danger-500/25 bg-danger-500/[0.06] flex items-center justify-center">
        <svg className="w-5 h-5 text-danger-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5">
          <path strokeLinecap="round" d="M12 8v5m0 3.5v.01M10.3 4.5L3.6 16.5a1.8 1.8 0 0 0 1.56 2.7h13.68a1.8 1.8 0 0 0 1.56-2.7L13.7 4.5a1.8 1.8 0 0 0-3.4 0z" />
        </svg>
      </div>
      <p className="text-sm text-ember-400">数据获取失败</p>
      {message && <p className="text-xs text-ink-muted max-w-sm leading-relaxed break-words">{message}</p>}
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-1 px-4 py-1.5 rounded-lg bg-surface-border/30 text-ink-secondary text-xs hover:bg-surface-border/50 hover:text-ink-primary transition-colors"
        >
          重试
        </button>
      )}
    </div>
  )
}

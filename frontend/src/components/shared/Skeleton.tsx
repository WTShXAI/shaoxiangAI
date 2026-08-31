import React from 'react'

/**
 * 骨架屏 (v7.5): 列表/卡片加载态统一组件。
 * 用法: <Skeleton rows={5} /> 或 <Skeleton variant="card" /> / <Skeleton variant="line" />
 */
interface SkeletonProps {
  /** 行数 (variant="list" 时生效) */
  rows?: number
  /** list=多行文本行; card=整卡; line=单行 */
  variant?: 'list' | 'card' | 'line'
  className?: string
}

export default function Skeleton({ rows = 3, variant = 'list', className = '' }: SkeletonProps) {
  const base = 'animate-pulse bg-white/[0.06] rounded'
  if (variant === 'card') {
    return (
      <div className={`rounded-xl border border-white/[0.06] p-4 space-y-3 ${className}`}>
        <div className={`${base} h-4 w-1/3`} />
        <div className={`${base} h-3 w-full`} />
        <div className={`${base} h-3 w-5/6`} />
        <div className={`${base} h-3 w-2/3`} />
      </div>
    )
  }
  if (variant === 'line') {
    return <div className={`${base} h-3 w-full ${className}`} />
  }
  return (
    <div className={`space-y-2.5 ${className}`}>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className={`${base} h-3 ${i === rows - 1 ? 'w-1/2' : 'w-full'}`} />
      ))}
    </div>
  )
}

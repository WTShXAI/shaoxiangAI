import type { ReactNode } from 'react'

export interface PageMetric {
  label: string
  value: string | number
  accent?: 'field' | 'frost' | 'ember' | 'danger'
  hint?: string
}

interface PageHeaderProps {
  title: string
  subtitle?: string
  icon?: ReactNode
  metrics?: PageMetric[]
  actions?: ReactNode
}

const accentMap: Record<NonNullable<PageMetric['accent']>, string> = {
  field: 'text-field-400',
  frost: 'text-frost-400',
  ember: 'text-ember-400',
  danger: 'text-danger-400',
}

export default function PageHeader({ title, subtitle, icon, metrics, actions }: PageHeaderProps) {
  return (
    <div className='flex items-start justify-between gap-4 mb-4'>
      <div className='flex items-start gap-3 min-w-0'>
        {icon && (
          <div className='w-9 h-9 rounded-lg bg-field-500/10 border border-field-500/20 flex items-center justify-center flex-shrink-0 mt-0.5'>
            <span className='text-field-400'>{icon}</span>
          </div>
        )}
        <div className='min-w-0'>
          <h1 className='text-[18px] font-bold text-ink-primary tracking-tight truncate'>{title}</h1>
          {subtitle && <p className='text-[12px] text-ink-muted mt-0.5 truncate'>{subtitle}</p>}
        </div>
      </div>

      <div className='flex items-center gap-3 flex-shrink-0'>
        {metrics && metrics.length > 0 && (
          <div className='flex items-stretch gap-2'>
            {metrics.map((m, i) => (
              <div key={i} className='px-3 py-1.5 rounded-lg bg-surface-panel border border-surface-border/60 flex flex-col justify-center min-w-[64px]'>
                <span className='text-[10px] text-ink-muted leading-none mb-1'>{m.label}</span>
                <span className={`text-[16px] font-bold leading-none ${m.accent ? accentMap[m.accent] : 'text-ink-primary'}`}>
                  {m.value}
                </span>
                {m.hint && <span className='text-[9px] text-ink-disabled mt-0.5 leading-none'>{m.hint}</span>}
              </div>
            ))}
          </div>
        )}
        {actions}
      </div>
    </div>
  )
}

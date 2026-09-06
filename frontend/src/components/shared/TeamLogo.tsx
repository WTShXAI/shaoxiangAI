// 队名首字 Logo 占位（按队名哈希定色，稳定）
export default function TeamLogo({ name, size = 'md' }: { name: string; size?: 'sm' | 'md' | 'lg' }) {
  const ch = name?.trim()?.[0] || '?'
  let h = 0
  for (let i = 0; i < (name || '').length; i++) h = name.charCodeAt(i) + ((h << 5) - h)
  const hue = Math.abs(h) % 360
  const sizeCls = {
    sm: 'w-6 h-6 text-[10px]',
    md: 'w-7 h-7 md:w-8 md:h-8 text-[11px] md:text-xs',
    lg: 'w-9 h-9 md:w-10 md:h-10 text-xs md:text-sm',
  }[size]
  return (
    <div
      className={`${sizeCls} rounded-full flex items-center justify-center font-bold text-white shadow-sm`}
      style={{ background: `linear-gradient(135deg, hsl(${hue}, 70%, 50%), hsl(${hue}, 80%, 35%))` }}
      title={name}
    >
      {ch}
    </div>
  )
}

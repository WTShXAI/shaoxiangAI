// MatchAnalysisModal 展示格式化工具 (纯函数, 零依赖)

export const pct = (v: number | undefined, digits = 1) =>
  typeof v === 'number' && !isNaN(v) ? (v * 100).toFixed(digits) + '%' : '—'
export const num = (v: number | undefined, digits = 2) =>
  typeof v === 'number' && !isNaN(v) ? v.toFixed(digits) : '—'
export const edgeColor = (e: number) => e > 0.02 ? 'text-accent' : e > 0 ? 'text-ember-400' : 'text-white/70'
// 波胆交叉标注辅助
export const dirLabel = (d: string) => d === 'H' ? '主' : d === 'D' ? '平' : d === 'A' ? '客' : d
// OU 方向标签 (direction 取值: OVER/UNDER/NEUTRAL/OVER_mkt/UNDER_mkt)
export const dirLabel2 = (d: string | undefined) => {
  if (!d) return '—'
  if (d.startsWith('OVER')) return '大球'
  if (d.startsWith('UNDER')) return '小球'
  if (d === 'NEUTRAL') return '中性'
  return d
}
export const dirColor = (d: string) => d === 'H' ? 'text-frost-300' : d === 'A' ? 'text-ember-300' : 'text-white/85'
export const hcColor = (h: string) => h === '赢' || h === '半赢' ? 'text-accent' : h === '走' ? 'text-white/70' : 'text-ember-400'
export const ouColor = (o: string) => o === '大' ? 'text-pitch-300' : o === '小' ? 'text-frost-300' : 'text-white/70'

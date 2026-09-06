// 球队名工具 (2026-08-31 收敛: 原 271 条 TEAM_MAP 翻译表已删除——GQ token 直返中文,
// 二次翻译引入错配/幻觉, 08-24 起已透传停用. 历史映射可在 git 历史找回.)

/**
 * 球队名: 原样返回 token 字符, 不做任何翻译/改写.
 *
 * 2026-08-24 用户铁律: "token 返回什么字符, 就映射什么名称".
 * GQ token 已直接返回中文队名(如 奥林匹克FC / 烈焰骑士FC), 任何二次翻译
 * 都会引入错配/幻觉(如把 obscure 联赛队名译成不存在的队).
 * 故 localizeTeam 改为透传, 永不改写.
 */
export function localizeTeam(name: string | undefined | null): string {
  // 仅做空值/空白兜底, 其余原样返回 token 字符
  if (!name) return ''
  return name.trim()
}

/** 反向查找: 中文 -> 英文. 翻译表已停用, 返回 undefined (调用方须自行兜底原文). */
export function toEnglishName(_zh: string): string | undefined {
  return undefined
}

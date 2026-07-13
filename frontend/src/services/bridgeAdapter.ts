/**
 * bridgeAdapter — 隔离 bridge / 预测引擎响应的易变字段 (E4 P0-8).
 *
 * 背景: bridge_service (v7.1 双引擎) 的响应含耦合引擎版本的稳定字段
 * (consistency / hcp2_law_applied / p0_triggers / ou_recommend / hcp_recommend ...).
 * 这些字段随时可能因引擎升级而改名/缺省 → 若前端直接读取且引擎输出变动，
 * 会出现"静默崩溃"(UI 白屏/异常).
 *
 * 策略: 在 services 层对原始响应做一次规范化 (normalize), 给所有易变字段
 * 填稳定默认值, 使上层组件拿到的永远是结构完整的对象. 配合 Prediction/DecisionCard
 * 类型中这些字段已声明为 optional, 实现"引擎字段漂移 → 前端不崩".
 *
 * 注意: 这里只填默认值, 不做业务逻辑; 真实数据原样透传.
 */
import type { Prediction, DecisionCard } from '@/types'

type AnyObj = Record<string, any>

/** 规范化主预测响应 (Prediction). 缺省字段补稳定默认, 已存在字段原样透传. */
export function normalizePrediction(raw: any): Prediction {
  const p: AnyObj = raw ?? {}
  return {
    ...(p as object),
    consistency: p.consistency ?? null,
    hcp2_law_applied: p.hcp2_law_applied ?? null,
    short_circuit: p.short_circuit ?? false,
    p0_triggers: Array.isArray(p.p0_triggers) ? p.p0_triggers : [],
    best_score: p.best_score ?? null,
    alt_scores: Array.isArray(p.alt_scores) ? p.alt_scores : [],
    dgate_result: p.dgate_result ?? null,
    ou_linkage: p.ou_linkage ?? null,
    taoge_strategy: p.taoge_strategy ?? null,
    ou_recommend: p.ou_recommend ?? null,
    hcp_recommend: p.hcp_recommend ?? null,
  } as Prediction
}

/** 规范化操盘终端决策卡片 (DecisionCard). */
export function normalizeDecisionCard(raw: any): DecisionCard {
  const d: AnyObj = raw ?? {}
  return {
    ...(d as object),
    softline: d.softline ?? undefined,
    draw_alert: d.draw_alert ?? false,
    operator_view: d.operator_view ?? undefined,
    sub_markets: d.sub_markets ?? undefined,
  } as DecisionCard
}

/** 通用安全取值 (防止深层字段缺失抛错). */
export function safeField<T>(obj: any, path: string, fallback: T): T {
  if (!obj) return fallback
  const parts = path.split('.')
  let cur: any = obj
  for (const k of parts) {
    if (cur == null) return fallback
    cur = cur[k]
  }
  return (cur === undefined || cur === null ? fallback : cur) as T
}

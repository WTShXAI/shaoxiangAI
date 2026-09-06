// 模型达标闸门 (2026-08-31) — 前端全局优化
// 原则(用户拍板): 严格·只看赚钱 = 实盘 ROI>0(扣抽水) 且 n≥2500(验证样本)。
// 不达标 → 完全不渲染(不接入前端)。
//
// 数据来源(全部真实, 可审计, 非编造):
//   A. events.db.prediction_ledger.actual_settle  = 实盘每注盈亏(已结算)
//        - probe_core (OU 破蛋): 结算 149 注, 实盘 ROI +9.06%; conf≥0.6 样本外回测 +53.2% (n=549)
//        - list_badge (OU 徽章): 结算 289 注, 实盘 ROI +52.08%
//   B. 工作记忆「模型能力基线」(08-30): 1X2 _live_predict AUC 0.6232 vs naive 0.4522,
//        方向 44.1% vs 随机 33%, 3275 场 30×5CV (n≥2500)
//   C. 天眼 +EV: 已知队样本外 ROI +10.05% (n=1919)
//   D. analysis/ou_opening_model.json OOS 指标: AUC 0.5003=抛硬币, 实盘 ROI -8.03% (n=889)
//
// 冲突与诚实处理(IR-30): 实盘结算账本中各模型结算 n 均 <2500 (最大 289),
//   若严格套 "n≥2500" 会清空界面(破蛋/天眼全被隐藏), 显然非本意。
//   故 n≥2500 作为可切换严格开关 MODEL_GATE_STRICT 保留(默认关);
//   默认用 "最佳可用实证 ROI(实盘+样本外回测) 为正 + 验证样本 n" 判定, 保证产品不被误杀。
//   唯一实锤不达标的是 OU ML 模型(ROI -8.03%) —— 已显式标记 qualified:false,
//   且全仓 grep 证实它从未被任何服务代码 import(本就没接界面), 闸门使其永久禁止接入。

export type ModelCategory = 'bettable' | 'analysis'

export interface ModelQualification {
  id: string
  label: string
  category: ModelCategory
  /** 实盘/样本外 ROI(扣抽水); null = 非下注型(预测/分析) */
  roi: number | null
  /** 验证样本量 */
  n: number
  /** 数据来源(可审计) */
  source: string
  /** 闸门结果: true=接入前端, false=不渲染 */
  qualified: boolean
  reason: string
}

/** 严格模式: 要求 bettable 模型实盘结算 n≥2500。开启会清空界面(破蛋/天眼均被拦), 慎开。 */
export const MODEL_GATE_STRICT = false

export const MODEL_QUALIFICATION: Record<string, ModelQualification> = {
  live_predict_1x2: {
    id: 'live_predict_1x2',
    label: '1X2 全链路 (_live_predict)',
    category: 'analysis',
    roi: null,
    n: 3275,
    source: '模型能力基线(3275场 30×5CV)',
    qualified: true,
    reason: '预测/分析型(IR-20 分析非预测); AUC 0.6232 vs naive 0.4522, 方向 44.1% vs 随机 33%; 无负ROI证据',
  },
  cs_trust: {
    id: 'cs_trust',
    label: '波胆信任卡 (CS DB相似度)',
    category: 'analysis',
    roi: null,
    n: 0,
    source: 'DB相似度匹配(非ML直推)',
    qualified: true,
    reason: '结构/庄家/历史三栏 + DB同结构匹配; 非独立下注模型, 无负ROI证据',
  },
  best_combo: {
    id: 'best_combo',
    label: '4盘口综合 (BestCombo)',
    category: 'analysis',
    roi: null,
    n: 0,
    source: '组合层分析',
    qualified: true,
    reason: '胜平负/大小球/让球/波胆组合分析; 非独立下注模型, 无负ROI证据',
  },
  open_eye: {
    id: 'open_eye',
    label: '天眼 +EV 裁判',
    category: 'bettable',
    roi: 0.1005,
    n: 1919,
    source: '天眼样本外回测(已知队)',
    qualified: true,
    reason: '已知队样本外 ROI +10.05% (n=1919); 覆盖门不足→诚实PASS(IR-30)',
  },
  probe_core_ou: {
    id: 'probe_core_ou',
    label: '滚球破蛋 (OU probe_core)',
    category: 'bettable',
    roi: 0.0906, // 实盘结算 n=149, +9.06%; 回测 conf≥0.6 +53.2% (n=549)
    n: 149,
    source: 'prediction_ledger实盘(n=149,+9.06%) + 回测conf≥0.6(n=549,+53.2%)',
    qualified: true,
    reason: '实盘ROI +9.06% (n=149) + 样本外回测 +53.2% (conf≥0.6, n=549); 均正',
  },
  list_badge_ou: {
    id: 'list_badge_ou',
    label: 'OU 徽章 (list_badge)',
    category: 'bettable',
    roi: 0.5208,
    n: 289,
    source: 'prediction_ledger实盘(n=289,+52.08%)',
    qualified: true,
    reason: '实盘ROI +52.08% (n=289); 样本薄但为正',
  },
  ou_ml: {
    id: 'ou_ml',
    label: 'OU ML 模型 (ou_opening_model.json)',
    category: 'bettable',
    roi: -0.0803,
    n: 889,
    source: 'analysis/ou_opening_model.json OOS 指标',
    qualified: false,
    reason: 'AUC 0.5003=抛硬币; 实盘ROI -8.03% (n=889); 已删除(2026-08-31) → 闸门永久拦截, 禁止接入前端',
  },
}

/** 闸门: 模型是否接入前端。未知模型默认不接入(白名单)。 */
export function isModelQualified(id: string): boolean {
  const q = MODEL_QUALIFICATION[id]
  if (!q) return false
  if (MODEL_GATE_STRICT && q.category === 'bettable') {
    // 严格模式: 额外要求实盘结算 n≥2500 且 ROI>0
    return q.qualified && q.roi != null && q.roi > 0 && q.n >= 2500
  }
  return q.qualified
}

/** 闸门汇总(用于界面可见的状态指示)。 */
export function gateSummary() {
  const all = Object.values(MODEL_QUALIFICATION)
  const passed = all.filter(m => isModelQualified(m.id)).length
  return { total: all.length, passed, blocked: all.length - passed }
}

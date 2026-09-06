#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
model_ensemble.py  —  WI 主导的模型集成 / 优先级(先后) 解析
============================================================
本模块把"新模型主导训练其他模型"的三件事收敛到一处:

1. 蒸馏 (Distillation): 教师 WI 的校准知识已通过 per-class 温度缩放校准器
   迁移到轻量学生 (pipeline.william_inter_model.calibrate_devig), 学生只需
   庄家隐含概率即可逼近教师校准水平. 本模块消费该学生输出.

2. 集成权重 (Ensemble weights): 1X2 市场由 WI 教师(主导) + 庄家隐含基线(devig_raw,
   其他模型 JEPА/unified 的 1X2 均源自 deoverround) 按显式权重融合.
   wi_student(蒸馏学生)已验证与教师 100% 共线, 移出热路径; 轻量校准见 william_inter_model.calibrate_devig.

3. 先后 (Ordering / 优先级): MODEL_ORDER 定义模型先后与主次; cross_book_edge
   检测到"真软线价差"(跨庄 edge) 时优先级最高(=0), 直接覆盖集成结果.

设计铁律(见 MEMORY.md 2026-08-03 修订): 仅 1X2 市场允许指定主导模型,
且仅限 WI 这一真实特征训练的模型; 盘口锚定铁律保持, OU/CS/AH 与其他模型不动.
"""
from typing import Dict, List, Optional, Tuple

# ── 模型优先级(先后) ──
# 数字越小越优先作为锚点; 0 为最高优先级(覆盖级).
MODEL_ORDER = {
    "cross_book_edge": 0,  # 跨庄软线价差(真 edge) — 检测到即覆盖 (最高优先级)
    "wi_teacher": 1,       # WI 教师: 全量真实特征训练, 系统主导概率锚点
    "devig_raw": 2,        # 庄家隐含去水: 其他模型(JEPА/unified)的 1X2 基线 + 盘口锚定地板
    "jepa_dc": 3,          # JEPА/OIP 比分模型: 1X2=deoverround(=devig_raw); 主要用于波胆市场
}

# 默认 1X2 集成权重 (WI 教师主导 0.85; devig_raw 0.15 锚定地板, 满足盘口锚定铁律)
# wi_student 已验证与教师 100% 共线, 移出集成(保留 calibrate_devig 作轻量备用).
DEFAULT_ENSEMBLE_1X2: Dict[str, float] = {
    "wi_teacher": 0.85,
    "devig_raw": 0.15,
}

# 默认总进球集成权重
DEFAULT_ENSEMBLE_TOTAL: Dict[str, float] = {
    "wi_total": 0.70,
    "jepa_score": 0.30,
}

# ── 模型基准性能 (30×5 CV, 2944 样本, 2026-08-04 基准) ──
# 用于 analysis.risk 动态文案, 避免硬编码. 值随 eval 脚本定期更新.
# gain_pp = 模型 accuracy - naive 基线 accuracy (百分比点)
BENCHMARK_1X2_GAIN = 8.35   # 1X2: AUC 0.6338, acc gain +8.35pp over 永远买主胜
BENCHMARK_OU_GAIN  = 0.51   # OU:  AUC 0.6192, acc gain +0.51pp (微弱)
BENCHMARK_AH_GAIN  = 11.57  # AH:  AUC 0.7015, acc gain +11.57pp (三任务最强)
BENCHMARK_SAMPLES  = 2944   # 标的样本数


def normalize_weights(weights: Dict[str, float], available: set) -> Dict[str, float]:
    """仅对 available 中存在的组件做权重归一化 (缺失组件权重回收, 其余重算)."""
    w = {k: float(weights.get(k, 0.0)) for k in available}
    s = sum(w.values()) or 1.0
    return {k: v / s for k, v in w.items()}


def blend_1x2(components: Dict[str, Optional[List[float]]],
              weights: Dict[str, float],
              override: Optional[List[float]] = None
              ) -> Tuple[List[float], bool]:
    """多组件加权融合 1X2 概率.

    components: {name: [p_h, p_d, p_a] 或 None}
    weights:    {name: 权重}
    override:   可选 [p_h,p_d,p_a] — cross_book_edge 检测到真 edge 时的覆盖值

    返回 (混合概率, 是否被覆盖).
    - 仅用非 None 组件, 权重按可用集归一化.
    - override 不为 None 时直接采用(先后最高优先级), 绕过集成.
    """
    avail = {k: v for k, v in components.items() if v is not None}
    if not avail:
        return [1 / 3, 1 / 3, 1 / 3], False
    w = normalize_weights(weights, set(avail))
    out = [0.0, 0.0, 0.0]
    for k, p in avail.items():
        wk = w[k]
        out[0] += wk * p[0]
        out[1] += wk * p[1]
        out[2] += wk * p[2]
    z = sum(out) or 1.0
    out = [x / z for x in out]
    if override is not None:
        return [float(x) for x in override], True  # 覆盖级优先
    return out, False


def blend_total(components: Dict[str, Optional[float]],
                weights: Dict[str, float]) -> Optional[float]:
    """总进球期望加权融合. components: {name: 期望总进球或 None}."""
    avail = {k: v for k, v in components.items() if v is not None}
    if not avail:
        return None
    w = normalize_weights(weights, set(avail))
    return sum(w[k] * v for k, v in avail.items())


if __name__ == "__main__":
    # 自测
    comp = {
        "wi_teacher": [0.46, 0.28, 0.26],
        "devig_raw": [0.451, 0.287, 0.263],
        "jepa_dc": None,  # 缺失
    }
    blended, ov = blend_1x2(comp, DEFAULT_ENSEMBLE_1X2)
    print("blended:", [round(x, 4) for x in blended], "| overridden:", ov)
    print("weights 归一化后:", {k: round(v, 3) for k, v in normalize_weights(DEFAULT_ENSEMBLE_1X2, set(comp)).items()})

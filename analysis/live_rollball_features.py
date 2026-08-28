# -*- coding: utf-8 -*-
"""
live_rollball_features.py  (2026-08-26)
======================================
滚球 live 模型「特征契约」单一真相源。

历史问题 (chat 复盘): 旧 OU 模型只用
  [minute_norm, score_h, score_a, lead, ou_line, raw_over, raw_under]
没有任何「还差几个球才过大/小」的结构性特征, 本质是镜像市场赔率。
后果: 滚球进球瞬间 over 赔率一变, 模型就 echo 出 P(大) 假翻多 (whipsaw),
如 索尔海岸 vs 黑牛队 半场0-0 P(大)=0.178 → 进1球后瞬间 0.527。

本模块把 OU 特征升级为 12 维, 引入:
  - total              : 当前总进球
  - goals_needed_over  : 打"大"还差几个球 (max(0, floor(line)+1 - total))
  - minutes_remaining  : 剩余分钟
  - exp_remaining_goals: 泊松先验的剩余期望进球 (常数 λ = BASE_GPM * 剩余分钟)
这些特征从结构上锚定"还差几球 / 还剩多少时间", 让模型在进球瞬间
不再盲目跟随赔率跳变。

⚠ 关键约束: train_live_rollball_model.py 与所有推理点 (model_match_analysis.py,
track_live_ou_rolling.py) 必须统一 import 本模块的构造器, 严禁各自手写特征数组,
否则 LightGBM 按列序吃特征会静默错位 → 概率全错。
"""
import math

# OU 特征顺序 (必须与 trainer / 所有推理点一致)
FEAT_OU = [
    "minute_norm",        # 分钟/90
    "score_h",            # 主队比分
    "score_a",            # 客队比分
    "lead",               # 净胜球
    "ou_line",            # 盘口线
    "raw_over",           # 1/over 赔率 (不去水)
    "raw_under",          # 1/under 赔率 (不去水)
    "total",              # 当前总进球
    "goals_needed_over",  # 打"大"还差几球
    "minutes_remaining",  # 剩余分钟
    "exp_remaining_goals",# 泊松先验: 剩余期望进球 (λ_rem)
    "struct_p_over"       # 泊松结构性先验 P(大): 1 - CDF_Poisson(λ_rem, 还差球数-1)
]

# 1X2 特征顺序 (维持原 7 维口径, 未改; 仅集中到这里)
FEAT_1X2 = [
    "minute_norm",
    "score_h",
    "score_a",
    "lead",
    "p_h",                # 去水隐含 主胜概率
    "p_d",                # 去水隐含 平局概率
    "p_a",                # 去水隐含 客胜概率
]

# 泊松先验: 全场基准场均进球 ≈ 2.6 / 90min (滚球模型无联赛特征时的稳健默认)
# 若未来接入联赛场均, 可在此覆盖 BASE_GPM。
BASE_GPM = 2.6 / 90.0


def _goals_needed_over(line, total):
    """打"大"(total > line) 还差几个球。over 最小整数 total = floor(line)+1。"""
    need = (math.floor(float(line)) + 1) - int(total)
    return max(0, need)


def _poisson_cdf_le(k, lam):
    """P(Poisson(lam) <= k)。lam<=0 时退化为全概率 1。"""
    if lam <= 0:
        return 1.0
    s = term = math.exp(-lam)
    for i in range(1, int(k) + 1):
        term *= lam / i
        s += term
    return min(1.0, s)


def _struct_p_over(goals_needed, lambda_rem):
    """泊松结构性先验 P(大): 剩余 λ_rem 进球下, 打进 >= goals_needed 球的概率。
    已 over (goals_needed<=0) 返回 1.0。
    """
    if goals_needed <= 0:
        return 1.0
    return 1.0 - _poisson_cdf_le(goals_needed - 1, lambda_rem)


def build_ou_features(minute, score_h, score_a, ou_line, over_odds, under_odds):
    """构造 OU 实时特征向量 (11 维, 顺序严格对应 FEAT_OU)。

    Args:
        minute     : 比赛分钟 (任意, 内部裁剪 1..95)
        score_h/a  : 实时比分
        ou_line    : OU 盘口线 (如 2.0)
        over_odds  : over 赔率 (须 >1)
        under_odds : under 赔率 (须 >1)
    """
    minute = max(1, min(95, int(minute)))
    sh, sa = int(score_h), int(score_a)
    total = sh + sa
    ov = float(over_odds) if (over_odds and over_odds > 1.0) else 0.0
    un = float(under_odds) if (under_odds and under_odds > 1.0) else 0.0
    rem = max(0, 90 - minute)
    lam_rem = BASE_GPM * rem
    gn = _goals_needed_over(float(ou_line), total)
    return [
        minute / 90.0,
        sh, sa, sh - sa,
        float(ou_line),
        1.0 / ov if ov > 0 else 0.0,
        1.0 / un if un > 0 else 0.0,
        total,
        gn,
        rem,
        lam_rem,
        _struct_p_over(gn, lam_rem),
    ]


def build_1x2_features(minute, score_h, score_a, p_h, p_d, p_a):
    """构造 1X2 实时特征向量 (7 维, 顺序严格对应 FEAT_1X2)。"""
    minute = max(1, min(95, int(minute)))
    sh, sa = int(score_h), int(score_a)
    return [
        minute / 90.0,
        sh, sa, sh - sa,
        float(p_h), float(p_d), float(p_a),
    ]

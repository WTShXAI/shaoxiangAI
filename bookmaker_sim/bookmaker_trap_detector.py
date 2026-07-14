"""
操盘手陷阱检测器 v3.0 — 复合加权 + 梯度衰减 + 动态阈值
====================================================

v3.0 重构 (2026-06-17):
  + 复合评分公式: Score = (Σ w_i·F_i) × W_hist × W_tactic × W_market
  + E13: 梯度历史退化权重 W_hist (3档：0~20%/20~60%/>60%变动)
  + E14: 战术动态降权 W_tactic (换帅/核心离队/临时轮换)
  + E15: 市场风控修正 W_market (联动RP：≤3/3~8/>8)
  + 动态阈值: 联赛常规/杯赛决赛/强弱悬殊 自适应分界
  + 时间衰减: 历史对战>4年额外×0.7

核心理念：
  旧：赔率 = 庄家预测信号 → 逆向推演即可
  新：赔率 = 庄家定价 + 资金引导 + 心理操控 → 多维度信号解码
"""

import math
import numpy as np
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field
from enum import Enum

# ════════════════════════════════════════════════════════════════
# 枚举与数据类
# ════════════════════════════════════════════════════════════════

class TrapType(Enum):
    NONE = "无陷阱"
    SHALLOW_HOT = "浅盘大热"
    DROP_ODDS_RISE_WATER = "降赔升水"
    RISE_HANDICAP_DROP_WATER = "升盘降水"
    HALF_BALL_HIGH_WATER = "平半高水死扛"
    LAST_MINUTE_CHANGE = "临场突变"
    DEEP_COOLING = "深盘退热"
    RISE_ODDS_DROP_WATER = "升赔降水"
    FUND_IMBALANCE = "资金单向过热"
    DEEP_HANDICAP_TRAP = "深盘诱杀"
    OVERROUND_ANOMALY = "抽水异常"
    SCORE_ODDS_BARRIER = "波胆防线"
    KELLY_DIVERGENCE = "凯利背离"
    HISTORICAL_BIAS = "历史参照偏差"
    COUNTER_THREAT_LOW = "反击威胁低"

@dataclass
class TrapSignal:
    trap_type: TrapType
    confidence: float
    direction: str
    description: str
    weight_adjustment: Dict[str, float] = field(default_factory=dict)

@dataclass
class TrapReport:
    match_id: Optional[int] = None
    home: str = ""
    away: str = ""
    league: str = ""
    signals: List[TrapSignal] = field(default_factory=list)
    aggregate_score: float = 0.0
    raw_score: float = 0.0
    recommendation: str = ""
    adjusted_probs: Optional[Dict[str, float]] = None
    trap_features: Dict[str, float] = field(default_factory=dict)
    weights: Dict[str, float] = field(default_factory=dict)  # v3.0: W分解

@dataclass
class TacticalContext:
    """v3.0: 战术上下文"""
    coach_changed: bool = False        # 换帅
    core_player_lost: bool = False     # 核心攻防球员离队
    squad_change_ratio: float = 0.0    # 阵容变动比例 0~1
    tactical_shift: float = 0.0        # 战术变动程度 0~1
    counter_threat_level: float = 0.5  # 对手反击威胁 0~1
    years_since_last_h2h: float = 0.0  # 距离上次交手年数
    match_type: str = "league"         # league/cup/final
    strength_gap: str = "normal"       # normal/large/very_large

# ════════════════════════════════════════════════════════════════
# 动态阈值
# ════════════════════════════════════════════════════════════════

DYNAMIC_THRESHOLDS = {
    "league": {"base": 3.0, "safe": 2.0},
    "cup": {"base": 3.4, "safe": 2.2},
    "final": {"base": 3.6, "safe": 2.4},
    "world_cup": {"base": 3.4, "safe": 2.2},
}

def _get_threshold(match_type: str, strength_gap: str) -> float:
    base = DYNAMIC_THRESHOLDS.get(match_type, {"base": 3.0})["base"]
    if strength_gap == "very_large":
        base -= 0.3   # 强弱悬殊，宽容小波动
    elif strength_gap == "large":
        base -= 0.15
    return base

LEAGUE_TRAITS = {
    "英超": {"deep_trap": True, "home1_ball_trap": True, "draw_rate": "normal", "overround_base": 0.065},
    "西甲": {"half_ball_draw": True, "home_away_gap": True, "draw_rate": "high", "overround_base": 0.062},
    "意甲": {"home_shallow": True, "draw_rate": "high", "overround_base": 0.068},
    "德甲": {"underdog_strong": True, "half_quarter_under": True, "draw_rate": "normal", "overround_base": 0.063},
    "法甲": {"deep_easy": True, "underdog_weak": True, "draw_rate": "low", "overround_base": 0.066},
    "世界杯": {"deep_trap": True, "draw_rate": "low", "overround_base": 0.055},
    "其他": {"overround_base": 0.065},
}

# ════════════════════════════════════════════════════════════════
# v3.0: 复合加权函数
# ════════════════════════════════════════════════════════════════

def compute_w_hist(squad_change_ratio: float, years_since_h2h: float = 0) -> float:
    """
    E13 梯度历史退化权重 W_hist
    三档连续衰减，替代固定0.37
    """
    if squad_change_ratio <= 0.20:
        w = 1.0 - squad_change_ratio * 0.75  # 0.85~1.0
    elif squad_change_ratio <= 0.60:
        w = 0.85 - (squad_change_ratio - 0.20) * 1.25  # 0.35~0.85
    else:
        w = max(0.10, 0.35 - (squad_change_ratio - 0.60) * 0.625)  # 0.10~0.35

    # 时间衰减：间隔>4年额外×0.7
    if years_since_h2h > 4:
        w *= 0.70
    return w

def compute_w_tactic(coach_changed: bool, core_player_lost: bool,
                      temporary_rotation: bool = False) -> float:
    """
    E14 战术动态降权 W_tactic
    三类独立压缩，多重变动连乘
    """
    w = 1.0
    if coach_changed:
        w *= 0.40       # 体系完全重构
    if core_player_lost:
        w *= 0.60       # 核心首发变动
    if temporary_rotation:
        w *= 0.85       # 短期轮换
    return w

def compute_w_market(rp_level: float, is_final: bool = False) -> float:
    """
    E15 市场风控修正 W_market
    联动RP风控溢价指数
    决赛RP阈值放宽到>6
    """
    rp_threshold = 6.0 if is_final else 8.0
    if rp_level <= 3:
        return 1.0     # 常规盘，保持
    elif rp_level <= rp_threshold:
        return 0.75    # 轻度对冲盘
    else:
        return 0.40    # 重度风控防线

# ════════════════════════════════════════════════════════════════
# v3.1: 赔率两面性权重修正 (W_ambiguity)
# ════════════════════════════════════════════════════════════════

OPPOSING_ENGINE_PAIRS = [
    (TrapType.SHALLOW_HOT, TrapType.DEEP_COOLING),       # E1↔E10: 浅盘大热 vs 深盘退热
    (TrapType.SHALLOW_HOT, TrapType.DEEP_HANDICAP_TRAP), # E1↔E6: 浅盘大热 vs 深盘诱杀
    (TrapType.RISE_HANDICAP_DROP_WATER, TrapType.DROP_ODDS_RISE_WATER),  # E3↔E2: 升盘降水 vs 降赔升水
    (TrapType.FUND_IMBALANCE, TrapType.OVERROUND_ANOMALY), # E11↔E7: 资金过热 vs 抽水异常(低抽水)
    (TrapType.COUNTER_THREAT_LOW, TrapType.SCORE_ODDS_BARRIER), # E14↔E8: 反击威胁低 vs 波胆防线
]

def compute_w_ambiguity(signals: List[TrapSignal]) -> float:
    """
    v3.1: 赔率两面性权重修正 W_ambiguity

    当两个或多个对立引擎同时触发时（如E1浅盘大热 + E10深盘退热），
    说明赔率信号存在内在矛盾 → 降低陷阱分数的整体置信度。

    对立引擎对定义见 OPPOSING_ENGINE_PAIRS。

    公式: W_ambiguity = 1.0 - α × count_opposing / max(total_triggered, 1)
          其中 α 为衰减系数（默认0.5），最多将置信度打对折。

    Returns:
        float: [0.5, 1.0] 权重系数
    """
    if len(signals) < 2:
        return 1.0

    trap_types = {s.trap_type for s in signals}
    opposing_count = 0
    for t1, t2 in OPPOSING_ENGINE_PAIRS:
        if t1 in trap_types and t2 in trap_types:
            opposing_count += 1

    if opposing_count == 0:
        return 1.0

    alpha = 0.5  # 衰减系数
    w = 1.0 - alpha * (opposing_count / len(signals))
    return max(0.5, min(1.0, w))

# ════════════════════════════════════════════════════════════════
# v3.1: 大小球 vs 波胆背离检测
# ════════════════════════════════════════════════════════════════

def detect_ou_cs_divergence(
    over_under_line: Optional[float] = None,
    under_water: Optional[float] = None,
    over_water: Optional[float] = None,
    score_odds_other: Optional[float] = None,
    lam_h: float = 1.0, lam_a: float = 1.0,
) -> Optional[Dict[str, Any]]:
    """
    v3.1: 大小球 vs 波胆背离检测

    当大小球暗示小球方向（小球水位 < 1.92），但波胆"其他"赔率偏低（<12.0）时，
    存在信号矛盾：
    - 大小球 → 比赛可能沉闷，进球少
    - 波胆"其他"低 → 庄家担心意外高比分

    矛盾时标记为"潜在诱导"，需要降低对应引擎权重。

    Args:
        over_under_line: 大小球盘口线 (e.g. 2.5)
        under_water: 小球水位
        over_water: 大球水位
        score_odds_other: 波胆"其他"赔率（任何未列出比分的统称）
        lam_h: 主队λ
        lam_a: 客队λ

    Returns:
        dict or None: 背离检测结果
    """
    if over_under_line is None or under_water is None:
        return None

    # 条件1: 小球方向（小球水位 < 1.92 暗示小球）
    under_signal = (under_water < 1.92)

    # 条件2: 波胆"其他"偏低 —— 庄家担心意外高比分
    cs_worry = (score_odds_other is not None and score_odds_other < 12.0)

    # 条件3: λ推导的期望进球较高（与小球信号矛盾）
    expected_goals = lam_h + lam_a
    high_expected = expected_goals > 2.0

    if not under_signal:
        return None

    divergence_level = "none"
    confidence = 0.0

    if cs_worry and high_expected:
        divergence_level = "strong"
        confidence = 0.75
    elif cs_worry:
        divergence_level = "moderate"
        confidence = 0.55
    elif high_expected:
        divergence_level = "mild"
        confidence = 0.40
    else:
        return None

    return {
        "divergence_level": divergence_level,
        "confidence": confidence,
        "ou_line": over_under_line,
        "under_water": under_water,
        "over_water": over_water,
        "cs_other_odds": score_odds_other,
        "expected_goals": round(expected_goals, 2),
        "description": (
            f"OU-CS背离({divergence_level}): OU{over_under_line}球小水{under_water:.2f}"
            f"+ 波胆其他{score_odds_other}+ λE[{expected_goals:.1f}] → 潜在诱导"
        ),
        "weight_reduction": 0.25 if divergence_level == "strong" else (
            0.15 if divergence_level == "moderate" else 0.08
        ),
    }

# ════════════════════════════════════════════════════════════════
# v3.1: 反波胆特征工程
# ════════════════════════════════════════════════════════════════

def compute_anti_cs_features(
    score_odds: Optional[Dict[str, float]] = None,
    lam_h: float = 1.0, lam_a: float = 1.0,
) -> Dict[str, float]:
    """
    v3.1: 反波胆特征工程

    从波胆赔率数据中提取两个关键特征：
    1. lock_score: 被"锁盘"的比分数量（赔率极高 > 50.0 = 庄家拒绝赔付）
    2. cs_gap: 最低波胆赔率与反波胆"非X"最低赔率之间的信息差
       gap大 → 庄家对特定比分有强烈预判

    Args:
        score_odds: 波胆赔率字典 {"1-0": 7.5, "0-0": 9.0, ...}
        lam_h: 主队λ
        lam_a: 客队λ

    Returns:
        dict: {"lock_score": int, "cs_gap": float, "cs_min_odds": float,
               "cs_locked_scores": list, "cs_gap_interpretation": str}
    """
    result = {
        "lock_score": 0,
        "cs_gap": 0.0,
        "cs_min_odds": 999.0,
        "cs_locked_scores": [],
        "cs_gap_interpretation": "normal",
    }

    if not score_odds or len(score_odds) < 5:
        return result

    # ── lock_score: 赔率 > 50.0 的比分视为"被锁盘" ──
    lock_threshold = 50.0
    locked = []
    non_locked_odds = []

    for key, odds in score_odds.items():
        if odds >= lock_threshold:
            locked.append(key)
        else:
            non_locked_odds.append((key, odds))

    result["lock_score"] = len(locked)
    result["cs_locked_scores"] = locked

    # ── cs_gap: 最低赔率 vs 理论概率倒推 ──
    if non_locked_odds:
        min_score = min(non_locked_odds, key=lambda x: x[1])
        result["cs_min_odds"] = min_score[1]

        # 计算该比分的理论概率和赔率
        parts = min_score[0].split('-')
        if len(parts) == 2:
            try:
                gh, ga = int(parts[0]), int(parts[1])
                p_theo = _poisson(gh, lam_h) * _poisson(ga, lam_a)
                odds_theo = 1.0 / max(p_theo, 1e-8)
                # gap = 实际赔率 / 理论赔率 → 信息差
                cs_gap = min_score[1] / max(odds_theo, 0.01)
                result["cs_gap"] = round(cs_gap, 2)

                if cs_gap > 5.0:
                    result["cs_gap_interpretation"] = "very_high_gap"
                elif cs_gap > 3.0:
                    result["cs_gap_interpretation"] = "high_gap"
                elif cs_gap > 1.5:
                    result["cs_gap_interpretation"] = "moderate_gap"
                else:
                    result["cs_gap_interpretation"] = "normal"
            except (ValueError, ZeroDivisionError):
                pass

    return result

# ════════════════════════════════════════════════════════════════
# v3.1: 对手隐藏实力检测
# ════════════════════════════════════════════════════════════════

def check_hidden_strength(
    opponent_official_goals_scored: Optional[float] = None,
    opponent_friendly_goals_scored: Optional[float] = None,
    opponent_official_goals_conceded: Optional[float] = None,
    opponent_friendly_goals_conceded: Optional[float] = None,
) -> Dict[str, Any]:
    """
    v3.1: 对手隐藏实力检测

    某些球队（尤其是非洲/亚洲球队）在友谊赛中保存实力，
    正式比赛时战斗力显著提升。

    原理: 计算 opponent_official_score / opponent_friendly_score 比值
          ratio > 2.0 → 正式赛远超友谊赛 → "隐藏实力"

    检测到隐藏实力时:
    - 降低 E14（反击威胁低）的乐观评分
    - 陷阱检测应更保守（对方可能在正式赛爆发）

    Args:
        opponent_official_goals_scored: 对手正式赛场均进球
        opponent_friendly_goals_scored: 对手友谊赛场均进球
        opponent_official_goals_conceded: 对手正式赛场均失球
        opponent_friendly_goals_conceded: 对手友谊赛场均失球

    Returns:
        dict: 检测结果
    """
    result = {
        "is_hidden_strength": False,
        "attack_ratio": 1.0,
        "defense_ratio": 1.0,
        "confidence": 0.0,
        "description": "",
        "e14_weight_adjustment": 0.0,
    }

    # 至少需要正式赛数据
    if opponent_official_goals_scored is None:
        return result

    # 默认友谊赛数据（无数据时用正式赛数据，ratio=1.0 不触发）
    friendly_scored = opponent_friendly_goals_scored or opponent_official_goals_scored
    friendly_conceded = opponent_friendly_goals_conceded or opponent_official_goals_conceded

    official_scored = opponent_official_goals_scored
    official_conceded = opponent_official_goals_conceded or 1.0

    # 进攻隐藏实力比
    attack_ratio = official_scored / max(friendly_scored, 0.01)
    # 防守隐藏实力比（失球少 = 防守强，取倒数）
    defense_ratio = friendly_conceded / max(official_conceded, 0.01)

    result["attack_ratio"] = round(attack_ratio, 2)
    result["defense_ratio"] = round(defense_ratio, 2)

    # 判定: 进攻比 > 2.0 或 防守比 > 2.0 表示隐藏实力
    is_hidden = (attack_ratio > 2.0) or (defense_ratio > 2.0)

    if is_hidden:
        # 置信度基于ratio的极端程度
        max_ratio = max(attack_ratio, defense_ratio)
        confidence = min(0.90, 0.50 + (max_ratio - 2.0) * 0.15)
        result["is_hidden_strength"] = True
        result["confidence"] = round(confidence, 3)

        desc_parts = []
        if attack_ratio > 2.0:
            desc_parts.append(f"进攻隐藏({official_scored:.1f} vs {friendly_scored:.1f}友谊)")
        if defense_ratio > 2.0:
            desc_parts.append(f"防守隐藏({friendly_conceded:.1f}友谊失 vs {official_conceded:.1f}正式)")
        result["description"] = "对手隐藏实力: " + ", ".join(desc_parts)

        # E14降权: 隐藏实力 → 降低"反击威胁低"的乐观评估
        # 降权幅度与隐藏程度成正比
        result["e14_weight_adjustment"] = -min(0.4, (max_ratio - 1.0) * 0.2)

    return result

def _poisson(k, lam):
    return max(np.exp(-lam) * lam**k / math.factorial(k), 1e-30)

def _solve_lambda(odds_h: float, odds_d: float, odds_a: float) -> Tuple[float, float, float, np.ndarray]:
    """从1X2赔率用2D网格搜索最优 (λ_H, λ_A, ρ)
    自动检测主/客热门方向，对称搜索"""
    # 热门方向检测：如果客队热门，交换H/A后求解再换回
    swapped = odds_h > odds_a  # 客队热门
    if swapped:
        oh, oa = odds_a, odds_h
    else:
        oh, oa = odds_h, odds_a

    raw_sum = 1/oh + 1/odds_d + 1/oa
    p_target = np.array([1/(oh*raw_sum), 1/(odds_d*raw_sum), 1/(oa*raw_sum)])

    best_loss, best_lh, best_la, best_rho = 999, 1.0, 1.0, 0.0
    best_probs = None

    for lh in np.arange(0.3, 7.0, 0.1):
        for la in np.arange(0.1, 3.0, 0.05):
            for rho in [0.0, -0.05, 0.05, -0.10, 0.10]:
                p_h, p_d, p_a = 0.0, 0.0, 0.0
                for gh in range(13):
                    for ga in range(13):
                        p = _poisson(gh, lh) * _poisson(ga, la)
                        if gh == 0 and ga == 0: p *= (1 - lh*la*rho)
                        elif gh == 0 and ga == 1: p *= (1 + lh*rho)
                        elif gh == 1 and ga == 0: p *= (1 + la*rho)
                        elif gh == 1 and ga == 1: p *= (1 - rho)
                        if gh > ga: p_h += p
                        elif gh == ga: p_d += p
                        else: p_a += p
                total = p_h + p_d + p_a
                probs = np.array([p_h, p_d, p_a]) / max(total, 1e-10)
                loss = np.sum((probs - p_target)**2)
                if loss < best_loss:
                    best_loss, best_lh, best_la, best_rho, best_probs = loss, lh, la, rho, probs

    if swapped:
        return best_la, best_lh, best_rho, p_target  # 换回：求解时H/A互换了
    return best_lh, best_la, best_rho, p_target

def _lambda_to_fair_handicap(lam_h: float, lam_a: float) -> float:
    """从 λ 推导理论让球盘口（绝对值，不区分方向）"""
    goal_diff = abs(lam_h - lam_a)
    if goal_diff > 2.5: return 2.5
    if goal_diff > 2.0: return 2.25
    if goal_diff > 1.7: return 2.0
    if goal_diff > 1.4: return 1.75
    if goal_diff > 1.2: return 1.5
    if goal_diff > 1.0: return 1.25
    if goal_diff > 0.75: return 1.0
    if goal_diff > 0.55: return 0.75
    if goal_diff > 0.35: return 0.5
    if goal_diff > 0.18: return 0.25
    return 0.0

def _estimate_fair_handicap(lam_h: float, lam_a: float, odds_h: float, odds_a: float) -> float:
    """
    混合估算理论让球盘口：
    1. 从 λ 推导（主要）
    2. 从赔率比值修正（兜底，防止 λ 求解不精确）
    取较大值 = 更保守的估算
    """
    # 方法1: λ推导
    hc_from_lambda = _lambda_to_fair_handicap(lam_h, lam_a)

    # 方法2: 赔率比推导（1X2赔率差距越大 → 盘口越深）
    p_h_implied = 1.0 / odds_h
    p_a_implied = 1.0 / odds_a
    ratio = max(p_h_implied, p_a_implied) / max(min(p_h_implied, p_a_implied), 0.01)
    if ratio > 15:  hc_from_odds = 2.5
    elif ratio > 10: hc_from_odds = 2.25
    elif ratio > 7:  hc_from_odds = 2.0
    elif ratio > 5:  hc_from_odds = 1.75
    elif ratio > 3.5: hc_from_odds = 1.5
    elif ratio > 2.5: hc_from_odds = 1.25
    elif ratio > 1.8: hc_from_odds = 0.75
    elif ratio > 1.4: hc_from_odds = 0.5
    elif ratio > 1.15: hc_from_odds = 0.25
    else: hc_from_odds = 0.0

    return max(hc_from_lambda, hc_from_odds)  # 保守估算

def _estimate_volume_ratio(odds_h: float, odds_d: float, odds_a: float,
                            asian_handicap: float, fair_handicap: float) -> Dict[str, float]:
    """
    从赔率结构自主估算资金分布
    核心假设：散户资金大致按赔率隐含概率分布，但热门方因"追强心理"会被放大
    """
    raw_sum = 1/odds_h + 1/odds_d + 1/odds_a
    p_raw = np.array([1/(odds_h*raw_sum), 1/(odds_d*raw_sum), 1/(odds_a*raw_sum)])

    # 热门放大系数：浅盘大热时追强心理呈指数增长
    # 直接用加法模型规避归一化稀释
    fav_idx = 0 if odds_h < odds_a else 2
    handicap_gap = abs(asian_handicap - fair_handicap)
    # 加法放大：每差0.5球追加10%投注占比
    raw_fav = p_raw[fav_idx]
    vol_fav_boosted = min(0.88, raw_fav + handicap_gap * 0.22)
    
    vol = p_raw.copy()
    # 从其他两项按比例扣除
    excess = vol_fav_boosted - raw_fav
    other_indices = [i for i in range(3) if i != fav_idx]
    other_total = sum(p_raw[i] for i in other_indices)
    for i in other_indices:
        vol[i] = p_raw[i] * (1 - excess / max(other_total, 0.01)) if other_total > 0 else p_raw[i]
    vol[fav_idx] = vol_fav_boosted

    # 归一化
    vol /= vol.sum()
    return {"H": float(vol[0]), "D": float(vol[1]), "A": float(vol[2])}

# ════════════════════════════════════════════════════════════════
# 主检测器
# ════════════════════════════════════════════════════════════════

class BookmakerTrapDetector:
    """
    操盘手陷阱检测器 v2.0

    9 大检测引擎，全部可在仅有 1X2+盘口 数据下运行
    """

    # ── E1: 浅盘大热 ──
    def _e1_shallow_hot(self, d: dict) -> Optional[TrapSignal]:
        gap = d['fair_handicap'] - d['asian_handicap']
        if gap > 0.4 and d['volume_ratio_fav'] > 0.65:
            conf = min(0.90, 0.5 + gap * 0.3)
            return TrapSignal(TrapType.SHALLOW_HOT, conf, "underdog_cover",
                f"浅盘大热：理论让{d['fair_handicap']:.1f}实际{d['asian_handicap']:.1f}（浅{abs(gap):.1f}球）+大热",
                {"D": +0.04, "A": +0.04} if d['favorite_is_home'] else {"D": +0.04, "H": +0.04})

    # ── E2: 降赔升水 ──
    def _e2_drop_odds_rise_water(self, d: dict) -> Optional[TrapSignal]:
        if d['odds_trend'] == "dropping" and d['water_trend'] == "rising" and d['volume_ratio_fav'] > 0.65:
            return TrapSignal(TrapType.DROP_ODDS_RISE_WATER, 0.72, "underdog",
                "降赔升水：降赔吸引+升水锁赔付 → 热门爆冷风险 ↑",
                {"H": -0.06, "D": +0.04} if d['favorite_is_home'] else {"A": -0.06, "D": +0.04})

    # ── E3: 升盘降水 → 造热 ──
    def _e3_rise_handicap_drop_water(self, d: dict) -> Optional[TrapSignal]:
        if d['handicap_change'] == "up" and d['water_trend'] == "dropping" and d['favorite_is_home']:
            return TrapSignal(TrapType.RISE_HANDICAP_DROP_WATER, 0.68, "underdog_cover",
                "升盘降水：制造稳赢假象 → 追强队陷阱",
                {"D": +0.03})

    # ── E4: 平半高水死扛 ──
    def _e4_half_ball_high_water(self, d: dict) -> Optional[TrapSignal]:
        if abs(d['asian_handicap'] - 0.25) < 0.05 and d['water_level'] >= 0.98:
            return TrapSignal(TrapType.HALF_BALL_HIGH_WATER, 0.78, "draw",
                "平半高水死扛：庄家不降水位 → 打平概率极高",
                {"D": +0.10, "H": -0.05})

    # ── E5: 临场突变 ──
    def _e5_last_minute_change(self, d: dict) -> Optional[TrapSignal]:
        if d['handicap_change_magnitude'] > 0.3:
            if not d['multi_bookmaker_sync']:
                return TrapSignal(TrapType.LAST_MINUTE_CHANGE, 0.82, "trap",
                    "临场突变(单家)：非真实信号 → 反向思考")
            return TrapSignal(TrapType.LAST_MINUTE_CHANGE, 0.55, "real_info",
                "临场突变(多机构同步)：可能真实信息 → 顺势")

    # ── E6: 深盘诱杀 [新增] ──
    def _e6_deep_handicap_trap(self, d: dict) -> Optional[TrapSignal]:
        """强队深让但 λ 实力差不足以支撑 → 造热上盘"""
        gap = d['asian_handicap'] - d['fair_handicap']
        if gap > 0.5 and d['volume_ratio_fav'] > 0.70:
            conf = min(0.85, 0.55 + gap * 0.2)
            return TrapSignal(TrapType.DEEP_HANDICAP_TRAP, conf, "underdog_cover",
                f"深盘诱杀：实力差仅{d['lam_diff']:.1f}球但让{d['asian_handicap']:.1f}球 → 赢球输盘高概率",
                {"D": +0.04})

    # ── E7: 抽水率异常 [新增] ──
    def _e7_overround_anomaly(self, d: dict) -> Optional[TrapSignal]:
        """
        抽水率 = 庄家的态度信号
        <5%: 极度自信 = 热门单边涌入 → 爆冷时庄家大赚
        5-7%: 正常偏低
        7-10%: 正常
        >10%: 赛果不确定性大 → 庄家需要更多对冲
        """
        overround = d['overround']
        vol_fav = d['volume_ratio_fav']

        if overround < 0.05 and vol_fav > 0.70:
            return TrapSignal(TrapType.OVERROUND_ANOMALY, 0.72, "reverse",
                f"抽水极低({overround*100:.1f}%)+资金集中({vol_fav*100:.0f}%) → 庄家设套，冷门概率↑",
                {"D": +0.04, "H": -0.04} if d['favorite_is_home'] else {"D": +0.04, "A": -0.04})

        if overround > 0.10:
            return TrapSignal(TrapType.OVERROUND_ANOMALY, 0.60, "uncertain",
                f"抽水偏高({overround*100:.1f}%) → 赛果不确定性大，庄家需要对冲",
                {"D": +0.03})

    # ── E8: 波胆防线 [新增] ──
    def _e8_score_odds_barrier(self, d: dict) -> Optional[TrapSignal]:
        """波胆赔率中是否存在异常高分赔率"""
        score_odds = d.get('score_odds', {})
        if not score_odds:
            return None

        overround = d.get('overround', 0.06)
        V = 1.0 + overround
        lam_h, lam_a = d.get('lam_h', 1.0), d.get('lam_a', 1.0)

        rp_values = []
        for key, odds_real in score_odds.items():
            parts = key.split('-')
            if len(parts) != 2: continue
            gh, ga = int(parts[0]), int(parts[1])
            p_theo = _poisson(gh, lam_h) * _poisson(ga, lam_a)
            odds_theo = 1.0 / max(p_theo * V, 1e-8)
            rp = odds_real / max(odds_theo, 1.01)
            rp_values.append(rp)

        if not rp_values:
            return None

        # 防线判定：任意比分 RP > 8 或 超过1/3的比分 RP > 3
        max_rp = max(rp_values)
        high_rp_count = sum(1 for r in rp_values if r > 3.0)
        high_rp_ratio = high_rp_count / len(rp_values)

        if max_rp > 8.0:
            return TrapSignal(TrapType.SCORE_ODDS_BARRIER, 0.85, "barrier",
                f"波胆防线：RP_max={max_rp:.1f} → 庄家对某些比分设防",
                {"D": +0.05})  # 防线触发 = 庄家担心低概率比分打出
        elif high_rp_ratio > 0.3:
            return TrapSignal(TrapType.SCORE_ODDS_BARRIER, 0.65, "mild_barrier",
                f"波胆轻防：{high_rp_count}/{len(rp_values)}比分RP>3",
                {"D": +0.03})

    # ── E9: 凯利背离 [新增] ──
    def _e9_kelly_divergence(self, d: dict) -> Optional[TrapSignal]:
        """
        凯利指数 = 庄家对风险的容忍度
        凯利 < 0.9: 庄家压低赔付 → 这个方向概率高
        凯利 > 1.2: 庄家不控制赔付 → 不担心这个方向
        """
        if d.get('score_odds'):
            score_odds = d['score_odds']
            lam_h, lam_a = d.get('lam_h', 1.0), d.get('lam_a', 1.0)
            kelly_list = []
            for key, odds_real in score_odds.items():
                parts = key.split('-')
                if len(parts) != 2: continue
                gh, ga = int(parts[0]), int(parts[1])
                p_theo = _poisson(gh, lam_h) * _poisson(ga, lam_a)
                kelly_list.append(1.0 / max(odds_real * p_theo, 1e-8))

            avg_kelly = np.mean(kelly_list)
            if avg_kelly > 1.5:
                return TrapSignal(TrapType.KELLY_DIVERGENCE, 0.68, "bookmaker_generous",
                    f"凯利偏高(avg={avg_kelly:.2f}) → 庄家慷慨定价，对赛果极度自信",
                    {"D": -0.03})

    # ── E13: 历史参照偏差修正 ──
    def _e13_historical_bias(self, d: dict) -> Optional[TrapSignal]:
        """战术升级 → 历史参照失真"""
        sc = d.get('squad_quality_change', 0)
        ts = d.get('tactical_shift', 0)
        if sc > 0.15 or ts > 0.15:
            return TrapSignal(TrapType.HISTORICAL_BIAS, min(0.90, 0.50+(sc+ts)*0.5),
                "downgrade_trap_signals",
                f"历史参照偏差(squad={sc:.2f},tac={ts:.2f})",
                {})

    # ── E14: 对手反击威胁评估 ──
    def _e14_counter_threat(self, d: dict) -> Optional[TrapSignal]:
        ct = d.get('counter_threat_level', 0.5)
        base_conf = 0.70
        base_desc = f"弱队反击威胁极低({ct:.1f}) → 强队零封率高"
        weight_adj = {"H": +0.03, "D": -0.03}

        # v3.1: 对手隐藏实力修正
        hidden_strength = d.get('hidden_strength_result', {})
        if hidden_strength.get('is_hidden_strength'):
            e14_adj = hidden_strength.get('e14_weight_adjustment', 0)
            base_conf = max(0.30, base_conf + e14_adj)
            base_desc += f" [隐藏实力修正: {e14_adj:+.2f}]"
            # 降低乐观的H加成
            weight_adj["H"] = max(0.0, weight_adj["H"] + e14_adj * 0.3)
            weight_adj["D"] = min(0.0, weight_adj["D"] - e14_adj * 0.3)

        if ct < 0.3 or (hidden_strength.get('is_hidden_strength') and hidden_strength.get('confidence', 0) > 0.6):
            final_conf = min(0.85, max(0.25, base_conf))
            return TrapSignal(TrapType.COUNTER_THREAT_LOW, final_conf, "low_upset_risk",
                base_desc, weight_adj)

    # ── E15: OU-CS背离检测 [v3.1新增] ──
    def _e15_ou_cs_divergence(self, d: dict) -> Optional[TrapSignal]:
        """大小球 vs 波胆背离 → 潜在诱导信号"""
        ou_line = d.get('over_under_line')
        under_water = d.get('under_water')
        over_water = d.get('over_water')
        cs_other = d.get('score_odds_other')
        lam_h = d.get('lam_h', 1.0)
        lam_a = d.get('lam_a', 1.0)

        result = detect_ou_cs_divergence(
            ou_line, under_water, over_water, cs_other,
            lam_h, lam_a
        )

        if result is None or result['divergence_level'] == 'none':
            return None

        conf = result['confidence']
        level = result['divergence_level']
        desc = result['description']
        wr = result['weight_reduction']

        # 矛盾信号 → 偏保守：提高平局概率，降低热门方向
        fav_is_home = d.get('favorite_is_home', True)
        adj = {"D": +0.04 * wr * 4.0}
        if fav_is_home:
            adj["H"] = -0.03 * wr * 4.0
        else:
            adj["A"] = -0.03 * wr * 4.0

        return TrapSignal(TrapType.NONE, conf, "ou_cs_divergence",
            desc, adj)

    # ── E16: 反波胆防线预警 [v3.1新增] ──
    def _e16_anti_cs_alert(self, d: dict) -> Optional[TrapSignal]:
        """反波胆特征异常 → 庄家对某些比分极度设防"""
        anti_cs = d.get('anti_cs_features', {})
        lock_score = anti_cs.get('lock_score', 0)
        cs_gap = anti_cs.get('cs_gap', 0.0)
        cs_gap_interp = anti_cs.get('cs_gap_interpretation', 'normal')

        # lock_score >= 3: 多个比分被锁盘 → 庄家极度不看好这些比分
        if lock_score >= 3:
            return TrapSignal(TrapType.SCORE_ODDS_BARRIER, 0.80, "lock_barrier",
                f"反波胆防线: {lock_score}个比分被锁盘(赔率>50) → 庄家设防",
                {"D": +0.05})

        # cs_gap 极端 → 庄家信息优势明显
        if cs_gap_interp == 'very_high_gap' and cs_gap > 5.0:
            return TrapSignal(TrapType.SCORE_ODDS_BARRIER, 0.72, "cs_gap_alert",
                f"反波胆信息差: cs_gap={cs_gap:.1f} → 庄家对特定比分有强烈预判",
                {"D": +0.04})

        return None

    # ── E10: 深盘退热（保护） ──
    def _e10_deep_cooling(self, d: dict) -> Optional[TrapSignal]:
        is_deep = d['asian_handicap'] > d['fair_handicap'] + 0.3
        if is_deep and d['water_level'] > 0.98:
            return TrapSignal(TrapType.DEEP_COOLING, 0.72, "favorite_win",
                f"深盘退热：深开{d['asian_handicap']:.1f}球+高水 → 真实保护上盘",
                {"H": +0.04} if d['favorite_is_home'] else {"A": +0.04})

    # ── E11: 资金过热+赔率不动 ──
    def _e11_fund_imbalance(self, d: dict) -> Optional[TrapSignal]:
        if d['volume_ratio_fav'] > 0.70 and d['odds_trend'] in ("stable", "rising"):
            conf = 0.80 + (d['volume_ratio_fav'] - 0.70) * 0.5
            dir_sign = "H" if d['favorite_is_home'] else "A"
            return TrapSignal(TrapType.FUND_IMBALANCE, conf, "reverse",
                f"资金过热({d['volume_ratio_fav']*100:.0f}%)但赔率不动 → 庄家不怕，反向",
                {dir_sign: -0.10})

    # ── E12: 欧亚联动 ──
    def _e12_eu_asian_linkage(self, d: dict) -> List[TrapSignal]:
        sigs = []
        p_book = d['p_book']
        # 规则1: 欧盘主胜高但亚盘浅 → 诱下
        if p_book[0] > 0.55 and d['asian_handicap'] < 0.5:
            sigs.append(TrapSignal(TrapType.NONE, 0.68, "underdog_cover",
                f"欧亚背离：欧主胜{p_book[0]*100:.0f}%亚浅({d['asian_handicap']}) → 诱下", {"D": +0.04, "A": +0.03}))
        # 规则2: 平赔低但亚盘半球 → 诱平陷阱(原"忽略平局"文案矛盾修正)
        # v3.2修正: 原文案"忽略平局：欧平{p}%亚半球 → 多走平"自相矛盾(忽略又多走)
        #          方向语义改为"ignore_draw", 文案明确为诱平陷阱, 优先排除平局
        if p_book[1] > 0.24 and 0.4 < d['asian_handicap'] < 0.6:
            sigs.append(TrapSignal(TrapType.NONE, 0.73, "ignore_draw",
                "平局热度偏高(欧平{p:.0f}%+亚半球), 机构数据存在诱平陷阱, 本场优先排除平局走势".format(p=p_book[1]*100),
                {"D": -0.08, "H": +0.04, "A": +0.04}))
        # 规则3: 客胜低但亚深 → 赢球输盘
        if p_book[2] < 0.15 and d['asian_handicap'] > 1.5:
            sigs.append(TrapSignal(TrapType.NONE, 0.63, "underdog_cover",
                "造热上盘：客胜低亚深让 → 赢球输盘", {"D": +0.04}))
        return sigs

    # ════════════════════════════════════════════════════════════
    # 综合检测
    # ════════════════════════════════════════════════════════════

    def detect(self, match_data: Dict[str, Any]) -> TrapReport:
        """
        综合陷阱检测

        最低输入:
          {"home": str, "away": str, "league": str,
           "odds_h": float, "odds_d": float, "odds_a": float,
           "asian_handicap": Optional[float]}

        可选增强:
          water_level, water_trend, odds_trend, score_odds,
          handicap_change, handicap_change_magnitude, multi_bookmaker_sync
        """
        h, a = match_data.get("home", ""), match_data.get("away", "")
        report = TrapReport(home=h, away=a, league=match_data.get("league", "其他"))

        oh = match_data.get("odds_h", 2)
        od = match_data.get("odds_d", 3.5)
        oa = match_data.get("odds_a", 4)
        raw_sum = 1/oh + 1/od + 1/oa
        overround = raw_sum - 1.0
        p_book = np.array([1/(oh*raw_sum), 1/(od*raw_sum), 1/(oa*raw_sum)])

        # → λ 自主推导
        lam_h, lam_a, lam_rho, _ = _solve_lambda(oh, od, oa)
        fair_hc = _estimate_fair_handicap(lam_h, lam_a, oh, oa)

        # → 盘口：输入或推导
        asian_hc = match_data.get("asian_handicap")
        if asian_hc is None:
            asian_hc = fair_hc  # 无数据时默认公平

        # → 资金分布自主估算
        volume = _estimate_volume_ratio(oh, od, oa, asian_hc, fair_hc)
        favorite_is_home = oh < oa
        vol_fav = volume['H'] if favorite_is_home else volume['A']

        # 构建统一数据字典
        d = {
            "odds_h": oh, "odds_d": od, "odds_a": oa,
            "p_book": p_book, "overround": overround,
            "lam_h": lam_h, "lam_a": lam_a, "lam_rho": lam_rho,
            "lam_diff": abs(lam_h - lam_a),
            "fair_handicap": fair_hc,
            "asian_handicap": asian_hc,
            "favorite_is_home": favorite_is_home,
            "volume": volume,
            "volume_ratio_fav": vol_fav,
            "water_level": match_data.get("water_level", 0.92),
            "water_trend": match_data.get("water_trend", "stable"),
            "odds_trend": match_data.get("odds_trend", "stable"),
            "handicap_change": match_data.get("handicap_change", "stable"),
            "handicap_change_magnitude": match_data.get("handicap_change_magnitude", 0),
            "multi_bookmaker_sync": match_data.get("multi_bookmaker_sync", True),
            "score_odds": match_data.get("score_odds"),
            # ── 战术/阵容上下文 ──
            "squad_quality_change": match_data.get("squad_quality_change", 0),
            "tactical_shift": match_data.get("tactical_shift", 0),
            "counter_threat_level": match_data.get("counter_threat_level", 0.5),
            # ── v3.1: 大小球数据 ──
            "over_under_line": match_data.get("over_under_line"),
            "under_water": match_data.get("under_water"),
            "over_water": match_data.get("over_water"),
            "score_odds_other": match_data.get("score_odds_other"),
        }

        # v3.1: 反波胆特征工程
        anti_cs = compute_anti_cs_features(
            match_data.get("score_odds"), lam_h, lam_a
        )
        d["anti_cs_features"] = anti_cs

        # v3.1: 对手隐藏实力检测
        hidden_strength = check_hidden_strength(
            opponent_official_goals_scored=match_data.get("opp_official_goals_scored"),
            opponent_friendly_goals_scored=match_data.get("opp_friendly_goals_scored"),
            opponent_official_goals_conceded=match_data.get("opp_official_goals_conceded"),
            opponent_friendly_goals_conceded=match_data.get("opp_friendly_goals_conceded"),
        )
        d["hidden_strength_result"] = hidden_strength

        # 运行全部检测引擎
        engines = [
            self._e1_shallow_hot, self._e2_drop_odds_rise_water,
            self._e3_rise_handicap_drop_water, self._e4_half_ball_high_water,
            self._e5_last_minute_change, self._e6_deep_handicap_trap,
            self._e7_overround_anomaly, self._e8_score_odds_barrier,
            self._e9_kelly_divergence, self._e10_deep_cooling,
            self._e11_fund_imbalance, self._e13_historical_bias,
            self._e14_counter_threat, self._e15_ou_cs_divergence,
            self._e16_anti_cs_alert,
        ]

        signals = []
        for engine in engines:
            s = engine(d)
            if s: signals.append(s)
        signals.extend(self._e12_eu_asian_linkage(d))

        # 联赛特性修正
        league_adj = self._league_adjustment(match_data.get("league", "其他"))

        # ═══ v3.1 复合加权评分 ═══
        # Score_trap = (Σ w_i · F_i) × W_ambiguity × W_hist × W_tactic × W_market

        # 基础分：仅计算真正的陷阱信号（排除信息型/提示型信号）
        display_signals = [s for s in signals if s.direction not in (
            "downgrade_trap_signals", "low_upset_risk"
        )]
        raw_base_score = sum(s.confidence * 2.5 for s in display_signals)

        # ── W_ambiguity: 赔率两面性修正 [v3.1] ──
        w_ambiguity = compute_w_ambiguity(display_signals)

        # ── W_hist: 梯度历史退化 ──
        squad_change = match_data.get("squad_quality_change", 0)
        years_since = match_data.get("years_since_last_h2h", 0)
        w_hist = compute_w_hist(squad_change, years_since)

        # ── W_tactic: 战术动态降权 ──
        coach_changed = match_data.get("coach_changed", False)
        core_lost = match_data.get("core_player_lost", False)
        temp_rotation = match_data.get("temporary_rotation", False)
        w_tactic = compute_w_tactic(coach_changed, core_lost, temp_rotation)

        # ── W_market: 市场风控修正 ──
        rp_level = match_data.get("rp_level", 0)
        is_final = match_data.get("match_type", "league") == "final"
        w_market = compute_w_market(rp_level, is_final)

        # ── v3.1: OU-CS背离降权 ──
        w_ou_cs = 1.0
        for s in signals:
            if s.direction == "ou_cs_divergence":
                div_result = detect_ou_cs_divergence(
                    match_data.get("over_under_line"),
                    match_data.get("under_water"),
                    match_data.get("over_water"),
                    match_data.get("score_odds_other"),
                    lam_h, lam_a,
                )
                if div_result:
                    w_ou_cs = 1.0 - div_result["weight_reduction"]
                break

        # 复合评分
        report.aggregate_score = raw_base_score * w_ambiguity * w_hist * w_tactic * w_market * w_ou_cs
        report.raw_score = raw_base_score
        report.weights = {
            "w_ambiguity": round(w_ambiguity, 3),
            "w_hist": round(w_hist, 3),
            "w_tactic": round(w_tactic, 3),
            "w_market": round(w_market, 3),
            "w_ou_cs": round(w_ou_cs, 3),
        }

        # ── 动态阈值判定 ──
        mt = match_data.get("match_type", "league")
        sg = match_data.get("strength_gap", "normal")
        threshold = _get_threshold(mt, sg)
        safe = DYNAMIC_THRESHOLDS.get(mt, {"safe": 2.0})["safe"]

        if report.aggregate_score >= threshold:
            report.recommendation = f"🔴 重度陷阱 ({len(display_signals)}信号/{report.aggregate_score:.1f}分 阈值{threshold:.1f})"
        elif report.aggregate_score >= safe:
            report.recommendation = f"🟡 轻度风险 ({len(display_signals)}信号/{report.aggregate_score:.1f}分)"
        else:
            report.recommendation = f"🟢 安全区间 ({report.aggregate_score:.1f}分)"

        # 保留完整信号列表
        report.signals = display_signals
        bias_signal = next((s for s in signals if s.direction == "downgrade_trap_signals"), None)
        if bias_signal:
            report.signals.append(bias_signal)

        # ── 概率修正 ──
        adj = {"H": 0.0, "D": 0.0, "A": 0.0}
        for s in signals:
            for k, v in s.weight_adjustment.items():
                base = k.split("_")[0]
                adj[base] = adj.get(base, 0) + v
        for k, v in league_adj.items():
            base = k.split("_")[0]
            adj[base] = adj.get(base, 0) + v

        # 陷阱评分联动概率修正：高陷阱区间扩大调整幅度
        trap_amplify = 1.0 + min(0.5, report.aggregate_score * 0.08)
        for k in adj:
            adj[k] *= trap_amplify
            adj[k] = max(-0.15, min(0.15, adj[k]))

        report.adjusted_probs = {
            "H": max(0.01, p_book[0] + adj.get("H", 0)),
            "D": max(0.01, p_book[1] + adj.get("D", 0)),
            "A": max(0.01, p_book[2] + adj.get("A", 0)),
        }
        total_p = sum(report.adjusted_probs.values())
        for k in report.adjusted_probs:
            report.adjusted_probs[k] /= total_p

        # ── 陷阱特征向量 (v3.1 扩展) ──
        report.trap_features = {
            "raw_score": round(raw_base_score, 1),
            "trap_score": round(report.aggregate_score, 1),
            "threshold": round(threshold, 1),
            "w_ambiguity": round(w_ambiguity, 3),
            "w_hist": round(w_hist, 3),
            "w_tactic": round(w_tactic, 3),
            "w_market": round(w_market, 3),
            "w_ou_cs": round(w_ou_cs, 3),
            "n_signals": len(display_signals),
            "overround": round(overround, 4),
            "handicap_gap": round(asian_hc - fair_hc, 2),
            "vol_fav_ratio": round(vol_fav, 3),
            "lam_diff": round(abs(lam_h - lam_a), 2),
            "draw_prob_adj": round(adj.get("D", 0), 3),
            "favorite_prob_adj": round(adj.get("H" if favorite_is_home else "A", 0), 3),
            # v3.1 新特征
            "anti_cs_lock_score": anti_cs.get("lock_score", 0),
            "anti_cs_gap": anti_cs.get("cs_gap", 0.0),
            "anti_cs_min_odds": anti_cs.get("cs_min_odds", 0.0),
            "hidden_strength": 1 if hidden_strength.get("is_hidden_strength") else 0,
            "hidden_attack_ratio": hidden_strength.get("attack_ratio", 1.0),
            "hidden_defense_ratio": hidden_strength.get("defense_ratio", 1.0),
        }

        return report

    def _league_adjustment(self, league: str) -> Dict[str, float]:
        traits = LEAGUE_TRAITS.get(league, {})
        adj = {"D": 0.0, "H_cover": 0.0, "A_cover": 0.0}
        if traits.get("half_ball_draw"): adj["D"] += 0.03
        if traits.get("draw_rate") == "high": adj["D"] += 0.02
        if traits.get("deep_trap"): adj["H_cover"] -= 0.03
        if traits.get("deep_easy"): adj["H_cover"] += 0.03
        if traits.get("underdog_strong"): adj["A_cover"] += 0.03
        return adj

# ════════════════════════════════════════════════════════════════
# 快速入口
# ════════════════════════════════════════════════════════════════

def quick_diagnose(home: str, away: str, odds_h: float, odds_d: float, odds_a: float,
                   asian_handicap: Optional[float] = None, league: str = "其他",
                   water_level: float = 0.92, score_odds: Optional[dict] = None) -> TrapReport:
    """
    快速诊断 — 仅需 1X2 赔率即可运行
    所有高级特征从赔率结构自主推导
    """
    return BookmakerTrapDetector().detect({
        "home": home, "away": away, "league": league,
        "odds_h": odds_h, "odds_d": odds_d, "odds_a": odds_a,
        "asian_handicap": asian_handicap,
        "water_level": water_level,
        "water_trend": "stable",
        "odds_trend": "stable",
        "score_odds": score_odds,
        "multi_bookmaker_sync": True,
        "handicap_change": "stable",
        "handicap_change_magnitude": 0,
    })

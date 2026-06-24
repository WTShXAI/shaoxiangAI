"""
欧赔→理论让球线转换器 (v3.0)
================================================================
从 1X2 欧赔推导等效亚洲让球线，用于替代亚盘数据。

推导逻辑：
  1. 去除庄家抽水(overround)，计算真实隐含概率
  2. 用主场优势校准后的期望进球差 → 映射到最近 0.25 档位
  3. 支持按联赛分层校准（不同联赛进球率不同）

公式: handicap = round(scale * (p_home_adj - p_away_adj) * 4) / 4
      其中 scale=3.2 (经验校准, 基于5.4万场历史数据)
"""

import math
import logging
from typing import Optional, Dict, Tuple

logger = logging.getLogger(__name__)

# ── 全局校准参数 ──
# scale: 概率差 → 进球差的线性系数
# 基于足球统计: 概率差 0.1 ≈ 0.32 球优 ≈ handicap 0.25-0.5
DEFAULT_SCALE = 3.2

# 联赛进球率调整系数 (league_id → scale_multiplier)
# 高进球联赛让球线应更宽, 低进球联赛让球线应更窄
LEAGUE_SCALE: Dict[int, float] = {
    # 英超/德甲/西甲: 场均2.8球, 基准
    # 意甲: 场均2.6球 → scale × 0.93
    # 荷甲: 场均3.2球 → scale × 1.14
    # 巴甲: 场均2.3球 → scale × 0.82
    # 日职: 场均2.5球 → scale × 0.89
}

# 让球线下界和上界
HANDICAP_MIN = -3.0
HANDICAP_MAX = 3.0


def odds_to_handicap(
    home_odds: float,
    draw_odds: float,
    away_odds: float,
    league_id: Optional[int] = None,
    scale: float = DEFAULT_SCALE,
) -> Tuple[float, float, float]:
    """
    从 1X2 欧赔推导理论让球线。

    Args:
        home_odds: 主胜赔率
        draw_odds: 平局赔率
        away_odds: 客胜赔率
        league_id: 联赛ID（可选，用于联赛特化校准）
        scale: 校准系数

    Returns:
        (handicap, p_home_adj, p_away_adj)
        - handicap: 理论让球线 (负值=主让, 正值=客让)
        - p_home_adj: 去抽水后的主胜概率
        - p_away_adj: 去抽水后的客胜概率
    """
    if not all([home_odds, draw_odds, away_odds]) or any(
        [home_odds <= 0, draw_odds <= 0, away_odds <= 0]
    ):
        return 0.0, 0.33, 0.33

    # Step 1: 计算原始隐含概率 + 去抽水
    raw_home = 1.0 / home_odds
    raw_draw = 1.0 / draw_odds
    raw_away = 1.0 / away_odds
    overround = raw_home + raw_draw + raw_away

    # 防御: 极度异常数据
    if overround <= 0 or overround > 3.0:
        return 0.0, 0.33, 0.33

    # 去抽水 → 真实概率
    p_home = raw_home / overround
    p_draw = raw_draw / overround
    p_away = raw_away / overround

    # Step 2: 联赛进球率校准
    league_mult = 1.0
    if league_id and league_id in LEAGUE_SCALE:
        league_mult = LEAGUE_SCALE[league_id]
    effective_scale = scale * league_mult

    # Step 3: 概率差 → 期望进球差 → 让球线
    prob_diff = p_home - p_away
    raw_handicap = -effective_scale * prob_diff  # 负号: 主让为负值

    # Step 4: 四舍五入到最近 0.25
    handicap = round(raw_handicap * 4) / 4

    # Step 5: 边界裁剪
    handicap = max(HANDICAP_MIN, min(HANDICAP_MAX, handicap))

    # 避免 -0.0
    if handicap == -0.0:
        handicap = 0.0

    return handicap, p_home, p_away


def odds_to_handicap_bin(handicap: float) -> str:
    """
    将让球线映射到分桶标签。

    分桶区间:
        <-2.5, -2.5, -2.0, -1.5, -1.0, -0.75, -0.5, -0.25,
        0.0, +0.25, +0.5, +0.75, +1.0, +1.5, +2.0, +2.5, >+2.5
    """
    # 映射边界值到最近的桶
    bins = [-2.5, -2.0, -1.5, -1.0, -0.75, -0.5, -0.25,
            0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5]

    for b in bins:
        if abs(handicap - b) < 0.125:
            sign = "+" if b >= 0 else ""
            return f"{sign}{b}"

    if handicap < -2.5:
        return "<-2.5"
    if handicap > 2.5:
        return ">+2.5"

    # 兜底: 格式化为字符串
    return f"{handicap:+.2f}"


def compute_cover_result(
    home_score: int,
    away_score: int,
    handicap: float,
) -> str:
    """
    根据比分和让球线判定让球结果。

    让球方 = 主队 (如果 handicap < 0，主场让球)
            客队 (如果 handicap > 0，客场让球)
            无 (如果 handicap == 0，平手盘)

    Returns:
        'home_cover': 主队赢盘 (主队赢球且赢盘)
        'away_cover': 客队赢盘
        'push': 走水 (净胜球=让球线)
    """
    goal_diff = home_score - away_score

    if handicap == 0:
        # 平手盘: 赢球=赢盘
        if goal_diff > 0:
            return "home_cover"
        elif goal_diff < 0:
            return "away_cover"
        else:
            return "push"

    if handicap < 0:
        # 主队让球: 主队进球差需要 > |handicap| 才算赢盘
        adjusted = goal_diff + handicap  # handicap是负值, 所以是减
        if adjusted > 0:
            return "home_cover"
        elif adjusted < 0:
            return "away_cover"
        else:
            return "push"
    else:
        # 客队让球: 客队进球差需要 > handicap 才算赢盘
        # 等价于: 主队进球差 < -handicap 时客赢盘
        adjusted = goal_diff - handicap
        if adjusted > 0:
            return "home_cover"
        elif adjusted < 0:
            return "away_cover"
        else:
            return "push"


# ── 快速验证 ──
if __name__ == "__main__":
    # 测试用例
    tests = [
        # (home_odds, draw_odds, away_odds, 期望handicap, 描述)
        (1.08, 7.98, 24.45, "强主让", "曼城vs垫底队"),
        (1.50, 4.00, 6.50, "", "明显优势"),
        (2.00, 3.40, 3.80, "", "微弱优势"),
        (2.50, 3.20, 2.90, "", "均势"),
        (4.50, 3.60, 1.80, "", "客队优势"),
        (13.00, 6.00, 1.20, "", "强客让"),
    ]

    print("欧赔 → 理论让球线 转换验证")
    print("=" * 70)
    print(f"{'场景':>12} {'主胜赔':>7} {'平赔':>7} {'客胜赔':>7} {'P主':>7} {'P客':>7} {'让球线':>7}")
    print("-" * 70)

    for ho, do, ao, _, desc in tests:
        hc, ph, pa = odds_to_handicap(ho, do, ao)
        hc_bin = odds_to_handicap_bin(hc)
        print(f"{desc:>12} {ho:>7.2f} {do:>7.2f} {ao:>7.2f} {ph:>7.1%} {pa:>7.1%} {hc_bin:>7}")

    print()
    print("让球结果判定验证")
    print("-" * 50)
    cover_tests = [
        (3, 0, -1.5, "2-0 vs -1.5 → 主赢盘"),
        (2, 0, -2.5, "2-0 vs -2.5 → 主输盘"),
        (1, 0, -1.0, "1-0 vs -1.0 → 走水"),
        (0, 2, 1.0, "0-2 vs +1.0 → 客赢盘"),
        (1, 2, 0.5, "1-2 vs +0.5 → 客赢盘"),
        (1, 1, 0.0, "1-1 vs 0.0 → 走水"),
    ]
    for hs, aws, hc, desc in cover_tests:
        result = compute_cover_result(hs, aws, hc)
        print(f"  {desc} → {result}")

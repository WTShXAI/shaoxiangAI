#!/usr/bin/env python3
"""
哨响AI - 跨市场套利检测模型
===========================
检测不同博彩市场之间的定价偏差，识别潜在套利机会。

检测维度:
  1. 1X2套利检测: 不同博彩公司之间的胜平负赔率套利
  2. 亚欧盘口差检测: 亚洲盘口与欧洲盘口的隐含概率偏差
  3. 大小球关联检测: 大小球盘口与胜平负概率的结构关联
"""
import numpy as np
from typing import Dict, List
from dataclasses import dataclass

@dataclass
class ArbitrageOpportunity:
    """套利机会"""
    match: str
    market_type: str       # 1x2_arbitrage / asian_euro_gap / over_under_corr
    bookmakers: List[str]
    odds_combination: Dict
    profit_margin: float   # 利润率(%)
    risk_level: str        # low / medium / high
    confidence: float      # 0~1

class CrossMarketArbitrage:
    """
    跨市场套利检测器

    用法:
        detector = CrossMarketArbitrage()
        result = detector.full_scan("曼联 vs 利物浦", {
            "1x2": {"home": [1.8, 1.9], "draw": [3.5, 3.6], "away": [4.0, 4.2]},
            "asian": {"handicap": -0.5, "home": [1.85], "away": [2.0]},
            "over_under": {"line": 2.5, "over": [1.9], "under": [1.95]}
        })
    """

    # === 基础计算 ===

    @staticmethod
    def calculate_implied_prob(odds: float) -> float:
        """计算隐含概率"""
        return 1.0 / odds if odds > 0 else 0.0

    # === 1X2 套利检测 ===

    def check_1x2_arbitrage(self, home_odds_list: List[float],
                              draw_odds_list: List[float],
                              away_odds_list: List[float]) -> Dict | None:
        """
        检测不同博彩公司间的1X2套利

        核心逻辑: 取各家最优赔率，若隐含概率之和 < 1.0，则存在套利空间

        Args:
            home_odds_list: 各家主胜赔率 [1.8, 1.9, 1.75]
            draw_odds_list: 各家平局赔率 [3.5, 3.6, 3.4]
            away_odds_list: 各家客胜赔率 [4.0, 4.2, 3.8]
        """
        best_home = max(home_odds_list) if home_odds_list else 0
        best_draw = max(draw_odds_list) if draw_odds_list else 0
        best_away = max(away_odds_list) if away_odds_list else 0

        if best_home <= 0 or best_draw <= 0 or best_away <= 0:
            return None

        total = 1 / best_home + 1 / best_draw + 1 / best_away

        if total < 1.0:
            margin = (1 - total) * 100
            return {
                "profit_margin": round(margin, 2),
                "best_home": best_home,
                "best_draw": best_draw,
                "best_away": best_away,
                "total_implied": round(total, 4),
            }
        return None

    # === 亚欧盘口差检测 ===

    def detect_asian_european_gap(self, euro_home: float, euro_away: float,
                                    asian_handicap: float,
                                    asian_home: float, asian_away: float) -> Dict | None:
        """
        检测亚洲盘与欧洲盘之间的隐含概率偏差

        核心逻辑: 比较欧赔隐含的主客差 vs 亚盘隐含的主客差
        偏差 > 0.05 即存在市场分歧
        """
        if euro_home <= 0 or euro_away <= 0 or asian_home <= 0 or asian_away <= 0:
            return None

        euro_diff = abs(1 / euro_home - 1 / euro_away)
        asian_diff = abs(1 / asian_home - 1 / asian_away)
        gap = abs(euro_diff - asian_diff)

        if gap > 0.05:
            return {
                "gap": round(gap, 4),
                "euro_diff": round(euro_diff, 4),
                "asian_diff": round(asian_diff, 4),
                "opportunity": gap > 0.10,
            }
        return None

    # === 大小球关联检测 ===

    def over_under_correlation(self, home_prob: float, away_prob: float,
                                 line: float, over_odds: float,
                                 under_odds: float) -> Dict | None:
        """
        检测大小球盘口与胜平负概率的结构性偏差

        核心逻辑: 基于胜平负概率推导预期进球数，与大小球隐含概率对比
        偏差越大 → 可能存在定价错误
        """
        if over_odds <= 0 or under_odds <= 0:
            return None

        # 大小球隐含的"大球"概率
        implied_over = 1 / over_odds

        # 基于胜平负概率推导预期进球（经验公式）
        expected_goals = -np.log(1 - home_prob) + 0.5
        expected_over_prob = 1 - np.exp(-expected_goals / max(line, 0.5))
        deviation = abs(implied_over - expected_over_prob)

        return {
            "implied_over": round(implied_over, 3),
            "expected_over": round(expected_over_prob, 3),
            "deviation": round(deviation, 3),
            "signal": "over" if expected_over_prob > implied_over else "under",
        }

    # === 综合扫描 ===

    def full_scan(self, match_name: str, odds_data: Dict) -> Dict:
        """
        全面扫描所有可用的套利机会

        Args:
            match_name: 比赛名称（如 "曼联 vs 利物浦"）
            odds_data: 赔率数据结构:
                {
                    "1x2": {"home": [...], "draw": [...], "away": [...]},
                    "asian": {"handicap": -0.5, "home": [...], "away": [...]},
                    "over_under": {"line": 2.5, "over": [...], "under": [...]}
                }

        Returns:
            扫描结果，含 opportunity 列表
        """
        results = {
            "match": match_name,
            "opportunities": [],
            "total_found": 0,
        }

        # 1. 1X2套利
        if "1x2" in odds_data:
            d = odds_data["1x2"]
            arb = self.check_1x2_arbitrage(
                d.get("home", []), d.get("draw", []), d.get("away", [])
            )
            if arb:
                results["opportunities"].append({"type": "1x2_arbitrage", **arb})

        # 2. 亚欧盘口差
        if "asian" in odds_data and "1x2" in odds_data:
            d1, d2 = odds_data["1x2"], odds_data["asian"]
            gap = self.detect_asian_european_gap(
                max(d1.get("home", [1.5])),
                max(d1.get("away", [1.5])),
                d2.get("handicap", 0),
                max(d2.get("home", [1.5])),
                max(d2.get("away", [1.5])),
            )
            if gap:
                results["opportunities"].append({"type": "asian_euro_gap", **gap})

        # 3. 大小球关联
        if "over_under" in odds_data and "1x2" in odds_data:
            d1, d3 = odds_data["1x2"], odds_data["over_under"]
            corr = self.over_under_correlation(
                1 / max(d1.get("home", [2.0])),
                1 / max(d1.get("away", [2.0])),
                d3.get("line", 2.5),
                max(d3.get("over", [1.8])),
                max(d3.get("under", [2.0])),
            )
            if corr:
                results["opportunities"].append({"type": "over_under_corr", **corr})

        results["total_found"] = len(results["opportunities"])
        return results

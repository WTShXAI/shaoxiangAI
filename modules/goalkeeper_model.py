#!/usr/bin/env python3
"""
哨响AI - 门将状态动态评估模型
==============================
基于 FORMULAS.md 0.11.1 节实现。

核心公式:
    keeper_risk = training_load * 0.4 + injury_recovery * 0.4 + pressure_factor * 0.2

影响:
    - 高风险门将 (risk < 0.7): 放大动态修正 +25%
    - 低风险门将 (risk > 0.85): 增强定位球防守权重
    - 替补门将压力系数 = 1.5 (默认)

数据来源:
    - 训练负荷: 近7天出场次数 × 平均出场时间
    - 伤愈指数: 伤后恢复场次 / 5场窗口
    - 压力因子: 是否为替补/杯赛关键战/德比

用法:
    from modules.goalkeeper_model import KeeperRiskModel
    model = KeeperRiskModel()
    risk = model.evaluate("Alisson", match_context={'is_derby': False, 'importance': 0.7})
    # risk['keeper_risk'] → 0~1, <0.7 表示高风险
    # risk['adjustment_factor'] → 预测调整系数
"""
import numpy as np
import logging
from typing import Dict, Optional, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class KeeperProfile:
    """门将画像"""
    name: str
    team: str
    is_first_choice: bool = True
    # 近期数据 (最近5-10场)
    recent_save_rate: float = 0.72      # 近期扑救率
    recent_clean_sheets: int = 2        # 近期零封
    recent_goals_conceded: float = 1.2  # 场均失球
    games_played_season: int = 25       # 赛季出场
    # 身体状态
    injury_status: str = 'fit'          # fit / returning / injured
    games_since_injury: int = 999       # 伤后场次
    training_load_7d: float = 5.0       # 近7天训练/比赛出场次数
    avg_minutes_per_game: float = 90.0  # 场均出场时间
    # 评估结果
    keeper_risk: float = 0.80          # 风险评分 (0-1, 越高越好)
    pressure_factor: float = 1.0       # 压力系数 (1.0=正常)


class KeeperRiskModel:
    """
    门将风险动态评估模型

    计算门将的即时风险值，并根据风险等级输出预测修正系数。

    公式 (来自 FORMULAS.md 0.11.1):
        keeper_risk = training_load_norm * 0.4
                    + injury_recovery * 0.4
                    + (1 / pressure_factor) * 0.2

    修正:
        - keeper_risk < 0.7 → 高风险，预测偏差放大 1.25x
        - keeper_risk > 0.85 → 健康，定位球防守增强
    """

    # === 门将数据库 (知名门将基准数据) ===
    KEEPER_DB = {
        # 英超
        "Alisson": {
            'team': 'Liverpool', 'recent_save_rate': 0.76, 'recent_clean_sheets': 6,
            'recent_goals_conceded': 0.9, 'games_played_season': 30, 'is_first_choice': True
        },
        "Ederson": {
            'team': 'Man City', 'recent_save_rate': 0.73, 'recent_clean_sheets': 8,
            'recent_goals_conceded': 0.8, 'games_played_season': 32, 'is_first_choice': True
        },
        "Raya": {
            'team': 'Arsenal', 'recent_save_rate': 0.78, 'recent_clean_sheets': 12,
            'recent_goals_conceded': 0.6, 'games_played_season': 34, 'is_first_choice': True
        },
        "Onana": {
            'team': 'Man United', 'recent_save_rate': 0.70, 'recent_clean_sheets': 5,
            'recent_goals_conceded': 1.4, 'games_played_season': 32, 'is_first_choice': True
        },
        "Vicario": {
            'team': 'Tottenham', 'recent_save_rate': 0.72, 'recent_clean_sheets': 4,
            'recent_goals_conceded': 1.3, 'games_played_season': 30, 'is_first_choice': True
        },
        "Sanchez": {
            'team': 'Chelsea', 'recent_save_rate': 0.68, 'recent_clean_sheets': 3,
            'recent_goals_conceded': 1.5, 'games_played_season': 28, 'is_first_choice': True
        },
        # 西甲
        "Courtois": {
            'team': 'Real Madrid', 'recent_save_rate': 0.77, 'recent_clean_sheets': 10,
            'recent_goals_conceded': 0.7, 'games_played_season': 32, 'is_first_choice': True
        },
        "Ter Stegen": {
            'team': 'Barcelona', 'recent_save_rate': 0.74, 'recent_clean_sheets': 9,
            'recent_goals_conceded': 0.8, 'games_played_season': 30, 'is_first_choice': True
        },
        "Oblak": {
            'team': 'Atletico Madrid', 'recent_save_rate': 0.80, 'recent_clean_sheets': 13,
            'recent_goals_conceded': 0.5, 'games_played_season': 35, 'is_first_choice': True
        },
        # 意甲
        "Maignan": {
            'team': 'AC Milan', 'recent_save_rate': 0.75, 'recent_clean_sheets': 8,
            'recent_goals_conceded': 0.9, 'games_played_season': 28, 'is_first_choice': True
        },
        "Sommer": {
            'team': 'Inter Milan', 'recent_save_rate': 0.76, 'recent_clean_sheets': 11,
            'recent_goals_conceded': 0.7, 'games_played_season': 33, 'is_first_choice': True
        },
    }

    def __init__(self):
        self.keeper_db = dict(self.KEEPER_DB)
        # 风险阈值
        self.HIGH_RISK_THRESHOLD = 0.70    # 低于此值=高风险
        self.LOW_RISK_THRESHOLD = 0.85     # 高于此值=健康
        # 修正系数
        self.HIGH_RISK_BOOST = 1.25         # 高风险时放大修正
        self.LOW_RISK_BOOST = 0.90          # 健康时缩小修正 (更精确)

    def get_keeper_profile(self, keeper_name: str) -> Optional[Dict]:
        """获取门将画像"""
        return self.keeper_db.get(keeper_name)

    def evaluate(self, keeper_name: str,
                 match_context: Dict = None) -> Dict:
        """
        评估门将即时风险

        Args:
            keeper_name: 门将姓名
            match_context: 比赛上下文 {'is_derby': bool, 'importance': 0~1,
                                      'is_cup': bool, 'opponent_strength': float}

        Returns:
            {
                'keeper_name': str,
                'keeper_risk': float (0-1, 越低越危险),
                'risk_level': 'high'/'medium'/'low',
                'adjustment_factor': float (将应用于预测修正),
                'components': {'training_load': ..., 'injury_recovery': ..., 'pressure': ...}
            }
        """
        match_context = match_context or {}
        profile = self.get_keeper_profile(keeper_name)

        if profile is None:
            # 未知门将使用默认评估
            return self._default_evaluation(keeper_name, match_context)

        # 1. 训练/比赛负荷因子 (0-1, 1=满负荷健康)
        # 近7天出场 ≤ 4 场: 负荷正常
        # > 4场: 过载累积
        games_7d = match_context.get('recent_games_7d', 3)
        max_load = 5
        training_load_norm = max(0.0, min(1.0, games_7d / max_load))
        training_load_score = 1.0 - abs(training_load_norm - 0.5) * 1.5  # 过高过低都降分

        # 2. 伤病恢复因子 (0-1, 1=完全恢复)
        games_since_injury = match_context.get('games_since_injury', 999)
        injury_window = 5  # 5场窗口
        injury_recovery = min(1.0, games_since_injury / injury_window)

        # 3. 压力因子
        pressure = self._calculate_pressure(keeper_name, profile, match_context)
        pressure_score = 1.0 / max(pressure, 0.5)

        # 核心公式 (FORMULAS.md 0.11.1)
        keeper_risk = (
            training_load_score * 0.4 +
            injury_recovery * 0.4 +
            pressure_score * 0.2
        )
        keeper_risk = max(0.1, min(1.0, keeper_risk))

        # 风险等级
        if keeper_risk < self.HIGH_RISK_THRESHOLD:
            risk_level = 'high'
            adjustment = self.HIGH_RISK_BOOST
        elif keeper_risk > self.LOW_RISK_THRESHOLD:
            risk_level = 'low'
            adjustment = self.LOW_RISK_BOOST
        else:
            risk_level = 'medium'
            adjustment = 1.0

        # 额外: 扑救率修正
        save_rate = profile.get('recent_save_rate', 0.72)
        if save_rate < 0.65:
            adjustment *= 1.10  # 扑救差 → 再放大修正

        return {
            'keeper_name': keeper_name,
            'team': profile.get('team', 'Unknown'),
            'is_first_choice': profile.get('is_first_choice', True),
            'keeper_risk': round(keeper_risk, 4),
            'risk_level': risk_level,
            'adjustment_factor': round(adjustment, 4),
            'recent_save_rate': save_rate,
            'recent_clean_sheets': profile.get('recent_clean_sheets', 0),
            'components': {
                'training_load': round(training_load_score, 4),
                'injury_recovery': round(injury_recovery, 4),
                'pressure': round(pressure, 2),
                'pressure_score': round(pressure_score, 4),
            }
        }

    def _calculate_pressure(self, keeper_name: str,
                            profile: Dict, context: Dict) -> float:
        """计算门将心理压力系数"""
        pressure = 1.0

        # 替补门将压力
        if not profile.get('is_first_choice', True):
            pressure *= 1.5

        # 德比战
        if context.get('is_derby', False):
            pressure *= 1.3

        # 杯赛淘汰赛
        if context.get('is_cup', False) and context.get('importance', 0) > 0.7:
            pressure *= 1.2

        # 对阵强敌 (对手评分 > 0.8)
        if context.get('opponent_strength', 0.5) > 0.8:
            pressure *= 1.15

        # 近期连续失球 > 1.5/场
        if profile.get('recent_goals_conceded', 0) > 1.5:
            pressure *= 1.10

        return pressure

    def _default_evaluation(self, keeper_name: str, context: Dict) -> Dict:
        """未知门将的默认评估"""
        return {
            'keeper_name': keeper_name,
            'team': 'Unknown',
            'is_first_choice': True,
            'keeper_risk': 0.75,
            'risk_level': 'medium',
            'adjustment_factor': 1.0,
            'recent_save_rate': 0.72,
            'recent_clean_sheets': 0,
            'components': {
                'training_load': 0.60,
                'injury_recovery': 1.0,
                'pressure': 1.0,
                'pressure_score': 1.0,
            }
        }

    def evaluate_both_keepers(self, home_keeper: str, away_keeper: str,
                              match_context: Dict = None) -> Dict:
        """同时评估双方门将"""
        match_context = match_context or {}

        home_ctx = dict(match_context)
        home_ctx['is_home'] = True

        away_ctx = dict(match_context)
        away_ctx['is_home'] = False
        away_ctx['opponent_strength'] = match_context.get('home_strength', 0.5)

        home_result = self.evaluate(home_keeper, home_ctx)
        away_result = self.evaluate(away_keeper, away_ctx)

        # 门将差距指标
        home_risk = home_result['keeper_risk']
        away_risk = away_result['keeper_risk']
        keeper_gap = home_risk - away_risk  # +值=主队门将更健康

        # 综合调整系数
        home_adj = home_result['adjustment_factor']
        away_adj = away_result['adjustment_factor']
        net_impact = home_adj - away_adj  # +值=对主队有利

        return {
            'home_keeper': home_result,
            'away_keeper': away_result,
            'keeper_gap': round(keeper_gap, 4),
            'net_impact': round(net_impact, 4),
            'recommendation': self._generate_recommendation(keeper_gap, net_impact),
        }

    def _generate_recommendation(self, keeper_gap: float, net_impact: float) -> str:
        """生成门将分析建议"""
        if abs(keeper_gap) < 0.05:
            return "双方门将状态接近，无显著偏差"
        if keeper_gap > 0.10:
            return "主队门将明显更健康，主队防守端预期强化"
        if keeper_gap < -0.10:
            return "客队门将状态更优，主场进攻方需警惕反击"
        if keeper_gap > 0:
            return "主队门将略优，关注零封概率"
        return "客队门将略优，关注客队防守稳固性"

    def apply_to_prediction(self, home_prob: float, draw_prob: float,
                            away_prob: float, keeper_eval: Dict) -> Dict:
        """
        将门将评估应用到预测概率

        逻辑: 门将风险修正主场防守概率
        - 主队门将高风险 → 主胜概率下调, 客胜概率上调
        - 客队门将高风险 → 反之
        """
        # ★ C4 加固：输入概率负值截断
        home_prob = max(0.0, home_prob)
        draw_prob = max(0.0, draw_prob)
        away_prob = max(0.0, away_prob)

        home_risk = keeper_eval.get('home_keeper', {}).get('keeper_risk', 0.75)
        away_risk = keeper_eval.get('away_keeper', {}).get('keeper_risk', 0.75)

        # 门将风险 → 修正因子
        # risk<0.7: +25%对手胜率
        # risk>0.85: -10%对手胜率
        home_impact = 1.0
        away_impact = 1.0
        if home_risk < 0.70:
            away_impact *= 1.25
        elif home_risk > 0.85:
            away_impact *= 0.90

        if away_risk < 0.70:
            home_impact *= 1.25
        elif away_risk > 0.85:
            home_impact *= 0.90

        new_home = home_prob * home_impact
        new_away = away_prob * away_impact
        new_draw = draw_prob  # 平局暂时不变

        total = new_home + new_draw + new_away
        return {
            'home': round(new_home / total, 4),
            'draw': round(new_draw / total, 4),
            'away': round(new_away / total, 4),
            'keeper_impact': {
                'home_risk': round(home_risk, 4),
                'away_risk': round(away_risk, 4),
                'home_factor': round(home_impact, 4),
                'away_factor': round(away_impact, 4),
            }
        }


# ============================================================
# CLI
# ============================================================
if __name__ == '__main__':
    model = KeeperRiskModel()

    print("=" * 60)
    print("  门将状态评估示例")
    print("=" * 60)

    # 示例 1: 双方顶级门将
    result = model.evaluate_both_keepers(
        "Alisson", "Ederson",
        {'importance': 0.85, 'is_derby': False}
    )
    print(f"\n🔵 Alisson vs Ederson:")
    print(f"  主队门将: risk={result['home_keeper']['keeper_risk']}, "
          f"level={result['home_keeper']['risk_level']}")
    print(f"  客队门将: risk={result['away_keeper']['keeper_risk']}, "
          f"level={result['away_keeper']['risk_level']}")
    print(f"  差距: {result['keeper_gap']}, 净影响: {result['net_impact']}")
    print(f"  建议: {result['recommendation']}")

    # 示例 2: 替补门将场景
    result2 = model.evaluate_both_keepers(
        "Raya", "Onana",
        {'importance': 0.90, 'is_derby': True, 'opponent_strength': 0.85}
    )
    print(f"\n🔴 Raya vs Onana (德比战):")
    print(f"  主队门将: risk={result2['home_keeper']['keeper_risk']}, "
          f"level={result2['home_keeper']['risk_level']}")
    print(f"  客队门将: risk={result2['away_keeper']['keeper_risk']}, "
          f"level={result2['away_keeper']['risk_level']}")
    print(f"  差距: {result2['keeper_gap']}, 净影响: {result2['net_impact']}")
    print(f"  建议: {result2['recommendation']}")

    # 示例 3: 应用于预测
    adjusted = model.apply_to_prediction(0.45, 0.25, 0.30, result)
    print(f"\n📊 预测修正 (Alisson vs Ederson):")
    print(f"  原始: home=0.45, draw=0.25, away=0.30")
    print(f"  修正: home={adjusted['home']}, draw={adjusted['draw']}, away={adjusted['away']}")
    print(f"  影响: {adjusted['keeper_impact']}")

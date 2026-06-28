#!/usr/bin/env python3
"""
哨响AI - 进攻效率衰减模型
==========================
基于 FORMULAS.md 0.11.2 节实现。

核心公式:
    α_finish = (8.5 / opponent_defense_rating) * (75% / keeper_recent_save_rate)

示例:
    对手防守评分 8.2, 门将扑救率 78%
    α = (8.2/8.5) * (78%/75%) = 1.02 → 预期进球(xG)下调12%

铁桶阵模式:
    对手禁区触球 < 100 → 自动触发，限制己方射正权重。

包含三个子模块:
1. FinishDecayCalculator — 终结能力衰减计算
2. DefensePressureAnalyzer — 防守压力分析
3. AttackEfficiencyModel — 综合进攻效率模型

用法:
    from modules.attack_efficiency import AttackEfficiencyModel
    model = AttackEfficiencyModel()
    result = model.evaluate(
        home_attack_rating=7.8, away_defense_rating=8.2,
        away_keeper_save_rate=0.78,
        away_box_touches=85  # <100 触发铁桶阵
    )
    # result['home_attack_decay'] → 进攻效率衰减系数
"""
import numpy as np
import logging
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class TeamRatings:
    """球队评分"""
    attack_rating: float = 7.0      # 进攻评分 (1-10)
    defense_rating: float = 7.0     # 防守评分 (1-10)
    midfield_rating: float = 7.0    # 中场评分 (1-10)
    box_touches: float = 150.0      # 场均禁区触球
    shots_on_target: float = 5.0    # 场均射正
    xg_per_match: float = 1.5       # 场均预期进球

# ============================================================
# 子模块 1: 终结能力衰减计算
# ============================================================
class FinishDecayCalculator:
    """
    终结能力衰减计算器

    核心公式 (FORMULAS.md 0.11.2):
        α_finish = (8.5 / opponent_defense_rating) * (75% / keeper_recent_save_rate)

    解读:
        - α > 1.0: 进攻方终结能力被压制 (防守方很强)
        - α = 1.0: 正常水平
        - α < 1.0: 进攻方终结能力增强 (防守方较弱)

    应用:
        xG_effective = xG_raw / α_finish
    """

    BASELINE_DEFENSE = 8.5       # 基准防守评分 (豪门级)
    BASELINE_SAVE_RATE = 0.75    # 基准扑救率

    def calculate(self, opponent_defense_rating: float,
                  keeper_save_rate: float) -> float:
        """
        计算终结能力衰减系数

        Args:
            opponent_defense_rating: 对手防守评分 (1-10)
            keeper_save_rate: 对手门将近期扑救率 (0-1)

        Returns:
            α_finish: 衰减系数 (>1 = 进攻被压制, <1 = 防守较弱)
        """
        defense_factor = opponent_defense_rating / self.BASELINE_DEFENSE
        keeper_factor = keeper_save_rate / self.BASELINE_SAVE_RATE

        alpha = defense_factor * keeper_factor
        return round(max(0.5, min(2.0, alpha)), 4)

    def apply_to_xg(self, raw_xg: float, alpha_finish: float) -> float:
        """对预期进球应用衰减"""
        return round(raw_xg / alpha_finish, 4)

    def apply_to_goal_expectation(self, expected_goals: float,
                                  alpha_finish: float) -> float:
        """对预期净胜球应用衰减"""
        return round(expected_goals / max(0.5, alpha_finish), 4)

# ============================================================
# 子模块 2: 防守压力分析
# ============================================================
class DefensePressureAnalyzer:
    """
    防守压力分析器

    检测铁桶阵模式:
        - 对手禁区触球 < 100 次: 触发铁桶阵
        - 铁桶阵效果: 己方射正权重 × 0.75
        - 高强度压迫: 对手场均射正 < 3.0 → 加强效果
    """

    BOX_TOUCH_THRESHOLD = 100     # 铁桶阵阈值
    SHOTS_THRESHOLD = 3.0         # 高强度压迫阈值

    def analyze(self, opponent_box_touches: float,
                opponent_shots_on_target: float) -> Dict:
        """
        分析对手防守压力/阵型

        Returns:
            {
                'is_bus_parking': bool,     # 是否为铁桶阵
                'pressure_level': str,       # 'normal' / 'bus_parking' / 'intense_bus'
                'shot_weight_modifier': float,  # 射正权重修正
                'space_compression': float,     # 空间压缩指数 (0-1)
            }
        """
        result = {
            'is_bus_parking': False,
            'pressure_level': 'normal',
            'shot_weight_modifier': 1.0,
            'space_compression': 0.0,
        }

        # 铁桶阵检测
        if opponent_box_touches < self.BOX_TOUCH_THRESHOLD:
            result['is_bus_parking'] = True

            # 空间压缩度
            compression = 1.0 - (opponent_box_touches / self.BOX_TOUCH_THRESHOLD)
            result['space_compression'] = round(compression, 4)

            if opponent_shots_on_target < self.SHOTS_THRESHOLD:
                result['pressure_level'] = 'intense_bus'
                result['shot_weight_modifier'] = 0.60  # 高强度铁桶
            else:
                result['pressure_level'] = 'bus_parking'
                result['shot_weight_modifier'] = 0.75  # 普通铁桶

        return result

# ============================================================
# 子模块 3: 综合进攻效率模型
# ============================================================
class AttackEfficiencyModel:
    """
    综合进攻效率模型

    整合终结能力衰减 + 防守压力分析，输出双向进攻效率评估。

    双向逻辑:
        - home_attack_decay: 主队进攻受客队防守 + 门将压制
        - away_attack_decay: 客队进攻受主队防守 + 门将压制
        - net_efficiency_gap: 主队进攻效率 - 客队进攻效率
    """

    def __init__(self):
        self.finish_calc = FinishDecayCalculator()
        self.pressure_analyzer = DefensePressureAnalyzer()

        # 默认球队评分 (用于缺失数据)
        self.DEFAULT_RATINGS = TeamRatings()

    def evaluate(self,
                 # 主队信息
                 home_attack_rating: float = None,
                 home_defense_rating: float = None,
                 home_box_touches: float = None,
                 home_shots_on_target: float = None,
                 # 客队信息
                 away_attack_rating: float = None,
                 away_defense_rating: float = None,
                 away_box_touches: float = None,
                 away_shots_on_target: float = None,
                 # 门将信息
                 away_keeper_save_rate: float = None,
                 home_keeper_save_rate: float = None,
                 ) -> Dict:
        """
        双向评估进攻效率

        Returns 包含:
            - home_attack_decay: 主队进攻衰减系数 (>1=被压制)
            - away_attack_decay: 客队进攻衰减系数
            - net_efficiency_gap: 主-客效率差 (+=主优)
            - home_pressure: 主队面对的防守压力分析
            - away_pressure: 客队面对的防守压力分析
            - prediction_adjustment: 预测调整建议
        """
        # 默认值
        home_def = home_defense_rating or self.DEFAULT_RATINGS.defense_rating
        away_def = away_defense_rating or self.DEFAULT_RATINGS.defense_rating
        home_att = home_attack_rating or self.DEFAULT_RATINGS.attack_rating
        away_att = away_attack_rating or self.DEFAULT_RATINGS.attack_rating
        away_kpr = away_keeper_save_rate or FinishDecayCalculator.BASELINE_SAVE_RATE
        home_kpr = home_keeper_save_rate or FinishDecayCalculator.BASELINE_SAVE_RATE
        away_bt = away_box_touches or self.DEFAULT_RATINGS.box_touches
        home_bt = home_box_touches or self.DEFAULT_RATINGS.box_touches
        away_st = away_shots_on_target or self.DEFAULT_RATINGS.shots_on_target
        home_st = home_shots_on_target or self.DEFAULT_RATINGS.shots_on_target

        # 主队进攻效率衰减 (客队防守+门将如何限制主队)
        home_attack_decay = self.finish_calc.calculate(away_def, away_kpr)

        # 客队进攻效率衰减 (主队防守+门将如何限制客队)
        away_attack_decay = self.finish_calc.calculate(home_def, home_kpr)

        # 防守压力分析
        # 主队面对客队防守阵型
        home_pressure = self.pressure_analyzer.analyze(away_bt, away_st)
        # 客队面对主队防守阵型
        away_pressure = self.pressure_analyzer.analyze(home_bt, home_st)

        # 综合修正
        home_total_decay = home_attack_decay * home_pressure['shot_weight_modifier']
        away_total_decay = away_attack_decay * away_pressure['shot_weight_modifier']

        # 效率差
        net_efficiency_gap = (1.0 / home_total_decay) - (1.0 / away_total_decay)

        # 产出
        return {
            'home_attack_decay': round(home_attack_decay, 4),
            'away_attack_decay': round(away_attack_decay, 4),
            'home_total_decay': round(home_total_decay, 4),
            'away_total_decay': round(away_total_decay, 4),
            'net_efficiency_gap': round(net_efficiency_gap, 4),
            'home_facing_pressure': home_pressure,
            'away_facing_pressure': away_pressure,
            'interpretation': self._interpret(home_total_decay, away_total_decay,
                                              home_pressure, away_pressure),
            'prediction_adjustment': {
                'home_goal_adj': round(1.0 / home_total_decay, 4),
                'away_goal_adj': round(1.0 / away_total_decay, 4),
                'net_goal_adj': round((1.0 / home_total_decay) - (1.0 / away_total_decay), 4),
            }
        }

    def _interpret(self, home_decay: float, away_decay: float,
                   home_press: Dict, away_press: Dict) -> str:
        """生成人类可读解释"""
        parts = []

        if home_press['is_bus_parking']:
            parts.append(f"客队{home_press['pressure_level']}防守(压缩{home_press['space_compression']:.0%})")
        if away_press['is_bus_parking']:
            parts.append(f"主队{away_press['pressure_level']}防守(压缩{away_press['space_compression']:.0%})")

        if home_decay > 1.2:
            parts.append("主队进攻显著被压制")
        elif home_decay < 0.85:
            parts.append("主队进攻机会良好")

        if away_decay > 1.2:
            parts.append("客队进攻显著被压制")
        elif away_decay < 0.85:
            parts.append("客队进攻机会良好")

        if not parts:
            parts.append("双方攻防效率接近基准线")

        return " | ".join(parts)

    def apply_to_prediction(self, home_prob: float, draw_prob: float,
                            away_prob: float, efficiency: Dict) -> Dict:
        """
        将进攻效率评估应用到预测概率

        逻辑:
        - 主队进攻被压制(decay>1) → 主胜↓, 平局↑
        - 客队进攻被压制 → 客胜↓, 主胜↑
        - 铁桶阵触发 → 低比分概率增加，平局概率↑
        """
        # ★ C4 加固：输入概率负值截断
        home_prob = max(0.0, home_prob)
        draw_prob = max(0.0, draw_prob)
        away_prob = max(0.0, away_prob)

        home_total = efficiency.get('home_total_decay', 1.0)
        away_total = efficiency.get('away_total_decay', 1.0)

        home_factor = 1.0 / home_total
        away_factor = 1.0 / away_total

        new_home = home_prob * home_factor
        new_away = away_prob * away_factor
        new_draw = draw_prob

        # 铁桶阵增强平局概率
        home_press = efficiency.get('home_facing_pressure', {})
        away_press = efficiency.get('away_facing_pressure', {})
        if home_press.get('is_bus_parking') or away_press.get('is_bus_parking'):
            new_draw *= 1.15

        total = new_home + new_draw + new_away
        return {
            'home': round(new_home / total, 4),
            'draw': round(new_draw / total, 4),
            'away': round(new_away / total, 4),
            'efficiency_impact': {
                'home_factor': round(home_factor, 4),
                'away_factor': round(away_factor, 4),
                'bus_parking_boost': bool(home_press.get('is_bus_parking') or
                                         away_press.get('is_bus_parking')),
            }
        }

    def evaluate_from_features(self, features: Dict) -> Dict:
        """从特征字典评估（适配集成模型管道）"""
        return self.evaluate(
            home_attack_rating=features.get('home_attack_rating'),
            home_defense_rating=features.get('home_defense_rating'),
            away_attack_rating=features.get('away_attack_rating'),
            away_defense_rating=features.get('away_defense_rating'),
            home_box_touches=features.get('home_box_touches'),
            away_box_touches=features.get('away_box_touches'),
            home_shots_on_target=features.get('home_shots_on_target'),
            away_shots_on_target=features.get('away_shots_on_target'),
            home_keeper_save_rate=features.get('home_keeper_save_rate'),
            away_keeper_save_rate=features.get('away_keeper_save_rate'),
        )

# ============================================================
# CLI
# ============================================================
if __name__ == '__main__':
    model = AttackEfficiencyModel()

    print("=" * 60)
    print("  进攻效率衰减模型示例")
    print("=" * 60)

    # 示例 1: 强防 vs 弱攻
    result = model.evaluate(
        home_attack_rating=7.2,
        away_defense_rating=8.5,
        away_keeper_save_rate=0.80,
        away_box_touches=120
    )
    print(f"\n📊 主队弱攻 vs 客队强防:")
    print(f"  主队进攻衰减: {result['home_attack_decay']}")
    print(f"  客队进攻衰减: {result['away_attack_decay']}")
    print(f"  净效率差: {result['net_efficiency_gap']}")
    print(f"  解读: {result['interpretation']}")
    print(f"  预测调整: {result['prediction_adjustment']}")

    # 示例 2: 铁桶阵检测
    result2 = model.evaluate(
        home_attack_rating=8.5,
        away_defense_rating=8.0,
        away_keeper_save_rate=0.72,
        away_box_touches=75,  # <100 = 铁桶
        away_shots_on_target=2.5,  # <3 = intense
    )
    print(f"\n🚌 主队强攻 vs 铁桶阵:")
    print(f"  主队进攻衰减: {result2['home_attack_decay']}")
    print(f"  主队面对压力: {result2['home_facing_pressure']}")
    print(f"  解读: {result2['interpretation']}")

    # 示例 3: 应用到预测
    adjusted = model.apply_to_prediction(0.55, 0.25, 0.20, result2)
    print(f"\n📊 预测修正:")
    print(f"  原始: home=0.55, draw=0.25, away=0.20")
    print(f"  修正: home={adjusted['home']}, draw={adjusted['draw']}, away={adjusted['away']}")
    print(f"  影响: {adjusted['efficiency_impact']}")

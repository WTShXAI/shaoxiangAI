"""
哨响AI - 进球时段分析模块
从 football/ 项目移植 GoalTimingAnalyzer + CornerStats
"""
import numpy as np
from typing import Dict, List, Optional
from collections import defaultdict

class GoalTimingAnalyzer:
    """分析球队各时段进球概率"""

    TIME_SLOTS = ["0-15", "16-30", "31-45", "46-60", "61-75", "76-90+"]

    def __init__(self):
        self.timing_profiles: Dict[str, Dict] = {}

    def analyze(self, team_profiles: Dict[str, float]):
        """基于球队评分估算各时段进球概率"""
        for name, rating in team_profiles.items():
            strength = max(0.3, min(1.0, (rating or 75) / 100.0))

            first_half_weight = 0.35 + 0.1 * (1 - strength)
            second_half_weight = 0.65 + 0.1 * (strength - 0.5)
            total_weight = first_half_weight + second_half_weight
            first_half_weight /= total_weight
            second_half_weight /= total_weight

            raw_probs = [
                first_half_weight * 0.30,
                first_half_weight * 0.35,
                first_half_weight * 0.35,
                second_half_weight * 0.30,
                second_half_weight * 0.35,
                second_half_weight * 0.35,
            ]
            # ⛔ 死命令合规：不使用 np.random，用评分确定性微调
            strength_offset = (strength - 0.65) * 0.02
            raw_probs = [max(0.05, p + strength_offset) for p in raw_probs]
            total = sum(raw_probs)
            probs = [round(p / total, 4) for p in raw_probs]

            self.timing_profiles[name] = {
                "time_slots": self.TIME_SLOTS,
                "probabilities": dict(zip(self.TIME_SLOTS, probs)),
                "peak_period": self.TIME_SLOTS[int(np.argmax(probs))],
            }

    def predict_timing(self, team_name: str) -> Dict:
        if team_name in self.timing_profiles:
            return self.timing_profiles[team_name]
        default_prob = round(1.0 / len(self.TIME_SLOTS), 4)
        return {
            "time_slots": self.TIME_SLOTS,
            "probabilities": {s: default_prob for s in self.TIME_SLOTS},
            "peak_period": "46-60",
        }

    def predict_match_timing(self, home_name: str, away_name: str) -> Dict:
        """预测一场比赛各时段的进球概率分布"""
        home_timing = self.predict_timing(home_name)
        away_timing = self.predict_timing(away_name)

        slots = self.TIME_SLOTS
        data = []
        for s in slots:
            h = home_timing["probabilities"].get(s, 0.1)
            a = away_timing["probabilities"].get(s, 0.1)
            data.append({
                "slot": s,
                "home_prob": round(h, 4),
                "away_prob": round(a, 4),
                "total_prob": round(min(h + a, 1.0), 4),
            })

        return {
            "time_slots": slots,
            "data": data,
            "home_peak": home_timing["peak_period"],
            "away_peak": away_timing["peak_period"],
        }

class CornerStats:
    """基于球队攻击力估算角球分析"""

    def __init__(self):
        self.team_corners: Dict[str, Dict] = {}

    def update_from_ratings(self, team_ratings: Dict[str, float]):
        """基于球队评分估算角球"""
        for name, rating in team_ratings.items():
            attack_strength = max(0.3, min(1.5, (rating or 75) / 70.0))
            avg_corners = 5.0 * attack_strength
            # ⛔ 死命令合规：不使用 np.random，用评分确定性微调
            adjustment = (attack_strength - 1.0) * 0.8
            avg_corners = max(1.5, avg_corners + adjustment)
            self.team_corners[name] = {
                "avg_corners": round(avg_corners, 2),
                "attack_coeff": round(attack_strength, 3),
            }

    def get_corner_alert(self, team_name: str) -> Optional[Dict]:
        """场均角球 > 6.5 返回预警"""
        info = self.team_corners.get(team_name)
        if info is None:
            return None
        if info["avg_corners"] > 6.5:
            return {
                "team": team_name,
                "avg_corners": info["avg_corners"],
                "alert": f"⚠️ {team_name} 场均角球 {info['avg_corners']:.1f}，高于阈值 6.5",
                "level": "高角球",
            }
        return None

    def get_match_corner_prediction(self, home_name: str, away_name: str) -> Dict:
        """预测一场比赛的总角球数"""
        home = self.team_corners.get(home_name, {}).get("avg_corners", 5.0)
        away = self.team_corners.get(away_name, {}).get("avg_corners", 5.0)
        # ⛔ 死命令合规：不使用 np.random，用确定性计算
        balance_factor = (home - away) * 0.3
        total = round(home + away + balance_factor, 1)
        over_9_5 = "高" if total > 9.5 else "中" if total > 8 else "低"

        return {
            "home_avg_corners": home,
            "away_avg_corners": away,
            "predicted_total": round(total, 1),
            "over_9_5_confidence": over_9_5,
            "recommendation": "大角球" if total > 9.5 else "小角球" if total < 8 else "观察",
        }

    def get_ranking(self) -> List[Dict]:
        ranked = sorted(self.team_corners.items(), key=lambda x: -x[1]["avg_corners"])
        return [
            {"rank": i + 1, "team": name, "avg_corners": info["avg_corners"]}
            for i, (name, info) in enumerate(ranked)
        ]

class KellyCalculator:
    """凯利公式 - 独立模块"""

    @staticmethod
    def calculate(bankroll: float, model_prob: float, odd: float,
                  use_half_kelly: bool = True) -> Dict:
        """凯利公式: f* = (bp - q) / b"""
        b = odd - 1
        p = model_prob
        q = 1 - p

        if b <= 0 or p <= 0:
            return {
                "full_kelly_fraction": 0,
                "half_kelly_fraction": 0,
                "recommended_bet": 0,
                "bankroll": bankroll,
                "odd": odd,
                "model_prob": round(p * 100, 1),
                "expected_return": 0,
                "recommendation": "不建议投注（无正期望）",
            }

        f = (b * p - q) / b
        if f <= 0:
            return {
                "full_kelly_fraction": 0,
                "half_kelly_fraction": 0,
                "recommended_bet": 0,
                "bankroll": bankroll,
                "odd": odd,
                "model_prob": round(p * 100, 1),
                "expected_return": 0,
                "recommendation": "不建议投注（无正期望）",
            }

        half_kelly = f / 2
        effective_fraction = half_kelly if use_half_kelly else f
        bet_amount = round(bankroll * effective_fraction, 2)
        expected_return = round(bet_amount * (odd - 1) * p - bet_amount * q, 2)

        return {
            "full_kelly_fraction": round(f, 4),
            "half_kelly_fraction": round(half_kelly, 4),
            "recommended_bet": bet_amount,
            "bankroll": bankroll,
            "odd": odd,
            "model_prob": round(p * 100, 1),
            "expected_return": expected_return,
            "recommendation": (
                f"建议投注 ¥{bet_amount}（{'半' if use_half_kelly else '满'}凯利 "
                f"{round(effective_fraction * 100, 1)}% 本金）"
                if bet_amount > 0 else "不建议投注"
            ),
        }

    @staticmethod
    def calculate_for_all_outcomes(bankroll: float, probs: Dict[str, float],
                                   odds: Dict[str, float]) -> Dict:
        """计算所有结果（主胜/平局/客胜）的凯利建议"""
        results = {}
        for outcome in ["home", "draw", "away"]:
            p = probs.get(outcome, 0)
            odd = odds.get(outcome, 0)
            results[outcome] = KellyCalculator.calculate(bankroll, p, odd)
        return results

class HalfTimeAnalyzer:
    """半场/全场比分模式分析"""

    DEFAULT_PATTERNS = {
        "H-H": 0.25, "D-H": 0.08, "A-H": 0.03,
        "D-D": 0.15, "H-D": 0.04, "A-D": 0.04,
        "A-A": 0.18, "D-A": 0.06, "H-A": 0.04,
        "H-A": 0.04,  # 逆转
    }

    def __init__(self):
        self.ht_ft_patterns: Dict[str, float] = {}

    def predict_ht_ft(self, home: str, away: str,
                      result_probs: Dict[str, float]) -> Dict:
        """预测最可能的 HT-FT 模式"""
        if not result_probs:
            return {"patterns": [], "most_likely": "D-D"}

        home_win_p = result_probs.get("home", result_probs.get("home_win", 0.4))
        draw_p = result_probs.get("draw", 0.3)
        away_win_p = result_probs.get("away", result_probs.get("away_win", 0.3))

        patterns = {
            "H-H": home_win_p * 0.65,
            "D-H": home_win_p * 0.20,
            "A-H": home_win_p * 0.15,
            "D-D": draw_p * 0.60,
            "H-D": draw_p * 0.20,
            "A-D": draw_p * 0.20,
            "A-A": away_win_p * 0.60,
            "D-A": away_win_p * 0.25,
            "H-A": away_win_p * 0.15,
        }

        # 归一化
        total = sum(patterns.values())
        if total > 0:
            patterns = {k: v / total for k, v in patterns.items()}

        sorted_patterns = sorted(
            [{"pattern": k, "probability": round(v, 4)} for k, v in patterns.items()],
            key=lambda x: -x["probability"]
        )

        return {
            "patterns": sorted_patterns[:5],
            "most_likely": sorted_patterns[0]["pattern"] if sorted_patterns else "D-D",
            "ht_ft_confidence": "高" if sorted_patterns[0]["probability"] > 0.3 else "中",
        }

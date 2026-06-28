"""
哨响AI - 趋势与表单分析模块
================================
基于 API 积分榜数据 + 历史比赛数据，计算:
1. 球队近期表单动量 (form_momentum)
2. 交锋历史优势因子 (h2h_factor)
3. 积分榜排名差因子 (rank_diff_factor)
4. 进球/失球趋势分析
5. 主客场分拆分析
"""

import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

class TrendAnalyzer:
    """趋势与表单分析器"""

    # 结果权重：胜=3, 平=1, 负=0
    RESULT_WEIGHT = {"W": 3, "D": 1, "L": 0}

    def __init__(self):
        pass

    def compute_form_momentum(self, team_form: List[Dict], last_n: int = 6) -> float:
        """
        计算近期表单动量因子 [−1, +1]
        
        考虑: 近期战绩 + 时间衰减 + 对手强弱
        
        Args:
            team_form: 按日期降序排列的表单记录列表
            last_n: 取最近N场比赛
        
        Returns:
            form_momentum ∈ [-1.0, 1.0]
        """
        if not team_form:
            return 0.0

        recent = team_form[:min(last_n, len(team_form))]
        if not recent:
            return 0.0

        # 时间衰减权重：最近的比赛权重最高
        total_weight = 0.0
        weighted_score = 0.0

        for i, match in enumerate(recent):
            time_weight = np.exp(-0.3 * i)  # 指数衰减
            result = match.get("result", "L")
            result_score = self.RESULT_WEIGHT.get(result, 0) / 3.0  # 归一化到 [0, 1]

            # 进球/失球对比分差因子
            gf = match.get("goals_for", 0) or 0
            ga = match.get("goals_against", 0) or 0
            goal_factor = np.clip((gf - ga) / 3.0, -1.0, 1.0)  # 净胜球归一化

            match_score = result_score * 0.6 + goal_factor * 0.4
            weighted_score += match_score * time_weight
            total_weight += time_weight

        if total_weight == 0:
            return 0.0

        raw_momentum = weighted_score / total_weight
        # 映射 [0, 1] → [−1, 1]
        return float(np.clip(raw_momentum * 2 - 1, -1.0, 1.0))

    def compute_form_streak(self, team_form: List[Dict]) -> Tuple[int, str]:
        """
        计算连胜/连败趋势

        Returns:
            (连续场次, 趋势类型: 'W'/'D'/'L')
        """
        if not team_form:
            return 0, ""

        streak = 1
        trend = team_form[0].get("result", "")
        for m in team_form[1:]:
            if m.get("result", "") == trend:
                streak += 1
            else:
                break
        return streak, trend

    def compute_goals_trend(self, team_form: List[Dict], last_n: int = 5) -> Dict:
        """
        计算进球/失球趋势

        Returns:
            {
                'avg_gf': 场均进球, 'avg_ga': 场均失球,
                'over25_rate': 大球率, 'clean_sheet_rate': 零封率,
                'btts_rate': 双方进球率, 'gf_trend': 进球趋势 ('up'/'down'/'flat'),
            }
        """
        if not team_form:
            return {"avg_gf": 0, "avg_ga": 0, "over25_rate": 0,
                    "clean_sheet_rate": 0, "btts_rate": 0, "gf_trend": "flat"}

        recent = team_form[:min(last_n, len(team_form))]
        if not recent:
            return {"avg_gf": 0, "avg_ga": 0, "over25_rate": 0,
                    "clean_sheet_rate": 0, "btts_rate": 0, "gf_trend": "flat"}

        goals_for = [m.get("goals_for", 0) or 0 for m in recent]
        goals_against = [m.get("goals_against", 0) or 0 for m in recent]
        over25 = sum(1 for i in range(len(recent))
                    if (goals_for[i] + goals_against[i]) > 2.5)
        clean_sheet = sum(1 for ga in goals_against if ga == 0)
        btts = sum(1 for i in range(len(recent))
                   if goals_for[i] > 0 and goals_against[i] > 0)

        # 进球趋势：比较前一半和后一半
        half = max(2, len(recent) // 2)
        first_half_gf = sum(goals_for[:half]) / half if half > 0 else 0
        second_half_gf = sum(goals_for[half:2*half]) / half if half > 0 else 0
        if second_half_gf - first_half_gf > 0.3:
            gf_trend = "up"
        elif first_half_gf - second_half_gf > 0.3:
            gf_trend = "down"
        else:
            gf_trend = "flat"

        return {
            "avg_gf": round(sum(goals_for) / len(recent), 2),
            "avg_ga": round(sum(goals_against) / len(recent), 2),
            "over25_rate": round(over25 / len(recent), 2),
            "clean_sheet_rate": round(clean_sheet / len(recent), 2),
            "btts_rate": round(btts / len(recent), 2),
            "gf_trend": gf_trend,
        }

    def compute_h2h_factor(self, h2h_matches: List[Dict],
                            team_a: str, team_b: str) -> float:
        """
        计算交锋历史优势因子 [−1, +1]

        Args:
            h2h_matches: 两队历史交锋记录
            team_a: 主队名称
            team_b: 客队名称

        Returns:
            h2h_factor: 正值=主队历史优势，负值=客队历史优势
        """
        if not h2h_matches:
            return 0.0

        # 时间衰减权重
        today = datetime.now(timezone.utc)
        total_weight = 0.0
        dominance = 0.0

        for i, m in enumerate(h2h_matches):
            # 时间衰减
            try:
                match_date = datetime.strptime(m.get("match_date", ""), "%Y-%m-%d")
                days_ago = max(1, (today - match_date).days)
                time_weight = np.exp(-days_ago / 730.0)  # 半衰期约2年
            except (ValueError, TypeError):
                time_weight = np.exp(-0.05 * i)

            home = m.get("home_team_name", "")
            hs = m.get("home_score") or 0
            aws = m.get("away_score") or 0

            if home == team_a:
                # 主队=team_a 坐镇主场
                if hs > aws:
                    result = 1.0
                elif hs < aws:
                    result = -1.0
                else:
                    result = 0.0
            elif home == team_b:
                # 主队=team_b 坐镇主场，反转
                if hs > aws:
                    result = -1.0
                elif hs < aws:
                    result = 1.0
                else:
                    result = 0.0
            else:
                result = 0.0

            # 比分差加权
            goal_diff_weight = min(abs(hs - aws), 3.0) / 3.0
            dominance += result * time_weight * (1.0 + goal_diff_weight * 0.5)
            total_weight += time_weight

        if total_weight == 0:
            return 0.0

        return float(np.clip(dominance / total_weight, -1.0, 1.0))

    def compute_rank_diff_factor(self, home_position: int, away_position: int,
                                  total_teams: int = 20) -> float:
        """
        计算排名差因子 [−1, +1]

        正值 = 客队排名更高（主队弱势）
        负值 = 主队排名更高（主队强势）

        Args:
            home_position: 主队排名 (1=榜首)
            away_position: 客队排名
            total_teams: 联赛总球队数
        """
        if not home_position or not away_position:
            return 0.0

        raw_diff = float(home_position - away_position)  # 正=客队更强
        normalized = raw_diff / (total_teams / 2.0)  # 归一化
        return float(np.clip(normalized, -1.0, 1.0))

    def compute_home_away_split(self, team_form: List[Dict]) -> Dict:
        """
        计算主客场分拆数据

        Returns:
            {'home_avg_gf': X, 'home_avg_ga': X, 'away_avg_gf': X, 'away_avg_ga': X,
             'home_win_rate': X, 'away_win_rate': X}
        """
        home_matches = [m for m in team_form if m.get("home_away") == "H"]
        away_matches = [m for m in team_form if m.get("home_away") == "A"]

        def calc_stats(matches):
            if not matches:
                return {"avg_gf": 0, "avg_ga": 0, "win_rate": 0}
            gf = [m.get("goals_for", 0) or 0 for m in matches]
            ga = [m.get("goals_against", 0) or 0 for m in matches]
            wins = sum(1 for m in matches if m.get("result") == "W")
            return {
                "avg_gf": round(sum(gf) / len(matches), 2),
                "avg_ga": round(sum(ga) / len(matches), 2),
                "win_rate": round(wins / len(matches), 2),
            }

        home_stats = calc_stats(home_matches)
        away_stats = calc_stats(away_matches)

        return {
            "home_avg_gf": home_stats["avg_gf"],
            "home_avg_ga": home_stats["avg_ga"],
            "home_win_rate": home_stats["win_rate"],
            "away_avg_gf": away_stats["avg_gf"],
            "away_avg_ga": away_stats["avg_ga"],
            "away_win_rate": away_stats["win_rate"],
        }

    def full_analysis(self, db, home_team: str, away_team: str,
                      league_id: int, season: int = None) -> Dict:
        """
        完整趋势分析（一站式计算）

        Returns:
            包含所有趋势因子的完整分析字典
        """
        result = {}

        # ── 1. 表单动量 ──
        home_form = db.get_team_form(home_team, limit=10)
        away_form = db.get_team_form(away_team, limit=10)
        result["home_form_momentum"] = self.compute_form_momentum(home_form)
        result["away_form_momentum"] = self.compute_form_momentum(away_form)
        result["form_momentum"] = round(
            (result["home_form_momentum"] - result["away_form_momentum"]) / 2.0, 4
        )

        # ── 2. 连胜/连败 ──
        h_streak, h_trend = self.compute_form_streak(home_form)
        a_streak, a_trend = self.compute_form_streak(away_form)
        result["home_streak"] = {"count": h_streak, "trend": h_trend}
        result["away_streak"] = {"count": a_streak, "trend": a_trend}

        # ── 3. 进球趋势 ──
        result["home_goals_trend"] = self.compute_goals_trend(home_form)
        result["away_goals_trend"] = self.compute_goals_trend(away_form)

        # ── 4. 交锋历史 ──
        h2h = db.get_h2h(home_team, away_team, limit=10)
        result["h2h_factor"] = round(self.compute_h2h_factor(h2h, home_team, away_team), 4)
        result["h2h_matches"] = h2h[:5]  # 返回最近5场

        # ── 5. 排名差 ──
        home_rank = db.get_team_rank(home_team, league_id, season)
        away_rank = db.get_team_rank(away_team, league_id, season)
        home_pos = home_rank.get("position") if home_rank else None
        away_pos = away_rank.get("position") if away_rank else None
        result["home_position"] = home_pos
        result["away_position"] = away_pos
        result["rank_diff_factor"] = round(
            self.compute_rank_diff_factor(
                home_pos or 10, away_pos or 10,
                total_teams=len(db.get_standings(league_id, season)) or 20
            ), 4
        )

        # ── 6. 主客场分拆 ──
        result["home_split"] = self.compute_home_away_split(home_form)
        result["away_split"] = self.compute_home_away_split(away_form)

        return result

"""
哨响AI v4.0 — 共同对手交叉对比引擎 (Common Opponent Cross-Comparator)
=====================================================================
从原 footballAI digital_human.py 移植的核心算法。

核心原理:
  如果A队和B队都跟同一对手C踢过，对比他们对C的表现可以发现赔率未反映的"隐藏实力差"。

经典案例:
  葡萄牙 vs 民主刚果:
    共同对手: 尼日利亚
    葡萄牙 2-1 尼日利亚 (主)
    民主刚果 4-5 尼日利亚 (客)  ← 刚果进了4球!
    → 结论: 刚果的进攻火力被赔率严重低估，穿盘/平局概率上升

算法:
  1. 找出共同对手
  2. 对比两队对共同对手的进球/失球
  3. 计算表现差分 (主场×0.9加权, 正式赛×1.2)
  4. 修正泊松 λ (预期进球)
  5. 生成修正后的比分推荐

用法:
    from modules.cross_opponent import CrossOpponentAnalyzer
    coa = CrossOpponentAnalyzer()
    result = coa.analyze(home_team, away_team, common_opponents_data)
    # result.alternative_scores — 含修正后的比分
    # result.upset_alert — 是否触发冷门预警

作者: Architecture v4.0 · 移植自 vip_final.py / digital_human.py
日期: 2026-06-19
"""
from __future__ import annotations
import math, logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 1. 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class MatchResult:
    """单场比赛结果"""
    opponent: str
    home_away: str           # 'home' or 'away'
    goals_for: int
    goals_against: int
    match_type: str = "official"  # official / friendly
    date: str = ""

    @property
    def result_str(self) -> str:
        """H/D/A"""
        if self.goals_for > self.goals_against:
            return "H"
        elif self.goals_for == self.goals_against:
            return "D"
        else:
            return "A"

    @property
    def points(self) -> int:
        if self.goals_for > self.goals_against:
            return 3
        elif self.goals_for == self.goals_against:
            return 1
        else:
            return 0


@dataclass
class CommonOpponentCompare:
    """共同对手对比"""
    opponent: str
    team_a: str
    team_b: str
    team_a_result: MatchResult
    team_b_result: MatchResult
    team_a_performance: float     # 综合表现分
    team_b_performance: float
    advantage: str                # "A"/"B"/"相当"
    key_diff: str                 # 关键差异描述
    significance: float = 1.0     # 显著性 [0,1]


@dataclass
class CrossCompareResult:
    """交叉对比完整结果"""
    home_team: str
    away_team: str
    common_opponents: List[CommonOpponentCompare] = field(default_factory=list)
    total_advantage: float = 0.0          # 综合优势 (正=客队优势, 负=主队优势)
    hidden_strength_team: str = ""        # 被低估的队名
    hidden_strength_desc: str = ""        # 隐藏实力描述
    lambda_adjust_h: float = 0.0          # λ_H 修正量
    lambda_adjust_a: float = 0.0
    alternative_scores: List[Dict] = field(default_factory=list)  # 修正比分推荐
    upset_alert: bool = False             # 冷门预警
    upset_reason: str = ""                # 预警原因
    p_draw_boost: float = 0.0             # P(D) 提升量


# ═══════════════════════════════════════════════════════════════
# 2. 核心引擎
# ═══════════════════════════════════════════════════════════════

class CrossOpponentAnalyzer:
    """
    共同对手交叉对比分析器

    通过比较两队对共同对手的表现，发现赔率未反映的隐藏实力差。
    """

    # 权重参数
    HOME_WEIGHT = 0.90      # 主场结果打折 (客场更难)
    FRIENDLY_WEIGHT = 0.70  # 友谊赛打折
    SIGNIFICANCE_THRESHOLD = 0.15  # 显著性阈值

    def analyze(self,
                home_team: str,
                away_team: str,
                home_results: List[MatchResult],
                away_results: List[MatchResult]) -> CrossCompareResult:
        """
        执行交叉对比分析

        Args:
            home_team: 主队名
            away_team: 客队名
            home_results: 主队近期赛果
            away_results: 客队近期赛果

        Returns:
            CrossCompareResult
        """
        result = CrossCompareResult(home_team=home_team, away_team=away_team)

        # ── 1. 找出共同对手 ──
        home_opponents = {r.opponent: r for r in home_results}
        away_opponents = {r.opponent: r for r in away_results}
        common = set(home_opponents.keys()) & set(away_opponents.keys())

        if not common:
            logger.info(f"[CrossOpponent] {home_team}vs{away_team}: 无共同对手")
            return result

        logger.info(f"[CrossOpponent] 共同对手: {common}")

        # ── 2. 逐一对比 ──
        for opp in sorted(common):
            ra = home_opponents[opp]
            rb = away_opponents[opp]

            # 计算表现分
            perf_a = self._compute_performance(ra)
            perf_b = self._compute_performance(rb)

            # 显著性: 正式赛权重, 对手越强越显著
            sig = 1.0
            if ra.match_type == "friendly" or rb.match_type == "friendly":
                sig *= self.FRIENDLY_WEIGHT

            # 关键差异描述
            key_diff = self._describe_difference(ra, rb, home_team, away_team)

            # 优势方向
            diff = perf_b - perf_a
            if abs(diff) < self.SIGNIFICANCE_THRESHOLD:
                advantage = "相当"
            elif diff > 0:
                advantage = away_team
            else:
                advantage = home_team

            comparison = CommonOpponentCompare(
                opponent=opp,
                team_a=home_team,
                team_b=away_team,
                team_a_result=ra,
                team_b_result=rb,
                team_a_performance=perf_a,
                team_b_performance=perf_b,
                advantage=advantage,
                key_diff=key_diff,
                significance=sig,
            )
            result.common_opponents.append(comparison)
            result.total_advantage += diff * sig

        # ── 3. 综合判断 ──
        if abs(result.total_advantage) > 0.5:
            if result.total_advantage > 0:
                result.hidden_strength_team = away_team
                result.hidden_strength_desc = (
                    f"{away_team}对共同对手的表现显著优于{home_team} "
                    f"(综合优势+{result.total_advantage:.2f})"
                )
                # 客队被低估 → λ_A 上调, P(D)可能上升
                result.lambda_adjust_a = min(0.5, result.total_advantage * 0.3)
                result.lambda_adjust_h = 0
                result.p_draw_boost = min(0.10, result.total_advantage * 0.05)
            else:
                result.hidden_strength_team = home_team
                result.hidden_strength_desc = (
                    f"{home_team}对共同对手的表现显著优于{away_team} "
                    f"(综合优势{result.total_advantage:.2f})"
                )
                result.lambda_adjust_h = min(0.5, -result.total_advantage * 0.3)
                result.lambda_adjust_a = 0
                result.p_draw_boost = 0

            # 冷门判断: 如果odds偏向的方向与交叉对比相反
            result.upset_alert = True
            stronger = home_team if result.total_advantage < 0 else away_team
            result.upset_reason = (
                f"交叉对比显示{stronger}被低估: "
                f"{'; '.join(c.key_diff[:60] for c in result.common_opponents)}"
            )

        # ── 4. 生成修正比分 (简化: 基于性能差调整泊松) ──
        if result.common_opponents:
            result.alternative_scores = self._generate_alternative_scores(result)

        return result

    def _compute_performance(self, mr: MatchResult) -> float:
        """计算单场表现分（交叉对比专用: 侧重攻击力而非比赛结果）"""
        # 进攻分 (核心): 对同一对手的进球能力是最直接的比较
        attack_score = min(1.0, mr.goals_for / 4.0)  # 进4球=满分
        attack_score *= 0.50  # 进攻占50%权重

        # 结果分: 胜=0.3, 平=0.2, 负=0
        result_score = {3: 0.30, 1: 0.20, 0: 0.0}[mr.points]
        
        # 净胜球: 对同一对手的净胜球差异
        goal_diff = mr.goals_for - mr.goals_against
        diff_score = max(-0.3, min(0.3, goal_diff * 0.08))  # 净胜球±3以内
        
        # 防守分 (次要)
        defense_score = max(0, 1.0 - mr.goals_against / 5.0) * 0.05  # 防守仅5%

        perf = attack_score + result_score + diff_score + defense_score

        # 位置修正
        if mr.home_away == "away":
            perf *= 1.15  # 客场进球更有说服力

        # 比赛类型修正
        if mr.match_type == "friendly":
            perf *= self.FRIENDLY_WEIGHT

        return perf

    def _describe_difference(self, ra: MatchResult, rb: MatchResult,
                            home_team: str, away_team: str) -> str:
        """生成关键差异描述"""
        parts = []

        # 进球对比
        goal_diff = rb.goals_for - ra.goals_for
        if abs(goal_diff) >= 2:
            who = away_team if goal_diff > 0 else home_team
            parts.append(f"{who}进球数+{abs(goal_diff)}(对同一对手)")

        # 比赛结果对比
        if ra.result_str != rb.result_str:
            a_label = {"H": "胜", "D": "平", "A": "负"}[ra.result_str]
            b_label = {"H": "胜", "D": "平", "A": "负"}[rb.result_str]
            parts.append(f"{home_team}{a_label} vs {away_team}{b_label}(对同一对手)")

        # 赛果对比
        ra_score = f"{ra.goals_for}-{ra.goals_against}"
        rb_score = f"{rb.goals_for}-{rb.goals_against}"
        parts.append(f"{ra_score} vs {rb_score} (vs {ra.opponent})")

        return "; ".join(parts)

    def _generate_alternative_scores(self, result: CrossCompareResult) -> List[Dict]:
        """
        生成考虑交叉对比的修正比分推荐

        基于隐藏实力调整泊松参数，重新计算最可能比分。
        """
        alt_scores = []

        if not result.hidden_strength_team:
            return alt_scores

        # 简单规则: 基于性能差异生成修正比分
        # 如果客队被低估 → 客队能进更多球 → 增加客胜/平局比分
        adv = result.total_advantage

        for comp in result.common_opponents:
            # 基于共同对手的比分反向推导可能比分
            ra = comp.team_a_result    # 主队对共同对手的表现
            rb = comp.team_b_result    # 客队对共同对手的表现

            # 客队对共同对手的进球数 — 主队对共同对手的进球数 = 进攻差
            attack_diff = rb.goals_for - ra.goals_for
            defense_diff = rb.goals_against - ra.goals_against

            if abs(attack_diff) >= 2:
                # 客队进攻显著更强 → 生成客队高进球比分
                if attack_diff > 0:
                    # 客队更能进球
                    for g in range(1, min(4, attack_diff + 2)):
                        if rb.goals_for >= 3:
                            alt_scores.append({
                                "score": f"1-{g+1}",
                                "probability": 0.08 * comp.significance,
                                "outcome": "away" if g > 0 else "draw",
                                "source": f"共同对手{comp.opponent}: {away_result_str(rb)}",
                                "confidence": "medium",
                            })
                        # 平局可能性
                        if abs(defense_diff) <= 1:
                            alt_scores.append({
                                "score": f"1-1",
                                "probability": 0.12 * comp.significance,
                                "outcome": "draw",
                                "source": f"共同对手{comp.opponent}: 双方进攻差距大但防守均不稳",
                                "confidence": "high",
                            })

        return alt_scores[:4]


def away_result_str(mr: MatchResult) -> str:
    """客队赛果描述"""
    return f"{mr.goals_for}-{mr.goals_against}({mr.home_away})"


# ═══════════════════════════════════════════════════════════════
# 3. 世界杯2026已知共同对手数据 (从原路线移植)
# ═══════════════════════════════════════════════════════════════

# 注: 这些数据来自 analysis_template_v2.md + digital_human.py 验证案例
KNOWN_COMMON_OPPONENTS: Dict[str, Tuple[List[MatchResult], List[MatchResult]]] = {
    # 葡萄牙 vs 民主刚果 → 共同对手: 尼日利亚
    ("葡萄牙", "民主刚果"): (
        [  # 葡萄牙近期
            MatchResult("尼日利亚", "home", 2, 1, "official"),
        ],
        [  # 民主刚果近期
            MatchResult("尼日利亚", "away", 4, 5, "official"),
        ],
    ),
    # 英格兰 vs 克罗地亚 → 共同对手: 巴西 (假设验证案例)
    ("英格兰", "克罗地亚"): (
        [MatchResult("巴西", "home", 1, 1, "official")],
        [MatchResult("巴西", "away", 0, 2, "official")],
    ),
}


def get_known_common_opponents(home: str, away: str) -> Tuple[List[MatchResult], List[MatchResult]]:
    """获取已知的共同对手数据 (精确匹配 + 模糊匹配)"""
    # 精确匹配
    key = (home, away)
    if key in KNOWN_COMMON_OPPONENTS:
        return KNOWN_COMMON_OPPONENTS[key]

    # 模糊匹配: 去掉可能的噪音后缀
    home_clean = home.strip()
    away_clean = away.strip()
    # 去掉常见后缀
    for suffix in ['谁赢','怎么看','预测','怎么样','会赢','会输','会平','分析','主场','客场']:
        away_clean = away_clean.replace(suffix, '').strip()
        home_clean = home_clean.replace(suffix, '').strip()

    fuzzy_key = (home_clean, away_clean)
    if fuzzy_key in KNOWN_COMMON_OPPONENTS:
        return KNOWN_COMMON_OPPONENTS[fuzzy_key]

    return [], []

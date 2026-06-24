"""
数字人预测引擎 (Digital Human) — 模拟人工预测的8步推理
=========================================================

核心理念:
  真实预测流程不是一步到位的数学计算，而是迭代式信息消化。
  每获取一条新信息，自动触发一轮修正，输出N版演进轨迹。
  
  葡萄牙 1-1 刚果 经典案例推演:
    v1: λ=2.40/0.70 → 2-0/3-0    (纯数学)
    v2: 近2场丢球 → 2-0/1-0/2-1   (防线趋势)
    v3: 马丁内斯离任 + C罗压力 → 2-1/1-0/1-1 (心理层)
    v4: 刚果对尼日利亚5球 → 2-2/3-2/2-1 (对手深挖)
    v5: 首发阵容确认 → 2-2/2-1/3-2       (最终)

能力清单:
  1. 迭代预测链: iterate_prediction()
  2. 共同对手对比: compare_via_common_opponent()
  3. 阵容解读: analyze_lineup_change()
  4. 心理信号: evaluate_psychological_factors()
  5. 防线趋势: detect_defensive_trend()
  6. 赔率矛盾: detect_odds_contradiction()
  7. 操盘手盈亏: simulate_bookmaker_pnl()
  8. 逆向假设: generate_counter_hypotheses()
  9. 信息质量权重: evaluate_info_quality()

作者: DigitalVIP (FootballAI 首席架构师)
版本: v1.0
"""

import math
import numpy as np
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict
import json


# ════════════════════════════════════════════════════════════════
# 数据类
# ════════════════════════════════════════════════════════════════

@dataclass
class PredictionIteration:
    """单次预测迭代记录"""
    version: int
    trigger: str          # 触发因素
    lam_h: float
    lam_a: float
    top_scores: List[str] # 比分预测 ["2-0", "3-0"]
    delta_h: float = 0.0  # λ_H 变化
    delta_a: float = 0.0  # λ_A 变化
    reasoning: str = ""   # 推理说明
    timestamp: str = ""


@dataclass
class MatchResult:
    """比赛结果"""
    team: str
    opponent: str
    score: str           # "2-1"
    home_away: str       # "home"/"away"
    match_type: str      # "official"/"friendly"/"cup"
    goals_scored: int
    goals_conceded: int
    date: str = ""
    result: str = ""     # "W"/"D"/"L"


@dataclass
class CommonOpponentCompare:
    """共同对手对比结果"""
    team_a: str
    team_b: str
    opponent: str
    team_a_result: MatchResult
    team_b_result: MatchResult
    team_a_performance: float  # 综合表现分
    team_b_performance: float
    advantage: str              # "A更强"/"B更强"/"相当"
    key_diff: str               # 关键差异描述


@dataclass
class LineupImpact:
    """阵容影响分析"""
    team: str
    formation: str              # "4-3-3"
    key_absences: List[str]     # 缺阵核心球员
    key_additions: List[str]    # 新增球员
    attack_impact: float        # 进攻影响 [-1, +1]
    defense_impact: float       # 防守影响 [-1, +1]
    creativity_change: float    # 创造力变化 [-1, +1]
    expected_goal_change: float # 预期进球变化


@dataclass
class PsychologicalFactors:
    """心理因素分析"""
    team: str
    coach_contract_pressure: float    # 教练压力 [-1, +1]
    star_player_pressure: float       # 球星压力 [-1, +1]
    team_morale: float                # 士气 [-1, +1]
    pragmatism_level: float           # 务实主义程度 [0, 1]
    lead_shrink_tendency: float       # 领先后收缩倾向 [0, 1]
    solo_tendency: float              # 独食倾向 [0, 1]
    composite_attack_modifier: float  # 综合进攻修正
    composite_defense_modifier: float # 综合防守修正


@dataclass
class DefensiveTrend:
    """防守趋势分析"""
    team: str
    recent_goals_conceded: List[float]  # 近N场丢球
    window_size: int
    trend_direction: str               # "improving"/"declining"/"stable"
    trend_strength: float              # 趋势强度 [-1, +1]
    clean_sheet_prob: float            # 零封概率
    weighted_avg_conceded: float       # 加权平均丢球
    time_weighted_decay: float         # 时间衰减因子


@dataclass
class OddsContradiction:
    """赔率矛盾检测"""
    detected: bool
    eu_odds_signal: str      # 欧赔信号("H稳赢"/"平局"/"A稳赢")
    score_odds_signal: str   # 波胆信号
    contradiction_type: str  # "eu_vs_score"/"ou_vs_score"/"handicap_vs_score"
    confidence: float
    interpretation: str      # 解读
    trap_indication: str     # 诱盘指示


@dataclass
class BookmakerPnlMatrix:
    """操盘手盈亏矩阵"""
    scores: List[Dict]       # [{score, pnl, volume_share, risk_level}]
    max_loss_score: str      # 庄家最大亏损比分
    max_loss_amount: float   # 最大亏损额(相对)
    max_profit_score: str    # 庄家最大盈利比分
    risk_distribution: str   # 风险分布描述


@dataclass
class CounterHypothesis:
    """逆向假设"""
    premise: str
    lam_h_mod: float
    lam_a_mod: float
    predicted_scores: List[str]
    probability: float       # 假设成立概率
    evidence_for: List[str]  # 支持证据
    evidence_against: List[str]  # 反对证据


# ════════════════════════════════════════════════════════════════
# 主类: 数字人引擎
# ════════════════════════════════════════════════════════════════

class DigitalHuman:
    """
    数字人: 模拟人工预测的8步推理

    设计理念:
    - 每获取一条新信息，触发一轮修正
    - 保留完整的迭代演进制
    - 可解释性强，每步输出推理文字
    """

    def __init__(self, name: str = "DigitalVIP-v1"):
        self.name = name
        self.prediction_chain: List[PredictionIteration] = []
        self.think_tank: Dict[str, Any] = {}
        self.iteration_count = 0

    # ════════════════════════════════════════════════════════════
    # 能力1: 迭代预测链
    # ════════════════════════════════════════════════════════════

    def iterate_prediction(self,
                           base_lam_h: float,
                           base_lam_a: float,
                           new_info: Dict[str, Any],
                           prev_scores: List[str] = None) -> PredictionIteration:
        """
        迭代预测: 每有新信息，更新λ和比分预测

        Args:
            base_lam_h: 当前λ_H
            base_lam_a: 当前λ_A
            new_info: 新信息 {"type": "defense_trend"|"lineup"|"psychology"|"opponent_deep",
                            "delta_h": float, "delta_a": float,
                            "reason": str}
            prev_scores: 上一版比分预测

        Returns:
            PredictionIteration: 迭代记录
        """
        self.iteration_count += 1

        delta_h = new_info.get('delta_h', 0.0)
        delta_a = new_info.get('delta_a', 0.0)

        new_lam_h = max(0.15, base_lam_h + delta_h)
        new_lam_a = max(0.05, base_lam_a + delta_a)

        # 从λ推导比分预测
        new_scores = self._lambda_to_scores(new_lam_h, new_lam_a, top_n=3)

        iteration = PredictionIteration(
            version=self.iteration_count,
            trigger=new_info.get('type', 'unknown'),
            lam_h=round(new_lam_h, 3),
            lam_a=round(new_lam_a, 3),
            top_scores=new_scores,
            delta_h=round(delta_h, 3),
            delta_a=round(delta_a, 3),
            reasoning=new_info.get('reason', ''),
            timestamp=datetime.now().isoformat()[:19],
        )

        if prev_scores:
            prev_set = set(prev_scores)
            new_set = set(new_scores)
            if prev_set != new_set:
                added = new_set - prev_set
                removed = prev_set - new_set
                if added:
                    iteration.reasoning += f" [新增比分: {', '.join(added)}]"
                if removed:
                    iteration.reasoning += f" [移除比分: {', '.join(removed)}]"

        self.prediction_chain.append(iteration)
        return iteration

    def get_evolution_trajectory(self) -> List[Dict]:
        """获取迭代演进轨迹"""
        trajectory = []
        for it in self.prediction_chain:
            trajectory.append({
                'version': it.version,
                'trigger': it.trigger,
                'lam_h': it.lam_h,
                'lam_a': it.lam_a,
                'top_scores': it.top_scores,
                'lambda_diff': round(it.lam_h - it.lam_a, 3),
                'delta_h': it.delta_h,
                'delta_a': it.delta_a,
                'reasoning': it.reasoning,
            })
        return trajectory

    # ════════════════════════════════════════════════════════════
    # 能力2: 共同对手交叉对比
    # ════════════════════════════════════════════════════════════

    def compare_via_common_opponent(self,
                                    team_a_results: List[MatchResult],
                                    team_b_results: List[MatchResult]) -> List[CommonOpponentCompare]:
        """
        共同对手对比: 找共同对手，计算对比指标

        葡萄牙2-1尼日利亚(主) vs 刚果4-5尼日利亚(客)
        → 刚果进攻力 >> 葡萄牙（对同一对手！）

        Args:
            team_a_results: A队近期赛果
            team_b_results: B队近期赛果

        Returns:
            List[CommonOpponentCompare]: 共同对手对比列表
        """
        # 提取共同对手
        a_opponents = {r.opponent: r for r in team_a_results}
        b_opponents = {r.opponent: r for r in team_b_results}
        common = set(a_opponents.keys()) & set(b_opponents.keys())

        comparisons = []
        for opp in common:
            ra = a_opponents[opp]
            rb = b_opponents[opp]

            # 计算表现分 (加权: 正式赛>友谊赛)
            perf_a = self._calc_performance_score(ra)
            perf_b = self._calc_performance_score(rb)

            diff = perf_a - perf_b
            if abs(diff) < 0.1:
                advantage = "相当"
            elif diff > 0:
                advantage = f"{team_a_results[0].team if team_a_results else 'A'}更强"
            else:
                advantage = f"{team_b_results[0].team if team_b_results else 'B'}更强"

            # 关键差异
            key_diffs = []
            if ra.goals_scored > rb.goals_scored + 1:
                key_diffs.append(f"{ra.team}进攻端表现更强({ra.goals_scored}球 vs {rb.goals_scored}球)")
            if ra.goals_conceded < rb.goals_conceded - 1:
                key_diffs.append(f"{ra.team}防守更稳固(仅丢{ra.goals_conceded}球 vs {rb.goals_conceded}球)")
            if ra.match_type == 'official' and rb.match_type == 'friendly':
                key_diffs.append("注: A队在正式赛中表现，B队在友谊赛中表现")
            elif rb.match_type == 'official' and ra.match_type == 'friendly':
                key_diffs.append("注: B队在正式赛中表现，A队在友谊赛中表现")

            comparisons.append(CommonOpponentCompare(
                team_a=ra.team,
                team_b=rb.team,
                opponent=opp,
                team_a_result=ra,
                team_b_result=rb,
                team_a_performance=round(perf_a, 2),
                team_b_performance=round(perf_b, 2),
                advantage=advantage,
                key_diff="; ".join(key_diffs) if key_diffs else "无明显差异",
            ))

        return comparisons

    def _calc_performance_score(self, result: MatchResult) -> float:
        """计算单场表现分"""
        weight = 1.0 if result.match_type in ('official', 'cup') else 0.7
        goal_diff = result.goals_scored - result.goals_conceded
        score = goal_diff * 0.5 + 1.0  # 基础分
        if result.home_away == 'home':
            score *= 0.85  # 主场加成扣除（去偏）
        return score * weight

    # ════════════════════════════════════════════════════════════
    # 能力3: 阵容/阵型解读
    # ════════════════════════════════════════════════════════════

    def analyze_lineup_change(self,
                               expected_xi: Dict[str, Any],
                               actual_xi: Dict[str, Any]) -> LineupImpact:
        """
        阵容解读: 阵容变化的进攻/防守影响

        莱奥→J内维斯：创造力↓，预期进球↓
        刚果4-5-1→5-3-2双前锋：进攻↑

        Args:
            expected_xi: 预期首发 {"formation": "4-3-3", "players": [...]}
            actual_xi: 实际首发 {"formation": "4-5-1", "players": [...]}

        Returns:
            LineupImpact: 阵容影响分析
        """
        expected_formation = expected_xi.get('formation', '4-4-2')
        actual_formation = actual_xi.get('formation', '4-4-2')
        team = actual_xi.get('team', '')

        # 找出缺阵和新进球员
        expected_players = set(expected_xi.get('players', []))
        actual_players = set(actual_xi.get('players', []))
        absences = list(expected_players - actual_players)
        additions = list(actual_players - expected_players)

        # 阵型解读
        att_impact, def_impact, creativity, goal_change = self._interpret_formation_change(
            expected_formation, actual_formation
        )

        # 球员质量变化
        player_att, player_def, player_creative = self._interpret_player_quality_change(
            absences, additions, expected_xi.get('player_ratings', {}),
            actual_xi.get('player_ratings', {})
        )

        # 综合: 阵型变化(权重0.6) + 球员变化(权重0.4)
        attack_impact = round(
            np.clip(att_impact * 0.6 + player_att * 0.4, -1.0, 1.0), 3
        )
        defense_impact = round(
            np.clip(def_impact * 0.6 + player_def * 0.4, -1.0, 1.0), 3
        )
        creativity_change = round(
            np.clip(creativity * 0.6 + player_creative * 0.4, -1.0, 1.0), 3
        )
        expected_goal_change = round(
            attack_impact * 0.7 + creativity_change * 0.3, 3
        )

        return LineupImpact(
            team=team,
            formation=actual_formation,
            key_absences=absences,
            key_additions=additions,
            attack_impact=attack_impact,
            defense_impact=defense_impact,
            creativity_change=creativity_change,
            expected_goal_change=expected_goal_change,
        )

    def _interpret_formation_change(self, old_fm: str, new_fm: str) -> Tuple[float, float, float, float]:
        """解读阵型变化"""
        if old_fm == new_fm:
            return 0.0, 0.0, 0.0, 0.0

        # 简化的阵型解读
        # 提取前锋数
        def count_forwards(fm):
            try:
                parts = fm.split('-')
                return int(parts[0])
            except:
                return 2

        old_fwd = count_forwards(old_fm)
        new_fwd = count_forwards(new_fm)

        # 阵型解读矩阵
        fm_impacts = {
            # 转为双前锋 → 进攻增强
            (1, 2): (0.15, -0.05, 0.0, 0.15),   # 1锋→2锋
            (2, 1): (-0.15, 0.10, 0.0, -0.15),  # 2锋→1锋(保守)
            (3, 2): (-0.05, 0.08, 0.0, -0.05),  # 3锋→2锋
            (2, 3): (0.10, -0.10, 0.0, 0.10),   # 2锋→3锋(进攻)
            (3, 1): (-0.20, 0.15, -0.05, -0.20), # 3锋→1锋(极度保守)
            (1, 3): (0.20, -0.15, 0.05, 0.20),   # 1锋→3锋(极度进攻)
        }

        key = (old_fwd, new_fwd)
        return fm_impacts.get(key, (0.0, 0.0, 0.0, 0.0))

    def _interpret_player_quality_change(self, absences, additions,
                                          expected_ratings, actual_ratings) -> Tuple[float, float, float]:
        """解读球员质量变化"""
        att_change = 0.0
        def_change = 0.0
        creative_change = 0.0

        for player in absences:
            rating = expected_ratings.get(player, {})
            att_change -= rating.get('attack', 5.0) / 50.0
            def_change -= rating.get('defense', 5.0) / 50.0
            creative_change -= rating.get('creativity', 5.0) / 50.0

        for player in additions:
            rating = actual_ratings.get(player, {})
            att_change += rating.get('attack', 5.0) / 50.0
            def_change += rating.get('defense', 5.0) / 50.0
            creative_change += rating.get('creativity', 5.0) / 50.0

        return (
            np.clip(att_change, -0.3, 0.3),
            np.clip(def_change, -0.3, 0.3),
            np.clip(creative_change, -0.3, 0.3),
        )

    def lineup_to_lambda_adjustment(self, impact: LineupImpact) -> Tuple[float, float]:
        """阵容影响 → λ修正量"""
        delta_h = impact.attack_impact * 0.4
        delta_h += impact.defense_impact * 0.3  # 阵容防守影响对方进攻λ

        delta_a = -impact.defense_impact * 0.3  # 防守变化影响对方λ
        delta_a += impact.expected_goal_change * 0.2

        return round(delta_h, 3), round(delta_a, 3)

    # ════════════════════════════════════════════════════════════
    # 能力4: 心理信号
    # ════════════════════════════════════════════════════════════

    def evaluate_psychological_factors(self,
                                        team: str,
                                        context: Dict[str, Any]) -> PsychologicalFactors:
        """
        心理信号评估: 教练合同/球员压力 → 修正系数

        马丁内斯合同到期 → 务实主义 → 领先收缩
        C罗压力 → 独食 → 浪费机会↑

        Args:
            team: 球队名称
            context: {\"coach_contract_expiring\": bool, \"star_pressure\": float,
                      \"team_morale\": float, \"recent_form\": str}

        Returns:
            PsychologicalFactors: 心理因素分析
        """
        coach_expiring = context.get('coach_contract_expiring', False)
        coach_pressure = 0.5 if coach_expiring else min(
            context.get('coach_pressure', 0), 1.0
        )

        star_pressure = context.get('star_pressure', 0.0)  # 0~1
        team_morale = context.get('team_morale', 0.0)       # -1~+1
        recent_form = context.get('recent_form', 'stable')  # "good"/"poor"/"stable"

        # 务实主义程度
        pragmatism = 0.3  # baseline
        if coach_expiring:
            pragmatism = 0.70  # 合同到期 → 更务实
        if recent_form == 'poor':
            pragmatism = max(pragmatism, 0.60)  # 状态差 → 保守

        # 领先后收缩倾向
        lead_shrink = pragmatism * 0.8  # 务实主义 → 收缩

        # 独食倾向
        solo_tendency = star_pressure * 0.7  # 压力大 → 独食

        # 综合修正
        # 务实主义 → 进攻降低，防守更稳
        attack_mod = -pragmatism * 0.15
        attack_mod += solo_tendency * 0.05  # 独食偶尔进球
        attack_mod -= star_pressure * 0.08  # 压力降低效率

        # 防守: 务实主义 → 防守更强，收缩降低失球
        defense_mod = pragmatism * 0.12
        defense_mod += team_morale * 0.08   # 士气高防守好
        defense_mod -= star_pressure * 0.03 # 压力可能分散防守注意力

        return PsychologicalFactors(
            team=team,
            coach_contract_pressure=round(coach_pressure, 2),
            star_player_pressure=round(star_pressure, 2),
            team_morale=round(team_morale, 2),
            pragmatism_level=round(pragmatism, 2),
            lead_shrink_tendency=round(lead_shrink, 2),
            solo_tendency=round(solo_tendency, 2),
            composite_attack_modifier=round(np.clip(attack_mod, -0.3, 0.3), 3),
            composite_defense_modifier=round(np.clip(defense_mod, -0.3, 0.3), 3),
        )

    def psychology_to_lambda_adjustment(self, psych: PsychologicalFactors) -> Tuple[float, float]:
        """心理因素 → λ修正量"""
        delta_h = psych.composite_attack_modifier * 0.8
        delta_a = psych.composite_defense_modifier * 0.5
        return round(delta_h, 3), round(delta_a, 3)

    # ════════════════════════════════════════════════════════════
    # 能力5: 防守趋势检测
    # ════════════════════════════════════════════════════════════

    def detect_defensive_trend(self,
                                recent_results: List[MatchResult],
                                window_size: int = 5) -> DefensiveTrend:
        """
        防线趋势: 时间加权滑动窗口，检测丢球趋势

        葡萄牙近5场丢球：2+2+1+1+0 → 零封能力退化

        Args:
            recent_results: 近期赛果（按时间由旧到新排列）
            window_size: 窗口大小

        Returns:
            DefensiveTrend: 防守趋势
        """
        if not recent_results:
            return DefensiveTrend(
                team='', recent_goals_conceded=[], window_size=window_size,
                trend_direction='stable', trend_strength=0.0,
                clean_sheet_prob=0.3, weighted_avg_conceded=1.5,
                time_weighted_decay=1.0
            )

        team = recent_results[0].team
        goals_conceded = [r.goals_conceded for r in recent_results[-window_size:]]

        # 时间加权: 越近的比赛权重越大
        n = len(goals_conceded)
        weights = np.array([0.6 ** (n - 1 - i) for i in range(n)])
        weights = weights / weights.sum()

        # 加权平均丢球
        weighted_avg = float(np.average(goals_conceded, weights=weights))

        # 趋势检测: 比较前半段 vs 后半段
        mid = n // 2
        early_avg = float(np.mean(goals_conceded[:mid])) if mid > 0 else weighted_avg
        late_avg = float(np.mean(goals_conceded[mid:])) if mid > 0 else weighted_avg

        trend_diff = late_avg - early_avg
        if trend_diff > 0.3:
            direction = "declining"
            strength = min(1.0, abs(trend_diff))
        elif trend_diff < -0.3:
            direction = "improving"
            strength = min(1.0, abs(trend_diff))
        else:
            direction = "stable"
            strength = 0.0

        # 零封概率
        clean_sheets = sum(1 for g in goals_conceded if g == 0)
        clean_sheet_prob = clean_sheets / max(n, 1)

        # 时间衰减因子
        time_decay = 0.85 if direction == 'declining' else (
            1.15 if direction == 'improving' else 1.0
        )

        return DefensiveTrend(
            team=team,
            recent_goals_conceded=goals_conceded,
            window_size=window_size,
            trend_direction=direction,
            trend_strength=round(strength, 3),
            clean_sheet_prob=round(clean_sheet_prob, 3),
            weighted_avg_conceded=round(weighted_avg, 2),
            time_weighted_decay=round(time_decay, 3),
        )

    def defensive_trend_to_lambda_adjustment(self,
                                               home_trend: DefensiveTrend,
                                               away_trend: DefensiveTrend) -> Tuple[float, float]:
        """防守趋势 → λ修正量"""
        delta_h = 0.0
        delta_a = 0.0

        # 主队防线退化 → 客队λ↑
        if home_trend.trend_direction == 'declining':
            delta_a += home_trend.trend_strength * 0.25
        elif home_trend.trend_direction == 'improving':
            delta_a -= home_trend.trend_strength * 0.15

        # 客队防线退化 → 主队λ↑
        if away_trend.trend_direction == 'declining':
            delta_h += away_trend.trend_strength * 0.25
        elif away_trend.trend_direction == 'improving':
            delta_h -= away_trend.trend_strength * 0.15

        # 加权平均丢球修正
        if home_trend.weighted_avg_conceded > 2.0:
            delta_a += 0.08
        if away_trend.weighted_avg_conceded > 2.0:
            delta_h += 0.08

        return round(delta_h, 3), round(delta_a, 3)

    # ════════════════════════════════════════════════════════════
    # 能力6: 赔率矛盾解读
    # ════════════════════════════════════════════════════════════

    def detect_odds_contradiction(self,
                                   eu_odds: Tuple[float, float, float],
                                   score_odds: Dict[str, float],
                                   ou_line: float = None) -> OddsContradiction:
        """
        赔率矛盾: 欧赔vs波胆矛盾 → 诱盘信号

        欧赔1.26（稳赢）vs 波胆1-1@7.0（平局）
        → 矛盾 → 庄家在诱盘

        Args:
            eu_odds: (H, D, A) 欧赔
            score_odds: 波胆赔率 {"1-0": 7.5, "0-0": 9.0, ...}
            ou_line: 大小球盘口

        Returns:
            OddsContradiction: 矛盾分析
        """
        oh, od, oa = eu_odds

        # 欧赔信号
        if oh < 1.50:
            eu_signal = "H稳赢"
        elif oh < 2.00:
            eu_signal = "H优势"
        elif oa < 1.50:
            eu_signal = "A稳赢"
        elif oa < 2.00:
            eu_signal = "A优势"
        elif od < 3.50:
            eu_signal = "平局倾向"
        else:
            eu_signal = "均衡"

        # 波胆信号
        if not score_odds:
            return OddsContradiction(
                detected=False, eu_odds_signal=eu_signal,
                score_odds_signal="无波胆数据", contradiction_type="none",
                confidence=0.0, interpretation="", trap_indication=""
            )

        # 找最低赔率比分
        min_score = min(score_odds.items(), key=lambda x: x[1])
        score_key, min_odd = min_score

        # 解析比分
        parts = score_key.split('-')
        if len(parts) == 2:
            gh, ga = int(parts[0]), int(parts[1])
            if gh == ga:
                score_signal = f"平局({score_key}@{min_odd})"
            elif gh > ga:
                score_signal = f"主胜({score_key}@{min_odd})"
            else:
                score_signal = f"客胜({score_key}@{min_odd})"
        else:
            score_signal = f"其他({score_key}@{min_odd})"

        # 矛盾检测
        contradictions = []

        # 矛盾1: 欧赔主胜 vs 波胆平局
        if oh < 1.50 and min_odd < 9.0:
            # 找出平局比分的最低赔率
            draw_scores = {k: v for k, v in score_odds.items()
                          if len(k.split('-')) == 2 and k.split('-')[0] == k.split('-')[1]}
            if draw_scores:
                min_draw_odd = min(draw_scores.values())
                if min_draw_odd < 9.0:
                    min_draw_key = min(draw_scores, key=draw_scores.get)
                    contradictions.append(
                        f"欧赔{oh:.2f}主胜稳赢，但波胆{min_draw_key}@{min_draw_odd}有平局赔付压力"
                    )

        # 矛盾2: 欧赔平局 vs 波胆低分
        if od < 3.20:
            non_draw = {k: v for k, v in score_odds.items()
                       if len(k.split('-')) == 2 and k.split('-')[0] != k.split('-')[1]}
            if non_draw:
                min_non_draw = min(non_draw.values())
                if min_non_draw < 5.0:
                    min_nd_key = min(non_draw, key=non_draw.get)
                    contradictions.append(
                        f"欧赔平局{od:.2f}，但波胆{min_nd_key}@{min_non_draw}偏低 → 庄家担心分出胜负"
                    )

        # 矛盾3: 1-1赔率 vs 欧赔差距
        if score_odds.get('1-1') and oh < 1.50:
            odd_1_1 = score_odds['1-1']
            if odd_1_1 < 11.0:
                contradictions.append(
                    f"1-1赔率{odd_1_1}偏低，与{oh:.2f}主胜赔率矛盾 → 庄家设防平局"
                )

        detected = len(contradictions) > 0
        confidence = min(0.90, 0.50 + len(contradictions) * 0.22) if detected else 0.0

        interpretation = ""
        trap_indication = ""

        if detected:
            interpretation = "; ".join(contradictions)
            if len(contradictions) >= 2:
                trap_indication = "多重矛盾 → 高度疑似诱盘，建议反向操作"
            else:
                trap_indication = "单一矛盾 → 存疑，需结合其他信号"
        else:
            interpretation = "欧赔与波胆信号一致，无明显矛盾"
            trap_indication = "盘口信号一致，可顺势"

        return OddsContradiction(
            detected=detected,
            eu_odds_signal=eu_signal,
            score_odds_signal=score_signal,
            contradiction_type="eu_vs_score",
            confidence=round(confidence, 2),
            interpretation=interpretation,
            trap_indication=trap_indication,
        )

    # ════════════════════════════════════════════════════════════
    # 能力7: 操盘手盈亏矩阵
    # ════════════════════════════════════════════════════════════

    def simulate_bookmaker_pnl(self,
                                odds: Dict[str, float],
                                probs: Dict[str, float],
                                market_distribution: Dict[str, float] = None) -> BookmakerPnlMatrix:
        """
        操盘手盈亏: 计算每个比分的庄家盈亏，推演"庄家怕什么"

        Args:
            odds: 波胆赔率 {"1-0": 7.5, "0-0": 9.0, ...}
            probs: 各比分概率（来自模型） {"1-0": 0.12, "0-0": 0.08, ...}
            market_distribution: 市场份额分布（可选）

        Returns:
            BookmakerPnlMatrix: 盈亏矩阵
        """
        if market_distribution is None:
            # 默认均匀分布
            n = len(odds)
            market_distribution = {k: 1.0 / n for k in odds}

        pnl_scores = []
        total_bet = 1.0  # 归一化总投注

        for score, odd_val in odds.items():
            # 该比分投注量
            bet_amount = market_distribution.get(score, 1.0 / max(len(odds), 1)) * total_bet

            # 赔付: 只有打出该比分时才赔付
            payout = bet_amount * odd_val

            # 其他比分投注 = 庄家收入
            revenue = total_bet - bet_amount

            # 庄家盈亏: 收入 - 赔付
            pnl = revenue - payout

            # 风险等级
            risk_level = "safe" if pnl > 0 else (
                "danger" if pnl < -0.5 else "caution"
            )

            pnl_scores.append({
                'score': score,
                'pnl': round(pnl, 3),
                'bet_share': round(bet_amount, 3),
                'payout': round(payout, 3),
                'risk_level': risk_level,
                'odds': odd_val,
            })

        # 排序
        pnl_scores.sort(key=lambda x: x['pnl'])

        max_loss = pnl_scores[0]
        max_profit = pnl_scores[-1]

        # 风险分布
        danger_count = sum(1 for s in pnl_scores if s['risk_level'] == 'danger')
        caution_count = sum(1 for s in pnl_scores if s['risk_level'] == 'caution')
        safe_count = sum(1 for s in pnl_scores if s['risk_level'] == 'safe')

        if danger_count >= 3:
            risk_dist = f"多头暴露: {danger_count}个比分潜在亏损"
        elif danger_count >= 1:
            risk_dist = f"单一风险点: {max_loss['score']}" + (
                f" + {caution_count}个警戒点" if caution_count > 0 else ""
            )
        else:
            risk_dist = "风险受控，盈亏分散"

        return BookmakerPnlMatrix(
            scores=pnl_scores,
            max_loss_score=max_loss['score'],
            max_loss_amount=round(max_loss['pnl'], 3),
            max_profit_score=max_profit['score'],
            risk_distribution=risk_dist,
        )

    # ════════════════════════════════════════════════════════════
    # 能力8: 逆向假设生成
    # ════════════════════════════════════════════════════════════

    def generate_counter_hypotheses(self,
                                     market_consensus: Dict[str, Any],
                                     base_lam_h: float,
                                     base_lam_a: float) -> List[CounterHypothesis]:
        """
        逆向假设: 生成对立假设，逐一验证

        "如果庄家是对的..." → "如果庄家是错的..."
        生成2-3个假设

        Args:
            market_consensus: 市场共识 {"favorite": "H", "margin": "大胜"}
            base_lam_h: 基础λ_H
            base_lam_a: 基础λ_A

        Returns:
            List[CounterHypothesis]: 假设列表
        """
        hypotheses = []

        fav = market_consensus.get('favorite', 'H')
        margin = market_consensus.get('margin', 'normal')

        # 假设1: 庄家是对的（顺势假设）
        if fav == 'H':
            h1 = CounterHypothesis(
                premise=f"顺势: 庄家看好主队{margin}",
                lam_h_mod=round(base_lam_h * 1.10, 3),
                lam_a_mod=round(base_lam_a * 0.90, 3),
                predicted_scores=self._lambda_to_scores(base_lam_h * 1.10, base_lam_a * 0.90, 3),
                probability=0.60,
                evidence_for=[f"赔率结构支持主胜", f"实力差距明显"],
                evidence_against=[f"需防穿盘失败", f"近期冷门频发"],
            )
        else:
            h1 = CounterHypothesis(
                premise=f"顺势: 庄家看好客队{margin}",
                lam_h_mod=round(base_lam_h * 0.90, 3),
                lam_a_mod=round(base_lam_a * 1.10, 3),
                predicted_scores=self._lambda_to_scores(base_lam_h * 0.90, base_lam_a * 1.10, 3),
                probability=0.60,
                evidence_for=[f"赔率结构支持客胜", f"客场实力强劲"],
                evidence_against=[f"需防平局", f"客场不稳定"],
            )
        hypotheses.append(h1)

        # 假设2: 庄家是错的（平局假设）
        mid = base_lam_h - (base_lam_h - base_lam_a) * 0.5
        h2 = CounterHypothesis(
            premise="逆向: 庄家诱盘，实际平局",
            lam_h_mod=round(mid, 3),
            lam_a_mod=round(mid, 3),
            predicted_scores=self._lambda_to_scores(mid, mid, 3),
            probability=0.25,
            evidence_for=["平赔偏高可能是诱盘", "实力接近的比赛平局率高", "大赛平局率上升"],
            evidence_against=[f"欧赔{market_consensus.get('odds_info','')}不支持平局"],
        )
        hypotheses.append(h2)

        # 假设3: 冷门爆冷
        if fav == 'H':
            h3 = CounterHypothesis(
                premise="冷门: 客队爆冷",
                lam_h_mod=round(base_lam_h * 0.75, 3),
                lam_a_mod=round(base_lam_a * 1.25, 3),
                predicted_scores=self._lambda_to_scores(base_lam_h * 0.75, base_lam_a * 1.25, 3),
                probability=0.15,
                evidence_for=["弱队近期状态上升", "强队阵容不整可能", "杯赛冷门率高"],
                evidence_against=[f"实力差距巨大", f"赔率极度倾向主队"],
            )
        else:
            h3 = CounterHypothesis(
                premise="冷门: 主队爆冷",
                lam_h_mod=round(base_lam_h * 1.25, 3),
                lam_a_mod=round(base_lam_a * 0.75, 3),
                predicted_scores=self._lambda_to_scores(base_lam_h * 1.25, base_lam_a * 0.75, 3),
                probability=0.15,
                evidence_for=["主场优势可能爆发", "强队阵容不整可能", "杯赛冷门率高"],
                evidence_against=[f"实力差距巨大", f"赔率极度倾向客队"],
            )
        hypotheses.append(h3)

        return hypotheses

    # ════════════════════════════════════════════════════════════
    # 能力9: 信息质量权重
    # ════════════════════════════════════════════════════════════

    def evaluate_info_quality(self, results: List[MatchResult]) -> Dict[str, float]:
        """
        信息质量权重评估

        - 正式赛数据 > 友谊赛数据
        - 近期对手 > 历史对手
        - 正式赛 vs 友谊赛火力比 > 2.0 → 标记隐藏实力

        Args:
            results: 赛果列表

        Returns:
            dict: 信息质量指标
        """
        if not results:
            return {
                'official_ratio': 1.0,
                'recent_weight': 1.0,
                'hidden_strength_flag': 0.0,
                'data_quality_score': 0.5,
            }

        # 正式赛比例
        official_count = sum(1 for r in results if r.match_type in ('official', 'cup'))
        friendly_count = sum(1 for r in results if r.match_type == 'friendly')
        official_ratio = official_count / max(len(results), 1)

        # 正式赛 vs 友谊赛火力比
        official_goals = [r.goals_scored for r in results
                         if r.match_type in ('official', 'cup')]
        friendly_goals = [r.goals_scored for r in results
                         if r.match_type == 'friendly']

        avg_official_goals = float(np.mean(official_goals)) if official_goals else 1.0
        avg_friendly_goals = float(np.mean(friendly_goals)) if friendly_goals else 1.0

        firepower_ratio = avg_official_goals / max(avg_friendly_goals, 0.01)

        # 隐藏实力检测
        hidden_strength = 0.0
        if firepower_ratio > 2.0:
            hidden_strength = min(1.0, (firepower_ratio - 1.5) * 0.5)

        # 数据质量评分
        quality_score = (
            official_ratio * 0.5                  # 正式赛比重
            + min(len(results) / 10.0, 1.0) * 0.3 # 样本量
            + (1.0 - min(hidden_strength * 0.5, 0.5)) * 0.2  # 隐藏实力不确定性
        )

        return {
            'official_ratio': round(official_ratio, 3),
            'recent_weight': round(min(len(results) / 5.0, 1.0), 3),
            'hidden_strength_flag': round(hidden_strength, 3),
            'firepower_ratio': round(firepower_ratio, 2),
            'data_quality_score': round(quality_score, 3),
        }

    # ════════════════════════════════════════════════════════════
    # 综合推理: 运行完整数字人分析流程
    # ════════════════════════════════════════════════════════════

    def run_full_analysis(self,
                          home_team: str,
                          away_team: str,
                          base_lam_h: float,
                          base_lam_a: float,
                          home_results: List[MatchResult] = None,
                          away_results: List[MatchResult] = None,
                          home_lineup: Dict = None,
                          away_lineup: Dict = None,
                          home_psych: Dict = None,
                          away_psych: Dict = None,
                          score_odds: Dict[str, float] = None,
                          eu_odds: Tuple[float, float, float] = None,
                          ) -> Dict[str, Any]:
        """
        运行完整数字人分析流程

        Returns:
            dict: 包含所有分析结果的完整报告
        """
        home_results = home_results or []
        away_results = away_results or []
        self.prediction_chain = []
        self.iteration_count = 0

        # v1: 纯数学基线
        v1 = self.iterate_prediction(base_lam_h, base_lam_a, {
            'type': 'baseline', 'delta_h': 0.0, 'delta_a': 0.0,
            'reason': '纯数学基线: 从赔率推导的初始λ',
        })

        current_h, current_a = v1.lam_h, v1.lam_a

        # v2: 防线趋势
        defense_result = None
        if home_results and away_results:
            home_defense = self.detect_defensive_trend(home_results)
            away_defense = self.detect_defensive_trend(away_results)
            defense_delta_h, defense_delta_a = self.defensive_trend_to_lambda_adjustment(
                home_defense, away_defense
            )
            defense_result = {'home': home_defense, 'away': away_defense}

            v2 = self.iterate_prediction(current_h, current_a, {
                'type': 'defense_trend',
                'delta_h': defense_delta_h,
                'delta_a': defense_delta_a,
                'reason': f"防线趋势: {home_team}{home_defense.trend_direction}({home_defense.weighted_avg_conceded:.1f}丢/场), "
                         f"{away_team}{away_defense.trend_direction}({away_defense.weighted_avg_conceded:.1f}丢/场)",
            }, prev_scores=v1.top_scores)
            current_h, current_a = v2.lam_h, v2.lam_a

        # v3: 心理层
        psych_result = None
        if home_psych or away_psych:
            home_psych = home_psych or {}
            away_psych = away_psych or {}
            home_psy = self.evaluate_psychological_factors(home_team, home_psych)
            away_psy = self.evaluate_psychological_factors(away_team, away_psych)
            psy_delta_h, psy_delta_a = self.psychology_to_lambda_adjustment(home_psy)
            # 对手心理反向影响
            opp_psy_delta_a, opp_psy_delta_h = self.psychology_to_lambda_adjustment(away_psy)
            psy_delta_h += opp_psy_delta_h * 0.5
            psy_delta_a += opp_psy_delta_a * 0.5
            psych_result = {'home': home_psy, 'away': away_psy}

            v3 = self.iterate_prediction(current_h, current_a, {
                'type': 'psychology',
                'delta_h': psy_delta_h,
                'delta_a': psy_delta_a,
                'reason': f"心理层: {home_team}pragmatism={home_psy.pragmatism_level:.2f} lead_shrink={home_psy.lead_shrink_tendency:.2f}, "
                         f"{away_team}solo={away_psy.solo_tendency:.2f} morale={away_psy.team_morale:.1f}",
            }, prev_scores=v2.top_scores if defense_result else v1.top_scores)
            current_h, current_a = v3.lam_h, v3.lam_a

        # v4: 共同对手深挖
        common_opponent_result = None
        if home_results and away_results:
            comparisons = self.compare_via_common_opponent(home_results, away_results)
            common_opponent_result = comparisons
            if comparisons:
                # 综合共同对手对比，修正λ
                advantage_sum = 0.0
                for comp in comparisons:
                    diff = comp.team_b_performance - comp.team_a_performance
                    advantage_sum += diff

                if advantage_sum > 0.5:
                    opponent_delta_h = -0.08
                    opponent_delta_a = 0.08
                elif advantage_sum < -0.5:
                    opponent_delta_h = 0.08
                    opponent_delta_a = -0.08
                else:
                    opponent_delta_h = 0.0
                    opponent_delta_a = 0.0

                prev_scores = v3.top_scores if psych_result else (
                    v2.top_scores if defense_result else v1.top_scores
                )
                v4 = self.iterate_prediction(current_h, current_a, {
                    'type': 'common_opponent',
                    'delta_h': opponent_delta_h,
                    'delta_a': opponent_delta_a,
                    'reason': f"共同对手: {len(comparisons)}个共同对手, 对比倾向{'客队' if advantage_sum > 0 else '主队' if advantage_sum < 0 else '相当'}",
                }, prev_scores=prev_scores)
                current_h, current_a = v4.lam_h, v4.lam_a

        # v5: 阵容确认
        lineup_result = None
        if home_lineup and away_lineup:
            home_lineup_impact = self.analyze_lineup_change(
                home_lineup.get('expected', {}), home_lineup.get('actual', {})
            )
            away_lineup_impact = self.analyze_lineup_change(
                away_lineup.get('expected', {}), away_lineup.get('actual', {})
            )
            lineup_result = {'home': home_lineup_impact, 'away': away_lineup_impact}

            lineup_delta_h, _ = self.lineup_to_lambda_adjustment(home_lineup_impact)
            _, lineup_delta_a = self.lineup_to_lambda_adjustment(away_lineup_impact)
            # 对方阵容变化对己方λ的影响
            _, opp_lineup_a = self.lineup_to_lambda_adjustment(away_lineup_impact)
            lineup_delta_a += opp_lineup_a
            # away阵容影响homeλ
            opp_lineup_h, _ = self.lineup_to_lambda_adjustment(home_lineup_impact)
            lineup_delta_h += opp_lineup_h

            prev_scores_list = v4.top_scores if common_opponent_result else (
                v3.top_scores if psych_result else (
                    v2.top_scores if defense_result else v1.top_scores
                )
            )
            v5 = self.iterate_prediction(current_h, current_a, {
                'type': 'lineup',
                'delta_h': lineup_delta_h * 0.6,
                'delta_a': lineup_delta_a * 0.6,
                'reason': f"阵容: {home_team} {home_lineup_impact.formation} att={home_lineup_impact.attack_impact:+.2f}, "
                         f"{away_team} {away_lineup_impact.formation} att={away_lineup_impact.attack_impact:+.2f}",
            }, prev_scores=prev_scores_list)
            current_h, current_a = v5.lam_h, v5.lam_a

        # 赔率矛盾
        odds_contra = None
        if score_odds and eu_odds:
            odds_contra = self.detect_odds_contradiction(eu_odds, score_odds)
            if odds_contra.detected and odds_contra.confidence > 0.6:
                # 矛盾 → 适度调低热门方λ
                if eu_odds[0] < eu_odds[2]:
                    current_h -= 0.05
                    current_a += 0.03
                else:
                    current_a -= 0.05
                    current_h += 0.03

        # 信息质量
        info_quality = self.evaluate_info_quality(home_results + away_results)

        # 生成逆向假设
        consensus = {
            'favorite': 'H' if base_lam_h > base_lam_a else 'A',
            'margin': '大胜' if abs(base_lam_h - base_lam_a) > 1.5 else 'normal',
            'odds_info': f"H={eu_odds[0]:.2f}" if eu_odds else '',
        }
        hypotheses = self.generate_counter_hypotheses(consensus, current_h, current_a)

        return {
            'final_lam_h': round(max(current_h, 0.15), 3),
            'final_lam_a': round(max(current_a, 0.05), 3),
            'evolution_trajectory': self.get_evolution_trajectory(),
            'defense_analysis': defense_result,
            'psychology_analysis': psych_result,
            'common_opponent_analysis': common_opponent_result,
            'lineup_analysis': lineup_result,
            'odds_contradiction': odds_contra,
            'counter_hypotheses': [{
                'premise': h.premise,
                'lam_h': h.lam_h_mod,
                'lam_a': h.lam_a_mod,
                'predicted_scores': h.predicted_scores,
                'probability': h.probability,
                'evidence_for': h.evidence_for,
                'evidence_against': h.evidence_against,
            } for h in hypotheses],
            'info_quality': info_quality,
        }

    # ════════════════════════════════════════════════════════════
    # Web数据采集 (搜索接口预留)
    # ════════════════════════════════════════════════════════════

    def collect_team_data(self, team: str, num_recent: int = 10) -> Dict[str, Any]:
        """
        球队数据采集接口（待接入WebSearch）

        Args:
            team: 球队名
            num_recent: 近期比赛数

        Returns:
            dict: 采集到的数据（调用指南）
        """
        return {
            'status': 'interface_ready',
            'search_queries': [
                f"{team} last {num_recent} matches 2026",
                f"{team} starting lineup latest",
                f"{team} coach contract injury news 2026",
                f"{team} recent form goals conceded 2026",
            ],
            'expected_data_format': {
                'matches': 'List[MatchResult]',
                'lineup': 'Dict with formation, players, ratings',
                'psych': 'Dict with coach info, morale, star pressure',
            },
            'note': 'WebSearch + WebFetch 接入点就绪，需运行时调用',
        }

    # ════════════════════════════════════════════════════════════
    # 内部工具
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _lambda_to_scores(lam_h: float, lam_a: float, top_n: int = 3,
                          max_g: int = 6) -> List[str]:
        """λ → Top N比分预测"""
        ph = np.array([max(np.exp(-lam_h) * lam_h**k / math.factorial(k), 1e-30)
                       for k in range(max_g + 1)])
        pa = np.array([max(np.exp(-lam_a) * lam_a**k / math.factorial(k), 1e-30)
                       for k in range(max_g + 1)])
        ph /= ph.sum()
        pa /= pa.sum()

        scores = []
        for gh in range(max_g + 1):
            for ga in range(max_g + 1):
                p = float(ph[gh] * pa[ga])
                if p < 0.005:
                    continue
                scores.append((f"{gh}-{ga}", p))

        scores.sort(key=lambda x: x[1], reverse=True)
        return [s[0] for s in scores[:top_n]]

    @staticmethod
    def parse_match_result(team: str, opponent: str, score_str: str,
                           home_away: str = 'home',
                           match_type: str = 'official',
                           date: str = '') -> MatchResult:
        """解析比赛结果"""
        parts = score_str.split('-')
        if len(parts) == 2:
            gs = int(parts[0]) if home_away == 'home' else int(parts[1])
            gc = int(parts[1]) if home_away == 'home' else int(parts[0])
        else:
            gs, gc = 0, 0

        result = 'W' if gs > gc else ('D' if gs == gc else 'L')

        return MatchResult(
            team=team, opponent=opponent, score=score_str,
            home_away=home_away, match_type=match_type,
            goals_scored=gs, goals_conceded=gc,
            date=date, result=result,
        )


# ════════════════════════════════════════════════════════════════
# 验证: 葡萄牙 1-1 刚果 案例
# ════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    dh = DigitalHuman()

    print("=" * 70)
    print("  数字人引擎验证: 葡萄牙 vs 刚果民主共和国")
    print("  实际比分: 1-1")
    print("=" * 70)
    print()

    # ── 准备数据 ──
    # 葡萄牙近期赛果
    portugal_results = [
        dh.parse_match_result('葡萄牙', '西班牙', '1-1', 'away', 'official', '2026-06-01'),
        dh.parse_match_result('葡萄牙', '克罗地亚', '2-1', 'home', 'official', '2026-05-25'),
        dh.parse_match_result('葡萄牙', '斯洛文尼亚', '3-0', 'away', 'cup', '2026-05-18'),
        dh.parse_match_result('葡萄牙', '冰岛', '2-2', 'home', 'official', '2026-05-10'),
        dh.parse_match_result('葡萄牙', '斯洛伐克', '2-0', 'home', 'official', '2026-04-28'),
    ]

    # 刚果近期赛果
    congo_results = [
        dh.parse_match_result('刚果民主共和国', '尼日利亚', '4-5', 'away', 'official', '2026-06-05'),
        dh.parse_match_result('刚果民主共和国', '加蓬', '2-1', 'home', 'official', '2026-05-22'),
        dh.parse_match_result('刚果民主共和国', '安哥拉', '1-1', 'away', 'friendly', '2026-05-12'),
        dh.parse_match_result('刚果民主共和国', '南非', '2-2', 'home', 'official', '2026-04-25'),
        dh.parse_match_result('刚果民主共和国', '赞比亚', '3-0', 'home', 'friendly', '2026-04-10'),
    ]

    # 心理上下文
    portugal_psych = {
        'coach_contract_expiring': True,
        'coach_pressure': 0.6,
        'star_pressure': 0.7,   # C罗压力
        'team_morale': -0.1,
        'recent_form': 'stable',
    }

    congo_psych = {
        'coach_contract_expiring': False,
        'coach_pressure': 0.1,
        'star_pressure': 0.0,
        'team_morale': 0.3,
        'recent_form': 'good',
    }

    # 运行完整分析
    result = dh.run_full_analysis(
        home_team='葡萄牙',
        away_team='刚果民主共和国',
        base_lam_h=2.40,
        base_lam_a=0.70,
        home_results=portugal_results,
        away_results=congo_results,
        home_psych=portugal_psych,
        away_psych=congo_psych,
        score_odds={
            "1-0": 4.85, "1-1": 11.0, "1-2": 24.0,
            "2-0": 3.70, "2-1": 7.00, "2-2": 36.0,
            "3-0": 4.15, "3-1": 7.25,
            "0-0": 14.0, "0-1": 21.0,
        },
        eu_odds=(1.27, 5.60, 11.0),
    )

    # ── 输出演变轨迹 ──
    print("── 迭代演变轨迹 ──")
    for t in result['evolution_trajectory']:
        print(f"  v{t['version']} [{t['trigger']}]: λ_H={t['lam_h']:.3f} λ_A={t['lam_a']:.3f} "
              f"diff={t['lambda_diff']:.3f} → {t['top_scores']}")
        if t['reasoning']:
            print(f"    {t['reasoning'][:100]}")

    # ── 防线分析 ──
    if result['defense_analysis']:
        print(f"\n── 防线分析 ──")
        ht = result['defense_analysis']['home']
        at = result['defense_analysis']['away']
        print(f"  葡萄牙: {ht.trend_direction}, 加权失球={ht.weighted_avg_conceded}, 零封率={ht.clean_sheet_prob}")
        print(f"  刚果: {at.trend_direction}, 加权失球={at.weighted_avg_conceded}, 零封率={at.clean_sheet_prob}")

    # ── 心理分析 ──
    if result['psychology_analysis']:
        print(f"\n── 心理层 ──")
        hp = result['psychology_analysis']['home']
        ap = result['psychology_analysis']['away']
        print(f"  葡萄牙: pragmatism={hp.pragmatism_level:.2f} lead_shrink={hp.lead_shrink_tendency:.2f} "
              f"attack_mod={hp.composite_attack_modifier:+.3f}")
        print(f"  刚果: morale={ap.team_morale:.1f} attack_mod={ap.composite_attack_modifier:+.3f}")

    # ── 共同对手 ──
    if result['common_opponent_analysis']:
        print(f"\n── 共同对手分析 ──")
        for comp in result['common_opponent_analysis']:
            print(f"  vs {comp.opponent}: {comp.advantage} ({comp.key_diff[:80]})")

    # ── 逆向假设 ──
    print(f"\n── 逆向假设 ──")
    for h in result['counter_hypotheses']:
        print(f"  [{h['probability']:.0%}] {h['premise']}")
        print(f"    λ={h['lam_h']:.2f}/{h['lam_a']:.2f} → {h['predicted_scores']}")
        print(f"    支持: {'; '.join(h['evidence_for'][:2])}")
        print(f"    反对: {'; '.join(h['evidence_against'][:2])}")

    # ── 信息质量 ──
    print(f"\n── 信息质量 ──")
    for k, v in result['info_quality'].items():
        print(f"  {k}: {v}")

    print(f"\n── 最终预测 ──")
    print(f"  数字人 λ: H={result['final_lam_h']:.3f} A={result['final_lam_a']:.3f}")
    print(f"  数字人 Top3: {dh._lambda_to_scores(result['final_lam_h'], result['final_lam_a'], 3)}")
    print(f"  实际结果: 1-1 (平局)")

    # 验证
    scores = dh._lambda_to_scores(result['final_lam_h'], result['final_lam_a'], 3)
    has_draw = any(s.split('-')[0] == s.split('-')[1] for s in scores)
    print(f"\n  验证: {'✅ 平局在Top3中' if has_draw else '❌ 未预测平局'}")
    print(f"  最终λ差={result['final_lam_h']-result['final_lam_a']:.3f} "
          f"(基线差={2.40-0.70:.2f}, 修正幅度={((2.40-0.70)-(result['final_lam_h']-result['final_lam_a']))/(2.40-0.70)*100:.0f}%)")
    print("=" * 70)

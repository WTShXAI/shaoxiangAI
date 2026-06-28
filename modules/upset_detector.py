#!/usr/bin/env python3
"""
哨响AI - 增强冷门检测器 v3.0
=============================
多维冷门分析，融合旧版完整引擎 v1.0 的 8 维信号 + 新版 v2.0 的结构化架构。

v1.0 信号（已恢复）：
  1. Poisson 比分预测 → 精确到具体比分，高赔率的数学基础
  2. 赔率背离检测 → 市场无效性，26sp/56sp 的来源
  3. 形态背离 → 弱队上升 vs 强队下滑
  4. 市场过度自信 → 赔率过度倾斜强队
  5. 历史冷门率 → 该联赛/球队的冷门倾向
  6. 反超/翻盘倾向 → 弱队爆冷历史
  7. 大球冷门加分 → Poisson 预期>2.5
  8. 大赛冷门模式 → 决赛/半决赛检测

v2.0 新增：
  - UpsetSignal/UpsetAnalysis 数据类
  - 结构化信号融合
  - 冷门加权概率调整
  - detect() 统一入口

v3.0 合并：
  - 保留 EnhancedUpsetDetector 类名 + detect() 接口（兼容现有调用）
  - 恢复 Poisson、Kelly、回测、高赔率比分推荐
  - 8 维加权融合替代 4 维简单加权
"""
import math
import warnings
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

try:
    from scipy.special import gammaln
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)

@dataclass
class UpsetSignal:
    """冷门信号"""
    signal_type: str       # odds_deviation / strength_gap / market_sentiment / historical / poisson / comeback / goal_rush / big_match
    direction: str         # home_win / draw / away_win
    strength: float        # 0~1
    edge: float            # 偏差百分比
    level: str             # 🔥强冷门 / ⚡中等冷门 / 💡轻微偏差
    description: str

@dataclass
class UpsetAnalysis:
    """冷门分析结果"""
    upset_probability: float
    upset_level: str
    upset_direction: str
    weighted_probs: Dict[str, float]
    signals: List[UpsetSignal]
    overall_score: float
    recommendation: str

class EnhancedUpsetDetector:
    """
    增强版冷门检测器 v3.0 — 合并 v1.0 完整引擎 + v2.0 结构化架构

    用法（兼容 v2.0）:
        detector = EnhancedUpsetDetector(db_manager=db)
        result = detector.detect(
            match_info={"home": "曼联", "away": "伯恩茅斯"},
            fused_probs={"home_win": 0.55, "draw": 0.25, "away_win": 0.20},
            odds={"home": 1.4, "draw": 4.5, "away": 7.5},
            league="PL",
        )

    用法（兼容 v1.0，完整分析）:
        result = detector.analyze_match(match_data)
    """

    # === v2.1 冷门阈值 (2026-06-19 赔率阈值数据校准, 强度阈值保持原值) ===
    STRONG_THRESHOLD = 0.15   # 保持原值 (需end-to-end校准)
    MEDIUM_THRESHOLD = 0.08   # 保持原值
    MILD_THRESHOLD = 0.03     # 保持原值

    # === v2.1 冷门模型融合权重 ===
    UPSET_FUSION_WEIGHT = 0.35

    # === v1.1 冷门赔率阈值 (数据校准: upset_matches 5,497条) ===
    UPSET_ODDS_THRESHOLD = 3.0   # ✅ P25=3.0，原值正确
    HIGH_ODDS_THRESHOLD = 3.8    # 原5.0 → P75=3.8 (75%冷门赔率≤3.8)
    VALUE_GAP_THRESHOLD = 0.05   # ✅ 保持
    EV_THRESHOLD = 0.05          # ✅ 保持

    # === v1.1 大球冷门参数 ===
    GOAL_RUSH_TOTAL = 2.5   # ✅ 保持
    GOAL_RUSH_DIFF = 2      # ✅ 保持

    # === v3.0 8维融合权重 ===
    WEIGHTS_V1 = {
        'odds_divergence': 0.20,
        'poisson_upset': 0.18,
        'form_divergence': 0.12,
        'market_overconfidence': 0.12,
        'historical_upset_rate': 0.10,
        'comeback_tendency': 0.10,
        'goal_rush_bonus': 0.10,
        'big_match_upset': 0.08,
    }

    def __init__(self, db_manager=None):
        self.db = db_manager
        self.league_upset_rates: Dict[str, float] = {}

    # ══════════════════════════════════════════════════════════════
    #  v2.0 兼容入口 — detect()
    # ══════════════════════════════════════════════════════════════

    def detect(self, match_info: Dict, fused_probs: Dict[str, float],
               odds: Dict[str, float], home_profile: Any = None,
               away_profile: Any = None, league: str = "PL",
               confidence_level: str = "中",
               models_agree: bool = True) -> Optional[Dict]:
        """
        多维冷门检测（主入口，v2.0 兼容）

        v3.0 增强：在原有 4 维信号基础上，新增 Poisson/反超/大球/大赛 4 维信号
        """
        signals = []

        # ── v2.0 原有 4 维 ──
        # 1. 赔率偏差检测
        odds_signals = self._detect_odds_deviation(match_info, fused_probs, odds)
        signals.extend(odds_signals)

        # 2. 实力差检测
        if home_profile is not None and away_profile is not None:
            strength_signals = self._detect_strength_gap(
                match_info, home_profile, away_profile, odds
            )
            signals.extend(strength_signals)

        # 3. 市场情绪冷门检测
        sentiment_signals = self._detect_sentiment_upset(match_info, odds)
        signals.extend(sentiment_signals)

        # 4. 历史冷门率检测
        if self.db:
            historical_signals = self._detect_historical_pattern(
                match_info, league, odds
            )
            signals.extend(historical_signals)

        # ── v3.0 新增 4 维 ──
        # 5. Poisson 冷门信号
        poisson_signals = self._detect_poisson_upset(match_info, odds, fused_probs)
        signals.extend(poisson_signals)

        # 6. 反超/翻盘倾向
        comeback_signals = self._detect_comeback_tendency(match_info, odds)
        signals.extend(comeback_signals)

        # 7. 大球冷门加分
        goal_rush_signals = self._detect_goal_rush(match_info, odds)
        signals.extend(goal_rush_signals)

        # 8. 大赛冷门模式
        big_match_signals = self._detect_big_match_upset(match_info, odds)
        signals.extend(big_match_signals)

        if not signals:
            return None

        signals.sort(key=lambda x: -x.strength)

        # 计算综合评分（v3.0 使用8维权重）
        upset_score = self._compute_upset_score_v3(signals)
        upset_prob, upset_level = self._classify_upset(upset_score)

        # 确定冷门方向
        upset_direction = self._determine_upset_direction(signals, fused_probs, odds)

        # 冷门加权概率
        weighted_probs = self._apply_upset_weighting(
            fused_probs, signals, upset_score, upset_direction
        )

        # 生成建议
        recommendation = self._generate_recommendation(
            upset_level, upset_direction, signals, weighted_probs, confidence_level
        )

        # Poisson 比分预测（可选增强）
        score_prediction = self._quick_score_prediction(odds, fused_probs)

        strongest = signals[0]
        result = {
            "match": f"{match_info['home']} vs {match_info['away']}",
            "signals": [
                {
                    "outcome": self._translate_direction(s.direction),
                    "direction": "看高" if s.direction == strongest.direction else "看低",
                    "level": s.level,
                    "level_color": {
                        "🔥强冷门": "red", "⚡中等冷门": "orange", "💡轻微偏差": "yellow"
                    }.get(s.level, "yellow"),
                    "model_prob": round(
                        self._get_prob_for_direction(s, fused_probs) * 100, 1
                    ),
                    "implied_prob": round(
                        self._get_implied_for_direction(s, odds) * 100, 1
                    ),
                    "edge": round(s.edge, 1),
                    "odd": self._get_odd_for_direction(s, odds),
                    "reason": s.description,
                }
                for s in signals[:5]
            ],
            "recommendation": recommendation,
            "confidence_level": confidence_level,
            "upset_probability": round(upset_prob, 4),
            "upset_level": upset_level,
            "upset_direction": upset_direction,
            "overall_score": round(upset_score, 3),
            "weighted_probs": {k: round(v * 100, 1) for k, v in weighted_probs.items()},
        }

        # 附加 Poisson 预测
        if score_prediction:
            result["score_prediction"] = score_prediction

        return result

    # ══════════════════════════════════════════════════════════════
    #  v1.0 兼容入口 — analyze_match()
    # ══════════════════════════════════════════════════════════════

    def analyze_match(self, match_data: Dict) -> Dict:
        """
        单场比赛冷门分析（v1.0 完整引擎入口）

        Args:
            match_data: {
                'match_id': int,
                'home_team_id': int, 'away_team_id': int,
                'home_team_name': str, 'away_team_name': str,
                'league_id': int, 'league_name': str,
                'model_prob': float,
                'odds': {'home_odds': float, 'draw_odds': float, 'away_odds': float},
            }
        """
        match_name = f"{match_data.get('home_team_name','主')} vs {match_data.get('away_team_name','客')}"
        logger.info(f"🔍 分析: {match_name}")

        model_prob = match_data.get('model_prob', 0.5)
        odds = match_data.get('odds', {})

        # 1. Poisson比分预测
        lambda_home, lambda_away = self.estimate_expected_goals(match_data)
        score_dist = self.predict_score_distribution(lambda_home, lambda_away)
        logger.info(f"  📊 λ_home={lambda_home:.2f} λ_away={lambda_away:.2f} "
                     f"期望总进球={lambda_home+lambda_away:.2f}")

        # 2. 赔率背离
        odds_div = self.calculate_odds_divergence(match_data, model_prob)

        # 3. 形态背离
        form_div = self.calculate_form_divergence(match_data)

        # 4. 市场过度自信
        market_conf = self.calculate_market_overconfidence(odds)

        # 5. 历史冷门率
        hist_upset = self.calculate_historical_upset_rate(match_data)

        # 6. 反超/翻盘倾向
        comeback = self.calculate_comeback_tendency(match_data, form_div)

        # 7. 大球冷门加分
        goal_rush = self.calculate_goal_rush_bonus(score_dist, odds, form_div)

        # 8. 大赛冷门模式
        big_match = self.calculate_big_match_upset(match_data, score_dist)

        # 9. 合成冷门评分
        upset_dirs = odds_div.get('upset_directions', [])
        if 'away' in upset_dirs:
            poisson_upset_prob = float(score_dist['outcomes']['away_win']) / 100
        elif 'home' in upset_dirs:
            poisson_upset_prob = float(score_dist['outcomes']['home_win']) / 100
        elif 'draw' in upset_dirs:
            poisson_upset_prob = float(score_dist['outcomes']['draw']) / 100
        elif odds_div['max_direction'] == 'away':
            poisson_upset_prob = float(score_dist['outcomes']['away_win']) / 100
        elif odds_div['max_direction'] == 'draw':
            poisson_upset_prob = float(score_dist['outcomes']['draw']) / 100
        elif odds_div['max_direction'] == 'home' and odds.get('home_odds', 2.0) > self.UPSET_ODDS_THRESHOLD:
            poisson_upset_prob = float(score_dist['outcomes']['home_win']) / 100
        else:
            poisson_upset_prob = 0.0

        signals = {
            'odds_divergence': odds_div,
            'poisson_upset': {'upset_probability': poisson_upset_prob},
            'form_divergence': form_div,
            'market_overconfidence': market_conf,
            'historical_upset': hist_upset,
            'comeback_tendency': comeback,
            'goal_rush_bonus': goal_rush,
            'big_match_upset': big_match,
        }
        upset = self.synthesize_upset_score(signals)

        logger.info(f"  🎰 冷门评分: {upset['upset_score']:.1f}/100")
        logger.info(f"  {upset['upset_level']}")

        # 10. 高赔率比分推荐
        bets = []
        if upset['upset_score'] >= 20:
            bets = self.recommend_high_odds_bets(score_dist, odds, upset['upset_score'])

        return {
            'match_id': match_data.get('match_id'),
            'match_name': match_name,
            'analyzed_at': datetime.now(timezone.utc).isoformat(),
            'poisson_prediction': score_dist,
            'odds_divergence': odds_div,
            'form_divergence': form_div,
            'market_overconfidence': market_conf,
            'historical_upset': hist_upset,
            'comeback_tendency': comeback,
            'goal_rush_bonus': goal_rush,
            'big_match_upset': big_match,
            'upset_assessment': upset,
            'high_odds_bets': bets,
        }

    # ══════════════════════════════════════════════════════════════
    #  v2.0 子检测方法（保持原样）
    # ══════════════════════════════════════════════════════════════

    def _detect_odds_deviation(self, match_info: Dict,
                                fused_probs: Dict[str, float],
                                odds: Dict[str, float]) -> List[UpsetSignal]:
        """赔率偏差检测 — 模型概率 vs 市场隐含概率"""
        margin = (1 / max(odds["home"], 1.01) +
                  1 / max(odds["draw"], 1.01) +
                  1 / max(odds["away"], 1.01))
        implied = {
            "home_win": (1 / odds["home"]) / margin,
            "draw": (1 / odds["draw"]) / margin,
            "away_win": (1 / odds["away"]) / margin,
        }

        signals = []
        for outcome in ("home_win", "draw", "away_win"):
            diff = fused_probs.get(outcome, 1/3) - implied[outcome]  # ★ C4：统一回退为1/3，替代旧0.33硬编码
            abs_diff = abs(diff)

            if abs_diff < self.MILD_THRESHOLD:
                continue

            level = self._get_diff_level_name(abs_diff)
            label_map = {"home_win": "主胜", "draw": "平局", "away_win": "客胜"}
            desc = (
                f"模型{label_map[outcome]}概率({fused_probs[outcome]*100:.1f}%)"
                f"{'高于' if diff > 0 else '低于'}赔率隐含({implied[outcome]*100:.1f}%)"
                f"，差值{abs_diff*100:.1f}%"
            )

            signals.append(UpsetSignal(
                signal_type="odds_deviation",
                direction=outcome,
                strength=min(abs_diff / self.STRONG_THRESHOLD, 1.0),
                edge=abs_diff * 100,
                level=level,
                description=desc,
            ))

        return signals

    def _detect_strength_gap(self, match_info: Dict,
                              home_profile: Any, away_profile: Any,
                              odds: Dict[str, float]) -> List[UpsetSignal]:
        """实力差冷门检测"""
        signals = []

        if isinstance(home_profile, dict):
            hp_rating = float(home_profile.get("rating", 72))
        else:
            hp_rating = float(getattr(home_profile, "rating", 72))

        if isinstance(away_profile, dict):
            ap_rating = float(away_profile.get("rating", 70))
        else:
            ap_rating = float(getattr(away_profile, "rating", 70))

        rating_gap = hp_rating - ap_rating

        min_odd_outcome = min(odds.items(), key=lambda x: x[1])
        odd_map = {"home": "home_win", "draw": "draw", "away": "away_win"}
        implied_favorite = odd_map.get(min_odd_outcome[0], "home_win")

        if abs(rating_gap) > 5:
            favorite_by_rating = "home_win" if rating_gap > 5 else "away_win"
            if favorite_by_rating != implied_favorite:
                strength = min(abs(rating_gap) / 20, 1.0)
                edge = abs(rating_gap) * 1.5
                signals.append(UpsetSignal(
                    signal_type="strength_gap",
                    direction=favorite_by_rating,
                    strength=strength,
                    edge=edge,
                    level=self._get_diff_level_by_strength(edge),
                    description=(
                        f"实力评分差 {abs(rating_gap):.1f} 分，"
                        f"但赔率倾向于 {self._translate_direction(implied_favorite)}，"
                        f"可能存在实力冷门"
                    ),
                ))

        if rating_gap > 8 and odds.get("away", 99) > 3.5:
            signals.append(UpsetSignal(
                signal_type="strength_gap",
                direction="away_win",
                strength=0.3, edge=5.0,
                level="💡轻微偏差",
                description=f"主队实力明显占优(差{rating_gap:.0f}分)，但客胜高赔({odds.get('away', 0):.2f})存在冷门空间",
            ))
        elif rating_gap < -8 and odds.get("home", 99) > 3.5:
            signals.append(UpsetSignal(
                signal_type="strength_gap",
                direction="home_win",
                strength=0.3, edge=5.0,
                level="💡轻微偏差",
                description=f"客队实力明显占优(差{abs(rating_gap):.0f}分)，但主胜高赔({odds.get('home', 0):.2f})存在冷门空间",
            ))

        return signals

    def _detect_sentiment_upset(self, match_info: Dict,
                                 odds: Dict[str, float]) -> List[UpsetSignal]:
        """市场情绪冷门检测（基于赔率结构）"""
        signals = []
        draw_odd = odds.get("draw", 3.5)
        home_odd = odds.get("home", 2.0)
        away_odd = odds.get("away", 3.0)

        spread = abs(1 / home_odd - 1 / away_odd) if home_odd > 0 and away_odd > 0 else 0

        if draw_odd > 3.8 and spread > 0.15:
            signals.append(UpsetSignal(
                signal_type="market_sentiment",
                direction="draw",
                strength=min((draw_odd - 3.5) / 3, 0.5),
                edge=min((draw_odd - 3.5) * 3, 15),
                level="💡轻微偏差",
                description=f"平赔({draw_odd:.2f})偏高，实力差距明显但平局有冷门空间",
            ))

        if home_odd < 1.4 and away_odd > 6.0:
            signals.append(UpsetSignal(
                signal_type="market_sentiment",
                direction="away_win",
                strength=0.4, edge=10.0,
                level="⚡中等冷门",
                description=f"主胜赔率极低({home_odd:.2f})，警惕过热陷阱，客胜冷门概率提升",
            ))
        if away_odd < 1.4 and home_odd > 6.0:
            signals.append(UpsetSignal(
                signal_type="market_sentiment",
                direction="home_win",
                strength=0.4, edge=10.0,
                level="⚡中等冷门",
                description=f"客胜赔率极低({away_odd:.2f})，警惕过热陷阱，主胜冷门概率提升",
            ))

        return signals

    def _detect_historical_pattern(self, match_info: Dict, league: str,
                                    odds: Dict[str, float]) -> List[UpsetSignal]:
        """历史冷门模式检测"""
        signals = []

        upset_rate = self.league_upset_rates.get(league, 0.0)
        if upset_rate == 0.0 and self.db:
            try:
                stats = self.db.get_stats()
                accuracy = stats.get("accuracy", 100)
                upset_rate = max(0.0, 1.0 - accuracy / 100)
                self.league_upset_rates[league] = upset_rate
            except (Exception, KeyError, IndexError, requests.exceptions.RequestException):
                pass

        if upset_rate > 0.35:
            signals.append(UpsetSignal(
                signal_type="historical",
                direction="away_win",
                strength=min(upset_rate, 0.5),
                edge=upset_rate * 100,
                level="⚡中等冷门" if upset_rate > 0.4 else "💡轻微偏差",
                description=f"该联赛历史冷门率 {upset_rate*100:.1f}%，冷门频发需警惕",
            ))
        elif upset_rate > 0.25:
            signals.append(UpsetSignal(
                signal_type="historical",
                direction="draw",
                strength=0.2,
                edge=upset_rate * 100,
                level="💡轻微偏差",
                description=f"该联赛历史冷门率 {upset_rate*100:.1f}%，有一定冷门风险",
            ))

        return signals

    # ══════════════════════════════════════════════════════════════
    #  v3.0 新增子检测方法（来自 v1.0）
    # ══════════════════════════════════════════════════════════════

    def _detect_poisson_upset(self, match_info: Dict,
                               odds: Dict[str, float],
                               fused_probs: Dict[str, float]) -> List[UpsetSignal]:
        """Poisson 冷门信号检测 — 高赔率方向模型概率高于市场隐含"""
        signals = []

        # 从赔率反推期望进球（简化估算）
        home_odd = odds.get("home", 2.0)
        away_odd = odds.get("away", 3.0)

        home_implied = 1 / max(home_odd, 1.01)
        away_implied = 1 / max(away_odd, 1.01)

        # 估算 λ：基于隐含概率和联赛均值
        avg_total = 2.7
        home_adv = 0.35
        lambda_home = (avg_total / 2 + home_adv / 2) * (home_implied / 0.4) ** 0.5
        lambda_away = (avg_total / 2 - home_adv / 2) * (away_implied / 0.3) ** 0.5
        lambda_home = max(0.3, min(6.0, lambda_home))
        lambda_away = max(0.3, min(6.0, lambda_away))

        # Poisson 客胜概率
        away_win_prob = self._poisson_outcome_prob(lambda_home, lambda_away, "away_win")
        draw_prob = self._poisson_outcome_prob(lambda_home, lambda_away, "draw")
        home_win_prob = self._poisson_outcome_prob(lambda_home, lambda_away, "home_win")

        # 检测高赔率方向的背离
        for direction, prob, odd_key in [
            ("away_win", away_win_prob, "away"),
            ("draw", draw_prob, "draw"),
            ("home_win", home_win_prob, "home"),
        ]:
            odd = odds.get(odd_key, 3.0)
            if odd > self.UPSET_ODDS_THRESHOLD:
                implied = 1 / odd
                diff = prob - implied
                if diff > self.VALUE_GAP_THRESHOLD:
                    strength = min(diff * 5, 1.0)
                    signals.append(UpsetSignal(
                        signal_type="poisson",
                        direction=direction,
                        strength=strength,
                        edge=diff * 100,
                        level=self._get_diff_level_name(diff),
                        description=(
                            f"Poisson模型{self._translate_direction(direction)}概率"
                            f"({prob*100:.1f}%)高于赔率隐含({implied*100:.1f}%)，"
                            f"λ主={lambda_home:.1f} λ客={lambda_away:.1f}"
                        ),
                    ))

        return signals

    def _detect_comeback_tendency(self, match_info: Dict,
                                   odds: Dict[str, float]) -> List[UpsetSignal]:
        """反超/翻盘倾向检测"""
        signals = []

        home_odd = odds.get("home", 2.0)
        away_odd = odds.get("away", 3.0)

        # 确定弱队方向
        if away_odd > home_odd:
            underdog_dir = "away_win"
            odds_ratio = away_odd / max(home_odd, 1.01)
        else:
            underdog_dir = "home_win"
            odds_ratio = home_odd / max(away_odd, 1.01)

        # 信号1：赔率比越大，爆冷赔率越高
        if odds_ratio > 3.5:
            signals.append(UpsetSignal(
                signal_type="comeback",
                direction=underdog_dir,
                strength=0.4,
                edge=odds_ratio * 2,
                level="⚡中等冷门",
                description=f"强弱势比{odds_ratio:.1f}:1，高赔率爆冷潜力",
            ))
        elif odds_ratio > 2.5:
            signals.append(UpsetSignal(
                signal_type="comeback",
                direction=underdog_dir,
                strength=0.25,
                edge=odds_ratio,
                level="💡轻微偏差",
                description=f"强弱差距明显(赔率比{odds_ratio:.1f}:1)，弱队有翻盘可能",
            ))

        # 信号2：从数据库查询弱队爆冷历史
        if self.db:
            try:
                with self.db.get_connection() as conn:
                    underdog_name = match_info.get('away' if underdog_dir == 'away_win' else 'home', '')
                    if underdog_name:
                        upsets = conn.execute(
                            '''SELECT COUNT(*) FROM matches m
                               JOIN odds o ON m.match_id = o.match_id
                               WHERE m.home_score IS NOT NULL
                               AND ((m.home_team_name=? AND o.home_odds > 3.0 AND m.home_score > m.away_score)
                                 OR (m.away_team_name=? AND o.away_odds > 3.0 AND m.away_score > m.home_score))
                               LIMIT 20''',
                            (underdog_name, underdog_name)
                        ).fetchone()
                        total = conn.execute(
                            '''SELECT COUNT(*) FROM matches
                               WHERE (home_team_name=? OR away_team_name=?) AND home_score IS NOT NULL
                               LIMIT 20''',
                            (underdog_name, underdog_name)
                        ).fetchone()

                        if total and total[0] > 0 and upsets:
                            rate = upsets[0] / total[0]
                            if rate >= 0.15:
                                signals.append(UpsetSignal(
                                    signal_type="comeback",
                                    direction=underdog_dir,
                                    strength=min(rate * 3, 0.6),
                                    edge=rate * 100,
                                    level="🔥强冷门" if rate > 0.25 else "⚡中等冷门",
                                    description=f"弱队近{total[0]}场爆冷{upsets[0]}次(率{rate*100:.0f}%)",
                                ))
            except (Exception, KeyError, IndexError):
                pass

        return signals

    def _detect_goal_rush(self, match_info: Dict,
                           odds: Dict[str, float]) -> List[UpsetSignal]:
        """大球冷门加分检测"""
        signals = []

        # 从赔率估算总进球
        home_odd = odds.get("home", 2.0)
        away_odd = odds.get("away", 3.0)

        home_implied = 1 / max(home_odd, 1.01)
        away_implied = 1 / max(away_odd, 1.01)

        avg_total = 2.7
        lambda_home = (avg_total / 2 + 0.175) * (home_implied / 0.4) ** 0.3
        lambda_away = (avg_total / 2 - 0.175) * (away_implied / 0.3) ** 0.3
        total_goals = lambda_home + lambda_away

        # 大球场景
        if total_goals > self.GOAL_RUSH_TOTAL:
            strength = min((total_goals - 2.5) / 2, 0.8)

            # 有高赔率冷门方向
            if away_odd > 3.0 or home_odd > 3.0:
                cold_dir = "away_win" if away_odd > home_odd else "home_win"
                signals.append(UpsetSignal(
                    signal_type="goal_rush",
                    direction=cold_dir,
                    strength=strength,
                    edge=total_goals * 3,
                    level="⚡中等冷门" if total_goals > 3.0 else "💡轻微偏差",
                    description=f"大球场景(预期{total_goals:.1f}球)，高赔率方向冷门概率提升",
                ))

            if total_goals > 3.0:
                signals.append(UpsetSignal(
                    signal_type="goal_rush",
                    direction="draw",
                    strength=0.2,
                    edge=total_goals * 2,
                    level="💡轻微偏差",
                    description=f"超级大球模式(预期{total_goals:.1f}球)，高比分平局有冷门空间",
                ))

        return signals

    def _detect_big_match_upset(self, match_info: Dict,
                                 odds: Dict[str, float]) -> List[UpsetSignal]:
        """大赛冷门模式检测"""
        signals = []

        # 比赛名称中检测大赛关键词
        match_name = f"{match_info.get('home', '')} vs {match_info.get('away', '')}"

        big_match_keywords = [
            'final', 'semi', 'quarter', 'cup', 'championship', 'champions',
            '决赛', '半决赛', '四强', '八强', '冠军', '杯', '淘汰赛',
            'playoff', 'knockout', 'clasico', 'derby',
            'world cup', 'euro', 'champions league', 'uefa',
        ]

        matched_keywords = []
        for kw in big_match_keywords:
            if kw.lower() in match_name.lower():
                matched_keywords.append(kw)

        if not matched_keywords:
            return signals

        # 大赛信号
        home_odd = odds.get("home", 2.0)
        away_odd = odds.get("away", 3.0)

        underdog_dir = "away_win" if away_odd > home_odd else "home_win"

        # 压力级
        pressure_bonus = 0.0
        if any(k in match_name.lower() for k in ['final', '决赛', '冠军']):
            pressure_bonus = 0.25
            pressure_level = "决赛级"
        elif any(k in match_name.lower() for k in ['semi', '半决赛', '四强']):
            pressure_bonus = 0.15
            pressure_level = "半决赛级"
        elif any(k in match_name.lower() for k in ['quarter', '八强']):
            pressure_bonus = 0.10
            pressure_level = "八强级"
        else:
            pressure_bonus = 0.05
            pressure_level = "大赛"

        if pressure_bonus > 0.05:
            signals.append(UpsetSignal(
                signal_type="big_match",
                direction=underdog_dir,
                strength=pressure_bonus,
                edge=pressure_bonus * 50,
                level="🔥强冷门" if pressure_bonus > 0.2 else "⚡中等冷门",
                description=f"大赛标签({','.join(matched_keywords[:3])})，{pressure_level}压力↑强队崩盘风险",
            ))

        return signals

    # ══════════════════════════════════════════════════════════════
    #  v1.0 Poisson 比分预测模型
    # ══════════════════════════════════════════════════════════════

    def estimate_expected_goals(self, match_data: Dict) -> Tuple[float, float]:
        """估算主客队期望进球 λ_home, λ_away"""
        home_id = match_data.get('home_team_id')
        away_id = match_data.get('away_team_id')
        league_id = match_data.get('league_id')

        league_avg_total = 2.70
        home_advantage = 0.35
        avg_home_goals = (league_avg_total / 2) + home_advantage / 2
        avg_away_goals = (league_avg_total / 2) - home_advantage / 2

        try:
            with self.db.get_connection() as conn:
                if home_id:
                    home_matches = conn.execute(
                        '''SELECT home_score, away_score FROM matches
                           WHERE home_team_id=? AND home_score IS NOT NULL
                           ORDER BY match_date DESC LIMIT 10''',
                        (home_id,)
                    ).fetchall()
                    if home_matches:
                        home_scored = sum(m[0] for m in home_matches)
                        home_conceded = sum(m[1] for m in home_matches)
                        home_n = len(home_matches)
                        avg_home_goals = home_scored / home_n
                        avg_home_conceded = home_conceded / home_n

                if away_id:
                    away_matches = conn.execute(
                        '''SELECT home_score, away_score FROM matches
                           WHERE away_team_id=? AND home_score IS NOT NULL
                           ORDER BY match_date DESC LIMIT 10''',
                        (away_id,)
                    ).fetchall()
                    if away_matches:
                        away_scored = sum(m[1] for m in away_matches)
                        away_conceded = sum(m[0] for m in away_matches)
                        away_n = len(away_matches)
                        avg_away_goals = away_scored / away_n
                        avg_away_conceded = away_conceded / away_n

                if league_id:
                    league_row = conn.execute(
                        'SELECT AVG(home_score + away_score) FROM matches '
                        'WHERE league_id=? AND status="finished"',
                        (league_id,)
                    ).fetchone()
                    if league_row and league_row[0]:
                        league_avg_total = float(league_row[0])

        except (Exception, ValueError, KeyError, IndexError, sqlite3.Error) as e:
            logger.debug(f"估算进球期望失败(使用默认值): {e}")

        home_attack = avg_home_goals / (league_avg_total / 2)
        away_attack = avg_away_goals / (league_avg_total / 2)

        lcl = locals()
        away_defense = lcl.get('avg_away_conceded', league_avg_total/2) / (league_avg_total / 2)
        home_defense = lcl.get('avg_home_conceded', league_avg_total/2) / (league_avg_total / 2)

        lambda_home = (league_avg_total / 2 + home_advantage / 2) * home_attack * away_defense
        lambda_away = (league_avg_total / 2 - home_advantage / 2) * away_attack * home_defense

        lambda_home = max(0.3, min(6.0, lambda_home))
        lambda_away = max(0.3, min(6.0, lambda_away))

        return lambda_home, lambda_away

    @staticmethod
    def poisson_prob(k: int, lam: float) -> float:
        """Poisson概率 P(X=k) = λ^k * e^{-λ} / k!"""
        if lam <= 0:
            return 1.0 if k == 0 else 0.0
        if HAS_SCIPY:
            log_p = k * math.log(lam) - lam - gammaln(k + 1)
        else:
            # 手动计算 k! 的对数
            log_factorial = sum(math.log(i) for i in range(1, k + 1)) if k > 0 else 0
            log_p = k * math.log(lam) - lam - log_factorial
        return math.exp(log_p)

    def predict_score_distribution(self, lambda_home: float, lambda_away: float,
                                    max_goals: int = 8) -> Dict:
        """预测比分分布矩阵"""
        matrix = np.zeros((max_goals + 1, max_goals + 1))

        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                matrix[i, j] = self.poisson_prob(i, lambda_home) * self.poisson_prob(j, lambda_away)

        matrix /= matrix.sum()

        home_win_prob = 0.0
        draw_prob = 0.0
        away_win_prob = 0.0
        over_2_5 = 0.0
        btts = 0.0

        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                p = matrix[i, j]
                if i > j: home_win_prob += p
                elif i == j: draw_prob += p
                else: away_win_prob += p
                if i + j > 2.5: over_2_5 += p
                if i > 0 and j > 0: btts += p

        # Top 5 最高概率比分
        scores_list = []
        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                scores_list.append({
                    'score': f"{i}-{j}",
                    'home_goals': i,
                    'away_goals': j,
                    'probability': float(matrix[i, j]),
                    'is_home_win': i > j,
                    'is_draw': i == j,
                    'is_away_win': i < j,
                })

        scores_list.sort(key=lambda x: x['probability'], reverse=True)
        top_scores = scores_list[:5]

        # 高赔率冷门比分
        cold_gate_scores = []
        for s in scores_list:
            if s['probability'] < 0.05 and s['probability'] >= 0.003:
                goal_diff = abs(s['home_goals'] - s['away_goals'])
                if goal_diff >= 2 or (s['is_away_win'] and goal_diff >= 1):
                    cold_gate_scores.append(s)
        cold_gate_scores = cold_gate_scores[:10]

        return {
            'lambda_home': round(lambda_home, 2),
            'lambda_away': round(lambda_away, 2),
            'expected_total_goals': round(lambda_home + lambda_away, 2),
            'outcomes': {
                'home_win': round(home_win_prob * 100, 1),
                'draw': round(draw_prob * 100, 1),
                'away_win': round(away_win_prob * 100, 1),
                'over_2_5': round(over_2_5 * 100, 1),
                'btts': round(btts * 100, 1),
            },
            'top_scores': [
                {**s, 'probability': f"{s['probability']*100:.1f}%"}
                for s in top_scores
            ],
            'cold_gate_scores': [
                {**s, 'probability': f"{s['probability']*100:.1f}%"}
                for s in cold_gate_scores
            ],
            'probability_matrix': matrix.tolist(),
        }

    def _poisson_outcome_prob(self, lambda_home: float, lambda_away: float,
                               outcome: str, max_goals: int = 6) -> float:
        """快速计算 Poisson 某个赛果的概率"""
        prob = 0.0
        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                p = self.poisson_prob(i, lambda_home) * self.poisson_prob(j, lambda_away)
                if outcome == "home_win" and i > j:
                    prob += p
                elif outcome == "draw" and i == j:
                    prob += p
                elif outcome == "away_win" and i < j:
                    prob += p
        return prob

    def _quick_score_prediction(self, odds: Dict[str, float],
                                 fused_probs: Dict[str, float]) -> Optional[Dict]:
        """快速比分预测（基于赔率，不需要数据库）"""
        try:
            home_odd = odds.get("home", 2.0)
            away_odd = odds.get("away", 3.0)

            home_implied = 1 / max(home_odd, 1.01)
            away_implied = 1 / max(away_odd, 1.01)

            avg_total = 2.7
            lambda_home = (avg_total / 2 + 0.175) * (home_implied / 0.4) ** 0.5
            lambda_away = (avg_total / 2 - 0.175) * (away_implied / 0.3) ** 0.5
            lambda_home = max(0.3, min(6.0, lambda_home))
            lambda_away = max(0.3, min(6.0, lambda_away))

            score_dist = self.predict_score_distribution(lambda_home, lambda_away)

            return {
                'lambda_home': score_dist['lambda_home'],
                'lambda_away': score_dist['lambda_away'],
                'expected_total_goals': score_dist['expected_total_goals'],
                'top_scores': score_dist['top_scores'][:3],
                'outcomes': score_dist['outcomes'],
            }
        except (Exception, KeyError, IndexError):
            return None

    # ══════════════════════════════════════════════════════════════
    #  v1.0 赔率背离 + 形态背离 + 市场过度自信 + 历史冷门率
    # ══════════════════════════════════════════════════════════════

    def calculate_odds_divergence(self, match_data: Dict, model_prob: float) -> Dict:
        """赔率背离度 — 市场赔率与模型概率的差异（v1.0 完整版）"""
        odds = match_data.get('odds', {})
        home_odds = odds.get('home_odds', 2.0)
        draw_odds = odds.get('draw_odds', 3.5)
        away_odds = odds.get('away_odds', 3.5)

        margin = (1/home_odds + 1/draw_odds + 1/away_odds) - 1.0
        fair_divisor = 1 + margin

        implied_home = (1 / home_odds) / fair_divisor
        implied_draw = (1 / draw_odds) / fair_divisor
        implied_away = (1 / away_odds) / fair_divisor

        divergence_home = model_prob - implied_home
        divergence_away = (1 - model_prob - 0.25) - implied_away
        divergence_draw = 0.25 - implied_draw

        divergences = {
            'home': divergence_home,
            'draw': divergence_draw,
            'away': divergence_away,
        }

        max_dir = max(divergences, key=divergences.get)
        max_div = divergences[max_dir]

        high_odds_signal = 0.0
        if away_odds > self.HIGH_ODDS_THRESHOLD and divergence_away > self.VALUE_GAP_THRESHOLD:
            high_odds_signal = divergence_away * (away_odds / 5.0)
        if draw_odds > 4.0 and divergence_draw > 0.03:
            high_odds_signal = max(high_odds_signal, divergence_draw * (draw_odds / 4.0))
        if home_odds > self.HIGH_ODDS_THRESHOLD and divergence_home > self.VALUE_GAP_THRESHOLD:
            high_odds_signal = max(high_odds_signal, divergence_home * (home_odds / 5.0))

        any_high_odds = (
            (home_odds > self.UPSET_ODDS_THRESHOLD and divergence_home > self.VALUE_GAP_THRESHOLD) or
            (away_odds > self.UPSET_ODDS_THRESHOLD and divergence_away > self.VALUE_GAP_THRESHOLD) or
            (draw_odds > 4.0 and divergence_draw > 0.03)
        )

        return {
            'home_odds': home_odds,
            'draw_odds': draw_odds,
            'away_odds': away_odds,
            'margin': round(margin * 100, 1),
            'implied': {
                'home': round(implied_home * 100, 1),
                'draw': round(implied_draw * 100, 1),
                'away': round(implied_away * 100, 1),
            },
            'model_prob': round(model_prob * 100, 1),
            'divergence': {
                'home': round(divergence_home * 100, 1),
                'draw': round(divergence_draw * 100, 1),
                'away': round(divergence_away * 100, 1),
            },
            'max_direction': max_dir,
            'max_divergence': round(max_div * 100, 1),
            'high_odds_signal': round(high_odds_signal * 100, 1),
            'is_upset_signal': any_high_odds,
            'upset_directions': [d for d in ['home', 'away', 'draw']
                                if divergences[d] > self.VALUE_GAP_THRESHOLD],
        }

    def calculate_form_divergence(self, match_data: Dict) -> Dict:
        """形态背离 — 弱队上升 vs 强队下滑"""
        home_id = match_data.get('home_team_id')
        away_id = match_data.get('away_team_id')

        try:
            with self.db.get_connection() as conn:
                form_data = {'home': {'form': [], 'trend': 0}, 'away': {'form': [], 'trend': 0}}

                for side, team_id in [('home', home_id), ('away', away_id)]:
                    if not team_id:
                        continue

                    rows = conn.execute(
                        '''SELECT home_score, away_score, home_team_id, away_team_id FROM matches
                           WHERE (home_team_id=? OR away_team_id=?) AND home_score IS NOT NULL
                           ORDER BY match_date DESC LIMIT 5''',
                        (team_id, team_id)
                    ).fetchall()

                    if not rows:
                        continue

                    scores = []
                    for r in rows:
                        h_score, a_score, h_id, a_id = r[0], r[1], r[2], r[3]
                        if team_id == h_id:
                            scores.append(3 if h_score > a_score else (1 if h_score == a_score else 0))
                            if side == 'home':
                                form_data[side].setdefault('goals_scored', []).append(h_score)
                                form_data[side].setdefault('goals_conceded', []).append(a_score)
                        else:
                            scores.append(3 if a_score > h_score else (1 if a_score == h_score else 0))
                            if side == 'away':
                                form_data[side].setdefault('goals_scored', []).append(a_score)
                                form_data[side].setdefault('goals_conceded', []).append(h_score)

                    form_data[side]['form'] = scores

                    if len(scores) >= 4:
                        recent = sum(scores[:2])
                        early = sum(scores[2:4])
                        form_data[side]['trend'] = (recent - early) / 6.0

                home_trend = form_data['home']['trend']
                away_trend = form_data['away']['trend']
                divergence = away_trend - home_trend

                if divergence > 0.5: level = '强冷门信号'
                elif divergence > 0.15: level = '中等冷门信号'
                elif divergence > 0.02: level = '微弱信号'
                else: level = '无信号'

                return {
                    'home_form': form_data['home']['form'],
                    'away_form': form_data['away']['form'],
                    'home_trend': round(home_trend, 3),
                    'away_trend': round(away_trend, 3),
                    'divergence': round(divergence, 3),
                    'level': level,
                    'is_upset_signal': divergence > 0.05,
                }

        except (Exception, KeyError, IndexError) as e:
            logger.debug(f"形态背离计算失败: {e}")
            return {'home_form': [], 'away_form': [], 'home_trend': 0, 'away_trend': 0,
                    'divergence': 0, 'level': '数据不足', 'is_upset_signal': False}

    def calculate_market_overconfidence(self, odds: Dict) -> Dict:
        """市场过度自信 — 赔率过度倾斜强队"""
        home_odds = odds.get('home_odds', 2.0)
        away_odds = odds.get('away_odds', 3.5)
        draw_odds = odds.get('draw_odds', 3.5)

        min_odds = min(home_odds, away_odds)
        max_odds = max(home_odds, away_odds)

        overconfidence_score = 0.0
        signals = []

        if min_odds < 1.4:
            overconfidence_score += (1.4 - min_odds) * 2.0
            signals.append(f'强队赔率过低({min_odds:.2f})')

        if max_odds / max(min_odds, 1.01) > 3.0:
            overconfidence_score += 0.3
            signals.append(f'赔率极度倾斜(比率{max_odds/min_odds:.1f}:1)')

        if draw_odds > 4.0:
            overconfidence_score += 0.2
            signals.append(f'平局赔率偏高({draw_odds:.1f})')

        margin = (1/home_odds + 1/draw_odds + 1/away_odds) - 1.0
        if margin < 0.03:
            overconfidence_score += 0.1
            signals.append(f'庄家利润率极低({margin*100:.1f}%)→比赛不确定')

        level = '高' if overconfidence_score > 0.5 else '中' if overconfidence_score > 0.2 else '低'

        return {
            'score': round(overconfidence_score, 3),
            'level': level,
            'signals': signals,
            'is_upset_signal': overconfidence_score > 0.3,
            'favorite_odds': min_odds,
            'underdog_odds': max_odds,
            'odds_ratio': round(max_odds / max(min_odds, 1.01), 1),
        }

    def calculate_historical_upset_rate(self, match_data: Dict) -> Dict:
        """历史冷门率 — 该联赛/对阵的冷门频率"""
        league_id = match_data.get('league_id')
        home_name = match_data.get('home_team_name', '')
        away_name = match_data.get('away_team_name', '')

        try:
            with self.db.get_connection() as conn:
                league_upsets = conn.execute(
                    '''SELECT COUNT(*) FROM matches m
                       JOIN odds o ON m.match_id = o.match_id
                       WHERE m.league_id=? AND m.home_score IS NOT NULL
                       AND ((o.away_odds > 3.0 AND m.away_score > m.home_score)
                         OR (o.home_odds > 3.0 AND m.home_score > m.away_score))''',
                    (league_id,)
                ).fetchone()

                league_total = conn.execute(
                    '''SELECT COUNT(*) FROM matches m
                       JOIN odds o ON m.match_id = o.match_id
                       WHERE m.league_id=? AND m.home_score IS NOT NULL''',
                    (league_id,)
                ).fetchone()

                upset_rate = 0.0
                if league_total and league_total[0] > 0:
                    upset_rate = league_upsets[0] / league_total[0]

                h2h_upset = False
                if home_name and away_name:
                    h2h_row = conn.execute(
                        '''SELECT COUNT(*) FROM matches m
                           JOIN odds o ON m.match_id = o.match_id
                           WHERE ((m.home_team_name=? AND m.away_team_name=?)
                               OR (m.home_team_name=? AND m.away_team_name=?))
                           AND m.home_score IS NOT NULL
                           AND ((o.away_odds > 3.0 AND m.away_score > m.home_score)
                             OR (o.home_odds > 3.0 AND m.home_score > m.away_score))''',
                        (home_name, away_name, away_name, home_name)
                    ).fetchone()
                    if h2h_row and h2h_row[0] > 0:
                        h2h_upset = True

                level = '高' if upset_rate > 0.25 else '中' if upset_rate > 0.15 else '低'

                return {
                    'league_upset_rate': round(upset_rate * 100, 1),
                    'league_upsets': league_upsets[0] if league_upsets else 0,
                    'league_total': league_total[0] if league_total else 0,
                    'h2h_upset_history': h2h_upset,
                    'level': level,
                    'is_upset_signal': upset_rate > 0.20 or h2h_upset,
                }

        except (Exception, KeyError, IndexError) as e:
            logger.debug(f"历史冷门率计算失败: {e}")
            return {'league_upset_rate': 0, 'league_upsets': 0, 'league_total': 0,
                    'h2h_upset_history': False, 'level': '数据不足', 'is_upset_signal': False}

    def calculate_comeback_tendency(self, match_data: Dict, form_data: Dict = None) -> Dict:
        """反超/翻盘倾向"""
        home_name = match_data.get('home_team_name', '')
        away_name = match_data.get('away_team_name', '')
        home_id = match_data.get('home_team_id')
        away_id = match_data.get('away_team_id')
        odds = match_data.get('odds', {})

        try:
            with self.db.get_connection() as conn:
                results = {'comeback_signals': [], 'score': 0.0}

                home_odds = odds.get('home_odds', 2.0)
                away_odds = odds.get('away_odds', 3.5)

                if away_odds > home_odds:
                    underdog_id, underdog_side = away_id, 'away'
                    favorite_odds, underdog_odds = home_odds, away_odds
                else:
                    underdog_id, underdog_side = home_id, 'home'
                    favorite_odds, underdog_odds = away_odds, home_odds

                if underdog_id:
                    upsets = conn.execute(
                        '''SELECT COUNT(*) FROM matches m
                           JOIN odds o ON m.match_id = o.match_id
                           WHERE m.home_score IS NOT NULL
                           AND ((m.home_team_id=? AND o.home_odds > 3.0 AND m.home_score > m.away_score)
                             OR (m.away_team_id=? AND o.away_odds > 3.0 AND m.away_score > m.home_score))
                           ORDER BY m.match_date DESC LIMIT 20''',
                        (underdog_id, underdog_id)
                    ).fetchone()

                    total_recent = conn.execute(
                        '''SELECT COUNT(*) FROM matches
                           WHERE (home_team_id=? OR away_team_id=?) AND home_score IS NOT NULL
                           ORDER BY match_date DESC LIMIT 20''',
                        (underdog_id, underdog_id)
                    ).fetchone()

                    if total_recent and total_recent[0] > 0 and upsets:
                        rate = upsets[0] / total_recent[0]
                        if rate >= 0.15:
                            results['comeback_signals'].append(
                                f'弱队近{total_recent[0]}场爆冷{upsets[0]}次(率{rate*100:.0f}%)'
                            )
                            results['score'] += min(0.6, rate * 3)

                odds_ratio = underdog_odds / max(favorite_odds, 1.01)
                if odds_ratio > 3.5:
                    results['score'] += 0.2
                    results['comeback_signals'].append(f'强弱势比{odds_ratio:.1f}:1→高赔率爆冷潜力')
                elif odds_ratio > 2.5:
                    results['score'] += 0.1
                    results['comeback_signals'].append(f'强弱差距明显(赔率比{odds_ratio:.1f}:1)')

                if form_data:
                    underdog_trend = form_data.get('away_trend' if underdog_side == 'away' else 'home_trend', 0)
                    if underdog_trend > 0:
                        results['score'] += 0.15
                        results['comeback_signals'].append(f'弱队形态上升(趋势{underdog_trend:+.2f})')

                results['score'] = min(1.0, round(results['score'], 3))

                if results['score'] > 0.25:
                    results['level'] = '强反超倾向'
                    results['is_upset_signal'] = True
                elif results['score'] > 0.1:
                    results['level'] = '有反超潜力'
                    results['is_upset_signal'] = True
                else:
                    results['level'] = '无反超信号'
                    results['is_upset_signal'] = False

                return results

        except (Exception, KeyError, IndexError) as e:
            logger.debug(f"反超倾向计算失败: {e}")
            return {'comeback_signals': [], 'score': 0, 'level': '数据不足', 'is_upset_signal': False}

    def calculate_big_match_upset(self, match_data: Dict, score_dist: Dict) -> Dict:
        """大赛冷门模式"""
        league_name = match_data.get('league_name', '')
        match_name = f"{match_data.get('home_team_name','')} vs {match_data.get('away_team_name','')}"
        odds = match_data.get('odds', {})

        big_match_keywords = [
            'final', 'semi', 'quarter', 'cup', 'championship', 'champions',
            '决赛', '半决赛', '四强', '八强', '冠军', '杯', '淘汰赛',
            'playoff', 'knockout', 'clasico', 'derby',
            'world cup', 'euro', 'champions league', 'uefa',
        ]

        is_big_match = False
        matched_keywords = []
        for kw in big_match_keywords:
            if kw.lower() in league_name.lower() or kw.lower() in match_name.lower():
                is_big_match = True
                matched_keywords.append(kw)

        if not is_big_match:
            return {'is_big_match': False, 'score': 0, 'level': '非大赛', 'signals': [], 'is_upset_signal': False}

        bonus = 0.0
        signals = [f'大赛标签: {",".join(matched_keywords[:3])}']

        pressure_level = 0
        if any(k in league_name.lower() for k in ['final', '决赛', '冠军']):
            pressure_level = 4
            bonus += 0.25
            signals.append('决赛级压力(↑强队崩盘风险)')
        elif any(k in league_name.lower() for k in ['semi', '半决赛', '四强']):
            pressure_level = 3
            bonus += 0.15
            signals.append('半决赛级压力')
        elif any(k in league_name.lower() for k in ['quarter', '八强']):
            pressure_level = 2
            bonus += 0.10
            signals.append('八强淘汰赛')
        else:
            bonus += 0.05

        # Poisson λ 分析
        home_odds = odds.get('home_odds', 2.0)
        away_odds = odds.get('away_odds', 3.5)
        lambda_home = score_dist.get('lambda_home', 1.0)
        lambda_away = score_dist.get('lambda_away', 1.0)

        underdog_lambda = lambda_away if away_odds > home_odds else lambda_home
        if underdog_lambda >= 0.8:
            bonus += 0.15
            signals.append(f'弱队有进球能力(λ={underdog_lambda:.1f})')
        elif underdog_lambda >= 0.5:
            bonus += 0.08
            signals.append(f'弱队偶有进球(λ={underdog_lambda:.1f})')

        total_goals = lambda_home + lambda_away
        if 2.3 <= total_goals <= 3.5:
            bonus += 0.10
            signals.append(f'上半场节制→下半场爆发模式(总进球{total_goals:.1f})')
        elif total_goals > 3.5:
            bonus += 0.15
            signals.append(f'超级大球模式(总进球{total_goals:.1f})')

        # 大赛冷门历史
        if match_data.get('league_id') and self.db:
            try:
                with self.db.get_connection() as conn:
                    league_id = match_data['league_id']
                    big_upsets = conn.execute(
                        '''SELECT COUNT(*) FROM matches m
                           JOIN odds o ON m.match_id = o.match_id
                           WHERE m.league_id=? AND m.home_score IS NOT NULL
                           AND ABS(m.home_score - m.away_score) >= 3
                           AND ((o.home_odds > 3.0) OR (o.away_odds > 3.0))''',
                        (league_id,)
                    ).fetchone()
                    total_finished = conn.execute(
                        '''SELECT COUNT(*) FROM matches
                           WHERE league_id=? AND home_score IS NOT NULL''',
                        (league_id,)
                    ).fetchone()
                    if total_finished and total_finished[0] > 0:
                        rate = big_upsets[0] / total_finished[0]
                        if rate > 0.15:
                            bonus += 0.15
                            signals.append(f'该联赛历史大比分冷门率{rate*100:.0f}%')
                        elif rate > 0.05:
                            bonus += 0.08
                            signals.append(f'该联赛有冷门传统({rate*100:.0f}%)')
            except (Exception, KeyError, IndexError):
                pass

        bonus = min(1.0, round(bonus, 3))

        return {
            'is_big_match': True,
            'score': bonus,
            'level': '强' if bonus > 0.4 else '中' if bonus > 0.2 else '低',
            'signals': signals,
            'pressure_level': pressure_level,
            'is_upset_signal': bonus > 0.15,
        }

    def calculate_goal_rush_bonus(self, score_dist: Dict, odds: Dict, form_div: Dict) -> Dict:
        """大球冷门加分"""
        try:
            bonus = 0.0
            signals = []

            home_lambda = score_dist.get('lambda_home', 1.0)
            away_lambda = score_dist.get('lambda_away', 1.0)
            total_goals = home_lambda + away_lambda

            if total_goals > self.GOAL_RUSH_TOTAL:
                bonus += 0.3
                signals.append(f'大球场景(预期{total_goals:.1f}球)')

            if total_goals > 3.0:
                bonus += 0.2
                signals.append(f'超级大球(预期{total_goals:.1f}球)')

            cold_scores = score_dist.get('cold_gate_scores', [])
            high_goal_upsets = [s for s in cold_scores
                              if abs(s['home_goals'] - s['away_goals']) >= self.GOAL_RUSH_DIFF
                              and s['home_goals'] + s['away_goals'] >= 3]

            if high_goal_upsets:
                best = high_goal_upsets[0]
                bonus += 0.2
                signals.append(f'高赔率大比分{best["score"]}(p={best["probability"]})')

            home_odds = odds.get('home_odds', 2.0)
            away_odds = odds.get('away_odds', 3.5)
            if away_odds > 3.0 or home_odds > 3.0:
                bonus += 0.15
                signals.append('赔率已预示冷门可能')

            if form_div.get('divergence', 0) > 0:
                bonus += 0.1
                signals.append('弱队形态支持进球')

            bonus = min(1.0, round(bonus, 3))

            return {
                'score': bonus,
                'total_goals_expected': round(total_goals, 2),
                'signals': signals,
                'is_upset_signal': bonus > 0.3,
                'level': '强' if bonus > 0.5 else '中' if bonus > 0.25 else '低'
            }

        except (Exception) as e:
            logger.debug(f"大球加分计算失败: {e}")
            return {'score': 0, 'total_goals_expected': 2.0, 'signals': [], 'is_upset_signal': False, 'level': '无'}

    # ══════════════════════════════════════════════════════════════
    #  v1.0 冷门评分合成 + 高赔率推荐 + Kelly
    # ══════════════════════════════════════════════════════════════

    def synthesize_upset_score(self, signals: Dict) -> Dict:
        """8维信号融合 → UpsetScore (0-100)"""
        w = self.WEIGHTS_V1

        odds_sig = min(1.0, abs(signals['odds_divergence'].get('high_odds_signal', 0)) / 0.3)
        poisson_sig = min(1.0, signals['poisson_upset'].get('upset_probability', 0) / 0.5)
        form_sig = max(0.0, min(1.0, signals['form_divergence'].get('divergence', 0) + 0.5))
        market_sig = min(1.0, signals['market_overconfidence'].get('score', 0) / 1.0)
        hist_sig = min(1.0, signals['historical_upset'].get('league_upset_rate', 0) / 40)
        comeback_sig = signals.get('comeback_tendency', {}).get('score', 0)
        goal_rush_sig = signals.get('goal_rush_bonus', {}).get('score', 0)
        big_match_sig = signals.get('big_match_upset', {}).get('score', 0)

        upset_score = (
            w['odds_divergence'] * odds_sig +
            w['poisson_upset'] * poisson_sig +
            w['form_divergence'] * form_sig +
            w['market_overconfidence'] * market_sig +
            w['historical_upset_rate'] * hist_sig +
            w['comeback_tendency'] * comeback_sig +
            w['goal_rush_bonus'] * goal_rush_sig +
            w['big_match_upset'] * big_match_sig
        ) * 100

        if upset_score >= 60:
            upset_level = '🔥 强烈冷门信号'
            action = '重点分析，大球/反超/大赛冷门 + 高赔率比分'
        elif upset_score >= 40:
            upset_level = '⚠️ 中等冷门信号'
            action = '关注高赔率方向，小注试水'
        elif upset_score >= 20:
            upset_level = '👀 微弱冷门信号'
            action = '观察但不急于投注'
        else:
            upset_level = '✅ 正常比赛'
            action = '按常规策略处理'

        return {
            'upset_score': round(upset_score, 1),
            'upset_level': upset_level,
            'action': action,
            'component_scores': {
                'odds_divergence': round(odds_sig * 100, 1),
                'poisson_upset': round(poisson_sig * 100, 1),
                'form_divergence': round(form_sig * 100, 1),
                'market_overconfidence': round(market_sig * 100, 1),
                'historical_upset': round(hist_sig * 100, 1),
                'comeback_tendency': round(comeback_sig * 100, 1),
                'goal_rush_bonus': round(goal_rush_sig * 100, 1),
                'big_match_upset': round(big_match_sig * 100, 1),
            },
        }

    def recommend_high_odds_bets(self, score_dist: Dict, odds: Dict,
                                  upset_score: float) -> List[Dict]:
        """高赔率比分推荐"""
        recommendations = []
        cold_scores = score_dist.get('cold_gate_scores', [])
        if not cold_scores:
            return recommendations

        away_odds = odds.get('away_odds', 3.5)
        home_odds = odds.get('home_odds', 2.0)
        draw_odds = odds.get('draw_odds', 3.5)

        for cs in cold_scores:
            prob_str = cs.get('probability', '0%')
            if isinstance(prob_str, str):
                prob = float(prob_str.replace('%', ''))
            else:
                prob = float(prob_str) * 100

            score = cs['score']
            goal_diff = abs(cs['home_goals'] - cs['away_goals'])

            if goal_diff == 0:
                est_odds = draw_odds * 3
            elif goal_diff == 1:
                est_odds = min(away_odds, home_odds) * 2
            elif goal_diff == 2:
                est_odds = max(away_odds, home_odds) * 4
            else:
                est_odds = max(away_odds, home_odds) * 8

            ev = (prob / 100) * est_odds - 1
            upset_bonus = upset_score / 200
            adjusted_ev = ev + upset_bonus

            if adjusted_ev > self.EV_THRESHOLD:
                recommendations.append({
                    'score': score,
                    'estimated_odds': round(est_odds, 1),
                    'model_prob': f"{prob:.1f}%",
                    'expected_value': round(adjusted_ev * 100, 1),
                    'confidence': '高' if adjusted_ev > 0.15 else '中',
                    'rationale': self._explain_cold_gate(cs, odds, est_odds),
                })

        recommendations.sort(key=lambda x: x['expected_value'], reverse=True)
        return recommendations[:5]

    @staticmethod
    def _explain_cold_gate(score_data: Dict, odds: Dict, est_odds: float) -> str:
        """解释冷门逻辑"""
        home_g = score_data['home_goals']
        away_g = score_data['away_goals']
        parts = []

        if away_g > home_g: parts.append("客胜冷门")
        if abs(home_g - away_g) >= 2: parts.append(f"净胜{abs(home_g-away_g)}球大胜")
        if home_g + away_g >= 4: parts.append("大球高比分")
        if est_odds > 20: parts.append(f"超高赔率~{est_odds:.0f}sp")

        return ' + '.join(parts) if parts else '冷门候选'

    @staticmethod
    def kelly_criterion(win_prob: float, odds: float, fraction: float = 0.5) -> Dict:
        """凯利准则 — 最优投注比例"""
        p = win_prob
        b = odds - 1
        q = 1 - p

        f_star = (p * b - q) / max(b, 0.01)
        f_star = max(0, f_star) * fraction

        ev = p * b - q
        ruin_risk = q ** 10

        return {
            'win_prob': round(p * 100, 1),
            'odds': odds,
            'kelly_fraction': round(f_star * 100, 2),
            'kelly_fraction_desc': f'建议投注总资金的 {f_star*100:.1f}%',
            'expected_value': round(ev * 100, 1),
            'is_value_bet': ev > 0.02,
            'ruin_risk_10_streak': f'{ruin_risk*100:.2f}%',
        }

    # ══════════════════════════════════════════════════════════════
    #  v2.0 评分与分类（保持原样 + v3.0 8维权重版本）
    # ══════════════════════════════════════════════════════════════

    def _compute_upset_score(self, signals: List[UpsetSignal]) -> float:
        """v2.0 4维评分"""
        type_weights = {
            "odds_deviation": 0.35,
            "strength_gap": 0.30,
            "market_sentiment": 0.20,
            "historical": 0.15,
        }
        total = 0.0
        total_weight = 0.0
        for s in signals:
            w = type_weights.get(s.signal_type, 0.1)
            total += s.strength * w
            total_weight += w

        base_score = total / max(total_weight, 0.01)
        multiplier = min(1.0 + len(signals) * 0.1, 1.5)
        return min(base_score * multiplier, 1.0)

    def _compute_upset_score_v3(self, signals: List[UpsetSignal]) -> float:
        """v3.0 8维评分"""
        type_weights = {
            "odds_deviation": 0.20,
            "strength_gap": 0.12,
            "market_sentiment": 0.12,
            "historical": 0.10,
            "poisson": 0.18,
            "comeback": 0.10,
            "goal_rush": 0.10,
            "big_match": 0.08,
        }
        total = 0.0
        total_weight = 0.0
        for s in signals:
            w = type_weights.get(s.signal_type, 0.05)
            total += s.strength * w
            total_weight += w

        base_score = total / max(total_weight, 0.01)
        multiplier = min(1.0 + len(signals) * 0.08, 1.6)
        return min(base_score * multiplier, 1.0)

    def _classify_upset(self, score: float) -> tuple:
        """分类冷门等级"""
        if score >= 0.7:
            return score, "🔥强冷门"
        elif score >= 0.4:
            return score, "⚡中等冷门"
        elif score >= 0.15:
            return score, "💡轻微偏差"
        else:
            return score, "正常"

    def _determine_upset_direction(self, signals: List[UpsetSignal],
                                    fused_probs: Dict[str, float],
                                    odds: Dict[str, float]) -> str:
        """确定冷门方向"""
        direction_scores = defaultdict(float)
        for s in signals:
            direction_scores[s.direction] += s.strength
        return max(direction_scores, key=direction_scores.get)

    def _apply_upset_weighting(self, fused_probs: Dict[str, float],
                                signals: List[UpsetSignal],
                                upset_score: float,
                                upset_direction: str) -> Dict[str, float]:
        """应用冷门加权调整概率"""
        w_upset = self.UPSET_FUSION_WEIGHT * upset_score
        w_fused = 1 - w_upset

        upset_probs = {"home_win": 0.25, "draw": 0.25, "away_win": 0.25}
        upset_probs[upset_direction] = 0.5

        weighted = {}
        for outcome in ("home_win", "draw", "away_win"):
            weighted[outcome] = (
                w_fused * fused_probs.get(outcome, 1/3) +  # ★ C4：统一回退为1/3
                w_upset * upset_probs[outcome]
            )

        total = sum(weighted.values())
        if total > 0:
            weighted = {k: v / total for k, v in weighted.items()}

        return weighted

    def _generate_recommendation(self, upset_level: str, upset_direction: str,
                                  signals: List[UpsetSignal],
                                  weighted_probs: Dict[str, float],
                                  confidence_level: str) -> str:
        """生成冷门建议"""
        label_map = {"home_win": "主胜", "draw": "平局", "away_win": "客胜"}
        direction_label = label_map.get(upset_direction, upset_direction)

        if upset_level == "🔥强冷门":
            rec = (f"⚠️ 强烈冷门预警！{direction_label}方向冷门概率极高，"
                   f"建议关注{direction_label}。若赔率>3.0，具备高价值投注空间")
        elif upset_level == "⚡中等冷门":
            rec = (f"⚡ 中等冷门信号：{direction_label}方向存在偏差，"
                   f"建议结合基本面审慎考虑{direction_label}方向")
        elif upset_level == "💡轻微偏差":
            best_prob_outcome = max(weighted_probs, key=weighted_probs.get)
            rec = (f"💡 轻微偏差：{direction_label}方向有微幅价值，"
                   f"综合概率仍倾向{label_map.get(best_prob_outcome, '未知')}")
        else:
            rec = "✅ 无明显冷门信号，模型预测方向可信度较高"

        if confidence_level == "低":
            rec += " ⚠️ 但模型整体置信度较低，建议谨慎"

        return rec

    # ══════════════════════════════════════════════════════════════
    #  v2.0 辅助方法（保持原样）
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _get_diff_level(abs_diff: float) -> str:
        if abs_diff >= EnhancedUpsetDetector.STRONG_THRESHOLD:
            return "🔥强冷门"
        elif abs_diff >= EnhancedUpsetDetector.MEDIUM_THRESHOLD:
            return "⚡中等冷门"
        else:
            return "💡轻微偏差"

    @staticmethod
    def _get_diff_level_name(abs_diff: float) -> str:
        return EnhancedUpsetDetector._get_diff_level(abs_diff)

    @staticmethod
    def _get_diff_level_by_strength(edge: float) -> str:
        if edge >= 15:
            return "🔥强冷门"
        elif edge >= 8:
            return "⚡中等冷门"
        else:
            return "💡轻微偏差"

    @staticmethod
    def _translate_direction(direction: str) -> str:
        return {"home_win": "主胜", "draw": "平局", "away_win": "客胜"}.get(
            direction, direction
        )

    @staticmethod
    def _get_prob_for_direction(signal: UpsetSignal, probs: Dict[str, float]) -> float:
        return probs.get(signal.direction, 1/3)  # ★ C4：统一回退为1/3

    @staticmethod
    def _get_implied_for_direction(signal: UpsetSignal, odds: Dict[str, float]) -> float:
        odd_map = {"home_win": "home", "draw": "draw", "away_win": "away"}
        key = odd_map.get(signal.direction, "home")
        odd = odds.get(key, 3.0)
        return 1.0 / odd if odd > 0 else 1/3  # ★ C4：统一回退为1/3

    @staticmethod
    def _get_odd_for_direction(signal: UpsetSignal, odds: Dict[str, float]) -> float:
        odd_map = {"home_win": "home", "draw": "draw", "away_win": "away"}
        return odds.get(odd_map.get(signal.direction, "home"), 0)

# ══════════════════════════════════════════════════════════════
#  兼容接口
# ══════════════════════════════════════════════════════════════

def detect_upset_enhanced(match_info: Dict, fused_probs: Dict[str, float],
                           odds: Dict[str, float], home_profile=None,
                           away_profile=None, league: str = "PL",
                           confidence_level: str = "中",
                           models_agree: bool = True,
                           db_manager=None) -> Optional[Dict]:
    """兼容接口：增强冷门检测"""
    detector = EnhancedUpsetDetector(db_manager=db_manager)
    return detector.detect(
        match_info, fused_probs, odds,
        home_profile=home_profile, away_profile=away_profile,
        league=league, confidence_level=confidence_level,
        models_agree=models_agree,
    )

# ══════════════════════════════════════════════════════════════
#  测试
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("🧪 增强冷门模型 v3.0 测试")
    print("=" * 60)

    detector = EnhancedUpsetDetector()

    # 测试1: 主队赔率极低（典型冷门场景）
    result1 = detector.detect(
        match_info={"home": "曼联", "away": "伯恩茅斯"},
        fused_probs={"home_win": 0.55, "draw": 0.25, "away_win": 0.20},
        odds={"home": 1.40, "draw": 4.50, "away": 7.50},
        league="PL", confidence_level="中",
    )
    print(f"\n📊 测试1 (主队赔率极低):")
    print(f"   冷门等级: {result1['upset_level']}")
    print(f"   冷门方向: {result1['upset_direction']}")
    print(f"   冷门评分: {result1['overall_score']}")
    print(f"   信号数: {len(result1['signals'])}")
    print(f"   加权概率: 主{result1['weighted_probs']['home_win']}%/"
          f"平{result1['weighted_probs']['draw']}%/客{result1['weighted_probs']['away_win']}%")
    if 'score_prediction' in result1:
        sp = result1['score_prediction']
        print(f"   Poisson: λ主={sp['lambda_home']} λ客={sp['lambda_away']} "
              f"最可能比分={sp['top_scores'][0]['score']}")
    print(f"   建议: {result1['recommendation']}")

    # 测试2: 实力接近（正常场景）
    result2 = detector.detect(
        match_info={"home": "利物浦", "away": "阿森纳"},
        fused_probs={"home_win": 0.40, "draw": 0.30, "away_win": 0.30},
        odds={"home": 2.20, "draw": 3.30, "away": 3.00},
        league="PL", confidence_level="中",
    )
    print(f"\n📊 测试2 (实力接近):")
    if result2:
        print(f"   冷门等级: {result2['upset_level']}")
        print(f"   冷门评分: {result2['overall_score']}")
        print(f"   信号数: {len(result2['signals'])}")
    else:
        print("   未检测到冷门信号（概率与赔率一致）")

    # 测试3: 大赛冷门模式
    result3 = detector.detect(
        match_info={"home": "拜仁", "away": "巴黎"},
        fused_probs={"home_win": 0.50, "draw": 0.25, "away_win": 0.25},
        odds={"home": 1.80, "draw": 3.80, "away": 4.50},
        league="Champions League Final", confidence_level="中",
    )
    print(f"\n📊 测试3 (大赛冷门):")
    if result3:
        print(f"   冷门等级: {result3['upset_level']}")
        print(f"   冷门评分: {result3['overall_score']}")
        print(f"   信号数: {len(result3['signals'])}")
        for s in result3['signals'][:3]:
            print(f"     └ {s['reason']}")

    # 测试4: Kelly 公式
    print(f"\n📊 测试4 (Kelly公式):")
    kelly = EnhancedUpsetDetector.kelly_criterion(0.35, 7.5, fraction=0.5)
    print(f"   {kelly}")

    print(f"\n✅ 增强冷门模型 v3.0 测试完成!")

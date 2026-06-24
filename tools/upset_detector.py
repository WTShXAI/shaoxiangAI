"""
哨响AI - 冷门检测与高赔率预测引擎 v1.0
=====================================
基于实战经验 + 数学模型的冷门/爆冷/高赔率检测系统

核心理论：
  1. Poisson比分预测 → 精确到具体比分，这是高赔率的数学基础
  2. 市场无效性检测 → 找到庄家定价错误，是26sp/56sp的来源
  3. 冷门信号融合 → 多维度信号合成 UpsetScore
  4. Kelly投注策略 → 资金管理和价值判断

使用方法:
    python upset_detector.py                      # 分析所有待预测比赛
    python upset_detector.py --match-id 123456    # 分析单场比赛
    python upset_detector.py --league PL          # 按联赛分析
    python upset_detector.py --backtest           # 回测历史冷门检测准确率
"""

import os
import sys
import json
import math
import logging
import argparse
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
from scipy import stats, optimize
from scipy.special import gammaln
import sympy as sp

from database.db_manager import DatabaseManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)


# ==================== 冷门信号因子定义 ====================

class UpsetDetector:
    """
    冷门检测器
    
    检测维度：
    1. 赔率分歧 —— 市场隐含概率 vs 模型概率的差值
    2. Poisson比分 —— 精确比分概率，识别大比分冷门
    3. 形态背离 —— 弱队上升 vs 强队下滑
    4. 市场过度自信 —— 赔率过度倾斜强队
    5. 历史冷门率 —— 该联赛/球队的冷门倾向
    6. 盘口陷阱 —— 让球盘与实力不匹配
    """

    def __init__(self):
        self.db = DatabaseManager()
        
        # 冷门阈值
        self.UPSET_ODDS_THRESHOLD = 3.0     # 赔率>3.0视为冷门候选
        self.HIGH_ODDS_THRESHOLD = 5.0      # 赔率>5.0视为高赔率
        self.VALUE_GAP_THRESHOLD = 0.05     # 价值缺口>5%值得关注
        self.EV_THRESHOLD = 0.05            # 期望价值>5%可投注
        
        # 信号权重（可调）
        self.weights = {
            'odds_divergence': 0.25,      # 赔率背离
            'poisson_upset': 0.20,        # Poisson冷门信号
            'form_divergence': 0.15,      # 形态背离
            'market_overconfidence': 0.12, # 市场过度自信
            'historical_upset_rate': 0.10, # 历史冷门率
            'comeback_tendency': 0.10,    # 反超/翻盘倾向
            'goal_rush_bonus': 0.08,      # 大球冷门额外加分
            'big_match_upset': 0.08,      # 大赛冷门加成（8强/半决赛/决赛）
        }
        
        # 大球冷门参数
        self.GOAL_RUSH_TOTAL = 2.5        # 预期总进球 > 2.5 触发大球加分
        self.GOAL_RUSH_DIFF = 2           # 净胜球 ≥ 2 的高赔率比分

    # ==================== Poisson 比分预测模型 ====================

    def estimate_expected_goals(self, match_data: Dict) -> Tuple[float, float]:
        """
        估算主客队期望进球 λ_home, λ_away
        
        基于多种因素：近期场均进球、对手防守质量、主场效应、联赛场均进球
        """
        home_id = match_data.get('home_team_id')
        away_id = match_data.get('away_team_id')
        league_id = match_data.get('league_id')
        
        # 默认值：联赛场均进球（五大联赛 ~2.7总进球）
        league_avg_total = 2.70
        home_advantage = 0.35  # 主场优势约 +0.35 球
        
        avg_home_goals = (league_avg_total / 2) + home_advantage / 2  # ≈1.525
        avg_away_goals = (league_avg_total / 2) - home_advantage / 2  # ≈1.175
        
        try:
            with self.db.get_connection() as conn:
                # 获取主队近期进球数据
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
                
                # 获取客队近期进球数据
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
                
                # 获取联赛平均进球（用于归一化）
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
        
        # 计算 λ_home 和 λ_away
        home_attack = avg_home_goals / (league_avg_total / 2)
        away_attack = avg_away_goals / (league_avg_total / 2)
        
        # 使用默认防守系数（若未获取到）
        lcl = locals()
        away_defense = lcl.get('avg_away_conceded', league_avg_total/2) / (league_avg_total / 2)
        home_defense = lcl.get('avg_home_conceded', league_avg_total/2) / (league_avg_total / 2)
        
        # λ = 基准进球率 × 攻击系数 × 对手防守系数
        lambda_home = (league_avg_total / 2 + home_advantage / 2) * home_attack * away_defense
        lambda_away = (league_avg_total / 2 - home_advantage / 2) * away_attack * home_defense
        
        # 限幅：每场进球不超过6（泊松截断）
        lambda_home = max(0.3, min(6.0, lambda_home))
        lambda_away = max(0.3, min(6.0, lambda_away))
        
        return lambda_home, lambda_away

    def poisson_prob(self, k: int, lam: float) -> float:
        """Poisson概率 P(X=k) = λ^k * e^{-λ} / k!"""
        if lam <= 0:
            return 1.0 if k == 0 else 0.0
        # 用 log 避免溢出
        log_p = k * math.log(lam) - lam - gammaln(k + 1)
        return math.exp(log_p)

    def predict_score_distribution(self, lambda_home: float, lambda_away: float,
                                    max_goals: int = 8) -> Dict:
        """
        预测比分分布矩阵
        
        Returns:
            matrix: 9x9 比分概率矩阵
            outcomes: {home_win, draw, away_win, over25, btts, ...}
            top_scores: 最高概率的5个比分
            cold_gate_scores: 高赔率比分推荐
        """
        matrix = np.zeros((max_goals + 1, max_goals + 1))
        
        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                matrix[i, j] = self.poisson_prob(i, lambda_home) * self.poisson_prob(j, lambda_away)
        
        # 归一化（截断分布）
        matrix /= matrix.sum()
        
        # 汇总结果概率
        home_win_prob = 0.0
        draw_prob = 0.0
        away_win_prob = 0.0
        over_2_5 = 0.0
        btts = 0.0  # Both Teams To Score
        under_0_5 = 0.0
        
        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                p = matrix[i, j]
                if i > j:
                    home_win_prob += p
                elif i == j:
                    draw_prob += p
                else:
                    away_win_prob += p
                
                if i + j > 2.5:
                    over_2_5 += p
                
                if i > 0 and j > 0:
                    btts += p
                
                if i + j < 0.5:
                    under_0_5 += p
        
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
        
        # 识别高赔率比分（冷门比分）
        # 条件：概率<5% 且 净胜球≥2 或 客胜
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
                'under_0_5': round(under_0_5 * 100, 1),
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

    # ==================== 冷门信号计算 ====================

    def calculate_odds_divergence(self, match_data: Dict, model_prob: float) -> Dict:
        """
        赔率背离度 —— 市场赔率与模型概率的差异
        
        这是26sp/56sp的根本来源：市场严重低估了某个结果。
        """
        odds = match_data.get('odds', {})
        home_odds = odds.get('home_odds', 2.0)
        draw_odds = odds.get('draw_odds', 3.5)
        away_odds = odds.get('away_odds', 3.5)
        
        # 市场隐含概率（扣除庄家利润后）
        margin = (1/home_odds + 1/draw_odds + 1/away_odds) - 1.0  # 庄家利润率
        fair_divisor = 1 + margin
        
        implied_home = (1 / home_odds) / fair_divisor
        implied_draw = (1 / draw_odds) / fair_divisor
        implied_away = (1 / away_odds) / fair_divisor
        
        # 模型概率 vs 市场隐含概率的背离
        # 正值 = 模型比市场更看好
        divergence_home = model_prob - implied_home
        divergence_away = (1 - model_prob - 0.25) - implied_away  # 近似客胜概率
        divergence_draw = 0.25 - implied_draw  # 平局固定25%基准
        
        # 找最大背离方向
        divergences = {
            'home': divergence_home,
            'draw': divergence_draw,
            'away': divergence_away,
        }
        
        max_dir = max(divergences, key=divergences.get)
        max_div = divergences[max_dir]
        
        # 高赔率背离信号（v2.2: 加入主队高赔率冷门检测）
        high_odds_signal = 0.0
        if away_odds > self.HIGH_ODDS_THRESHOLD and divergence_away > self.VALUE_GAP_THRESHOLD:
            high_odds_signal = divergence_away * (away_odds / 5.0)  # 赔率越高信号越强
        if draw_odds > 4.0 and divergence_draw > 0.03:
            high_odds_signal = max(high_odds_signal, divergence_draw * (draw_odds / 4.0))
        if home_odds > self.HIGH_ODDS_THRESHOLD and divergence_home > self.VALUE_GAP_THRESHOLD:
            high_odds_signal = max(high_odds_signal, divergence_home * (home_odds / 5.0))
        
        # 冷门信号：任何方向赔率>3.0 + 显著背离 → 都是冷门候选
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
        """
        形态背离 —— 弱队上升 vs 强队下滑
        
        检测"强势队的暗弱"和"弱势队的崛起"
        """
        home_id = match_data.get('home_team_id')
        away_id = match_data.get('away_team_id')
        
        try:
            with self.db.get_connection() as conn:
                form_data = {'home': {'form': [], 'trend': 0}, 'away': {'form': [], 'trend': 0}}
                
                for side, team_id in [('home', home_id), ('away', away_id)]:
                    if not team_id:
                        continue
                    
                    # 球队最近5场（不论主客，全面评估近期状态）
                    rows = conn.execute(
                        '''SELECT home_score, away_score, home_team_id, away_team_id FROM matches 
                           WHERE (home_team_id=? OR away_team_id=?) AND home_score IS NOT NULL
                           ORDER BY match_date DESC LIMIT 5''',
                        (team_id, team_id)
                    ).fetchall()
                    
                    if not rows:
                        continue
                    
                    # 计算形态分（赢3, 平1, 输0）和趋势
                    scores = []
                    for r in rows:
                        h_score, a_score = r[0], r[1]
                        h_id, a_id = r[2], r[3]
                        
                        # 判断该队在这场比赛中的结果
                        if team_id == h_id:
                            # 该队是主队
                            if h_score > a_score:
                                scores.append(3)
                            elif h_score == a_score:
                                scores.append(1)
                            else:
                                scores.append(0)
                            # 同时记录进球/失球用于攻防分析
                            if side == 'home':
                                form_data[side].setdefault('goals_scored', []).append(h_score)
                                form_data[side].setdefault('goals_conceded', []).append(a_score)
                        else:
                            # 该队是客队
                            if a_score > h_score:
                                scores.append(3)
                            elif a_score == h_score:
                                scores.append(1)
                            else:
                                scores.append(0)
                            if side == 'away':
                                form_data[side].setdefault('goals_scored', []).append(a_score)
                                form_data[side].setdefault('goals_conceded', []).append(h_score)
                    
                    form_data[side]['form'] = scores
                    
                    # 趋势：近期得分减去前期得分（上升为正）
                    if len(scores) >= 4:
                        recent = sum(scores[:2])
                        early = sum(scores[2:4])
                        form_data[side]['trend'] = (recent - early) / 6.0  # 归一化到[-1, 1]
                
                home_trend = form_data['home']['trend']
                away_trend = form_data['away']['trend']
                
                # 背离度 = 客队形态趋势 - 主队形态趋势
                # 正值 = 客队在上升而主队在下滑 → 冷门信号
                divergence = away_trend - home_trend
                
                # 等级（降低阈值以捕捉微弱上升信号）
                if divergence > 0.5:
                    level = '强冷门信号'
                elif divergence > 0.15:
                    level = '中等冷门信号'
                elif divergence > 0.02:
                    level = '微弱信号'
                else:
                    level = '无信号'
                
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
        """
        市场过度自信 —— 赔率过度倾斜强队
        
        现象：强队赔率很低（<1.4），但弱队赔率很高（>6.0）
        这往往意味着市场高估了强队，低估了弱队爆冷可能
        """
        home_odds = odds.get('home_odds', 2.0)
        away_odds = odds.get('away_odds', 3.5)
        draw_odds = odds.get('draw_odds', 3.5)
        
        # 过度自信指标
        # 条件1: 一方赔率过低
        min_odds = min(home_odds, away_odds)
        max_odds = max(home_odds, away_odds)
        
        overconfidence_score = 0.0
        signals = []
        
        # 强队赔率 < 1.4 → 可能存在过度自信
        if min_odds < 1.4:
            overconfidence_score += (1.4 - min_odds) * 2.0
            signals.append(f'强队赔率过低({min_odds:.2f})')
        
        # 赔率比 > 3.0 → 市场极度倾斜
        if max_odds / max(min_odds, 1.01) > 3.0:
            overconfidence_score += 0.3
            signals.append(f'赔率极度倾斜(比率{max_odds/min_odds:.1f}:1)')
        
        # 平局赔率 > 4.0 → 市场认为不可能平
        if draw_odds > 4.0:
            overconfidence_score += 0.2
            signals.append(f'平局赔率偏高({draw_odds:.1f})')
        
        # 返还率异常低 → 庄家更有信心
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
        """
        历史冷门率 —— 该联赛/对阵的冷门频率
        
        有些联赛天然冷门多（如德甲、英超），有些对阵有冷门传统
        """
        league_id = match_data.get('league_id')
        home_name = match_data.get('home_team_name', '')
        away_name = match_data.get('away_team_name', '')
        
        try:
            with self.db.get_connection() as conn:
                # 联赛级冷门率（赔率>3.0方获胜的比例）
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
                
                # 历史交锋冷门（如果主客队交手有冷门记录）
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
        """
        反超/翻盘倾向 — 基于用户实战经验：下半场打平或反超的比赛
        
        检测指标：
        1. 弱队近期有过爆冷获胜（赔率>3.0却赢了）
        2. 弱队近期客场/逆风球表现（趋势上升）
        3. 强弱差距越大，爆冷赔率越高
        """
        home_name = match_data.get('home_team_name', '')
        away_name = match_data.get('away_team_name', '')
        home_id = match_data.get('home_team_id')
        away_id = match_data.get('away_team_id')
        odds = match_data.get('odds', {})
        
        try:
            with self.db.get_connection() as conn:
                results = {'comeback_signals': [], 'score': 0.0}
                
                # 确定哪个是弱队（赔率高的一方）
                home_odds = odds.get('home_odds', 2.0)
                away_odds = odds.get('away_odds', 3.5)
                
                if away_odds > home_odds:
                    underdog_id, underdog_side = away_id, 'away'
                    favorite_odds = home_odds
                    underdog_odds = away_odds
                else:
                    underdog_id, underdog_side = home_id, 'home'
                    favorite_odds = away_odds
                    underdog_odds = home_odds
                
                # 信号1: 弱队近期爆冷获胜次数
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
                    
                    if total_recent and total_recent[0] > 0:
                        upset_rate = upsets[0] / total_recent[0]
                        if upset_rate >= 0.15:
                            results['comeback_signals'].append(
                                f'弱队近{total_recent[0]}场爆冷{upsets[0]}次(率{upset_rate*100:.0f}%)'
                            )
                            results['score'] += min(0.6, upset_rate * 3)
                
                # 信号2: 赔率比越大，爆冷赔率越高
                odds_ratio = underdog_odds / max(favorite_odds, 1.01)
                if odds_ratio > 3.5:
                    results['score'] += 0.2
                    results['comeback_signals'].append(f'强弱势比{odds_ratio:.1f}:1→高赔率爆冷潜力')
                elif odds_ratio > 2.5:
                    results['score'] += 0.1
                    results['comeback_signals'].append(f'强弱差距明显(赔率比{odds_ratio:.1f}:1)')
                
                # 信号3: 形态趋势辅助——弱队趋势上升 = 可能翻盘
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
        """
        大赛冷门模式 — 基于用户实战：8强赛/半决赛/决赛中热门队看似稳赢实则惨败
        
        用户案例：2026世俱赛冠军战 拜仁 vs 巴黎 3:0，56sp。
        模式特征：
        1. 大赛压力下强队崩盘（淘汰赛/决赛）
        2. 上半场小比分（1球以下），下半场爆发（全场≥3球）
        3. 赔率看似合理但隐含冷门因子
        
        Detection:
        - 检测是否大赛（final/semi/quarter/cup/championship关键词）
        - 高赔方Poisson预期不被完全压制（λ ≥ 0.8）
        - 总进球预期接近2.5
        """
        league_name = match_data.get('league_name', '')
        match_name = f"{match_data.get('home_team_name','')} vs {match_data.get('away_team_name','')}"
        odds = match_data.get('odds', {})
        
        # 大赛关键词检测
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
            return {
                'is_big_match': False, 'score': 0, 'level': '非大赛',
                'signals': [], 'is_upset_signal': False
            }
        
        bonus = 0.0
        signals = [f'大赛标签: {",".join(matched_keywords[:3])}']
        
        # 信号1：大赛压力级（半决赛=2, 决赛=4）
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
            
        # 信号2：高赔方有进球能力（Poisson λ分析）
        home_odds = odds.get('home_odds', 2.0)
        away_odds = odds.get('away_odds', 3.5)
        lambda_home = score_dist.get('lambda_home', 1.0)
        lambda_away = score_dist.get('lambda_away', 1.0)
        
        # 确定弱队
        if away_odds > home_odds:
            underdog_lambda = lambda_away
        else:
            underdog_lambda = lambda_home
            
        if underdog_lambda >= 0.8:
            bonus += 0.15
            signals.append(f'弱队有进球能力(λ={underdog_lambda:.1f})')
        elif underdog_lambda >= 0.5:
            bonus += 0.08
            signals.append(f'弱队偶有进球(λ={underdog_lambda:.1f})')
        
        # 信号3：上半场vs全场模式 — 预期总进球≥2.5但上半场节制
        total_goals = lambda_home + lambda_away
        if 2.3 <= total_goals <= 3.5:
            bonus += 0.10
            signals.append(f'上半场节制→下半场爆发模式(总进球{total_goals:.1f})')
        elif total_goals > 3.5:
            bonus += 0.15
            signals.append(f'超级大球模式(总进球{total_goals:.1f})')
        
        # 信号4：大赛冷门率历史加成
        if match_data.get('league_id'):
            try:
                with self.db.get_connection() as conn:
                    league_id = match_data['league_id']
                    # 该联赛历史大赛冷门率
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
        """
        大球冷门加分 — 基于用户实战经验：中的冷门都是3球以上
        
        逻辑：
        1. Poisson预期总进球 > 2.5 → 大球场景
        2. 有高比分概率（如3-0, 4-1, 2-2等）
        3. 赔率背离方向是高赔方 → 加分
        """
        try:
            bonus = 0.0
            signals = []
            
            # 从Poisson分布中提取信息
            home_lambda = score_dist.get('lambda_home', 1.0)
            away_lambda = score_dist.get('lambda_away', 1.0)
            total_goals = home_lambda + away_lambda
            
            # 条件1: 预期总进球 > 2.5（大球场景）
            if total_goals > self.GOAL_RUSH_TOTAL:
                bonus += 0.3
                signals.append(f'大球场景(预期{total_goals:.1f}球)')
            
            if total_goals > 3.0:
                bonus += 0.2
                signals.append(f'超级大球(预期{total_goals:.1f}球)')
            
            # 条件2: 有净胜≥2的高赔率比分
            cold_scores = score_dist.get('cold_gate_scores', [])
            high_goal_upsets = [s for s in cold_scores 
                              if abs(s['home_goals'] - s['away_goals']) >= self.GOAL_RUSH_DIFF
                              and s['home_goals'] + s['away_goals'] >= 3]
            
            if high_goal_upsets:
                best = high_goal_upsets[0]
                bonus += 0.2
                signals.append(f'高赔率大比分{best["score"]}(p={best["probability"]})')
            
            # 条件3: 赔率背离是客胜/平局方向（冷门方向）
            home_odds = odds.get('home_odds', 2.0)
            away_odds = odds.get('away_odds', 3.5)
            
            if away_odds > 3.0 or home_odds > 3.0:
                bonus += 0.15
                signals.append('赔率已预示冷门可能')
            
            # 条件4: 形态背离辅助——弱队能进球
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

    # ==================== 冷门评分合成 ====================

    def synthesize_upset_score(self, signals: Dict) -> Dict:
        """
        多维度信号融合 → UpsetScore (0-100)
        
        高UpsetScore = 高冷门概率 = 高赔率投注机会
        
        v2.2 新增: 主队高赔率冷门 + 大赛冷门模式（8强/半决赛/决赛）
        """
        w = self.weights
        
        # 归一化各信号到 [0, 1]
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
        
        # 等级判定（降低门槛以适配实战高赔率模式）
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

    # ==================== 高赔率比分推荐 ====================

    def recommend_high_odds_bets(self, score_dist: Dict, odds: Dict, 
                                  upset_score: float) -> List[Dict]:
        """
        高赔率比分推荐
        
        结合Poisson比分分布 + 赔率背离，推荐有价投价值的高赔率投注
        
        用户实战经验：26sp、56sp的比分预测 → 核心在λ估计的准确性
        """
        recommendations = []
        
        cold_scores = score_dist.get('cold_gate_scores', [])
        if not cold_scores:
            return recommendations
        
        away_odds = odds.get('away_odds', 3.5)
        home_odds = odds.get('home_odds', 2.0)
        draw_odds = odds.get('draw_odds', 3.5)
        
        for cs in cold_scores:
            prob = float(cs['probability'].replace('%', ''))
            score = cs['score']
            
            # 估算该比分的赔率
            # 大比分（净胜球≥2）通常赔率 ≥ 10sp
            goal_diff = abs(cs['home_goals'] - cs['away_goals'])
            
            if goal_diff == 0:
                est_odds = draw_odds * 3  # 高比分平局 → 高赔率
            elif goal_diff == 1:
                est_odds = min(away_odds, home_odds) * 2
            elif goal_diff == 2:
                est_odds = max(away_odds, home_odds) * 4  # ~20-30sp
            else:
                est_odds = max(away_odds, home_odds) * 8  # ~40-60sp
            
            # 期望价值
            ev = (prob / 100) * est_odds - 1
            
            # UpsetScore加成
            upset_bonus = upset_score / 200  # 0-0.5范围
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
        
        # 按EV排序
        recommendations.sort(key=lambda x: x['expected_value'], reverse=True)
        return recommendations[:5]

    def _explain_cold_gate(self, score_data: Dict, odds: Dict, est_odds: float) -> str:
        """解释冷门逻辑"""
        home_g = score_data['home_goals']
        away_g = score_data['away_goals']
        parts = []
        
        if away_g > home_g:
            parts.append(f"客胜冷门")
        if abs(home_g - away_g) >= 2:
            parts.append(f"净胜{abs(home_g-away_g)}球大胜")
        if home_g + away_g >= 4:
            parts.append(f"大球高比分")
        if est_odds > 20:
            parts.append(f"超高赔率~{est_odds:.0f}sp")
        
        return ' + '.join(parts) if parts else '冷门候选'

    # ==================== 单场分析 ====================

    def analyze_match(self, match_data: Dict) -> Dict:
        """
        单场比赛冷门分析
        
        Args:
            match_data: {
                'match_id': int,
                'home_team_id': int,
                'away_team_id': int,
                'home_team_name': str,
                'away_team_name': str,
                'league_id': int,
                'model_prob': float,  # 模型预测的主胜概率
                'odds': {'home_odds': float, 'draw_odds': float, 'away_odds': float},
            }
        """
        logger.info("=" * 60)
        match_name = f"{match_data.get('home_team_name','主')} vs {match_data.get('away_team_name','客')}"
        logger.info(f"🔍 分析: {match_name}")
        logger.info("=" * 60)
        
        model_prob = match_data.get('model_prob', 0.5)
        odds = match_data.get('odds', {})
        
        # 1. Poisson比分预测
        lambda_home, lambda_away = self.estimate_expected_goals(match_data)
        score_dist = self.predict_score_distribution(lambda_home, lambda_away)
        
        logger.info(f"  📊 λ_home={lambda_home:.2f} λ_away={lambda_away:.2f} "
                   f"期望总进球={lambda_home+lambda_away:.2f}")
        logger.info(f"  ⚽ 最可能比分: {score_dist['top_scores'][0]['score']} "
                   f"({score_dist['top_scores'][0]['probability']})")
        
        # 2. 赔率背离
        odds_div = self.calculate_odds_divergence(match_data, model_prob)
        logger.info(f"  💰 赔率背离: {odds_div['max_direction']}方向 "
                   f"偏差{odds_div['max_divergence']}%")
        
        # 3. 形态背离
        form_div = self.calculate_form_divergence(match_data)
        logger.info(f"  📈 形态背离: {form_div['level']} "
                   f"(主队趋势={form_div['home_trend']}, 客队趋势={form_div['away_trend']})")
        
        # 4. 市场过度自信
        market_conf = self.calculate_market_overconfidence(odds)
        logger.info(f"  🎯 市场过度自信: {market_conf['level']} "
                   f"(得分={market_conf['score']})")
        if market_conf['signals']:
            for sig in market_conf['signals']:
                logger.info(f"     └ {sig}")
        
        # 5. 历史冷门率
        hist_upset = self.calculate_historical_upset_rate(match_data)
        logger.info(f"  📜 历史冷门率: {hist_upset['league_upset_rate']}% "
                   f"(级别={hist_upset['level']})")
        
        # 6. 反超/翻盘倾向（用户实战：下半场打平/反超）
        comeback = self.calculate_comeback_tendency(match_data, form_div)
        if comeback['comeback_signals']:
            logger.info(f"  🔄 反超倾向: {comeback['level']} (得分={comeback['score']})")
            for sig in comeback['comeback_signals']:
                logger.info(f"     └ {sig}")
        else:
            logger.info(f"  🔄 反超倾向: {comeback['level']}")
        
        # 7. 大球冷门加分（用户实战：3球以上冷门）
        goal_rush = self.calculate_goal_rush_bonus(score_dist, odds, form_div)
        if goal_rush['signals']:
            logger.info(f"  ⚡ 大球冷门: {goal_rush['level']} (得分={goal_rush['score']})")
            for sig in goal_rush['signals']:
                logger.info(f"     └ {sig}")
        else:
            logger.info(f"  ⚡ 大球冷门: {goal_rush['level']}")
        
        # 8. 大赛冷门模式（用户实战：8强/半决赛/决赛 热门崩盘）
        big_match = self.calculate_big_match_upset(match_data, score_dist)
        if big_match.get('is_big_match'):
            logger.info(f"  🏆 大赛冷门: {big_match['level']} (得分={big_match['score']})")
            for sig in big_match['signals']:
                logger.info(f"     └ {sig}")
        
        # 9. 合成冷门评分（v2.2: 支持全方向冷门概率）
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
        
        logger.info(f"\n  {'='*40}")
        logger.info(f"  🎰 冷门评分: {upset['upset_score']:.1f}/100")
        logger.info(f"  {upset['upset_level']}")
        logger.info(f"  📋 {upset['action']}")
        logger.info(f"  {'='*40}")
        
        # 9. 高赔率比分推荐（降低门槛至20以适应大球/反超模式）
        if upset['upset_score'] >= 20:
            logger.info(f"\n  🎯 高赔率比分推荐:")
            bets = self.recommend_high_odds_bets(score_dist, odds, upset['upset_score'])
            if bets:
                for b in bets:
                    logger.info(f"    比分 {b['score']:5s}  预估赔率~{b['estimated_odds']:.0f}sp  "
                               f"模型概率{b['model_prob']:>6s}  EV={b['expected_value']:+.1f}%  "
                               f"理由: {b['rationale']}")
            else:
                logger.info(f"    (当前冷门评分下无高EV推荐)")
        else:
            logger.info(f"\n  ℹ️ 冷门评分偏低，不建议高赔率投注")
            bets = []
        
        return {
            'match_id': match_data.get('match_id'),
            'match_name': match_name,
            'analyzed_at': datetime.now().isoformat(),
            'poisson_prediction': score_dist,
            'odds_divergence': odds_div,
            'form_divergence': form_div,
            'market_overconfidence': market_conf,
            'historical_upset': hist_upset,
            'comeback_tendency': comeback,
            'goal_rush_bonus': goal_rush,
            'upset_assessment': upset,
            'high_odds_bets': bets,
        }

    # ==================== 批量分析 ====================

    def analyze_upcoming_matches(self, league: str = None) -> List[Dict]:
        """分析所有待预测比赛，找出冷门候选"""
        try:
            with self.db.get_connection() as conn:
                # v2.3: 使用子查询替代 LEFT JOIN 避免 WAL 模式下间歇性返回0行bug
                query = '''
                    SELECT m.match_id, m.home_team_id, m.away_team_id,
                           m.home_team_name, m.away_team_name, m.league_id,
                           m.league_name, m.match_date,
                           (SELECT home_odds FROM odds WHERE match_id=m.match_id LIMIT 1) as home_odds,
                           (SELECT draw_odds FROM odds WHERE match_id=m.match_id LIMIT 1) as draw_odds,
                           (SELECT away_odds FROM odds WHERE match_id=m.match_id LIMIT 1) as away_odds,
                           (SELECT confidence_level FROM predictions WHERE match_id=m.match_id LIMIT 1) as model_prob
                    FROM matches m
                    WHERE m.status = 'scheduled'
                '''
                params = []
                if league:
                    query += ' AND m.league_name = ?'
                    params.append(league)
                
                query += ' ORDER BY m.match_date ASC LIMIT 100'
                rows = conn.execute(query, params).fetchall()
                
                results = []
                for row in rows:
                    match_data = {
                        'match_id': row[0],
                        'home_team_id': row[1],
                        'away_team_id': row[2],
                        'home_team_name': row[3],
                        'away_team_name': row[4],
                        'league_id': row[5],
                        'league_name': row[6],
                        'match_date': row[7],
                        'model_prob': (row[11] or 50) / 100.0,
                        'odds': {
                            'home_odds': row[8] or 2.0,
                            'draw_odds': row[9] or 3.5,
                            'away_odds': row[10] or 3.5,
                        },
                    }
                    
                    result = self.analyze_match(match_data)
                    results.append(result)
                
                return results
        
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"批量分析失败: {e}", exc_info=True)
            return []

    def backtest_upset_detection(self, limit: int = 100):
        """回测冷门检测准确率"""
        logger.info("=" * 60)
        logger.info("📊 冷门检测回测")
        logger.info("=" * 60)
        
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    '''SELECT m.match_id, m.home_team_name, m.away_team_name,
                              m.home_team_id, m.away_team_id, m.league_id,  -- 新增
                              m.home_score, m.away_score, m.league_name,
                              o.home_odds, o.draw_odds, o.away_odds,
                              p.confidence_level
                       FROM matches m
                       LEFT JOIN odds o ON m.match_id = o.match_id
                       LEFT JOIN predictions p ON m.match_id = p.match_id
                       WHERE m.home_score IS NOT NULL
                       AND o.odds_id IS NOT NULL
                       ORDER BY m.match_date DESC
                       LIMIT ?''',
                    (limit,)
                ).fetchall()
                
                total = 0
                upset_predicted = 0
                upset_correct = 0
                high_odds_hits = 0
                
                for row in rows:
                    # row: [0]mid [1]h_name [2]a_name [3]h_id [4]a_id [5]l_id
                    #       [6]h_score [7]a_score [8]l_name
                    #       [9]h_odds [10]d_odds [11]a_odds [12]confidence
                    match_data = {
                        'match_id': row[0],
                        'home_team_name': row[1],
                        'away_team_name': row[2],
                        'home_team_id': row[3],
                        'away_team_id': row[4],
                        'league_id': row[5],
                        'league_name': row[8],
                        'model_prob': (row[12] or 50) / 100.0,
                        'odds': {
                            'home_odds': row[9] or 2.0,
                            'draw_odds': row[10] or 3.5,
                            'away_odds': row[11] or 3.5,
                        },
                    }
                    
                    # 实际结果
                    home_score, away_score = row[6], row[7]
                    actual_home_win = home_score > away_score
                    actual_draw = home_score == away_score
                    actual_away_win = home_score < away_score
                    
                    # 是否为冷门结果（高赔率方赢了）
                    odds = match_data['odds']
                    is_upset_outcome = (
                        (actual_away_win and odds['away_odds'] >= self.UPSET_ODDS_THRESHOLD) or
                        (actual_home_win and odds['home_odds'] >= self.UPSET_ODDS_THRESHOLD) or
                        (actual_draw and odds['draw_odds'] >= 4.0)
                    )
                    
                    # 判断胜负差的冷门程度
                    goal_diff = abs(home_score - away_score)
                    if goal_diff >= 3 and max(odds['home_odds'], odds['away_odds']) > 5.0:
                        is_upset_outcome = True
                    
                    if not is_upset_outcome:
                        total += 1  # 非冷门比赛也计入
                        continue
                    
                    total += 1
                    
                    # 运行冷门检测
                    try:
                        result = self.analyze_match(match_data)
                        upset_score = result['upset_assessment']['upset_score']
                        
                        if upset_score >= 30:
                            upset_predicted += 1
                            upset_correct += 1  # 有冷门且预测到
                        
                        # 检测是否推荐的高赔率比分命中
                        actual_score = f"{home_score}-{away_score}"
                        for bet in result.get('high_odds_bets', []):
                            if bet['score'] == actual_score:
                                high_odds_hits += 1
                                logger.info(f"  🎯 命中! {match_data['home_team_name']} "
                                           f"{actual_score} {match_data['away_team_name']} "
                                           f"(UpsetScore={upset_score:.0f})")
                    except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
                        logger.debug(f"回测单场失败: {e}")
                
                precision = upset_correct / max(upset_predicted, 1) * 100
                recall = upset_correct / max(
                    sum(1 for r in rows if True), 1
                ) * 100
                
                logger.info(f"\n  回测结果 ({limit}场):")
                logger.info(f"  实际冷门: {total}场")
                logger.info(f"  预测冷门: {upset_predicted}场")
                logger.info(f"  正确预测: {upset_correct}场")
                logger.info(f"  高赔率比分命中: {high_odds_hits}次")
                logger.info(f"  精确率: {precision:.1f}%")
                
                return {
                    'total': total,
                    'upset_predicted': upset_predicted,
                    'upset_correct': upset_correct,
                    'high_odds_hits': high_odds_hits,
                    'precision': precision,
                }
        
        except (Exception) as e:
            logger.error(f"回测失败: {e}", exc_info=True)
            return {}

    # ==================== 凯利公式 ====================

    @staticmethod
    def kelly_criterion(win_prob: float, odds: float, 
                        fraction: float = 0.5) -> Dict:
        """
        凯利准则 —— 最优投注比例
        
        f* = (p * b - q) / b
        其中: p = 胜率, b = 净赔率(odds-1), q = 1-p
        
        Args:
            win_prob: 模型预测胜率
            odds: 赔率（含本金）
            fraction: 凯利分数（0.5=半凯利，更保守）
        """
        p = win_prob
        b = odds - 1  # 净赔率
        q = 1 - p
        
        f_star = (p * b - q) / max(b, 0.01)  # 全凯利
        f_star = max(0, f_star) * fraction   # 半凯利
        
        # 期望价值
        ev = p * b - q
        
        # 破产风险（连续N次失败的概率）
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


# ==================== CLI ====================

def main():
    parser = argparse.ArgumentParser(description='哨响AI 冷门检测引擎')
    parser.add_argument('--match-id', type=int, help='分析指定比赛ID')
    parser.add_argument('--league', type=str, help='分析指定联赛')
    parser.add_argument('--backtest', action='store_true', help='回测历史冷门检测')
    parser.add_argument('--limit', type=int, default=100, help='回测场次限制')
    parser.add_argument('--output', type=str, default='upset_analysis.json', help='输出文件')
    args = parser.parse_args()
    
    detector = UpsetDetector()
    
    if args.backtest:
        result = detector.backtest_upset_detection(limit=args.limit)
        if result:
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        return
    
    if args.match_id:
        # 单场分析
        with detector.db.get_connection() as conn:
            row = conn.execute(
                '''SELECT m.match_id, m.home_team_id, m.away_team_id,
                          m.home_team_name, m.away_team_name, m.league_id, m.league_name,
                          o.home_odds, o.draw_odds, o.away_odds, p.confidence_level
                   FROM matches m
                   LEFT JOIN odds o ON m.match_id = o.match_id
                   LEFT JOIN predictions p ON m.match_id = p.match_id
                   WHERE m.match_id = ?''',
                (args.match_id,)
            ).fetchone()
            
            if not row:
                logger.error(f"比赛 {args.match_id} 不存在")
                return
            
            match_data = {
                'match_id': row[0],
                'home_team_id': row[1],
                'away_team_id': row[2],
                'home_team_name': row[3],
                'away_team_name': row[4],
                'league_id': row[5],
                'league_name': row[6],
                'model_prob': (row[10] or 50) / 100.0,
                'odds': {
                    'home_odds': row[7] or 2.0,
                    'draw_odds': row[8] or 3.5,
                    'away_odds': row[9] or 3.5,
                },
            }
            
            result = detector.analyze_match(match_data)
    else:
        # 批量分析
        results = detector.analyze_upcoming_matches(league=args.league)
        
        if not results:
            logger.info("没有找到待分析比赛（状态=SCHEDULED且有赔率数据）")
            return
        
        # 按冷门评分排序
        results.sort(key=lambda x: x['upset_assessment']['upset_score'], reverse=True)
        
        # 输出 Top 10 冷门候选
        logger.info("\n" + "=" * 60)
        logger.info("🏆 Top 10 冷门候选")
        logger.info("=" * 60)
        
        for i, r in enumerate(results[:10]):
            upset = r['upset_assessment']
            odds = r['odds_divergence']
            logger.info(
                f"  #{i+1:2d}  {r['match_name']:<30s}  "
                f"UpsetScore={upset['upset_score']:5.1f}  "
                f"赔率(H/D/A)={odds['home_odds']}/{odds['draw_odds']}/{odds['away_odds']}  "
                f"背离方向={odds['max_direction']}"
            )
            if r['high_odds_bets']:
                for bet in r['high_odds_bets'][:2]:
                    logger.info(f"      └ 推荐比分 {bet['score']} ~{bet['estimated_odds']:.0f}sp  "
                               f"EV={bet['expected_value']:+.1f}%")
        
        # 保存完整报告
        report = {
            'generated_at': datetime.now().isoformat(),
            'total_analyzed': len(results),
            'top_upsets': [
                {
                    'rank': i + 1,
                    'match_name': r['match_name'],
                    'upset_score': r['upset_assessment']['upset_score'],
                    'upset_level': r['upset_assessment']['upset_level'],
                    'poisson_prediction': {
                        'lambda_home': r['poisson_prediction']['lambda_home'],
                        'lambda_away': r['poisson_prediction']['lambda_away'],
                        'top_scores': r['poisson_prediction']['top_scores'][:3],
                    },
                    'high_odds_bets': r['high_odds_bets'],
                }
                for i, r in enumerate(results[:15])
            ],
            'all_results': results,
        }
        
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        
        logger.info(f"\n✅ 完整报告已保存到: {args.output}")
        
        result = report
    
    return result


if __name__ == '__main__':
    main()

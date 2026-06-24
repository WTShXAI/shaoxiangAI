"""
哨响AI - 特征计算模块
基于第零章公式计算所有特征
包含8大模块19+核心公式的完整实现
"""
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class FeatureCalculator:
    """特征计算器 - 实现第零章全部公式"""

    def __init__(self, w_high_ball: float = 0.6, beta: float = 0.3,
                 fitness_coeff: float = 0.3, sigma_trap_threshold: float = 0.15):
        self.params = {
            'w_high_ball': w_high_ball,
            'beta': beta,
            'fitness_coeff': fitness_coeff,
            'sigma_trap_threshold': sigma_trap_threshold,
        }

    def calculate_all_features(self, match_data: Dict) -> Dict:
        """
        计算一场比赛的所有特征
        输入: 比赛数据字典
        输出: 完整特征字典
        """
        features = {'match_id': match_data.get('match_id')}

        # ===== 模块1: 盘口动态特征 =====
        features['sigma_trap'] = self.calc_odd_volatility(
            match_data.get('odds_history', []), match_data.get('avg_volume', 1000))
        features['v_value'] = self.calc_kelly_odd_value(
            match_data.get('model_prob', 0.5), match_data.get('current_odd', 2.0),
            match_data.get('return_rate', 0.95))
        features['p_implied'] = self.calc_implied_probability(
            match_data.get('current_odd', 2.0), match_data.get('return_rate', 0.95))
        features['beta_dev'] = self.calc_handicap_deviation(
            match_data.get('theoretical_handicap', 0.0), match_data.get('actual_handicap', 0.0))

        # ===== 模块2: 赛事基本面特征 =====
        features['lambda_crush'] = self.calc_tactical_restraint(
            match_data.get('strengths', [70, 65]), match_data.get('weights', [0.5, 0.5]),
            match_data.get('historical_suppression', 0.5))
        features['delta_fatigue'] = self.calc_fatigue_factor(
            match_data.get('midweek_intensity', 0))
        features['fitness_75'] = self.calc_75min_fitness(
            match_data.get('home_goals_75plus', 0), match_data.get('away_conceded_75plus', 1),
            match_data.get('rest_days', 7))
        features['aerial_advantage'] = self.calc_aerial_effectiveness(
            match_data.get('attacker_aerial_win', 50.0), match_data.get('defender_aerial_win', 50.0))
        features['press_intensity'] = self.calc_pressing_confrontation(
            match_data.get('home_press_count', 10), match_data.get('away_pass_success', 80.0))

        # ===== 模块3: 市场情绪特征 =====
        features['epsilon_senti'] = self.calc_sentiment_bias(
            match_data.get('nlp_score', 0.5), match_data.get('discussion_growth', 0.5))
        features['s_whale'] = self.calc_whale_signal(
            match_data.get('euro_big_bet_home', False), match_data.get('asia_big_bet_away', False))
        features['discussion_growth'] = self.calc_discussion_index(
            match_data.get('home_discussion_growth', 0.0), match_data.get('away_discussion_growth', 0.0),
            match_data.get('total_discussion', 1000))
        features['news_impact'] = self.calc_news_impact(
            match_data.get('breaking_news_count', 0), match_data.get('sentiment_polarity', 0.0))

        # ===== 模块4: 时空断裂带特征 =====
        features['time_suppression'] = self.calc_time_suppression(
            match_data.get('home_goals_by_time', {}), match_data.get('away_conceded_by_time', {}),
            match_data.get('optimal_time', 60))

        # ===== 模块5: 裁判影响特征 =====
        features['card_risk'] = self.calc_card_risk(
            match_data.get('referee_avg_cards', 3.5), features['press_intensity'])
        features['referee_matrix'] = self.calc_referee_matrix(
            match_data.get('referee_strictness', 0.5), match_data.get('referee_home_bias', 0.0),
            match_data.get('referee_var_rate', 0.3))

        # ===== 模块6: 跨市场套利特征 =====
        features['arbitrage_index'] = self.calc_arbitrage_index(
            match_data.get('asian_home_prob', 0.5), match_data.get('euro_home_prob', 0.5),
            match_data.get('volume_ratio', 1.0))
        features['arbitrage_window'] = self.calc_arbitrage_window(
            match_data.get('odds_diff', 0.05), match_data.get('avg_volume', 1000))

        # ===== 新增：排名差 / 表单动量 / 交锋优势 =====
        rank_diff = match_data.get('rank_diff_factor', 0.0)
        form_mom = match_data.get('form_momentum', 0.0)
        h2h = match_data.get('h2h_factor', 0.0)
        features['rank_diff_factor'] = rank_diff
        features['form_momentum'] = form_mom
        features['h2h_factor'] = h2h
        features['rank_factor'] = self.calc_rank_factor(rank_diff)
        features['form_factor'] = self.calc_form_factor(form_mom)

        # ===== 计算核心因子A1, A2, A3 =====
        features['a1'] = self._calc_a1(features)
        features['a2'] = self._calc_a2(features)  # 含新因子
        features['a3'] = self._calc_a3(features)

        # ===== 新增核心因子 A4, A5, A6 (v2.0) =====
        features['a4'] = self._calc_a4(features)
        features['a5'] = self._calc_a5(features)
        features['a6'] = self._calc_a6(features)

        # ===== 新增核心因子 A7, A8 (v2.1) =====
        features['a7'] = self._calc_a7(features)
        features['a8'] = self._calc_a8(features)

        # ===== D预测增强特征 (v2.4) =====
        features['match_evenness'] = self.calc_match_evenness(
            features.get('rank_diff_factor', 0.0))
        features['home_advantage_neutral'] = self.calc_home_advantage_neutral(
            features.get('a1', 0.0))
        features['imp_d_norm'] = self.calc_imp_d_norm(
            match_data.get('home_odds', 0.0), match_data.get('draw_odds', 0.0),
            match_data.get('away_odds', 0.0))
        features['odds_balance'] = self.calc_odds_balance(
            match_data.get('home_odds', 0.0), match_data.get('draw_odds', 0.0),
            match_data.get('away_odds', 0.0))

        # ===== P_final 冷门补偿 =====
        features['p_final'] = self.calc_upset_compensation(
            features.get('p_fusion', 0.5), features.get('sigma_trap', 0.0),
            features.get('s_whale', 1.0), features.get('lambda_crush', 1.0))

        # ===== 三维动态融合 =====
        # p_odds = 市场隐含概率，来自赔率归一化
        features['p_odds'] = features['p_implied']
        features['p_fusion'] = self.calc_3d_fusion(
            features['p_odds'],
            features['sigma_trap'], features['lambda_crush'], features['epsilon_senti'])

        return features

    # ===================== 模块1: 盘口动态 (4公式) =====================

    def calc_odd_volatility(self, odds_series: List[float], avg_volume: float) -> float:
        """公式0.2.1: 异常波动率 σ_trap"""
        if not odds_series or len(odds_series) < 2:
            return 0.0
        try:
            log_returns = [abs(np.log(odds_series[i] / odds_series[i - 1]))
                           for i in range(1, len(odds_series))]
            sigma = np.mean(log_returns) * (24.0 / max(avg_volume, 1.0))
            return float(sigma)
        except (Exception, ValueError, KeyError, IndexError) as e:
            logger.warning(f"[P0-3] calc_odd_volatility 异常: {e}")
            return 0.0

    def calc_kelly_odd_value(self, model_prob: float, odd: float, return_rate: float) -> float:
        """公式0.2.2: 凯利-赔率价值方程 V_value"""
        if odd <= 1.0:
            return 0.0
        value = (model_prob * odd - 1) / (odd - 1) - 0.05 * return_rate
        return float(value)

    def calc_implied_probability(self, odd: float, return_rate: float) -> float:
        """公式0.2.3: 隐含胜率转换 P_implied（return_rate 是庄家利润率）"""
        if odd <= 0:
            return 0.0
        # return_rate = 利润率（~6%），返还率 = 1 - return_rate
        payout_rate = max(1.0 - return_rate, 0.01)
        return float((1.0 / odd) * payout_rate)

    def calc_handicap_deviation(self, theoretical_hc: float, actual_hc: float) -> float:
        """公式0.2.4: 盘口偏差指数 β_dev"""
        return float(abs(theoretical_hc - actual_hc))

    # ===================== 模块2: 赛事基本面 (5公式) =====================

    def calc_tactical_restraint(self, strengths: List[float], weights: List[float],
                                 historical_suppression: float) -> float:
        """公式0.3.1: 战术克制系数 λ_crush"""
        if len(strengths) < 2 or not weights:
            return 1.0
        try:
            ratio = strengths[0] / max(strengths[1], 0.001)
            weighted_ratio = np.average([ratio] * len(weights), weights=weights)
            lambda_crush = weighted_ratio * (1 - np.exp(-historical_suppression))
            return float(lambda_crush)
        except (Exception, ValueError, KeyError, IndexError) as e:
            logger.warning(f"[P0-3] calc_lambda_crush 异常: {e}")
            return 1.0

    def calc_fatigue_factor(self, midweek_intensity: float) -> float:
        """公式0.3.2: 体能衰减因子 δ_fatigue"""
        return float(np.exp(-0.05 * midweek_intensity))

    def calc_75min_fitness(self, home_goals_75plus: int, away_conceded_75plus: int,
                            rest_days: int) -> float:
        """公式0.3.3: 75分钟体能定律（归一化版，输出[0, 2]）"""
        if away_conceded_75plus <= 0:
            return 1.0
        base_ratio = home_goals_75plus / max(away_conceded_75plus, 1)
        # 休息因子：7天为基准(=1.0)，越多越有利，上限1.5倍
        rest_factor = min(rest_days / 7.0, 1.5)
        # 限制在合理范围 [0, 2.0]
        return float(max(0.0, min(base_ratio * rest_factor, 2.0)))

    def calc_aerial_effectiveness(self, attacker_aerial_win: float, defender_aerial_win: float) -> float:
        """公式0.3.4: 防空有效性指数 α_aerial"""
        if defender_aerial_win == 0:
            return 1.0
        return float(attacker_aerial_win / defender_aerial_win)

    def calc_pressing_confrontation(self, home_press_count: int, away_pass_success: float) -> float:
        """公式0.3.5: 逼抢对抗公式 π_press"""
        if away_pass_success == 0:
            return 0.0
        return float(home_press_count / away_pass_success)

    # ===================== 模块3: 市场情绪 (4公式) =====================

    def calc_sentiment_bias(self, nlp_score: float, discussion_growth: float) -> float:
        """公式0.4.1: 情绪偏差因子 ε_senti"""
        try:
            denominator = 1 + np.exp(-(discussion_growth - 0.5))
            if denominator == 0:
                return 0.0
            return float(nlp_score * (1 - 2.0 / denominator))
        except (Exception, ValueError) as e:
            logger.warning(f"[P0-3] calc_epsilon_senti 异常: {e}")
            return 0.0

    def calc_whale_signal(self, euro_big_bet_home: bool, asia_big_bet_away: bool) -> float:
        """公式0.4.2: 大户博弈信号 S_whale"""
        if euro_big_bet_home and asia_big_bet_away:
            return -1.0
        return 1.0

    def calc_discussion_index(self, home_growth: float, away_growth: float, total: float) -> float:
        """公式0.4.3: 舆情增长指数 g_discuss"""
        if total == 0:
            return 0.0
        return float((home_growth - away_growth) / total)

    def calc_news_impact(self, breaking_news_count: int, sentiment_polarity: float) -> float:
        """公式0.4.4: 新闻冲击系数 γ_news"""
        return float(1.0 + (breaking_news_count / 10.0) * sentiment_polarity)

    # ===================== 模块4: 时空断裂带 (2公式) =====================

    def calc_time_suppression(self, home_goals_by_time: Dict, away_conceded_by_time: Dict,
                               optimal_time: int = 60) -> float:
        """公式0.5.1: 时段压制系数 δ_t"""
        if not home_goals_by_time or not away_conceded_by_time:
            return 1.0
        try:
            t = optimal_time
            hg = home_goals_by_time.get(str(t), home_goals_by_time.get(t, 1))
            ag = away_conceded_by_time.get(str(t), away_conceded_by_time.get(t, 1))
            if ag == 0:
                return 1.0
            delta_t = (hg / ag) * np.exp(-0.1 * abs(t - optimal_time))
            return float(delta_t)
        except (Exception, ValueError, requests.exceptions.RequestException) as e:
            logger.warning(f"[P0-3] calc_time_suppression 异常: {e}")
            return 1.0

    def calc_optimal_time(self, home_goals_by_time: Dict, away_conceded_by_time: Dict) -> int:
        """公式0.5.2: 基准时段 μ_τ"""
        if not home_goals_by_time or not away_conceded_by_time:
            return 60
        try:
            max_ratio = 0
            best_t = 60
            for t in range(0, 91, 15):
                hg = home_goals_by_time.get(str(t), home_goals_by_time.get(t, 0))
                ag = away_conceded_by_time.get(str(t), away_conceded_by_time.get(t, 1))
                if ag > 0:
                    ratio = hg / ag
                    if ratio > max_ratio:
                        max_ratio = ratio
                        best_t = t
            return best_t
        except (Exception, requests.exceptions.RequestException) as e:
            logger.warning(f"[P0-3] calc_peak_attack_time 异常: {e}")
            return 60

    # ===================== 模块5: 裁判影响 (2公式) =====================

    def calc_card_risk(self, referee_avg_cards: float, press_intensity: float) -> float:
        """公式0.6.1: 红黄牌风险模型 R_card"""
        x = referee_avg_cards * (1 + 0.2 * press_intensity)
        return float(1.0 / (1.0 + np.exp(-x + 3)))

    def calc_referee_matrix(self, strictness: float, home_bias: float, var_rate: float) -> float:
        """公式0.6.2: 裁判影响矩阵 → 复合标量
        综合裁判严格度、主场偏向、方差率，输出 [0, 1] 标量
        高值=主场有利，低值=客场有利/严格执法
        """
        return float(max(0.0, min(1.0, home_bias * 0.6 + (1.0 - strictness) * 0.2 + var_rate * 0.2)))

    # ===================== 模块6: 跨市场套利 (2公式) =====================

    def calc_arbitrage_index(self, asian_prob: float, euro_prob: float, volume_ratio: float) -> float:
        """公式0.7.1: 亚欧背离指数 α_arb"""
        if euro_prob == 0:
            return 0.0
        return float(abs(asian_prob / euro_prob - 1) * volume_ratio)

    def calc_arbitrage_window(self, odds_diff: float, avg_volume: float) -> float:
        """公式0.7.2: 套利时间窗口 W_arb"""
        if avg_volume == 0:
            return 0.0
        return float(odds_diff / avg_volume)

    # ===================== 模块7: 融合补偿 (4公式) =====================

    def calc_3d_fusion(self, p_odds: float, sigma_trap: float,
                        lambda_crush: float, epsilon_senti: float,
                        gamma: float = 0.7, alpha: float = 0.6, beta: float = 0.15) -> float:
        """公式0.8.1: 三维动态融合 P_fusion"""
        p_fusion = gamma * (alpha * p_odds + (1 - alpha) * sigma_trap) + \
                   (1 - gamma) * lambda_crush + beta * epsilon_senti
        return float(np.clip(p_fusion, 0.0, 1.0))

    def calc_upset_compensation(self, p_fusion: float, sigma_trap: float,
                                 s_whale: float, lambda_crush: float) -> float:
        """公式0.8.2: 冷门补偿机制 P_final"""
        if sigma_trap > self.params['sigma_trap_threshold'] and s_whale == -1:
            multiplier = 1.25
        elif lambda_crush > 2.0:
            multiplier = 0.8
        else:
            multiplier = 1.0
        return float(np.clip(p_fusion * multiplier, 0.0, 1.0))

    def calc_motivation_compensation(self, rank_diff: float) -> float:
        """公式0.8.3: 战意补偿因子 ΔP（增强版：含保级/争冠曲线）"""
        # 基础线性补偿
        base = 0.03 * rank_diff
        # 非线性增强：大排名差时战意更强（保级队 vs 争冠队）
        if abs(rank_diff) > 12:
            base *= (1.0 + (abs(rank_diff) - 12) * 0.02)
        return float(np.clip(base, -0.15, 0.15))

    def calc_rank_factor(self, rank_diff_factor: float) -> float:
        """排名差因子：归一化到 [0, 1]，0.5=均势"""
        return float(np.clip(0.5 + rank_diff_factor * 0.5, 0.0, 1.0))

    def calc_form_factor(self, form_momentum: float) -> float:
        """表单动量因子：归一化到 [0, 1]"""
        return float(np.clip(0.5 + form_momentum * 0.5, 0.0, 1.0))

    def calc_h2h_advantage(self, h2h_factor: float) -> float:
        """交锋优势因子：归一化到 [0, 1]，0.5=均势"""
        return float(np.clip(0.5 + h2h_factor * 0.5, 0.0, 1.0))

    def calc_timespace_compensation(self, time_suppressions: List[float],
                                     threshold: float = 1.8) -> float:
        """公式0.8.4: 时空断裂补偿 δ_QF"""
        if not time_suppressions:
            return 0.0
        filtered = [dt for dt in time_suppressions if dt > threshold]
        if not filtered:
            return 0.0
        return float(np.mean(filtered))

    # ===================== 核心因子计算 =====================

    def _calc_a1(self, features: Dict) -> float:
        """A1: 盘口价值因子 = 价值缺口 × (1 + 异常波动调整)，[-0.5, 0.5]"""
        value_gap = features.get('v_value', 0.0)
        sigma_trap = features.get('sigma_trap', 0.0)
        return float(np.clip(value_gap * (1 + sigma_trap), -0.5, 0.5))

    def _calc_a2(self, features: Dict) -> float:
        """A2: 基本面优势因子 = (战术克制 + 体能优势 + 逼抢对抗 + 排名因子 + 表单动量) / 5，[0, 1]"""
        lambda_crush = features.get('lambda_crush', 1.0)
        fitness_75 = features.get('fitness_75', 0.0)
        press = features.get('press_intensity', 0.0)
        rank_factor = features.get('rank_factor', 0.5)
        form_factor = features.get('form_factor', 0.5)
        return float(np.clip(
            (lambda_crush + fitness_75 + press + rank_factor + form_factor) / 5.0, 0.0, 1.0))

    def _calc_a3(self, features: Dict) -> float:
        """A3: 市场情绪因子 = (情绪偏差 + 新闻冲击 + 大户信号归一化) / 3，[0, 1]"""
        epsilon = features.get('epsilon_senti', 0.5)
        news = features.get('news_impact', 1.0)
        whale = (features.get('s_whale', 1.0) + 1) / 2  # 归一化到[0,1]
        return float(np.clip((epsilon + news + whale) / 3.0, 0.0, 1.0))

    def _calc_a4(self, features: Dict) -> float:
        """
        A4: 盘口-基本面协同因子 (v2.0 新增)
        = A1 * A2 — 高盘口价值 + 高基本面 = 强信号；二者背离 = 弱信号
        范围 [-0.5, 0.5]，语义：正值=盘口和基本面都指向主队优势
        """
        a1 = features.get('a1', 0.0)
        a2 = features.get('a2', 0.5)
        return float(np.clip(a1 * a2, -0.5, 0.5))

    def _calc_a5(self, features: Dict) -> float:
        """
        A5: 波动调整信号因子 (v2.0 新增)
        = A1 / (1 + |sigma_trap|) — 用波动率对盘口信号做风险调整
        高波动 → A5 衰减，低波动 → A5 接近 A1
        范围 [-0.5, 0.5]
        """
        a1 = features.get('a1', 0.0)
        sigma = features.get('sigma_trap', 0.0)
        return float(np.clip(a1 / (1.0 + abs(sigma)), -0.5, 0.5))

    def calculate_match_features(self, home_team: str, away_team: str,
                                   home_data: Dict, away_data: Dict) -> Dict:
        """
        从两队聚合数据计算对阵特征（供 PredictionService API 调用）

        Args:
            home_team: 主队名
            away_team: 客队名
            home_data: db.get_team_features(home_team) 输出
            away_data: db.get_team_features(away_team) 输出

        Returns:
            特征字典，键名匹配模型 feature_names；未提供的特征由 ModelBridge 默认值填充
        """
        features = {}

        # ── 直接从 team 聚合数据映射 ──
        mapping = {
            'sigma_trap': 'avg_sigma_trap',
            'lambda_crush': 'avg_lambda_crush',
            'epsilon_senti': 'avg_epsilon_senti',
            'air_dominance': 'avg_aerial_advantage',
            'press_intensity': 'avg_press_intensity',
            'card_risk': 'avg_card_risk',
            'beta_dev': 'avg_beta_dev',
            'delta_fatigue': 'avg_delta_fatigue',
            'aerial_advantage': 'avg_aerial_advantage',
        }
        for feat_key, data_key in mapping.items():
            features[feat_key] = home_data.get(data_key, 0.0)

        # ── 差距型特征（主队 - 客队）──
        rank_diff = self._compute_rank_diff(home_data, away_data)
        features['rank_diff_factor'] = rank_diff

        form_diff = (home_data.get('recent_form_score', 0.5) -
                     away_data.get('recent_form_score', 0.5))
        features['form_momentum'] = form_diff

        # ── 交锋优势（主队视角）──
        features['h2h_factor'] = home_data.get('h2h_advantage', 0.0)

        # ── 归一化因子 ──
        features['rank_factor'] = self.calc_rank_factor(rank_diff)
        features['form_factor'] = self.calc_form_factor(form_diff)

        # ── 综合主队强势度特征 ──
        features['home_strength'] = (
            home_data.get('avg_a2', 0.5) * 0.4 +
            features['rank_factor'] * 0.3 +
            home_data.get('recent_form_score', 0.5) * 0.3
        )
        features['away_strength'] = (
            away_data.get('avg_a2', 0.5) * 0.4 +
            (1 - features['rank_factor']) * 0.3 +
            away_data.get('recent_form_score', 0.5) * 0.3
        )

        # ── 进球能力 ──
        features['home_goals_avg'] = home_data.get('goals_for_avg', 1.0)
        features['away_goals_avg'] = away_data.get('goals_for_avg', 1.0)
        features['home_conceded_avg'] = home_data.get('goals_against_avg', 1.0)
        features['away_conceded_avg'] = away_data.get('goals_against_avg', 1.0)

        # ── 核心因子 A1-A6 ──
        features['a1'] = self._calc_a1(features)
        features['a2'] = self._calc_a2(features)
        features['a3'] = self._calc_a3(features)
        features['a4'] = self._calc_a4(features)
        features['a5'] = self._calc_a5(features)
        features['a6'] = self._calc_a6(features)

        # ===== 新增核心因子 A7, A8 (v2.1) =====
        features['a7'] = self._calc_a7(features)
        features['a8'] = self._calc_a8(features)

        # ===== v2.8 冷启动特征 (Cold-Start Features) =====
        # 这些特征让模型能区分"有数据的正常预测"和"无数据的冷启动猜测"
        # 在 _build_features() 中会被实际值覆盖（如果可用），此处给默认值
        features['is_cold_start'] = 0.0          # 二值: 0=正常, 1=冷启动
        features['feat_coverage_ratio'] = 1.0     # 连续: 非默认特征比例 [0, 1]
        features['home_match_count_norm'] = 1.0   # 归一化主队历史场数（log scale）
        features['away_match_count_norm'] = 1.0   # 归一化客队历史场数（log scale）
        features['odds_entropy'] = 1.099          # 赔率熵（均匀分布时的最大熵≈1.099）

        return features

    @staticmethod
    def _compute_rank_diff(home_data: Dict, away_data: Dict) -> float:
        """计算归一化排名差 [-1, 1]，正值=主队排名更高（rank值更小）"""
        h_pos = home_data.get('avg_position', 10)
        a_pos = away_data.get('avg_position', 10)
        diff = h_pos - a_pos
        return round(max(-1.0, min(1.0, -diff / 10.0)), 4)

    def _calc_a6(self, features: Dict) -> float:
        """
        A6: 市场分歧度因子 (v2.0 新增)
        = |A3 - 0.5| * sign(A1) — 市场情绪偏离中性×盘口方向
        市场极度乐观/悲观 + 盘口同向 = 强信号
        范围 [-0.5, 0.5]，语义：正值=市场一致看好主队
        """
        a1 = features.get('a1', 0.0)
        a3 = features.get('a3', 0.5)
        divergence = abs(a3 - 0.5) * 2  # 归一化到 [0, 1]
        direction = 1 if a1 > 0 else (-1 if a1 < 0 else 0)
        return float(np.clip(divergence * direction, -0.5, 0.5))

    def _calc_a7(self, features: Dict) -> float:
        """A7: 体能-逼抢交互因子 = δ_fatigue × π_press，[0, 2]"""
        delta_fatigue = features.get('delta_fatigue', 1.0)
        press_intensity = features.get('press_intensity', 0.0)
        return float(np.clip(delta_fatigue * press_intensity, 0.0, 2.0))

    def _calc_a8(self, features: Dict) -> float:
        """A8: 盘口偏差-波动联合因子 = β_dev × |σ_trap|，[0, 2]
        
        v2.2: 替代旧公式 β_dev × α_arb (arbitrage_index 99.9%默认无法入模)
        新公式用 sigma_trap (54.3%真实) 替代 arbitrage_index
        含义: 市场不确定性(overround) × 市场波动方向 → 捕获盘口异动
        """
        beta_dev = features.get('beta_dev', 0.0)
        sigma_trap = abs(features.get('sigma_trap', 0.0))
        return float(np.clip(beta_dev * sigma_trap, 0.0, 2.0))

    # ===================== 模块8: D预测增强特征 (v2.4) =====================

    def calc_match_evenness(self, rank_diff_factor: float) -> float:
        """比赛均势度 = 1 - |rank_diff_factor|
        
        v2.4 新增: D预测最强特征 (corr=0.1052)
        含义: 两队实力越接近(rank_diff≈0) → 均势度越高 → D概率越大
        范围 [0, 1], 1=完全均势, 0=实力悬殊
        """
        return float(np.clip(1.0 - abs(rank_diff_factor), 0.0, 1.0))

    def calc_home_advantage_neutral(self, a1: float) -> float:
        """主场优势中性度 = 1 - |a1|
        
        v2.4 新增: D预测辅助特征 (corr=0.0499)
        含义: a1越小(无主队盘口优势) → 中性度越高 → D概率越大
        范围 [0, 1], 1=无主队优势, 0=强主队优势
        """
        return float(np.clip(1.0 - abs(a1), 0.0, 1.0))

    def calc_imp_d_norm(self, home_odds: float, draw_odds: float, away_odds: float) -> float:
        """归一化D隐含概率 = (1/D) / (1/H + 1/D + 1/A)
        
        v2.4 新增: 赔率市场对平局的评估 (corr=0.0764)
        含义: 赔率认为D概率越高 → 实际D概率越高
        范围 [0, 1], 约0.10-0.22
        """
        if home_odds <= 1.01 or draw_odds <= 1.01 or away_odds <= 1.01:
            return 0.0
        try:
            imp_h = 1.0 / home_odds
            imp_d = 1.0 / draw_odds
            imp_a = 1.0 / away_odds
            total = imp_h + imp_d + imp_a
            return float(imp_d / total) if total > 0 else 0.0
        except (Exception, ValueError, KeyError, IndexError):
            return 0.0

    def calc_odds_balance(self, home_odds: float, draw_odds: float, away_odds: float) -> float:
        """市场平衡度 = |P(H) - P(A)| (归一化隐含概率差)
        
        v2.4 新增: D预测#2特征 (|corr|=0.0782)
        含义: H/A赔率越接近 → balance越小 → 越可能D
        范围 [0, 1], 越小=越均势
        """
        if home_odds <= 1.01 or draw_odds <= 1.01 or away_odds <= 1.01:
            return 0.0
        try:
            imp_h = 1.0 / home_odds
            imp_d = 1.0 / draw_odds
            imp_a = 1.0 / away_odds
            total = imp_h + imp_d + imp_a
            if total <= 0:
                return 0.0
            norm_h = imp_h / total
            norm_a = imp_a / total
            return float(abs(norm_h - norm_a))
        except (Exception, ValueError):
            return 0.0

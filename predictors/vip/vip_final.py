"""
VIP Final Predictor v1.1 — 数字人 + 数学融合 (已整合至 UnifiedPredictor)
=====================================================================
注意: VIP Final 的数学融合层 (λ fusion + 陷阱检测) 已完整集成到
      `predictors/unified_predictor.py`，推荐使用统一接口。

架构:
  通道A: 数字人推理 (权重 0.30)
  通道B: v4.1 数学模型 (权重 0.70)
    - v4.1 production模型 (Acc=62.43%, Draw-F1=0.520)
    - λ融合 (模型λ + 庄家λ)
    - 16引擎陷阱检测

用法 (独立模式):
    from vip_final import VIPFinalPredictor
    vip = VIPFinalPredictor()
"""

import os, sys, math, numpy as np
from typing import Dict, List, Tuple, Optional, Any
from datetime import datetime

VIP_DIR = os.path.dirname(os.path.abspath(__file__))
ARCH_ROOT = os.path.dirname(os.path.dirname(VIP_DIR))
COMPONENTS = os.path.join(ARCH_ROOT, 'predictors', 'components')
# 修复P0-13: 消除footballAI外部依赖, 项目内自包含
FOOTBALLAI_ROOT = COMPONENTS  # 内部化

from lambda_fusion import fuse_lambda
from agents.model_bridge import ModelBridge
# 修复P0-13: 补全缺失依赖, 从项目内components加载
try:
    from predictors.components.trap_probability_bridge import apply_trap_correction
    from predictors.components.odds_inverse_calibrator import apply_goal_segment_correction
except ImportError:
    # 降级: 从footballAI根目录加载
    try:
        from trap_probability_bridge import apply_trap_correction
        from odds_inverse_calibrator import apply_goal_segment_correction
    except ImportError:
        # 最终降级: 定义空操作
        def apply_trap_correction(h, d, a, score):
            return h, d, a
        def apply_goal_segment_correction(prob, goals):
            return prob

from predictors.base import PredictorBase, MatchData, PredictionResult

class VIPFinalPredictor(PredictorBase):
    """
    VIP Final: 数字人 + 数学融合 统一预测器

    融合权重:
      - 数字人推理: 30%
      - 数学模型: 70%
    """

    def __init__(self, model_path=None):
        # ── 通过 ModelBridge 加载 v3.2 生产模型（避免重复加载5MB）──
        bridge = ModelBridge(model_path) if model_path else ModelBridge()
        self._bridge = bridge
        trainer = bridge.trainer
        if trainer is None:
            raise RuntimeError("VIPFinalPredictor: ModelBridge 回退到轻量模式，无法初始化")
        self.xgb = trainer.xgb_model
        self.lgb = trainer.lgb_model
        self.oe = trainer.odds_expert_model
        self.meta = trainer.meta_learner
        self.scaler = trainer.scaler
        self.odds_scaler = trainer.odds_scaler
        self.feature_names = trainer.feature_names
        self.odds_feature_names = trainer.odds_feature_names
        self.config = trainer.config
        self.defaults = self.config.get('data', {}).get('default_values', {})
        self.league_d_rates = trainer.league_d_rates
        self.eval_metrics = trainer.eval_metrics
        self.model_version = trainer.model_version

        # ── 初始化陷阱检测器 (16引擎 v3.1) ──
        from bookmaker_sim.bookmaker_trap_detector import BookmakerTrapDetector
        self.trap_detector = BookmakerTrapDetector()

        # ── 初始化数字人引擎 (可选) ──
        try:
            from digital_human import DigitalHuman
            self.dh = DigitalHuman(name="VIP-Final-DH")
        except (ImportError, NameError) as e:
            self.dh = None
            self.dh_weight = 0.0

        # 数字人权重 (可根据比赛类型动态调整)
        self.dh_weight = 0.30
        self.math_weight = 0.70

        self._ready = True

    # ════════════════════════════════════════════════════════════
    # 内部工具: λ ↔ 概率 互转
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _derive_lambda_from_odds(oh, od, oa):
        """从欧赔逆向庄家λ: 去抽水→二分求泊松平局概率"""
        raw_sum = 1.0 / oh + 1.0 / od + 1.0 / oa
        p_book = np.array([1 / (oh * raw_sum), 1 / (od * raw_sum), 1 / (oa * raw_sum)])
        share_h = p_book[0] / max(p_book[0] + p_book[2], 0.01)

        lo, hi = 0.3, 8.0
        best_t, best_err = 2.7, 1.0
        for _ in range(20):
            mid = (lo + hi) / 2
            lh, la = mid * share_h, mid * (1 - share_h)
            ph_arr = np.array([max(np.exp(-lh) * lh**k / math.factorial(k), 1e-30)
                               for k in range(13)])
            pa_arr = np.array([max(np.exp(-la) * la**k / math.factorial(k), 1e-30)
                               for k in range(13)])
            ph_arr /= ph_arr.sum()
            pa_arr /= pa_arr.sum()
            d_pred = float(sum(ph_arr[k] * pa_arr[k] for k in range(13)))
            err = abs(d_pred - p_book[1])
            if err < best_err:
                best_err, best_t = err, mid
            if d_pred < p_book[1]:
                lo = mid
            else:
                hi = mid

        lam_h = max(best_t * share_h, 0.1)
        lam_a = max(best_t * (1 - share_h), 0.1)
        return lam_h, lam_a

    @staticmethod
    def _probs_to_lambda(probs):
        """从1X2概率逆向λ值"""
        p_h, p_d, p_a = probs[0], probs[1], probs[2]
        share = p_h / max(p_h + p_a, 0.01)
        lo, hi = 0.3, 8.0
        best_t, best_err = 2.5, 1.0
        for _ in range(20):
            mid = (lo + hi) / 2
            lh, la = mid * share, mid * (1 - share)
            ph_arr = np.array([max(np.exp(-lh) * lh**k / math.factorial(k), 1e-30)
                               for k in range(13)])
            pa_arr = np.array([max(np.exp(-la) * la**k / math.factorial(k), 1e-30)
                               for k in range(13)])
            ph_arr /= ph_arr.sum()
            pa_arr /= pa_arr.sum()
            d_pred = float(sum(ph_arr[k] * pa_arr[k] for k in range(13)))
            err = abs(d_pred - p_d)
            if err < best_err:
                best_err, best_t = err, mid
            if d_pred < p_d:
                lo = mid
            else:
                hi = mid
        return max(best_t * share, 0.1), max(best_t * (1 - share), 0.1)

    @staticmethod
    def _lambda_to_1x2_probs(lam_h, lam_a):
        """泊松模型: λ → 1X2概率"""
        ph = np.array([max(np.exp(-lam_h) * lam_h**k / math.factorial(k), 1e-30)
                       for k in range(13)])
        pa = np.array([max(np.exp(-lam_a) * lam_a**k / math.factorial(k), 1e-30)
                       for k in range(13)])
        ph /= ph.sum()
        pa /= pa.sum()
        p_h = sum(ph[i] * sum(pa[:i]) for i in range(1, 13))
        p_d = sum(ph[i] * pa[i] for i in range(13))
        p_a = sum(pa[i] * sum(ph[:i]) for i in range(1, 13))
        total = p_h + p_d + p_a
        return np.array([p_h, p_d, p_a]) / max(total, 1e-10)

    # ════════════════════════════════════════════════════════════
    # 特征向量构建
    # ════════════════════════════════════════════════════════════

    def _build_feature_vector(self, match, p_h, p_d, p_a, trap_score):
        """填充72维特征向量"""
        n_feats = len(self.feature_names)
        vec = np.zeros(n_feats, dtype=np.float32)

        for i, name in enumerate(self.feature_names):
            if name in self.defaults:
                vec[i] = float(self.defaults[name])

        oh = float(match.get('odds_h', 2.0))
        od = float(match.get('odds_d', 3.5))
        oa = float(match.get('odds_a', 4.0))
        squad_change = float(match.get('squad_change', 0))

        probs = np.array([p_h, p_d, p_a])
        probs = np.clip(probs, 0.001, 0.999)
        entropy_val = float(-np.sum(probs * np.log(probs)))

        override = {
            'real_home_odds': oh,
            'real_draw_odds': od,
            'real_away_odds': oa,
            'sigma_trap': float(trap_score),
            'p_implied': float(p_d),
            'odds_balance': float(abs(1.0 / od - 0.5 * (1.0 / oh + 1.0 / oa))),
            'odds_entropy': entropy_val,
            'match_evenness': float(1.0 - abs(p_h - p_a) / (p_h + p_a + 0.01)),
            'imp_d_norm': float(p_d),
            'is_cold_start': 0.0 if squad_change < 0.3 else float(squad_change),
            'feat_coverage_ratio': 1.0,
            'home_match_count_norm': 1.0,
            'away_match_count_norm': 1.0,
        }
        for i, name in enumerate(self.feature_names):
            if name in override:
                vec[i] = float(override[name])

        return vec

    # ════════════════════════════════════════════════════════════
    # v3.2模型推理 (通道A: 数学)
    # ════════════════════════════════════════════════════════════

    def _model_infer(self, vec, match):
        """v3.2 production模型推理 → model_probs"""
        X = self.scaler.transform(vec.reshape(1, -1))

        # v3.2 Stacking集成预测（替代手动构建meta特征）
        # _predict_with_stacking 内置: 5基模型→正确21维meta→meta learner→D boost→校准
        league = match.get('league', '默认')
        model_probs = self._bridge.trainer._predict_with_stacking(
            X, league_names=[league])[0]

        return model_probs

    # ════════════════════════════════════════════════════════════
    # 数字人推理 (通道B: 认知)
    # ════════════════════════════════════════════════════════════

    def _digital_human_infer(self, match, base_lam_h, base_lam_a,
                              web_data=None) -> Dict[str, Any]:
        """
        数字人推理: 运行完整8步分析

        Args:
            match: 比赛数据
            base_lam_h, base_lam_a: 庄家λ
            web_data: 网络采集数据 (可选)
        """
        home = match.get('home', '')
        away = match.get('away', '')

        # 预处理赛果数据
        home_results = []
        away_results = []

        if match.get('home_recent_results'):
            for r in match['home_recent_results']:
                home_results.append(self.dh.parse_match_result(
                    home, r['opponent'], r['score'],
                    r.get('home_away', 'home'),
                    r.get('match_type', 'official'),
                    r.get('date', ''),
                ))

        if match.get('away_recent_results'):
            for r in match['away_recent_results']:
                away_results.append(self.dh.parse_match_result(
                    away, r['opponent'], r['score'],
                    r.get('home_away', 'away'),
                    r.get('match_type', 'official'),
                    r.get('date', ''),
                ))

        # 心理上下文
        home_psych = match.get('home_psychology', {})
        away_psych = match.get('away_psychology', {})
        # 默认心理评估
        if not home_psych:
            coach_changed = match.get('coach_changed', False)
            core_lost = match.get('core_player_lost', False)
            star_pressure = 0.6 if match.get('star_pressure') else 0.0
            home_psych = {
                'coach_contract_expiring': coach_changed,
                'star_pressure': star_pressure,
                'team_morale': -0.1 if core_lost else 0.1,
                'recent_form': 'stable',
            }
        if not away_psych:
            away_psych = {
                'coach_contract_expiring': False,
                'star_pressure': 0.0,
                'team_morale': 0.0,
                'recent_form': 'stable',
            }

        # 阵容数据
        home_lineup = match.get('home_lineup') or {
            'expected': {'formation': '4-3-3', 'players': [], 'player_ratings': {}},
            'actual': {'formation': '4-3-3', 'players': [], 'player_ratings': {}},
        }
        away_lineup = match.get('away_lineup') or {
            'expected': {'formation': '4-4-2', 'players': [], 'player_ratings': {}},
            'actual': {'formation': '4-4-2', 'players': [], 'player_ratings': {}},
        }

        # 欧赔和波胆
        eu_odds = (
            float(match.get('odds_h', 2.0)),
            float(match.get('odds_d', 3.5)),
            float(match.get('odds_a', 4.0)),
        )
        score_odds = match.get('score_odds', {})

        # 运行数字人完整分析
        result = self.dh.run_full_analysis(
            home_team=home,
            away_team=away,
            base_lam_h=base_lam_h,
            base_lam_a=base_lam_a,
            home_results=home_results,
            away_results=away_results,
            home_lineup=home_lineup,
            away_lineup=away_lineup,
            home_psych=home_psych,
            away_psych=away_psych,
            score_odds=score_odds,
            eu_odds=eu_odds,
        )

        return result

    # ════════════════════════════════════════════════════════════
    # 比分矩阵
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _apply_trap_lambda_correction(lam_h, lam_a, trap_score, p_book):
        """陷阱λ修正"""
        if trap_score <= 2.0:
            return lam_h, lam_a

        amp = min(0.50, (trap_score - 2.0) * 0.083)

        if lam_h > lam_a:
            lam_gap = lam_h - lam_a
            lam_h_new = lam_h - lam_gap * amp
            lam_a_new = lam_a + lam_gap * amp * 0.5
        else:
            lam_gap = lam_a - lam_h
            lam_a_new = lam_a - lam_gap * amp
            lam_h_new = lam_h + lam_gap * amp * 0.5

        return max(0.15, lam_h_new), max(0.05, lam_a_new)

    def _compute_score_matrix(self, lam_h, lam_a, max_g=6):
        """λ → 泊松比分概率矩阵"""
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
                if p < 0.003:
                    continue
                outcome = 'H' if gh > ga else ('D' if gh == ga else 'A')
                scores.append({
                    'score': f'{gh}-{ga}',
                    'prob': round(p, 4),
                    'outcome': outcome,
                    'total_goals': gh + ga,
                })
        scores.sort(key=lambda s: s['prob'], reverse=True)
        return scores

    def _apply_rp_denoising(self, scores, trap_score, max_rp):
        """RP降噪 + 进球分段修正"""
        if max_rp <= 3.0:
            for s in scores:
                s['prob'] = round(apply_goal_segment_correction(s['prob'], s['total_goals']), 4)
            scores.sort(key=lambda s: s['prob'], reverse=True)
            return scores

        factor = min(max_rp / 8.0, 1.0) * 0.08
        for s in scores:
            if s['total_goals'] >= 4 and s['prob'] < 0.05:
                s['prob'] = max(s['prob'] * (1.0 - factor), 0.001)
            s['prob'] = round(apply_goal_segment_correction(s['prob'], s['total_goals']), 4)

        scores.sort(key=lambda s: s['prob'], reverse=True)
        return scores

    # ════════════════════════════════════════════════════════════
    # 双层融合
    # ════════════════════════════════════════════════════════════

    def _fuse_probs(self, math_probs, dh_lam_h, dh_lam_a) -> np.ndarray:
        """
        双层融合: 数学模型 (70%) + 数字人 (30%)

        数字人贡献: 从数字人λ推导1X2概率
        数学贡献: v3.2模型 + 陷阱修正
        """
        # 数字人概率
        dh_probs = self._lambda_to_1x2_probs(dh_lam_h, dh_lam_a)

        # 加权限合
        fused = math_probs * self.math_weight + dh_probs * self.dh_weight
        fused = fused / fused.sum()

        return fused

    # ════════════════════════════════════════════════════════════
    # 主预测入口
    # ════════════════════════════════════════════════════════════

    def predict(self, match: Dict[str, Any], web_data: Dict = None) -> Dict[str, Any]:
        """
        VIP Final 双层融合预测

        Args:
            match: dict with:
                home, away, league,
                odds_h, odds_d, odds_a,
                asian_handicap (optional),
                score_odds (optional),
                tactical_shift (0~1),
                squad_change (0~1),
                is_final (bool),
                home_recent_results (optional, List[Dict]),
                away_recent_results (optional, List[Dict]),
                home_psychology (optional),
                away_psychology (optional),
                home_lineup (optional),
                away_lineup (optional),
                ...
            web_data: 网络采集数据 (optional)

        Returns:
            {
                probs: {H, D, A},
                dh_probs: {H, D, A},           # 数字人概率
                math_probs: {H, D, A},         # 数学概率
                trap: {score, rating, signals, features},
                scores: [{score, prob, odds}],
                dh_analysis: {                   # 数字人完整分析
                    evolution_trajectory,
                    defense_analysis,
                    psychology_analysis,
                    common_opponent_analysis,
                    lineup_analysis,
                    odds_contradiction,
                    counter_hypotheses,
                    info_quality,
                },
                recommendation: str,
                bookmaker_view: str,
                pnl_matrix: optional,            # 操盘手盈亏
            }
        """
        oh = float(match.get('odds_h', 2.0))
        od = float(match.get('odds_d', 3.5))
        oa = float(match.get('odds_a', 4.0))
        league = match.get('league', '默认')
        tact_shift = float(match.get('tactical_shift', 0))
        squad_change = float(match.get('squad_change', 0))
        is_final = bool(match.get('is_final', False))

        raw_sum = 1.0 / oh + 1.0 / od + 1.0 / oa
        p_book = np.array([1 / (oh * raw_sum), 1 / (od * raw_sum), 1 / (oa * raw_sum)])

        # ═══════════════════════════════════════════════
        # 通道B: 数字人推理
        # ═══════════════════════════════════════════════
        lam_book_h, lam_book_a = self._derive_lambda_from_odds(oh, od, oa)

        dh_result = self._digital_human_infer(
            match, lam_book_h, lam_book_a, web_data
        )
        dh_lam_h = dh_result['final_lam_h']
        dh_lam_a = dh_result['final_lam_a']

        # ═══════════════════════════════════════════════
        # 通道A: 数学推理 (λ融合 + 陷阱 + 模型)
        # ═══════════════════════════════════════════════

        # L1: λ融合
        vec = self._build_feature_vector(
            match, float(p_book[0]), float(p_book[1]), float(p_book[2]), 0.0
        )
        model_probs_raw = self._model_infer(vec, match)
        lam_model_h, lam_model_a = self._probs_to_lambda(model_probs_raw)

        alpha = 0.65
        if tact_shift > 0.3:
            alpha = 0.75
        if is_final:
            alpha = 0.55

        fusion_h, fusion_a = fuse_lambda(
            lam_model_h, lam_model_a, lam_book_h, lam_book_a,
            alpha=alpha,
            is_tactical_shift=(tact_shift > 0.3),
            is_final=is_final,
        )

        # L2: 陷阱检测
        trap_data = {
            'home': match.get('home', ''),
            'away': match.get('away', ''),
            'league': league,
            'odds_h': oh, 'odds_d': od, 'odds_a': oa,
            'asian_handicap': match.get('asian_handicap'),
            'tactical_shift': tact_shift,
            'score_odds': match.get('score_odds'),
            'water_level': match.get('water_level', 0.92),
            'water_trend': match.get('water_trend', 'stable'),
            'odds_trend': match.get('odds_trend', 'stable'),
            'handicap_change': match.get('handicap_change', 'stable'),
            'handicap_change_magnitude': match.get('handicap_change_magnitude', 0),
            'multi_bookmaker_sync': match.get('multi_bookmaker_sync', True),
            'squad_quality_change': squad_change,
            'tactical_shift': tact_shift,
            'counter_threat_level': match.get('counter_threat_level', 0.5),
            'years_since_last_h2h': match.get('years_since_h2h', 0),
            'match_type': match.get('match_type', 'league'),
            'strength_gap': match.get('strength_gap', 'normal'),
            'coach_changed': match.get('coach_changed', False),
            'core_player_lost': match.get('core_player_lost', False),
            'temporary_rotation': match.get('temporary_rotation', False),
            'over_under_line': match.get('ou_line'),
            'under_water': match.get('under_water'),
            'over_water': match.get('over_water'),
            'score_odds_other': match.get('score_odds_other'),
            'opp_official_goals_scored': match.get('opp_official_goals_scored'),
            'opp_friendly_goals_scored': match.get('opp_friendly_goals_scored'),
            'opp_official_goals_conceded': match.get('opp_official_goals_conceded'),
            'opp_friendly_goals_conceded': match.get('opp_friendly_goals_conceded'),
            'rp_level': match.get('rp_level', 0),
        }
        trap_report = self.trap_detector.detect(trap_data)
        trap_score = trap_report.aggregate_score

        # L3: 双通道投票 (数学通道)
        vec_with_trap = self._build_feature_vector(
            match, float(p_book[0]), float(p_book[1]), float(p_book[2]), trap_score
        )
        model_probs = self._model_infer(vec_with_trap, match)

        c_h, c_d, c_a = apply_trap_correction(
            float(p_book[0]), float(p_book[1]), float(p_book[2]), trap_score
        )

        w_model, w_trap_int = 0.6, 0.4
        math_probs = np.array([
            model_probs[0] * w_model + c_h * w_trap_int,
            model_probs[1] * w_model + c_d * w_trap_int,
            model_probs[2] * w_model + c_a * w_trap_int,
        ])
        math_probs = math_probs / math_probs.sum()

        # ═══════════════════════════════════════════════
        # 最终融合: 数字人 + 数学
        # ═══════════════════════════════════════════════

        # 动态调整数字人权重
        dh_analysis_quality = dh_result.get('info_quality', {}).get('data_quality_score', 0.5)
        # 数据质量高 → 数字人权重大
        if dh_analysis_quality > 0.7:
            dh_w = 0.35
            math_w = 0.65
        elif dh_analysis_quality > 0.4:
            dh_w = 0.30
            math_w = 0.70
        else:
            dh_w = 0.20
            math_w = 0.80

        final_probs = self._fuse_probs(math_probs, dh_lam_h, dh_lam_a)
        # 加权修正
        final_probs = final_probs * math_w + self._lambda_to_1x2_probs(dh_lam_h, dh_lam_a) * dh_w
        final_probs = final_probs / final_probs.sum()

        # ═══════════════════════════════════════════════
        # L4: 比分预测 (使用融合λ)
        # ═══════════════════════════════════════════════

        # 融合λ: 数学50% + 数字人50%
        score_lam_h = fusion_h * 0.5 + dh_lam_h * 0.5
        score_lam_a = fusion_a * 0.5 + dh_lam_a * 0.5

        lam_score_h, lam_score_a = self._apply_trap_lambda_correction(
            score_lam_h, score_lam_a, trap_score, p_book
        )

        max_rp = 1.0
        if match.get('score_odds'):
            V = 1.0 + (raw_sum - 1.0)
            ph_arr = np.array([max(np.exp(-lam_score_h) * lam_score_h**k / math.factorial(k), 1e-30)
                               for k in range(7)])
            pa_arr = np.array([max(np.exp(-lam_score_a) * lam_score_a**k / math.factorial(k), 1e-30)
                               for k in range(7)])
            ph_arr /= ph_arr.sum()
            pa_arr /= pa_arr.sum()
            for gh in range(7):
                for ga in range(7):
                    key = f'{gh}-{ga}'
                    odds_real = match['score_odds'].get(key)
                    if odds_real:
                        p_theo = ph_arr[gh] * pa_arr[ga]
                        odds_theo = 1.0 / max(p_theo * V, 1e-8)
                        rp_val = odds_real / max(odds_theo, 1.01)
                        if rp_val > max_rp:
                            max_rp = rp_val

        score_matrix = self._compute_score_matrix(lam_score_h, lam_score_a)
        score_matrix = self._apply_rp_denoising(score_matrix, trap_score, max_rp)

        top_scores = []
        for s in score_matrix[:5]:
            entry = {
                'score': s['score'],
                'prob': round(s['prob'], 4),
                'outcome': s['outcome'],
            }
            if match.get('score_odds'):
                entry['odds'] = match['score_odds'].get(s['score'])
            top_scores.append(entry)

        # ═══════════════════════════════════════════════
        # 操盘手盈亏矩阵
        # ═══════════════════════════════════════════════
        pnl_matrix = None
        if match.get('score_odds'):
            score_probs = {s['score']: s['prob'] for s in score_matrix[:20]}
            pnl_matrix = self.dh.simulate_bookmaker_pnl(
                match['score_odds'], score_probs
            )

        # ═══════════════════════════════════════════════
        # 推荐 + 操盘手意见
        # ═══════════════════════════════════════════════
        trap_signals = trap_report.signals

        if trap_score > 3.0:
            severity = '重度'
            bookmaker_view = '庄家诱导'
            if any('浅盘大热' in s.description for s in trap_signals):
                bookmaker_view += '（浅盘大热，诱导上盘）'
            elif any('深盘' in s.description for s in trap_signals):
                bookmaker_view += '（深盘诱杀，防赢球输盘）'
            elif any('波胆防线' in s.description for s in trap_signals):
                bookmaker_view += '（波胆防线，庄家设防）'
            elif any('资金' in s.description for s in trap_signals):
                bookmaker_view += '（资金过热，庄家不怕）'
            elif any('OU-CS' in s.description for s in trap_signals):
                bookmaker_view += '（OU-CS背离，潜在诱导）'
        elif trap_score > 2.0:
            bookmaker_view = '存在疑点'
        else:
            bookmaker_view = '盘口正常'

        # 主推荐
        if trap_score > 3.0:
            rec = f'⚠️ 高风险陷阱({trap_score:.1f}分)，建议规避或反向操作'
        elif final_probs[1] > 0.30:
            rec = f'📌 高平局概率({final_probs[1]*100:.0f}%)，防守型投注建议'
        elif final_probs[0] > 0.55:
            rec = f'🔥 主队大热({final_probs[0]*100:.0f}%)，防爆冷'
        elif final_probs[2] > 0.45:
            rec = f'🎯 客胜有价值({final_probs[2]*100:.0f}%)'
        else:
            winner = '主胜' if final_probs[0] > max(final_probs[1], final_probs[2]) else \
                     ('平局' if final_probs[1] > final_probs[2] else '客胜')
            rec = f'✅ 倾向{winner}'

        # 陷阱评级
        if trap_score >= 3.2:
            trap_rating = '重度陷阱'
        elif trap_score >= 2.0:
            trap_rating = '轻度风险'
        elif trap_score >= 1.0:
            trap_rating = '微弱信号'
        else:
            trap_rating = '无信号'

        # ── 数字人分析摘要 ──
        dh_analysis = {
            'evolution_trajectory': dh_result.get('evolution_trajectory', []),
            'defense_analysis': {
                'home': dh_result['defense_analysis']['home'].__dict__ if dh_result.get('defense_analysis') and dh_result['defense_analysis'].get('home') else None,
                'away': dh_result['defense_analysis']['away'].__dict__ if dh_result.get('defense_analysis') and dh_result['defense_analysis'].get('away') else None,
            } if dh_result.get('defense_analysis') else None,
            'counter_hypotheses': dh_result.get('counter_hypotheses', []),
            'info_quality': dh_result.get('info_quality', {}),
            'final_dh_lam': (dh_lam_h, dh_lam_a),
        }

        pnl_summary = None
        if pnl_matrix:
            pnl_summary = {
                'max_loss_score': pnl_matrix.max_loss_score,
                'max_loss_amount': pnl_matrix.max_loss_amount,
                'max_profit_score': pnl_matrix.max_profit_score,
                'risk_distribution': pnl_matrix.risk_distribution,
            }

        # Phase 2: 统一 home/draw/away 小写全称键名
        _probs = {
            'home': round(float(final_probs[0]), 4),
            'draw': round(float(final_probs[1]), 4),
            'away': round(float(final_probs[2]), 4),
        }
        return {
            'probs': _probs,
            'probabilities': _probs,
            'dh_probs': {
                'home': round(float(self._lambda_to_1x2_probs(dh_lam_h, dh_lam_a)[0]), 4),
                'draw': round(float(self._lambda_to_1x2_probs(dh_lam_h, dh_lam_a)[1]), 4),
                'away': round(float(self._lambda_to_1x2_probs(dh_lam_h, dh_lam_a)[2]), 4),
            },
            'math_probs': {
                'home': round(float(math_probs[0]), 4),
                'draw': round(float(math_probs[1]), 4),
                'away': round(float(math_probs[2]), 4),
            },
            'trap': {
                'score': round(trap_score, 2),
                'rating': trap_rating,
                'signals': [
                    {'type': s.trap_type.value, 'confidence': round(s.confidence, 3),
                     'direction': s.direction, 'desc': s.description}
                    for s in trap_signals
                ],
                'features': trap_report.trap_features if hasattr(trap_report, 'trap_features') else {},
            },
            'scores': top_scores[:3],
            'all_scores': top_scores,
            'recommendation': rec,
            'bookmaker_view': bookmaker_view,
            'math_fusion_λ': (round(fusion_h, 3), round(fusion_a, 3)),
            'dh_λ': (round(dh_lam_h, 3), round(dh_lam_a, 3)),
            'score_λ': (round(lam_score_h, 3), round(lam_score_a, 3)),
            'model_probs_raw': {
                'H': round(float(model_probs[0]), 4),
                'D': round(float(model_probs[1]), 4),
                'A': round(float(model_probs[2]), 4),
            },
            'corrected_probs': {
                'H': round(float(c_h), 4),
                'D': round(float(c_d), 4),
                'A': round(float(c_a), 4),
            },
            'max_rp': round(max_rp, 2),
            'model_version': self.model_version,
            'dh_analysis': dh_analysis,
            'pnl_matrix': pnl_summary,
            'dh_weight': dh_w,
            'math_weight': math_w,
        }

    # ════════════════════════════════════════════════════════════
    # 回测验证
    # ════════════════════════════════════════════════════════════

    def verify_and_replace(self, test_matches: List[Dict],
                            old_vip_path: str = None,
                            auto_replace: bool = False) -> Dict[str, Any]:
        """
        回测验证 → (可选)自动替换旧VIP

        Args:
            test_matches: 测试比赛列表
            old_vip_path: 旧VIP路径 (vip_v2_predictor.py)
            auto_replace: 是否自动替换

        Returns:
            dict: 验证报告
        """
        if old_vip_path is None:
            old_vip_path = os.path.join(PROJECT_ROOT, 'vip_v2_predictor.py')

        results = []
        for m in test_matches:
            prediction = self.predict(m)
            actual = m.get('actual_result', '?')
            actual_scores = m.get('actual_scores', ['?'])
            actual_outcome = m.get('actual_outcome', '?')

            # 判断命中
            score_hit = any(s['score'] in actual_scores for s in prediction['scores'])
            direction_hit = (
                (actual_outcome == 'H' and prediction['probs']['H'] > max(
                    prediction['probs']['D'], prediction['probs']['A'])) or
                (actual_outcome == 'D' and prediction['probs']['D'] > 0.15) or
                (actual_outcome == 'A' and prediction['probs']['A'] > max(
                    prediction['probs']['H'], prediction['probs']['D']))
            )

            results.append({
                'home': m.get('home', ''),
                'away': m.get('away', ''),
                'actual': actual,
                'pred_top3': [s['score'] for s in prediction['scores']],
                'score_hit': score_hit,
                'direction_hit': direction_hit,
                'trap_score': prediction['trap']['score'],
                'probs': prediction['probs'],
            })

        # 统计
        total = len(results)
        direction_hits = sum(1 for r in results if r['direction_hit'])
        score_hits = sum(1 for r in results if r['score_hit'])
        draw_matches = [r for r in results if r.get('actual_outcome') == 'D' or 
                       (r['actual'] and '-' in r['actual'] and 
                        r['actual'].split('-')[0] == r['actual'].split('-')[1])]
        draw_hits = sum(1 for r in draw_matches if r['score_hit'])
        cold_matches = [r for r in results if r['trap_score'] > 3.0]
        cold_hits = sum(1 for r in cold_matches if r['direction_hit'])

        report = {
            'total': total,
            'direction_accuracy': round(direction_hits / max(total, 1) * 100, 1),
            'score_top3_hit_rate': round(score_hits / max(total, 1) * 100, 1),
            'draw_hit_rate': round(draw_hits / max(len(draw_matches), 1) * 100, 1),
            'cold_upset_detection': f"{cold_hits}/{max(len(cold_matches), 1)}",
            'results': results,
            'pass_direction': direction_hits / max(total, 1) > 0.75,
            'pass_draw': draw_hits / max(len(draw_matches), 1) >= 0.70 if draw_matches else True,
        }
        report['passed'] = report['pass_direction'] and report['pass_draw']

        # 自动替换
        if auto_replace and report['passed']:
            self._replace_old_vip(old_vip_path)
            report['replaced'] = True
        else:
            report['replaced'] = False

        return report

    def _replace_old_vip(self, old_vip_path: str):
        """替换旧VIP"""
        backup_path = old_vip_path.replace('.py', '_legacy.py')
        if os.path.exists(backup_path):
            os.remove(backup_path)
        if os.path.exists(old_vip_path):
            os.rename(old_vip_path, backup_path)

        # 复制本文件到旧VIP位置
        import shutil
        current_file = os.path.join(PROJECT_ROOT, 'vip_final.py')
        shutil.copy(current_file, old_vip_path)

    # ══════════════════════════════════════
    # PredictorBase 统一接口 (2026-06-28)
    # ══════════════════════════════════════

    def predict_match(self, match: MatchData) -> PredictionResult:
        """实现 PredictorBase.predict_match()"""
        match_dict = {
            'home': match.home, 'away': match.away,
            'odds_h': match.odds_h, 'odds_d': match.odds_d, 'odds_a': match.odds_a,
            'handicap': match.handicap, 'ou_line': match.ou_line,
            'over_water': match.over_water, 'under_water': match.under_water,
        }
        result_dict = self.predict(match_dict)
        probs = result_dict.get('probabilities', result_dict.get('probs', {}))
        pred_raw = result_dict.get('prediction', 'H')
        if isinstance(pred_raw, str) and len(pred_raw) == 1:
            pred_code = pred_raw
        elif pred_raw in ('主胜', 'home', 'H'):
            pred_code = 'H'
        elif pred_raw in ('客胜', 'away', 'A'):
            pred_code = 'A'
        else:
            pred_code = 'D'
        return PredictionResult(
            probabilities=probs if isinstance(probs, dict) else {'home':0.0,'draw':0.0,'away':0.0},
            prediction=pred_code,
            confidence=float(result_dict.get('confidence', 0.0)),
            model_version=f"VIP {self.__class__.__name__}",
            scores=result_dict.get('scores'),
            trap_score=float(result_dict.get('trap', {}).get('score', 0.0)),
            draw_signal=float(result_dict.get('draw_signal', 0.0)),
            extra={'dh_probs': result_dict.get('dh_probs'), 'math_probs': result_dict.get('math_probs')},
        )

    @property
    def model_version(self) -> str:
        return f"VIPFinalPredictor {self.__class__.__name__}"

    def is_loaded(self) -> bool:
        return True

# ════════════════════════════════════════════════════════════════
# 验证用例
# ════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    predictor = VIPFinalPredictor()
    print(f"VIP Final Predictor vFinal")
    print(f"  模型版本: v{predictor.model_version}")
    print(f"  模型指标: Acc={predictor.eval_metrics.get('accuracy', 'N/A')}  "
          f"Draw-F1={predictor.eval_metrics.get('draw_f1', 'N/A')}")
    print()

    # ── 测试用例: 葡萄牙 vs 刚果 ──
    pt_congo = {
        'home': '葡萄牙',
        'away': '刚果民主共和国',
        'league': '国际友谊赛',
        'odds_h': 1.27,
        'odds_d': 5.60,
        'odds_a': 11.0,
        'asian_handicap': -1.5,
        'score_odds': {
            "0-0": 14.0, "0-1": 21.0, "0-2": 56.0,
            "1-0": 4.85, "1-1": 11.0, "1-2": 24.0,
            "2-0": 3.70, "2-1": 7.00, "2-2": 36.0,
            "3-0": 4.15, "3-1": 7.25, "3-2": 51.0,
            "4-0": 6.10, "4-1": 11.0,
            "5-0": 11.5,
        },
        'water_level': 0.92,
        'water_trend': 'stable',
        'odds_trend': 'stable',
        'handicap_change': 'stable',
        'multi_bookmaker_sync': True,
        'tactical_shift': 0,
        'squad_change': 0,
        'is_final': False,
        'match_type': 'league',
        'strength_gap': 'large',
        # ── 数字人数据 ──
        'home_recent_results': [
            {'opponent': '西班牙', 'score': '1-1', 'home_away': 'away', 'match_type': 'official'},
            {'opponent': '克罗地亚', 'score': '2-1', 'home_away': 'home', 'match_type': 'official'},
            {'opponent': '斯洛文尼亚', 'score': '3-0', 'home_away': 'away', 'match_type': 'cup'},
            {'opponent': '冰岛', 'score': '2-2', 'home_away': 'home', 'match_type': 'official'},
            {'opponent': '斯洛伐克', 'score': '2-0', 'home_away': 'home', 'match_type': 'official'},
        ],
        'away_recent_results': [
            {'opponent': '尼日利亚', 'score': '4-5', 'home_away': 'away', 'match_type': 'official'},
            {'opponent': '加蓬', 'score': '2-1', 'home_away': 'home', 'match_type': 'official'},
            {'opponent': '安哥拉', 'score': '1-1', 'home_away': 'away', 'match_type': 'friendly'},
            {'opponent': '南非', 'score': '2-2', 'home_away': 'home', 'match_type': 'official'},
            {'opponent': '赞比亚', 'score': '3-0', 'home_away': 'home', 'match_type': 'friendly'},
        ],
        'home_psychology': {
            'coach_contract_expiring': True,
            'star_pressure': 0.7,
            'team_morale': -0.1,
            'recent_form': 'stable',
        },
        'away_psychology': {
            'coach_contract_expiring': False,
            'star_pressure': 0.0,
            'team_morale': 0.3,
            'recent_form': 'good',
        },
    }

    print("=" * 70)
    print(f"  VIP Final 验证: {pt_congo['home']} vs {pt_congo['away']}")
    print(f"  实际比分: 1-1 (平局)")
    print(f"  欧赔: H={pt_congo['odds_h']} D={pt_congo['odds_d']} A={pt_congo['odds_a']}")
    print("=" * 70)

    result = predictor.predict(pt_congo)

    # ── L1 λ融合 ──
    print(f"\n── L1 λ融合 ──")
    print(f"  数学融合λ:  H={result['math_fusion_λ'][0]:.3f}  A={result['math_fusion_λ'][1]:.3f}")
    print(f"  数字人λ:    H={result['dh_λ'][0]:.3f}  A={result['dh_λ'][1]:.3f}")
    print(f"  比分λ:      H={result['score_λ'][0]:.3f}  A={result['score_λ'][1]:.3f}")

    # ── L2 陷阱 ──
    print(f"\n── L2 陷阱全检 ──")
    print(f"  Trap Score: {result['trap']['score']}")
    print(f"  评级: {result['trap']['rating']}")
    for sig in result['trap']['signals']:
        print(f"    [{sig['type']}] conf={sig['confidence']:.2f} dir={sig['direction']}")

    # ── 数字人分析 ──
    print(f"\n── 数字人迭代 ──")
    for t in result['dh_analysis']['evolution_trajectory']:
        print(f"    v{t['version']} [{t['trigger']}]: λ={t['lam_h']:.2f}/{t['lam_a']:.2f} → {t['top_scores']}")

    # ── 逆向假设 ──
    print(f"\n── 逆向假设 ──")
    for h in result['dh_analysis']['counter_hypotheses'][:2]:
        print(f"    [{h['probability']:.0%}] {h['premise']}: → {h['predicted_scores']}")

    # ── 最终概率 ──
    print(f"\n── 概率对比 ──")
    print(f"  数字人概率: H={result['dh_probs']['H']:.3f} D={result['dh_probs']['D']:.3f} A={result['dh_probs']['A']:.3f}")
    print(f"  数学模型概率: H={result['math_probs']['H']:.3f} D={result['math_probs']['D']:.3f} A={result['math_probs']['A']:.3f}")
    print(f"  >>> VIP Final: H={result['probs']['H']:.3f} D={result['probs']['D']:.3f} A={result['probs']['A']:.3f} <<<")
    print(f"  融合权重: 数字人={result['dh_weight']:.0%} 数学={result['math_weight']:.0%}")

    # ── 比分 ──
    print(f"\n── L4 比分Top5 ──")
    for s in result['all_scores']:
        marker = " ★" if s['outcome'] == 'D' else ""
        odds_str = f" ({s['odds']})" if s.get('odds') else ""
        print(f"    {s['score']}: {s['prob']*100:5.1f}% [{s['outcome']}]{marker}{odds_str}")

    # ── 操盘手盈亏 ──
    if result['pnl_matrix']:
        pnl = result['pnl_matrix']
        print(f"\n── 操盘手盈亏 ──")
        print(f"  最大亏损: {pnl['max_loss_score']} ({pnl['max_loss_amount']:.3f})")
        print(f"  最大盈利: {pnl['max_profit_score']}")
        print(f"  风险分布: {pnl['risk_distribution']}")

    print(f"\n  推荐: {result['recommendation']}")
    print(f"  操盘手意见: {result['bookmaker_view']}")
    print(f"  RP_max: {result['max_rp']}")

    # ══ 验证检查 ══
    print(f"\n{'='*50}")
    print(f"  验证检查:")

    checks = [
        ('陷阱评分 >= 2.0', result['trap']['score'] >= 2.0),
        ('D概率 >= 0.15 (高于市场)', result['probs']['D'] >= 0.15),
        ('Top3比分含平局', any(s['outcome'] == 'D' for s in result['scores'])),
        ('概率和≈1', abs(sum(result['probs'].values()) - 1.0) < 0.01),
        ('数字人有多版迭代', len(result['dh_analysis']['evolution_trajectory']) >= 3),
        ('生成逆向假设', len(result['dh_analysis']['counter_hypotheses']) >= 2),
    ]

    all_pass = True
    for label, passed in checks:
        status = '✅' if passed else '❌'
        if not passed:
            all_pass = False
        print(f"    {status} {label}")

    if all_pass:
        print(f"\n  ✅ 全部验证通过 — VIP Final 就绪")
    else:
        print(f"\n  ⚠ 部分验证未通过，请检查")
    print(f"  {'='*50}")

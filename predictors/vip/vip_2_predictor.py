"""
VIP-2 Predictor — 仅保留已验证组件，三层融合架构
====================================================

VIP-2 v2 架构:
  [入口] 赔率 + 战术上下文
    ↓
  [L1] λ融合: fuse_lambda(model_λ, book_λ, α=0.65)
    ↓
  [L2] 陷阱全检: BookmakerTrapDetector.detect()
         → 16引擎 + W_ambiguity + E15 + E16 + hidden_strength
         → Score_trap
    ↓
  [L3] 双通道投票:
       通道A: v3.2 production模型推理 (权重0.6)
       通道B: 陷阱修正概率 apply_trap_correction() (权重0.4)
         → 最终 H/D/A
    ↓
  [L4] 比分: 泊松矩阵 + RP降噪 + 分段修正 → Top3
    ↓
  [输出] 概率 + 比分推荐 + 陷阱报告 + 操盘手意见

已验证组件:
  | v3.2模型 | production.joblib | Acc=59.20%, 12场75% | ✅
  | v2.1模板 | analysis_template_v1.md | 12场83%方向准确率 | ✅ 分析框架
  | λ融合 | lambda_fusion.py | 葡萄牙λ_A抬升0.70→1.00命中 | ✅
  | 陷阱16引擎 | bookmaker_trap_detector.py | 葡萄牙4.1分, 西班牙6.2分 | ✅
  | 陷阱→概率桥 | trap_probability_bridge.py | 葡萄牙H 59.5%→53.8% | ✅
  | 隐藏实力 | check_hidden_strength() | 西班牙ratio=3.0命中0-0 | ✅
  | 反波胆E16 | compute_anti_cs_features() | 锁盘检测命中 | ✅
  | OU-CS背离E15 | detect_ou_cs_divergence() | 验证有效 | ✅
  | 赔率两面性 | compute_w_ambiguity() | 矛盾信号降权 | ✅
  | RP风控 | odds_inverse_calibrator.py | 比分降噪 | ✅
  | 进球分段 | apply_goal_segment_correction | r01/r23/r4+ | ✅

已删除组件:
  | v3.3 独立重训 | Acc=61.08%但Draw-F1→0.14, 不可用 | ❌
  | v3.4 Draw极端修复 | DW=2.0时Acc→40%, 不可用 | ❌
  | v3.2 OddsExpert solo | Draw-F1=0.03, 拖累融合 | ❌
  | quick_diagnose() 简版 | 不传战术上下文, 不如detect() | ❌
"""

import os
import sys
import math
import logging
import numpy as np

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

from lambda_fusion import fuse_lambda
from trap_probability_bridge import apply_trap_correction
from odds_inverse_calibrator import apply_goal_segment_correction
from agents.model_bridge import ModelBridge

class VIP2Predictor:
    """
    VIP-2 v2 三层融合预测器

    仅保留12场批量回测+葡萄牙实时验证通过的组件。
    去掉v3.3/v3.4/OddsExpert solo/quick_diagnose。
    """

    def __init__(self, model_path=None):
        # ── 通过 ModelBridge 加载 v3.2 生产模型（避免重复加载5MB）──
        bridge = ModelBridge(model_path) if model_path else ModelBridge()
        self._bridge = bridge
        trainer = bridge.trainer
        if trainer is None:
            raise RuntimeError("VIP2Predictor: ModelBridge 回退到轻量模式，无法初始化")
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

        self._ready = True

    # ════════════════════════════════════════════════════════════════
    # 内部工具: λ ↔ 概率 互转
    # ════════════════════════════════════════════════════════════════

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

    # ════════════════════════════════════════════════════════════════
    # 72维特征向量构建 → v3.2模型输入
    # ════════════════════════════════════════════════════════════════

    def _build_feature_vector(self, match, p_h, p_d, p_a, trap_score):
        """填充72维特征向量"""
        n_feats = len(self.feature_names)
        vec = np.zeros(n_feats, dtype=np.float32)

        # 先填默认值
        for i, name in enumerate(self.feature_names):
            if name in self.defaults:
                vec[i] = float(self.defaults[name])

        oh = float(match.get('odds_h', 2.0))
        od = float(match.get('odds_d', 3.5))
        oa = float(match.get('odds_a', 4.0))
        squad_change = float(match.get('squad_change', 0))

        # 填充可推导特征
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

    # ════════════════════════════════════════════════════════════════
    # v3.2模型推理 (通道A)
    # ════════════════════════════════════════════════════════════════

    def _model_infer(self, vec, match):
        """v3.2 production模型推理 → model_probs"""
        X = self.scaler.transform(vec.reshape(1, -1))

        # v3.2 Stacking集成预测（替代手动构建meta特征）
        # _predict_with_stacking 内置: 5基模型→正确21维meta→meta learner→D boost→校准
        league = match.get('league', '默认')
        model_probs = self._bridge.trainer._predict_with_stacking(
            X, league_names=[league])[0]

        return model_probs

    # ════════════════════════════════════════════════════════════════
    # L4: 比分矩阵 + λ陷阱修正 + RP降噪 + 分段修正
    # ════════════════════════════════════════════════════════════════

    @staticmethod
    def _apply_trap_lambda_correction(lam_h, lam_a, trap_score, p_book):
        """
        陷阱λ修正: 高中阱分时适度收敛λ差距，使比分预测更保守

        高陷阱分 → 庄家在诱导热门方向 → 热门方λ应下压，冷门方λ应抬升
        """
        if trap_score <= 2.0:
            return lam_h, lam_a

        # 陷阱分越高，修正力度越大
        # max修正: trap>=8时，λ差压缩到原来的50%
        amp = min(0.50, (trap_score - 2.0) * 0.083)

        # 判断热门方向
        if lam_h > lam_a:
            # 主队热门，压缩主队λ，适当抬升客队λ
            lam_gap = lam_h - lam_a
            lam_h_new = lam_h - lam_gap * amp
            lam_a_new = lam_a + lam_gap * amp * 0.5  # 客队抬升较温和
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
            # 常规盘, 仅应用分段修正
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

    def _apply_market_goal_weight(self, scores, match):
        """让球+大小球→比分概率加权修正"""
        ou_line = match.get('ou_line')
        over_w = match.get('over_water')
        handicap = match.get('asian_handicap')
        
        if not ou_line or not over_w:
            return scores  # 无数据，跳过
        
        try:
            from predictors.market_goal_predictor import MarketGoalPredictor
            mgp = MarketGoalPredictor()
            h_g, a_g, _ = mgp.predict(
                handicap_line=handicap or 0,
                ou_line=ou_line, over_water=over_w,
                under_water=match.get('under_water', 2.05)
            )
            expected_total = h_g + a_g
            
            for s in scores:
                tg = s.get('total_goals', 0)
                deviation = abs(tg - expected_total)
                # 偏离市场预期越远，权重越低
                if deviation <= 0.5:
                    s['prob'] *= 1.05   # 吻合→小幅加成
                elif deviation <= 1.5:
                    s['prob'] *= 0.95   # 轻微偏离
                elif deviation <= 2.5:
                    s['prob'] *= 0.80   # 中度偏离
                else:
                    s['prob'] *= 0.60   # 严重偏离
        except Exception as e:
            logger.warning("VIP比重偏离修正失败: %s", e)
        
        # 重新归一化
        total = sum(s['prob'] for s in scores)
        if total > 0:
            for s in scores:
                s['prob'] = round(s['prob'] / total, 4)
        scores.sort(key=lambda s: s['prob'], reverse=True)
        return scores

    # ════════════════════════════════════════════════════════════════
    # 主预测入口
    # ════════════════════════════════════════════════════════════════

    def predict(self, match):
        """
        VIP-2 v2 三层融合预测

        Args:
            match: dict with keys:
                home, away, league,
                odds_h, odds_d, odds_a,
                asian_handicap (optional),
                score_odds (optional),
                tactical_shift (0~1),
                squad_change (0~1),
                is_final (bool),
                water_level, water_trend, odds_trend, handicap_change (optional),
                counter_threat_level, years_since_h2h, match_type, strength_gap,
                ou_line, under_water, over_water, score_odds_other,
                opp_official_goals_scored, opp_friendly_goals_scored,
                opp_official_goals_conceded, opp_friendly_goals_conceded,
                coach_changed, core_player_lost, temporary_rotation,

        Returns:
            {
                probs: {H, D, A},
                trap: {score, rating, signals, features},
                scores: [{score, prob, odds}],
                recommendation: str,
                bookmaker_view: str,
                model_λ, book_λ, fusion_λ,
                model_probs_raw, corrected_probs,
            }
        """
        # ── v5.0: 自动注入球队上下文 (TeamDataCollector) ──
        home_team = match.get('home', '')
        away_team = match.get('away', '')
        team_context = None
        
        if home_team and away_team and match.get('auto_context', True):
            try:
                from data_collector.team_data_collector import TeamDataCollector
                collector = TeamDataCollector()
                team_context = collector.get_match_context(home_team, away_team)
                
                # 自动注入 squad_change (基于伤病/阵容变化)
                if match.get('squad_change', 0) == 0 and team_context.get('all_fresh'):
                    home_inj = team_context['home'].get('injuries', [])
                    away_inj = team_context['away'].get('injuries', [])
                    core_lost = sum(1 for i in home_inj + away_inj if i.get('caps', 0) > 30)
                    if core_lost > 0:
                        match['squad_change'] = min(0.3 + core_lost * 0.15, 0.9)
                        match['core_player_lost'] = core_lost
                
                # 自动注入 tactical_shift (基于教练变动)
                if match.get('tactical_shift', 0) == 0 and team_context.get('all_fresh'):
                    home_coach = team_context['home'].get('coach_news', [])
                    away_coach = team_context['away'].get('coach_news', [])
                    coach_change_signals = sum(1 for n in home_coach + away_coach 
                        if any(kw in n for kw in ['换帅', '下课', '新帅', '新教练']))
                    if coach_change_signals > 0:
                        match['tactical_shift'] = min(0.2 + coach_change_signals * 0.2, 0.8)
                
                # 注入阵型信息
                if team_context['home'].get('formation'):
                    match['home_formation'] = team_context['home']['formation']
                if team_context['away'].get('formation'):
                    match['away_formation'] = team_context['away']['formation']
                    
            except Exception as e:
                logger.debug(f"[VIP-2] TeamDataCollector 注入失败: {e}")
        
        oh = float(match.get('odds_h', 2.0))
        od = float(match.get('odds_d', 3.5))
        oa = float(match.get('odds_a', 4.0))
        league = match.get('league', '默认')
        tact_shift = float(match.get('tactical_shift', 0))
        squad_change = float(match.get('squad_change', 0))
        is_final = bool(match.get('is_final', False))

        # p_book 从赔率比例法直接计算
        raw_sum = 1.0 / oh + 1.0 / od + 1.0 / oa
        p_book = np.array([1 / (oh * raw_sum), 1 / (od * raw_sum), 1 / (oa * raw_sum)])

        # ═══════════════════════════════════════════════════════
        # L1: λ融合 — 模型λ + 庄家λ
        # ═══════════════════════════════════════════════════════
        lam_book_h, lam_book_a = self._derive_lambda_from_odds(oh, od, oa)

        # 先用简单模型获取模型λ (后续用于融合)
        # 先跑一遍模型推理获取模型λ
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

        # ═══════════════════════════════════════════════════════
        # L2: 陷阱全检 — 16引擎 + W_ambiguity + E15 + E16 + hidden_strength
        # ═══════════════════════════════════════════════════════
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
            # ── 战术上下文 ──
            'squad_quality_change': squad_change,
            'tactical_shift': tact_shift,
            'counter_threat_level': match.get('counter_threat_level', 0.5),
            'years_since_last_h2h': match.get('years_since_h2h', 0),
            'match_type': match.get('match_type', 'league'),
            'strength_gap': match.get('strength_gap', 'normal'),
            'coach_changed': match.get('coach_changed', False),
            'core_player_lost': match.get('core_player_lost', False),
            'temporary_rotation': match.get('temporary_rotation', False),
            # ── v3.1: 大小球 ──
            'over_under_line': match.get('ou_line'),
            'under_water': match.get('under_water'),
            'over_water': match.get('over_water'),
            'score_odds_other': match.get('score_odds_other'),
            # ── v3.1: 隐藏实力 ──
            'opp_official_goals_scored': match.get('opp_official_goals_scored'),
            'opp_friendly_goals_scored': match.get('opp_friendly_goals_scored'),
            'opp_official_goals_conceded': match.get('opp_official_goals_conceded'),
            'opp_friendly_goals_conceded': match.get('opp_friendly_goals_conceded'),
            'rp_level': match.get('rp_level', 0),
        }
        trap_report = self.trap_detector.detect(trap_data)
        trap_score = trap_report.aggregate_score

        # ═══════════════════════════════════════════════════════
        # L3: 双通道投票
        # ═══════════════════════════════════════════════════════

        # 通道A: v3.2模型推理 (用陷阱分数重新构建特征)
        vec_with_trap = self._build_feature_vector(
            match, float(p_book[0]), float(p_book[1]), float(p_book[2]), trap_score
        )
        model_probs = self._model_infer(vec_with_trap, match)

        # 通道B: 陷阱修正概率
        c_h, c_d, c_a = apply_trap_correction(
            float(p_book[0]), float(p_book[1]), float(p_book[2]), trap_score
        )

        # 双通道加权投票: 模型×0.6 + 陷阱修正×0.4
        w_model, w_trap = 0.6, 0.4
        final_probs = np.array([
            model_probs[0] * w_model + c_h * w_trap,
            model_probs[1] * w_model + c_d * w_trap,
            model_probs[2] * w_model + c_a * w_trap,
        ])
        final_probs = final_probs / final_probs.sum()

        # ═══════════════════════════════════════════════════════
        # L4: 比分: λ陷阱修正 + 泊松矩阵 + RP降噪 + 分段修正 → Top3
        # ═══════════════════════════════════════════════════════
        # 陷阱λ修正: 高中阱分时压缩λ差距, 使比分预测更保守
        lam_score_h, lam_score_a = self._apply_trap_lambda_correction(
            fusion_h, fusion_a, trap_score, p_book
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
        score_matrix = self._apply_market_goal_weight(score_matrix, match)
        score_matrix = self._apply_rp_denoising(score_matrix, trap_score, max_rp)

        # 比分添加赔率信息
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

        # ═══════════════════════════════════════════════════════
        # 推荐 + 操盘手意见
        # ═══════════════════════════════════════════════════════
        rec = ''
        bookmaker_view = ''

        # 操盘手意见层次
        trap_signals = trap_report.signals
        trap_types_detected = [s.trap_type.value for s in trap_signals]

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
            severity = '轻度'
            bookmaker_view = '存在疑点'
        else:
            severity = '安全'
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

        return {
            'probs': {
                'H': round(float(final_probs[0]), 4),
                'D': round(float(final_probs[1]), 4),
                'A': round(float(final_probs[2]), 4),
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
            'model_λ': (round(lam_model_h, 3), round(lam_model_a, 3)),
            'book_λ': (round(lam_book_h, 3), round(lam_book_a, 3)),
            'fusion_λ': (round(fusion_h, 3), round(fusion_a, 3)),
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
        }

# ════════════════════════════════════════════════════════════════
# 验证: 葡萄牙 1-1 刚果 (12场批量回测验证赛例)
# ════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    predictor = VIP2Predictor()
    print(f"VIP-2 Predictor v{predictor.model_version}")
    print(f"  模型指标: Acc={predictor.eval_metrics.get('accuracy', 'N/A')}  "
          f"Draw-F1={predictor.eval_metrics.get('draw_f1', 'N/A')}")
    print()

    # ── 葡萄牙 vs 刚果民主共和国 (国际友谊赛, 2026-06-09) ──
    pt_congo = {
        'home': '葡萄牙',
        'away': '刚果民主共和国',
        'league': '国际友谊赛',
        'odds_h': 1.27,
        'odds_d': 5.60,
        'odds_a': 11.0,
        'asian_handicap': -1.5,
        'score_odds': {
            "0-0": 14.0, "0-1": 21.0, "0-2": 56.0, "0-3": 190,
            "1-0": 4.85, "1-1": 11.0, "1-2": 24.0, "1-3": 95.0,
            "2-0": 3.70, "2-1": 7.00, "2-2": 36.0, "2-3": 95.0,
            "3-0": 4.15, "3-1": 7.25, "3-2": 51.0, "3-3": 180,
            "4-0": 6.10, "4-1": 11.0, "4-2": 81.0,
            "5-0": 11.5,
        },
        'water_level': 0.92,
        'water_trend': 'stable',
        'odds_trend': 'stable',
        'handicap_change': 'stable',
        'handicap_change_magnitude': 0,
        'multi_bookmaker_sync': True,
        'tactical_shift': 0,
        'squad_change': 0,
        'is_final': False,
        'match_type': 'league',
        'strength_gap': 'large',
        'years_since_h2h': 0,
    }

    print("=" * 65)
    print(f"  验证: {pt_congo['home']} vs {pt_congo['away']}")
    print(f"  实际比分: 1-1 (平局)")
    print(f"  欧赔: H={pt_congo['odds_h']} D={pt_congo['odds_d']} A={pt_congo['odds_a']}")
    print(f"  亚盘: {pt_congo['asian_handicap']}")
    print("=" * 65)

    result = predictor.predict(pt_congo)

    print(f"\n  ── L1  λ融合 ──")
    print(f"  庄家λ:  H={result['book_λ'][0]:.3f}  A={result['book_λ'][1]:.3f}")
    print(f"  模型λ:  H={result['model_λ'][0]:.3f}  A={result['model_λ'][1]:.3f}")
    print(f"  融合λ:  H={result['fusion_λ'][0]:.3f}  A={result['fusion_λ'][1]:.3f}")

    print(f"\n  ── L2  陷阱全检 ──")
    print(f"  Trap Score: {result['trap']['score']}")
    print(f"  评级: {result['trap']['rating']}")
    print(f"  检测信号 {len(result['trap']['signals'])} 个:")
    for sig in result['trap']['signals']:
        print(f"    [{sig['type']}] conf={sig['confidence']:.2f} dir={sig['direction']}")
        if 'desc' in sig:
            print(f"      {sig['desc'][:80]}")

    print(f"\n  ── L3  双通道投票 ──")
    print(f"  通道A(模型):  H={result['model_probs_raw']['H']:.3f}  "
          f"D={result['model_probs_raw']['D']:.3f}  "
          f"A={result['model_probs_raw']['A']:.3f}")
    print(f"  通道B(陷阱修正):  H={result['corrected_probs']['H']:.3f}  "
          f"D={result['corrected_probs']['D']:.3f}  "
          f"A={result['corrected_probs']['A']:.3f}")

    print(f"\n  ── 最终概率 (模型×0.6 + 陷阱修正×0.4) ──")
    print(f"  >>> VIP-2: H={result['probs']['H']:.3f} "
          f"D={result['probs']['D']:.3f} "
          f"A={result['probs']['A']:.3f} <<<")

    print(f"\n  ── L4  比分Top5 (λ陷阱修正+泊松+RP降噪+分段修正) ──")
    print(f"  比分λ:  H={result['score_λ'][0]:.3f}  A={result['score_λ'][1]:.3f}")
    for s in result['all_scores']:
        marker = " ★" if s['outcome'] == 'D' else ""
        odds_str = f" ({s['odds']})" if s.get('odds') else ""
        print(f"    {s['score']}: {s['prob']*100:5.1f}% [{s['outcome']}]{marker}{odds_str}")

    print(f"\n  推荐: {result['recommendation']}")
    print(f"  操盘手意见: {result['bookmaker_view']}")
    print(f"  RP_max: {result['max_rp']}")

    # ══ 验证检查 ══
    print(f"\n  {'='*50}")
    print(f"  验证检查:")

    checks = []
    # 1. 陷阱评分 > 3.2 (重度)
    checks.append(('陷阱评分 > 3.2 (重度)', result['trap']['score'] > 3.2))
    # 2. D ≥ 18% (高于市场16.9%)
    checks.append(('D ≥ 18% (高于市场16.9%)', result['probs']['D'] >= 0.18))
    # 3. Top3比分推荐含1-1
    top3_scores = [s['score'] for s in result['scores']]
    checks.append(('Top3比分推荐含1-1', '1-1' in top3_scores))
    # 4. 操盘手意见含"庄家诱导"
    checks.append(('操盘手意见含"庄家诱导"', '庄家诱导' in result['bookmaker_view']))

    all_pass = True
    for label, passed in checks:
        status = '✅' if passed else '❌'
        if not passed:
            all_pass = False
        print(f"    {status} {label}")

    # 5. 概率范围检查
    total = result['probs']['H'] + result['probs']['D'] + result['probs']['A']
    prob_range_ok = abs(total - 1.0) < 0.01
    checks.append(('概率和=1', prob_range_ok))
    print(f"    {'✅' if prob_range_ok else '❌'} 概率和={total:.4f}")

    if all_pass and prob_range_ok:
        print(f"\n  ✅ 全部验证通过 — VIP-2 Predictor 就绪")
    else:
        print(f"\n  ⚠ 部分验证未通过，请检查")
    print(f"  {'='*50}")

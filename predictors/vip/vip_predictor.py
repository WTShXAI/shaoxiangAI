"""
VIPPredictor — 整合所有最佳调参的VIP预测器
============================================

六层流水线:
  L1: λ融合 (模型λ + 庄家λ)
  L2: 陷阱检测 (16引擎 v3.1)
  L3: 陷阱→概率桥 (Score→ΔP 校正)
  L4: v3.2生产模型推理
  L5: 加权融合 (model×0.65 + corrected×0.35)
  L6: RP降噪 + 比分输出 (RP分段+进球分段修正)
"""

import os
import sys
import math
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from agents.model_bridge import ModelBridge

from lambda_fusion import fuse_lambda
from trap_probability_bridge import apply_trap_correction
from stacking_vector import build_stacking_vector
from odds_inverse_calibrator import apply_goal_segment_correction
from agents.model_bridge import ModelBridge


class VIPPredictor:
    """
    六层流水线VIP预测器

    初始化时加载v3.2生产模型(Acc=59.20%, Draw-F1=0.504)和陷阱检测器(16引擎)
    """

    def __init__(self):
        # ── 通过 ModelBridge 加载 v3.2 生产模型（避免重复加载5MB）──
        bridge = ModelBridge()
        self._bridge = bridge
        trainer = bridge.trainer
        if trainer is None:
            raise RuntimeError("VIPPredictor: ModelBridge 回退到轻量模式，无法初始化")
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

        # ── 初始化陷阱检测器 ──
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
    # 特征向量构建 (72维 → v3.2模型输入)
    # ════════════════════════════════════════════════════════════════

    def _build_feature_vector(self, match, p_h, p_d, p_a, trap_score):
        """填充72维特征向量: 默认值 + 可推导值"""
        n_feats = len(self.feature_names)
        vec = np.zeros(n_feats, dtype=np.float32)

        # 先填默认值
        for i, name in enumerate(self.feature_names):
            if name in self.defaults:
                vec[i] = float(self.defaults[name])

        oh = float(match.get('odds_h', 2.0))
        od = float(match.get('odds_d', 3.5))
        oa = float(match.get('odds_a', 4.0))
        tact_shift = float(match.get('tactical_shift', 0))
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
    # 比分矩阵 & RP降噪
    # ════════════════════════════════════════════════════════════════

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
                if p < 0.005:
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
        """RP降噪: 对高分险比赛下调冷门比分概率, 上调常规比分"""
        if max_rp <= 3.0:
            return scores  # 常规盘, 不做额外修正

        factor = min(max_rp / 8.0, 1.0) * 0.08  # 最多下调8%
        for s in scores:
            if s['total_goals'] >= 4 and s['prob'] < 0.05:
                s['prob'] = max(s['prob'] * (1.0 - factor), 0.001)
            s['prob'] = round(apply_goal_segment_correction(s['prob'], s['total_goals']), 4)

        scores.sort(key=lambda s: s['prob'], reverse=True)
        return scores

    # ════════════════════════════════════════════════════════════════
    # 主预测入口
    # ════════════════════════════════════════════════════════════════

    def predict(self, match):
        """
        六层流水线预测

        Args:
            match: dict with keys:
                home, away, league,
                odds_h, odds_d, odds_a,
                asian_handicap (optional),
                score_odds (optional),
                tactical_shift (0~1),
                squad_change (0~1),
                is_final (bool),
                water_level, water_trend, odds_trend, handicap_change (optional)

        Returns:
            {
                'H': float, 'D': float, 'A': float,
                'trap_report': TrapReport,
                'top3_scores': [{'score','prob','outcome'},...],
                'recommendation': str,
            }
        """
        oh = float(match.get('odds_h', 2.0))
        od = float(match.get('odds_d', 3.5))
        oa = float(match.get('odds_a', 4.0))
        league = match.get('league', '默认')
        tact_shift = float(match.get('tactical_shift', 0))
        squad_change = float(match.get('squad_change', 0))
        is_final = bool(match.get('is_final', False))

        # ═══════════════════════════════════════════════════════
        # L1: λ融合 — 模型λ + 庄家λ
        # ═══════════════════════════════════════════════════════
        lam_book_h, lam_book_a = self._derive_lambda_from_odds(oh, od, oa)
        # p_book 从赔率比例法直接计算 (不经过λ→概率再转换)
        raw_sum = 1.0 / oh + 1.0 / od + 1.0 / oa
        p_book = np.array([1 / (oh * raw_sum), 1 / (od * raw_sum), 1 / (oa * raw_sum)])

        # ═══════════════════════════════════════════════════════
        # L2: 陷阱检测 — 16引擎 v3.1
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
        }
        trap_report = self.trap_detector.detect(trap_data)
        trap_score = trap_report.aggregate_score

        # ═══════════════════════════════════════════════════════
        # L4: v3.2生产模型推理 → model_probs
        # ═══════════════════════════════════════════════════════
        vec = self._build_feature_vector(
            match, float(p_book[0]), float(p_book[1]), float(p_book[2]), trap_score
        )
        X = self.scaler.transform(vec.reshape(1, -1))

        # v3.2 Stacking集成预测（替代手动构建meta特征）
        # _predict_with_stacking 内置: 5基模型→正确21维meta→meta learner→D boost→校准
        # 不再手工拼3子模型+重复填充垃圾特征
        model_probs = self._bridge.trainer._predict_with_stacking(
            X, league_names=[league])[0]

        # L1 (续): λ model → 融合
        lam_model_h, lam_model_a = self._probs_to_lambda(model_probs)

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
        # L3: 陷阱→概率桥 — Score→ΔP校正
        # ═══════════════════════════════════════════════════════
        corrected_probs = apply_trap_correction(
            float(p_book[0]), float(p_book[1]), float(p_book[2]), trap_score
        )

        # ═══════════════════════════════════════════════════════
        # L5: 加权融合 = model×0.65 + corrected×0.35
        # ═══════════════════════════════════════════════════════
        w_model = 0.65
        w_corrected = 0.35
        final_probs = np.array([
            model_probs[0] * w_model + corrected_probs[0] * w_corrected,
            model_probs[1] * w_model + corrected_probs[1] * w_corrected,
            model_probs[2] * w_model + corrected_probs[2] * w_corrected,
        ])
        final_probs = final_probs / final_probs.sum()

        # ═══════════════════════════════════════════════════════
        # L6: RP降噪 + 比分输出
        # ═══════════════════════════════════════════════════════
        max_rp = 1.0
        raw_rp_map = {}
        if match.get('score_odds'):
            lam_final_h, lam_final_a = fusion_h, fusion_a
            raw_sum = 1.0 / oh + 1.0 / od + 1.0 / oa
            V = 1.0 + (raw_sum - 1.0)
            ph_arr = np.array([max(np.exp(-lam_final_h) * lam_final_h**k / math.factorial(k), 1e-30)
                               for k in range(7)])
            pa_arr = np.array([max(np.exp(-lam_final_a) * lam_final_a**k / math.factorial(k), 1e-30)
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
                        raw_rp_map[key] = rp_val
                        if rp_val > max_rp:
                            max_rp = rp_val

        score_matrix = self._compute_score_matrix(fusion_h, fusion_a)
        score_matrix = self._apply_rp_denoising(score_matrix, trap_score, max_rp)

        # 推荐
        top_score = score_matrix[0] if score_matrix else None
        rec = ''
        if trap_score > 3.0:
            rec = '⚠️ 高风险陷阱，建议规避或反向操作'
        elif final_probs[1] > 0.30:
            rec = '📌 高平局概率，防守型投注建议'
        elif final_probs[0] > 0.55:
            rec = '🔥 主队大热，防爆冷'
        elif final_probs[2] > 0.45:
            rec = '🎯 客胜有价值'
        else:
            winner = '主胜' if final_probs[0] > max(final_probs[1], final_probs[2]) else \
                     ('平局' if final_probs[1] > final_probs[2] else '客胜')
            rec = f'✅ 倾向{winner}'

        return {
            'H': round(float(final_probs[0]), 4),
            'D': round(float(final_probs[1]), 4),
            'A': round(float(final_probs[2]), 4),
            'trap_report': trap_report,
            'trap_score': round(trap_score, 2),
            'max_rp': round(max_rp, 2),
            'fusion_λ': (round(fusion_h, 3), round(fusion_a, 3)),
            'model_λ': (round(lam_model_h, 3), round(lam_model_a, 3)),
            'book_λ': (round(lam_book_h, 3), round(lam_book_a, 3)),
            'model_probs_raw': {
                'H': round(float(model_probs[0]), 4),
                'D': round(float(model_probs[1]), 4),
                'A': round(float(model_probs[2]), 4),
            },
            'corrected_probs': {
                'H': round(float(corrected_probs[0]), 4),
                'D': round(float(corrected_probs[1]), 4),
                'A': round(float(corrected_probs[2]), 4),
            },
            'top3_scores': score_matrix[:3],
            'top10_scores': score_matrix[:10],
            'recommendation': rec,
            'trap_signals': [
                {'type': s.trap_type.value, 'confidence': s.confidence,
                 'direction': s.direction, 'desc': s.description}
                for s in trap_report.signals
            ] if trap_report.signals else [],
        }


if __name__ == '__main__':
    # ── 验证1: 葡萄牙1-1刚果 真实数据 ──
    predictor = VIPPredictor()
    print(f"VIP Predictor v{predictor.model_version}")
    print(f"  模型指标: Acc={predictor.eval_metrics.get('accuracy', 'N/A')}  "
          f"Draw-F1={predictor.eval_metrics.get('draw_f1', 'N/A')}")
    print()

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

    print(f"\n  ── L2  陷阱检测 ──")
    print(f"  Trap Score: {result['trap_score']}")
    print(f"  检测信号 {len(result['trap_signals'])} 个:")
    for sig in result['trap_signals']:
        print(f"    [{sig['type']}] conf={sig['confidence']:.2f} dir={sig['direction']}")
        print(f"      {sig['desc'][:80]}")

    print(f"\n  ── L3 陷阱桥 + L4 模型推理 ──")
    print(f"  模型预测:   H={result['model_probs_raw']['H']:.3f}  "
          f"D={result['model_probs_raw']['D']:.3f}  "
          f"A={result['model_probs_raw']['A']:.3f}")
    print(f"  陷阱修正:   H={result['corrected_probs']['H']:.3f}  "
          f"D={result['corrected_probs']['D']:.3f}  "
          f"A={result['corrected_probs']['A']:.3f}")

    print(f"\n  ── L5 最终概率 ──")
    print(f"  >>> VIP预测: H={result['H']:.3f} D={result['D']:.3f} A={result['A']:.3f} <<<")

    print(f"\n  ── L6 比分Top5 ──")
    for s in result['top3_scores'][:5]:
        marker = " ★" if s['outcome'] == 'D' else ""
        print(f"    {s['score']}: {s['prob']*100:5.1f}% [{s['outcome']}]{marker}")

    print(f"\n  推荐: {result['recommendation']}")
    print(f"  RP_max: {result['max_rp']}")

    # 验证: 真实结果1-1
    print(f"\n  {'='*50}")
    print(f"  真实比分 1-1 → 平局")
    print(f"  预测平局概率: {result['D']*100:.1f}%")
    if result['D'] > 0.20:
        print(f"  ✅ 平局概率 >= 20%，成功捕捉平局信号")
    else:
        print(f"  ⚠ 平局概率 < 20%，信号偏弱")

    # 概率分布合理性检查
    total = result['H'] + result['D'] + result['A']
    assert abs(total - 1.0) < 0.01, f"概率和不等于1: {total}"
    assert 0 < result['H'] < 1 and 0 < result['D'] < 1 and 0 < result['A'] < 1, "概率范围异常"
    assert len(result['top3_scores']) >= 3, "比分输出不足3个"
    print(f"\n  ✅ 全部验证通过")
    print(f"  {'='*50}")

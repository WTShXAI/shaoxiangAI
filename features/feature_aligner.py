"""
FeatureAligner — 统一特征构建器 (P2 解耦)
=========================================
训练和生产共用同一入口, 彻底消除特征对齐不一致问题。

核心问题 (P2 修复前):
  - _sky_predict 构建 40+ 维特征 (内联在 unified_predictor.py)
  - _get_draw_expert_signal 只构建 5 维 (完全不对齐 → 输出恒定 0.331)
  - 两处各自维护, 训练/生产口径不一致

P2 修复后:
  - 所有特征构建逻辑收敛到此模块
  - unified_predictor / draw_expert / NN 调用同一入口
  - 特征向量完全一致, 可独立验证子模型

用法:
    from features.feature_aligner import FeatureAligner
    aligner = FeatureAligner.from_trainer(trainer)
    vec = aligner.build(home, away, oh, od, oa, asian_handicap, ou_line, ...)
"""
import os, sys, math, logging
import numpy as np
from typing import Dict, Optional, List, Tuple, Any

logger = logging.getLogger(__name__)

class FeatureAligner:
    """统一特征构建器 — 训练和生产共用"""

    def __init__(self):
        self.feature_names: List[str] = []
        self.defaults: Dict[str, float] = {}
        self.scaler = None
        # P0-4: Chain -1 战绩数据缓存 (由 intake_team_form 注入)
        self._form_cache: Dict[str, float] = {}

    def intake_team_form(self, form_result: Any = None) -> None:
        """P0-4: 注入 Chain -1 战绩特征，供 build() 自动混合

        form_result: TeamFormResult 或 None (清空缓存)
        注入 10 维特征: goal_diff_advantage, avg_gf_home, avg_ga_home,
                       avg_gf_away, avg_ga_away, defensive_collapse,
                       scorer_advantage, form_momentum_real, strength_gap_level,
                       net_gd_diff
        """
        if form_result is None:
            self._form_cache.clear()
            return
        try:
            self._form_cache['goal_diff_advantage'] = float(getattr(form_result, 'goal_diff_advantage', 0))
            self._form_cache['avg_gf_home'] = float(getattr(form_result.home, 'avg_gf', 0))
            self._form_cache['avg_ga_home'] = float(getattr(form_result.home, 'avg_ga', 0))
            self._form_cache['avg_gf_away'] = float(getattr(form_result.away, 'avg_gf', 0))
            self._form_cache['avg_ga_away'] = float(getattr(form_result.away, 'avg_ga', 0))
            self._form_cache['defensive_collapse'] = 1.0 if getattr(form_result.home, 'defensive_collapse', False) or getattr(form_result.away, 'defensive_collapse', False) else 0.0
            # scorer_advantage: 基于净胜差推导 (正净胜差意味着对方防守差，强队攻击强)
            abs_gap = abs(self._form_cache.get('goal_diff_advantage', 0))
            self._form_cache['scorer_advantage'] = 1.0 if abs_gap >= 2.0 else (0.5 if abs_gap >= 1.0 else 0.0)
            self._form_cache['form_momentum_real'] = float(max(getattr(form_result.home, 'momentum', 0.5), getattr(form_result.away, 'momentum', 0.5)))
            gap_map = {'massacre': 1.0, 'dominate': 0.7, 'edge': 0.4, 'even': 0.0, 'upset_risk': -0.4}
            self._form_cache['strength_gap_level'] = gap_map.get(getattr(form_result, 'strength_gap', 'even'), 0.0)
            self._form_cache['net_gd_diff'] = float(getattr(form_result, 'goal_diff_advantage', 0))
            logger.debug(f"FeatureAligner: 注入 10 维 Chain -1 战绩特征 (净胜差={self._form_cache['net_gd_diff']:+.2f})")
        except Exception as e:
            logger.warning(f"FeatureAligner.intake_team_form 异常: {e}")
            self._form_cache.clear()

    @classmethod
    def from_trainer(cls, trainer) -> "FeatureAligner":
        """从 EnsembleTrainer 加载特征名和默认值"""
        inst = cls()
        inst.feature_names = list(getattr(trainer, 'feature_names', []))
        inst.defaults = trainer.config.get('data', {}).get('default_values', {})
        inst.scaler = getattr(trainer, 'scaler', None)
        return inst

    @classmethod
    def from_model_dict(cls, model_dict: dict) -> "FeatureAligner":
        """从 v4.1 模型 dict 加载 (用于独立子模型调用)"""
        inst = cls()
        inst.feature_names = list(model_dict.get('feature_names', []))
        inst.defaults = model_dict.get('config', {}).get('data', {}).get('default_values', {})
        inst.scaler = model_dict.get('scaler')
        return inst

    def build(
        self,
        home: str = "",
        away: str = "",
        oh: float = 2.5,
        od: float = 3.3,
        oa: float = 2.8,
        asian_handicap: float = 0.0,
        ou_line: float = 2.5,
        over_water: float = 1.90,
        under_water: float = 1.92,
        open_h: float = 0,
        open_d: float = 0,
        open_a: float = 0,
        form_result: Any = None,  # P0-4: Chain -1 战绩数据
    ) -> np.ndarray:
        """构建完整特征向量 — 与 _sky_predict 逻辑完全一致

        P0-4: 传入 form_result 时自动注入 10 维战绩特征

        Returns:
            (n_features,) 标准化后的特征向量 (如果 scaler 存在)
        """
        n_feats = len(self.feature_names)
        vec = np.zeros(n_feats, dtype=np.float32)

        # 1. 填充默认值
        for i, name in enumerate(self.feature_names):
            if name in self.defaults:
                vec[i] = float(self.defaults[name])

        # 2. 从赔率推导核心特征
        feat_vals = self._compute_odds_features(
            oh, od, oa, asian_handicap, ou_line,
            over_water, under_water, open_h, open_d, open_a
        )

        for name, val in feat_vals.items():
            if name in self.feature_names:
                idx = self.feature_names.index(name)
                vec[idx] = float(val)

        # 3. P0-4: 注入 Chain -1 战绩特征 (覆盖赔率推导的 form_momentum 等)
        if form_result is not None:
            self._inject_form_features(vec, form_result)

        # 4. 标准化
        if self.scaler is not None:
            vec = self.scaler.transform(vec.reshape(1, -1))[0]

        return vec

    def _inject_form_features(self, vec: np.ndarray, form_result: Any) -> None:
        """P0-4: 将 Chain -1 战绩数据注入特征向量"""
        # 先从缓存提取
        if not self._form_cache:
            self.intake_team_form(form_result)
        # 注入特征 (仅当特征名存在于 feature_names 中时)
        _inject = lambda name, val: self._set_feat(vec, name, val)
        if self._form_cache:
            _inject('goal_diff_advantage', min(max(self._form_cache.get('goal_diff_advantage', 0) / 4.0, -1), 1))
            _inject('net_gd_diff', min(max(self._form_cache.get('net_gd_diff', 0) / 4.0, -1), 1))
            _inject('defensive_collapse', self._form_cache.get('defensive_collapse', 0))
            _inject('scorer_advantage', self._form_cache.get('scorer_advantage', 0))
            _inject('strength_gap_level', self._form_cache.get('strength_gap_level', 0))
            # 覆盖赔率推导的 form_momentum (硬编码 0.5) → 真实值
            _inject('form_momentum', self._form_cache.get('form_momentum_real', 0.5))
            _inject('form_factor', self._form_cache.get('form_momentum_real', 0.5))
            # 主客队场均进球/失球 (归一化到 [0,1])
            _inject('avg_gf_home', min(self._form_cache.get('avg_gf_home', 0) / 5.0, 1.0))
            _inject('avg_ga_home', min(self._form_cache.get('avg_ga_home', 0) / 5.0, 1.0))
            _inject('avg_gf_away', min(self._form_cache.get('avg_gf_away', 0) / 5.0, 1.0))
            _inject('avg_ga_away', min(self._form_cache.get('avg_ga_away', 0) / 5.0, 1.0))

    def _set_feat(self, vec: np.ndarray, name: str, val: float) -> None:
        """安全写入特征值"""
        if name in self.feature_names:
            idx = self.feature_names.index(name)
            vec[idx] = float(val)

    def build_raw_dict(
        self,
        oh: float, od: float, oa: float,
        asian_handicap: float = 0.0,
        ou_line: float = 2.5,
        over_water: float = 1.90,
        under_water: float = 1.92,
        open_h: float = 0, open_d: float = 0, open_a: float = 0,
    ) -> Dict[str, float]:
        """返回原始特征字典 (不映射到向量, 用于调试)"""
        return self._compute_odds_features(
            oh, od, oa, asian_handicap, ou_line,
            over_water, under_water, open_h, open_d, open_a
        )

    @staticmethod
    def _compute_odds_features(
        oh: float, od: float, oa: float,
        asian_handicap: float = 0.0,
        ou_line: float = 2.5,
        over_water: float = 1.90,
        under_water: float = 1.92,
        open_h: float = 0, open_d: float = 0, open_a: float = 0,
    ) -> Dict[str, float]:
        """从赔率推导特征 — 提取自 unified_predictor._sky_predict

        训练和生产完全一致, 是唯一的特征计算入口
        """
        imp_sum = 1/oh + 1/od + 1/oa
        imp_h = (1/oh) / imp_sum
        imp_d = (1/od) / imp_sum
        imp_a = (1/oa) / imp_sum

        # 虚拟开盘价推导
        if asian_handicap != 0:
            shift = min(abs(asian_handicap) * 0.04, 0.15)
            if asian_handicap < 0:
                _oih, _oia = imp_h + shift, imp_a - shift * 0.5
            else:
                _oih, _oia = imp_h - shift * 0.5, imp_a + shift
            _oid = max(1 - _oih - _oia, 0.02)
            _oor = imp_sum - 1.0
            oph = 1/max(_oih*(1+_oor), 0.05)
            opd = 1/max(_oid*(1+_oor), 0.05)
            opa = 1/max(_oia*(1+_oor), 0.05)
        else:
            oph, opd, opa = oh*1.02, od*1.02, oa*1.02

        move_h = imp_h - (1/oph)/(1/oph+1/opd+1/opa)
        move_d = imp_d - (1/opd)/(1/oph+1/opd+1/opa)
        move_a = imp_a - (1/opa)/(1/oph+1/opd+1/opa)
        move_mag = abs(move_h) + abs(move_d) + abs(move_a)
        fav_idx = 0 if min(oh, od, oa) == oh else (1 if min(oh, od, oa) == od else 2)
        fav_move = [move_h, move_d, move_a][fav_idx]

        # a1-a8 系列
        a1 = imp_h
        a2 = min(imp_h * 0.7 + imp_d * 0.3, 1)
        a3 = min(abs(imp_h - imp_a) * 2, 1)
        a4 = min(a2 * a3, 1)
        a5 = min(imp_d, 1)
        a6 = min(1 - abs(imp_h - imp_a), 1)
        a7 = min(imp_h * 0.5 + imp_a * 0.5, 1)
        a8 = min(abs(imp_d - 1/3) * 3, 1)
        sigma_trap = min(move_mag * 5, 1)
        lambda_crush = min(a2 * a5 * 2, 1)
        epsilon_senti = min(a3 * a6 * 2, 1)

        odds_conf = float(np.sqrt((imp_h - 1/3)**2 + (imp_d - 1/3)**2 + (imp_a - 1/3)**2) * 3.0)
        odds_entropy = float(-sum(p * math.log(max(p, 1e-9)) for p in [imp_h, imp_d, imp_a]))

        return {
            'real_home_odds': oh, 'real_draw_odds': od, 'real_away_odds': oa,
            'p_implied': imp_d, 'imp_d_norm': imp_d,
            'odds_imp_h': imp_h, 'odds_imp_d': imp_d, 'odds_imp_a': imp_a,
            'odds_confidence': odds_conf, 'odds_entropy': odds_entropy,
            'odds_draw_dev': imp_d - 0.333, 'odds_balance': abs(imp_h - imp_a),
            'odds_spread': oa - oh, 'odds_overround': imp_sum - 1.0,
            'match_evenness': 1.0 - abs(imp_h - imp_a) / max(imp_h + imp_a, 0.01),
            'draw_odds_attract': float(min(max(1.0 - (od - 3.0) / 2.0, 0), 1)),
            'draw_odds_vs_imp': float(min(max((1.0 / od if od > 0 else 0.3) - imp_d, -1), 1)),
            'market_fav_strength': max(1/oh, 1/od, 1/oa) / imp_sum,
            'is_cold_start': 0.0, 'feat_coverage_ratio': 0.5,
            'miss_drift': 0.0,
            'a1': a1, 'a2': a2, 'a3': a3, 'a4': a4, 'a5': a5, 'a6': a6, 'a7': a7, 'a8': a8,
            'sigma_trap': sigma_trap, 'lambda_crush': lambda_crush, 'epsilon_senti': epsilon_senti,
            'ix_a1_sigma': min(a1 * sigma_trap * 2, 1), 'ix_a2_lambda': min(a2 * lambda_crush * 2, 1),
            'ix_a3_epsilon': min(a3 * epsilon_senti * 2, 1), 'ix_a1_a2': min(a1 * a2 * 2, 1),
            'ix_a7_lambda': min(a7 * lambda_crush * 2, 1), 'ix_a8_sigma': min(a8 * sigma_trap * 2, 1),
            'ix_even_impd': min((1 - abs(imp_h - imp_a)) * imp_d * 3, 1),
            'ix_bal_even': min((1 - abs(imp_h - imp_a)) * imp_d * 3, 1),
            'ix_power_gap': min(abs(imp_h - imp_a) * 3, 1),
            'ix_drift_draw_odds': min(imp_d * move_mag * 3, 1),
            'ix_drift_confidence': min(move_mag * 3 * odds_conf / 3, 1),
            'ix_sharp_draw': min(imp_d * min(1 - (od - 3) / 2, 1) * 2, 1),
            'drift_d_signal': min(imp_d * 3, 1),
            'ix_drift_even_draw': min((1 - abs(imp_h - imp_a)) * move_mag * 2, 1),
            'drift_magnitude': min(move_mag * 4, 1),
            'drift_direction': min(max(move_h * 10, -1), 1),
            'drift_h_val': min(max(move_h * 8, -1), 1),
            'drift_d': min(max(move_d * 10, -1), 1),
            'drift_a_val': min(max(move_a * 8, -1), 1),
            'drift_sharp_signal': min(abs(move_h) * 8, 1),
            'odds_move_h': move_h * 5, 'odds_move_d': move_d * 5, 'odds_move_a': move_a * 5,
            'odds_move_magnitude': move_mag * 3, 'odds_fav_move': fav_move * 5,
            'v_value': min(max(imp_d * od - 1, -1), 1),
            'otsm_state_NOISE': min(move_mag * 3, 1),
            'press_intensity': min(abs(asian_handicap) / 2, 1),
            'beta_dev': min(abs(asian_handicap - (oa - oh) * 0.3) / 2, 1),
            'handicap_cover_prob': 0.3,
            'rank_diff_factor': (imp_h - imp_a) * 3,
            'form_momentum': 0.5, 'h2h_factor': 0.5,
            'rank_factor': 0.5, 'form_factor': 0.5,
            'home_advantage_neutral': 0.5,
            'home_match_count_norm': 1.0, 'away_match_count_norm': 1.0,
            'odds_model_diverge': imp_h - (0.33 + a1 * 0.4),
            'market_disagreement': 0.0, 'ix_rank_form': 0.5,
        }

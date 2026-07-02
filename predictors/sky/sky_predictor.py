"""
SKY Predictor v1.1 — 全版本最优组件融合预测器
===============================================
架构: L0-L5 五层融合
  [L0] 特征层: v4.1 72维特征
  [L1] 基模型: LGB + XGB + Heuristic + OE + NN + DrawExpert (v4.1 Stacking)
  [L2] Draw专科: DrawExpert 二分类器 (DE×0.25)
  [L3] Meta融合: LightGBM meta-learner (v4.1 21维输入)
  [L4] 进球修正: market_goal_predictor v2.0
  [L5] 后处理: D-Gate + λ Fusion + Trap修正

用法:
    from sky_predictor import SKYPredictor
    sky = SKYPredictor()
    result = sky.predict(home="英格兰", away="克罗地亚", odds={"H": 2.10, "D": 3.20, "A": 3.80})
"""

import os, sys, logging, numpy as np
from typing import Dict, Optional, Any, Tuple

# 路径配置
SKY_DIR = os.path.dirname(os.path.abspath(__file__))
ARCH_ROOT = os.path.dirname(os.path.dirname(SKY_DIR))
# 修复P0-13: 消除footballAI外部依赖, 项目内components自包含
FOOTBALLAI_ROOT = os.path.join(ARCH_ROOT, 'predictors', 'components')

logger = logging.getLogger(__name__)

from predictors.base import PredictorBase, MatchData, PredictionResult

class SKYPredictor(PredictorBase):
    """
    SKY v1.1 — v4.1 五层融合预测器
    """

    def __init__(self, model_path: str = None):
        """
        Args:
            model_path: 模型路径，默认自动搜索 v4.1 > v4.0
        """
        self.model_path = model_path or self._find_model()
        self.trainer = None
        self._loaded = False
        self._load()

    def _find_model(self) -> str:
        """自动搜索: v4.1 > v4.0, 项目内路径优先"""
        candidates = [
            os.path.join(ARCH_ROOT, 'models', 'main', 'football_v4.1_production.joblib'),
            os.path.join(ARCH_ROOT, 'saved_models', 'football_v4.1_production.joblib'),
            os.path.join(FOOTBALLAI_ROOT, 'saved_models', 'football_v4.1_production.joblib'),
            os.path.join(ARCH_ROOT, 'models', 'main', 'football_v4.0_production.joblib'),
            os.path.join(ARCH_ROOT, 'saved_models', 'football_v4.0_production.joblib'),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        raise FileNotFoundError(f"未找到 production 模型")

    def _load(self):
        """加载模型"""
        try:
            from ensemble_trainer import EnsembleTrainer
        except ImportError:
            from predictors.components.ensemble_trainer import EnsembleTrainer

        logger.info(f"[SKY] 加载模型: {self.model_path}")
        self.trainer = EnsembleTrainer.load_pipeline(self.model_path)
        logger.info(f"[SKY] 版本: {self.trainer.model_version}, "
                   f"特征: {len(self.trainer.feature_names)}, "
                   f"DrawExpert: {self.trainer.draw_expert_model is not None}")

        # 加载 NN (NN权重在项目根 saved_models/, 非 predictors/components/)
        nn_path = os.path.join(ARCH_ROOT, 'saved_models', 'football_nn_20260616_125617.pth')
        if os.path.exists(nn_path):
            self.trainer.load_nn_model(nn_path)

        # 加载进球预测器
        try:
            from predictors.market_goal_predictor import MarketGoalPredictor
            self.market_goal = MarketGoalPredictor()
            logger.info("[SKY] MarketGoalPredictor v2.0 已加载")
        except Exception as e:
            logger.warning(f"[SKY] MarketGoalPredictor 加载失败: {e}")
            self.market_goal = None

        self._loaded = True

    @property
    def model_version(self) -> str:
        return getattr(self.trainer, 'model_version', 'unknown')

    @property
    def has_draw_expert(self) -> bool:
        return self.trainer is not None and self.trainer.draw_expert_model is not None

    def predict(
        self,
        home: str,
        away: str,
        odds: Optional[Dict[str, float]] = None,
        league: Optional[str] = None,
        handicap_line: Optional[float] = None,
        handicap_water: Optional[Dict[str, float]] = None,
        ou_line: Optional[float] = None,
        over_water: Optional[float] = None,
        under_water: Optional[float] = None,
        elo_home: Optional[float] = None,
        elo_away: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        SKY 五层融合预测

        Args:
            home: 主队名
            away: 客队名
            odds: 赔率字典 {"H": 2.10, "D": 3.20, "A": 3.80}
            league: 联赛名
            handicap_line: 让球盘口线 (如 -0.5)
            handicap_water: 让球水位 {"home": 0.90, "away": 0.95}
            ou_line: 大小球盘口线 (如 2.5)
            over_water: 大球水位
            under_water: 小球水位
            elo_home: 主队 ELO
            elo_away: 客队 ELO

        Returns:
            完整预测结果字典
        """
        if not self._loaded:
            self._load()

        result = {
            'match': f"{home} vs {away}",
            'model_version': f"SKY v1.0 ({self.model_version})",
            'has_draw_expert': self.has_draw_expert,
        }

        # ── L0: 特征构建 ──
        features = self._build_features(home, away, odds, league, elo_home, elo_away)

        # ── L1+L2+L3: 模型推理 (含DrawExpert) ──
        proba_raw = self._model_predict(features)
        result['proba_raw'] = {
            'home': float(proba_raw[0]),
            'draw': float(proba_raw[1]),
            'away': float(proba_raw[2]),
        }

        # ── L2 补充: DrawExpert独立输出 ──
        if self.has_draw_expert:
            de_pdraw = self._get_draw_expert_output()
            result['draw_expert'] = {
                'p_draw': float(de_pdraw) if de_pdraw is not None else None,
            }

        # ── L4: 进球修正 ──
        goals_correction = None
        if self.market_goal and handicap_line is not None and ou_line is not None:
            goals_correction = self._apply_goal_correction(
                proba_raw, handicap_line, handicap_water,
                ou_line, over_water, under_water, home, away
            )
            if goals_correction:
                result['goals_correction'] = goals_correction
                proba_raw = goals_correction['proba_corrected']

        # ── L5: 赔率后处理 ──
        proba_final = proba_raw.copy()
        if odds:
            proba_final = self._apply_odds_postprocess(proba_final, odds)

        # 归一化
        total = proba_final.sum()
        if total > 0:
            proba_final /= total

        # 修复P0-11: 同时返回 proba_final 和 probabilities 两种键名, 消除键名不匹配
        result['proba_final'] = {
            'home': float(proba_final[0]),
            'draw': float(proba_final[1]),
            'away': float(proba_final[2]),
        }
        result['probabilities'] = result['proba_final']  # 兼容six_layer引擎查找
        result['prediction'] = self._class_name(np.argmax(proba_final))

        # 比分预测
        if goals_correction and 'expected_goals' in goals_correction:
            result['expected_goals'] = goals_correction['expected_goals']

        return result

    def _build_features(
        self, home: str, away: str,
        odds: Optional[Dict] = None,
        league: Optional[str] = None,
        elo_home: Optional[float] = None,
        elo_away: Optional[float] = None,
    ) -> np.ndarray:
        """
        L0: 构建特征向量
        简化版: 使用赔率衍生特征 + 默认值填充非赔率特征

        Phase 0: 无赔率时从 DB/API 尝试获取，不再用零向量静默降级。
        """
        n_features = len(self.trainer.feature_names)
        feats = np.zeros(n_features, dtype=np.float64)

        # Phase 0: 赔率缺失 → 尝试从 DB 获取实时赔率
        if not odds:
            odds = self._fetch_live_odds(home, away)

        if odds:
            h_odd = float(odds.get('H', 2.5))
            d_odd = float(odds.get('D', 3.2))
            a_odd = float(odds.get('A', 2.8))

            # 隐含概率
            imp_h = 1.0 / max(h_odd, 1.01)
            imp_d = 1.0 / max(d_odd, 1.01)
            imp_a = 1.0 / max(a_odd, 1.01)
            total_imp = imp_h + imp_d + imp_a
            if total_imp > 0:
                p_h_implied = imp_h / total_imp
                p_d_implied = imp_d / total_imp
                p_a_implied = imp_a / total_imp
            else:
                p_h_implied = p_d_implied = p_a_implied = 1.0 / 3.0

            # 填充已知特征
            feature_map = {
                'p_h_implied': p_h_implied,
                'p_d_implied': p_d_implied,
                'p_a_implied': p_a_implied,
                'imp_h': imp_h,
                'imp_d': imp_d,
                'imp_a': imp_a,
                'odds_h': h_odd,
                'odds_d': d_odd,
                'odds_a': a_odd,
                'home_odds': h_odd,
                'draw_odds': d_odd,
                'away_odds': a_odd,
                'overround': total_imp - 1.0,
                'odds_confidence': 1.0 - (total_imp - 1.0),
                'match_evenness': 1.0 - abs(p_h_implied - p_a_implied),
                'imp_d_norm': p_d_implied,
                'odds_balance': abs(p_h_implied - p_a_implied),
            }

            for fname, val in feature_map.items():
                if fname in self.trainer.feature_names:
                    idx = self.trainer.feature_names.index(fname)
                    feats[idx] = val
        else:
            # 真·无赔率 → 均匀降级 (不再伪造差异化特征)
            logger.warning(
                f"[SKY] 无赔率数据: {home} vs {away}，使用均匀降级 "
                f"(所有 odds=2.5, implied=0.333)"
            )
            imp_uniform = 1.0 / 3.0
            uniform_map = {
                'p_h_implied': imp_uniform, 'p_d_implied': imp_uniform, 'p_a_implied': imp_uniform,
                'odds_h': 2.5, 'odds_d': 2.5, 'odds_a': 2.5,
                'home_odds': 2.5, 'draw_odds': 2.5, 'away_odds': 2.5,
                'match_evenness': 1.0, 'odds_balance': 0.0, 'imp_d_norm': imp_uniform,
            }
            for fname, val in uniform_map.items():
                if fname in self.trainer.feature_names:
                    idx = self.trainer.feature_names.index(fname)
                    feats[idx] = val

        # ELO
        if elo_home is not None and elo_away is not None:
            elo_diff = elo_home - elo_away
            for fname in ['elo_diff', 'elo_home', 'elo_away', 'rank_diff']:
                if fname in self.trainer.feature_names:
                    idx = self.trainer.feature_names.index(fname)
                    if fname == 'elo_diff' or fname == 'rank_diff':
                        feats[idx] = elo_diff
                    elif fname == 'elo_home':
                        feats[idx] = elo_home
                    elif fname == 'elo_away':
                        feats[idx] = elo_away

        return feats.reshape(1, -1)

    def _fetch_live_odds(self, home: str, away: str) -> Optional[Dict[str, float]]:
        """
        Phase 0: 从 DB / odds_fetcher 获取实时赔率。

        优先级: DB odds_snapshots > odds_fetcher.get_odds() > None

        Returns:
            {"H": home_odds, "D": draw_odds, "A": away_odds} 或 None
        """
        try:
            # 1. 尝试 DB
            import sqlite3
            db_path = os.path.join(ARCH_ROOT, 'data', 'football_data.db')
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    '''SELECT home_odds, draw_odds, away_odds
                       FROM odds_snapshots
                       WHERE match_id IN (
                           SELECT match_id FROM matches
                           WHERE home_team_name=? AND away_team_name=?
                           ORDER BY match_date DESC LIMIT 1
                       )
                       ORDER BY snapshot_time DESC LIMIT 1''',
                    (home, away)
                )
                row = cur.fetchone()
                conn.close()
                if row and row[0] is not None:
                    return {"H": float(row[0]), "D": float(row[1] or 3.2), "A": float(row[2])}

            # 2. 回退 odds_fetcher
            try:
                from data_collector.odds_fetcher import get_odds
                oh, od, oa, _hcp, _ou = get_odds(home, away)
                return {"H": oh, "D": od, "A": oa}
            except ImportError:
                pass

        except (Exception, sqlite3.Error, KeyError, IndexError) as e:
            logger.debug(f"[SKY] _fetch_live_odds 失败: {e}")

        return None

    def _model_predict(self, features: np.ndarray) -> np.ndarray:
        """L1+L2+L3: Stacking推理 (含DrawExpert)"""
        try:
            proba = self.trainer._predict_with_stacking(features)
            if proba is None or proba.shape[0] == 0:
                return np.array([0.40, 0.27, 0.33])
            return proba[0]
        except Exception as e:
            logger.warning(f"[SKY] Stacking推理失败: {e}")
            return np.array([0.40, 0.27, 0.33])

    def _get_draw_expert_output(self) -> Optional[float]:
        """获取 DrawExpert P(Draw)"""
        try:
            sub = self.trainer._last_submodel_probas
            if sub and 'draw_expert' in sub:
                de = sub['draw_expert']
                if de is not None and len(de) > 0:
                    return float(de[0])
        except (KeyError, TypeError, IndexError) as e:
            logger.debug("获取DrawExpert输出失败: %s", e)
        return None

    def _apply_goal_correction(
        self, proba: np.ndarray,
        handicap_line: float,
        handicap_water: Optional[Dict],
        ou_line: float,
        over_water: Optional[float],
        under_water: Optional[float],
        home: str, away: str,
    ) -> Optional[Dict]:
        """
        L4: 进球修正层
        用让球+大小球推导预期总球，修正胜平负概率
        """
        try:
            # 使用主队让球水作为 handicap_water（单值）
            hw = handicap_water.get('home', 0.90) if handicap_water else 0.90

            from predictors.market_goal_predictor import predict_goals
            exp_home, exp_away = predict_goals(
                handicap_line=handicap_line,
                handicap_water=hw,
                ou_line=ou_line,
                over_water=over_water or 0.90,
                under_water=under_water or 0.90,
            )
            exp_total = exp_home + exp_away

            # 进球修正逻辑:
            # 总球数越高 → Draw概率越低，H/A分化越大
            # 总球数越低 → Draw概率越高
            if exp_total > 3.5:
                draw_factor = 0.75  # 高进球 → Draw概率×0.75
            elif exp_total > 2.8:
                draw_factor = 0.88
            elif exp_total < 1.8:
                draw_factor = 1.25  # 低进球 → Draw概率×1.25
            elif exp_total < 2.2:
                draw_factor = 1.12
            else:
                draw_factor = 1.0

            # 进球差越大 → H/A越分明
            goal_diff = abs(exp_home - exp_away)
            if goal_diff > 1.5:
                ha_boost = 1.15
            elif goal_diff > 0.8:
                ha_boost = 1.05
            elif goal_diff < 0.3:
                ha_boost = 0.90
            else:
                ha_boost = 1.0

            h, d, a = proba[0], proba[1], proba[2]
            d_new = d * draw_factor
            remaining = 1.0 - d_new
            ha_sum = h + a
            if ha_sum > 0.001:
                h_new = remaining * (h * ha_boost) / (h * ha_boost + a * (2 - ha_boost))
                a_new = remaining - h_new
            else:
                h_new = a_new = remaining / 2.0

            proba_corrected = np.array([h_new, d_new, a_new])
            proba_corrected /= proba_corrected.sum()

            return {
                'expected_goals': {'home': exp_home, 'away': exp_away, 'total': exp_total},
                    'goal_diff': goal_diff,
                    'draw_factor': draw_factor,
                    'proba_corrected': proba_corrected,
                }
        except Exception as e:
            logger.warning(f"[SKY] L4进球修正失败: {e}")

        return None

    def _apply_odds_postprocess(
        self, proba: np.ndarray, odds: Dict[str, float]
    ) -> np.ndarray:
        """L5: 赔率后处理 — 融入隐含概率"""
        try:
            h_odd = float(odds.get('H', 2.5))
            d_odd = float(odds.get('D', 3.2))
            a_odd = float(odds.get('A', 2.8))

            imp_h = 1.0 / max(h_odd, 1.01)
            imp_d = 1.0 / max(d_odd, 1.01)
            imp_a = 1.0 / max(a_odd, 1.01)
            total = imp_h + imp_d + imp_a

            if total > 0:
                odds_proba = np.array([imp_h / total, imp_d / total, imp_a / total])
                # spread 驱动的融合权重
                spread = abs(odds_proba[0] - odds_proba[2])
                if spread > 0.50:
                    w_model = 0.20
                elif spread > 0.30:
                    w_model = 0.40
                else:
                    w_model = 0.55
                w_odds = 1.0 - w_model

                return proba * w_model + odds_proba * w_odds
        except Exception as e:
            logger.debug("赔率融合修正失败: %s", e)
        return proba

    @staticmethod
    def _class_name(idx: int) -> str:
        return {0: 'H'}.get(idx, {1: 'D'}.get(idx, 'A'))

    # ══════════════════════════════════════
    # PredictorBase 统一接口 (2026-06-28)
    # ══════════════════════════════════════

    def predict_match(self, match: MatchData) -> PredictionResult:
        """实现 PredictorBase.predict_match()"""
        odds = match.odds_dict if match.odds_h > 0 else None
        result_dict = self.predict(
            home=match.home, away=match.away,
            odds=odds,
            league=match.league,
            handicap_line=match.handicap,
            ou_line=match.ou_line,
            over_water=match.over_water,
            under_water=match.under_water,
            elo_home=match.elo_home,
            elo_away=match.elo_away,
        )
        probs = result_dict.get('probabilities', result_dict.get('proba_final', {}))
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
            probabilities=probs if isinstance(probs, dict) else {'H': 0.0, 'D': 0.0, 'A': 0.0},
            prediction=pred_code,
            confidence=float(result_dict.get('confidence', 0.0)),
            model_version=result_dict.get('model_version', f"SKY {self.__class__.__name__}"),
            expected_goals=result_dict.get('expected_goals'),
            extra={k: v for k, v in result_dict.items()
                   if k not in ('probabilities', 'proba_final', 'proba_raw', 'prediction')},
        )

    @property
    def model_version(self) -> str:
        return f"SKYPredictor {self.__class__.__name__}"

    def is_loaded(self) -> bool:
        return self._loaded

# ─── 便捷函数 ───

_sky_instance: Optional[SKYPredictor] = None

def get_sky_predictor() -> SKYPredictor:
    """获取 SKY Predictor 单例"""
    global _sky_instance
    if _sky_instance is None or not _sky_instance._loaded:
        _sky_instance = SKYPredictor()
    return _sky_instance

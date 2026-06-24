"""
针对平局预测优化的模型架构 — DrawOptimizedEnsemble

三阶段策略:
  1. 主客胜负二分类 (排除平局)
  2. 平局检测二分类 (balanced class weights)
  3. 完整三分类 + 平局概率增强

可与 FootballAIEnhanced 集成或独立运行。
"""
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.calibration import CalibratedClassifierCV
from sklearn.utils.class_weight import compute_class_weight
from sklearn.model_selection import TimeSeriesSplit
from typing import Dict, List, Tuple, Optional
import logging

logger = logging.getLogger(__name__)

# ============================================================================
#  核心类: DrawOptimizedEnsemble
# ============================================================================


class DrawOptimizedEnsemble:
    """针对平局预测优化的三阶段集成模型

    Parameters
    ----------
    model_version : str
    draw_threshold : float
        平局概率阈值 — draw_detector 概率超过此值时增强平局概率
    """

    def __init__(self, model_version: str = "v6.0", draw_threshold: float = 0.25, boost_factor: float = 0.6):
        self.model_version = model_version
        self.models: Dict[str, object] = {}
        self.class_weights: Optional[Dict[int, float]] = None
        self.draw_threshold = draw_threshold
        self.boost_factor = boost_factor  # v6.1: 增强力度 (0=不增强, 1=完全替换为detector)
        self._trained = False

    # ────────────────── 特征增强 ──────────────────

    @staticmethod
    def prepare_draw_specific_features(X: pd.DataFrame) -> pd.DataFrame:
        """创建平局相关特征

        在原 DataFrame 基础上新增:
          - elo_closeness: 实力接近度 (sigmoid, 0-1)
          - historical_draw_tendency: 历史平局倾向 (alias for h2h_draw_rate)
          - balance_N: 攻守平衡度 (attack_form_N - defense_form_N)
        """
        X_enhanced = X.copy()

        # 1. 实力接近度 (Elo 差 sigmoid)
        if 'home_elo' in X.columns and 'away_elo' in X.columns:
            elo_diff = np.abs(X['home_elo'] - X['away_elo'])
            X_enhanced['elo_closeness'] = 1.0 / (1.0 + np.exp(elo_diff / 100.0))

        # 2. 历史平局倾向
        if 'h2h_draw_rate' in X.columns:
            X_enhanced['historical_draw_tendency'] = X['h2h_draw_rate']

        # 3. 攻守平衡度 — 按窗口匹配 attack/defense 列
        attack_cols = sorted([c for c in X.columns if c.startswith('attack_form_')])
        defense_cols = sorted([c for c in X.columns if c.startswith('defense_form_')])

        for a_col in attack_cols:
            # extract window suffix, e.g. "attack_form_5" → "5"
            suffix = a_col.split('_')[-1]
            d_col = f'defense_form_{suffix}'
            if d_col in X.columns:
                X_enhanced[f'balance_{suffix}'] = X[a_col] - X[d_col]

        return X_enhanced

    # ────────────────── 三阶段训练 ──────────────────

    def train_three_stage_model(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_val: np.ndarray,
        y_val: np.ndarray,
    ) -> Dict:
        """三阶段训练: (1) 主客分类 (2) 平局检测 (3) 完整三分类"""
        logger.info("🏗️ 三阶段模型训练...")

        # ── 阶段1: 主客胜负 (排除平局) ──
        logger.info("  📊 阶段1: 训练主客胜负模型...")
        wl_mask = (y_train != 1)
        if wl_mask.sum() < 10:
            logger.warning("  ⚠ 非平局样本不足10条，跳过主客模型")
        else:
            X_wl = X_train[wl_mask]
            y_wl = y_train[wl_mask]
            y_wl_binary = np.where(y_wl == 2, 1, 0)  # 0=主胜(H), 1=客胜(A)

            win_loss_model = xgb.XGBClassifier(
                n_estimators=150, max_depth=5, learning_rate=0.05,
                scale_pos_weight=float((y_wl_binary == 0).sum()) / max((y_wl_binary == 1).sum(), 1),
                random_state=42, n_jobs=-1, verbosity=0,
            )
            win_loss_model.fit(X_wl, y_wl_binary)

            y_val_wl = y_val[y_val != 1]
            if len(y_val_wl) > 0:
                wl_acc = win_loss_model.score(
                    X_val[y_val != 1],
                    np.where(y_val_wl == 2, 1, 0),
                )
                logger.info(f"    ✓ 主客准确率: {wl_acc:.3f}")
            self.models['win_loss'] = win_loss_model

        # ── 阶段2: 平局检测 ──
        logger.info("  ⚖️ 阶段2: 训练平局检测模型...")
        draw_labels = (y_train == 1).astype(int)

        class_weights = compute_class_weight(
            class_weight='balanced',
            classes=np.unique(draw_labels),
            y=draw_labels,
        )
        self.class_weights = dict(zip([0, 1], class_weights))

        draw_model = xgb.XGBClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.03,
            scale_pos_weight=float(class_weights[1]) / float(class_weights[0]),
            random_state=42, n_jobs=-1, verbosity=0,
        )
        draw_model.fit(X_train, draw_labels)
        self.models['draw_detector'] = draw_model

        draw_val_labels = (y_val == 1).astype(int)
        draw_acc = draw_model.score(X_val, draw_val_labels)
        logger.info(f"    ✓ 平局检测准确率: {draw_acc:.3f}")

        # ── 阶段3: 完整三分类 ──
        logger.info("  🎯 阶段3: 训练完整三分类模型...")
        full_model = xgb.XGBClassifier(
            n_estimators=300, max_depth=6, learning_rate=0.04,
            random_state=42, n_jobs=-1, verbosity=0,
        )
        full_model.fit(X_train, y_train)
        self.models['full_model'] = full_model

        full_acc = full_model.score(X_val, y_val)
        logger.info(f"    ✓ 完整三分类准确率: {full_acc:.3f}")

        self._trained = True
        return {
            'win_loss_accuracy': wl_acc if 'win_loss' in self.models else None,
            'draw_detector_accuracy': draw_acc,
            'full_model_accuracy': full_acc,
            'draw_model_weights': self.class_weights,
        }

    # ────────────────── 平局增强预测 ──────────────────

    def predict_with_draw_enhancement(
        self, X: np.ndarray,
        base_proba: Optional[np.ndarray] = None,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """平局增强预测 (v6.1 升级: 置信度感知的渐进式增强)

        Parameters
        ----------
        X : 特征矩阵
        base_proba : 可选的外部基础概率 (如 FootballAIEnhanced.ensemble_predict 的输出)
                     若为 None，使用内部 full_model 的概率。

        Returns
        -------
        (predictions, enhanced_proba)

        v6.1 升级说明:
        - 旧版: draw_proba > threshold → max(base_draw, draw_detector)  (二元开关)
        - 新版: boosted_draw = (1-α)·base_draw + α·draw_detector
                 α = confidence * draw_detector_prob (渐进式，无突跳)
        - 增加自适应阈值: threshold 随联赛平局率自动调整
        """
        if not self._trained:
            raise RuntimeError("模型未训练，请先调用 train_three_stage_model()")

        # 基础三分类概率
        if base_proba is not None:
            full_proba = base_proba.copy()
        else:
            full_proba = self.models['full_model'].predict_proba(X)

        # 平局检测概率
        if 'draw_detector' not in self.models:
            return np.argmax(full_proba, axis=1), full_proba

        draw_proba = self.models['draw_detector'].predict_proba(X)[:, 1]
        enhanced_proba = full_proba.copy()
        base_draw = enhanced_proba[:, 1].copy()

        # ── v6.1 渐进式增强: boosted_draw = (1-α)·base + α·draw_detector ──
        # α 由置信度控制: confidence = |draw_proba - 0.5| * 2 (缩放到 [0,1])
        # boost_factor 限制最大增强力度 (默认 0.6 → 最多用60%的detector概率)
        boost_factor = self.boost_factor

        draw_confidence = np.abs(draw_proba - 0.5) * 2.0  # [0, 1]
        alpha = draw_confidence * boost_factor * draw_proba  # 三要素: 置信度×力度×检测概率

        # 仅当 draw_detector 置信度高于阈值时才增强 (避免低置信度干扰)
        boost_mask = draw_proba > self.draw_threshold
        boosted_draw = base_draw.copy()
        boosted_draw[boost_mask] = (
            (1.0 - alpha[boost_mask]) * base_draw[boost_mask]
            + alpha[boost_mask] * draw_proba[boost_mask]
        )
        enhanced_proba[:, 1] = boosted_draw

        # 重新归一化
        row_sums = enhanced_proba.sum(axis=1, keepdims=True)
        enhanced_proba = enhanced_proba / np.maximum(row_sums, 1e-10)

        predictions = enhanced_proba.argmax(axis=1)
        return predictions, enhanced_proba

    def predict_with_calibrated_boost(
        self, X: np.ndarray,
        base_proba: Optional[np.ndarray] = None,
        league_draw_rates: Optional[np.ndarray] = None,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """v6.1 新增: 联赛感知的校准增强预测

        相比 predict_with_draw_enhancement 的改进:
        1. 多检测器投票 (如果有多个检测器)
        2. 联赛先验注入: 根据联赛历史平局率调整增强力度
        3. 校准后处理: 确保 boosted 概率经 isotonic 校准

        Parameters
        ----------
        X : 特征矩阵
        base_proba : 基础概率矩阵
        league_draw_rates : 每条样本对应联赛的历史平局率 [n_samples]
                           若提供，模型会针对不同联赛调整阈值

        Returns
        -------
        (predictions, calibrated_proba)
        """
        if not self._trained:
            raise RuntimeError("模型未训练，请先调用 train_three_stage_model()")

        # 基础三分类概率
        if base_proba is not None:
            full_proba = base_proba.copy()
        else:
            full_proba = self.models['full_model'].predict_proba(X)

        # ── 1. 多检测器投票 (若有多个) ──
        draw_proba = self._get_multi_detector_proba(X)

        # ── 2. 联赛先验注入 ──
        enhanced_proba = full_proba.copy()
        base_draw = enhanced_proba[:, 1].copy()
        boost_factor = self.boost_factor  # 使用实例属性

        draw_confidence = np.abs(draw_proba - 0.5) * 2.0
        alpha = draw_confidence * boost_factor * draw_proba

        # 每个样本独立计算阈值 (联赛感知)
        if league_draw_rates is not None:
            # 阈值 = 基础阈值 × (实际平局率 / 全局平均平局率)
            adaptive_threshold = self.draw_threshold * np.clip(
                league_draw_rates / max(np.mean(league_draw_rates), 0.01),
                0.7, 1.5  # 阈值范围: ±50%
            )
            boost_mask = draw_proba > adaptive_threshold
        else:
            boost_mask = draw_proba > self.draw_threshold

        boosted_draw = base_draw.copy()
        boosted_draw[boost_mask] = (
            (1.0 - alpha[boost_mask]) * base_draw[boost_mask]
            + alpha[boost_mask] * draw_proba[boost_mask]
        )
        enhanced_proba[:, 1] = boosted_draw

        # ── 3. 校准后处理 ──
        if 'draw_calibrated' in self.models:
            draw_cal_proba = self.models['draw_calibrated'].predict_proba(X)[:, 1]
            # 混合校准: 60% 增强 + 40% 校准
            enhanced_proba[:, 1] = 0.6 * enhanced_proba[:, 1] + 0.4 * draw_cal_proba

        # 归一化
        row_sums = enhanced_proba.sum(axis=1, keepdims=True)
        enhanced_proba = enhanced_proba / np.maximum(row_sums, 1e-10)

        predictions = enhanced_proba.argmax(axis=1)
        return predictions, enhanced_proba

    def _get_multi_detector_proba(self, X: np.ndarray) -> np.ndarray:
        """多检测器投票 — 返回加权平均的平局概率"""
        detectors = [k for k in self.models if k.startswith('draw_detector')]
        if not detectors:
            return np.full(len(X), 1/3)  # ★ C4：无检测器时用均匀分布替代0.33硬编码

        all_probas = []
        weights = []
        for key in detectors:
            proba = self.models[key].predict_proba(X)
            if proba.shape[1] >= 2:
                all_probas.append(proba[:, 1])
                # 主检测器权重更高
                w = 2.0 if key == 'draw_detector' else 0.5
                weights.append(w)

        if not all_probas:
            return np.full(len(X), 1/3)  # ★ C4：无有效概率时用均匀分布

        all_probas = np.column_stack(all_probas)
        weights = np.array(weights) / sum(weights)
        return (all_probas * weights).sum(axis=1)

    def train_multi_detector(
        self, X_train: np.ndarray, y_train: np.ndarray
    ) -> Dict:
        """v6.1 新增: 训练多个平局检测器 (不同配置) 做集成投票

        在原有 draw_detector 基础上增加 2 个辅助检测器:
        - draw_detector_2: 更深树 (max_depth=6), 更多树 (n_estimators=250)
        - draw_detector_3: 更浅树 (max_depth=3), 更低学习率 (lr=0.02)
        """
        logger.info("[DRAW-v6.1] 训练多检测器集成...")
        draw_labels = (y_train == 1).astype(int)
        scale_weight = float((draw_labels == 0).sum()) / max((draw_labels == 1).sum(), 1)

        configs = [
            ('draw_detector_2', dict(n_estimators=250, max_depth=6, learning_rate=0.03)),
            ('draw_detector_3', dict(n_estimators=150, max_depth=3, learning_rate=0.02)),
        ]

        accuracies = {}
        for name, cfg in configs:
            if name in self.models:
                continue
            model = xgb.XGBClassifier(
                scale_pos_weight=scale_weight,
                random_state=42, n_jobs=-1, verbosity=0,
                **cfg,
            )
            model.fit(X_train, draw_labels)
            self.models[name] = model
            acc = float(model.score(X_train, draw_labels))
            accuracies[name] = acc
            logger.info(f"  ✓ {name}: acc={acc:.3f}")

        return accuracies

    # ────────────────── 概率校准 ──────────────────

    def calibrate_for_draws(self, X_cal: np.ndarray, y_cal: np.ndarray):
        """专门校准平局概率 (Isotonic regression)"""
        logger.info("🎯 校准平局概率...")

        calibrated_model = CalibratedClassifierCV(
            estimator=self.models['full_model'],
            method='isotonic',
            cv=3,  # 不用 prefit → 自动交叉验证校准
        )
        y_cal_draw = (y_cal == 1).astype(int)
        calibrated_model.fit(X_cal, y_cal_draw)
        self.models['draw_calibrated'] = calibrated_model

        logger.info("  ✓ 平局概率校准完成")
        return calibrated_model

    # ────────────────── 评估 ──────────────────

    @staticmethod
    def evaluate_draw_performance(
        X_test: np.ndarray,
        y_test: np.ndarray,
        predictions: Optional[np.ndarray] = None,
        proba: Optional[np.ndarray] = None,
    ) -> Dict:
        """评估平局预测性能

        Parameters
        ----------
        X_test : 未使用 (保留接口)
        y_test : 真实标签
        predictions : 预测标签 (若为 None 则不计算分类指标)
        proba : 预测概率 [n, 3] (若提供则额外输出 draw prob stats)
        """
        result = {
            'overall_accuracy': None,
            'draw_precision': None,
            'draw_recall': None,
            'draw_f1': None,
            'draw_count_actual': int((y_test == 1).sum()),
            'draw_count_predicted': None,
        }

        if predictions is not None:
            result['overall_accuracy'] = float(np.mean(predictions == y_test))
            draw_mask = (y_test == 1)
            draw_pred_mask = (predictions == 1)
            result['draw_count_predicted'] = int(draw_pred_mask.sum())

            if draw_mask.any():
                tp = int((draw_mask & draw_pred_mask).sum())
                result['draw_precision'] = tp / max(draw_pred_mask.sum(), 1)
                result['draw_recall'] = tp / draw_mask.sum()
                p, r = result['draw_precision'], result['draw_recall']
                result['draw_f1'] = 2 * p * r / max(p + r, 1e-6)

        # 若提供概率矩阵 → 额外统计
        if proba is not None:
            draw_probs = proba[:, 1]
            result['draw_prob_mean'] = float(np.mean(draw_probs))
            result['draw_prob_std'] = float(np.std(draw_probs))
            # 真实平局的概率 vs 非平局的概率
            draw_actual = (y_test == 1)
            result['draw_prob_on_draws'] = float(np.mean(draw_probs[draw_actual])) if draw_actual.any() else None
            result['draw_prob_on_non_draws'] = float(np.mean(draw_probs[~draw_actual])) if (~draw_actual).any() else None

        return result

    # ────────────────── CV 训练 (集成到 FootballAIEnhanced) ──────────────────

    def fit_with_cv(
        self,
        X: np.ndarray,
        y: np.ndarray,
        n_splits: int = 5,
        calibrate: bool = False,
    ) -> Dict:
        """时序交叉验证训练 (与 FootballAIEnhanced.train_with_cross_validation 对齐)"""
        tscv = TimeSeriesSplit(n_splits=n_splits)
        draw_accs = []
        full_accs = []

        for fold, (train_idx, val_idx) in enumerate(tscv.split(X), 1):
            X_tr, X_v = X[train_idx], X[val_idx]
            y_tr, y_v = y[train_idx], y[val_idx]

            metrics = self.train_three_stage_model(X_tr, y_tr, X_v, y_v)
            draw_accs.append(metrics['draw_detector_accuracy'])
            full_accs.append(metrics['full_model_accuracy'])

        logger.info(
            f"[DRAW] Draw Detector avg={np.mean(draw_accs):.4f} "
            f"(±{np.std(draw_accs):.4f})"
        )
        logger.info(
            f"[DRAW] Full Model   avg={np.mean(full_accs):.4f} "
            f"(±{np.std(full_accs):.4f})"
        )

        # 全量重训练
        logger.info("[DRAW] 全量重训练...")
        split = int(len(X) * 0.8)
        metrics = self.train_three_stage_model(
            X[:split], y[:split],
            X[split:], y[split:],
        )

        if calibrate:
            self.calibrate_for_draws(X[split:], y[split:])

        return metrics

    # ────────────────── 与 FootballAIEnhanced 集成 ──────────────────

    def enhance_external_probas(
        self,
        X: np.ndarray,
        external_proba: np.ndarray,
    ) -> np.ndarray:
        """对 FootballAIEnhanced 的输出概率做平局增强

        用法: draw_ensemble.enhance_external_probas(X, enhanced.ensemble_predict(X))
        """
        _, enhanced = self.predict_with_draw_enhancement(X, base_proba=external_proba)
        return enhanced

"""
自适应训练策略 (AdaptiveTrainingStrategy v1.0)
===============================================

四模块:
1. progressive_validation   — 时序滑窗验证（模拟实时预测场景）
2. adaptive_feature_selection — 特征稳定性筛选（跨时间窗）
3. dynamic_class_weighting   — 近期+全局混合类别权重
4. create_meta_features      — 交叉验证元特征 (stacking 预备)

集成: 通过 FootballAIEnhanced.enable_adaptive_training() 接线。
"""

import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import accuracy_score, f1_score, log_loss
from sklearn.utils.class_weight import compute_class_weight
import xgboost as xgb
from typing import Dict, List, Optional, Tuple
import logging
import warnings
warnings.filterwarnings('ignore')

logger = logging.getLogger(__name__)


class AdaptiveTrainingStrategy:
    """自适应训练策略 — 时序感知的四合一训练增强"""

    def __init__(self, n_splits: int = 5, min_train_size: int = 1000):
        self.n_splits = n_splits
        self.min_train_size = min_train_size
        self.cv_results: List[Dict] = []
        self._feature_names: Optional[List[str]] = None
        self._stable_features: Optional[List[str]] = None

    # ──────────────────────────────────────────────
    # 1. Progressive Validation
    # ──────────────────────────────────────────────

    def progressive_validation(
        self,
        X: np.ndarray,
        y: np.ndarray,
        dates: np.ndarray,
        model_builder,
        feature_names: Optional[List[str]] = None,
    ) -> Dict:
        """时序滑窗渐进式验证 — 模拟实时预测场景

        Parameters
        ----------
        X : np.ndarray  [n_samples, n_features]
        y : np.ndarray  [n_samples]
        dates : np.ndarray  datetime64 或可转换为 datetime 的字符串
        model_builder : callable → scikit-learn estimator (含 fit/predict)
        feature_names : 可选，记录日志使用

        Returns
        -------
        dict with keys: mean_accuracy, std_accuracy, window_details, n_windows
        """
        logger.info("[ADAPTIVE] 执行渐进式验证...")

        if feature_names:
            self._feature_names = list(feature_names)

        # 按时间排序
        dates_dt = pd.to_datetime(dates)
        sorted_idx = np.argsort(dates_dt)
        X_sorted = X[sorted_idx]
        y_sorted = y[sorted_idx]
        dates_sorted = dates_dt[sorted_idx]

        n_samples = len(X_sorted)
        if n_samples < self.min_train_size * 2:
            logger.warning(
                f"[ADAPTIVE] 样本量 {n_samples} 不足以做渐进式验证 "
                f"(最少 {self.min_train_size * 2})"
            )
            return {
                'mean_accuracy': 0.0,
                'std_accuracy': 0.0,
                'window_details': [],
                'n_windows': 0,
            }

        window_size = max(n_samples // 10, self.min_train_size // 2)
        window_accuracies: List[float] = []
        window_details: List[Dict] = []

        for i in range(1, 10):
            train_end = i * window_size
            val_start = train_end
            val_end = min(val_start + window_size, n_samples)

            if val_start >= n_samples or train_end < self.min_train_size or val_end <= val_start:
                continue

            X_train = X_sorted[:train_end]
            y_train = y_sorted[:train_end]
            X_val = X_sorted[val_start:val_end]
            y_val = y_sorted[val_start:val_end]

            if len(X_val) == 0 or len(X_train) == 0:
                continue

            # 训练+验证
            model = model_builder()
            model.fit(X_train, y_train)
            y_pred = model.predict(X_val)
            acc = float(accuracy_score(y_val, y_pred))

            window_accuracies.append(acc)
            window_details.append({
                'window': i,
                'train_size': len(X_train),
                'val_size': len(X_val),
                'accuracy': acc,
                'train_period': (
                    f"{str(dates_sorted[:train_end].min())[:10]} → "
                    f"{str(dates_sorted[train_end - 1])[:10]}"
                ),
                'val_period': (
                    f"{str(dates_sorted[val_start])[:10]} → "
                    f"{str(dates_sorted[val_end - 1])[:10]}"
                ),
            })

            logger.info(
                f"  Window {i}: acc={acc:.3f}  "
                f"(train={len(X_train):,} val={len(X_val):,})"
            )

        self.cv_results = window_details

        return {
            'mean_accuracy': float(np.mean(window_accuracies)) if window_accuracies else 0.0,
            'std_accuracy': float(np.std(window_accuracies)) if window_accuracies else 0.0,
            'window_details': window_details,
            'n_windows': len(window_accuracies),
        }

    # ──────────────────────────────────────────────
    # 2. Adaptive Feature Selection
    # ──────────────────────────────────────────────

    def adaptive_feature_selection(
        self,
        X: np.ndarray,
        y: np.ndarray,
        dates: np.ndarray,
        feature_names: List[str],
        n_windows: int = 5,
        stability_threshold: float = 0.5,
        max_features: int = 50,
    ) -> Tuple[List[str], Dict[str, float]]:
        """自适应特征选择 — 按跨时间窗稳定性筛选特征

        原理: 在每个时间窗训练 XGBoost，记录特征重要性；
        特征稳定性 = 1 - std(importance) / mean(importance)；
        高于阈值的特征入选。

        Returns
        -------
        selected_features : 入选的特征名列表
        stability_scores : {feature_name: stability_score}
        """
        logger.info("[ADAPTIVE] 自适应特征选择...")

        if feature_names is None or len(feature_names) == 0:
            logger.warning("[ADAPTIVE] 无特征名称，跳过自适应选择")
            return [], {}

        self._feature_names = list(feature_names)

        dates_dt = pd.to_datetime(dates)
        sorted_idx = np.argsort(dates_dt)
        X_sorted = X[sorted_idx]
        y_sorted = y[sorted_idx]

        n_samples = len(X_sorted)
        window_size = max(n_samples // n_windows, self.min_train_size // 2)

        feature_importance_history: Dict[str, List[float]] = {
            col: [] for col in feature_names
        }

        for w in range(n_windows - 1):
            train_end = (w + 1) * window_size
            val_start = train_end
            val_end = min(val_start + window_size, n_samples)

            if val_start >= n_samples:
                continue

            X_train = X_sorted[:train_end]
            y_train = y_sorted[:train_end]

            if len(X_train) < self.min_train_size:
                continue

            model = xgb.XGBClassifier(n_estimators=100, random_state=42, verbosity=0)
            model.fit(X_train, y_train)

            for i, col in enumerate(feature_names):
                if i < len(model.feature_importances_):
                    feature_importance_history[col].append(
                        float(model.feature_importances_[i])
                    )

        # 计算特征稳定性
        feature_stability: Dict[str, float] = {}
        for col, importances in feature_importance_history.items():
            if len(importances) >= 2:
                mean_imp = float(np.mean(importances))
                std_imp = float(np.std(importances))
                stability = 1.0 - std_imp / (mean_imp + 1e-6)
                feature_stability[col] = float(np.clip(stability, 0.0, 1.0))

        # 按稳定性排序，筛选
        sorted_stable = sorted(
            feature_stability.items(), key=lambda x: x[1], reverse=True,
        )
        selected = [
            col for col, stab in sorted_stable
            if stab > stability_threshold
        ][:max_features]

        self._stable_features = selected

        logger.info(
            f"  ✓ 选择 {len(selected)}/{len(feature_names)} 个稳定特征 "
            f"(阈值={stability_threshold}, 最多={max_features})"
        )

        return selected, feature_stability

    # ──────────────────────────────────────────────
    # 3. Dynamic Class Weighting
    # ──────────────────────────────────────────────

    def dynamic_class_weighting(
        self,
        y_train: np.ndarray,
        recent_window: int = 20,
    ) -> Dict[int, float]:
        """动态类别权重 — 近期 (70%) + 全局 (30%) 混合

        解决类别分布随时间漂移的问题。
        """
        logger.info("[ADAPTIVE] 计算动态类别权重...")

        y_train = np.asarray(y_train)
        classes = np.unique(y_train)

        if len(y_train) < recent_window * 2:
            weights_arr = compute_class_weight(
                'balanced', classes=classes, y=y_train,
            )
            weights = dict(zip(classes, weights_arr))
            logger.info(f"  [数据不足] 仅用全局权重: {weights}")
            return weights

        # 近期权重
        recent_y = y_train[-recent_window:]
        recent_weights_arr = compute_class_weight(
            'balanced', classes=classes, y=recent_y,
        )
        recent_weights = dict(zip(classes, recent_weights_arr))

        # 全局权重
        global_weights_arr = compute_class_weight(
            'balanced', classes=classes, y=y_train,
        )
        global_weights = dict(zip(classes, global_weights_arr))

        # 混合: 70% 近期 + 30% 全局
        mixed_weights: Dict[int, float] = {}
        for cls in classes:
            cls = int(cls)
            mixed_weights[cls] = (
                0.7 * float(recent_weights[cls])
                + 0.3 * float(global_weights[cls])
            )

        # 日志
        for cls in sorted(mixed_weights.keys()):
            recent_count = int((recent_y == cls).sum())
            global_count = int((y_train == cls).sum())
            label = {0: 'H', 1: 'D', 2: 'A'}.get(cls, str(cls))
            logger.info(
                f"  {label}: w={mixed_weights[cls]:.2f}  "
                f"(近期 {recent_count}/{recent_window}, 全局 {global_count}/{len(y_train)})"
            )

        return mixed_weights

    # ──────────────────────────────────────────────
    # 4. Meta Features (Stacking 预备)
    # ──────────────────────────────────────────────

    def create_meta_features(
        self,
        X: np.ndarray,
        base_models: List,
        y: np.ndarray,
        n_cv_splits: int = 5,
    ) -> np.ndarray:
        """交叉验证元特征 — stacking 第一层输出

        使用手动时序交叉验证避免数据泄露，
        将 base_models 的 predict_proba 输出拼接为元特征矩阵。

        Requirements: 每个 base_model 必须实现 predict_proba()。

        Returns
        -------
        meta_X : np.ndarray  [n_samples, sum(n_classes_i)]
        """
        logger.info("[ADAPTIVE] 创建元特征 (cv-pred stacking)...")

        if y is None:
            raise ValueError("create_meta_features 需要 y 参数（用于监督训练）")

        n_cv = min(n_cv_splits, 5)
        tscv = TimeSeriesSplit(n_splits=n_cv)
        X_arr = np.asarray(X)
        y_arr = np.asarray(y)
        n_samples = len(X_arr)

        meta_parts: List[np.ndarray] = []

        for i, model in enumerate(base_models):
            try:
                # 手动循环时序CV — 比 cross_val_predict 更可靠
                n_classes = 3  # H/D/A
                cv_proba = np.full((n_samples, n_classes), np.nan, dtype=np.float64)

                for train_idx, test_idx in tscv.split(X_arr):
                    model.fit(X_arr[train_idx], y_arr[train_idx])
                    cv_proba[test_idx] = model.predict_proba(X_arr[test_idx])

                # 跳过未覆盖的样本（前几折无训练数据的部分）
                valid_mask = ~np.isnan(cv_proba[:, 0])
                if not valid_mask.any():
                    logger.warning(
                        f"  ⚠ base_model[{i}] 无有效CV预测，跳过"
                    )
                    continue

                # 对 NaN 行用 1/n_classes 填充
                cv_proba[~valid_mask] = 1.0 / n_classes

            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                logger.warning(
                    f"  ⚠ base_model[{i}] ({type(model).__name__}) "
                    f"CV预测失败: {exc}"
                )
                continue

            for j in range(cv_proba.shape[1]):
                meta_parts.append(cv_proba[:, j:j + 1].copy())

            logger.debug(f"  model[{i}] → {cv_proba.shape[1]} 维概率")

        if not meta_parts:
            raise RuntimeError("create_meta_features: 所有 base_models 均失败")

        meta_X = np.hstack(meta_parts)
        logger.info(f"  ✓ 元特征矩阵: {meta_X.shape}")

        return meta_X.astype(np.float32)

    # ──────────────────────────────────────────────
    # Convenience: full pipeline summary
    # ──────────────────────────────────────────────

    def run_full_pipeline(
        self,
        X: np.ndarray,
        y: np.ndarray,
        dates: np.ndarray,
        feature_names: List[str],
        model_builder,
    ) -> Dict:
        """运行完整自适应训练管道: 验证 + 特征选择 + 权重"""
        result: Dict = {}

        # 1) 渐进式验证
        result['progressive'] = self.progressive_validation(
            X, y, dates, model_builder, feature_names,
        )

        # 2) 自适应特征选择
        selected, stability = self.adaptive_feature_selection(
            X, y, dates, feature_names,
        )
        result['selected_features'] = selected
        result['feature_stability'] = stability

        # 3) 动态权重
        result['class_weights'] = self.dynamic_class_weighting(y)

        return result

    # ──────────────────────────────────────────────
    # v7.1 新增: 实际重训 + 早停 + 学习率调度
    # ──────────────────────────────────────────────

    def retrain_with_selected_features(
        self,
        X: np.ndarray,
        y: np.ndarray,
        dates: np.ndarray,
        feature_names: List[str],
        selected_features: List[str],
        class_weights: Optional[Dict[int, float]] = None,
        early_stopping_rounds: int = 20,
        validation_size: float = 0.15,
    ) -> Tuple[object, Dict]:
        """v7.1 升级: 用筛选后的特征 + 优化权重重新训练 XGBoost

        相比旧版仅分析不做动作，此方法:
        1. 仅使用稳定特征子集构建 X
        2. 应用动态类别权重到 sample_weight
        3. 时序分割确保训练集在前、验证集在后
        4. 早停 (early_stopping) 防止过拟合
        5. 返回重训后的模型 + 评估指标

        Returns
        -------
        (retrained_model, evaluation_metrics)
        """
        logger.info("[ADAPTIVE-v7.1] 用稳定特征重训模型...")

        # 1) 构建特征子集
        if selected_features and len(selected_features) < len(feature_names):
            selected_indices = []
            for feat in selected_features:
                try:
                    selected_indices.append(feature_names.index(feat))
                except ValueError:
                    pass  # 特征名不在列表中时跳过
            if selected_indices:
                X = X[:, selected_indices]
                feature_names = selected_features
                logger.info(f"  特征压缩: {X.shape[1]} 个稳定特征")

        # 2) 时序分割: train (前85%) / val (后15%)
        dates_dt = pd.to_datetime(dates)
        sorted_idx = np.argsort(dates_dt)
        X_sorted = X[sorted_idx]
        y_sorted = y[sorted_idx]

        split_idx = int(len(X_sorted) * (1 - validation_size))
        X_tr, X_val = X_sorted[:split_idx], X_sorted[split_idx:]
        y_tr, y_val = y_sorted[:split_idx], y_sorted[split_idx:]

        logger.info(f"  训练集: {len(X_tr):,} | 验证集: {len(X_val):,}")

        # 3) 构建样本权重 (类别权重)
        sample_weight = None
        if class_weights:
            sample_weight = np.array([class_weights.get(int(cls), 1.0) for cls in y_tr])
            logger.info(f"  应用类别权重: {class_weights}")

        # 4) 训练模型 (带早停)
        model = xgb.XGBClassifier(
            n_estimators=500,  # 大上限，依赖早停
            max_depth=5,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
            verbosity=0,
            n_jobs=-1,
            early_stopping_rounds=early_stopping_rounds,
        )

        model.fit(
            X_tr, y_tr,
            sample_weight=sample_weight,
            eval_set=[(X_val, y_val)],
            verbose=False,
        )

        # 5) 评估
        y_pred_val = model.predict(X_val)
        y_pred_train = model.predict(X_tr)
        proba_val = model.predict_proba(X_val)

        metrics = {
            'train_accuracy': float(accuracy_score(y_tr, y_pred_train)),
            'val_accuracy': float(accuracy_score(y_val, y_pred_val)),
            'val_log_loss': float(log_loss(y_val, proba_val)),
            'val_draw_f1': float(f1_score(y_val == 1, y_pred_val == 1, zero_division=0)),
            'n_estimators_used': int(model.best_iteration + 1) if hasattr(model, 'best_iteration') else 500,
            'n_features': X.shape[1],
            'overfit_gap': float(accuracy_score(y_tr, y_pred_train) - accuracy_score(y_val, y_pred_val)),
        }

        logger.info(
            f"  ✓ 重训完成 — val_acc={metrics['val_accuracy']:.4f}  "
            f"draw_f1={metrics['val_draw_f1']:.4f}  "
            f"overfit_gap={metrics['overfit_gap']:.4f}"
        )

        return model, metrics

    def train_with_lr_schedule(
        self,
        X: np.ndarray,
        y: np.ndarray,
        dates: np.ndarray,
        learning_rates: Optional[List[float]] = None,
        early_stopping_rounds: int = 20,
    ) -> Tuple[object, Dict]:
        """v7.1 新增: 带学习率调度的训练 (warmup + cosine decay)

        先小学习率 warmup，再主学习率训练，最后低学习率微调。
        """
        if learning_rates is None:
            learning_rates = [0.01, 0.05, 0.02]  # warmup, main, finetune

        dates_dt = pd.to_datetime(dates)
        sorted_idx = np.argsort(dates_dt)
        X_sorted = X[sorted_idx]
        y_sorted = y[sorted_idx]

        split_idx = int(len(X_sorted) * 0.85)
        X_tr, X_val = X_sorted[:split_idx], X_sorted[split_idx:]

        best_model = None
        best_val_acc = 0.0
        all_metrics = []

        for stage, lr in enumerate(learning_rates):
            n_est = [100, 500, 300][stage] if stage < 3 else 300
            md = [3, 6, 5][stage] if stage < 3 else 5

            model = xgb.XGBClassifier(
                n_estimators=n_est,
                max_depth=md,
                learning_rate=lr,
                subsample=0.8,
                colsample_bytree=0.8,
                random_state=42 + stage,
                verbosity=0,
                n_jobs=-1,
                early_stopping_rounds=early_stopping_rounds,
            )

            model.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=False)
            val_acc = float(model.score(X_val, y_val))

            stage_metrics = {
                'stage': stage,
                'learning_rate': lr,
                'n_estimators': n_est,
                'max_depth': md,
                'val_accuracy': val_acc,
                'best_iteration': int(model.best_iteration + 1) if hasattr(model, 'best_iteration') else n_est,
            }
            all_metrics.append(stage_metrics)

            if val_acc > best_val_acc:
                best_val_acc = val_acc
                best_model = model

            logger.info(f"  LR阶段{stage}: lr={lr} md={md} → val_acc={val_acc:.4f}")

        return best_model, {
            'best_val_accuracy': best_val_acc,
            'stage_metrics': all_metrics,
            'n_stages': len(learning_rates),
        }

    def compare_retrained_vs_baseline(
        self,
        X_val: np.ndarray,
        y_val: np.ndarray,
        baseline_model,
        retrained_model,
    ) -> Dict:
        """v7.1 新增: 对比基线模型 vs 重训模型"""
        baseline_pred = baseline_model.predict(X_val)
        baseline_proba = (
            baseline_model.predict_proba(X_val)
            if hasattr(baseline_model, 'predict_proba') else None
        )

        retrained_pred = retrained_model.predict(X_val)
        retrained_proba = retrained_model.predict_proba(X_val)

        comparison = {
            'baseline': {
                'accuracy': float(accuracy_score(y_val, baseline_pred)),
                'draw_f1': float(f1_score(y_val == 1, baseline_pred == 1, zero_division=0)),
            },
            'retrained': {
                'accuracy': float(accuracy_score(y_val, retrained_pred)),
                'draw_f1': float(f1_score(y_val == 1, retrained_pred == 1, zero_division=0)),
                'log_loss': float(log_loss(y_val, retrained_proba)) if retrained_proba is not None else None,
            },
        }

        # 计算改进
        for metric in ['accuracy', 'draw_f1']:
            base_val = comparison['baseline'][metric]
            ret_val = comparison['retrained'][metric]
            comparison[f'{metric}_improvement'] = float(ret_val - base_val)
            comparison[f'{metric}_improvement_pct'] = float((ret_val - base_val) / max(base_val, 1e-6) * 100)

        logger.info(
            f"  对比: baseline_acc={comparison['baseline']['accuracy']:.4f} → "
            f"retrained_acc={comparison['retrained']['accuracy']:.4f} "
            f"({comparison['accuracy_improvement_pct']:+.2f}%)"
        )

        return comparison

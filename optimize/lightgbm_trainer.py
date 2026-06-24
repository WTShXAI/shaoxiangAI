"""
哨响AI - LightGBM 模型训练器 (T08)
===================================
基于 LightGBM 梯度提升决策树，产出多分类概率预测。

特性：
- 与 XGBoost 共享统一的 GBDTDataAdapter 数据格式
- Early Stopping + Bagging 防过拟合
- 平衡类别权重 + 平局专项优化
- 概率校准 (Isotonic)
- Joblib 持久化

用法:
    trainer = LightGBMTrainer()
    bundle = make_training_bundle()
    eval_result = trainer.train(bundle)
    proba = trainer.predict_proba(X_test)  # → (n, 3)
    trainer.save_model('lightgbm_v1.joblib')
"""

import os, time, logging
from typing import Dict, List, Optional, Tuple

import numpy as np
import joblib

from sklearn.preprocessing import StandardScaler
from sklearn.metrics import (
    accuracy_score, classification_report, brier_score_loss,
    log_loss, confusion_matrix, matthews_corrcoef,
)
from sklearn.calibration import CalibratedClassifierCV
from sklearn.utils.class_weight import compute_class_weight

from optimize.gbdt_adapter import TrainingBundle

logger = logging.getLogger(__name__)

_LGB_AVAILABLE = False
try:
    import lightgbm as lgb
    _LGB_AVAILABLE = True
except ImportError:
    pass


# ─── 默认超参数 ─────────────────────────────────────────────

DEFAULT_LGB_PARAMS: Dict = {
    'objective': 'multiclass',
    'num_class': 3,
    'metric': 'multi_logloss',
    'boosting_type': 'gbdt',
    'num_leaves': 31,
    'max_depth': -1,           # LightGBM 用 num_leaves 控制复杂度
    'learning_rate': 0.05,
    'n_estimators': 500,
    'subsample': 0.8,
    'subsample_freq': 1,
    'colsample_bytree': 0.8,
    'min_child_samples': 20,
    'min_child_weight': 1e-3,
    'reg_alpha': 0.0,
    'reg_lambda': 0.0,
    'min_split_gain': 0.01,
    'verbose': -1,
    'random_state': 42,
    'n_jobs': -1,
    # [NEW] 针对足球数据的优化
    'class_weight': 'balanced',
    'num_iterations': 500,
    'early_stopping_rounds': 50,
    'is_unbalance': False,
}


class LightGBMTrainer:
    """
    LightGBM 足球预测模型训练器。
    对外 API 与 XGBoost 端统一: train() / predict_proba() / predict() / save() / load()
    """

    def __init__(
        self,
        lgb_params: Optional[Dict] = None,
        calibrate: bool = False,
        random_state: int = 42,
    ):
        if not _LGB_AVAILABLE:
            raise ImportError("lightgbm 未安装，请执行: pip install lightgbm")

        self.lgb_params = {**DEFAULT_LGB_PARAMS, **(lgb_params or {})}
        self.lgb_params['random_state'] = random_state
        self.calibrate = calibrate
        self.random_state = random_state

        self.model = None
        self.scaler: Optional[StandardScaler] = None
        self.feature_names: List[str] = []
        self._eval_result: Dict = {}
        self._best_iteration = 0
        self._class_names = ['home', 'draw', 'away']
        self._trained = False

    # ─── 训练 ──────────────────────────────────────────────

    def train(
        self,
        bundle: TrainingBundle,
        custom_params: Optional[Dict] = None,
    ) -> Dict:
        """
        使用 TrainingBundle 训练 LightGBM 模型。

        Args:
            bundle: GBDTDataAdapter 产出的统一数据包
            custom_params: 可选超参数覆盖

        Returns:
            dict: 评估指标
        """
        self.scaler = bundle.scaler
        self.feature_names = bundle.feature_names

        params = {**self.lgb_params}
        if custom_params:
            params.update(custom_params)

        # 类别权重 → sample_weight
        class_w = bundle.class_weights_train
        sample_w = np.array([class_w[int(c)] for c in bundle.y_train])

        logger.info(f"[LGBM] 开始训练 | 样本={bundle.train_size} "
                     f"特征={len(self.feature_names)} "
                     f"num_leaves={params.get('num_leaves')} "
                     f"lr={params.get('learning_rate')}")

        t0 = time.time()

        # LightGBM 专用参数映射
        train_params = {
            'objective': params.get('objective', 'multiclass'),
            'num_class': params.get('num_class', 3),
            'metric': params.get('metric', 'multi_logloss'),
            'boosting_type': params.get('boosting_type', 'gbdt'),
            'num_leaves': params.get('num_leaves', 31),
            'max_depth': params.get('max_depth', -1),
            'learning_rate': params.get('learning_rate', 0.05),
            'subsample': params.get('subsample', 0.8),
            'subsample_freq': params.get('subsample_freq', 1),
            'colsample_bytree': params.get('colsample_bytree', 0.8),
            'min_child_samples': params.get('min_child_samples', 20),
            'reg_alpha': params.get('reg_alpha', 0.0),
            'reg_lambda': params.get('reg_lambda', 0.0),
            'min_split_gain': params.get('min_split_gain', 0.01),
            'verbose': params.get('verbose', -1),
            'random_state': params.get('random_state', self.random_state),
            'n_jobs': params.get('n_jobs', -1),
        }

        model = lgb.LGBMClassifier(**train_params)

        model.fit(
            bundle.X_train, bundle.y_train,
            sample_weight=sample_w,
            eval_set=[(bundle.X_val, bundle.y_val)],
            eval_metric=['multi_logloss', 'multi_error'],
            callbacks=[
                lgb.early_stopping(
                    stopping_rounds=params.get('early_stopping_rounds', 50),
                    verbose=False,
                ),
                lgb.log_evaluation(period=0),
            ],
        )

        train_time = time.time() - t0

        # 最佳迭代
        self._best_iteration = getattr(model, 'best_iteration_', model.n_estimators_)
        best_score = getattr(model, 'best_score_', {}).get('valid_0', {}).get('multi_logloss', 0)

        logger.info(f"[LGBM] 训练完成 ({train_time:.0f}s) "
                     f"best_iter={self._best_iteration} "
                     f"best_score={best_score:.4f}")

        # 概率校准（可选）
        if self.calibrate:
            t1 = time.time()
            cal_params = {k: v for k, v in train_params.items()
                          if k not in ('early_stopping_rounds', 'eval_set')}
            base = lgb.LGBMClassifier(**cal_params)
            calibrated = CalibratedClassifierCV(
                estimator=base, method='isotonic', cv=3, n_jobs=1,
            )
            calibrated.fit(bundle.X_train, bundle.y_train, sample_weight=sample_w)
            self.model = calibrated
            logger.info(f"[LGBM] 概率校准完成 ({time.time()-t1:.0f}s)")
        else:
            self.model = model

        self._trained = True

        # 评估
        self._eval_result = self.evaluate(bundle)
        return self._eval_result

    # ─── 预测 ──────────────────────────────────────────────

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """预测概率: (n, 3) → [P_home, P_draw, P_away]"""
        if self.model is None:
            raise RuntimeError("模型未训练或未加载")
        return self.model.predict_proba(X)

    def predict(self, X: np.ndarray) -> np.ndarray:
        """硬分类预测"""
        if self.model is None:
            raise RuntimeError("模型未训练或未加载")
        return self.model.predict(X)

    # ─── 评估 ──────────────────────────────────────────────

    def evaluate(self, bundle: TrainingBundle) -> Dict:
        """在测试集上评估模型"""
        if self.model is None:
            raise RuntimeError("模型未训练或未加载")

        X_test = bundle.X_test
        y_test = bundle.y_test

        y_pred = self.model.predict(X_test)
        y_proba = self.model.predict_proba(X_test)

        acc = accuracy_score(y_test, y_pred) * 100

        # 自适应平局阈值
        actual_draw_rate = (y_test == 1).mean()
        effective_thresh = max(actual_draw_rate * 1.1, 0.20)
        y_pred_adj = y_pred.copy()
        for i in range(len(y_pred_adj)):
            if y_pred_adj[i] == 1 and y_proba[i, 1] < effective_thresh:
                y_pred_adj[i] = 0 if y_proba[i, 0] >= y_proba[i, 2] else 2
        acc_adj = accuracy_score(y_test, y_pred_adj) * 100

        report = classification_report(
            y_test, y_pred_adj,
            target_names=self._class_names,
            output_dict=True,
            zero_division=0,
        )

        # Brier
        y_onehot = np.zeros((len(y_test), 3))
        for i, c in enumerate(y_test):
            y_onehot[i, int(c)] = 1
        brier = np.mean([
            brier_score_loss(y_onehot[:, i], y_proba[:, i])
            for i in range(3)
        ])

        ll = log_loss(y_test, y_proba)
        mcc = matthews_corrcoef(y_test, y_pred_adj)
        cm = confusion_matrix(y_test, y_pred_adj)

        result = {
            'accuracy_pct': round(acc_adj, 2),
            'raw_accuracy_pct': round(acc, 2),
            'draw_recall_pct': round(report['draw']['recall'] * 100, 2),
            'draw_precision_pct': round(report['draw']['precision'] * 100, 2),
            'draw_f1_pct': round(report['draw']['f1-score'] * 100, 2),
            'home_recall_pct': round(report['home']['recall'] * 100, 2),
            'away_recall_pct': round(report['away']['recall'] * 100, 2),
            'brier_score': round(brier, 4),
            'log_loss': round(ll, 4),
            'mcc': round(mcc, 4),
            'confusion_matrix': cm.tolist(),
            'best_iteration': self._best_iteration,
            'pred_draw_pct': round(np.mean(y_pred_adj == 1) * 100, 2),
            'actual_draw_pct': round((y_test == 1).mean() * 100, 2),
            'num_leaves': self.lgb_params.get('num_leaves', 31),
            'n_features': len(self.feature_names),
        }
        return result

    # ─── 持久化 ────────────────────────────────────────────

    def save_model(self, filepath: str) -> str:
        """保存完整模型管道到 .joblib"""
        if self.model is None:
            raise RuntimeError("无模型可保存")

        os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)

        pipeline = {
            'model': self.model,
            'scaler': self.scaler,
            'feature_names': self.feature_names,
            'params': self.lgb_params,
            'eval_result': self._eval_result,
            'best_iteration': self._best_iteration,
            'model_type': 'lightgbm',
            'version': '1.0',
        }
        joblib.dump(pipeline, filepath, compress=3)
        logger.info(f"[LGBM] 模型已保存: {filepath} ({os.path.getsize(filepath)/1024:.0f} KB)")
        return filepath

    @classmethod
    def load_model(cls, filepath: str) -> 'LightGBMTrainer':
        """从 .joblib 加载模型"""
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"模型文件不存在: {filepath}")

        pipeline = joblib.load(filepath)
        trainer = cls(
            lgb_params=pipeline.get('params', {}),
            calibrate=False,
        )
        trainer.model = pipeline['model']
        trainer.scaler = pipeline.get('scaler')
        trainer.feature_names = pipeline.get('feature_names', [])
        trainer._eval_result = pipeline.get('eval_result', {})
        trainer._best_iteration = pipeline.get('best_iteration', 0)
        trainer._trained = True

        logger.info(f"[LGBM] 模型已加载: {filepath} "
                     f"(类型={pipeline.get('model_type', 'unknown')})")
        return trainer

    # ─── 特征重要性 ────────────────────────────────────────

    def feature_importance(self, top_k: int = 20) -> Dict:
        """获取特征重要性"""
        if self.model is None:
            return {}

        if hasattr(self.model, 'feature_importances_'):
            importances = self.model.feature_importances_
        elif hasattr(self.model, 'estimators_') and len(self.model.estimators_) > 0:
            importances = self.model.estimators_[0].feature_importances_
        else:
            return {}

        named = sorted(
            zip(self.feature_names[:len(importances)], importances),
            key=lambda x: x[1], reverse=True,
        )[:top_k]
        return {name: round(val, 4) for name, val in named}

    # ─── 便捷类方法 ────────────────────────────────────────

    @classmethod
    def train_from_bundle(
        cls,
        bundle: TrainingBundle,
        params: Optional[Dict] = None,
    ) -> 'LightGBMTrainer':
        """从 TrainingBundle 一键训练"""
        trainer = cls(lgb_params=params)
        trainer.train(bundle)
        return trainer

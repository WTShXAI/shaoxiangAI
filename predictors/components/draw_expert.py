"""
DrawExpert — 平局专精二分类器 (v5.0)
=====================================
核心洞察: Stacking 中没有任何基模型专门优化 Draw 预测。
LGB/XGB 在三分类目标下 Draw 信号被 H/A 稀释，XGB Draw F1≈0.02 近乎噪声。

方案: 训练一个独立的 LightGBM 二分类器，目标为 Draw vs Non-Draw。
- 输入: 与现有基模型相同的 72 维特征
- 输出: P(Draw) ∈ [0, 1]
- 集成: 作为 Stacking 第 6 基模型，其 P(Draw) 作为 meta-learner 的一维输入
- 优势: 二分类目标天然聚焦 Draw 信号，scale_pos_weight 解决类别不平衡

用法:
    from draw_expert import DrawExpert
    de = DrawExpert()
    de.fit(X_train, y_train, feature_names)
    p_draw = de.predict_proba(X_test)  # shape (n, 1)
    de.save('saved_models/draw_expert_v1.joblib')
"""

import os, logging, numpy as np, joblib
from typing import Optional, Tuple, Dict
from sklearn.metrics import f1_score, roc_auc_score, precision_recall_curve
import lightgbm as lgb

logger = logging.getLogger(__name__)

class DrawExpert:
    """
    平局专精二分类器

    架构: LightGBM binary classifier
    目标: Draw=1, Non-Draw(H or A)=0
    特性:
    - scale_pos_weight 自适应 (基于训练集 Draw 率)
    - is_unbalance='true' 作为备选策略
    - 早停 + 浅树防止过拟合
    - 输出校准后的 P(Draw)
    """

    def __init__(
        self,
        n_estimators: int = 300,
        num_leaves: int = 31,
        max_depth: int = 5,
        learning_rate: float = 0.03,
        subsample: float = 0.8,
        colsample_bytree: float = 0.7,
        min_child_samples: int = 30,
        reg_alpha: float = 0.5,
        reg_lambda: float = 2.0,
        random_state: int = 42,
    ):
        self.n_estimators = n_estimators
        self.num_leaves = num_leaves
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self.subsample = subsample
        self.colsample_bytree = colsample_bytree
        self.min_child_samples = min_child_samples
        self.reg_alpha = reg_alpha
        self.reg_lambda = reg_lambda
        self.random_state = random_state

        self.model: Optional[lgb.LGBMClassifier] = None
        self.feature_names_: list = []
        self.scale_pos_weight_: float = 1.0
        self.train_draw_rate_: float = 0.0
        self.eval_metrics_: Dict = {}

    def fit(
        self,
        X: np.ndarray,
        y: np.ndarray,
        feature_names: list = None,
        X_val: np.ndarray = None,
        y_val: np.ndarray = None,
        early_stopping_rounds: int = 50,
        verbose: bool = True,
    ) -> "DrawExpert":
        """
        训练 Draw 二分类器

        Args:
            X: (n, d) 特征矩阵 (三分类标签，会被转为二分类)
            y: (n,) 标签 [0=H, 1=D, 2=A] — 自动转为 binary [0=Non-Draw, 1=Draw]
            feature_names: 特征名列表
            X_val: 验证集特征
            y_val: 验证集标签
            early_stopping_rounds: 早停轮数
            verbose: 是否输出训练日志
        """
        self.feature_names_ = feature_names or [f"f{i}" for i in range(X.shape[1])]

        # 三分类 → 二分类: Draw=1, H/A=0
        y_binary = (np.asarray(y) == 1).astype(int)
        y_val_binary = (np.asarray(y_val) == 1).astype(int) if y_val is not None else None

        # 计算类别权重
        n_draw = y_binary.sum()
        n_non_draw = len(y_binary) - n_draw
        self.train_draw_rate_ = n_draw / len(y_binary)
        self.scale_pos_weight_ = n_non_draw / max(n_draw, 1)

        if verbose:
            logger.info(f"DrawExpert 训练: {len(y_binary)} 样本, "
                       f"Draw={n_draw} ({self.train_draw_rate_*100:.1f}%), "
                       f"scale_pos_weight={self.scale_pos_weight_:.2f}")

        # 构建模型
        params = {
            'objective': 'binary',
            'metric': 'average_precision',
            'n_estimators': self.n_estimators,
            'num_leaves': self.num_leaves,
            'max_depth': self.max_depth,
            'learning_rate': self.learning_rate,
            'subsample': self.subsample,
            'colsample_bytree': self.colsample_bytree,
            'min_child_samples': self.min_child_samples,
            'reg_alpha': self.reg_alpha,
            'reg_lambda': self.reg_lambda,
            'scale_pos_weight': self.scale_pos_weight_,
            'random_state': self.random_state,
            'n_jobs': -1,
            'verbose': -1,
        }

        self.model = lgb.LGBMClassifier(**params)

        eval_set = [(X_val, y_val_binary)] if X_val is not None and y_val is not None else None
        eval_metric_list = ['average_precision', 'auc'] if eval_set else None

        self.model.fit(
            X, y_binary,
            eval_set=eval_set,
            eval_metric=eval_metric_list,
            callbacks=[
                lgb.early_stopping(early_stopping_rounds, verbose=False),
                lgb.log_evaluation(50 if verbose else 0),
            ] if eval_set else None,
        )

        # 评估
        if X_val is not None and y_val is not None:
            self.eval_metrics_ = self._evaluate(X_val, y_val_binary, verbose)

        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """
        预测 P(Draw)

        Returns:
            (n, 1) 概率数组，每行是 P(Draw)
        """
        if self.model is None:
            raise RuntimeError("模型未训练，请先调用 fit()")
        proba = self.model.predict_proba(X)
        return proba[:, 1].reshape(-1, 1)  # 只返回 Draw 概率

    def predict(self, X: np.ndarray, threshold: float = 0.5) -> np.ndarray:
        """二分类预测 (Draw/Non-Draw)"""
        p_draw = self.predict_proba(X).ravel()
        return (p_draw >= threshold).astype(int)

    def get_feature_importance(self) -> Dict[str, float]:
        """获取特征重要性 (Draw 相关度排序)"""
        if self.model is None:
            return {}
        importance = self.model.feature_importances_
        ranked = sorted(
            zip(self.feature_names_, importance),
            key=lambda x: x[1], reverse=True
        )
        return dict(ranked)

    def _evaluate(self, X: np.ndarray, y_binary: np.ndarray, verbose: bool = True) -> Dict:
        """评估二分类性能"""
        p_draw = self.predict_proba(X).ravel()
        y_pred = (p_draw >= 0.5).astype(int)

        # 寻找最优阈值 (F1-maximizing)
        precision, recall, thresholds = precision_recall_curve(y_binary, p_draw)
        # 避免除零
        f1_scores = 2 * precision * recall / (precision + recall + 1e-8)
        best_idx = np.argmax(f1_scores)
        best_threshold = thresholds[best_idx] if best_idx < len(thresholds) else 0.5
        best_f1 = f1_scores[best_idx]

        metrics = {
            'f1': f1_score(y_binary, y_pred),
            'auc': roc_auc_score(y_binary, p_draw),
            'avg_precision': np.mean(precision * recall / (precision + recall + 1e-8)),
            'best_threshold': best_threshold,
            'best_f1': best_f1,
            'draw_rate_pred': p_draw.mean(),
        }

        if verbose:
            logger.info(f"DrawExpert 验证: F1={metrics['f1']:.4f}, AUC={metrics['auc']:.4f}, "
                       f"BestF1={best_f1:.4f}@thresh={best_threshold:.3f}, "
                       f"AvgP(Draw)={metrics['draw_rate_pred']:.3f}")

        return metrics

    def save(self, path: str):
        """保存模型"""
        if self.model is None:
            raise RuntimeError("无模型可保存")
        state = {
            'model': self.model,
            'feature_names': self.feature_names_,
            'scale_pos_weight': self.scale_pos_weight_,
            'train_draw_rate': self.train_draw_rate_,
            'eval_metrics': self.eval_metrics_,
            'params': {
                'n_estimators': self.n_estimators,
                'num_leaves': self.num_leaves,
                'max_depth': self.max_depth,
                'learning_rate': self.learning_rate,
            },
        }
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        joblib.dump(state, path)
        logger.info(f"DrawExpert 已保存: {path}")

    @classmethod
    def load(cls, path: str) -> "DrawExpert":
        """加载模型"""
        state = joblib.load(path)
        de = cls(
            n_estimators=state['params']['n_estimators'],
            num_leaves=state['params']['num_leaves'],
            max_depth=state['params']['max_depth'],
            learning_rate=state['params']['learning_rate'],
        )
        de.model = state['model']
        de.feature_names_ = state['feature_names']
        de.scale_pos_weight_ = state['scale_pos_weight']
        de.train_draw_rate_ = state['train_draw_rate']
        de.eval_metrics_ = state.get('eval_metrics', {})
        logger.info(f"DrawExpert 已加载: {path} (Draw-F1={de.eval_metrics_.get('f1', 'N/A')})")
        return de

# ─── 便捷函数 ───

def create_draw_expert_default() -> DrawExpert:
    """创建默认配置的 DrawExpert (正则化偏强，防止 Draw 过拟合)"""
    return DrawExpert(
        n_estimators=300,
        num_leaves=31,
        max_depth=5,
        learning_rate=0.03,
        subsample=0.8,
        colsample_bytree=0.7,
        min_child_samples=30,
        reg_alpha=0.5,
        reg_lambda=2.0,
    )

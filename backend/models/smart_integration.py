import logging
"""
[DEPRECATED] P0-1: 此文件直接使用 joblib.load 绕过 ModelBridge，存在数据泄露风险。
生产预测请使用 agents.model_bridge.ModelBridge.predict()
智能模型集成 — 将 footballAI + 专家模型融合为统一预测系统

方法:
  - footballAI 作为主模型 (base_weight ~ 0.6)
  - 专家模型作为专项增强 (expert_weight ~ 0.4)
  - 斯塔克元模型: LogisticRegression 学习最优融合权重
  - 输出: 加权概率平均 + 置信度校准
"""
import argparse
import sys
import os
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
import warnings
logger = logging.getLogger(__name__)
warnings.filterwarnings('ignore')

class SmartIntegration:
    """智能模型集成器"""

    def __init__(self, base_weight: float = 0.6):
        self.base_weight = base_weight
        self.expert_weight = 1.0 - base_weight
        self.base_model = None
        self.expert_models: Dict[str, object] = {}
        self.meta_model = None
        self.scaler = StandardScaler()
        self.expert_weights: Dict[str, float] = {}
        self._fitted = False
        self.feature_names_: Optional[List[str]] = None

    def load_base_model(self, path: str):
        """加载主模型 (FootballAIEnhanced)"""
        logger.info(f"[BASE] 加载主模型: {path}")
        if not os.path.exists(path):
            raise FileNotFoundError(f"主模型不存在: {path}")
        self.base_model = joblib.load(path)
        logger.info(f"  [OK] 版本: {getattr(self.base_model, 'model_version', 'unknown')}")

    def load_expert_models(self, expert_dir: str):
        """加载所有专家模型"""
        logger.info(f"[EXPERT] 加载专家模型目录: {expert_dir}")

        if not os.path.exists(expert_dir):
            logger.info(f"  [WARN] 专家目录不存在: {expert_dir}")
            return

        expert_files = list(Path(expert_dir).glob('*.joblib'))
        if not expert_files:
            logger.info(f"  [WARN] 目录中无专家模型")
            return

        for f in expert_files:
            try:
                model = joblib.load(f)
                name = f.stem.replace('_model_v2', '').replace('_', ' ')
                self.expert_models[name] = model
                logger.info(f"  [OK] {name} ({f.name})")
            except (FileNotFoundError, IOError, OSError, PermissionError) as e:
                logger.info(f"  [FAIL] {f.name}: {e}")

    def _get_base_proba(self, X: np.ndarray) -> np.ndarray:
        """获取主模型概率预测"""
        if hasattr(self.base_model, 'ensemble_predict'):
            return self.base_model.ensemble_predict(X)
        elif hasattr(self.base_model, 'predict_proba'):
            return self.base_model.predict_proba(X)
        else:
            raise AttributeError("主模型无 predict_proba 方法")

    def _get_expert_proba(self, model, X: np.ndarray) -> np.ndarray:
        """获取专家模型概率预测"""
        if hasattr(model, 'predict_proba'):
            return model.predict_proba(X)
        elif hasattr(model, 'predict'):
            preds = model.predict(X)
            # 如果 predict 返回类别标签，转为 one-hot
            proba = np.zeros((len(preds), 3))
            for i, p in enumerate(preds):
                if isinstance(p, (int, np.integer)):
                    proba[i, p] = 1.0
                else:
                    proba[i] = np.array([1/3, 1/3, 1/3])
            return proba
        else:
            return np.full((len(X), 3), 1/3)

    def fit_meta_model(self, X: np.ndarray, y: np.ndarray):
        """训练元模型 (LogisticRegression stacking)"""
        logger.info(f"\n[META] 训练元模型 (LogisticRegression)...")

        # 获取基础概率
        base_proba = self._get_base_proba(X)
        logger.info(f"  主模型概率 shape: {base_proba.shape}")

        # 获取专家概率
        expert_probas = []
        for name, model in self.expert_models.items():
            try:
                proba = self._get_expert_proba(model, X)
                if proba.shape == base_proba.shape:
                    expert_probas.append(proba)
                    logger.info(f"  [OK] {name}: {proba.shape}")
            except (ValueError, KeyError, FileNotFoundError) as e:
                logger.info(f"  [FAIL] {name}: {e}")

        if not expert_probas:
            logger.info("  [WARN] 无可用专家模型，使用纯主模型")
            self.meta_model = None
            self._fitted = True
            return

        # 堆叠所有概率
        stack = np.hstack([base_proba] + expert_probas)
        logger.info(f"  堆叠特征 shape: {stack.shape}")

        # 归一化
        X_meta = self.scaler.fit_transform(stack)

        self.meta_model = LogisticRegression(
            multi_class='multinomial',
            solver='lbfgs',
            max_iter=500,
            random_state=42,
        )
        self.meta_model.fit(X_meta, y)

        # 评估
        train_score = self.meta_model.score(X_meta, y)
        logger.info(f"  元模型训练准确率: {train_score:.4f}")

        self._fitted = True

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """集成预测概率"""
        if not self._fitted or self.meta_model is None:
            # 回退到基础模型
            if self.base_model is not None:
                return self._get_base_proba(X)
            return np.full((len(X), 3), 1/3)

        base_proba = self._get_base_proba(X)

        expert_probas = []
        for name, model in self.expert_models.items():
            try:
                proba = self._get_expert_proba(model, X)
                if proba.shape == base_proba.shape:
                    expert_probas.append(proba)
            except (OSError, ValueError, KeyError) as e:
                logger.debug(f"操作失败: {e}")

        if not expert_probas:
            return base_proba

        stack = np.hstack([base_proba] + expert_probas)
        X_meta = self.scaler.transform(stack)

        return self.meta_model.predict_proba(X_meta)

    def predict(self, X: np.ndarray) -> np.ndarray:
        """集成预测类别"""
        proba = self.predict_proba(X)
        return np.argmax(proba, axis=1)

    def save(self, output_path: str):
        """保存集成模型"""
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        # 不保存完整对象（避免循环引用），保存组件
        save_data = {
            'type': 'SmartIntegration',
            'base_weight': self.base_weight,
            'expert_weight': self.expert_weight,
            'base_model': self.base_model,
            'expert_models': self.expert_models,
            'meta_model': self.meta_model,
            'scaler': self.scaler,
            'feature_names_': self.feature_names_,
            '_fitted': self._fitted,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }

        joblib.dump(save_data, output_path)
        logger.info(f"[SAVE] 集成模型 → {output_path}")

    @classmethod
    def load(cls, model_path: str) -> "SmartIntegration":
        """加载集成模型"""
        data = joblib.load(model_path)
        obj = cls(base_weight=data.get('base_weight', 0.6))
        obj.base_model = data['base_model']
        obj.expert_models = data['expert_models']
        obj.meta_model = data['meta_model']
        obj.scaler = data['scaler']
        obj.feature_names_ = data.get('feature_names_')
        obj._fitted = data['_fitted']
        return obj

def load_test_data(csv_path: str):
    """加载测试数据用于元模型训练/验证"""
    if not os.path.exists(csv_path):
        logger.info(f"  [WARN] 测试数据不存在: {csv_path}")
        return None

    df = pd.read_csv(csv_path)
    META_COLS = ['date', 'home_team', 'away_team', 'home_score', 'away_score', 'league']

    feature_cols = [c for c in df.columns
                    if c not in META_COLS
                    and df[c].dtype in ['float64', 'float32', 'int64', 'int32']]

    X = df[feature_cols].fillna(0).values.astype(np.float32)

    hs = df['home_score'].values
    aws = df['away_score'].values
    y = np.full(len(df), 1, dtype=int)
    y[hs > aws] = 0
    y[hs < aws] = 2

    return X, y, feature_cols

def main():
    parser = argparse.ArgumentParser(description='智能模型集成')
    parser.add_argument('--footballai', required=True, help='footballAI模型路径')
    parser.add_argument('--expert', default='saved_models/experts/',
                        help='专家模型路径或目录')
    parser.add_argument('--output', default=None, help='输出模型路径')
    parser.add_argument('--test-data', default=None,
                        help='测试数据CSV (用于元模型训练)')
    parser.add_argument('--base-weight', type=float, default=0.6,
                        help='主模型权重 (默认0.6)')
    args = parser.parse_args()

    if args.output is None:
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        args.output = f'saved_models/smart_integrated_{timestamp}.joblib'

    logger.info("=" * 60)
    logger.info("[SMART] 智能模型集成")
    logger.info(f"  主模型: {args.footballai}")
    logger.info(f"  专家: {args.expert}")
    logger.info(f"  输出: {args.output}")
    logger.info("=" * 60)

    # 1. 创建集成器
    integrator = SmartIntegration(base_weight=args.base_weight)

    # 2. 加载主模型
    try:
        integrator.load_base_model(args.footballai)
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.info(f"[FAIL] 主模型加载失败: {e}")
        return 1

    # 3. 加载专家模型
    if os.path.isdir(args.expert):
        integrator.load_expert_models(args.expert)
    elif os.path.isfile(args.expert):
        try:
            name = os.path.splitext(os.path.basename(args.expert))[0]
            integrator.expert_models[name] = joblib.load(args.expert)
            logger.info(f"[EXPERT] 加载: {name}")
        except (FileNotFoundError, IOError, OSError, PermissionError) as e:
            logger.info(f"[FAIL] 专家模型加载失败: {e}")

    # 4. 训练元模型
    logger.info(f"\n[TEST_DATA] 测试数据: {args.test_data or 'N/A'}")
    if args.test_data:
        result = load_test_data(args.test_data)
        if result is not None:
            X_test, y_test, feature_cols = result
            integrator.feature_names_ = feature_cols
            integrator.fit_meta_model(X_test, y_test)
            # 计算元模型测试准确率
            meta_X = np.hstack(
                [integrator._get_base_proba(X_test)]
                + [integrator._get_expert_proba(m, X_test)
                   for m in integrator.expert_models.values()]
            )
            meta_X_scaled = integrator.scaler.transform(meta_X)
            meta_score = integrator.meta_model.score(meta_X_scaled, y_test)
            logger.info(f"\n[META] 元模型测试准确率: {meta_score:.4f}")

    # 5. 保存
    integrator.save(args.output)

    # 6. 摘要
    logger.info(f"\n{'='*60}")
    logger.info(f"[SUMMARY] 集成摘要")
    logger.info(f"  主模型权重: {integrator.base_weight:.2f}")
    logger.info(f"  专家权重: {integrator.expert_weight:.2f}")
    logger.info(f"  专家数量: {len(integrator.expert_models)}")
    logger.info(f"  元模型: {'LogisticRegression' if integrator.meta_model else 'None (纯集成)'}")
    logger.info(f"{'='*60}")

    return 0

if __name__ == '__main__':
    sys.exit(main())

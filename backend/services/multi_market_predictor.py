"""
哨响AI - 多市场预测器 v1.0
============================
加载 AH/OU/Goals 模型, 提供多市场预测能力。

集成方式:
- 接受已计算好的72维特征矩阵
- 返回各市场的预测结果
- 可独立使用或集成到 PredictionService
"""
import logging
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
import joblib

warnings.filterwarnings('ignore')
logger = logging.getLogger(__name__)


class MultiMarketPredictor:
    """多市场预测器 (让球/大小球/进球数)"""

    def __init__(self, model_dir: str = None):
        if model_dir is None:
            model_dir = Path(__file__).parent.parent.parent / 'saved_models'
        self.model_dir = Path(model_dir)
        self._ah_model = None
        self._ou_model = None
        self._goals_model = None
        self._loaded = False

    def _find_latest_model(self, prefix: str) -> Optional[Path]:
        """查找最新的模型文件"""
        candidates = sorted(
            self.model_dir.glob(f'multi_{prefix}_*.joblib'),
            reverse=True
        )
        return candidates[0] if candidates else None

    def load_models(self):
        """加载所有多市场模型"""
        if self._loaded:
            return

        # AH模型
        ah_path = self._find_latest_model('ah_handicap')
        if ah_path:
            self._ah_model = joblib.load(ah_path)
            metrics = self._ah_model.get('metrics', {})
            logger.info(f"AH模型已加载: {ah_path.name} "
                        f"(Acc={metrics.get('accuracy', 0):.3f})")
        else:
            logger.warning("未找到AH(让球)模型")

        # OU模型
        ou_path = self._find_latest_model('ou_totals')
        if ou_path:
            self._ou_model = joblib.load(ou_path)
            metrics = self._ou_model.get('metrics', {})
            logger.info(f"OU模型已加载: {ou_path.name} "
                        f"(Acc={metrics.get('accuracy', 0):.3f})")
        else:
            logger.warning("未找到OU(大小球)模型")

        # Goals模型
        goals_path = self._find_latest_model('goals_total')
        if goals_path:
            self._goals_model = joblib.load(goals_path)
            metrics = self._goals_model.get('metrics', {})
            logger.info(f"Goals模型已加载: {goals_path.name} "
                        f"(Acc={metrics.get('accuracy', 0):.3f})")
        else:
            logger.warning("未找到Goals(进球数)模型")

        self._loaded = True

    @property
    def is_ready(self) -> bool:
        """是否所有模型都已加载"""
        if not self._loaded:
            self.load_models()
        return all([self._ah_model, self._ou_model, self._goals_model])

    def get_feature_names(self) -> list:
        """返回72维特征名列表(从任意一个多市场模型拿, 都相同)"""
        if not self._loaded:
            self.load_models()
        for pkg in [self._ah_model, self._ou_model, self._goals_model]:
            if pkg and 'feature_names' in pkg:
                return list(pkg['feature_names'])
        return []

    def _transform_features(self, features: np.ndarray, package: dict) -> np.ndarray:
        """特征标准化"""
        scaler = package.get('scaler')
        model = package.get('model')
        expected_features = package.get('feature_names', [])

        if scaler is None:
            return features

        # 特征对齐
        if len(expected_features) > 0 and hasattr(features, 'shape'):
            if features.shape[1] != len(expected_features):
                logger.warning(f"特征维度不匹配: 输入{features.shape[1]} vs 期望{len(expected_features)}")
                # 尝试按名称对齐 (如果是DataFrame)
                if hasattr(features, 'columns'):
                    aligned = np.zeros((features.shape[0], len(expected_features)))
                    for i, name in enumerate(expected_features):
                        if name in features.columns:
                            aligned[:, i] = features[name].values
                    features = aligned

        return scaler.transform(features)

    def predict_handicap(self, features: np.ndarray) -> Dict:
        """
        预测让球盘结果

        Args:
            features: (n_samples, 72) 特征矩阵

        Returns:
            {
                'prediction': 'home_cover'/'away_cover'/'push',
                'confidence': float,
                'probabilities': {'home_cover': 0.x, 'away_cover': 0.y, 'push': 0.z},
                'handicap_line': float,  # 推导让球线
            }
        """
        if not self._ah_model:
            return {'error': 'AH模型未加载'}

        package = self._ah_model
        model = package['model']
        X = self._transform_features(features, package)

        if X.ndim == 1:
            X = X.reshape(1, -1)

        proba = model.predict_proba(X)[0]
        pred_idx = int(np.argmax(proba))
        classes = model.classes_

        # 类名映射 (可能与训练时的encoder不同, 尝试推断)
        if len(classes) == 3:
            # LGBM 三分类: 0=away_cover, 1=home_cover, 2=push (按字母序)
            # 实际需根据训练时的LabelEncoder确认
            label_map = {0: 'away_cover', 1: 'home_cover', 2: 'push'}
        else:
            label_map = {i: str(c) for i, c in enumerate(classes)}

        return {
            'prediction': label_map.get(pred_idx, str(pred_idx)),
            'confidence': float(proba[pred_idx]),
            'probabilities': {
                label_map.get(i, str(i)): float(p)
                for i, p in enumerate(proba)
            },
        }

    def predict_over_under(self, features: np.ndarray,
                           ou_line: float = 2.5) -> Dict:
        """
        预测大小球 — 任意盘口

        基础模型预测 2.5 盘口的 over/under 概率，
        再通过 Poisson 分布推导任意盘口的概率。

        Args:
            features: (n_samples, 72) 特征矩阵
            ou_line: 大小球盘口线 (1.5/2.0/2.5/3.0/3.5 等)

        Returns:
            {
                'prediction': 'over'/'under',
                'confidence': float,
                'probabilities': {'over': 0.x, 'under': 0.y},
                'line': float,
                'expected_goals': float,
            }
        """
        if not self._ou_model:
            return {'error': 'OU模型未加载'}

        package = self._ou_model
        model = package['model']
        X = self._transform_features(features, package)

        if X.ndim == 1:
            X = X.reshape(1, -1)

        proba = model.predict_proba(X)[0]  # [P(under), P(over)] for 2.5 line
        pred_idx = int(np.argmax(proba))

        # LGBM二分类: 0=Under, 1=Over (训练时: 0=under, 1=over)
        label_map = {0: 'under', 1: 'over'}

        # ── 任意盘口推导: 通过 Goals 模型拿期望进球λ → Poisson 分布 ──
        if ou_line != 2.5 and self._goals_model:
            try:
                goals_pkg = self._goals_model
                goals_model = goals_pkg['model']
                Xg = self._transform_features(features, goals_pkg)
                if Xg.ndim == 1:
                    Xg = Xg.reshape(1, -1)
                g_proba = goals_model.predict_proba(Xg)[0]
                n_cls = len(g_proba)
                if n_cls == 3:
                    bin_mid = [0.5, 2.5, 4.5]
                elif n_cls == 5:
                    bin_mid = [0.0, 1.0, 2.0, 3.0, 4.5]
                else:
                    bin_mid = [float(i) for i in range(n_cls)]
                expected_goals = sum(bin_mid[i] * float(g_proba[i]) for i in range(n_cls))

                # Poisson P(total <= line)
                from scipy.stats import poisson
                lam = max(0.1, expected_goals)
                under_prob = poisson.cdf(int(ou_line), lam)
                # 如果盘口是整数 (如 2.0), 需要处理 push
                if ou_line == int(ou_line):
                    push_prob = poisson.pmf(int(ou_line), lam)
                    under_prob -= push_prob  # 排除恰好等于盘口的push
                    over_prob = 1.0 - under_prob - push_prob
                else:
                    over_prob = 1.0 - under_prob

                return {
                    'prediction': 'over' if over_prob > under_prob else 'under',
                    'confidence': max(over_prob, under_prob),
                    'probabilities': {
                        'over': round(float(over_prob), 4),
                        'under': round(float(under_prob), 4),
                    },
                    'line': ou_line,
                    'expected_goals': round(float(expected_goals), 2),
                    'method': 'poisson_derived',
                }
            except Exception:
                logger.warning("Poisson推导失败，回退到2.5盘口模型", exc_info=True)

        # 默认: 直接使用 2.5 盘口模型
        return {
            'prediction': label_map.get(pred_idx, 'under'),
            'confidence': float(proba[pred_idx]),
            'probabilities': {
                'over': float(proba[1]),
                'under': float(proba[0]),
            },
            'line': ou_line,
            'method': 'model_direct',
        }

    def predict_goals(self, features: np.ndarray) -> Dict:
        """
        预测总进球数分布

        Args:
            features: (n_samples, 72) 特征矩阵

        Returns:
            {
                'prediction': int,  # 最可能进球数
                'expected_goals': float,  # 期望进球
                'probabilities': {0: 0.x, 1: 0.y, ..., '4+': 0.z},
            }
        """
        if not self._goals_model:
            return {'error': 'Goals模型未加载'}

        package = self._goals_model
        model = package['model']
        X = self._transform_features(features, package)

        if X.ndim == 1:
            X = X.reshape(1, -1)

        proba = model.predict_proba(X)[0]
        n_classes = len(proba)

        # 根据模型类型生成标签
        model_type = package.get('model_type', '')

        if n_classes == 3:
            # 3分类模型: 0-1球, 2-3球, 4+球
            labels = ['0-1球', '2-3球', '4+球']
        elif n_classes == 5:
            # 5分类模型: 0,1,2,3,4+
            labels = [str(i) if i < 4 else '4+球' for i in range(n_classes)]
        else:
            labels = [str(i) if i < n_classes - 1 else f'{n_classes-1}+球'
                      for i in range(n_classes)]

        probs = {labels[i]: float(proba[i]) for i in range(n_classes)}

        # 期望进球 — 用桶中值而非桶索引
        if n_classes == 3:
            bin_midpoints = [0.5, 2.5, 4.5]          # 0-1球, 2-3球, 4+球
        elif n_classes == 5:
            bin_midpoints = [0.0, 1.0, 2.0, 3.0, 4.5] # 0,1,2,3,4+
        else:
            bin_midpoints = [float(i) for i in range(n_classes)]
        expected = sum(bin_midpoints[i] * float(proba[i]) for i in range(n_classes))

        # 最可能
        pred_idx = int(np.argmax(proba))

        return {
            'prediction': labels[pred_idx],
            'expected_goals': round(expected, 2),
            'probabilities': probs,
        }

    def predict_all(self, features: np.ndarray,
                    ou_line: float = 2.5) -> Dict:
        """
        一次性返回所有市场预测

        Returns:
            {
                'handicap': {...},
                'over_under': {...},
                'goals': {...},
            }
        """
        self.load_models()
        return {
            'handicap': self.predict_handicap(features),
            'over_under': self.predict_over_under(features, ou_line),
            'goals': self.predict_goals(features),
        }


# 全局单例
_multi_predictor: Optional[MultiMarketPredictor] = None


def get_multi_market_predictor() -> MultiMarketPredictor:
    """获取多市场预测器单例"""
    global _multi_predictor
    if _multi_predictor is None:
        _multi_predictor = MultiMarketPredictor()
    return _multi_predictor

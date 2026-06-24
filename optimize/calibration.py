"""
哨响AI - 概率校准模块 (T15)
==============================
实现多种概率校准方法，包括 Platt Scaling (温度缩放 + 逻辑回归)、
Isotonic Regression、Beta Calibration，提供统一对比框架和稀疏数据适应性。

核心设计:
  1. Platt Scaling — 逻辑回归校准 (经典 sigmoid 映射)
  2. Temperature Scaling — 单参数温度缩放 (深度学习常用)
  3. Isotonic Regression — 非参数单调校准
  4. Beta Calibration — 三参数 Beta 分布校准
  5. 多方法对比框架 — Brier Score / ECE / Log Loss / 可靠性图
  6. 稀疏数据适应 — 正则化 + 交叉验证 + 最小样本检测

输出:
  - 校准后概率 (home/draw/away)
  - 校准评估报告 (对比表 + 可靠性数据)
  - 持久化校准器 (joblib)

用法:
    from optimize.calibration import CalibratorSuite
    suite = CalibratorSuite()
    suite.fit(y_true, raw_probs)
    report = suite.compare()
    calibrated = suite.predict(raw_probs, method='platt')
"""

import logging
import warnings
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, field
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# sklearn 依赖 (必需)
from sklearn.linear_model import LogisticRegression
from sklearn.isotonic import IsotonicRegression
from sklearn.model_selection import TimeSeriesSplit, cross_val_score
from sklearn.metrics import brier_score_loss, log_loss

# scipy 依赖 (Beta Calibration 需要)
try:
    from scipy.optimize import minimize
    from scipy.special import betainc
    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False


# ════════════════════════════════════════════════════════════════
# 校准评估指标
# ════════════════════════════════════════════════════════════════

def compute_ece(probs: np.ndarray, labels: np.ndarray, n_bins: int = 10) -> float:
    """
    Expected Calibration Error (ECE)
    ECE = Σ (n_b/N) |acc_b - conf_b|
    """
    n_samples = len(labels)
    if n_samples == 0:
        return 0.0

    confidences = np.max(probs, axis=1) if probs.ndim > 1 else probs
    predictions = np.argmax(probs, axis=1) if probs.ndim > 1 else (probs > 0.5).astype(int)
    correct = (predictions == labels).astype(float)

    bin_boundaries = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    for i in range(n_bins):
        mask = (confidences > bin_boundaries[i]) & (confidences <= bin_boundaries[i + 1])
        if mask.sum() > 0:
            acc = correct[mask].mean()
            conf = confidences[mask].mean()
            ece += mask.sum() / n_samples * abs(acc - conf)
    return float(ece)


def compute_mce(probs: np.ndarray, labels: np.ndarray, n_bins: int = 10) -> float:
    """Maximum Calibration Error (MCE)"""
    n_samples = len(labels)
    if n_samples == 0:
        return 0.0

    confidences = np.max(probs, axis=1) if probs.ndim > 1 else probs
    predictions = np.argmax(probs, axis=1) if probs.ndim > 1 else (probs > 0.5).astype(int)
    correct = (predictions == labels).astype(float)

    bin_boundaries = np.linspace(0.0, 1.0, n_bins + 1)
    mce = 0.0
    for i in range(n_bins):
        mask = (confidences > bin_boundaries[i]) & (confidences <= bin_boundaries[i + 1])
        if mask.sum() > 0:
            acc = correct[mask].mean()
            conf = confidences[mask].mean()
            mce = max(mce, abs(acc - conf))
    return float(mce)


def compute_reliability(probs: np.ndarray, labels: np.ndarray,
                         n_bins: int = 10) -> List[Dict]:
    """计算可靠性图数据"""
    n_samples = len(labels)
    if n_samples == 0:
        return []

    confidences = np.max(probs, axis=1) if probs.ndim > 1 else probs
    predictions = np.argmax(probs, axis=1) if probs.ndim > 1 else (probs > 0.5).astype(int)
    correct = (predictions == labels).astype(float)

    bin_boundaries = np.linspace(0.0, 1.0, n_bins + 1)
    reliability = []
    for i in range(n_bins):
        mask = (confidences > bin_boundaries[i]) & (confidences <= bin_boundaries[i + 1])
        count = mask.sum()
        if count > 0:
            reliability.append({
                'bin_lower': float(bin_boundaries[i]),
                'bin_upper': float(bin_boundaries[i + 1]),
                'count': int(count),
                'accuracy': float(correct[mask].mean()),
                'confidence': float(confidences[mask].mean()),
                'gap': float(correct[mask].mean() - confidences[mask].mean()),
            })
    return reliability


def multiclass_brier(probs: np.ndarray, labels: np.ndarray, n_classes: int = 3) -> float:
    """
    多分类 Brier Score
    BS = (1/N) Σ Σ (p_ij - y_ij)^2
    """
    n = len(labels)
    onehot = np.zeros((n, n_classes))
    onehot[np.arange(n), labels] = 1.0
    return float(np.mean(np.sum((probs - onehot) ** 2, axis=1)))


# ════════════════════════════════════════════════════════════════
# Platt Scaling 校准器
# ════════════════════════════════════════════════════════════════

class PlattScaler:
    """
    Platt Scaling — 经典逻辑回归校准

    原理:
        对每个类别 c, 训练二分类 LogisticRegression:
        P(y=c|x) = 1 / (1 + exp(A * s_c + B))
        其中 s_c 是原始概率 (或 logit), A, B 是学习的参数

    多分类: one-vs-rest × 3 → 归一化
    """

    def __init__(self, C: float = 1.0, class_weight: str = 'balanced',
                 min_samples: int = 50):
        """
        Args:
            C: 正则化强度 (越小越强, 稀疏数据适应性)
            class_weight: 类别权重
            min_samples: 最小训练样本数
        """
        self.C = C
        self.class_weight = class_weight
        self.min_samples = min_samples
        self.models: List[LogisticRegression] = []
        self._trained = False
        self._n_classes = 3

    def fit(self, probs: np.ndarray, labels: np.ndarray) -> 'PlattScaler':
        """
        训练 Platt Scaling

        Args:
            probs: (N, C) 原始概率
            labels: (N,) 真实标签
        """
        n, c = probs.shape
        self._n_classes = c

        if n < self.min_samples:
            logger.warning(f"PlattScaler: 样本不足 ({n} < {self.min_samples}), 使用弱正则化")
            C = min(self.C, 0.01)  # 更强正则化
        else:
            C = self.C

        self.models = []
        for cls_idx in range(c):
            y_binary = (labels == cls_idx).astype(int)
            lr = LogisticRegression(
                solver='lbfgs',
                max_iter=500,
                C=C,
                class_weight=self.class_weight,
                random_state=42,
            )
            # 输入: 原始概率的 logit (避免 0/1)
            X_input = self._to_logit(probs)
            lr.fit(X_input, y_binary)
            self.models.append(lr)

        self._trained = True
        return self

    def predict(self, probs: np.ndarray) -> np.ndarray:
        """校准概率"""
        if not self._trained:
            return probs

        n = probs.shape[0]
        X_input = self._to_logit(probs)
        calibrated = np.zeros((n, self._n_classes))

        for i, lr in enumerate(self.models):
            calibrated[:, i] = lr.predict_proba(X_input)[:, 1]

        # 归一化
        row_sums = calibrated.sum(axis=1, keepdims=True)
        calibrated = np.divide(calibrated, row_sums,
                                out=np.full_like(calibrated, 1.0 / self._n_classes),
                                where=row_sums > 0)
        return calibrated

    @staticmethod
    def _to_logit(probs: np.ndarray, eps: float = 1e-6) -> np.ndarray:
        """概率 → logit (避免 0/1)"""
        p = np.clip(probs, eps, 1.0 - eps)
        return np.log(p / (1.0 - p))


# ════════════════════════════════════════════════════════════════
# Temperature Scaling 校准器 (深度学习常用)
# ════════════════════════════════════════════════════════════════

class TemperatureScaler:
    """
    Temperature Scaling — 单参数温度缩放

    原理:
        P_cal = softmax(logits / T)
        T > 1 → 概率更平滑 (过度自信修正)
        T < 1 → 概率更尖锐 (欠自信修正)

    优势: 只有一个参数, 不易过拟合, 适合稀疏数据
    """

    def __init__(self, min_samples: int = 30):
        self.temperature: float = 1.0
        self._trained = False
        self.min_samples = min_samples

    def fit(self, probs: np.ndarray, labels: np.ndarray) -> 'TemperatureScaler':
        """通过 NLL 最小化搜索最优温度"""
        n = len(labels)
        if n < self.min_samples:
            logger.warning(f"TemperatureScaler: 样本不足 ({n}), 保持 T=1.0")
            self._trained = True
            return self

        logits = self._probs_to_logits(probs)

        # 网格搜索 (不依赖 scipy)
        best_T, best_nll = 1.0, float('inf')
        for T in np.linspace(0.1, 5.0, 50):
            scaled = self._apply_temperature(logits, T)
            try:
                nll = log_loss(labels, scaled, labels=list(range(probs.shape[1])))
            except (Exception, ValueError, KeyError, IndexError):
                continue
            if nll < best_nll:
                best_nll = nll
                best_T = T

        # 精细搜索
        for T in np.linspace(max(0.1, best_T - 0.2), best_T + 0.2, 20):
            scaled = self._apply_temperature(logits, T)
            try:
                nll = log_loss(labels, scaled, labels=list(range(probs.shape[1])))
            except (Exception, KeyError, IndexError):
                continue
            if nll < best_nll:
                best_nll = nll
                best_T = T

        self.temperature = best_T
        self._trained = True
        logger.info(f"TemperatureScaler: 最优温度 T={self.temperature:.4f} (NLL: {best_nll:.4f})")
        return self

    def predict(self, probs: np.ndarray) -> np.ndarray:
        """校准概率"""
        if not self._trained or self.temperature == 1.0:
            return probs
        logits = self._probs_to_logits(probs)
        return self._apply_temperature(logits, self.temperature)

    @staticmethod
    def _probs_to_logits(probs: np.ndarray, eps: float = 1e-7) -> np.ndarray:
        """概率 → logits"""
        p = np.clip(probs, eps, 1.0 - eps)
        return np.log(p)

    @staticmethod
    def _apply_temperature(logits: np.ndarray, T: float) -> np.ndarray:
        """softmax(logits / T)"""
        scaled = logits / T
        # 数值稳定 softmax
        shifted = scaled - np.max(scaled, axis=1, keepdims=True)
        exp_s = np.exp(shifted)
        return exp_s / exp_s.sum(axis=1, keepdims=True)


# ════════════════════════════════════════════════════════════════
# Isotonic Regression 校准器
# ════════════════════════════════════════════════════════════════

class IsotonicScaler:
    """
    Isotonic Regression — 非参数单调校准

    原理:
        对每个类别, 将原始概率映射为单调递增的校准概率
        适合: 大数据集, 非线性校准曲线

    注意: 小样本 (<300) 容易过拟合, 建议使用 Platt/Temperature
    """

    def __init__(self, min_samples: int = 300):
        self.min_samples = min_samples
        self.models: List[Tuple[IsotonicRegression, np.ndarray]] = []
        self._trained = False
        self._n_classes = 3

    def fit(self, probs: np.ndarray, labels: np.ndarray) -> 'IsotonicScaler':
        n, c = probs.shape
        self._n_classes = c

        if n < self.min_samples:
            logger.warning(f"IsotonicScaler: 样本不足 ({n} < {self.min_samples}), "
                           f"结果可能过拟合, 建议使用 Platt/Temperature")

        self.models = []
        for cls_idx in range(c):
            y_binary = (labels == cls_idx).astype(int)
            # 使用该类原始概率作为一维输入
            scores = probs[:, cls_idx]
            iso = IsotonicRegression(out_of_bounds='clip')
            iso.fit(scores, y_binary)
            self.models.append(iso)

        self._trained = True
        return self

    def predict(self, probs: np.ndarray) -> np.ndarray:
        if not self._trained:
            return probs

        n = probs.shape[0]
        calibrated = np.zeros((n, self._n_classes))
        for i, iso in enumerate(self.models):
            calibrated[:, i] = iso.predict(probs[:, i])

        # 归一化
        row_sums = calibrated.sum(axis=1, keepdims=True)
        calibrated = np.divide(calibrated, row_sums,
                                out=np.full_like(calibrated, 1.0 / self._n_classes),
                                where=row_sums > 0)
        return calibrated


# ════════════════════════════════════════════════════════════════
# Beta Calibration 校准器
# ════════════════════════════════════════════════════════════════

class BetaScaler:
    """
    Beta Calibration — 三参数 Beta 分布校准

    原理:
        P_cal = F_Beta(a * x^c / (a * x^c + b * (1-x)^c))
        三参数 (a, b, c) 比 Platt (2参数) 更灵活

    参考: Kull et al. (2017) "Beyond sigmoids: How to obtain well-calibrated
          probabilities from binary classifiers with beta calibration"
    """

    def __init__(self, min_samples: int = 100):
        self.min_samples = min_samples
        self.params: List[Dict] = []  # 每个类的参数
        self._trained = False
        self._n_classes = 3

    def fit(self, probs: np.ndarray, labels: np.ndarray) -> 'BetaScaler':
        if not _SCIPY_AVAILABLE:
            logger.warning("BetaScaler 需要 scipy, 降级为 Platt Scaling")
            self._fallback_platt = PlattScaler()
            self._fallback_platt.fit(probs, labels)
            self._trained = True
            self._use_platt = True
            return self

        self._use_platt = False
        n, c = probs.shape
        self._n_classes = c

        if n < self.min_samples:
            logger.warning(f"BetaScaler: 样本不足 ({n} < {self.min_samples}), 降级为 Platt")
            self._fallback_platt = PlattScaler()
            self._fallback_platt.fit(probs, labels)
            self._use_platt = True
            self._trained = True
            return self

        self.params = []
        for cls_idx in range(c):
            y_binary = (labels == cls_idx).astype(int)
            x = np.clip(probs[:, cls_idx], 1e-6, 1.0 - 1e-6)
            p = self._fit_beta(x, y_binary)
            self.params.append(p)

        self._trained = True
        return self

    def predict(self, probs: np.ndarray) -> np.ndarray:
        if not self._trained:
            return probs
        if getattr(self, '_use_platt', False):
            return self._fallback_platt.predict(probs)

        n = probs.shape[0]
        calibrated = np.zeros((n, self._n_classes))
        for i, p in enumerate(self.params):
            x = np.clip(probs[:, i], 1e-6, 1.0 - 1e-6)
            a, b, c_param = p['a'], p['b'], p['c']
            # Beta CDF 近似
            u = a * x ** c_param
            v = b * (1.0 - x) ** c_param
            calibrated[:, i] = u / (u + v + 1e-10)

        # 归一化
        row_sums = calibrated.sum(axis=1, keepdims=True)
        calibrated = np.divide(calibrated, row_sums,
                                out=np.full_like(calibrated, 1.0 / self._n_classes),
                                where=row_sums > 0)
        return calibrated

    @staticmethod
    def _fit_beta(x: np.ndarray, y: np.ndarray) -> Dict:
        """用 NLL 最小化拟合 Beta 校准参数"""
        def neg_log_likelihood(params):
            a, b, c_p = np.exp(params[0]), np.exp(params[1]), np.exp(params[2])
            u = a * x ** c_p
            v = b * (1.0 - x) ** c_p
            p_cal = u / (u + v + 1e-10)
            p_cal = np.clip(p_cal, 1e-7, 1.0 - 1e-7)
            return -np.mean(y * np.log(p_cal) + (1 - y) * np.log(1 - p_cal))

        best_result = None
        best_nll = float('inf')
        for init in [
            [0.0, 0.0, 0.0],    # a=b=c=1
            [1.0, 0.0, 0.0],    # a=e
            [0.0, 1.0, 0.0],    # b=e
            [0.5, 0.5, 0.5],
        ]:
            try:
                result = minimize(neg_log_likelihood, init, method='Nelder-Mead',
                                   options={'maxiter': 500, 'xatol': 1e-5})
                if result.fun < best_nll:
                    best_nll = result.fun
                    best_result = result
            except (Exception, KeyError, IndexError):
                continue

        if best_result is None:
            return {'a': 1.0, 'b': 1.0, 'c': 1.0}

        return {
            'a': float(np.exp(best_result.x[0])),
            'b': float(np.exp(best_result.x[1])),
            'c': float(np.exp(best_result.x[2])),
        }


# ════════════════════════════════════════════════════════════════
# 校准方法注册表
# ════════════════════════════════════════════════════════════════

CALIBRATOR_REGISTRY = {
    'platt': PlattScaler,
    'temperature': TemperatureScaler,
    'isotonic': IsotonicScaler,
    'beta': BetaScaler,
}

CALIBRATOR_DESCRIPTIONS = {
    'platt': 'Platt Scaling — 逻辑回归校准 (经典, 稳健)',
    'temperature': 'Temperature Scaling — 单参数温度缩放 (简单, 不易过拟合)',
    'isotonic': 'Isotonic Regression — 非参数单调校准 (灵活, 需大数据)',
    'beta': 'Beta Calibration — 三参数 Beta 分布 (最灵活, 需中等数据)',
}


# ════════════════════════════════════════════════════════════════
# 校准套件 — 多方法对比
# ════════════════════════════════════════════════════════════════

@dataclass
class CalibrationReport:
    """校准评估报告"""
    method: str
    n_samples: int
    n_classes: int
    # 校准前指标
    brier_before: float = 0.0
    ece_before: float = 0.0
    mce_before: float = 0.0
    log_loss_before: float = 0.0
    # 校准后指标
    brier_after: float = 0.0
    ece_after: float = 0.0
    mce_after: float = 0.0
    log_loss_after: float = 0.0
    # 改进
    brier_delta: float = 0.0
    ece_delta: float = 0.0
    ll_delta: float = 0.0
    # 可靠性数据
    reliability_before: List[Dict] = field(default_factory=list)
    reliability_after: List[Dict] = field(default_factory=list)
    # 参数信息
    params: Dict = field(default_factory=dict)
    # 建议
    recommendation: str = ''


class CalibratorSuite:
    """
    校准套件 — 多方法训练 + 对比 + 选择

    用法:
        suite = CalibratorSuite()
        suite.fit(y_true, raw_probs)
        report = suite.compare()
        calibrated = suite.predict(raw_probs, method='platt')
    """

    def __init__(self, methods: List[str] = None, n_classes: int = 3,
                 cv_splits: int = 3, min_samples: int = 100):
        """
        Args:
            methods: 要对比的方法列表 (None=全部)
            n_classes: 分类数
            cv_splits: 交叉验证折数
            min_samples: 最小样本数
        """
        if methods is None:
            methods = list(CALIBRATOR_REGISTRY.keys())
        self.methods = methods
        self.n_classes = n_classes
        self.cv_splits = cv_splits
        self.min_samples = min_samples

        # 训练好的校准器
        self._calibrators: Dict[str, object] = {}
        # 训练数据
        self._y_true: Optional[np.ndarray] = None
        self._raw_probs: Optional[np.ndarray] = None
        # 对比报告
        self._reports: Dict[str, CalibrationReport] = {}

    def fit(self, y_true: np.ndarray, raw_probs: np.ndarray,
            test_ratio: float = 0.2) -> Dict[str, Dict]:
        """
        训练所有校准方法

        Args:
            y_true: (N,) 真实标签 (0, 1, 2)
            raw_probs: (N, C) 原始预测概率
            test_ratio: 测试集比例

        Returns:
            {method_name: training_metrics}
        """
        # 输入验证
        y_true = np.asarray(y_true, dtype=int)
        raw_probs = np.asarray(raw_probs, dtype=np.float64)

        # 概率裁剪和归一化
        raw_probs = np.clip(raw_probs, 1e-6, 1.0 - 1e-6)
        row_sums = raw_probs.sum(axis=1, keepdims=True)
        raw_probs = raw_probs / row_sums

        self._y_true = y_true
        self._raw_probs = raw_probs
        n = len(y_true)

        logger.info(f"CalibratorSuite: {n} 样本, {raw_probs.shape[1]} 类, "
                     f"方法: {self.methods}")

        # 时序分割
        split = int(n * (1 - test_ratio))
        y_train, y_test = y_true[:split], y_true[split:]
        p_train, p_test = raw_probs[:split], raw_probs[split:]

        # 训练前指标
        metrics_before = self._compute_metrics(y_test, p_test)

        results = {}
        for method in self.methods:
            if method not in CALIBRATOR_REGISTRY:
                logger.warning(f"未知校准方法: {method}, 跳过")
                continue

            logger.info(f"  训练 {method}...")
            try:
                cal = CALIBRATOR_REGISTRY[method](min_samples=max(self.min_samples, split // 5))
                cal.fit(p_train, y_train)

                # 校准测试集
                p_cal = cal.predict(p_test)
                metrics_after = self._compute_metrics(y_test, p_cal)

                # 构建报告
                report = CalibrationReport(
                    method=method,
                    n_samples=n,
                    n_classes=self.n_classes,
                    brier_before=metrics_before['brier'],
                    ece_before=metrics_before['ece'],
                    mce_before=metrics_before['mce'],
                    log_loss_before=metrics_before['log_loss'],
                    brier_after=metrics_after['brier'],
                    ece_after=metrics_after['ece'],
                    mce_after=metrics_after['mce'],
                    log_loss_after=metrics_after['log_loss'],
                    brier_delta=metrics_before['brier'] - metrics_after['brier'],
                    ece_delta=metrics_before['ece'] - metrics_after['ece'],
                    ll_delta=metrics_before['log_loss'] - metrics_after['log_loss'],
                    reliability_before=compute_reliability(p_test, y_test),
                    reliability_after=compute_reliability(p_cal, y_test),
                )

                # 保存校准器参数
                if hasattr(cal, 'temperature'):
                    report.params = {'temperature': cal.temperature}
                elif hasattr(cal, 'models'):
                    report.params = {'n_models': len(cal.models)}

                self._calibrators[method] = cal
                self._reports[method] = report
                results[method] = {
                    'brier_delta': report.brier_delta,
                    'ece_delta': report.ece_delta,
                    'll_delta': report.ll_delta,
                }

                logger.info(f"  {method}: Brier {metrics_before['brier']:.4f}→{metrics_after['brier']:.4f} "
                            f"({report.brier_delta:+.4f}), "
                            f"ECE {metrics_before['ece']:.4f}→{metrics_after['ece']:.4f} "
                            f"({report.ece_delta:+.4f})")

            except (Exception, KeyError, IndexError) as e:
                logger.error(f"  {method} 训练失败: {e}")
                results[method] = {'error': str(e)}

        return results

    def predict(self, probs: np.ndarray, method: str = None) -> np.ndarray:
        """
        使用指定方法校准概率

        Args:
            probs: (N, C) 原始概率
            method: 校准方法名 (None=自动选择最优)
        """
        if method is None:
            method = self.best_method()

        if method not in self._calibrators:
            logger.warning(f"校准器 {method} 未训练, 返回原始概率")
            return probs

        return self._calibrators[method].predict(probs)

    def best_method(self, metric: str = 'ece') -> str:
        """
        根据指定指标选择最优方法

        Args:
            metric: 'ece' | 'brier' | 'log_loss'
        """
        if not self._reports:
            return 'temperature'  # 安全默认值

        best_method = 'temperature'
        best_delta = -float('inf')
        for name, report in self._reports.items():
            delta = getattr(report, f'{metric}_delta', 0) if metric != 'log_loss' else report.ll_delta
            if delta > best_delta:
                best_delta = delta
                best_method = name
        return best_method

    def compare(self) -> pd.DataFrame:
        """
        生成多方法对比表

        Returns:
            DataFrame with columns: method, brier_before, brier_after, brier_delta,
            ece_before, ece_after, ece_delta, ll_before, ll_after, ll_delta, recommendation
        """
        rows = []
        for name, report in self._reports.items():
            # 推荐策略
            if report.ece_delta > 0.02 and report.brier_delta > 0.005:
                rec = 'strong_calibrate'
            elif report.ece_delta > 0.005:
                rec = 'moderate_calibrate'
            elif report.ece_delta > -0.005:
                rec = 'marginal'
            else:
                rec = 'skip_calibration'
            report.recommendation = rec

            rows.append({
                'method': name,
                'brier_before': round(report.brier_before, 4),
                'brier_after': round(report.brier_after, 4),
                'brier_delta': round(report.brier_delta, 4),
                'ece_before': round(report.ece_before, 4),
                'ece_after': round(report.ece_after, 4),
                'ece_delta': round(report.ece_delta, 4),
                'll_before': round(report.log_loss_before, 4),
                'll_after': round(report.log_loss_after, 4),
                'll_delta': round(report.ll_delta, 4),
                'recommendation': rec,
            })

        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values('ece_delta', ascending=False).reset_index(drop=True)
        return df

    def sparse_data_test(self, sample_sizes: List[int] = None) -> pd.DataFrame:
        """
        稀疏数据适应性测试

        用不同大小的子集训练各方法，观察校准质量随样本量的变化。

        Args:
            sample_sizes: 要测试的样本量列表

        Returns:
            DataFrame with columns: n_samples, method, ece, brier, log_loss
        """
        if self._y_true is None:
            raise ValueError("请先调用 fit()")

        if sample_sizes is None:
            n_total = len(self._y_true)
            sample_sizes = [50, 100, 200, 500, 1000, 2000, n_total]
            sample_sizes = [s for s in sample_sizes if s <= n_total]

        results = []
        n_total = len(self._y_true)

        for n in sample_sizes:
            # 从训练集前 n 个取 (保持时序)
            y_sub = self._y_true[:n]
            p_sub = self._raw_probs[:n]

            # 留出 20% 测试
            split = int(n * 0.8)
            y_tr, y_te = y_sub[:split], y_sub[split:]
            p_tr, p_te = p_sub[:split], p_sub[split:]

            if len(y_te) < 10:
                continue

            # 原始指标
            raw_metrics = self._compute_metrics(y_te, p_te)

            for method in self.methods:
                try:
                    cal = CALIBRATOR_REGISTRY[method](min_samples=30)
                    cal.fit(p_tr, y_tr)
                    p_cal = cal.predict(p_te)
                    cal_metrics = self._compute_metrics(y_te, p_cal)

                    results.append({
                        'n_samples': n,
                        'method': method,
                        'ece_raw': round(raw_metrics['ece'], 4),
                        'ece_cal': round(cal_metrics['ece'], 4),
                        'ece_delta': round(raw_metrics['ece'] - cal_metrics['ece'], 4),
                        'brier_raw': round(raw_metrics['brier'], 4),
                        'brier_cal': round(cal_metrics['brier'], 4),
                        'brier_delta': round(raw_metrics['brier'] - cal_metrics['brier'], 4),
                    })
                except (Exception, KeyError, IndexError) as e:
                    results.append({
                        'n_samples': n,
                        'method': method,
                        'ece_raw': round(raw_metrics['ece'], 4),
                        'ece_cal': float('nan'),
                        'ece_delta': float('nan'),
                        'brier_raw': round(raw_metrics['brier'], 4),
                        'brier_cal': float('nan'),
                        'brier_delta': float('nan'),
                    })

        df = pd.DataFrame(results)
        logger.info(f"稀疏数据测试完成: {len(sample_sizes)} 样本量 × {len(self.methods)} 方法")
        return df

    def _compute_metrics(self, y_true: np.ndarray, probs: np.ndarray) -> Dict[str, float]:
        """计算全部校准指标"""
        try:
            brier = multiclass_brier(probs, y_true, self.n_classes)
        except (Exception, KeyError, IndexError):
            brier = float('nan')

        try:
            ece = compute_ece(probs, y_true)
        except (Exception, ValueError, KeyError, IndexError):
            ece = float('nan')

        try:
            mce = compute_mce(probs, y_true)
        except (Exception, ValueError):
            mce = float('nan')

        try:
            ll = log_loss(y_true, probs, labels=list(range(self.n_classes)))
        except (Exception, ValueError):
            ll = float('nan')

        return {'brier': brier, 'ece': ece, 'mce': mce, 'log_loss': ll}

    # ─── 持久化 ───

    def save(self, path: str, method: str = None):
        """保存校准器"""
        import joblib
        if method is None:
            method = self.best_method()

        data = {
            'calibrator': self._calibrators.get(method),
            'method': method,
            'reports': {k: {'brier_delta': v.brier_delta, 'ece_delta': v.ece_delta,
                            'll_delta': v.ll_delta, 'recommendation': v.recommendation}
                       for k, v in self._reports.items()},
            'saved_at': datetime.now().isoformat(),
            'version': '1.0',
        }
        joblib.dump(data, path, compress=3)
        logger.info(f"校准器已保存: {path} (方法: {method})")

    @classmethod
    def load(cls, path: str) -> 'CalibratorSuite':
        """加载校准器"""
        import joblib
        data = joblib.load(path)
        suite = cls(methods=[data['method']])
        suite._calibrators[data['method']] = data['calibrator']
        logger.info(f"校准器已加载: {path} (方法: {data['method']})")
        return suite


# ════════════════════════════════════════════════════════════════
# 便捷函数
# ════════════════════════════════════════════════════════════════

def calibrate_predictions(y_true: np.ndarray, raw_probs: np.ndarray,
                           method: str = 'platt') -> np.ndarray:
    """快捷校准"""
    suite = CalibratorSuite(methods=[method])
    suite.fit(y_true, raw_probs)
    return suite.predict(raw_probs, method=method)


def compare_calibrators(y_true: np.ndarray, raw_probs: np.ndarray) -> pd.DataFrame:
    """快捷对比"""
    suite = CalibratorSuite()
    suite.fit(y_true, raw_probs)
    return suite.compare()


# ════════════════════════════════════════════════════════════════
# 与 ExpertCalibrator 的桥接
# ════════════════════════════════════════════════════════════════

def upgrade_expert_calibrator(expert_name: str, db_path: str = None) -> Dict:
    """
    为 ExpertCalibrator 提供 T15 增强对比

    在 ExpertCalibrator 基础上, 使用 CalibratorSuite 做全方法对比,
    自动选择最优校准方法。

    Args:
        expert_name: 专家名
        db_path: 数据库路径

    Returns:
        对比结果
    """
    from agents.expert_calibrator import ExpertCalibrator

    ec = ExpertCalibrator(expert_name)
    db = db_path or _get_db_path()
    n = ec.collect_predictions(db)

    if n < 100:
        return {'error': f'样本不足: {n}'}

    suite = CalibratorSuite()
    results = suite.fit(ec.y_true, ec.X_raw)
    compare_df = suite.compare()

    # 稀疏数据测试
    sparse_df = suite.sparse_data_test()

    return {
        'expert': expert_name,
        'n_samples': n,
        'compare': compare_df.to_dict('records'),
        'best_method': suite.best_method(),
        'sparse_test': sparse_df.to_dict('records'),
    }


def _get_db_path():
    import os
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'football_data.db'
    )


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    print("=" * 60)
    print("T15 概率校准模块 — 多方法对比")
    print("=" * 60)

    # 生成模拟数据 (3分类)
    np.random.seed(42)
    n = 2000
    y_true = np.random.choice(3, n, p=[0.45, 0.28, 0.27])

    # 模拟原始概率 (带偏差: 高估主胜, 低估平局)
    raw_probs = np.zeros((n, 3))
    for i in range(n):
        if y_true[i] == 0:
            raw_probs[i] = np.random.dirichlet([5, 2, 2])
        elif y_true[i] == 1:
            raw_probs[i] = np.random.dirichlet([4, 3, 2])
        else:
            raw_probs[i] = np.random.dirichlet([3, 2, 4])

    # 引入系统性偏差
    raw_probs[:, 0] += 0.08  # 高估主胜
    raw_probs[:, 1] -= 0.05  # 低估平局
    raw_probs = np.clip(raw_probs, 0.01, 0.98)
    raw_probs = raw_probs / raw_probs.sum(axis=1, keepdims=True)

    suite = CalibratorSuite()
    suite.fit(y_true, raw_probs)

    # 对比
    print("\n[对比报告]")
    compare_df = suite.compare()
    print(compare_df.to_string(index=False))

    print(f"\n最优方法: {suite.best_method()}")

    # 稀疏数据测试
    print("\n[稀疏数据适应性]")
    sparse_df = suite.sparse_data_test()
    print(sparse_df.to_string(index=False))

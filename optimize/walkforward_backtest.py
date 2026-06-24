"""
哨响AI - 滚动窗口回测框架 (T17)
===================================
实现 walk-forward 验证策略、按月/季度时间分割、滚动窗口回测引擎，
以及回测结果分析和历史性能报告生成。

核心组件:
  1. TimeSplitter — 时间分割策略
     - expanding: 扩展窗口 (训练集不断扩大)
     - sliding: 滑动窗口 (固定训练窗口大小)
     - 支持月/季度/赛季/自定义频率
  2. WalkForwardEngine — 滚动窗口回测引擎
     - 逐折训练+预测
     - 支持任意预测器 (ELO/Ensemble/Expert/自定义)
     - 与 T15/T16 校准系统集成
  3. BacktestResult — 回测结果容器
     - 逐折/汇总指标 (Accuracy/Brier/ECE/LogLoss/MCC)
     - 置信区间 & 性能退化检测
     - 按联赛/时段分解
  4. BacktestVisualizer — 回测可视化
     - 滚动性能曲线
     - 逐折雷达图
     - 退化热力图
  5. BacktestReportBuilder — HTML 历史性能报告

依赖:
  - numpy, pandas, matplotlib
  - T15 calibration.py (compute_ece, multiclass_brier)
  - T16 calibration_viz.py (ECEMonitor, CalibrationVisualizer)

用法:
    from optimize.walkforward_backtest import TimeSplitter, WalkForwardEngine

    splitter = TimeSplitter(freq='quarter')
    folds = splitter.split(df, date_col='match_date')

    engine = WalkForwardEngine(predictor_factory=my_predictor)
    result = engine.run(df, folds, label_col='result_label', prob_cols=['home_prob','draw_prob','away_prob'])
    result.summary()
"""

import logging
import os
import json
import warnings
from typing import Dict, List, Optional, Tuple, Union, Callable
from dataclasses import dataclass, field
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

# matplotlib Agg backend
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from matplotlib.gridspec import GridSpec

# sklearn metrics
from sklearn.metrics import (
    accuracy_score, log_loss, brier_score_loss,
    matthews_corrcoef, confusion_matrix, classification_report
)

# T15 calibration
from optimize.calibration import compute_ece, multiclass_brier, CalibratorSuite

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════════

CLASS_LABELS = ['Home', 'Draw', 'Away']
DEFAULT_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'evaluation_results')


# ════════════════════════════════════════════════════════════════
# 1. TimeSplitter — 时间分割策略
# ════════════════════════════════════════════════════════════════

@dataclass
class TimeFold:
    """单折训练/测试数据索引"""
    fold_id: int
    train_idx: np.ndarray
    test_idx: np.ndarray
    train_start: str    # 训练集起始日期
    train_end: str      # 训练集结束日期
    test_start: str      # 测试集起始日期
    test_end: str        # 测试集结束日期
    n_train: int
    n_test: int
    label: str = ''      # 可读标签 e.g. "Q3 2024"

    def __repr__(self):
        return (f"Fold({self.fold_id}): train[{self.train_start}~{self.train_end}] "
                f"({self.n_train}) → test[{self.test_start}~{self.test_end}] ({self.n_test})")


class TimeSplitter:
    """
    时序数据分割器 — 支持月/季度/赛季/自定义频率的 walk-forward 分割。

    Parameters
    ----------
    freq : str
        分割频率: 'month' | 'quarter' | 'season' | 'year' | 'custom'
    window : str
        窗口模式: 'expanding' (扩展窗口) | 'sliding' (滑动窗口)
    train_size : int
        滑动窗口模式下训练窗口大小 (按 freq 单位计)。
        expanding 模式下忽略此参数。
    min_train : int
        最小训练样本数 (少于此数的折会被跳过)
    min_test : int
        最小测试样本数
    gap : int
        训练集和测试集之间的间隔 (按 freq 单位计), 避免数据泄露
    """

    # 频率 → pandas Period 频率字符串
    FREQ_MAP = {
        'month': 'M',       # 月
        'quarter': 'Q',     # 季度
        'season': 'Q',      # 赛季 (按季度近似)
        'year': 'Y',        # 年
    }

    def __init__(self, freq: str = 'quarter', window: str = 'expanding',
                 train_size: int = 8, min_train: int = 200, min_test: int = 30,
                 gap: int = 0):
        assert freq in (*self.FREQ_MAP, 'custom'), f"freq must be one of {list(self.FREQ_MAP)} + 'custom'"
        assert window in ('expanding', 'sliding'), "window must be 'expanding' or 'sliding'"

        self.freq = freq
        self.window = window
        self.train_size = train_size
        self.min_train = min_train
        self.min_test = min_test
        self.gap = gap

    def split(self, df: pd.DataFrame, date_col: str = 'match_date',
              custom_periods: Optional[List[Tuple[str, str]]] = None) -> List[TimeFold]:
        """
        对 DataFrame 执行 walk-forward 分割。

        Parameters
        ----------
        df : pd.DataFrame
            必须包含 date_col 列 (可解析为日期)
        date_col : str
            日期列名
        custom_periods : list of (start, end) tuples, optional
            仅 freq='custom' 时使用

        Returns
        -------
        list of TimeFold
        """
        # 解析日期
        dates = pd.to_datetime(df[date_col])
        sorted_pos = dates.argsort().values  # 排序位置 → 原始索引的映射
        df_sorted = df.iloc[sorted_pos].reset_index(drop=True)
        dates_sorted = dates.iloc[sorted_pos].reset_index(drop=True)

        if self.freq == 'custom':
            return self._split_custom(df_sorted, dates_sorted, custom_periods or [])

        # 按频率切分时间区间
        period_freq = self.FREQ_MAP[self.freq]
        periods = dates_sorted.dt.to_period(period_freq)

        unique_periods = np.sort(periods.unique())
        period_info = []
        for p in unique_periods:
            mask = periods == p
            indices = np.where(mask)[0]
            if len(indices) >= self.min_test // 5:  # 至少有一些数据
                period_info.append({
                    'period': p,
                    'start': dates_sorted.iloc[indices[0]].strftime('%Y-%m-%d'),
                    'end': dates_sorted.iloc[indices[-1]].strftime('%Y-%m-%d'),
                    'indices': indices,
                    'n': len(indices),
                })

        # 构建折
        folds = []
        fold_id = 0
        train_period_start = 0

        for i in range(len(period_info)):
            # 确定训练区间
            if self.window == 'expanding':
                train_end = i - self.gap - 1
                train_start = 0
            else:  # sliding
                train_end = i - self.gap - 1
                train_start = max(0, train_end - self.train_size + 1)

            if train_end < 0:
                continue  # 没有训练数据

            # 收集排序后索引，映射回原始 DataFrame 索引
            sorted_train_idx = np.concatenate([period_info[j]['indices'] for j in range(train_start, train_end + 1)])
            sorted_test_idx = period_info[i]['indices']
            train_idx = sorted_pos[sorted_train_idx]
            test_idx = sorted_pos[sorted_test_idx]

            if len(train_idx) < self.min_train:
                continue

            if len(test_idx) < self.min_test:
                continue

            label = str(period_info[i]['period'])
            folds.append(TimeFold(
                fold_id=fold_id,
                train_idx=train_idx,
                test_idx=test_idx,
                train_start=period_info[train_start]['start'],
                train_end=period_info[train_end]['end'],
                test_start=period_info[i]['start'],
                test_end=period_info[i]['end'],
                n_train=len(train_idx),
                n_test=len(test_idx),
                label=label,
            ))
            fold_id += 1

        logger.info(f"TimeSplitter(freq={self.freq}, window={self.window}): "
                     f"{len(folds)} folds from {len(df)} samples")
        return folds

    def _split_custom(self, df, dates, periods):
        """自定义时间段分割"""
        folds = []
        for fold_id, (start, end) in enumerate(periods):
            start_dt = pd.to_datetime(start)
            end_dt = pd.to_datetime(end)
            mask_test = (dates >= start_dt) & (dates <= end_dt)
            # 训练集: 所有早于 start 的数据
            mask_train = dates < start_dt

            train_idx = np.where(mask_train)[0]
            test_idx = np.where(mask_test)[0]

            if len(train_idx) < self.min_train or len(test_idx) < self.min_test:
                continue

            folds.append(TimeFold(
                fold_id=fold_id,
                train_idx=train_idx,
                test_idx=test_idx,
                train_start=dates.iloc[train_idx[0]].strftime('%Y-%m-%d'),
                train_end=dates.iloc[train_idx[-1]].strftime('%Y-%m-%d'),
                test_start=start,
                test_end=end,
                n_train=len(train_idx),
                n_test=len(test_idx),
                label=f"{start}~{end}",
            ))
        return folds

    def _period_freq(self):
        """将 freq 映射为 pandas Period 频率字符串"""
        mapping = {'month': 'M', 'quarter': 'Q', 'season': 'Q', 'year': 'Y'}
        return mapping.get(self.freq, 'Q')


# ════════════════════════════════════════════════════════════════
# 2. FoldMetrics — 单折指标
# ════════════════════════════════════════════════════════════════

@dataclass
class FoldMetrics:
    """单折回测指标"""
    fold_id: int
    label: str
    n_test: int
    accuracy: float
    brier: float
    ece: float
    log_loss: float
    mcc: float
    # 分类别
    home_recall: float
    draw_recall: float
    away_recall: float
    # 校准相关
    ece_home: float = 0.0
    ece_draw: float = 0.0
    ece_away: float = 0.0
    # 混淆矩阵 (3x3)
    confusion_matrix: Optional[np.ndarray] = None
    # 元数据
    train_size: int = 0
    test_period: str = ''


# ════════════════════════════════════════════════════════════════
# 3. WalkForwardEngine — 回测引擎
# ════════════════════════════════════════════════════════════════

class WalkForwardEngine:
    """
    滚动窗口回测引擎。

    支持两种模式:
    1. 预计算概率模式: DataFrame 中已有预测概率列
    2. 预测器工厂模式: 每折重新训练并预测

    Parameters
    ----------
    predictor_factory : callable, optional
        签名: (train_df, test_df) → (y_pred_proba, y_true)
        y_pred_proba: (n, 3) 数组, y_true: (n,) 数组
    calibrate : bool
        是否对每折进行概率校准 (T15)
    calibrate_method : str
        校准方法: 'auto' | 'platt' | 'temperature' | 'isotonic' | 'beta'
    """

    def __init__(self, predictor_factory: Optional[Callable] = None,
                 calibrate: bool = False, calibrate_method: str = 'auto'):
        self.predictor_factory = predictor_factory
        self.calibrate = calibrate
        self.calibrate_method = calibrate_method

    def run(self, df: pd.DataFrame, folds: List[TimeFold],
            label_col: str = 'result_label',
            prob_cols: Optional[List[str]] = None,
            callback: Optional[Callable] = None) -> 'BacktestResult':
        """
        执行 walk-forward 回测。

        Parameters
        ----------
        df : pd.DataFrame
            全量数据
        folds : list of TimeFold
            时间分割折
        label_col : str
            真实标签列名 (0=Home, 1=Draw, 2=Away)
        prob_cols : list of str, optional
            预测概率列名, e.g. ['home_prob', 'draw_prob', 'away_prob']
            如果提供, 使用预计算概率; 否则使用 predictor_factory
        callback : callable, optional
            每折完成后的回调: callback(fold_id, FoldMetrics)

        Returns
        -------
        BacktestResult
        """
        all_metrics = []
        all_predictions = []  # 收集所有折的预测
        all_labels = []
        all_fold_ids = []

        for fold in folds:
            logger.info(f"Fold {fold.fold_id}: {fold.label} "
                        f"(train={fold.n_train}, test={fold.n_test})")

            train_df = df.iloc[fold.train_idx]
            test_df = df.iloc[fold.test_idx]

            # 获取预测概率
            if prob_cols is not None:
                # 预计算概率模式
                y_proba = test_df[prob_cols].values.astype(float)
                y_true = test_df[label_col].values.astype(int)
            elif self.predictor_factory is not None:
                # 预测器工厂模式
                y_proba, y_true = self.predictor_factory(train_df, test_df)
            else:
                raise ValueError("Either prob_cols or predictor_factory must be provided")

            # 概率归一化 (确保和为1)
            row_sums = y_proba.sum(axis=1, keepdims=True)
            row_sums[row_sums == 0] = 1.0
            y_proba = y_proba / row_sums

            # 裁剪避免 log(0)
            y_proba = np.clip(y_proba, 1e-10, 1.0)
            y_proba = y_proba / y_proba.sum(axis=1, keepdims=True)

            # 可选校准
            if self.calibrate and len(train_df) >= 100:
                y_proba = self._calibrate_fold(train_df, test_df, y_proba, label_col, prob_cols)

            # 计算指标
            metrics = self._compute_fold_metrics(fold, y_proba, y_true)
            all_metrics.append(metrics)

            # 收集预测
            all_predictions.append(y_proba)
            all_labels.append(y_true)
            all_fold_ids.extend([fold.fold_id] * len(y_true))

            if callback:
                callback(fold.fold_id, metrics)

            logger.info(f"  Acc={metrics.accuracy:.4f} Brier={metrics.brier:.4f} "
                         f"ECE={metrics.ece:.4f} LL={metrics.log_loss:.4f}")

        # 合并所有折
        all_pred = np.vstack(all_predictions) if all_predictions else np.array([])
        all_true = np.concatenate(all_labels) if all_labels else np.array([])

        return BacktestResult(
            fold_metrics=all_metrics,
            all_predictions=all_pred,
            all_labels=all_true,
            all_fold_ids=np.array(all_fold_ids),
            n_folds=len(folds),
        )

    def _calibrate_fold(self, train_df, test_df, test_proba, label_col, prob_cols):
        """对单折应用概率校准"""
        try:
            # 训练集标签和概率
            if prob_cols is not None:
                train_proba = train_df[prob_cols].values.astype(float)
                train_labels = train_df[label_col].values.astype(int)
            else:
                return test_proba  # 无法校准

            # 归一化训练概率
            row_sums = train_proba.sum(axis=1, keepdims=True)
            row_sums[row_sums == 0] = 1.0
            train_proba = train_proba / row_sums
            train_proba = np.clip(train_proba, 1e-10, 1.0)
            train_proba = train_proba / train_proba.sum(axis=1, keepdims=True)

            suite = CalibratorSuite()
            suite.fit(train_labels, train_proba, test_ratio=0.0)  # 全部训练集用于校准

            method = self.calibrate_method
            if method == 'auto':
                method = suite.best_method(metric='ece')

            return suite.predict(test_proba, method=method)
        except (Exception) as e:
            logger.warning(f"Calibration failed for fold: {e}")
            return test_proba

    @staticmethod
    def _compute_fold_metrics(fold: TimeFold, y_proba: np.ndarray,
                               y_true: np.ndarray) -> FoldMetrics:
        """计算单折评估指标"""
        y_true = np.asarray(y_true, dtype=np.int64)
        y_pred = np.argmax(y_proba, axis=1)

        # 基础指标
        acc = accuracy_score(y_true, y_pred)
        brier = multiclass_brier(y_proba, y_true)
        ece = compute_ece(y_proba, y_true)
        ll = log_loss(y_true, y_proba, labels=[0, 1, 2])
        mcc = matthews_corrcoef(y_true, y_pred)

        # 分类别召回率
        cm = confusion_matrix(y_true, y_pred, labels=[0, 1, 2])
        recalls = cm.diagonal() / cm.sum(axis=1).clip(1)

        # 分类别 ECE
        ece_per_class = []
        for c in range(3):
            mask = (y_true == c)
            if mask.sum() > 10:
                class_proba = y_proba[mask]
                class_labels = np.ones(mask.sum(), dtype=int)  # 二分类: 正确=1
                class_conf = class_proba[:, c]  # 该类的预测概率
                # ECE for this class (binary)
                class_preds = (np.argmax(class_proba, axis=1) == c).astype(int)
                ece_c = compute_ece(
                    np.column_stack([1 - class_conf, class_conf]),
                    class_preds
                )
                ece_per_class.append(ece_c)
            else:
                ece_per_class.append(0.0)

        return FoldMetrics(
            fold_id=fold.fold_id,
            label=fold.label,
            n_test=len(y_true),
            accuracy=acc,
            brier=brier,
            ece=ece,
            log_loss=ll,
            mcc=mcc,
            home_recall=float(recalls[0]) if len(recalls) > 0 else 0.0,
            draw_recall=float(recalls[1]) if len(recalls) > 1 else 0.0,
            away_recall=float(recalls[2]) if len(recalls) > 2 else 0.0,
            ece_home=ece_per_class[0] if len(ece_per_class) > 0 else 0.0,
            ece_draw=ece_per_class[1] if len(ece_per_class) > 1 else 0.0,
            ece_away=ece_per_class[2] if len(ece_per_class) > 2 else 0.0,
            confusion_matrix=cm,
            train_size=fold.n_train,
            test_period=f"{fold.test_start}~{fold.test_end}",
        )


# ════════════════════════════════════════════════════════════════
# 4. BacktestResult — 回测结果容器
# ════════════════════════════════════════════════════════════════

@dataclass
class BacktestResult:
    """回测结果 — 包含逐折和汇总指标"""
    fold_metrics: List[FoldMetrics]
    all_predictions: np.ndarray
    all_labels: np.ndarray
    all_fold_ids: np.ndarray
    n_folds: int

    # ── 汇总统计 ──

    def summary(self) -> Dict:
        """返回汇总指标字典"""
        if not self.fold_metrics:
            return {}

        metrics_names = ['accuracy', 'brier', 'ece', 'log_loss', 'mcc']
        result = {'n_folds': self.n_folds}

        for name in metrics_names:
            vals = [getattr(fm, name) for fm in self.fold_metrics]
            result[f'{name}_mean'] = float(np.mean(vals))
            result[f'{name}_std'] = float(np.std(vals))
            result[f'{name}_min'] = float(np.min(vals))
            result[f'{name}_max'] = float(np.max(vals))
            # 95% 置信区间
            n = len(vals)
            se = np.std(vals, ddof=1) / np.sqrt(n) if n > 1 else 0
            result[f'{name}_ci95'] = float(1.96 * se)

        # 整体指标 (用所有预测计算, 非折平均)
        if len(self.all_predictions) > 0:
            y_pred = np.argmax(self.all_predictions, axis=1)
            result['overall_accuracy'] = float(accuracy_score(self.all_labels, y_pred))
            result['overall_brier'] = float(multiclass_brier(self.all_predictions, self.all_labels))
            result['overall_ece'] = float(compute_ece(self.all_predictions, self.all_labels))
            result['overall_log_loss'] = float(log_loss(self.all_labels, self.all_predictions,
                                                         labels=[0, 1, 2]))
            result['overall_mcc'] = float(matthews_corrcoef(self.all_labels, y_pred))
            result['total_samples'] = int(len(self.all_labels))

        # 分类召回率
        for cls_name in ['home', 'draw', 'away']:
            vals = [getattr(fm, f'{cls_name}_recall') for fm in self.fold_metrics]
            result[f'{cls_name}_recall_mean'] = float(np.mean(vals))

        return result

    def to_dataframe(self) -> pd.DataFrame:
        """折指标转为 DataFrame"""
        records = []
        for fm in self.fold_metrics:
            records.append({
                'fold': fm.fold_id,
                'label': fm.label,
                'n_test': fm.n_test,
                'train_size': fm.train_size,
                'accuracy': fm.accuracy,
                'brier': fm.brier,
                'ece': fm.ece,
                'log_loss': fm.log_loss,
                'mcc': fm.mcc,
                'home_recall': fm.home_recall,
                'draw_recall': fm.draw_recall,
                'away_recall': fm.away_recall,
            })
        return pd.DataFrame(records)

    def degradation_check(self, window: int = 3, threshold: float = 0.05) -> List[Dict]:
        """
        检测性能退化。

        Parameters
        ----------
        window : int
            滑动窗口大小 (折数)
        threshold : float
            退化阈值 (相对下降)

        Returns
        -------
        list of dict with keys: metric, fold, delta, severity
        """
        alerts = []
        metrics_names = ['accuracy', 'brier', 'ece', 'log_loss']

        for metric in metrics_names:
            vals = [getattr(fm, metric) for fm in self.fold_metrics]
            for i in range(window, len(vals)):
                prev_avg = np.mean(vals[i - window:i])
                curr = vals[i]
                if metric in ('accuracy', 'mcc'):
                    delta = prev_avg - curr  # 下降是负值
                else:
                    delta = curr - prev_avg  # 上升是退化

                if delta > threshold:
                    severity = 'CRITICAL' if delta > threshold * 2 else 'WARNING'
                    alerts.append({
                        'metric': metric,
                        'fold': self.fold_metrics[i].fold_id,
                        'label': self.fold_metrics[i].label,
                        'previous_avg': round(prev_avg, 4),
                        'current': round(curr, 4),
                        'delta': round(delta, 4),
                        'severity': severity,
                    })

        return alerts

    def confidence_interval(self, metric: str = 'accuracy',
                            confidence: float = 0.95) -> Tuple[float, float, float]:
        """
        计算指定指标的置信区间 (bootstrap)。

        Returns
        -------
        (mean, lower, upper)
        """
        vals = [getattr(fm, metric) for fm in self.fold_metrics]
        vals = np.array(vals)

        n_bootstrap = 1000
        rng = np.random.RandomState(42)
        boot_means = []
        for _ in range(n_bootstrap):
            sample = rng.choice(vals, size=len(vals), replace=True)
            boot_means.append(np.mean(sample))

        boot_means = np.array(boot_means)
        alpha = (1 - confidence) / 2
        lower = float(np.percentile(boot_means, alpha * 100))
        upper = float(np.percentile(boot_means, (1 - alpha) * 100))
        mean = float(np.mean(vals))

        return mean, lower, upper


# ════════════════════════════════════════════════════════════════
# 5. BacktestVisualizer — 回测可视化
# ════════════════════════════════════════════════════════════════

class BacktestVisualizer:
    """
    回测结果可视化工具。

    Parameters
    ----------
    output_dir : str
        图表输出目录
    dpi : int
        图表分辨率
    """

    def __init__(self, output_dir: str = DEFAULT_OUTPUT_DIR, dpi: int = 150):
        self.output_dir = output_dir
        self.dpi = dpi
        os.makedirs(output_dir, exist_ok=True)

    def rolling_performance(self, result: BacktestResult,
                            metrics: Optional[List[str]] = None,
                            title: str = 'Rolling Performance') -> str:
        """
        滚动性能曲线图。

        Returns
        -------
        输出文件路径
        """
        if metrics is None:
            metrics = ['accuracy', 'brier', 'ece', 'log_loss']

        fig, axes = plt.subplots(len(metrics), 1, figsize=(14, 3.5 * len(metrics)),
                                  sharex=True)
        if len(metrics) == 1:
            axes = [axes]

        colors = {'accuracy': '#1E88E5', 'brier': '#E53935', 'ece': '#FB8C00',
                  'log_loss': '#43A047', 'mcc': '#8E24AA'}
        higher_better = {'accuracy', 'mcc'}

        for ax, metric in zip(axes, metrics):
            vals = [getattr(fm, metric) for fm in result.fold_metrics]
            labels = [fm.label for fm in result.fold_metrics]
            color = colors.get(metric, '#333333')

            ax.plot(range(len(vals)), vals, 'o-', color=color, linewidth=2, markersize=6)

            # 均值线
            mean_val = np.mean(vals)
            ax.axhline(y=mean_val, color=color, linestyle='--', alpha=0.4,
                       label=f'Mean: {mean_val:.4f}')

            # 趋势线
            if len(vals) > 2:
                z = np.polyfit(range(len(vals)), vals, 1)
                p = np.poly1d(z)
                trend = p(range(len(vals)))
                ax.plot(range(len(vals)), trend, ':', color=color, alpha=0.5, linewidth=1.5,
                        label=f'Trend: {z[0]:+.4f}/fold')

            # 标注退化
            if metric not in higher_better:
                # 指标越低越好, 上升趋势=退化
                for i in range(1, len(vals)):
                    if vals[i] > vals[i-1] * 1.1:
                        ax.annotate('↑', (i, vals[i]), fontsize=10, color='red', ha='center')
            else:
                for i in range(1, len(vals)):
                    if vals[i] < vals[i-1] * 0.9:
                        ax.annotate('↓', (i, vals[i]), fontsize=10, color='red', ha='center')

            ax.set_ylabel(metric.upper())
            ax.legend(loc='best', fontsize=9)
            ax.grid(True, alpha=0.3)

        axes[-1].set_xlabel('Fold')
        fig.suptitle(title, fontsize=14, fontweight='bold', y=1.01)
        plt.tight_layout()

        path = os.path.join(self.output_dir, 'rolling_performance.png')
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"Saved rolling performance chart: {path}")
        return path

    def fold_radar(self, result: BacktestResult,
                   title: str = 'Fold Performance Radar') -> str:
        """
        逐折雷达图对比。

        Returns
        -------
        输出文件路径
        """
        categories = ['Accuracy', 'Brier-Inv', 'ECE-Inv', 'LL-Inv', 'MCC']
        n_cats = len(categories)

        # 归一化到 [0, 1]
        metrics_raw = []
        for fm in result.fold_metrics:
            metrics_raw.append([fm.accuracy, fm.brier, fm.ece, fm.log_loss, fm.mcc])

        raw = np.array(metrics_raw)
        # 反转 (越低越好的指标)
        normalized = np.zeros_like(raw)
        for j in range(raw.shape[1]):
            col = raw[:, j]
            if j in (1, 2, 3):  # brier, ece, log_loss → 越低越好, 反转
                normalized[:, j] = 1 - col if col.max() <= 1 else (col.max() - col) / (col.max() - col.min() + 1e-10)
            else:
                if col.max() > col.min():
                    normalized[:, j] = (col - col.min()) / (col.max() - col.min())
                else:
                    normalized[:, j] = 0.5

        angles = np.linspace(0, 2 * np.pi, n_cats, endpoint=False).tolist()
        angles += angles[:1]

        fig, ax = plt.subplots(figsize=(8, 8), subplot_kw=dict(polar=True))
        cmap = plt.cm.Set2
        n_folds = len(result.fold_metrics)
        max_folds_to_show = min(n_folds, 12)  # 最多展示12折

        for i in range(max_folds_to_show):
            values = normalized[i].tolist()
            values += values[:1]
            color = cmap(i / max_folds_to_show)
            ax.plot(angles, values, 'o-', linewidth=1.5, markersize=4,
                    color=color, label=f'F{result.fold_metrics[i].fold_id}', alpha=0.7)
            ax.fill(angles, values, alpha=0.05, color=color)

        ax.set_xticks(angles[:-1])
        ax.set_xticklabels(categories)
        ax.set_title(title, pad=20, fontsize=13, fontweight='bold')
        ax.legend(loc='upper right', bbox_to_anchor=(1.3, 1.1), fontsize=8, ncol=2)

        path = os.path.join(self.output_dir, 'fold_radar.png')
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"Saved fold radar chart: {path}")
        return path

    def metric_heatmap(self, result: BacktestResult,
                       metric: str = 'accuracy',
                       group_by: Optional[str] = None,
                       df: Optional[pd.DataFrame] = None,
                       title: str = '') -> str:
        """
        指标热力图 (折 × 联赛/类别)。

        Parameters
        ----------
        result : BacktestResult
        metric : str
        group_by : str, optional
            'league' | 'class' | None
        df : pd.DataFrame, optional
            原始 DataFrame, 用于提取联赛信息
        title : str

        Returns
        -------
        输出文件路径
        """
        if not title:
            title = f'{metric.upper()} Heatmap'

        if group_by == 'league' and df is not None and 'league_name' in df.columns:
            # 按联赛 × 折
            leagues = sorted(df['league_name'].unique())
            fold_labels = [fm.label for fm in result.fold_metrics]
            data = np.zeros((len(leagues), len(fold_labels)))

            for j, fm in enumerate(result.fold_metrics):
                test_idx = np.where(result.all_fold_ids == fm.fold_id)[0]
                fold_df = df.iloc[test_idx] if len(test_idx) == fm.n_test else None
                if fold_df is not None:
                    for i, league in enumerate(leagues):
                        league_mask = fold_df['league_name'].values == league
                        if league_mask.sum() > 5:
                            league_pred = result.all_predictions[test_idx][league_mask]
                            league_true = result.all_labels[test_idx][league_mask]
                            if metric == 'accuracy':
                                data[i, j] = accuracy_score(league_true, np.argmax(league_pred, axis=1))
                            elif metric == 'brier':
                                data[i, j] = multiclass_brier(league_true, league_pred)

            # 只保留非零行
            nonzero_rows = data.sum(axis=1) > 0
            data = data[nonzero_rows]
            leagues = [l for l, m in zip(leagues, nonzero_rows) if m]

            w = max(10, len(fold_labels) * 1.2)
            h = max(6, len(leagues) * 0.4)
            fig, ax = plt.subplots(figsize=(w, h))
            im = ax.imshow(data, cmap='RdYlGn' if metric in ('accuracy', 'mcc') else 'RdYlGn_r',
                           aspect='auto')
            ax.set_xticks(range(len(fold_labels)))
            ax.set_xticklabels(fold_labels, rotation=45, ha='right', fontsize=9)
            ax.set_yticks(range(len(leagues)))
            ax.set_yticklabels(leagues, fontsize=9)
            ax.set_title(title)

            # 标注数值
            for i in range(data.shape[0]):
                for j in range(data.shape[1]):
                    if data[i, j] > 0:
                        ax.text(j, i, f'{data[i, j]:.3f}', ha='center', va='center',
                                fontsize=7, color='black' if 0.3 < data[i, j] < 0.7 else 'white')

            plt.colorbar(im, ax=ax, shrink=0.8)
            plt.tight_layout()

        else:
            # 折 × 指标
            metrics_to_show = ['accuracy', 'brier', 'ece', 'log_loss', 'mcc']
            data = np.zeros((len(metrics_to_show), result.n_folds))
            for j, fm in enumerate(result.fold_metrics):
                for i, m in enumerate(metrics_to_show):
                    data[i, j] = getattr(fm, m)

            w = max(10, result.n_folds * 1.2)
            fig, ax = plt.subplots(figsize=(w, 4))
            # 标准化每行
            for i in range(data.shape[0]):
                row = data[i]
                if row.max() > row.min():
                    data[i] = (row - row.min()) / (row.max() - row.min())
                else:
                    data[i] = 0.5

            im = ax.imshow(data, cmap='RdYlGn', aspect='auto')
            ax.set_xticks(range(result.n_folds))
            ax.set_xticklabels([fm.label for fm in result.fold_metrics],
                               rotation=45, ha='right', fontsize=9)
            ax.set_yticks(range(len(metrics_to_show)))
            ax.set_yticklabels([m.upper() for m in metrics_to_show])
            ax.set_title(title)

            for i in range(data.shape[0]):
                for j in range(data.shape[1]):
                    ax.text(j, i, f'{data[i, j]:.2f}', ha='center', va='center',
                            fontsize=8, color='black' if 0.3 < data[i, j] < 0.7 else 'white')

            plt.colorbar(im, ax=ax, shrink=0.8)
            plt.tight_layout()

        path = os.path.join(self.output_dir, f'metric_heatmap_{metric}.png')
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"Saved metric heatmap: {path}")
        return path

    def strategy_comparison(self, results: Dict[str, BacktestResult],
                            metric: str = 'accuracy',
                            title: str = 'Strategy Comparison') -> str:
        """
        多策略对比柱状图。

        Parameters
        ----------
        results : dict
            {strategy_name: BacktestResult}
        metric : str

        Returns
        -------
        输出文件路径
        """
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

        strategies = list(results.keys())
        means = [np.mean([getattr(fm, metric) for fm in r.fold_metrics]) for r in results.values()]
        stds = [np.std([getattr(fm, metric) for fm in r.fold_metrics]) for r in results.values()]

        colors = plt.cm.Set2(np.linspace(0, 1, len(strategies)))

        # 左: 柱状图 + 误差棒
        x = np.arange(len(strategies))
        ax1.bar(x, means, yerr=stds, capsize=5, color=colors, edgecolor='white', alpha=0.8)
        ax1.set_xticks(x)
        ax1.set_xticklabels(strategies, rotation=30, ha='right')
        ax1.set_ylabel(metric.upper())
        ax1.set_title(f'{metric.upper()} Mean ± Std')
        ax1.grid(axis='y', alpha=0.3)

        # 右: 逐折趋势
        for i, (name, r) in enumerate(results.items()):
            vals = [getattr(fm, metric) for fm in r.fold_metrics]
            ax2.plot(range(len(vals)), vals, 'o-', color=colors[i],
                     linewidth=1.5, markersize=5, label=name, alpha=0.8)

        ax2.set_xlabel('Fold')
        ax2.set_ylabel(metric.upper())
        ax2.set_title(f'{metric.upper()} per Fold')
        ax2.legend(fontsize=9)
        ax2.grid(True, alpha=0.3)

        fig.suptitle(title, fontsize=14, fontweight='bold')
        plt.tight_layout()

        path = os.path.join(self.output_dir, 'strategy_comparison.png')
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"Saved strategy comparison chart: {path}")
        return path

    def confusion_overview(self, result: BacktestResult,
                           title: str = 'Aggregate Confusion Matrix') -> str:
        """汇总混淆矩阵可视化"""
        # 合并所有折的混淆矩阵
        total_cm = np.zeros((3, 3), dtype=int)
        for fm in result.fold_metrics:
            if fm.confusion_matrix is not None:
                total_cm += fm.confusion_matrix

        fig, ax = plt.subplots(figsize=(7, 6))
        im = ax.imshow(total_cm, cmap='Blues')
        ax.set_xticks(range(3))
        ax.set_xticklabels(CLASS_LABELS)
        ax.set_yticks(range(3))
        ax.set_yticklabels(CLASS_LABELS)
        ax.set_xlabel('Predicted')
        ax.set_ylabel('Actual')
        ax.set_title(title)

        # 标注数值
        for i in range(3):
            for j in range(3):
                pct = total_cm[i, j] / total_cm[i].sum() * 100 if total_cm[i].sum() > 0 else 0
                ax.text(j, i, f'{total_cm[i, j]}\n({pct:.1f}%)',
                        ha='center', va='center',
                        fontsize=11, color='white' if total_cm[i, j] > total_cm.max() / 2 else 'black')

        plt.colorbar(im, ax=ax, shrink=0.8)
        plt.tight_layout()

        path = os.path.join(self.output_dir, 'confusion_matrix.png')
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"Saved confusion matrix: {path}")
        return path

    def generate_all_charts(self, result: BacktestResult,
                            df: Optional[pd.DataFrame] = None,
                            extra_results: Optional[Dict[str, BacktestResult]] = None) -> List[str]:
        """一键生成所有图表"""
        paths = []
        paths.append(self.rolling_performance(result))
        paths.append(self.fold_radar(result))
        paths.append(self.metric_heatmap(result, metric='accuracy'))
        paths.append(self.metric_heatmap(result, metric='brier'))
        paths.append(self.confusion_overview(result))

        if df is not None and 'league_name' in df.columns:
            paths.append(self.metric_heatmap(result, metric='accuracy',
                                              group_by='league', df=df,
                                              title='Accuracy by League & Fold'))

        if extra_results:
            paths.append(self.strategy_comparison(extra_results))

        return paths


# ════════════════════════════════════════════════════════════════
# 6. BacktestReportBuilder — HTML 报告生成
# ════════════════════════════════════════════════════════════════

class BacktestReportBuilder:
    """
    回测结果 HTML 报告生成器。
    生成自包含的 HTML 文件 (图表 base64 嵌入)。
    """

    def __init__(self, output_dir: str = DEFAULT_OUTPUT_DIR):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def _embed_image(self, img_path: str) -> str:
        """将图片转为 base64 嵌入 HTML"""
        import base64
        if not os.path.exists(img_path):
            return ''
        with open(img_path, 'rb') as f:
            data = base64.b64encode(f.read()).decode('utf-8')
        return f'data:image/png;base64,{data}'

    def generate(self, result: BacktestResult,
                 chart_paths: Optional[List[str]] = None,
                 title: str = 'Walk-Forward Backtest Report',
                 df: Optional[pd.DataFrame] = None) -> str:
        """
        生成 HTML 报告。

        Returns
        -------
        HTML 文件路径
        """
        summary = result.summary()
        deg_alerts = result.degradation_check()
        fold_df = result.to_dataframe()

        # 嵌入图表
        chart_html = ''
        if chart_paths:
            for path in chart_paths:
                b64 = self._embed_image(path)
                if b64:
                    chart_html += f'<div style="text-align:center;margin:15px 0;"><img src="{b64}" style="max-width:100%;border-radius:4px;box-shadow:0 1px 3px rgba(0,0,0,0.1);"></div>\n'

        # 指标卡片
        cards_html = ''
        card_items = [
            ('Overall Accuracy', f"{summary.get('overall_accuracy', 0):.4f}"),
            ('Overall Brier', f"{summary.get('overall_brier', 0):.4f}"),
            ('Overall ECE', f"{summary.get('overall_ece', 0):.4f}"),
            ('Overall LogLoss', f"{summary.get('overall_log_loss', 0):.4f}"),
            ('Overall MCC', f"{summary.get('overall_mcc', 0):.4f}"),
            ('Folds', str(summary.get('n_folds', 0))),
            ('Total Samples', str(summary.get('total_samples', 0))),
        ]
        for label, value in card_items:
            cards_html += f'''<div class="metric-card">
                <div class="value">{value}</div>
                <div class="label">{label}</div>
            </div>'''

        # 告警
        alerts_html = ''
        if deg_alerts:
            for a in deg_alerts:
                cls = 'alert-danger' if a['severity'] == 'CRITICAL' else 'alert-warning'
                alerts_html += (f'<div class="alert {cls}">{a["severity"]}: '
                                f'{a["metric"].upper()} degradation at fold {a["label"]} '
                                f'({a["previous_avg"]:.4f} -> {a["current"]:.4f}, '
                                f'delta={a["delta"]:+.4f})</div>\n')

        # 折表格
        table_rows = ''
        for _, row in fold_df.iterrows():
            table_rows += (f'<tr><td>{int(row["fold"])}</td><td>{row["label"]}</td>'
                           f'<td>{int(row["n_test"])}</td>'
                           f'<td>{row["accuracy"]:.4f}</td><td>{row["brier"]:.4f}</td>'
                           f'<td>{row["ece"]:.4f}</td><td>{row["log_loss"]:.4f}</td>'
                           f'<td>{row["mcc"]:.4f}</td>'
                           f'<td>{row["home_recall"]:.4f}</td>'
                           f'<td>{row["draw_recall"]:.4f}</td>'
                           f'<td>{row["away_recall"]:.4f}</td></tr>\n')

        # 置信区间
        ci_rows = ''
        for metric in ['accuracy', 'brier', 'ece', 'log_loss', 'mcc']:
            mean, lower, upper = result.confidence_interval(metric)
            ci_rows += (f'<tr><td>{metric.upper()}</td><td>{mean:.4f}</td>'
                        f'<td>[{lower:.4f}, {upper:.4f}]</td></tr>\n')

        gen_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        body {{ font-family: 'Segoe UI', 'SimHei', sans-serif; margin: 20px; background: #f8f9fa; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        h1 {{ color: #1565C0; border-bottom: 2px solid #1565C0; padding-bottom: 10px; }}
        h2 {{ color: #424242; margin-top: 30px; }}
        .table {{ width: 100%; border-collapse: collapse; margin: 15px 0; font-size: 13px; }}
        .table th, .table td {{ padding: 6px 10px; border: 1px solid #ddd; text-align: center; }}
        .table th {{ background: #1565C0; color: white; }}
        .table-striped tr:nth-child(even) {{ background: #f5f5f5; }}
        .alert {{ padding: 12px 20px; border-radius: 4px; margin: 10px 0; }}
        .alert-warning {{ background: #fff3cd; color: #856404; border: 1px solid #ffeaa7; }}
        .alert-danger {{ background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
        .metric-card {{ display: inline-block; padding: 15px 25px; margin: 5px; background: #e3f2fd; border-radius: 8px; text-align: center; }}
        .metric-card .value {{ font-size: 22px; font-weight: bold; color: #1565C0; }}
        .metric-card .label {{ font-size: 12px; color: #666; }}
        .footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid #ddd; color: #999; font-size: 12px; }}
    </style>
</head>
<body>
<div class="container">
    <h1>{title}</h1>
    <p>Generated: {gen_time}</p>

    {alerts_html}

    <h2>Key Metrics</h2>
    {cards_html}

    <h2>Charts</h2>
    {chart_html}

    <h2>Fold-by-Fold Results</h2>
    <table class="table table-striped">
        <tr><th>Fold</th><th>Period</th><th>N</th><th>Acc</th><th>Brier</th>
            <th>ECE</th><th>LL</th><th>MCC</th><th>H_Rec</th><th>D_Rec</th><th>A_Rec</th></tr>
        {table_rows}
    </table>

    <h2>Confidence Intervals (95% Bootstrap)</h2>
    <table class="table table-striped">
        <tr><th>Metric</th><th>Mean</th><th>95% CI</th></tr>
        {ci_rows}
    </table>

    <div class="footer">
        <p>FootballAI - Walk-Forward Backtest Report (T17) | Auto-generated on {gen_time[:10]}</p>
    </div>
</div>
</body>
</html>"""

        path = os.path.join(self.output_dir, 'backtest_report.html')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html)

        logger.info(f"Saved backtest HTML report: {path}")
        return path


# ════════════════════════════════════════════════════════════════
# 7. 便捷函数
# ════════════════════════════════════════════════════════════════

def run_walkforward_backtest(
    df: pd.DataFrame,
    date_col: str = 'match_date',
    label_col: str = 'result_label',
    prob_cols: Optional[List[str]] = None,
    predictor_factory: Optional[Callable] = None,
    freq: str = 'quarter',
    window: str = 'expanding',
    train_size: int = 8,
    min_train: int = 200,
    min_test: int = 30,
    gap: int = 0,
    calibrate: bool = False,
    calibrate_method: str = 'auto',
    output_dir: Optional[str] = None,
    generate_report: bool = True,
) -> Tuple[BacktestResult, Optional[str]]:
    """
    一键运行 walk-forward 回测 + 可视化 + HTML 报告。

    Parameters
    ----------
    df : pd.DataFrame
    date_col : str
    label_col : str
    prob_cols : list of str, optional
    predictor_factory : callable, optional
    freq : str
    window : str
    train_size : int
    min_train : int
    min_test : int
    gap : int
    calibrate : bool
    calibrate_method : str
    output_dir : str, optional
    generate_report : bool

    Returns
    -------
    (BacktestResult, report_path_or_None)
    """
    if output_dir is None:
        output_dir = DEFAULT_OUTPUT_DIR

    # 分割
    splitter = TimeSplitter(freq=freq, window=window, train_size=train_size,
                             min_train=min_train, min_test=min_test, gap=gap)
    folds = splitter.split(df, date_col=date_col)

    if not folds:
        logger.warning("No valid folds generated! Check data volume and min_train/min_test thresholds.")
        return BacktestResult(fold_metrics=[], all_predictions=np.array([]),
                               all_labels=np.array([]), all_fold_ids=np.array([]), n_folds=0), None

    logger.info(f"Generated {len(folds)} folds: {folds[0]} ... {folds[-1]}")

    # 回测
    engine = WalkForwardEngine(predictor_factory=predictor_factory,
                                calibrate=calibrate, calibrate_method=calibrate_method)
    result = engine.run(df, folds, label_col=label_col, prob_cols=prob_cols)

    # 可视化 + 报告
    report_path = None
    if generate_report:
        viz = BacktestVisualizer(output_dir=output_dir)
        chart_paths = viz.generate_all_charts(result, df=df)

        builder = BacktestReportBuilder(output_dir=output_dir)
        report_path = builder.generate(result, chart_paths=chart_paths, df=df)

    return result, report_path


def run_multi_strategy_comparison(
    df: pd.DataFrame,
    strategies: Dict[str, Optional[List[str]]],
    date_col: str = 'match_date',
    label_col: str = 'result_label',
    freq: str = 'quarter',
    window: str = 'expanding',
    output_dir: Optional[str] = None,
) -> Tuple[Dict[str, BacktestResult], Optional[str]]:
    """
    多策略对比回测。

    Parameters
    ----------
    strategies : dict
        {strategy_name: prob_cols_list} 每个策略的概率列名列表

    Returns
    -------
    (results_dict, report_path)
    """
    if output_dir is None:
        output_dir = DEFAULT_OUTPUT_DIR

    splitter = TimeSplitter(freq=freq, window=window)
    folds = splitter.split(df, date_col=date_col)

    results = {}
    for name, prob_cols in strategies.items():
        logger.info(f"Running strategy: {name}")
        engine = WalkForwardEngine()
        result = engine.run(df, folds, label_col=label_col, prob_cols=prob_cols)
        results[name] = result

    # 对比图
    if len(results) > 1 and output_dir:
        viz = BacktestVisualizer(output_dir=output_dir)
        viz.strategy_comparison(results)

    return results, None


# ════════════════════════════════════════════════════════════════
# 8. __main__ 测试
# ════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    print("=" * 70)
    print("  T17 Walk-Forward Backtest — Synthetic Test")
    print("=" * 70)

    np.random.seed(42)

    # 生成模拟数据 (3年, 月度)
    n = 3000
    dates = pd.date_range('2021-01-01', periods=n, freq='D')
    labels = np.random.choice([0, 1, 2], size=n, p=[0.46, 0.26, 0.28])

    # 模拟预测概率 (略好于随机)
    probs = np.random.dirichlet([3, 2, 2], size=n)
    # 让预测略偏向真实标签
    for i in range(n):
        probs[i, labels[i]] += 0.15
    probs = probs / probs.sum(axis=1, keepdims=True)

    df = pd.DataFrame({
        'match_date': dates,
        'result_label': labels,
        'home_prob': probs[:, 0],
        'draw_prob': probs[:, 1],
        'away_prob': probs[:, 2],
        'league_name': np.random.choice(['Premier League', 'La Liga', 'Serie A', 'Bundesliga'], size=n),
    })

    output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               'evaluation_results', 't17_test')
    os.makedirs(output_dir, exist_ok=True)

    # --- 测试1: 季度扩展窗口 ---
    print("\n[Test 1] Quarterly expanding window...")
    result, report = run_walkforward_backtest(
        df, prob_cols=['home_prob', 'draw_prob', 'away_prob'],
        freq='quarter', window='expanding',
        output_dir=output_dir, generate_report=True
    )
    summary = result.summary()
    print(f"  Folds: {summary.get('n_folds', 0)}")
    print(f"  Overall Accuracy: {summary.get('overall_accuracy', 0):.4f}")
    print(f"  Overall Brier: {summary.get('overall_brier', 0):.4f}")
    print(f"  Overall ECE: {summary.get('overall_ece', 0):.4f}")

    # --- 测试2: 月度滑动窗口 ---
    print("\n[Test 2] Monthly sliding window...")
    result2, _ = run_walkforward_backtest(
        df, prob_cols=['home_prob', 'draw_prob', 'away_prob'],
        freq='month', window='sliding', train_size=6,
        output_dir=output_dir, generate_report=False
    )
    summary2 = result2.summary()
    print(f"  Folds: {summary2.get('n_folds', 0)}")
    print(f"  Overall Accuracy: {summary2.get('overall_accuracy', 0):.4f}")

    # --- 测试3: 退化检测 ---
    print("\n[Test 3] Degradation check...")
    alerts = result.degradation_check()
    if alerts:
        for a in alerts:
            print(f"  {a['severity']}: {a['metric']} at fold {a['label']} (delta={a['delta']:+.4f})")
    else:
        print("  No degradation detected")

    # --- 测试4: 置信区间 ---
    print("\n[Test 4] Confidence intervals...")
    for metric in ['accuracy', 'brier', 'ece']:
        mean, lower, upper = result.confidence_interval(metric)
        print(f"  {metric}: {mean:.4f} [{lower:.4f}, {upper:.4f}]")

    # --- 测试5: 多策略对比 ---
    print("\n[Test 5] Multi-strategy comparison...")
    # 创建一个稍差的策略
    df['home_prob_noisy'] = df['home_prob'] * 0.8 + 0.1
    df['draw_prob_noisy'] = df['draw_prob'] * 0.8 + 0.1
    df['away_prob_noisy'] = df['away_prob'] * 0.8 + 0.1
    # 归一化
    row_sums = df[['home_prob_noisy', 'draw_prob_noisy', 'away_prob_noisy']].sum(axis=1)
    df['home_prob_noisy'] /= row_sums
    df['draw_prob_noisy'] /= row_sums
    df['away_prob_noisy'] /= row_sums

    strategies = {
        'Original': ['home_prob', 'draw_prob', 'away_prob'],
        'Noisy': ['home_prob_noisy', 'draw_prob_noisy', 'away_prob_noisy'],
    }
    multi_results, _ = run_multi_strategy_comparison(
        df, strategies, freq='quarter', output_dir=output_dir
    )
    for name, r in multi_results.items():
        s = r.summary()
        print(f"  {name}: Acc={s.get('overall_accuracy', 0):.4f}, Brier={s.get('overall_brier', 0):.4f}")

    # --- 测试6: 折级别 DataFrame ---
    print("\n[Test 6] Fold DataFrame...")
    fold_df = result.to_dataframe()
    print(fold_df.to_string(index=False))

    print(f"\n{'=' * 70}")
    print(f"  T17 Test Complete! Output: {output_dir}")
    print(f"{'=' * 70}")

"""
哨响AI - 校准可视化与ECE监控 (T16)
=====================================
实现概率校准的可靠性图可视化、ECE 追踪监控、多方法对比图表，
以及 HTML 评估报告生成。

核心组件:
  1. CalibrationVisualizer — 校准可视化工具
     - reliability_diagram: 可靠性图 (per-class + overall)
     - before_after_comparison: 校准前后对比图
     - multi_method_comparison: 多方法对比柱状图
     - confidence_histogram: 置信度分布直方图
     - class_wise_reliability: 分类别可靠性图
  2. ECEMonitor — ECE 时间序列监控
     - track: 记录评估点
     - trend_chart: 趋势图
     - alert_check: 异常检测
  3. CalibrationReportBuilder — HTML 评估报告生成
     - 从 CalibratorSuite / ExpertCalibrator 生成完整报告
     - 嵌入图表 + 表格 + 建议

依赖:
  - matplotlib (Agg 后端, 无 GUI)
  - T15 calibration.py 的 compute_ece / compute_reliability / CalibratorSuite

用法:
    from optimize.calibration_viz import CalibrationVisualizer
    viz = CalibrationVisualizer(output_dir='reports/calibration')
    viz.reliability_diagram(y_true, raw_probs, calibrated_probs)
    viz.before_after_comparison(suite)
    viz.generate_html_report(suite)
"""

import logging
import os
import json
import warnings
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, field
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# matplotlib Agg 后端 (无 GUI)
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from matplotlib.gridspec import GridSpec

# 中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'DejaVu Sans', 'Arial']
plt.rcParams['axes.unicode_minus'] = False

# T15 校准模块
from optimize.calibration import (
    compute_ece, compute_mce, compute_reliability,
    multiclass_brier, CalibratorSuite, CalibrationReport,
    CALIBRATOR_REGISTRY, CALIBRATOR_DESCRIPTIONS,
)

logger = logging.getLogger(__name__)

# 赔率类别标签
CLASS_LABELS = ['Home', 'Draw', 'Away']
CLASS_COLORS = ['#2196F3', '#FF9800', '#4CAF50']  # 蓝/橙/绿
CLASS_COLORS_CN = ['#1565C0', '#E65100', '#2E7D32']

# ════════════════════════════════════════════════════════════════
# 可靠性图 — 核心可视化
# ════════════════════════════════════════════════════════════════

class CalibrationVisualizer:
    """
    校准可视化工具 — 生成可靠性图、ECE 监控、对比图

    所有图表保存为 PNG, 返回文件路径。
    """

    def __init__(self, output_dir: str = None, dpi: int = 150,
                 style: str = 'seaborn-v0_8-whitegrid'):
        """
        Args:
            output_dir: 图表输出目录
            dpi: 图像分辨率
            style: matplotlib 样式
        """
        if output_dir is None:
            output_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'reports', 'calibration'
            )
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.dpi = dpi

        # 尝试设置样式
        try:
            plt.style.use(style)
        except (Exception):
            pass

    # ─── 1. 可靠性图 (Reliability Diagram) ───

    def reliability_diagram(
        self,
        y_true: np.ndarray,
        probs: np.ndarray,
        probs_calibrated: np.ndarray = None,
        n_bins: int = 10,
        title: str = None,
        filename: str = None,
    ) -> str:
        """
        生成可靠性图 (含校准前后对比)

        Args:
            y_true: (N,) 真实标签
            probs: (N, C) 原始概率
            probs_calibrated: (N, C) 校准后概率 (None则只画原始)
            n_bins: 分箱数
            title: 图表标题
            filename: 输出文件名

        Returns:
            PNG 文件路径
        """
        if filename is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"reliability_{ts}.png"

        fig, axes = plt.subplots(1, 2 if probs_calibrated is not None else 1,
                                  figsize=(14, 6) if probs_calibrated is not None else (7, 6))
        if probs_calibrated is None:
            axes = [axes]

        # 左图: 原始概率
        self._draw_reliability_single(axes[0], y_true, probs, n_bins,
                                       title='Raw Probability' if title is None else f'{title} (Raw)',
                                       color='#E53935')

        # 右图: 校准后概率
        if probs_calibrated is not None:
            self._draw_reliability_single(axes[1], y_true, probs_calibrated, n_bins,
                                           title='Calibrated' if title is None else f'{title} (Calibrated)',
                                           color='#1E88E5')

        fig.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"可靠性图已保存: {path}")
        return path

    def _draw_reliability_single(self, ax, y_true, probs, n_bins,
                                   title: str, color: str):
        """绘制单个可靠性图"""
        reliability = compute_reliability(probs, y_true, n_bins)
        ece = compute_ece(probs, y_true, n_bins)

        # 完美校准线
        ax.plot([0, 1], [0, 1], 'k--', alpha=0.5, linewidth=1, label='Perfect Calibration')

        if not reliability:
            ax.set_title(f'{title}\n(无数据)')
            return

        # 提取数据
        confidences = [r['confidence'] for r in reliability]
        accuracies = [r['accuracy'] for r in reliability]
        counts = [r['count'] for r in reliability]
        bin_centers = [(r['bin_lower'] + r['bin_upper']) / 2 for r in reliability]

        # 柱状图 (gap)
        gaps = [r['gap'] for r in reliability]
        bar_colors = [('#4CAF50' if g >= 0 else '#F44336') for g in gaps]
        ax.bar(bin_centers, gaps, width=0.08, color=bar_colors, alpha=0.4,
               edgecolor='none', label='Overconfident(R)/Underconfident(G)')

        # 可靠性曲线
        ax.plot(confidences, accuracies, 'o-', color=color, markersize=6,
                linewidth=2, label=f'Calibration (ECE={ece:.4f})')

        # 样本量标注
        for i, (conf, acc, cnt) in enumerate(zip(confidences, accuracies, counts)):
            if cnt > 0:
                ax.annotate(f'n={cnt}', (conf, acc), fontsize=7,
                           textcoords="offset points", xytext=(0, 8),
                           ha='center', alpha=0.6)

        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_xlabel('Confidence')
        ax.set_ylabel('Accuracy')
        ax.set_title(title)
        ax.legend(loc='lower right', fontsize=8)
        ax.set_aspect('equal')
        ax.grid(True, alpha=0.3)

    # ─── 2. 校准前后对比图 ───

    def before_after_comparison(
        self,
        suite: CalibratorSuite,
        filename: str = None,
    ) -> str:
        """
        多方法校准前后 ECE/Brier 对比柱状图

        Args:
            suite: 已训练的 CalibratorSuite
            filename: 输出文件名

        Returns:
            PNG 文件路径
        """
        if not suite._reports:
            logger.warning("CalibratorSuite 无报告数据, 跳过对比图")
            return ''

        if filename is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"before_after_{ts}.png"

        methods = list(suite._reports.keys())
        n_methods = len(methods)

        fig, axes = plt.subplots(1, 3, figsize=(18, 6))

        metrics = [
            ('ECE', 'ece_before', 'ece_after', '#E53935', '#1E88E5'),
            ('Brier Score', 'brier_before', 'brier_after', '#FF6F00', '#00897B'),
            ('Log Loss', 'log_loss_before', 'log_loss_after', '#6A1B9A', '#00838F'),
        ]

        for ax, (metric_name, before_key, after_key, c_before, c_after) in zip(axes, metrics):
            before_vals = [getattr(suite._reports[m], before_key, 0) for m in methods]
            after_vals = [getattr(suite._reports[m], after_key, 0) for m in methods]

            x = np.arange(n_methods)
            width = 0.35

            bars1 = ax.bar(x - width / 2, before_vals, width, label='Before',
                           color=c_before, alpha=0.8, edgecolor='white')
            bars2 = ax.bar(x + width / 2, after_vals, width, label='After',
                           color=c_after, alpha=0.8, edgecolor='white')

            # 标注 delta
            for i, (b, a) in enumerate(zip(before_vals, after_vals)):
                delta = a - b
                sign = '+' if delta > 0 else ''
                ax.annotate(f'{sign}{delta:.4f}', (i, max(b, a)),
                           fontsize=8, ha='center', va='bottom',
                           color='green' if delta < 0 else 'red')

            ax.set_xlabel('Method')
            ax.set_ylabel(metric_name)
            ax.set_title(f'{metric_name} Before vs After')
            ax.set_xticks(x)
            ax.set_xticklabels(methods, rotation=30, ha='right')
            ax.legend(fontsize=8)
            ax.grid(True, alpha=0.3, axis='y')

        fig.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"校准前后对比图已保存: {path}")
        return path

    # ─── 3. 多方法对比排名图 ───

    def multi_method_comparison(
        self,
        suite: CalibratorSuite,
        filename: str = None,
    ) -> str:
        """
        多方法雷达图 + 排名柱状图

        Args:
            suite: 已训练的 CalibratorSuite
            filename: 输出文件名

        Returns:
            PNG 文件路径
        """
        if not suite._reports:
            return ''

        if filename is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"method_comparison_{ts}.png"

        methods = list(suite._reports.keys())
        n_methods = len(methods)

        fig = plt.figure(figsize=(16, 7))
        gs = GridSpec(1, 2, width_ratios=[1, 1.2])

        # 左: 改进量柱状图
        ax1 = fig.add_subplot(gs[0])
        ece_deltas = [suite._reports[m].ece_delta for m in methods]
        brier_deltas = [suite._reports[m].brier_delta for m in methods]
        ll_deltas = [suite._reports[m].ll_delta for m in methods]

        x = np.arange(n_methods)
        width = 0.25
        ax1.bar(x - width, ece_deltas, width, label='ECE Δ', color='#1E88E5', alpha=0.8)
        ax1.bar(x, brier_deltas, width, label='Brier Δ', color='#43A047', alpha=0.8)
        ax1.bar(x + width, ll_deltas, width, label='Log Loss Δ', color='#FB8C00', alpha=0.8)
        ax1.axhline(y=0, color='k', linewidth=0.5)
        ax1.set_xticks(x)
        ax1.set_xticklabels(methods, rotation=30, ha='right')
        ax1.set_ylabel('Improvement (positive=better)')
        ax1.set_title('Calibration Improvement')
        ax1.legend(fontsize=8)
        ax1.grid(True, alpha=0.3, axis='y')

        # 右: 雷达图
        ax2 = fig.add_subplot(gs[1], polar=True)
        categories = ['ECE Gain', 'Brier Gain', 'LL Gain', 'ECE Abs', 'Brier Abs']
        n_cats = len(categories)
        angles = np.linspace(0, 2 * np.pi, n_cats, endpoint=False).tolist()
        angles += angles[:1]

        # 归一化到 [0, 1]
        for i, m in enumerate(methods):
            r = suite._reports[m]
            # 改进量归一化 (0~1, 越大越好)
            ece_d_norm = max(0, min(1, r.ece_delta * 10 + 0.5))
            brier_d_norm = max(0, min(1, r.brier_delta * 50 + 0.5))
            ll_d_norm = max(0, min(1, r.ll_delta * 10 + 0.5))
            # 绝对值归一化 (越小越好 → 反转)
            ece_abs_norm = max(0, min(1, 1 - r.ece_after * 5))
            brier_abs_norm = max(0, min(1, 1 - r.brier_after))

            values = [ece_d_norm, brier_d_norm, ll_d_norm, ece_abs_norm, brier_abs_norm]
            values += values[:1]
            ax2.plot(angles, values, 'o-', linewidth=2, markersize=4,
                     label=m, color=CLASS_COLORS[i % 3], alpha=0.8)
            ax2.fill(angles, values, alpha=0.1, color=CLASS_COLORS[i % 3])

        ax2.set_xticks(angles[:-1])
        ax2.set_xticklabels(categories, fontsize=8)
        ax2.set_title('Multi-Method Evaluation', pad=20)
        ax2.legend(loc='upper right', bbox_to_anchor=(1.3, 1.1), fontsize=8)

        fig.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"多方法对比图已保存: {path}")
        return path

    # ─── 4. 置信度分布直方图 ───

    def confidence_histogram(
        self,
        y_true: np.ndarray,
        probs: np.ndarray,
        probs_calibrated: np.ndarray = None,
        n_bins: int = 15,
        title: str = 'Confidence Distribution',
        filename: str = None,
    ) -> str:
        """
        预测置信度分布直方图 (校准前后对比)

        Args:
            y_true: (N,) 真实标签
            probs: (N, C) 原始概率
            probs_calibrated: (N, C) 校准后概率
            n_bins: 分箱数
            title: 标题
            filename: 输出文件名

        Returns:
            PNG 文件路径
        """
        if filename is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"confidence_hist_{ts}.png"

        fig, ax = plt.subplots(figsize=(10, 6))

        # 原始置信度
        conf_raw = np.max(probs, axis=1)
        ax.hist(conf_raw, bins=n_bins, alpha=0.5, color='#E53935',
                label='Raw', edgecolor='white', density=True)

        # 校准后置信度
        if probs_calibrated is not None:
            conf_cal = np.max(probs_calibrated, axis=1)
            ax.hist(conf_cal, bins=n_bins, alpha=0.5, color='#1E88E5',
                    label='Calibrated', edgecolor='white', density=True)

        # 标注
        ax.axvline(x=conf_raw.mean(), color='#E53935', linestyle='--', alpha=0.7)
        ax.annotate(f'Raw mean: {conf_raw.mean():.3f}',
                    xy=(conf_raw.mean(), ax.get_ylim()[1] * 0.9),
                    fontsize=9, color='#E53935')

        if probs_calibrated is not None:
            ax.axvline(x=conf_cal.mean(), color='#1E88E5', linestyle='--', alpha=0.7)
            ax.annotate(f'Cal. mean: {conf_cal.mean():.3f}',
                        xy=(conf_cal.mean(), ax.get_ylim()[1] * 0.8),
                        fontsize=9, color='#1E88E5')

        ax.set_xlabel('Confidence (Max Probability)')
        ax.set_ylabel('Density')
        ax.set_title(title)
        ax.legend()
        ax.grid(True, alpha=0.3)

        fig.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"置信度分布图已保存: {path}")
        return path

    # ─── 5. 分类别可靠性图 ───

    def class_wise_reliability(
        self,
        y_true: np.ndarray,
        probs: np.ndarray,
        n_bins: int = 10,
        title: str = 'Class-wise Reliability',
        filename: str = None,
    ) -> str:
        """
        分类 (Home/Draw/Away) 独立可靠性图

        对每个类别, 计算 P(y=c|x) 的校准质量

        Args:
            y_true: (N,) 真实标签
            probs: (N, C) 概率
            n_bins: 分箱数
            title: 标题
            filename: 输出文件名

        Returns:
            PNG 文件路径
        """
        if filename is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"class_reliability_{ts}.png"

        n_classes = probs.shape[1]
        fig, axes = plt.subplots(1, n_classes, figsize=(7 * n_classes, 6))

        for cls_idx in range(n_classes):
            ax = axes[cls_idx] if n_classes > 1 else axes
            y_binary = (y_true == cls_idx).astype(int)
            p_binary = probs[:, cls_idx]

            # 二分类可靠性
            reliability = self._binary_reliability(p_binary, y_binary, n_bins)
            ece = self._binary_ece(p_binary, y_binary, n_bins)

            # 完美线
            ax.plot([0, 1], [0, 1], 'k--', alpha=0.5, linewidth=1)

            if reliability:
                confs = [r['confidence'] for r in reliability]
                accs = [r['accuracy'] for r in reliability]
                gaps = [r['gap'] for r in reliability]
                bin_centers = [(r['bin_lower'] + r['bin_upper']) / 2 for r in reliability]

                # gap 柱状图
                bar_colors = ['#4CAF50' if g >= 0 else '#F44336' for g in gaps]
                ax.bar(bin_centers, gaps, width=0.08, color=bar_colors, alpha=0.4)

                # 曲线
                ax.plot(confs, accs, 'o-', color=CLASS_COLORS[cls_idx],
                        markersize=6, linewidth=2,
                        label=f'{CLASS_LABELS[cls_idx]} (ECE={ece:.4f})')

            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            ax.set_xlabel(f'P({CLASS_LABELS[cls_idx]})')
            ax.set_ylabel('Empirical Frequency')
            ax.set_title(f'{CLASS_LABELS[cls_idx]} Reliability')
            ax.legend(loc='lower right', fontsize=8)
            ax.set_aspect('equal')
            ax.grid(True, alpha=0.3)

        fig.suptitle(title, fontsize=14, fontweight='bold')
        fig.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"分类别可靠性图已保存: {path}")
        return path

    # ─── 6. 稀疏数据适应性图 ───

    def sparse_data_chart(
        self,
        sparse_df: pd.DataFrame,
        filename: str = None,
    ) -> str:
        """
        稀疏数据适应性测试可视化

        Args:
            sparse_df: CalibratorSuite.sparse_data_test() 返回的 DataFrame
            filename: 输出文件名

        Returns:
            PNG 文件路径
        """
        if sparse_df.empty:
            return ''

        if filename is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"sparse_data_{ts}.png"

        fig, axes = plt.subplots(1, 2, figsize=(14, 6))

        methods = sparse_df['method'].unique()
        colors = {m: CLASS_COLORS[i % 3] for i, m in enumerate(methods)}

        # 左: ECE vs 样本量
        for m in methods:
            sub = sparse_df[sparse_df['method'] == m].sort_values('n_samples')
            axes[0].plot(sub['n_samples'], sub['ece_cal'], 'o-', color=colors[m],
                         label=m, linewidth=2, markersize=5)

        axes[0].set_xlabel('Training Samples')
        axes[0].set_ylabel('Calibrated ECE')
        axes[0].set_title('ECE vs Sample Size')
        axes[0].legend(fontsize=8)
        axes[0].grid(True, alpha=0.3)
        axes[0].set_xscale('log')

        # 右: Brier vs 样本量
        for m in methods:
            sub = sparse_df[sparse_df['method'] == m].sort_values('n_samples')
            axes[1].plot(sub['n_samples'], sub['brier_cal'], 'o-', color=colors[m],
                         label=m, linewidth=2, markersize=5)

        axes[1].set_xlabel('Training Samples')
        axes[1].set_ylabel('Calibrated Brier Score')
        axes[1].set_title('Brier Score vs Sample Size')
        axes[1].legend(fontsize=8)
        axes[1].grid(True, alpha=0.3)
        axes[1].set_xscale('log')

        fig.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"稀疏数据适应性图已保存: {path}")
        return path

    # ─── 7. 一键全图 ───

    def generate_all_charts(
        self,
        y_true: np.ndarray,
        raw_probs: np.ndarray,
        suite: CalibratorSuite,
        n_bins: int = 10,
    ) -> Dict[str, str]:
        """
        生成所有校准可视化图表

        Args:
            y_true: 真实标签
            raw_probs: 原始概率
            suite: 已训练的 CalibratorSuite
            n_bins: 分箱数

        Returns:
            {chart_name: file_path}
        """
        charts = {}

        # 获取校准后概率
        best_method = suite.best_method()
        calibrated_probs = suite.predict(raw_probs, method=best_method)

        # 1. 可靠性图 (校准前后对比)
        path = self.reliability_diagram(y_true, raw_probs, calibrated_probs, n_bins)
        charts['reliability'] = path

        # 2. 校准前后指标对比
        path = self.before_after_comparison(suite)
        if path:
            charts['before_after'] = path

        # 3. 多方法对比
        path = self.multi_method_comparison(suite)
        if path:
            charts['method_comparison'] = path

        # 4. 置信度分布
        path = self.confidence_histogram(y_true, raw_probs, calibrated_probs)
        if path:
            charts['confidence_hist'] = path

        # 5. 分类别可靠性
        path = self.class_wise_reliability(y_true, raw_probs, n_bins)
        if path:
            charts['class_reliability'] = path

        # 6. 稀疏数据适应性
        try:
            sparse_df = suite.sparse_data_test()
            path = self.sparse_data_chart(sparse_df)
            if path:
                charts['sparse_data'] = path
        except (Exception, KeyError, IndexError) as e:
            logger.warning(f"稀疏数据图生成失败: {e}")

        logger.info(f"校准可视化完成: {len(charts)} 张图表")
        return charts

    # ─── 辅助方法 ───

    @staticmethod
    def _binary_reliability(probs: np.ndarray, labels: np.ndarray,
                              n_bins: int = 10) -> List[Dict]:
        """二分类可靠性数据"""
        n_samples = len(labels)
        if n_samples == 0:
            return []

        predictions = (probs > 0.5).astype(int)
        correct = (predictions == labels).astype(float)

        bin_boundaries = np.linspace(0.0, 1.0, n_bins + 1)
        reliability = []
        for i in range(n_bins):
            mask = (probs > bin_boundaries[i]) & (probs <= bin_boundaries[i + 1])
            count = mask.sum()
            if count > 0:
                reliability.append({
                    'bin_lower': float(bin_boundaries[i]),
                    'bin_upper': float(bin_boundaries[i + 1]),
                    'count': int(count),
                    'accuracy': float(correct[mask].mean()),
                    'confidence': float(probs[mask].mean()),
                    'gap': float(correct[mask].mean() - probs[mask].mean()),
                })
        return reliability

    @staticmethod
    def _binary_ece(probs: np.ndarray, labels: np.ndarray,
                     n_bins: int = 10) -> float:
        """二分类 ECE"""
        n_samples = len(labels)
        if n_samples == 0:
            return 0.0

        predictions = (probs > 0.5).astype(int)
        correct = (predictions == labels).astype(float)

        bin_boundaries = np.linspace(0.0, 1.0, n_bins + 1)
        ece = 0.0
        for i in range(n_bins):
            mask = (probs > bin_boundaries[i]) & (probs <= bin_boundaries[i + 1])
            if mask.sum() > 0:
                acc = correct[mask].mean()
                conf = probs[mask].mean()
                ece += mask.sum() / n_samples * abs(acc - conf)
        return float(ece)

# ════════════════════════════════════════════════════════════════
# ECE 监控器 — 时间序列追踪
# ════════════════════════════════════════════════════════════════

@dataclass
class ECETrackPoint:
    """ECE 追踪点"""
    timestamp: str
    ece: float
    mce: float
    brier: float
    log_loss: float
    n_samples: int
    method: str = ''
    label: str = ''  # 可选标注 (如 "retrained", "data_update")

class ECEMonitor:
    """
    ECE 时间序列监控器

    用法:
        monitor = ECEMonitor()
        monitor.track(y_true, probs, method='platt')
        monitor.track(y_true, probs, method='isotonic')
        monitor.trend_chart()
        monitor.alert_check()
    """

    def __init__(self, output_dir: str = None,
                 alert_threshold: float = 0.08,
                 degradation_threshold: float = 0.02):
        """
        Args:
            output_dir: 图表输出目录
            alert_threshold: ECE 告警阈值
            degradation_threshold: 退化检测阈值 (相邻点 ECE 增量)
        """
        if output_dir is None:
            output_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'reports', 'calibration'
            )
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.alert_threshold = alert_threshold
        self.degradation_threshold = degradation_threshold
        self.history: List[ECETrackPoint] = []

    def track(
        self,
        y_true: np.ndarray,
        probs: np.ndarray,
        method: str = '',
        label: str = '',
        n_bins: int = 10,
    ) -> ECETrackPoint:
        """
        记录一个评估点

        Args:
            y_true: 真实标签
            probs: 概率 (原始或校准后)
            method: 校准方法名
            label: 可选标注
            n_bins: ECE 分箱数

        Returns:
            ECETrackPoint
        """
        point = ECETrackPoint(
            timestamp=datetime.now(timezone.utc).isoformat(),
            ece=compute_ece(probs, y_true, n_bins),
            mce=compute_mce(probs, y_true, n_bins),
            brier=multiclass_brier(probs, y_true, probs.shape[1]),
            log_loss=float(
                __import__('sklearn.metrics', fromlist=['log_loss'])
                .log_loss(y_true, probs, labels=list(range(probs.shape[1])))
            ),
            n_samples=len(y_true),
            method=method,
            label=label,
        )
        self.history.append(point)
        logger.info(f"ECE Track: method={method}, ECE={point.ece:.4f}, "
                     f"Brier={point.brier:.4f}, label={label}")
        return point

    def track_from_report(self, report: CalibrationReport, method: str = '') -> ECETrackPoint:
        """从 CalibrationReport 记录"""
        point = ECETrackPoint(
            timestamp=datetime.now(timezone.utc).isoformat(),
            ece=report.ece_after,
            mce=report.mce_after,
            brier=report.brier_after,
            log_loss=report.log_loss_after,
            n_samples=report.n_samples,
            method=method or report.method,
        )
        self.history.append(point)
        return point

    def trend_chart(self, metric: str = 'ece', filename: str = None) -> str:
        """
        生成 ECE/Brier/LogLoss 趋势图

        Args:
            metric: 'ece' | 'brier' | 'log_loss'
            filename: 输出文件名

        Returns:
            PNG 文件路径
        """
        if len(self.history) < 2:
            logger.warning("追踪点不足 2 个, 无法生成趋势图")
            return ''

        if filename is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"ece_trend_{ts}.png"

        fig, ax = plt.subplots(figsize=(12, 6))

        # 按方法分组
        by_method = defaultdict(list)
        for p in self.history:
            by_method[p.method].append(p)

        for i, (method, points) in enumerate(by_method.items()):
            timestamps = range(len(points))
            values = [getattr(p, metric) for p in points]
            ax.plot(timestamps, values, 'o-', color=CLASS_COLORS[i % 3],
                    label=method or 'default', linewidth=2, markersize=6)

            # 标注标签
            for j, p in enumerate(points):
                if p.label:
                    ax.annotate(p.label, (j, getattr(p, metric)),
                               fontsize=7, textcoords="offset points",
                               xytext=(0, 10), ha='center', alpha=0.7)

        # 告警线
        if metric == 'ece':
            ax.axhline(y=self.alert_threshold, color='red', linestyle='--',
                       alpha=0.5, label=f'Alert threshold ({self.alert_threshold})')

        ax.set_xlabel('Evaluation #')
        ax.set_ylabel(metric.upper())
        ax.set_title(f'{metric.upper()} Trend Monitor')
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

        fig.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=self.dpi if hasattr(self, 'dpi') else 150,
                    bbox_inches='tight')
        plt.close(fig)
        logger.info(f"ECE 趋势图已保存: {path}")
        return path

    def alert_check(self) -> List[Dict]:
        """
        检查异常: ECE 超阈值 / 退化

        Returns:
            告警列表
        """
        alerts = []

        for p in self.history:
            if p.ece > self.alert_threshold:
                alerts.append({
                    'level': 'WARNING',
                    'type': 'ece_exceeded',
                    'message': f'ECE={p.ece:.4f} 超过阈值 {self.alert_threshold}',
                    'timestamp': p.timestamp,
                    'method': p.method,
                })

        # 退化检测 (同方法相邻点)
        by_method = defaultdict(list)
        for p in self.history:
            by_method[p.method].append(p)

        for method, points in by_method.items():
            for i in range(1, len(points)):
                delta = points[i].ece - points[i - 1].ece
                if delta > self.degradation_threshold:
                    alerts.append({
                        'level': 'CRITICAL',
                        'type': 'ece_degradation',
                        'message': (f'{method}: ECE degradation {delta:+.4f} '
                                    f'({points[i-1].ece:.4f} -> {points[i].ece:.4f})'),
                        'timestamp': points[i].timestamp,
                        'method': method,
                    })

        if alerts:
            logger.warning(f"校准告警: {len(alerts)} 个")
        return alerts

    def save_history(self, path: str = None):
        """保存追踪历史为 JSON"""
        if path is None:
            path = os.path.join(self.output_dir, 'ece_monitor_history.json')

        data = [{
            'timestamp': p.timestamp,
            'ece': p.ece,
            'mce': p.mce,
            'brier': p.brier,
            'log_loss': p.log_loss,
            'n_samples': p.n_samples,
            'method': p.method,
            'label': p.label,
        } for p in self.history]

        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"ECE 监控历史已保存: {path}")

    def load_history(self, path: str = None):
        """加载追踪历史"""
        if path is None:
            path = os.path.join(self.output_dir, 'ece_monitor_history.json')

        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        self.history = []
        for d in data:
            self.history.append(ECETrackPoint(**d))
        logger.info(f"ECE 监控历史已加载: {len(self.history)} 个追踪点")

# ════════════════════════════════════════════════════════════════
# HTML 评估报告生成器
# ════════════════════════════════════════════════════════════════

class CalibrationReportBuilder:
    """
    校准评估 HTML 报告生成器

    用法:
        builder = CalibrationReportBuilder(output_dir='reports/calibration')
        builder.generate(suite, y_true, raw_probs)
    """

    def __init__(self, output_dir: str = None, viz: CalibrationVisualizer = None):
        if output_dir is None:
            output_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'reports', 'calibration'
            )
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.viz = viz or CalibrationVisualizer(output_dir)

    def generate(
        self,
        suite: CalibratorSuite,
        y_true: np.ndarray = None,
        raw_probs: np.ndarray = None,
        title: str = '概率校准评估报告',
    ) -> str:
        """
        生成完整 HTML 评估报告

        Args:
            suite: 已训练的 CalibratorSuite
            y_true: 测试集标签 (可选, 用于生成图表)
            raw_probs: 测试集原始概率 (可选)
            title: 报告标题

        Returns:
            HTML 文件路径
        """
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        html_filename = f"calibration_report_{ts}.html"

        # 生成图表
        chart_paths = {}
        if y_true is not None and raw_probs is not None:
            chart_paths = self.viz.generate_all_charts(y_true, raw_probs, suite)

        # 对比表
        compare_df = suite.compare()

        # 构建 HTML
        html = self._build_html(title, chart_paths, compare_df, suite)
        path = os.path.join(self.output_dir, html_filename)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html)

        logger.info(f"校准评估报告已生成: {path}")
        return path

    def generate_from_data(
        self,
        y_true: np.ndarray,
        raw_probs: np.ndarray,
        methods: List[str] = None,
        title: str = '概率校准评估报告',
    ) -> str:
        """
        从原始数据一键生成报告 (含训练 + 可视化)

        Args:
            y_true: 真实标签
            raw_probs: 原始概率
            methods: 校准方法列表
            title: 报告标题

        Returns:
            HTML 文件路径
        """
        # 训练
        suite = CalibratorSuite(methods=methods)
        suite.fit(y_true, raw_probs)

        # 生成报告
        return self.generate(suite, y_true, raw_probs, title)

    def _build_html(self, title: str, chart_paths: Dict[str, str],
                     compare_df: pd.DataFrame, suite: CalibratorSuite) -> str:
        """构建 HTML 内容"""
        # 图表转 base64 嵌入 (自包含 HTML)
        chart_tags = {}
        for name, path in chart_paths.items():
            if path and os.path.exists(path):
                import base64
                with open(path, 'rb') as f:
                    img_data = base64.b64encode(f.read()).decode()
                chart_tags[name] = f'<img src="data:image/png;base64,{img_data}" style="max-width:100%;margin:10px 0">'

        # 指标表格
        table_html = ''
        if not compare_df.empty:
            table_html = compare_df.to_html(index=False, classes='table table-striped',
                                              float_format='%.4f')

        # ECE 监控摘要
        best_method = suite.best_method()
        best_report = suite._reports.get(best_method)

        # 告警
        alerts_html = ''
        if best_report:
            if best_report.ece_after > 0.08:
                alerts_html += '<div class="alert alert-danger">ECE 过高 ({:.4f}), 需要改进校准方法</div>'.format(best_report.ece_after)
            elif best_report.ece_after > 0.05:
                alerts_html += '<div class="alert alert-warning">ECE 偏高 ({:.4f}), 建议优化校准参数</div>'.format(best_report.ece_after)
            else:
                alerts_html += '<div class="alert alert-success">ECE 良好 ({:.4f}), 校准质量达标</div>'.format(best_report.ece_after)

        # 可靠性数据表
        reliability_html = ''
        if best_report and best_report.reliability_after:
            rel_df = pd.DataFrame(best_report.reliability_after)
            reliability_html = rel_df.to_html(index=False, classes='table table-sm')

        # 预计算指标值 (避免 f-string 条件格式化问题)
        val_ece = f'{best_report.ece_after:.4f}' if best_report else 'N/A'
        val_brier = f'{best_report.brier_after:.4f}' if best_report else 'N/A'
        val_samples = str(best_report.n_samples) if best_report else 'N/A'
        val_methods = str(len(suite._reports))
        gen_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

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
        .table {{ width: 100%; border-collapse: collapse; margin: 15px 0; }}
        .table th, .table td {{ padding: 8px 12px; border: 1px solid #ddd; text-align: center; }}
        .table th {{ background: #1565C0; color: white; }}
        .table-striped tr:nth-child(even) {{ background: #f5f5f5; }}
        .table-sm th, .table-sm td {{ padding: 4px 8px; font-size: 12px; }}
        .alert {{ padding: 12px 20px; border-radius: 4px; margin: 10px 0; }}
        .alert-success {{ background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
        .alert-warning {{ background: #fff3cd; color: #856404; border: 1px solid #ffeaa7; }}
        .alert-danger {{ background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
        .chart-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin: 20px 0; }}
        .chart-grid img {{ width: 100%; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .metric-card {{ display: inline-block; padding: 15px 25px; margin: 5px; background: #e3f2fd; border-radius: 8px; text-align: center; }}
        .metric-card .value {{ font-size: 24px; font-weight: bold; color: #1565C0; }}
        .metric-card .label {{ font-size: 12px; color: #666; }}
        .footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid #ddd; color: #999; font-size: 12px; }}
    </style>
</head>
<body>
<div class="container">
    <h1>{title}</h1>
    <p>生成时间: {gen_time}</p>

    {alerts_html}

    <h2>核心指标</h2>
    <div>
        <div class="metric-card">
            <div class="value">{val_ece}</div>
            <div class="label">最优 ECE ({best_method})</div>
        </div>
        <div class="metric-card">
            <div class="value">{val_brier}</div>
            <div class="label">最优 Brier</div>
        </div>
        <div class="metric-card">
            <div class="value">{val_samples}</div>
            <div class="label">样本量</div>
        </div>
        <div class="metric-card">
            <div class="value">{val_methods}</div>
            <div class="label">对比方法数</div>
        </div>
    </div>

    <h2>可靠性图</h2>
    {chart_tags.get('reliability', '<p>未生成</p>')}

    <h2>校准前后对比</h2>
    {chart_tags.get('before_after', '<p>未生成</p>')}

    <h2>分类别可靠性</h2>
    {chart_tags.get('class_reliability', '<p>未生成</p>')}

    <div class="chart-grid">
        <div>
            <h3>多方法对比</h3>
            {chart_tags.get('method_comparison', '<p>未生成</p>')}
        </div>
        <div>
            <h3>置信度分布</h3>
            {chart_tags.get('confidence_hist', '<p>未生成</p>')}
        </div>
    </div>

    <h2>方法对比详情</h2>
    {table_html}

    <h2>可靠性分箱数据 (最优方法)</h2>
    {reliability_html}

    <div class="chart-grid">
        <div>
            <h3>稀疏数据适应性</h3>
            {chart_tags.get('sparse_data', '<p>未生成</p>')}
        </div>
    </div>

    <div class="footer">
        <p>哨响AI — 概率校准评估报告 (T16) | 自动生成于 {gen_time[:10]}</p>
    </div>
</div>
</body>
</html>"""
        return html

# ════════════════════════════════════════════════════════════════
# 与 ExpertCalibrator 的桥接
# ════════════════════════════════════════════════════════════════

def visualize_expert_calibration(
    expert_name: str,
    db_path: str = None,
    output_dir: str = None,
) -> Dict[str, str]:
    """
    为指定专家生成校准可视化

    Args:
        expert_name: 专家名
        db_path: 数据库路径
        output_dir: 输出目录

    Returns:
        {chart_name: file_path}
    """
    from agents.expert_calibrator import ExpertCalibrator

    ec = ExpertCalibrator(expert_name)
    db = db_path or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'football_data.db'
    )
    n = ec.collect_predictions(db)

    if n < 100:
        logger.warning(f"样本不足: {n}")
        return {'error': f'样本不足: {n}'}

    # 使用 CalibratorSuite 做全方法对比
    suite = CalibratorSuite()
    suite.fit(ec.y_true, ec.X_raw)

    # 生成可视化
    viz = CalibrationVisualizer(output_dir=output_dir)
    charts = viz.generate_all_charts(ec.y_true, ec.X_raw, suite)

    # ECE 监控
    monitor = ECEMonitor(output_dir=output_dir)
    for method, report in suite._reports.items():
        monitor.track_from_report(report, method=method)
    monitor.save_history()

    # 生成 HTML 报告
    builder = CalibrationReportBuilder(output_dir=output_dir, viz=viz)
    html_path = builder.generate(suite, ec.y_true, ec.X_raw,
                                  title=f'{expert_name} 校准评估报告')
    charts['html_report'] = html_path

    return charts

# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    print("=" * 60)
    print("T16 校准可视化与ECE监控 — 模拟数据测试")
    print("=" * 60)

    # 模拟数据
    np.random.seed(42)
    n = 2000
    y_true = np.random.choice(3, n, p=[0.45, 0.28, 0.27])

    raw_probs = np.zeros((n, 3))
    for i in range(n):
        if y_true[i] == 0:
            raw_probs[i] = np.random.dirichlet([5, 2, 2])
        elif y_true[i] == 1:
            raw_probs[i] = np.random.dirichlet([4, 3, 2])
        else:
            raw_probs[i] = np.random.dirichlet([3, 2, 4])

    raw_probs[:, 0] += 0.08
    raw_probs[:, 1] -= 0.05
    raw_probs = np.clip(raw_probs, 0.01, 0.98)
    raw_probs = raw_probs / raw_probs.sum(axis=1, keepdims=True)

    # 训练校准器
    suite = CalibratorSuite()
    suite.fit(y_true, raw_probs)

    # 1. 生成所有图表
    print("\n[1] 生成校准可视化图表...")
    viz = CalibrationVisualizer()
    charts = viz.generate_all_charts(y_true, raw_probs, suite)
    for name, path in charts.items():
        print(f"  {name}: {path}")

    # 2. ECE 监控
    print("\n[2] ECE 监控...")
    monitor = ECEMonitor()
    for method, report in suite._reports.items():
        monitor.track_from_report(report, method=method)

    # 模拟退化
    monitor.track(y_true, raw_probs, method='degradation_test', label='模拟退化')
    trend_path = monitor.trend_chart()
    print(f"  趋势图: {trend_path}")

    alerts = monitor.alert_check()
    print(f"  告警数: {len(alerts)}")
    for a in alerts:
        print(f"    [{a['level']}] {a['message']}")

    # 3. HTML 报告
    print("\n[3] 生成 HTML 报告...")
    builder = CalibrationReportBuilder()
    html_path = builder.generate(suite, y_true, raw_probs)
    print(f"  报告: {html_path}")

    print("\n" + "=" * 60)
    print("T16 模拟测试完成!")
    print("=" * 60)

"""
哨响AI - 联赛差异化评估模块 (T18)
===================================
按联赛划分测试集，计算各联赛专属指标，识别模型薄弱环节并给出针对性改进建议。

核心组件:
  1. LeagueMetrics — 单联赛评估指标数据类
  2. WeakSpot — 薄弱环节识别
  3. LeagueEvaluator — 联赛差异化评估器
     - 接受 BacktestResult + 原始 DataFrame (含 league_name 列)
     - 按联赛分割，计算完整指标集
     - 自动识别薄弱环节 (低于全局均值 + 阈值)
     - 生成针对性改进建议
  4. LeagueEvaluationResult — 评估结果容器
  5. LeagueVisualizer — 联赛级可视化 (6 类图表)
  6. LeagueReportBuilder — 自包含 HTML 报告

依赖:
  - numpy, pandas, matplotlib
  - T15 calibration.py (compute_ece, multiclass_brier)
  - T17 walkforward_backtest.py (BacktestResult, FoldMetrics)

用法:
    from optimize.league_evaluator import LeagueEvaluator

    evaluator = LeagueEvaluator()
    league_result = evaluator.evaluate(backtest_result, df, league_col='league_name')
    league_result.summary()
    league_result.weak_spots
    league_result.suggestions
"""

import logging
import os
import warnings
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import defaultdict
from datetime import datetime, timezone

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
from optimize.calibration import compute_ece, multiclass_brier

# T17 backtest
from optimize.walkforward_backtest import BacktestResult, FoldMetrics, CLASS_LABELS

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'evaluation_results')

# ════════════════════════════════════════════════════════════════
# 1. LeagueMetrics — 单联赛评估指标
# ════════════════════════════════════════════════════════════════

@dataclass
class LeagueMetrics:
    """单联赛评估指标"""
    league_name: str
    n_matches: int
    # 核心指标
    accuracy: float
    brier: float
    ece: float
    log_loss: float
    mcc: float
    # 分类别召回率
    home_recall: float
    draw_recall: float
    away_recall: float
    # 分类别 ECE
    ece_home: float
    ece_draw: float
    ece_away: float
    # 混淆矩阵 (3x3)
    confusion_matrix: Optional[np.ndarray] = None
    # 类别分布
    home_rate: float = 0.0
    draw_rate: float = 0.0
    away_rate: float = 0.0
    # 与全局对比
    accuracy_delta: float = 0.0   # vs 全局 accuracy
    brier_delta: float = 0.0
    ece_delta: float = 0.0
    # 元数据
    date_range: str = ''
    n_folds_covered: int = 0

    def to_dict(self) -> Dict:
        """转为字典"""
        d = {
            'league_name': self.league_name,
            'n_matches': self.n_matches,
            'accuracy': round(self.accuracy, 4),
            'brier': round(self.brier, 4),
            'ece': round(self.ece, 4),
            'log_loss': round(self.log_loss, 4),
            'mcc': round(self.mcc, 4),
            'home_recall': round(self.home_recall, 4),
            'draw_recall': round(self.draw_recall, 4),
            'away_recall': round(self.away_recall, 4),
            'ece_home': round(self.ece_home, 4),
            'ece_draw': round(self.ece_draw, 4),
            'ece_away': round(self.ece_away, 4),
            'home_rate': round(self.home_rate, 4),
            'draw_rate': round(self.draw_rate, 4),
            'away_rate': round(self.away_rate, 4),
            'accuracy_delta': round(self.accuracy_delta, 4),
            'brier_delta': round(self.brier_delta, 4),
            'ece_delta': round(self.ece_delta, 4),
        }
        if self.date_range:
            d['date_range'] = self.date_range
        if self.n_folds_covered:
            d['n_folds_covered'] = self.n_folds_covered
        return d

# ════════════════════════════════════════════════════════════════
# 2. WeakSpot — 薄弱环节
# ════════════════════════════════════════════════════════════════

@dataclass
class WeakSpot:
    """识别的薄弱环节"""
    league: str
    metric: str
    value: float
    global_avg: float
    delta: float           # 与全局的差值 (负=低于全局)
    severity: str          # 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'
    category: str          # 'overall' | 'class_recall' | 'class_ece' | 'calibration'
    suggestion: str = ''   # 改进建议

# ════════════════════════════════════════════════════════════════
# 3. LeagueEvaluator — 联赛差异化评估器
# ════════════════════════════════════════════════════════════════

class LeagueEvaluator:
    """
    联赛差异化评估器 — 按联赛划分测试集并计算各联赛专属指标。

    Parameters
    ----------
    min_samples : int
        每个联赛最少样本数，低于此数的联赛被合并为 "Other"
    delta_threshold : float
        薄弱环节识别阈值: |delta| > delta_threshold 标记为薄弱
    severity_levels : dict
        严重等级阈值: {'LOW': 0.05, 'MEDIUM': 0.10, 'HIGH': 0.15, 'CRITICAL': 0.20}
    """

    SEVERITY_THRESHOLDS = {
        'LOW': 0.05,
        'MEDIUM': 0.10,
        'HIGH': 0.15,
        'CRITICAL': 0.20,
    }

    def __init__(self, min_samples: int = 100, delta_threshold: float = 0.05):
        self.min_samples = min_samples
        self.delta_threshold = delta_threshold

    def evaluate(self, result: BacktestResult, df: pd.DataFrame,
                 league_col: str = 'league_name',
                 date_col: str = 'match_date') -> 'LeagueEvaluationResult':
        """
        执行联赛差异化评估。

        Parameters
        ----------
        result : BacktestResult
            T17 回测结果
        df : pd.DataFrame
            原始数据, 需含 league_col 列
        league_col : str
            联赛列名
        date_col : str
            日期列名 (用于计算联赛时间范围)

        Returns
        -------
        LeagueEvaluationResult
        """
        assert league_col in df.columns, f"DataFrame 缺少联赛列: {league_col}"

        # 全局指标
        global_metrics = self._compute_global_metrics(result)

        # ── 关键: 通过折的 test_idx 将回测样本映射回原始 df 的联赛 ──
        # result.all_predictions 的行顺序 = 逐折拼接的 test_idx 对应的 df 行
        sample_leagues = np.empty(len(result.all_labels), dtype=object)
        sample_dates = np.empty(len(result.all_labels), dtype='datetime64[ns]')
        offset = 0
        for fm in result.fold_metrics:
            fold_mask = result.all_fold_ids == fm.fold_id
            fold_size = int(fold_mask.sum())
            # 从折指标中无法直接获取 test_idx，需要用 fold_id 匹配
            # BacktestResult 不保存 test_idx，所以遍历 folds 来映射
            # 但我们有更简单的办法: 用 all_fold_ids 确定行归属后，
            # 通过 T17 引擎的输出顺序对齐原始 df 的行
            pass  # 下面用统一映射方式

        # 统一映射: 遍历 BacktestResult.fold_metrics 获取各折样本
        # 由于 BacktestResult 不保存 test_idx，我们需要从 WalkForwardEngine 的
        # 输出顺序推断。T17 的 run() 按 folds 顺序拼接，所以:
        #   result 的第 i 个样本 = fold_metrics[fold_id].fold_id 对应折的第 j 个测试样本
        # 但折间顺序可能不连续，用 all_fold_ids 定位更可靠。
        #
        # 最安全的办法: 重建 df → result 的行映射
        # 方法: 逐折遍历，每折的 test_idx 指向 df 的行号

        # 我们需要 folds 列表来获取 test_idx
        # 但 BacktestResult 不直接保存 folds，所以采用替代方案:
        # 使用 df 和 result 的样本顺序对齐

        # 方案: 从 df 重建回测覆盖的行索引
        # WalkForwardEngine.run() 中: all_predictions 按 fold 顺序拼接,
        # 每折内部 test_df = df.iloc[fold.test_idx]
        # 所以 result 的第 k 行对应 df 的某个 test_idx 行

        # 由于我们没有 folds，用概率匹配来对齐:
        # 对于预计算概率模式, result.all_predictions 的行 = df 的某些行 (测试集)
        # 可以通过概率向量匹配找到对应行

        # 最简方案: 让 evaluate() 接受额外的 sample_indices 参数
        # 或: 直接从 df 重构, 要求 df 的行顺序与 result 一致

        # 实际最佳方案: 利用 fold_metrics 重建映射
        # 约定: result 中样本按 fold_id 顺序排列, 每个 fold 内样本顺序
        # 与 df.iloc[fold.test_idx] 一致

        # 为避免传递 folds 的复杂性, 使用索引对齐:
        # df 的行号与 result 样本的对应关系通过概率列匹配

        # 最终简化方案: 用 result 中的 fold 信息从 df 重建联赛标签
        # 如果 df 的长度与 result.all_labels 相同, 且行顺序一致 (全局模式)

        n_result = len(result.all_labels)
        n_df = len(df)

        # 尝试直接对齐 (df 与 result 行数相同 → 可能是全局评估)
        if n_df == n_result:
            sample_leagues = df[league_col].values
            if date_col in df.columns:
                sample_dates = pd.to_datetime(df[date_col]).values
            else:
                sample_dates = None
        else:
            # df 比 result 长: result 是回测子集
            # 需要找到 result 样本对应的 df 行
            # 方法: 利用概率向量匹配 (最精确)
            # 但这要求 df 中有概率列
            sample_leagues, sample_dates = self._align_result_to_df(
                result, df, league_col, date_col
            )

        # 按联赛分割
        league_metrics = {}
        leagues = sorted(set(sample_leagues) - {None})

        for league in leagues:
            league_mask = sample_leagues == league
            league_pred = result.all_predictions[league_mask]
            league_true = result.all_labels[league_mask]
            n_league = len(league_true)

            if n_league < self.min_samples:
                continue  # 样本不足, 跳过

            # 日期范围
            if sample_dates is not None and hasattr(sample_dates, '__len__'):
                league_dates = sample_dates[league_mask]
                # 过滤 NaT
                try:
                    valid_mask = ~pd.isna(league_dates)
                    valid_dates = league_dates[valid_mask]
                    date_range = ''
                    if len(valid_dates) > 0:
                        d_min = pd.Timestamp(valid_dates.min()).strftime('%Y-%m')
                        d_max = pd.Timestamp(valid_dates.max()).strftime('%Y-%m')
                        date_range = f'{d_min} ~ {d_max}'
                except (Exception, KeyError, IndexError):
                    date_range = ''
            else:
                date_range = ''

            metrics = self._compute_league_metrics(
                league, league_pred, league_true,
                global_metrics, date_range
            )
            league_metrics[league] = metrics

        # 样本不足的联赛合并
        small_leagues = [l for l in leagues if l not in league_metrics]
        if small_leagues:
            small_mask = np.isin(sample_leagues, small_leagues)
            if small_mask.sum() >= self.min_samples:
                small_pred = result.all_predictions[small_mask]
                small_true = result.all_labels[small_mask]
                metrics = self._compute_league_metrics(
                    'Other', small_pred, small_true,
                    global_metrics, ''
                )
                league_metrics['Other'] = metrics

        # 识别薄弱环节
        weak_spots = self._identify_weak_spots(league_metrics, global_metrics)

        # 生成改进建议
        suggestions = self._generate_suggestions(league_metrics, weak_spots, global_metrics)

        return LeagueEvaluationResult(
            league_metrics=league_metrics,
            weak_spots=weak_spots,
            suggestions=suggestions,
            global_metrics=global_metrics,
            n_leagues=len(league_metrics),
            total_samples=len(result.all_labels),
        )

    # ── 内部方法 ──

    def _align_result_to_df(self, result: BacktestResult, df: pd.DataFrame,
                             league_col: str, date_col: str):
        """
        将 BacktestResult 样本对齐回原始 DataFrame, 提取联赛和日期。

        使用概率向量匹配: result.all_predictions[i] 对应 df 中概率相同的行。
        对于预计算概率模式, 概率唯一标识一行。
        """
        n_result = len(result.all_labels)

        # 查找概率列
        prob_col_candidates = [
            ['home_prob', 'draw_prob', 'away_prob'],
            ['home_win_prob', 'draw_prob', 'away_win_prob'],
        ]
        prob_cols = None
        for cols in prob_col_candidates:
            if all(c in df.columns for c in cols):
                prob_cols = cols
                break

        if prob_cols is None:
            # 无法匹配, 返回空
            logger.warning("Cannot align result to df: no probability columns found. "
                           "League info may be incomplete.")
            sample_leagues = np.array(['Unknown'] * n_result, dtype=object)
            sample_dates = None
            return sample_leagues, sample_dates

        # 构建 df 的概率矩阵 (用于快速查找)
        df_probs = df[prob_cols].values.astype(float)

        # 逐折对齐: 利用 fold_id 分组
        sample_leagues = np.empty(n_result, dtype=object)
        sample_dates_arr = np.empty(n_result, dtype='datetime64[ns]')

        for fm in result.fold_metrics:
            fold_mask = result.all_fold_ids == fm.fold_id
            fold_indices = np.where(fold_mask)[0]
            fold_pred = result.all_predictions[fold_mask]

            # 尝试在 df 中匹配这些概率
            # 简化: 对每个概率向量找最近的 df 行
            for i, idx in enumerate(fold_indices):
                pred_vec = fold_pred[i]
                # 找距离最近的行
                dists = np.abs(df_probs - pred_vec).sum(axis=1)
                best_row = np.argmin(dists)
                if dists[best_row] < 1e-6:
                    sample_leagues[idx] = df.iloc[best_row][league_col]
                    if date_col in df.columns:
                        sample_dates_arr[idx] = pd.Timestamp(df.iloc[best_row][date_col])

        return sample_leagues, sample_dates_arr

    def _compute_global_metrics(self, result: BacktestResult) -> Dict:
        """计算全局指标"""
        y_pred = np.argmax(result.all_predictions, axis=1)
        y_true = result.all_labels
        y_proba = result.all_predictions

        cm = confusion_matrix(y_true, y_pred, labels=[0, 1, 2])
        recalls = cm.diagonal() / cm.sum(axis=1).clip(1)

        return {
            'accuracy': float(accuracy_score(y_true, y_pred)),
            'brier': float(multiclass_brier(y_proba, y_true)),
            'ece': float(compute_ece(y_proba, y_true)),
            'log_loss': float(log_loss(y_true, y_proba, labels=[0, 1, 2])),
            'mcc': float(matthews_corrcoef(y_true, y_pred)),
            'home_recall': float(recalls[0]),
            'draw_recall': float(recalls[1]),
            'away_recall': float(recalls[2]),
            'home_rate': float((y_true == 0).sum() / len(y_true)),
            'draw_rate': float((y_true == 1).sum() / len(y_true)),
            'away_rate': float((y_true == 2).sum() / len(y_true)),
        }

    def _compute_league_metrics(self, league: str,
                                 y_proba: np.ndarray, y_true: np.ndarray,
                                 global_metrics: Dict,
                                 date_range: str = '') -> LeagueMetrics:
        """计算单联赛指标"""
        y_true_int = np.asarray(y_true, dtype=np.int64)
        y_pred = np.argmax(y_proba, axis=1)

        # 概率归一化
        y_proba = np.clip(y_proba, 1e-10, 1.0)
        y_proba = y_proba / y_proba.sum(axis=1, keepdims=True)

        # 核心指标
        acc = float(accuracy_score(y_true_int, y_pred))
        brier = float(multiclass_brier(y_proba, y_true_int))
        ece = float(compute_ece(y_proba, y_true_int))
        ll = float(log_loss(y_true_int, y_proba, labels=[0, 1, 2]))
        mcc = float(matthews_corrcoef(y_true_int, y_pred))

        # 混淆矩阵
        cm = confusion_matrix(y_true_int, y_pred, labels=[0, 1, 2])
        recalls = cm.diagonal() / cm.sum(axis=1).clip(1)

        # 分类别 ECE
        ece_per_class = []
        for c in range(3):
            mask = (y_true_int == c)
            if mask.sum() > 10:
                class_proba = y_proba[mask]
                class_conf = class_proba[:, c]
                class_preds = (np.argmax(class_proba, axis=1) == c).astype(int)
                ece_c = float(compute_ece(
                    np.column_stack([1 - class_conf, class_conf]),
                    class_preds
                ))
                ece_per_class.append(ece_c)
            else:
                ece_per_class.append(0.0)

        # 类别分布
        n = len(y_true_int)
        home_rate = float((y_true_int == 0).sum() / n)
        draw_rate = float((y_true_int == 1).sum() / n)
        away_rate = float((y_true_int == 2).sum() / n)

        # 与全局对比的 delta
        acc_delta = acc - global_metrics['accuracy']
        brier_delta = global_metrics['brier'] - brier  # Brier 越低越好, 正值=优于全局
        ece_delta = global_metrics['ece'] - ece        # ECE 越低越好, 正值=优于全局

        return LeagueMetrics(
            league_name=league,
            n_matches=n,
            accuracy=acc,
            brier=brier,
            ece=ece,
            log_loss=ll,
            mcc=mcc,
            home_recall=float(recalls[0]),
            draw_recall=float(recalls[1]),
            away_recall=float(recalls[2]),
            ece_home=ece_per_class[0],
            ece_draw=ece_per_class[1],
            ece_away=ece_per_class[2],
            confusion_matrix=cm,
            home_rate=home_rate,
            draw_rate=draw_rate,
            away_rate=away_rate,
            accuracy_delta=acc_delta,
            brier_delta=brier_delta,
            ece_delta=ece_delta,
            date_range=date_range,
        )

    def _identify_weak_spots(self, league_metrics: Dict[str, LeagueMetrics],
                              global_metrics: Dict) -> List[WeakSpot]:
        """识别薄弱环节"""
        weak_spots = []

        # 需要检查的指标: (属性名, 全局键, 方向, 类别)
        # 方向: 'higher_better' 或 'lower_better'
        checks = [
            ('accuracy', 'accuracy', 'higher_better', 'overall'),
            ('brier', 'brier', 'lower_better', 'calibration'),
            ('ece', 'ece', 'lower_better', 'calibration'),
            ('log_loss', 'log_loss', 'lower_better', 'overall'),
            ('mcc', 'mcc', 'higher_better', 'overall'),
            ('home_recall', 'home_recall', 'higher_better', 'class_recall'),
            ('draw_recall', 'draw_recall', 'higher_better', 'class_recall'),
            ('away_recall', 'away_recall', 'higher_better', 'class_recall'),
            ('ece_home', 'home_recall', 'lower_better', 'class_ece'),
            ('ece_draw', 'draw_recall', 'lower_better', 'class_ece'),
            ('ece_away', 'away_recall', 'lower_better', 'class_ece'),
        ]

        # 分类别 ECE 用全局对应类别的 ECE
        # 需要从全局混淆矩阵计算分类别 ECE
        # 简化: 用全局 ece 作为基准

        for league, lm in league_metrics.items():
            for attr, global_key, direction, category in checks:
                value = getattr(lm, attr)
                global_val = global_metrics.get(global_key, 0)

                if direction == 'higher_better':
                    delta = value - global_val  # 负值 = 低于全局
                    if delta < -self.delta_threshold:
                        severity = self._classify_severity(abs(delta))
                        weak_spots.append(WeakSpot(
                            league=league, metric=attr, value=value,
                            global_avg=global_val, delta=delta,
                            severity=severity, category=category,
                        ))
                else:  # lower_better
                    delta = value - global_val  # 正值 = 高于全局 (差)
                    if delta > self.delta_threshold:
                        severity = self._classify_severity(abs(delta))
                        weak_spots.append(WeakSpot(
                            league=league, metric=attr, value=value,
                            global_avg=global_val, delta=delta,
                            severity=severity, category=category,
                        ))

        # 按 severity 排序
        severity_order = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}
        weak_spots.sort(key=lambda w: severity_order.get(w.severity, 4))

        return weak_spots

    def _classify_severity(self, abs_delta: float) -> str:
        """分类严重等级"""
        for level, threshold in sorted(self.SEVERITY_THRESHOLDS.items(),
                                        key=lambda x: -x[1]):
            if abs_delta >= threshold:
                return level
        return 'LOW'

    def _generate_suggestions(self, league_metrics: Dict[str, LeagueMetrics],
                               weak_spots: List[WeakSpot],
                               global_metrics: Dict) -> List[Dict]:
        """生成针对性改进建议"""
        suggestions = []

        # 按联赛分组薄弱环节
        league_weaknesses = defaultdict(list)
        for ws in weak_spots:
            league_weaknesses[ws.league].append(ws)

        for league, weaknesses in league_weaknesses.items():
            lm = league_metrics[league]
            categories = set(w.category for w in weaknesses)

            # 1. 平局召回率低
            if 'class_recall' in categories:
                draw_ws = [w for w in weaknesses if w.metric == 'draw_recall']
                if draw_ws:
                    suggestions.append({
                        'league': league,
                        'type': 'draw_prediction',
                        'priority': draw_ws[0].severity,
                        'problem': f"平局召回率仅 {lm.draw_recall:.2%}，低于全局 {lm.draw_recall - global_metrics['draw_recall']:+.2%}",
                        'suggestion': (
                            f"1. 增加 {league} 平局特征权重 (如低赔率差、历史平局率高的对决)\n"
                            f"2. 考虑联赛专属平局先验 (当前平局率 {lm.draw_rate:.2%} vs 全局 {global_metrics['draw_rate']:.2%})\n"
                            f"3. 引入平局专项特征: 近N轮平局数、联赛阶段(赛季末平局更多)"
                        ),
                        'metrics': {'draw_recall': lm.draw_recall, 'draw_rate': lm.draw_rate},
                    })

            # 2. 校准差
            if 'calibration' in categories:
                ece_ws = [w for w in weaknesses if w.metric == 'ece']
                brier_ws = [w for w in weaknesses if w.metric == 'brier']
                if ece_ws or brier_ws:
                    suggestions.append({
                        'league': league,
                        'type': 'calibration',
                        'priority': max((w.severity for w in (ece_ws + brier_ws)),
                                       key=lambda s: {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}.get(s, 4)),
                        'problem': f"概率校准偏差大: ECE={lm.ece:.4f}, Brier={lm.brier:.4f}",
                        'suggestion': (
                            f"1. 为 {league} 训练联赛专属校准器 (Temperature Scaling 或 Isotonic Regression)\n"
                            f"2. 检查赔率数据源: 该联赛是否有足够的历史赔率数据\n"
                            f"3. 考虑联赛专属后处理: 根据联赛特征调整概率分布"
                        ),
                        'metrics': {'ece': lm.ece, 'brier': lm.brier},
                    })

            # 3. 整体准确率低
            if 'overall' in categories:
                acc_ws = [w for w in weaknesses if w.metric == 'accuracy']
                if acc_ws:
                    suggestions.append({
                        'league': league,
                        'type': 'accuracy',
                        'priority': acc_ws[0].severity,
                        'problem': f"准确率仅 {lm.accuracy:.2%}，低于全局 {lm.accuracy - global_metrics['accuracy']:+.2%}",
                        'suggestion': (
                            f"1. 增加联赛专属特征: {league} 的战术风格、主客场强度差异\n"
                            f"2. 检查数据质量: 该联赛 {lm.n_matches} 场比赛，是否样本充足\n"
                            f"3. 考虑联赛专属模型或模型集成中增加联赛权重\n"
                            f"4. 分析混淆矩阵: Home={lm.home_recall:.2%} Draw={lm.draw_recall:.2%} Away={lm.away_recall:.2%}"
                        ),
                        'metrics': {'accuracy': lm.accuracy, 'mcc': lm.mcc},
                    })

            # 4. 分类别 ECE
            if 'class_ece' in categories:
                ece_class_ws = [w for w in weaknesses if w.metric.startswith('ece_')]
                if ece_class_ws:
                    worst = ece_class_ws[0]
                    cls_name = worst.metric.replace('ece_', '').title()
                    suggestions.append({
                        'league': league,
                        'type': 'class_calibration',
                        'priority': worst.severity,
                        'problem': f"{cls_name} 类校准差: ECE={worst.value:.4f}",
                        'suggestion': (
                            f"1. 检查 {league} 的 {cls_name} 概率分布是否偏移\n"
                            f"2. 为该类别增加训练样本或使用类权重平衡\n"
                            f"3. 考虑针对 {cls_name} 预测增加特征: 如防守数据、伤病信息"
                        ),
                        'metrics': {worst.metric: worst.value},
                    })

        # 5. 联赛间对比建议
        if len(league_metrics) > 1:
            best_league = max(league_metrics.items(), key=lambda x: x[1].accuracy)
            worst_league = min(league_metrics.items(), key=lambda x: x[1].accuracy)
            gap = best_league[1].accuracy - worst_league[1].accuracy

            if gap > 0.10:
                suggestions.append({
                    'league': 'ALL',
                    'type': 'cross_league_gap',
                    'priority': 'HIGH' if gap > 0.15 else 'MEDIUM',
                    'problem': f"联赛间准确率差距 {gap:.2%}: 最佳 {best_league[0]}={best_league[1].accuracy:.2%}, 最差 {worst_league[0]}={worst_league[1].accuracy:.2%}",
                    'suggestion': (
                        f"1. 考虑分层建模: 为不同联赛训练独立模型或使用联赛 embedding\n"
                        f"2. 分析 {worst_league[0]} 与 {best_league[0]} 的数据分布差异\n"
                        f"3. 迁移学习: 用 {best_league[0]} 的模型参数初始化 {worst_league[0]} 模型\n"
                        f"4. 特征工程: 添加联赛级特征 (进球率、平局率、主客场差异度)"
                    ),
                    'metrics': {'gap': gap, 'best': best_league[0], 'worst': worst_league[0]},
                })

        # 按优先级排序
        priority_order = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}
        suggestions.sort(key=lambda s: priority_order.get(s.get('priority', 'LOW'), 4))

        return suggestions

# ════════════════════════════════════════════════════════════════
# 4. LeagueEvaluationResult — 评估结果容器
# ════════════════════════════════════════════════════════════════

@dataclass
class LeagueEvaluationResult:
    """联赛差异化评估结果"""
    league_metrics: Dict[str, LeagueMetrics]
    weak_spots: List[WeakSpot]
    suggestions: List[Dict]
    global_metrics: Dict
    n_leagues: int
    total_samples: int

    def summary(self) -> Dict:
        """汇总信息"""
        result = {
            'n_leagues': self.n_leagues,
            'total_samples': self.total_samples,
            'global': self.global_metrics,
            'leagues': {},
            'n_weak_spots': len(self.weak_spots),
            'n_suggestions': len(self.suggestions),
        }

        for league, lm in self.league_metrics.items():
            result['leagues'][league] = lm.to_dict()

        # 严重等级统计
        severity_counts = defaultdict(int)
        for ws in self.weak_spots:
            severity_counts[ws.severity] += 1
        result['severity_distribution'] = dict(severity_counts)

        # 建议类型统计
        type_counts = defaultdict(int)
        for s in self.suggestions:
            type_counts[s['type']] += 1
        result['suggestion_types'] = dict(type_counts)

        return result

    def to_dataframe(self) -> pd.DataFrame:
        """联赛指标转为 DataFrame"""
        records = []
        for league, lm in self.league_metrics.items():
            records.append(lm.to_dict())
        return pd.DataFrame(records)

    def weak_spots_dataframe(self) -> pd.DataFrame:
        """薄弱环节转 DataFrame"""
        records = []
        for ws in self.weak_spots:
            records.append({
                'league': ws.league,
                'metric': ws.metric,
                'value': round(ws.value, 4),
                'global_avg': round(ws.global_avg, 4),
                'delta': round(ws.delta, 4),
                'severity': ws.severity,
                'category': ws.category,
            })
        return pd.DataFrame(records)

    def get_league(self, league_name: str) -> Optional[LeagueMetrics]:
        """获取指定联赛的指标"""
        return self.league_metrics.get(league_name)

    def get_weak_leagues(self, min_severity: str = 'MEDIUM') -> List[str]:
        """获取存在薄弱环节的联赛列表"""
        severity_order = {'LOW': 0, 'MEDIUM': 1, 'HIGH': 2, 'CRITICAL': 3}
        min_level = severity_order.get(min_severity, 1)
        leagues = set()
        for ws in self.weak_spots:
            if severity_order.get(ws.severity, 0) >= min_level:
                leagues.add(ws.league)
        return sorted(leagues)

# ════════════════════════════════════════════════════════════════
# 5. LeagueVisualizer — 联赛级可视化
# ════════════════════════════════════════════════════════════════

class LeagueVisualizer:
    """
    联赛差异化评估可视化工具。

    Parameters
    ----------
    output_dir : str
        图表输出目录
    dpi : int
        图表分辨率
    """

    # 配色方案
    LEAGUE_COLORS = {
        'Premier League': '#3D195B',
        'La Liga': '#EE8707',
        'Serie A': '#024494',
        'Bundesliga': '#D20515',
        'Ligue 1': '#091C3E',
    }
    DEFAULT_COLORS = ['#1E88E5', '#E53935', '#43A047', '#FB8C00', '#8E24AA',
                       '#00ACC1', '#F4511E', '#6D4C41']

    def __init__(self, output_dir: str = DEFAULT_OUTPUT_DIR, dpi: int = 150):
        self.output_dir = output_dir
        self.dpi = dpi
        os.makedirs(output_dir, exist_ok=True)

    def _get_color(self, league: str, idx: int = 0) -> str:
        """获取联赛颜色"""
        return self.LEAGUE_COLORS.get(league, self.DEFAULT_COLORS[idx % len(self.DEFAULT_COLORS)])

    # ── 图1: 联赛指标对比柱状图 ──

    def league_comparison_bar(self, result: LeagueEvaluationResult,
                               metrics: Optional[List[str]] = None,
                               title: str = 'League Comparison') -> str:
        """
        联赛指标对比柱状图。

        Parameters
        ----------
        result : LeagueEvaluationResult
        metrics : list of str, optional
            要对比的指标, 默认 ['accuracy', 'brier', 'ece']
        title : str

        Returns
        -------
        输出文件路径
        """
        if metrics is None:
            metrics = ['accuracy', 'brier', 'ece']

        leagues = list(result.league_metrics.keys())
        n_leagues = len(leagues)
        n_metrics = len(metrics)

        fig, axes = plt.subplots(1, n_metrics, figsize=(5 * n_metrics, 6))
        if n_metrics == 1:
            axes = [axes]

        for ax, metric in zip(axes, metrics):
            values = [getattr(result.league_metrics[l], metric) for l in leagues]
            global_val = result.global_metrics.get(metric, 0)
            colors = [self._get_color(l, i) for i, l in enumerate(leagues)]

            bars = ax.barh(range(n_leagues), values, color=colors, alpha=0.85,
                           edgecolor='white', height=0.6)

            # 全局均值线
            ax.axvline(x=global_val, color='red', linestyle='--', linewidth=1.5,
                       label=f'Global: {global_val:.4f}')

            # 标注数值
            for i, (v, l) in enumerate(zip(values, leagues)):
                ax.text(v + 0.002, i, f'{v:.4f}', va='center', fontsize=9)

            ax.set_yticks(range(n_leagues))
            ax.set_yticklabels(leagues, fontsize=10)
            ax.set_xlabel(metric.upper())
            ax.set_title(metric.upper())
            ax.legend(fontsize=9)
            ax.grid(axis='x', alpha=0.3)

        fig.suptitle(title, fontsize=14, fontweight='bold')
        plt.tight_layout()

        path = os.path.join(self.output_dir, 'league_comparison_bar.png')
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"Saved league comparison bar chart: {path}")
        return path

    # ── 图2: 联赛雷达图 ──

    def league_radar(self, result: LeagueEvaluationResult,
                      title: str = 'League Performance Radar') -> str:
        """
        各联赛雷达图对比。

        Returns
        -------
        输出文件路径
        """
        categories = ['Accuracy', 'Brier-Inv', 'ECE-Inv', 'MCC', 'H_Recall', 'D_Recall', 'A_Recall']
        n_cats = len(categories)
        attr_map = ['accuracy', 'brier', 'ece', 'mcc', 'home_recall', 'draw_recall', 'away_recall']
        invert = {1, 2}  # Brier, ECE 越低越好

        leagues = list(result.league_metrics.keys())
        # 收集原始值
        raw = np.zeros((len(leagues), n_cats))
        for i, l in enumerate(leagues):
            lm = result.league_metrics[l]
            for j, attr in enumerate(attr_map):
                raw[i, j] = getattr(lm, attr)

        # 归一化到 [0, 1]
        normalized = np.zeros_like(raw)
        for j in range(n_cats):
            col = raw[:, j]
            if j in invert:
                if col.max() > col.min():
                    normalized[:, j] = (col.max() - col) / (col.max() - col.min())
                else:
                    normalized[:, j] = 0.5
            else:
                if col.max() > col.min():
                    normalized[:, j] = (col - col.min()) / (col.max() - col.min())
                else:
                    normalized[:, j] = 0.5

        angles = np.linspace(0, 2 * np.pi, n_cats, endpoint=False).tolist()
        angles += angles[:1]

        fig, ax = plt.subplots(figsize=(9, 9), subplot_kw=dict(polar=True))

        for i, league in enumerate(leagues):
            values = normalized[i].tolist()
            values += values[:1]
            color = self._get_color(league, i)
            ax.plot(angles, values, 'o-', linewidth=2, markersize=5,
                    color=color, label=league, alpha=0.8)
            ax.fill(angles, values, alpha=0.1, color=color)

        ax.set_xticks(angles[:-1])
        ax.set_xticklabels(categories, fontsize=10)
        ax.set_title(title, pad=20, fontsize=13, fontweight='bold')
        ax.legend(loc='upper right', bbox_to_anchor=(1.3, 1.1), fontsize=9)

        path = os.path.join(self.output_dir, 'league_radar.png')
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"Saved league radar chart: {path}")
        return path

    # ── 图3: 类别分布堆叠图 ──

    def class_distribution_stacked(self, result: LeagueEvaluationResult,
                                    title: str = 'Result Distribution by League') -> str:
        """
        各联赛 H/D/A 分布堆叠柱状图。

        Returns
        -------
        输出文件路径
        """
        leagues = list(result.league_metrics.keys())
        home_rates = [result.league_metrics[l].home_rate for l in leagues]
        draw_rates = [result.league_metrics[l].draw_rate for l in leagues]
        away_rates = [result.league_metrics[l].away_rate for l in leagues]

        fig, ax = plt.subplots(figsize=(10, 6))

        x = np.arange(len(leagues))
        width = 0.5

        p1 = ax.bar(x, home_rates, width, label='Home', color='#1E88E5', alpha=0.85)
        p2 = ax.bar(x, draw_rates, width, bottom=home_rates, label='Draw', color='#FB8C00', alpha=0.85)
        p3 = ax.bar(x, away_rates, width,
                     bottom=[h + d for h, d in zip(home_rates, draw_rates)],
                     label='Away', color='#E53935', alpha=0.85)

        # 全局均值线
        global_home = result.global_metrics['home_rate']
        global_draw = result.global_metrics['draw_rate']
        global_away = result.global_metrics['away_rate']
        ax.axhline(y=global_home, color='#1E88E5', linestyle=':', alpha=0.5)
        ax.axhline(y=global_home + global_draw, color='#FB8C00', linestyle=':', alpha=0.5)

        # 标注比例
        for i in range(len(leagues)):
            ax.text(i, home_rates[i] / 2, f'{home_rates[i]:.0%}',
                    ha='center', va='center', fontsize=9, color='white', fontweight='bold')
            ax.text(i, home_rates[i] + draw_rates[i] / 2, f'{draw_rates[i]:.0%}',
                    ha='center', va='center', fontsize=9, color='white', fontweight='bold')
            ax.text(i, home_rates[i] + draw_rates[i] + away_rates[i] / 2, f'{away_rates[i]:.0%}',
                    ha='center', va='center', fontsize=9, color='white', fontweight='bold')

        ax.set_xticks(x)
        ax.set_xticklabels(leagues, rotation=30, ha='right', fontsize=10)
        ax.set_ylabel('Proportion')
        ax.set_ylim(0, 1.05)
        ax.legend(fontsize=10)
        ax.set_title(title, fontsize=13, fontweight='bold')
        ax.grid(axis='y', alpha=0.2)

        plt.tight_layout()

        path = os.path.join(self.output_dir, 'class_distribution_stacked.png')
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"Saved class distribution stacked chart: {path}")
        return path

    # ── 图4: 各联赛混淆矩阵 ──

    def confusion_per_league(self, result: LeagueEvaluationResult,
                              title: str = 'Confusion Matrices by League') -> str:
        """
        各联赛混淆矩阵网格图。

        Returns
        -------
        输出文件路径
        """
        leagues = list(result.league_metrics.keys())
        n = len(leagues)

        cols = min(3, n)
        rows = (n + cols - 1) // cols

        fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 4.5 * rows))
        if rows == 1 and cols == 1:
            axes = np.array([[axes]])
        elif rows == 1:
            axes = axes.reshape(1, -1)
        elif cols == 1:
            axes = axes.reshape(-1, 1)

        for idx, league in enumerate(leagues):
            r, c = divmod(idx, cols)
            ax = axes[r, c]
            lm = result.league_metrics[league]
            cm = lm.confusion_matrix

            if cm is not None:
                im = ax.imshow(cm, cmap='Blues')
                for i in range(3):
                    for j in range(3):
                        pct = cm[i, j] / cm[i].sum() * 100 if cm[i].sum() > 0 else 0
                        ax.text(j, i, f'{cm[i, j]}\n({pct:.0f}%)',
                                ha='center', va='center', fontsize=8,
                                color='white' if cm[i, j] > cm.max() / 2 else 'black')

                ax.set_xticks(range(3))
                ax.set_xticklabels(CLASS_LABELS, fontsize=8)
                ax.set_yticks(range(3))
                ax.set_yticklabels(CLASS_LABELS, fontsize=8)
                ax.set_title(f'{league}\n(Acc={lm.accuracy:.2%}, N={lm.n_matches})', fontsize=9)
                ax.set_xlabel('Predicted', fontsize=8)
                ax.set_ylabel('Actual', fontsize=8)
            else:
                ax.text(0.5, 0.5, 'N/A', ha='center', va='center', fontsize=12)
                ax.set_title(league, fontsize=9)

        # 隐藏多余子图
        for idx in range(n, rows * cols):
            r, c = divmod(idx, cols)
            axes[r, c].set_visible(False)

        fig.suptitle(title, fontsize=14, fontweight='bold')
        plt.tight_layout()

        path = os.path.join(self.output_dir, 'confusion_per_league.png')
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"Saved confusion per league chart: {path}")
        return path

    # ── 图5: 薄弱环节热力图 ──

    def weak_spots_heatmap(self, result: LeagueEvaluationResult,
                            title: str = 'Weak Spots Heatmap') -> str:
        """
        联赛×指标 薄弱环节热力图 (delta 值, 红色=差于全局, 绿色=优于全局)。

        Returns
        -------
        输出文件路径
        """
        metrics_list = ['accuracy', 'brier', 'ece', 'log_loss', 'mcc',
                         'home_recall', 'draw_recall', 'away_recall']
        leagues = list(result.league_metrics.keys())

        # 构建 delta 矩阵
        data = np.zeros((len(leagues), len(metrics_list)))
        for i, league in enumerate(leagues):
            lm = result.league_metrics[league]
            for j, metric in enumerate(metrics_list):
                value = getattr(lm, metric)
                global_val = result.global_metrics.get(metric, 0)
                # 统一方向: 正值 = 优于全局
                if metric in ('brier', 'ece', 'log_loss'):
                    data[i, j] = global_val - value  # 越低越好, 全局-联赛 = 优势
                else:
                    data[i, j] = value - global_val    # 越高越好, 联赛-全局 = 优势

        fig, ax = plt.subplots(figsize=(12, max(4, len(leagues) * 0.6)))

        # 自定义颜色: 红(差) - 白(持平) - 绿(优)
        from matplotlib.colors import TwoSlopeNorm
        vmax = max(abs(data.min()), abs(data.max()), 0.05)
        norm = TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)

        im = ax.imshow(data, cmap='RdYlGn', norm=norm, aspect='auto')

        # 标注
        for i in range(data.shape[0]):
            for j in range(data.shape[1]):
                val = data[i, j]
                color = 'white' if abs(val) > vmax * 0.5 else 'black'
                ax.text(j, i, f'{val:+.3f}', ha='center', va='center',
                        fontsize=8, color=color)

        ax.set_xticks(range(len(metrics_list)))
        ax.set_xticklabels([m.upper() for m in metrics_list], rotation=45, ha='right', fontsize=9)
        ax.set_yticks(range(len(leagues)))
        ax.set_yticklabels(leagues, fontsize=10)
        ax.set_title(title, fontsize=13, fontweight='bold')

        plt.colorbar(im, ax=ax, shrink=0.8, label='Delta vs Global (+ = better)')
        plt.tight_layout()

        path = os.path.join(self.output_dir, 'weak_spots_heatmap.png')
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"Saved weak spots heatmap: {path}")
        return path

    # ── 图6: 联赛召回率对比图 ──

    def class_recall_comparison(self, result: LeagueEvaluationResult,
                                 title: str = 'Class Recall by League') -> str:
        """
        各联赛分类别召回率分组柱状图。

        Returns
        -------
        输出文件路径
        """
        leagues = list(result.league_metrics.keys())
        n = len(leagues)

        home_recalls = [result.league_metrics[l].home_recall for l in leagues]
        draw_recalls = [result.league_metrics[l].draw_recall for l in leagues]
        away_recalls = [result.league_metrics[l].away_recall for l in leagues]

        fig, ax = plt.subplots(figsize=(max(8, n * 1.5), 6))

        x = np.arange(n)
        width = 0.25

        bars1 = ax.bar(x - width, home_recalls, width, label='Home', color='#1E88E5', alpha=0.85)
        bars2 = ax.bar(x, draw_recalls, width, label='Draw', color='#FB8C00', alpha=0.85)
        bars3 = ax.bar(x + width, away_recalls, width, label='Away', color='#E53935', alpha=0.85)

        # 全局均值线
        ax.axhline(y=result.global_metrics['home_recall'], color='#1E88E5', linestyle=':', alpha=0.5)
        ax.axhline(y=result.global_metrics['draw_recall'], color='#FB8C00', linestyle=':', alpha=0.5)
        ax.axhline(y=result.global_metrics['away_recall'], color='#E53935', linestyle=':', alpha=0.5)

        # 标注数值
        for bars in [bars1, bars2, bars3]:
            for bar in bars:
                height = bar.get_height()
                ax.annotate(f'{height:.2%}',
                           xy=(bar.get_x() + bar.get_width() / 2, height),
                           xytext=(0, 3), textcoords='offset points',
                           ha='center', va='bottom', fontsize=8)

        ax.set_xticks(x)
        ax.set_xticklabels(leagues, rotation=30, ha='right', fontsize=10)
        ax.set_ylabel('Recall')
        ax.set_ylim(0, max(max(home_recalls), max(draw_recalls), max(away_recalls)) * 1.15)
        ax.legend(fontsize=10)
        ax.set_title(title, fontsize=13, fontweight='bold')
        ax.grid(axis='y', alpha=0.2)

        plt.tight_layout()

        path = os.path.join(self.output_dir, 'class_recall_comparison.png')
        fig.savefig(path, dpi=self.dpi, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"Saved class recall comparison chart: {path}")
        return path

    # ── 一键生成所有图表 ──

    def generate_all_charts(self, result: LeagueEvaluationResult) -> List[str]:
        """一键生成所有联赛评估图表"""
        paths = []
        paths.append(self.league_comparison_bar(result))
        paths.append(self.league_radar(result))
        paths.append(self.class_distribution_stacked(result))
        paths.append(self.confusion_per_league(result))
        paths.append(self.weak_spots_heatmap(result))
        paths.append(self.class_recall_comparison(result))
        return paths

# ════════════════════════════════════════════════════════════════
# 6. LeagueReportBuilder — HTML 报告生成
# ════════════════════════════════════════════════════════════════

class LeagueReportBuilder:
    """
    联赛差异化评估 HTML 报告生成器。
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

    def generate(self, result: LeagueEvaluationResult,
                 chart_paths: Optional[List[str]] = None,
                 title: str = 'League-Differentiated Evaluation Report') -> str:
        """
        生成 HTML 报告。

        Returns
        -------
        HTML 文件路径
        """
        summary = result.summary()

        # ── 嵌入图表 ──
        chart_html = ''
        if chart_paths:
            for path in chart_paths:
                b64 = self._embed_image(path)
                if b64:
                    chart_html += f'<div style="text-align:center;margin:15px 0;"><img src="{b64}" style="max-width:100%;border-radius:4px;box-shadow:0 1px 3px rgba(0,0,0,0.1);"></div>\n'

        # ── 全局指标卡片 ──
        global_cards = ''
        g = result.global_metrics
        card_items = [
            ('Global Accuracy', f"{g['accuracy']:.4f}"),
            ('Global Brier', f"{g['brier']:.4f}"),
            ('Global ECE', f"{g['ece']:.4f}"),
            ('Global MCC', f"{g['mcc']:.4f}"),
            ('Leagues', str(result.n_leagues)),
            ('Total Samples', str(result.total_samples)),
        ]
        for label, value in card_items:
            global_cards += f'''<div class="metric-card">
                <div class="value">{value}</div>
                <div class="label">{label}</div>
            </div>'''

        # ── 联赛指标表格 ──
        league_rows = ''
        for league, lm in result.league_metrics.items():
            # 根据与全局差距着色
            acc_class = 'cell-bad' if lm.accuracy_delta < -0.05 else ('cell-good' if lm.accuracy_delta > 0.05 else '')
            brier_class = 'cell-bad' if lm.brier_delta < -0.05 else ('cell-good' if lm.brier_delta > 0.05 else '')
            ece_class = 'cell-bad' if lm.ece_delta < -0.05 else ('cell-good' if lm.ece_delta > 0.05 else '')

            league_rows += (f'<tr><td><b>{league}</b></td>'
                           f'<td>{lm.n_matches}</td>'
                           f'<td class="{acc_class}">{lm.accuracy:.4f} ({lm.accuracy_delta:+.4f})</td>'
                           f'<td class="{brier_class}">{lm.brier:.4f} ({lm.brier_delta:+.4f})</td>'
                           f'<td class="{ece_class}">{lm.ece:.4f} ({lm.ece_delta:+.4f})</td>'
                           f'<td>{lm.log_loss:.4f}</td>'
                           f'<td>{lm.mcc:.4f}</td>'
                           f'<td>{lm.home_recall:.4f}</td>'
                           f'<td>{lm.draw_recall:.4f}</td>'
                           f'<td>{lm.away_recall:.4f}</td>'
                           f'<td>{lm.home_rate:.2%}</td>'
                           f'<td>{lm.draw_rate:.2%}</td>'
                           f'<td>{lm.away_rate:.2%}</td>'
                           f'<td>{lm.date_range}</td></tr>\n')

        # ── 薄弱环节告警 ──
        weak_html = ''
        for ws in result.weak_spots:
            cls = 'alert-danger' if ws.severity in ('CRITICAL', 'HIGH') else 'alert-warning'
            direction = '↓' if ws.delta < 0 else '↑'
            weak_html += (f'<div class="alert {cls}">'
                         f'[{ws.severity}] <b>{ws.league}</b> — {ws.metric}: '
                         f'{ws.value:.4f} {direction} (global: {ws.global_avg:.4f}, '
                         f'delta: {ws.delta:+.4f}, category: {ws.category})</div>\n')

        # ── 改进建议 ──
        suggestion_html = ''
        for s in result.suggestions:
            priority_cls = 'sug-high' if s['priority'] in ('CRITICAL', 'HIGH') else 'sug-medium'
            suggestion_html += f'''<div class="suggestion {priority_cls}">
                <h4>🎯 {s['league']} — {s['type'].replace('_', ' ').title()} [{s['priority']}]</h4>
                <p><b>Problem:</b> {s['problem']}</p>
                <pre>{s['suggestion']}</pre>
            </div>\n'''

        gen_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        body {{ font-family: 'Segoe UI', 'SimHei', sans-serif; margin: 20px; background: #f8f9fa; }}
        .container {{ max-width: 1400px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        h1 {{ color: #1565C0; border-bottom: 2px solid #1565C0; padding-bottom: 10px; }}
        h2 {{ color: #424242; margin-top: 30px; }}
        .table {{ width: 100%; border-collapse: collapse; margin: 15px 0; font-size: 12px; }}
        .table th, .table td {{ padding: 6px 8px; border: 1px solid #ddd; text-align: center; }}
        .table th {{ background: #1565C0; color: white; position: sticky; top: 0; }}
        .table-striped tr:nth-child(even) {{ background: #f5f5f5; }}
        .cell-good {{ background: #c8e6c9 !important; }}
        .cell-bad {{ background: #ffcdd2 !important; }}
        .alert {{ padding: 10px 15px; border-radius: 4px; margin: 8px 0; font-size: 13px; }}
        .alert-warning {{ background: #fff3cd; color: #856404; border: 1px solid #ffeaa7; }}
        .alert-danger {{ background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
        .metric-card {{ display: inline-block; padding: 12px 20px; margin: 4px; background: #e3f2fd; border-radius: 8px; text-align: center; }}
        .metric-card .value {{ font-size: 20px; font-weight: bold; color: #1565C0; }}
        .metric-card .label {{ font-size: 11px; color: #666; }}
        .suggestion {{ padding: 15px; margin: 10px 0; border-radius: 6px; border-left: 4px solid; }}
        .sug-high {{ background: #fff3e0; border-color: #e65100; }}
        .sug-medium {{ background: #e8f5e9; border-color: #2e7d32; }}
        .suggestion h4 {{ margin: 0 0 8px 0; color: #333; }}
        .suggestion pre {{ font-size: 12px; white-space: pre-wrap; background: #fafafa; padding: 10px; border-radius: 4px; margin: 8px 0; }}
        .footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid #ddd; color: #999; font-size: 12px; }}
    </style>
</head>
<body>
<div class="container">
    <h1>{title}</h1>
    <p>Generated: {gen_time}</p>

    <h2>Global Metrics</h2>
    {global_cards}

    <h2>Charts</h2>
    {chart_html}

    <h2>League Metrics Comparison</h2>
    <div style="overflow-x:auto;">
    <table class="table table-striped">
        <tr>
            <th>League</th><th>N</th><th>Acc (Δ)</th><th>Brier (Δ)</th><th>ECE (Δ)</th>
            <th>LogLoss</th><th>MCC</th><th>H_Rec</th><th>D_Rec</th><th>A_Rec</th>
            <th>H%</th><th>D%</th><th>A%</th><th>Period</th>
        </tr>
        {league_rows}
    </table>
    </div>

    <h2>Weak Spots ({len(result.weak_spots)} identified)</h2>
    {weak_html if result.weak_spots else '<p style="color:#43A047;">No significant weak spots identified.</p>'}

    <h2>Improvement Suggestions ({len(result.suggestions)} items)</h2>
    {suggestion_html if result.suggestions else '<p>No specific suggestions needed.</p>'}

    <div class="footer">
        <p>FootballAI - League-Differentiated Evaluation Report (T18) | Auto-generated on {gen_time[:10]}</p>
    </div>
</div>
</body>
</html>"""

        path = os.path.join(self.output_dir, 'league_evaluation_report.html')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html)

        logger.info(f"Saved league evaluation HTML report: {path}")
        return path

# ════════════════════════════════════════════════════════════════
# 7. 便捷函数
# ════════════════════════════════════════════════════════════════

def run_league_evaluation(
    result: BacktestResult,
    df: pd.DataFrame,
    league_col: str = 'league_name',
    date_col: str = 'match_date',
    min_samples: int = 100,
    delta_threshold: float = 0.05,
    output_dir: Optional[str] = None,
    generate_report: bool = True,
) -> Tuple[LeagueEvaluationResult, Optional[str]]:
    """
    一键运行联赛差异化评估 + 可视化 + HTML 报告。

    Parameters
    ----------
    result : BacktestResult
        T17 回测结果
    df : pd.DataFrame
        原始数据 (需含 league_col 列)
    league_col : str
        联赛列名
    date_col : str
        日期列名
    min_samples : int
        每个联赛最少样本数
    delta_threshold : float
        薄弱环节识别阈值
    output_dir : str, optional
        输出目录
    generate_report : bool
        是否生成 HTML 报告

    Returns
    -------
    (LeagueEvaluationResult, report_path_or_None)
    """
    if output_dir is None:
        output_dir = os.path.join(DEFAULT_OUTPUT_DIR, 'league_eval')

    # 评估
    evaluator = LeagueEvaluator(min_samples=min_samples, delta_threshold=delta_threshold)
    league_result = evaluator.evaluate(result, df, league_col=league_col, date_col=date_col)

    # 可视化 + 报告
    report_path = None
    if generate_report:
        viz = LeagueVisualizer(output_dir=output_dir)
        chart_paths = viz.generate_all_charts(league_result)

        builder = LeagueReportBuilder(output_dir=output_dir)
        report_path = builder.generate(league_result, chart_paths=chart_paths)

    return league_result, report_path

# ════════════════════════════════════════════════════════════════
# 8. __main__ 测试
# ════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    print("=" * 70)
    print("  T18 League-Differentiated Evaluation — Real Data Test (Phase 0)")
    print("=" * 70)

    # ── Phase 0: 从 DB 加载真实数据，不再使用 np.random.choice ──
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'football_data.db')
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            query = """
                SELECT m.match_date, m.home_score, m.away_score, m.league_name,
                       mf.home_prob, mf.draw_prob, mf.away_prob
                FROM matches m
                JOIN match_features mf ON m.match_id = mf.match_id
                WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                ORDER BY m.match_date
            """
            df = pd.read_sql_query(query, conn)
            conn.close()

            if len(df) > 0:
                df['match_date'] = pd.to_datetime(df['match_date'])
                df['result_label'] = df.apply(
                    lambda r: 0 if r['home_score'] > r['away_score']
                    else (1 if r['home_score'] == r['away_score'] else 2), axis=1
                )
                for col in ['home_prob', 'draw_prob', 'away_prob']:
                    if col not in df.columns or df[col].isna().all():
                        df[col] = 1.0 / 3.0
                    else:
                        df[col] = df[col].fillna(1.0 / 3.0)
                row_sums = df[['home_prob', 'draw_prob', 'away_prob']].sum(axis=1)
                df['home_prob'] = (df['home_prob'] / row_sums.replace(0, 1)).clip(0.001, 0.999)
                df['draw_prob'] = (df['draw_prob'] / row_sums.replace(0, 1)).clip(0.001, 0.999)
                df['away_prob'] = 1.0 - df['home_prob'] - df['draw_prob']
                df['away_prob'] = df['away_prob'].clip(0.001, 0.999)
                print(f"  DB 真实数据: {len(df)} 条样本")
            else:
                raise ValueError("DB 无有效数据")
        else:
            raise FileNotFoundError(f"数据库未找到: {db_path}")
        use_synthetic = False
    except (Exception, sqlite3.Error, ImportError, FileNotFoundError, KeyError, ValueError) as e:
        print(f"  ⚠️ DB 数据加载失败 ({e})，使用 synthetic fallback (仅供结构验证)")
        use_synthetic = True

    if use_synthetic:
        np.random.seed(42)

        leagues_config = {
            'Premier League': {'n': 2000, 'h': 0.46, 'd': 0.26, 'a': 0.28, 'skill': 0.15},
            'La Liga':        {'n': 1800, 'h': 0.44, 'd': 0.28, 'a': 0.28, 'skill': 0.12},
            'Serie A':         {'n': 1700, 'h': 0.45, 'd': 0.27, 'a': 0.28, 'skill': 0.10},
            'Bundesliga':      {'n': 1500, 'h': 0.47, 'd': 0.24, 'a': 0.29, 'skill': 0.18},
            'Ligue 1':         {'n': 1400, 'h': 0.44, 'd': 0.26, 'a': 0.30, 'skill': 0.08},
        }

        all_dates, all_labels, all_probs, all_leagues = [], [], [], []
        base_date = pd.Timestamp('2021-01-01')

        for league, cfg in leagues_config.items():
            n = cfg['n']
            dates = pd.date_range(base_date, periods=n, freq='D')
            base_date = dates[-1] + pd.Timedelta(days=1)

            labels = np.random.choice([0, 1, 2], size=n, p=[cfg['h'], cfg['d'], cfg['a']])
            probs = np.random.dirichlet([3, 2, 2], size=n)
            for i in range(n):
                probs[i, labels[i]] += cfg['skill']
            probs = probs / probs.sum(axis=1, keepdims=True)

            all_dates.extend(dates)
            all_labels.extend(labels)
            all_probs.extend(probs)
            all_leagues.extend([league] * n)

        df = pd.DataFrame({
            'match_date': all_dates,
            'result_label': all_labels,
            'home_prob': [p[0] for p in all_probs],
            'draw_prob': [p[1] for p in all_probs],
            'away_prob': [p[2] for p in all_probs],
            'league_name': all_leagues,
        })

    # 先运行 T17 回测
    from optimize.walkforward_backtest import run_walkforward_backtest

    output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               'evaluation_results', 't18_test')
    os.makedirs(output_dir, exist_ok=True)

    print("\n[Step 1] Running T17 backtest...")
    bt_result, _ = run_walkforward_backtest(
        df, prob_cols=['home_prob', 'draw_prob', 'away_prob'],
        freq='quarter', window='expanding',
        output_dir=output_dir, generate_report=False
    )
    bt_summary = bt_result.summary()
    print(f"  Folds: {bt_summary.get('n_folds', 0)}, Overall Accuracy: {bt_summary.get('overall_accuracy', 0):.4f}")

    # 运行 T18 联赛评估
    print("\n[Step 2] Running T18 league evaluation...")
    league_result, report_path = run_league_evaluation(
        bt_result, df, output_dir=output_dir, generate_report=True
    )

    # 打印结果
    print(f"\n  Leagues evaluated: {league_result.n_leagues}")
    print(f"  Weak spots: {len(league_result.weak_spots)}")
    print(f"  Suggestions: {len(league_result.suggestions)}")

    # 联赛指标
    print("\n  ── League Metrics ──")
    for league, lm in league_result.league_metrics.items():
        delta_str = f"({lm.accuracy_delta:+.4f})" if lm.accuracy_delta != 0 else ""
        print(f"  {league:20s}: Acc={lm.accuracy:.4f} {delta_str:12s} Brier={lm.brier:.4f} "
              f"ECE={lm.ece:.4f} MCC={lm.mcc:.4f} N={lm.n_matches}")

    # 薄弱环节
    if league_result.weak_spots:
        print("\n  ── Weak Spots ──")
        for ws in league_result.weak_spots[:10]:
            print(f"  [{ws.severity:8s}] {ws.league:20s} {ws.metric:15s}: "
                  f"{ws.value:.4f} (global: {ws.global_avg:.4f}, delta: {ws.delta:+.4f})")

    # 建议
    if league_result.suggestions:
        print("\n  ── Top Suggestions ──")
        for s in league_result.suggestions[:5]:
            print(f"  [{s['priority']:8s}] {s['league']} — {s['type']}")
            print(f"    Problem: {s['problem'][:80]}")

    # 保存 CSV
    csv_path = os.path.join(output_dir, 'league_metrics.csv')
    league_result.to_dataframe().to_csv(csv_path, index=False, encoding='utf-8-sig')
    print(f"\n  Saved league metrics CSV: {csv_path}")

    ws_csv_path = os.path.join(output_dir, 'weak_spots.csv')
    league_result.weak_spots_dataframe().to_csv(ws_csv_path, index=False, encoding='utf-8-sig')
    print(f"  Saved weak spots CSV: {ws_csv_path}")

    print(f"\n  Report: {report_path}")
    print("\n" + "=" * 70)
    print("  T18 Synthetic Test Complete!")
    print("=" * 70)

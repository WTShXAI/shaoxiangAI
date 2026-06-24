"""
哨响AI - 类别权重网格搜索优化器 (T18)
===========================================
替代简单的 draw_weight_multiplier=1.15 固定值，
使用 TimeSeriesSplit + GridSearch 自动搜索最优三分类权重组合。

核心思路:
  1. 基础权重 = compute_class_weight('balanced')
  2. 搜索空间 = home_mult × base + draw_mult × base + away_mult × base
  3. 归一化 → sample_weight
  4. CV评估 → 复合得分排序

用法:
    from optimize.class_weight_optimizer import ClassWeightOptimizer
    opt = ClassWeightOptimizer()
    result = opt.optimize(X, y, dates)
    # result.optimal_weights → {'home': 0.9, 'draw': 1.4, 'away': 0.8}
    # result.optimal_composite → 52.3
"""

import logging
import time
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import (
        accuracy_score, brier_score_loss, log_loss,
        confusion_matrix, matthews_corrcoef,
    )
    from sklearn.utils.class_weight import compute_class_weight
except ImportError as e:
    raise ImportError(f"scikit-learn 依赖缺失: {e}")

try:
    import xgboost as xgb
    XGB_AVAILABLE = True
except ImportError:
    XGB_AVAILABLE = False


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════

@dataclass
class WeightSearchResult:
    """单组权重的搜索结果"""
    home_mult: float
    draw_mult: float
    away_mult: float
    normalized_weights: Dict[str, float]  # {'home': w0, 'draw': w1, 'away': w2}
    cv_accuracy: float
    cv_draw_recall: float
    cv_draw_precision: float
    cv_draw_f1: float
    cv_home_recall: float
    cv_away_recall: float
    cv_brier: float
    cv_log_loss: float
    cv_mcc: float
    cv_pred_draw_pct: float
    cv_actual_draw_pct: float
    composite_score: float
    fold_scores: List[float] = field(default_factory=list)
    fold_details: List[Dict] = field(default_factory=list)


@dataclass
class OptimizationResult:
    """完整的优化结果"""
    optimal_result: WeightSearchResult
    all_results: List[WeightSearchResult]
    search_space: Dict
    search_time_seconds: float
    n_combinations: int
    n_valid_folds: int

    def top_n(self, n: int = 5) -> List[WeightSearchResult]:
        """返回 Top-N 结果"""
        return sorted(self.all_results, key=lambda r: r.composite_score, reverse=True)[:n]

    def summary(self) -> str:
        """生成优化摘要"""
        best = self.optimal_result
        lines = [
            "=" * 60,
            "  类别权重优化结果 (T18 GridSearch)",
            "=" * 60,
            f"  搜索组合数: {self.n_combinations}",
            f"  有效折数:   {self.n_valid_folds}",
            f"  搜索耗时:   {self.search_time_seconds:.1f}s",
            "",
            "  [最优权重]",
            f"    home_mult = {best.home_mult:.2f}  →  归一化权重 = {best.normalized_weights['home']:.3f}",
            f"    draw_mult = {best.draw_mult:.2f}  →  归一化权重 = {best.normalized_weights['draw']:.3f}",
            f"    away_mult = {best.away_mult:.2f}  →  归一化权重 = {best.normalized_weights['away']:.3f}",
            "",
            "  [最优性能]",
            f"    Accuracy:       {best.cv_accuracy*100:.1f}%",
            f"    Draw Recall:    {best.cv_draw_recall*100:.1f}%",
            f"    Draw Precision: {best.cv_draw_precision*100:.1f}%",
            f"    Draw F1:        {best.cv_draw_f1:.3f}",
            f"    Home Recall:    {best.cv_home_recall*100:.1f}%",
            f"    Away Recall:    {best.cv_away_recall*100:.1f}%",
            f"    Brier:          {best.cv_brier:.4f}",
            f"    Log Loss:       {best.cv_log_loss:.4f}",
            f"    MCC:            {best.cv_mcc:.4f}",
            f"    复合得分:       {best.composite_score:.2f}",
            "",
            "  [Top-5 候选]",
        ]
        for i, r in enumerate(self.top_n(5)):
            lines.append(
                f"    #{i+1} h={r.home_mult:.2f} d={r.draw_mult:.2f} a={r.away_mult:.2f} "
                f"→ acc={r.cv_accuracy*100:.1f}% dr={r.cv_draw_recall*100:.1f}% "
                f"score={r.composite_score:.1f}"
            )
        lines.append("=" * 60)
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# 核心优化器
# ════════════════════════════════════════════════════════════════

class ClassWeightOptimizer:
    """
    三分类权重网格搜索优化器。

    搜索策略:
      - home_mult:  [0.7, 0.8, 0.9, 1.0, 1.1, 1.2]
      - draw_mult:  [0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0]
      - away_mult:  [0.7, 0.8, 0.9, 1.0, 1.1, 1.2]
      总计: 6 × 7 × 6 = 252 组合

    CV: TimeSeriesSplit(n_splits=5)

    复合得分:
      composite = accuracy × 0.40 + draw_recall × 0.30 + draw_precision × 0.20 + mcc × 0.10
    """

    # 默认搜索空间
    DEFAULT_HOME_MULTS = [0.7, 0.8, 0.9, 1.0, 1.1, 1.2]
    DEFAULT_DRAW_MULTS = [0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0]
    DEFAULT_AWAY_MULTS = [0.7, 0.8, 0.9, 1.0, 1.1, 1.2]

    def __init__(
        self,
        xgb_params: Optional[Dict] = None,
        n_splits: int = 5,
        random_state: int = 42,
        device: str = 'cpu',
        composite_weights: Optional[Dict[str, float]] = None,
    ):
        """
        Parameters
        ----------
        xgb_params : dict, optional
            XGBoost 训练参数 (不含 sample_weight 相关)
        n_splits : int
            TimeSeriesSplit 折数
        random_state : int
            随机种子
        device : str
            XGBoost 设备: 'cpu' 或 'cuda'
        composite_weights : dict, optional
            复合得分各项权重, 默认:
            {'accuracy': 0.40, 'draw_recall': 0.30, 'draw_precision': 0.20, 'mcc': 0.10}
        """
        self.xgb_params = xgb_params or {
            'objective': 'multi:softprob',
            'num_class': 3,
            'eval_metric': ['mlogloss'],
            'n_estimators': 300,
            'max_depth': 5,
            'learning_rate': 0.05,
            'subsample': 0.8,
            'colsample_bytree': 0.8,
            'min_child_weight': 3,
            'reg_alpha': 0.1,
            'reg_lambda': 1.0,
            'gamma': 0.05,
            'verbosity': 0,
            'n_jobs': -1,
            'tree_method': 'hist',
            'device': device,
            'random_state': random_state,
        }
        self.n_splits = n_splits
        self.random_state = random_state
        self.device = device
        self.composite_weights = composite_weights or {
            'accuracy': 0.40,
            'draw_recall': 0.30,
            'draw_precision': 0.20,
            'mcc': 0.10,
        }

    def optimize(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        dates: Optional[pd.Series] = None,
        home_mults: Optional[List[float]] = None,
        draw_mults: Optional[List[float]] = None,
        away_mults: Optional[List[float]] = None,
        verbose: bool = True,
    ) -> OptimizationResult:
        """
        执行网格搜索优化。

        Parameters
        ----------
        X : pd.DataFrame
            特征矩阵
        y : pd.Series
            标签 (0=主胜, 1=平局, 2=客胜)
        dates : pd.Series, optional
            日期列, 用于时序排序 (不提供则假定已排序)
        home_mults : list of float, optional
        draw_mults : list of float, optional
        away_mults : list of float, optional
        verbose : bool

        Returns
        -------
        OptimizationResult
        """
        if not XGB_AVAILABLE:
            raise RuntimeError("XGBoost 未安装!")

        t0 = time.time()

        home_mults = home_mults or self.DEFAULT_HOME_MULTS
        draw_mults = draw_mults or self.DEFAULT_DRAW_MULTS
        away_mults = away_mults or self.DEFAULT_AWAY_MULTS

        # 确保按时间排序
        if dates is not None:
            sort_idx = np.argsort(dates)
            X = X.iloc[sort_idx].reset_index(drop=True)
            y = y.iloc[sort_idx].reset_index(drop=True)
        else:
            X = X.reset_index(drop=True)
            y = y.reset_index(drop=True)

        # 基础类别权重
        classes = np.array([0, 1, 2])
        base_weights = compute_class_weight('balanced', classes=classes, y=y)
        # base_weights = [w_home, w_draw, w_away]

        # 构建搜索组合
        combinations = [
            (hm, dm, am)
            for hm in home_mults
            for dm in draw_mults
            for am in away_mults
        ]
        n_combinations = len(combinations)

        if verbose:
            logger.info(f"类别权重搜索空间: {len(home_mults)}×{len(draw_mults)}×{len(away_mults)} = {n_combinations} 组合")

        all_results: List[WeightSearchResult] = []

        for idx, (hm, dm, am) in enumerate(combinations):
            # 计算多乘子权重
            raw_weights = base_weights.copy()
            raw_weights[0] *= hm
            raw_weights[1] *= dm
            raw_weights[2] *= am
            # 归一化：除以均值保持相对比例
            normalized = raw_weights / raw_weights.mean()

            # 时序交叉验证
            cv_metrics = self._time_series_cv_evaluate(
                X, y, normalized, verbose=False
            )

            if cv_metrics is None:
                continue  # 数据太少，跳过

            # 计算复合得分
            cw = self.composite_weights
            composite = (
                cv_metrics['mean_accuracy'] * cw['accuracy'] +
                cv_metrics['mean_draw_recall'] * cw['draw_recall'] +
                cv_metrics['mean_draw_precision'] * cw['draw_precision'] +
                cv_metrics['mean_mcc'] * cw['mcc']
            )

            result = WeightSearchResult(
                home_mult=hm,
                draw_mult=dm,
                away_mult=am,
                normalized_weights={
                    'home': round(float(normalized[0]), 4),
                    'draw': round(float(normalized[1]), 4),
                    'away': round(float(normalized[2]), 4),
                },
                cv_accuracy=cv_metrics['mean_accuracy'],
                cv_draw_recall=cv_metrics['mean_draw_recall'],
                cv_draw_precision=cv_metrics['mean_draw_precision'],
                cv_draw_f1=cv_metrics['mean_draw_f1'],
                cv_home_recall=cv_metrics['mean_home_recall'],
                cv_away_recall=cv_metrics['mean_away_recall'],
                cv_brier=cv_metrics['mean_brier'],
                cv_log_loss=cv_metrics['mean_log_loss'],
                cv_mcc=cv_metrics['mean_mcc'],
                cv_pred_draw_pct=cv_metrics['mean_pred_draw_pct'],
                cv_actual_draw_pct=cv_metrics['mean_actual_draw_pct'],
                composite_score=round(composite, 2),
                fold_scores=cv_metrics.get('fold_composite_scores', []),
                fold_details=cv_metrics.get('fold_details', []),
            )
            all_results.append(result)

            if verbose and (idx + 1) % 30 == 0:
                logger.info(f"  进度: {idx+1}/{n_combinations} ... "
                            f"当前最佳: h={all_results[0].home_mult:.2f} "
                            f"d={all_results[0].draw_mult:.2f} "
                            f"a={all_results[0].away_mult:.2f} "
                            f"score={all_results[0].composite_score:.1f}")

            # 动态排序，保持 best-first
            all_results.sort(key=lambda r: r.composite_score, reverse=True)

        elapsed = time.time() - t0
        best = all_results[0]

        if verbose:
            logger.info(f"\n[OK] 搜索完成! 耗时 {elapsed:.1f}s")
            logger.info(f"   最优: h={best.home_mult:.2f} d={best.draw_mult:.2f} a={best.away_mult:.2f}")
            logger.info(f"   归一化权重: {best.normalized_weights}")
            logger.info(f"   复合得分: {best.composite_score:.1f}")
            logger.info(f"   Acc={best.cv_accuracy*100:.1f}% DR={best.cv_draw_recall*100:.1f}% "
                        f"DP={best.cv_draw_precision*100:.1f}% DF1={best.cv_draw_f1:.3f}")

        return OptimizationResult(
            optimal_result=best,
            all_results=all_results,
            search_space={
                'home_mults': home_mults,
                'draw_mults': draw_mults,
                'away_mults': away_mults,
            },
            search_time_seconds=elapsed,
            n_combinations=n_combinations,
            n_valid_folds=self.n_splits,
        )

    def _time_series_cv_evaluate(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        normalized_weights: np.ndarray,
        verbose: bool = False,
    ) -> Optional[Dict]:
        """执行时序交叉验证并返回汇总指标"""
        try:
            tscv = TimeSeriesSplit(n_splits=self.n_splits)
        except ValueError:
            # 数据不够 split
            return None

        fold_metrics = {
            'accuracy': [], 'draw_recall': [], 'draw_precision': [],
            'draw_f1': [], 'home_recall': [], 'away_recall': [],
            'brier': [], 'log_loss': [], 'mcc': [],
            'pred_draw_pct': [], 'actual_draw_pct': [],
            'composite': [],
        }
        fold_details = []

        for train_idx, val_idx in tscv.split(X):
            X_tr, X_val = X.iloc[train_idx], X.iloc[val_idx]
            y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]

            if len(X_val) < 10:
                continue

            # sample weights
            sample_weights = np.array([normalized_weights[int(c)] for c in y_tr])

            # 标准化
            scaler = StandardScaler()
            X_tr_scaled = scaler.fit_transform(X_tr)
            X_val_scaled = scaler.transform(X_val)

            # 训练
            model = xgb.XGBClassifier(**self.xgb_params)
            model.fit(
                X_tr_scaled, y_tr,
                sample_weight=sample_weights,
                verbose=False,
            )

            # 预测
            y_pred = model.predict(X_val_scaled)
            y_proba = model.predict_proba(X_val_scaled)

            # 指标
            acc = accuracy_score(y_val, y_pred)
            cm = confusion_matrix(y_val, y_pred, labels=[0, 1, 2])
            diag = cm.diagonal()
            row_sums = cm.sum(axis=1).clip(min=1)

            home_recall = diag[0] / row_sums[0] if len(row_sums) > 0 else 0
            draw_recall = diag[1] / row_sums[1] if len(row_sums) > 1 else 0
            away_recall = diag[2] / row_sums[2] if len(row_sums) > 2 else 0

            # Draw precision
            draw_col_sum = cm[:, 1].sum()
            draw_precision = diag[1] / draw_col_sum if draw_col_sum > 0 else 0
            draw_f1 = (
                2 * draw_precision * draw_recall / (draw_precision + draw_recall)
                if (draw_precision + draw_recall) > 0 else 0
            )

            # Brier
            y_onehot = np.zeros((len(y_val), 3))
            for i, c in enumerate(y_val):
                y_onehot[i, int(c)] = 1
            brier = np.mean([
                brier_score_loss(y_onehot[:, i], y_proba[:, i]) for i in range(3)
            ])
            ll = log_loss(y_val, y_proba, labels=[0, 1, 2])

            try:
                mcc = matthews_corrcoef(y_val, y_pred)
            except (Exception, ValueError, KeyError, IndexError):
                mcc = 0.0

            pred_draw_pct = np.mean(y_pred == 1) * 100
            actual_draw_pct = np.mean(y_val == 1) * 100

            cw = self.composite_weights
            composite = (
                acc * cw['accuracy'] +
                draw_recall * cw['draw_recall'] +
                draw_precision * cw['draw_precision'] +
                mcc * cw['mcc']
            )

            fold_metrics['accuracy'].append(acc)
            fold_metrics['draw_recall'].append(draw_recall)
            fold_metrics['draw_precision'].append(draw_precision)
            fold_metrics['draw_f1'].append(draw_f1)
            fold_metrics['home_recall'].append(home_recall)
            fold_metrics['away_recall'].append(away_recall)
            fold_metrics['brier'].append(brier)
            fold_metrics['log_loss'].append(ll)
            fold_metrics['mcc'].append(mcc)
            fold_metrics['pred_draw_pct'].append(pred_draw_pct)
            fold_metrics['actual_draw_pct'].append(actual_draw_pct)
            fold_metrics['composite'].append(composite)

            fold_details.append({
                'n_train': len(X_tr),
                'n_val': len(X_val),
                'accuracy': round(acc, 4),
                'draw_recall': round(draw_recall, 4),
                'draw_precision': round(draw_precision, 4),
                'mcc': round(mcc, 4),
                'composite': round(composite, 2),
            })

        if not fold_metrics['accuracy']:
            return None

        # 汇总
        result = {}
        for k, v in fold_metrics.items():
            if k == 'fold_details':
                continue
            if v:
                result[f'mean_{k}'] = round(float(np.mean(v)), 4)
                result[f'std_{k}'] = round(float(np.std(v)), 4)

        result['fold_composite_scores'] = fold_metrics['composite']
        result['fold_details'] = fold_details

        return result


# ════════════════════════════════════════════════════════════════
# 便捷函数
# ════════════════════════════════════════════════════════════════

def optimize_class_weights(
    X: pd.DataFrame,
    y: pd.Series,
    dates: Optional[pd.Series] = None,
    xgb_params: Optional[Dict] = None,
    n_splits: int = 5,
    device: str = 'cpu',
    verbose: bool = True,
    home_mults: Optional[List[float]] = None,
    draw_mults: Optional[List[float]] = None,
    away_mults: Optional[List[float]] = None,
) -> OptimizationResult:
    """
    一键优化类别权重。

    Parameters
    ----------
    X : pd.DataFrame
    y : pd.Series (0=home, 1=draw, 2=away)
    dates : pd.Series, optional
    xgb_params : dict, optional
    n_splits : int
    device : str
    verbose : bool
    home_mults : list of float, optional
    draw_mults : list of float, optional
    away_mults : list of float, optional

    Returns
    -------
    OptimizationResult
    """
    optimizer = ClassWeightOptimizer(
        xgb_params=xgb_params,
        n_splits=n_splits,
        device=device,
    )
    return optimizer.optimize(
        X, y, dates=dates, verbose=verbose,
        home_mults=home_mults, draw_mults=draw_mults, away_mults=away_mults,
    )


def get_optimal_sample_weights(
    y: np.ndarray,
    home_mult: float = 1.0,
    draw_mult: float = 1.15,
    away_mult: float = 1.0,
) -> np.ndarray:
    """
    根据优化后的乘子计算 sample_weight 数组。

    Parameters
    ----------
    y : np.ndarray
        标签数组 (0=home, 1=draw, 2=away)
    home_mult : float
        主胜乘子
    draw_mult : float
        平局乘子
    away_mult : float
        客胜乘子

    Returns
    -------
    np.ndarray of sample weights
    """
    classes = np.array([0, 1, 2])
    base_weights = compute_class_weight('balanced', classes=classes, y=y)
    base_weights[0] *= home_mult
    base_weights[1] *= draw_mult
    base_weights[2] *= away_mult
    normalized = base_weights / base_weights.mean()
    return np.array([normalized[int(c)] for c in y])


# ════════════════════════════════════════════════════════════════
# 模拟数据测试 (独立运行用)
# ════════════════════════════════════════════════════════════════

def _run_synthetic_test():
    """模拟数据测试"""
    np.random.seed(42)

    # 生成3年数据
    n = 3000
    dates = pd.date_range('2021-01-01', periods=n, freq='D')
    labels = np.random.choice([0, 1, 2], size=n, p=[0.46, 0.26, 0.28])

    # 生成有区分度的特征
    X = pd.DataFrame()
    for i in range(19):
        col = f'feature_{i}'
        # 各类别有不同的均值
        means = {0: 0.2, 1: 0.0, 2: -0.2}
        X[col] = [np.random.normal(means[l], 0.5) for l in labels]

    y = pd.Series(labels)

    print(f"\n  模拟数据: {n} 条, {19} 特征")
    print(f"   主胜: {(y==0).sum()} ({(y==0).mean()*100:.1f}%)")
    print(f"   平局: {(y==1).sum()} ({(y==1).mean()*100:.1f}%)")
    print(f"   客胜: {(y==2).sum()} ({(y==2).mean()*100:.1f}%)")
    print()

    print("[搜索] 快速搜索 (36 组合)...")

    # 快速搜索: 减小空间
    result = optimize_class_weights(
        X, y, dates=dates,
        home_mults=[0.8, 1.0, 1.2],
        draw_mults=[0.8, 1.0, 1.2, 1.5],
        away_mults=[0.8, 1.0, 1.2],
        xgb_params={
            'objective': 'multi:softprob',
            'num_class': 3,
            'eval_metric': ['mlogloss'],
            'n_estimators': 100,
            'max_depth': 4,
            'learning_rate': 0.1,
            'subsample': 0.8,
            'verbosity': 0,
            'n_jobs': -1,
            'tree_method': 'hist',
            'random_state': 42,
        },
        n_splits=3,
        verbose=True,
    )

    print()
    print(result.summary())


# ════════════════════════════════════════════════════════════════
# __main__ 独立运行
# ════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
    )

    print("=" * 70)
    print("  T18 类别权重优化器 -- 独立测试")
    print("=" * 70)

    # 尝试加载真实数据
    try:
        PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(PROJECT_ROOT, 'data', 'football_data.db')

        if os.path.exists(db_path):
            import sqlite3
            import yaml
            conn = sqlite3.connect(db_path)

            # 从 config.yaml 读取 feature_columns
            cfg_path = os.path.join(PROJECT_ROOT, 'config.yaml')
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f)
            FEATURE_COLS = cfg['data']['feature_columns']
            DEFAULT_VALUES = cfg['data']['default_values']

            cols_sql = ", ".join([f"mf.{c}" for c in FEATURE_COLS])
            query = f"""
            SELECT m.match_date, m.home_score, m.away_score, {cols_sql}
            FROM matches m
            JOIN match_features mf ON m.match_id = mf.match_id
            WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            ORDER BY m.match_date
            """
            df = pd.read_sql_query(query, conn)
            conn.close()

            print(f"\n  真实数据: {len(df)} 条样本")

            # 特征（用 config.yaml 默认值填充）
            X = df[FEATURE_COLS].fillna(DEFAULT_VALUES)

            # 标签
            def calc_label(row):
                if row['home_score'] > row['away_score']:
                    return 0
                elif row['home_score'] == row['away_score']:
                    return 1
                else:
                    return 2
            y = df.apply(calc_label, axis=1)
            dates = pd.to_datetime(df['match_date'])

            n_total = len(y)
            print(f"   主胜: {(y==0).sum()} ({(y==0).mean()*100:.1f}%)")
            print(f"   平局: {(y==1).sum()} ({(y==1).mean()*100:.1f}%)")
            print(f"   客胜: {(y==2).sum()} ({(y==2).mean()*100:.1f}%)")
            print(f"   日期范围: {dates.min().date()} ~ {dates.max().date()}")
            print()

            # 使用GriD搜索
            print("[搜索] GridSearch优化 (72 组合)...")
            result = optimize_class_weights(
                X, y, dates=dates,
                home_mults=[0.7, 0.8, 0.9, 1.0, 1.1, 1.2],
                draw_mults=[0.8, 1.0, 1.2, 1.4, 1.6],
                away_mults=[0.7, 0.8, 0.9, 1.0, 1.1, 1.2],
                xgb_params={
                    'objective': 'multi:softprob',
                    'num_class': 3,
                    'eval_metric': ['mlogloss'],
                    'n_estimators': 200,
                    'max_depth': 5,
                    'learning_rate': 0.05,
                    'subsample': 0.8,
                    'colsample_bytree': 0.8,
                    'min_child_weight': 3,
                    'reg_alpha': 0.1,
                    'reg_lambda': 1.0,
                    'gamma': 0.05,
                    'verbosity': 0,
                    'n_jobs': -1,
                    'tree_method': 'hist',
                    'random_state': 42,
                },
                n_splits=5,
                verbose=True,
            )

            print()
            print(result.summary())

            # 保存结果
            output_dir = os.path.join(PROJECT_ROOT, 'evaluation_results')
            os.makedirs(output_dir, exist_ok=True)

            best = result.optimal_result
            result_json = {
                'optimal_weights': {
                    'home_mult': best.home_mult,
                    'draw_mult': best.draw_mult,
                    'away_mult': best.away_mult,
                    'normalized': best.normalized_weights,
                },
                'performance': {
                    'accuracy': round(best.cv_accuracy, 4),
                    'draw_recall': round(best.cv_draw_recall, 4),
                    'draw_precision': round(best.cv_draw_precision, 4),
                    'draw_f1': round(best.cv_draw_f1, 4),
                    'home_recall': round(best.cv_home_recall, 4),
                    'away_recall': round(best.cv_away_recall, 4),
                    'brier': round(best.cv_brier, 4),
                    'log_loss': round(best.cv_log_loss, 4),
                    'mcc': round(best.cv_mcc, 4),
                    'composite_score': best.composite_score,
                },
                'top5_candidates': [
                    {
                        'home_mult': r.home_mult,
                        'draw_mult': r.draw_mult,
                        'away_mult': r.away_mult,
                        'composite_score': r.composite_score,
                        'accuracy': round(r.cv_accuracy, 4),
                        'draw_recall': round(r.cv_draw_recall, 4),
                    }
                    for r in result.top_n(5)
                ],
                'search_time_seconds': result.search_time_seconds,
                'n_combinations': result.n_combinations,
            }

            output_path = os.path.join(output_dir, 'class_weight_optimization.json')
            import json as json_lib
            with open(output_path, 'w', encoding='utf-8') as f:
                json_lib.dump(result_json, f, indent=2, ensure_ascii=False)
            print(f"\n  [OK] 结果已保存: {output_path}")

        else:
            print(f"  数据库未找到: {db_path}")
            print("  使用模拟数据测试...")
            _run_synthetic_test()

    except (Exception, ValueError, KeyError, IndexError, IOError, FileNotFoundError) as e:
        print(f"  真实数据加载失败 ({e}), 使用模拟数据测试...")
        _run_synthetic_test()

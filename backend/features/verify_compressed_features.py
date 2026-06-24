import logging
"""
验证压缩特征质量 — 对比原始特征和压缩特征的差异
"""
import argparse
import sys
import os
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import accuracy_score, f1_score
import xgboost as xgb
import warnings
logger = logging.getLogger(__name__)
warnings.filterwarnings('ignore')

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

META_COLS = ['date', 'home_team', 'away_team', 'home_score', 'away_score', 'league']


def load_and_prepare(df: pd.DataFrame):
    """准备训练数据"""
    feature_cols = [c for c in df.columns if c not in META_COLS and df[c].dtype in
                    ['float64', 'float32', 'int64', 'int32']]

    X = df[feature_cols].fillna(0).values.astype(np.float32)

    hs = df['home_score'].values
    aws = df['away_score'].values
    y = np.full(len(df), 1, dtype=int)
    y[hs > aws] = 0
    y[hs < aws] = 2

    return X, y, feature_cols


def quick_cv_benchmark(X, y, n_folds=5) -> dict:
    """快速时序CV基准测试"""
    tscv = TimeSeriesSplit(n_splits=n_folds)
    scores = {'accuracy': [], 'f1_macro': []}

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X), 1):
        X_train, X_val = X[train_idx], X[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]

        model = xgb.XGBClassifier(
            n_estimators=150, max_depth=5, learning_rate=0.05,
            subsample=0.8, random_state=42, n_jobs=-1, verbosity=0,
        )
        model.fit(X_train, y_train)
        y_pred = model.predict(X_val)

        scores['accuracy'].append(accuracy_score(y_val, y_pred))
        scores['f1_macro'].append(f1_score(y_val, y_pred, average='macro'))

    return {
        'accuracy_mean': np.mean(scores['accuracy']),
        'accuracy_std': np.std(scores['accuracy']),
        'f1_macro_mean': np.mean(scores['f1_macro']),
        'f1_macro_std': np.std(scores['f1_macro']),
    }


def main():
    parser = argparse.ArgumentParser(description='验证压缩特征质量')
    parser.add_argument('--original', type=int, default=58, help='原始特征数')
    parser.add_argument('--compressed', type=int, default=35, help='压缩特征数')
    parser.add_argument('--original-csv', default='data/enhanced_features_v1.csv',
                        help='原始CSV路径')
    parser.add_argument('--compressed-csv', default='data/features_compressed_v1.csv',
                        help='压缩CSV路径')
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("🔍 压缩特征验证")
    logger.info(f"  原始: {args.original} → 压缩: {args.compressed}")
    logger.info("=" * 60)

    # 加载数据
    logger.info(f"\n[1/4] 加载原始数据...")
    df_orig = pd.read_csv(args.original_csv)
    logger.info(f"  {len(df_orig)} 行 × {len(df_orig.columns)} 列")

    logger.info(f"\n[2/4] 加载压缩数据...")
    if not os.path.exists(args.compressed_csv):
        logger.info(f"  ❌ 压缩文件不存在: {args.compressed_csv}")
        return 1
    df_comp = pd.read_csv(args.compressed_csv)
    logger.info(f"  {len(df_comp)} 行 × {len(df_comp.columns)} 列")

    # 统计
    orig_feat = [c for c in df_orig.columns
                 if c not in META_COLS and df_orig[c].dtype in
                 ['float64', 'float32', 'int64', 'int32']]
    comp_feat = [c for c in df_comp.columns
                 if c not in META_COLS and df_comp[c].dtype in
                 ['float64', 'float32', 'int64', 'int32']]

    overlap = set(orig_feat) & set(comp_feat)
    dropped = set(orig_feat) - set(comp_feat)

    logger.info(f"\n[3/4] 统计分析")
    logger.info(f"  原始特征数: {len(orig_feat)}")
    logger.info(f"  压缩特征数: {len(comp_feat)}")
    logger.info(f"  覆盖特征数: {len(overlap)} ({len(overlap)/len(comp_feat)*100:.1f}%)")
    logger.info(f"  丢弃特征数: {len(dropped)}")
    logger.info(f"  压缩率: {(1-len(comp_feat)/len(orig_feat))*100:.1f}%")
    if dropped:
        logger.info(f"  丢弃的关键特征: {list(dropped)[:8]}")

    # 性能基准
    logger.info(f"\n[4/4] 性能基准测试")

    logger.info("  → 原始特征基准...")
    X_orig, y_orig, _ = load_and_prepare(df_orig)
    orig_scores = quick_cv_benchmark(X_orig, y_orig)

    logger.info("  → 压缩特征基准...")
    X_comp, y_comp, _ = load_and_prepare(df_comp)
    comp_scores = quick_cv_benchmark(X_comp, y_comp)

    acc_diff = comp_scores['accuracy_mean'] - orig_scores['accuracy_mean']
    f1_diff = comp_scores['f1_macro_mean'] - orig_scores['f1_macro_mean']

    logger.info(f"\n{'='*60}")
    logger.info(f"{'指标':<20} {'原始(58)':>12} {'压缩(35)':>12} {'差异':>10}")
    logger.info(f"{'-'*60}")
    logger.info(f"{'Accuracy':<20} {orig_scores['accuracy_mean']:>12.4f} "
          f"{comp_scores['accuracy_mean']:>12.4f} {acc_diff:>+10.4f}")
    logger.info(f"{'F1 Macro':<20} {orig_scores['f1_macro_mean']:>12.4f} "
          f"{comp_scores['f1_macro_mean']:>12.4f} {f1_diff:>+10.4f}")
    logger.info(f"{'='*60}")

    # 判定
    threshold = -0.01  # 允许1%准确率损失
    if acc_diff >= threshold:
        logger.info(f"\n✅ 验证通过！准确率损失 {abs(acc_diff)*100:.2f}% < 1%，可接受。")
        return 0
    else:
        logger.info(f"\n⚠️ 准确率损失 {abs(acc_diff)*100:.2f}% > 1%，建议调整特征选择。")
        return 1


if __name__ == '__main__':
    sys.exit(main())

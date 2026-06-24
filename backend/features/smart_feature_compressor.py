import logging
"""
智能特征压缩器 — 将58维特征压缩到目标维度
方法: XGBoost特征重要性 + 相关性去重 + 递归消除
"""
import argparse
import sys
import os
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.preprocessing import LabelEncoder
import joblib
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from backend.models.train_enhanced import ALL_CANDIDATE_FEATURES, LEAKY_FEATURE_PATTERNS
logger = logging.getLogger(__name__)

# 非特征列（不参与训练，但保留在输出中）
META_COLS = ['date', 'home_team', 'away_team', 'home_score', 'away_score', 'league']


def load_data(csv_path: str) -> pd.DataFrame:
    logger.info(f"[LOAD] 读取 {csv_path}...")
    df = pd.read_csv(csv_path)
    logger.info(f"  → {len(df)} 行 × {len(df.columns)} 列")
    return df


def identify_feature_cols(df: pd.DataFrame) -> List[str]:
    """识别可用于训练的特征列（排除元数据列和泄漏特征）"""
    feature_cols = []
    for col in df.columns:
        if col in META_COLS:
            continue
        # 排除泄漏特征模式
        is_leaky = any(pat in col.lower() for pat in LEAKY_FEATURE_PATTERNS)
        if is_leaky:
            continue
        # 必须是数值型
        if df[col].dtype in ['float64', 'float32', 'int64', 'int32']:
            feature_cols.append(col)
    return feature_cols


def get_feature_importance(
    df: pd.DataFrame, feature_cols: List[str], n_estimators: int = 200
) -> pd.DataFrame:
    """用XGBoost计算特征重要性"""
    logger.info(f"\n[IMPORTANCE] XGBoost 训练 ({n_estimators} trees)...")

    X = df[feature_cols].fillna(0).values.astype(np.float32)

    # 从 home_score/away_score 推导标签
    hs = df['home_score'].values
    aws = df['away_score'].values
    y = np.full(len(df), 1, dtype=int)  # default D
    y[hs > aws] = 0  # H
    y[hs < aws] = 2  # A

    model = xgb.XGBClassifier(
        n_estimators=n_estimators,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        n_jobs=-1,
        verbosity=0,
    )
    model.fit(X, y)

    importance = pd.DataFrame({
        'feature': feature_cols,
        'importance': model.feature_importances_,
    }).sort_values('importance', ascending=False).reset_index(drop=True)

    logger.info(f"  Top-10 features:")
    for _, row in importance.head(10).iterrows():
        logger.info(f"    {row['feature']:40s} {row['importance']:.6f}")

    return importance


def correlation_pruning(
    df: pd.DataFrame, feature_cols: List[str], threshold: float = 0.95
) -> List[str]:
    """移除高相关性特征（保留重要性更高的）"""
    logger.info(f"\n[CORR] 相关性去重 (阈值={threshold})...")

    corr_matrix = df[feature_cols].corr().abs()
    upper = corr_matrix.where(np.triu(np.ones(corr_matrix.shape), k=1).astype(bool))

    removed = []
    for col in upper.columns:
        if col in removed:
            continue
        high_corr = upper.index[upper[col] > threshold].tolist()
        for hc in high_corr:
            if hc not in removed:
                removed.append(hc)

    keep = [f for f in feature_cols if f not in removed]
    logger.info(f"  移除 {len(removed)} 个高相关特征，保留 {len(keep)} 个")
    if removed:
        logger.info(f"  移除: {removed[:10]}{'...' if len(removed) > 10 else ''}")
    return keep


def compress_features(
    df: pd.DataFrame,
    feature_cols: List[str],
    importance: pd.DataFrame,
    target_n: int = 35,
) -> List[str]:
    """选择最终的目标特征集"""
    logger.info(f"\n[COMPRESS] 选择 top-{target_n} 特征...")

    # 按重要性排序，排除已移除的高相关特征
    imp_map = dict(zip(importance['feature'], importance.index))
    available = [f for f in feature_cols if f in imp_map]
    available.sort(key=lambda f: imp_map.get(f, len(feature_cols)))

    selected = available[:target_n]

    # 确保必要的核心特征不被遗漏
    core_patterns = ['elo_diff', 'home_elo', 'away_elo',
                     'home_last_5_wins', 'away_last_5_wins',
                     'diff_last_5_wins']

    for pat in core_patterns:
        if pat not in selected:
            for f in available:
                if f == pat and f not in selected:
                    selected[-1] = f  # 替换最后一个
                    break

    logger.info(f"  选择 {len(selected)} 个特征:")
    for i, f in enumerate(selected):
        rank = imp_map.get(f, '?')
        rank_str = str(rank)
        logger.info(f"    {i+1:2d}. [{rank_str:>3s}] {f}")

    return selected


def save_compressed(
    df: pd.DataFrame,
    selected_features: List[str],
    output_path: str,
):
    """保存压缩后的特征CSV"""
    logger.info(f"\n[SAVE] 保存到 {output_path}...")

    # 保留元数据列 + 选中的特征列
    meta_cols_present = [c for c in META_COLS if c in df.columns]
    out_df = df[meta_cols_present + selected_features]

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    out_df.to_csv(output_path, index=False)
    logger.info(f"  → {len(out_df)} 行 × {len(out_df.columns)} 列 ({len(selected_features)} 特征)")


def main():
    parser = argparse.ArgumentParser(description='智能特征压缩')
    parser.add_argument('--input', required=True, help='输入CSV路径')
    parser.add_argument('--output', default=None, help='输出CSV路径 (默认: input_compressed)')
    parser.add_argument('--target', type=int, default=35, help='目标特征数')
    parser.add_argument('--corr-threshold', type=float, default=0.95,
                        help='相关性阈值')
    parser.add_argument('--importance-model', default=None,
                        help='保存特征重要性模型路径')
    args = parser.parse_args()

    if args.output is None:
        base, ext = os.path.splitext(args.input)
        args.output = f"{base}_compressed{ext}"

    logger.info("=" * 60)
    logger.info("🧬 智能特征压缩器")
    logger.info(f"  源: {args.input}")
    logger.info(f"  目标: {args.target} 特征")
    logger.info("=" * 60)

    # 1. 加载数据
    df = load_data(args.input)

    # 2. 识别特征列
    feature_cols = identify_feature_cols(df)
    logger.info(f"[FEATURE] 候选特征: {len(feature_cols)} 个")

    # 3. 计算重要性
    importance = get_feature_importance(df, feature_cols)

    # 4. 相关性去重
    pruned = correlation_pruning(df, feature_cols, args.corr_threshold)

    # 5. 压缩选择
    selected = compress_features(df, pruned, importance, args.target)

    # 6. 保存
    save_compressed(df, selected, args.output)

    # 7. 保存重要性模型
    if args.importance_model:
        joblib.dump(importance, args.importance_model)
        logger.info(f"[SAVE] 重要性模型 → {args.importance_model}")

    logger.info("\n✅ 特征压缩完成!")
    return 0


if __name__ == '__main__':
    sys.exit(main())

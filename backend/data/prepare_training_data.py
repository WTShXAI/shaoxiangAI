import logging
"""
哨响AI — 训练数据准备 (Day 4)
=============================
从原始比赛数据生成增强特征，可选训练/测试集切分。

使用方式:
    python backend/data/prepare_training_data.py --window-size 5,10,20

    python backend/data/prepare_training_data.py \\
        --input data/30000_matches.csv \\
        --output data/features_enhanced.csv \\
        --window-size 5,10,20 \\
        --elo-k 20 \\
        --split-ratio 0.15
"""
import argparse
import sys
import os
from datetime import datetime, timezone

import pandas as pd
import numpy as np

from backend.data.enhancement import DataEnhancer
logger = logging.getLogger(__name__)

def split_temporal(df: pd.DataFrame, test_ratio: float = 0.15) -> tuple:
    """按时间切分训练/测试集（保留最近 N% 作为测试集）。

    Args:
        df: 已完成特征工程的 DataFrame（含 date 列）
        test_ratio: 测试集比例 (默认 0.15 = 15%)

    Returns:
        (train_df, test_df)
    """
    if 'date' not in df.columns:
        raise ValueError("DataFrame 缺少 'date' 列，无法按时间切分")

    df_sorted = df.sort_values('date').reset_index(drop=True)
    split_idx = int(len(df_sorted) * (1 - test_ratio))
    train_df = df_sorted.iloc[:split_idx].copy()
    test_df = df_sorted.iloc[split_idx:].copy()

    return train_df, test_df

def main():
    parser = argparse.ArgumentParser(
        description='哨响AI — 训练数据准备 (Day 4)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''示例:
  # 默认参数
  python backend/data/prepare_training_data.py

  # 指定窗口
  python backend/data/prepare_training_data.py --window-size 5,10,20

  # 指定输入输出 + 切分测试集
  python backend/data/prepare_training_data.py \\
      --input data/30000_matches.csv \\
      --output data/features_enhanced.csv \\
      --window-size 5,10,20 \\
      --split-ratio 0.15
        ''',
    )
    parser.add_argument('--input', type=str, default='data/30000_matches.csv',
                        help='原始比赛数据 CSV 路径 (默认 data/30000_matches.csv)')
    parser.add_argument('--output', type=str, default='data/features_enhanced.csv',
                        help='增强特征输出路径 (默认 data/features_enhanced.csv)')
    parser.add_argument('--window-size', type=str, default='5,10,20',
                        help='滚动窗口大小，逗号分隔 (默认 5,10,20)')
    parser.add_argument('--elo-initial', type=float, default=1500.0,
                        help='初始 ELO 分 (默认 1500)')
    parser.add_argument('--elo-k', type=float, default=20.0,
                        help='ELO K 因子 (默认 20)')
    parser.add_argument('--no-poisson', action='store_true',
                        help='跳过泊松特征生成')
    parser.add_argument('--sample', type=int, default=0,
                        help='采样行数 (0=全部)')
    parser.add_argument('--split-ratio', type=float, default=0.0,
                        help='测试集切分比例 0.0~1.0 (0=不切分，默认 0)')

    args = parser.parse_args()

    # ── 路径解析 ──
    this_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(this_dir))

    def resolve_path(p: str) -> str:
        if os.path.isabs(p):
            return p
        return os.path.join(project_root, p)

    input_path = resolve_path(args.input)
    output_path = resolve_path(args.output)

    if not os.path.exists(input_path):
        logger.info(f"❌ 输入文件不存在: {input_path}")
        logger.info(f"   ⚠️  请确保原始数据文件存在，或通过 --input 指定正确路径")
        sys.exit(1)

    # ── 加载数据 ──
    logger.info(f"\n{'=' * 60}")
    logger.info(f"  哨响AI — Day 4: 训练数据准备")
    logger.info(f"{'=' * 60}")
    logger.info(f"  输入:   {os.path.basename(input_path)}")
    logger.info(f"  输出:   {os.path.basename(output_path)}")
    logger.info(f"  窗口:   {args.window_size}")
    logger.info(f"  切分:   {'是' if args.split_ratio > 0 else '否'}")
    logger.info(f"{'=' * 60}\n")

    logger.info(f"📂 加载数据...")
    df = pd.read_csv(input_path, low_memory=False)

    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date'], errors='coerce')

    if args.sample > 0 and args.sample < len(df):
        df = df.sample(args.sample, random_state=42)
        logger.info(f"   📐 采样: {args.sample:,} 行")

    logger.info(f"   ✅ 加载: {len(df):,} 行 × {len(df.columns)} 列")

    # ── 增强管道 ──
    enhancer = DataEnhancer(df)
    windows = [int(w.strip()) for w in args.window_size.split(',')]
    t0 = datetime.now(timezone.utc)

    # 1. 滚动特征
    col_before = len(enhancer.df.columns)
    logger.info(f"\n🔄 步骤 1/3: 滚动特征 (窗口: {windows})...")
    enhancer.create_rolling_features(windows)
    new_cols = len(enhancer.df.columns) - col_before
    logger.info(f"   ✅ 新增 {new_cols} 个滚动特征列")

    # 2. ELO 评级
    col_before_elo = len(enhancer.df.columns)
    logger.info(f"\n📈 步骤 2/3: ELO 球队评级 (K={args.elo_k})...")
    enhancer.generate_team_rating_features(
        initial_elo=args.elo_initial,
        k_factor=args.elo_k,
    )
    logger.info(f"   ✅ 新增 {len(enhancer.df.columns) - col_before_elo} 个 ELO 特征列")

    # 3. 泊松特征
    if not args.no_poisson:
        col_before_poisson = len(enhancer.df.columns)
        logger.info(f"\n🎯 步骤 3/3: 泊松进球期望特征...")
        enhancer.create_poisson_features()
        logger.info(f"   ✅ 新增 {len(enhancer.df.columns) - col_before_poisson} 个泊松特征列")
    else:
        logger.info(f"\n⏭️  步骤 3/3: 跳过泊松特征")

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    enhanced_df = enhancer.df

    # ── 训练/测试集切分 ──
    if args.split_ratio > 0:
        logger.info(f"\n✂️  按时间切分训练/测试集 (test={args.split_ratio:.0%})...")
        train_df, test_df = split_temporal(enhanced_df, test_ratio=args.split_ratio)
        train_path = output_path.replace('.csv', '_train.csv')
        test_path = output_path.replace('.csv', '_test.csv')

        os.makedirs(os.path.dirname(train_path) or '.', exist_ok=True)
        train_df.to_csv(train_path, index=False)
        test_df.to_csv(test_path, index=False)

        logger.info(f"   ├─ 训练集: {len(train_df):,} 行 → {os.path.basename(train_path)}")
        logger.info(f"   ├─ 测试集: {len(test_df):,} 行 → {os.path.basename(test_path)}")
        logger.info(f"   └─ 日期范围: {train_df['date'].min().date()} ~ {test_df['date'].max().date()}")

    # ── 保存完整增强数据 ──
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    enhanced_df.to_csv(output_path, index=False)

    # ── 最终报告 ──
    logger.info(f"\n{'=' * 60}")
    logger.info(f"  ✅ Day 4 数据准备完成!")
    logger.info(f"{'=' * 60}")
    logger.info(f"  输入:   {len(df):,} 行 × {len(df.columns)} 列")
    logger.info(f"  输出:   {len(enhanced_df):,} 行 × {len(enhanced_df.columns)} 列")
    logger.info(f"  特征增长: +{len(enhanced_df.columns) - len(df.columns)} 列")
    logger.info(f"  耗时:   {elapsed:.1f}s")
    logger.info(f"  输出路径: {output_path}")
    logger.info(f"{'=' * 60}\n")

if __name__ == '__main__':
    main()

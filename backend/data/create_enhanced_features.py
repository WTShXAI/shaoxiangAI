import logging
"""
哨响AI — 增强特征生成
=====================
从原始比赛数据生成增强特征，包含:
- 多窗口滚动统计 (5/10/20 场)
- ELO 球队评级系统
- 泊松分布进球期望

用法:
    python backend/data/create_enhanced_features.py \
        --input data/30000_matches.csv \
        --output data/enhanced_features_v1.csv
"""
import argparse
import sys
import os
from datetime import datetime, timezone

import pandas as pd
import numpy as np

from backend.data.enhancement import DataEnhancer
logger = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser(
        description='哨响AI — 增强特征生成 (基于 DataEnhancer)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''示例:
  python backend/data/create_enhanced_features.py \\
      --input data/30000_matches.csv \\
      --output data/enhanced_features_v1.csv

  python backend/data/create_enhanced_features.py \\
      --input data/enhanced_matches.csv \\
      --output data/enhanced_features_v2.csv \\
      --windows 3,7,15 \\
      --elo-k 32
        '''
    )
    parser.add_argument('--input', type=str, required=True,
                        help='原始比赛数据 CSV 路径')
    parser.add_argument('--output', type=str, default='data/enhanced_features_v1.csv',
                        help='增强特征输出路径')
    parser.add_argument('--windows', type=str, default='5,10,20',
                        help='滚动窗口大小，逗号分隔 (默认 5,10,20)')
    parser.add_argument('--elo-initial', type=float, default=1500.0,
                        help='初始 ELO 分 (默认 1500)')
    parser.add_argument('--elo-k', type=float, default=20.0,
                        help='ELO K 因子 (默认 20)')
    parser.add_argument('--no-poisson', action='store_true',
                        help='跳过泊松特征生成')
    parser.add_argument('--sample', type=int, default=0,
                        help='采样行数 (0=全部)')

    args = parser.parse_args()

    # ── 路径解析 ──
    this_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(this_dir))

    input_path = args.input
    if not os.path.isabs(input_path):
        input_path = os.path.join(project_root, input_path)

    output_path = args.output
    if not os.path.isabs(output_path):
        output_path = os.path.join(project_root, output_path)

    if not os.path.exists(input_path):
        logger.info(f"❌ 输入文件不存在: {input_path}")
        sys.exit(1)

    # ── 加载数据 ──
    logger.info(f"\n{'=' * 60}")
    logger.info(f"  哨响AI — 增强特征生成")
    logger.info(f"{'=' * 60}")
    logger.info(f"  输入: {os.path.basename(input_path)}")
    logger.info(f"  输出: {os.path.basename(output_path)}")
    logger.info(f"{'=' * 60}\n")

    logger.info(f"📂 加载数据...")
    df = pd.read_csv(input_path, low_memory=False)

    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date'])

    if args.sample > 0 and args.sample < len(df):
        df = df.sample(args.sample, random_state=42)
        logger.info(f"   📐 采样: {args.sample:,} 行")

    logger.info(f"   ✅ 加载: {len(df):,} 行 × {len(df.columns)} 列")

    # ── 增强管道 ──
    enhancer = DataEnhancer(df)
    windows = [int(w.strip()) for w in args.windows.split(',')]

    t0 = datetime.now(timezone.utc)

    # 1. 滚动特征
    logger.info(f"\n🔄 步骤 1/3: 滚动特征 (窗口: {windows})...")
    enhancer.create_rolling_features(windows)
    logger.info(f"   ✅ 新增列数: {len(enhancer.df.columns) - len(df.columns)}")

    col_before_elo = len(enhancer.df.columns)

    # 2. ELO 评级
    logger.info(f"\n📈 步骤 2/3: ELO 球队评级 (K={args.elo_k})...")
    enhancer.generate_team_rating_features(
        initial_elo=args.elo_initial,
        k_factor=args.elo_k,
    )
    logger.info(f"   ✅ 新增列数: {len(enhancer.df.columns) - col_before_elo}")

    col_before_poisson = len(enhancer.df.columns)

    # 3. 泊松特征
    if not args.no_poisson:
        logger.info(f"\n🎯 步骤 3/3: 泊松进球期望特征...")
        enhancer.create_poisson_features()
        logger.info(f"   ✅ 新增列数: {len(enhancer.df.columns) - col_before_poisson}")
    else:
        logger.info(f"\n⏭️  步骤 3/3: 跳过泊松特征")

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()

    # ── 保存 ──
    logger.info(f"\n💾 保存增强数据...")
    enhancer.save_enhanced_data(output_path)

    # ── 最终报告 ──
    logger.info(f"\n{'=' * 60}")
    logger.info(f"  ✅ 增强特征生成完成!")
    logger.info(f"{'=' * 60}")
    logger.info(f"  输入:   {len(df):,} 行 × {len(df.columns)} 列")
    logger.info(f"  输出:   {len(enhancer.df):,} 行 × {len(enhancer.df.columns)} 列")
    logger.info(f"  耗时:   {elapsed:.1f}s")
    logger.info(f"  特征增长: +{len(enhancer.df.columns) - len(df.columns)} 列")
    logger.info(f"  输出路径: {output_path}")
    logger.info(f"{'=' * 60}\n")

if __name__ == '__main__':
    main()

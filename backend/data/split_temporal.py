import logging
"""
哨响AI — 时序数据分割
=====================
按时间顺序分割增强数据为训练集/测试集，保持时序完整性。

使用方式:
    python backend/data/split_temporal.py --input data/enhanced_matches.csv --test-size 0.2
"""
import argparse
import sys
import os
import pandas as pd
logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def split_temporal(
    df: pd.DataFrame,
    test_size: float = 0.2,
    date_col: str = 'date',
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按时序分割数据

    Args:
        df: 比赛数据 (含日期列)
        test_size: 测试集比例
        date_col: 日期列名

    Returns:
        (train_df, test_df) 按时间排序的训练集和测试集
    """
    df_sorted = df.sort_values(date_col).reset_index(drop=True)
    n = len(df_sorted)
    split_idx = int(n * (1 - test_size))

    train_df = df_sorted.iloc[:split_idx].reset_index(drop=True)
    test_df = df_sorted.iloc[split_idx:].reset_index(drop=True)

    logger.info(f"[SPLIT] 总数据: {n:,} 场")
    logger.info(f"   ├─ 训练集: {len(train_df):,} 场 ({len(train_df) / n * 100:.1f}%)")
    logger.info(f"   ├─ 测试集: {len(test_df):,} 场 ({len(test_df) / n * 100:.1f}%)")
    logger.info(f"   ├─ 时间范围 (训练): {train_df[date_col].min()} ~ {train_df[date_col].max()}")
    logger.info(f"   └─ 时间范围 (测试): {test_df[date_col].min()} ~ {test_df[date_col].max()}")

    return train_df, test_df


def main():
    parser = argparse.ArgumentParser(description="时序分割增强数据")
    parser.add_argument('--input', type=str, default='data/enhanced_matches.csv',
                        help='增强数据 CSV 路径')
    parser.add_argument('--test-size', type=float, default=0.2,
                        help='测试集比例 (默认 0.2)')
    parser.add_argument('--output-dir', type=str, default='data',
                        help='输出目录')
    parser.add_argument('--train-name', type=str, default='train_matches.csv',
                        help='训练集文件名')
    parser.add_argument('--test-name', type=str, default='test_matches.csv',
                        help='测试集文件名')
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    input_path = args.input
    if not os.path.isabs(input_path):
        input_path = os.path.join(project_root, input_path)

    logger.info(f"\n{'=' * 60}")
    logger.info(f"  时序数据分割")
    logger.info(f"{'=' * 60}")
    logger.info(f"  输入: {input_path}")

    # 加载数据
    logger.info(f"\n📂 加载增强数据...")
    df = pd.read_csv(input_path, low_memory=False)

    # 解析日期
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date'])
    else:
        logger.info("❌ 数据缺少 'date' 列")
        sys.exit(1)

    # 分割
    train_df, test_df = split_temporal(df, test_size=args.test_size)

    # 保存
    output_dir = args.output_dir
    if not os.path.isabs(output_dir):
        output_dir = os.path.join(project_root, output_dir)
    os.makedirs(output_dir, exist_ok=True)

    train_path = os.path.join(output_dir, args.train_name)
    test_path = os.path.join(output_dir, args.test_name)

    train_df.to_csv(train_path, index=False)
    test_df.to_csv(test_path, index=False)

    logger.info(f"\n💾 已保存:")
    logger.info(f"   ├─ 训练集: {train_path}")
    logger.info(f"   └─ 测试集: {test_path}")

    # 验证: 训练集样本在全部数据中的分布
    logger.info(f"\n📊 数据分布验证:")
    logger.info(f"   训练集联赛数: {train_df.get('league', pd.Series()).nunique()}")
    logger.info(f"   测试集联赛数: {test_df.get('league', pd.Series()).nunique()}")
    logger.info(f"   无时间重叠: {train_df['date'].max() <= test_df['date'].min()}")

    logger.info(f"\n{'=' * 60}")
    logger.info(f"  ✅ 时序分割完成")
    logger.info(f"{'=' * 60}\n")


if __name__ == '__main__':
    main()

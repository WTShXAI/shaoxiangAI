import logging
"""
用压缩特征重新训练模型 — 使用 FootballAIEnhanced 架构
"""
import argparse
import sys
import os
import json
from pathlib import Path
from datetime import datetime
from typing import List

import numpy as np
import pandas as pd
import joblib
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from backend.models.footballai_enhanced import FootballAIEnhanced
logger = logging.getLogger(__name__)

META_COLS = ['date', 'home_team', 'away_team', 'home_score', 'away_score', 'league']


def prepare_data(csv_path: str):
    """从压缩CSV加载并准备训练数据"""
    logger.info(f"[LOAD] {csv_path}")
    df = pd.read_csv(csv_path)
    logger.info(f"  {len(df)} 行 × {len(df.columns)} 列")

    # 区分特征列和元数据列
    feature_cols = [c for c in df.columns
                    if c not in META_COLS
                    and df[c].dtype in ['float64', 'float32', 'int64', 'int32']]
    logger.info(f"[FEAT] {len(feature_cols)} 个特征")

    # 构建标签
    hs = df['home_score'].values
    aws = df['away_score'].values
    y_str = np.full(len(df), 'D', dtype=object)
    y_str[hs > aws] = 'H'
    y_str[hs < aws] = 'A'

    return df, feature_cols, y_str


def main():
    parser = argparse.ArgumentParser(description='用压缩特征重新训练模型')
    parser.add_argument('--features', required=True, help='压缩特征CSV路径')
    parser.add_argument('--output', default=None, help='输出模型路径')
    parser.add_argument('--version', default='compressed_v1', help='模型版本标签')
    parser.add_argument('--cv-folds', type=int, default=5, help='交叉验证折数')
    parser.add_argument('--no-draw', action='store_true', help='禁用平局优化')
    parser.add_argument('--no-adaptive', action='store_true', help='禁用自适应训练')
    args = parser.parse_args()

    if args.output is None:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        args.output = f'saved_models/footballai_{args.version}.joblib'

    logger.info("=" * 60)
    logger.info("🔄 压缩特征模型重训练")
    logger.info(f"  特征源: {args.features}")
    logger.info(f"  模型输出: {args.output}")
    logger.info(f"  版本: {args.version}")
    logger.info("=" * 60)

    # 1. 加载数据
    df, feature_cols, y_str = prepare_data(args.features)

    # 2. 初始化模型
    logger.info(f"\n[MODEL] FootballAIEnhanced ({args.version})")
    model = FootballAIEnhanced(model_version=args.version)

    # 3. 准备特征
    df_prepared = df[feature_cols].copy()
    df_prepared['home_score'] = df['home_score']
    df_prepared['away_score'] = df['away_score']

    X, y = model.prepare_features(df_prepared)
    logger.info(f"[DATA] X={X.shape}, y={y.shape}")

    # 4. 训练
    logger.info(f"\n[TRAIN] 时序交叉验证 ({args.cv_folds}-fold)...")
    model.train_with_cross_validation(X, y)

    # 5. 可选: 平局优化
    if not args.no_draw:
        try:
            logger.info(f"\n[DRAW] 启用平局优化...")
            model.enable_draw_optimization(X, y)
        except (ValueError, KeyError, FileNotFoundError) as e:
            logger.info(f"  ⚠️ 平局优化失败: {e}")

    # 6. 可选: 自适应训练
    if not args.no_adaptive:
        try:
            logger.info(f"\n[ADAPT] 启用自适应训练...")
            model.enable_adaptive_training(
                X, y,
                dates=pd.to_datetime(df['date']) if 'date' in df.columns else None,
                model_builder=lambda: model.xgb_model.__class__(**model.xgb_model.get_params()
                    if model.xgb_model else {}),
            )
        except (ValueError, KeyError, TypeError, AttributeError, ValueError, TypeError) as e:
            logger.info(f"  ⚠️ 自适应训练失败: {e}")

    # 7. 保存模型
    output_path = model.save_model(os.path.dirname(args.output))
    # 如果文件名不一致则重命名
    if os.path.basename(output_path) != os.path.basename(args.output):
        import shutil
        shutil.move(output_path, args.output)
        logger.info(f"[SAVE] 模型已保存 → {args.output}")

    # 8. 保存特征列表
    feat_file = args.output.replace('.joblib', '_features.json')
    with open(feat_file, 'w') as f:
        json.dump({
            'feature_names': feature_cols,
            'n_features': len(feature_cols),
            'version': args.version,
            'timestamp': datetime.now().isoformat(),
        }, f, indent=2, ensure_ascii=False)
    logger.info(f"[SAVE] 特征列表 → {feat_file}")

    # 9. 打印摘要
    logger.info(f"\n{'='*60}")
    logger.info(f"📊 训练摘要")
    logger.info(f"  模型: {os.path.basename(args.output)}")
    logger.info(f"  特征数: {len(feature_cols)}")
    logger.info(f"  样本数: {len(df)}")
    if model.feature_importance is not None:
        logger.info(f"  Top-3 特征: {model.feature_importance.head(3)['feature'].tolist()}")
    logger.info(f"{'='*60}")

    return 0


if __name__ == '__main__':
    sys.exit(main())

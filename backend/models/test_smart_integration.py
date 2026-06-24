import logging
"""
[DEPRECATED] P0-1: 此文件直接使用 joblib.load 绕过 ModelBridge，存在数据泄露风险。
生产预测请使用 agents.model_bridge.ModelBridge.predict()
验证集成模型性能 — 对比单体 footballAI 和集成模型
"""
import argparse
import sys
import os
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score, f1_score, log_loss, classification_report,
    confusion_matrix, brier_score_loss,
)
from sklearn.preprocessing import LabelEncoder
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from backend.models.smart_integration import SmartIntegration
logger = logging.getLogger(__name__)


def load_data(csv_path: str):
    """加载测试数据"""
    logger.info(f"[LOAD] {csv_path}")
    if not os.path.exists(csv_path):
        logger.info(f"  ❌ 文件不存在")
        return None

    df = pd.read_csv(csv_path)
    logger.info(f"  {len(df)} 行 × {len(df.columns)} 列")

    META_COLS = ['date', 'home_team', 'away_team', 'home_score', 'away_score', 'league']
    feature_cols = [c for c in df.columns
                    if c not in META_COLS
                    and df[c].dtype in ['float64', 'float32', 'int64', 'int32']]

    X = df[feature_cols].fillna(0).values.astype(np.float32)

    hs = df['home_score'].values
    aws = df['away_score'].values
    y = np.full(len(df), 1, dtype=int)
    y[hs > aws] = 0
    y[hs < aws] = 2

    # 标签映射: 0=H, 1=D, 2=A
    class_names = ['H (主胜)', 'D (平局)', 'A (客胜)']

    return X, y, class_names, df


def evaluate_model(model, X: np.ndarray, y: np.ndarray, name: str,
                   class_names: list) -> Dict:
    """评估模型性能"""
    logger.info(f"\n{'─'*50}")
    logger.info(f"📊 评估: {name}")

    # 预测
    if hasattr(model, 'predict_proba'):
        y_proba = model.predict_proba(X)
        y_pred = np.argmax(y_proba, axis=1)
    elif hasattr(model, 'predict'):
        y_pred = model.predict(X)
        y_proba = None
    else:
        logger.info("  ❌ 模型无预测方法")
        return {}

    # 确保 y_pred 是整数
    y_pred = np.array(y_pred).astype(int)

    # 计算指标
    acc = accuracy_score(y, y_pred)
    f1 = f1_score(y, y_pred, average='macro')

    results = {
        'accuracy': acc,
        'f1_macro': f1,
        'n_samples': len(y),
    }

    logger.info(f"  Accuracy:  {acc:.4f}")
    logger.info(f"  F1 Macro:  {f1:.4f}")

    # 如果有多类概率
    if y_proba is not None and y_proba.shape[1] >= 3:
        try:
            logl = log_loss(y, y_proba)
            results['log_loss'] = logl
            logger.info(f"  Log Loss:  {logl:.4f}")
        except (OSError, ValueError, KeyError) as e:
            logger.debug(f"操作失败: {e}")

    # 混淆矩阵
    cm = confusion_matrix(y, y_pred)
    results['confusion_matrix'] = cm
    logger.info(f"  混淆矩阵:")
    logger.info(f"              预测H  预测D  预测A")
    for i, name_c in enumerate(class_names):
        if i < len(cm):
            logger.info(f"    实际{name_c[0]:4s}  {cm[i][0] if i < cm.shape[0] and 0 < cm.shape[1] else 0:5d}"
                  f"  {cm[i][1] if 1 < cm.shape[1] else 0:5d}"
                  f"  {cm[i][2] if 2 < cm.shape[1] else 0:5d}")

    # 各类别F1
    f1_per_class = f1_score(y, y_pred, average=None)
    for i, name_c in enumerate(class_names):
        if i < len(f1_per_class):
            logger.info(f"  F1 {name_c}: {f1_per_class[i]:.4f}")
            results[f'f1_class_{i}'] = f1_per_class[i]

    return results


def main():
    parser = argparse.ArgumentParser(description='测试智能集成模型')
    parser.add_argument('--model', required=True, help='集成模型路径')
    parser.add_argument('--test', required=True, help='测试数据CSV')
    parser.add_argument('--baseline', default=None,
                        help='基线 footballAI 模型路径 (对比用)')
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("🧪 集成模型性能验证")
    logger.info(f"  模型: {args.model}")
    logger.info(f"  数据: {args.test}")
    logger.info("=" * 60)

    # 1. 加载数据
    result = load_data(args.test)
    if result is None:
        return 1
    X, y, class_names, df = result

    # 2. 加载集成模型
    logger.info(f"\n[LOAD] 集成模型: {args.model}")
    if not os.path.exists(args.model):
        logger.info(f"  ❌ 模型文件不存在")
        return 1
    integrated = SmartIntegration.load(args.model)
    logger.info(f"  ✓ 加载成功")

    # 3. 评估集成模型
    integrated_results = evaluate_model(integrated, X, y, 'SmartIntegration', class_names)

    # 4. 可选: 评估基线模型
    baseline_results = None
    if args.baseline and os.path.exists(args.baseline):
        logger.info(f"\n[LOAD] 基线模型: {args.baseline}")
        try:
            import joblib
            baseline = joblib.load(args.baseline)
            baseline_results = evaluate_model(baseline, X, y, 'FootballAI (baseline)', class_names)
        except (FileNotFoundError, IOError, OSError, PermissionError) as e:
            logger.info(f"  ⚠️ 基线模型加载失败: {e}")

    # 5. 对比摘要
    logger.info(f"\n{'='*60}")
    logger.info(f"📊 对比摘要")
    logger.info(f"{'指标':<15} {'集成':>12}", end='')
    if baseline_results:
        logger.info(f" {'基线':>12} {'提升':>10}", end='')
    print()

    logger.info(f"{'-'*50}")
    msg = f"{'Accuracy':<15} {integrated_results.get('accuracy', 0):>12.4f}"
    if baseline_results:
        diff = integrated_results.get('accuracy', 0) - baseline_results.get('accuracy', 0)
        msg += f" {baseline_results.get('accuracy', 0):>12.4f} {diff:>+10.4f}"
    logger.info(msg)

    msg = f"{'F1 Macro':<15} {integrated_results.get('f1_macro', 0):>12.4f}"
    if baseline_results:
        diff = integrated_results.get('f1_macro', 0) - baseline_results.get('f1_macro', 0)
        msg += f" {baseline_results.get('f1_macro', 0):>12.4f} {diff:>+10.4f}"
    logger.info(msg)

    logger.info(f"{'='*60}")

    if baseline_results:
        acc_improve = integrated_results.get('accuracy', 0) - baseline_results.get('accuracy', 0)
        if acc_improve > 0:
            logger.info(f"\n✅ 集成模型优于基线: +{acc_improve*100:.2f}% 准确率")
        elif acc_improve > -0.005:
            logger.info(f"\n✅ 集成模型与基线基本持平")
        else:
            logger.info(f"\n⚠️ 集成模型低于基线: {acc_improve*100:.2f}%")

    return 0


if __name__ == '__main__':
    sys.exit(main())

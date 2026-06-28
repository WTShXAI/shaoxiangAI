import logging
"""
哨响AI — 评估增强版 FootballAI 模型
===================================
命令行评估工具，加载已训练模型并在测试集上生成报告。

使用方式:
    python backend/models/evaluate_enhanced.py \
        --model saved_models/footballai_enhanced_v4.0.joblib \
        --test data/test_matches.csv \
        --output output/evaluation_report.txt
"""
import argparse
import sys
import os
import time
import json
import pandas as pd
import numpy as np

from backend.models.footballai_enhanced import FootballAIEnhanced
logger = logging.getLogger(__name__)

def evaluate_model(
    model: FootballAIEnhanced,
    X_test: np.ndarray,
    y_test: np.ndarray,
) -> dict:
    """全面评估模型

    Returns:
        包含 accuracy / per_class_metrics / confusion_matrix 的字典
    """
    from sklearn.metrics import (
        classification_report, accuracy_score,
        confusion_matrix, log_loss,
    )

    y_pred = model.predict(X_test)
    proba = model.ensemble_predict(X_test)

    report = classification_report(
        y_test, y_pred,
        target_names=['H(主胜)', 'D(平局)', 'A(客胜)'],
        output_dict=True,
        digits=4,
    )

    cm = confusion_matrix(y_test, y_pred)

    # 计算 log-loss (平滑处理)
    eps = 1e-15
    proba_clipped = np.clip(proba, eps, 1 - eps)
    ll = log_loss(y_test, proba_clipped)

    results = {
        'accuracy': float(accuracy_score(y_test, y_pred)),
        'log_loss': float(ll),
        'n_samples': int(len(y_test)),
        'class_distribution': {
            'H': int(np.sum(y_test == 0)),
            'D': int(np.sum(y_test == 1)),
            'A': int(np.sum(y_test == 2)),
        },
        'per_class': {
            label: {
                'precision': report[label]['precision'],
                'recall': report[label]['recall'],
                'f1-score': report[label]['f1-score'],
                'support': int(report[label]['support']),
            }
            for label in ['H(主胜)', 'D(平局)', 'A(客胜)']
        },
        'confusion_matrix': cm.tolist(),
        'macro_avg': {
            'precision': report['macro avg']['precision'],
            'recall': report['macro avg']['recall'],
            'f1-score': report['macro avg']['f1-score'],
        },
        'weighted_avg': {
            'precision': report['weighted avg']['precision'],
            'recall': report['weighted avg']['recall'],
            'f1-score': report['weighted avg']['f1-score'],
        },
    }

    return results

def print_report(results: dict):
    """打印格式化评估报告"""
    logger.info(f"\n{'=' * 60}")
    logger.info(f"  FootballAIEnhanced 模型评估报告")
    logger.info(f"{'=' * 60}")

    logger.info(f"\n📊 总体指标:")
    logger.info(f"   ├─ 测试样本: {results['n_samples']:,}")
    logger.info(f"   ├─ 准确率:   {results['accuracy']:.4f}")
    logger.info(f"   └─ Log-Loss:  {results['log_loss']:.4f}")

    logger.info(f"\n📈 类别分布:")
    for cls, cnt in results['class_distribution'].items():
        pct = cnt / results['n_samples'] * 100
        logger.info(f"   └─ {cls}: {cnt:,} ({pct:.1f}%)")

    logger.info(f"\n🎯 每类指标:")
    logger.info(f"   {'类别':<12s} {'Precision':>10s} {'Recall':>10s} {'F1-score':>10s} {'Support':>10s}")
    logger.info(f"   {'-' * 52}")
    for label, metrics in results['per_class'].items():
        logger.info(f"   {label:<12s} {metrics['precision']:>10.4f} {metrics['recall']:>10.4f} "
              f"{metrics['f1-score']:>10.4f} {metrics['support']:>10d}")

    logger.info(f"\n📊 混淆矩阵 (行=实际, 列=预测):")
    logger.info(f"   {'':>12s} H(主胜)  D(平局)  A(客胜)")
    cm = results['confusion_matrix']
    labels = ['H(主胜)', 'D(平局)', 'A(客胜)']
    for i, label in enumerate(labels):
        logger.info(f"   {label:<12s} {cm[i][0]:>7d} {cm[i][1]:>7d} {cm[i][2]:>7d}")

    logger.info(f"\n📊 宏平均:")
    for k, v in results['macro_avg'].items():
        logger.info(f"   ├─ {k}: {v:.4f}")

    logger.info(f"\n📊 加权平均:")
    for k, v in results['weighted_avg'].items():
        logger.info(f"   ├─ {k}: {v:.4f}")

    # 通过/不通过判定
    logger.info(f"\n{'=' * 60}")
    checks = []
    checks.append(('准确率 ≥ 50%', results['accuracy'] >= 0.50))
    checks.append(('Log-Loss < 1.1', results['log_loss'] < 1.1))
    checks.append(('平局 Recall > 0%', results['per_class']['D(平局)']['recall'] > 0.0))

    all_pass = True
    for check_name, passed in checks:
        status = '✅' if passed else '❌'
        if not passed:
            all_pass = False
        logger.info(f"  {status} {check_name}: {'通过' if passed else '未通过'}")

    logger.info(f"{'=' * 60}")
    if all_pass:
        logger.info(f"  ✅ 模型评估: 全部通过")
    else:
        logger.info(f"  ⚠️  模型评估: 部分指标未达标")
    logger.info(f"{'=' * 60}\n")

def main():
    parser = argparse.ArgumentParser(description="评估 FootballAIEnhanced 模型")
    parser.add_argument('--model', type=str, required=True,
                        help='已训练的模型文件路径 (.joblib)')
    parser.add_argument('--test', type=str, required=True,
                        help='测试数据 CSV 路径')
    parser.add_argument('--output', type=str, default=None,
                        help='评估报告输出路径 (.txt 或 .json)')
    parser.add_argument('--json', action='store_true',
                        help='以 JSON 格式输出')
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    model_path = args.model
    if not os.path.isabs(model_path):
        model_path = os.path.join(project_root, model_path)
    test_path = args.test
    if not os.path.isabs(test_path):
        test_path = os.path.join(project_root, test_path)

    logger.info(f"\n{'=' * 60}")
    logger.info(f"  FootballAIEnhanced 模型评估")
    logger.info(f"{'=' * 60}")
    logger.info(f"  模型: {model_path}")
    logger.info(f"  测试: {test_path}")
    logger.info(f"{'=' * 60}")

    # ── 1. 加载模型 ──
    logger.info(f"\n📂 加载模型...")
    if not os.path.exists(model_path):
        logger.info(f"❌ 模型文件不存在: {model_path}")
        sys.exit(1)
    t0 = time.time()
    model = FootballAIEnhanced.load_model(model_path)
    logger.info(f"   ├─ 版本: {model.model_version}")
    logger.info(f"   ├─ 集成权重: {model.ensemble_weights} (来源: {model._weights_source})")
    logger.info(f"   └─ 加载耗时: {time.time() - t0:.1f}s")

    # ── 2. 加载测试数据 ──
    logger.info(f"\n📂 加载测试数据...")
    if not os.path.exists(test_path):
        logger.info(f"❌ 测试数据不存在: {test_path}")
        sys.exit(1)
    df_test = pd.read_csv(test_path, low_memory=False)
    if 'date' in df_test.columns:
        df_test['date'] = pd.to_datetime(df_test['date'])
    logger.info(f"   ├─ 数据量: {len(df_test):,} 行")
    logger.info(f"   └─ 列数: {len(df_test.columns)}")

    # ── 3. 准备特征 ──
    logger.info(f"\n🔧 准备特征...")
    X_test, y_test, features = model.prepare_features(df_test)
    logger.info(f"   ├─ 特征数: {len(features)}")
    logger.info(f"   └─ 样本量: {len(y_test):,}")

    # ── 4. 评估 ──
    logger.info(f"\n🎯 评估中...")
    results = evaluate_model(model, X_test, y_test)

    # ── 5. 打印报告 ──
    print_report(results)

    # ── 6. 保存报告 ──
    output_path = args.output
    if output_path:
        if not os.path.isabs(output_path):
            output_path = os.path.join(project_root, output_path)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        if args.json or output_path.endswith('.json'):
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(results, f, indent=2, ensure_ascii=False, default=str)
        else:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(f"FootballAIEnhanced {model.model_version} 评估报告\n")
                f.write(f"{'=' * 60}\n")
                f.write(f"测试样本: {results['n_samples']:,}\n")
                f.write(f"准确率:   {results['accuracy']:.4f}\n")
                f.write(f"Log-Loss:  {results['log_loss']:.4f}\n\n")
                f.write(f"每类指标:\n")
                f.write(f"{'类别':<12s} {'Precision':>10s} {'Recall':>10s} {'F1':>10s}\n")
                for label, m in results['per_class'].items():
                    f.write(f"{label:<12s} {m['precision']:>10.4f} {m['recall']:>10.4f} {m['f1-score']:>10.4f}\n")
                f.write(f"\n混淆矩阵:\n{np.array(results['confusion_matrix'])}\n")

        logger.info(f"💾 报告已保存: {output_path}")

if __name__ == '__main__':
    main()

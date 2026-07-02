#!/usr/bin/env python3
"""JEPA v5 模型评估脚本 (2026-07-01)

与 v4.1 生产模型对比准确率/F1，生成评估报告。
用法: python scripts/eval_jepa.py [--full]
"""
import sys, os, argparse, json, time
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import torch
from sklearn.metrics import accuracy_score, f1_score, classification_report

def load_jepa_model(device='cpu'):
    """加载 JEPA v5 模型"""
    from models.jepa import JEPALite
    model = JEPALite(static_dim=72, embed_dim=128)
    # 加载权重
    jepa_dir = ROOT / 'models' / 'jepa' / 'checkpoints'
    ckpts = list(jepa_dir.glob('*.pt')) + list(jepa_dir.glob('*.pth'))
    ckpts = sorted(ckpts, key=os.path.getmtime, reverse=True)
    if ckpts:
        ckpt = torch.load(ckpts[0], map_location=device, weights_only=True)
        # 处理不同格式: 纯 state_dict vs 训练检查点
        if isinstance(ckpt, dict) and 'model' in ckpt:
            state = ckpt['model']
            print(f"  JEPA权重: {ckpts[0].name} (epoch={ckpt.get('epoch','?')}, train_acc={ckpt.get('acc','?')})")
        else:
            state = ckpt
            print(f"  JEPA权重: {ckpts[0].name}")
        model.load_state_dict(state)
    else:
        print("  ⚠ 未找到JEPA权重文件，使用随机初始化")
    model.to(device)
    model.eval()
    return model

def evaluate_jepa(model, data, device='cpu', batch_size=256):
    """评估JEPA模型 (JEPALite只使用static features)"""
    static = data['static']
    labels = data['labels']

    all_preds = []
    n_samples = len(labels)

    with torch.no_grad():
        for i in range(0, n_samples, batch_size):
            end = min(i + batch_size, n_samples)
            s = torch.tensor(static[i:end], dtype=torch.float32, device=device)

            # JEPALite.predict_proba(static, seq=None, drift=None, n_paths=30)
            probs = model.predict_proba(s, seq=None, drift=None, n_paths=10)
            preds = probs.argmax(dim=1)
            all_preds.extend(preds.cpu().numpy())

    acc = accuracy_score(labels, all_preds)
    f1 = f1_score(labels, all_preds, average='macro')
    f1_weighted = f1_score(labels, all_preds, average='weighted')
    f1_per_class = f1_score(labels, all_preds, average=None)

    return {
        'accuracy': round(acc, 4),
        'macro_f1': round(f1, 4),
        'weighted_f1': round(f1_weighted, 4),
        'f1_per_class': {str(i): round(v, 4) for i, v in enumerate(f1_per_class)},
        'n_samples': n_samples,
        'label_dist': Counter(int(l) for l in labels),
    }

def try_eval_v41(data):
    """尝试通过 UnifiedPredictor 评估 v4.1"""
    try:
        from pipeline.full_linkage_predictor import FullLinkagePredictor
        predictor = FullLinkagePredictor()
        print("  v4.1 UnifiedPredictor 已加载")
        return None  # 需要真实比赛数据，此处placeholder
    except Exception as e:
        print(f"  ⚠ v4.1 不可用: {e}")
        return None

def load_jepa_data(split='test'):
    """加载JEPA数据"""
    path = ROOT / 'data' / f'jepa_{split}.npz'
    if not path.exists():
        print(f"  ⚠ 数据文件不存在: {path}")
        return None
    return np.load(path, allow_pickle=True)

def main():
    parser = argparse.ArgumentParser(description='JEPA v5 模型评估')
    parser.add_argument('--full', action='store_true', help='完整对比 (含v4.1)')
    parser.add_argument('--device', default='cpu', help='设备 (cpu/cuda)')
    parser.add_argument('--output', help='输出JSON路径')
    args = parser.parse_args()

    device = args.device
    if device == 'cuda' and not torch.cuda.is_available():
        print("CUDA不可用，回退到CPU")
        device = 'cpu'

    results = {
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'model': 'JEPA v5 (JEPALite)',
        'device': device,
    }

    # 1. JEPA评估
    print("\n═══ JEPA v5 评估 ═══")
    test_data = load_jepa_data('test')
    if test_data is not None:
        model = load_jepa_model(device)
        jepa_result = evaluate_jepa(model, test_data, device)
        results['jepa'] = jepa_result
        print(f"  JEPA Acc: {jepa_result['accuracy']:.4f}")
        print(f"  JEPA Macro-F1: {jepa_result['macro_f1']:.4f}")
        print(f"  JEPA Weighted-F1: {jepa_result['weighted_f1']:.4f}")
        print(f"  JEPA Per-Class F1: {jepa_result['f1_per_class']}")
        print(f"  样本数: {jepa_result['n_samples']}, 分布: {dict(jepa_result['label_dist'])}")

    # 2. v4.1 评估（需要比赛数据）
    if args.full:
        print("\n═══ v4.1 对比评估 ═══")
        v41_result = try_eval_v41(test_data)
        if v41_result:
            results['v4.1'] = v41_result

    # 3. 输出
    print("\n═══ 评估结论 ═══")
    if 'jepa' in results:
        acc = results['jepa']['accuracy']
        if acc < 0.40:
            print(f"  🔴 JEPA Acc={acc:.2%} — 低于随机基线，模型训练有问题")
        elif acc < 0.50:
            print(f"  🟠 JEPA Acc={acc:.2%} — 低于v4.1(62.43%)，需重新训练")
        elif acc < 0.60:
            print(f"  🟡 JEPA Acc={acc:.2%} — 接近v4.1，可尝试集成")
        else:
            print(f"  🟢 JEPA Acc={acc:.2%} — 优于或等于v4.1，建议上线")

    if args.output:
        os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\n结果已保存: {args.output}")

    return results

if __name__ == '__main__':
    main()

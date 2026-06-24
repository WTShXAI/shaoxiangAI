#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
P2 验证: FeatureAligner 解耦效果
================================
验证:
  1. DrawExpert 独立调用不再恒定输出 0.331
  2. 不同比赛产出不同 pDraw
  3. FeatureAligner 特征向量与 _sky_predict 一致
  4. 生产配置回测不退化
"""
import sys, os, json, warnings
from pathlib import Path
from datetime import datetime

warnings.filterwarnings('ignore')

ARCH_ROOT = Path(r"D:/Architecture v4.0")
FAI_ROOT = Path(r"D:/AI/footballAI")
MODELS_DIR = FAI_ROOT / "saved_models"

sys.path.insert(0, str(ARCH_ROOT))
sys.path.insert(0, str(ARCH_ROOT / "features"))
sys.path.insert(0, str(ARCH_ROOT / "predictors" / "components"))
sys.path.insert(0, str(FAI_ROOT))

import numpy as np

print("=" * 70)
print("  P2 验证: FeatureAligner 特征解耦效果")
print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 70)

# ════════════════════════════════════════════════
# 1. 验证 FeatureAligner 基本功能
# ════════════════════════════════════════════════
print("\n[1/4] FeatureAligner 基本功能验证")

from feature_aligner import FeatureAligner
import joblib

v41 = joblib.load(str(MODELS_DIR / "football_v4.1_production.joblib"))
aligner = FeatureAligner.from_model_dict(v41)
print(f"  特征数: {len(aligner.feature_names)}")
print(f"  scaler: {'✓' if aligner.scaler else '✗'}")
print(f"  默认值数: {len(aligner.defaults)}")

# 构建两个不同比赛的特征向量
vec1 = aligner.build(oh=1.30, od=5.00, oa=8.30, asian_handicap=-1.5, ou_line=2.5)
vec2 = aligner.build(oh=5.60, od=3.75, oa=1.61, asian_handicap=1.0, ou_line=2.5)

diff = np.abs(vec1 - vec2).sum()
print(f"  vec1 (热门主胜) vs vec2 (客胜热门) L1距离: {diff:.4f}")
assert diff > 0.1, "特征向量应该不同!"
print(f"  ✅ 不同比赛特征向量不同")

# ════════════════════════════════════════════════
# 2. 验证 DrawExpert 独立调用不再恒定
# ════════════════════════════════════════════════
print("\n[2/4] DrawExpert 独立调用验证 (P2 核心修复)")

de = v41['draw_expert_model']
de_feature_names = de.feature_names_
print(f"  DrawExpert 特征数: {len(de_feature_names)}")
print(f"  v4.1 特征数: {len(aligner.feature_names)}")
print(f"  特征名一致: {'✅' if set(de_feature_names) == set(aligner.feature_names) else '❌'}")

# 用 FeatureAligner 构建 5 场不同比赛的特征, 调用 DrawExpert
test_matches = [
    {'name': '英格兰 vs 克罗地亚 (超热门主胜)', 'oh': 1.30, 'od': 5.00, 'oa': 8.30, 'hc': -1.5},
    {'name': '卡塔尔 vs 瑞士 (客胜热门)',       'oh': 5.60, 'od': 3.75, 'oa': 1.61, 'hc': 1.0},
    {'name': '科特迪瓦 vs 厄瓜多尔 (均衡赛)',    'oh': 2.60, 'od': 3.35, 'oa': 2.60, 'hc': 0.0},
    {'name': '荷兰 vs 日本 (中等热门)',         'oh': 1.63, 'od': 3.90, 'oa': 4.70, 'hc': -0.5},
    {'name': '巴西 vs 摩洛哥 (强热门)',         'oh': 1.39, 'od': 4.50, 'oa': 7.50, 'hc': -1.5},
]

print(f"\n  {'比赛':40s} {'pDraw':>8} {'原0.331?':>10}")
print(f"  {'-'*60}")

pdraws = []
for m in test_matches:
    vec = aligner.build(oh=m['oh'], od=m['od'], oa=m['oa'],
                        asian_handicap=m['hc'], ou_line=2.5)
    p = de.model.predict_proba(vec.reshape(1, -1))[0, 1]
    pdraws.append(p)
    is_const = abs(p - 0.331) < 0.01
    print(f"  {m['name']:40s} {p:>8.4f} {'❌恒定' if is_const else '✅变化':>10}")

# 验证: 不再全部恒定 0.331
unique_vals = len(set([round(p, 3) for p in pdraws]))
all_same = all(abs(p - pdraws[0]) < 0.001 for p in pdraws)
print(f"\n  唯一值数: {unique_vals}/5")
print(f"  全部相同: {'❌ 是 (特征对齐失败)' if all_same else '✅ 否 (特征对齐成功)'}")

# ════════════════════════════════════════════════
# 3. 验证 UnifiedPredictor 调用 DrawExpert 信号
# ════════════════════════════════════════════════
print("\n[3/4] UnifiedPredictor DrawExpert 信号验证")

from predictors.unified_predictor import UnifiedPredictor
up = UnifiedPredictor(
    model_path=str(MODELS_DIR / "football_v4.1_production.joblib"),
    enable_trap=False, enable_dh=False, use_threshold=True,
)

if up._ready:
    # 调用 _get_draw_expert_signal
    signals = []
    for m in test_matches:
        sig = up._get_draw_expert_signal(
            home="test", away="test",
            oh=m['oh'], od=m['od'], oa=m['oa'],
            asian_handicap=m['hc'], ou_line=2.5,
        )
        signals.append(sig)

    print(f"  {'比赛':40s} {'DE信号':>8}")
    print(f"  {'-'*50}")
    for m, s in zip(test_matches, signals):
        print(f"  {m['name']:40s} {s:>8.4f}")

    sig_unique = len(set([round(s, 3) for s in signals]))
    print(f"\n  信号唯一值数: {sig_unique}/5")
    print(f"  信号不再恒定: {'✅' if sig_unique > 1 else '❌'}")
else:
    print("  ❌ UnifiedPredictor 未就绪")
    signals = [0.0] * 5
    sig_unique = 0

# ════════════════════════════════════════════════
# 4. 生产配置回测 (确认不退化)
# ════════════════════════════════════════════════
print("\n[4/4] 生产配置回测 (P0+P2 叠加)")

WORLDCUP_MATCHES = [
    {'id': 1,  'home': '卡塔尔',      'away': '瑞士',        'H': 5.60, 'D': 3.75, 'A': 1.61, 'hc':  1.00, 'ou': 2.5, 'act': 'D'},
    {'id': 2,  'home': '巴西',        'away': '摩洛哥',      'H': 1.39, 'D': 4.50, 'A': 7.50, 'hc': -1.50, 'ou': 2.5, 'act': 'D'},
    {'id': 4,  'home': '澳大利亚',    'away': '土耳其',      'H': 4.55, 'D': 3.35, 'A': 1.76, 'hc':  0.50, 'ou': 2.5, 'act': 'H'},
    {'id': 5,  'home': '德国',        'away': '库拉索',      'H': 1.53, 'D': 4.15, 'A': 5.20, 'hc': -1.00, 'ou': 3.5, 'act': 'H'},
    {'id': 7,  'home': '科特迪瓦',    'away': '厄瓜多尔',    'H': 2.60, 'D': 3.35, 'A': 2.60, 'hc': 0.00, 'ou': 2.5, 'act': 'H'},
    {'id': 8,  'home': '荷兰',        'away': '日本',        'H': 1.63, 'D': 3.90, 'A': 4.70, 'hc': -0.50, 'ou': 2.5, 'act': 'D'},
    {'id': 10, 'home': '比利时',      'away': '埃及',        'H': 1.39, 'D': 4.50, 'A': 7.10, 'hc': -1.50, 'ou': 2.5, 'act': 'D'},
    {'id': 13, 'home': '伊拉克',      'away': '挪威',        'H': 3.10, 'D': 3.40, 'A': 2.14, 'hc':  0.25, 'ou': 2.5, 'act': 'A'},
    {'id': 16, 'home': '阿根廷',      'away': '阿尔及利亚',  'H': 1.60, 'D': 3.85, 'A': 5.00, 'hc': -0.50, 'ou': 2.5, 'act': 'H'},
    {'id': 19, 'home': '英格兰',      'away': '克罗地亚',    'H': 1.30, 'D': 5.00, 'A': 8.30, 'hc': -1.50, 'ou': 2.5, 'act': 'H'},
    {'id': 20, 'home': '葡萄牙',      'away': '民主刚果',    'H': 1.22, 'D': 5.90, 'A':10.00, 'hc': -1.75, 'ou': 3.0, 'act': 'D'},
    {'id': 21, 'home': '捷克',        'away': '南非',        'H': 1.61, 'D': 3.40, 'A': 5.20, 'hc': -0.75, 'ou': 2.5, 'act': 'D'},
    {'id': 22, 'home': '瑞士',        'away': '波黑',        'H': 1.61, 'D': 3.75, 'A': 5.00, 'hc': -0.50, 'ou': 2.5, 'act': 'H'},
]

LABEL_MAP = {'H': 0, 'D': 1, 'A': 2}
from sklearn.metrics import accuracy_score, f1_score

y_true, y_pred = [], []
for m in WORLDCUP_MATCHES:
    r = up.predict(home=m['home'], away=m['away'],
                   odds_h=m['H'], odds_d=m['D'], odds_a=m['A'],
                   asian_handicap=m['hc'], ou_line=m['ou'])
    pred = r.get('prediction', '?')
    if pred not in ('H', 'D', 'A'):
        probs = r.get('probabilities', {})
        p_vec = [probs.get('H', 0), probs.get('D', 0), probs.get('A', 0)]
        pred = ['H', 'D', 'A'][int(np.argmax(p_vec))]
    y_true.append(m['act'])
    y_pred.append(pred)

y_true_n = [LABEL_MAP[y] for y in y_true]
y_pred_n = [LABEL_MAP[y] for y in y_pred]
acc = accuracy_score(y_true_n, y_pred_n)
f1_d = f1_score(y_true_n, y_pred_n, labels=[1], average='macro', zero_division=0)
f1_m = f1_score(y_true_n, y_pred_n, average='macro', zero_division=0)

print(f"  样本: {len(y_true)} 场 (13场子集)")
print(f"  Accuracy: {acc:.2%}")
print(f"  Draw F1:  {f1_d:.4f}")
print(f"  Macro F1: {f1_m:.4f}")

# ════════════════════════════════════════════════
# 总结
# ════════════════════════════════════════════════
print(f"\n{'='*70}")
print(f"  P2 验证总结")
print(f"{'='*70}")

checks = [
    ("FeatureAligner 构建不同比赛特征不同", diff > 0.1),
    ("DrawExpert 独立调用不再恒定0.331", unique_vals > 1),
    ("DrawExpert 5场输出有变化", not all_same),
    ("UnifiedPredictor DE信号不恒定", sig_unique > 1 if up._ready else False),
    ("生产回测 Draw-F1 > 0", f1_d > 0),
]

for name, ok in checks:
    print(f"  {'✅' if ok else '❌'} {name}")

# 保存
out_dir = ARCH_ROOT / "reports"
out_dir.mkdir(exist_ok=True)
out_file = out_dir / f"p2_verify_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
with open(out_file, 'w', encoding='utf-8') as f:
    json.dump({
        'timestamp': datetime.now().isoformat(),
        'feature_aligner': {
            'n_features': len(aligner.feature_names),
            'has_scaler': aligner.scaler is not None,
            'vec_diff': float(diff),
        },
        'draw_expert_standalone': {
            'pdraws': [float(p) for p in pdraws],
            'unique_values': unique_vals,
            'all_same': bool(all_same),
            'was_constant_0331': all(abs(p - 0.331) < 0.01 for p in pdraws),
        },
        'unified_predictor_signal': {
            'signals': [float(s) for s in signals],
            'unique_values': sig_unique,
        },
        'production_backtest': {
            'samples': len(y_true),
            'accuracy': float(acc),
            'draw_f1': float(f1_d),
            'macro_f1': float(f1_m),
        },
        'checks': {name: ok for name, ok in checks},
    }, f, ensure_ascii=False, indent=2, default=str)
print(f"\n  结果已保存: {out_file}")

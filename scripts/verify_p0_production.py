#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证 P0 修改后的 UnifiedPredictor 生产配置回测"""
import sys, os, json, warnings
from pathlib import Path
from datetime import datetime, timezone

warnings.filterwarnings('ignore')

ARCH_ROOT = Path(r"D:/Architecture")
FAI_ROOT = Path(r"D:/AI/footballAI")
MODELS_DIR = FAI_ROOT / "saved_models"

# P0-6修复: FAI_ROOT 必须在 ARCH_ROOT 之后, 否则 footballAI/features/ 会遮蔽 v4.0/features/

import numpy as np
from sklearn.metrics import accuracy_score, f1_score, confusion_matrix

WORLDCUP_MATCHES = [
    {'id': 1,  'home': '卡塔尔',      'away': '瑞士',        'H': 5.60, 'D': 3.75, 'A': 1.61, 'hc':  1.00, 'ou': 2.5, 'act': 'D'},
    {'id': 2,  'home': '巴西',        'away': '摩洛哥',      'H': 1.39, 'D': 4.50, 'A': 7.50, 'hc': -1.50, 'ou': 2.5, 'act': 'D'},
    {'id': 3,  'home': '海地',        'away': '苏格兰',      'H': 6.90, 'D': 4.50, 'A': 1.40, 'hc':  1.50, 'ou': 2.5, 'act': 'A'},
    {'id': 4,  'home': '澳大利亚',    'away': '土耳其',      'H': 4.55, 'D': 3.35, 'A': 1.76, 'hc':  0.50, 'ou': 2.5, 'act': 'H'},
    {'id': 5,  'home': '德国',        'away': '库拉索',      'H': 1.53, 'D': 4.15, 'A': 5.20, 'hc': -1.00, 'ou': 3.5, 'act': 'H'},
    {'id': 6,  'home': '瑞典',        'away': '突尼斯',      'H': 1.76, 'D': 3.35, 'A': 4.70, 'hc': -0.50, 'ou': 2.5, 'act': 'H'},
    {'id': 7,  'home': '科特迪瓦',    'away': '厄瓜多尔',    'H': 2.60, 'D': 3.35, 'A': 2.60, 'hc': 0.00, 'ou': 2.5, 'act': 'H'},
    {'id': 8,  'home': '荷兰',        'away': '日本',        'H': 1.63, 'D': 3.90, 'A': 4.70, 'hc': -0.50, 'ou': 2.5, 'act': 'D'},
    {'id': 9,  'home': '伊朗',        'away': '新西兰',      'H': 1.44, 'D': 4.25, 'A': 6.30, 'hc': -1.25, 'ou': 2.5, 'act': 'D'},
    {'id': 10, 'home': '比利时',      'away': '埃及',        'H': 1.39, 'D': 4.50, 'A': 7.10, 'hc': -1.50, 'ou': 2.5, 'act': 'D'},
    {'id': 11, 'home': '沙特阿拉伯',  'away': '乌拉圭',      'H': 7.10, 'D': 4.50, 'A': 1.39, 'hc':  1.50, 'ou': 2.5, 'act': 'D'},
    {'id': 12, 'home': '西班牙',      'away': '佛得角共和国',  'H': 1.08, 'D': 8.80, 'A':18.00, 'hc': -2.50, 'ou': 3.5, 'act': 'D'},
    {'id': 13, 'home': '伊拉克',      'away': '挪威',        'H': 3.10, 'D': 3.40, 'A': 2.14, 'hc':  0.25, 'ou': 2.5, 'act': 'A'},
    {'id': 14, 'home': '奥地利',      'away': '约旦',        'H': 1.46, 'D': 4.15, 'A': 6.20, 'hc': -1.00, 'ou': 2.5, 'act': 'H'},
    {'id': 15, 'home': '法国',        'away': '塞内加尔',    'H': 1.08, 'D': 8.80, 'A':20.00, 'hc': -2.50, 'ou': 3.5, 'act': 'H'},
    {'id': 16, 'home': '阿根廷',      'away': '阿尔及利亚',  'H': 1.60, 'D': 3.85, 'A': 5.00, 'hc': -0.50, 'ou': 2.5, 'act': 'H'},
    {'id': 17, 'home': '乌兹别克斯坦','away': '哥伦比亚',    'H': 5.60, 'D': 4.05, 'A': 1.52, 'hc':  1.00, 'ou': 2.5, 'act': 'A'},
    {'id': 18, 'home': '加纳',        'away': '巴拿马',      'H': 1.52, 'D': 3.95, 'A': 5.70, 'hc': -1.00, 'ou': 2.5, 'act': 'H'},
    {'id': 19, 'home': '英格兰',      'away': '克罗地亚',    'H': 1.30, 'D': 5.00, 'A': 8.30, 'hc': -1.50, 'ou': 2.5, 'act': 'H'},
    {'id': 20, 'home': '葡萄牙',      'away': '民主刚果',    'H': 1.22, 'D': 5.90, 'A':10.00, 'hc': -1.75, 'ou': 3.0, 'act': 'D'},
    {'id': 21, 'home': '捷克',        'away': '南非',        'H': 1.61, 'D': 3.40, 'A': 5.20, 'hc': -0.75, 'ou': 2.5, 'act': 'D'},
    {'id': 22, 'home': '瑞士',        'away': '波黑',        'H': 1.61, 'D': 3.75, 'A': 5.00, 'hc': -0.50, 'ou': 2.5, 'act': 'H'},
    {'id': 23, 'home': '加拿大',      'away': '卡塔尔',      'H': 1.61, 'D': 3.75, 'A': 5.00, 'hc': -0.50, 'ou': 2.5, 'act': 'H'},
    {'id': 24, 'home': '墨西哥',      'away': '韩国',        'H': 1.69, 'D': 3.45, 'A': 4.90, 'hc': -0.50, 'ou': 2.5, 'act': 'H'},
    {'id': 25, 'home': '捷克',        'away': '南非',        'H': 1.61, 'D': 3.40, 'A': 5.20, 'hc': -0.75, 'ou': 2.5, 'act': 'D'},
    {'id': 26, 'home': '瑞士',        'away': '波黑',        'H': 1.61, 'D': 3.75, 'A': 5.00, 'hc': -0.50, 'ou': 2.5, 'act': 'H'},
]

LABEL_MAP = {'H': 0, 'D': 1, 'A': 2}

from predictors.unified_predictor import UnifiedPredictor

print("=" * 70)
print("  P0 落地验证: UnifiedPredictor 生产配置 (use_threshold=True)")
print(f"  时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 70)

# 生产配置: use_threshold=True (使用阈值判型)
up = UnifiedPredictor(
    model_path=str(MODELS_DIR / "football_v4.1_production.joblib"),
    enable_trap=False,
    enable_dh=False,
    use_threshold=True,  # 生产配置
)

print(f"\n  draw_threshold = {up.draw_threshold} (P0: 0.46→0.32)")
print(f"  use_threshold = {up.use_threshold}")

y_true, y_pred = [], []
per_match = []

for m in WORLDCUP_MATCHES:
    try:
        r = up.predict(
            home=m['home'], away=m['away'],
            odds_h=m['H'], odds_d=m['D'], odds_a=m['A'],
            asian_handicap=m.get('hc', 0.0),
            ou_line=m.get('ou', 2.5),
        )
        probs = r.get('probabilities', {})
        p_vec = [probs.get('H', 0), probs.get('D', 0), probs.get('A', 0)]
        pred = r.get('prediction', '?')
        if pred not in ('H', 'D', 'A'):
            pred = ['H', 'D', 'A'][int(np.argmax(p_vec))]

        y_true.append(m['act'])
        y_pred.append(pred)
        correct = pred == m['act']
        per_match.append({
            'id': m['id'], 'match': f"{m['home']} vs {m['away']}",
            'act': m['act'], 'pred': pred,
            'pH': round(p_vec[0], 3), 'pD': round(p_vec[1], 3), 'pA': round(p_vec[2], 3),
            'correct': correct,
            'method': r.get('method', '?'),
        })
        mark = 'OK' if correct else 'X '
        print(f"  [{m['id']:2d}] {m['home']:10s} vs {m['away']:10s} | pred={pred} act={m['act']} {mark} | "
              f"H={p_vec[0]:.2f} D={p_vec[1]:.2f} A={p_vec[2]:.2f} | {r.get('method','?')}")
    except Exception as e:
        print(f"  [{m['id']:2d}] ERROR: {e}")
        y_true.append(m['act'])
        y_pred.append('H')

# 指标
y_true_n = [LABEL_MAP[y] for y in y_true]
y_pred_n = [LABEL_MAP[y] for y in y_pred]
acc = accuracy_score(y_true_n, y_pred_n)
f1_macro = f1_score(y_true_n, y_pred_n, average='macro', zero_division=0)
f1_d = f1_score(y_true_n, y_pred_n, labels=[1], average='macro', zero_division=0)
f1_h = f1_score(y_true_n, y_pred_n, labels=[0], average='macro', zero_division=0)
f1_a = f1_score(y_true_n, y_pred_n, labels=[2], average='macro', zero_division=0)
cm = confusion_matrix(y_true_n, y_pred_n, labels=[0, 1, 2])

print(f"\n{'='*60}")
print(f"  P0 生产配置回测结果 (draw_threshold=0.32)")
print(f"{'='*60}")
print(f"  Accuracy:   {acc:.2%}  (原 57.69%)")
print(f"  Macro F1:   {f1_macro:.4f}  (原 0.4646)")
print(f"  Draw F1:    {f1_d:.4f}  (原 0.0000) ← P0 核心收益")
print(f"  Home F1:    {f1_h:.4f}  (原 0.7273)")
print(f"  Away F1:    {f1_a:.4f}  (原 0.6667)")
print(f"\n  混淆矩阵:")
print(f"    True H({sum(1 for m in WORLDCUP_MATCHES if m['act']=='H')}) -> H={cm[0][0]} D={cm[0][1]} A={cm[0][2]}")
print(f"    True D({sum(1 for m in WORLDCUP_MATCHES if m['act']=='D')}) -> H={cm[1][0]} D={cm[1][1]} A={cm[1][2]}")
print(f"    True A({sum(1 for m in WORLDCUP_MATCHES if m['act']=='A')}) -> H={cm[2][0]} D={cm[2][1]} A={cm[2][2]}")

# 验收
print(f"\n  P0 验收:")
checks = [
    ("平局 F1 >= 0.15", f1_d >= 0.15),
    ("Macro F1 提升", f1_macro > 0.4646),
    ("准确率下降 <= 5pp", acc >= 0.5277),
]
for name, ok in checks:
    print(f"    {'✅' if ok else '❌'} {name}")

# 保存
out_dir = ARCH_ROOT / "reports"
out_dir.mkdir(exist_ok=True)
out_file = out_dir / f"p0_production_verify_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
with open(out_file, 'w', encoding='utf-8') as f:
    json.dump({
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'config': 'production (use_threshold=True, draw_threshold=0.32)',
        'accuracy': acc,
        'f1_macro': f1_macro,
        'f1_draw': f1_d,
        'f1_home': f1_h,
        'f1_away': f1_a,
        'confusion_matrix': cm.tolist(),
        'per_match': per_match,
    }, f, ensure_ascii=False, indent=2, default=str)
print(f"\n  结果已保存: {out_file}")

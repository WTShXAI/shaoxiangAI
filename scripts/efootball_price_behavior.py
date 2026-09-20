#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""电子域价格行为实验 (2026-09-20) — 模拟域最后一张牌
================================================================================
问题: 生成器的下一步价格动作是否可预测? (不预测赛果, 预测 ph=去水主胜概率的方向)
样本: (比赛, 变价点 t) — 从 ef_seq 序列展开; |Δph|>0.002 才算真动作 (滤微噪声)。
特征: t 时刻 17 维状态 + 上一拍 17 维变动量 + 序列位置。
对照: 50% / 类先验 / 动量(重复上一方向)。
判定: 方向准确率 >55% 且胜过动量 → 引擎行为指纹存在 (下一步: 幅度建模)。
用法: python scripts/efootball_price_behavior.py
"""
import json
import sqlite3
import sys

sys.path.insert(0, r'D:\Architecture')
import numpy as np

EV_DB = r'D:\Architecture\data\events.db'
OUT = r'D:\Architecture\reports\efootball_price_behavior.json'
MOVE_EPS = 0.002


def load():
    ev = sqlite3.connect(f'file:{EV_DB}?mode=ro', uri=True)
    rows = ev.execute("SELECT seq_json, mgt FROM ef_seq").fetchall()
    ev.close()
    return [(json.loads(s), g) for s, g in rows]


def main():
    seqs = load()
    X, y, momentum = [], [], []
    mgts = []
    for seq, mgt in seqs:
        arr = np.array(seq, dtype=np.float32)
        if len(arr) < 4:
            continue
        prev_state = None
        for t in range(len(arr) - 1):
            d = float(arr[t + 1][0] - arr[t][0])
            if abs(d) < MOVE_EPS:
                continue
            feat = list(arr[t]) + [t / max(1, len(arr) - 1)]
            if prev_state is not None:
                feat += [float(v) for v in (arr[t] - prev_state)]
            else:
                feat += [0.0] * 17
            X.append(feat)
            y.append(1 if d > 0 else 0)
            mgts.append(mgt or 0)
            prev_state = arr[t]
    X = np.array(X, dtype=np.float32)
    y = np.array(y)
    order = np.argsort(mgts)
    X, y = X[order], y[order]
    print(f'样本 {len(y)} 个变价点 | 上行占比 {y.mean():.3f}')

    from sklearn.model_selection import TimeSeriesSplit
    from lightgbm import LGBMClassifier
    folds = list(TimeSeriesSplit(n_splits=5).split(X))
    accs, prior_accs, moms = [], [], []
    for tr, te in folds:
        m = LGBMClassifier(n_estimators=200, learning_rate=0.05, max_depth=5,
                           verbose=-1, random_state=42)
        m.fit(X[tr], y[tr])
        pm = m.predict(X[te])
        accs.append((pm == y[te]).mean())
        prior_accs.append(max(y[te].mean(), 1 - y[te].mean()))
        # 动量基线: 上一变动方向 = 特征第 17+17 列 (Δph_prev)
        prev_dir = (X[te][:, 17] > 0).astype(int)
        moms.append((prev_dir == y[te]).mean())
    acc = float(np.mean(accs)); prior = float(np.mean(prior_accs)); mom = float(np.mean(moms))
    rep = {'n_points': int(len(y)), 'up_ratio': round(float(y.mean()), 4),
           'model_acc': round(acc, 4), 'prior_acc': round(prior, 4), 'momentum_acc': round(mom, 4),
           'verdict': ('引擎行为指纹存在 (>55% 且胜动量)' if acc > 0.55 and acc > mom + 0.01
                       else '方向不可预测 / 不胜动量 — 生成器价格行为近随机游走')}
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(f'→ {OUT}')


if __name__ == '__main__':
    main()

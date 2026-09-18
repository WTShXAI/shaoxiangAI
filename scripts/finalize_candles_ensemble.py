#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""固化K线集成模型 (2026-09-15, E4 采纳后的产物固化).

4轮walkforward验证通过 (平均+3.15pp vs 现任) 后, 全量训练并持久化:
  - data/models/candles_ensemble/lgb_candles_static.joblib  (LightGBM: 111维K线+8维静态)
  - data/models/candles_ensemble/transformer.pt             (4层decoder-only, 12×14序列)
  - data/models/candles_ensemble/meta.json                  (特征口径/训练窗口/验证数字)

推理入口: pipeline/odds_candles_predict.py (load_ensemble → predict_direction)
"""
import json
import sys
import time

sys.path.insert(0, r'D:\Architecture')
import numpy as np

from analysis.live_goal_probe import _open_gq
from scripts.train_odds_candles_model import build_dataset
from scripts.train_odds_candles_model import lgb_walkforward, train_transformer  # noqa
from pipeline.odds_candles import candle_flat_features, candle_sequence  # noqa

OUT_DIR = r'D:\Architecture\data\models\candles_ensemble'


def main():
    import os
    import joblib
    import lightgbm as lgb
    import torch
    os.makedirs(OUT_DIR, exist_ok=True)

    con = _open_gq()
    # M2 升级 (2026-09-18): 窗口 26→70 天全语料, walkforward 同协议对照过线
    # (TOP1 47.5% vs 46.8%, LL 1.0264 vs 1.0343 — reports/candles_window_{26,70}d.json)
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 26
    rows = build_dataset(con, days=days, max_matches=20000)
    if len(rows) < 500:
        print('样本不足, 退出'); return
    y = np.array([r['label'] for r in rows])
    X_c = np.array([r['candles'] for r in rows], dtype=np.float32)
    X_s = np.array([r['static'] for r in rows], dtype=np.float32)
    X = np.hstack([X_c, X_s])

    # 1. LightGBM (candles+static)
    t0 = time.time()
    m = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, max_depth=5,
                           verbose=-1, random_state=42)
    m.fit(X, y)
    joblib.dump(m, rf'{OUT_DIR}\lgb_candles_static.joblib')
    print(f'LGB 已保存 [{time.time()-t0:.0f}s]')

    # 2. Transformer 全量重训 (复用训练函数, 单折=全集, 早停留尾10%)
    import torch.nn as nn
    torch.manual_seed(42)
    d_model, n_layers, n_heads = 64, 4, 4

    class Block(nn.Module):
        def __init__(self):
            super().__init__()
            self.ln1 = nn.LayerNorm(d_model)
            self.attn = nn.MultiheadAttention(d_model, n_heads, batch_first=True)
            self.ln2 = nn.LayerNorm(d_model)
            self.ff = nn.Sequential(nn.Linear(d_model, d_model*2), nn.GELU(), nn.Linear(d_model*2, d_model))
        def forward(self, x, attn_mask):
            h = self.ln1(x)
            a, _ = self.attn(h, h, h, attn_mask=attn_mask)
            x = x + a
            return x + self.ff(self.ln2(x))

    class Model(nn.Module):
        def __init__(self, F=14, n_cls=3):
            super().__init__()
            self.proj = nn.Linear(F, d_model)
            self.pos = nn.Parameter(torch.zeros(1, 12, d_model))
            self.blocks = nn.ModuleList(Block() for _ in range(n_layers))
            self.lnf = nn.LayerNorm(d_model)
            self.head = nn.Linear(d_model, n_cls)
        def forward(self, seq):
            T = seq.shape[1]
            x = self.proj(seq) + self.pos[:, :T]
            mask = torch.triu(torch.ones(T, T, dtype=torch.bool), diagonal=1)
            for b in self.blocks:
                x = b(x, mask)
            return self.head(self.lnf(x[:, -1]))

    seqs = torch.tensor(np.stack([r['seq'] for r in rows]))
    model = Model()
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    cls_cnt = np.bincount(y, minlength=3)
    w = torch.tensor(cls_cnt.sum() / (3.0 * np.maximum(cls_cnt, 1)), dtype=torch.float32)
    lossf = nn.CrossEntropyLoss(weight=w)
    n = len(seqs)
    cut = int(n * 0.95)
    best_state, best_val, patience = None, -1, 0
    for ep in range(30):
        model.train()
        perm = torch.randperm(cut)
        for i in range(0, cut, 128):
            bidx = perm[i:i+128]
            loss = lossf(model(seqs[bidx]), torch.tensor(y[bidx]))
            opt.zero_grad(); loss.backward(); opt.step()
        model.eval()
        with torch.no_grad():
            val_acc = (model(seqs[cut:]).argmax(1) == torch.tensor(y[cut:])).float().mean().item()
        if val_acc > best_val:
            best_val, patience = val_acc, 0
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
        else:
            patience += 1
            if patience >= 6:
                break
    if best_state:
        model.load_state_dict(best_state)
    torch.save(model.state_dict(), rf'{OUT_DIR}\transformer.pt')
    print(f'Transformer 已保存 (内部验证 {best_val*100:.1f}%)')

    # 3. 元数据
    meta = {
        'trained_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'window_days': days, 'n_matches': len(rows),
        'val_protocol': ('2026-09-18 M2全语料升级: 窗口对照 70d vs 26d 同协议 (TimeSeriesSplit5, seed42, '
                         'LGB+transformer集成), 双指标过线: TOP1 47.5% vs 46.8%, LogLoss 1.0264 vs 1.0343; '
                         '历史基线: walkforward 4轮 vs static+traj 现任 +3.15pp'),
        'val_results': {
            'window_compare': {'top1_70d': 47.5, 'top1_26d': 46.8,
                               'll_70d': 1.0264, 'll_26d': 1.0343,
                               'n_70d': 3825, 'n_26d': 3219},
            'G_ensemble_vs_incumbent_pp': [3.1, 3.5, 2.3, 3.7],
            'majority_baseline': 39.6,
        },
        'features': {
            'lgb': '111维K线(12桶×3通道×{ret,amp,tick}+尾桶水平) + 8维开盘去水静态',
            'transformer': '12×14序列(每桶3通道OHLC+log tick+is_new), 因果注意力, 末位置读出',
        },
        'iron_rules': '全特征=去水概率变化量/计数, 无原始赔率值; 时间轴=距开赛偏移',
    }
    with open(rf'{OUT_DIR}\meta.json', 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print(f'meta.json 已保存 → {OUT_DIR}')


if __name__ == '__main__':
    main()

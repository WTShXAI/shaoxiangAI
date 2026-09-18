#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""K线集成赛前方向推理 (2026-09-15, E4 采纳后的生产入口).

Kronos 移植: 赔率K线化 → 双模型 (LightGBM + 小型decoder-only transformer) → 概率平均。
验证: 4轮walkforward, TOP1 平均比现任 static+traj 高 +3.15pp (采纳线+2pp)。

用法:
  from pipeline.odds_candles_predict import predict_match
  verdict = predict_match(con, match_key)   # {'direction': 'home'|'draw'|'away',
                                            #  'probs': {'home':..,'draw':..,'away':..},
                                            #  'confidence': float, 'ok': bool}
"""
import json
import os
import sys

sys.path.insert(0, r'D:\Architecture')
import numpy as np

MODEL_DIR = r'D:\Architecture\data\models\candles_ensemble'
CLS = ('home', 'draw', 'away')


class Ensemble:
    def __init__(self):
        import joblib
        import torch
        import torch.nn as nn
        self.torch = torch
        self.lgb = joblib.load(os.path.join(MODEL_DIR, 'lgb_candles_static.joblib'))
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

        self.net = Model()
        self.net.load_state_dict(torch.load(os.path.join(MODEL_DIR, 'transformer.pt'), map_location='cpu'))
        self.net.eval()
        with open(os.path.join(MODEL_DIR, 'meta.json'), encoding='utf-8') as f:
            self.meta = json.load(f)

    def predict(self, xc_flat, seq):
        """xc_flat: (111,) K线扁平特征 + 静态8维在 predict_match 里拼; seq: (12,14)."""
        raise NotImplementedError


def build_feature_vector(con, match_key, kickoff_ts=None):
    """match_key → (x_lgb (119,), seq (12,14)) 或 None (赛前数据不足).

    特征口径与训练一致: 开盘=赛前每通道最低赔率 (与 scripts/train_odds_candles_model.build_dataset 相同)。
    """
    from pipeline.odds_candles import load_match_events, parse_kickoff_ts, candleize_1x2, \
        candle_flat_features, candle_sequence
    if kickoff_ts is None:
        row = con.execute("SELECT kickoff FROM matches WHERE match_key=?", (match_key,)).fetchone()
        if not row or not row[0]:
            return None
        kickoff_ts = parse_kickoff_ts(row[0])
        if kickoff_ts is None:
            return None
    events = load_match_events(con, match_key)
    pre = [e for e in events if e['captured_at'] <= kickoff_ts]
    if len(pre) < 20:
        return None
    op = {}
    for sel in ('home', 'draw', 'away'):
        vs = [e['to_odds'] for e in pre if e['selection'] == sel]
        op[sel] = min(vs) if vs else None
    if any(v is None or v <= 1.001 for v in op.values()):
        return None
    h0, d0, a0 = op['home'], op['draw'], op['away']
    inv = [1/h0, 1/d0, 1/a0]
    s0 = sum(inv)
    ph, pd_, pa = inv[0]/s0, inv[1]/s0, inv[2]/s0
    xs = [ph, pd_, pa, ph*pd_, ph*pa, pd_*pa, ph+pd_, ph+pa]
    cand = candleize_1x2(events, kickoff_ts)
    if cand is None:
        return None
    xc = candle_flat_features(cand)
    seq, _ = candle_sequence(cand)
    return np.array(xc + xs, dtype=np.float32), seq.astype(np.float32), len(pre)


def predict_match(con, match_key, kickoff_ts=None):
    """赛前方向三分类: LGB + transformer 概率平均. 数据不足返回 ok=False."""
    feat = build_feature_vector(con, match_key, kickoff_ts)
    if feat is None:
        return {'ok': False, 'reason': '赛前K线数据不足(<20 tick或通道缺失)'}
    x, seq, n_ticks = feat
    ens = _get_ensemble()
    p_lgb = ens['lgb'].predict_proba(x.reshape(1, -1))[0]
    torch = ens['torch']
    with torch.no_grad():
        logits = ens['net'](torch.tensor(seq).unsqueeze(0))
        p_net = torch.softmax(logits, dim=1).numpy()[0]
    p = 0.5 * p_lgb + 0.5 * p_net
    ci = int(p.argmax())
    return {
        'ok': True,
        'direction': CLS[ci],
        'probs': {c: round(float(p[i]), 4) for i, c in enumerate(CLS)},
        'confidence': round(float(p[ci]), 4),
        'margin': round(float(p[ci] - sorted(p)[-2]), 4),
        'n_ticks': int(n_ticks),
        'models': {'lgb_top': CLS[int(p_lgb.argmax())], 'transformer_top': CLS[int(p_net.argmax())]},
    }


_ENS = None


def _get_ensemble():
    global _ENS
    if _ENS is None:
        ens = Ensemble()
        _ENS = {'lgb': ens.lgb, 'net': ens.net, 'torch': ens.torch, 'obj': ens}
    return _ENS


if __name__ == '__main__':
    con = _open_gq() if False else None
    from analysis.live_goal_probe import _open_gq
    con = _open_gq()
    # 抽3场近期已完赛演示
    rows = con.execute("""SELECT match_key FROM matches WHERE status='finished'
        AND score_home IS NOT NULL AND kickoff >= datetime('now','-3 day') LIMIT 3""").fetchall()
    for (mk,) in rows:
        r = predict_match(con, mk)
        print(mk[:36], '→', r)

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""E6: 哨响-mini 多市场多任务模型 (2026-09-16, Kronos 章节收官实验).

7 概率通道 (1X2×3 + OU2.5×2 + AH0×2) × 12桶赛前K线 →
  H1: 多通道 LightGBM (1X2 三分类)
  H2: 哨响-mini decoder-only (E3 同构, F 扩到 37), 多任务双头:
      1X2 三分类 + OU2.5 大小二分类 (OU 第一次有序列模型)
对照: 现任 C(static+traj) + E4 集成 ( candles+static LGB + E3 transformer )
纪律: 同 build 窗口 / 同 TimeSeriesSplit(5) / +2pp 采纳线 / 多轮稳健性。

用法: python scripts/train_odds_multi_model.py [--days 26] [--seed 42]
"""
import argparse
import json
import math
import sys
import time
import warnings

sys.path.insert(0, r'D:\Architecture')
warnings.filterwarnings('ignore')
import numpy as np


def build_multi_dataset(con, days=26, max_matches=12000):
    from pipeline.odds_candles_multi import (load_events_multi, candleize_multi,
                                             multi_flat_features, multi_sequence)
    from pipeline.odds_candles import parse_kickoff_ts
    mrows = con.execute("""
        SELECT match_key, kickoff, score_home, score_away FROM matches
        WHERE status='finished' AND score_home IS NOT NULL AND kickoff IS NOT NULL
        AND kickoff >= datetime('now', ?) ORDER BY kickoff ASC LIMIT ?""",
        (f'-{days} day', max_matches)).fetchall()
    rows = []
    skipped = 0
    t0 = time.time()
    for mk, ko, fsh, fsa in mrows:
        try:
            ko_ts = parse_kickoff_ts(ko)
            if ko_ts is None:
                skipped += 1; continue
            ev = load_events_multi(con, mk)
            # 严格赛前过滤 (load_events_multi 返回含滚球, static/覆盖判定都必须只用赛前)
            pre_1x2 = [e for e in ev['1X2'] if e['captured_at'] <= ko_ts]
            if len(pre_1x2) < 20:
                skipped += 1; continue
            cand = candleize_multi(ev, ko_ts)
            if cand is None:
                skipped += 1; continue
            total = int(fsh) + int(fsa)
            # 静态 8 维 (与 E1-E4 同口径: 赛前每通道最低赔率去水)
            op = {}
            for sel in ('home', 'draw', 'away'):
                vs = [e['to_odds'] for e in pre_1x2 if e['selection'] == sel]
                op[sel] = min(vs) if vs else None
            if any(v is None or v <= 1.001 for v in op.values()):
                skipped += 1; continue
            inv = [1/op['home'], 1/op['draw'], 1/op['away']]
            s0 = sum(inv)
            ph, pd_, pa = inv[0]/s0, inv[1]/s0, inv[2]/s0
            xs = [ph, pd_, pa, ph*pd_, ph*pa, pd_*pa, ph+pd_, ph+pa]

            label = 0 if fsh > fsa else (1 if fsh == fsa else 2)
            ou_label = 1 if total > 2.5 else 0  # over=1, under=0 (盘口2.5无走盘)
            has_ou = int(cand['presence'][3])
            rows.append({'match_key': mk, 'kickoff': ko, 'label': label,
                         'ou_label': ou_label, 'has_ou': has_ou,
                         'static': xs,
                         'multi_flat': multi_flat_features(cand),
                         'multi_seq': multi_sequence(cand)[0]})
        except Exception:
            skipped += 1
            continue
    print(f'数据集: {len(rows)} 场 (跳过 {skipped}) [{time.time()-t0:.0f}s]')
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=26)
    ap.add_argument('--max', type=int, default=12000)
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--save', default='')
    args = ap.parse_args()

    from analysis.live_goal_probe import _open_gq
    from scripts.train_odds_candles_model import build_dataset, lgb_walkforward
    from sklearn.model_selection import TimeSeriesSplit

    con = _open_gq()
    rows = build_multi_dataset(con, days=args.days, max_matches=args.max)
    if len(rows) < 300:
        print('样本不足'); return
    y = np.array([r['label'] for r in rows])
    y_ou = np.array([r['ou_label'] for r in rows])
    has_ou = np.array([r['has_ou'] for r in rows])
    kickoffs = [r['kickoff'] for r in rows]
    assert kickoffs == sorted(kickoffs)
    folds = list(TimeSeriesSplit(n_splits=5).split(np.zeros(len(y))))

    # ── 对照 1: 现任 C (需从原 build 拿 traj) — 用原 build_dataset 对齐 match_key
    rows_e1 = build_dataset(con, days=args.days, max_matches=args.max)
    e1_idx = {r['match_key']: i for i, r in enumerate(rows_e1)}
    common = [r for r in rows if r['match_key'] in e1_idx]
    if len(common) >= 300:
        y_c = np.array([rows_e1[e1_idx[r['match_key']]]['label'] for r in common])
        tdim = len(rows_e1[0]['traj'] or [0]*12)
        X_t = np.array([rows_e1[e1_idx[r['match_key']]]['traj'] or [np.nan]*tdim for r in common], dtype=np.float32)
        mu = np.nanmean(X_t, 0); X_t = np.where(np.isnan(X_t), mu, X_t)
        X_s = np.array([rows_e1[e1_idx[r['match_key']]]['static'] for r in common], dtype=np.float32)
        folds_c = list(TimeSeriesSplit(n_splits=5).split(np.zeros(len(y_c))))
        acc_c, _ = lgb_walkforward(np.hstack([X_s, X_t]), y_c, folds_c)
        print(f'  对照 现任 C(static+traj): {np.mean(acc_c)*100:.1f}% (对齐 {len(common)} 场)')
    else:
        acc_c = [0.441]; print('  对照集不足, 用历史值 44.1%')

    # ── H1: 多通道 LightGBM (multi_flat + static)
    X_m = np.array([r['multi_flat'] for r in rows], dtype=np.float32)
    X_s2 = np.array([r['static'] for r in rows], dtype=np.float32)
    acc_h1, _ = lgb_walkforward(np.hstack([X_m, X_s2]), y, folds)
    print(f'  H1 多通道LGB(1X2):       {np.mean(acc_h1)*100:.1f}% ± {np.std(acc_h1)*100:.1f}%')

    # ── H1-OU: OU 大小二分类 (仅 has_ou 场)
    idx_ou = np.where(has_ou == 1)[0]
    y_ou2 = y_ou[idx_ou]
    folds_ou = list(TimeSeriesSplit(n_splits=5).split(np.zeros(len(idx_ou))))
    acc_h1ou, _ = lgb_walkforward(X_m[idx_ou], y_ou2, folds_ou)
    base_ou = max(np.bincount(y_ou2)) / len(y_ou2)
    print(f'  H1 多通道LGB(OU大小):    {np.mean(acc_h1ou)*100:.1f}% (多数类 {base_ou*100:.1f}%, n={len(idx_ou)})')

    # ── H2: 哨响-mini 多任务双头 (E3 同构, F=37)
    import torch
    import torch.nn as nn
    d_model, n_layers, n_heads = 64, 4, 4
    F_dim = rows[0]['multi_seq'].shape[1]

    class Block(nn.Module):
        def __init__(self):
            super().__init__()
            self.ln1 = nn.LayerNorm(d_model)
            self.attn = nn.MultiheadAttention(d_model, n_heads, batch_first=True)
            self.ln2 = nn.LayerNorm(d_model)
            self.ff = nn.Sequential(nn.Linear(d_model, d_model*2), nn.GELU(), nn.Linear(d_model*2, d_model))
        def forward(self, x, am):
            h = self.ln1(x); a, _ = self.attn(h, h, h, attn_mask=am)
            return x + a + self.ff(self.ln2(x + a))

    class Mini(nn.Module):
        def __init__(self):
            super().__init__()
            self.proj = nn.Linear(F_dim, d_model)
            self.pos = nn.Parameter(torch.zeros(1, 12, d_model))
            self.blocks = nn.ModuleList(Block() for _ in range(n_layers))
            self.lnf = nn.LayerNorm(d_model)
            self.head_1x2 = nn.Linear(d_model, 3)
            self.head_ou = nn.Linear(d_model, 2)
        def forward(self, seq):
            T = seq.shape[1]
            x = self.proj(seq) + self.pos[:, :T]
            am = torch.triu(torch.ones(T, T, dtype=torch.bool), diagonal=1)
            for b in self.blocks:
                x = b(x, am)
            return self.lnf(x[:, -1])

    seqs = torch.tensor(np.stack([r['multi_seq'] for r in rows]))
    torch.manual_seed(args.seed)
    acc_h2_1x2, acc_h2_ou = [], []
    for fi, (tr, te) in enumerate(folds):
        torch.manual_seed(args.seed + fi)
        model = Mini()
        opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=0.01)
        cnt1 = np.bincount(y[tr], minlength=3)
        w1 = torch.tensor(cnt1.sum()/(3.0*np.maximum(cnt1, 1)), dtype=torch.float32)
        cnt2 = np.bincount(y_ou[tr], minlength=2)
        w2 = torch.tensor(cnt2.sum()/(2.0*np.maximum(cnt2, 1)), dtype=torch.float32)
        lf1, lf2 = nn.CrossEntropyLoss(weight=w1), nn.CrossEntropyLoss(weight=w2)
        ho = torch.tensor(has_ou[tr], dtype=torch.float32)
        n = len(tr); cut = int(n*0.9)
        best, patience, best_state = -1, 0, None
        for ep in range(30):
            model.train()
            perm = torch.randperm(cut)
            for i in range(0, cut, 128):
                b = perm[i:i+128].numpy()
                h = model(seqs[torch.tensor(b)])
                loss = lf1(model.head_1x2(h), torch.from_numpy(y[tr][b]))
                # OU 头仅 has_ou 样本参与 (掩码)
                m = torch.from_numpy(has_ou[tr][b]).bool()
                if m.any():
                    loss = loss + lf2(model.head_ou(h[m]), torch.from_numpy(y_ou[tr][b][m.numpy()]))
                opt.zero_grad(); loss.backward(); opt.step()
            model.eval()
            with torch.no_grad():
                va = float((model.head_1x2(model(seqs[torch.tensor(list(range(cut, n)))])).argmax(1).numpy()
                            == y[tr][cut:]).mean())
            if va > best:
                best, patience = va, 0
                best_state = {k: v.clone() for k, v in model.state_dict().items()}
            else:
                patience += 1
                if patience >= 5: break
        if best_state: model.load_state_dict(best_state)
        model.eval()
        with torch.no_grad():
            hh = model(seqs[torch.tensor(te)])
            acc_h2_1x2.append(float((model.head_1x2(hh).argmax(1).numpy() == y[te]).mean()))
            mte = torch.from_numpy(has_ou[te]).bool()
            if mte.any():
                acc_h2_ou.append(float((model.head_ou(hh[mte]).argmax(1).numpy() == y_ou[te][mte.numpy()]).mean()))
    print(f'  H2 哨响-mini(1X2):       {np.mean(acc_h2_1x2)*100:.1f}% ± {np.std(acc_h2_1x2)*100:.1f}%')
    if acc_h2_ou:
        print(f'  H2 哨响-mini(OU大小):    {np.mean(acc_h2_ou)*100:.1f}%')

    # ── 结论
    inc = float(np.mean(acc_c))
    table = {'C_incumbent': acc_c, 'H1_multiLGB_1x2': acc_h1_1x2 if False else acc_h1,
             'H1_multiLGB_OU': acc_h1ou, 'H2_mini_1x2': acc_h2_1x2, 'H2_mini_OU': acc_h2_ou}
    print(f'\n── 结论 (现任 {inc*100:.1f}%, 采纳线 +2pp) ──')
    for k in ('H1_multiLGB_1x2', 'H2_mini_1x2'):
        d = (np.mean(table[k]) - inc) * 100
        print(f'  {k:>18}: {np.mean(table[k])*100:5.1f}%  {d:+.1f}pp  '
              f'{"✓过线" if d >= 2 else ("△正向" if d >= 0 else "✗退化")}  '
              f'折: {" ".join(f"{a*100:.0f}" for a in table[k])}')
    if args.save:
        with open(args.save, 'w', encoding='utf-8') as fp:
            json.dump({k: [float(a) for a in v] for k, v in table.items()}, fp, indent=1)
        print(f'结果已存: {args.save}')


if __name__ == '__main__':
    main()

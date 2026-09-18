#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""E5: Kronos 预训练权重迁移 (2026-09-16, "把哨响训练出来" = 借Kronos的脑).

Kronos-mini (4.1M, 121亿K线预训练) → 赔率K线三分类微调。
适配: 赔率概率通道(ph/pd/pa)映射为 OHLCV 序列 → 冻结 tokenizer 编码 →
predictor 出 context → 新分类头(3类)。归一化严格复刻官方 KronosPredictor.predict
(逐样本 z-score + clip±5, 时间戳五分量)。

变体:
  V1  仅 ph 通道, T=12 (最大分布内 — 预训练见到的就是单标的OHLCV)
  V2  三通道块拼接, T=36 (信息全, 读出=各通道末token均值)
  V0  随机初始化同架构 (对照: 证明"预训练知识"本身有贡献, 否则借脑证伪)
模式:
  probe 线性探测 (冻结全部 predictor, 只训头) / full 全量微调 (lr分层)

对照与纪律: 与 train_odds_candles_model 同一 build_dataset / 同一 TimeSeriesSplit(5);
采纳线 vs 现任 C(static+traj) +2pp; 结果 JSON 落盘。

用法: python scripts/train_kronos_transfer.py [--days 26] [--max 12000] [--variant all]
"""
import argparse
import json
import os
import sys
import time
import warnings

sys.path.insert(0, r'D:\Architecture')
sys.path.insert(0, r'D:\Architecture\external\Kronos_src')
warnings.filterwarnings('ignore')
import numpy as np
import torch
import torch.nn as nn

KRONOS_DIR = r'D:\Architecture\data\models\kronos'
CLS = ('home', 'draw', 'away')


def build_kronos_inputs(rows):
    """rows(含 seq 12×14 + kickoff) → 每场 (s1,s2,stamp) 两变体, 冻结tokenizer一次性编码."""
    from model.kronos import KronosTokenizer
    from pipeline.odds_candles import BAR_EDGES_H
    from datetime import datetime
    tok = KronosTokenizer.from_pretrained(os.path.join(KRONOS_DIR, 'Kronos-Tokenizer-2k'))
    tok.eval()

    def ohlcv(seq):
        """seq布局 [ph_o,ph_c,ph_h,ph_l]*3 + tick + is_new → 三通道 (open,high,low,close,vol,amt)."""
        vol = np.expm1(seq[:, 12])  # log1p(ticks) 还原计数 → (12,)
        chs = []
        for c in range(3):
            o, cl = seq[:, 4*c], seq[:, 4*c+1]
            h, l = seq[:, 4*c+2], seq[:, 4*c+3]
            chs.append(np.stack([o, h, l, cl, vol, vol], axis=1))  # (12,6) Kronos列序 O,H,L,C,V,A
        return chs

    def stamps(kickoff_str):
        ko = None
        from pipeline.odds_candles import parse_kickoff_ts
        ko = parse_kickoff_ts(kickoff_str)
        out = []
        for i in range(12):
            mid_h = (BAR_EDGES_H[i] + BAR_EDGES_H[i+1]) / 2
            t = datetime.fromtimestamp(ko - mid_h * 3600)
            out.append([t.minute, t.hour, t.weekday(), t.day, t.month])
        return np.array(out, dtype=np.int64)

    enc_v1 = {'s1': [], 's2': []}
    enc_v2 = {'s1': [], 's2': []}
    sts = []
    B = 256
    seqs_v1, seqs_v2, stamp_list = [], [], []
    for r in rows:
        chs = ohlcv(r['seq'])
        # 逐样本逐通道 z-score + clip ±5 (复刻官方)
        def norm(x):
            mu, sd = x.mean(0), x.std(0)
            return np.clip((x - mu) / (sd + 1e-5), -5, 5)
        chs = [norm(c.astype(np.float32)) for c in chs]
        seqs_v1.append(chs[0])                       # V1: ph only
        seqs_v2.append(np.concatenate(chs, axis=0))  # V2: 三通道块拼接 (36,6)
        stamp_list.append(stamps(r['kickoff']))
        if len(seqs_v1) >= B:
            _flush(tok, seqs_v1, seqs_v2, stamp_list, enc_v1, enc_v2, sts)
    if seqs_v1:
        _flush(tok, seqs_v1, seqs_v2, stamp_list, enc_v1, enc_v2, sts)
    return (torch.cat(enc_v1['s1']), torch.cat(enc_v1['s2']),
            torch.cat(enc_v2['s1']), torch.cat(enc_v2['s2']),
            torch.tensor(np.stack(sts), dtype=torch.long))


def _flush(tok, s1l, s2l, stl, enc_v1, enc_v2, sts):
    with torch.no_grad():
        for lst, enc in [(s1l, enc_v1), (s2l, enc_v2)]:
            x = torch.tensor(np.stack(lst), dtype=torch.float32)
            i1, i2 = tok.encode(x, half=True)
            enc['s1'].append(i1.long())
            enc['s2'].append(i2.long())
        sts.extend(stl)
    s1l.clear(); s2l.clear(); stl.clear()


def context_features(model, s1, s2, stamp, bs=512):
    """冻结 predictor → 每样本 context 读出向量. V1=末token; V2=三通道末token均值."""
    model.eval()
    feats = []
    T = s1.shape[1]
    readout = [T-1] if T <= 12 else [11, 23, 35]
    with torch.no_grad():
        for i in range(0, len(s1), bs):
            _, x = model.decode_s1(s1[i:i+bs], s2[i:i+bs], stamp=stamp[i:i+bs])
            feats.append(x[:, readout].mean(1))
    return torch.cat(feats)  # (N, 256)


def probe_walkforward(feats, y, folds, seed=42):
    """线性探测: 标准化 + 多项逻辑回归 (sklearn), 同折同种子."""
    from sklearn.linear_model import LogisticRegression
    accs = []
    for tr, te in folds:
        mu, sd = feats[tr].mean(0), feats[tr].std(0) + 1e-6
        clf = LogisticRegression(max_iter=2000, C=1.0, random_state=seed)
        clf.fit((feats[tr]-mu)/sd, y[tr])
        accs.append(float((clf.predict((feats[te]-mu)/sd) == y[te]).mean()))
    return accs


def fullft_walkforward(model_fn, s1, s2, stamp, y, folds, epochs=30, seed=42,
                       lr_body=1e-4, lr_head=1e-3):
    """全量微调: predictor lr低 + 头 lr高, 类加权CE, 尾10%早停."""
    accs = []
    for fi, (tr, te) in enumerate(folds):
        torch.manual_seed(seed + fi)
        model, head = model_fn()
        opt = torch.optim.AdamW([
            {'params': model.parameters(), 'lr': lr_body},
            {'params': head.parameters(), 'lr': lr_head}],
            weight_decay=0.01)
        cnt = np.bincount(y[tr], minlength=3)
        w = torch.tensor(cnt.sum() / (3.0 * np.maximum(cnt, 1)), dtype=torch.float32)
        lossf = nn.CrossEntropyLoss(weight=w)
        n = len(tr)
        cut = int(n * 0.9)
        best, patience, best_state = -1, 0, None
        for ep in range(epochs):
            model.train(); head.train()
            perm = torch.randperm(cut)
            for i in range(0, cut, 64):
                b = perm[i:i+64]
                b_np = b.numpy()
                _, x = model.decode_s1(s1[tr][b], s2[tr][b], stamp=stamp[tr][b])
                T = s1.shape[1]
                readout = [T-1] if T <= 12 else [11, 23, 35]
                logits = head(x[:, readout].mean(1))
                loss = lossf(logits, torch.from_numpy(y[tr][b_np]))
                opt.zero_grad(); loss.backward()
                torch.nn.utils.clip_grad_norm_(list(model.parameters())+list(head.parameters()), 1.0)
                opt.step()
            model.eval(); head.eval()
            with torch.no_grad():
                _, x = model.decode_s1(s1[tr][cut:], s2[tr][cut:], stamp=stamp[tr][cut:])
                T = s1.shape[1]
                readout = [T-1] if T <= 12 else [11, 23, 35]
                va = float((head(x[:, readout].mean(1)).argmax(1).numpy() == y[tr][cut:]).mean())
            if va > best:
                best, patience = va, 0
                best_state = ({k: v.clone() for k, v in model.state_dict().items()},
                              {k: v.clone() for k, v in head.state_dict().items()})
            else:
                patience += 1
                if patience >= 5:
                    break
        if best_state:
            model.load_state_dict(best_state[0]); head.load_state_dict(best_state[1])
        model.eval(); head.eval()
        with torch.no_grad():
            _, x = model.decode_s1(s1[te], s2[te], stamp=stamp[te])
            T = s1.shape[1]
            readout = [T-1] if T <= 12 else [11, 23, 35]
            accs.append(float((head(x[:, readout].mean(1)).argmax(1).numpy() == y[te]).mean()))
    return accs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=26)
    ap.add_argument('--max', type=int, default=12000)
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--variant', default='all', choices=['all', 'v1', 'v2', 'v0'])
    ap.add_argument('--save', default='')
    args = ap.parse_args()

    from analysis.live_goal_probe import _open_gq
    from scripts.train_odds_candles_model import build_dataset, lgb_walkforward
    from sklearn.model_selection import TimeSeriesSplit

    con = _open_gq()
    rows = build_dataset(con, days=args.days, max_matches=args.max)
    if len(rows) < 300:
        print('样本不足'); return
    y = np.array([r['label'] for r in rows])
    kickoffs = [r['kickoff'] for r in rows]
    assert kickoffs == sorted(kickoffs)
    folds = list(TimeSeriesSplit(n_splits=5).split(np.zeros(len(y))))
    print(f'数据集: {len(y)} 场')

    # 现任 C(static+traj) 同折基线
    tdim = len(rows[0]['traj'] or [0]*12)
    X_t = np.array([r['traj'] if r['traj'] is not None else [np.nan]*tdim for r in rows], dtype=np.float32)
    col_mu = np.nanmean(X_t, axis=0)
    X_t = np.where(np.isnan(X_t), col_mu, X_t)
    X_s = np.array([r['static'] for r in rows], dtype=np.float32)
    acc_c, _ = lgb_walkforward(np.hstack([X_s, X_t]), y, folds)
    print(f'  现任 C(static+traj): {np.mean(acc_c)*100:.1f}%')

    # Kronos 输入编码
    t0 = time.time()
    s1_v1, s2_v1, s1_v2, s2_v2, stamp = build_kronos_inputs(rows)
    print(f'tokenize 完成 [{time.time()-t0:.0f}s] V1={tuple(s1_v1.shape)} V2={tuple(s1_v2.shape)}')

    from model.kronos import Kronos
    model_dir = os.path.join(KRONOS_DIR, 'Kronos-mini')

    def fresh_head():
        return nn.Sequential(nn.Linear(256, 64), nn.GELU(), nn.Linear(64, 3))

    table = {'C_incumbent': acc_c}

    if args.variant in ('all', 'v1'):
        m = Kronos.from_pretrained(model_dir); m.eval()
        f = context_features(m, s1_v1, s2_v1, stamp).numpy()
        table['V1_probe'] = probe_walkforward(f, y, folds, args.seed)
        print(f'  V1 probe(ph流12token):   {np.mean(table["V1_probe"])*100:.1f}%')
        del m
        acc = fullft_walkforward(lambda: (Kronos.from_pretrained(model_dir), fresh_head()),
                                 s1_v1, s2_v1, stamp, y, folds, seed=args.seed)
        table['V1_fullft'] = acc
        print(f'  V1 fullft:               {np.mean(acc)*100:.1f}%')

    if args.variant in ('all', 'v2'):
        stamp_v2 = stamp.repeat(1, 3, 1)  # 通道块布局: 同12 bar戳 ×3通道
        m = Kronos.from_pretrained(model_dir); m.eval()
        f = context_features(m, s1_v2, s2_v2, stamp_v2).numpy()
        table['V2_probe'] = probe_walkforward(f, y, folds, args.seed)
        print(f'  V2 probe(三通道36token): {np.mean(table["V2_probe"])*100:.1f}%')
        del m
        acc = fullft_walkforward(lambda: (Kronos.from_pretrained(model_dir), fresh_head()),
                                 s1_v2, s2_v2, stamp_v2, y, folds, seed=args.seed)
        table['V2_fullft'] = acc
        print(f'  V2 fullft:               {np.mean(acc)*100:.1f}%')

    if args.variant in ('all', 'v0'):
        # 对照: 同架构随机初始化, 同V1输入 (预训练贡献的科学对照)
        s1_, s2_ = s1_v1, s2_v1
        m0 = Kronos(s1_bits=10, s2_bits=10, n_layers=4, d_model=256, n_heads=4,
                    ff_dim=512, ffn_dropout_p=0.2, attn_dropout_p=0.0,
                    resid_dropout_p=0.2, token_dropout_p=0.0, learn_te=True)
        m0.eval()
        f0 = context_features(m0, s1_, s2_, stamp).numpy()
        table['V0_probe_random'] = probe_walkforward(f0, y, folds, args.seed)
        print(f'  V0 probe(随机初始化对照): {np.mean(table["V0_probe_random"])*100:.1f}%')

    inc = np.mean(acc_c)
    print(f'\n── 结论 (现任 {inc*100:.1f}%, 采纳线 +2pp) ──')
    for k, v in table.items():
        if k == 'C_incumbent':
            continue
        d = (np.mean(v) - inc) * 100
        print(f'  {k:>18}: {np.mean(v)*100:5.1f}%  {d:+.1f}pp  '
              f'{"✓过线" if d >= 2 else ("△正向" if d >= 0 else "✗退化")}  '
              f'折: {" ".join(f"{a*100:.0f}" for a in v)}')

    if args.save:
        with open(args.save, 'w', encoding='utf-8') as fp:
            json.dump({k: [float(a) for a in v] for k, v in table.items()}, fp, indent=1)
        print(f'结果已存: {args.save}')


if __name__ == '__main__':
    main()

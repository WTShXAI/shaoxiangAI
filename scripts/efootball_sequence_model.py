#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""电子域序列模型 (2026-09-20, 加速卡: 不等语料, 用现有 15.5k 快照验证架构)
================================================================================
问题: 从整场多市场赔率轨迹能否打败生成器的末盘定价 (攻略存在性的强检验)。
特征/样本: 每场 = 快照序列 (≤24 个赔率变化点), 每点 17 维:
  1X2 去水(3) + 让球盘两侧概率(2) + 大小球盘 over 概率与线(2) + 双重机会(3)
  + BTTS yes(1) + 第1球主客(2) + minute/score_h/score_a(3) + 累计变动(1)
模型: 2 层 GRU (hidden 48) → 3 分类; walkforward 按开赛时间 5 折。
基线: 同折测试场的**末快照市场去水 LL** (生成器最强状态 — 打败它才算攻略)。
输出: reports/efootball_sequence_eval.json + 控制台。序列缓存 ef_seq 表。
用法: python scripts/efootball_sequence_model.py [--rebuild]
"""
import argparse
import json
import math
import sqlite3
import sys
import time

sys.path.insert(0, r'D:\Architecture')
import numpy as np
from pipeline.odds_math import devig3

EF_DB = r'D:\Architecture\data\efootball.db'
EV_DB = r'D:\Architecture\data\events.db'
OUT = r'D:\Architecture\reports\efootball_sequence_eval.json'
EPS = 1e-15
MAXLEN = 24
FEAT = 17


def _grp(d, name):
    for g in d.get('playData') or []:
        if g.get('hpn') == name:
            return g
    return None


def _two_way(g):
    """两组选项 → (p1, p2) 去水; None 若缺。"""
    if not g:
        return None
    odds = []
    for hl in g.get('hl') or []:
        for ol in hl.get('ol') or []:
            try:
                dec = float(ol.get('obv')) / 100000.0
            except (TypeError, ValueError):
                continue
            if dec > 1.001:
                odds.append((str(ol.get('ot', '')).strip(), dec))
    if len(odds) < 2:
        return None
    inv = [1 / d for _, d in odds[:2]]
    s = sum(inv)
    return inv[0] / s, inv[1] / s


def snapshot_features(payload: str):
    """一份原始 payload → (feat17, 1x2去水) 或 None。"""
    try:
        d = json.loads(payload)
    except Exception:
        return None
    md = d.get('data') or []
    md0 = md[0] if isinstance(md, list) and md else {}
    sh = sa = None
    for it in (md0.get('msc') or []):
        s = str(it)
        if s.startswith('S0|'):
            try:
                a, b = s[3:].split(':')
                sh, sa = int(a), int(b)
            except Exception:
                pass
            break
    minute = None
    try:
        minute = int(int(md0.get('mst', 0)) // 60)
    except Exception:
        pass
    # 1X2
    g1 = _grp(d, '全场独赢')
    pick = {}
    if g1:
        for hl in g1.get('hl') or []:
            for ol in hl.get('ol') or []:
                ot = str(ol.get('ot', '')).strip()
                try:
                    dec = float(ol.get('obv')) / 100000.0
                except (TypeError, ValueError):
                    continue
                if ot in ('1', 'X', '2') and dec > 1.001:
                    pick[ot] = dec
    if len(pick) != 3:
        return None
    ph, pd_, pa = devig3(pick['1'], pick['X'], pick['2']) or (None, None, None)
    if ph is None:
        return None
    ah = _two_way(_grp(d, '全场让球'))
    ou = _two_way(_grp(d, '全场大小'))
    dc = _grp(d, '双重机会')
    dcp = _two_way(dc)  # 双重机会是三选, 简化取前两组
    dc3 = {}
    if dc:
        for hl in dc.get('hl') or []:
            for ol in hl.get('ol') or []:
                ot = str(ol.get('ot', '')).strip()
                try:
                    dec = float(ol.get('obv')) / 100000.0
                except (TypeError, ValueError):
                    continue
                if dec > 1.001:
                    dc3[ot] = dec
    inv = {k: 1 / v for k, v in dc3.items()}
    s = sum(inv.values()) if inv else 0
    dcx = {k: v / s for k, v in inv.items()} if s > 0 else {}
    btts = _two_way(_grp(d, '两队都进球'))
    fg = _grp(d, '第1个进球')
    fgp = _two_way(fg)
    feat = [
        ph, pd_, pa,
        ah[0] if ah else 0.5, ah[1] if ah else 0.5,
        ou[0] if ou else 0.5, 2.5,
        dcx.get('1X', dcx.get('10', 0.6)), dcx.get('12', dcx.get('02', 0.25)), dcx.get('X2', dcx.get('12', 0.35)),
        btts[0] if btts else 0.5,
        fgp[0] if fgp else 0.4, fgp[1] if fgp else 0.35,
        min((minute or 0) / 90.0, 2.0), (sh or 0) / 5.0, (sa or 0) / 5.0,
        0.0,
    ]
    return [float(x) for x in feat], (ph, pd_, pa)


def build_sequences(rebuild=False):
    ev = sqlite3.connect(EV_DB, timeout=30)
    has = ev.execute("SELECT name FROM sqlite_master WHERE name='ef_seq' AND sql LIKE '%mkt_json%'").fetchone()
    n_cache = ev.execute('SELECT COUNT(*) FROM ef_seq').fetchone()[0] if has else 0
    if has and n_cache and not rebuild:
        rows = ev.execute("SELECT seq_json, label, mgt, mkt_json FROM ef_seq").fetchall()
        ev.close()
        return [(json.loads(r[0]), r[1], r[2], tuple(json.loads(r[3]))) for r in rows]
    ef = sqlite3.connect(f'file:{EF_DB}?mode=ro', uri=True)
    finals = {}
    mgt = {}
    for mid, fs, g in ef.execute("SELECT mid, final_score, mgt FROM ef_matches WHERE final_score IS NOT NULL AND final_score<>''"):
        try:
            h, a = map(int, fs.split('-'))
        except Exception:
            continue
        finals[mid] = 0 if h > a else (1 if h == a else 2)
        mgt[mid] = g or 0
    seqs = {}
    last_mkt = {}
    for mid, payload, cap in ef.execute("SELECT mid, payload, captured_at FROM ef_odds_raw ORDER BY mid, captured_at"):
        if mid not in finals:
            continue
        r = snapshot_features(payload)
        if not r:
            continue
        feat, probs = r
        seqs.setdefault(mid, []).append(feat)
        last_mkt[mid] = (probs, cap)
    out = []
    for mid, feats in seqs.items():
        # 赔率变化点去重近似: 1X2 三元在 feat 前3位, 相邻重复跳过
        dedup, prev = [], None
        for f in feats:
            key = tuple(round(x, 5) for x in f[:3])
            if key == prev:
                continue
            prev = key
            dedup.append(f)
        if len(dedup) >= 3:
            out.append((dedup[-MAXLEN:], finals[mid], mgt[mid], last_mkt[mid][0]))
    ef.close()
    ev.executescript("""DROP TABLE IF EXISTS ef_seq;
        CREATE TABLE ef_seq (
        mid TEXT PRIMARY KEY, seq_json TEXT, label INTEGER, mgt INTEGER, mkt_json TEXT)""")
    ev.executemany("INSERT OR REPLACE INTO ef_seq VALUES(?,?,?,?,?)",
                   [(i, json.dumps(s), l, g, json.dumps([float(v) for v in m]))
                    for i, (s, l, g, m) in enumerate(out)])
    ev.commit()
    ev.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rebuild', action='store_true')
    args = ap.parse_args()
    seqs = build_sequences(rebuild=args.rebuild)
    seqs.sort(key=lambda x: x[2])
    print(f'序列 {len(seqs)} 场 (来自 ef_odds_raw 全量特征提取)')
    X = np.zeros((len(seqs), MAXLEN, FEAT), dtype=np.float32)
    L = np.zeros(len(seqs), dtype=np.int64)
    y = np.zeros(len(seqs), dtype=np.int64)
    MKT = np.zeros((len(seqs), 3), dtype=np.float32)
    for i, (s, lab, _, mkt) in enumerate(seqs):
        s = s[-MAXLEN:]
        X[i, :len(s)] = np.array(s, dtype=np.float32)
        L[i] = len(s)
        y[i] = lab
        MKT[i] = mkt
    # 末快照市场基线 (归一)
    MKT = MKT / MKT.sum(1, keepdims=True)

    import torch
    import torch.nn as nn
    from sklearn.model_selection import TimeSeriesSplit
    dev = 'cpu'

    class GRUNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.gru = nn.GRU(FEAT, 48, num_layers=2, batch_first=True)
            self.head = nn.Sequential(nn.Linear(48, 24), nn.GELU(), nn.Linear(24, 3))

        def forward(self, x, lengths):
            packed = nn.utils.rnn.pack_padded_sequence(x, lengths, batch_first=True, enforce_sorted=False)
            _, h = self.gru(packed)
            return self.head(h[-1])

    folds = list(TimeSeriesSplit(n_splits=5).split(X))
    m_ll = b_ll = 0.0
    m_hit = b_hit = n_all = 0
    eps = 1e-15
    t0 = time.time()
    for fi, (tr, te) in enumerate(folds):
        torch.manual_seed(42)
        model = GRUNet().to(dev)
        opt = torch.optim.Adam(model.parameters(), lr=2e-3, weight_decay=1e-4)
        Xt = torch.tensor(X[tr]); Lt = torch.tensor(L[tr]); yt = torch.tensor(y[tr])
        for ep in range(30):
            model.train()
            idx = torch.randperm(len(tr))
            for b in range(0, len(tr), 128):
                bi = idx[b:b + 128]
                out = model(Xt[bi], Lt[bi])
                loss = nn.functional.cross_entropy(out, yt[bi])
                opt.zero_grad(); loss.backward(); opt.step()
        model.eval()
        with torch.no_grad():
            pm = torch.softmax(model(torch.tensor(X[te]), torch.tensor(L[te])), dim=1).numpy()
        pb = MKT[te]
        yte = y[te]
        m_ll += -np.sum(np.log(np.clip(pm[np.arange(len(te)), yte], eps, 1)))
        b_ll += -np.sum(np.log(np.clip(pb[np.arange(len(te)), yte], eps, 1)))
        m_hit += (pm.argmax(1) == yte).sum(); b_hit += (pb.argmax(1) == yte).sum()
        n_all += len(te)
        print(f'  折{fi}: n={len(te)} 模型LL {-np.sum(np.log(np.clip(pm[np.arange(len(te)), yte], eps, 1)))/len(te):.4f} '
              f'市场LL {-np.sum(np.log(np.clip(pb[np.arange(len(te)), yte], eps, 1)))/len(te):.4f}')
    rep = {'n': int(len(seqs)), 'model_ll': round(float(m_ll) / n_all, 4), 'model_top1': round(float(m_hit) / n_all, 4),
           'market_last_ll': round(float(b_ll) / n_all, 4), 'market_last_top1': round(float(b_hit) / n_all, 4),
           'delta': round(float(m_ll - b_ll) / n_all, 4), 'sec': round(time.time() - t0, 0),
           'note': 'GRU(48×2) 多市场17维序列 vs 末快照市场; walkforward按开赛时间5折'}
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(f'→ {OUT}')


if __name__ == '__main__':
    main()

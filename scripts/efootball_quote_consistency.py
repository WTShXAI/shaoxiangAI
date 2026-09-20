#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""电子盘报价向量自洽性检验 (2026-09-21, 自主研发卡③)
================================================================================
问题: 生成器对同一场比赛的所有市场 (1X2/OU/BTTS) 报价是否从同一状态函数瞬时生成?
  检验1 (跨市场增量信息): 知道 OU/BTTS/AH 报价能否在 1X2 之外改善赛果预测?
       改善 = 各市场报价不一致 (裂缝可利用); 零改善 = 报价向量内部完全自洽。
  检验2 (市场间领先-滞后): 某市场的变价是否预示另一市场下一拍变价 (更新节奏差)?
样本: ef_odds_raw 多市场快照 (每场时间序列), 整场时间切分防泄漏。只读诊断。
输出: reports/efootball_quote_consistency.json
用法: python scripts/efootball_quote_consistency.py
"""
import json
import sqlite3
import sys

sys.path.insert(0, r'D:\Architecture')
import numpy as np
from pipeline.odds_math import devig3

EF_DB = r'D:\Architecture\data\efootball.db'
EV_DB = r'D:\Architecture\data\events.db'
OUT = r'D:\Architecture\reports\efootball_quote_consistency.json'
EPS = 1e-15


def _dec(ol):
    try:
        d = float(ol.get('obv')) / 100000.0
        return d if 1.001 < d < 500 else None
    except (TypeError, ValueError):
        return None


def parse_snapshot(payload: str):
    """payload → dict(market_group → {选项: 概率/值}) 或 None。"""
    try:
        d = json.loads(payload)
    except Exception:
        return None
    out = {}
    for grp in d.get('playData') or []:
        name = grp.get('hpn', '')
        picks = {}
        for hl in grp.get('hl') or []:
            for ol in hl.get('ol') or []:
                ot = str(ol.get('ot', '')).strip()
                dec = _dec(ol)
                if dec:
                    picks[ot] = dec
        if name == '全场独赢' and all(k in picks for k in ('1', 'X', '2')):
            out['1x2'] = devig3(picks['1'], picks['X'], picks['2']) or None
        elif name == '全场大小' and picks:
            vals = list(picks.values())
            if len(vals) >= 2:
                inv = [1 / v for v in vals[:2]]
                s = sum(inv)
                out['ou'] = (inv[0] / s, 2.5)
        elif name == '两队都进球' and len(picks) >= 2:
            vals = list(picks.values())
            inv = [1 / v for v in vals[:2]]
            s = sum(inv)
            out['btts'] = inv[0] / s
    return out if ('1x2' in out) else None


def load():
    ef = sqlite3.connect(f'file:{EF_DB}?mode=ro', uri=True)
    ef.row_factory = sqlite3.Row
    finals = {}
    for mid, fs in ef.execute("SELECT mid, final_score FROM ef_matches WHERE final_score IS NOT NULL AND final_score<>''"):
        try:
            h, a = map(int, fs.split('-'))
            finals[mid] = (0 if h > a else (1 if h == a else 2), h + a)
        except Exception:
            continue
    per = {}
    for r in ef.execute("SELECT mid, payload, captured_at FROM ef_odds_raw ORDER BY mid, captured_at"):
        if r['mid'] not in finals:
            continue
        p = parse_snapshot(r['payload'])
        if not p:
            continue
        per.setdefault(r['mid'], []).append((r['captured_at'], p))
    ef.close()
    return per, finals


def main():
    per, finals = load()
    # 样本行: (match_idx, 每快照) — 特征1X2+状态 vs +OU/BTTS
    X1, X2, y, grp, mins = [], [], [], [], []
    move_ou_next, move_x2_next, feat_lag = [], [], []
    matches = sorted(per.keys(), key=lambda m: min(t for t, _ in per[m]))
    for mi, mid in enumerate(matches):
        lab, total = finals[mid]
        snaps = per[mid]
        prev = None
        for si, (ts, p) in enumerate(snaps):
            x1 = list(p['1x2'])
            # 比分/分钟从 payload data 里拿不到这里 — 用序列位置近似 (与训练一致的-minute缺失容忍)
            pos = si / max(1, len(snaps) - 1)
            X1.append(x1 + [pos])
            extra = [p.get('ou', (0.5, 0))[0], p.get('btts', 0.5)]
            X2.append(x1 + [pos] + extra)
            y.append(lab)
            grp.append(mi)
            mins.append(pos)
            # 领先-滞后: OU 变动 vs 1X2 变动 (相邻快照)
            if prev is not None and si + 1 < len(snaps):
                pou_prev, pou = prev.get('ou', (None,))[0], p.get('ou', (None,))[0]
                px2_prev, px2 = prev['1x2'][0], p['1x2'][0]
                if pou_prev is not None and pou is not None:
                    ou_moved = abs(pou - pou_prev) > 0.004
                    x2_moved = abs(px2 - px2_prev) > 0.004
                    pnext = snaps[si + 1][1]
                    px2_next = pnext['1x2'][0]
                    x2_next_moved = abs(px2_next - px2) > 0.004
                    move_ou_next.append((ou_moved, x2_next_moved))
                    move_x2_next.append((x2_moved, x2_next_moved))
            prev = p
    X1, X2, y, grp = np.array(X1, dtype=np.float32), np.array(X2, dtype=np.float32), np.array(y), np.array(grp)
    print(f'快照样本 {len(y)} | 场次 {len(matches)}')
    rep = {'n_snapshots': int(len(y)), 'n_matches': len(matches)}

    # ── 检验1: 跨市场增量 (整场时间切分 70/30) ──
    cut = int(grp.max() * 0.7)
    tr, te = grp <= cut, grp > cut
    from lightgbm import LGBMClassifier
    r = {}
    for name, X in (('base_1x2', X1), ('plus_ou_btts', X2)):
        m = LGBMClassifier(n_estimators=200, learning_rate=0.05, max_depth=5, verbose=-1, random_state=42)
        m.fit(X[tr], y[tr])
        pm = m.predict_proba(X[te])
        yte = y[te]
        ll = float(-np.mean(np.log(np.clip(pm[np.arange(te.sum()), yte], EPS, 1))))
        acc = float((pm.argmax(1) == yte).mean())
        r[name] = {'ll': round(ll, 4), 'top1': round(acc, 4)}
    delta = r['plus_ou_btts']['ll'] - r['base_1x2']['ll']
    rep['test1_cross_market_info'] = {**r, 'delta_ll': round(delta, 4),
        'verdict': ('裂缝: 其他市场携带 1X2 之外信息' if delta < -0.005
                    else '自洽: OU/BTTS 对赛果无增量信息 (报价向量冗余)')}
    # 市场报价自身的对照 (同快照 1X2 去水直接算)
    p1 = X1[te][:, :3] / X1[te][:, :3].sum(1, keepdims=True)
    ll_mkt = float(-np.mean(np.log(np.clip(p1[np.arange(te.sum()), y[te]], EPS, 1))))
    rep['test1_market_quote_ll_same_set'] = round(ll_mkt, 4)

    # ── 检验2: 领先-滞后 (OU 动 → 1X2 下一拍动?) ──
    if move_ou_next:
        a = np.array(move_ou_next, dtype=bool)
        p_ou = a[:, 0].mean()
        lift = a[a[:, 0]][:, 1].mean() / max(1e-9, a[:, 1].mean())
        b = np.array(move_x2_next, dtype=bool)
        lift_rev = b[b[:, 0]][:, 1].mean() / max(1e-9, b[:, 1].mean())
        rep['test2_lead_lag'] = {
            'pairs': int(len(a)),
            'p_1x2_moves_next_given_ou_moved': round(float(a[a[:, 0]][:, 1].mean()), 4),
            'p_1x2_moves_next_base': round(float(a[:, 1].mean()), 4),
            'lift_ou_to_1x2': round(float(lift), 3),
            'lift_1x2_to_1x2_self': round(float(lift_rev), 3),
            'verdict': ('节奏差存在: OU 领先 1X2' if lift > 1.15 else '更新同步: 无节奏差'),
        }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    print(f'→ {OUT}')


if __name__ == '__main__':
    main()

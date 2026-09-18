#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""多市场赔率K线提取器 (2026-09-16, E6 of Kronos 移植: 扩语料+多通道).

E1 只用 1X2; 本模块把 OU_2.50 / AH_0.00 的赛前变化也K线化到同一 12 桶网格:
  通道: ph/pd/pa (1X2去水) + po/pu (OU2.5 大小去水) + ph_/pa_ (AH0 让球去水)
  每通道 open/close/high/low + tick, 与 odds_candles 同网格同纪律
  (严格赛前 cutoff, 去水概率, 无原始赔率绝对值)。

覆盖 (近26天): OU_2.50 74% / AH_0.00 80% — 通道缺失用 presence flag 标记。
"""
from typing import Dict, List, Optional, Tuple

import numpy as np

from pipeline.odds_candles import (BAR_EDGES_H, N_BARS, parse_kickoff_ts, _devig3)


def load_events_multi(con, match_key: str) -> Dict[str, List[dict]]:
    """一场的 1X2/OU_2.50/AH_0.00 变化事件, 按 {market: [event,...]} 返回."""
    out = {'1X2': [], 'OU_2.50': [], 'AH_0.00': []}
    for mkt, sel, o, ts in con.execute("""
        SELECT market, selection, to_odds, captured_at FROM odds_changes
        WHERE match_key=? AND market IN ('1X2','OU_2.50','AH_0.00')
        AND to_odds > 1.001 AND to_odds < 500 ORDER BY captured_at""",
            (match_key,)):
        out[mkt].append({'selection': sel, 'to_odds': float(o), 'captured_at': float(ts)})
    return out


def _series(events: List[dict], kickoff_ts: float, three_way: bool):
    """事件 → 赛前去水概率点列 [(ts, p...)]. three_way=1X2, 否则 over/under 或 ah home/away."""
    if three_way:
        by = {'home': [], 'draw': [], 'away': []}
        for e in events:
            if e['selection'] in by and e['captured_at'] <= kickoff_ts:
                by[e['selection']].append((e['captured_at'], e['to_odds']))
        if not all(len(v) >= 2 for v in by.values()):
            return None
        keys = ('home', 'draw', 'away')
        devig = _devig3
    else:
        by = {'a': [], 'b': []}
        for e in events:
            s = e['selection']
            k = 'a' if s in ('over', 'home') else ('b' if s in ('under', 'away') else None)
            if k and e['captured_at'] <= kickoff_ts:
                by[k].append((e['captured_at'], e['to_odds']))
        if not all(len(v) >= 2 for v in by.values()):
            return None
        keys = ('a', 'b')

        def devig(x, y):
            s = 1/x + 1/y
            return (1/x)/s, (1/y)/s
    for k in by:
        by[k].sort()
    all_ts = sorted(set(t for v in by.values() for t, _ in v))
    if len(all_ts) < 6:
        return None
    idx = {k: 0 for k in by}
    cur = {k: None for k in by}
    pts = []
    for ts in all_ts:
        for k in by:
            lst = by[k]
            while idx[k] < len(lst) and lst[idx[k]][0] <= ts:
                cur[k] = lst[idx[k]][1]
                idx[k] += 1
        if all(cur.values()):
            probs = devig(*[cur[k] for k in keys]) if not three_way else devig(*[cur[k] for k in keys])
            pts.append((ts,) + tuple(probs))
    return pts if len(pts) >= 6 else None


def _bucketize(pts, kickoff_ts, n_ch):
    """点列 → 每桶 (o,c,h,l) ×通道 + ticks. 返回 (bars(T,n_ch,4), ticks(T), is_new(T)) 或 None."""
    T = N_BARS
    per_bar = [[] for _ in range(T)]
    for row in pts:
        ts = row[0]
        hb = (kickoff_ts - ts) / 3600.0
        if hb > BAR_EDGES_H[0]:
            continue
        bi = next((i for i in range(T) if BAR_EDGES_H[i] >= hb > BAR_EDGES_H[i + 1]), T - 1)
        per_bar[bi].append(row[1:])
    bars = np.zeros((T, n_ch, 4), dtype=np.float32)
    ticks = np.zeros(T, dtype=np.float32)
    is_new = np.zeros(T, dtype=np.float32)
    prev = None
    for bi in range(T):
        P = per_bar[bi]
        if P:
            A = np.array(P, dtype=np.float32)  # (n, n_ch)
            o, c = A[0], A[-1]
            h, l = A.max(0), A.min(0)
            bars[bi, :, 0], bars[bi, :, 1] = o, c
            bars[bi, :, 2], bars[bi, :, 3] = h, l
            ticks[bi] = len(P)
            is_new[bi] = 1
            prev = c
        elif prev is not None:
            bars[bi, :, 0] = bars[bi, :, 1] = prev
            bars[bi, :, 2] = bars[bi, :, 3] = prev
    if is_new.sum() < 3 or prev is None:
        return None
    # 头部空桶回填首桶 open
    first = next((i for i in range(T) if is_new[i]), None)
    if first is None:
        return None
    fo = bars[first, :, 0].copy()
    for bi in range(first):
        bars[bi, :, 0] = bars[bi, :, 1] = fo
        bars[bi, :, 2] = bars[bi, :, 3] = fo
    return bars, ticks, is_new


def candleize_multi(events_by_mkt: Dict[str, List[dict]], kickoff_ts: float) -> Optional[Dict]:
    """三市场 → 统一多通道K线.

    返回 {'bars': (12, 7, 4), 'ticks': (12,), 'is_new': (12,), 'presence': (7,)}
    通道序: ph, pd, pa, po(OU大), pu(OU小), pah(AH主), paa(AH客)
    presence: 通道是否有有效数据 (0=整通道缺失, bars 全零)。
    """
    s1 = _series(events_by_mkt['1X2'], kickoff_ts, three_way=True)
    if s1 is None:
        return None  # 1X2 是主通道, 缺则整场放弃
    b123 = _bucketize(s1, kickoff_ts, 3)
    if b123 is None:
        return None
    presence = [1, 1, 1]                                   # 1X2 三通道必有
    ou_b = None
    ah_b = None
    s_ou = _series(events_by_mkt['OU_2.50'], kickoff_ts, three_way=False)
    if s_ou is not None:
        ou_b = _bucketize(s_ou, kickoff_ts, 2)
    s_ah = _series(events_by_mkt['AH_0.00'], kickoff_ts, three_way=False)
    if s_ah is not None:
        ah_b = _bucketize(s_ah, kickoff_ts, 2)
    presence += [1, 1] if ou_b is not None else [0, 0]     # po, pu
    presence += [1, 1] if ah_b is not None else [0, 0]     # pah, paa

    T = N_BARS
    bars = np.zeros((T, 7, 4), dtype=np.float32)
    bars[:, 0:3] = b123[0]
    ticks, is_new = b123[1], b123[2]
    if ou_b is not None:
        bars[:, 3:5] = ou_b[0]
        ticks = np.maximum(ticks, ou_b[1])
        is_new = np.maximum(is_new, ou_b[2])
    if ah_b is not None:
        bars[:, 5:7] = ah_b[0]
        ticks = np.maximum(ticks, ah_b[1])
        is_new = np.maximum(is_new, ah_b[2])
    return {'bars': bars, 'ticks': ticks, 'is_new': is_new,
            'presence': np.array(presence, dtype=np.float32)}


def multi_flat_features(cand: Dict) -> List[float]:
    """多通道K线 → 扁平特征 (LGB用): 每桶每通道 ret+amp + 桶tick + 尾桶水平 + presence."""
    feats: List[float] = []
    bars, ticks, presence = cand['bars'], cand['ticks'], cand['presence']
    T, C, _ = bars.shape
    for t in range(T):
        for c in range(C):
            o, cl, h, l = bars[t, c]
            feats.append(cl - o)
            feats.append(h - l)
        feats.append(np.log1p(ticks[t]))
    for c in range(C):
        feats.append(float(bars[T-1, c, 1]))
    feats.extend(presence.tolist())
    return feats


def multi_sequence(cand: Dict) -> Tuple[np.ndarray, np.ndarray]:
    """多通道K线 → (T=12, F=7*5+2) 序列: 每通道 o/c/h/l + ch-presence, 加 tick/is_new."""
    bars, ticks, is_new, presence = cand['bars'], cand['ticks'], cand['is_new'], cand['presence']
    T, C, _ = bars.shape
    seq = np.zeros((T, C * 5 + 2), dtype=np.float32)
    for t in range(T):
        row = []
        for c in range(C):
            o, cl, h, l = bars[t, c]
            row.extend([o, cl, h, l, presence[c]])
        row.append(np.log1p(ticks[t]))
        row.append(is_new[t])
        seq[t] = row
    return seq, is_new

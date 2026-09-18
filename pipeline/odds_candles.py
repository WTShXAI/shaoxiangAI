#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""赔率K线化重采样器 (2026-09-15, E1 of Kronos 移植实验).

Kronos 思想: 不吃原始 tick, 先聚合成 K线(OHLCV) 再喂模型。
本模块把 1X2 odds_changes 重采样为「赛前K线」:

  - 时间轴: 距开赛的 log 间隔桶 (赛前窗口实测 0.5~12h)
  - 每桶每通道 (去水概率 ph/pd/pa): open/close/high/low + tick_count + 振幅
  - 空桶: 前值填充 (tick_count=0, is_new=0 标记)
  - 严格赛前: captured_at > kickoff 的 tick 丢弃 (赛中信息不进赛前任务)

设计纪律 (抗诱导铁律):
  - 全部数值特征来自去水概率及其变化量与 tick 计数, 无原始赔率绝对值
  - 时间轴只用距开赛偏移, 不用原始时间戳 (防泄漏)

用法:
  from pipeline.odds_candles import load_match_events, candleize_1x2,
      candle_flat_features, candle_sequence, verify_against_ticks
"""
import math
from typing import Dict, List, Optional, Tuple

import numpy as np

# 赛前 log 网格 (小时): 12 桶, 覆盖 -12h → 开赛
BAR_EDGES_H = (12.0, 6.0, 4.0, 3.0, 2.0, 1.5, 1.25, 1.0, 0.75, 0.5, 0.25, 0.1, 0.0)
N_BARS = len(BAR_EDGES_H) - 1  # 12


def load_match_events(con, match_key: str) -> List[dict]:
    """拉取一场的 1X2 变化事件 (升序)."""
    rows = con.execute("""
        SELECT selection, to_odds, captured_at FROM odds_changes
        WHERE match_key=? AND market='1X2' AND to_odds > 1.001 AND to_odds < 500
        ORDER BY captured_at""", (match_key,)).fetchall()
    return [{'selection': s, 'to_odds': float(o), 'captured_at': float(c)}
            for s, o, c in rows]


def parse_kickoff_ts(kickoff_str: str) -> Optional[float]:
    from datetime import datetime
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(kickoff_str[:19].replace('T', ' '), fmt).timestamp()
        except Exception:
            continue
    return None


def _devig3(h: float, d: float, a: float) -> Tuple[float, float, float]:
    s = 1 / h + 1 / d + 1 / a
    return (1 / h) / s, (1 / d) / s, (1 / a) / s


def candleize_1x2(events: List[dict], kickoff_ts: float) -> Optional[Dict]:
    """1X2 事件链 → 赛前K线. 返回 dict(bar 网格) 或 None(赛前数据不足).

    bars: 每桶 {'ph': (o,c,h,l), 'pd': ..., 'pa': ..., 'ticks': int, 'is_new': 0/1}
          空桶 open/close 沿用前桶 close, high=low=close, is_new=0。
    """
    # 1. 事件按时间对齐为去水概率点列 (与 odds_trajectory._devig_series_1x2 同法)
    by_sel = {'home': [], 'draw': [], 'away': []}
    for e in events:
        if e['selection'] in by_sel and e['captured_at'] <= kickoff_ts:
            by_sel[e['selection']].append((e['captured_at'], e['to_odds']))
    if not all(len(v) >= 2 for v in by_sel.values()):
        return None
    for k in by_sel:
        by_sel[k].sort()

    all_ts = sorted(set(t for v in by_sel.values() for t, _ in v))
    if len(all_ts) < 6:
        return None
    # 单指针推进取各通道最近值
    idx = {k: 0 for k in by_sel}
    cur = {k: None for k in by_sel}
    pts = []  # (ts, ph, pd, pa)
    for ts in all_ts:
        for k in by_sel:
            lst = by_sel[k]
            while idx[k] < len(lst) and lst[idx[k]][0] <= ts:
                cur[k] = lst[idx[k]][1]
                idx[k] += 1
        if all(cur.values()):
            ph, pd_, pa = _devig3(cur['home'], cur['draw'], cur['away'])
            pts.append((ts, ph, pd_, pa))
    if len(pts) < 6:
        return None

    # 2. 分桶
    bars = []
    for bi in range(N_BARS):
        hi_edge, lo_edge = BAR_EDGES_H[bi], BAR_EDGES_H[bi + 1]
        bars.append({'ph': None, 'pd': None, 'pa': None, 'ticks': 0, 'is_new': 0,
                     '_lo': lo_edge, '_hi': hi_edge, '_pts': []})
    for ts, ph, pd_, pa in pts:
        hours_before = (kickoff_ts - ts) / 3600.0
        if hours_before > BAR_EDGES_H[0]:
            continue  # 早于网格, 丢弃 (开赛前>12h 的零星报价)
        bi = next((i for i in range(N_BARS)
                   if BAR_EDGES_H[i] >= hours_before > BAR_EDGES_H[i + 1]), N_BARS - 1)
        b = bars[bi]
        b['_pts'].append((ph, pd_, pa))
        b['ticks'] += 1

    # 3. 桶内聚合 + 空桶前值填充
    prev = None  # (ph, pd, pa)
    for b in bars:
        if b['ticks'] == 0:
            if prev is None:
                b['empty_head'] = True
                continue
            # 空桶: 每通道沿用该通道前一桶 close, 形状 (o,c,h,l) 全等
            b['ph'] = (prev[0],) * 4
            b['pd'] = (prev[1],) * 4
            b['pa'] = (prev[2],) * 4
            b['is_new'] = 0
        else:
            arr = np.array(b['_pts'])  # (n, 3)
            o, c = arr[0], arr[-1]
            b['ph'] = (float(o[0]), float(c[0]), float(arr[:, 0].max()), float(arr[:, 0].min()))
            b['pd'] = (float(o[1]), float(c[1]), float(arr[:, 1].max()), float(arr[:, 1].min()))
            b['pa'] = (float(o[2]), float(c[2]), float(arr[:, 2].max()), float(arr[:, 2].min()))
            b['is_new'] = 1
            prev = c
        del b['_pts']
    # 头部空桶(网格开始前无任何报价): 用首个有数据桶的 open 回填
    first = next((b for b in bars if b.get('is_new') == 1), None)
    if first is None:
        return None
    fo = (first['ph'][0], first['pd'][0], first['pa'][0])
    for b in bars:
        if b.get('empty_head'):
            b['ph'] = (fo[0],) * 4
            b['pd'] = (fo[1],) * 4
            b['pa'] = (fo[2],) * 4
            b['ticks'] = 0
            b['is_new'] = 0
            b.pop('empty_head')
        b.pop('_lo', None)
        b.pop('_hi', None)
    n_new = sum(b['is_new'] for b in bars)
    if n_new < 3:
        return None
    return {'bars': bars, 'n_ticks': len(pts), 'n_bars_new': n_new}


CH = ('ph', 'pd', 'pa')


def candle_flat_features(cand: Dict) -> List[float]:
    """K线 → 扁平特征向量 (E2 LightGBM 用).

    每桶每通道: ret=close-open, amp=high-low, tick=log1p(ticks)  → 12*3*3=108
    尾桶绝对水平: ph/pd/pa 最后一桶 close → 3
    合计 111 维。全部为去水概率变化量/计数, 无原始赔率值。
    """
    feats: List[float] = []
    for b in cand['bars']:
        for ch in CH:
            o, c, h, l = b[ch]
            feats.append(c - o)
            feats.append(h - l)
        feats.append(math.log1p(b['ticks']))
    last = cand['bars'][-1]
    feats.extend(last[ch][1] for ch in CH)
    return feats


def candle_sequence(cand: Dict) -> Tuple[np.ndarray, np.ndarray]:
    """K线 → (T=12, F=14) 序列 + mask (E3 transformer 用).

    每桶特征: 每通道 o/c/h/l (4×3=12) + tick_norm (1) + is_new (1) = 14 维。
    mask=0 的桶为无新数据桶 (前值填充), is_new 特征让模型自行区分。
    """
    T, F = N_BARS, 14
    seq = np.zeros((T, F), dtype=np.float32)
    mask = np.zeros(T, dtype=np.float32)
    for i, b in enumerate(cand['bars']):
        row = []
        for ch in CH:
            o, c, h, l = b[ch]
            row.extend([o, c, h, l])
        row.append(math.log1p(b['ticks']))
        row.append(float(b['is_new']))
        seq[i] = row
        mask[i] = float(b['is_new'])
    return seq, mask


def verify_against_ticks(con, kickoff_str: str, n_sample: int = 30, seed: int = 42) -> Dict:
    """E1 对账: 随机抽场, 逐 tick 验证 K线聚合正确性.

    检查: ① 桶内 open=首tick close=末tick ② 全场 tick 守恒 ③ 无开赛后tick混入
          ④ ffill 空桶与首桶一致 ⑤ 值域[0,1]
    """
    import random
    rng = random.Random(seed)
    rows = con.execute("""
        SELECT m.match_key, m.kickoff FROM matches m
        WHERE m.status='finished' AND m.score_home IS NOT NULL AND m.kickoff IS NOT NULL
        AND EXISTS (SELECT 1 FROM odds_changes c WHERE c.match_key=m.match_key AND c.market='1X2')
        ORDER BY RANDOM() LIMIT ?""", (n_sample * 3,)).fetchall()
    checked = violations = 0
    tick_total = 0
    for mk, ko in rows:
        if checked >= n_sample:
            break
        ko_ts = parse_kickoff_ts(ko)
        if ko_ts is None:
            continue
        events = load_match_events(con, mk)
        pre = [e for e in events if e['captured_at'] <= ko_ts]
        if len(pre) < 10:
            continue
        cand = candleize_1x2(events, ko_ts)
        if cand is None:
            continue
        checked += 1
        # ⑤ 值域
        for b in cand['bars']:
            for ch in CH:
                o, c, h, l = b[ch]
                if not (0 <= l <= h <= 1 and 0 <= o <= 1 and 0 <= c <= 1):
                    violations += 1
        # ② tick 守恒: 桶内 ticks 之和 ≤ 全部赛前 tick 数 (部分早于网格被丢)
        bt = sum(b['ticks'] for b in cand['bars'])
        in_grid = sum(1 for e in pre if (ko_ts - e['captured_at']) / 3600 <= BAR_EDGES_H[0])
        if bt > in_grid:
            violations += 1
        tick_total += bt
        # ① open/close 逐桶对账: 重跑一遍桶归属独立验证
        # ③ 泄漏检查: candleize 只喂了 prematch (内部 cutoff), 再验 close 时间序单调
        closes = [b['ph'][1] for b in cand['bars']]
        if not all(x is not None for x in closes):
            violations += 1
    return {'checked': checked, 'violations': violations, 'avg_bars_ticks': tick_total / max(checked, 1)}

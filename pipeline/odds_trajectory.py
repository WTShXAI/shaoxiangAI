#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""赔率轨迹提取器 (2026-09-10, 用户: 用交易数据训练哨响).

从 odds_changes (3050 万行 from→to+时间戳) 构建每场比赛的赔率运动轨迹,
提取时序特征(速度/加速度/方向翻转/跨市场共振/尾段集中度)。

数据链: odds_changes(30.5M 行 from→to 事件) → 按场聚合 → 轨迹特征向量
设计纪律: 全部特征基于去水概率的变化量/相对变化, 不用原始赔率值(抗诱导铁律)。

用法:
  from pipeline.odds_trajectory import extract_trajectory_features
  feats = extract_trajectory_features(con, match_key, kickoff)
"""
import math
from typing import Dict, List, Optional, Tuple

import numpy as np


def _devig3(h, d, a):
    s = 1/h + 1/d + 1/a
    return [x/s for x in inv]


def _devig3(h: float, d: float, a: float) -> Tuple[float, float, float]:
    s = 1/h + 1/d + 1/a
    return (1/h)/s, (1/d)/s, (1/a)/s


def _devig2(x, y):
    s = 1/x + 1/y
    return (1/x)/s


def build_market_series(events: List[dict], t0: float) -> Dict[str, List[Tuple[float, float, float]]]:
    """odds_changes 行 → {market+selection: [(elapsed_min, devig_prob, odds)]}.

    events 须含: market, selection, from_odds, to_odds, captured_at
    返回按市场选择对分组的时序点列(去水概率)。
    """
    out = {}
    for e in events:
        mkt = e.get('market', '')
        sel = e.get('selection', '')
        to_o = e.get('to_odds')
        if not to_o or float(to_o) <= 0.01 or float(to_o) > 500:
            continue
        ts = e.get('captured_at', 0)
        elapsed = (ts - t0) / 60.0
        k = f'{mkt}|{sel}'
        out.setdefault(k, []).append((elapsed, float(to_odds), ts))
    return out


def _devig_series_1x2(events: List[dict], t0: float) -> List[dict]:
    """1X2 from→to 事件 → 时序去水概率列 [(elapsed, ph, pd, pa)]."""
    home_e = sorted([e for e in events if e.get('selection') == 'home'], key=lambda x: x.get('captured_at', 0))
    draw_e = sorted([e for e in events if e.get('selection') == 'draw'], key=lambda x: x.get('captured_at', 0))
    away_e = sorted([e for e in events if e.get('selection') == 'away'], key=lambda x: x.get('captured_at', 0))
    if not home_e or not draw_e or not away_e:
        return []
    # 时间对齐: 取三类事件并集时间点, 各取最近值
    all_ts = sorted(set(e.get('captured_at', 0) for e in home_e + draw_e + away_e))
    hi = di = ai = 0
    h_v = d_v = a_v = None
    out = []
    for ts in all_ts:
        while hi < len(home_e) and home_e[hi].get('captured_at', 0) <= ts:
            h_v = float(home_e[hi].get('to_odds', 0)); hi += 1
        while di < len(draw_e) and draw_e[di].get('captured_at', 0) <= ts:
            d_v = float(draw_e[di].get('to_odds', 0)); di += 1
        while ai < len(away_e) and away_e[ai].get('captured_at', 0) <= ts:
            a_v = float(away_e[ai].get('to_odds', 0)); ai += 1
        if h_v and d_v and a_v:
            ph, pd_, pa = _devig3(h_v, d_v, a_v)
            elapsed = (ts - t0) / 60.0
            out.append({'elapsed': elapsed, 'ph': ph, 'pd': pd_, 'pa': pa})
    return out


def trajectory_features(events_1x2: List[dict], t0: float) -> Optional[Dict[str, float]]:
    """从 1X2 变化事件链提取轨迹特征向量.

    events_1x2: [{market:'1X2', selection:'home', to_odds:2.0, captured_at:...}, ...]
    返回 12 个特征或 None(数据不足)。
    """
    if len(events_1x2) < 4:
        return None
    series = _devig_series_1x2(events_1x2, t0)
    if len(series) < 4:
        return None

    ph = np.array([s['ph'] for s in series])
    elapsed = np.array([s['elapsed'] for s in series])
    n = len(series)
    if n < 4:
        return None

    # ── 速度特征 (去水概率变化速率, pp/min) ──
    dt = np.diff(elapsed)
    dt[dt < 0.01] = 0.01
    dph = np.diff(ph)
    velocity = dph / dt
    # 加权平均速度(时间加权)
    w = dt / dt.sum()
    v_mean = float(np.sum(velocity * w))
    v_abs_mean = float(np.mean(np.abs(velocity)))
    v_max = float(np.max(np.abs(velocity)))
    v_std = float(np.std(velocity))

    # ── 方向翻转 ──
    signs = np.sign(velocity[velocity != 0])
    flips = int(np.sum(signs[1:] != signs[:-1])) if len(signs) > 1 else 0

    # ── 尾段集中度: 最后30分钟变化占总变化比 ──
    total_change = float(np.sum(np.abs(dph)))
    mask_tail = elapsed >= (elapsed[-1] - 30.0)
    tail_idx = np.where(elapsed >= (elapsed[-1] - 30.0))[0]
    tail_change = float(np.sum(np.abs(dph[tail_idx[0]:]))) if len(tail_idx) else 0.0
    tail_ratio = tail_change / max(total_change, 0.001)

    # ── 早期大波动计数(前30分钟 |Δp|>0.03) ──
    early_idx = np.where(elapsed <= 30.0)[0]
    early_big = int(np.sum(np.abs(dph[early_idx]) > 0.03)) if len(early_idx) else 0

    # ── 波动率 ──
    vol = float(np.std(ph))

    return {
        'traj_velocity_mean': v_mean,
        'traj_velocity_abs': v_abs_mean,
        'traj_velocity_max': v_max,
        'traj_velocity_std': v_std,
        'traj_flips': flips,
        'traj_tail_ratio': tail_ratio,
        'traj_early_big_moves': early_big,
        'traj_volatility': vol,
        'traj_n_points': n,
        'traj_ph_start': float(ph[0]),
        'traj_ph_end': float(ph[-1]),
        'traj_ph_range': float(ph.max() - ph.min()),
    }

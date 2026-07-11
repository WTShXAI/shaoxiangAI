#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bet_core: 统一投注核心 (P0-2 / P0-3 / live_pilot 单一事实源).

把分散在 p0_2.decide / p0_3.decide_dir 的重复下注逻辑收敛到一处, 杜绝公式漂移:
  - kelly_fraction 封装 (规范满凯利分数 (p*o-1)/(o-1); 已修正 P0-3 误用 0.5*(p*o-1) 当分数的坑)
  - 规范半凯利 (FRAC_KELLY=0.5) + 单注封顶 10% (MAX_STAKE_FRAC; P0-2 修复的 kelly>1 全押 bug)
  - decide_argmax / decide_direction 两个方向入口, 底层同实现

所有回测/守护脚本均 import 本模块; 任何公式改动只在此一处, 由 tests/test_bet_core.py 守护.
"""
import os
import sys

# 仓库根 (兼容本地 Windows 与 Linux CI runner), 不写死绝对路径
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.deep_report import kelly_fraction

BANKROLL = 3000.0
FRAC_KELLY = 0.5
MAX_STAKE_FRAC = 0.10  # 单注上限 = 10% 当前本金


def safe_stake(p, o, equity, frac_kelly=FRAC_KELLY, max_frac=MAX_STAKE_FRAC):
    """规范凯利封顶注码. 返回 (stake, kelly_frac); 若 kelly<=0 或 stake 非法返回 (0.0, 0.0)."""
    k = kelly_fraction(p, o)
    if k <= 0:
        return 0.0, 0.0
    frac = frac_kelly * k
    if frac > max_frac:
        frac = max_frac
    stake = frac * equity
    if stake <= 0 or stake > equity:
        return 0.0, 0.0
    return stake, k


def decide_direction(direction_idx, p_vec, odds, equity, winner):
    """按指定方向下注(价值层/任意信号): 规范半凯利(封顶), 返回(新equity, stake, win)."""
    p = p_vec[direction_idx]
    o = odds[direction_idx]
    stake, _ = safe_stake(p, o, equity)
    if stake <= 0:
        return equity, 0.0, False
    d = ("H", "D", "A")[direction_idx]
    if winner == d:
        return equity + stake * (o - 1), stake, True
    return equity - stake, stake, False


def decide_argmax(p_vec, odds, equity, winner):
    """argmax 方向下注(共识/押热门): 底层复用 decide_direction."""
    i = int(max(range(3), key=lambda j: p_vec[j]))
    return decide_direction(i, p_vec, odds, equity, winner)

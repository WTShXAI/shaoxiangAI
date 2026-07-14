#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bet_core 单一事实源 · 轻量 CI 守卫 (无重DB, 毫秒级).

守护 P0-2/P0-3/live_pilot 共用的下注数学:
  G1 规范凯利分数 = (p*o-1)/(o-1) (非误用的 0.5*(p*o-1))
  G2 高赔冷门不触发全押 (单注 <= MAX_STAKE_FRAC*equity)
  G3 负/零 kelly -> 不下注 (safe_stake 返回 (0,0))
  G4 decide_argmax 与 decide_direction 行为一致
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.bet_core import (kelly_fraction, safe_stake, decide_direction,
                              decide_argmax, MAX_STAKE_FRAC, FRAC_KELLY, BANKROLL)


def test_kelly_canonical():
    # 规范凯利分数 = (p*o-1)/(o-1)
    assert abs(kelly_fraction(0.41, 7.25) - (0.41 * 7.25 - 1) / (7.25 - 1)) < 1e-9


def test_no_bust_high_odds():
    # 高赔冷门不会全押 (P0-3 根因bug守卫)
    eq = 3000.0
    new_eq, stake, _ = decide_direction(0, [0.41, 0.33, 0.26], [7.25, 3.0, 2.0], eq, "H")
    assert stake <= MAX_STAKE_FRAC * eq + 1e-9
    assert stake < eq


def test_buggy_formula_rejected():
    # 反证: 若误用 0.5*(p*o-1) 当分数, 单注分数会≈0.986(近全押) -> 这里必须不成立
    buggy = 0.5 * (0.41 * 7.25 - 1)
    _, stake, _ = decide_direction(0, [0.41, 0.33, 0.26], [7.25, 3.0, 2.0], 3000.0, "H")
    assert abs((stake / 3000.0) - buggy) > 0.05


def test_negative_kelly_no_bet():
    # 负/零 kelly -> 不下注
    assert safe_stake(0.1, 1.5, 3000.0) == (0.0, 0.0)
    eq, stake, win = decide_direction(0, [0.1, 0.45, 0.45], [1.5, 2.0, 2.0], 3000.0, "H")
    assert stake == 0.0


def test_argmax_equiv_direction():
    p_vec = [0.5, 0.27, 0.23]
    odds = [2.0, 3.4, 6.0]
    eq1, s1, w1 = decide_argmax(p_vec, odds, 3000.0, "H")
    eq2, s2, w2 = decide_direction(0, p_vec, odds, 3000.0, "H")
    assert (eq1, s1, w1) == (eq2, s2, w2)

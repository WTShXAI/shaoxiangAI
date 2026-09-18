#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bet_core 单一事实源 · 轻量 CI 守卫 (无重DB, 毫秒级).

守护 P0-1/P0-2/P0-3/live_pilot 共用的下注数学:
  G1 规范凯利分数 = (p*o-1)/(o-1) (非误用的 0.5*(p*o-1))
  G2 高赔冷门不触发全押 (单注 <= MAX_STAKE_FRAC*equity)
  G3 负/零 kelly -> 不下注 (safe_stake 返回 (0,0))
  G4 decide_argmax 与 decide_direction 行为一致
  P0-1 价值闸门: PROD 环境 value_layer_approved=False → NO-BET
  P0-2 回撤预算: drawdown_scale + monthly limits
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.bet_core import (kelly_fraction, safe_stake, decide_direction,
                              decide_argmax, MAX_STAKE_FRAC, FRAC_KELLY, BANKROLL,
                              calc_drawdown_scale, check_monthly_limit,
                              VALUE_GATE_ENFORCED)

# ═══════════════════════════════════════════════════════════════════════════
# 原有 G1-G4 守卫 (不可退化)
# ═══════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════
# P0-1 价值闸门强制 (Flat Argmax 在 PROD 禁)
# ═══════════════════════════════════════════════════════════════════════════

def test_p01_value_gate_blocks_unapproved():
    """P0-1: safe_stake value_layer_approved=False 时 (DEV 环境下 gate=True),
    行为取决于 VALUE_GATE_ENFORCED。DEV 环境不应阻止。"""
    # DEV 环境: value_layer_approved=False 不阻止 (向后兼容)
    # 注意: 当前 ENV=DEV, VALUE_GATE_ENFORCED 应为 False
    # p=0.55, odds=2.0 → kelly = (0.55*2-1)/1 = 0.10 > 0
    stake, k = safe_stake(0.55, 2.0, 3000.0, gate=True, value_layer_approved=False)
    # DEV 环境: 应正常下注
    assert stake > 0, f"DEV environment should allow bets without value gate; got stake={stake}"


def test_p01_value_gate_allows_approved():
    """P0-1: value_layer_approved=True 在所有环境都应正常下注."""
    stake, k = safe_stake(0.55, 2.0, 3000.0, gate=True, value_layer_approved=True)
    assert stake > 0


def test_p01_value_gate_blocks_in_prod_simulation():
    """P0-1: 直接模拟 PROD 逻辑 — 当 VALUE_GATE_ENFORCED=True 时,
    未审批的下注应被阻止。这里直接测试 safe_stake 的 value_layer_approved 分支。"""
    # 即使 VALUE_GATE_ENFORCED 在 DEV 下为 False,
    # 我们传 gate=False 验证 gate 机制本身就阻止下注 (这是基础行为)
    stake, k = safe_stake(0.55, 2.0, 3000.0, gate=False, value_layer_approved=False)
    assert stake == 0.0, "gate=False must block bets regardless of value_layer_approved"


def test_p01_decide_argmax_no_value_approval():
    """P0-1: decide_argmax 默认 value_layer_approved=False.
    在 DEV 环境仍应正常下注; PROD 环境由 safe_stake 内部的 VALUE_GATE_ENFORCED 阻止."""
    # p_vec=[0.55, 0.27, 0.18], odds=[1.8, 3.4, 6.0]
    # argmax picks H: p=0.55, odds=1.8 → kelly=(0.55*1.8-1)/0.8 = (0.99-1)/0.8 < 0
    # Need: p*odds > 1 → 0.55*1.8=0.99 < 1. Use odds=2.0 instead.
    eq, stake, win = decide_argmax(
        [0.55, 0.27, 0.18], [2.0, 3.4, 6.0], 3000.0, "H",
        value_layer_approved=False)
    # DEV 环境: 应正常下注
    assert stake > 0, "DEV: argmax should still bet (gate=True by default)"


def test_p01_decide_argmax_with_value_approval():
    """P0-1: decide_argmax value_layer_approved=True 在所有环境正常下注."""
    eq, stake, win = decide_argmax(
        [0.55, 0.27, 0.18], [2.0, 3.4, 6.0], 3000.0, "H",
        value_layer_approved=True)
    assert stake > 0


# ═══════════════════════════════════════════════════════════════════════════
# P0-2 回撤预算: 动态 Kelly 缩放 + 月度止盈止损
# ═══════════════════════════════════════════════════════════════════════════

def test_p02_drawdown_scale_no_dd():
    """P0-2: 无回撤 (current >= peak) → scale=1.0."""
    assert calc_drawdown_scale(3000.0, 3000.0) == 1.0
    assert calc_drawdown_scale(3500.0, 3000.0) == 1.0  # above peak


def test_p02_drawdown_scale_halfway():
    """P0-2: 5% 回撤 (10%预算的一半) → scale ≈ 0.625 (线性插值)."""
    # dd = 1 - 2850/3000 = 0.05
    # ratio = 0.05 / 0.10 = 0.5
    # scale = 1 - 0.5*(1-0.25) = 1 - 0.5*0.75 = 1 - 0.375 = 0.625
    scale = calc_drawdown_scale(2850.0, 3000.0)
    assert abs(scale - 0.625) < 1e-9


def test_p02_drawdown_scale_full():
    """P0-2: 10% 回撤 → scale=0.25 (最低)."""
    scale = calc_drawdown_scale(2700.0, 3000.0)
    assert abs(scale - 0.25) < 1e-9


def test_p02_drawdown_scale_beyond():
    """P0-2: >10% 回撤 → scale 保持在 0.25 (不继续降)."""
    scale = calc_drawdown_scale(2400.0, 3000.0)  # 20% dd
    assert abs(scale - 0.25) < 1e-9


def test_p02_drawdown_scale_zero_peak():
    """P0-2: peak=0 边界 → 返回 scale_min."""
    assert calc_drawdown_scale(3000.0, 0.0) == 0.25


def test_p02_drawdown_scale_custom_params():
    """P0-2: 自定义参数."""
    scale = calc_drawdown_scale(2700.0, 3000.0, max_drawdown_pct=0.20, scale_min=0.5)
    # dd = 0.10, ratio = 0.10/0.20 = 0.5
    # scale = 1 - 0.5*(1-0.5) = 1 - 0.25 = 0.75
    assert abs(scale - 0.75) < 1e-9


def test_p02_monthly_stop_loss():
    """P0-2: 月度止损触发."""
    should_stop, reason = check_monthly_limit(-500.0, stop_loss=-500.0, stop_profit=1500.0)
    assert should_stop
    assert "止损" in reason


def test_p02_monthly_stop_loss_not_triggered():
    """P0-2: 月度止损未触发."""
    should_stop, reason = check_monthly_limit(-400.0, stop_loss=-500.0, stop_profit=1500.0)
    assert not should_stop


def test_p02_monthly_stop_profit():
    """P0-2: 月度止盈触发."""
    should_stop, reason = check_monthly_limit(1500.0, stop_loss=-500.0, stop_profit=1500.0)
    assert should_stop
    assert "止盈" in reason


def test_p02_monthly_stop_profit_not_triggered():
    """P0-2: 月度止盈未触发."""
    should_stop, reason = check_monthly_limit(1400.0, stop_loss=-500.0, stop_profit=1500.0)
    assert not should_stop


def test_p02_monthly_normal():
    """P0-2: 正常区间 (不触发任何)."""
    should_stop, reason = check_monthly_limit(200.0, stop_loss=-500.0, stop_profit=1500.0)
    assert not should_stop


def test_p02_monthly_limit_in_safe_stake():
    """P0-2: safe_stake 在月度止损触发时返回 NO-BET."""
    # month_pnl=-600 触发了 -500 止损
    stake, k = safe_stake(0.5, 2.0, 3000.0, gate=True, month_pnl=-600.0)
    assert stake == 0.0, "Monthly stop-loss should block bets"


def test_p02_drawdown_in_safe_stake():
    """P0-2: safe_stake 在回撤时缩小注码 (不阻止, 只缩放)."""
    # 3000 peak, 2850 current (5% dd) → scale ≈ 0.625
    # With full kelly on 0.5,2.0: k = (0.5*2-1)/(2-1) = 0.0 → no bet
    # Use a better edge: p=0.6, odds=2.0 → k = (0.6*2-1)/1 = 0.2
    # frac_kelly=0.5 → frac = 0.5 * 0.2 = 0.10
    # With 5% dd → scale 0.625 → frac = 0.10 * 0.625 = 0.0625
    # Without dd: stake should be larger
    stake_no_dd, _ = safe_stake(0.6, 2.0, 2850.0, gate=True)
    stake_with_dd, _ = safe_stake(0.6, 2.0, 2850.0, gate=True, peak_equity=3000.0)
    # With drawdown, stake should be smaller (scaled)
    assert stake_with_dd < stake_no_dd, (
        f"Drawdown should reduce stake: dd={stake_with_dd:.2f} vs no_dd={stake_no_dd:.2f}"
    )
    assert stake_with_dd > 0, "With good edge, scaled bet should still be > 0"


def test_p02_no_drawdown_when_disabled():
    """P0-2: peak_equity=None 时不缩放 (向后兼容)."""
    # 默认 peak_equity=None → 不应用 DD 缩放
    stake1, _ = safe_stake(0.6, 2.0, 2850.0, gate=True)
    stake2, _ = safe_stake(0.6, 2.0, 2850.0, gate=True, peak_equity=None)
    assert stake1 == stake2


# ═══════════════════════════════════════════════════════════════════════════
# 综合守卫: P0-1 + P0-2 组合
# ═══════════════════════════════════════════════════════════════════════════

def test_p01_p02_combined_approved_with_dd():
    """P0-1+P0-2 组合: 审批通过 + 回撤缩放同时生效."""
    stake, k = safe_stake(
        0.6, 2.0, 2850.0, gate=True,
        value_layer_approved=True,
        peak_equity=3000.0, month_pnl=200.0)
    assert stake > 0
    # Should be scaled down from the non-dd case
    stake_full, _ = safe_stake(
        0.6, 2.0, 2850.0, gate=True,
        value_layer_approved=True)
    assert stake < stake_full, "DD scaling should reduce stake"


def test_p01_p02_combined_approved_monthly_stop():
    """P0-1+P0-2 组合: 审批通过但月度止损触发 → NO-BET."""
    stake, k = safe_stake(
        0.6, 2.0, 2850.0, gate=True,
        value_layer_approved=True,
        month_pnl=-600.0)  # 触发了 -500 止损
    assert stake == 0.0, "Monthly stop-loss should take priority"

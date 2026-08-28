#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略层+组合层 SSoT · 轻量 CI 守卫 (无重DB, 毫秒级).

守护 P0 #19 底座:
  G1 只收 decision=="BET" 入组合 (可证伪)
  G2 总暴露硬约束: Σ期望占比 > max_exposure 时等比缩放, 最终暴露 ≈ max_exposure
  G3 单注封顶: 每注 stake <= MAX_STAKE_FRAC * bankroll (bet_core SSoT 强制)
  G4 max_bets 截断: 超出部分进 rejected(max_bets_exceeded)
  G5 min_edge_pct 阈值: 低于进 rejected(edge_below_threshold)
  G6 gate=False → 全 PASS (safe_stake 返回 0)
  G7 最终注码一律过 bet_core.safe_stake (不直接用价值层 stake_unit)
  G8 KellyAggregateStrategy.build 与 build_portfolio 等价
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.strategy import (
    build_portfolio, ValueSignal, Constraints, KellyAggregateStrategy,
)
from scripts.bet_core import MAX_STAKE_FRAC


def _bet(mid, odds, model_prob, edge_pct, selection="H"):
    return ValueSignal(
        mid=mid, home=f"H{mid}", away=f"A{mid}", market="1X2",
        selection=selection, odds=odds, model_prob=model_prob,
        edge_pct=edge_pct, decision="BET", strategy_id="t",
    )


def test_only_bet_signals_aggregated():
    sigs = [
        _bet("1", 2.5, 0.5, 10.0),
        ValueSignal(mid="2", decision="PASS", edge_pct=20.0),  # PASS 不进
        _bet("3", 2.5, 0.5, 8.0),
    ]
    plan = build_portfolio(sigs, bankroll=3000.0)
    assert len(plan.intents) == 2
    assert all(i.strategy_id == "t" for i in plan.intents)
    assert any(r["reason"] == "not_bet" for r in plan.rejected)


def test_total_exposure_scaled_to_cap():
    # 5 注各 kelly=0.1667, 半凯利期望占比 0.0833, Σ=0.4167 > 0.30 → 等比缩放
    sigs = [_bet(f"m{i}", 2.5, 0.5, 10.0 + i) for i in range(5)]
    plan = build_portfolio(sigs, bankroll=3000.0, constraints=Constraints(max_exposure=0.30))
    assert abs(plan.total_exposure_pct - 0.30) < 1e-6
    assert plan.total_stake <= 0.30 * 3000.0 + 1e-6
    # 每注仍过 safe_stake 封顶
    for i in plan.intents:
        assert i.stake <= MAX_STAKE_FRAC * 3000.0 + 1e-6


def test_per_bet_cap_enforced():
    # odds=4, p=0.5 → kelly=0.333, 半凯利期望占比 0.1667 > 10% 封顶 → safe_stake 截到 300
    sigs = [_bet("big", 4.0, 0.5, 30.0)]
    plan = build_portfolio(sigs, bankroll=3000.0)
    assert len(plan.intents) == 1
    assert abs(plan.intents[0].stake - MAX_STAKE_FRAC * 3000.0) < 1e-6


def test_max_bets_truncation():
    sigs = [_bet(f"m{i}", 2.5, 0.5, float(i)) for i in range(1, 13)]  # edge 1..12
    plan = build_portfolio(sigs, bankroll=3000.0, constraints=Constraints(max_bets=10))
    assert len(plan.intents) == 10
    assert any(r["reason"] == "max_bets_exceeded" for r in plan.rejected)
    # 保留 edge 最高的 10 个 (12..3), 丢弃 edge 1,2
    kept = {i.mid for i in plan.intents}
    assert "m1" not in kept and "m2" not in kept
    assert "m12" in kept


def test_min_edge_threshold():
    sigs = [
        _bet("hi", 2.5, 0.5, 10.0),
        _bet("lo", 2.5, 0.5, 3.0),   # < 5
        _bet("mid", 2.5, 0.5, 8.0),
    ]
    plan = build_portfolio(sigs, bankroll=3000.0, constraints=Constraints(min_edge_pct=5.0))
    assert len(plan.intents) == 2
    assert any(r["reason"] == "edge_below_threshold" for r in plan.rejected)


def test_gate_false_blocks_all():
    sigs = [_bet("1", 2.5, 0.5, 10.0), _bet("2", 2.0, 0.6, 12.0)]
    plan = build_portfolio(sigs, bankroll=3000.0, gate=False)
    assert len(plan.intents) == 0
    assert plan.total_stake == 0.0


def test_kelly_aggregate_equals_build_portfolio():
    sigs = [_bet(f"m{i}", 2.5, 0.5, 10.0 + i) for i in range(4)]
    cons = Constraints(max_exposure=0.25, max_bets=3)
    p1 = build_portfolio(sigs, bankroll=3000.0, constraints=cons)
    p2 = KellyAggregateStrategy().build(sigs, bankroll=3000.0, constraints=cons)
    assert p1.to_dict() == p2.to_dict()


def test_value_signal_from_dict():
    d = {"mid": "x", "home": "A", "away": "B", "odds": 2.1,
         "model_prob": 0.55, "edge_pct": 7.5, "decision": "BET"}
    s = ValueSignal.from_dict(d)
    assert s.mid == "x" and s.odds == 2.1 and s.model_prob == 0.55
    assert s.decision == "BET" and s.market == "1X2"

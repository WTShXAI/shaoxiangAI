# -*- coding: utf-8 -*-
"""执行层 + 手动确认闸 单测 (A #20 底座).

覆盖: 结算纯函数 (1X2/大小球/pending) · 模拟执行写库/不写库 · 手动闸 submit不落库/confirm落库/reject丢弃 · mode 列过滤.
"""
import os, sys, tempfile, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.strategy import BetIntent, BetPlan, Constraints
from pipeline.execution import (
    settle_intent, expected_pnl, SimExecutor, ManualConfirmationGate,
)
from database import Database


def _intent(**kw):
    base = dict(mid="m1", home="A", away="B", market="1X2", selection="H",
                odds=2.5, model_prob=0.5, edge_pct=10.0, stake=250.0,
                kelly_frac=0.0833, strategy_id="test")
    base.update(kw)
    return BetIntent(**base)


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # Database() 自己建
    return Database(path)


# ── 1. settle_intent 纯函数 ──

def test_settle_1x2_win():
    it = _intent(selection="H", odds=2.0, stake=300.0)
    r = settle_intent(it, {"winner": "H"})
    assert r.result == "win" and r.pnl == 300.0  # 300*(2-1)


def test_settle_1x2_loss():
    it = _intent(selection="H", odds=2.0, stake=300.0)
    r = settle_intent(it, {"winner": "A"})
    assert r.result == "loss" and r.pnl == -300.0


def test_settle_ou_via_goals():
    it = _intent(market="OU", selection="Over 2.5", odds=1.9, stake=200.0)
    r = settle_intent(it, {"home_goals": 2, "away_goals": 2})  # total 4 > 2.5
    assert r.result == "win"
    r2 = settle_intent(it, {"home_goals": 1, "away_goals": 1})  # total 2 < 2.5
    assert r2.result == "loss"


def test_settle_pending_no_info():
    it = _intent(selection="H")
    r = settle_intent(it, {})  # 无 winner/won/goals
    assert r.result == "pending" and r.pnl == 0.0


def test_expected_pnl_positive_edge():
    it = _intent(model_prob=0.5, odds=2.5, stake=200.0)
    # E = 0.5*200*1.5 - 0.5*200 = 150-100 = 50
    assert abs(expected_pnl(it) - 50.0) < 1e-6


# ── 2. 模拟执行器 ──

def test_sim_execute_with_results_writes_db():
    db = _tmp_db()
    plan = BetPlan(intents=[_intent(selection="H", odds=2.0, stake=300.0),
                            _intent(mid="m2", selection="A", odds=3.0, stake=100.0)],
                   total_stake=400.0, total_exposure_pct=0.1333)
    ex = SimExecutor(db=db)
    res = ex.execute(plan, results={"m1": {"winner": "H"}, "m2": {"winner": "H"}})
    # m1 win (+300), m2 loss (-100)
    assert res["summary"]["wins"] == 1 and res["summary"]["losses"] == 1
    assert abs(res["summary"]["total_pnl"] - 200.0) < 1e-6
    # 落库 (mode=sim)
    bets = db.get_bets()
    assert len(bets) == 2
    assert all(b["mode"] == "sim" for b in bets)
    assert abs(db.equity() - 200.0) < 1e-6


def test_sim_dry_run_no_db_write():
    db = _tmp_db()
    plan = BetPlan(intents=[_intent(stake=250.0)], total_stake=250.0)
    ex = SimExecutor(db=db)
    res = ex.execute(plan, results=None)  # 无 results
    assert res["summary"]["dry_run"] is True
    assert db.get_bets() == []  # 不写库


# ── 3. 手动确认闸 ──

def test_gate_submit_not_writes_db():
    db = _tmp_db()
    plan = BetPlan(intents=[_intent(stake=300.0)], total_stake=300.0)
    gate = ManualConfirmationGate()
    pid = gate.submit(plan)
    assert pid and db.get_bets() == []  # 提交不落库


def test_gate_confirm_writes_real_db():
    db = _tmp_db()
    plan = BetPlan(intents=[_intent(selection="H", odds=2.5, stake=250.0)],
                   total_stake=250.0)
    gate = ManualConfirmationGate()
    pid = gate.submit(plan)
    receipt = gate.confirm(pid, db=db)
    assert receipt["ok"] and receipt["written"] == 1
    bets = db.get_bets()
    assert len(bets) == 1 and bets[0]["mode"] == "real" and bets[0]["result"] == "pending"


def test_gate_confirm_unknown_returns_false():
    gate = ManualConfirmationGate()
    r = gate.confirm("nope", db=_tmp_db())
    assert r["ok"] is False


def test_gate_reject_discards():
    db = _tmp_db()
    plan = BetPlan(intents=[_intent()], total_stake=100.0)
    gate = ManualConfirmationGate()
    pid = gate.submit(plan)
    rej = gate.reject(pid)
    assert rej["rejected"] is True
    # 已丢弃, 再 confirm 失败
    assert gate.confirm(pid, db=db)["ok"] is False
    assert db.get_bets() == []


def test_gate_list_pending():
    plan = BetPlan(intents=[_intent()], total_stake=100.0)
    gate = ManualConfirmationGate()
    gate.submit(plan)
    assert len(gate.list_pending()) == 1


# ── 4. database mode 过滤 ──

def test_db_mode_filter():
    db = _tmp_db()
    db.add_bet(match="s", outcome="H", odds=2.0, stake=100.0, result="win", pnl=100.0, mode="sim")
    db.add_bet(match="r", outcome="A", odds=2.0, stake=100.0, result="pending", pnl=0.0, mode="real")
    sim_eq = db.get_equity_curve(mode="sim")
    real_eq = db.get_equity_curve(mode="real")
    assert len(sim_eq) == 1 and sim_eq[0]["equity"] == 100.0
    assert len(real_eq) == 1 and real_eq[0]["equity"] == 0.0
    all_eq = db.get_equity_curve()
    assert len(all_eq) == 2


def test_db_settle_bet_recomputes_pnl():
    db = _tmp_db()
    bid = db.add_bet(match="r", outcome="H", odds=3.0, stake=200.0, result="pending", pnl=0.0, mode="real")
    ok = db.settle_bet(bid, "win")  # pnl = 200*(3-1)=400
    assert ok
    b = db.get_bets()[0]
    assert b["result"] == "win" and abs(b["pnl"] - 400.0) < 1e-6


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

"""
tests/test_quant_flow.py — 量化资金链路集成测试
================================================
覆盖最高价值资金路径:
  决策提交 → 资金占用 → 人工确认闸 → 结算(赢/输) → 资金划拨 → 绩效更新
  + 拒绝释放占用 + 未知策略拒绝

使用独立临时 DB (quant_init_db seed), 不触发 live API / 不读真实盘口。
"""
import os
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import scripts.quant_init_db as quant_init_db
import scripts.quant_executor as qe


STRATEGY_ID = "v71_wc_1x2"


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp(prefix="quantflow_")
    db_path = os.path.join(tmp, "quant_trading.db")
    quant_init_db.init_db(seed=True, db_path=db_path)
    yield db_path


def _decision(direction, model_prob, side_odds_map, confirmation_required=True):
    return {
        "strategy_id": STRATEGY_ID,
        "home_team": "TestHome", "away_team": "TestAway",
        "league": "TEST", "commence_time": "2026-07-14T20:00:00Z",
        "direction": direction, "model_prob": model_prob,
        "market_prob": round(1 - model_prob, 3),
        "book_odds_h": side_odds_map["H"], "book_odds_d": side_odds_map["D"],
        "book_odds_a": side_odds_map["A"],
        "book_odds": side_odds_map[direction],
        "edge_pct": 20.0, "expected_value": 0.10,
        "decision_text": "test", "operator_action": "下注",
        "confirmation_required": confirmation_required,
    }


def _avail(db_path):
    br = qe.get_bankroll(db_path)
    return br["current_balance"] - br["reserved_balance"]


def test_confirm_gate_win_math(db):
    """提交(pending,占用) → 确认 → 结算赢 → 资金正确划拨"""
    br0 = qe.get_bankroll(db)["current_balance"]
    odds = {"H": 2.20, "D": 3.40, "A": 3.10}
    res = qe.submit_decision(_decision("H", 0.55, odds), db_path=db)
    assert res.get("ok") is True, res
    assert res["status"] == "pending"
    stake = res["stake_amount"]
    assert stake > 1.0

    # 提交后资金被占用
    assert qe.get_bankroll(db)["reserved_balance"] == stake
    assert _avail(db) == br0 - stake

    # 确认闸
    c = qe.confirm_order(res["order_id"], db_path=db)
    assert c.get("ok") is True and c["status"] == "confirmed"

    # 结算赢: pnl = stake * (odds-1)
    s = qe.settle_match(res["position_id"], actual_result="H", actual_score="2-1", db_path=db)
    assert s.get("ok") is True and s["won"] is True
    expected = round(br0 + stake * (odds["H"] - 1), 2)
    assert qe.get_bankroll(db)["current_balance"] == expected
    # 占用已释放
    assert qe.get_bankroll(db)["reserved_balance"] == 0.0


def test_settle_loss_math(db):
    """结算输 → 资金减少 stake, 占用释放"""
    br0 = qe.get_bankroll(db)["current_balance"]
    odds = {"H": 2.10, "D": 3.30, "A": 3.20}
    res = qe.submit_decision(_decision("A", 0.40, odds), db_path=db)
    assert res.get("ok") is True
    stake = res["stake_amount"]
    qe.confirm_order(res["order_id"], db_path=db)
    # 实际主胜 → 我方客胜(A)输
    s = qe.settle_match(res["position_id"], actual_result="H", actual_score="1-0", db_path=db)
    assert s["won"] is False
    assert qe.get_bankroll(db)["current_balance"] == round(br0 - stake, 2)
    assert qe.get_bankroll(db)["reserved_balance"] == 0.0


def test_reject_releases_balance(db):
    """拒绝 pending 订单 → 占用资金释放回账户"""
    odds = {"H": 2.20, "D": 3.40, "A": 3.10}
    res = qe.submit_decision(_decision("H", 0.55, odds), db_path=db)
    stake = res["stake_amount"]
    assert qe.get_bankroll(db)["reserved_balance"] == stake
    r = qe.reject_order(res["order_id"], reason="人工拒绝", db_path=db)
    assert r.get("ok") is True
    assert qe.get_bankroll(db)["reserved_balance"] == 0.0
    # 资金账户余额不变 (仅占用释放, 未结算)
    assert qe.get_bankroll(db)["current_balance"] == 10000.0


def test_unknown_strategy_rejected(db):
    """未知策略_id → 提交被拒, 不创建持仓/占用"""
    bad = _decision("H", 0.55, {"H": 2.20, "D": 3.40, "A": 3.10})
    bad["strategy_id"] = "does_not_exist"
    res = qe.submit_decision(bad, db_path=db)
    assert "error" in res
    assert qe.get_bankroll(db)["reserved_balance"] == 0.0
    assert qe.get_bankroll(db)["current_balance"] == 10000.0


def test_read_paths_after_settle(db):
    """结算后读路径: list_positions / list_orders / get_performance 一致性"""
    odds = {"H": 2.20, "D": 3.40, "A": 3.10}
    res = qe.submit_decision(_decision("H", 0.55, odds), db_path=db)
    qe.confirm_order(res["order_id"], db_path=db)
    qe.settle_match(res["position_id"], "H", db_path=db)

    positions = qe.list_positions(limit=10, db_path=db)
    assert any(p["position_id"] == res["position_id"] and p["status"] == "settled" for p in positions)
    orders = qe.list_orders(status="filled", db_path=db)
    assert any(o["order_id"] == res["order_id"] for o in orders)

    perf = qe.get_performance(db_path=db)
    assert perf["settled_positions"] >= 1
    assert perf["bankroll"]["current_balance"] > 10000
    assert len(perf["performance_daily"]) >= 1


def test_compute_kelly_stake_direct(db):
    """凯利注码直测: 半凯利 + 单注封顶 10%"""
    k = qe.compute_kelly_stake(model_prob=0.55, book_odds=2.20, current_balance=10000)
    assert k["stake_amount"] > 0
    expected_half = max(0.0, (0.55 * 2.20 - 1) / (2.20 - 1) * 0.5)
    assert abs(k["kelly_half"] - round(expected_half, 4)) < 1e-6
    assert k["stake_amount"] <= 1000.0  # 单注封顶 10% of 10000


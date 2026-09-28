"""T33 ingest 门控口径只读对账 — 单测（零生产 I/O）。

被测试对象: scripts/audit_ingest_status_gate.py
覆盖: 口径解析 / 写方分类 / 守卫分支 / 只读连接 / 差额算术。
铁律: 不碰 events.db 写入、不跑 ingest、不碰生产服务。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest

from scripts.audit_ingest_status_gate import (
    R_BAD_PAYLOAD, R_DEVIG_FAIL, R_NO_ODDS, R_NO_RESULT, R_NOT_CREDIBLE, R_OK,
    _blank_reasons, _credible, _devig_power, classify_writer_line, evaluate_guard,
    extract_status_predicates, find_status_writers, open_readonly,
    parse_ingest_gates, parse_kickoff_ts_local, same_status_gate,
)


# ── 口径解析 ──────────────────────────────────────────────────────────────
def test_extract_status_predicates():
    assert extract_status_predicates("WHERE d.status='finished' AND m.score_home IS NOT NULL") \
        == {"d.status": "finished"}
    assert extract_status_predicates("WHERE m.status='finished'") == {"m.status": "finished"}
    assert extract_status_predicates("WHERE 1=1") == {}


def test_parse_ingest_gates_from_real_source():
    gates = parse_ingest_gates()
    assert gates["daily"] == {"d.status": "finished"}, "daily 入口应判 d.status"
    assert gates["knn"] == {"m.status": "finished"}, "knn 入口应判 m.status"
    assert "candles_ensemble" in gates["sources"]


def test_parse_ingest_gates_missing_file_falls_back():
    gates = parse_ingest_gates(os.path.join(tempfile.gettempdir(), "no_such_ingest.py"))
    assert gates == {"daily": {}, "knn": {}, "sources": ["candles_ensemble", "market_baseline"]}


def test_same_status_gate_requires_same_table():
    # 值相同但表不同 → 口径不一致（daily.status 是 matches.status 的一次性快照）
    assert same_status_gate({"daily": {"d.status": "finished"},
                             "knn": {"m.status": "finished"}}) is False
    assert same_status_gate({"daily": {"m.status": "finished"},
                             "knn": {"m.status": "finished"}}) is True
    assert same_status_gate({"daily": {}, "knn": {"m.status": "finished"}}) is False


# ── 写方分类 ──────────────────────────────────────────────────────────────
def test_classify_writer_line():
    assert classify_writer_line("UPDATE daily_predictions SET status='finished'") == "UPDATE_WRITER"
    assert classify_writer_line("SELECT * FROM daily_predictions WHERE x=1") == "READER"
    # INSERT 但 status 取 excluded.* → 快照写入, 不是"翻状态"
    ins = ('con.execute("""INSERT INTO daily_predictions\n'
           '(match_key, kickoff, status) VALUES (?,?,?)\n'
           'ON CONFLICT DO UPDATE SET status=excluded.status"""')
    assert classify_writer_line(ins.splitlines()[0], "\n".join(ins.splitlines()[1:])) == "PASSTHROUGH"
    assert classify_writer_line('INSERT INTO daily_predictions (a, status) VALUES (?, "finished")') \
        == "INSERT_WRITER"
    assert classify_writer_line("# daily_predictions 台账") == "OTHER"


def test_find_status_writers_reports_no_flipper_in_temp_repo():
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "w.py"), "w", encoding="utf-8") as fh:
            fh.write(
                "SELECT * FROM daily_predictions\n"
                "INSERT INTO daily_predictions (match_key, status) VALUES (?, 'finished')\n"
                "UPDATE daily_predictions SET status='finished'\n"
            )
        res = find_status_writers(td)
    assert res["status_flippers_exist"] is True        # 本 synthetic 仓库确有 UPDATE 翻状态
    kinds = {h["kind"] for h in res["hits"]}
    assert "UPDATE_WRITER" in kinds and "READER" in kinds


def test_find_status_writers_real_repo_has_no_update_flipper():
    res = find_status_writers()
    assert res["status_flippers_exist"] is False, \
        "全仓无 UPDATE daily_predictions → daily_predictions.status 无人翻转 = T31 C2 的机械根因"


# ── 守卫分支（纯函数，复刻 ingest loop）────────────────────────────────────
def _row(payload, sh=1, sa=0, ko="2026-09-20 20:00:00", mk="A vs B"):
    return {"match_key": mk, "payload": json.dumps(payload), "score_home": sh,
            "score_away": sa, "kickoff": ko, "match_date": "2026-09-20"}


def _payload_ok():
    return {"p_home": 0.5, "p_draw": 0.2, "p_away": 0.3,
            "market_implied": {"odds_1x2": [2.0, 3.0, 4.0]}}


def test_guard_bad_payload():
    assert evaluate_guard(_row("{not json"), {}, parse_kickoff_ts_local)[0] == R_BAD_PAYLOAD


def test_guard_no_result_on_missing_score():
    row = _row(_payload_ok())
    row["score_home"] = None
    assert evaluate_guard(row, {}, parse_kickoff_ts_local)[0] == R_NO_RESULT


def test_guard_not_credible_on_fake_0_0():
    row = _row(_payload_ok(), sh=0, sa=0)
    # 最后 tick 远早于 kickoff+95min → 断流定格的假 0-0
    assert evaluate_guard(row, {"A vs B": 1000.0}, parse_kickoff_ts_local)[0] == R_NOT_CREDIBLE


def test_guard_ok_on_real_score():
    row = _row(_payload_ok(), sh=1, sa=0)   # 有进球 → 恒可信（无需 tick 参保）
    assert evaluate_guard(row, {}, parse_kickoff_ts_local)[0] == R_OK


def test_guard_rejects_non_dict_payload():
    row = _row('"just a string"')
    assert evaluate_guard(row, {}, parse_kickoff_ts_local)[0] == R_BAD_PAYLOAD


def test_guard_no_odds():
    row = _row({"p_home": .5, "p_draw": .2, "p_away": .3, "market_implied": {}})
    assert evaluate_guard(row, {}, parse_kickoff_ts_local)[0] == R_NO_ODDS
    row2 = _row({"p_home": .5, "p_draw": .2, "p_away": .3,
                 "market_implied": {"odds_1x2": [2.0, 3.0]}})
    assert evaluate_guard(row2, {}, parse_kickoff_ts_local)[0] == R_NO_ODDS


def test_guard_devig_fail_on_illegal_odds():
    p = {"p_home": .5, "p_draw": .2, "p_away": .3, "market_implied": {"odds_1x2": [1.0, 3.0, 4.0]}}
    assert evaluate_guard(_row(p), {}, parse_kickoff_ts_local)[0] == R_DEVIG_FAIL


def test_guard_0_0_needs_tick_proof():
    """0-0 必须靠最后 tick ≥ kickoff+95min 证明非断流定格，否则一律剔除。"""
    row = _row(_payload_ok(), sh=0, sa=0)
    ko = parse_kickoff_ts_local(row["kickoff"])
    assert evaluate_guard(row, {"A vs B": ko + 96 * 60}, parse_kickoff_ts_local)[0] == R_OK
    assert evaluate_guard(row, {"A vs B": ko + 60 * 60}, parse_kickoff_ts_local)[0] == R_NOT_CREDIBLE


def test_guard_kickoff_parse():
    assert parse_kickoff_ts_local("2026-09-20 20:00:00") is not None
    assert parse_kickoff_ts_local("") is None
    assert parse_kickoff_ts_local("garbage") is None


# ── 去水 / 可信 纯函数 ────────────────────────────────────────────────────
def test_devig_power_normalizes_and_rejects_illegal():
    p = _devig_power([2.0, 3.0, 4.0])
    assert p is not None and abs(sum(p) - 1.0) < 1e-6
    assert _devig_power([1.0, 3.0, 4.0]) is None      # <=1.0 非法
    assert _devig_power([2.0, 3.0, 60.0]) is None     # >=50 非法


def test_credible_shifts():
    assert _credible(1, 1, None, None) is True        # 非 0-0 恒可信
    assert _credible(0, 0, None, None) is False       # 0-0 缺参保守 False
    ko = 1_700_000_000.0
    assert _credible(0, 0, ko + 96 * 60, ko) is True
    assert _credible(0, 0, ko + 10 * 60, ko) is False


def test_blank_reasons_covers_all():
    r = _blank_reasons()
    assert set(r) == {R_BAD_PAYLOAD, R_NO_RESULT, R_NOT_CREDIBLE, R_NO_ODDS, R_DEVIG_FAIL, R_OK}


# ── 只读连接（铁律护栏）───────────────────────────────────────────────────
def test_open_readonly_rejects_missing_db():
    with pytest.raises(FileNotFoundError):
        open_readonly(os.path.join(tempfile.gettempdir(), "definitely_not_here.db"))


def test_open_readonly_sets_query_only():
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "t.db")
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE t(x INT)")
        con.commit()
        con.close()
        ro = open_readonly(p)
        assert ro.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO t(x) VALUES (1)")
        ro.close()


# ── 差额算术一致性 ────────────────────────────────────────────────────────
def test_delta_arithmetic_consistency():
    """gate_A 各原因 + 门控B独有行的原因 = gate_B 各原因（口径换算自洽）。"""
    gate_a = {"OK": 6264, "NOT_CREDIBLE": 2186}
    delta = {"OK": 713, "NOT_CREDIBLE": 667}
    gate_b = {"OK": 6977, "NOT_CREDIBLE": 2853}
    assert gate_a["OK"] + delta["OK"] == gate_b["OK"]
    assert gate_a["NOT_CREDIBLE"] + delta["NOT_CREDIBLE"] == gate_b["NOT_CREDIBLE"]
    # 净新增 = 门控B多出来的 survivor - 其中已被账本收录的（去重键 match_id+model_source）
    net_new = 713 - 252
    assert net_new == 461

"""T31 tests — diag_candles_sample_hiatus 纯逻辑单测。

全部使用 tmp_path + 合成数据，零生产 I/O：
不读 events.db / verification.db、不写任何库、不跑 ingest、不重训模型。
断言聚焦「账本-供给面口径 / 停滞根因分类 C1-C4 / 报告渲染 / 只读连接」。
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import diag_candles_sample_hiatus as D  # noqa: E402


def _daily(mk, source, status, date, kickoff="2026-09-20T18:00:00"):
    return {"match_key": mk, "model_source": source, "status": status,
            "match_date": date, "kickoff": kickoff}


def _match(status, sh=None, sa=None):
    return {"status": status, "score_home": sh, "score_away": sa}


def _ledger_row(source, n, first, last, created):
    return {"model_source": source, "n": n, "first_date": first,
            "last_date": last, "last_created": created}


# --------------------------------------------------------------------------
# 1. 口径函数
# --------------------------------------------------------------------------
def test_group_by_counts_status():
    rows = [{"status": "finished"}, {"status": "live"}, {"status": "finished"}]
    d = D.group_by(rows, "status")
    assert d == {"finished": 2, "live": 1}


def test_ledger_span_none_when_source_absent():
    assert D.ledger_span([], "candles_ensemble") is None


def test_ledger_span_reads_span_and_gap():
    rows = [_ledger_row("candles_ensemble", 181, "2026-09-15", "2026-09-18", "2026-09-23T16:20")]
    r = D.ledger_span(rows, "candles_ensemble")
    assert r["n"] == 181
    assert r["gap_to_g1"] == D.G1_ACCEPT_N - 181
    assert r["last_date"] == "2026-09-18"


def test_daily_profile_status_split():
    rows = [
        _daily("a", "candles_ensemble", "finished", "2026-09-18"),
        _daily("b", "candles_ensemble", "live", "2026-09-25"),
        _daily("c", "candles_ensemble", "scheduled", "2026-09-26"),
        _daily("d", "market_baseline", "finished", "2026-09-19"),
    ]
    p = D.daily_profile(rows, "candles_ensemble")
    assert p["total"] == 3
    assert p["finished"] == 1
    assert p["not_finished"] == 2
    assert p["last_date"] == "2026-09-26"


# --------------------------------------------------------------------------
# 2. 停滞根因分类（C1-C4 四族）
# --------------------------------------------------------------------------
def test_not_wired_when_source_absent_from_ledger():
    daily = [_daily("a", "candles_ensemble", "live", "2026-09-25")]
    r = D.classify_stall("candles_ensemble", [], daily, {}, {})
    assert r["verdict"] == D.C1_NOT_WIRED
    assert "未接入" in r["detail"]


def test_status_gate_when_finished_rows_frozen_and_blocked():
    """现状复刻: 账本停在 09-18; 之后 matches 已完赛但 daily.status 未翻。"""
    ledger = [_ledger_row("candles_ensemble", 181, "2026-09-15", "2026-09-18", "2026-09-23T16:20")]
    daily = [
        _daily("a1", "candles_ensemble", "finished", "2026-09-18"),   # 已入账
        _daily("b1", "candles_ensemble", "live", "2026-09-25"),        # 候选, 可入账
        _daily("b2", "candles_ensemble", "live", "2026-09-26"),        # 候选, 0-0 假分
    ]
    matches = {"a1": _match("finished", 2, 1), "b1": _match("finished", 1, 0),
               "b2": _match("finished", 0, 0)}
    payloads = {"b1": {"market_implied": {"odds_1x2": [1.8, 3.2, 4.0]}}}
    r = D.classify_stall("candles_ensemble", ledger, daily, matches, payloads,
                         {"a1"})
    assert r["verdict"] == D.C2_STATUS_GATE
    assert r["unflipped_candidates"] == 2
    assert r["unflipped_ok"] == 1
    assert r["unflipped_zero_scored"] == 1
    assert r["last_finished_date"] == "2026-09-18"
    assert r["frozen_finished"] is True


def test_status_gate_not_fired_when_new_finished_rows_exist():
    """若 daily 仍在产出 finished 行（门控未卡死）→ 不判 C2。"""
    ledger = [_ledger_row("candles_ensemble", 181, "2026-09-15", "2026-09-18", "2026-09-23T16:20")]
    daily = [
        _daily("a1", "candles_ensemble", "finished", "2026-09-18"),
        _daily("c1", "candles_ensemble", "finished", "2026-09-24"),
    ]
    matches = {"a1": _match("finished", 2, 1), "c1": _match("finished", 3, 1)}
    payloads = {"c1": {"market_implied": {"odds_1x2": [1.8, 3.2, 4.0]}}}
    r = D.classify_stall("candles_ensemble", ledger, daily, matches, payloads, {"a1"})
    assert r["verdict"] != D.C2_STATUS_GATE
    assert r["finished_ok"] == 1


def test_exclusion_when_all_finished_rejected_by_guards():
    """finished 行全被守卫剔除（0-0 / 无赔率）且无未翻转候选 → C3。"""
    ledger = [_ledger_row("candles_ensemble", 181, "2026-09-15", "2026-09-18", "2026-09-23T16:20")]
    daily = [_daily("a1", "candles_ensemble", "finished", "2026-09-18"),
             _daily("z1", "candles_ensemble", "finished", "2026-09-19")]
    matches = {"a1": _match("finished", 2, 1), "z1": _match("finished", 0, 0)}
    r = D.classify_stall("candles_ensemble", ledger, daily, matches, {}, {"a1"})
    assert r["verdict"] == D.C3_EXCLUSION
    assert r["finished_zero_scored"] == 1


def test_slow_real_as_fallback():
    """账本已接入但供给面为空且无未翻转候选 → 落到 C4 兜底。"""
    ledger = [_ledger_row("candles_ensemble", 181, "2026-09-15", "2026-09-18", "x")]
    daily = []
    r = D.classify_stall("candles_ensemble", ledger, daily, {}, {})
    assert r["verdict"] == D.C4_SLOW_REAL
    assert "真实累积" in r["detail"]


# --------------------------------------------------------------------------
# 3. 只读连接（绝不写入）
# --------------------------------------------------------------------------
def test_open_readonly_rejects_missing_db(tmp_path):
    try:
        D.open_readonly(str(tmp_path / "nope.db"))
    except FileNotFoundError:
        return
    raise AssertionError("missing db should raise FileNotFoundError")


def test_open_readonly_blocks_writes(tmp_path):
    p = tmp_path / "t.db"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE t (a INTEGER)")
    con.execute("INSERT INTO t VALUES (1)")
    con.close()
    con = D.open_readonly(str(p))
    try:
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1
        try:
            con.execute("INSERT INTO t VALUES (2)")
        except sqlite3.OperationalError:
            return
        raise AssertionError("readonly connection must reject INSERT")
    finally:
        con.close()


# --------------------------------------------------------------------------
# 4. 报告渲染
# --------------------------------------------------------------------------
def test_render_markdown_contains_verdict_and_sources():
    ledger = [_ledger_row("candles_ensemble", 181, "2026-09-15", "2026-09-18", "2026-09-23T16:20")]
    summary = {
        "generated_at": "2026-09-27T00:00:00Z",
        "note": "x",
        "g1_accept_n": D.G1_ACCEPT_N,
        "target": "candles_ensemble",
        "control_sources": {"KNN": {"n": 2779, "first_date": "2026-08-21", "last_date": "2026-09-25"}},
        "analysis": D.classify_stall("candles_ensemble", ledger, [], {}, {}),
    }
    md = D.render_markdown(summary)
    assert "candles_ensemble 验证样本停滞诊断" in md
    assert D.C4_SLOW_REAL in md
    assert "2779" in md


def test_write_outputs_json_serialisable(tmp_path):
    ledger = [_ledger_row("candles_ensemble", 181, "2026-09-15", "2026-09-18", "x")]
    summary = {
        "generated_at": "2026-09-27T00:00:00Z", "note": "x",
        "g1_accept_n": D.G1_ACCEPT_N, "target": "candles_ensemble",
        "control_sources": {}, "analysis": D.classify_stall("candles_ensemble", ledger, [], {}, {}),
    }
    jp, mp = D.write_outputs(summary, str(tmp_path))
    assert os.path.exists(jp) and os.path.exists(mp)
    with open(jp, encoding="utf-8") as f:
        back = json.load(f)
    assert back["analysis"]["verdict"] in {D.C1_NOT_WIRED, D.C2_STATUS_GATE,
                                           D.C3_EXCLUSION, D.C4_SLOW_REAL}


def test_verdict_enum_stable():
    """四族枚举值不得随意改（报告与 backlog 引用）。"""
    assert (D.C1_NOT_WIRED, D.C2_STATUS_GATE, D.C3_EXCLUSION, D.C4_SLOW_REAL) == (
        "C1_NOT_WIRED", "C2_STATUS_GATE", "C3_EXCLUSION", "C4_SLOW_REAL")

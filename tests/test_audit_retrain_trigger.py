# -*- coding: utf-8 -*-
"""T36 retrain_gate 触发链路只读审计 — 测试（零生产 I/O）。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
import audit_retrain_trigger as art  # noqa: E402

MONITOR_SRC = "\n".join([
    "LOG = os.path.join(ROOT, 'logs', 'autonomous_monitor.log')",
    "TRAIN_MARKER = os.path.join(ROOT, 'reports', 'last_candles_train_marker.json')",
    "",
    "def calibration_check():",
    "    drift = {'ll_delta_vs_market': delta,",
    "             'drift_alert': bool(delta > 0.03)}",
    "    return {'report_age_hours': 1.0, 'rerun': False, 'drift': drift}",
    "",
    "def _last_train_ts():",
    "    with open(TRAIN_MARKER, encoding='utf-8') as f:",
    "        return json.load(f)['ts']",
    "",
    "def retrain_gate():",
    "    con = sqlite3.connect('file:...?mode=ro', uri=True)",
    "    n_new = con.execute(",
    "        'SELECT COUNT(*) FROM prematch_candles_verdict WHERE captured_at > COALESCE(?, 0)',",
    "        (_last_train_ts(),)).fetchone()[0]",
    "    con.close()",
    "    return {'new_verdicts_since_train': n_new,",
    "            'suggest': n_new >= 100}",
    "",
])

HISTORY_TXT = "\n".join([
    json.dumps({"cycle_at": "2026-09-24 09:54:35",
                "calibration": {"drift": {"ll_delta_vs_market": 0.02435}},
                "retrain_gate": {"new_verdicts_since_train": 180, "suggest": True}}),
    json.dumps({"cycle_at": "2026-09-24 10:04:35",
                "calibration": {"drift": {"ll_delta_vs_market": 0.02800}},
                "retrain_gate": {"new_verdicts_since_train": 181, "suggest": True}}),
    json.dumps({"cycle_at": "2026-09-24 10:14:35",
                "calibration": {"drift": {"ll_delta_vs_market": 0.03231}},
                "retrain_gate": {"new_verdicts_since_train": 193, "suggest": True}}),
])


# --------------------------------------------------------------- 阈值解析
def test_extract_gate_spec_thresholds():
    s = art.extract_gate_spec(MONITOR_SRC)
    assert s["threshold_new_verdicts"] == 100
    assert s["threshold_syntax"] == "n_new >= 100"
    assert s["drift_threshold"] == 0.03
    assert s["gauge_table"] == "prematch_candles_verdict"
    assert s["marker_key"] == "ts"


def test_extract_gate_spec_drift_not_wired():
    """核心事实：drift 不在门控函数体内，故不可能翻转 suggest。"""
    s = art.extract_gate_spec(MONITOR_SRC)
    assert s["drift_wired_into_gate"] is False


def test_extract_gate_spec_drift_wired_variant():
    src = MONITOR_SRC.replace("'suggest': n_new >= 100}",
                              "'suggest': (n_new >= 100) or (ll_delta > 0.03)}")
    assert art.extract_gate_spec(src)["drift_wired_into_gate"] is True


def test_def_block_boundary():
    """函数体不得吞掉下一个 def（否则漂移误判为门控内引用）。"""
    s = art.extract_gate_spec(MONITOR_SRC)
    assert "def calibration_check" not in s["gauge_sql"]


def test_extract_gate_spec_missing_function():
    assert art.extract_gate_spec("x = 1")["threshold_new_verdicts"] is None


# --------------------------------------------------------------- 消费方
def test_classify_layers():
    assert art._classify("bridge_service.py", "open(x)")["layer"] == "backend"
    assert art._classify("frontend/src/a.ts", "x")["layer"] == "frontend"
    assert art._classify("scripts/audit.py", "x")["layer"] == "automation_or_test"
    assert art._classify("docs/P.md", "x")["layer"] == "doc"


def test_summarize_consumers_runtime_layer():
    hits = [{"path": "scripts/audit.py", "line": 1, "layer": "automation_or_test",
             "access": "consumer"},
            {"path": "frontend/src/a.tsx", "line": 2, "layer": "frontend",
             "access": "consumer"},
            {"path": "docs/P.md", "line": 3, "layer": "doc", "access": "unknown"}]
    s = art.summarize_consumers(hits)
    assert s["runtime_layer_hits"] == 1
    assert s["by_layer"]["frontend"] == 1


def test_summarize_consumers_dedup():
    hits = [{"path": "a.py", "line": 1, "layer": "backend", "access": "consumer"}] * 3
    assert art.summarize_consumers(hits)["total_hits"] == 1


def test_scan_needle_respects_skip_dirs(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("retrain_gate", encoding="utf-8")
    (tmp_path / "scripts" / "y.py").write_text("# retrain_gate\n", encoding="utf-8")
    hits = art.scan_needle("retrain_gate", root=str(tmp_path))
    assert [h["path"] for h in hits] == ["scripts/y.py"]


def test_self_exclusion(tmp_path):
    """本脚本自身引用不计入消费方/写方（与 T33 同坑：自指假阳性）。"""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "audit_retrain_trigger.py").write_text(
        "MARKER = 'last_candles_train_marker.json'\n", encoding="utf-8")
    (tmp_path / "scripts" / "other.py").write_text(
        "# retrain_gate consumer\nTRAIN_MARKER = 'last_candles_train_marker.json'\n",
        encoding="utf-8")
    assert art.find_marker_writers(root=str(tmp_path))["has_writer"] is False
    hits = art.scan_needle("retrain_gate", root=str(tmp_path))
    assert [h["path"] for h in hits] == ["scripts/other.py"]


def test_suggest_timeline_current_streak_start(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text("\n".join([
        json.dumps({"cycle_at": "A", "retrain_gate": {"suggest": True,
                                                      "new_verdicts_since_train": 431}}),
        json.dumps({"cycle_at": "B", "retrain_gate": {"suggest": False,
                                                      "new_verdicts_since_train": 1}}),
        json.dumps({"cycle_at": "C", "retrain_gate": {"suggest": True,
                                                      "new_verdicts_since_train": 180}}),
        json.dumps({"cycle_at": "D", "retrain_gate": {"suggest": True,
                                                      "new_verdicts_since_train": 181}}),
    ]), encoding="utf-8")
    tl = art.suggest_timeline(str(p))
    assert tl["current_streak_start"] == "C"
    assert tl["first_true"] == "A"


def test_find_marker_writers_writers(tmp_path):
    (tmp_path / "s.py").write_text(
        "MARK = 'last_candles_train_marker.json'\n"
        "json.dump({'ts': 1}, open(MARK, 'w'))\n", encoding="utf-8")
    r = art.find_marker_writers(root=str(tmp_path))
    # 写方行不直接含 marker 名（间接引用 MARK），故写方仍归 0 —— 守卫按字面匹配，属已知边界
    assert r["has_writer"] is False
    assert len(r["writers"]) == 0


def test_find_marker_writers_literal_writer(tmp_path):
    (tmp_path / "s.py").write_text(
        "with open('reports/last_candles_train_marker.json', 'w') as f:\n"
        "    json.dump({'ts': 1}, f)\n", encoding="utf-8")
    assert art.find_marker_writers(root=str(tmp_path))["has_writer"] is True


def test_find_marker_writers_readers_only(tmp_path):
    (tmp_path / "s.py").write_text(
        "TRAIN_MARKER = os.path.join(ROOT, 'reports', 'last_candles_train_marker.json')\n"
        "json.load(open(TRAIN_MARKER))\n", encoding="utf-8")
    r = art.find_marker_writers(root=str(tmp_path))
    assert r["has_writer"] is False
    # 只有字面含 marker 名的那一行被计入（间接引用 TRAIN_MARKER 不计）
    assert len(r["reader_or_defs"]) == 1


# --------------------------------------------------------------- 时序
def test_suggest_timeline_flips(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text(HISTORY_TXT, encoding="utf-8")
    tl = art.suggest_timeline(str(p))
    assert tl["n_cycles"] == 3
    assert tl["flips"] == []  # 首条即 True，不算翻转（无 prev）
    assert tl["first_true"] == "2026-09-24 09:54:35"


def test_suggest_timeline_flip_detection(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text("\n".join([
        json.dumps({"cycle_at": "A", "retrain_gate": {"suggest": False,
                                                      "new_verdicts_since_train": 5}}),
        json.dumps({"cycle_at": "B", "retrain_gate": {"suggest": True,
                                                      "new_verdicts_since_train": 101}}),
        json.dumps({"cycle_at": "C", "retrain_gate": {"suggest": False,
                                                      "new_verdicts_since_train": 0}}),
    ]), encoding="utf-8")
    tl = art.suggest_timeline(str(p))
    assert [f["cycle_at"] for f in tl["flips"]] == ["B", "C"]
    assert tl["flips"][0]["new_verdicts_at_flip"] == 101


def test_suggest_timeline_missing_file(tmp_path):
    tl = art.suggest_timeline(str(tmp_path / "nope.jsonl"))
    assert tl["n_cycles"] == 0 and tl["first_true"] is None


# --------------------------------------------------------------- 解耦
def test_decoupling_metrics_decoupled():
    c = {"verdicts_since_train": 596}
    l = {"new_rows_after_last_match_date": 0, "ledger_rows": 181, "gap_to_g1": 2319}
    d = art.decoupling_metrics(c, l)
    assert d["decoupled"] is True
    assert d["gap_to_g1"] == 2319


def test_decoupling_metrics_sync():
    c = {"verdicts_since_train": 10}
    l = {"new_rows_after_last_match_date": 7}
    assert art.decoupling_metrics(c, l)["decoupled"] is False


def test_read_ledger_counters_readonly_db(tmp_path):
    import sqlite3
    db = tmp_path / "v.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE verification_ledger (row_id INTEGER, model_source TEXT, "
                "match_date TEXT, created_at TEXT)")
    con.executemany("INSERT INTO verification_ledger VALUES (?,?,?,?)",
                    [(1, "candles_ensemble", "2026-09-17", "2026-09-23T16:20:45Z"),
                     (2, "candles_ensemble", "2026-09-18", "2026-09-23T16:20:45Z"),
                     (3, "candles_ensemble", "2026-09-25", "2026-09-25T01:00:00Z")])
    con.commit()
    con.close()
    old = art.LEDGER_DB
    art.LEDGER_DB = str(db)
    try:
        r = art.read_ledger_counters()
        assert r["ledger_rows"] == 3
        assert r["ledger_max_match_date"] == "2026-09-25"
        assert r["new_rows_after_last_match_date"] == 0
        assert r["gap_to_g1"] == pytest.approx(2500 - 3)
    finally:
        art.LEDGER_DB = old


def test_read_ledger_counters_stalled_ledger(tmp_path):
    import sqlite3
    db = tmp_path / "v2.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE verification_ledger (row_id INTEGER, model_source TEXT, "
                "match_date TEXT, created_at TEXT)")
    con.executemany("INSERT INTO verification_ledger VALUES (?,?,?,?)",
                    [(1, "candles_ensemble", "2026-09-18", "x"),
                     (2, "candles_ensemble", "2026-09-22", "y"),
                     (3, "candles_ensemble", "2026-09-25", "z")])
    con.commit()
    con.close()
    old = art.LEDGER_DB
    art.LEDGER_DB = str(db)
    try:
        r = art.read_ledger_counters()
        assert r["new_rows_after_last_match_date"] == 0  # 最新样本之后零增长=停滞
        assert r["ledger_max_match_date"] == "2026-09-25"
    finally:
        art.LEDGER_DB = old


# --------------------------------------------------------------- 发现与报告
def test_build_findings_核心三条():
    spec = {"gate_line": 158, "threshold_new_verdicts": 100, "drift_threshold": 0.03,
            "drift_wired_into_gate": False}
    cons = {"runtime_layer_hits": 0}
    marker = {"has_writer": False, "reader_or_defs": [1], "writers": []}
    tl = {"first_true": "2026-09-24 09:54:35", "n_cycles": 85, "flips": [1]}
    deci = {"verdicts_since_train": 596, "new_ledger_rows_after_last_sample": 0,
            "gap_to_g1": 2319, "decoupled": True}
    led = {"ledger_max_match_date": "2026-09-18"}
    ids = {f["id"] for f in art.build_findings(spec, cons, marker, tl, deci, led)}
    assert ids == {"F1", "F2", "F3", "F4", "F5"}
    sev = {f["id"]: f["severity"] for f in art.build_findings(spec, cons, marker, tl, deci, led)}
    assert sev["F3"] == "critical"


def test_build_report_recommendation_says_no_retrain():
    spec = {"gate_line": 158, "threshold_new_verdicts": 100, "drift_threshold": 0.03,
            "drift_wired_into_gate": False}
    rep = art.build_report(spec, {"runtime_layer_hits": 0}, {"has_writer": False,
                                                             "reader_or_defs": [],
                                                             "writers": []},
                           {"first_true": None, "n_cycles": 0, "flips": []}, {},
                           {"ledger_max_match_date": "2026-09-18"},
                           {"verdicts_since_train": 0,
                            "new_ledger_rows_after_last_sample": 0, "gap_to_g1": 2319})
    assert "不重训" in rep["recommendation"]
    assert rep["redlines"][0].startswith("只读")


def test_render_md_contains_all_sections():
    spec = {"gate_line": 158, "threshold_new_verdicts": 100, "drift_threshold": 0.03,
            "drift_wired_into_gate": False, "gauge_table": "prematch_candles_verdict",
            "threshold_syntax": "n_new >= 100", "drift_threshold_str": "0.03",
            "gauge_sql": "SELECT COUNT(*) FROM prematch_candles_verdict"}
    rep = art.build_report(spec, {"runtime_layer_hits": 0, "total_hits": 3,
                                  "by_layer": {"doc": 3}, "by_access": {"consumer": 3},
                                  "runtime_consumers": []},
                           {"has_writer": False, "marker": "m", "reader_or_defs": [1],
                            "writers": []},
                           {"first_true": "T", "n_cycles": 85, "flips": [],
                            "last": {"cycle_at": "Z"}},
                           {"marker_ts": 1.0, "marker_mtime": "2026-09-19 04:34:56",
                            "verdict_total": 1026, "verdicts_since_train": 596},
                           {"ledger_rows": 181, "ledger_max_match_date": "2026-09-18",
                            "new_rows_after_last_match_date": 0},
                           {"verdicts_since_train": 596,
                            "new_ledger_rows_after_last_sample": 0, "gap_to_g1": 2319,
                            "decoupled": True})
    md = art.render_md(rep)
    for sec in ("## 1 触发条件", "## 2 消费方", "## 3 marker", "## 4 suggest 翻转时序",
                "## 5 门控计数 vs 账本入账", "## 6 发现", "## 7 建议"):
        assert sec in md
    assert "prematch_candles_verdict" in md

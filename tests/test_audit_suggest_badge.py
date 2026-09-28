# -*- coding: utf-8 -*-
"""T38 审计脚本只读盘点 retrain_gate.suggest 的引用污染面。

纯单元/离线测试：不碰 events.db、不重训、不改生产文件、不写生产 reports
（输出目录用 tmp_path 重定向）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import audit_suggest_badge as A  # noqa: E402


# ---------------------------------------------------------------------------
# 引用行分类
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("line,dates,dur", [
    ("- 当前 `retrain_gate.suggest=True`", False, False),
    ("| monitor retrain_gate.suggest | True |", False, False),
    ("前提：开展一次重训（当前 monitor drift_alert=True, retrain_gate.suggest=True）",
     False, False),
    ("唯一持久告警仍为 `retrain_gate.suggest=true`（慢性状态，非突发）", False, True),
    ("suggest 自 2026-09-24 起连续 True（慢性状态，非突发告警）", True, True),
    ("记录于 2026-09-27 的快照：retrain_gate.suggest=True", True, False),
])
def test_classify_ref_date_duration(line, dates, dur):
    c = A._classify_ref(line)
    assert c["has_date"] is dates
    assert c["has_duration"] is dur


def test_prescriptive_and_present_tense():
    c = A._classify_ref("前提：开展一次重训（当前 retrain_gate.suggest=True）")
    assert c["prescriptive"] is True
    assert c["present_tense"] is True
    c2 = A._classify_ref("retrain_gate 计数阈值解析")
    assert c2["prescriptive"] is False or c2["present_tense"] is False


def test_risk_tiers():
    assert A._risk(A._classify_ref("前提：开展一次重训（当前 suggest=True）")) == "HIGH"
    assert A._risk(A._classify_ref("当前 `retrain_gate.suggest=True`")) == "MEDIUM"
    assert A._risk(A._classify_ref("自 2026-09-24 起 suggest=True，已持续 3 天")) == "OK"
    assert A._risk(A._classify_ref("retrain_gate.suggest=True")) == "LOW"


# ---------------------------------------------------------------------------
# 相关性过滤（必须锚定 retrain 上下文，避免抓到无关 "suggest"）
# ---------------------------------------------------------------------------
def test_relevance_filter():
    assert A._relevant("当前 retrain_gate.suggest=True") is True
    assert A._relevant("T36：marker 无写方，suggest 永不回落（重训相关）") is True
    assert A._relevant("他建议我们换个思路") is False
    assert A._relevant("drift_alert=True, ll_delta=0.0325，suggest 为 True") is True


def test_source_kind_and_layer(tmp_path):
    assert A._source_kind("reports/monitor_status.json") == "LIVE_FILE"
    assert A._source_kind("reports/stale_reports_audit.md") == "FROZEN_REPORT"
    assert A._source_kind(".workbuddy/memory/2026-09-27.md") == "MEMORY_NARRATIVE"
    assert A._layer("reports/x.json") == "report"
    assert A._layer(".workbuddy/memory/x.md") == "agent_memory"


def test_self_exclusion():
    assert A._is_self("scripts/audit_suggest_badge.py") is True
    assert A._is_self("tests/test_audit_suggest_badge.py") is True
    assert A._is_self("reports/other_audit.json") is False


# ---------------------------------------------------------------------------
# 真值解析（只读 monitor_history.jsonl）
# ---------------------------------------------------------------------------
def test_suggest_truth_streak(tmp_path):
    p = tmp_path / "monitor_history.jsonl"
    rows = [
        {"cycle_at": "2026-09-24T10:00:00", "retrain_gate": {"suggest": False}},
        {"cycle_at": "2026-09-24T11:00:00", "retrain_gate": {"suggest": True}},
        {"cycle_at": "2026-09-25T11:00:00", "retrain_gate": {"suggest": True}},
        {"cycle_at": "2026-09-26T11:00:00", "retrain_gate": {"suggest": True}},
        {"cycle_at": "2026-09-27T11:00:00", "retrain_gate": {"suggest": True}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    t = A.suggest_truth(str(p))
    assert t["n_cycles"] == 5
    assert t["flips"] == 1
    assert t["streak_start"] == "2026-09-24T11:00:00"
    assert t["first_true"] == "2026-09-24T11:00:00"
    assert t["last_cycle_at"] == "2026-09-27T11:00:00"


def test_duration_days_uses_streak_start():
    truth = {"streak_start": "2026-09-24T11:00:00", "first_true": None}
    as_of = datetime(2026, 9, 27, 11, 0, 0)
    assert A.duration_days(truth, as_of) == pytest.approx(3.0)


def test_read_live_status_missing(tmp_path):
    out = A.read_live_status(str(tmp_path / "nope.json"))
    assert out["exists"] is False
    assert out["retrain_gate"] is None


def test_read_live_status_ok(tmp_path):
    p = tmp_path / "monitor_status.json"
    p.write_text(json.dumps({"retrain_gate": {"suggest": True}}), encoding="utf-8")
    out = A.read_live_status(str(p))
    assert out["exists"] is True
    assert out["retrain_gate"]["suggest"] is True
    assert out["mtime"] is not None


# ---------------------------------------------------------------------------
# 扫描（离线，指向临时目录）
# ---------------------------------------------------------------------------
def _write_tree(tmp_path):
    r = tmp_path / "reports"
    r.mkdir()
    (r / "monitor_status.json").write_text(
        json.dumps({"retrain_gate": {"suggest": True}}), encoding="utf-8")
    (r / "plan.md").write_text(
        "- 前提：开展一次重训（当前 `retrain_gate.suggest=True`）\n"
        "- 若补入区间跨模型迭代（当前 `retrain_gate.suggest=True`，自 2026-09-24 起持续 3 天）\n",
        encoding="utf-8")
    (r / "note.txt").write_text("ignored ext", encoding="utf-8")
    m = tmp_path / ".workbuddy" / "memory"
    m.mkdir(parents=True)
    (m / "2026-09-27.md").write_text(
        "- 本轮不重训；唯一告警 `retrain_gate.suggest=true`\n", encoding="utf-8")


def test_scan_refs_offline(tmp_path, monkeypatch):
    _write_tree(tmp_path)
    monkeypatch.setattr(A, "ROOT", str(tmp_path))
    monkeypatch.setattr(A, "REPORTS", str(tmp_path / "reports"))
    monkeypatch.setattr(A, "MEMDIR", str(tmp_path / ".workbuddy" / "memory"))
    refs, derived = A.scan_refs(include_docs=False)
    by_path = {(r["path"], r["line"]): r for r in refs}
    # 活文件被识别
    assert ("reports/monitor_status.json", 1) in by_path
    assert by_path[("reports/monitor_status.json", 1)]["source_kind"] == "LIVE_FILE"
    # 命令式 + 现在时且无时长 = HIGH
    assert by_path[("reports/plan.md", 1)]["risk"] == "HIGH"
    assert by_path[("reports/plan.md", 1)]["prescriptive"] is True
    # 同文件第二行有日期+时长 = OK
    assert by_path[("reports/plan.md", 2)]["risk"] == "OK"
    # memory 叙述层
    assert ("reports/../" + ".workbuddy/memory/2026-09-27.md", 1) in by_path or \
        any(r["layer"] == "agent_memory" for r in refs)
    # 非扫描扩展名的文件不参与
    assert not any(r["path"].endswith("note.txt") for r in refs)


def test_report_and_render(tmp_path, monkeypatch):
    _write_tree(tmp_path)
    monkeypatch.setattr(A, "ROOT", str(tmp_path))
    monkeypatch.setattr(A, "REPORTS", str(tmp_path / "reports"))
    refs, derived = A.scan_refs(include_docs=False)
    truth = {"streak_start": "2026-09-24T11:00:00", "n_cycles": 85, "flips": 2,
             "first_true": "2026-09-24T11:00:00", "last_cycle_at": "2026-09-27T00:00:00"}
    live = {"file": "reports/monitor_status.json", "mtime": "2026-09-27 08:00:00",
            "retrain_gate": {"suggest": True}}
    rep = A.build_report(refs, derived, truth, live, False)
    assert rep["summary"]["total"] == len(refs)
    assert any(f["id"] == "F1" for f in rep["findings"])
    assert any(f["id"] == "F2" for f in rep["findings"])
    assert any(f["id"] == "F3" for f in rep["findings"])
    md = A.render_md(rep)
    assert "T38" in md and "suggest" in md


def test_redlines_are_declared():
    rep = A.build_report([], [], {}, {}, False)
    assert rep["redlines"]
    assert "events.db" in "".join(rep["redlines"])

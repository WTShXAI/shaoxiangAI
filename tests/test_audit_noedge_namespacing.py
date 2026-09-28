#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T58 信号词 ``NO_EDGE`` 命名隔离审计的守卫用例（承接 T51 §2 / T53 §2 / T57）。

覆盖四组:
  A 词族边界      —— 命中的是「信号拼写」, 不吃 ``MY_NO_EDGE`` / ``NO_OVEREDGE``。
  B 同文件撞车    —— 用 SSoT 词表判三态行时必须**丢掉信号拼写**, 否则每行都假阳性。
  C 改名成本      —— 持久化工件/间接写入方/字符串比较站点, 三类成本面各自可判定。
  D 守卫 fail-closed —— 「没查」不能等于「通过」; 未登记命中必须 RED。

零生产 I/O: 活体用例只读仓库静态文件, 结构性用例走 ``tmp_path``。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import audit_noedge_namespacing as A  # noqa: E402


# --------------------------------------------------------------------------
# A 词族边界
# --------------------------------------------------------------------------

def test_signal_token_matches_the_underscore_spelling():
    assert A.RE_SIGNAL_TOKEN.search("signal = 'NO_EDGE'")


def test_signal_token_does_not_match_word_internals():
    """词边界必须把下划线算作词内字符 —— 否则 ``MY_NO_EDGE`` 会被误判成信号词。"""
    for ghost in ("MY_NO_EDGE", "NO_EDGE_X", "NO_OVEREDGE"):
        assert not A.RE_SIGNAL_TOKEN.search(ghost), ghost


def test_verdict_lines_discard_signal_spelling():
    """本轮实测踩到的坑: SSoT 词表同时含两种拼写, 直接拿来判断同文件撞车会把
    每一行信号行都算成三态行 (live_goal_probe.py 一次性 18 行假阳性)。"""
    text = "signal = 'NO_EDGE'\nverdict = 'NO EDGE'\nfoo = 1\n"
    assert A.verdict_lines_of(text) == [2]


def test_verdict_lines_of_live_probe_has_no_signal_spelling_pollution():
    """活体反证: live_goal_probe.py 的信号行不得出现在三态行清单里。"""
    src = A.read_text(os.path.join(ROOT, "analysis", "live_goal_probe.py")) or ""
    verdicts = A.verdict_lines_of(src)
    signal_lines = [i for i, ln in enumerate(src.splitlines(), 1)
                    if A.RE_SIGNAL_TOKEN.search(ln)]
    overlap = sorted(set(verdicts) & set(signal_lines))
    assert not overlap, f"三态行判定被信号拼写污染: {overlap[:5]}"


def test_layer_of_maps_each_layer():
    assert A.layer_of("analysis/live_goal_probe.py") == A.LAYER_ANALYSIS
    assert A.layer_of("scripts/backtest_ou_signal.py") == A.LAYER_SCRIPTS
    assert A.layer_of("bridge_service.py") == A.LAYER_BRIDGE
    assert A.layer_of("frontend/src/pages/Rollball/index.tsx") == A.LAYER_FRONTEND
    assert A.layer_of("tests/test_x.py") == A.LAYER_TESTS


# --------------------------------------------------------------------------
# B 登记册 / 自避 / 同文件撞车
# --------------------------------------------------------------------------

def test_registry_reasons_are_all_non_empty():
    """空理由不许登记 (沿用 T57 纪律: 防豁免清单静默长大)。"""
    empty = [p for p, r in A.SIGNAL_FILE_REGISTRY.items() if not str(r).strip()]
    assert not empty, empty


def test_self_exclude_covers_script_and_its_test():
    names = A.self_exclude()
    own = os.path.basename(A.__file__)
    assert own in names
    assert "test_" + own in names


def test_live_registry_covers_the_dead_code_candidate():
    """死代码候选必须已登记, 否则 T51/T53 两个守卫会把它判成未登记命中。"""
    assert "scripts/backtest_ou_signal.py" in A.SIGNAL_FILE_REGISTRY
    assert A.classify_signal_file("scripts/backtest_ou_signal.py")


def test_unregistered_hit_is_reported(tmp_path):
    """未登记的信号命中必须进 unregistered (否则守卫失去意义)。"""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "mystery.py").write_text("signal = 'NO_EDGE'\n", encoding="utf-8")
    res = A.scan_signal_sources(str(repo))
    assert res["unregistered"], "未登记文件没被报出来, 守卫形同虚设"
    assert res["unregistered"][0]["file"] == "scripts/mystery.py"


def test_registered_hit_is_not_reported_as_unregistered(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "mystery.py").write_text("signal = 'NO_EDGE'\n", encoding="utf-8")
    A.SIGNAL_FILE_REGISTRY["scripts/mystery.py"] = "测试用登记 (用例临时写入)"
    try:
        res = A.scan_signal_sources(str(repo))
        assert res["unregistered"] == []
        assert res["total_hits"] == 1
    finally:
        A.SIGNAL_FILE_REGISTRY.pop("scripts/mystery.py", None)


def test_same_file_collision_detects_mixed_file(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "mixed.py").write_text(
        "signal = 'NO_EDGE'\nverdict = 'EDGE'\n", encoding="utf-8")
    got = A.scan_same_file_collision(str(repo))
    assert [g["file"] for g in got] == ["scripts/mixed.py"]
    assert got[0]["verdict_lines"] == [2]
    assert got[0]["registered"] is False


def test_live_collision_files_are_all_registered():
    """活体不变式: 今天同文件撞车只发生在审计/SSoT/测试面, 且都已登记。"""
    got = A.scan_same_file_collision()
    assert got, "活体扫描什么都没扫到 → 先怀疑假零 (同 T52 失效模式)"
    assert all(g["registered"] for g in got), \
        f"未登记的同文件撞车位: {[g['file'] for g in got if not g['registered']]}"


def test_live_live_layer_counts_non_empty():
    res = A.scan_signal_sources()
    assert res["total_hits"] > 0
    assert A.LAYER_ANALYSIS in res["by_layer"]
    assert res["by_layer"][A.LAYER_ANALYSIS] > 0


# --------------------------------------------------------------------------
# C 改名成本面
# --------------------------------------------------------------------------

def test_scan_callers_finds_real_importer(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "consumer.py").write_text(
        "from scripts.backtest_ou_signal import backend_signal\n", encoding="utf-8")
    got = A.scan_callers(root=str(repo))
    assert [g["file"] for g in got] == ["scripts/consumer.py"]


def test_dead_code_candidate_has_zero_callers_live():
    """T58 的核心机械事实: 改名收益最大的那个文件零调用方。"""
    got = A.scan_callers()
    assert got == [], f"{A.DEAD_CODE_CANDIDATE_MODULE} 竟然有引用方: {got}"
    assert os.path.exists(
        os.path.join(ROOT, "scripts", A.DEAD_CODE_CANDIDATE_MODULE + ".py"))


def test_signal_comparison_sites_detected(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "svc.py").write_text(
        "line = 1\nif ou.get('signal') == 'NO_EDGE':\n    pass\n", encoding="utf-8")
    got = A.scan_signal_comparison_sites(str(repo))
    assert [g["line"] for g in got] == [2]
    assert got[0]["layer"] == A.LAYER_SCRIPTS


def test_live_comparison_sites_cover_bridge_and_analysis():
    """字符串比较面必须覆盖后端消费点 (改名的静默失效点全在这里)。"""
    got = A.scan_signal_comparison_sites()
    layers = A._count_by(got, "layer")
    assert A.LAYER_BRIDGE in layers, f"后端比较面漏检: {layers}"
    assert A.LAYER_ANALYSIS in layers


def test_persist_scan_excludes_own_output(tmp_path, monkeypatch):
    """自污染面: 本脚本刚写出的报告自带信号词, 下一轮扫描必须排除它。"""
    repo = tmp_path / "repo"
    (repo / "reports").mkdir(parents=True)
    (repo / "reports" / "zz_self.json").write_text('{"a": "NO_EDGE"}\n', encoding="utf-8")
    (repo / "reports" / "zz_other.json").write_text('{"a": "NO_EDGE"}\n', encoding="utf-8")
    monkeypatch.setattr(A, "AUDIT_OWN_OUTPUT", "reports/zz_self.json")
    res = A.scan_persisted_artifacts(str(repo))
    names = [t["file"] for t in res["files_with_token"]]
    assert "reports/zz_self.json" not in names
    assert "reports/zz_other.json" in names


def test_persist_scan_separates_business_from_self_quote(tmp_path):
    repo = tmp_path / "repo"
    (repo / "reports").mkdir(parents=True)
    (repo / "data").mkdir(parents=True)
    (repo / "reports" / "one_audit.json").write_text('{"a": "NO_EDGE"}\n', encoding="utf-8")
    (repo / "data" / "watch.json").write_text('{"a": "NO_EDGE"}\n', encoding="utf-8")
    res = A.scan_persisted_artifacts(str(repo))
    kinds = {t["file"]: t["kind"] for t in res["files_with_token"]}
    assert kinds.get("data/watch.json") == "business"
    assert kinds.get("reports/one_audit.json") == "self_quote"


def test_live_business_artifact_carries_the_token():
    """本轮唯一 RED 的实证来源: 业务快照确实把这个词写进了磁盘。"""
    res = A.scan_persisted_artifacts()
    assert res["business_count"] >= 1
    names = [t["file"] for t in res["business_files"]]
    assert any(A.WATCH_TARGET in n for n in names), names


def test_indirect_persister_detected(tmp_path):
    """间接写入方: 不含字面量, 但把上游值原样落盘 (按文件级判 writes, 行级会漏)。"""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "w.py").write_text(
        "WATCH = r'D:\\x\\data\\watch_verdicts.json'\n"
        "def save(d):\n    json.dump(d, open(WATCH, 'w'))\n", encoding="utf-8")
    (repo / "scripts" / "r.py").write_text(
        "WATCH = r'D:\\x\\data\\watch_verdicts.json'\nprint(1)\n", encoding="utf-8")
    got = A.scan_indirect_persisters(str(repo))
    by_file = {g["file"]: g["writes"] for g in got}
    assert by_file["scripts/w.py"] is True
    assert by_file["scripts/r.py"] is False


def test_rename_cost_rows_cover_live_files():
    rows = A.rename_cost_table(A.scan_signal_sources())
    files = {r["file"] for r in rows}
    assert "analysis/live_goal_probe.py" in files
    assert sum(r["literal_count"] for r in rows) == A.scan_signal_sources()["total_hits"]


# --------------------------------------------------------------------------
# D 结论合成 fail-closed
# --------------------------------------------------------------------------

def test_build_findings_fails_when_scan_not_run():
    """「这项没查」必须算 FAIL (沿用 T52 静默空结果教训)。"""
    out = A.build_findings({}, [], [], [], {}, [])
    assert "R3_SIGNAL_SCAN_NOT_RUN" in out["red"]
    assert out["verdict"] == "FAIL"


def test_build_findings_raises_on_business_persisted_token():
    out = A.build_findings(
        {"by_layer": {A.LAYER_ANALYSIS: 3}, "total_hits": 3, "unregistered": []},
        [], [], [],
        {"business_count": 1, "business_files": [{"file": "data/watch.json"}],
         "files_with_token": [{"file": "data/watch.json"}], "count": 1},
        [], [])
    assert "R5_RENAME_NEEDS_MIGRATION" in out["red"]
    assert out["verdict"] == "FAIL"


def test_build_findings_clean_case_is_pass_with_advisories():
    scan = {"by_layer": {A.LAYER_ANALYSIS: 2}, "total_hits": 2, "unregistered": []}
    out = A.build_findings(scan, [], [], [], {"business_count": 0, "count": 0}, [], [])
    assert out["verdict"] == "PASS"
    assert out["red"] == []
    ids = {f["id"] for f in out["findings"]}
    assert "A1_HIGH_VALUE_RENAME_IS_DEAD_CODE" in ids


def test_build_findings_flags_indirect_persister():
    indirect = [{"file": "scripts/watch_live_verdicts.py", "lines": [22],
                 "layer": A.LAYER_SCRIPTS, "writes": True}]
    out = A.build_findings(
        {"by_layer": {A.LAYER_SCRIPTS: 1}, "total_hits": 1, "unregistered": []},
        [], [], [], {"business_count": 0, "count": 0}, [], indirect)
    ids = {f["id"] for f in out["findings"]}
    assert "A4_INDIRECT_PERSISTER" in ids


def test_write_report_emits_verdict_line(tmp_path):
    scan = {"by_layer": {A.LAYER_ANALYSIS: 1}, "total_hits": 1,
            "unregistered": [], "registered_files": []}
    out = A.build_findings(scan, [], [], [], {"business_count": 0, "count": 0}, [], [])
    j = tmp_path / "a.json"
    m = tmp_path / "a.md"
    A.write_report(out, str(j), str(m))
    payload = json.loads(j.read_text(encoding="utf-8"))
    assert payload["verdict"] == "PASS"
    md = m.read_text(encoding="utf-8")
    assert "**判定: PASS**" in md
    assert "分层命中明细" in md


def test_run_audit_end_to_end():
    out = A.run_audit()
    assert out["verdict"] in ("PASS", "FAIL")
    assert out["signal_scan"]["total_hits"] > 0
    assert out["rename_cost"]


def test_live_persist_scan_not_truncated():
    """持久面扫描必须跑满预算, 否则「无持久化携带」结论覆盖面不完整。"""
    res = A.scan_persisted_artifacts()
    assert not res["truncated"]
    assert res["scanned_files"] > 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T53 守卫测试: mh 训练脚本绕过 gates.verdict 的只读盘点与静态守卫语义。

覆盖四族:
  T1 判定映射语义 (mh 标签 -> gates.verdict)
  T2 bundle 字段映射 (缺什么 / 现成什么)
  T3 分类 Tier 与 fail-closed 结论装配
  T4 活体回归 (真实仓库 + 真实数据集, 缺数据集则 SKIP) + 跨审计回归 (T51 基线不漂移)
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if R not in sys.path:
    sys.path.insert(0, R)

import scripts.audit_mh_train_div_bypass as A  # noqa: E402


# ── T1 判定映射 ─────────────────────────────────────────────────────────
def test_beats_market_branch_changes_verdict():
    """唯一会翻转结论的一支: mh 说 BEATS MARKET, gates 只会说 NO EDGE。"""
    got = A.gates_verdict_for_mh("BEATS MARKET", n_samples=3902, n_matches=1141)
    assert got["branch"] == "BEATS MARKET"
    assert got["g3_quality"] is True
    assert got["gates_verdict"] == "NO EDGE"
    assert got["edge_claimable"] is False
    assert got["conclusion_changed"] is True
    assert got["n_samples_g1"] == "PASS"
    assert got["n_matches_g1"] == "FAIL"


def test_other_two_branches_do_not_change_verdict():
    for label in ("TIE", "NO EDGE"):
        got = A.gates_verdict_for_mh(label)
        assert got["gates_verdict"] == "NO EDGE"
        assert got["conclusion_changed"] is False
        assert got["blocking_gates"] == ["G3_quality"]


def test_no_edge_label_means_model_worse_not_meaning_of_gates():
    """mh 的 NO EDGE 语义 = 模型劣于市场; 与 gates 的「无统计边缘」不是同义词。"""
    got = A.gates_verdict_for_mh("NO EDGE")
    assert "劣于市场" in got["mh_label"]
    assert A.gates_verdict_for_mh("TIE")["mh_label"] == "ΔLL 在 ±0.001 带内"


def test_semantics_delta_explains_single_vs_conjunctive_gate():
    d = A.gates_verdict_for_mh("BEATS MARKET")["semantics_delta"]
    assert "G3" in d and "六门禁合取" in d


# ── T2 bundle 映射 ──────────────────────────────────────────────────────
def test_bundle_missing_set_is_exactly_roi_and_g6():
    b = A.bundle_field_map()
    assert set(b["missing"]) == {
        "roi_point", "roi_ci_low", "roi_ci_high", "roi_method",
        "mech_fav_roi", "paired_excess", "paired_excess_ci_low", "model_source",
    }
    assert b["missing_count"] == 8


def test_vs_market_ll_is_the_only_ready_field():
    b = A.bundle_field_map()
    assert b["ready"] == ["vs_market_ll"]
    assert "vs_market_ll" not in b["computable"]
    assert "vs_market_ll" not in b["missing"]


def test_missing_count_is_enough_to_block_edge():
    """缺 G2 与 G6 三元组 → 无论 G3 多好都不可能 EDGE (与 gates.verdict 同形)。"""
    b = A.bundle_field_map()
    assert {"roi_ci_low", "paired_excess_ci_low"} <= set(b["missing"])


# ── T3 分类与 fail-closed ───────────────────────────────────────────────
def test_known_files_classified_as_expected_or_noise():
    assert A.classify_file("scripts/mh_train_div.py") == A.TIER_EXPECTED
    assert A.classify_file("scripts/mh_train_walkforward.py") == A.TIER_EXPECTED
    assert A.classify_file("scripts/mh_train_walkforward_x.py") == A.TIER_EXPECTED
    assert A.classify_file("scripts/backtest_ou_signal.py") == A.TIER_NOISE
    assert A.classify_file("scripts/model_g1_reach_plan.py") == A.TIER_DOC_META


def test_unknown_file_is_unexpected_not_auto_exempted():
    assert A.classify_file("scripts/some_new_research.py") == A.TIER_UNEXPECTED
    assert A.TIER_UNEXPECTED not in ("scripts/some_new_research.py",)


def test_findings_fail_closed_on_unexpected_hit():
    emitters = {
        "unexpected": [{"file": "scripts/brand_new.py", "line": 3, "token": "NO EDGE"}],
        "expected": [],
    }
    reg = {"registry": [], "all_present": True}
    f = A.build_findings(emitters, reg, A.bundle_field_map(), None, None,
                         {"proportional_method_found": False, "g6_ready": True,
                          "dataset": "x", "verdict": "n/a"},
                         {"min_sample": 2500, "expected": 2500}, live_ok=False)
    assert A.verdict_of(f) == "FAIL"
    assert any(x["id"] == "R1_UNREGISTERED_VERDICT_EMITTER" and x["sev"] == A.FINDING_RED
               for x in f)


def test_verdict_pass_when_only_ok_findings():
    emitters = {"unexpected": [], "expected": []}
    f = A.build_findings(emitters, {"registry": [], "all_present": True},
                         {"missing": [], "missing_count": 0}, None, None,
                         {"proportional_method_found": False, "g6_ready": True,
                          "dataset": "x", "verdict": "n/a"},
                         {"min_sample": 2500, "expected": 2500}, live_ok=False)
    assert A.verdict_of(f) != "FAIL"


def test_min_sample_drift_is_red():
    f = A.build_findings({"unexpected": [], "expected": []}, {"registry": [],
                                                              "all_present": True},
                         {"missing": [], "missing_count": 0}, None, None,
                         {"proportional_method_found": False, "g6_ready": True,
                          "dataset": "x", "verdict": "n/a"},
                         {"min_sample": 2400, "expected": 2500}, live_ok=False)
    assert any(x["id"] == "R7_MIN_SAMPLE_DRIFT" for x in f)


# ── T4 纯函数数值行为 ───────────────────────────────────────────────────
def test_cluster_bootstrap_sd_is_deterministic_and_positive():
    vals = [0.8 + 0.01 * (i % 7) for i in range(50)]
    a = A.cluster_bootstrap_sd(vals, reps=100, seed=7)
    b = A.cluster_bootstrap_sd(vals, reps=100, seed=7)
    c = A.cluster_bootstrap_sd(vals, reps=100, seed=8)
    assert a == pytest.approx(b)
    assert a > 0
    assert a != pytest.approx(c)


def test_cluster_bootstrap_sd_flat_input_is_zero():
    assert A.cluster_bootstrap_sd([0.5] * 30) == 0.0
    assert A.cluster_bootstrap_sd([0.5]) == 0.0


def test_load_rows_parses_header_and_types():
    import csv
    p = os.path.join(R, "data", "mh_dataset_x.csv")
    if not os.path.exists(p):
        pytest.skip("数据集不存在")
    rows = A.load_rows(p)
    assert rows and len(rows) > 1000
    r = rows[0]
    assert isinstance(r["_y"], int) and isinstance(r["_cp"], int)
    assert {"match_key", "imp_home", "ridx"} <= set(r.keys())


def test_walkforward_g1_dilemma_on_real_data():
    p = os.path.join(R, "data", "mh_dataset_x.csv")
    if not os.path.exists(p):
        pytest.skip("数据集不存在")
    g = A.walkforward_g1_counts(A.load_rows(p))
    assert g["total_test_samples"] > g["total_test_matches"]
    assert g["g1_by_sample"] is True and g["g1_by_match"] is False
    assert g["design_effect"] > 2.0
    assert g["naive_ci_understate_factor"] > 1.2


def test_devig_caliber_flags_proportional_as_not_g6_ready():
    """mh_build_dataset.fair_probs = 比例法 → G6 机械基准不可直接沿用。"""
    p = os.path.join(R, "scripts", "mh_build_dataset.py")
    src = A.read_text(p)
    d = A.devig_caliber_of(src, None)
    assert d["proportional_method_found"] is True
    assert d["g6_ready"] is False
    assert "09-23" in d["note"]


def test_devig_power_source_is_flagged_ready():
    d = A.devig_caliber_of("def f():\n    p = devig_power(o)\n", None)
    assert d["g6_ready"] is True


# ── T5 活体回归 ─────────────────────────────────────────────────────────
def test_live_scan_has_no_unregistered_emitter():
    """守卫 G1 的脸面: 今日零未登记产出体；新增一个即 FAIL。"""
    hits = A.scan_verdict_emitters()
    assert hits["unexpected_count"] == 0, hits["unexpected"]
    assert set(A.EXPECTED_EMITTERS) <= {h["file"] for h in hits["expected"]}


def test_live_expected_registry_all_present():
    reg = A.scan_expectation_registry()
    assert reg["all_present"] is True
    assert reg["with_literals"] == len(A.EXPECTED_EMITTERS)


def test_live_constants_baseline():
    assert A.constant_baseline()["min_sample"] == 2500


@pytest.mark.skipif(not os.path.exists(os.path.join(R, "data", "mh_dataset_x.csv")),
                    reason="数据集不存在")
def test_live_fold_and_noise_numbers():
    rows = A.load_rows(os.path.join(R, "data", "mh_dataset_x.csv"))
    g = A.walkforward_g1_counts(rows)
    assert 3000 < g["total_test_samples"] < 5000
    assert 1000 < g["total_test_matches"] < 1300
    n = A.noise_floor(rows)
    assert n["worst_band_over_sd"] is not None
    assert 0 < n["worst_band_over_sd"] < 0.1       # ±0.001 远细于噪声
    assert all(f["cluster_sd_market_ll"] > 0 for f in n["per_fold"])


def test_guard_fails_when_someone_prints_verdict_again(tmp_path):
    """防回退主用例: 往 scripts/ 丢一个自印判定的新脚本, 守卫必须变红。

    用临时目录跑真实扫描（不污染仓库、不落地任何文件）。
    """
    (tmp_path / "new_research.py").write_text(
        "print('VERDICT: ' + str(d) + ' -> NO EDGE')\n", encoding="utf-8")
    hits = A.scan_verdict_emitters(root=str(tmp_path))
    assert hits["unexpected_count"] >= 1
    assert any(h["file"] == "new_research.py" for h in hits["unexpected"])
    f = A.build_findings(hits, A.scan_expectation_registry(), A.bundle_field_map(),
                         A.walkforward_g1_counts(A.load_rows(
                             os.path.join(R, "data", "mh_dataset_x.csv"))) or None,
                         A.noise_floor(A.load_rows(
                             os.path.join(R, "data", "mh_dataset_x.csv"))) or None,
                         A.devig_caliber_of(A.read_text(
                             os.path.join(R, "scripts", "mh_build_dataset.py")), None),
                         A.constant_baseline(), live_ok=True)
    assert A.verdict_of(f) == "FAIL"
    assert any(x["id"] == "R1_UNREGISTERED_VERDICT_EMITTER" for x in f)


def test_cross_audit_regression_t51_baseline_unshifted():
    """踩过的坑: 本轮新写的文件不得把 T51 的三态扫描基线从 2 打到大。

    T47 的调度方扫描已被本仓库一份脚本的注释打过一次(0→1); 同类事故不得再犯。
    """
    path = os.path.join(R, "scripts", "audit_verification_report_freshness.py")
    spec = importlib.util.spec_from_file_location("t51_audit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    hits = mod.scan_verdict_literal_sources()
    files = sorted({h["file"] for h in hits["unexpected"]})
    # T57 放宽词表 + 共享带理由登记册后的新基线: 三个真绕过体落 EXPECTED_TIER,
    # pipeline/fusion_wdl_proto.py 落 KNOWN_FALSE_POSITIVE → UNEXPECTED 为空。
    assert files == []
    assert {h["file"] for h in hits.get("expected", [])} == set(A.EXPECTED_EMITTERS)

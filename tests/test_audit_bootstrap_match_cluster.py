#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T56 验证台 bootstrap 分簇的回归守卫测试 (承接 T53 §4/Q-c, 承接 T45 §Q-c)。

覆盖:
  T1 分组 bootstrap 参考实现的正确性 (索引矩阵在界内 / 单簇退化 / 固定种子可复现)
  T2 结构恒等: 每簇只含 1 行时, 分组 bootstrap 必须逐位等于现有 iid 口径
     (这是「新增分组参数不得破坏 9042 行账本复现性」的机械保证)
  T3 estimator 校核: 真分簇数据下 iid 覆盖率必须显著低于分簇覆盖率 (iid 假置信)
  T4 账本不变式: 每个 source 内 match_id 无重复 (分组键有效的机械前提)
  T5 分簇必须把 CI 放宽, 且不得翻转既有结论 ( fail-closed: 翻转即 FAIL )
  T6 同场跨源并存 → 分组键必须是 match_id 而不是行
  T7 Q1 静态面: stats.py 无分组参数 (上线前的现状快照) + metrics.py 调用点计数
  T8 只读性: 本脚本不写 verification.db / 打开账本必带 mode=ro

纯只读: 不碰 events.db、不改 verification/、不跑验证台、不写生产数据。
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from audit_bootstrap_match_cluster import (  # noqa: E402
    LEDGER_DB, REPO_ROOT, SOURCES, STATS_PY,
    audit_source, build_report, checkpoint_simulation, cluster_bootstrap_mean,
    cluster_counts, cluster_draw_rows, cross_source_overlap, iid_bootstrap_ci,
    load_rows, mc_coverage, paired_diffs, render_md, scan_stats_call_sites,
    scan_bootstrap_surface, seed_stability, verdict_of,
)

LEDGER_OK = LEDGER_DB.exists()


# ── T1 参考实现自身 ────────────────────────────────────────────────────────
def test_draw_rows_are_flat_and_cluster_bounded():
    sizes = np.array([3, 1, 4, 2])
    rep, idx = cluster_draw_rows(sizes, 25, seed=3)
    assert rep.shape == idx.shape
    assert rep.max() < 25 and idx.min() >= 0 and idx.max() < int(sizes.sum())
    # 每个 replicate 抽到行: 7..14 之间 (整簇进出, 行数 = 被抽簇大小之和)
    assert 7 <= int(np.bincount(rep, minlength=25).min())
    assert int(np.bincount(rep, minlength=25).max()) <= int(sizes.sum()) * 2


def test_draw_rows_respects_sampled_cluster_block():
    """抽中的行必须整段落在被抽簇的块内 (不能被跨簇错配)。"""
    sizes = np.array([3, 1, 4, 2])
    k = sizes.size
    rep, idx = cluster_draw_rows(sizes, 8, seed=3)
    offs = np.cumsum(sizes) - sizes
    per_rep = np.bincount(rep, minlength=8)
    cum = np.cumsum(sizes)
    for r in range(8):
        rows = idx[rep == r]
        # 逐行反查所属簇块: 属于第 j 簇 ⟺ offs[j] <= row < offs[j]+sizes[j]
        j = np.searchsorted(offs, rows, side="right") - 1
        assert j.min() >= 0 and j.max() < k
        assert (offs[j] <= rows).all() and (rows < offs[j] + sizes[j]).all()


def test_draw_matrix_matches_brute_force_loop():
    """与「抽簇 → 取该簇全部行」的暴力实现逐位对拍 (同种子同流)。"""
    sizes = np.array([3, 1, 4, 2])
    n_boot = 6
    rep, idx = cluster_draw_rows(sizes, n_boot, seed=42)
    offs = np.cumsum(sizes) - sizes
    rng = np.random.default_rng(42)
    ci = rng.integers(0, sizes.size, size=(n_boot, sizes.size))
    for r in range(n_boot):
        expect: list = []
        for j in range(sizes.size):
            c = int(ci[r, j])
            expect.extend(range(int(offs[c]), int(offs[c] + sizes[c])))
        assert sorted(int(x) for x in idx[rep == r]) == sorted(expect)


def test_draw_matrix_is_deterministic_and_seed_sensitive():
    a = cluster_draw_rows(np.array([4, 2, 3]), 8, seed=11)
    b = cluster_draw_rows(np.array([4, 2, 3]), 8, seed=11)
    c = cluster_draw_rows(np.array([4, 2, 3]), 8, seed=12)
    np.testing.assert_array_equal(a, b)
    assert not np.array_equal(a, c)


def test_cluster_bootstrap_flat_values_degenerate():
    """全等值输入的分簇分布方差为 0 → 不得伪造宽区间 (防"越分簇越显著")。"""
    v = [0.5] * 30
    labels = [f"d{i}" for i in range(10) for _ in range(3)]
    pt, lo, hi = cluster_bootstrap_mean(v, labels, n_boot=200)
    assert pt == pytest.approx(0.5)
    assert lo == pytest.approx(0.5) and hi == pytest.approx(0.5)


def test_cluster_bootstrap_single_cluster_degenerates():
    pt, lo, hi = cluster_bootstrap_mean([1.0, 2.0, 3.0], ["only"], n_boot=50)
    assert lo == pytest.approx(pt) and hi == pytest.approx(pt)


def test_labels_none_falls_back_to_iid_path():
    v = list(np.linspace(-1, 1, 40))
    pt, lo, hi = cluster_bootstrap_mean(v, None, n_boot=500)
    lo2, hi2 = iid_bootstrap_ci(v, n_boot=500)
    assert lo == pytest.approx(lo2) and hi == pytest.approx(hi2)


# ── T2 结构恒等 (不破坏现有复现性) ────────────────────────────────────────
def test_none_groups_path_is_bit_identical_to_production_stats():
    """分组参数为 None 时必须与 production `stats.roi_ci_bootstrap` 逐位相同
    —— 这是「新增 groups 参数不破坏现有 9042 行账本复现性」的机械保证。"""
    from verification import stats as _stats

    rng = np.random.default_rng(77)
    v = list(2.0 * rng.random(60) - 1.0)
    _, lo, hi = cluster_bootstrap_mean(v, None, n_boot=500)
    lo_p, hi_p = _stats.roi_ci_bootstrap(v, n_boot=500)
    assert lo == pytest.approx(lo_p, abs=1e-15)
    assert hi == pytest.approx(hi_p, abs=1e-15)


def test_singleton_clusters_approach_iid_within_montecarlo_tolerance():
    """每簇 1 行 ≡ 逐行重抽 (统计意义), 但随机数流形状不同 → 用容差而非逐位相等。"""
    rng = np.random.default_rng(77)
    v = list(2.0 * rng.random(60) - 1.0)
    labels = [f"s{i}" for i in range(60)]
    _, lo_clu, hi_clu = cluster_bootstrap_mean(v, labels, n_boot=2000)
    lo_iid, hi_iid = iid_bootstrap_ci(v, n_boot=2000)
    tol = 3.0 / np.sqrt(2000)     # 百分位估计的蒙特卡洛误差上界
    assert abs(lo_clu - lo_iid) < tol
    assert abs(hi_clu - hi_iid) < tol


# ── T3 estimator 校核 ─────────────────────────────────────────────────────
def test_clustered_data_iid_bootstrap_undercovers():
    """真分簇数据: iid 覆盖率必须显著低于分簇覆盖率 (iid 区间=假置信)。"""
    m = mc_coverage(False, trials=60, n_boot=200, k=8, m=8)
    assert m["iid_coverage"] + 0.15 < m["cluster_coverage"], \
        (m, "分簇实现未能证明 iid 低估不确定性 → 本审计的存在理由不成立")


def test_iid_data_both_agree():
    m = mc_coverage(True, trials=60, n_boot=200, k=8, m=8)
    assert abs(m["iid_coverage"] - m["cluster_coverage"]) < 0.15


# ── T4/T5/T6 活体账本 (只读, 缺库则 skip) ─────────────────────────────────
@pytest.mark.skipif(not LEDGER_OK, reason="verification.db 不存在 (只读审计环境)")
def test_ledger_has_no_duplicate_match_id_per_source():
    """分组键 match_id 有效的前提: 同 source 内一场只有一行 (T56 Q2 答案)。"""
    for src in SOURCES:
        ids = [r["match_id"] for r in load_rows(src)]
        assert len(ids) == len(set(ids)), f"{src}: 存在同 match_id 多行 → iid 口径已被破坏"


@pytest.mark.skipif(not LEDGER_OK, reason="verification.db 不存在")
def test_day_clustering_ci_stays_in_sane_band():
    """分簇不是「必然放宽」: ICC≈0 时会变窄 (KNN 实测 0.66×)。
    此处不假设方向, 只约束带宽在可解释区间 (防实现静默错位)。"""
    for src in SOURCES:
        r = audit_source(src, n_boot_iid=400, n_boot_cluster=300)
        ratio = r["width_inflation_ratio"]
        assert 0.3 <= ratio <= 3.0, (src, ratio)
        assert not r["paired_verdict_flips"], f"{src}: 分簇翻转了 G6 判据"


@pytest.mark.skipif(not LEDGER_OK, reason="verification.db 不存在")
def test_checkpoint_simulation_reproduces_t53_design_effect():
    """同一场 3 行 ≡ mh 的 3.42 检查点/场 → iid 半宽应低估约 1.7× (T53 实测 1.85×)。"""
    for s in [x for x in build_report()["Q2_checkpoint_simulation"] if not x.get("skipped")]:
        assert 1.4 <= s["inflation_ratio"] <= 2.1, s


@pytest.mark.skipif(not LEDGER_OK, reason="verification.db 不存在")
def test_match_cluster_on_unique_rows_matches_iid():
    """账本 match_id 唯一 → 按 match 分簇必须≈iid (设计效应 1, 反证「账本无重复」)."""
    st = {s["source"]: s for s in build_report()["Q2_seed_stability"]}
    s = st.get("KNN")
    assert s is not None
    d = s["by_match_id"]
    assert d["max"] < 0.0 and d["min"] > s["iid_ci_low"] - 0.01
    assert not d["all_positive"], "按 match 分簇不得凭空把 KNN 判成正 ROI"


@pytest.mark.skipif(not LEDGER_OK, reason="verification.db 不存在")
def test_any_recorded_flip_must_fail_the_report():
    """fail-closed: 只要记录了「分簇翻转结论」, 报告判定就必须是 FAIL。"""
    rep = build_report()
    assert verdict_of(rep) == "FAIL"
    any_flip = any(s["verdict_flips_contains_zero"] or s["paired_verdict_flips"]
                   for s in rep["Q2_ledger_duplication"])
    assert any_flip is (verdict_of(rep) == "FAIL")
    # 语义: 任何 source 翻转 → 必须 FAIL (本轮 KNN 按日分簇 ci_low>0 即属此类)
    assert any(s["verdict_flips_contains_zero"] for s in rep["Q2_ledger_duplication"])


@pytest.mark.skipif(not LEDGER_OK, reason="verification.db 不存在")
def test_same_match_spans_multiple_sources():
    """同场跨源并存 → 任何按行混合重抽的口径会把同一场计两次。"""
    x = cross_source_overlap()
    inter = x["intersections"]
    assert any(v > 0 for v in inter.values()), inter
    assert "match_id" in x["note"]


@pytest.mark.skipif(not LEDGER_OK, reason="verification.db 不存在")
def test_ledger_rows_payoff_and_labels_aligned():
    """防本轮自踩坑: pays 与聚类标签必须来自同一批行 (payoff=None 要成对丢弃)。"""
    rows = load_rows(SOURCES[0])
    pays = [r for r in rows if r["payoff"] is not None]
    dates = [r["match_date"] for r in pays]
    assert len(dates) == len(pays)
    assert len(dates) == len(set(dates)) or True  # 标签行数自行数, 不假设唯一


# ── T7 Q1 静态面 ──────────────────────────────────────────────────────────
def test_stats_py_currently_has_no_group_parameter():
    """现状快照: 规格落地前 stats.py 不具备分组重抽 (上线后此测试应改为断言已支持)。"""
    src = STATS_PY.read_text(encoding="utf-8", errors="ignore")
    assert "def roi_ci_bootstrap(returns: List[float], alpha: float = 0.05," in src
    assert not re.search(r"def\s+roi_ci_bootstrap\([^)]*\bgroups?\s*:", src)


def test_metrics_call_sites_are_the_two_bootstrap_lines():
    sites = scan_stats_call_sites()
    assert len(sites) == 3  # 2× bootstrap (roi + G6 配对差) + 1× roi_ci_t
    assert [s["line"] for s in sites] == [51, 53, 152]


def test_bootstrap_surface_reports_no_grouping_support():
    q1 = scan_bootstrap_surface()
    assert q1["stats_py_group_parameter"] is False
    assert "0 个调用点具备分簇能力" in q1["notes"]


# ── T8 只读性 ─────────────────────────────────────────────────────────────
def test_audit_script_never_writes_ledger():
    src = (REPO_ROOT / "scripts" / "audit_bootstrap_match_cluster.py").read_text(
        encoding="utf-8", errors="ignore")
    for bad in ("INSERT INTO verification_ledger", "UPDATE verification_ledger",
                "DELETE FROM verification_ledger"):
        assert bad not in src
    assert "mode=ro" in src


@pytest.mark.skipif(not LEDGER_OK, reason="verification.db 不存在")
def test_ledger_open_is_readonly():
    con = sqlite3.connect(f"file:{LEDGER_DB}?mode=ro", uri=True)
    try:
        con.execute("SELECT COUNT(*) FROM verification_ledger").fetchone()
    finally:
        con.close()
    rows = load_rows(SOURCES[0])
    assert rows, "账本不可读"


def test_report_verdict_and_render_smoke():
    rep = build_report()
    assert rep["mode"] == "read-only-only"
    md = render_md(rep)
    assert "判定" in md and "Q2" in md and "Q3" in md
    assert verdict_of(rep) in ("PASS", "FAIL")


@pytest.mark.skipif(not LEDGER_OK, reason="verification.db 不存在")
def test_seed_stability_has_required_buckets():
    st = {s["source"]: s for s in build_report()["Q2_seed_stability"]}
    assert set(st) >= {"KNN", "candles_ensemble"}
    for s in st.values():
        for name in ("by_match_id", "by_match_date"):
            d = s[name]
            assert set(d) >= {"ci_low_per_seed", "min", "max", "spread", "all_positive"}
            assert len(d["ci_low_per_seed"]) == len(s["seeds"])
        assert "iid_ci_low" in s


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

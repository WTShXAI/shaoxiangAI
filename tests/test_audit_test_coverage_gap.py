#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T20 解析逻辑单测：不碰生产，用临时 ast 片段与真实 tests/ 校验。"""
import ast
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import audit_test_coverage_gap as m  # noqa: E402


def _write_tmp_module(code: str) -> str:
    d = tempfile.mkdtemp()
    p = os.path.join(d, "sample_mod.py")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(code)
    return p


def test_extract_public_symbols_basic():
    p = _write_tmp_module(
        "def public_fn():\n    pass\n"
        "def _private_fn():\n    pass\n"
        "class PublicCls:\n"
        "    def method(self):\n        pass\n"
        "    def _hidden(self):\n        pass\n"
        "class _PrivateCls:\n"
        "    def m(self):\n        pass\n"
    )
    syms = m.extract_public_symbols(p)
    names = {s.name for s in syms}
    assert "public_fn" in names
    assert "_private_fn" not in names
    assert "PublicCls" in names
    assert "method" in names            # 类内公共方法应被提取
    assert "_hidden" not in names
    assert "_PrivateCls" not in names   # 私有类本身不提取
    # PublicCls 内的 method 应记录 container
    method = next(s for s in syms if s.name == "method")
    assert method.container == "PublicCls"
    assert method.kind == "function"


def test_load_test_symbols_finds_reference():
    d = tempfile.mkdtemp()
    tp = os.path.join(d, "test_sample.py")
    with open(tp, "w", encoding="utf-8") as fh:
        fh.write("import os\n\ndef test_foo():\n    x = my_target_fn(1)\n    return x\n")
    ids = m.load_test_symbols(d)
    assert "my_target_fn" in ids
    assert "test_foo" in ids
    assert "os" in ids


def test_rate_risk_high_keywords():
    risk, _ = m.rate_risk("settle", "settle_match")
    assert risk == "HIGH"
    risk, _ = m.rate_risk("odds_math", "devig_power")
    assert risk == "HIGH"
    risk, _ = m.rate_risk("some_mod", "crossbook_guard")
    assert risk == "HIGH"


def test_rate_risk_med_keywords():
    risk, _ = m.rate_risk("feature_library", "build_feature")
    assert risk == "MED"
    risk, _ = m.rate_risk("odds_collector", "fetch_odds")
    assert risk == "MED"


def test_rate_risk_low_explicit():
    risk, _ = m.rate_risk("odds_theory_exam", "whatever")
    assert risk == "LOW"
    risk, _ = m.rate_risk("utils", "random_helper")
    assert risk == "LOW"


def test_build_gap_report_runs_on_real_pipeline():
    """实跑真实 pipeline/ 与 tests/，确保不抛错且输出结构合理。"""
    pipeline_root, tests_dir = m._default_paths()
    if not (os.path.isdir(pipeline_root) and os.path.isdir(tests_dir)):
        import pytest
        pytest.skip("pipeline/tests 不在默认路径（CI 环境）")
    rep = m.build_gap_report(pipeline_root, tests_dir)
    assert "summary" in rep and "top_uncovered_gaps" in rep
    s = rep["summary"]
    assert s["total_public_symbols"] > 0
    assert 0 <= s["coverage_proxy_pct"] <= 100
    # 风险统计一致
    assert s["by_risk"]["HIGH"]["total"] >= 0
    # Top 缺口全部为未覆盖
    for g in rep["top_uncovered_gaps"]:
        assert g["covered"] is False


def test_iter_python_modules_skips_cache():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "__pycache__"))
    with open(os.path.join(d, "__pycache__", "x.py"), "w") as fh:
        fh.write("x=1\n")
    with open(os.path.join(d, "real.py"), "w") as fh:
        fh.write("def f(): pass\n")
    mods = list(m.iter_python_modules(d))
    assert all("__pycache__" not in rp for rp, _ in mods)
    assert any(rp == "real.py" for rp, _ in mods)

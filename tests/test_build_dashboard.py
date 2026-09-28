"""tests/test_build_dashboard.py — T11 样本累积趋势面板回归

只读既有 reports 产物, 不碰 events.db / 不写生产数据。
"""
import os
import csv
import json
import importlib.util
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "build_dashboard.py")
CSV = os.path.join(ROOT, "reports", "verification_sample_growth.csv")
JSON = os.path.join(ROOT, "reports", "verification_sample_growth.json")


def _load_module():
    spec = importlib.util.spec_from_file_location("build_dashboard_t11", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_load_sample_csv_parses_series():
    """CSV 时间序列正确还原为 {source: [[date, cum_n], ...]}。"""
    mod = _load_module()
    summary, series = mod.load_sample()
    assert "KNN" in series
    assert "market_baseline" in series
    # 末值与 JSON 汇总一致
    by = summary.get("by_source", {})
    for src, pts in series.items():
        assert all(len(p) == 2 and isinstance(p[1], int) for p in pts)
        if src in by:
            assert pts[-1][1] == by[src]["cum_n"]


def test_load_sample_handles_missing_csv(tmp_path, monkeypatch):
    """CSV 缺失时返回空 series 而非抛错。"""
    mod = _load_module()
    monkeypatch.setattr(mod, "SAMPLE_CSV", str(tmp_path / "nope.csv"))
    summary, series = mod.load_sample()
    assert series == {}


def test_main_runs_and_renders_panel():
    """main() 顺利生成 HTML 且含样本累积趋势面板标记。"""
    mod = _load_module()
    # 确保 csv/json 存在(真实产物), 否则仅验证不崩
    mod.main()
    out = mod.OUT
    assert os.path.exists(out), "看板 HTML 未生成"
    html = open(out, encoding="utf-8").read()
    assert "样本累积趋势" in html
    assert "G1 验收线" in html or "G1=" in html
    # 趋势数据已注入
    assert "__SAMPLE__" not in html, "占位符未替换"
    if os.path.exists(CSV):
        # 真实产物在: 面板应渲染 KNN / 市场基线 折线
        assert "KNN赛前" in html

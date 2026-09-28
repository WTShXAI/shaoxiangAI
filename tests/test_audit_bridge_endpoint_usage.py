"""B1 bridge 端点使用率审计脚本的只读自测（不写 DB / 不杀进程）。"""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "audit_bridge_endpoint_usage.py")


def _load():
    spec = importlib.util.spec_from_file_location("audit_bridge_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_extract_endpoints_basic():
    mod = _load()
    eps = mod.extract_endpoints()
    assert isinstance(eps, list) and len(eps) > 0
    # 每个元素为 (path, methods, static_prefix)
    for path, methods, static in eps:
        assert path.startswith("/")
        assert isinstance(methods, list) and len(methods) > 0
        # 通配 /{full_path:path} 的静态前缀为空（被归类 infra，不计入候选）
        assert static.startswith("/") or static == ""


def test_extract_skips_param_suffix():
    mod = _load()
    eps = dict((p, (m, s)) for p, m, s in mod.extract_endpoints())
    # 参数化端点的最深静态前缀应截断到首个 { 之前
    assert eps["/api/leagues/{sport_key}/fixtures"][1] == "/api/leagues"
    assert eps["/api/live-odds/{match_key}"][1] == "/api/live-odds"


def test_main_writes_reports(tmp_path, monkeypatch):
    mod = _load()
    # 重定向报告输出到临时目录
    monkeypatch.setattr(mod, "REPORT_JSON", str(tmp_path / "r.json"))
    monkeypatch.setattr(mod, "REPORT_MD", str(tmp_path / "r.md"))
    mod.main()
    assert os.path.exists(tmp_path / "r.json")
    assert os.path.exists(tmp_path / "r.md")
    import json
    data = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert data["total_api_endpoints"] > 0
    # 基础设施路径应排除，不计入业务端点候选
    assert data["infra_paths_excluded"] >= 1

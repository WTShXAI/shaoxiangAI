"""P0 启动守卫 — 核心引擎模块必须可干净导入 (SSoT 单一事实源守卫).

背景 (2026-07-11 SRE 评估):
    pipeline/engine.py, pipeline/wc_engine.py, pipeline/league_engine.py 长期 untracked,
    导致干净 clone / CI 检出缺文件 -> `import pipeline.engine` 直接崩溃.
    bridge_service.py (已跟踪) 依赖这三件套, 故该风险是"新环境不可复现"级 P0.

本测试纳入 pytest 套件; CI 跑 `pytest tests/` 即覆盖此风险, 任何核心模块缺失/语法损坏会立即 FAIL.
"""
import importlib


def _assert_importable(module: str) -> None:
    mod = importlib.import_module(module)
    assert mod is not None, f"{module} 导入返回空对象"


def test_import_reverse_odds_engine():
    """赔率破解唯一权威模块 (SSoT)."""
    _assert_importable("pipeline.reverse_odds_engine")


def test_import_engine():
    """权威预测引擎 (WCEngine/LeagueEngine + apply_softline_to_result)."""
    _assert_importable("pipeline.engine")


def test_import_wc_engine():
    """生产 World Cup 引擎 (1133 行)."""
    _assert_importable("pipeline.wc_engine")


def test_import_league_engine():
    """联赛引擎."""
    _assert_importable("pipeline.league_engine")

"""model_g1_reach_plan 纯推演函数测试（零 DB、零生产 I/O）。

覆盖：累计序列构造 / 达标首日 / 窗口速率 / 推演算术（含零速率·超地平线）/ 情景构建 /
假设依赖表 / Markdown 渲染含诚实口径。
"""
from __future__ import annotations

import datetime as dt

import pytest

from scripts.model_g1_reach_plan import (
    S0_AS_IS,
    S1_GATE_FIXED,
    S2_OPTIMISTIC,
    STATUS_BEYOND,
    STATUS_NEVER,
    STATUS_PROJECTED,
    STATUS_REACHED,
    build_assumptions,
    build_scenarios,
    build_series,
    bulk_share_over_window,
    crossing_date,
    project,
    rate_over_window,
    render_markdown,
    segment_aligned_reach,
)


def counts() -> dict[tuple[str, str], int]:
    return {
        ("KNN", "2026-09-01"): 100,
        ("KNN", "2026-09-02"): 150,   # cum 250 -> crossing 2026-09-02
        ("KNN", "2026-09-05"): 50,
        ("candles_ensemble", "2026-09-15"): 181,  # 停滞源
        ("market_baseline", "2026-08-21"): 600,
        ("market_baseline", "2026-08-22"): 200,
    }


def test_build_series_accumulates_and_sorts():
    ser = build_series(counts())
    knn = ser["KNN"]
    assert [r["date"] for r in knn] == ["2026-09-01", "2026-09-02", "2026-09-05"]
    assert [r["cum_n"] for r in knn] == [100, 250, 300]
    # 不存在的 source 返回空序列而非异常
    assert ser.get("no_such_source", []) == []


def test_crossing_date_first_time_at_or_above_target():
    ser = build_series(counts())
    assert crossing_date(ser["KNN"], 250) == "2026-09-02"   # cum 首次 >=250
    assert crossing_date(ser["KNN"], 251) == "2026-09-05"   # 251 要到 cum=300 才跨过
    assert crossing_date(ser["KNN"], 1000) is None          # 全序列最高 cum=300
    assert crossing_date(ser["candles_ensemble"], 2500) is None


def test_rate_over_window_excludes_older_outside_window():
    ser = build_series(counts())
    # 窗口 14 天 → (08-22, 09-05] 含 09-01(100)+09-02(150)+09-05(50) = 300 条
    assert rate_over_window(ser["KNN"], "2026-09-05", 14) == pytest.approx(300 / 14, rel=1e-3)
    # 窗口 4 天 → (09-01, 09-05] 下界开区间排除 09-01 → 200 条
    assert rate_over_window(ser["KNN"], "2026-09-05", 4) == pytest.approx(200 / 4)
    assert rate_over_window(ser["KNN"], "2026-09-05", 0) == 0.0


def test_project_already_reached():
    r = project(3000, 10.0, as_of="2026-09-27")
    assert r["status"] == STATUS_REACHED


def test_project_zero_rate_never():
    r = project(181, 0.0, as_of="2026-09-27")
    assert r["status"] == STATUS_NEVER
    assert r["reach_date"] is None


def test_project_arithmetic_reach_date():
    r = project(181, 37.0, target=2500, as_of="2026-09-27")  # 需 2319/37=62.67→63 天
    assert r["days_needed"] == 63
    assert r["reach_date"] == (dt.date(2026, 9, 27) + dt.timedelta(days=63)).isoformat()
    assert r["status"] == STATUS_PROJECTED


def test_project_beyond_horizon():
    r = project(181, 1.0, target=2500, as_of="2026-09-27", horizon_days=100)  # 需 2319 天
    assert r["status"] == STATUS_BEYOND
    assert r["reach_date"] is None


def test_segment_aligned_restarts_from_zero():
    r = segment_aligned_reach(cum=181, rate=37.0, as_of="2026-09-27", horizon_days=120)
    assert r["status"] == STATUS_PROJECTED
    assert r["days_needed"] == 68  # ceil(2500/37)


def test_build_scenarios_s0_zero_when_stalled_and_s1_from_backlog():
    """停滞源：账本停在 09-10（距基准日 10 天 > 7 天停滞线）→ S0 速率按 0 → NEVER。"""
    ser = build_series({("candles_ensemble", "2026-09-10"): 181})
    supply = {
        "candles_ensemble": {
            "2026-09-18": {"total": 6, "finished": 0},
            "2026-09-19": {"total": 20, "finished": 0},
            "2026-09-20": {"total": 24, "finished": 0},
        },
    }
    sc = build_scenarios(ser, supply, window_days=14, as_of="2026-09-20")
    rows = sc["candles_ensemble"]
    s0 = next(r for r in rows if r["scenario"] == S0_AS_IS)
    s1 = next(r for r in rows if r["scenario"] == S1_GATE_FIXED)
    assert s0["stale_days"] == 10
    assert s0["rate"] == 0.0 and s0["status"] == STATUS_NEVER
    # 补账候选 = (6-0)+(20-0)+(24-0) = 50；距上次账本日期 09-10→09-20 = 10 天
    assert s1["backlog"] == 50 and s1["days_since"] == 10
    assert s1["rate"] == pytest.approx(5.0, rel=1e-3)
    s2 = next(r for r in rows if r["scenario"] == S2_OPTIMISTIC)
    assert s2["rate"] == pytest.approx(50 / 14, rel=1e-3)


def test_build_scenarios_s0_keeps_rate_when_freshly_accruing():
    """非停滞源（账本仍在增长）保留窗口速率，不被停滞闸门误杀。"""
    ser = build_series({("KNN", "2026-09-19"): 60, ("KNN", "2026-09-20"): 60})
    sc = build_scenarios(ser, {"KNN": {"2026-09-20": {"total": 10, "finished": 4}}},
                         window_days=14, as_of="2026-09-20")
    s0 = next(r for r in sc["KNN"] if r["scenario"] == S0_AS_IS)
    assert s0["stale_days"] == 0
    assert s0["rate"] == pytest.approx(120 / 14, rel=1e-3)
    assert s0["status"] == STATUS_PROJECTED


def test_bulk_share_over_window_detects_one_off_write():
    ser = build_series({("x", "2026-09-01"): 5, ("x", "2026-09-20"): 95})
    assert bulk_share_over_window(ser["x"], "2026-09-20", 30) == pytest.approx(0.95, rel=1e-3)
    assert bulk_share_over_window(ser["x"], "2026-09-20", 1) == 1.0   # 只剩 09-20 单日
    assert bulk_share_over_window(ser["x"], "2026-09-05", 1) == 0.0   # 窗口内无任何数据


def test_assumptions_marks_pre_fix_scenarios_invalid():
    ser = build_series(counts())
    supply = {"KNN": {"2026-09-20": {"total": 10, "finished": 0}}}
    sc = build_scenarios(ser, supply, window_days=14, as_of="2026-09-20")
    rows = build_assumptions(sc, ser, gate_blocked_srcs=["candles_ensemble"], drift_alert=True,
                             retrain_suggest=True, as_of="2026-09-20")
    # KNN 未被 daily 门控卡住，其 S0 前提成立；candles 被卡住 → 不成立
    s0_knn = next(a for a in rows if a["scenario"] == S0_AS_IS and a["source"] == "KNN")
    assert s0_knn["holds_now"] is True and s0_knn["valid_before_fix"] is False
    s0_cand = next(a for a in rows if a["scenario"] == S0_AS_IS and a["source"] == "candles_ensemble")
    assert s0_cand["holds_now"] is False and s0_cand["valid_before_fix"] is True
    # 极端乐观情景一律标注"未扣守卫剔除"
    s2 = next(a for a in rows if a["scenario"] == S2_OPTIMISTIC and a["source"] == "KNN")
    assert "守卫" in s2["note"]


def test_render_markdown_carries_honesty_disclaimer():
    ser = build_series(counts())
    supply = {"KNN": {"2026-09-20": {"total": 10, "finished": 4}}}
    sc = build_scenarios(ser, supply, window_days=14, as_of="2026-09-20")
    assumptions = build_assumptions(sc, ser, gate_blocked_srcs=[], drift_alert=False,
                                    retrain_suggest=False, as_of="2026-09-20")
    md = render_markdown({
        "generated_at": "2026-09-20T00:00:00Z",
        "gate_blocked_sources": ["candles_ensemble"],
        "as_of": "2026-09-20",
        "g1_accept_n": 2500,
        "horizon_days": 720,
        "drift_ll_delta": 0.0323,
        "retrain_suggest": True,
        "current": {"KNN": {"cum_n": 800, "gap_to_g1": 1700, "first_date": "2026-08-21",
                            "last_date": "2026-09-20", "verdict": "NO EDGE"}},
        "scenarios": sc,
        "assumptions": assumptions,
        "conclusions": ["测试结论行"],
    })
    assert "G1 只是样本及格线" in md
    assert "零信息机械对照" in md
    assert "walkforward" in md
    assert "测试结论行" in md

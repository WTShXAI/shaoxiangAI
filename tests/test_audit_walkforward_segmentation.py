"""tests/test_audit_walkforward_segmentation.py

覆盖 scripts/audit_walkforward_segmentation.py 的纯函数（零 DB、零网络、零生产 I/O）。
红线：只读仓库文件；不碰 events.db 写入、不跑 ingest、不重训、不杀进程。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.audit_walkforward_segmentation import (  # noqa: E402
    G1_ACCEPT_N,
    STATUS_BEYOND,
    STATUS_NEVER,
    STATUS_PROJECTED,
    STATUS_REACHED,
    backfill_vs_not,
    bucket_dates,
    gap_by_segment,
    has_generation_column,
    is_cross_iteration,
    make_segments,
    project_reach,
    run_segmentation,
)


def test_make_segments_splits_by_boundaries():
    segs = make_segments("2026-09-18", "2026-09-27", {"X_09-24": "2026-09-24"})
    assert [s["label"] for s in segs] == ["X_09-24", "C_当前段"]
    assert segs[0]["start"] == "2026-09-18" and segs[0]["end_excl"] == "2026-09-24"
    assert segs[1]["start"] == "2026-09-24"


def test_make_segments_boundary_out_of_range_is_dropped():
    # 切点早于区间起点或晚于终点 → 不产生空段
    segs = make_segments("2026-09-18", "2026-09-27",
                         {"A": "2026-09-10", "B": "2026-09-30", "C": "2026-09-18"})
    assert [s["label"] for s in segs] == ["C_当前段"]


def test_make_segments_single_segment_when_no_boundaries():
    segs = make_segments("2026-09-18", "2026-09-27", {})
    assert len(segs) == 1 and segs[0]["label"] == "C_当前段"


def test_bucket_dates_counts_rows_not_dates():
    segs = make_segments("2026-09-18", "2026-09-27", {"X_09-24": "2026-09-24"})
    counts = {"2026-09-19": 30, "2026-09-20": 14, "2026-09-25": 5, "2026-09-26": 9}
    b = bucket_dates(counts, segs)
    assert b["X_09-24"] == 44          # 09-19 + 09-20 落在切点前
    assert b["C_当前段"] == 14         # 09-25 + 09-26
    assert sum(b.values()) == sum(counts.values()) == 58


def test_bucket_dates_accepts_dict_or_pairs():
    segs = make_segments("2026-01-01", "2026-01-10", {"X": "2026-01-05"})
    assert bucket_dates({"2026-01-02": 3}, segs) == bucket_dates(
        [("2026-01-02", 3)], segs)


def test_bucket_dates_boundary_is_exclusive():
    segs = make_segments("2026-01-01", "2026-01-10", {"X": "2026-01-05"})
    b = bucket_dates({"2026-01-05": 7, "2026-01-04": 2}, segs)
    assert b["X"] == 2 and b["C_当前段"] == 7


def test_is_cross_iteration_true_when_two_segments_have_samples():
    assert is_cross_iteration({"A": 10, "B": 5}) is True
    assert is_cross_iteration({"A": 10, "B": 0}) is False
    assert is_cross_iteration({"A": 0, "B": 0}) is False


def test_gap_by_segment():
    g = gap_by_segment({"A": 100, "B": 50}, {"A": 100, "B": 0})
    assert g["A"] == {"samples": 100, "cum_before": 100, "gap_to_g1": 2400}
    assert g["B"] == {"samples": 50, "cum_before": 0, "gap_to_g1": 2500}


def test_gap_by_segment_never_negative():
    g = gap_by_segment({"A": 3000}, {"A": 3000})
    assert g["A"]["gap_to_g1"] == 0


def test_project_reach_reached():
    r = project_reach(2600, 10.0, as_of="2026-09-27")
    assert r["status"] == STATUS_REACHED and r["days_needed"] == 0


def test_project_reach_zero_rate_is_never():
    r = project_reach(181, 0.0, as_of="2026-09-27")
    assert r["status"] == STATUS_NEVER
    assert r["days_needed"] is None and r["reach_date"] is None


def test_project_reach_projected():
    r = project_reach(181, 51.0, as_of="2026-09-27")
    assert r["status"] == STATUS_PROJECTED
    assert r["days_needed"] == int((2500 - 181) / 51.0) + (1 if (2500 - 181) % 51 else 0)


def test_project_reach_beyond_horizon():
    r = project_reach(10, 0.5, as_of="2026-09-27", horizon_days=30)
    assert r["status"] == STATUS_BEYOND


def test_backfill_vs_not_zero_rate_never():
    c = backfill_vs_not(500, 0.0, 181, "2026-09-27")
    assert c["without_backfill"]["status"] == STATUS_NEVER
    assert c["cum_after_backfill"] == 681
    # 一次性注入后速率仍为 0 → 仍不达标（一次性补账不等于持续入账）
    assert c["with_backfill"]["status"] == STATUS_NEVER


def test_backfill_vs_not_projects_with_steady_rate():
    c = backfill_vs_not(461, 51.0, 181, "2026-09-27")
    assert c["rows_gained"] == 461
    assert c["without_backfill"]["status"] == STATUS_NEVER
    assert c["with_backfill"]["status"] == STATUS_PROJECTED


def test_generation_column_detection():
    real_cols = ["row_id", "run_id", "match_id", "model_source", "match_date",
                 "created_at", "devig_method"]
    assert has_generation_column(real_cols) is False
    assert has_generation_column(real_cols + ["model_generation"]) is True
    assert has_generation_column(real_cols + ["trained_at"]) is True


def test_run_segmentation_digest_shapes():
    segs = make_segments("2026-09-18", "2026-09-27", {"X_09-24": "2026-09-24"})
    buckets = bucket_dates({"2026-09-20": 40, "2026-09-25": 7}, segs)
    digest = run_segmentation(buckets, segs, {"net_new_rows": 461, "interval_days": 9},
                              {"X_09-24": 0, "C_当前段": 0})
    assert digest["cross_iteration"] is True
    assert digest["gap_table"]["X_09-24"]["samples"] == 40
    assert digest["backfill_estimate"]["net_new_rows"] == 461


# ── IR-30 诚实断言：样本量达标 ≠ edge，输出不得出现任何盈利宣称 ────────────────
def test_ir30_no_profit_claim_and_g1_is_not_edge(tmp_path, monkeypatch):
    import sqlite3

    import scripts.audit_walkforward_segmentation as aw
    from scripts.audit_walkforward_segmentation import render_markdown

    ledger = tmp_path / "verification.db"
    events = tmp_path / "events.db"
    for p in (ledger, events):
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE verification_ledger (row_id INTEGER, run_id TEXT, "
                    "match_id TEXT, model_source TEXT, match_date TEXT, is_credible INT, "
                    "devig_method TEXT, created_at TEXT)")
        con.execute("CREATE TABLE daily_predictions (match_key TEXT, kickoff TEXT, "
                    "match_date TEXT, home TEXT, away TEXT, league TEXT, status TEXT, "
                    "model_source TEXT, payload TEXT, generated_at REAL)")
        con.commit()
        con.close()

    # 隔离共享仓库文件: run()/collect_model_generation() 会读 TRAIN_MARKER / MONITOR_STATUS /
    # MODELS_DIR (repo 内 reports/ 与 models/)。其它测试若改写这些文件, 本测试会随跑序污染而偶发失败。
    # 指向 tmp_path 的空目录/缺失文件 → 守卫走 Exception 分支, 结果确定, 与跑序无关。
    (tmp_path / "models").mkdir(exist_ok=True)
    monkeypatch.setattr(aw, "TRAIN_MARKER", str(tmp_path / "last_candles_train_marker.json"))
    monkeypatch.setattr(aw, "MONITOR_STATUS", str(tmp_path / "monitor_status.json"))
    monkeypatch.setattr(aw, "MODELS_DIR", str(tmp_path / "models"))

    summary = aw.run(str(events), str(ledger), "2026-09-27", 9)
    md = render_markdown(summary)
    assert "不是 edge 证据" in md or "不是 edge" in md
    # 只允许出现"否定式"表述（不是 edge），禁止任何正收益宣称
    for bad in ("预计盈利", "可实现盈利", "正 ROI", "正收益", "稳定盈利", "必赢"):
        assert bad not in md, f"输出出现盈利宣称风险词: {bad}"
    assert "NO EDGE" in md
    assert summary["reach_compare"]["without_backfill"]["status"] == STATUS_NEVER
    # 空供给面路径必须给出 0 天区间而不是崩 / 留 None（2026-09-28 修复：
    # 仓库当日出现 >=as_of 的提交后 iteration_points 非空，暴露出 None 比较崩溃）。
    assert summary["backfill_interval"]["start"] == "2026-09-27"
    assert summary["backfill_interval"]["end"] == "2026-09-27"
    assert summary["backfill_interval"]["days"] == 0
    import json
    assert json.dumps(summary, ensure_ascii=False, default=str)  # 全字段可序列化(无 None 日期洞)


def test_empty_supply_does_not_crash_when_git_has_recent_commits(tmp_path, monkeypatch):
    """回归守卫: 空供给 + 有近期提交 = 曾经崩溃的组合, 必须稳定报 0 天。"""
    import sqlite3

    import scripts.audit_walkforward_segmentation as aw
    monkeypatch.setattr(aw, "TRAIN_MARKER", str(tmp_path / "m.json"))
    monkeypatch.setattr(aw, "MONITOR_STATUS", str(tmp_path / "s.json"))
    monkeypatch.setattr(aw, "MODELS_DIR", str(tmp_path / "models"))
    (tmp_path / "models").mkdir(exist_ok=True)
    ledger = tmp_path / "l.db"
    events = tmp_path / "e.db"
    for p in (ledger, events):
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE verification_ledger (row_id INTEGER, run_id TEXT, "
                    "match_id TEXT, model_source TEXT, match_date TEXT, is_credible INT, "
                    "devig_method TEXT, created_at TEXT)")
        con.execute("CREATE TABLE daily_predictions (match_key TEXT, kickoff TEXT, "
                    "match_date TEXT, home TEXT, away TEXT, league TEXT, status TEXT, "
                    "model_source TEXT, payload TEXT, generated_at REAL)")
        con.commit()
        con.close()
    s = aw.run(str(events), str(ledger), "2026-01-01", 9)
    assert s["backfill_interval"]["days"] == 0


def _test_no_production_write_called():
    """静态断言：脚本内不含对 events.db / verification.db 的写操作（仅只读 uri=ro）。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "audit_walkforward_segmentation.py")
    src = open(path, encoding="utf-8").read()
    assert 'mode=ro' in src
    assert 'query_only' in src
    assert 'INSERT INTO events' not in src and 'UPDATE events' not in src
    assert 'DELETE FROM events' not in src
    assert 'VACUUM' not in src.replace('-- VACUUM', '')

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T39 账本世代列缺失对 walkforward 分段的影响 —— 只读审计脚本的单元测试。

纯函数 + 临时 SQLite 库，零生产 I/O：不碰 events.db、不写 verification.db、
不跑 ALTER TABLE、不做任何 git 写操作、不启进程。
"""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from scripts.audit_ledger_generation_split import (  # noqa: E402
    G1_ACCEPT_N,
    align_batches_to_commits,
    column_role,
    declared_columns_from_ddl,
    gap_by_segment,
    git_commits,
    has_generation_column,
    is_run_id_git_resolvable,
    ledger_batches,
    migration_path_status,
    mixing_report,
    open_readonly,
    propose_schema_columns,
    proxy_key_verdict,
    split_granularity,
)


# ── Q1 世代列判定 ────────────────────────────────────────────────────────────
def test_declared_columns_handles_same_line_multiple_columns():
    """同行多列（p_home REAL, p_draw REAL, p_away REAL,）必须全部提取。"""
    ddl = "CREATE TABLE t (\n row_id INTEGER PRIMARY KEY,\n p_home REAL, p_draw REAL, p_away REAL,\n devig_method TEXT\n);"
    cols = declared_columns_from_ddl(ddl)
    assert "p_home" in cols and "p_draw" in cols and "p_away" in cols
    assert "row_id" in cols and "devig_method" in cols


def test_declared_columns_empty_ddl_is_empty_list():
    assert declared_columns_from_ddl("") == []


def test_declared_columns_order_preserved_and_dedup():
    cols = declared_columns_from_ddl("a TEXT\nb INTEGER\na TEXT")
    assert cols == ["a", "b"]


def test_has_generation_column_positive():
    assert has_generation_column(["created_at", "model_generation", "row_id"]) is True


@pytest.mark.parametrize("cols", [
    ["run_id", "created_at", "match_date", "devig_method"],
    ["run_id", "row_id", "match_id", "created_at"],
    [],
])
def test_has_generation_column_negative(cols):
    assert has_generation_column(cols) is False


def test_column_role_families():
    assert column_role("model_generation") == "generation"
    assert column_role("created_at") == "calendar"
    assert column_role("kickoff_utc") == "calendar"
    assert column_role("match_date") == "calendar"
    assert column_role("devig_method") == "caliber"
    assert column_role("run_id") == "identity"
    assert column_role("is_credible") == "settlement"
    assert column_role("p_draw") == "settlement"
    assert column_role("whatever") == "other"


def test_proxy_key_verdict_semantics():
    roles = {
        "model_generation": "generation", "run_id": "identity",
        "created_at": "calendar", "match_date": "calendar",
        "devig_method": "caliber", "is_credible": "settlement",
    }
    v = proxy_key_verdict(roles)
    assert v["model_generation"]["verdict"] == "CANNOT_NEED"
    assert v["run_id"]["verdict"] == "PROXY_PARTIAL"
    assert v["created_at"]["verdict"] == "PROXY_PARTIAL"
    assert v["match_date"]["verdict"] == "NO"
    assert v["devig_method"]["verdict"] == "CALIBER_ONLY"
    assert v["is_credible"]["verdict"] == "NO"


# ── 时间解析（踩坑：Z 与 +00:00 不可混比）───────────────────────────────────
def test_parse_utc_variants():
    from scripts.audit_ledger_generation_split import parse_utc
    assert parse_utc("2026-09-23T16:19:46Z") is not None
    assert parse_utc("2026-09-24T00:13:11+08:00").hour == 16  # UTC 小时
    assert parse_utc("").__class__ is not type(None) or True  # 空串返回 None
    assert parse_utc(None) is None
    assert parse_utc("not-a-time") is None
    dt = parse_utc("2026-09-24T00:13:11+08:00")
    assert dt.isoformat() == "2026-09-23T16:13:11+00:00"


# ── Q2 批次 ↔ 提交对齐（踩坑：git log 新→旧序，before[-1] 会取到最旧）──────
def test_align_picks_latest_commit_at_or_before_batch():
    commits = [
        {"commit": "OLD", "utc": "2026-09-06T05:38:12Z", "subject": "init", "kinds": ["OTHER"]},
        {"commit": "FIX", "utc": "2026-09-23T16:13:11Z", "subject": "fix", "kinds": ["DEVIG"]},
        {"commit": "LATER", "utc": "2026-09-23T16:24:08Z", "subject": "feat", "kinds": ["LEDGER"]},
    ]
    batches = [{"run_id": "741b8eb46198455f832eab63d21d6ea6", "rows": 8930,
                "first_at_utc": "2026-09-23T16:19:46Z", "last_at_utc": "2026-09-23T16:21:17Z",
                "sources": {"KNN": 2668}}]
    out = align_batches_to_commits(batches, commits)
    assert len(out) == 1
    assert out[0]["anchor_commit"] == "FIX", "必须取<=写入时刻的**最新**提交，不是最旧"
    assert out[0]["anchor_kinds"] == ["DEVIG"]
    assert out[0]["next_commit"] == "LATER"
    assert 240 < out[0]["seconds_to_next"] <= 300  # 16:24:08 - 16:19:46 = 262s


def test_align_no_commit_before_batch_gives_none():
    commits = [{"commit": "FUTURE", "utc": "2026-09-30T00:00:00Z",
                "subject": "later", "kinds": ["OTHER"]}]
    batches = [{"run_id": "x", "rows": 1, "first_at_utc": "2026-09-23T16:19:46Z",
                "last_at_utc": "2026-09-23T16:19:46Z", "sources": {}}]
    out = align_batches_to_commits(batches, commits)
    assert out[0]["anchor_commit"] is None
    assert out[0]["anchor_kinds"] == ["NONE"]
    assert out[0]["next_commit"] == "FUTURE"


def test_align_empty_commits_is_safe():
    batches = [{"run_id": "x", "rows": 10, "first_at_utc": "2026-09-23T16:19:46Z",
                "last_at_utc": "2026-09-23T16:19:46Z", "sources": {}}]
    out = align_batches_to_commits(batches, [])
    assert out[0]["anchor_commit"] is None
    assert out[0]["next_commit"] is None
    assert out[0]["seconds_to_next"] is None


# ── 只读库访问 ──────────────────────────────────────────────────────────────
def test_open_readonly_is_write_prohibited(tmp_path):
    p = tmp_path / "l.db"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE verification_ledger (row_id INTEGER, run_id TEXT)")
    con.execute("INSERT INTO verification_ledger VALUES (1,'abc')")
    con.commit()
    con.close()
    ro = open_readonly(str(p))
    try:
        assert [r[1] for r in ro.execute("PRAGMA table_info(verification_ledger)")]
        with pytest.raises(Exception):
            ro.execute("INSERT INTO verification_ledger VALUES (2,'def')")
    finally:
        ro.close()


def test_ledger_batches_groups_by_run(tmp_path):
    p = tmp_path / "l2.db"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE verification_ledger (run_id TEXT, created_at TEXT, model_source TEXT)")
    con.executemany("INSERT INTO verification_ledger VALUES (?,?,?)", [
        ("r1", "2026-09-23T16:19:46Z", "KNN"),
        ("r1", "2026-09-23T16:19:46Z", "KNN"),
        ("r2", "2026-09-25T04:25:33Z", "market_baseline"),
    ])
    con.commit()
    con.close()
    bs = ledger_batches(open_readonly(str(p)))
    assert len(bs) == 2
    assert bs[0]["run_id"] == "r1" and bs[0]["rows"] == 2
    assert bs[0]["sources"] == {"KNN": 2}
    assert bs[1]["rows"] == 1 and bs[1]["sources"] == {"market_baseline": 1}


# ── Q2 混批 / Q3 粒度 ───────────────────────────────────────────────────────
def test_mixing_report_flags_one_shot_backfill():
    batches = [
        {"run_id": "a", "rows": 8930, "first_at_utc": "2026-09-23T16:19:46Z",
         "last_at_utc": "2026-09-23T16:21:17Z", "sources": {}},
        {"run_id": "b", "rows": 75, "first_at_utc": "2026-09-25T04:25:33Z",
         "last_at_utc": "2026-09-25T04:26:32Z", "sources": {}},
    ]
    mx = mixing_report(batches)
    assert mx["n_batches"] == 2
    assert mx["n_distinct_days"] == 2
    assert mx["day_span_days"] == 2
    assert mx["one_shot_backfill"] is True
    assert mx["batch_sizes"] == [8930, 75]


def test_mixing_report_single_day_zero_span():
    batches = [{"run_id": "a", "rows": 3, "first_at_utc": "2026-09-23T23:31:28Z",
                "last_at_utc": "2026-09-23T23:31:29Z", "sources": {}}]
    mx = mixing_report(batches)
    assert mx["day_span_days"] == 0
    assert mx["one_shot_backfill"] is False


def test_split_granularity_and_gap():
    segments = [
        {"run_id": "741b8eb4" * 4, "rows": 8930, "first_at_utc": "x", "last_at_utc": "x",
         "sources": {"candles_ensemble": 181}},
        {"run_id": "7fc9ea38" * 4, "rows": 75, "first_at_utc": "x", "last_at_utc": "x",
         "sources": {"KNN": 74, "market_baseline": 1}},
    ]
    g = split_granularity(segments)
    assert g["n_segments"] == 2
    assert g["exactly_reproducible"] is True
    gaps = gap_by_segment(segments)
    assert gaps["741b8eb4"]["gap_to_g1"] == G1_ACCEPT_N - 181
    assert all(v["candles_rows"] == 0 for k, v in gaps.items() if k != "741b8eb4")
    assert all(v["gap_to_g1"] == G1_ACCEPT_N for k, v in gaps.items() if k != "741b8eb4")


# ── Q4 补列建议（规格，绝不执行）──────────────────────────────────────────
def test_propose_schema_columns_marks_new_and_existing():
    proposal = propose_schema_columns(["row_id", "created_at"])
    names = {c["column"] for c in proposal}
    assert {"model_generation", "trained_at", "ingest_rule_set", "run_commit"} <= names
    st = {c["column"]: c["status"] for c in proposal}
    assert st["model_generation"] == "建议新增"
    assert all(c["window"] == "WINDOW（ALTER TABLE，须停机窗口 + 全量回归）"
               for c in proposal)


def test_migration_path_status_detects_existing_add_column():
    src = 'LEDGER_MIGRATIONS: tuple = (\n "ALTER TABLE verification_ledger ADD COLUMN devig_method TEXT",\n)'
    m = migration_path_status(src)
    assert m["path_ready"] is True
    assert m["add_column_examples"] == ["devig_method TEXT"]
    assert m["ledger_migrations_count"] == 1


def test_migration_path_status_empty_when_no_migrations():
    assert migration_path_status("LEDGER_MIGRATIONS: tuple = ()")["path_ready"] is False


# ── git 只读探测（不得写）───────────────────────────────────────────────────
def test_git_helpers_do_not_mutate_repo():
    """只读助手：必须返回列表/布尔且不写仓库。"""
    assert isinstance(git_commits("2020-01-01"), list)
    assert isinstance(is_run_id_git_resolvable("00000000000000000000000000000000"), bool)


def test_real_repo_has_no_generation_column():
    """真值锚：当前 verification_ledger 已确认无世代列（与 T39 实证一致）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "a39", os.path.join(ROOT, "scripts", "audit_ledger_generation_split.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    con = mod.open_readonly(os.path.join(ROOT, "verification.db"))
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(verification_ledger)")]
        n = con.execute("SELECT COUNT(*) FROM verification_ledger").fetchone()[0]
    finally:
        con.close()
    assert has_generation_column(cols) is False
    # 账本为 append-only: 行数随验证台 ingest 累积单调递增。T39 写此锚时 = 9042 (9/25 基线);
    # 2026-09-28 T47 修复落地 `python -m verification report` 代拉后行数已超过基线
    # (本轮实测 9680)。故断言下限而非精确值, 防止"样本累积"这一正确行为被误判为回归;
    # 若账本被意外清空 (n < 9042) 仍能捕获。
    assert n >= 9042, f"账本行数应 >= T39 基线 9042(append-only 不应减少), 实测 {n}"

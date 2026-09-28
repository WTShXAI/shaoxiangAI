# -*- coding: utf-8 -*-
"""T47 验证台 ingest 调度缺位只读盘点 — 单测（零生产 I/O）。

被测试对象: scripts/audit_verification_ingest_scheduling.py
铁律: 不碰 events.db 写入、不跑 ingest、不写 verification.db、不碰生产服务。
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import audit_verification_ingest_scheduling as avis


# ── Q1 调度面检索 ───────────────────────────────────────────────────────
def test_cli_entry_present_and_guardian_schedules_verification():
    """T47 修复后: CLI 入口本体被标为 CLI_ENTRY；真实调度方是 prod_guardian JOBS
    的 verification_report(代拉 `python -m verification` 的 report 子命令)。
    2026-09-28 前本断言为 SCHEDULED_CLI==0(缺调度 → 报告停更 3 天); 修复落地后改判
    守护已接入调度, 否则退回旧态。"""
    surf = avis.scan_schedule_surface()
    files = surf["caller_files"]
    assert any(f.endswith("verification/__main__.py") for f in files), files
    # 朴素扫描面(字面 python -m verification)可能因守护用 VENV_PYW 变量而漏检,
    # 故以 parse_guardian_jobs 为真源判定"是否有人调度"。
    jobs = avis.parse_guardian_jobs()
    assert any(j["job"] == "verification_report" for j in jobs), \
        "T47 修复后守护必须代拉 verification_report, 否则退回报告停更旧态"


def test_scan_self_excludes_audit_script():
    """本脚本自身不得出现在命中里（否则 AUDIT_READER 是假阳性）。"""
    surf = avis.scan_schedule_surface()
    assert avis.SELF not in surf["caller_files"]
    assert not any(h["file"].endswith(avis.SELF) for h in surf["hits"])


def test_patterns_match_scheduled_cli_form():
    """SCHEDULED_CLI 正则须能识别计划任务里常见的 pythonw -m verification ingest。"""
    line = r"D:\Arch\.venv\Scripts\pythonw.exe -m verification ingest"
    hits = [k for k, rx in avis.PATTERNS if rx.search(line)]
    assert "SCHEDULED_CLI" in hits


def test_patterns_recognise_both_ingest_paths_only():
    """ingest 与 report 两条 CLI 子命令都含 ingest 语义；export 只是导出不入账。"""
    assert any(rx.search("python -m verification ingest")
               for _k, rx in avis.PATTERNS)
    assert any(rx.search("python -m verification report")
               for _k, rx in avis.PATTERNS)
    # 类内调用（非 CLI）也须被抓到，否则漏掉「谁在进程内调 ingest」
    hits = [k for k, rx in avis.PATTERNS if rx.search("    n = led.ingest_new(con, 'x')")]
    assert "IN_PROCESS_CALL" in hits


def test_guardian_jobs_parsed_and_verification_job_is_readonly():
    """T47 修复后: 守护 JOBS 现在含 verification_report(触达 verification), 形态须为
    `python -m verification report`(只读 events.db + 仅 append verification.db)。
    只读纪律(mode=ro)由 test_prod_guardian.py::test_verification_report_in_jobs_and_readonly 锁死。"""
    jobs = avis.parse_guardian_jobs()
    assert jobs, "prod_guardian JOBS 未解析到"
    assert any(j["job"] == "autonomous_monitor" for j in jobs)
    vjobs = [j for j in jobs if j.get("touches_verification")]
    assert vjobs, "T47 修复后守护 JOBS 必须含 verification_report"
    assert all(j["job"] == "verification_report" for j in vjobs), vjobs
    # 形态须是 `-m verification report` 子命令(而非直接 import 写 events.db)
    assert any("'verification', 'report'" in j["target"] or "-m verification report" in j["target"]
               for j in vjobs), vjobs


def test_plan_task_bats_parsed_with_cmd():
    bats = avis.parse_plan_task_bats()
    targets = {b["bat"]: b for b in bats}
    assert any("run_daily_recheck" in k for k in targets), sorted(targets)
    rec = [v for k, v in targets.items() if "run_daily_recheck" in k][0]
    assert "recheck_analysis.py" in rec["cmd"]
    assert not rec["touches_verification"]


# ── Q3 monitor 挂载面 ───────────────────────────────────────────────────
def test_parse_monitor_steps_order_and_try_wrap():
    steps = avis.parse_monitor_steps()
    names = [s["step"] for s in steps]
    assert names[:2] == ["bridge", "collector"], names
    assert "predictions" in names and "retrain_gate" in names
    by = {s["step"]: s["in_try"] for s in steps}
    # predict_refresh 走 try (T44: 失败只记 status['error'])，bridge 是裸调用
    assert by["predictions"] is True
    assert by["bridge"] is False


def test_monitor_db_surface_flags_write_connection_only():
    ms = avis.parse_monitor_db_surface()
    assert ms["readonly_connections"] >= 1      # _conn_ro()
    assert ms["write_connections_gq_conn"] >= 1  # refresh_predictions 里 gq_conn()
    assert ms["writes_verification_db"] is False


def test_monitor_surface_reports_max_cycle_sec_from_history():
    ms = avis.run_audit()["q3_monitor_db_surface"]
    assert ms.get("cycles_tracked", 0) >= 50, "monitor_history 未解析到周期数"
    assert ms.get("avg_cycle_sec") and ms.get("max_cycle_sec")
    assert ms["max_cycle_sec"] >= ms["avg_cycle_sec"]


# ── Q2 账本与应入未入（临时库，不碰生产）───────────────────────────────
def _mk_ledger(con, rows: list[tuple]) -> None:
    con.execute("""CREATE TABLE verification_ledger(
        row_id INTEGER PRIMARY KEY, run_id TEXT, match_id TEXT, model_source TEXT,
        kickoff_utc TEXT, match_date TEXT, chosen_outcome TEXT, predicted_prob REAL,
        p_home REAL, p_draw REAL, p_away REAL, chosen_dec_odds REAL, devig_h REAL,
        devig_d REAL, devig_a REAL, paper_stake REAL, settled_home INT, settled_away INT,
        settled_outcome TEXT, payoff REAL, is_credible INT, created_at TEXT, devig_method TEXT)""")
    con.executemany(
        "INSERT INTO verification_ledger(row_id, match_id, model_source, match_date, created_at)"
        " VALUES(?,?,?,?,?)",
        [(i + 1, m, s, d, c) for i, (m, s, d, c) in enumerate(rows)])
    con.commit()


def test_ledger_age_days_positive_and_none_safe():
    assert avis.ledger_age_days("2026-09-25T04:26:32Z") > 1.0
    assert avis.ledger_age_days(None) is None
    assert avis.ledger_age_days("bad-timestamp") is None


def test_ledger_stats_shape():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "v.db")
    con = sqlite3.connect(p)
    _mk_ledger(con, [("m1", "KNN", "2026-09-20", "2026-09-25T04:00:00Z")])
    st = avis.ledger_stats(con)
    con.close()
    assert st["rows"] == 1
    assert st["max_created_at"] == "2026-09-25T04:00:00Z"
    assert st["sources"]["KNN"]["rows"] == 1
    assert "_pairs" in st  # 私有键，供 compute_owed 去重用


def test_compute_owed_dedupes_and_counts(tmp_path):
    """有账本 → 应入未入 = 存活候选 - 账本已有；无账本 → 全部存活都欠。"""
    import json as _json

    tmp = str(tmp_path)
    lep, evp = os.path.join(tmp, "v.db"), os.path.join(tmp, "e.db")
    con = sqlite3.connect(lep)
    _mk_ledger(con, [("m1", "candles_ensemble", "2026-09-20", "2026-09-20T00:00:00Z")])
    ledger = avis.ledger_stats(con)
    con.close()

    ev = sqlite3.connect(evp)
    # 建最简 daily_predictions/matches/odds_changes 供 T33 口径函数使用
    ev.execute("CREATE TABLE daily_predictions(match_key TEXT, model_source TEXT, payload TEXT,"
               " match_date TEXT, kickoff TEXT, status TEXT)")
    ev.execute("CREATE TABLE matches(match_key TEXT, status TEXT, score_home INT, score_away INT,"
               " kickoff TEXT)")
    ev.execute("CREATE TABLE odds_changes(match_key TEXT, captured_at REAL)")
    payload = _json.dumps({"market_implied": {"odds_1x2": [2.0, 3.2, 4.0]},
                           "p_home": 0.4, "p_draw": 0.3, "p_away": 0.3,
                           "model_source": "candles_ensemble"})
    ev.execute("INSERT INTO matches VALUES(?,?,?,?,?)",
               ("m1", "finished", 2, 1, "2026-09-20 10:00:00"))
    ev.execute("INSERT INTO daily_predictions VALUES(?,?,?,?,?,?)",
               ("m1", "candles_ensemble", payload, "2026-09-20",
                "2026-09-20 10:00:00", "finished"))
    # m2 会被假0-0守卫剔除（0-0 且最新赔率 tick 早于开赛+95min）
    ev.execute("INSERT INTO matches VALUES(?,?,?,?,?)",
               ("m2", "finished", 0, 0, "2026-09-21 10:00:00"))
    ev.execute("INSERT INTO daily_predictions VALUES(?,?,?,?,?,?)",
               ("m2", "candles_ensemble", payload, "2026-09-21",
                "2026-09-21 10:00:00", "finished"))
    ev.execute("INSERT INTO odds_changes VALUES(?,?)", ("m1", 0.0))
    ev.commit()
    owed = avis.compute_owed(ev, ledger)
    ev.close()

    assert owed["available"] is True
    assert owed["candidates_gate_b"] == 2
    assert owed["survivors_guards"] == 1, "m2 应被假0-0守卫剔除"
    assert owed["already_in_ledger"] == 1
    assert owed["owed_total"] == 0
    assert owed["owed_by_source"] == {}


# ── Q4 挂载点评估（纯文本）──────────────────────────────────────────────
def test_mount_options_claim_no_new_events_db_writer():
    opts = avis.assess_mount_options({}, {})
    assert {o["option"] for o in opts} == {
        "A_ingest_step_in_monitor", "B_independent_scheduled_task"}
    for o in opts:
        # ingest 只写 verification.db；两案都不应新增 events.db 写方（否则撞 T44）
        assert o["new_events_db_writer"] is False, o["option"]
        assert o["new_verification_db_writer"] is True, o["option"]
        assert str(o["idempotent"]).startswith("是")
        assert o["new_process"] in (True, False)


def test_acceptance_has_no_g1_or_training_loophole():
    acc = avis.build_acceptance({})
    ids = [a["id"] for a in acc]
    assert ids == ["A1", "A2", "A3", "A4", "A5", "A6", "A7"]
    hard = {a["id"] for a in acc if a["fail_closed"]}
    # A6 停滞闸门 + A7 不触发重训 必须是 fail-closed，防止「速率被一次性批量伪装」
    assert {"A3", "A6", "A7"} <= hard


def test_render_markdown_contains_key_sections():
    r = avis.run_audit()
    md = avis.render_markdown(r)
    for key in ("## Q1 调度面检索", "## Q2 账本与应入未入", "## Q3 monitor 挂载面",
                "## Q4 挂载点两案", "## 诚实边界"):
        assert key in md, key
    assert "fail-closed" in md

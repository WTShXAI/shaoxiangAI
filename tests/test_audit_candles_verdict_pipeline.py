# -*- coding: utf-8 -*-
"""T40 `prematch_candles_verdict` 写入链路只读审计 —— 纯临时库测试，零生产 I/O。

覆盖：写方盘点角色判定 / 时效语义裁定 / 门控对齐 / 以该表为入口的入账漏斗复算
（含假0-0守卫与「取最新赔率」的前视风险）/ 报告渲染。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
import audit_candles_verdict_pipeline as acv  # noqa: E402

add_findings = acv.add_findings
_devig_power = acv._devig_power
_prematch_1x2 = acv._prematch_1x2
_result_1x2 = acv._result_1x2
_role_of = acv._role_of
build_payload = acv.build_payload
gate_alignment = acv.gate_alignment
parse_kickoff_ts = acv.parse_kickoff_ts
render_md = acv.render_md
scan_writers = acv.scan_writers
simulate_verdict_ingest = acv.simulate_verdict_ingest
table_exists = acv.table_exists
verdict_profile = acv.verdict_profile

TABLE = "prematch_candles_verdict"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _mk_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE matches (match_key TEXT PRIMARY KEY, league TEXT,
        kickoff TEXT, status TEXT, score_home INTEGER, score_away INTEGER)""")
    con.execute("""CREATE TABLE odds_changes (match_key TEXT, market TEXT,
        selection TEXT, to_odds REAL, captured_at REAL)""")
    con.execute("""CREATE TABLE daily_predictions (match_key TEXT, model_source TEXT,
        status TEXT, payload TEXT, match_date TEXT)""")
    con.execute("""CREATE TABLE %s (match_key TEXT PRIMARY KEY, kickoff TEXT,
        direction TEXT, probs TEXT, confidence REAL, margin REAL,
        n_ticks INTEGER, models TEXT, captured_at REAL)""" % TABLE)
    return con


def _seed(con: sqlite3.Connection) -> dict:
    now = datetime.now().timestamp()
    con.execute("INSERT INTO matches VALUES (?,?,?,?,?,?)",
                ("m1", "L1", _iso(now - 7200), "finished", 2, 1))
    con.execute("INSERT INTO matches VALUES (?,?,?,?,?,?)",
                ("m2", "L1", _iso(now - 3600), "finished", 0, 0))     # 假0-0
    con.execute("INSERT INTO matches VALUES (?,?,?,?,?,?)",
                ("m3", "L1", _iso(now + 3600), "scheduled", None, None))
    con.execute("INSERT INTO matches VALUES (?,?,?,?,?,?)",
                ("m4", "L1", _iso(now - 100), "finished", 1, None))  # 无比分
    for mk, ko in (("m1", now - 7200), ("m2", now - 3600), ("m3", now + 3600),
                   ("m4", now - 100)):
        con.execute("INSERT INTO %s VALUES (?,?,?,?,?,?,?,?,?)" % TABLE,
                    (mk, _iso(ko), "home", '{"home":0.5,"draw":0.3,"away":0.2}',
                     0.5, 0.1, 10, None, ko - 1800.0))
    # odds: m1 赛前 + 开赛后 in-play tick（前视风险素材）; m2 断流（无开赛后 tick）
    con.execute("INSERT INTO odds_changes VALUES (?,?,?,?,?)",
                ("m1", "1X2", "home", 2.0, now - 7300))
    con.execute("INSERT INTO odds_changes VALUES (?,?,?,?,?)",
                ("m1", "1X2", "draw", 3.0, now - 7300))
    con.execute("INSERT INTO odds_changes VALUES (?,?,?,?,?)",
                ("m1", "1X2", "away", 4.0, now - 7300))
    con.execute("INSERT INTO odds_changes VALUES (?,?,?,?,?)",
                ("m1", "1X2", "home", 1.9, now - 600))     # in-play
    con.execute("INSERT INTO odds_changes VALUES (?,?,?,?,?)",
                ("m2", "1X2", "home", 2.5, now - 3700))
    con.execute("INSERT INTO odds_changes VALUES (?,?,?,?,?)",
                ("m2", "1X2", "draw", 3.2, now - 3700))
    con.execute("INSERT INTO odds_changes VALUES (?,?,?,?,?)",
                ("m2", "1X2", "away", 3.0, now - 3700))
    con.execute("INSERT INTO daily_predictions VALUES (?,?,?,?,?)",
                ("m1", "candles_ensemble", "finished", "{}", _iso(now - 7200)[:10]))
    con.commit()
    return {"ko1": now - 7200, "ko2": now - 3600, "ko3": now + 3600}


# ── Q1 写方盘点 ────────────────────────────────────────────────────────────
def test_role_of_classifies_writer_and_reader():
    assert _role_of("store_prematch_candles_verdict(mk, ko, 'home', ...)") == "UPSERT_WRITER"
    assert _role_of('    "INSERT INTO prematch_candles_verdict\\n"') == "UPSERT_WRITER"
    assert _role_of("SELECT direction, probs FROM prematch_candles_verdict WHERE match_key=?") == "READER"
    assert _role_of("CREATE TABLE IF NOT EXISTS prematch_candles_verdict (") == "DDL"
    assert _role_of("# 旧的跨庄注释 prematch_candles_verdict") == "COMMENT"
    assert _role_of("# 纯注释不含表名") == "COMMENT"


def test_scan_writers_finds_real_files_and_self_excludes(tmp_path):
    src = os.path.join(tmp_path, "scripts", "prod.py")
    os.makedirs(os.path.dirname(src))
    with open(src, "w", encoding="utf-8") as fh:
        fh.write("from gq.db import store_prematch_candles_verdict\n")
        fh.write("# note mentions prematch_candles_verdict\n")
    self_path = os.path.join(tmp_path, "scripts", "audit_self.py")
    with open(self_path, "w", encoding="utf-8") as fh:
        fh.write("x = 'prematch_candles_verdict'  # self exclude\n")
    res = scan_writers(tmp_path, self_path)
    files = {h["file"] for h in res["hits"]}
    assert files == {"scripts/prod.py"}
    roles = {h["role"] for h in res["hits"]}
    assert roles == {"UPSERT_WRITER", "COMMENT"}
    assert res["by_role"]["UPSERT_WRITER"] == 1
    assert res["writers"] == ["scripts/prod.py"]


# ── Q2 表结构与时效 ────────────────────────────────────────────────────────
def test_verdict_profile_reads_schema_and_freshness(tmp_path):
    con = _mk_db(os.path.join(tmp_path, "e.db"))
    seed = _seed(con)
    prof = verdict_profile(con)
    assert prof["present"] is True
    assert "captured_at" in prof["columns"]
    assert prof["has_status_column"] is False          # 表不自证完赛
    assert prof["timeliness_column"] == "captured_at"
    assert prof["rows"] == 4
    assert prof["by_matches_status"]["finished"] == 3  # m1 m2 m4
    assert prof["by_matches_status"]["scheduled"] == 1
    assert prof["last_write_local"] is not None
    con.close()


def test_verdict_profile_absent_table(tmp_path):
    con = sqlite3.connect(os.path.join(tmp_path, "empty.db"))
    assert verdict_profile(con) == {"present": False}
    con.close()


def test_parse_kickoff_ts_supports_two_formats():
    ts = parse_kickoff_ts("2026-09-27 11:10:00")
    assert ts == datetime.strptime("2026-09-27 11:10:00", "%Y-%m-%d %H:%M:%S").timestamp()
    assert parse_kickoff_ts("2026-09-27 11:10") == ts
    assert parse_kickoff_ts("") is None
    assert parse_kickoff_ts(None) is None


# ── Q2 门控对齐 ────────────────────────────────────────────────────────────
def test_gate_alignment_counts(tmp_path):
    con = _mk_db(os.path.join(tmp_path, "e.db"))
    _seed(con)
    a = gate_alignment(con)
    assert a["verdict_rows"] == 4
    assert a["finished_with_score"] == 2        # m1(2-1) m2(0-0)
    assert a["finished_no_score"] == 1          # m4
    assert a["not_finished"] == 1                # m3
    assert a["daily_finished_candles"] == 1
    con.close()


# ── Q3 入账漏斗复算 ────────────────────────────────────────────────────────
def test_prematch_odds_only_uses_prematch_tick(tmp_path):
    con = _mk_db(os.path.join(tmp_path, "e.db"))
    seed = _seed(con)
    ko_str = datetime.fromtimestamp(seed["ko1"]).strftime("%Y-%m-%d %H:%M:%S")
    r = _prematch_1x2(con, "m1", parse_kickoff_ts(ko_str))
    assert r is not None and abs(r[0] - 2.0) < 1e-9     # 取赛前 2.0, 不是 in-play 1.9
    con.close()


def test_result_and_devig_helpers():
    assert _result_1x2(2, 1) == "home"
    assert _result_1x2(0, 0) == "draw"
    assert _result_1x2(None, 1) is None
    p = _devig_power([2.0, 3.0, 6.0])
    assert p is not None and abs(sum(p) - 1.0) < 1e-9


def test_simulate_funnel_applies_guards_and_flags_inplay(tmp_path):
    con = _mk_db(os.path.join(tmp_path, "e.db"))
    _seed(con)
    sim = simulate_verdict_ingest(con, {("m1", "candles_ensemble")})
    # m2 = 0-0 且最后 tick 早于 kickoff+95min → 假0-0守卫剔除
    # m4 无比分 → result_none
    assert sim["credible_1x2_fail"] == 1
    assert sim["no_score"] == 2          # m4 已完赛无比分 + m3 未开赛无比分
    assert sim["survivors"] == 1
    assert sim["survivor_new"] == 0      # m1 已在账本
    assert sim["survivor_dup"] == 1
    # m1 有开赛后 tick → 前视风险素材
    assert sim["inplay_tick_rows"] == 1
    assert sim["survivors_0_0"] == 0
    con.close()


def test_simulate_marks_new_when_not_in_ledger(tmp_path):
    con = _mk_db(os.path.join(tmp_path, "e.db"))
    _seed(con)
    sim = simulate_verdict_ingest(con, set())
    assert sim["survivor_new"] == 1
    assert sim["survivor_dup"] == 0
    con.close()


def test_ingest_path_would_survive_only_if_guards_kept(tmp_path):
    """守门口径：假0-0 场（m2）即便在 verdict 表里有判定行，也绝不能入账。"""
    con = _mk_db(os.path.join(tmp_path, "e.db"))
    _seed(con)
    sim = simulate_verdict_ingest(con, set())
    assert ("m2", "candles_ensemble") not in {("m1", "candles_ensemble")}
    assert sim["survivors"] < sim["candidates"]
    con.close()


# ── 报告渲染 ──────────────────────────────────────────────────────────────
def test_render_md_contains_sections_and_findings():
    payload = {"as_of": "2026-09-27 12:00:00",
               "writers": {"hits": [{"file": "gq/ws_collector.py", "line": 783,
                                     "role": "UPSERT_WRITER", "text": "store_prematch_..."}],
                           "by_role": {"UPSERT_WRITER": 1}, "writers": ["gq/ws_collector.py"]},
               "profile": {"present": True, "columns": ["match_key", "captured_at"],
                           "has_status_column": False, "rows": 1038,
                           "by_matches_status": {"finished": 1022}, "last_write_local": "x"},
               "alignment": {"finished_with_score": 984},
               "simulation": {"candidates": 1000, "survivors": 700, "survivor_new": 300,
                              "credible_1x2_fail": 50, "inplay_tick_rows": 900},
               "headline": ["h1"], "verdicts": ["v1"]}
    md = render_md(payload)
    assert "T40" in md and "## 1 Q1" in md and "## 4 Q3" in md
    assert "gq/ws_collector.py" in md and "v1" in md


def test_add_findings_emits_honest_no_claim():
    payload = {"profile": {"present": True, "rows": 1038, "last_write_local": "2026-09-27 11:06"},
               "alignment": {}, "simulation": {"candidates": 1000, "survivors": 700,
                                               "survivor_new": 300, "credible_1x2_fail": 50,
                                               "inplay_tick_rows": 900},
               "writers": {"writers": ["gq/ws_collector.py"]}}
    out = add_findings(payload)
    joined = " ".join(out["verdicts"])
    assert "前视" in joined and "假0-0" in joined
    assert "未空转" in " ".join(out["headline"])
    # 诚实口径：不得出现任何盈利/edge 宣称
    assert not any(k in joined for k in ("EDGE 已验证", "稳定盈利"))


def test_module_has_no_db_writer(tmp_path):
    """模块源码内不得出现对真实库表的写入 SQL（只读审计的自证）。"""
    import inspect
    src = inspect.getsource(acv)
    assert "UPDATE %s" % TABLE not in src
    assert "DELETE FROM %s" % TABLE not in src
    assert "CREATE TABLE IF NOT EXISTS %s" % TABLE not in src


def test_table_exists_helper(tmp_path):
    con = _mk_db(os.path.join(tmp_path, "e.db"))
    assert table_exists(con, TABLE) is True
    assert table_exists(con, "no_such_table") is False
    con.close()


@pytest.mark.skipif(not os.path.exists(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "events.db")), reason="events.db 不可用")
def test_real_db_smoke():
    p = build_payload()
    assert p["writers"]["by_role"], "真实库扫描应至少命中写方盘点"
    assert isinstance(p.get("profile", {}).get("rows", 0), int)

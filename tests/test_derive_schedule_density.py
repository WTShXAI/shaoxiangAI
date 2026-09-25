#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_derive_schedule_density.py — T03 单测 (临时库, 不碰 events.db 生产)。

验证: load_matches 只读 / derive_density 7日窗口计数正确 / write_isolated 幂等写隔离库。
"""

import os
import sys
import sqlite3
import tempfile
import importlib.util

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

_spec = importlib.util.spec_from_file_location(
    "derive_schedule_density",
    os.path.join(REPO, "scripts", "derive_schedule_density.py"))
dsd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dsd)


def _make_events_db(path):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE matches ("
        "  match_key TEXT, home TEXT, away TEXT, kickoff TEXT,"
        "  league TEXT, status TEXT, score_home INT, score_away INT)"
    )
    rows = [
        # match_key, home, away, kickoff,            league, status
        ("M1", "A", "B", "2026-01-10 20:00", "L", "finished"),
        ("M2", "A", "C", "2026-01-05 20:00", "L", "finished"),  # A, 7d内 before M1
        ("M3", "B", "D", "2026-01-08 20:00", "L", "finished"),  # B, 7d内 before M1
        ("M4", "A", "E", "2026-01-01 20:00", "L", "finished"),  # A, 9d前, 窗口外
        ("M5", "F", "G", "2026-02-01 20:00", "L", "finished"),  # 无关
    ]
    con.executemany(
        "INSERT INTO matches (match_key, home, away, kickoff, league, status) "
        "VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def test_load_matches_readonly():
    with tempfile.TemporaryDirectory() as d:
        ev = os.path.join(d, "events.db")
        _make_events_db(ev)
        ms = dsd.load_matches(ev)
        keys = {m[0] for m in ms}
        assert keys == {"M1", "M2", "M3", "M4", "M5"}


def test_derive_density_window_7d():
    with tempfile.TemporaryDirectory() as d:
        ev = os.path.join(d, "events.db")
        _make_events_db(ev)
        ms = dsd.load_matches(ev)
        dens = dsd.derive_density(ms, window_days=7)
        # M1: A 在窗口内有 M2(1/5) -> 1; B 有 M3(1/8) -> 1
        assert dens["M1"] == (1, 1)
        # M4 (A vs E, 1/1): A 窗口 [12/25..1/1) 内无其他 A 场 -> 0
        assert dens["M4"] == (0, 0)
        # M5 孤立 -> (0,0)
        assert dens["M5"] == (0, 0)


def test_write_isolated_idempotent():
    with tempfile.TemporaryDirectory() as d:
        ev = os.path.join(d, "events.db")
        out = os.path.join(d, "schedule_density.db")
        _make_events_db(ev)
        ms = dsd.load_matches(ev)
        dens = dsd.derive_density(ms, window_days=7)
        n1 = dsd.write_isolated(out, ms, dens, dry_run=False)
        assert n1 == 5
        # 重跑应幂等(INSERT OR REPLACE)
        n2 = dsd.write_isolated(out, ms, dens, dry_run=False)
        assert n2 == 5
        con = sqlite3.connect(out)
        cnt = con.execute("SELECT COUNT(*) FROM schedule_density").fetchone()[0]
        row = con.execute(
            "SELECT home_density_7d, away_density_7d FROM schedule_density "
            "WHERE match_key='M1'").fetchone()
        con.close()
        assert cnt == 5
        assert row == (1, 1)


def test_dry_run_no_write():
    with tempfile.TemporaryDirectory() as d:
        ev = os.path.join(d, "events.db")
        out = os.path.join(d, "schedule_density.db")
        _make_events_db(ev)
        ms = dsd.load_matches(ev)
        dens = dsd.derive_density(ms, window_days=7)
        n = dsd.write_isolated(out, ms, dens, dry_run=True)
        assert n == 5
        assert not os.path.exists(out)   # dry-run 不落盘

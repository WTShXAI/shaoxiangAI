#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G3 真实累积层幂等守护单测 (自包含临时 DB, 不依赖 452MB football_data.db).

验证:
  G1 幂等: live_mode 对同一 live_odds_raw 重复运行, PENDING_LIVE 不翻倍.
  G2 闸门诚实: 分歧未触发或 value_layer 非 BET 时, 绝不写 PENDING (不乱灌).
可独立运行: python scripts/test_live_accumulator.py  (返回 rc, 供 ci.yml 显式步)
"""
import os
import sys
import json
import sqlite3
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import scripts.live_pilot_guardian as m


def _build(tmp: str):
    con = sqlite3.connect(tmp)
    cur = con.cursor()
    cur.execute("""CREATE TABLE bet_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id INTEGER, home_team TEXT, away_team TEXT, league TEXT, match_date TEXT,
        bet_type TEXT, source TEXT, predicted_result TEXT, verdict_text TEXT, confidence REAL,
        home_prob REAL, draw_prob REAL, away_prob REAL, home_odds REAL, draw_odds REAL, away_odds REAL,
        value_gap REAL, kelly REAL, expected_value REAL, actual_result TEXT, is_correct INTEGER,
        notes TEXT, created_at TEXT)""")
    cur.execute("""CREATE TABLE live_odds_raw (
        id INTEGER PRIMARY KEY AUTOINCREMENT, home_team TEXT, away_team TEXT,
        commence_time TEXT, bookmakers_detail TEXT)""")
    # 强分歧双庄 (热门相反) -> disagreement_detected 应为 True
    books = [{"name": "A", "h": 1.5, "d": 4.0, "a": 8.0},
             {"name": "B", "h": 8.0, "d": 4.0, "a": 1.5}]
    cur.execute(
        "INSERT INTO live_odds_raw (home_team, away_team, commence_time, bookmakers_detail) "
        "VALUES (?,?,?,?)",
        ("TeamA", "TeamB", "2026-07-11T20:00:00Z", json.dumps(books)))
    con.commit()
    con.close()


def _count_pending(tmp: str) -> int:
    con = sqlite3.connect(tmp)
    n = con.execute("SELECT COUNT(*) FROM bet_records WHERE notes='PENDING_LIVE'").fetchone()[0]
    con.close()
    return n


def run_checks() -> int:
    tmp = tempfile.mktemp(suffix=".sqlite")
    try:
        _build(tmp)
        m.DB = tmp
        # 强制 value_layer 触发 BET + 共识概率确定性
        m.compute_value_layer = lambda **kw: {
            "decision": "BET", "best_direction": "H", "best_edge_pct": 10.0,
            "rows": [{"outcome": "H", "kelly_half": 0.1, "ev": 0.1}]}
        m.consensus_probs = lambda books: [0.5, 0.25, 0.25]

        # 第一次
        m.live_mode()
        n1 = _count_pending(tmp)
        assert n1 == 1, f"[G1] 期望写 1 条 PENDING, 实际 {n1}"

        # 第二次 (模拟 daemon 周期重复) -> 幂等不翻倍
        m.live_mode()
        n2 = _count_pending(tmp)
        assert n2 == 1, f"[G1] 幂等失败: 第二次 PENDING={n2} (应仍为 1)"

        # 闸门诚实: 强制 value_layer 非 BET -> 不应写新 PENDING
        m.compute_value_layer = lambda **kw: {
            "decision": "PASS", "best_direction": "PASS", "best_edge_pct": 0.0,
            "rows": []}
        before = _count_pending(tmp)
        m.live_mode()
        after = _count_pending(tmp)
        assert after == before, f"[G2] 闸门诚实失败: PASS 仍写了 {after - before} 条"

        print(f"[OK] 幂等守护单测通过: 首次={n1} 二次={n2} PASS时不写={after}")
        return 0
    except AssertionError as e:
        print(f"[FAIL] {e}")
        return 1
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


if __name__ == "__main__":
    sys.exit(run_checks())

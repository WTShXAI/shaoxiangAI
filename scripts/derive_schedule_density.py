#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/derive_schedule_density.py — 赛程密度(schedule_density)离线派生 (P-SNAPSHOT-data 备料)

用途: 由 events.db 的 matches 表, 按 team + 开赛前 window 日内的场次计数, 派生"赛程密度"
      (疲劳代理), 输出到**隔离库** data/schedule_density.db。

纪律红线(本脚本严守):
- events.db **只读** (mode=ro 打开), 零生产写入, 不杀进程, 不 VACUUM。
- 输出仅写隔离库; 本脚本**不**把结果接回 match_snapshot.freeze() ("不应用" — 留待维护窗口)。
- 默认 dry-run(仅报告将派生多少场); --apply 才写隔离库。

队列编号: T03 (自动化 dbda4380 每 10 分钟 pull 一项执行并打勾)。
关联: docs/WINDOW_PREP.md §1, docs/AUTONOMOUS_BACKLOG.md T03.
"""

import sys
import os
import argparse
import sqlite3
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WINDOW_DAYS = 7
OUT_DB_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "schedule_density.db")


def _kickoff_ts(kickoff: str):
    """解析 kickoff(ISO '%Y-%m-%d %H:%M') 为 unix 秒; 失败返回 None。"""
    if not kickoff:
        return None
    try:
        return datetime.strptime(kickoff[:16], "%Y-%m-%d %H:%M").timestamp()
    except Exception:
        return None


def load_matches(events_db_path: str) -> list:
    """只读 events.db, 返回 [(match_key, home, away, kickoff_ts)]。"""
    con = sqlite3.connect(f"file:{events_db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT match_key, home, away, kickoff FROM matches "
            "WHERE kickoff IS NOT NULL AND kickoff != ''"
        ).fetchall()
    finally:
        con.close()
    out = []
    for mk, h, a, ko in rows:
        ts = _kickoff_ts(ko)
        if ts is None:
            continue
        out.append((mk, h, a, ts))
    return out


def derive_density(matches: list, window_days: int = WINDOW_DAYS) -> dict:
    """对每场计算主/客队开赛前 window 日内的其他场次计数。

    返回 {match_key: (home_density, away_density)}。联赛/中立场未区分(同队即计数)。
    """
    win = window_days * 86400.0
    # team -> [(ts, match_key)]
    by_team = {}
    for mk, h, a, ts in matches:
        for team in (h, a):
            by_team.setdefault(team, []).append((ts, mk))

    result = {}
    for mk, h, a, ts in matches:
        def _count(team):
            cnt = 0
            for ots, omk in by_team.get(team, []):
                if omk == mk:
                    continue
                if ts - win <= ots < ts:   # 开赛前 window 日内, 不含本场
                    cnt += 1
            return cnt
        result[mk] = (_count(h), _count(a))
    return result


def write_isolated(out_db_path: str, matches: list, density: dict, dry_run: bool) -> int:
    """写隔离库 data/schedule_density.db。返回写入行数。dry_run 时不写。"""
    n = 0
    if dry_run:
        return len(density)
    con = sqlite3.connect(out_db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS schedule_density ("
            "  match_key TEXT PRIMARY KEY,"
            "  kickoff TEXT, home TEXT, away TEXT,"
            "  home_density_7d INT, away_density_7d INT,"
            "  computed_at REAL)"
        )
        for mk, h, a, ts in matches:
            hd, ad = density.get(mk, (0, 0))
            ko_iso = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            con.execute(
                "INSERT OR REPLACE INTO schedule_density "
                "(match_key, kickoff, home, away, home_density_7d, away_density_7d, computed_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (mk, ko_iso, h, a, hd, ad, time.time()),
            )
            n += 1
        con.commit()
    finally:
        con.close()
    return n


def main():
    ap = argparse.ArgumentParser(description="Derive schedule_density (read-only over events.db)")
    ap.add_argument("--events", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "events.db"))
    ap.add_argument("--out", default=OUT_DB_DEFAULT)
    ap.add_argument("--window", type=int, default=WINDOW_DAYS)
    ap.add_argument("--apply", action="store_true",
                    help="写隔离库; 默认 dry-run(仅报告)")
    args = ap.parse_args()

    matches = load_matches(args.events)
    density = derive_density(matches, args.window)
    written = write_isolated(args.out, matches, density, dry_run=not args.apply)

    if args.apply:
        print(f"[schedule_density] applied: {written} matches -> {args.out}")
    else:
        print(f"[schedule_density] dry-run: {written} matches would be derived "
              f"(window={args.window}d). Use --apply to write isolated db.")
    # 简要统计
    if density:
        vals = list(density.values())
        avg_h = sum(v[0] for v in vals) / len(vals)
        avg_a = sum(v[1] for v in vals) / len(vals)
        print(f"[schedule_density] avg home_density={avg_h:.2f} avg away_density={avg_a:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

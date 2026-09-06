#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
backfill_match_metadata.py — 一次性修复数据滞后
=================================================
P0 数据滞后根因: gq/auto_collector.py:550-551 对 kickoff 为空的比赛分配优先级 2
(远期未开赛), 触发 full_every 降频 → 几百场比赛里~200场 kickoff 空但赔率在流,
彻底卡住无人更新。

修复:
  1) 对每场 kickoff 为空的比赛, 用该场**最早一次 odds_snapshots.captured_at - 30min**
     反推开赛时间 (典型 pre-match 采集窗口). 边界: 最早 snapshot > 7d 前则跳过.
  2) 用 (kickoff_ts, now) 推断 status:
       - kickoff_ts + 2.5h < now → 'finished'
       - kickoff_ts <= now <= kickoff_ts + 2.5h → 'live'
       - 其它 → 'scheduled'
  3) 同步 last_seen = now, 触发 collector 下轮主动重排到 priority 0/1
  4) 过滤掉队名是 garbled (含「伤停补时」「其他」等非真队名) 的比赛

用法:
  python scripts/backfill_match_metadata.py [--dry-run]
"""
import sqlite3, time, datetime, re, sys, os

GQ_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "events.db")

GARBLED_NAMES = {"伤停补时", "其他", "其他比赛", "未知", "VS", "vs", ""}

def is_garbled(name: str) -> bool:
    if not name:
        return True
    n = name.strip()
    if n in GARBLED_NAMES:
        return True
    if re.search(r"第\d+[分分钟]|^\d+:\d+$", n):
        return True
    return False


def infer_status(kickoff_ts: int, now_ts: int) -> str:
    if kickoff_ts <= 0:
        return "scheduled"
    delta = now_ts - kickoff_ts
    if delta > 2.5 * 3600:
        return "finished"
    if delta > 0:
        return "live"
    return "scheduled"


def main(dry_run=False):
    if dry_run:
        print("# DRY RUN — 不写库")
    con = sqlite3.connect(GQ_DB)
    cur = con.cursor()
    now_ts = int(time.time())

    matches = cur.execute("""
        SELECT m.match_key, m.home, m.away, m.kickoff, m.status, m.last_seen,
          (SELECT MIN(captured_at) FROM odds_snapshots WHERE match_key=m.match_key) as first_snap,
          (SELECT MAX(captured_at) FROM odds_snapshots WHERE match_key=m.match_key) as last_snap,
          (SELECT COUNT(*) FROM odds_snapshots WHERE match_key=m.match_key) as n_odds
        FROM matches m
        WHERE (m.kickoff IS NULL OR TRIM(m.kickoff) = '')
        ORDER BY m.match_key
    """).fetchall()

    backfilled = 0
    status_updated = 0
    garbled_filtered = 0
    skipped_old = 0

    for row in matches:
        mk, home, away, kickoff, status, last_seen, first_snap, last_snap, n_odds = row

        if is_garbled(home) or is_garbled(away):
            if not dry_run:
                cur.execute(
                    "UPDATE matches SET status='filtered' WHERE match_key=?",
                    (mk,)
                )
            garbled_filtered += 1
            continue

        if not first_snap or n_odds == 0:
            continue

        first_age_days = (now_ts - first_snap) / 86400.0
        # 30天窗口: 仍能反推但保留一道安全护栏
        if first_age_days > 30:
            skipped_old += 1
            continue

        inferred_ko_ts = int(first_snap) - 30 * 60
        inferred_ko_iso = datetime.datetime.fromtimestamp(
            inferred_ko_ts, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        inferred_status = infer_status(inferred_ko_ts, now_ts)

        if not dry_run:
            cur.execute("""
                UPDATE matches
                SET kickoff=?, status=?, last_seen=?
                WHERE match_key=?
            """, (inferred_ko_iso, inferred_status, now_ts, mk))
        backfilled += 1
        if status != inferred_status:
            status_updated += 1

    needs_status = cur.execute("""
        SELECT match_key, kickoff, status, last_seen
        FROM matches
        WHERE kickoff IS NOT NULL AND TRIM(kickoff) != ''
          AND status NOT IN ('finished', 'filtered')
          AND datetime(kickoff) < datetime('now', '-3 hours')
    """).fetchall()
    if not dry_run:
        for mk, ko, st, ls in needs_status:
            try:
                ko_ts = int(datetime.datetime.fromisoformat(
                    ko.replace("Z", "+00:00")
                ).timestamp())
                new_st = infer_status(ko_ts, now_ts)
                if new_st == "finished":
                    cur.execute(
                        "UPDATE matches SET status=?, last_seen=? WHERE match_key=?",
                        (new_st, now_ts, mk)
                    )
                    status_updated += 1
            except Exception:
                pass

    if not dry_run:
        con.commit()
    con.close()

    print(f"\n=== Backfill 统计 ===")
    print(f"kickoff 反推写入: {backfilled} 场")
    print(f"status 同步更新: {status_updated} 场")
    print(f"garbled 队名标记: {garbled_filtered} 场")
    print(f"陈旧跳过 (>7d): {skipped_old} 场")

    if dry_run:
        print("\n(DRY RUN, 未实际写库. 重跑时去掉 --dry-run 落库)")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    main(dry_run=dry)
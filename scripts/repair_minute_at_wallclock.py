#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""历史 minute_at 污染修复 — 墙钟口径批量回填 (2026-09-10).

背景: 2026-09-08 前的采集端分钟污染(45/90 占位 + 垃圾 mmp)让
odds_snapshots.minute_at 大面积失真(实测全库 61.8% 卡 45/90, 另有
垃圾 6/7 穿插)。采集端已根治(gq/auto_collector._rescue_missing_scores
+ ws_collector 消毒), 但历史快照仍带着脏 minute_at — 污染任何按
分钟窗口检索的回测(HT 窗口 38-52' / 滚球 55-65' 等)。

算法 (SSoT: kickoff 墙钟, 与 ws_collector._sanitize_minute 同口径):
  对每条快照, wallclock_minute = 三段映射(captured_at - kickoff):
    elapsed ≤ 47min → floor(elapsed); 47-62min → 45; >62 → 45+elapsed-62
  仅当 |现值 - 墙钟值| > 5 才更新(保留 feed 真值, 只修垃圾)。
  captured_at - kickoff < 0 (开赛前快照) → minute_at 归 0。

安全: 分批提交(每批一个 match_key), 限速, 尊重只读期; 默认 dry-run。
⚠ 大表(67M 行)批量写会撑 WAL — 建议在采集空闲窗口执行, 或加 --limit
  小批试跑。可选 --finished-only 只修完赛场(避开正在采集的比赛)。

用法:
  python scripts/repair_minute_at_wallclock.py                    # dry-run 报告
  python scripts/repair_minute_at_wallclock.py --apply --finished-only
  python scripts/repair_minute_at_wallclock.py --apply --limit 500
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, r'D:\Architecture')

DB = r'D:\Architecture\data\events.db'
BATCH_COMMIT = 200      # 每处理 N 个 match 提交一次
TOL = 5                 # 与墙钟差 >5 分钟才算污染


def _parse_kickoff(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt.timestamp()
    except Exception:
        return None


def wallclock_minute(elapsed_min):
    """墙钟流逝(分钟) → 比赛分钟 (三段映射, 与采集端同口径)。"""
    if elapsed_min <= 47:
        return max(0, int(elapsed_min))
    if elapsed_min <= 62:
        return 45
    return min(120, 45 + int(elapsed_min) - 62)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--finished-only', action='store_true', help='只修完赛场(避开采集中的比赛)')
    ap.add_argument('--limit', type=int, default=0, help='最多处理 N 个 match (0=不限)')
    args = ap.parse_args()

    con = sqlite3.connect(DB, timeout=60)
    con.execute('PRAGMA busy_timeout=30000')
    where = "WHERE kickoff IS NOT NULL AND kickoff != ''"
    if args.finished_only:
        where += " AND status='finished'"
    matches = con.execute(f"SELECT match_key, kickoff FROM matches {where}").fetchall()
    if args.limit:
        matches = matches[:args.limit]
    print(f'目标 match: {len(matches)} 个 (apply={args.apply}, finished_only={args.finished_only})')

    fixed_total = scanned = 0
    t0 = time.time()
    buf = []
    for mi, (mk, ko) in enumerate(matches):
        kots = _parse_kickoff(ko)
        if not kots:
            continue
        rows = con.execute(
            "SELECT id, minute_at, captured_at FROM odds_snapshots WHERE match_key=? AND minute_at > 0",
            (mk,)).fetchall()
        for rid, cur, cap in rows:
            elapsed = (cap - kots) / 60.0
            want = 0 if elapsed < 0 else wallclock_minute(elapsed)
            scanned += 1
            if abs(int(cur) - want) > TOL:
                buf.append((want, rid))
        if len(buf) >= 20000:
            if args.apply:
                con.executemany("UPDATE odds_snapshots SET minute_at=? WHERE id=?", buf)
                con.commit()
            fixed_total += len(buf)
            buf = []
            print(f'  [{mi+1}/{len(matches)}] 累计污染 {fixed_total} 条 ({time.time()-t0:.0f}s)')
        if mi % 500 == 499:
            time.sleep(0.05)   # 限速让位采集
    if buf:
        if args.apply:
            con.executemany("UPDATE odds_snapshots SET minute_at=? WHERE id=?", buf)
            con.commit()
        fixed_total += len(buf)

    print(f'{"✅ 已修复" if args.apply else "(dry-run) 可修复"}: {fixed_total} 条 / 扫描 {scanned} 条, '
          f'耗时 {time.time()-t0:.0f}s')
    if not args.apply:
        print('加 --apply 写库; 大表首次执行建议 --finished-only + 采集空闲窗口')
    con.close()


if __name__ == '__main__':
    main()

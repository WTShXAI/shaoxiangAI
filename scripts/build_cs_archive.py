#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""赛前波胆(CS)赔率归档 + 赛果验证 — 历史回填/刷新脚本。

用法:
  python scripts/build_cs_archive.py            # 干跑: 统计覆盖
  python scripts/build_cs_archive.py --apply    # 写入: 回填 pre_match_cs + cs_verification

逻辑:
  - 未开赛(scheduled)比赛 → freeze_pre_match_cs (赛前CS盘口冻结, 已开赛不采)
  - 已完场(finished)且有比分 → verify_cs (取赛前盘口按实际比分验证归档)
  - 类似31K: 赛后可在 cs_verification 按联赛/日期/比分/命中查询历史验证
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gq.db import (ensure_cs_tables, freeze_pre_match_cs, verify_cs,
                   query_pre_match_cs, query_cs_verification)

import sqlite3

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "events.db")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写入(默认干跑)")
    ap.add_argument("--limit", type=int, default=0, help="仅处理前N场(调试)")
    args = ap.parse_args()

    ensure_cs_tables()
    print(f"[build_cs_archive] apply={args.apply}")

    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row

    # 1) 未开赛 → 冻结赛前CS
    sched = c.execute("SELECT match_key FROM matches WHERE status='scheduled'").fetchall()
    n_frozen = 0
    if args.apply:
        for (mk,) in sched:
            if freeze_pre_match_cs(mk):
                n_frozen += 1
            if args.limit and n_frozen >= args.limit:
                break
    print(f"  未开赛比赛: {len(sched)} 场 | 冻结赛前CS: {n_frozen} 场")

    # 2) 已完场+比分 → 验证归档
    fin = c.execute(
        "SELECT match_key FROM matches WHERE status='finished' AND score_home IS NOT NULL"
    ).fetchall()
    n_verified = 0
    n_no_market = 0
    if args.limit:
        fin = fin[:args.limit]
    if args.apply:
        t0 = time.time()
        for (mk,) in fin:
            vr = verify_cs(mk, source='backfill')
            if vr is None:
                n_no_market += 1
            else:
                n_verified += 1
        dt = time.time() - t0
        print(f"  已完场(有比分): {len(fin)} 场 | 验证归档: {n_verified} | 无赛前盘口跳过: {n_no_market}")
        print(f"  耗时: {dt:.1f}s")
    else:
        print(f"  已完场(有比分): {len(fin)} 场 (干跑, 待 --apply 验证)")

    c.close()

    # 3) 汇总
    pre = query_pre_match_cs(limit=1)
    ver = query_cs_verification(limit=1)
    print(f"\n[汇总] pre_match_cs 现存(未开赛冻结): {len(query_pre_match_cs(limit=100000))} 场")
    print(f"[汇总] cs_verification 现存(历史验证): {len(query_cs_verification(limit=100000))} 场")

    if not args.apply:
        print("\n(干跑模式, 未写入。加 --apply 执行回填)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""match_outcomes 存量数据保真修复 (一次性 + 可重复执行, 幂等).

背景
----
`gq/db.py::record_match_outcome` 早期幂等逻辑为「mid 已存在即 return」,
而采集器在 `mlet` 短暂异常时会把「尚未真正完场」的比赛判成 finished,
于是抢跑归档了中途比分(如 1-0). 之后比赛继续踢到 2-0, `matches` 表随实时
更新, 但 `match_outcomes` 永久锁死在错误比分上.

`match_outcomes` 是训练/回测的唯一标注源 => 错误赛果会直接污染模型.

本脚本做两件事 (均以 `matches` 表为权威源, 按 mid join):
  1. 赛果校正: matches 总进球 > outcomes 总进球 时, 回填 score + result.
     进球只增不减, 故只单调向上校正, 绝不回退(防脏数据倒灌).
  2. 半场比分回填: outcomes.ht_score 为空且 matches.ht_score 可用时补齐.

默认 dry-run 只打印差异; 加 --apply 才真正写库.

用法
----
    python scripts/fix_outcome_drift.py            # 预览
    python scripts/fix_outcome_drift.py --apply    # 执行
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "events.db")


def _result_of(sh: int, sa: int) -> str:
    if sh > sa:
        return "home"
    if sh == sa:
        return "draw"
    return "away"


def scan_score_drift(cur) -> list:
    """返回 outcomes 比分落后于 matches 的场次 (单调向上才算)。"""
    rows = cur.execute("""
        SELECT m.mid, m.home, m.away, m.status,
               m.score_home, m.score_away,
               o.score_home, o.score_away, o.result
        FROM matches m
        JOIN match_outcomes o ON o.mid = m.mid
        WHERE m.score_home IS NOT NULL AND m.score_away IS NOT NULL
          AND o.score_home IS NOT NULL AND o.score_away IS NOT NULL
          AND (m.score_home + m.score_away) > (o.score_home + o.score_away)
        ORDER BY m.mid
    """).fetchall()
    return rows


def scan_reverse_drift(cur) -> list:
    """outcomes 比分 > matches 比分 (反向异常, 多为腰斩/数据重置)。

    进球不可能减少, 故这类差异必有一侧是脏数据, 无法自动判定权威源,
    只报告不修改, 交人工确认。
    """
    return cur.execute("""
        SELECT m.mid, m.home, m.away, m.status, m.minute,
               m.score_home, m.score_away, o.score_home, o.score_away, o.result
        FROM matches m
        JOIN match_outcomes o ON o.mid = m.mid
        WHERE m.score_home IS NOT NULL AND o.score_home IS NOT NULL
          AND (m.score_home + m.score_away) < (o.score_home + o.score_away)
        ORDER BY m.mid
    """).fetchall()


def scan_ht_missing(cur) -> list:
    """返回 outcomes 缺半场比分但 matches 有的场次。"""
    rows = cur.execute("""
        SELECT m.mid, m.home, m.away,
               m.ht_score_home, m.ht_score_away,
               m.score_home, m.score_away
        FROM matches m
        JOIN match_outcomes o ON o.mid = m.mid
        WHERE m.ht_score_home IS NOT NULL AND m.ht_score_away IS NOT NULL
          AND o.ht_score_home IS NULL
        ORDER BY m.mid
    """).fetchall()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="match_outcomes 保真修复")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="真正写库(默认仅预览)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[ERR] DB 不存在: {args.db}")
        return 1

    c = sqlite3.connect(args.db, timeout=30)
    cur = c.cursor()

    drift = scan_score_drift(cur)
    reverse = scan_reverse_drift(cur)
    ht_miss = scan_ht_missing(cur)

    print(f"DB: {args.db}")
    print(f"模式: {'APPLY (写库)' if args.apply else 'DRY-RUN (仅预览)'}")
    print("=" * 78)

    # ---- 1. 赛果校正 ----
    print(f"\n[1] 赛果落后(归档抢跑)场次: {len(drift)}")
    for mid, home, away, status, msh, msa, osh, osa, ores in drift:
        new_res = _result_of(msh, msa)
        flag = "  结果翻转!" if new_res != ores else ""
        print(f"    {mid} {home} vs {away} [{status}] "
              f"outcomes {osh}-{osa}({ores}) -> matches {msh}-{msa}({new_res}){flag}")

    # ---- 1b. 反向异常(不自动修, 仅报告) ----
    print(f"\n[1b] 反向异常(outcomes 比分 > matches, 需人工确认): {len(reverse)}")
    for mid, home, away, status, minute, msh, msa, osh, osa, ores in reverse:
        print(f"    {mid} {home} vs {away} [{status} {minute}'] "
              f"matches {msh}-{msa} < outcomes {osh}-{osa}({ores})  <- 疑似腰斩/数据重置, 未修改")

    # ---- 2. 半场比分回填 ----
    print(f"\n[2] 半场比分可回填场次: {len(ht_miss)}")
    bad_ht = []
    for mid, home, away, hh, ha, sh, sa in ht_miss:
        # 合理性校验: 半场进球不得超过全场
        if sh is not None and sa is not None and (hh > sh or ha > sa):
            bad_ht.append((mid, home, away, hh, ha, sh, sa))
    if ht_miss[:10]:
        for row in ht_miss[:10]:
            print(f"    {row[0]} {row[1]} vs {row[2]}  HT {row[3]}-{row[4]} / FT {row[5]}-{row[6]}")
        if len(ht_miss) > 10:
            print(f"    ... 其余 {len(ht_miss) - 10} 场略")
    if bad_ht:
        print(f"\n    [WARN] {len(bad_ht)} 场半场进球 > 全场进球(数据矛盾), 已跳过不回填:")
        for row in bad_ht[:5]:
            print(f"      {row[0]} {row[1]} vs {row[2]} HT {row[3]}-{row[4]} > FT {row[5]}-{row[6]}")

    if not args.apply:
        print("\n" + "=" * 78)
        print("DRY-RUN 结束, 未写库. 加 --apply 执行修复.")
        c.close()
        return 0

    # ---- 执行 ----
    n_score = 0
    for mid, home, away, status, msh, msa, osh, osa, ores in drift:
        cur.execute(
            "UPDATE match_outcomes SET score_home=?, score_away=?, result=? WHERE mid=?",
            (msh, msa, _result_of(msh, msa), mid))
        n_score += cur.rowcount

    bad_mids = {r[0] for r in bad_ht}
    n_ht = 0
    for mid, home, away, hh, ha, sh, sa in ht_miss:
        if mid in bad_mids:
            continue
        cur.execute(
            "UPDATE match_outcomes SET ht_score_home=?, ht_score_away=? WHERE mid=?",
            (hh, ha, mid))
        n_ht += cur.rowcount

    c.commit()

    # ---- 复核 ----
    left_drift = len(scan_score_drift(cur))
    ht_total = cur.execute(
        "SELECT COUNT(*) FROM match_outcomes WHERE ht_score_home IS NOT NULL").fetchone()[0]
    mo_total = cur.execute("SELECT COUNT(*) FROM match_outcomes").fetchone()[0]

    print("\n" + "=" * 78)
    print(f"赛果校正: {n_score} 场")
    print(f"半场回填: {n_ht} 场 (跳过矛盾 {len(bad_ht)} 场)")
    print(f"复核 - 残留落后场次: {left_drift} (应为 0)")
    print(f"复核 - match_outcomes 半场比分覆盖: {ht_total}/{mo_total}")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""verify_gov_gain_20260901.py — GQ 假0-0 治理收益量化 (08-31 打标 1686 场后).

对比 OU-over 命中率三组: 脏组(含假0:0) / 干净组(score_missing!=1) / 假0:0组.
主盘线走 build_opening_lines SSoT (IR-01), 结算用 matches 比分 (IR-04 干净口径).

用法: python scripts/verify_gov_gain_20260901.py [--since 2025-01-01]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline.clean_outcomes import clean_where          # noqa: E402
from pipeline.opening_line import build_opening_lines    # noqa: E402

DB = os.path.join(ROOT, "data", "events.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2025-01-01")
    args = ap.parse_args()

    op = build_opening_lines(DB, market="OU", full_time_only=True)
    op_map = {r["match_key"]: r for _, r in op.iterrows()}
    print(f"[OU 主盘 SSoT] {len(op_map)} 场")

    con = sqlite3.connect(DB, timeout=60)
    rows = con.execute(
        "SELECT match_key, score_home, score_away, score_missing, kickoff FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND kickoff>=? "
        "ORDER BY kickoff ASC", (args.since,)).fetchall()

    dirty, clean, fake = [], [], []   # 每组存 over 命中
    n_dirty, n_clean, n_fake = 0, 0, 0
    for mk, sh, sa, sm, ko in rows:
        o = op_map.get(mk)
        if o is None:
            continue
        line = float(o["line"])
        tot = int(sh) + int(sa)
        over_hit = 1 if tot > line else 0
        dirty.append(over_hit); n_dirty += 1
        if sm == 1:
            if sh == 0 and sa == 0:
                fake.append(over_hit); n_fake += 1   # 假 0:0 (打标且比分仍 0:0)
            continue
        clean.append(over_hit); n_clean += 1
    con.close()

    def stat(name: str, arr: list, n: int) -> None:
        hit = sum(arr)
        print(f"  {name:>10}: n={n:>5}  over命中率={hit/max(n,1)*100:6.2f}%  "
              f"(under={n-hit})")

    print(f"\n== OU-over 命中率: 脏组 vs 干净组 vs 假0:0组 (since {args.since}) ==")
    stat("脏组(含假0:0)", dirty, n_dirty)
    stat("干净组", clean, n_clean)
    stat("假0:0组", fake, n_fake)

    if n_clean and n_dirty:
        delta = sum(dirty) / n_dirty - sum(clean) / n_clean
        print(f"\n污染幅度: 脏组 over 命中率 {delta*100:+.2f}pp vs 干净组 "
              f"({'over 被压低(污染确认)' if delta < 0 else '无污染'})")
        print(f"假0:0 {n_fake} 场全部判 under → 脏组每含 1 场假0:0, over 命中率约被压低 "
              f"{100.0/n_dirty:.2f}pp")
    print("\n结论: 打标后回测用 clean_where() 过滤, 假0:0 不再污染 OU/主客/让球结算 (IR-04)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

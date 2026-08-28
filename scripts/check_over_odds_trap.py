# -*- coding: utf-8 -*-
"""Test user's claim: over priced ~2.09 is 诱盘 (inducement) -> tends to draw/under.
For each common OU line, bucket the OPENING over-odds and compute real over-hit rate + draw rate.
"""
import os, sqlite3
ROOT = r"D:\Architecture"
GQ = os.path.join(ROOT, "data", "events.db")
con = sqlite3.connect(GQ); con.execute("PRAGMA busy_timeout=60000")

def opening_ou(con, mk, line):
    rows = con.execute(
        "SELECT selection, odds, minute_at FROM odds_snapshots "
        "WHERE match_key=? AND market LIKE 'OU_%' AND selection IN ('over','under') AND line=? "
        "ORDER BY minute_at ASC", (mk, line)).fetchall()
    best = {}
    for sel, odds, mn in rows:
        if sel not in best or mn < best[sel][1]:
            best[sel] = (odds, mn)
    if 'over' in best and 'under' in best:
        return best['over'][0], best['under'][0]
    return None, None

for line in [2.0, 2.25, 2.5]:
    print(f"\n===== OU line {line:g} : opening over-odds bucket -> (n, over_hit%, draw%) =====")
    # bucket width 0.1
    buckets = {}
    rows = con.execute(
        "SELECT home, away, score_home, score_away, op_ou_line FROM match_outcomes "
        "WHERE result IS NOT NULL AND score_home IS NOT NULL AND score_away IS NOT NULL").fetchall()
    for home, away, sh, sa, op_line in rows:
        if (op_line or 2.5) != line:
            continue
        mk = f"{home} vs {away}"
        ov, un = opening_ou(con, mk, line)
        if not ov or not un:
            continue
        total = (sh or 0) + (sa or 0)
        over_hit = 1 if total > line else 0
        draw = 1 if sh == sa else 0
        b = round(ov * 10) / 10.0  # 0.1 bucket
        d = buckets.setdefault(b, [0, 0, 0])
        d[0] += 1; d[1] += over_hit; d[2] += draw
    for b in sorted(buckets):
        n, oh, dr = buckets[b]
        if n >= 20:  # only meaningful buckets
            print(f"  over_odds {b:g} : n={n:4d}  over_hit={100*oh/n:5.1f}%  draw={100*dr/n:5.1f}%")
con.close()

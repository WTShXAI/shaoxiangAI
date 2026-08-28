# -*- coding: utf-8 -*-
"""Finer check: is opening over-odds in [2.0, 2.2) a trap (low over-hit, high draw)
at each common OU line? Uses only matches whose opening OU line == target line.
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

for line in [2.0, 2.25, 2.5, 3.0]:
    print(f"\n===== OU line {line:g} : over-odds in [2.0,2.2) =====")
    n=oh=dr=0
    rows = con.execute(
        "SELECT home, away, score_home, score_away, op_ou_line FROM match_outcomes "
        "WHERE result IS NOT NULL AND score_home IS NOT NULL AND score_away IS NOT NULL "
        "AND op_ou_line=? ", (line,)).fetchall()
    for home, away, sh, sa, op_line in rows:
        mk = f"{home} vs {away}"
        ov, un = opening_ou(con, mk, line)
        if not ov or not un:
            continue
        if 2.0 <= ov < 2.2:
            total = (sh or 0) + (sa or 0)
            n += 1; oh += (1 if total > line else 0); dr += (1 if sh == sa else 0)
    if n:
        print(f"  over_odds[2.0,2.2) n={n}  over_hit={100*oh/n:.1f}%  draw={100*dr/n:.1f}%  (vs 50% baseline)")
    else:
        print("  no samples")
con.close()

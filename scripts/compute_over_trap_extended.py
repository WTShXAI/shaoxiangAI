# -*- coding: utf-8 -*-
"""Recompute extended trap segments (catch over[2.0,2.2)):
 line 2.5 over [1.9, 2.2);  line 2.25 over [1.75, 2.2)."""
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

def dewatered(ov, un):
    a, b = 1.0/ov, 1.0/un
    return a/(a+b)

for line, lo in [(2.5, 1.9), (2.25, 1.75)]:
    n=oh=0; implied_sum=0.0; draw=0
    rows = con.execute(
        "SELECT home, away, score_home, score_away, op_ou_line FROM match_outcomes "
        "WHERE result IS NOT NULL AND score_home IS NOT NULL AND score_away IS NOT NULL AND op_ou_line=? ",
        (line,)).fetchall()
    for home, away, sh, sa, op_line in rows:
        mk = f"{home} vs {away}"
        ov, un = opening_ou(con, mk, line)
        if not ov or not un:
            continue
        if lo <= ov < 2.2:
            total = (sh or 0) + (sa or 0)
            n += 1; oh += (1 if total > line else 0); draw += (1 if sh==sa else 0)
            implied_sum += dewatered(ov, un)
    if n:
        actual = oh/n; implied = implied_sum/n
        print(f"line {line:g} over[{lo},2.2): n={n} actual_overhit={actual:.4f} implied={implied:.4f} "
              f"edge={actual-implied:+.4f} calib_pp={100*(actual-implied):+.1f} draw={100*draw/n:.1f}%")
con.close()

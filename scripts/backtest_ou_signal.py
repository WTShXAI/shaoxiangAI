# -*- coding: utf-8 -*-
"""Backtest the LiveGoalProbe OVER/UNDER signal logic against real final scores.
Reconstructs the exact backend branch (live_goal_probe.py:1821-1840) and tests
whether "low over-odds -> 看大(OVER)" is a real edge or a coin-flip.
"""
import os, sqlite3, json
ROOT = r"D:\Architecture"
GQ = os.path.join(ROOT, "data", "events.db")
con = sqlite3.connect(GQ); con.execute("PRAGMA busy_timeout=60000")
con.execute("CREATE INDEX IF NOT EXISTS idx_os_mk ON odds_snapshots(match_key)")

PGAP_WEAK = 0.015
PGAP_STRONG = 0.03

def dewatered(ov, un):
    if not ov or not un: return None
    inv_o, inv_u = 1.0/ov, 1.0/un
    s = inv_o + inv_u
    if s <= 0: return None
    return inv_o / s

def backend_signal(ov, un, line, current_total=0):
    """Mirror live_goal_probe.py:1764-1840 full-match branch (no cup shrink, no momentum).
    `current_total` = score at signal time (pre-match = 0). The ALREADY_BROKEN
    branch only fires for tiny lines; at pre-match score 0-0 it never triggers for OU 2.5."""
    if current_total >= line:
        return None  # ALREADY_BROKEN -> not a directional bet
    p = dewatered(ov, un)
    if p is None:
        return None
    pgap = abs(p - 0.5)
    if p >= 0.65:
        return 'OVER'
    elif p <= 0.35:
        return 'UNDER'
    elif pgap >= PGAP_WEAK:
        return 'OVER' if ov < un else 'UNDER'   # WEAK_TREND follows low-water side
    else:
        return 'OVER' if p >= 0.5 else 'UNDER'  # NO_EDGE

def opening_ou(con, mk, line):
    rows = con.execute(
        "SELECT selection, odds, minute_at FROM odds_snapshots "
        "WHERE match_key=? AND market LIKE 'OU_%' AND selection IN ('over','under') AND line=? "
        "ORDER BY minute_at ASC", (mk, line)).fetchall()
    # pick the row with min minute_at per selection
    best = {}
    for sel, odds, mn in rows:
        if sel not in best or mn < best[sel][1]:
            best[sel] = (odds, mn)
    if 'over' in best and 'under' in best:
        return best['over'][0], best['under'][0]
    return None, None

# iterate finished matches
rows = con.execute(
    "SELECT home, away, score_home, score_away, op_ou_line FROM match_outcomes "
    "WHERE result IS NOT NULL AND score_home IS NOT NULL AND score_away IS NOT NULL").fetchall()

stats = {
    'backend':   {'n':0,'correct':0,'over_called':0,'over_called_correct':0},
    'lowwater':  {'n':0,'correct':0,'over_called':0,'over_called_correct':0},
    'dewatered': {'n':0,'correct':0,'over_called':0,'over_called_correct':0},
    'over_ended_zerozero': 0,  # backend said OVER but ended 0-0
}
total_matches = 0
for home, away, sh, sa, op_line in rows:
    total_matches += 1
    line = op_line if op_line else 2.5
    mk = f"{home} vs {away}"
    ov, un = opening_ou(con, mk, line)
    if not ov or not un:
        continue
    total = (sh or 0) + (sa or 0)
    over_hit = total > line
    # backend full signal — evaluated at pre-match (score 0-0), judged vs final over_hit
    d = backend_signal(ov, un, line, current_total=0)
    if d is not None:
        s = stats['backend']; s['n'] += 1
        s['correct'] += (1 if (d=='OVER')==over_hit else 0)
        if d=='OVER':
            s['over_called'] += 1
            s['over_called_correct'] += (1 if over_hit else 0)
            if total == 0:
                stats['over_ended_zerozero'] += 1
    # pure low-water rule
    lw = 'OVER' if ov < un else 'UNDER'
    s = stats['lowwater']; s['n'] += 1
    s['correct'] += (1 if (lw=='OVER')==over_hit else 0)
    if lw=='OVER':
        s['over_called'] += 1
        s['over_called_correct'] += (1 if over_hit else 0)
    # dewatered >=0.5 rule
    p = dewatered(ov, un)
    dw = 'OVER' if p >= 0.5 else 'UNDER'
    s = stats['dewatered']; s['n'] += 1
    s['correct'] += (1 if (dw=='OVER')==over_hit else 0)
    if dw=='OVER':
        s['over_called'] += 1
        s['over_called_correct'] += (1 if over_hit else 0)

print(f"scanned {total_matches} finished matches\n")
for name, s in stats.items():
    if name == 'over_ended_zerozero':
        print(f"[over_ended_zerozero] backend said OVER but true total==0: {s} matches")
        continue
    n = s['n']; c = s['correct']
    oc = s['over_called']; occ = s['over_called_correct']
    print(f"=== {name} ===")
    print(f"  directional calls: {n}, overall accuracy: {c}/{n} = {100*c/n:.1f}%")
    if oc:
        print(f"  OVER calls: {oc}, OVER accuracy: {occ}/{oc} = {100*occ/oc:.1f}%")
    # 50% baseline for reference
    print(f"  (50% coin-flip baseline = {100*(n//2)/n:.1f}%)\n")

con.close()

import sqlite3
from collections import Counter

DB = r"D:/Architecture/data/events.db"
try:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
except Exception:
    con = sqlite3.connect(DB, timeout=30)
con.row_factory = sqlite3.Row
c = con.cursor()

print("=== match_outcomes rows archived 2026-08-24 (backfill writes) ===")
n = 0
for r in c.execute(
    "SELECT mid, home, away, league, score_home, score_away, result, source, archived_at "
    "FROM match_outcomes WHERE archived_at LIKE '2026-08-24%' ORDER BY mid"
):
    print(dict(r)); n += 1
print("backfilled rows:", n)

print("\n=== total match_outcomes count ===")
print(c.execute("SELECT COUNT(*) FROM match_outcomes").fetchone()[0])

print("\n=== re-derive targets (has odds, no valid result) ===")
valid = {r[0] for r in c.execute(
    "SELECT mid FROM match_outcomes WHERE result IN ('home','draw','away') AND is_valid=1 AND mid IS NOT NULL")}
rows = c.execute(
    "SELECT m.mid, m.home, m.away, m.league, m.status, m.score_home, m.score_away "
    "FROM matches m WHERE m.mid IS NOT NULL").fetchall()
targets = []
for r in rows:
    mid = r["mid"]
    if mid in valid:
        continue
    if not c.execute("SELECT 1 FROM odds_snapshots WHERE mid=?", (mid,)).fetchone():
        continue
    targets.append(r)
print("targets total:", len(targets))

have_score = [t for t in targets if t["score_home"] is not None and t["score_away"] is not None]
no_score = [t for t in targets if not (t["score_home"] is not None and t["score_away"] is not None)]
print("  with score in matches:", len(have_score))
print("  without score anywhere:", len(no_score))

live = [t for t in no_score if str(t["status"] or "") in ("live", "inplay", "scheduled", "NS", "upcoming", "not_started")]
fin = [t for t in no_score if str(t["status"] or "") in ("finished", "FT", "AET", "FT_PEN")]
other = [t for t in no_score if t not in live and t not in fin]
print("    no_score & still live/scheduled:", len(live))
print("    no_score & finished(stored):", len(fin))
print("    no_score & other status:", len(other), [t["status"] for t in other[:10]])

lg = Counter(t["league"] for t in no_score)
print("  no_score league top:", lg.most_common(12))
con.close()

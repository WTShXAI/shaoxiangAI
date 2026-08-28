import sqlite3
from collections import Counter

DB = r"D:/Architecture/data/events.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
con.row_factory = sqlite3.Row
c = con.cursor()

# matches match_key -> mid (only non-null mid)
mk_mid = {}
for mk, mid in c.execute("SELECT match_key, mid FROM matches WHERE mid IS NOT NULL"):
    mk_mid[mk] = mid
print("matches total:", c.execute("SELECT COUNT(*) FROM matches").fetchone()[0])
print("matches with non-null mid:", len(mk_mid))

valid_mids = {r[0] for r in c.execute(
    "SELECT mid FROM match_outcomes WHERE result IN ('home','draw','away') AND is_valid=1 AND mid IS NOT NULL")}
print("valid match_outcomes mids:", len(valid_mids))

odds_mks = {r[0] for r in c.execute("SELECT DISTINCT match_key FROM odds_snapshots")}
print("odds_snapshots distinct match_keys:", len(odds_mks))

target_mks = []
orphan_mks = []
for mk in odds_mks:
    mid = mk_mid.get(mk)
    if mid is None:
        orphan_mks.append(mk)
    elif mid not in valid_mids:
        target_mks.append(mk)

print("\n>>> CURRENT targets (have odds + mid, no valid result):", len(target_mks))
print(">>> ORPHANS (have odds, but matches.mid NULL -> cannot make match_outcomes):", len(orphan_mks))

# classify targets by matches state
have_score = no_score = live = friendly = 0
league_noscore = Counter()
status_noscore = Counter()
for mk in target_mks:
    m = c.execute("SELECT home,away,league,status,score_home,score_away FROM matches WHERE match_key=?", (mk,)).fetchone()
    if not m:
        continue
    lg = m["league"] or ""
    if "友谊" in lg:
        friendly += 1
    if m["score_home"] is not None and m["score_away"] is not None:
        have_score += 1
    else:
        no_score += 1
        league_noscore[lg] += 1
        st = str(m["status"] or "")
        status_noscore[st] += 1
        if st in ("live", "inplay", "scheduled", "NS", "upcoming", "not_started", ""):
            live += 1

print("\n  targets WITH score in matches (backfillable, excl friendly):", have_score)
print("  targets WITHOUT score anywhere:", no_score)
print("    of which still live/scheduled (collector will finish):", live)
print("    friendly (excluded by rule):", friendly)
print("  no_score status distribution:", status_noscore.most_common(12))
print("  no_score league top 15:")
for lg, n in league_noscore.most_common(15):
    print(f"    {n:4d}  {lg}")

# Also: of the backfilled 5, are they in targets still or now valid?
print("\n=== sample of current targets (first 15) ===")
for mk in target_mks[:15]:
    m = c.execute("SELECT home,away,league,status,score_home,score_away FROM matches WHERE match_key=?", (mk,)).fetchone()
    print("  ", m["home"], "vs", m["away"], "|", m["league"], "| st=", m["status"], "| sc=", m["score_home"], m["score_away"])
con.close()

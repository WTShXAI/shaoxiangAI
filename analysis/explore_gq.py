import sqlite3
db = "D:/Architecture/data/events.db"
c = sqlite3.connect(db)
tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("TABLES:", [t for t in tabs if 'odd' in t.lower() or 'match' in t.lower() or 'outcome' in t.lower()])
print("\nodds_snapshots cols:", [d[1] for d in c.execute("PRAGMA table_info(odds_snapshots)")])
print("\nmarkets:", [r for r in c.execute("SELECT DISTINCT market FROM odds_snapshots LIMIT 20")])
print("\nmatches w/ outcome finished:", c.execute("SELECT COUNT(*) FROM matches WHERE score_home IS NOT NULL AND status='finished'").fetchone())
print("odds_snapshots rows:", c.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone())
print("CS rows:", c.execute("SELECT COUNT(*) FROM odds_snapshots WHERE market='CS'").fetchone())
print("1X2 rows:", c.execute("SELECT COUNT(*) FROM odds_snapshots WHERE market='1X2'").fetchone())
print("OU rows:", c.execute("SELECT COUNT(*) FROM odds_snapshots WHERE market='OU'").fetchone())
# sample a CS 0-0 selection format
for r in c.execute("SELECT match_key, selection, latest_odds, captured_at FROM odds_snapshots WHERE market='CS' AND selection LIKE '%0-0%' LIMIT 5"):
    print("  CS0-0:", r)

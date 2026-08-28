import sqlite3
# events.db 1X2 selection sample
c = sqlite3.connect("D:/Architecture/data/events.db")
print("=== GQ 1X2 sample ===")
for r in c.execute("SELECT match_key, selection, odds, line FROM odds_snapshots WHERE market='1X2' LIMIT 6"):
    print("  ", r)
print("=== GQ AH sample ===")
for r in c.execute("SELECT match_key, selection, odds, line FROM odds_snapshots WHERE market LIKE 'AH%' LIMIT 6"):
    print("  ", r)
print("=== GQ CS selection formats (distinct) ===")
for r in c.execute("SELECT DISTINCT selection FROM odds_snapshots WHERE market='CS' LIMIT 30"):
    print("  ", r[0])
# football_data.db
f = sqlite3.connect("D:/Architecture/data/football_data.db")
tabs=[r[0] for r in f.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("\n=== football_data.db tables ===", [t for t in tabs][:30])
# find a table with 1x2 odds + result
for t in tabs:
    cols=[d[1] for d in f.execute(f"PRAGMA table_info({t})")]
    if any('home' in c2.lower() for c2 in cols) and any(('draw' in c2.lower()) or ('odds' in c2.lower()) for c2 in cols):
        print(f"  candidate {t}: {cols[:15]}")

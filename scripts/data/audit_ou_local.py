"""Local audit: find ANY table linking OU odds to match results in football_data.db."""
import sqlite3
DB = r"D:\Architecture\data\football_data.db"
con = sqlite3.connect(DB)
cur = con.cursor()

print("=== all tables ===")
tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print(tables)

def schema(t):
    return [r[1] for r in cur.execute(f"PRAGMA table_info('{t}')")]

for t in tables:
    cols = schema(t)
    cl = " ".join(cols).lower()
    # look for tables that mention over/under/totals or scores
    if any(k in cl for k in ["over", "under", "total", "score", "ou", "goals"]):
        print(f"\n### {t} cols: {cols}")

print("\n=== cross_market_consistency sample ===")
try:
    cols = schema("cross_market_consistency")
    print("cols:", cols)
    n = cur.execute("SELECT COUNT(*) FROM cross_market_consistency").fetchone()[0]
    print("rows:", n)
    # find a row with prob_over_25
    samp = cur.execute("SELECT * FROM cross_market_consistency LIMIT 1").fetchone()
    print("sample row:", samp)
except Exception as e:
    print("err:", e)

print("\n=== historical_matches schema + score linkage ===")
try:
    cols = schema("historical_matches")
    print("cols:", cols)
    n = cur.execute("SELECT COUNT(*) FROM historical_matches").fetchone()[0]
    print("rows:", n)
except Exception as e:
    print("err:", e)

print("\n=== matches: does it have any odds columns? ===")
print("cols:", schema("matches"))

print("\n=== any table with both a score col and an odds/line col? ===")
for t in tables:
    cols = [c.lower() for c in schema(t)]
    has_score = any("score" in c for c in cols)
    has_odds = any(k in c for k in ["odds","line","over","under","total","implied"])
    if has_score and has_odds:
        print(f"  -> {t}: score={has_score} odds={has_odds} cols={schema(t)}")

con.close()

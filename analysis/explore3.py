import sqlite3
f = sqlite3.connect("D:/Architecture/data/football_data.db")
tabs=[r[0] for r in f.execute("SELECT name FROM sqlite_master WHERE type='table'")]
for t in tabs:
    cols=[d[1] for d in f.execute(f"PRAGMA table_info({t})")]
    if ('home_score' in cols or 'hs' in cols or 'score_home' in cols) and ('draw_odds' in cols or 'draw' in ' '.join(cols).lower()):
        print(f"{t}: {cols}")
# also check historical_matches / matches specifically
for t in ['matches','historical_matches']:
    try:
        cols=[d[1] for d in f.execute(f"PRAGMA table_info({t})")]
        n=f.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"\n{t} ({n} rows) cols: {cols[:20]}")
        # sample 0-0 count
        sc = 'home_score' if 'home_score' in cols else ('score_home' if 'score_home' in cols else None)
        if sc:
            zz=f.execute(f"SELECT COUNT(*) FROM {t} WHERE {sc}=0 AND away_score=0").fetchone()[0]
            print(f"  0-0 count: {zz}")
    except Exception as e:
        print(t,"err",e)
# odds table join check
print("\nodds sample:", f.execute("SELECT match_id, home_odds, draw_odds, away_odds, return_rate FROM odds LIMIT 3").fetchall())
print("odds count:", f.execute("SELECT COUNT(*) FROM odds").fetchone())

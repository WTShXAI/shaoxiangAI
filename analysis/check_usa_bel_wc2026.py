"""
验证用户下注：世界杯2026 美国 vs 比利时 1-4 @56
查询 football_data.db (wc_all_matches) 和 events.db (matches/odds_snapshots/match_outcomes)
"""
import sqlite3, os, json

DATA_DIR = "D:/Architecture/data"

def query(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def tables_and_cols(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    schema = {}
    for t in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
        schema[t] = cols
    conn.close()
    return schema

# 先看 wc_all_matches 表结构
print("=== football_data.db schema (wc_all_matches) ===")
schema = tables_and_cols(f"{DATA_DIR}/football_data.db")
for t, cols in schema.items():
    if 'wc' in t.lower() or 'match' in t.lower():
        print(f"{t}: {cols}")

print("\n=== football_data.db / wc_all_matches 相关行 ===")
rows = query(f"{DATA_DIR}/football_data.db", """
SELECT * FROM wc_all_matches
WHERE home LIKE '%USA%' OR away LIKE '%USA%'
   OR home LIKE '%United States%' OR away LIKE '%United States%'
   OR home LIKE '%Belgium%' OR away LIKE '%Belgium%'
   OR home LIKE '%美国%' OR away LIKE '%美国%'
""")
print(f"找到 {len(rows)} 行")
for r in rows[:20]:
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))

print("\n=== events.db / matches (按队名) ===")
rows = query(f"{DATA_DIR}/events.db", """
SELECT mid, league, home, away, kickoff, score_home, score_away, status
FROM matches
WHERE (home LIKE '%美国%' OR home LIKE '%USA%' OR home LIKE '%美國%'
       OR away LIKE '%美国%' OR away LIKE '%USA%' OR away LIKE '%美國%')
   OR (home LIKE '%比利时%' OR home LIKE '%比利時%' OR home LIKE '%Belgium%'
       OR away LIKE '%比利时%' OR away LIKE '%比利時%' OR away LIKE '%Belgium%')
ORDER BY kickoff
""")
print(f"找到 {len(rows)} 行")
for r in rows:
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))

print("\n=== events.db / odds_snapshots CS 1-4 ===")
mids = [r['mid'] for r in rows if any(k in str(r.get('kickoff','')) for k in ['2026-07-07','2026-07-06'])]
if mids:
    for mid in mids:
        cs = query(f"{DATA_DIR}/events.db", """
        SELECT * FROM odds_snapshots
        WHERE mid=? AND market='CS' AND selection='1-4'
        ORDER BY timestamp DESC
        LIMIT 5
        """, (mid,))
        print(f"mid={mid} CS 1-4 rows: {len(cs)}")
        for c in cs:
            print(json.dumps(c, ensure_ascii=False, indent=2, default=str))
else:
    print("未找到 2026-07-06/07 的美国/比利时比赛 mid")

print("\n=== events.db / match_outcomes ===")
if mids:
    for mid in mids:
        mo = query(f"{DATA_DIR}/events.db", "SELECT * FROM match_outcomes WHERE mid=?", (mid,))
        print(f"mid={mid} outcomes: {len(mo)}")
        for m in mo:
            print(json.dumps(m, ensure_ascii=False, indent=2, default=str))

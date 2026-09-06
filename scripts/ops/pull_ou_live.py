"""Forward OU collection: pull current pinnacle totals odds for soccer leagues.
Stores to football_data.db.ou_live_feed (seed for unbiased OU validation set).
Credit budget: ~1 per soccer sport (36 soccer leagues). Safe within 500.
"""
import sqlite3, urllib.request, urllib.parse, urllib.error, json, os, sys, datetime

DB = r"D:\Architecture\data\football_data.db"
ENV = r"D:\Architecture\.env"
BASE = "https://api.the-odds-api.com/v4"

key = None
with open(ENV, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line.startswith("THEODDS_API_KEY"):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
assert key, "THEODDS_API_KEY missing"

def get(path, params=None):
    q = dict(params or {}); q["apiKey"] = key
    url = BASE + path + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": "shaoxiang/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8")), r.headers.get("x-requests-remaining")
    except urllib.error.HTTPError as e:
        return ("HTTPERR", e.code, e.read().decode("utf-8","replace")), None

# list soccer sports
sports, _ = get("/sports")
soccer = [s["key"] for s in sports if s.get("key","").startswith("soccer_")]
print("soccer sports:", len(soccer))

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else len(soccer)
soccer = soccer[:LIMIT]
print("pulling", len(soccer), "sports")

con = sqlite3.connect(DB)
con.execute("DROP TABLE IF EXISTS ou_live_feed")
con.execute("""CREATE TABLE ou_live_feed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT, sport_key TEXT, home_team TEXT, away_team TEXT,
    commence_time TEXT, ou_point REAL, over_odds REAL, under_odds REAL,
    bookmaker TEXT, fetched_at TEXT, result TEXT, total_goals INTEGER, settle TEXT,
    UNIQUE(event_id, ou_point))""")

rows = []
min_rem = None
for sp in soccer:
    data, rem = get(f"/sports/{sp}/odds", {
        "regions": "us,eu,uk", "markets": "totals"
    })
    if rem: min_rem = rem if min_rem is None else min(min_rem, rem)
    if not isinstance(data, list):
        print(f"  {sp}: FAIL {data}")
        continue
    cnt = 0
    PREF = ["pinnacle", "lowvig", "betonlineag", "betmgm", "bovada", "williamhill"]
    for ev in data:
        eid = ev.get("id"); h = ev.get("home_team"); a = ev.get("away_team"); ct = ev.get("commence_time")
        # pick preferred bookmaker that has totals
        chosen = None
        bms = {b.get("key"): b for b in ev.get("bookmakers", [])}
        for pk in PREF:
            if pk in bms:
                chosen = bms[pk]; break
        if chosen is None:
            continue
        bk = chosen.get("key")
        for mk in chosen.get("markets", []):
            if mk.get("key") != "totals": continue
            over = under = None
            for oc in mk.get("outcomes", []):
                if oc.get("name") == "Over": over = oc.get("price")
                elif oc.get("name") == "Under": under = oc.get("price")
            pt = mk.get("outcomes", [{}])[0].get("point") if mk.get("outcomes") else None
            if pt is None or over is None or under is None: continue
            rows.append(dict(event_id=eid, sport_key=sp, home_team=h, away_team=a, commence_time=ct,
                             ou_point=pt, over_odds=over, under_odds=under, bookmaker=bk,
                             fetched_at=datetime.datetime.now().isoformat()))
            cnt += 1
    print(f"  {sp}: events={len(data)} ou_entries={cnt}")

# cache rows to JSON (avoid re-pull on re-run)
import json as _json
with open(r"D:\Architecture\ou_live_cache.json", "w", encoding="utf-8") as _f:
    _json.dump(rows, _f)
print(f"cached {len(rows)} rows to ou_live_cache.json")

# insert
ins = 0
for r in rows:
    try:
        con.execute("INSERT OR IGNORE INTO ou_live_feed (event_id,sport_key,home_team,away_team,commence_time,ou_point,over_odds,under_odds,bookmaker,fetched_at) VALUES (:event_id,:sport_key,:home_team,:away_team,:commence_time,:ou_point,:over_odds,:under_odds,:bookmaker,:fetched_at)", r)
        ins += 1
    except Exception as e:
        print("insert err", e)
con.commit()
n = con.execute("SELECT COUNT(*) FROM ou_live_feed").fetchone()[0]
con.close()
print(f"\nInserted {ins} new rows (total ou_live_feed={n}). min remaining credits={min_rem}")

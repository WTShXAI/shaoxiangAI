"""Build wc2026_r16_results.json: match REAL R16 results (worldcup26.ir API,
cached or live) to REAL odds (wc2026_blob_all_odds.json) and DB knockout rows.

Read-only on DB. Only writes wc2026_r16_results.json.
"""
import json, sqlite3, urllib.request, time
from pathlib import Path

DATA = Path(r"D:\Architecture\data")
API_BASE = "https://worldcup26.ir"
PY = r"C:\Users\ShXAI\.workbuddy\binaries\python\versions\3.13.12\python.exe"

# EN -> ZH (from worldcup26_enricher.py, extended)
TEAM_EN_TO_ZH = {
    "Mexico": "墨西哥", "South Africa": "南非", "South Korea": "韩国",
    "Czech Republic": "捷克", "Canada": "加拿大", "Bosnia and Herzegovina": "波黑",
    "Qatar": "卡塔尔", "Switzerland": "瑞士", "Brazil": "巴西",
    "Morocco": "摩洛哥", "Haiti": "海地", "Scotland": "苏格兰",
    "United States": "美国", "Paraguay": "巴拉圭", "Australia": "澳大利亚",
    "Turkey": "土耳其", "Germany": "德国", "Curaçao": "库拉索",
    "Ivory Coast": "科特迪瓦", "Ecuador": "厄瓜多尔", "Netherlands": "荷兰",
    "Japan": "日本", "Sweden": "瑞典", "Tunisia": "突尼斯",
    "Belgium": "比利时", "Egypt": "埃及", "Iran": "伊朗",
    "New Zealand": "新西兰", "Spain": "西班牙", "Cape Verde": "佛得角",
    "Saudi Arabia": "沙特阿拉伯", "Uruguay": "乌拉圭", "France": "法国",
    "Senegal": "塞内加尔", "Iraq": "伊拉克", "Norway": "挪威",
    "Argentina": "阿根廷", "Algeria": "阿尔及利亚", "Austria": "奥地利",
    "Jordan": "约旦", "Portugal": "葡萄牙", "DR Congo": "民主刚果",
    "Democratic Republic of the Congo": "民主刚果", "Uzbekistan": "乌兹别克斯坦",
    "Colombia": "哥伦比亚", "England": "英格兰", "Croatia": "克罗地亚",
    "Ghana": "加纳", "Panama": "巴拿马", "Congo DR": "民主刚果",
}
def en_to_zh(n): return TEAM_EN_TO_ZH.get(n, n)

# ---- 1. Fetch API games (live, fallback to cache) ----
api_reachable = True
games = None
try:
    req = urllib.request.Request(f"{API_BASE}/get/games",
                                 headers={"User-Agent": "FootballAI/4.0"})
    resp = urllib.request.urlopen(req, timeout=15)
    games = json.loads(resp.read())
    print("[API] live fetch OK")
except Exception as e:
    print(f"[API] live fetch FAILED: {e}")
    api_reachable = False
    gf = DATA / "api_cache" / "wc26__get_games.json"
    if gf.exists():
        games = json.load(open(gf, encoding="utf-8"))
        print("[API] using cached games.json (real, from earlier today)")
    else:
        games = {}

game_list = games if isinstance(games, list) else games.get("games", games.get("data", []))

# ---- 2. Query DB unsynced WC rows (read-only) ----
con = sqlite3.connect(str(DATA / "football_data.db"))
db_rows = list(con.execute(
    "SELECT match_id,home_team_name,away_team_name,match_date,matchday,status "
    "FROM matches WHERE league_name='世界杯' AND final_result IS NULL"))
con.close()

# Anchor R16 on the 8 real API r16 fixtures (type=='r16')
r16_pairs = set()
r16_dates = set()
r32_pairs = set()
for g in game_list:
    hz = en_to_zh(g.get("home_team_name_en")); az = en_to_zh(g.get("away_team_name_en"))
    pair = frozenset([hz, az])
    ld = g.get("local_date", "")
    gdate = ""
    if ld:
        try:
            mm, dd, yyyy = ld.split(" ")[0].split("/"); gdate = f"{yyyy}-{mm}-{dd}"
        except Exception:
            gdate = ld
    if g.get("type") == "r16":
        r16_pairs.add(pair); r16_dates.add(gdate)
    elif g.get("type") == "r32":
        r32_pairs.add(pair)

DB_ALIAS = {"USA": "美国", "Bosnia-H.": "波黑", "Congo DR": "民主刚果",
            "Korea Republic": "韩国", "Czechia": "捷克"}
def dbzh(n):
    if n is None: return None
    if n in DB_ALIAS: return DB_ALIAS[n]
    for k, v in TEAM_EN_TO_ZH.items():
        if n == k or n == v: return v
    return n

def is_r16(r):
    mid, h, a, md, md2, st = r[0], r[1], r[2], r[3], r[4], r[5]
    pair = frozenset([x for x in [dbzh(h), dbzh(a)] if x])
    if pair in r16_pairs:
        return True
    if pair in r32_pairs:
        return False  # it's R32, not R16
    if md in r16_dates and (h is None or a is None):
        return True   # TBD fixture on an R16 date
    return False

r16_db = [r for r in db_rows if is_r16(r)]
print(f"[DB] R16 unsynced rows: {len(r16_db)} -> {[(r[0],r[1],r[2],r[3]) for r in r16_db]}")

# ---- 3. R16 API games (type r16) ----
r16_api = [g for g in game_list if g.get("type") == "r16"]
print(f"[API] R16 games: {len(r16_api)}")

# odds lookup by frozenset of zh team pair
odds = json.load(open(DATA / "wc2026_blob_all_odds.json", encoding="utf-8"))
odds_lookup = {}
for o in odds:
    key = frozenset([o["home"], o["away"]])
    odds_lookup[key] = o

# ---- 4. Match & build records ----
matched = []
unmatched_db = []
name_issues = []

for g in r16_api:
    h_en = g.get("home_team_name_en"); a_en = g.get("away_team_name_en")
    h_zh = en_to_zh(h_en); a_zh = en_to_zh(a_en)
    finished = g.get("finished") == "TRUE"
    hs = g.get("home_score"); aw = g.get("away_score")
    # parse date
    ld = g.get("local_date", "")
    api_date = ""
    if ld:
        try:
            mm, dd, yyyy = ld.split(" ")[0].split("/")
            api_date = f"{yyyy}-{mm}-{dd}"
        except Exception:
            api_date = ld
    # find odds by zh pair (order-independent)
    o = odds_lookup.get(frozenset([h_zh, a_zh]))
    if finished and o is not None:
        try:
            hg = int(hs or 0); ag = int(aw or 0)
        except Exception:
            hg, ag = 0, 0
        res = "H" if hg > ag else ("D" if hg == ag else "A")
        rec = {
            "home": h_zh, "away": a_zh,
            "date": api_date,
            "res": res, "sc": f"{hg}-{ag}",
            "oh": o["oh"], "od": o["od"], "oa": o["oa"],
            "hcp": o["hcp"], "ou": o["ou"], "src": "api+r16",
        }
        matched.append(rec)
        print(f"  MATCHED: {h_zh}-{a_zh} {hg}-{ag} res={res} (odds {o['src']})")
    elif not finished and o is not None:
        name_issues.append(f"{h_zh}-{a_zh} ({api_date}): API not finished yet (no result)")
    elif o is None:
        name_issues.append(f"{h_zh}-{a_zh}: no odds entry found in blob")

# Map which DB R16 rows got matched (by zh pair)
matched_pairs = set(frozenset([m["home"], m["away"]]) for m in matched)
for r in r16_db:
    mid, h, a, md, *_ = r
    # normalize db team name to zh if possible
    def dbzh(n):
        if n is None: return None
        for k, v in TEAM_EN_TO_ZH.items():
            if n == k or n == v: return v
        return n
    hz, az = dbzh(h), dbzh(a)
    key = frozenset([x for x in [hz, az] if x])
    if key and key in matched_pairs:
        continue  # matched
    # not matched -> unmatched
    unmatched_db.append({"match_id": mid, "home": h, "away": a, "date": md,
                         "reason": "no finished API result yet / future fixture"})

out = {
    "matched": matched,
    "unmatched_db": unmatched_db,
    "api_not_reachable": (not api_reachable and games == {}),
}
json.dump(out, open(DATA / "wc2026_r16_results.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)

print("\n=== SUMMARY ===")
print(f"matched R16 results: {len(matched)}")
print(f"unmatched DB R16 rows: {len(unmatched_db)}")
print(f"api_reachable(live): {api_reachable}")
print("name issues:", name_issues)
print("written -> wc2026_r16_results.json")

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ingest_odds_oddpapi.py  —  在沙盒里运行（解析端）。

读取 fetch_odds_oddpapi.py 产出的 oddpapi_raw.json：
  1) 从 DB 动态取出"当前 10 场缺赔率"的世界杯比赛，建 (日期, 主队, 客队) -> match_id 映射
  2) 把 oddpapi 每条 record 归一化队名后映射到 match_id（含主客颠倒兜底）
  3) 对 (match_id, provider) 先 DELETE 再 INSERT —— 幂等，重复贴不重复写
  4) 顺手算 return_rate（1 / 隐含概率和，即庄家返还率）一起存

用法：
    python ingest_odds_oddpapi.py [path/to/oddpapi_raw.json]
默认读 D:/Architecture/data/oddpapi_raw.json
"""
import json
import sqlite3
import sys

DB = "D:/Architecture/data/football_data.db"
RAW = sys.argv[1] if len(sys.argv) > 1 else "D:/Architecture/data/oddpapi_raw.json"

# 队名同义词（oddpapi 可能返回的变体 -> 我库里的标准名）。小写比对。
SYN = {
    "united states": "usa", "us": "usa", "usa": "usa", "usmnt": "usa",
    "england": "england", "mexico": "mexico", "france": "france",
    "brazil": "brazil", "norway": "norway", "spain": "spain",
    "portugal": "portugal", "belgium": "belgium", "argentina": "argentina",
    "egypt": "egypt", "canada": "canada", "morocco": "morocco",
    "paraguay": "paraguay", "switzerland": "switzerland", "colombia": "colombia",
}


def norm(n):
    if not n:
        return ""
    n = str(n).lower().strip()
    return SYN.get(n, n)


con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()

# —— 动态取出当前缺赔率的 10 场世界杯 ——
cur.execute(
    "SELECT match_id FROM matches WHERE league_name='世界杯' "
    "AND (SELECT COUNT(*) FROM odds o WHERE o.match_id=matches.match_id)=0"
)
missing_ids = [r["match_id"] for r in cur.fetchall()]
if not missing_ids:
    print("DB 里已没有缺赔率的世界杯场次，无需入库。")
    con.close()
    sys.exit(0)
ph = ",".join(str(i) for i in missing_ids)

lut = {}
for r in cur.execute(
    f"SELECT match_id, match_date, home_team_name, away_team_name "
    f"FROM matches WHERE match_id IN ({ph})"
):
    h, a, d = norm(r["home_team_name"]), norm(r["away_team_name"]), r["match_date"]
    lut[(d, h, a)] = r["match_id"]
    lut[(d, a, h)] = r["match_id"]  # 主客颠倒兜底

data = json.load(open(RAW, encoding="utf-8"))
recs = data.get("records", [])
print(f"JSON 内共 {len(recs)} 条 record；DB 当前缺赔率 {len(missing_ids)} 场\n")

matched = inserted = 0
notfound = []
for rec in recs:
    date = str(rec.get("date", ""))[:10]
    h, a = norm(rec.get("home")), norm(rec.get("away"))
    mid = lut.get((date, h, a))
    if not mid:
        notfound.append(rec)
        continue
    matched += 1
    bm = rec.get("bookmaker") or "oddpapi"
    provider = f"oddpapi_{bm}" if bm != "unknown" else "oddpapi"
    try:
        ho, do, ao = float(rec["home_odds"]), float(rec["draw_odds"]), float(rec["away_odds"])
    except (TypeError, ValueError):
        notfound.append(rec)
        continue
    # 返还率（去水前的庄家边际）
    s = 1.0 / ho + 1.0 / do + 1.0 / ao
    rr = round(1.0 / s, 4) if s > 0 else None
    cur.execute("DELETE FROM odds WHERE match_id=? AND provider=?", (mid, provider))
    cur.execute(
        "INSERT INTO odds (match_id, provider, home_odds, draw_odds, away_odds, "
        "return_rate, odds_timestamp, created_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
        (mid, provider, ho, do, ao, rr, date),
    )
    inserted += 1
    print(f"  + match_id={mid} {rec['home']} vs {rec['away']} [{provider}] "
          f"{ho}/{do}/{ao}  rr={rr}")

con.commit()
con.close()
print(f"\n映射命中 {matched}/{len(recs)} 条；实际写入 {inserted} 行 odds。")

if notfound:
    print(f"\n⚠️ {len(notfound)} 条无法映射到这 10 场（可能是非目标比赛/队名对不上），未入库：")
    for r in notfound:
        print(f"  - {r.get('date')} {r.get('home')} vs {r.get('away')} [{r.get('bookmaker')}]")

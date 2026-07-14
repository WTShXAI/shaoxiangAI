#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ingest_odds_theoddsapi.py
解析 The Odds API (the-odds-api.com) 拉取的 FIFA World Cup 2026 实时盘 JSON,
去抽水(de-vig)取共识赔率, 按 (主队,客队) 映射到 matches.match_id, 幂等写入 odds 表.

用法:
  python ingest_odds_theoddsapi.py --raw data/oddsapi_wc_raw.json
"""
import json, sqlite3, argparse, sys
from collections import defaultdict

DB = "D:/Architecture/data/football_data.db"
PROVIDER = "theoddsapi"

# 同义词归一 (The Odds API 队名 <-> 我们 DB 队名)
SYN = {
    "USA": "USA", "United States": "USA",
    "England": "England", "Belgium": "Belgium",
}

def norm_team(t):
    t = (t or "").strip()
    return SYN.get(t, t)

def devig(odds_list):
    """odds_list: list of (home, draw, away) decimal odds. 返回去抽水共识赔率."""
    if not odds_list:
        return None
    inv_sum = []
    for h, d, a in odds_list:
        try:
            inv = 1.0/h + 1.0/d + 1.0/a
        except Exception:
            continue
        if inv <= 0:
            continue
        inv_sum.append((1.0/h, 1.0/d, 1.0/a, inv))
    if not inv_sum:
        return None
    # fair prob = inv_i / sum(inv); 平均跨庄家
    avg = [0.0, 0.0, 0.0]
    for ih, id_, ia, inv in inv_sum:
        avg[0] += ih/inv
        avg[1] += id_/inv
        avg[2] += ia/inv
    n = len(inv_sum)
    avg = [x/n for x in avg]
    # 转回赔率 (fair)
    fair = [ (1.0/p if p > 0 else None) for p in avg ]
    if any(x is None for x in fair):
        return None
    # 平均 overround (用于 return_rate)
    overrounds = [inv for *_ , inv in inv_sum]
    avg_over = sum(overrounds)/len(overrounds)
    return_rate = 1.0/avg_over if avg_over > 0 else None
    return fair[0], fair[1], fair[2], return_rate

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="D:/Architecture/data/oddsapi_wc_raw.json")
    args = ap.parse_args()

    raw = json.load(open(args.raw, encoding="utf-8"))
    if isinstance(raw, dict) and "message" in raw:
        print("API error:", raw); sys.exit(1)

    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    cur = con.cursor()

    # 预载所有世界杯场次 (用于映射)
    wc = {}
    for r in cur.execute("SELECT match_id, match_date, home_team_name, away_team_name, status FROM matches WHERE league_name='世界杯'"):
        wc[r["match_id"]] = dict(r)

    ingested = []
    not_in_db = []
    for e in raw:
        ht = norm_team(e.get("home_team")); at = norm_team(e.get("away_team"))
        ct = e.get("commence_time")
        try:
            ts = float(ct); dtu = __import__("datetime").datetime.fromtimestamp(ts, __import__("datetime").UTC)
            date_str = dtu.strftime("%Y-%m-%d")
        except Exception:
            date_str = "?"
        # 收集所有庄家 h2h
        odds_list = []
        bms = e.get("bookmakers", [])
        for bm in bms:
            for m in bm.get("markets", []):
                if m.get("key") != "h2h":
                    continue
                price = {}
                for o in m.get("outcomes", []):
                    price[o.get("name")] = o.get("price")
                if ht in price and at in price and "Draw" in price:
                    try:
                        odds_list.append((float(price[ht]), float(price["Draw"]), float(price[at])))
                    except Exception:
                        pass
        if not odds_list:
            continue
        fair = devig(odds_list)
        if not fair:
            continue
        h_o, d_o, a_o, rr = fair

        # 映射 match_id: 找 主客队一致 的世界杯场次 (允许日期辅助)
        mid = None
        for m in wc.values():
            if norm_team(m["home_team_name"]) == ht and norm_team(m["away_team_name"]) == at:
                mid = m["match_id"]; break
            if norm_team(m["home_team_name"]) == at and norm_team(m["away_team_name"]) == ht:
                mid = m["match_id"]; break
        if mid is None:
            not_in_db.append((date_str, ht, at, len(bms)))
            continue

        # 幂等: 先删 theoddsapi 旧行再插
        cur.execute("DELETE FROM odds WHERE match_id=? AND provider=?", (mid, PROVIDER))
        cur.execute(
            "INSERT INTO odds (match_id, provider, home_odds, draw_odds, away_odds, return_rate, odds_timestamp, created_at) VALUES (?,?,?,?,?,?,?, datetime('now'))",
            (mid, PROVIDER, round(h_o, 3), round(d_o, 3), round(a_o, 3), round(rr, 4) if rr else None, ct)
        )
        ingested.append((mid, date_str, ht, at, round(h_o,3), round(d_o,3), round(a_o,3), len(bms)))

    con.commit()
    con.close()

    print("=== 入库成功 (de-vig 共识赔率, provider=theoddsapi) ===")
    for mid, ds, ht, at, h, d, a, nb in ingested:
        print(f"  match_id={mid} | {ds} | {ht} vs {at} | H={h} D={d} A={a} | #bm={nb}")
    print(f"\n  共入库 {len(ingested)} 场")
    if not_in_db:
        print("\n=== 以下 Odds API 事件在 DB 中找不到对应场次(未入库) ===")
        for ds, ht, at, nb in not_in_db:
            print(f"  {ds} | {ht} vs {at} | #bm={nb}")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ingest_odds_digitalsanctuary.py
把从 digital-sanctuary.net (World Cup 2026 odds 聚合页) 抓到的 6 场 R16 真实 1X2 赔率,
按 (home_team_name, away_team_name) 定向映射到 matches.match_id, 幂等写入 odds 表.

来源说明: digital-sanctuary.net 该页未署名具体庄家, 但其值与 The Odds API de-vig 共识
高度吻合(阿根廷1.374 vs 1.395; 瑞士3.73/哥伦比亚2.3 vs 3.767/2.357), 视为可靠市场赔率.
如需 William Hill/Bet365/Interwetten 分庄家数值, 改从 oddsportal 单场页抓取.

用法:
  python ingest_odds_digitalsanctuary.py
"""
import sqlite3

DB = "D:/Architecture/data/football_data.db"
PROVIDER = "digitalsanctuary"

# (home_team_name, away_team_name, home_odds, draw_odds, away_odds)  -- 按本库主客方向
# 数据来自 digital-sanctuary.net/world-cup-2026-odds (decimal, R16 closing-ish)
ROWS = [
    ("Paraguay",  "France",   18.5,  7.95,  1.196),   # 07-04 France won 1-0
    ("Canada",    "Morocco",   4.6,  3.46,  1.964),   # 07-04 Morocco won 3-0
    ("Brazil",    "Norway",    1.846, 3.835, 4.69),   # 07-05 Norway won 2-1
    ("Mexico",    "England",   3.225, 3.28,  2.497),  # 07-05 England won 3-2
    ("Portugal",  "Spain",     4.0,  3.765, 2.001),   # 07-06 Spain won; home=Portugal, away=Spain
    ("USA",       "Belgium",   2.732, 3.545, 2.722),  # 07-06 Belgium won 4-1
]

def main():
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    cur = con.cursor()
    ok = []
    for h, a, ho, d, ao in ROWS:
        r = cur.execute(
            "SELECT match_id FROM matches WHERE league_name='世界杯' AND home_team_name=? AND away_team_name=?",
            (h, a)).fetchone()
        if not r:
            print(f"  !! 未找到映射: {h} vs {a} (跳过)")
            continue
        mid = r["match_id"]
        over = 1.0/ho + 1.0/d + 1.0/ao
        rr = round(1.0/over, 4) if over > 0 else None
        cur.execute("DELETE FROM odds WHERE match_id=? AND provider=?", (mid, PROVIDER))
        cur.execute(
            "INSERT INTO odds (match_id, provider, home_odds, draw_odds, away_odds, return_rate, odds_timestamp, created_at) "
            "VALUES (?,?,?,?,?,?, datetime('now'), datetime('now'))",
            (mid, PROVIDER, ho, d, ao, rr))
        ok.append((mid, h, a, ho, d, ao, rr))
    con.commit(); con.close()
    print(f"=== 入库 {len(ok)} 场 (provider={PROVIDER}) ===")
    for mid, h, a, ho, d, ao, rr in ok:
        print(f"  id={mid} | {h} vs {a} | H={ho} D={d} A={ao} | return_rate={rr}")

if __name__ == "__main__":
    main()

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
并 wc_xlsx_matches(2026, 权威市场均值) 赔率进主 odds 表
========================================================
目的: 让 WDL 回测/流水线对 2026 世界杯有完整赔率覆盖(此前主 odds 表靠 bf/ocr/pinnacle/vwc/default 覆盖 73 场).
源  : wc_xlsx_matches WHERE edition='2026' (88 场, 英文队名, 含 bet365/Pinnacle/Betfair 均值 oh/od/oa)
落点: odds 表, provider='xlsx2026'
匹配: matches.league_name='世界杯' 的 (home_team_name, away_team_name) 经 _canon_team 小写归一后与 xlsx (home, away) 对齐
安全: 仅 DELETE/INSERT provider='xlsx2026' 的行, 不动既有 bf/ocr/pinnacle/vwc/default 等 provider (防自举污染/误删)
幂等: 每次先删本 provider 旧行再插新行
"""
import sys, sqlite3
from pathlib import Path
from datetime import datetime

ARCH = Path(r"D:/Architecture")
PIPE = ARCH / "pipeline"
DB   = ARCH / "data" / "football_data.db"
sys.path.insert(0, str(PIPE))
import wc_engine as W

# 复刻 ETL 的 canonical 逻辑, 与现有 odds 表/matches 对齐口径完全一致
ZH2EN = {v: k for k, v in W._TEAM_ALIAS.items() if v}
SRC_ALIAS = {'佛得角共和国': 'cape verde', '巴拿马': 'panama', 'saudi': 'saudi arabia',
              'Bosnia & Herzegovina': 'Bosnia-H.', 'D.R. Congo': 'Congo DR'}
def to_en(name):
    if name is None:
        return None
    n = name.strip()
    n = SRC_ALIAS.get(n, n)
    e = ZH2EN.get(n, n)
    return W._canon_team(e).lower()

PROVIDER = "xlsx2026"
con = sqlite3.connect(str(DB)); con.row_factory = sqlite3.Row
cur = con.cursor()

# 1. 取 xlsx 2026 赔率
xlsx_rows = cur.execute(
    "SELECT home,away,date,oh,od,oa,hg,ag FROM wc_xlsx_matches WHERE edition='2026'"
).fetchall()
print(f"[源] wc_xlsx_matches 2026 行: {len(xlsx_rows)}")

# 2. 取 matches 世界杯候选 (match_id -> canonical (h,a))
wc_rows = cur.execute(
    "SELECT match_id, home_team_name, away_team_name, status FROM matches WHERE league_name='世界杯'"
).fetchall()
cand = {}
for r in wc_rows:
    h, a = to_en(r['home_team_name']), to_en(r['away_team_name'])
    if h and a:
        cand.setdefault((h, a), []).append(r)
print(f"[候选] matches 世界杯行: {len(wc_rows)}, 可 canonical 匹配对: {len(cand)}")

# 3. 幂等清理本 provider
cur.execute("DELETE FROM odds WHERE provider=?", (PROVIDER,))
print(f"[清理] 删除旧 provider='{PROVIDER}' 行完成")

now = datetime.now().isoformat(timespec="seconds")
inserted = 0; matched = 0; unmatched = []
for x in xlsx_rows:
    h, a = to_en(x['home']), to_en(x['away'])
    if not h or not a:
        unmatched.append((x['home'], x['away'], 'canon_fail')); continue
    ms = cand.get((h, a), [])
    if not ms:
        unmatched.append((x['home'], x['away'], 'no_match')); continue
    matched += 1
    oh, od, oa = float(x['oh']), float(x['od']), float(x['oa'])
    over = 1.0/oh + 1.0/od + 1.0/oa
    rr = round(over - 1.0, 4)
    for m in ms:
        cur.execute(
            """INSERT INTO odds(match_id, provider, home_odds, draw_odds, away_odds, return_rate, odds_timestamp, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (m['match_id'], PROVIDER, oh, od, oa, rr, x['date'], now))
        inserted += 1

con.commit(); con.close()
print(f"[写入] 插入 odds 行: {inserted} (匹配对={matched})")
if unmatched:
    print(f"[未匹配] {len(unmatched)} 场:")
    for u in unmatched:
        print("   ", u)
print("DONE")

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ETL: 世界杯赔率归一化进 odds 表 (数据层固化)
===========================================
源(优先级, 与 backtest_wc_90 完全一致):
  1. odds 表(已有 WC 行, 英文队名)
  2. pipeline/wc2026_backtest_final.py  -> bf.COMPLETED   (home,away,oh,od,oa,...,date)
  3. pipeline/worldcup2026_backtest.py   -> wb.MATCHES     (home,away,oh,od,oa,...)
  4. pipeline/archive/validate_wc2026.py -> vwc.WC2026     (date,home,away,...,ho,do,ao)
  5. data/wc2026_screenshot_odds_full.json (37场, 中文, oh/od/oa)
流程:
  - 复刻 backtest 的 put()/to_en() canonical 逻辑, 构建 odds_map[(canon_h,canon_a)]=(oh,od,oa,src,date)
  - 反查 matches.match_id (WC 全部 98 行, 含 scheduled), canonical 同款匹配
  - DELETE 旧 WC odds 行 -> INSERT 新行 (provider=胜出源, return_rate=overround-1)
  - 碰撞/未匹配日志
幂等: 每次清空 WC 的 odds 再重插。
"""
import sys, os, json, importlib.util, sqlite3
from pathlib import Path
from collections import defaultdict
from datetime import datetime

ARCH = Path(r"D:/Architecture")
PIPE = ARCH / "pipeline"
DB = ARCH / "data" / "football_data.db"
sys.path.insert(0, str(PIPE)); sys.path.insert(0, str(PIPE / "archive"))
import wc_engine as W

ZH2EN = {v: k for k, v in W._TEAM_ALIAS.items()}
# 源侧别名 -> canonical (OCR/脚本源用了 佛得角共和国/saudi/巴拿马 等变体, DB 存 Cape Verde/saudi arabia/Panama)
SRC_ALIAS = {
    '佛得角共和国': 'cape verde',
    '巴拿马': 'panama',
    'saudi': 'saudi arabia',
}
def to_en(name):
    if name is None:
        return None
    n = name.strip()
    n = SRC_ALIAS.get(n, n)        # 源变体归一
    e = ZH2EN.get(n, n)
    return W._canon_team(e).lower()   # 统一小写, 消除 _canon_team 大小写不一致(Panama vs panama)导致的匹配失败

# ── 1. 构建 consolidated odds_map (优先级: odds表 > bf > wb > vwc > ocr) ──
odds_map = {}   # (canon_h, canon_a) -> (oh, od, oa, src, date)
src_count = defaultdict(int)
def put(h_zh, a_zh, oh, od, oa, src, date=None):
    if not oh or not od or not oa:
        return
    h, a = to_en(h_zh), to_en(a_zh)
    if not h or not a:
        return
    key = (h, a)
    if key not in odds_map:   # 先到先得
        odds_map[key] = (float(oh), float(od), float(oa), src, date)
        src_count[src] += 1

con = sqlite3.connect(str(DB)); con.row_factory = sqlite3.Row
cur = con.cursor()

# 1. odds 表(已有) -- 注: odds 表是 ETL 的输出目的地, 不是输入源; 真实外部源见 2/3.
#    首轮跑过一次后 odds 表已含 ETL 产物, 若再读会自举污染 provenance, 故此处不读。
#    如确需保留某条"原始 odds 表独占"的赔率, 应在 ETL 外单独 seed, 不在此循环内。

# 2. 3 脚本源
def load_mod(n, p):
    try:
        s = importlib.util.spec_from_file_location(n, str(p))
        m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
        return m
    except Exception as e:
        print(f"  [WARN] 加载源 {n} 失败: {e}")
        return None

bf = load_mod("bf", PIPE / "wc2026_backtest_final.py")
wb = load_mod("wb", PIPE / "worldcup2026_backtest.py")
vwc = load_mod("vwc", PIPE / "archive" / "validate_wc2026.py")

if bf is not None:
    for m in bf.COMPLETED:
        put(m[0], m[1], m[2], m[3], m[4], 'bf', m[9] if len(m) > 9 else None)
if wb is not None:
    for m in wb.MATCHES:
        put(m[0], m[1], m[2], m[3], m[4], 'wb', m[9] if len(m) > 9 else None)
if vwc is not None:
    for m in vwc.WC2026:
        put(m[1], m[2], m[6], m[7], m[8], 'vwc', m[0])

# 3. OCR json
ocr_path = ARCH / "data" / "wc2026_screenshot_odds_full.json"
if ocr_path.exists():
    ocr = json.load(open(str(ocr_path), encoding="utf-8"))
    for m in ocr:
        put(m['home'], m['away'], m['oh'], m['od'], m['oa'], 'ocr', m.get('date'))

print(f"[源] 去重后独立比赛赔率: {len(odds_map)} 场  {dict(src_count)}")

# ── 2. 反查 match_id ──
wc_rows = cur.execute("""SELECT match_id, home_team_name, away_team_name, status
  FROM matches WHERE league_name='世界杯'""").fetchall()
cand = defaultdict(list)
for r in wc_rows:
    h, a = to_en(r['home_team_name']), to_en(r['away_team_name'])
    if h and a:
        cand[(h, a)].append(r)

wc_ids = [r['match_id'] for r in wc_rows]
placeholders = ",".join("?" * len(wc_ids)) if wc_ids else "NULL"
cur.execute(f"DELETE FROM odds WHERE match_id IN ({placeholders})", wc_ids)
print(f"[清理] 删除旧 WC odds 行 (match_ids={len(wc_ids)})")

now = datetime.now().isoformat(timespec="seconds")
inserted = 0
matched_pairs = 0
unmatched = []
collisions = []
for key, (oh, od, oa, src, date) in odds_map.items():
    matches = cand.get(key, [])
    if not matches:
        unmatched.append(key)
        continue
    matched_pairs += 1
    if len(matches) > 1:
        collisions.append((key, [m['match_id'] for m in matches], src))
    over = 1.0 / oh + 1.0 / od + 1.0 / oa
    rr = round(over - 1.0, 4)
    ts = date if date else now
    for m in matches:   # 每个匹配到的 match_id 都插一行(保留回测"同对皆得同赔率"语义, 更完整)
        cur.execute("""INSERT INTO odds(match_id, provider, home_odds, draw_odds, away_odds, return_rate, odds_timestamp, created_at)
          VALUES(?,?,?,?,?,?,?,?)""",
          (m['match_id'], src, oh, od, oa, rr, ts, now))
        inserted += 1

con.commit()
con.close()

print(f"[写入] 插入 odds 行: {inserted}  (匹配对={matched_pairs})")
if unmatched:
    print(f"[未匹配] {len(unmatched)} 个赔率对无对应 WC match:")
    for k in unmatched:
        print("   ", k)
if collisions:
    print(f"[碰撞] {len(collisions)} 个 (home,away) 命中多个 match_id (均插入):")
    for k, ids, src in collisions:
        print("   ", k, "->", ids, f"[{src}]")
print("DONE")

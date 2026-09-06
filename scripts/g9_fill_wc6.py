#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
G9 · WC2026 仍缺 6 场赔率幂等回填 (手动/付费档入口)
==================================================
背景 (deliverables/oddsapi_fill_report.md, 2026-07-07):
  - OGDS = The Odds API (the-odds-api.com), 非 oddpapi.io.
  - 免费档 historical 被墙 (HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN), 这 6 场已完赛拉不到.
  - 6 场 match_id: 537375 / 537376 / 537377 / 537378 / 2130600 / 537380
  - 537380 USA-Belgium 疑似 phantom; 真实盘为 Spain-Belgium(07-10), 需你核实.

本脚本定位 = "下半句准备好": 一旦你升级付费档 key 或手动贴赔率, 一键幂等入库.
  - 幂等 upsert 到 odds 表 (provider 可配, 默认 manual)
  - 若 odds_features 缺该场 (按 主客队名+日期), 同步补 close_h/d/a (open=close, historical 无初盘)

用法:
  python g9_fill_wc6.py --json g9_wc6_odds.json
  # g9_wc6_odds.json: [{"match_id":537375,"home_odds":1.4,"draw_odds":5.0,"away_odds":11.6,"return_rate":0.95}, ...]
"""
import sqlite3, json, argparse, sys, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'football_data.db')

# G9 目标 6 场 (oddsapi_fill_report.md)
WC6 = {537375, 537376, 537377, 537378, 2130600, 537380}
# phantom 提示: 真实盘为 Spain-Belgium(07-10), USA-Belgium 疑似陈旧/错录
PHANTOM = {537380: 'USA-Belgium 疑似 phantom; 真实盘为 Spain-Belgium(07-10), 请核实 match_id 真实性'}

SYN = {"USA": "USA", "United States": "USA", "England": "England", "Belgium": "Belgium"}
def norm_team(t):
    t = (t or "").strip()
    return SYN.get(t, t)


def load_records(path):
    raw = json.load(open(path, encoding='utf-8'))
    if isinstance(raw, dict):
        raw = raw.get('records', raw.get('odds', []))
    return raw


def upsert(db_path, match_id, provider, h, d, a, return_rate=None, verbose=True):
    """幂等入库 odds 表 + 同步 odds_features (按队名). 返回是否入库成功."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    # 验证 match_id 在世界杯
    row = cur.execute(
        "SELECT match_id, match_date, home_team_name, away_team_name FROM matches "
        "WHERE match_id=? AND league_name='世界杯'", (match_id,)
    ).fetchone()
    if not row:
        if verbose:
            print(f'  [skip] match_id={match_id} 不在世界杯 matches 表')
        con.close()
        return False
    # 幂等 odds 表
    cur.execute("DELETE FROM odds WHERE match_id=? AND provider=?", (match_id, provider))
    cur.execute(
        "INSERT INTO odds (match_id, provider, home_odds, draw_odds, away_odds, return_rate, odds_timestamp, created_at) "
        "VALUES (?,?,?,?,?,?,?, datetime('now'))",
        (match_id, provider, round(h, 3), round(d, 3), round(a, 3),
         round(return_rate, 4) if return_rate else None, None)
    )
    # odds_features 同步 (按队名, 无则插, 有则更新 close)
    ht, at = norm_team(row['home_team_name']), norm_team(row['away_team_name'])
    md = row['match_date']
    exist = cur.execute(
        "SELECT 1 FROM odds_features WHERE home_team=? AND away_team=? AND match_date=?",
        (ht, at, md)
    ).fetchone()
    if exist:
        cur.execute(
            "UPDATE odds_features SET close_h=?, close_d=?, close_a=? "
            "WHERE home_team=? AND away_team=? AND match_date=?",
            (round(h, 3), round(d, 3), round(a, 3), ht, at, md)
        )
    else:
        cur.execute(
            "INSERT INTO odds_features (home_team, away_team, match_date, "
            "open_h, open_d, open_a, close_h, close_d, close_a) VALUES (?,?,?,?,?,?,?,?,?)",
            (ht, at, md, round(h, 3), round(d, 3), round(a, 3), round(h, 3), round(d, 3), round(a, 3))
        )
    con.commit()
    con.close()
    if verbose:
        flag = ' [phantom警告]' if match_id in PHANTOM else ''
        print(f'  [ok] match_id={match_id} ({ht} vs {at}) odds[{provider}] H={h} D={d} A={a}{flag}')
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', required=True, help='6场赔率 JSON 路径')
    ap.add_argument('--provider', default='manual')
    ap.add_argument('--db', default=DB)
    a = ap.parse_args()

    recs = load_records(a.json)
    print(f'[G9] 读到 {len(recs)} 条赔率记录, 目标 6 场={sorted(WC6)}')
    done = 0
    for r in recs:
        mid = int(r['match_id'])
        if mid not in WC6:
            print(f'  [skip] match_id={mid} 非 G9 目标')
            continue
        if mid in PHANTOM:
            print(f'  [WARN] {PHANTOM[mid]}')
        ok = upsert(a.db, mid, a.provider,
                    float(r['home_odds']), float(r['draw_odds']), float(r['away_odds']),
                    float(r['return_rate']) if r.get('return_rate') else None)
        if ok:
            done += 1
    print(f'\n[G9] 完成, 入库 {done} 场 (剩余需付费档/手动补充)')


if __name__ == '__main__':
    main()

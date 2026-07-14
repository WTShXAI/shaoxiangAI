# -*- coding: utf-8 -*-
"""把 2026 小组赛真实赔率并入 wc_all_matches (edition='2026'), 用于 argmax 基线对照."""
import sqlite3, os, json
from wc_historical_ingest import canon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'football_data.db')
ODDS = os.path.join(ROOT, 'data', 'wc2026_group_odds_final.json')

def main():
    d = json.load(open(ODDS, encoding='utf-8'))
    matches = d.get('matches', [])
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    updated = 0
    for m in matches:
        h = canon(m.get('home_team')); a = canon(m.get('away_team'))
        oh = m.get('1x2_home'); od = m.get('1x2_draw'); oa = m.get('1x2_away')
        if not (h and a and oh and od and oa):
            continue
        # 正向
        r = c.execute(
            "UPDATE wc_all_matches SET oh=?,od=?,oa=? WHERE edition='2026' AND home=? AND away=?",
            (oh, od, oa, h, a))
        if c.rowcount == 0:
            # 反向尝试
            c.execute(
                "UPDATE wc_all_matches SET oh=?,od=?,oa=? WHERE edition='2026' AND home=? AND away=?",
                (oa, od, oh, a, h))
        if c.rowcount > 0:
            updated += 1
    conn.commit()
    conn.close()
    print(f"[merge] 2026 odds updated={updated} / source={len(matches)}")

if __name__ == '__main__':
    main()

"""
用队名匹配 match_key，拉取命中波胆的真实赔率结构
重点验证：哈萨克甲 0-0@42 是否与 1X2/OU 定价矛盾
"""
import sqlite3, json

DATA = "D:/Architecture/data"
DB = f"{DATA}/events.db"

def q(sql, p=()):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    r = [dict(x) for x in c.execute(sql, p).fetchall()]
    c.close(); return r

# (home_key, away_key, 命中比分, 命中赔率)
targets = [
    ("凯拉特扎斯塔尔", "奥杜斯基克学院", "0-0", 42.0, "哈萨克甲"),
    ("宁比U19", "哥本哈根U19", "3-3", 34.0, "丹麦U19"),
    ("多瑙斯特雷达U19", "伯德布雷佐夫U19", "3-2", 17.5, "斯洛伐克U19"),
    ("克拉斯诺耶兹纳米亚", "布良斯克戴拿模", "2-0", 17.0, "俄杯资格赛"),
    ("恩津加乌尼奥", "万博英雄", "0-2", 12.0, "安哥拉班图"),
]

for hk, ak, hit, ho, lg in targets:
    keys = q("SELECT DISTINCT match_key FROM odds_snapshots WHERE match_key LIKE ? AND match_key LIKE ?",
             (f"%{hk}%", f"%{ak}%"))
    print(f"\n{'='*64}\n{lg}: {hk} vs {ak} | 命中 {hit}@{ho}")
    print(f"匹配 match_key 数: {len(keys)}")
    for k in keys[:3]:
        print(f"  match_key = {k['match_key']}")
        snaps = q("SELECT market, selection, odds, line FROM odds_snapshots WHERE match_key=? ORDER BY market, odds",
                  (k['match_key'],))
        mkt = {}
        for s in snaps: mkt.setdefault(s['market'], []).append(s)
        for mk, rows in mkt.items():
            print(f"    [{mk}] {len(rows)}行:")
            for r in rows[:16]:
                mark = " <== 命中" if mk=='CS' and r['selection']==hit else ""
                line = f" line={r['line']}" if r['line'] else ""
                print(f"      {str(r['selection']):14s} @ {r['odds']:8.2f}{line}{mark}")

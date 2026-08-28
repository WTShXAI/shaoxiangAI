"""
用户2天真实 obscure 联赛波胆下注：挖数据库存档 CS 赔率 + 历史 0-0 频率
目标场次：
 1. 哈萨克斯坦甲  凯拉特斯科斯塔尔 vs 奥杜斯克学院  0-0@42
 2. 安哥拉班图    恩津加乌尼奥 vs 万博英雄          0-2@12 (滚球)
 3. 斯洛伐克U19   多瑙斯特雷达U19 vs 伯德布雷佐夫U19 3-2@17.5
 4. 丹麦U19       宁比U19 vs 哥本哈根U19            3-3@34
 5. 俄罗斯杯资格赛 克拉斯诺亚兹纳米亚 vs 布良斯克戴拿模 2-0@17
"""
import sqlite3, json

DATA = "D:/Architecture/data"

def q(db, sql, p=()):
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    r = [dict(x) for x in c.execute(sql, p).fetchall()]
    c.close(); return r

def find_matches(db):
    print(f"\n##### {db} matches 模糊搜队名/联赛 #####")
    rows = q(db, """
    SELECT mid, league, home, away, kickoff, score_home, score_away, status
    FROM matches
    WHERE home LIKE '%凯拉特%' OR away LIKE '%奥杜斯克%'
       OR home LIKE '%恩津加%' OR away LIKE '%万博%'
       OR home LIKE '%多瑙%' OR away LIKE '%伯德%'
       OR home LIKE '%宁比%' OR away LIKE '%哥本哈根%'
       OR home LIKE '%克拉斯%' OR away LIKE '%布良%'
       OR home LIKE '%戴拿模%'
    ORDER BY kickoff
    """)
    print(f"匹配 {len(rows)} 行")
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return rows

def dump_cs(db, rows):
    print(f"\n##### {db} odds_snapshots CS (命中波胆赔率结构) #####")
    for r in rows:
        mid = r['mid']
        cs = q(db, """
        SELECT selection, odds, market, timestamp FROM odds_snapshots
        WHERE mid=? AND market='CS'
        ORDER BY timestamp DESC, odds
        """, (mid,))
        if cs:
            print(f"\n-- mid={mid} {r['home']} vs {r['away']} ({r['league']}) 赛果 {r['score_home']}-{r['score_away']}")
            print(f"   CS 快照 {len(cs)} 行, 取最新一批:")
            # 取最新时间戳的一批
            latest = cs[0]['timestamp']
            batch = [c for c in cs if c['timestamp']==latest]
            if not batch: batch = cs[:15]
            for c in batch[:15]:
                mark = " <== 命中" if c['selection']==r['hit_score'] else ""
                print(f"     {c['selection']:6s} @ {c['odds']:7.2f}{mark}")
        else:
            print(f"-- mid={mid} {r['home']} vs {r['away']}: 无 CS 快照")

# 给命中比分标记
def tag_hit(rows, hits):
    for r in rows:
        h = hits.get((r['home'], r['away']))
        if h: r['hit_score'] = h
    return rows

hits = {
    ('凯拉特斯科斯塔尔','奥杜斯克学院'): '0-0',
    ('恩津加乌尼奥','万博英雄'): '0-2',
    ('多瑙斯特雷达U19','伯德布雷佐夫U19'): '3-2',
    ('宁比U19','哥本哈根U19'): '3-3',
    ('克拉斯诺亚兹纳米亚','布良斯克戴拿模'): '2-0',
}

for db in [f"{DATA}/events.db", f"{DATA}/football_data.db"]:
    try:
        rows = find_matches(db)
        rows = tag_hit(rows, hits)
        if rows:
            dump_cs(db, rows)
    except Exception as e:
        print(f"{db} 查询失败: {e}")

# 历史 0-0 频率对照：football_data.db matches 表，按联赛统计
print("\n\n===== football_data.db 历史 0-0 频率（按联赛，样本>=200场）=====")
try:
    rows = q(f"{DATA}/football_data.db", """
    SELECT league_name,
           COUNT(*) AS n,
           SUM(CASE WHEN home_score=0 AND away_score=0 THEN 1 ELSE 0 END) AS zz
    FROM matches
    WHERE home_score IS NOT NULL AND away_score IS NOT NULL
    GROUP BY league_name
    HAVING n >= 200
    ORDER BY CAST(zz AS REAL)/n DESC
    LIMIT 25
    """)
    print(f"{'联赛':30s} {'场次':>8s} {'0-0数':>8s} {'0-0率':>8s}")
    for r in rows:
        rate = r['zz']/r['n']
        print(f"{str(r['league_name']):30s} {r['n']:8d} {r['zz']:8d} {rate*100:7.2f}%")
except Exception as e:
    print(f"历史统计失败: {e}")

# 0-0 @42 的隐含概率对照
print("\n===== 0-0 @42 隐含概率 vs 各联赛真实0-0率 =====")
implied = 1/(42*1.10)
print(f"0-0@42 去水10%后隐含概率 = {implied*100:.2f}%")
print("若某联赛真实0-0率显著高于此值，则该赔率存在定价偏差(+EV空间)")

"""联赛赛果分布 -> OU 大球概率 (按盘口线).

核心认知: 赛果(总进球)就是 OU 的结算面. 给定盘口线 L(半盘, 如 2.5),
大球概率 = P(总进球 > L) = P(总进球 >= ceil(L)) 直接由赛果分布算出,
不需要任何盘口赔率. 这是 OU 的赛果侧 fair probability, 13 赛季 31 万场.

口径: historical_matches, 排除友谊赛, 仅保留场数>=100 的联赛(统计显著).
输出: data/league_ou_prob.csv (league, matches, avg_goals, over_0.5..under_4.5)
"""
import sqlite3, csv

DB = "data/football_data.db"
OUT = "data/league_ou_prob.csv"
LINES = [0.5, 1.5, 2.5, 3.5, 4.5]  # 标准半盘; over_L = P(总进球 >= int(L)+1)

c = sqlite3.connect(DB)
cur = c.cursor()
cur.execute("""
SELECT league_name, (home_score+away_score) AS tg, COUNT(*)
FROM historical_matches
WHERE home_score IS NOT NULL AND away_score IS NOT NULL
  AND league_name NOT LIKE '%友谊%'
GROUP BY league_name, tg
""")
raw = {}
for lg, tg, n in cur.fetchall():
    raw.setdefault(lg, {})[int(tg)] = n
c.close()

# 过滤场数>=100
leagues = {lg: cnt for lg, cnt in raw.items() if sum(cnt.values()) >= 100}

cols = ["league", "matches", "avg_goals"] + \
       [f"over_{int(l)}" for l in LINES] + [f"under_{int(l)}" for l in LINES]

rows = []
for lg, cnt in leagues.items():
    m = sum(cnt.values())
    avg = sum(k * v for k, v in cnt.items()) / m
    over = {}
    for l in LINES:
        thr = int(l) + 1  # 半盘: 2.5 -> >=3
        over[l] = sum(v for k, v in cnt.items() if k >= thr) / m
    rows.append((lg, m, avg, over))

rows.sort(key=lambda r: -r[1])

with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(cols)
    for lg, m, avg, over in rows:
        w.writerow([lg, m, round(avg, 3)] +
                   [round(over[l], 4) for l in LINES] +
                   [round(1 - over[l], 4) for l in LINES])

print(f"入选联赛数 = {len(rows)}  (非友谊, 场数>=100)")
print(f"CSV -> {OUT}\n")

# 摘要: 主流联赛
print("=== 主流联赛 OU 大球概率 (赛果侧) ===")
print(f"{'联赛':<10}{'场':>6}{'均进':>6}{'ov1.5':>7}{'ov2.5':>7}{'ov3.5':>7}{'un2.5':>7}")
MAIN = ["英超", "西甲", "意甲", "德甲", "法甲", "英冠", "英甲", "西丙", "德甲", "荷甲", "葡超", "巴甲", "阿甲", "日职", "韩K"]
seen = 0
for lg, m, avg, over in rows:
    if lg in MAIN and seen < 15:
        print(f"{lg:<10}{m:>6}{avg:>6.2f}{over[1.5]*100:>6.1f}%{over[2.5]*100:>6.1f}%{over[3.5]*100:>6.1f}%{(1-over[2.5])*100:>6.1f}%")
        seen += 1

# 大球联赛 (over2.5 最高)
print("\n=== over2.5 概率最高 (大球场次多) ===")
for lg, m, avg, over in sorted(rows, key=lambda r: -r[3][2.5])[:6]:
    print(f"  {lg:<10} 场={m:<5} 均进={avg:.2f} ov2.5={over[2.5]*100:.1f}% ov3.5={over[3.5]*100:.1f}%")

# 小球联赛 (over2.5 最低)
print("\n=== over2.5 概率最低 (小球联赛) ===")
for lg, m, avg, over in sorted(rows, key=lambda r: r[3][2.5])[:6]:
    print(f"  {lg:<10} 场={m:<5} 均进={avg:.2f} ov2.5={over[2.5]*100:.1f}% un2.5={(1-over[2.5])*100:.1f}%")

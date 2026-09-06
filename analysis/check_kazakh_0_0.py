"""
聚焦：哈萨克甲 凯拉特扎斯塔尔 vs 奥杜斯基克学院
确认 CS 0-0 实际赔率，并看完整波胆曲线 vs 1X2/OU 是否矛盾
"""
import sqlite3
DB = "D:/Architecture/data/events.db"
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row

mk = "凯拉特扎斯塔尔 vs 奥杜斯基克学院"

print("=== 该场 CS 波胆完整赔率曲线 (按赔率升序, 取每组selection的最新赔率) ===")
rows = c.execute("""
SELECT selection,
       (SELECT odds FROM odds_snapshots s2
        WHERE s2.match_key=s1.match_key AND s2.market='CS'
          AND s2.selection=s1.selection
        ORDER BY captured_at DESC LIMIT 1) AS latest_odds,
       COUNT(*) AS n
FROM odds_snapshots s1
WHERE match_key=? AND market='CS'
GROUP BY selection
ORDER BY latest_odds
""", (mk,)).fetchall()
for r in rows:
    print(f"  {r['selection']:6s} @ {r['latest_odds']:8.2f}  (快照{r['n']}条)")

print("\n=== 0-0 专项 ===")
zz = c.execute("SELECT odds, captured_at FROM odds_snapshots WHERE match_key=? AND market='CS' AND selection='0-0' ORDER BY captured_at DESC LIMIT 5", (mk,)).fetchall()
for r in zz:
    print(f"  0-0 @ {r['odds']:.2f}  ({r['captured_at']})")

print("\n=== 该场 1X2 / 关键 OU 市场(最新快照) ===")
for mkt in ['1X2', 'OU_1.50', 'OU_2.00', 'OU_2.50', 'OU_2.75']:
    rows = c.execute("""
    SELECT selection, odds, line FROM odds_snapshots
    WHERE match_key=? AND market=?
    ORDER BY captured_at DESC LIMIT 4
    """, (mk, mkt)).fetchall()
    if rows:
        print(f"  [{mkt}] " + " | ".join(f"{r['selection']} {r['odds']:.2f}" + (f"(L{r['line']})" if r['line'] else "") for r in rows))

# 隐含概率对照
print("\n=== 隐含概率对照 ===")
implied_00 = 1/(42*1.10)
print(f"0-0@42 去水10%后隐含概率 = {implied_00*100:.2f}%")
print("OU_2.50 under 低水(1.48) -> 总进球<=2 隐含概率约 61%")
print("1X2 draw 1.22 / home 1.24 -> 主队大热+平局概率高")
print("=> 低进球+平局倾向的盘口下, 0-0 真实概率应显著高于 2.16%, 42倍定价偏低 => +EV 信号")
c.close()

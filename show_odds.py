import sqlite3
con = sqlite3.connect(r'D:\Architecture\data\events.db')

# 重点比赛：巴塞罗那、国际米兰、AC米兰
for match_name in ['巴塞罗那', '国际米兰', 'AC米兰']:
    rows = con.execute("""
        SELECT play_name, option_name, odds FROM match_odds 
        WHERE home=? OR away=? ORDER BY play_name, odds
    """, (match_name, match_name)).fetchall()
    if rows:
        # 找出主客队
        info = con.execute("SELECT home, away, league FROM match_odds WHERE home=? OR away=? LIMIT 1", (match_name, match_name)).fetchone()
        print(f"\n=== {info[0]} vs {info[1]} [{info[2]}] ===")
        cur_play = ''
        for r in rows:
            if r[0] != cur_play:
                print(f"\n  【{r[0]}】")
                cur_play = r[0]
            print(f"    {r[1]:8s}  {r[2]:.2f}")

# 中奖波胆对比
print("\n\n=== 中奖波胆对比（已中奖比分 vs 未开始比赛中的同比分赔率）===")
win_refs = [
    ('1:0', 2.23, '厄瓜多尔甲级联赛'),
    ('1:2', 11.5, '世界杯2026'),
    ('3:2', 56.0, '韩国杯'),
    ('0:2', 27.0, '韩国杯'),
]
for score, odds_val, league in win_refs:
    rows = con.execute("""
        SELECT home, away, league, odds FROM match_odds 
        WHERE play_name='全场波胆' AND option_name=? 
        ORDER BY odds LIMIT 8
    """, (score,)).fetchall()
    print(f"\n{score} @ {odds_val} [{league}] 在未开始比赛中:")
    for r in rows:
        print(f"  {r[0]:20s} vs {r[1]:20s} [{r[2]:12s}] -> {r[3]:.2f}")

con.close()

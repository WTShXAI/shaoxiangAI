"""
补全 2026 WC 最后两场未进行的比赛 (fixture, 未进行 => 比分NULL, status='scheduled').

依据:
- cupngoal 2026 WC 完整赛程(112场) 最后两场 = 07-12 Argentina-Switzerland / 07-14 France-Spain(决赛)
- 库内 matches 表 WC fixture 链(league_id=2000, ID空间 213060x) 仅覆盖到 07-11 (Norway-England, id 2130605)
  => 07-12 / 07-14 两场在 matches 表完全缺失, 需补全.
- 与现有 5 条 WC NULL 比分 fixture (2130602~2130606) 保持同构: league_id=2000, status='scheduled', 比分NULL.

用户指令: "世界杯还有最后两场未进行的比赛, 补全缺失的场次" => 仅补 fixture, 不伪造赛果.
"""
import sqlite3, os
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "football_data.db")

# (match_id, date, time, home_id, home, away_id, away, matchday)
FIXTURES = [
    (2130607, "2026-07-12", "12:00", 762, "Argentina",    788, "Switzerland", 7),
    (2130608, "2026-07-14", "12:00", 773, "France",       760, "Spain",       7),
]
LEAGUE_ID = 2000
LEAGUE_NAME = "世界杯"
NOW = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

con = sqlite3.connect(DB)
cur = con.cursor()
inserted = []
for mid, date, t, hid, hn, aid, an, md in FIXTURES:
    # 防重复
    ex = cur.execute("SELECT 1 FROM matches WHERE match_id=?", (mid,)).fetchone()
    if ex:
        print(f"  SKIP {mid} {hn}-{an} 已存在")
        continue
    cur.execute(
        """INSERT INTO matches
           (match_id, match_date, match_time, league_id, league_name,
            home_team_id, home_team_name, away_team_id, away_team_name,
            home_score, away_score, final_result, status, matchday,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,'scheduled',?,?,?)""",
        (mid, date, t, LEAGUE_ID, LEAGUE_NAME, hid, hn, aid, an, md, NOW, NOW),
    )
    inserted.append((mid, date, hn, an))
con.commit()

print(f"补全 {len(inserted)} 场 WC fixture:")
for mid, date, hn, an in inserted:
    print(f"  + {mid}  {date}  {hn} vs {an}  [世界杯 / matchday=7 / scheduled]")
con.close()

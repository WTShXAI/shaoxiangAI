"""构建 OU 开盘数据集: 从 events.db odds_snapshots 还原每场每条全场比赛 OU 线的开盘大小球赔率,
join matches + match_outcomes 拿赛果(总进球)与联赛, 物化到独立分析库 ou_opening_analysis.db.
所有建表都在分析库完成, events.db 仅以 ATTACH 只读挂载, 绝不污染生产库.

SSoT 铁律(2026-08-20 加固): 开盘价 = 该 (match_key, OU线) 在 **开赛前** (captured_at < kickoff + 5min宽限)
最早 captured_at 快照的 over/under 赔率. 仅全场比赛 OU (market LIKE 'OU_%' 且不含 1H/2H).
排除虚拟联赛 (is_virtual=1).

根因修复: 旧脚本用 MIN(captured_at) 抓最早快照即当"开盘", 但**未校验比赛是否未开赛**,
导致 23% 比赛的"开盘"实为进球后活盘价(莫罗多莫科: OU 开赛前 0 条快照, 最早 13:38 已是活盘).
现已加 `captured_at < kickoff+300s` 闸门; 无开赛前 OU 快照的 (match,line) 直接剔除,
**禁止用活盘价伪造开盘**. ou_meta 新增开盘覆盖率统计, 量化被剔除的污染样本.
"""
import sqlite3, time, os

SRC = 'data/events.db'
OUT = 'data/ou_opening_analysis.db'

OPENING_GRACE = 300  # 秒: 开赛前 5 分钟宽限(采集器延迟), 超出则视为无真实开盘价

t0 = time.time()
ana = sqlite3.connect(OUT, timeout=120)
ana.execute("PRAGMA busy_timeout=60000")
acur = ana.cursor()
# 只读挂载生产库
acur.execute(f"ATTACH DATABASE '{SRC}' AS gq")

# kickoff 双格式 → unix 时间戳 (与 live_goal_probe._parse_kickoff 语义一致):
#   'YYYY-MM-DD HH:MM' (GMT+8 裸) → strftime 视为 UTC 再减 8h
#   'YYYY-MM-DDTHH:MM:SSZ' (UTC ISO) → 去 Z 当 UTC
# 其他格式 strftime 返回 NULL → kots=NULL → 该场无开盘(诚实降级).
KICK_CTE = """
SELECT match_key,
  CASE
    WHEN kickoff LIKE '%Z' THEN strftime('%s', REPLACE(kickoff,'Z',''))
    WHEN kickoff LIKE '%T%' THEN strftime('%s', REPLACE(kickoff,'T',' '))
    ELSE strftime('%s', kickoff || ':00') - 8*3600
  END AS kots
FROM gq.matches
WHERE kickoff IS NOT NULL
"""

acur.execute("DROP TABLE IF EXISTS opening_ou")
acur.execute("DROP TABLE IF EXISTS ou_labeled")
acur.execute("DROP TABLE IF EXISTS ou_clean")
acur.execute("DROP TABLE IF EXISTS ou_meta")

print("[1/3] 还原开盘大小球赔率 (扫全场 OU 快照, 仅取开赛前快照当开盘) ...")
acur.execute(f"""
CREATE TABLE opening_ou AS
WITH kick AS ({KICK_CTE}),
ranked AS (
  SELECT s.match_key, s.market, s.selection, s.odds,
         ROW_NUMBER() OVER (PARTITION BY s.match_key, s.market, s.selection ORDER BY s.captured_at ASC) AS rn
  FROM gq.odds_snapshots s
  JOIN kick k ON s.match_key = k.match_key
  WHERE s.market LIKE 'OU_%' AND s.market NOT LIKE '%1H%' AND s.market NOT LIKE '%2H%'
    AND s.selection IN ('over','under')
    AND k.kots IS NOT NULL
    AND s.captured_at < k.kots + {OPENING_GRACE}   -- 仅开赛前(含5min宽限)快照当开盘
)
SELECT o.match_key,
       CAST(SUBSTR(o.market,4) AS REAL) AS line,
       o.odds AS over_odds,
       u.odds AS under_odds
FROM (SELECT match_key, market, odds FROM ranked WHERE selection='over'  AND rn=1) o
JOIN (SELECT match_key, market, odds FROM ranked WHERE selection='under' AND rn=1) u
  ON o.match_key=u.match_key AND o.market=u.market
""")
ana.commit()
n = acur.execute("SELECT COUNT(*) FROM opening_ou").fetchone()[0]
print(f"    开盘样本 (match,line) = {n}  (%.1fs)" % (time.time()-t0))

print("[1b] 统计开盘覆盖率 (量化被剔除的活盘污染样本) ...")
n_ou_matches = acur.execute("""
  SELECT COUNT(DISTINCT match_key) FROM gq.odds_snapshots
  WHERE market LIKE 'OU_%' AND market NOT LIKE '%1H%' AND market NOT LIKE '%2H%'
""").fetchone()[0]
n_ou_pre = acur.execute(f"""
  SELECT COUNT(DISTINCT s.match_key) FROM gq.odds_snapshots s
  JOIN ({KICK_CTE}) k ON s.match_key = k.match_key
  WHERE s.market LIKE 'OU_%' AND s.market NOT LIKE '%1H%' AND s.market NOT LIKE '%2H%'
    AND k.kots IS NOT NULL
    AND s.captured_at < k.kots + {OPENING_GRACE}
""").fetchone()[0]
n_no_open = n_ou_matches - n_ou_pre
print(f"    有全场OU快照的比赛 = {n_ou_matches}")
print(f"    有真实开赛前OU的比赛 = {n_ou_pre}")
print(f"    无真实开盘价(活盘污染, 已剔除) = {n_no_open} ({100.0*n_no_open/max(n_ou_matches,1):.1f}%)")

print("[2/3] join 赛果 (matches.match_key -> match_outcomes 经 home/away/league/kickoff) ...")
acur.execute("""
CREATE TABLE ou_labeled AS
SELECT
  oo.match_key, oo.line, oo.over_odds, oo.under_odds,
  mo.score_home, mo.score_away,
  (mo.score_home + mo.score_away) AS total_goals,
  mo.league, mo.is_virtual, mo.kickoff,
  (1.0/oo.over_odds) / (1.0/oo.over_odds + 1.0/oo.under_odds) AS implied_p_over,
  CASE WHEN (mo.score_home+mo.score_away) > oo.line THEN 1
       WHEN (mo.score_home+mo.score_away) < oo.line THEN 0
       ELSE NULL END AS over_win
FROM opening_ou oo
JOIN gq.matches m ON oo.match_key = m.match_key
JOIN gq.match_outcomes mo
  ON mo.home=m.home AND mo.away=m.away AND mo.league=m.league AND mo.kickoff=m.kickoff
WHERE mo.score_home IS NOT NULL AND mo.score_away IS NOT NULL
  AND oo.over_odds IS NOT NULL AND oo.under_odds IS NOT NULL
  AND oo.over_odds > 1.01 AND oo.under_odds > 1.01
  AND oo.over_odds < 1000 AND oo.under_odds < 1000
""")
ana.commit()
nlab = acur.execute("SELECT COUNT(*) FROM ou_labeled").fetchone()[0]
nvirt = acur.execute("SELECT COUNT(*) FROM ou_labeled WHERE is_virtual=1").fetchone()[0]
print(f"    带赛果样本 = {nlab}, 其中虚拟联赛 = {nvirt}")

acur.execute("DROP TABLE IF EXISTS ou_clean")
acur.execute("CREATE TABLE ou_clean AS SELECT * FROM ou_labeled WHERE is_virtual IS NULL OR is_virtual=0")
acur.execute("DROP TABLE ou_labeled")
ana.commit()
nclean = acur.execute("SELECT COUNT(*) FROM ou_clean").fetchone()[0]
print(f"    排除虚拟后最终样本 = {nclean}")

acur.execute(f"""CREATE TABLE ou_meta AS SELECT
  (SELECT COUNT(*) FROM ou_clean) AS n_rows,
  (SELECT COUNT(DISTINCT match_key) FROM ou_clean) AS n_matches,
  (SELECT MIN(line) FROM ou_clean) AS min_line,
  (SELECT MAX(line) FROM ou_clean) AS max_line,
  (SELECT COUNT(*) FROM ou_clean WHERE over_win IS NOT NULL) AS n_labeled,
  (SELECT COUNT(*) FROM ou_clean WHERE over_win IS NULL) AS n_push,
  (SELECT {n_ou_matches}) AS n_ou_matches_total,
  (SELECT {n_ou_pre}) AS n_ou_matches_with_real_open,
  (SELECT {n_no_open}) AS n_ou_matches_no_real_open
""")
ana.commit()

print("[3/3] 样本预览:")
for r in acur.execute("SELECT match_key,line,over_odds,under_odds,total_goals,league,implied_p_over,over_win FROM ou_clean LIMIT 8"):
    print("   ", r)
m = acur.execute("SELECT * FROM ou_meta").fetchone()
print("META:", m)
ana.close()
print("DONE (%.1fs)" % (time.time()-t0))

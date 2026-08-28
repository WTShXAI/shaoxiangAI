"""构建 AH 开盘数据集: 从 events.db odds_snapshots 还原每场每条 AH 线的开盘双边赔率(含 draw=走水),
join matches + match_outcomes 拿赛果(主客进球 -> 让球覆盖判定), 物化到独立分析库 ah_opening_analysis.db.
所有建表都在分析库完成, events.db 仅以 ATTACH 只读挂载, 绝不污染生产库.

AH 结构(SSoT): market = 'AH_<line>' (line 可正负), selection = home/draw/away.
  - 整/半线(±0.0/±0.5/±1.0...): 3-way, draw = 走水(让球精确打平)
  - 1/4线(±0.25/±0.75): 2-way home/away(无 draw, 分注无干净走水)
开盘价 = 该 (match_key, market, selection) 最早 captured_at 快照赔率.

覆盖判定(favorite = 负线方, 即让球方):
  mag = ABS(line)
  L<0 (主队让球, 主 favorite): fav_cover = (home-away) > mag ; push = (home-away)==mag
  L>0 (客队让球, 客 favorite): fav_cover = (away-home) > mag ; push = (away-home)==mag
  L==0: 退化为 1X2, 排除.
去水隐含 P(fav cover) = (1/fav_odds)/((1/fav_odds)+(1/dog_odds))  仅双边去水(draw=走水不计入).
"""
import sqlite3, time, os

SRC = 'data/events.db'
OUT = 'data/ah_opening_analysis.db'

t0 = time.time()
ana = sqlite3.connect(OUT, timeout=120)
ana.execute("PRAGMA busy_timeout=60000")
acur = ana.cursor()
acur.execute(f"ATTACH DATABASE '{SRC}' AS gq")

acur.execute("DROP TABLE IF EXISTS opening_ah")
acur.execute("DROP TABLE IF EXISTS ah_labeled")
acur.execute("DROP TABLE IF EXISTS ah_clean")
acur.execute("DROP TABLE IF EXISTS ah_meta")

print("[1/3] 还原开盘 AH 赔率 (扫全场 AH 快照, 取每场每线每选边最早快照) ...")
acur.execute("""
CREATE TABLE opening_ah AS
WITH ranked AS (
  SELECT match_key, market, selection, odds,
         ROW_NUMBER() OVER (PARTITION BY match_key, market, selection ORDER BY captured_at ASC) AS rn
  FROM gq.odds_snapshots
  WHERE market LIKE 'AH_%' AND market NOT LIKE '%1H%' AND market NOT LIKE '%2H%'
    AND selection IN ('home','away','draw')
)
SELECT
  h.match_key,
  CAST(SUBSTR(h.market,4) AS REAL) AS line,
  h.odds AS home_odds,
  a.odds AS away_odds,
  d.odds AS draw_odds
FROM (SELECT match_key, market, odds FROM ranked WHERE selection='home'  AND rn=1) h
JOIN (SELECT match_key, market, odds FROM ranked WHERE selection='away'  AND rn=1) a
  ON h.match_key=a.match_key AND h.market=a.market
LEFT JOIN (SELECT match_key, market, odds FROM ranked WHERE selection='draw' AND rn=1) d
  ON h.match_key=d.match_key AND h.market=d.market
""")
ana.commit()
n = acur.execute("SELECT COUNT(*) FROM opening_ah").fetchone()[0]
print(f"    开盘样本 (match,line) = {n}  (%.1fs)" % (time.time()-t0))

print("[2/3] join 赛果 -> 去水隐含P(fav cover) + 覆盖标签 ...")
acur.execute("""
CREATE TABLE ah_labeled AS
SELECT
  oa.match_key, oa.line,
  oa.home_odds, oa.away_odds, oa.draw_odds,
  mo.score_home, mo.score_away,
  (mo.score_home - mo.score_away) AS diff,
  mo.league, mo.is_virtual, mo.kickoff,
  -- 让球方(负线=主让, 正线=客让); L==0 退化1X2 排除
  CASE WHEN oa.line < 0 THEN oa.home_odds ELSE oa.away_odds END AS fav_odds,
  CASE WHEN oa.line < 0 THEN oa.away_odds ELSE oa.home_odds END AS dog_odds,
  (1.0/(CASE WHEN oa.line < 0 THEN oa.home_odds ELSE oa.away_odds END))
    / ((1.0/(CASE WHEN oa.line < 0 THEN oa.home_odds ELSE oa.away_odds END))
     + (1.0/(CASE WHEN oa.line < 0 THEN oa.away_odds ELSE oa.home_odds END))) AS implied_p_fav,
  ABS(oa.line) AS mag,
  CASE
    WHEN oa.line = 0 THEN NULL                                  -- 退化1X2, 排除
    WHEN oa.line < 0 AND (mo.score_home - mo.score_away)  >  ABS(oa.line) THEN 1
    WHEN oa.line > 0 AND (mo.score_away - mo.score_home)  >  ABS(oa.line) THEN 1
    WHEN ABS(mo.score_home - mo.score_away) = ABS(oa.line) THEN NULL   -- 走水
    ELSE 0
  END AS fav_cover
FROM opening_ah oa
JOIN gq.matches m ON oa.match_key = m.match_key
JOIN gq.match_outcomes mo
  ON mo.home=m.home AND mo.away=m.away AND mo.league=m.league AND mo.kickoff=m.kickoff
WHERE mo.score_home IS NOT NULL AND mo.score_away IS NOT NULL
  AND oa.line <> 0
  AND oa.home_odds IS NOT NULL AND oa.away_odds IS NOT NULL
  AND oa.home_odds > 1.01 AND oa.away_odds > 1.01
  AND oa.home_odds < 1000 AND oa.away_odds < 1000
""")
ana.commit()
nlab = acur.execute("SELECT COUNT(*) FROM ah_labeled").fetchone()[0]
nvirt = acur.execute("SELECT COUNT(*) FROM ah_labeled WHERE is_virtual=1").fetchone()[0]
npush = acur.execute("SELECT COUNT(*) FROM ah_labeled WHERE fav_cover IS NULL").fetchone()[0]
print(f"    带赛果样本 = {nlab}, 虚拟 = {nvirt}, 走水(NULL) = {npush}")

acur.execute("DROP TABLE IF EXISTS ah_clean")
acur.execute("CREATE TABLE ah_clean AS SELECT * FROM ah_labeled WHERE (is_virtual IS NULL OR is_virtual=0) AND fav_cover IS NOT NULL")
acur.execute("DROP TABLE ah_labeled")
ana.commit()
nclean = acur.execute("SELECT COUNT(*) FROM ah_clean").fetchone()[0]
print(f"    排除虚拟+走水后最终样本 = {nclean}")

acur.execute("""CREATE TABLE ah_meta AS SELECT
  (SELECT COUNT(*) FROM ah_clean) AS n_rows,
  (SELECT COUNT(DISTINCT match_key) FROM ah_clean) AS n_matches,
  (SELECT MIN(line) FROM ah_clean) AS min_line,
  (SELECT MAX(line) FROM ah_clean) AS max_line,
  (SELECT COUNT(*) FROM ah_clean WHERE fav_cover=1) AS n_cover,
  (SELECT COUNT(*) FROM ah_clean WHERE fav_cover=0) AS n_nocover
""")
ana.commit()

print("[3/3] 样本预览:")
for r in acur.execute("SELECT match_key,line,home_odds,away_odds,draw_odds,diff,implied_p_fav,fav_cover FROM ah_clean LIMIT 8"):
    print("   ", r)
m = acur.execute("SELECT * FROM ah_meta").fetchone()
print("META:", m)
ana.close()
print("DONE (%.1fs)" % (time.time()-t0))

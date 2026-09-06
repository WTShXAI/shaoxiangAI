-- ============================================================================
-- GQ.db :: odds_snapshots 上半场大小球(OU_1H) 特征抽取 SQL
-- 真实表结构 (D:\Architecture\data\GQ.db, 约 31.15M 行 / 3137 个 match_key)
-- ----------------------------------------------------------------------------
-- 字段定义:
--   id           INTEGER  PK
--   match_key    TEXT     场次键, 形如 '加的斯B队 vs 狮城水手' (中文 主 vs 客)
--   captured_at  REAL     抓取时间戳(Unix, 高精度, 同批次同秒)
--   market       TEXT     盘口类别, 例: 'OU_1H_1.50' / 'OU_2H_1.25' / '1X2' / 'AH_1H_-0.25' / 'CS'
--   selection    TEXT     'over' / 'under' (OU); 'home'/'draw'/'away' (1X2); '1:0'... (CS)
--   odds         REAL     赔率
--   line         REAL     盘口线 (OU_1H_1.50 -> 1.5); OU 类 line 永不为 NULL 且与 market 后缀一致
--   score_at     TEXT     抓取时比分, 如 '0-0' / '1-0'; 开盘期多为 '' (空)
--   minute_at    INTEGER  抓取时分钟; 开盘期=0, 滚球期>0
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1) 开盘赔率: 每个 (match_key, market, line) 取最早 captured_at
--    (开盘期 score_at='' 且 minute_at=0, 即 pre-match)
-- ----------------------------------------------------------------------------
WITH first_cap AS (
  SELECT match_key, market, line, MIN(captured_at) AS open_t
  FROM odds_snapshots
  WHERE market LIKE 'OU_1H%'
  GROUP BY match_key, market, line
)
SELECT s.match_key, s.market, s.line, s.selection, s.odds AS open_odds, s.captured_at
FROM odds_snapshots s
JOIN first_cap f
  ON s.match_key=f.match_key AND s.market=f.market AND s.line=f.line AND s.captured_at=f.open_t
WHERE s.selection IN ('over','under')
ORDER BY s.match_key, s.line, s.selection;

-- ----------------------------------------------------------------------------
-- 2) 收盘赔率: 每个 (match_key, market, line) 取最晚 captured_at
--    (这里取收盘前最后一个 pre-match 快照: score_at='' 且 minute_at=0)
-- ----------------------------------------------------------------------------
WITH last_prematch AS (
  SELECT match_key, market, line, MAX(captured_at) AS close_t
  FROM odds_snapshots
  WHERE market LIKE 'OU_1H%'
    AND (score_at='' OR score_at IS NULL) AND minute_at=0
  GROUP BY match_key, market, line
)
SELECT s.match_key, s.market, s.line, s.selection, s.odds AS close_odds
FROM odds_snapshots s
JOIN last_prematch c
  ON s.match_key=c.match_key AND s.market=c.market AND s.line=c.line AND s.captured_at=c.close_t
WHERE s.selection IN ('over','under')
ORDER BY s.match_key, s.line, s.selection;

-- ----------------------------------------------------------------------------
-- 3) 开盘去水隐含概率 P(半场总进球 > line)  —— 逐场逐线
--    去水方法: 比例法 (proportional de-vig)
--      p_over  = (1/over_o) / (1/over_o + 1/under_o)
--      p_under = (1/under_o) / (1/over_o + 1/under_o)
--      margin  = 1/over_o + 1/under_o - 1
-- ----------------------------------------------------------------------------
WITH open_pair AS (
  SELECT s.match_key, s.line,
         MAX(CASE WHEN s.selection='over'  THEN s.odds END) AS over_o,
         MAX(CASE WHEN s.selection='under' THEN s.odds END) AS under_o
  FROM odds_snapshots s
  JOIN (SELECT match_key, market, line, MIN(captured_at) AS open_t
        FROM odds_snapshots WHERE market LIKE 'OU_1H%'
        GROUP BY match_key, market, line) f
    ON s.match_key=f.match_key AND s.market=f.market AND s.line=f.line AND s.captured_at=f.open_t
  WHERE s.selection IN ('over','under')
  GROUP BY s.match_key, s.line
)
SELECT match_key, line,
       (1.0/over_o) / (1.0/over_o + 1.0/under_o)            AS p_over,
       (1.0/under_o) / (1.0/over_o + 1.0/under_o)          AS p_under,
       (1.0/over_o + 1.0/under_o - 1.0)                    AS margin
FROM open_pair
WHERE over_o > 0 AND under_o > 0
ORDER BY match_key, line;

-- ----------------------------------------------------------------------------
-- 4) 结算拼接: 用 matches.ht_score_home/away 得到真实半场进球数
--    (matches 表含 ht_score_home, ht_score_away; 与 odds_snapshots 通过 match_key 关联)
--    over_win = 1 当且仅当 半场总进球 > line
-- ----------------------------------------------------------------------------
WITH open_pair AS (
  SELECT s.match_key, s.line,
         MAX(CASE WHEN s.selection='over'  THEN s.odds END) AS over_o,
         MAX(CASE WHEN s.selection='under' THEN s.odds END) AS under_o
  FROM odds_snapshots s
  JOIN (SELECT match_key, market, line, MIN(captured_at) AS open_t
        FROM odds_snapshots WHERE market LIKE 'OU_1H%'
        GROUP BY match_key, market, line) f
    ON s.match_key=f.match_key AND s.market=f.market AND s.line=f.line AND s.captured_at=f.open_t
  WHERE s.selection IN ('over','under')
  GROUP BY s.match_key, s.line
),
implied AS (
  SELECT match_key, line,
         (1.0/over_o) / (1.0/over_o + 1.0/under_o) AS p_over
  FROM open_pair WHERE over_o>0 AND under_o>0
)
SELECT i.match_key, i.line, i.p_over,
       m.ht_score_home + m.ht_score_away AS ht_goals,
       CASE WHEN (m.ht_score_home + m.ht_score_away) > i.line THEN 1 ELSE 0 END AS over_win
FROM implied i
JOIN matches m ON i.match_key = m.match_key
WHERE m.ht_score_home IS NOT NULL AND m.ht_score_away IS NOT NULL
ORDER BY i.match_key, i.line;

-- ----------------------------------------------------------------------------
-- 5) 开盘->收盘漂移 (陷阱/平衡检测): 同 line 的 over 赔率变化
--    drift > 0 表示 over 赔率上升(庄家不看好大球/或大球受热后退水)
-- ----------------------------------------------------------------------------
WITH o AS (
  SELECT s.match_key, s.line,
         MAX(CASE WHEN s.selection='over' THEN s.odds END) AS open_over
  FROM odds_snapshots s
  JOIN (SELECT match_key, market, line, MIN(captured_at) AS open_t
        FROM odds_snapshots WHERE market LIKE 'OU_1H%'
        GROUP BY match_key, market, line) f
    ON s.match_key=f.match_key AND s.market=f.market AND s.line=f.line AND s.captured_at=f.open_t
  WHERE s.selection='over'
  GROUP BY s.match_key, s.line
),
c AS (
  SELECT s.match_key, s.line,
         MAX(CASE WHEN s.selection='over' THEN s.odds END) AS close_over
  FROM odds_snapshots s
  JOIN (SELECT match_key, market, line, MAX(captured_at) AS close_t
        FROM odds_snapshots
        WHERE market LIKE 'OU_1H%' AND (score_at='' OR score_at IS NULL) AND minute_at=0
        GROUP BY match_key, market, line) f
    ON s.match_key=f.match_key AND s.market=f.market AND s.line=f.line AND s.captured_at=f.close_t
  WHERE s.selection='over'
  GROUP BY s.match_key, s.line
)
SELECT o.match_key, o.line, o.open_over, c.close_over,
       (c.close_over - o.open_over) AS drift
FROM o JOIN c ON o.match_key=c.match_key AND o.line=c.line
ORDER BY drift DESC;

-- ============================================================================
-- 注意:
--   * 纯 SQL 只能完成「去水 + 结算 + 漂移」; 泊松 λ 拟合需要 Python
--     (SQLite 无 Poisson CDF)。拟合脚本见配套 .md 文档。
--   * 实测结论(见 .md): 本数据集 OU_1H 开盘/收盘隐含 λ≈1.45,
--     但真实半场进球均值≈2.9 (拟合子集), 存在 ~2x 系统性低估 ——
--     直接把隐含概率当预测会用翻车, 必须做经验校准(见 .md 第 4 节)。
-- ============================================================================

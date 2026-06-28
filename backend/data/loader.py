"""
数据加载器 — 从 SQLite 数据库加载比赛数据转为 DataFrame

将数据库表 (matches) 的字段映射为 DataEnhancer 所需的标准格式:
  match_date → date
  home_team_name → home_team
  away_team_name → away_team
  home_score, away_score (保持)
  league_name → league
"""

import pandas as pd
import sqlite3
import logging
import os
from typing import Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)

# 数据库路径 — 与 db_manager.py 一致
_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'data', 'football_data.db'
)

def load_matches_from_db(
    db_path: str = None,
    league_codes: Optional[List[str]] = None,
    min_date: Optional[str] = None,
    max_date: Optional[str] = None,
    limit: Optional[int] = None,
) -> pd.DataFrame:
    """从 SQLite 数据库加载历史比赛数据

    自动映射列名 → DataEnhancer 期望格式。
    只加载已完赛的比赛 (home_score IS NOT NULL)。

    Args:
        db_path:     数据库路径，默认 data/football_data.db
        league_codes: 限定联赛代码列表 (如 ['PL', 'PD', 'BL1'])
        min_date:    最早日期 (YYYY-MM-DD)
        max_date:    最晚日期 (YYYY-MM-DD)
        limit:       最大行数限制

    Returns:
        标准格式 DataFrame，列: date, home_team, away_team,
        home_score, away_score, league
    """
    if db_path is None:
        db_path = _DB_PATH

    if not os.path.exists(db_path):
        raise FileNotFoundError(f"数据库不存在: {db_path}")

    logger.info(f"加载数据库: {db_path}")

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        # 基础查询 — 只取已完赛的比赛
        query = """
            SELECT
                match_date  AS date,
                home_team_name AS home_team,
                away_team_name AS away_team,
                home_score,
                away_score,
                league_name AS league
            FROM matches
            WHERE home_score IS NOT NULL
              AND away_score IS NOT NULL
        """
        params: list = []

        # 日期过滤
        if min_date:
            query += " AND match_date >= ?"
            params.append(min_date)
        if max_date:
            query += " AND match_date <= ?"
            params.append(max_date)

        # 联赛过滤
        if league_codes:
            placeholders = ','.join(['?'] * len(league_codes))
            query += f" AND league_name IN ({placeholders})"
            params.extend(league_codes)

        # 按日期排序 (升序，便于 ELO 和滚动特征)
        query += " ORDER BY match_date ASC, match_id ASC"

        # 行数限制
        if limit:
            query += f" LIMIT {int(limit)}"

        df = pd.read_sql_query(query, conn, params=params)

        # 类型转换
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        df['home_score'] = pd.to_numeric(df['home_score'], errors='coerce').astype('Int64')
        df['away_score'] = pd.to_numeric(df['away_score'], errors='coerce').astype('Int64')

        # 删除无效行
        initial = len(df)
        df = df.dropna(subset=['date', 'home_team', 'away_team', 'home_score', 'away_score'])
        dropped = initial - len(df)
        if dropped > 0:
            logger.warning(f"丢弃 {dropped} 条无效记录 (缺失关键字段)")

        total = len(df)
        logger.info(f"✓ 加载 {total:,} 场历史比赛")

        if total > 0:
            logger.info(f"  日期范围: {df['date'].min().date()} ~ {df['date'].max().date()}")
            logger.info(f"  联赛数: {df['league'].nunique()}")
            logger.info(f"  球队数: {pd.concat([df['home_team'], df['away_team']]).nunique()}")

        return df

    finally:
        conn.close()

def load_matches_from_csv(csv_path: str) -> pd.DataFrame:
    """从 CSV 文件加载比赛数据 (备选方案)

    自动检测列名并映射为标准格式。

    Args:
        csv_path: CSV 文件路径

    Returns:
        标准格式 DataFrame
    """
    df = pd.read_csv(csv_path)

    # 列名自动映射
    col_map = {
        'match_date': 'date',
        'home_team_name': 'home_team',
        'away_team_name': 'away_team',
        'league_name': 'league',
    }
    for old, new in col_map.items():
        if old in df.columns and new not in df.columns:
            df.rename(columns={old: new}, inplace=True)

    # 日期解析
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date'], errors='coerce')

    logger.info(f"从 CSV 加载 {len(df):,} 场比赛: {csv_path}")
    return df

if __name__ == "__main__":
    # 快速测试
    logging.basicConfig(level=logging.INFO)

    try:
        df = load_matches_from_db()
        logger.info(f"\n列名: {df.columns.tolist()}")
        logger.info(f"前5行:\n{df.head()}")
    except FileNotFoundError as e:
        logger.info(f"错误: {e}")
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.info(f"加载失败: {e}")

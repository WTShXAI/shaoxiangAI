"""
让球结果标签计算器 (v3.0 — 欧盘版)
================================================================
从比分 + 欧赔推导让球线 → 判定让球覆盖结果。

不再依赖 asian_handicap 字段, 改用 odds_handicap_converter 从 1X2 赔率推导理论让球线。
"""

import sqlite3
import pandas as pd
import logging
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass

from odds_handicap_converter import (
    odds_to_handicap,
    odds_to_handicap_bin,
    compute_cover_result,
)

logger = logging.getLogger(__name__)


@dataclass
class HandicapLabel:
    match_id: int
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    home_odds: float
    draw_odds: float
    away_odds: float
    derived_handicap: float      # 从欧赔推导的理论让球线
    handicap_bin: str            # 让球分桶标签
    cover_result: str            # home_cover / away_cover / push
    goal_diff: int               # 实际进球差


class HandicapLabeler:
    """从比分+欧赔计算让球覆盖标签"""

    def __init__(self, db_path: str = "data/football_data.db"):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

    def initialize(self):
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._ensure_tables()
        logger.info(f"HandicapLabeler 初始化完成, DB: {self.db_path}")

    def _ensure_tables(self):
        """确保标签表存在"""
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS handicap_labels (
                match_id INTEGER PRIMARY KEY,
                home_team TEXT,
                away_team TEXT,
                home_score INTEGER,
                away_score INTEGER,
                home_odds REAL,
                draw_odds REAL,
                away_odds REAL,
                derived_handicap REAL,
                handicap_bin TEXT,
                cover_result TEXT,
                goal_diff INTEGER,
                computed_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def compute_labels(self, force_refresh: bool = False) -> List[HandicapLabel]:
        """
        从数据库计算所有已完成比赛的让球标签。

        流程:
        1. 从 matches JOIN odds 读取比分 + 1X2 赔率
        2. 用 odds_to_handicap 推导理论让球线
        3. 用 compute_cover_result 判定赢盘方向
        4. 写入 handicap_labels 表
        """
        query = """
            SELECT 
                m.match_id, m.home_team_name, m.away_team_name,
                m.home_score, m.away_score, m.match_date, m.league_id,
                o.home_odds, o.draw_odds, o.away_odds
            FROM matches m
            JOIN odds o ON m.match_id = o.match_id
            WHERE m.status = 'finished'
              AND m.home_score IS NOT NULL
              AND m.away_score IS NOT NULL
              AND o.home_odds IS NOT NULL
              AND o.draw_odds IS NOT NULL
              AND o.away_odds IS NOT NULL
            ORDER BY m.match_date DESC
        """

        logger.info("查询比赛数据 (matches + odds)...")
        df = pd.read_sql_query(query, self.conn)
        logger.info(f"获取 {len(df)} 场已完赛比赛 (含欧赔)")

        labels: List[HandicapLabel] = []
        cur = self.conn.cursor()

        # 如果不用强制刷新, 跳过已有的
        if not force_refresh:
            cur.execute("SELECT match_id FROM handicap_labels")
            existing = set(r["match_id"] for r in cur.fetchall())
        else:
            existing = set()

        new_count = 0
        for _, row in df.iterrows():
            mid = int(row["match_id"])
            if mid in existing:
                continue

            ho = float(row["home_odds"])
            do = float(row["draw_odds"])
            ao = float(row["away_odds"])

            # ★ 核心: 欧赔 → 理论让球线
            handicap, p_home, p_away = odds_to_handicap(
                ho, do, ao,
                league_id=row.get("league_id"),
            )
            handicap_bin = odds_to_handicap_bin(handicap)

            # ★ 核心: 比分 → 让球结果
            hs = int(row["home_score"])
            aws = int(row["away_score"])
            cover = compute_cover_result(hs, aws, handicap)
            goal_diff = hs - aws

            label = HandicapLabel(
                match_id=mid,
                home_team=str(row["home_team_name"]),
                away_team=str(row["away_team_name"]),
                home_score=hs,
                away_score=aws,
                home_odds=ho,
                draw_odds=do,
                away_odds=ao,
                derived_handicap=handicap,
                handicap_bin=handicap_bin,
                cover_result=cover,
                goal_diff=goal_diff,
            )
            labels.append(label)
            new_count += 1

        logger.info(f"新计算 {new_count} 条标签 (跳过 {len(existing)} 条已有)")

        # 写入数据库
        for label in labels:
            cur.execute(
                """INSERT OR REPLACE INTO handicap_labels
                   (match_id, home_team, away_team, home_score, away_score,
                    home_odds, draw_odds, away_odds, derived_handicap,
                    handicap_bin, cover_result, goal_diff)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    label.match_id, label.home_team, label.away_team,
                    label.home_score, label.away_score,
                    label.home_odds, label.draw_odds, label.away_odds,
                    label.derived_handicap, label.handicap_bin,
                    label.cover_result, label.goal_diff,
                ),
            )

        self.conn.commit()
        logger.info(f"标签写入完成, 总计 {len(labels) + len(existing)} 条")

        return labels

    def get_stats(self) -> Dict:
        """获取标签统计"""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM handicap_labels")
        total = cur.fetchone()[0]

        cur.execute("""
            SELECT cover_result, COUNT(*) as cnt
            FROM handicap_labels GROUP BY cover_result
        """)
        dist = {r["cover_result"]: r["cnt"] for r in cur.fetchall()}

        return {
            "total_labels": total,
            "distribution": dist,
        }

    def close(self):
        if self.conn:
            self.conn.close()


# ── CLI ──
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    labeler = HandicapLabeler()
    labeler.initialize()

    labels = labeler.compute_labels()
    stats = labeler.get_stats()
    print(f"\n标签统计: 总计 {stats['total_labels']} 条")
    print(f"分布: {stats['distribution']}")

    # 抽样展示
    if labels:
        print(f"\n抽样 (前10条):")
        print(f"{'match_id':>8} {'主队':>12} {'比分':>6} {'进球差':>4} {'赔率(H/D/A)':>22} {'让球线':>7} {'结果':>12}")
        for lb in labels[:10]:
            odds_str = f"{lb.home_odds:.2f}/{lb.draw_odds:.2f}/{lb.away_odds:.2f}"
            print(f"{lb.match_id:>8} {lb.home_team:>12} {lb.home_score}-{lb.away_score:<3} {lb.goal_diff:>4} {odds_str:>22} {lb.handicap_bin:>7} {lb.cover_result:>12}")

    labeler.close()

"""
让球深度×OTSM 历史覆盖率映射器 (v3.0 — 欧盘版)
================================================================
从 handicap_labels 表读取历史标签, 按 [让球分桶 × OTSM状态] 交叉统计覆盖概率。

不再依赖原始亚盘数据, 让球线由欧赔推导 (via odds_handicap_converter)。
"""

import sqlite3
import pandas as pd
import logging
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# OTSM 状态 → 优先级映射
OTSM_STATES = ["LOCKED", "ACTIVE", "NOISE", "UNKNOWN"]
DEFAULT_STATE = "UNKNOWN"

@dataclass
class CoverageProfile:
    """单个 [handicap_bin × otsm_state] 组合的覆盖统计"""
    handicap_bin: str         # 让球分桶标签, 如 "-1.5"
    otsm_state: str           # OTSM 状态
    n_samples: int            # 样本量
    home_cover_rate: float    # 主队赢盘率
    away_cover_rate: float    # 客队赢盘率
    push_rate: float          # 走水率
    avg_goal_diff: float      # 平均进球差
    home_cover_std: float     # 赢盘波动率

class HandicapDepthMapper:
    """
    让球深度×OTSM 历史覆盖率映射器。

    对每个 [handicap_bin × otsm_state] 组合:
      1. 统计主队赢盘率 / 客队赢盘率 / 走水率
      2. 记录样本量和波动率
      3. 计算"理论赢盘率 vs 实际赢盘率"偏差 → 价值信号
    """

    def __init__(self, db_path: str = "data/football_data.db"):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self.profiles: Dict[Tuple[str, str], CoverageProfile] = {}

    def initialize(self):
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._ensure_tables()
        logger.info(f"HandicapDepthMapper 初始化完成, DB: {self.db_path}")

    def _ensure_tables(self):
        """确保覆盖率剖面表存在"""
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS handicap_depth_profile (
                handicap_bin TEXT,
                otsm_state TEXT,
                n_samples INTEGER,
                home_cover_rate REAL,
                away_cover_rate REAL,
                push_rate REAL,
                avg_goal_diff REAL,
                home_cover_std REAL,
                computed_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (handicap_bin, otsm_state)
            )
        """)
        self.conn.commit()

    def build_profiles(self, force_refresh: bool = False) -> Dict[Tuple[str, str], CoverageProfile]:
        """
        构建所有 [让球分桶 × OTSM状态] 的覆盖率剖面。

        数据来源: handicap_labels JOIN match_features_otsm

        Returns:
            {(handicap_bin, otsm_state): CoverageProfile}
        """
        cur = self.conn.cursor()

        # 检查 match_features_otsm 表是否存在
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='match_features_otsm'"
        )
        otsm_exists = cur.fetchone() is not None

        if otsm_exists:
            query = """
                SELECT
                    hl.handicap_bin,
                    CASE
                        WHEN mf.otsm_state_LOCKED = 1 THEN 'LOCKED'
                        WHEN mf.otsm_state_ACTIVE = 1 THEN 'ACTIVE'
                        WHEN mf.otsm_state_NOISE = 1 THEN 'NOISE'
                        ELSE 'UNKNOWN'
                    END as otsm_state,
                    COUNT(*) as n_samples,
                    AVG(CASE WHEN hl.cover_result = 'home_cover' THEN 1.0 ELSE 0.0 END) as home_cover_rate,
                    AVG(CASE WHEN hl.cover_result = 'away_cover' THEN 1.0 ELSE 0.0 END) as away_cover_rate,
                    AVG(CASE WHEN hl.cover_result = 'push' THEN 1.0 ELSE 0.0 END) as push_rate,
                    AVG(hl.goal_diff) as avg_goal_diff
                FROM handicap_labels hl
                LEFT JOIN match_features_otsm mf ON hl.match_id = mf.match_id
                GROUP BY hl.handicap_bin, otsm_state
                ORDER BY hl.handicap_bin, otsm_state
            """
        else:
            # 无 OTSM 表时用 UNKNOWN 兜底
            logger.warning("match_features_otsm 不存在, 所有比赛标记为 UNKNOWN")
            query = """
                SELECT
                    hl.handicap_bin,
                    'UNKNOWN' as otsm_state,
                    COUNT(*) as n_samples,
                    AVG(CASE WHEN hl.cover_result = 'home_cover' THEN 1.0 ELSE 0.0 END) as home_cover_rate,
                    AVG(CASE WHEN hl.cover_result = 'away_cover' THEN 1.0 ELSE 0.0 END) as away_cover_rate,
                    AVG(CASE WHEN hl.cover_result = 'push' THEN 1.0 ELSE 0.0 END) as push_rate,
                    AVG(hl.goal_diff) as avg_goal_diff
                FROM handicap_labels hl
                GROUP BY hl.handicap_bin
                ORDER BY hl.handicap_bin
            """

        logger.info("构建让球深度×OTSM覆盖率剖面...")
        df = pd.read_sql_query(query, self.conn)

        profiles: Dict[Tuple[str, str], CoverageProfile] = {}
        for _, row in df.iterrows():
            hbin = str(row["handicap_bin"])
            state = str(row["otsm_state"])
            n = int(row["n_samples"])

            # 样本量太少的不纳入
            if n < 3:
                continue

            # 计算赢盘波动率 (二项分布近似)
            hcr = float(row["home_cover_rate"])
            home_std = (hcr * (1 - hcr) / max(n, 1)) ** 0.5

            profile = CoverageProfile(
                handicap_bin=hbin,
                otsm_state=state,
                n_samples=n,
                home_cover_rate=hcr,
                away_cover_rate=float(row["away_cover_rate"]),
                push_rate=float(row["push_rate"]),
                avg_goal_diff=float(row["avg_goal_diff"]),
                home_cover_std=home_std,
            )
            profiles[(hbin, state)] = profile

        self.profiles = profiles
        logger.info(f"构建完成: {len(profiles)} 个组合剖面")

        # 写入数据库
        cur = self.conn.cursor()
        cur.execute("DELETE FROM handicap_depth_profile")
        for (hbin, state), p in profiles.items():
            cur.execute(
                """INSERT INTO handicap_depth_profile
                   (handicap_bin, otsm_state, n_samples, home_cover_rate,
                    away_cover_rate, push_rate, avg_goal_diff, home_cover_std)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (hbin, state, p.n_samples, p.home_cover_rate,
                 p.away_cover_rate, p.push_rate, p.avg_goal_diff, p.home_cover_std),
            )
        self.conn.commit()

        return profiles

    def query(self, handicap_bin: str, otsm_state: str = "UNKNOWN") -> Optional[CoverageProfile]:
        """查询特定组合的覆盖率剖面"""
        key = (handicap_bin, otsm_state)
        if key in self.profiles:
            return self.profiles[key]
        # 降级: 查 UNKNOWN 状态
        fallback_key = (handicap_bin, "UNKNOWN")
        return self.profiles.get(fallback_key)

    def get_summary(self) -> pd.DataFrame:
        """返回覆盖率剖面汇总表"""
        records = []
        for (hbin, state), p in sorted(self.profiles.items()):
            records.append({
                "handicap_bin": hbin,
                "otsm_state": state,
                "n": p.n_samples,
                "home_cover%": round(p.home_cover_rate * 100, 1),
                "away_cover%": round(p.away_cover_rate * 100, 1),
                "push%": round(p.push_rate * 100, 1),
                "avg_gd": round(p.avg_goal_diff, 2),
            })
        return pd.DataFrame(records)

    def close(self):
        if self.conn:
            self.conn.close()

# ── CLI ──
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    mapper = HandicapDepthMapper()
    mapper.initialize()
    mapper.build_profiles()

    summary = mapper.get_summary()
    if not summary.empty:
        print("\n让球深度×OTSM覆盖率剖面汇总:")
        print(f"{'handicap_bin':>12} {'state':>8} {'n':>6} {'home_cover%':>12} {'away_cover%':>12} {'push%':>7} {'avg_gd':>7}")
        print("-" * 70)
        for _, r in summary.iterrows():
            print(f"{r['handicap_bin']:>12} {r['otsm_state']:>8} {r['n']:>6} "
                  f"{r['home_cover%']:>11.1f}% {r['away_cover%']:>11.1f}% {r['push%']:>6.1f}% {r['avg_gd']:>7.2f}")

    mapper.close()

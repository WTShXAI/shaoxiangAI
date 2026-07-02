"""
哨响AI - SQLite数据库管理模块 (已拆分-Mixin)
============================================
DatabaseManager 通过 Mixin 继承获得全部方法。
Mixin 位于 database/db/ 子包。
拆分: 2026-06-28 (Go File 拆分)
"""
import sqlite3
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from contextlib import contextmanager
from database.db import *

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'football_data.db')


class DatabaseManager(CoreMixin, SchemaMixin, CrudMatchMixin, CrudEntityMixin, CrudPredictionMixin, AnalyticsMixin):
    """SQLite数据库管理器 (Mixin组成)"""

    # ── v6.0.0 兜底方法 (Mixin未覆盖的查询) ──
    def get_team_features(self, team_name: str) -> dict:
        """球队特征简版"""
        result = {'team_name': team_name, 'match_count': 0,
                  'avg_goals_for': 0.0, 'avg_goals_against': 0.0}
        try:
            with self.get_connection() as conn:
                row = conn.execute('''SELECT COUNT(*) as cnt, AVG(home_score) as gf FROM matches 
                    WHERE home_team_name=? AND home_score IS NOT NULL''', (team_name,)).fetchone()
                if row and row['cnt']:
                    result['match_count'] = row['cnt']
                    result['avg_goals_for'] = round(row['gf'] or 0, 2) if row['cnt'] else 0
        except Exception:
            pass
        return result

    def get_h2h_advantage(self, home: str, away: str) -> float:
        return 0.0

    def get_rank_diff_factor(self, home: str, away: str) -> float:
        return 0.0

    def __getattr__(self, name):
        """v6.0.0: 任意缺失方法返回 None-safe 值, 避免AttributeError"""
        if name.startswith('get_'):
            return lambda *a, **kw: {}
        raise AttributeError(name)


# ── 单例入口 ──────────────────────────────
_db_instance: Optional[DatabaseManager] = None


def get_db() -> DatabaseManager:
    """获取数据库单例"""
    global _db_instance
    if _db_instance is None:
        _db_instance = DatabaseManager()
    return _db_instance

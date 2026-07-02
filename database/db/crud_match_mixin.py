"""Database Manager Mixin — crud_match_mixin"""
import sqlite3
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'football_data.db')


class CrudMatchMixin:
    """DatabaseManager Mixin — crud_match_mixin"""

    def add_match(self, match_data: Dict) -> int:
        """添加比赛（支持外部 match_id 去重）"""
        external_id = match_data.get('match_id')  # 外部 API 的 match_id
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # 如果提供了外部 match_id 且已存在，直接返回已有记录
            if external_id:
                existing = conn.execute(
                    'SELECT match_id FROM matches WHERE match_id=?', (external_id,)
                ).fetchone()
                if existing:
                    return existing[0]

            if external_id:
                cursor.execute('''
                    INSERT INTO matches (match_id, match_date, match_time, league_id, league_name,
                        home_team_id, home_team_name, away_team_id, away_team_name,
                        status, matchday, home_score, away_score, halftime_home, halftime_away, minute)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    external_id, match_data.get('match_date'), match_data.get('match_time'),
                    match_data.get('league_id'), match_data.get('league_name'),
                    match_data.get('home_team_id'), match_data.get('home_team_name'),
                    match_data.get('away_team_id'), match_data.get('away_team_name'),
                    match_data.get('status', 'scheduled'), match_data.get('matchday'),
                    match_data.get('home_score'), match_data.get('away_score'),
                    match_data.get('halftime_home'), match_data.get('halftime_away'),
                    match_data.get('minute')
                ))
            else:
                cursor.execute('''
                    INSERT INTO matches (match_date, match_time, league_id, league_name,
                        home_team_id, home_team_name, away_team_id, away_team_name,
                        status, matchday, home_score, away_score, halftime_home, halftime_away, minute)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    match_data.get('match_date'), match_data.get('match_time'),
                    match_data.get('league_id'), match_data.get('league_name'),
                    match_data.get('home_team_id'), match_data.get('home_team_name'),
                    match_data.get('away_team_id'), match_data.get('away_team_name'),
                    match_data.get('status', 'scheduled'), match_data.get('matchday'),
                    match_data.get('home_score'), match_data.get('away_score'),
                    match_data.get('halftime_home'), match_data.get('halftime_away'),
                    match_data.get('minute')
                ))
            return cursor.lastrowid


    def update_match_result(self, match_id: int, home_score: int, away_score: int) -> None:
        """更新比赛结果"""
        result = 'H' if home_score > away_score else ('A' if home_score < away_score else 'D')
        with self.get_connection() as conn:
            conn.execute('''
                UPDATE matches SET home_score=?, away_score=?, final_result=?, status='finished',
                    updated_at=datetime('now','localtime')
                WHERE match_id=?
            ''', (home_score, away_score, result, match_id))


    def archive_expired_matches(self) -> Optional[Dict]:
        """归档过期比赛：将 match_date < 今天 且状态为 scheduled/live 的比赛标记为 expired"""
        with self.get_connection() as conn:
            cursor = conn.execute('''
                UPDATE matches SET status='expired', updated_at=datetime('now','localtime')
                WHERE status IN ('scheduled', 'live')
                  AND match_date IS NOT NULL
                  AND match_date != ''
                  AND match_date < date('now', 'localtime')
            ''')
            affected = cursor.rowcount
            if affected > 0:
                logger.info(f"[归档] 已将 {affected} 场过期比赛标记为 expired")
            return affected


    def is_match_predictable(self, match_id) -> tuple:
        """
        检查比赛是否可预测。
        Returns: (ok: bool, reason: str, match_data: dict|None)
        规则：
        - 状态为 finished → 不可预测（已结束）
        - 状态为 live → 不可预测（已开赛）
        - 状态为 expired → 不可预测（已过期）
        - match_date < today → 不可预测（日期已过，自动归档）
        - 状态为 scheduled + 日期在将来或今天 → 可预测
        """
        match = self.get_match(match_id)
        if not match:
            return False, '比赛不存在', None
        status = str(match.get('status', '')).lower()
        match_date = str(match.get('match_date', ''))
        # 已结束/进行中/已过期
        if status in ('finished', 'live', 'expired'):
            return False, f'比赛状态为 {status}，无法预测', match
        # 检查日期
        if match_date and match_date < datetime.now(timezone.utc).strftime('%Y-%m-%d'):
            # 自动归档
            self.archive_expired_matches()
            return False, f'比赛日期 {match_date} 已过期，已自动归档', match
        return True, 'ok', match


    def get_match(self, match_id: int) -> Optional[Dict]:
        """获取单场比赛"""
        with self.get_connection() as conn:
            row = conn.execute('SELECT * FROM matches WHERE match_id=?', (match_id,)).fetchone()
            return dict(row) if row else None


    def find_match_by_teams(self, home_team: str, away_team: str, match_date: str = None) -> Optional[Dict]:
        """按球队名+日期查重（防重复创建）"""
        with self.get_connection() as conn:
            if match_date:
                row = conn.execute(
                    '''SELECT * FROM matches WHERE home_team_name=? AND away_team_name=?
                       AND match_date=? AND status='scheduled' LIMIT 1''',
                    (home_team, away_team, match_date)
                ).fetchone()
            else:
                row = conn.execute(
                    '''SELECT * FROM matches WHERE home_team_name=? AND away_team_name=?
                       AND status='scheduled' LIMIT 1''',
                    (home_team, away_team)
                ).fetchone()
            return dict(row) if row else None


    def get_matches(self, league_id: int = None, league_name: str = None,
                    date_from: str = None, date_to: str = None,
                    status: str = None, limit: int = 100, offset: int = 0) -> List[Dict]:
        """查询比赛列表（含赔率和预测数据）"""
        conditions = ['m.match_id IS NOT NULL']  # 始终为真，方便拼接
        params = []
        if league_id:
            conditions.append("m.league_id=?")
            params.append(league_id)
        elif league_name:
            # league_name 后备：按联赛名称模糊匹配
            conditions.append("m.league_name LIKE ?")
            params.append(f"%{league_name}%")
        if date_from:
            conditions.append("m.match_date>=?")
            params.append(date_from)
        if date_to:
            conditions.append("m.match_date<=?")
            params.append(date_to)
        if status:
            conditions.append("m.status=?")
            params.append(status)

        where_clause = " AND ".join(conditions)
        with self.get_connection() as conn:
            # 确保 teams 表有 team_name_zh 列
            try:
                conn.execute('ALTER TABLE teams ADD COLUMN team_name_zh TEXT')
            except (Exception, sqlite3.Error):
                pass
            rows = conn.execute(f'''
                SELECT m.match_id, m.match_date, m.match_time, m.league_id, m.league_name,
                    m.home_team_id, m.home_team_name, m.away_team_id, m.away_team_name,
                    m.home_score, m.away_score, m.final_result, m.status, m.matchday,
                    m.created_at, m.updated_at, m.halftime_home, m.halftime_away, m.minute,
                    o.home_odds, o.draw_odds, o.away_odds,
                    p.predicted_result AS prediction,
                    p.tier AS decision,
                    p.confidence_score AS confidence,
                    p.prob_h AS home_prob,
                    p.prob_d AS draw_prob,
                    p.prob_a AS away_prob,
                    p.consensus_score,
                    p.feature_score,
                    p.odds_clarity_score,
                    p.odds_direction,
                    p.feature_coverage,
                    p.default_ratio,
                    p.odds_h AS pred_odds_h,
                    p.odds_d AS pred_odds_d,
                    p.odds_a AS pred_odds_a,
                    p.total_score,
                    CASE WHEN p.tier = 'S' THEN 1 ELSE 0 END AS has_value,
                    COALESCE(th.team_name_zh, th.team_name) AS home_team_zh,
                    COALESCE(ta.team_name_zh, ta.team_name) AS away_team_zh
                FROM matches m
                LEFT JOIN teams th ON m.home_team_id = th.team_id
                LEFT JOIN teams ta ON m.away_team_id = ta.team_id
                LEFT JOIN (
                    SELECT match_id, home_odds, draw_odds, away_odds,
                           ROW_NUMBER() OVER (PARTITION BY match_id ORDER BY odds_id DESC) AS rn
                    FROM odds
                ) o ON m.match_id = o.match_id AND o.rn = 1
                LEFT JOIN (
                    SELECT match_id, predicted_result, tier, confidence_score,
                           prob_h, prob_d, prob_a,
                           consensus_score, feature_score, odds_clarity_score,
                           odds_direction, feature_coverage, default_ratio,
                           odds_h, odds_d, odds_a, total_score,
                           ROW_NUMBER() OVER (PARTITION BY match_id ORDER BY prediction_id DESC) AS rn
                    FROM predictions
                ) p ON m.match_id = p.match_id AND p.rn = 1
                WHERE {where_clause}
                ORDER BY m.match_date DESC, m.match_time DESC
                LIMIT ? OFFSET ?
            ''', params + [limit, offset]).fetchall()
            return [dict(r) for r in rows]

    # ===================== 球队相关操作 =====================

    def get_next_scheduled_match(self) -> Optional[Dict]:
        """获取下一场待预测比赛（status=scheduled, match_date>=今天）"""
        with self.get_connection() as conn:
            row = conn.execute("""
                SELECT m.*, o.home_odds, o.draw_odds, o.away_odds
                FROM matches m
                LEFT JOIN (
                    SELECT match_id, home_odds, draw_odds, away_odds,
                           ROW_NUMBER() OVER (PARTITION BY match_id ORDER BY odds_id DESC) AS rn
                    FROM odds
                ) o ON m.match_id = o.match_id AND o.rn = 1
                WHERE m.status = 'scheduled'
                  AND m.match_date >= date('now', 'localtime')
                ORDER BY m.match_date ASC, m.match_time ASC
                LIMIT 1
            """).fetchone()
            return dict(row) if row else None



"""Database Manager Mixin — analytics_mixin"""
import sqlite3
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'football_data.db')


class AnalyticsMixin:
    """DatabaseManager Mixin — analytics_mixin"""

    def get_stats(self) -> Dict:
        """获取系统统计"""
        with self.get_connection() as conn:
            total_matches = conn.execute('SELECT COUNT(*) FROM matches').fetchone()[0]
            finished_matches = conn.execute("SELECT COUNT(*) FROM matches WHERE status='finished'").fetchone()[0]
            total_predictions = conn.execute('SELECT COUNT(*) FROM predictions').fetchone()[0]
            correct_predictions = conn.execute(
                'SELECT COUNT(*) FROM predictions WHERE is_correct=1').fetchone()[0]
            invest_count = conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE decision='INVEST'").fetchone()[0]
            total_teams = conn.execute('SELECT COUNT(*) FROM teams').fetchone()[0]
            total_training = conn.execute('SELECT COUNT(*) FROM model_training').fetchone()[0]

            accuracy = (correct_predictions / total_predictions * 100) if total_predictions > 0 else 0.0

            # 已评估预测数（有 is_correct 标记的）
            evaluated = conn.execute(
                'SELECT COUNT(*) FROM predictions WHERE is_correct IS NOT NULL').fetchone()[0]

            # INVEST 决策的正确率（predictions 表现在有 decision 列）
            invest_correct = conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE decision='INVEST' AND is_correct=1").fetchone()[0]
            invest_total = conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE decision='INVEST' AND is_correct IS NOT NULL").fetchone()[0]
            invest_accuracy = (invest_correct / invest_total * 100) if invest_total > 0 else 0.0

            # 最近训练信息
            last_training = conn.execute(
                'SELECT * FROM model_training ORDER BY training_id DESC LIMIT 1').fetchone()

            return {
                'total_matches': total_matches,
                'finished_matches': finished_matches,
                'total_predictions': total_predictions,
                'correct_predictions': correct_predictions,
                'evaluated_predictions': evaluated,
                'accuracy': round(accuracy, 2),
                'invest_count': invest_count,
                'invest_accuracy': round(invest_accuracy, 2),
                'total_teams': total_teams,
                'total_training_runs': total_training,
                'last_training': dict(last_training) if last_training else None,
            }


    def get_team_recent_form(self, team_name: str, league_id: int = None,
                             limit: int = 5) -> str:
        """获取球队近期形态（W/D/L 字符串）"""
        with self.get_connection() as conn:
            query = '''
                SELECT final_result, home_team_name, away_team_name FROM matches
                WHERE (home_team_name = ? OR away_team_name = ?)
                  AND status = 'finished' AND final_result IS NOT NULL
            '''
            params = [team_name, team_name]
            if league_id:
                query += ' AND league_id = ?'
                params.append(league_id)
            query += ' ORDER BY match_date DESC LIMIT ?'
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            form = ''
            for r in rows:
                result = r['final_result']  # H=主胜, D=平, A=客胜
                is_home_team = (r['home_team_name'] == team_name)
                # 转换 H/D/A 为该球队视角的 W/D/L
                if result == 'D':
                    form += 'D'
                elif is_home_team:
                    form += 'W' if result == 'H' else 'L'
                else:  # 该队是客队
                    form += 'W' if result == 'A' else 'L'
            return form


    def get_unresolved_predictions(self) -> List[Dict]:
        """获取已结束比赛但未记录实际结果的预测"""
        with self.get_connection() as conn:
            rows = conn.execute('''
                SELECT p.*, m.home_team_name, m.away_team_name, m.league_name,
                    m.match_date, m.match_time, m.home_score, m.away_score, m.final_result,
                    m.status as match_status
                FROM predictions p
                JOIN matches m ON p.match_id = m.match_id
                WHERE m.status = 'finished' AND p.is_correct IS NULL
                ORDER BY m.match_date DESC
            ''').fetchall()
            return [dict(r) for r in rows]


    def resolve_prediction_results(self) -> int:
        """自动检查已结束比赛的预测并标记正确性。返回更新数量。"""
        updated = 0
        with self.get_connection() as conn:
            rows = conn.execute('''
                SELECT p.prediction_id, p.home_prob, p.draw_prob, p.away_prob,
                    m.home_score, m.away_score, m.final_result
                FROM predictions p
                JOIN matches m ON p.match_id = m.match_id
                WHERE m.status = 'finished' AND m.home_score IS NOT NULL
                    AND p.is_correct IS NULL
            ''').fetchall()

            for row in rows:
                actual = row['final_result']
                pred = 'H' if row['home_prob'] >= max(row['draw_prob'], row['away_prob']) else \
                    ('A' if row['away_prob'] >= row['draw_prob'] else 'D')
                is_correct = 1 if pred == actual else 0
                conn.execute(
                    'UPDATE predictions SET actual_result=?, is_correct=? WHERE prediction_id=?',
                    (actual, is_correct, row['prediction_id'])
                )
                updated += 1

        if updated:
            logger.info(f"自动标记 {updated} 条预测结果")
        return updated


    def get_accuracy_timeline(self, days: int = 30) -> List[Dict]:
        """获取按天分组的准确率趋势（仅计算已评估的预测）"""
        with self.get_connection() as conn:
            rows = conn.execute('''
                SELECT 
                    substr(p.prediction_time, 1, 10) as day,
                    COUNT(*) as total,
                    SUM(CASE WHEN p.is_correct=1 THEN 1 ELSE 0 END) as correct
                FROM predictions p
                WHERE p.is_correct IS NOT NULL
                    AND p.prediction_time >= date('now', ? || ' days')
                GROUP BY day
                ORDER BY day ASC
            ''', (str(-days),)).fetchall()
            return [dict(r) for r in rows]


    def get_model_versions(self) -> List[Dict]:
        """获取所有模型版本及对应精度"""
        with self.get_connection() as conn:
            rows = conn.execute('''
                SELECT model_version,
                    COUNT(*) as pred_count,
                    SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) as correct_count,
                    SUM(CASE WHEN is_correct=0 THEN 1 ELSE 0 END) as wrong_count
                FROM predictions
                WHERE is_correct IS NOT NULL
                GROUP BY model_version
                ORDER BY MAX(prediction_time) DESC
            ''').fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d['accuracy'] = round(d['correct_count'] / d['pred_count'] * 100, 2) if d['pred_count'] > 0 else 0.0
                result.append(d)
            return result


    def get_leagues(self) -> List[Dict]:
        """获取所有联赛列表"""
        with self.get_connection() as conn:
            rows = conn.execute('''
                SELECT league_id, league_name, COUNT(*) as match_count
                FROM matches GROUP BY league_id, league_name
                ORDER BY match_count DESC
            ''').fetchall()
            return [dict(r) for r in rows]

    # ===================== 积分榜操作 =====================


    def save_standings(self, standings_data: Dict) -> int:
        """保存/更新积分榜数据（INSERT OR REPLACE）"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO standings (league_id, league_name, season, team_name,
                    position, played_games, wins, draws, losses, goals_for, goals_against,
                    goal_diff, points, form, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
            ''', (
                standings_data.get('league_id'),
                standings_data.get('league_name', ''),
                standings_data.get('season', 2024),
                standings_data.get('team_name', ''),
                standings_data.get('position'),
                standings_data.get('played_games', 0),
                standings_data.get('wins', 0),
                standings_data.get('draws', 0),
                standings_data.get('losses', 0),
                standings_data.get('goals_for', 0),
                standings_data.get('goals_against', 0),
                standings_data.get('goal_diff', 0),
                standings_data.get('points', 0),
                standings_data.get('form', ''),
            ))
            return cursor.lastrowid


    def get_standings(self, league_id: int, season: int = None) -> List[Dict]:
        """获取联赛积分榜"""
        with self.get_connection() as conn:
            if season:
                rows = conn.execute('''
                    SELECT * FROM standings WHERE league_id=? AND season=?
                    ORDER BY position ASC
                ''', (league_id, season)).fetchall()
            else:
                rows = conn.execute('''
                    SELECT * FROM standings WHERE league_id=?
                    ORDER BY season DESC, position ASC
                ''', (league_id,)).fetchall()
            return [dict(r) for r in rows]


    def get_team_rank(self, team_name: str, league_id: int, season: int = None) -> Optional[Dict]:
        """获取球队排名"""
        with self.get_connection() as conn:
            if season:
                row = conn.execute('''
                    SELECT * FROM standings WHERE team_name=? AND league_id=? AND season=?
                ''', (team_name, league_id, season)).fetchone()
            else:
                row = conn.execute('''
                    SELECT * FROM standings WHERE team_name=? AND league_id=?
                    ORDER BY season DESC LIMIT 1
                ''', (team_name, league_id)).fetchone()
            return dict(row) if row else None


    def get_rank_diff(self, home_team: str, away_team: str, league_id: int,
                      season: int = None) -> float:
        """计算两队排名差（正值=客队排名更高，负值=主队排名更高）"""
        home_rank = self.get_team_rank(home_team, league_id, season)
        away_rank = self.get_team_rank(away_team, league_id, season)
        if home_rank and away_rank:
            return float(home_rank.get('position', 10) - away_rank.get('position', 10))
        return 0.0

    # ===================== 趋势/表单操作 =====================


    def save_form_trend(self, trend_data: Dict) -> int:
        """保存球队表单趋势数据"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO form_trends (team_name, league_id, season, match_date, opponent,
                    result, home_away, goals_for, goals_against, is_clean_sheet, is_over25,
                    trend_score, rolling_form_5, goals_for_last5, goals_against_last5)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                trend_data.get('team_name', ''),
                trend_data.get('league_id', 0),
                trend_data.get('season'),
                trend_data.get('match_date', ''),
                trend_data.get('opponent', ''),
                trend_data.get('result'),
                trend_data.get('home_away', 'H'),
                trend_data.get('goals_for', 0),
                trend_data.get('goals_against', 0),
                trend_data.get('is_clean_sheet', 0),
                trend_data.get('is_over25', 0),
                trend_data.get('trend_score', 0.0),
                trend_data.get('rolling_form_5', ''),
                trend_data.get('goals_for_last5', 0.0),
                trend_data.get('goals_against_last5', 0.0),
            ))
            return cursor.lastrowid


    def get_team_form(self, team_name: str, limit: int = 10) -> List[Dict]:
        """获取球队近期表单"""
        with self.get_connection() as conn:
            rows = conn.execute('''
                SELECT * FROM form_trends WHERE team_name=?
                ORDER BY match_date DESC LIMIT ?
            ''', (team_name, limit)).fetchall()
            return [dict(r) for r in rows]


    def get_h2h(self, team_a: str, team_b: str, limit: int = 10) -> List[Dict]:
        """获取两队历史交锋记录"""
        with self.get_connection() as conn:
            rows = conn.execute('''
                SELECT m.match_date, m.home_team_name, m.away_team_name,
                       m.home_score, m.away_score, m.final_result, m.league_name
                FROM matches m
                WHERE m.status='finished'
                  AND ((m.home_team_name=? AND m.away_team_name=?)
                       OR (m.home_team_name=? AND m.away_team_name=?))
                ORDER BY m.match_date DESC
                LIMIT ?
            ''', (team_a, team_b, team_b, team_a, limit)).fetchall()
            return [dict(r) for r in rows]


    def get_latest_form_trend(self, team_name: str) -> Optional[Dict]:
        """获取球队最新趋势数据"""
        with self.get_connection() as conn:
            row = conn.execute('''
                SELECT * FROM form_trends WHERE team_name=?
                ORDER BY match_date DESC LIMIT 1
            ''', (team_name,)).fetchone()
            return dict(row) if row else None


    def get_standings_seasons(self, league_id: int) -> List[int]:
        """获取联赛已有哪些赛季的积分榜数据"""
        with self.get_connection() as conn:
            rows = conn.execute('''
                SELECT DISTINCT season FROM standings WHERE league_id=?
                ORDER BY season DESC
            ''', (league_id,)).fetchall()
            return [r['season'] for r in rows]

    # ===================== 数据同步状态追踪 =====================



"""Database Manager Mixin — core_mixin"""
import sqlite3
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'football_data.db')


class CoreMixin:
    """DatabaseManager Mixin — core_mixin"""

    def __init__(self, db_path: str = None) -> None:
        self.db_path = db_path or DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_tables()

    @contextmanager

    def get_connection(self) -> None:
        """获取数据库连接上下文管理器"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except (Exception, sqlite3.Error) as e:
            conn.rollback()
            logger.error(f"数据库操作失败: {e}")
            raise
        finally:
            conn.close()


    def transaction(self) -> None:
        """跨操作事务上下文管理器（用于原子性操作）"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
            logger.debug("事务已提交")
        except (Exception, sqlite3.Error):
            conn.rollback()
            logger.warning("事务已回滚")
            raise
        finally:
            conn.close()


    def _execute_with_conn(self, conn, sql: str, params: tuple = ()) -> Optional[Dict]:
        """在给定连接上执行 SQL（用于事务内操作）"""
        return conn.execute(sql, params)


    def add_match_tx(self, conn, match_data: Dict) -> int:
        """事务内添加比赛（已存在则更新比分/状态）"""
        external_id = match_data.get('match_id')
        cursor = conn.cursor()
        if external_id:
            existing = conn.execute(
                'SELECT match_id, status, home_score, away_score FROM matches WHERE match_id=?',
                (external_id,)
            ).fetchone()
            if existing:
                # 如果比赛已完成且数据库里还没有比分，更新比分
                new_status = (match_data.get('status') or '').lower()
                new_home_score = match_data.get('home_score')
                new_away_score = match_data.get('away_score')
                new_final = match_data.get('final_result')
                if new_status == 'finished' and (existing['status'] != 'finished' or existing['home_score'] is None):
                    if new_home_score is not None and new_away_score is not None:
                        if not new_final:
                            new_final = 'H' if new_home_score > new_away_score else ('A' if new_home_score < new_away_score else 'D')
                        conn.execute('''
                            UPDATE matches SET home_score=?, away_score=?, final_result=?,
                                status='finished', updated_at=datetime('now','localtime')
                            WHERE match_id=?
                        ''', (new_home_score, new_away_score, new_final, external_id))
                        conn.execute('''
                            UPDATE predictions SET actual_result=?
                            WHERE match_id=?
                        ''', (new_final, external_id))
                        logger.info(f"[DB] 比分更新: match_id={external_id} {new_home_score}-{new_away_score}")
                # Live 比赛：同步更新实时比分（包括半场比分和分钟数）
                elif new_status == 'live':
                    conn.execute('''
                        UPDATE matches SET home_score=?, away_score=?, status='live',
                            halftime_home=?, halftime_away=?, minute=?,
                            updated_at=datetime('now','localtime')
                        WHERE match_id=?
                    ''', (
                        new_home_score, new_away_score,
                        match_data.get('halftime_home'), match_data.get('halftime_away'),
                        match_data.get('minute'), external_id
                    ))
                return existing[0]
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


    def add_odds_tx(self, conn, odds_data: Dict) -> int:
        """事务内添加赔率"""
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO odds (match_id, provider, home_odds, draw_odds, away_odds,
                asian_handicap, over_under, over_odds, under_odds, return_rate, odds_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            odds_data.get('match_id'), odds_data.get('provider', 'default'),
            odds_data.get('home_odds'), odds_data.get('draw_odds'), odds_data.get('away_odds'),
            odds_data.get('asian_handicap'), odds_data.get('over_under'),
            odds_data.get('over_odds'), odds_data.get('under_odds'),
            odds_data.get('return_rate', 0.95),
            odds_data.get('odds_timestamp', datetime.now(timezone.utc).isoformat())
        ))
        return cursor.lastrowid


    def save_features_tx(self, conn, features_data: Dict) -> int:
        """事务内保存特征"""
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO match_features (match_id, sigma_trap, beta_dev, lambda_crush,
                delta_fatigue, aerial_advantage, press_intensity, epsilon_senti,
                discussion_growth, news_impact, time_suppression, card_risk,
                arbitrage_index, arbitrage_window, a1, a2, a3, a4, a5, a6,
                rank_diff_factor, form_momentum, h2h_factor,
                rank_factor, form_factor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            features_data.get('match_id'),
            features_data.get('sigma_trap', 0.0), features_data.get('beta_dev', 0.0),
            features_data.get('lambda_crush', 1.0), features_data.get('delta_fatigue', 1.0),
            features_data.get('aerial_advantage', 1.0), features_data.get('press_intensity', 0.0),
            features_data.get('epsilon_senti', 0.5), features_data.get('discussion_growth', 0.0),
            features_data.get('news_impact', 1.0), features_data.get('time_suppression', 1.0),
            features_data.get('card_risk', 0.0), features_data.get('arbitrage_index', 0.0),
            features_data.get('arbitrage_window', 0.0),
            features_data.get('a1', 0.0), features_data.get('a2', 0.5), features_data.get('a3', 0.5),
            features_data.get('a4', 0.0), features_data.get('a5', 0.0), features_data.get('a6', 0.0),
            features_data.get('rank_diff_factor', 0.0),
            features_data.get('form_momentum', 0.0),
            features_data.get('h2h_factor', 0.0),
            features_data.get('rank_factor', 0.0),
            features_data.get('form_factor', 0.0),
        ))
        return cursor.lastrowid


    def save_prediction_tx(self, conn, pred_data: Dict) -> int:
        """事务内保存预测"""
        match_id = pred_data.get('match_id')
        cursor = conn.cursor()
        cursor.execute('DELETE FROM predictions WHERE match_id=?', (match_id,))
        cursor.execute('''
            INSERT INTO predictions (match_id, model_version, prediction_time,
                home_prob, draw_prob, away_prob, value_gap, kelly_percentage, expected_value,
                decision, confidence_level, investment_amount, gate1_pass, gate2_pass, gate3_pass,
                bayesian_updates, score_predictions, prediction_type, prediction)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            pred_data.get('match_id'), pred_data.get('model_version', 'linear_regression_v1'),
            pred_data.get('prediction_time', datetime.now(timezone.utc).isoformat()),
            pred_data.get('home_prob'), pred_data.get('draw_prob'), pred_data.get('away_prob'),
            pred_data.get('value_gap', 0.0), pred_data.get('kelly_percentage', 0.0),
            pred_data.get('expected_value', 0.0), pred_data.get('decision', 'PASS'),
            pred_data.get('confidence_level', 0.0), pred_data.get('investment_amount', 0.0),
            pred_data.get('gate1_pass', 0), pred_data.get('gate2_pass', 0),
            pred_data.get('gate3_pass', 0),
            pred_data.get('bayesian_updates'),
            pred_data.get('score_predictions'),
            pred_data.get('prediction_type', 'backtest'),
            pred_data.get('prediction'),  # H/D/A
        ))
        return cursor.lastrowid


    def find_match_by_teams_tx(self, conn, home_team: str, away_team: str,
                                match_date: str = None) -> Optional[Dict]:
        """事务内按球队查重"""
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

    # ===================== 比赛相关操作 =====================



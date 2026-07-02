"""Database Manager Mixin — crud_entity_mixin"""
import sqlite3
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'football_data.db')


class CrudEntityMixin:
    """DatabaseManager Mixin — crud_entity_mixin"""

    def add_team(self, team_data: Dict) -> int:
        """添加球队"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO teams (team_name, team_code, country, league_id, league_name,
                    rating, attack_strength, defense_strength)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                team_data.get('team_name'), team_data.get('team_code'),
                team_data.get('country'), team_data.get('league_id'),
                team_data.get('league_name'), team_data.get('rating', 70.0),
                team_data.get('attack_strength', 1.0), team_data.get('defense_strength', 1.0)
            ))
            return cursor.lastrowid


    def get_team(self, team_id: int = None, team_name: str = None) -> Optional[Dict]:
        """获取球队信息"""
        with self.get_connection() as conn:
            if team_id:
                row = conn.execute('SELECT * FROM teams WHERE team_id=?', (team_id,)).fetchone()
            elif team_name:
                row = conn.execute('SELECT * FROM teams WHERE team_name=?', (team_name,)).fetchone()
            else:
                return None
            return dict(row) if row else None


    def get_teams_by_league(self, league_id: int) -> List[Dict]:
        """获取联赛下的所有球队"""
        with self.get_connection() as conn:
            rows = conn.execute('SELECT * FROM teams WHERE league_id=? ORDER BY rating DESC',
                                (league_id,)).fetchall()
            return [dict(r) for r in rows]

    # ===================== 赔率相关操作 =====================


    def add_odds(self, odds_data: Dict) -> int:
        """添加赔率"""
        with self.get_connection() as conn:
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


    def get_latest_odds(self, match_id: int) -> Optional[Dict]:
        """获取比赛最新赔率"""
        with self.get_connection() as conn:
            row = conn.execute(
                'SELECT * FROM odds WHERE match_id=? ORDER BY odds_timestamp DESC LIMIT 1',
                (match_id,)
            ).fetchone()
            return dict(row) if row else None

    # ===================== 特征相关操作 =====================


    def save_features(self, features_data: Dict) -> int:
        """保存特征数据（INSERT OR REPLACE，UNIQUE 约束保证不重复）"""
        match_id = features_data.get('match_id')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO match_features (match_id, sigma_trap, beta_dev, lambda_crush,
                    delta_fatigue, aerial_advantage, press_intensity, epsilon_senti,
                    discussion_growth, news_impact, time_suppression, card_risk,
                    arbitrage_index, arbitrage_window, a1, a2, a3, a4, a5, a6,
                    rank_diff_factor, form_momentum, h2h_factor,
                    rank_factor, form_factor, weather_modifier)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                match_id,
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
                features_data.get('weather_modifier', 1.0),
            ))
            return cursor.lastrowid


    def get_features(self, match_id: int) -> Optional[Dict]:
        """获取比赛特征"""
        with self.get_connection() as conn:
            row = conn.execute('SELECT * FROM match_features WHERE match_id=?',
                                (match_id,)).fetchone()
            return dict(row) if row else None

    # ===================== 赔率历史操作 =====================


    def save_odds_history(self, match_id: int, odds_list: List[Dict]) -> int:
        """批量保存赔率历史 (用于 sigma_trap 波动率计算)"""
        count = 0
        with self.get_connection() as conn:
            for odds in odds_list:
                conn.execute('''
                    INSERT OR IGNORE INTO odds_history
                    (match_id, provider, home_odds, draw_odds, away_odds,
                     asian_handicap, odds_timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    match_id,
                    odds.get('provider', 'football-data.org'),
                    odds.get('home_odds'), odds.get('draw_odds'), odds.get('away_odds'),
                    odds.get('asian_handicap'),
                    odds.get('odds_timestamp', datetime.now(timezone.utc).isoformat()),
                ))
                count += 1
        return count


    def get_odds_history(self, match_id: int) -> List[Dict]:
        """获取某场比赛的赔率历史序列"""
        with self.get_connection() as conn:
            rows = conn.execute('''
                SELECT home_odds, draw_odds, away_odds, asian_handicap, odds_timestamp, provider
                FROM odds_history WHERE match_id=? ORDER BY odds_timestamp ASC
            ''', (match_id,)).fetchall()
            return [dict(r) for r in rows]


    def get_odds_series(self, match_id: int, market: str = 'home') -> List[float]:
        """获取赔率时间序列 (用于 calc_odd_volatility)"""
        with self.get_connection() as conn:
            col = {'home': 'home_odds', 'draw': 'draw_odds', 'away': 'away_odds'}.get(market, 'home_odds')
            rows = conn.execute(f'''
                SELECT {col} FROM odds_history WHERE match_id=? AND {col} IS NOT NULL
                ORDER BY odds_timestamp ASC
            ''', (match_id,)).fetchall()
            return [r[0] for r in rows if r[0]]

    # ===================== 球场操作 =====================


    def seed_stadiums(self, stadium_data: List[Tuple[str, str, str, float, float]]) -> None:
        """初始化球场坐标 (team_name, stadium_name, city, lat, lon)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for team_name, stadium, city, lat, lon in stadium_data:
                # 尝试匹配 team_id
                team = conn.execute(
                    'SELECT team_id FROM teams WHERE team_name=? LIMIT 1',
                    (team_name,)).fetchone()
                team_id = team[0] if team else None
                cursor.execute('''
                    INSERT OR REPLACE INTO stadiums
                    (team_id, team_name, stadium_name, city, latitude, longitude)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (team_id, team_name, stadium, city, lat, lon))
        logger.info(f"球场坐标播种完成: {len(stadium_data)} 座")


    def get_stadium_coords(self, team_name: str) -> Optional[Tuple[float, float]]:
        """根据队名获取球场坐标"""
        with self.get_connection() as conn:
            # 精确匹配
            row = conn.execute(
                'SELECT latitude, longitude FROM stadiums WHERE team_name=?',
                (team_name,)).fetchone()
            if row and row[0]:
                return (row[0], row[1])
            # 模糊匹配
            row = conn.execute(
                'SELECT latitude, longitude FROM stadiums WHERE team_name LIKE ?',
                (f'%{team_name}%',)).fetchone()
            if row and row[0]:
                return (row[0], row[1])
        return None


    def get_match_stadium_coords(self, match_id: int) -> Optional[Tuple[float, float, str]]:
        """根据比赛ID获取主队球场坐标"""
        with self.get_connection() as conn:
            row = conn.execute('''
                SELECT s.latitude, s.longitude, m.match_date
                FROM matches m
                LEFT JOIN stadiums s ON s.team_name = m.home_team_name
                WHERE m.match_id=?
            ''', (match_id,)).fetchone()
            if row and row[0]:
                return (row[0], row[1], row[2])
        return None

    # ===================== 天气数据操作 =====================


    def save_weather_data(self, match_id: int, weather: Dict) -> bool:
        """保存天气数据"""
        if not weather:
            return False
        with self.get_connection() as conn:
            conn.execute('''
                INSERT OR REPLACE INTO weather_data
                (match_id, temperature_mean, temperature_max, temperature_min,
                 precipitation, humidity, wind_speed_max, wind_gusts_max,
                 weather_code, weather_desc, is_rainy, is_stormy, is_windy,
                 is_cold, is_hot, source, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                match_id,
                weather.get('temperature_mean'),
                weather.get('temperature_max'),
                weather.get('temperature_min'),
                weather.get('precipitation'),
                weather.get('humidity'),
                weather.get('wind_speed_max'),
                weather.get('wind_gusts_max'),
                weather.get('weather_code'),
                weather.get('weather_desc', ''),
                int(weather.get('is_rainy', False)),
                int(weather.get('is_stormy', False)),
                int(weather.get('is_windy', False)),
                int(weather.get('is_cold', False)),
                int(weather.get('is_hot', False)),
                weather.get('source', 'open-meteo'),
                weather.get('fetched_at', datetime.now(timezone.utc).isoformat()),
            ))
        return True


    def get_weather_data(self, match_id: int) -> Optional[Dict]:
        """获取天气数据"""
        with self.get_connection() as conn:
            row = conn.execute(
                'SELECT * FROM weather_data WHERE match_id=?', (match_id,)).fetchone()
            return dict(row) if row else None


    def get_weather_features(self, match_id: int) -> Dict:
        """获取天气特征修饰因子 (用于注入 feature_calculator)"""
        w = self.get_weather_data(match_id)
        if not w:
            return {'aerial_advantage_mod': 1.0, 'press_mod': 1.0,
                    'fatigue_mod': 1.0, 'weather_risk': 0.0}
        mods = {'aerial_advantage_mod': 1.0, 'press_mod': 1.0,
                'fatigue_mod': 1.0, 'weather_risk': 0.0}
        if w.get('is_rainy'):
            precip = w.get('precipitation', 0) or 0
            mods['aerial_advantage_mod'] = max(0.6, 1.0 - precip * 0.03)
            mods['weather_risk'] += 0.15
        if w.get('is_windy'):
            wind = w.get('wind_speed_max', 30) or 30
            mods['aerial_advantage_mod'] -= max(0, (wind - 30) * 0.01)
            mods['press_mod'] *= 0.9
            mods['weather_risk'] += 0.1
        if w.get('is_hot'):
            mods['fatigue_mod'] = 0.85
            mods['weather_risk'] += 0.08
        if w.get('is_cold'):
            mods['fatigue_mod'] = 0.9
            mods['press_mod'] *= 0.85
            mods['weather_risk'] += 0.05
        if w.get('is_stormy'):
            mods['weather_risk'] += 0.25
        mods['weather_risk'] = min(mods['weather_risk'], 1.0)
        return mods

    # ===================== 预测相关操作 =====================



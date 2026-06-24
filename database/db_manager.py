"""
哨响AI - SQLite数据库管理模块
实现7张表: matches, teams, odds, match_features, predictions, model_training, task_logs
"""
import sqlite3
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from contextlib import contextmanager
from typing import Optional, Dict

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'football_data.db')


class DatabaseManager:
    """SQLite数据库管理器"""

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

    def _init_tables(self) -> None:
        """初始化所有数据表（重构版）
        
        重构说明：
        - 原函数 502 行 → 主函数 12 行
        - 将每张表的创建、迁移、索引提取到独立函数
        - 保持所有业务逻辑完全不变
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            self._create_all_tables(cursor)
            self._run_all_migrations(cursor)
            self._create_all_indexes(cursor)
            logger.info("数据库表初始化完成")

    # ==================== 主协调函数 ====================

    def _create_all_tables(self, cursor) -> None:
        """创建所有数据表"""
        self._create_matches_table(cursor)
        self._create_teams_table(cursor)
        self._create_odds_tables(cursor)
        self._create_stadiums_table(cursor)
        self._create_weather_table(cursor)
        self._create_match_features_table(cursor)
        self._create_predictions_table(cursor)
        self._create_model_training_table(cursor)
        self._create_standings_table(cursor)
        self._create_form_trends_table(cursor)
        self._create_task_logs_table(cursor)
        self._create_bet_records_table(cursor)
        self._create_data_sync_status_table(cursor)

    def _run_all_migrations(self, cursor) -> None:
        """执行所有数据库迁移"""
        self._migrate_matches_table(cursor)
        self._migrate_match_features_table(cursor)
        self._migrate_predictions_table(cursor)
        self._migrate_model_training_table(cursor)

    def _create_all_indexes(self, cursor) -> None:
        """创建所有索引"""
        # matches 表索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_match_date ON matches(match_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_league ON matches(league_id)')
        
        # odds 表索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_odds_match ON odds(match_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_odds_history_match ON odds_history(match_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_odds_history_ts ON odds_history(match_id, odds_timestamp)')
        
        # stadiums 表索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_stadium_team ON stadiums(team_id)')
        
        # weather_data 表索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_weather_match ON weather_data(match_id)')
        
        # match_features 表索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_features_match_uniq ON match_features(match_id)')
        
        # predictions 表索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_pred_match ON predictions(match_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_pred_time ON predictions(prediction_time)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_pred_decision ON predictions(decision)')
        
        # standings 表索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_standings_league ON standings(league_id, season)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_standings_team ON standings(team_name)')
        
        # form_trends 表索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_form_trends_team ON form_trends(team_name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_form_trends_date ON form_trends(match_date)')
        
        # task_logs 表索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_name ON task_logs(task_name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_status ON task_logs(status)')
        
        # bet_records 表索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bet_match ON bet_records(match_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bet_type ON bet_records(bet_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_bet_resolved ON bet_records(is_correct)')
        
        # data_sync_status 表索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sync_type ON data_sync_status(sync_type, league_id)')

    # ==================== 表创建函数 ====================

    def _create_matches_table(self, cursor) -> None:
        """创建比赛基础信息表"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS matches (
                match_id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_date TEXT NOT NULL,
                match_time TEXT,
                league_id INTEGER NOT NULL,
                league_name TEXT,
                home_team_id INTEGER NOT NULL,
                home_team_name TEXT,
                away_team_id INTEGER NOT NULL,
                away_team_name TEXT,
                home_score INTEGER,
                away_score INTEGER,
                final_result TEXT CHECK(final_result IN ('H', 'D', 'A')),
                status TEXT DEFAULT 'scheduled' CHECK(status IN ('scheduled', 'live', 'finished', 'postponed', 'expired')),
                matchday INTEGER,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

    def _create_teams_table(self, cursor) -> None:
        """创建球队信息表"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS teams (
                team_id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name TEXT NOT NULL UNIQUE,
                team_code TEXT,
                country TEXT,
                league_id INTEGER,
                league_name TEXT,
                rating REAL DEFAULT 70.0,
                attack_strength REAL DEFAULT 1.0,
                defense_strength REAL DEFAULT 1.0,
                home_attack REAL DEFAULT 1.0,
                home_defense REAL DEFAULT 1.0,
                away_attack REAL DEFAULT 1.0,
                away_defense REAL DEFAULT 1.0,
                recent_form TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

    def _create_odds_tables(self, cursor) -> None:
        """创建赔率相关表"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS odds (
                odds_id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                provider TEXT DEFAULT 'default',
                home_odds REAL,
                draw_odds REAL,
                away_odds REAL,
                asian_handicap REAL,
                over_under REAL,
                over_odds REAL,
                under_odds REAL,
                return_rate REAL DEFAULT 0.95,
                odds_timestamp TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS odds_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                provider TEXT DEFAULT 'football-data.org',
                home_odds REAL,
                draw_odds REAL,
                away_odds REAL,
                asian_handicap REAL,
                odds_timestamp TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            )
        ''')

    def _create_stadiums_table(self, cursor) -> None:
        """创建球场坐标表"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stadiums (
                stadium_id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER UNIQUE,
                team_name TEXT NOT NULL,
                stadium_name TEXT,
                city TEXT,
                latitude REAL,
                longitude REAL,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

    def _create_weather_table(self, cursor) -> None:
        """创建天气数据表"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS weather_data (
                weather_id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER UNIQUE NOT NULL,
                temperature_mean REAL,
                temperature_max REAL,
                temperature_min REAL,
                precipitation REAL DEFAULT 0.0,
                humidity REAL,
                wind_speed_max REAL DEFAULT 0.0,
                wind_gusts_max REAL DEFAULT 0.0,
                weather_code INTEGER DEFAULT 0,
                weather_desc TEXT,
                is_rainy INTEGER DEFAULT 0,
                is_stormy INTEGER DEFAULT 0,
                is_windy INTEGER DEFAULT 0,
                is_cold INTEGER DEFAULT 0,
                is_hot INTEGER DEFAULT 0,
                source TEXT DEFAULT 'open-meteo',
                fetched_at TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            )
        ''')

    def _create_match_features_table(self, cursor) -> None:
        """创建核心数据特征表"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS match_features (
                feature_id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                sigma_trap REAL DEFAULT 0.0,
                beta_dev REAL DEFAULT 0.0,
                lambda_crush REAL DEFAULT 1.0,
                delta_fatigue REAL DEFAULT 1.0,
                aerial_advantage REAL DEFAULT 1.0,
                press_intensity REAL DEFAULT 0.0,
                epsilon_senti REAL DEFAULT 0.5,
                discussion_growth REAL DEFAULT 0.0,
                news_impact REAL DEFAULT 1.0,
                time_suppression REAL DEFAULT 1.0,
                card_risk REAL DEFAULT 0.0,
                arbitrage_index REAL DEFAULT 0.0,
                arbitrage_window REAL DEFAULT 0.0,
                a1 REAL DEFAULT 0.0,
                a2 REAL DEFAULT 0.5,
                a3 REAL DEFAULT 0.5,
                a4 REAL DEFAULT 0.0,
                a5 REAL DEFAULT 0.0,
                a6 REAL DEFAULT 0.0,
                rank_factor REAL DEFAULT 0.5,
                form_factor REAL DEFAULT 0.5,
                rank_diff_factor REAL DEFAULT 0.0,
                form_momentum REAL DEFAULT 0.0,
                h2h_factor REAL DEFAULT 0.0,
                weather_modifier REAL DEFAULT 1.0,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            )
        ''')

    def _create_predictions_table(self, cursor) -> None:
        """创建预测结果表"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS predictions (
                prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                model_version TEXT NOT NULL DEFAULT 'linear_regression_v1',
                prediction_time TEXT NOT NULL,
                home_prob REAL,
                draw_prob REAL,
                away_prob REAL,
                value_gap REAL DEFAULT 0.0,
                kelly_percentage REAL DEFAULT 0.0,
                expected_value REAL DEFAULT 0.0,
                decision TEXT CHECK(decision IN ('INVEST', 'WATCH', 'PASS', 'SKIP')) DEFAULT 'PASS',
                confidence_level REAL DEFAULT 0.0,
                investment_amount REAL DEFAULT 0.0,
                gate1_pass INTEGER DEFAULT 0,
                gate2_pass INTEGER DEFAULT 0,
                gate3_pass INTEGER DEFAULT 0,
                actual_result TEXT CHECK(actual_result IN ('H', 'D', 'A')),
                is_correct INTEGER,
                profit_loss REAL,
                bayesian_updates TEXT,
                score_predictions TEXT,
                prediction_type TEXT CHECK(prediction_type IN ('forward', 'backtest')) DEFAULT 'backtest',
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            )
        ''')

    def _create_model_training_table(self, cursor) -> None:
        """创建模型训练记录表"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS model_training (
                training_id INTEGER PRIMARY KEY AUTOINCREMENT,
                training_date TEXT NOT NULL,
                model_name TEXT NOT NULL,
                algorithm TEXT,
                training_samples INTEGER DEFAULT 0,
                test_samples INTEGER DEFAULT 0,
                feature_count INTEGER DEFAULT 0,
                training_score REAL DEFAULT 0.0,
                test_score REAL DEFAULT 0.0,
                mse REAL DEFAULT 0.0,
                mae REAL DEFAULT 0.0,
                feature_importance_json TEXT,
                model_path TEXT,
                train_cutoff_date TEXT,
                test_cutoff_date TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

    def _create_standings_table(self, cursor) -> None:
        """创建联赛积分榜表"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS standings (
                standing_id INTEGER PRIMARY KEY AUTOINCREMENT,
                league_id INTEGER NOT NULL,
                league_name TEXT,
                season INTEGER NOT NULL,
                team_name TEXT NOT NULL,
                position INTEGER,
                played_games INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                draws INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                goals_for INTEGER DEFAULT 0,
                goals_against INTEGER DEFAULT 0,
                goal_diff INTEGER DEFAULT 0,
                points INTEGER DEFAULT 0,
                form TEXT,
                updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(league_id, season, team_name)
            )
        ''')

    def _create_form_trends_table(self, cursor) -> None:
        """创建球队趋势/表单数据表"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS form_trends (
                trend_id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name TEXT NOT NULL,
                league_id INTEGER NOT NULL,
                season INTEGER,
                match_date TEXT NOT NULL,
                opponent TEXT,
                result TEXT CHECK(result IN ('W', 'D', 'L')),
                home_away TEXT CHECK(home_away IN ('H', 'A')),
                goals_for INTEGER DEFAULT 0,
                goals_against INTEGER DEFAULT 0,
                is_clean_sheet INTEGER DEFAULT 0,
                is_over25 INTEGER DEFAULT 0,
                trend_score REAL DEFAULT 0.0,
                rolling_form_5 TEXT,
                goals_for_last5 REAL DEFAULT 0.0,
                goals_against_last5 REAL DEFAULT 0.0,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

    def _create_task_logs_table(self, cursor) -> None:
        """创建系统任务日志表"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_name TEXT NOT NULL,
                task_type TEXT CHECK(task_type IN ('DATA_COLLECTION', 'MODEL_TRAINING', 'PREDICTION', 'EVALUATION')),
                start_time TEXT NOT NULL,
                end_time TEXT,
                status TEXT DEFAULT 'RUNNING' CHECK(status IN ('RUNNING', 'SUCCESS', 'FAILED')),
                records_processed INTEGER DEFAULT 0,
                error_message TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

    def _create_bet_records_table(self, cursor) -> None:
        """创建投注记录表"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bet_records (
                bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                league TEXT,
                match_date TEXT,
                bet_type TEXT NOT NULL CHECK(bet_type IN ('recommendation', 'executed')),
                source TEXT DEFAULT 'manual' CHECK(source IN ('manual', 'upset_scan', 'prediction')),
                predicted_result TEXT CHECK(predicted_result IN ('H', 'D', 'A')),
                verdict_text TEXT,
                confidence REAL DEFAULT 0.0,
                home_prob REAL, draw_prob REAL, away_prob REAL,
                home_odds REAL, draw_odds REAL, away_odds REAL,
                value_gap REAL DEFAULT 0.0,
                kelly REAL DEFAULT 0.0,
                expected_value REAL DEFAULT 0.0,
                upset_score REAL DEFAULT 0.0,
                actual_result TEXT CHECK(actual_result IN ('H', 'D', 'A')),
                is_correct INTEGER,
                actual_score TEXT,
                resolved_at TEXT,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

    def _create_data_sync_status_table(self, cursor) -> None:
        """创建数据同步状态表"""
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS data_sync_status (
                sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_type TEXT NOT NULL CHECK(sync_type IN (
                    'HISTORICAL_SEASON', 'LATEST_MATCHES', 'STANDINGS', 'TEAMS'
                )),
                league_id INTEGER NOT NULL,
                league_code TEXT,
                season INTEGER,
                last_sync_time TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                match_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'COMPLETED' CHECK(status IN ('COMPLETED', 'PARTIAL', 'FAILED')),
                sync_details TEXT,
                UNIQUE(sync_type, league_id, season)
            )
        ''')

    # ==================== 数据库迁移函数 ====================

    def _migrate_matches_table(self, cursor) -> None:
        """迁移 matches 表（添加新列和扩展CHECK约束）"""
        # 添加 match_time 列
        try:
            cursor.execute('ALTER TABLE matches ADD COLUMN match_time TEXT')
        except (Exception, sqlite3.Error):
            pass
        
        # 添加实时比分字段
        for col in ['halftime_home', 'halftime_away', 'minute']:
            try:
                cursor.execute(f'ALTER TABLE matches ADD COLUMN {col} INTEGER')
            except (Exception, sqlite3.Error):
                pass
        
        # 扩展 status CHECK 约束
        try:
            cur_check = cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='matches'"
            ).fetchone()
            if cur_check and "'expired'" not in (cur_check[0] or ''):
                logger.info("[迁移] 扩展 matches.status CHECK 约束，添加 'expired'")
                cursor.execute("DROP TABLE IF EXISTS matches_new")
                old_cols = [c[1] for c in cursor.execute("PRAGMA table_info(matches)").fetchall()]
                col_str = ','.join(old_cols)
                cursor.execute('''
                    CREATE TABLE matches_new (
                        match_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        match_date TEXT NOT NULL,
                        match_time TEXT,
                        league_id INTEGER NOT NULL,
                        league_name TEXT,
                        home_team_id INTEGER NOT NULL,
                        home_team_name TEXT,
                        away_team_id INTEGER NOT NULL,
                        away_team_name TEXT,
                        home_score INTEGER,
                        away_score INTEGER,
                        final_result TEXT,
                        status TEXT DEFAULT 'scheduled' CHECK(status IN ('scheduled', 'live', 'finished', 'postponed', 'expired')),
                        matchday INTEGER,
                        created_at TEXT DEFAULT (datetime('now', 'localtime')),
                        updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                        halftime_home INTEGER,
                        halftime_away INTEGER,
                        minute INTEGER
                    )
                ''')
                cursor.execute(f'INSERT INTO matches_new ({col_str}) SELECT {col_str} FROM matches')
                cursor.execute('DROP TABLE matches')
                cursor.execute('ALTER TABLE matches_new RENAME TO matches')
                logger.info("[迁移] matches 表 CHECK 约束扩展完成")
        except (Exception, KeyError, IndexError, sqlite3.Error) as e:
            logger.debug(f"[迁移] CHECK 约束扩展跳过: {e}")

    def _migrate_match_features_table(self, cursor) -> None:
        """迁移 match_features 表（添加新列）"""
        migration_columns = {
            'rank_diff_factor': 'REAL DEFAULT 0.0',
            'form_momentum': 'REAL DEFAULT 0.0',
            'h2h_factor': 'REAL DEFAULT 0.0',
            'a4': 'REAL DEFAULT 0.0',
            'a5': 'REAL DEFAULT 0.0',
            'a6': 'REAL DEFAULT 0.0',
            'rank_factor': 'REAL DEFAULT 0.0',
            'form_factor': 'REAL DEFAULT 0.0',
            'weather_modifier': 'REAL DEFAULT 1.0',
            'handicap_cover_prob': 'REAL DEFAULT 0.5',
            'handicap_cover_confidence': 'REAL DEFAULT 0.0',
            'handicap_value_signal': 'REAL DEFAULT 0.0',
            'handicap_value_exists': 'INTEGER DEFAULT 0',
        }
        try:
            existing_cols = {r[1] for r in cursor.execute('PRAGMA table_info(match_features)').fetchall()}
            for col_name, col_def in migration_columns.items():
                if col_name not in existing_cols:
                    cursor.execute(f'ALTER TABLE match_features ADD COLUMN {col_name} {col_def}')
                    logger.info(f"match_features 新增列: {col_name}")
        except (Exception, KeyError, IndexError, sqlite3.Error) as e:
            logger.debug(f"match_features 列迁移: {e}")

    def _migrate_predictions_table(self, cursor) -> None:
        """迁移 predictions 表（添加新列）"""
        pred_migrations = {
            'score_predictions': 'TEXT',
            'prediction_type': "TEXT CHECK(prediction_type IN ('forward', 'backtest')) DEFAULT 'backtest'",
        }
        try:
            existing_pred_cols = {r[1] for r in cursor.execute('PRAGMA table_info(predictions)').fetchall()}
            for col_name, col_def in pred_migrations.items():
                if col_name not in existing_pred_cols:
                    cursor.execute(f'ALTER TABLE predictions ADD COLUMN {col_name} {col_def}')
                    logger.info(f"predictions 新增列: {col_name}")
        except (Exception, KeyError, IndexError, sqlite3.Error) as e:
            logger.debug(f"predictions 列迁移: {e}")

    def _migrate_model_training_table(self, cursor) -> None:
        """迁移 model_training 表（添加新列）"""
        try:
            existing_mt_cols = {r[1] for r in cursor.execute('PRAGMA table_info(model_training)').fetchall()}
            for col_name, col_def in [('train_cutoff_date', 'TEXT'), ('test_cutoff_date', 'TEXT')]:
                if col_name not in existing_mt_cols:
                    cursor.execute(f'ALTER TABLE model_training ADD COLUMN {col_name} {col_def}')
                    logger.info(f"model_training 新增列: {col_name}")
        except (Exception, KeyError, IndexError, sqlite3.Error) as e:
            logger.debug(f"model_training 列迁移: {e}")
    @contextmanager
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
            odds_data.get('odds_timestamp', datetime.now().isoformat())
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
            pred_data.get('prediction_time', datetime.now().isoformat()),
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
        if match_date and match_date < datetime.now().strftime('%Y-%m-%d'):
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
                odds_data.get('odds_timestamp', datetime.now().isoformat())
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
                    odds.get('odds_timestamp', datetime.now().isoformat()),
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
                weather.get('fetched_at', datetime.now().isoformat()),
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

    def save_prediction(self, pred_data: Dict) -> int:
        """保存预测结果（同一比赛只保留最新预测）"""
        match_id = pred_data.get('match_id')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # 删除该比赛已有的旧预测（只保留最新一条）
            cursor.execute('DELETE FROM predictions WHERE match_id=?', (match_id,))
            cursor.execute('''
                INSERT INTO predictions (match_id, model_version, prediction_time,
                    home_prob, draw_prob, away_prob, value_gap, kelly_percentage, expected_value,
                    decision, confidence_level, investment_amount, gate1_pass, gate2_pass, gate3_pass,
                    bayesian_updates, score_predictions, prediction_type, prediction)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                pred_data.get('match_id'), pred_data.get('model_version', 'linear_regression_v1'),
                pred_data.get('prediction_time', datetime.now().isoformat()),
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

    def update_prediction_result(self, prediction_id: int, actual_result: str,
                                  is_correct: bool, profit_loss: float = None):
        """更新预测实际结果"""
        with self.get_connection() as conn:
            conn.execute('''
                UPDATE predictions SET actual_result=?, is_correct=?, profit_loss=?
                WHERE prediction_id=?
            ''', (actual_result, int(is_correct), profit_loss, prediction_id))

    def get_predictions(self, decision: str = None, match_id: int = None,
                        limit: int = 50, offset: int = 0) -> List[Dict]:
        """获取预测列表，支持按决策或比赛ID过滤"""
        with self.get_connection() as conn:
            conditions = []
            params = []

            if decision:
                conditions.append('decision=?')
                params.append(decision)
            if match_id is not None:
                conditions.append('match_id=?')
                params.append(match_id)

            where_clause = ' AND '.join(conditions) if conditions else '1=1'
            rows = conn.execute(
                f'SELECT * FROM predictions WHERE {where_clause} ORDER BY prediction_time DESC LIMIT ? OFFSET ?',
                tuple(params) + (limit, offset)
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                # 解析 JSON 字段
                if d.get('score_predictions') and isinstance(d['score_predictions'], str):
                    try:
                        d['score_predictions'] = json.loads(d['score_predictions'])
                    except (json.JSONDecodeError, TypeError):
                        d['score_predictions'] = None
                results.append(d)
            return results

    def get_prediction_with_match(self, prediction_id: int) -> Optional[Dict]:
        """获取预测及关联的比赛信息"""
        with self.get_connection() as conn:
            row = conn.execute('''
                SELECT p.*, m.match_date, m.league_name, m.home_team_name, m.away_team_name,
                    m.home_score, m.away_score, m.final_result as match_result
                FROM predictions p
                LEFT JOIN matches m ON p.match_id = m.match_id
                WHERE p.prediction_id=?
            ''', (prediction_id,)).fetchone()
            if row:
                d = dict(row)
                if d.get('score_predictions') and isinstance(d['score_predictions'], str):
                    try:
                        d['score_predictions'] = json.loads(d['score_predictions'])
                    except (json.JSONDecodeError, TypeError):
                        d['score_predictions'] = None
                return d
            return None

    # ===================== 训练记录操作 =====================

    def save_training_record(self, record_data: Dict) -> int:
        """保存训练记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO model_training (training_date, model_name, algorithm,
                    training_samples, test_samples, feature_count, training_score, test_score,
                    mse, mae, feature_importance_json, model_path,
                    train_cutoff_date, test_cutoff_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                record_data.get('training_date', datetime.now().strftime('%Y-%m-%d')),
                record_data.get('model_name'), record_data.get('algorithm'),
                record_data.get('training_samples', 0), record_data.get('test_samples', 0),
                record_data.get('feature_count', 0), record_data.get('training_score', 0.0),
                record_data.get('test_score', 0.0), record_data.get('mse', 0.0),
                record_data.get('mae', 0.0),
                json.dumps(record_data.get('feature_importance', {}), ensure_ascii=False),
                record_data.get('model_path'),
                record_data.get('train_cutoff_date'),
                record_data.get('test_cutoff_date'),
            ))
            return cursor.lastrowid

    def get_training_records(self, limit: int = 20) -> List[Dict]:
        """获取训练记录"""
        with self.get_connection() as conn:
            rows = conn.execute(
                'SELECT * FROM model_training ORDER BY training_date DESC LIMIT ?',
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ===================== 任务日志操作 =====================

    def log_task_start(self, task_name: str, task_type: str) -> int:
        """记录任务开始"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO task_logs (task_name, task_type, start_time, status)
                VALUES (?, ?, ?, 'RUNNING')
            ''', (task_name, task_type, datetime.now().isoformat()))
            return cursor.lastrowid

    def log_task_end(self, log_id: int, status: str, records: int = 0, error: str = None) -> None:
        """记录任务结束"""
        with self.get_connection() as conn:
            conn.execute('''
                UPDATE task_logs SET end_time=?, status=?, records_processed=?, error_message=?
                WHERE log_id=?
            ''', (datetime.now().isoformat(), status, records, error, log_id))

    def get_recent_task_logs(self, limit: int = 20) -> List[Dict]:
        """获取最近的任务日志"""
        with self.get_connection() as conn:
            rows = conn.execute('''
                SELECT * FROM task_logs ORDER BY log_id DESC LIMIT ?
            ''', (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_failed_tasks(self, limit: int = 10) -> List[Dict]:
        """获取失败的任务"""
        with self.get_connection() as conn:
            rows = conn.execute('''
                SELECT * FROM task_logs WHERE status = 'FAILED'
                ORDER BY log_id DESC LIMIT ?
            ''', (limit,)).fetchall()
            return [dict(r) for r in rows]

    # ===================== 统计查询 =====================

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

    def is_season_cached(self, league_id: int, season: int,
                         sync_type: str = 'HISTORICAL_SEASON') -> bool:
        """检查某个联赛-赛季的历史数据是否已完整缓存"""
        with self.get_connection() as conn:
            row = conn.execute('''
                SELECT status FROM data_sync_status
                WHERE sync_type=? AND league_id=? AND season=?
            ''', (sync_type, league_id, season)).fetchone()
            return row is not None and row['status'] == 'COMPLETED'

    def mark_season_cached(self, league_id: int, league_code: str, season: int,
                           match_count: int = 0, sync_type: str = 'HISTORICAL_SEASON',
                           status: str = 'COMPLETED', details: str = None):
        """标记某联赛-赛季数据已缓存"""
        with self.get_connection() as conn:
            conn.execute('''
                INSERT OR REPLACE INTO data_sync_status
                    (sync_type, league_id, league_code, season, last_sync_time,
                     match_count, status, sync_details)
                VALUES (?, ?, ?, ?, datetime('now','localtime'), ?, ?, ?)
            ''', (sync_type, league_id, league_code, season, match_count, status, details))

    def get_cached_seasons(self, league_id: int,
                           sync_type: str = 'HISTORICAL_SEASON') -> List[int]:
        """获取某联赛已缓存的赛季列表"""
        with self.get_connection() as conn:
            rows = conn.execute('''
                SELECT season FROM data_sync_status
                WHERE sync_type=? AND league_id=? AND status='COMPLETED'
                ORDER BY season DESC
            ''', (sync_type, league_id)).fetchall()
            return [r['season'] for r in rows]

    def get_latest_sync_time(self, league_id: int,
                             sync_type: str = 'LATEST_MATCHES') -> Optional[str]:
        """获取某联赛最近一次增量同步的时间"""
        with self.get_connection() as conn:
            row = conn.execute('''
                SELECT MAX(last_sync_time) as latest FROM data_sync_status
                WHERE sync_type=? AND league_id=?
            ''', (sync_type, league_id)).fetchone()
            return row['latest'] if row and row['latest'] else None

    def mark_latest_sync(self, league_id: int, league_code: str,
                         match_count: int = 0, status: str = 'COMPLETED'):
        """标记增量同步完成（用于追踪最新数据拉取）"""
        with self.get_connection() as conn:
            conn.execute('''
                INSERT OR REPLACE INTO data_sync_status
                    (sync_type, league_id, league_code, season, last_sync_time,
                     match_count, status, sync_details)
                VALUES ('LATEST_MATCHES', ?, ?, NULL, datetime('now','localtime'),
                        ?, ?, '增量更新完成')
            ''', (league_id, league_code, match_count, status))

    def get_latest_match_date(self, league_id: int = None) -> Optional[str]:
        """获取数据库中最新的比赛日期（用于增量拉取的起始点）"""
        with self.get_connection() as conn:
            if league_id:
                row = conn.execute('''
                    SELECT MAX(match_date) as latest FROM matches
                    WHERE league_id=? AND status='finished'
                ''', (league_id,)).fetchone()
            else:
                row = conn.execute('''
                    SELECT MAX(match_date) as latest FROM matches
                    WHERE status='finished'
                ''').fetchone()
            return row['latest'] if row else None

    def get_matches_needing_update(self, days_back: int = 14) -> List[Dict]:
        """获取需要更新比分的近期比赛（scheduled/live 状态但已过期）"""
        with self.get_connection() as conn:
            rows = conn.execute('''
                SELECT * FROM matches
                WHERE status IN ('scheduled', 'live')
                  AND match_date < date('now', 'localtime')
                ORDER BY match_date DESC
                LIMIT 100
            ''').fetchall()
            return [dict(r) for r in rows]

    # ===================== 投注记录操作 =====================

    def save_bet_record(self, bet_data: Dict) -> int:
        """保存投注记录（同一比赛+来源+类型只保留最新记录，防止批量预测重复）"""
        match_id = bet_data.get('match_id')
        source = bet_data.get('source', 'manual')
        bet_type = bet_data.get('bet_type', 'recommendation')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # 删除该比赛同一来源同一类型的旧记录，防止重复
            cursor.execute(
                'DELETE FROM bet_records WHERE match_id=? AND source=? AND bet_type=?',
                (match_id, source, bet_type)
            )
            cursor.execute('''
                INSERT INTO bet_records (match_id, home_team, away_team, league, match_date,
                    bet_type, source, predicted_result, verdict_text, confidence,
                    home_prob, draw_prob, away_prob,
                    home_odds, draw_odds, away_odds,
                    value_gap, kelly, expected_value, upset_score, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                match_id,
                bet_data.get('home_team', ''), bet_data.get('away_team', ''),
                bet_data.get('league', ''), bet_data.get('match_date', ''),
                bet_type,
                source,
                bet_data.get('predicted_result'), bet_data.get('verdict_text', ''),
                bet_data.get('confidence', 0.0),
                bet_data.get('home_prob'), bet_data.get('draw_prob'), bet_data.get('away_prob'),
                bet_data.get('home_odds'), bet_data.get('draw_odds'), bet_data.get('away_odds'),
                bet_data.get('value_gap', 0.0), bet_data.get('kelly', 0.0),
                bet_data.get('expected_value', 0.0), bet_data.get('upset_score', 0.0),
                bet_data.get('notes', ''),
            ))
            return cursor.lastrowid

    def update_bet_result(self, bet_id: int, actual_result: str, is_correct: int,
                          actual_score: str = None):
        """更新投注实际结果"""
        with self.get_connection() as conn:
            conn.execute('''
                UPDATE bet_records SET actual_result=?, is_correct=?, actual_score=?,
                    resolved_at=datetime('now','localtime')
                WHERE bet_id=?
            ''', (actual_result, is_correct, actual_score, bet_id))

    def get_bet_records(self, limit: int = 50, offset: int = 0,
                        bet_type: str = None, resolved_only: bool = False) -> List[Dict]:
        """获取投注记录列表"""
        conditions = []
        params = []
        if bet_type:
            conditions.append("b.bet_type=?")
            params.append(bet_type)
        if resolved_only:
            conditions.append("b.is_correct IS NOT NULL")
        where = " AND ".join(conditions) if conditions else "1=1"
        with self.get_connection() as conn:
            rows = conn.execute(f'''
                SELECT b.*,
                    m.league_name as match_league, m.status as match_status,
                    m.home_score, m.away_score
                FROM bet_records b
                LEFT JOIN matches m ON b.match_id = m.match_id
                WHERE {where}
                ORDER BY b.created_at DESC LIMIT ? OFFSET ?
            ''', params + [limit, offset]).fetchall()
            return [dict(r) for r in rows]

    def get_bet_stats(self) -> Dict:
        """获取投注统计"""
        with self.get_connection() as conn:
            total = conn.execute('SELECT COUNT(*) FROM bet_records').fetchone()[0]
            executed = conn.execute(
                "SELECT COUNT(*) FROM bet_records WHERE bet_type='executed'"
            ).fetchone()[0]
            recommendations = conn.execute(
                "SELECT COUNT(*) FROM bet_records WHERE bet_type='recommendation'"
            ).fetchone()[0]
            resolved = conn.execute(
                "SELECT COUNT(*) FROM bet_records WHERE is_correct IS NOT NULL"
            ).fetchone()[0]
            correct = conn.execute(
                "SELECT COUNT(*) FROM bet_records WHERE is_correct=1"
            ).fetchone()[0]
            accuracy = round(correct / resolved * 100, 2) if resolved > 0 else 0.0

            # 按类型分准确率
            exec_correct = conn.execute(
                "SELECT COUNT(*) FROM bet_records WHERE bet_type='executed' AND is_correct=1"
            ).fetchone()[0]
            exec_total = conn.execute(
                "SELECT COUNT(*) FROM bet_records WHERE bet_type='executed' AND is_correct IS NOT NULL"
            ).fetchone()[0]
            exec_accuracy = round(exec_correct / exec_total * 100, 2) if exec_total > 0 else 0.0

            return {
                'total': total, 'executed': executed, 'recommendations': recommendations,
                'resolved': resolved, 'correct': correct, 'accuracy': accuracy,
                'exec_accuracy': exec_accuracy,
            }

    def resolve_bet_results(self, match_id: int = None) -> int:
        """自动结算：找出已结束比赛的投注记录并标记结果"""
        updated = 0
        with self.get_connection() as conn:
            if match_id:
                rows = conn.execute('''
                    SELECT b.bet_id, b.predicted_result,
                        m.home_score, m.away_score, m.final_result, m.status
                    FROM bet_records b
                    JOIN matches m ON b.match_id = m.match_id
                    WHERE b.match_id = ? AND m.status = 'finished'
                        AND b.is_correct IS NULL AND m.final_result IS NOT NULL
                ''', (match_id,)).fetchall()
            else:
                rows = conn.execute('''
                    SELECT b.bet_id, b.predicted_result,
                        m.home_score, m.away_score, m.final_result, m.status
                    FROM bet_records b
                    JOIN matches m ON b.match_id = m.match_id
                    WHERE m.status = 'finished' AND b.is_correct IS NULL
                        AND m.final_result IS NOT NULL
                ''').fetchall()

            for row in rows:
                actual = row['final_result']
                is_correct = 1 if row['predicted_result'] == actual else 0
                score = f"{row['home_score']}-{row['away_score']}"
                conn.execute('''
                    UPDATE bet_records SET actual_result=?, is_correct=?, actual_score=?,
                        resolved_at=datetime('now','localtime')
                    WHERE bet_id=?
                ''', (actual, is_correct, score, row['bet_id']))
                updated += 1

        if updated:
            logger.info(f"自动结算 {updated} 条投注记录")
        return updated

    def get_review_stats(self, days: int = 30) -> Dict:
        """
        复盘统计：多维度分析投注准确率、ROI、联赛分布等
        """
        import math
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        with self.get_connection() as conn:
            # 基础统计
            total = conn.execute(
                'SELECT COUNT(*) FROM bet_records WHERE is_correct IS NOT NULL'
            ).fetchone()[0]
            correct = conn.execute(
                'SELECT COUNT(*) FROM bet_records WHERE is_correct=1'
            ).fetchone()[0]
            accuracy = round(correct / total * 100, 2) if total > 0 else 0.0

            # 近N天统计
            recent_total = conn.execute(
                'SELECT COUNT(*) FROM bet_records WHERE is_correct IS NOT NULL AND created_at >= ?',
                (since,)
            ).fetchone()[0]
            recent_correct = conn.execute(
                'SELECT COUNT(*) FROM bet_records WHERE is_correct=1 AND created_at >= ?',
                (since,)
            ).fetchone()[0]
            recent_accuracy = round(recent_correct / recent_total * 100, 2) if recent_total > 0 else 0.0

            # 按联赛
            league_rows = conn.execute('''
                SELECT COALESCE(NULLIF(league,''), '未知') as lg,
                       COUNT(*) as total,
                       SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) as correct
                FROM bet_records WHERE is_correct IS NOT NULL
                GROUP BY lg ORDER BY total DESC
            ''').fetchall()
            league_stats = []
            for r in league_rows:
                lg = dict(r)
                lg['accuracy'] = round(lg['correct'] / lg['total'] * 100, 2) if lg['total'] > 0 else 0.0
                league_stats.append(lg)

            # 按置信度区间
            conf_buckets = [(0, 50), (50, 60), (60, 70), (70, 80), (80, 100)]
            conf_stats = []
            for lo, hi in conf_buckets:
                c = conn.execute(
                    'SELECT COUNT(*), SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) '
                    'FROM bet_records WHERE is_correct IS NOT NULL AND confidence >= ? AND confidence < ?',
                    (lo, hi)
                ).fetchone()
                total_c = c[0] or 0
                correct_c = c[1] or 0
                conf_stats.append({
                    'range': f'{lo}-{hi}%',
                    'total': total_c,
                    'correct': correct_c,
                    'accuracy': round(correct_c / total_c * 100, 2) if total_c > 0 else 0.0,
                })

            # 按预测方向
            dir_rows = conn.execute('''
                SELECT predicted_result,
                       COUNT(*) as total,
                       SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) as correct
                FROM bet_records WHERE is_correct IS NOT NULL
                GROUP BY predicted_result
            ''').fetchall()
            dir_map = {'H': '主胜', 'D': '平局', 'A': '客胜'}
            dir_stats = []
            for r in dir_rows:
                d = dict(r)
                d['label'] = dir_map.get(d['predicted_result'], d['predicted_result'])
                d['accuracy'] = round(d['correct'] / d['total'] * 100, 2) if d['total'] > 0 else 0.0
                dir_stats.append(d)

            # ROI 估算（按推荐投注）
            roi_total = conn.execute(
                'SELECT COUNT(*), AVG(home_odds), AVG(draw_odds), AVG(away_odds) '
                'FROM bet_records WHERE is_correct IS NOT NULL'
            ).fetchone()
            roi_correct = conn.execute('''
                SELECT predicted_result, COUNT(*)
                FROM bet_records WHERE is_correct=1 AND is_correct IS NOT NULL
                GROUP BY predicted_result
            ''').fetchall()
            roi_by_dir = {r['predicted_result']: r[1] for r in roi_correct}
            # 简化ROI: 正确=赢赔率-1，错误= -1
            total_roi = 0.0
            for r in conn.execute(
                'SELECT predicted_result, is_correct, home_odds, draw_odds, away_odds '
                'FROM bet_records WHERE is_correct IS NOT NULL'
            ).fetchall():
                odds_map = {'H': r['home_odds'], 'D': r['draw_odds'], 'A': r['away_odds']}
                odds_val = odds_map.get(r['predicted_result'], 2.0) or 2.0
                total_roi += (odds_val - 1) if r['is_correct'] == 1 else -1.0

            roi_pct = round(total_roi / total * 100, 2) if total > 0 else 0.0

            # 每日准确率时间线
            timeline_rows = conn.execute('''
                SELECT substr(created_at, 1, 10) as dt,
                       COUNT(*) as total,
                       SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) as correct
                FROM bet_records WHERE is_correct IS NOT NULL AND created_at >= ?
                GROUP BY dt ORDER BY dt ASC
            ''', (since,)).fetchall()
            timeline = [{
                'date': r[0],
                'total': r[1],
                'correct': r[2],
                'accuracy': round(r[2] / r[1] * 100, 2) if r[1] > 0 else 0,
            } for r in timeline_rows]

            # 最近连胜/连败
            streak_rows = conn.execute(
                'SELECT is_correct FROM bet_records WHERE is_correct IS NOT NULL '
                'ORDER BY created_at DESC LIMIT 20'
            ).fetchall()
            current_streak = 0
            streak_type = None
            for s in streak_rows:
                if current_streak == 0:
                    streak_type = 'win' if s[0] == 1 else 'lose'
                    current_streak = 1
                elif streak_type == 'win' and s[0] == 1:
                    current_streak += 1
                elif streak_type == 'lose' and s[0] == 0:
                    current_streak += 1
                else:
                    break

            # 待结算
            pending = conn.execute(
                'SELECT COUNT(*) FROM bet_records WHERE is_correct IS NULL'
            ).fetchone()[0]

        return {
            'total': total, 'correct': correct, 'accuracy': accuracy,
            'recent_total': recent_total, 'recent_correct': recent_correct,
            'recent_accuracy': recent_accuracy,
            'pending': pending,
            'roi_pct': roi_pct,
            'current_streak': current_streak,
            'streak_type': streak_type or '',
            'by_league': league_stats,
            'by_confidence': conf_stats,
            'by_direction': dir_stats,
            'timeline': timeline,
            'days': days,
        }

    def get_db_sync_stats(self) -> Dict:
        """获取数据同步状态统计"""
        with self.get_connection() as conn:
            total_seasons = conn.execute(
                "SELECT COUNT(*) FROM data_sync_status WHERE sync_type='HISTORICAL_SEASON'"
            ).fetchone()[0]
            completed_seasons = conn.execute('''
                SELECT COUNT(*) FROM data_sync_status
                WHERE sync_type='HISTORICAL_SEASON' AND status='COMPLETED'
            ''').fetchone()[0]
            latest_sync = conn.execute('''
                SELECT league_code, last_sync_time, match_count FROM data_sync_status
                WHERE sync_type='LATEST_MATCHES' ORDER BY last_sync_time DESC LIMIT 1
            ''').fetchone()

            return {
                'total_cached_seasons': total_seasons,
                'completed_seasons': completed_seasons,
                'latest_sync': dict(latest_sync) if latest_sync else None,
                'has_historical_data': completed_seasons > 0,
            }


    # ===================== 预测服务专用查询 =====================

    def get_next_scheduled_match(self) -> Optional[Dict]:
        """获取下一场待预测比赛（status=scheduled, match_date>=今天）"""
        with self.get_connection() as conn:
            row = conn.execute('''
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
            ''').fetchone()
            return dict(row) if row else None

    def get_team_features(self, team_name: str,
                           recent_days: int = 180,
                           min_matches: int = 3) -> Dict[str, float]:
        """
        获取球队近期特征聚合（从 match_features 表 + standings + form_trends 综合计算）

        返回包含 avg_* 前缀的特征均值 + 排名/形态/交锋数据，
        供 FeatureCalculator.calculate_match_features 使用。

        Returns:
            {
                'match_count': int,
                'avg_a1': float, ..., 'avg_a6': float,
                'avg_sigma_trap': float, ..., 'avg_delta_fatigue': float,
                'avg_rank_diff_factor': float, 'avg_form_momentum': float,
                'avg_h2h_factor': float, 'avg_rank_factor': float,
                'avg_form_factor': float, 'avg_aerial_advantage': float,
                'avg_press_intensity': float, 'avg_card_risk': float,
                'avg_beta_dev': float, 'avg_lambda_crush': float,
                'avg_epsilon_senti': float,
                'avg_position': float, 'avg_points': float,
                'recent_form_score': float, 'goals_for_avg': float,
                'goals_against_avg': float,
            }
        """
        feature_cols = [
            'a1', 'a2', 'a3', 'a4', 'a5', 'a6',
            'sigma_trap', 'lambda_crush', 'epsilon_senti',
            'rank_diff_factor', 'form_momentum', 'h2h_factor',
            'rank_factor', 'form_factor', 'aerial_advantage',
            'press_intensity', 'card_risk', 'beta_dev', 'delta_fatigue',
        ]
        result: Dict[str, float] = {'team_name': team_name}

        with self.get_connection() as conn:
            # 1. 聚合 match_features 表中该队近期比赛的特征均值
            avg_cols = ', '.join(f'AVG(mf.{col}) as avg_{col}' for col in feature_cols)
            row = conn.execute(f'''
                SELECT COUNT(*) as match_count, {avg_cols}
                FROM matches m
                JOIN match_features mf ON m.match_id = mf.match_id
                WHERE (m.home_team_name = ? OR m.away_team_name = ?)
                  AND m.status = 'finished'
                  AND m.match_date >= date('now', '-' || ? || ' days', 'localtime')
            ''', (team_name, team_name, recent_days)).fetchone()

            if row and row['match_count'] >= min_matches:
                result['match_count'] = row['match_count']
                for col in feature_cols:
                    result[f'avg_{col}'] = round(float(row[f'avg_{col}'] or 0), 4)
            else:
                result['match_count'] = row['match_count'] if row else 0
                for col in feature_cols:
                    result[f'avg_{col}'] = 0.0

            # 2. 球队排名（standings 表最新赛季）
            standing_row = conn.execute('''
                SELECT position, points, played_games, wins, draws, losses,
                       goals_for * 1.0 / MAX(played_games, 1) as gpg_for,
                       goals_against * 1.0 / MAX(played_games, 1) as gpg_against
                FROM standings
                WHERE team_name = ?
                ORDER BY season DESC LIMIT 1
            ''', (team_name,)).fetchone()
            if standing_row:
                result['avg_position'] = float(standing_row['position'] or 10)
                result['avg_points'] = float(standing_row['points'] or 0)
                result['goals_for_avg'] = round(float(standing_row['gpg_for'] or 0), 2)
                result['goals_against_avg'] = round(float(standing_row['gpg_against'] or 0), 2)
            else:
                result['avg_position'] = 10.0
                result['avg_points'] = 0.0
                result['goals_for_avg'] = 1.0
                result['goals_against_avg'] = 1.0

            # 3. 近期形态分数（最近 5 场：W=1, D=0.5, L=0）
            form_rows = conn.execute('''
                SELECT final_result, home_team_name
                FROM matches
                WHERE (home_team_name = ? OR away_team_name = ?)
                  AND status = 'finished' AND final_result IS NOT NULL
                ORDER BY match_date DESC LIMIT 5
            ''', (team_name, team_name)).fetchall()
            if form_rows:
                score = 0.0
                for r in form_rows:
                    is_home = (r['home_team_name'] == team_name)
                    res = r['final_result']
                    if res == 'D':
                        score += 0.5
                    elif (is_home and res == 'H') or (not is_home and res == 'A'):
                        score += 1.0
                result['recent_form_score'] = round(score / len(form_rows), 4)
            else:
                result['recent_form_score'] = 0.5

        return result

    def get_h2h_advantage(self, team_a: str, team_b: str,
                           limit: int = 10) -> float:
        """
        计算 team_a 对 team_b 的历史交锋优势分数 [-1, 1]
        正值 = team_a 占优，负值 = team_b 占优
        """
        with self.get_connection() as conn:
            rows = conn.execute('''
                SELECT home_team_name, away_team_name, final_result,
                       home_score, away_score
                FROM matches
                WHERE status = 'finished' AND final_result IS NOT NULL
                  AND ((home_team_name = ? AND away_team_name = ?)
                       OR (home_team_name = ? AND away_team_name = ?))
                ORDER BY match_date DESC
                LIMIT ?
            ''', (team_a, team_b, team_b, team_a, limit)).fetchall()

            if not rows:
                return 0.0

            score = 0.0
            for r in rows:
                is_a_home = (r['home_team_name'] == team_a)
                if r['final_result'] == 'D':
                    score += 0.0
                elif (is_a_home and r['final_result'] == 'H') or \
                     (not is_a_home and r['final_result'] == 'A'):
                    score += 1.0  # team_a wins
                else:
                    score -= 1.0  # team_b wins

            return round(max(-1.0, min(1.0, score / len(rows))), 4)

    def get_rank_diff_factor(self, home_team: str, away_team: str) -> float:
        """计算排名差因子（归一化到 [-1, 1]）"""
        h_pos = 10.0
        a_pos = 10.0
        with self.get_connection() as conn:
            for team, attr in [(home_team, 'h_pos'), (away_team, 'a_pos')]:
                row = conn.execute('''
                    SELECT position FROM standings
                    WHERE team_name = ? ORDER BY season DESC LIMIT 1
                ''', (team,)).fetchone()
                if row:
                    if attr == 'h_pos':
                        h_pos = float(row['position'] or 10)
                    else:
                        a_pos = float(row['position'] or 10)
        diff = h_pos - a_pos  # 正值=客队排名更高，负值=主队排名更高
        return round(max(-1.0, min(1.0, -diff / 10.0)), 4)

    def get_recent_predictions(self, limit: int = 50,
                                league: str = None) -> List[Dict]:
        """获取最近预测记录"""
        conditions = []
        params = []
        if league:
            conditions.append('m.league_name = ?')
            params.append(league)
        where = ' AND '.join(conditions) if conditions else '1=1'
        with self.get_connection() as conn:
            # 确保 teams 表有 team_name_zh 列
            try:
                conn.execute('ALTER TABLE teams ADD COLUMN team_name_zh TEXT')
            except (Exception, KeyError, IndexError, sqlite3.Error):
                pass
            rows = conn.execute(f'''
                SELECT p.*, m.match_date, m.home_team_name, m.away_team_name,
                       m.league_name, m.home_score, m.away_score, m.final_result,
                       m.status as match_status,
                       th.team_name_zh AS home_team_zh,
                       ta.team_name_zh AS away_team_zh
                FROM predictions p
                LEFT JOIN matches m ON p.match_id = m.match_id
                LEFT JOIN teams th ON m.home_team_id = th.team_id
                LEFT JOIN teams ta ON m.away_team_id = ta.team_id
                WHERE {where}
                ORDER BY p.prediction_time DESC
                LIMIT ?
            ''', params + [limit]).fetchall()
            return [dict(r) for r in rows]

    def get_prediction_stats(self) -> Dict:
        """获取预测统计摘要 (v3.0: 扩展返回字段)"""
        with self.get_connection() as conn:
            total = conn.execute('SELECT COUNT(*) FROM predictions').fetchone()[0]
            evaluated = conn.execute(
                'SELECT COUNT(*) FROM predictions WHERE is_correct IS NOT NULL'
            ).fetchone()[0]
            correct = conn.execute(
                'SELECT COUNT(*) FROM predictions WHERE is_correct = 1'
            ).fetchone()[0]
            accuracy = round(correct / evaluated, 3) if evaluated > 0 else 0.0

            # 按等级统计
            s_total = conn.execute("SELECT COUNT(*) FROM predictions WHERE tier='S'").fetchone()[0]
            s_correct = conn.execute("SELECT COUNT(*) FROM predictions WHERE tier='S' AND is_correct=1").fetchone()[0]
            s_acc = round(s_correct / s_total, 3) if s_total > 0 else 0.0

            a_total = conn.execute("SELECT COUNT(*) FROM predictions WHERE tier='A'").fetchone()[0]
            a_correct = conn.execute("SELECT COUNT(*) FROM predictions WHERE tier='A' AND is_correct=1").fetchone()[0]
            a_acc = round(a_correct / a_total, 3) if a_total > 0 else 0.0

            # locked: lock_confidence >= 0.70
            locked_total = conn.execute("SELECT COUNT(*) FROM predictions WHERE lock_confidence >= 0.70").fetchone()[0]

            return {
                'total_predictions': total,
                'evaluated': evaluated,
                'correct': correct,
                'overall_accuracy': accuracy,
                's_tier_count': s_total,
                's_tier_accuracy': s_acc,
                'a_tier_count': a_total,
                'a_tier_accuracy': a_acc,
                'locked_count': locked_total,
            }

# 全局数据库实例
_db_instance = None


def get_db() -> DatabaseManager:
    """获取数据库单例"""
    global _db_instance
    if _db_instance is None:
        _db_instance = DatabaseManager()
    return _db_instance

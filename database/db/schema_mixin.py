"""Database Manager Mixin — schema_mixin"""
import sqlite3
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'football_data.db')


class SchemaMixin:
    """DatabaseManager Mixin — schema_mixin"""

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

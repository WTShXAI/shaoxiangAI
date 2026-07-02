"""Database Manager Mixin — crud_prediction_mixin"""
import sqlite3
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'football_data.db')


class CrudPredictionMixin:
    """DatabaseManager Mixin — crud_prediction_mixin"""

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
                record_data.get('training_date', datetime.now(timezone.utc).strftime('%Y-%m-%d')),
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
            ''', (task_name, task_type, datetime.now(timezone.utc).isoformat()))
            return cursor.lastrowid


    def log_task_end(self, log_id: int, status: str, records: int = 0, error: str = None) -> None:
        """记录任务结束"""
        with self.get_connection() as conn:
            conn.execute('''
                UPDATE task_logs SET end_time=?, status=?, records_processed=?, error_message=?
                WHERE log_id=?
            ''', (datetime.now(timezone.utc).isoformat(), status, records, error, log_id))


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



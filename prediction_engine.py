"""
哨响AI - 预测引擎 v3.0
=====================
- 从数据库加载未预测比赛
- 使用集成模型进行批量预测
- 输出标准化 CSV: match_id, home/away_team, home/draw/away_prob, prediction, confidence
- 同步写入数据库 predictions 表
"""
import sys, os, logging, yaml, joblib, csv
from datetime import datetime
from typing import Dict, List, Optional
import numpy as np
import pandas as pd
import sqlite3

from ensemble_trainer import EnsembleTrainer


class PredictionEngine:
    """
    预测引擎：从数据加载到 CSV/DB 输出的完整流程
    """

    def __init__(self, model_path: str = None, config_path: str = None,
                 db_path: str = None):
        """
        初始化预测引擎

        Args:
            model_path: .joblib 模型文件路径
            config_path: config.yaml 路径
            db_path: SQLite 数据库路径（可选，从 config 读取）
        """
        self.logger = self._setup_logger()

        # 加载配置
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), 'config.yaml'
            )
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

        # 数据库路径
        root = self.config['paths']['project_root']
        if not os.path.isabs(root):
            root = os.path.abspath(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), root)
            )
        self.db_path = db_path or os.path.join(root, self.config['database']['path'])

        # 输出目录
        self._output_dir = os.path.join(root, self.config['paths']['output_dir'])
        os.makedirs(self._output_dir, exist_ok=True)

        # 加载模型
        if model_path is None:
            # 自动查找最新模型
            model_dir = os.path.join(root, self.config['paths']['model_dir'])
            model_path = self._find_latest_model(model_dir)

        if model_path and os.path.exists(model_path):
            self.trainer = EnsembleTrainer.load_pipeline(model_path)
            self.logger.info(f"模型加载成功: {model_path}")
        else:
            raise FileNotFoundError(
                f"模型文件不存在: {model_path}\n"
                f"请先运行 ensemble_trainer.py 训练模型"
            )

        self.model_path = model_path

    @staticmethod
    def _setup_logger():
        logger = logging.getLogger('PredictionEngine')
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                '%(asctime)s [%(levelname)s] %(name)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            ))
            logger.addHandler(handler)
        return logger

    def _find_latest_model(self, model_dir: str) -> Optional[str]:
        """自动查找最新的模型文件"""
        if not os.path.isdir(model_dir):
            return None
        files = [f for f in os.listdir(model_dir)
                 if f.startswith(self.config['output']['model_prefix'])
                 and f.endswith('.joblib')]
        if not files:
            return None
        files.sort(reverse=True)
        path = os.path.join(model_dir, files[0])
        self.logger.info(f"自动选择最新模型: {path}")
        return path

    def _get_connection(self) -> sqlite3.Connection:
        """获取复用的数据库连接（带 busy_timeout 避免 SQLITE_BUSY）"""
        if not hasattr(self, '_conn') or self._conn is None:
            self._conn = sqlite3.connect(self.db_path, timeout=30.0)
            self._conn.execute("PRAGMA busy_timeout = 30000")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    # ══════════════════════════════════════════════════
    # 数据加载
    # ══════════════════════════════════════════════════

    def load_matches_for_prediction(
        self, date_from: str = None, date_to: str = None,
        league_id: int = None, limit: int = None
    ) -> pd.DataFrame:
        """
        从数据库加载待预测比赛（scheduled/live状态，有特征数据）

        Args:
            date_from: 开始日期 (YYYY-MM-DD)
            date_to: 结束日期 (YYYY-MM-DD)
            league_id: 联赛ID过滤
            limit: 最大比赛数
        """
        feat_cols = self.config['data']['feature_columns']
        cols_sql = ", ".join([f"mf.{c}" for c in feat_cols])

        conn = self._get_connection()

        conditions = [
            "m.status IN ('scheduled', 'live')",
            "mf.match_id IS NOT NULL"
        ]
        params = []

        if date_from:
            conditions.append("m.match_date >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("m.match_date <= ?")
            params.append(date_to)
        if league_id:
            conditions.append("m.league_id = ?")
            params.append(league_id)

        where_clause = " AND ".join(conditions)
        limit_clause = ""
        if limit:
            limit_clause = " LIMIT ?"
            params.append(limit)

        query = f"""
        SELECT m.match_id, m.home_team_name, m.away_team_name, m.match_date,
               m.match_time, m.league_name,
               {cols_sql}
        FROM matches m
        JOIN match_features mf ON m.match_id = mf.match_id
        WHERE {where_clause}
        ORDER BY m.match_date, m.match_time
        {limit_clause}
        """

        df = pd.read_sql_query(query, conn, params=params)

        self.logger.info(f"加载 {len(df)} 场待预测比赛")
        return df

    # ══════════════════════════════════════════════════
    # 预测执行
    # ══════════════════════════════════════════════════

    def predict(self, df: pd.DataFrame = None,
                date_from: str = None, date_to: str = None,
                league_id: int = None) -> pd.DataFrame:
        """
        执行预测

        Args:
            df: 待预测比赛 DataFrame (如不提供则从数据库加载)
            date_from, date_to, league_id: 数据加载过滤条件

        Returns:
            包含预测结果的 DataFrame
        """
        if df is None:
            df = self.load_matches_for_prediction(
                date_from=date_from, date_to=date_to, league_id=league_id
            )

        if len(df) == 0:
            self.logger.warning("无待预测比赛")
            return pd.DataFrame()

        self.logger.info(f"开始预测 {len(df)} 场比赛...")

        feature_cols = self.trainer.feature_names
        defaults = self.config['data']['default_values']

        # 构建特征矩阵
        feature_dicts = []
        for _, row in df.iterrows():
            feat = {}
            for col in feature_cols:
                if col in row and pd.notna(row[col]):
                    feat[col] = float(row[col])
                else:
                    feat[col] = defaults.get(col, 0.0)
            feature_dicts.append(feat)

        # 批量预测
        proba = self.trainer.predict_batch(feature_dicts)

        # 构建结果 DataFrame
        results = []
        for i, (_, row) in enumerate(df.iterrows()):
            home_prob = float(proba[i, 0])
            draw_prob = float(proba[i, 1])
            away_prob = float(proba[i, 2])

            # 确定预测方向
            if home_prob >= max(draw_prob, away_prob):
                prediction = 'H'
                confidence = home_prob
            elif draw_prob >= away_prob:
                prediction = 'D'
                confidence = draw_prob
            else:
                prediction = 'A'
                confidence = away_prob

            results.append({
                'match_id': row['match_id'],
                'home_team': row.get('home_team_name', ''),
                'away_team': row.get('away_team_name', ''),
                'match_date': row.get('match_date', ''),
                'league': row.get('league_name', ''),
                'home_prob': round(home_prob, 4),
                'draw_prob': round(draw_prob, 4),
                'away_prob': round(away_prob, 4),
                'prediction': prediction,
                'confidence': round(confidence, 4),
            })

        result_df = pd.DataFrame(results)

        # 统计
        pred_counts = result_df['prediction'].value_counts()
        self.logger.info(
            f"预测完成 | 主胜: {pred_counts.get('H',0)} 场 | "
            f"平局: {pred_counts.get('D',0)} 场 | "
            f"客胜: {pred_counts.get('A',0)} 场"
        )

        return result_df

    # ══════════════════════════════════════════════════
    # CSV 输出
    # ══════════════════════════════════════════════════

    def export_csv(self, result_df: pd.DataFrame = None,
                   date_from: str = None, date_to: str = None,
                   league_id: int = None, output_path: str = None) -> str:
        """
        导出预测结果为标准化 CSV 文件

        输出格式:
        match_id, home_team, away_team, match_date, league,
        home_prob, draw_prob, away_prob, prediction, confidence

        Args:
            result_df: 预测结果 DataFrame
            date_from, date_to, league_id: 预测过滤条件
            output_path: 自定义输出路径

        Returns:
            输出的 CSV 文件路径
        """
        if result_df is None or len(result_df) == 0:
            result_df = self.predict(
                date_from=date_from, date_to=date_to, league_id=league_id
            )

        if len(result_df) == 0:
            self.logger.info("无数据输出")
            return ""

        # 生成输出路径
        if output_path is None:
            timestamp = datetime.now().strftime(
                self.config['output']['timestamp_format']
            )
            prefix = self.config['output']['prediction_prefix']
            output_path = os.path.join(
                self._output_dir, f"{prefix}_{timestamp}.csv"
            )

        # 确保目录存在
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # 写入 CSV (UTF-8 with BOM for Excel compatibility)
        result_df.to_csv(output_path, index=False, encoding='utf-8-sig',
                         quoting=csv.QUOTE_MINIMAL)

        self.logger.info(f"CSV 预测文件已保存: {output_path} ({len(result_df)} 行)")
        return output_path

    # ══════════════════════════════════════════════════
    # 数据库同步
    # ══════════════════════════════════════════════════

    def save_to_database(self, result_df: pd.DataFrame) -> int:
        """将预测结果批量写入数据库 predictions 表（executemany）"""
        if result_df is None or len(result_df) == 0:
            return 0

        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            # 批量删除旧预测
            match_ids = [(int(mid),) for mid in result_df['match_id']]
            cursor.executemany('DELETE FROM predictions WHERE match_id=?', match_ids)

            # 批量插入新预测
            now = datetime.now().isoformat()
            rows = []
            for _, row in result_df.iterrows():
                rows.append((
                    int(row['match_id']),
                    'ensemble_v3',
                    now,
                    float(row['home_prob']),
                    float(row['draw_prob']),
                    float(row['away_prob']),
                    'WATCH',
                    float(row['confidence']),
                    'backtest',
                ))

            cursor.executemany('''
                INSERT INTO predictions (
                    match_id, model_version, prediction_time,
                    home_prob, draw_prob, away_prob,
                    decision, confidence_level, prediction_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', rows)

            saved = len(rows)
            conn.commit()
        except (Exception, sqlite3.Error) as e:
            self.logger.error(f"批量保存预测失败: {e}")
            conn.rollback()
            saved = 0

        self.logger.info(f"数据库同步完成: {saved} 条预测")
        return saved

    # ══════════════════════════════════════════════════
    # 一站式管道
    # ══════════════════════════════════════════════════

    def run_pipeline(
        self, date_from: str = None, date_to: str = None,
        league_id: int = None, save_db: bool = True,
        output_path: str = None
    ) -> Dict:
        """
        一站式预测管道：
        加载数据 → 预测 → 导出CSV → 写入数据库

        Returns:
            {'csv_path': str, 'db_saved': int, 'n_predictions': int}
        """
        self.logger.info("=" * 70)
        self.logger.info("  哨响AI 预测管道 v3.0")
        self.logger.info(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info("=" * 70)

        # 预测
        result_df = self.predict(
            date_from=date_from, date_to=date_to, league_id=league_id
        )

        if len(result_df) == 0:
            return {'csv_path': '', 'db_saved': 0, 'n_predictions': 0}

        # CSV 输出
        csv_path = self.export_csv(result_df, output_path=output_path)

        # 数据库写入
        db_saved = 0
        if save_db:
            db_saved = self.save_to_database(result_df)

        self.logger.info(f"\n预测管道完成!")

        return {
            'csv_path': csv_path,
            'db_saved': db_saved,
            'n_predictions': len(result_df),
        }


# ══════════════════════════════════════════════════
# 命令行入口
# ══════════════════════════════════════════════════

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='哨响AI 预测引擎')
    parser.add_argument('--model', type=str, help='模型文件路径')
    parser.add_argument('--date-from', type=str, help='开始日期 (YYYY-MM-DD)')
    parser.add_argument('--date-to', type=str, help='结束日期 (YYYY-MM-DD)')
    parser.add_argument('--league', type=int, help='联赛ID')
    parser.add_argument('--output', type=str, help='自定义CSV输出路径')
    parser.add_argument('--no-db', action='store_true', help='不同步数据库')
    args = parser.parse_args()

    engine = PredictionEngine(model_path=args.model)

    result = engine.run_pipeline(
        date_from=args.date_from,
        date_to=args.date_to,
        league_id=args.league,
        save_db=not args.no_db,
        output_path=args.output,
    )

    print(f"\n结果: CSV={result['csv_path']}, DB保存={result['db_saved']}条, "
          f"预测数={result['n_predictions']}")

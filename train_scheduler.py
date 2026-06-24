#!/usr/bin/env python3
"""
哨响AI - 模型训练调度器 v1.0
=============================
从Football项目迁移并适配哨响AI的训练调度器。

功能:
  1. 定时从数据库读取已标注数据进行模型训练
  2. 适配哨响AI的 LinearRegressionTrainer（替换 FootballPredictorAgent）
  3. 训练结果记录到 model_training 表
  4. 支持命令行手动触发和守护进程模式

用法:
  python train_scheduler.py              # 单次训练
  python train_scheduler.py --daemon     # 守护进程模式（每天03:00训练）
  python train_scheduler.py --interval 6 # 每6小时训练一次
  python train_scheduler.py --status     # 查看训练状态
"""
import os
import sys
import json
import time
import argparse
import warnings
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

# 确保项目根目录在路径中
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np

warnings.filterwarnings("ignore")

from database.db_manager import get_db, DatabaseManager
from models.linear_regression_trainer import LinearRegressionTrainer, FEATURE_NAMES, OPTIONAL_FEATURES
from config.hardware_config import get_hardware_config, GPUMemoryMonitor

DB_PATH = os.path.join(PROJECT_ROOT, "data", "football_data.db")


def _print_hardware_info():
    """打印硬件配置信息"""
    hw = get_hardware_config()
    summary = hw.summary()
    gpu = summary['gpu']
    if gpu['available']:
        gpu_str = f"GPU: {gpu['name']} ({gpu['memory_mb']:.0f}MB)"
    else:
        gpu_str = "GPU: 不可用"
    print(f"  {gpu_str}")
    print(f"  CPU: {summary['cpu']['logical_cores']} 核心")
    print(f"  内存: {summary['ram']['total_mb']:.0f}MB 总量 / {summary['ram']['available_mb']:.0f}MB 可用")
    print(f"  训练后端: {summary['optimization']['training_backend']}")
    print(f"  批处理大小: {summary['optimization']['batch_size']}")
    print(f"  设备: {summary['device'].upper()}")


class TrainingScheduler:
    """
    训练调度器（适配哨响AI）

    核心流程:
      1. 从数据库提取已标注数据（finished + 有特征值）
      2. 使用哨响AI的 LinearRegressionTrainer 训练
      3. 保存模型到 saved_models/
      4. 记录训练结果到 model_training 表
    """

    def __init__(self, db_path: str = None):
        self.db = DatabaseManager(db_path or DB_PATH)
        self.trainer: Optional[LinearRegressionTrainer] = None
        self.last_train_time: Optional[datetime] = None
        self.train_count = 0
        self.is_training = False

    def prepare_training_data(self) -> tuple:
        """
        从数据库准备训练数据

        查询逻辑: 找到所有已结束且有特征的比赛
        自动检测可选特征列
        净胜球作为标签
        """
        # 检测可用的特征列
        available_features = list(FEATURE_NAMES)
        with self.db.get_connection() as conn:
            existing_cols = {r[1] for r in conn.execute(
                'PRAGMA table_info(match_features)').fetchall()}
            for feat in OPTIONAL_FEATURES:
                if feat in existing_cols:
                    available_features.append(feat)

            cols_sql = ", ".join([f"f.{f}" for f in available_features])
            rows = conn.execute(f'''
                SELECT m.match_id, m.home_score, m.away_score, m.home_team_name, m.away_team_name,
                       {cols_sql}
                FROM matches m
                JOIN match_features f ON m.match_id = f.match_id
                WHERE m.status = 'finished' AND m.home_score IS NOT NULL
                ORDER BY m.match_date DESC
            ''').fetchall()

        if not rows or len(rows) < 10:
            print(f"⚠️  已标注数据不足 ({len(rows) if rows else 0} 条)，需要至少10条")
            return None, None, [], 0

        print(f"📊 从数据库获取 {len(rows)} 条已标注比赛记录")
        print(f"   特征列: {available_features}")

        X_data = []
        y_data = []
        default_values = {
            'a1': 0.0, 'a2': 0.5, 'a3': 0.5, 'sigma_trap': 0.0,
            'lambda_crush': 1.0, 'epsilon_senti': 0.5,
            'rank_diff_factor': 0.0, 'form_momentum': 0.0, 'h2h_factor': 0.0,
            'rank_factor': 0.5, 'form_factor': 0.5,
        }

        for row in rows:
            feats = [row[f] if row[f] is not None else default_values.get(f, 0.0) for f in available_features]
            X_data.append(feats)
            y_data.append(row['home_score'] - row['away_score'])

        import pandas as pd
        X = pd.DataFrame(X_data, columns=available_features)
        y = pd.Series(y_data, name='goal_diff')

        # 标签分布统计
        home_wins = sum(1 for gd in y_data if gd > 0)
        draws = sum(1 for gd in y_data if gd == 0)
        away_wins = sum(1 for gd in y_data if gd < 0)

        print(f"✅ 特征工程完成: {X.shape[1]} 个特征, {X.shape[0]} 条样本")
        print(f"   标签分布: 主胜={home_wins}, 平局={draws}, 客胜={away_wins}")

        return X, y, available_features, len(rows)

    def run_training(self) -> Dict:
        """执行单次训练"""
        if self.is_training:
            print("⚠️  训练正在进行中，跳过本次调度")
            return {"status": "skipped", "reason": "training_in_progress"}

        self.is_training = True
        t_start = time.time()
        task_id = None

        try:
            print("\n" + "=" * 60)
            print(f"🤖 哨响AI模型训练 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print("=" * 60)

            # 显示硬件信息
            print("  [硬件配置]")
            _print_hardware_info()
            print("-" * 60)

            # 记录任务日志
            task_id = self.db.log_task_start("model_training_scheduled", "MODEL_TRAINING")

            # 准备数据
            X, y, feat_names, sample_count = self.prepare_training_data()
            if X is None or len(X) < 10:
                self.is_training = False
                if task_id:
                    self.db.log_task_end(task_id, "FAILED", 0, "insufficient_data")
                return {"status": "insufficient_data", "sample_count": sample_count}

            # 创建训练器并训练（自动检测 GPU）。优先使用 CLI --model 指定的类型
            if self.trainer is None:
                self.trainer = LinearRegressionTrainer(model_type='ridge', alpha=1.0)
            results = self.trainer.train(X, y)

            # 保存模型
            model_path = self.trainer.save_model()

            duration = time.time() - t_start

            # GPU 显存监控
            gpu_monitor = GPUMemoryMonitor()
            gpu_usage = gpu_monitor.get_memory_usage()
            if gpu_usage:
                print(f"   显存使用: {gpu_usage['allocated_mb']:.0f}MB / {gpu_usage['total_mb']:.0f}MB "
                      f"(峰值 {gpu_usage['peak_mb']:.0f}MB)")

            # 保存训练记录（统一 test_score 语义：Ridge/Linear/Lasso 为 R²，XGBoost 为准确率）
            is_xgboost = args.model == 'xgboost'
            self.db.save_training_record({
                'training_date': datetime.now().strftime('%Y-%m-%d'),
                'model_name': f'linear_regression_{args.model}_scheduled',
                'algorithm': args.model,
                'training_samples': results['training_samples'],
                'test_samples': results['test_samples'],
                'feature_count': results['feature_count'],
                'training_score': results['train_r2'],
                'test_score': results['test_r2'],  # Ridge: R²; XGBoost: accuracy
                'mse': results['test_mse'],
                'mae': results['test_mae'],
                'metric_name': 'accuracy' if is_xgboost else 'r2',
                'feature_importance': results.get('feature_importance', {}),
                'model_path': model_path,
            })

            if task_id:
                self.db.log_task_end(task_id, "SUCCESS", sample_count)

            self.train_count += 1
            self.last_train_time = datetime.now()

            result = {
                "status": "completed",
                "train_id": task_id,
                "sample_count": sample_count,
                "feature_count": len(feat_names),
                "train_r2": round(results['train_r2'], 4),
                "test_r2": round(results['test_r2'], 4),
                "test_mse": round(results['test_mse'], 4),
                "test_mae": round(results['test_mae'], 4),
                "cv_mean": results.get('cv_mean', 0),
                "feature_importance": results.get('feature_importance', {}),
                "duration_seconds": round(duration, 2),
                "backend": results.get('backend', 'sklearn'),
                "duration_ms": results.get('train_duration_ms', round(duration * 1000, 0)),
                "model_path": model_path,
                "gpu_memory_mb": gpu_usage['peak_mb'] if gpu_usage else 0,
                "timestamp": datetime.now().isoformat(),
            }

            print(f"\n✅ 训练完成: {duration:.1f}秒")
            print(f"   训练R²: {result['train_r2']}")
            print(f"   测试R²: {result['test_r2']}")
            print(f"   特征重要性: {json.dumps(result['feature_importance'], ensure_ascii=False)}")
            print(f"   模型路径: {model_path}")

            # ── 自动触发 E1-E7 评估管道 ──
            self._trigger_evaluation()

            return result

        except (Exception, ValueError, KeyError, IndexError, json.JSONDecodeError) as e:
            duration = time.time() - t_start
            print(f"❌ 训练失败: {e}")
            import traceback
            traceback.print_exc()

            if task_id:
                self.db.log_task_end(task_id, "FAILED", 0, str(e))

            return {"status": "failed", "error": str(e), "duration_seconds": round(duration, 2)}

        finally:
            self.is_training = False

    def _trigger_evaluation(self):
        """训练/回测后自动运行E1-E7七维评估管道"""
        try:
            from agents.evaluator.evaluation_pipeline import EvaluationPipeline
            pipeline = EvaluationPipeline(db_path=DB_PATH)
            eval_result = pipeline.run(trigger_type="auto", skip_if_unchanged=False)
            score = eval_result.get('overall_score', 0)
            rating = eval_result.get('overall_rating', '?')
            urgency = eval_result.get('urgency', 'none')
            print(f"\n📊 E1-E7全链路评估完成: {score}/100 {rating} | 紧迫度: {urgency}")
            if eval_result.get('action_items'):
                print("   待处理问题:")
                for item in eval_result['action_items'][:5]:
                    print(f"     ⚠️ {item}")
        except (Exception, ValueError, KeyError, IndexError, requests.exceptions.RequestException) as e:
            print(f"   ⚠️ 评估管道执行异常(非致命): {e}")

    def start_daemon(self, interval_hours: int = 24, run_immediately: bool = True):
        """
        启动守护进程模式

        Args:
            interval_hours: 训练间隔（小时）
            run_immediately: 是否立即执行一次
        """
        import schedule as scheduler_lib

        print(f"\n🕐 哨响AI训练调度器守护进程启动")
        print(f"   训练间隔: 每 {interval_hours} 小时")
        print(f"   立即执行: {'是' if run_immediately else '否'}")
        print(f"   数据库: {self.db.db_path}")
        print("  [硬件配置]")
        _print_hardware_info()
        print("-" * 60)

        def _train_job():
            result = self.run_training()
            status = result.get("status", "unknown")
            samples = result.get("sample_count", 0)
            print(f"   [调度] 训练{status}: {samples}条样本, "
                  f"测试R²={result.get('test_r2', 'N/A')}")

        if run_immediately:
            _train_job()

        if interval_hours >= 24:
            scheduler_lib.every().day.at("03:00").do(_train_job)
            print("   📅 已安排在每天 03:00 训练")
        else:
            scheduler_lib.every(interval_hours).hours.do(_train_job)
            print(f"   📅 已安排每 {interval_hours} 小时训练")

        try:
            while True:
                scheduler_lib.run_pending()
                time.sleep(60)
        except KeyboardInterrupt:
            print("\n⏹️  调度器已停止")
            print(f"   共执行 {self.train_count} 次训练")
            print(f"   最后训练: {self.last_train_time}")

    def get_status(self) -> Dict:
        """获取调度器状态"""
        try:
            history = self.db.get_training_records(limit=5)
        except (Exception, ValueError):
            history = []

        return {
            "train_count": self.train_count,
            "last_train_time": self.last_train_time.isoformat() if self.last_train_time else None,
            "is_training": self.is_training,
            "db_path": self.db.db_path,
            "recent_trainings": history,
        }


# ═══════════════════════════════════════════════════════════════════════
#  CLI入口
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="哨响AI模型训练调度器 v1.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python train_scheduler.py                  # 单次训练
  python train_scheduler.py --daemon         # 守护进程模式 (每天训练)
  python train_scheduler.py --interval 6     # 每6小时训练一次
  python train_scheduler.py --status         # 查看训练状态
  python train_scheduler.py --model lasso    # 使用Lasso算法训练
        """,
    )
    parser.add_argument("--daemon", action="store_true", help="守护进程模式")
    parser.add_argument("--interval", type=int, default=24, help="训练间隔(小时)，默认24")
    parser.add_argument("--status", action="store_true", help="查看训练状态")
    parser.add_argument("--db", type=str, default=DB_PATH, help="数据库路径")
    parser.add_argument("--model", type=str, default="ridge",
                        choices=["ridge", "linear", "lasso", "xgboost"], help="模型类型")

    args = parser.parse_args()

    scheduler = TrainingScheduler(db_path=args.db)

    if args.status:
        status = scheduler.get_status()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return

    if args.model != "ridge":
        scheduler.trainer = LinearRegressionTrainer(model_type=args.model)

    if args.daemon:
        scheduler.start_daemon(interval_hours=args.interval)
    else:
        result = scheduler.run_training()
        print("\n" + "=" * 60)
        print("📊 训练结果汇总:")
        print("=" * 60)
        print(json.dumps(result, ensure_ascii=False, indent=2))

        if result.get("status") == "completed":
            print("\n✅ 训练成功！模型已更新。")
            print("💡 使用 --daemon 参数可启动定时训练。")
        else:
            print(f"\n⚠️  训练未完成: {result.get('status', 'unknown')}")


if __name__ == "__main__":
    main()

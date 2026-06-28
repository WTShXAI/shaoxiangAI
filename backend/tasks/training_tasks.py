"""
训练任务 — Celery 异步训练
"""
import sys
import os
import logging
from datetime import datetime, timezone
from tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

@celery_app.task(bind=True, name="train_model_task")
def train_model_task(
    self,
    data_source: str = "latest",
    n_estimators: int = 1000,
    force_retrain: bool = False,
    description: str = "",
):
    """
    异步模型训练（Celery Worker 执行）

    集成 MLflow 实验追踪（如果可用）
    """
    self.update_state(state="STARTED", meta={"progress": 0.0})

    try:
        # ── MLflow 追踪（可选）─────────────────
        try:
            import mlflow
            from core.config import settings
            mlflow.set_tracking_uri(settings.MLFLOW_TRACKING_URI)
            mlflow.set_experiment(settings.MLFLOW_EXPERIMENT_NAME)

            with mlflow.start_run(run_name=f"train_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}"):
                mlflow.log_param("data_source", data_source)
                mlflow.log_param("n_estimators", n_estimators)
        except ImportError as e:
            logger.debug(f"MLflow 不可用: {e}")
            mlflow = None
        except (ValueError, TypeError) as e:
            logger.warning(f"MLflow 初始化失败: {e}")
            mlflow = None

        # ── 执行训练 ───────────────────────────
        from core.config import settings as s

        self.update_state(state="RUNNING", meta={"progress": 0.3, "message": "训练中..."})

        from training.training_pipeline import TrainingPipeline
        from optimize.model_registry import ModelRegistry

        registry = ModelRegistry()
        pipeline = TrainingPipeline(registry)
        result = pipeline.run_training_pipeline(
            data_source=data_source,
            n_estimators=n_estimators,
            description=description or f"Celery auto-train {datetime.now(timezone.utc):%Y-%m-%d %H:%M}",
        )

        # ── 记录 MLflow 指标 ──────────────────
        if mlflow:
            metrics = result.get("metrics", {})
            for k, v in metrics.items():
                if isinstance(v, (int, float)):
                    mlflow.log_metric(k, v)

        return {
            "status": "completed",
            "version": result.get("version"),
            "model_hash": result.get("model_hash"),
            "metrics": result.get("metrics"),
        }

    except (ValueError, TypeError) as e:
        logger.error(f"训练失败: {e}", exc_info=True)
        self.update_state(state="FAILURE", meta={"error": str(e)})
        raise

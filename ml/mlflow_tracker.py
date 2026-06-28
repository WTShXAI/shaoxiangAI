"""
MLflow 实验追踪服务 — 封装 MLflow Python API
=============================================
用途:
    - 训练实验记录（参数 + 指标 + 模型）
    - 模型版本注册（对齐 MLflow Model Registry）
    - 实验对比（不同训练运行的指标对比）

使用:
    tracker = MLflowTracker()
    with tracker.start_run("train_v3"):
        tracker.log_params({"n_estimators": 1000})
        # ... 训练 ...
        tracker.log_metrics({"accuracy": 0.45})
        tracker.log_model(model, "football_ensemble")
"""
import os
import sys
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from contextlib import contextmanager

logger = logging.getLogger(__name__)

class MLflowTracker:
    """MLflow 实验追踪器"""

    def __init__(self, tracking_uri: str = None, experiment_name: str = None):
        import mlflow
        self._mlflow = mlflow

        from core.config import settings
        self.tracking_uri = tracking_uri or settings.MLFLOW_TRACKING_URI
        self.experiment_name = experiment_name or settings.MLFLOW_EXPERIMENT_NAME

        self._mlflow.set_tracking_uri(self.tracking_uri)
        self._experiment = self._get_or_create_experiment()

    def _get_or_create_experiment(self):
        """获取或创建实验"""
        try:
            exp = self._mlflow.get_experiment_by_name(self.experiment_name)
            if exp is None:
                exp_id = self._mlflow.create_experiment(self.experiment_name)
                logger.info(f"创建 MLflow 实验: {self.experiment_name} (id={exp_id})")
                exp = self._mlflow.get_experiment(exp_id)
            return exp
        except (Exception) as e:
            logger.warning(f"MLflow 实验初始化失败: {e}")
            return None

    @contextmanager
    def start_run(self, run_name: str = None):
        """上下文管理器 — 自动开始/结束 run"""
        if run_name is None:
            run_name = f"train_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        try:
            self._mlflow.start_run(
                experiment_id=self._experiment.experiment_id if self._experiment else None,
                run_name=run_name,
            )
            logger.info(f"MLflow Run 开始: {run_name}")
            yield self
        except (Exception) as e:
            logger.error(f"MLflow Run 错误: {e}")
            raise
        finally:
            try:
                self._mlflow.end_run()
            except (Exception):
                pass

    def log_params(self, params: Dict[str, Any]):
        """记录超参数"""
        try:
            self._mlflow.log_params(params)
        except (Exception, KeyError, IndexError) as e:
            logger.debug(f"MLflow log_params 失败: {e}")

    def log_metrics(self, metrics: Dict[str, float], step: int = None):
        """记录指标"""
        try:
            self._mlflow.log_metrics(metrics, step=step)
        except (Exception, KeyError, IndexError) as e:
            logger.debug(f"MLflow log_metrics 失败: {e}")

    def log_model(self, model, artifact_path: str = "model"):
        """记录模型"""
        try:
            self._mlflow.sklearn.log_model(model, artifact_path)
        except (Exception) as e:
            logger.debug(f"MLflow log_model 失败: {e}")

    def log_artifact(self, path: str):
        """记录文件"""
        try:
            self._mlflow.log_artifact(path)
        except (Exception) as e:
            logger.debug(f"MLflow log_artifact 失败: {e}")

    def register_model(self, model_name: str = "football_ensemble"):
        """注册模型到 MLflow Registry"""
        try:
            run_id = self._mlflow.active_run().info.run_id
            result = self._mlflow.register_model(
                f"runs:/{run_id}/model",
                model_name,
            )
            logger.info(f"模型已注册: {model_name} v{result.version}")
            return result
        except (Exception) as e:
            logger.warning(f"MLflow 模型注册失败: {e}")
            return None

    def list_runs(self, max_results: int = 20) -> List[Dict]:
        """列出最近的训练运行"""
        try:
            if not self._experiment:
                return []
            runs = self._mlflow.search_runs(
                experiment_ids=[self._experiment.experiment_id],
                max_results=max_results,
                order_by=["start_time DESC"],
            )
            return [
                {
                    "run_id": r.info.run_id,
                    "run_name": r.data.tags.get("mlflow.runName", ""),
                    "start_time": datetime.fromtimestamp(r.info.start_time / 1000).isoformat()
                        if r.info.start_time else None,
                    "metrics": r.data.metrics,
                    "params": r.data.params,
                }
                for r in runs
            ]
        except (Exception, requests.exceptions.RequestException) as e:
            logger.warning(f"MLflow 查询失败: {e}")
            return []

    def compare_runs(self, run_id_a: str, run_id_b: str) -> Dict:
        """对比两个 run"""
        try:
            r1 = self._mlflow.get_run(run_id_a)
            r2 = self._mlflow.get_run(run_id_b)
            return {
                "run_a": {"metrics": r1.data.metrics, "params": r1.data.params},
                "run_b": {"metrics": r2.data.metrics, "params": r2.data.params},
                "diff": {
                    k: round(float(r2.data.metrics.get(k, 0)) - float(r1.data.metrics.get(k, 0)), 4)
                    for k in set(r1.data.metrics) | set(r2.data.metrics)
                },
            }
        except (Exception, ValueError, requests.exceptions.RequestException) as e:
            return {"error": str(e)}

    def load_model(self, model_name: str = "football_ensemble", version: str = "latest"):
        """从 MLflow Registry 加载模型"""
        try:
            if version == "latest":
                client = self._mlflow.tracking.MlflowClient()
                versions = client.get_latest_versions(model_name, stages=["Production"])
                if not versions:
                    versions = client.get_latest_versions(model_name)
                if versions:
                    version = versions[0].version

            model_uri = f"models:/{model_name}/{version}"
            return self._mlflow.sklearn.load_model(model_uri)
        except (Exception, KeyError, IndexError) as e:
            logger.warning(f"MLflow 加载模型失败: {e}")
            return None

# ── 全局单例 ──────────────────────────────
_tracker: Optional[MLflowTracker] = None

def get_mlflow_tracker() -> MLflowTracker:
    """获取 MLflow 追踪器单例"""
    global _tracker
    if _tracker is None:
        _tracker = MLflowTracker()
    return _tracker

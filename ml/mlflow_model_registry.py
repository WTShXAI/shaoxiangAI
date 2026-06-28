"""
MLflow 模型注册表集成 — 在 MLflow 和本地 ModelRegistry 间同步
"""
import logging
from typing import Optional, Dict, List
from mlflow_tracker import MLflowTracker

logger = logging.getLogger(__name__)

class MLflowModelRegistry:
    """同步 MLflow Registry 与本地 ModelRegistry"""

    def __init__(self, mlflow_tracker: MLflowTracker = None):
        self._tracker = mlflow_tracker or MLflowTracker()
        try:
            from mlflow.tracking import MlflowClient
            self._client = MlflowClient()
        except ImportError:
            self._client = None

    def sync_from_mlflow(self, model_name: str = "football_ensemble") -> List[Dict]:
        """从 MLflow 同步模型到本地注册表"""
        if self._client is None:
            return []

        try:
            versions = self._client.search_model_versions(f"name='{model_name}'")
            from optimize.model_registry import ModelRegistry
            local_registry = ModelRegistry()

            synced = []
            for v in versions:
                run = self._client.get_run(v.run_id)
                metrics = run.data.metrics
                local_registry.register(
                    model_or_path=f"models:/{model_name}/{v.version}",
                    semver=v.version,
                    model_type="ensemble",
                    source="mlflow",
                    description=v.description or "",
                    metrics={
                        "accuracy": metrics.get("accuracy", 0) * 100,
                        "draw_f1": metrics.get("draw_f1", 0) * 100,
                        "log_loss": metrics.get("log_loss", 0),
                        "brier": metrics.get("brier", 0),
                    },
                )
                synced.append({"version": v.version, "stage": v.current_stage})

            logger.info(f"从 MLflow 同步 {len(synced)} 个模型版本")
            return synced
        except (Exception, requests.exceptions.RequestException) as e:
            logger.warning(f"MLflow 同步失败: {e}")
            return []

    def get_model_uri(self, model_name: str = "football_ensemble", version: str = "latest") -> Optional[str]:
        """获取模型 URI"""
        if self._client is None:
            return None
        try:
            if version == "latest":
                versions = self._client.get_latest_versions(model_name, stages=["Production"])
                if not versions:
                    versions = self._client.get_latest_versions(model_name)
                if versions:
                    version = versions[0].version
            return f"models:/{model_name}/{version}"
        except (Exception, KeyError, IndexError):
            return None

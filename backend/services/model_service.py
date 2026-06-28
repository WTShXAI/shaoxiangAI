"""
模型管理服务 — 封装 ModelRegistry + 版本对比/回滚
"""
import sys
import os
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

class ModelService:
    """模型版本管理服务"""

    def __init__(self):
        self._registry = None

    @property
    def registry(self):
        """延迟加载 ModelRegistry"""
        if self._registry is None:
            from core.config import settings

            from optimize.model_registry import ModelRegistry
            self._registry = ModelRegistry()
        return self._registry

    def list_models(
        self,
        status: Optional[str] = None,
        model_type: Optional[str] = None,
        limit: int = 20,
    ) -> Dict:
        """列出模型版本"""
        models = []
        all_models = self.registry._data.get("models", {})

        for mid, m in all_models.items():
            if status and m.get("status") != status:
                continue
            if model_type and m.get("model_type") != model_type:
                continue
            models.append(self._format_model(mid, m))

        # 按注册时间降序
        models.sort(key=lambda x: x.get("registered_at", ""), reverse=True)
        models = models[:limit]

        return {
            "models": models,
            "total": len(all_models),
            "current_production": self.registry._data.get("current_production"),
        }

    def get_model(self, model_id: str) -> Optional[Dict]:
        """获取模型详情"""
        m = self.registry.get(model_id)
        if not m:
            return None
        return self._format_model(model_id, m)

    def deploy(self, model_id: str) -> bool:
        """部署模型"""
        return self.registry.deploy(model_id)

    def rollback(self, target_version: Optional[str] = None) -> Optional[str]:
        """回滚模型"""
        if target_version:
            return self.registry.rollback(target_version)
        return self.registry.rollback()

    def compare_versions(self, model_id_a: str, model_id_b: str) -> Dict:
        """对比版本"""
        return self.registry.compare_versions(model_id_a, model_id_b)

    def register(
        self,
        model_path: str,
        semver: Optional[str] = None,
        model_type: str = "ensemble",
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> str:
        """注册新模型"""
        return self.registry.register(
            model_path=model_path,
            metrics={},
            model_type=model_type,
            tags=tags or [],
            semver=semver,
            description=description or "",
        )

    def get_best(self, metric: str = "accuracy") -> Optional[Dict]:
        """获取最优模型"""
        best = self.registry.get_best_by_metric(metric)
        if not best:
            return None
        return self._format_model(best["model_id"], best)

    def get_current_info(self) -> Optional[Dict]:
        """获取当前生产模型信息"""
        prod = self.registry.get_production_version()
        if not prod:
            # 降级：返回最新活跃模型
            active = self.registry.list_models(status="active", limit=1)
            if active:
                return self._format_model(active[0]["model_id"], active[0])
            return None
        return self._format_model(prod["model_id"], prod)

    def get_current_model_path(self) -> Optional[str]:
        """获取当前生产模型路径"""
        prod = self.registry.get_production_version()
        if prod:
            return prod.get("model_path")
        return None

    def auto_promote(self, min_gain: float = 0.5) -> Optional[str]:
        """自动晋升"""
        return self.registry.auto_promote(min_accuracy_gain=min_gain)

    def get_training_history(self, limit: int = 20) -> List[Dict]:
        """获取训练历史（从注册表）"""
        return self.registry.get_improvement_history()[:limit]

    def _format_model(self, model_id: str, m: Dict) -> Dict:
        """格式化模型信息"""
        metrics = m.get("metrics", {})
        return {
            "model_id": model_id,
            "model_type": m.get("model_type", "unknown"),
            "version": m.get("semver"),
            "model_hash": m.get("model_hash"),
            "status": m.get("status", "active"),
            "metrics": {
                "accuracy": metrics.get("accuracy", 0),
                "draw_f1": metrics.get("draw_f1", 0),
                "draw_recall": metrics.get("draw_recall", 0),
                "home_recall": metrics.get("home_recall", 0),
                "away_recall": metrics.get("away_recall", 0),
                "brier": metrics.get("brier", 0),
                "log_loss": metrics.get("log_loss", 0),
                "ece": metrics.get("ece", 0),
                "mcc": metrics.get("mcc", 0),
                "test_samples": metrics.get("test_samples", 0),
            },
            "registered_at": m.get("registered_at"),
            "is_production": m.get("status") == "production",
            "description": m.get("description"),
        }

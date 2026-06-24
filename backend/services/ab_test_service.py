"""
A/B测试服务 — 多模型版本并行评测
=================================
支持将流量分流到不同模型版本，实时对比效果。
"""
import sys
import os
import json
import time
import hashlib
import logging
from typing import Optional, Dict, List, Any, Tuple
from datetime import datetime
from collections import defaultdict

logger = logging.getLogger(__name__)


class ABTestConfig:
    """A/B测试配置"""
    def __init__(
        self,
        name: str,
        variants: Dict[str, str],      # {variant_name: model_id}
        traffic_split: Dict[str, float],  # {variant_name: ratio} — 总和应=1.0
        metrics: List[str] = None,
        min_sample_size: int = 100,
        confidence_level: float = 0.95,
        duration_hours: int = 168,         # 7天
    ):
        self.name = name
        self.variants = variants
        self.traffic_split = traffic_split
        self.metrics = metrics or ["accuracy", "log_loss", "brier"]
        self.min_sample_size = min_sample_size
        self.confidence_level = confidence_level
        self.duration_hours = duration_hours
        self.created_at = datetime.now().isoformat()
        self.status = "active"


class ABTestService:
    """A/B测试服务"""

    def __init__(self):
        self._active_tests: Dict[str, ABTestConfig] = {}
        self._results: Dict[str, Dict] = defaultdict(lambda: defaultdict(list))
        self._storage_path = None
        self._load_state()

    @property
    def storage_path(self) -> str:
        if self._storage_path is None:
            from core.config import settings
            self._storage_path = os.path.join(
                settings.PROJECT_ROOT, "data", "ab_test_state.json"
            )
        return self._storage_path

    def _load_state(self):
        """加载持久化状态"""
        try:
            if os.path.exists(self.storage_path):
                with open(self.storage_path, "r") as f:
                    state = json.load(f)
                    for name, cfg in state.get("tests", {}).items():
                        self._active_tests[name] = ABTestConfig(**cfg)
                    for test_name, variants in state.get("results", {}).items():
                        self._results[test_name] = defaultdict(list, variants)
        except (OSError, ValueError, KeyError) as e:
            logger.debug(f"操作失败: {e}")

    def _save_state(self):
        """持久化状态"""
        try:
            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            state = {
                "tests": {
                    name: {
                        "name": t.name,
                        "variants": t.variants,
                        "traffic_split": t.traffic_split,
                        "metrics": t.metrics,
                        "min_sample_size": t.min_sample_size,
                        "confidence_level": t.confidence_level,
                        "duration_hours": t.duration_hours,
                        "created_at": t.created_at,
                        "status": t.status,
                    }
                    for name, t in self._active_tests.items()
                },
                "results": {k: dict(v) for k, v in self._results.items()},
            }
            with open(self.storage_path, "w") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except (FileNotFoundError, IOError, OSError, PermissionError, ValueError, KeyError, TypeError, AttributeError) as e:
            logger.warning(f"保存A/B测试状态失败: {e}")

    def create_test(
        self,
        name: str,
        variants: Dict[str, str],
        traffic_split: Optional[Dict[str, float]] = None,
        metrics: List[str] = None,
    ) -> ABTestConfig:
        """创建A/B测试"""
        if traffic_split is None:
            n = len(variants)
            traffic_split = {v: 1.0 / n for v in variants}

        cfg = ABTestConfig(
            name=name,
            variants=variants,
            traffic_split=traffic_split,
            metrics=metrics,
        )
        self._active_tests[name] = cfg
        self._save_state()
        logger.info(f"A/B测试已创建: {name}, 变体={list(variants.keys())}")
        return cfg

    def get_variant(self, test_name: str, user_id: str) -> Optional[str]:
        """
        根据用户ID确定性分配变体（同一用户始终看到同一变体）
        """
        if test_name not in self._active_tests:
            return None
        cfg = self._active_tests[test_name]
        if cfg.status != "active":
            return None

        # 哈希分桶
        hash_val = int(hashlib.md5(f"{test_name}:{user_id}".encode()).hexdigest(), 16) % 10000
        cumulative = 0.0
        for variant, ratio in cfg.traffic_split.items():
            cumulative += ratio * 10000
            if hash_val < cumulative:
                return variant

        return list(cfg.variants.keys())[0]

    def record_result(
        self,
        test_name: str,
        variant: str,
        is_correct: bool,
        confidence: float,
    ):
        """记录一次预测结果"""
        if test_name not in self._active_tests:
            return
        self._results[test_name][variant].append({
            "is_correct": is_correct,
            "confidence": confidence,
            "timestamp": datetime.now().isoformat(),
        })
        # 每100条保存一次
        total = sum(len(v) for v in self._results[test_name].values())
        if total % 100 == 0:
            self._save_state()

    def get_results(self, test_name: str) -> Dict:
        """获取A/B测试结果"""
        if test_name not in self._active_tests:
            return {"error": "测试不存在"}

        cfg = self._active_tests[test_name]
        results = self._results.get(test_name, {})

        summary = {
            "test_name": test_name,
            "status": cfg.status,
            "created_at": cfg.created_at,
            "variants": {},
            "conclusion": None,
        }

        for variant_name in cfg.variants:
            variant_results = results.get(variant_name, [])
            n = len(variant_results)
            if n == 0:
                summary["variants"][variant_name] = {"samples": 0, "accuracy": 0}
                continue

            correct = sum(1 for r in variant_results if r["is_correct"])
            avg_conf = sum(r["confidence"] for r in variant_results) / n
            summary["variants"][variant_name] = {
                "samples": n,
                "accuracy": round(correct / n * 100, 2),
                "correct": correct,
                "avg_confidence": round(avg_conf, 4),
            }

        # 结论
        variants = summary["variants"]
        if all(v["samples"] >= cfg.min_sample_size for v in variants.values()):
            best = max(variants, key=lambda k: variants[k]["accuracy"])
            worst = min(variants, key=lambda k: variants[k]["accuracy"])
            diff = variants[best]["accuracy"] - variants[worst]["accuracy"]
            if diff > 2:
                summary["conclusion"] = f"✅ {best} 显著优于 {worst} (+{diff:.1f}pp)"
            else:
                summary["conclusion"] = "➖ 变体间差异不显著"
        else:
            summary["conclusion"] = "⏳ 样本量不足，继续收集"

        return summary

    def stop_test(self, test_name: str) -> Dict:
        """停止A/B测试"""
        if test_name not in self._active_tests:
            return {"error": "测试不存在"}
        self._active_tests[test_name].status = "stopped"
        self._save_state()

        final = self.get_results(test_name)
        self._active_tests.pop(test_name, None)
        logger.info(f"A/B测试已停止: {test_name}")
        return final

    def list_tests(self) -> List[Dict]:
        """列出所有测试"""
        return [
            {
                "name": t.name,
                "status": t.status,
                "variants": list(t.variants.keys()),
                "created_at": t.created_at,
                "samples": sum(
                    len(self._results.get(t.name, {}).get(v, []))
                    for v in t.variants
                ),
            }
            for t in self._active_tests.values()
        ]


# 全局单例
_ab_test_service: Optional[ABTestService] = None


def get_ab_test_service() -> ABTestService:
    global _ab_test_service
    if _ab_test_service is None:
        _ab_test_service = ABTestService()
    return _ab_test_service

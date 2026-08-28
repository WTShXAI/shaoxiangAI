#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""strategy_registry: 策略注册表 (Phase B).

将散布在各个模块中的策略逻辑统一管理:
  - StrategyMetadata — 策略元数据 (名称/版本/适用市场/权重/开关)
  - @register_strategy — 装饰器式注册
  - StrategyRegistry — 注册/发现/过滤/排序

注册表可独立运作.

用法:
    @register_strategy(
        name="分歧闸门·价值层",
        asset_class=["1X2"],
        edge_type="价差",
        weight=1.0,
    )
    class MyStrategy(BaseStrategy):
        ...
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

logger = logging.getLogger(__name__)


# ── 元数据 ──


@dataclass
class StrategyMetadata:
    """策略元数据."""

    name: str  # 策略名称 (中文)
    version: str = "1.0.0"  # 版本
    asset_class: List[str] = field(default_factory=lambda: ["1X2"])  # 适用市场
    edge_type: str = "价差"  # 价差/趋势/套利/ML/规则
    enabled: bool = True  # 全局开关
    weight: float = 1.0  # 组合权重 (0~1)
    min_samples: int = 5  # 最小触发样本数
    max_stake_frac: float = 0.10  # 单注封顶

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "asset_class": list(self.asset_class),
            "edge_type": self.edge_type,
            "enabled": self.enabled,
            "weight": self.weight,
            "min_samples": self.min_samples,
            "max_stake_frac": self.max_stake_frac,
        }


# ── 注册表 ──


class StrategyRegistry:
    """策略注册表 (单例)."""

    _instance: Optional["StrategyRegistry"] = None
    _strategies: Dict[str, Type] = {}
    _metadata: Dict[str, StrategyMetadata] = {}
    _instances: Dict[str, Any] = {}

    def __new__(cls) -> "StrategyRegistry":
        if cls._instance is None:
            inst = super().__new__(cls)
            inst._strategies = {}
            inst._metadata = {}
            inst._instances = {}
            cls._instance = inst
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """重置注册表 (仅测试用)."""
        cls._instance = None
        cls._strategies = {}
        cls._metadata = {}
        cls._instances = {}

    def register(
        self,
        strategy_cls: Type,
        metadata: StrategyMetadata,
    ) -> Type:
        """注册一个策略类."""
        sid = strategy_cls.__name__
        if sid in self._strategies:
            logger.warning(f"策略已注册, 覆盖: {sid}")
        self._strategies[sid] = strategy_cls
        self._metadata[sid] = metadata
        logger.info(f"策略已注册: {sid} ({metadata.name})")
        return strategy_cls

    def get(self, strategy_id: str) -> Optional[Any]:
        """获取策略实例 (惰性初始化)."""
        if strategy_id in self._instances:
            return self._instances[strategy_id]
        cls = self._strategies.get(strategy_id)
        if cls is None:
            return None
        inst = cls()
        self._instances[strategy_id] = inst
        return inst

    def get_metadata(self, strategy_id: str) -> Optional[StrategyMetadata]:
        return self._metadata.get(strategy_id)

    @property
    def all_ids(self) -> List[str]:
        return list(self._strategies.keys())

    @property
    def all_metadata(self) -> List[StrategyMetadata]:
        return list(self._metadata.values())

    def list_enabled(self) -> List[str]:
        """返回所有已启用的策略ID列表."""
        return [
            sid for sid, meta in self._metadata.items()
            if meta.enabled
        ]

    def list_by_edge(self, edge_type: str) -> List[str]:
        """按 edge 类型过滤."""
        return [
            sid for sid, meta in self._metadata.items()
            if meta.edge_type == edge_type
        ]

    def set_enabled(self, strategy_id: str, enabled: bool) -> None:
        if strategy_id in self._metadata:
            self._metadata[strategy_id].enabled = enabled

    def set_weight(self, strategy_id: str, weight: float) -> None:
        if strategy_id in self._metadata:
            self._metadata[strategy_id].weight = max(0.0, min(1.0, weight))

    def summary(self) -> List[Dict[str, Any]]:
        """注册表概览 (供 API/前端)."""
        return [
            {"id": sid, **meta.to_dict()}
            for sid, meta in self._metadata.items()
        ]


# ── 全局注册表实例 ──

_REGISTRY = StrategyRegistry()


# ── 装饰器 ──


def register_strategy(
    name: str = "",
    version: str = "1.0.0",
    asset_class: Optional[List[str]] = None,
    edge_type: str = "规则",
    enabled: bool = True,
    weight: float = 1.0,
    min_samples: int = 5,
    max_stake_frac: float = 0.10,
) -> Callable[[Type], Type]:
    """策略注册装饰器.

    Example:
        @register_strategy(name="我的策略", edge_type="价差")
        class MyStrategy:
            def signal(self, match_data) -> dict: ...
    """
    def decorator(cls: Type) -> Type:
        meta = StrategyMetadata(
            name=name or cls.__name__,
            version=version,
            asset_class=asset_class or ["1X2"],
            edge_type=edge_type,
            enabled=enabled,
            weight=weight,
            min_samples=min_samples,
            max_stake_frac=max_stake_frac,
        )
        _REGISTRY.register(cls, meta)
        return cls
    return decorator


def get_registry() -> StrategyRegistry:
    """获取全局注册表."""
    return _REGISTRY

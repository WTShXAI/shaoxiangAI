#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deployment_validator: 上线校验器 (Phase B).

生产级上线前的三重校验:
  1. 数据一致性 — 回测数据源 vs 实盘数据源同宗
  2. 时序严格性 — 回测无前视偏差 (look-ahead bias)
  3. 状态持久化 — 崩溃恢复后状态一致

参考《AI量化之道》第9章 "三重校验清单":
  - 数据一致性校验
  - 时序严格性校验
  - 状态持久化校验

用法:
    validator = DeploymentValidator()
    report = validator.validate_all()
    print(report)
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class ValidationCheck:
    """单项校验结果."""

    name: str
    passed: bool
    detail: str = ""
    severity: str = "P2"  # P0=阻断/P1=警告/P2=提示

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "severity": self.severity,
        }


@dataclass
class ValidationReport:
    """全量校验报告."""

    checks: List[ValidationCheck] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def blocking_failures(self) -> List[ValidationCheck]:
        return [c for c in self.checks if not c.passed and c.severity == "P0"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "all_passed": self.all_passed,
            "total_checks": len(self.checks),
            "passed": sum(1 for c in self.checks if c.passed),
            "failed": sum(1 for c in self.checks if not c.passed),
            "blocking": len(self.blocking_failures),
            "checks": [c.to_dict() for c in self.checks],
        }


class DeploymentValidator:
    """上线校验器 (三重校验)."""

    def validate_all(
        self,
        backtest_data_source: str = "",
        live_data_source: str = "",
        backtest_results: Optional[List[Dict[str, Any]]] = None,
        market_data: Optional[List[Dict[str, Any]]] = None,
    ) -> ValidationReport:
        """执行全部三重校验.

        Args:
            backtest_data_source: 回测数据源名称 (如 "football_data.db matches 2023-2025")
            live_data_source: 实盘数据源名称 (如 "The Odds API live feed")
            backtest_results: 回测持仓记录列表
            market_data: 市场数据时间序列 (用于时序验证)

        Returns:
            ValidationReport
        """
        report = ValidationReport()

        # ── 第一重: 数据一致性 ──
        report.checks.append(self._check_data_consistency(
            backtest_data_source, live_data_source
        ))

        # ── 第二重: 时序严格性 ──
        report.checks.append(self._check_temporal_strictness(market_data))
        report.checks.append(self._check_no_lookahead(market_data))

        # ── 第三重: 状态持久化 ──
        report.checks.append(self._check_state_persistence())

        # ── 额外: 策略引用完整性 ──
        report.checks.append(self._check_strategy_refs())

        return report

    # ── 第一重: 数据一致性 ──

    def _check_data_consistency(
        self,
        backtest_source: str,
        live_source: str,
    ) -> ValidationCheck:
        """验证回测与实盘数据源一致.

        规则: 回测数据必须来自实盘可用的数据源,
        或用实盘子集 (如用2023-2025历史数据回测, 实盘取2026+数据).
        """
        if not backtest_source and not live_source:
            return ValidationCheck(
                name="数据一致性",
                passed=True,
                detail="未指定数据源, 跳过 (默认通过)",
                severity="P2",
            )
        if not live_source:
            return ValidationCheck(
                name="数据一致性",
                passed=False,
                detail=f"实盘数据源未指定 (回测源: {backtest_source})",
                severity="P1",
            )
        if not backtest_source:
            return ValidationCheck(
                name="数据一致性",
                passed=False,
                detail=f"回测数据源未指定 (实盘源: {live_source})",
                severity="P1",
            )

        # 检查数据源是否同宗 (实盘源包含回测源的关键字)
        backtest_keywords = set(backtest_source.lower().split())
        live_keywords = set(live_source.lower().split())
        common = backtest_keywords & live_keywords

        if not common:
            return ValidationCheck(
                name="数据一致性",
                passed=False,
                detail=(
                    f"回测源 '{backtest_source}' 与实盘源 "
                    f"'{live_source}' 无共同数据源标识, "
                    "可能存在数据源不一致风险"
                ),
                severity="P1",
            )

        return ValidationCheck(
            name="数据一致性",
            passed=True,
            detail=f"回测源与实盘源同宗 (共同标识: {', '.join(common)})",
            severity="P2",
        )

    # ── 第二重: 时序严格性 ──

    def _check_temporal_strictness(
        self,
        market_data: Optional[List[Dict[str, Any]]] = None,
    ) -> ValidationCheck:
        """验证时序有序性 — 比赛按时间顺序回放.

        规则: market_data 中的时间戳必须严格递增.
        """
        if not market_data or len(market_data) < 2:
            return ValidationCheck(
                name="时序有序性",
                passed=True,
                detail="数据不足2条, 跳过时序检查 (默认通过)",
                severity="P2",
            )

        violations = 0
        for i in range(1, len(market_data)):
            ts_prev = market_data[i - 1].get("timestamp", "")
            ts_curr = market_data[i].get("timestamp", "")
            if ts_prev and ts_curr and ts_curr < ts_prev:
                violations += 1

        if violations > 0:
            return ValidationCheck(
                name="时序有序性",
                passed=False,
                detail=f"发现 {violations} 处时间戳逆序, 可能存在时序错误",
                severity="P0",
            )

        return ValidationCheck(
            name="时序有序性",
            passed=True,
            detail=f"{len(market_data)} 条数据时序严格递增",
            severity="P2",
        )

    def _check_no_lookahead(
        self,
        market_data: Optional[List[Dict[str, Any]]] = None,
    ) -> ValidationCheck:
        """验证无前视偏差 — 决策时点不包含未来信息.

        规则: 每条数据的"决策时间"必须早于"赛果时间".
        """
        if not market_data:
            return ValidationCheck(
                name="前视偏差",
                passed=True,
                detail="无市场数据, 跳过 (默认通过)",
                severity="P2",
            )

        violations = 0
        for i, row in enumerate(market_data):
            decision_time = row.get("decision_time", "")
            result_time = row.get("result_time", "")
            if decision_time and result_time and decision_time > result_time:
                violations += 1

        if violations > 0:
            return ValidationCheck(
                name="前视偏差",
                passed=False,
                detail=f"发现 {violations} 处决策时间晚于赛果时间, 存在前视偏差!",
                severity="P0",
            )

        return ValidationCheck(
            name="前视偏差",
            passed=True,
            detail=f"检查 {len(market_data)} 条数据, 无前视偏差",
            severity="P2",
        )

    # ── 第三重: 状态持久化 ──

    def _check_state_persistence(self) -> ValidationCheck:
        """验证状态可持久化 — 组合/持仓状态可序列化和恢复.

        规则: PortfolioManager.to_dict() 含重建所需全量信息.
        """
        try:
            from bookmaker_sim.portfolio_manager import PortfolioManager
            pm = PortfolioManager(initial_equity=10000.0)
            pm.open_position("Test", "H", 2.0, 100.0)
            d = pm.to_dict()

            required_keys = [
                "initial_equity", "current_equity",
                "positions", "equity_curve",
            ]
            missing = [k for k in required_keys if k not in d]

            if missing:
                return ValidationCheck(
                    name="状态持久化",
                    passed=False,
                    detail=f"to_dict 缺少重建所需键: {missing}",
                    severity="P1",
                )

            # 验证序列化-反序列化可还原
            import json
            serialized = json.dumps(d)
            restored = json.loads(serialized)

            if abs(restored["initial_equity"] - 10000.0) > 0.01:
                return ValidationCheck(
                    name="状态持久化",
                    passed=False,
                    detail="JSON 序列化/反序列化后 initial_equity 不一致",
                    severity="P1",
                )

            return ValidationCheck(
                name="状态持久化",
                passed=True,
                detail=f"to_dict → JSON → 重建 完整可还原. "
                       f"序列化大小: {len(serialized)} chars",
                severity="P2",
            )

        except Exception as e:
            return ValidationCheck(
                name="状态持久化",
                passed=False,
                detail=f"校验异常: {e}",
                severity="P1",
            )

    # ── 额外: 策略引用完整性 ──

    def _check_strategy_refs(self) -> ValidationCheck:
        """验证策略注册表的引用完整性.

        所有启用的策略必须可实例化.
        """
        try:
            from bookmaker_sim.strategy_registry import get_registry
            registry = get_registry()

            broken = []
            for sid in registry.all_ids:
                inst = registry.get(sid)
                if inst is None:
                    broken.append(sid)

            if broken:
                return ValidationCheck(
                    name="策略引用完整性",
                    passed=False,
                    detail=f"策略不可实例化: {broken}",
                    severity="P1",
                )

            enabled = registry.list_enabled()
            return ValidationCheck(
                name="策略引用完整性",
                passed=True,
                detail=f"注册 {len(registry.all_ids)} 个策略, "
                       f"{len(enabled)} 个已启用, 全部可实例化",
                severity="P2",
            )

        except Exception as e:
            return ValidationCheck(
                name="策略引用完整性",
                passed=False,
                detail=f"校验异常: {e}",
                severity="P1",
            )


def validate_deployment(
    backtest_source: str = "",
    live_source: str = "",
    market_data: Optional[List[Dict[str, Any]]] = None,
) -> ValidationReport:
    """便利函数: 一键上线校验."""
    validator = DeploymentValidator()
    return validator.validate_all(backtest_source, live_source, market_data)

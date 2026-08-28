#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""portfolio_manager: 组合/账户层 (Phase A).

量化系统底座 — Bankroll + Position + 资金曲线追踪.

对接 bet_core 的下注/回测接口, 消费其 safe_stake / decide_direction 的 equity
输入输出, 包装为有状态的对象:
  - 初始资金/动态净值/出入金
  - 持仓列表 (开仓/平仓/浮动盈亏)
  - 资金曲线时序记录 (支持回放和可视化)
  - 总风险敞口计算

用法:
    pm = PortfolioManager(initial_equity=10000.0)
    pos = pm.open_position("TeamA vs TeamB", "H", 2.10, 500.0)
    pm.settle_position(pos, "H")  # 主胜 → win
    pm.to_dict()  # 含 equity_curve
"""

import json
import os
import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── 数据模型 ──


@dataclass
class Position:
    """单笔投注持仓."""

    position_id: str  # 唯一ID (时间戳+序号)
    match: str  # 对阵 "主队 vs 客队"
    direction: str  # H/D/A
    odds: float  # 下注时赔率
    stake: float  # 下注金额
    opened_at: datetime  # 开仓时间
    closed_at: Optional[datetime] = None  # 平仓时间
    pnl: Optional[float] = None  # 盈亏 (正=盈利)
    result: Optional[str] = None  # win / loss / pending
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EquityPoint:
    """资金曲线数据点."""

    timestamp: datetime
    equity: float  # 当前净值
    position_count: int  # 持仓数
    pnl: float = 0.0  # 本次变动
    note: str = ""


# ── 组合管理器 ──


class PortfolioManager:
    """组合/账户管理器.

    负责:
      - Bankroll 状态管理 (初始/动态/出入金)
      - 持仓生命周期 (开仓→结算/平仓)
      - 资金曲线记录
      - 风险敞口计算
    """

    def __init__(self, initial_equity: float = 3000.0) -> None:
        self._initial = initial_equity
        self._equity = initial_equity
        self._positions: List[Position] = []
        self._history: List[EquityPoint] = []
        self._pos_counter = 0

        # 记录初始点
        self._record_point(initial_equity, 0.0, "初始化")

    # ── 只读属性 ──

    @property
    def equity(self) -> float:
        """当前净值."""
        return self._equity

    @property
    def initial_equity(self) -> float:
        return self._initial

    @property
    def total_pnl(self) -> float:
        """总盈亏."""
        return self._equity - self._initial

    @property
    def total_roi(self) -> float:
        """总收益率 (%)."""
        if self._initial <= 0:
            return 0.0
        return (self._equity - self._initial) / self._initial * 100.0

    @property
    def open_positions(self) -> List[Position]:
        """当前持仓."""
        return [p for p in self._positions if p.result in (None, "pending")]

    @property
    def closed_positions(self) -> List[Position]:
        """已结算持仓."""
        return [p for p in self._positions if p.result not in (None, "pending")]

    @property
    def all_positions(self) -> List[Position]:
        return list(self._positions)

    @property
    def position_count(self) -> int:
        return len(self._positions)

    @property
    def equity_curve(self) -> List[EquityPoint]:
        """资金曲线 (从头至今)."""
        return list(self._history)

    # ── 核心操作 ──

    def open_position(
        self,
        match: str,
        direction: str,
        odds: float,
        stake: float,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Position:
        """开仓: 记录持仓, 扣减资金.

        Args:
            match: 对阵
            direction: H/D/A
            odds: 下注赔率
            stake: 下注金额
            metadata: 附加信息 (策略名/信号来源等)

        Returns:
            Position 对象

        Raises:
            ValueError: stake 超出可用资金
        """
        if stake > self._equity:
            raise ValueError(
                f"stake {stake:.2f} > available equity {self._equity:.2f}"
            )

        self._pos_counter += 1
        now = datetime.now(timezone.utc)
        pos = Position(
            position_id=f"P{now.strftime('%y%m%d%H%M%S')}{self._pos_counter:04d}",
            match=match,
            direction=direction,
            odds=odds,
            stake=stake,
            opened_at=now,
            result="pending",
            metadata=metadata or {},
        )
        self._positions.append(pos)
        self._equity -= stake
        self._record_point(self._equity, -stake, f"开仓 {match} {direction} @{odds:.2f}")
        return pos

    def close_position(
        self, position_id: str, result: str, pnl: float
    ) -> Optional[Position]:
        """平仓: 结算盈亏, 更新净值.

        Args:
            position_id: 持仓ID
            result: win / loss / draw
            pnl: 本次盈亏金额 (正=盈利, 负=亏损)

        Returns:
            已结算的 Position, 未找到返回 None
        """
        for pos in self._positions:
            if pos.position_id == position_id and pos.result in (None, "pending"):
                pos.result = result
                pos.pnl = pnl
                pos.closed_at = datetime.now(timezone.utc)
                self._equity += pos.stake + pnl  # 返还本金 + 盈亏
                self._record_point(
                    self._equity, pnl, f"平仓 {pos.match} {pos.direction}: {result}"
                )
                return pos
        return None

    def settle_position(self, pos: Position, winner: str) -> Position:
        """根据赛果结算持仓 (便利方法).

        Args:
            pos: 持仓对象
            winner: 实际赛果 H/D/A

        Returns:
            已结算的 Position
        """
        if pos.direction == winner:
            pnl = pos.stake * (pos.odds - 1.0)
            return self.close_position(pos.position_id, "win", pnl) or pos
        else:
            return self.close_position(pos.position_id, "loss", -pos.stake) or pos

    # ── 出入金 ──

    def deposit(self, amount: float, note: str = "入金") -> None:
        """入金."""
        if amount <= 0:
            raise ValueError(f"deposit amount must be positive: {amount}")
        self._equity += amount
        self._record_point(self._equity, amount, f"{note}: +{amount:.2f}")

    def withdraw(self, amount: float, note: str = "出金") -> None:
        """出金."""
        if amount <= 0:
            raise ValueError(f"withdraw amount must be positive: {amount}")
        if amount > self._equity:
            raise ValueError(
                f"withdraw {amount:.2f} > equity {self._equity:.2f}"
            )
        self._equity -= amount
        self._record_point(self._equity, -amount, f"{note}: -{amount:.2f}")

    # ── 风险敞口 ──

    def risk_exposure(self) -> float:
        """当前总风险敞口 = 所有待结算持仓的下注总额."""
        return sum(p.stake for p in self.open_positions)

    def risk_exposure_pct(self) -> float:
        """风险敞口占净值比例 (%). 净值≤0返回100."""
        total_at_risk = self.risk_exposure()
        total_capital = self._equity + total_at_risk
        if total_capital <= 0:
            return 100.0
        return total_at_risk / total_capital * 100.0

    # ── 内部 ──

    def _record_point(self, equity: float, pnl: float, note: str = "") -> None:
        self._history.append(
            EquityPoint(
                timestamp=datetime.now(timezone.utc),
                equity=equity,
                position_count=len(self.open_positions),
                pnl=pnl,
                note=note,
            )
        )

    # ── 序列化 ──

    def to_dict(self) -> Dict[str, Any]:
        """导出完整快照 (供前端/API消费)."""
        return {
            "initial_equity": round(self._initial, 2),
            "current_equity": round(self._equity, 2),
            "total_pnl": round(self.total_pnl, 2),
            "total_roi_pct": round(self.total_roi, 2),
            "position_count": self.position_count,
            "open_count": len(self.open_positions),
            "risk_exposure": round(self.risk_exposure(), 2),
            "risk_exposure_pct": round(self.risk_exposure_pct(), 2),
            "positions": [
                {
                    "position_id": p.position_id,
                    "match": p.match,
                    "direction": p.direction,
                    "odds": p.odds,
                    "stake": round(p.stake, 2),
                    "opened_at": p.opened_at.isoformat(),
                    "closed_at": p.closed_at.isoformat() if p.closed_at else None,
                    "pnl": round(p.pnl, 2) if p.pnl is not None else None,
                    "result": p.result,
                }
                for p in self._positions[-200:]  # 最近200条
            ],
            "equity_curve": [
                {
                    "timestamp": pt.timestamp.isoformat(),
                    "equity": round(pt.equity, 2),
                    "pnl": round(pt.pnl, 2),
                    "note": pt.note,
                }
                for pt in self._history[-500:]  # 最近500个点
            ],
        }


# ── 便捷工厂 ──

def create_portfolio(initial_equity: float = 3000.0) -> PortfolioManager:
    """创建默认组合管理器 (便利函数)."""
    return PortfolioManager(initial_equity=initial_equity)

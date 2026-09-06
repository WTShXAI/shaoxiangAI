#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""performance_analyzer: 绩效归因分析 (Phase A).

消费 PortfolioManager 的持仓/资金曲线数据, 计算量化交易标准绩效指标:
  - 夏普比率 (Sharpe Ratio)
  - 最大回撤 (Max Drawdown)
  - 卡玛比率 (Calmar Ratio)
  - 胜率 (Win Rate)
  - 盈亏比 (Profit / Loss Ratio)
  - 期望值 (Expected Value / EV)
  - 总收益率 (ROI)

用法:
    metrics = compute_all_metrics(
        equity_curve=[10000, 10200, 10150, ...],
        positions=[{"result": "win", "pnl": 200}, ...],
        total_roi_pct=5.2
    )
"""

import math
from typing import Any, Dict, List, Optional


def sharpe_ratio(
    returns: List[float],
    risk_free_rate: float = 0.0,
    periods: int = 252,
) -> float:
    """夏普比率.

    衡量风险调整后的超额收益.

    Args:
        returns: 每期收益率列表 (小数, 如 [0.01, -0.005, ...])
        risk_free_rate: 无风险利率 (年化, 默认0)
        periods: 年化周期数 (日频=252, 周频=52, 月频=12)

    Returns:
        年化夏普比率. 数据不足2期或收益≤0时返回0.
    """
    if len(returns) < 2:
        return 0.0

    avg_r = sum(returns) / len(returns)
    if avg_r <= 0:
        return 0.0

    variance = sum((r - avg_r) ** 2 for r in returns) / (len(returns) - 1)
    if variance <= 0:
        return 0.0

    std = math.sqrt(variance)
    annual_factor = math.sqrt(periods)
    daily_rf = risk_free_rate / periods
    return (avg_r - daily_rf) / std * annual_factor


def max_drawdown(equity_curve: List[float]) -> float:
    """最大回撤 (%).

    从峰值到谷底的最大百分比跌幅.

    Args:
        equity_curve: 资金净值序列

    Returns:
        最大回撤百分比 (如 12.5 表示回撤12.5%). 数据不足2期返回0.
    """
    if len(equity_curve) < 2:
        return 0.0

    peak: float = equity_curve[0]
    max_dd: float = 0.0

    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100.0
        if dd > max_dd:
            max_dd = dd

    return max_dd


def calmar_ratio(total_roi_pct: float, max_dd_pct: float) -> float:
    """卡玛比率 = 总收益率 / 最大回撤.

    衡量回撤调整后的收益. 回撤为0时返回0.

    Args:
        total_roi_pct: 总收益率 (%)
        max_dd_pct: 最大回撤 (%)

    Returns:
        卡玛比率
    """
    if max_dd_pct <= 0:
        return 0.0
    return total_roi_pct / max_dd_pct


def win_rate(positions: List[Dict[str, Any]]) -> float:
    """胜率 (%) = 盈利注数 / 总注数 × 100.

    Args:
        positions: 持仓列表, 每项含 result (win/loss) 字段

    Returns:
        胜率百分比
    """
    total = len(positions)
    if total == 0:
        return 0.0
    wins = sum(1 for p in positions if p.get("result") == "win")
    return wins / total * 100.0


def profit_loss_ratio(positions: List[Dict[str, Any]]) -> float:
    """盈亏比 = 平均盈利 / 平均亏损.

    Args:
        positions: 持仓列表, 每项含 result 和 pnl 字段

    Returns:
        盈亏比. 无盈利或无亏损时返回0.
    """
    wins = [p for p in positions if p.get("result") == "win" and (p.get("pnl") or 0) > 0]
    losses = [
        p for p in positions if p.get("result") == "loss" and (p.get("pnl") or 0) < 0
    ]

    if not wins or not losses:
        return 0.0

    avg_win = sum(p["pnl"] for p in wins) / len(wins)
    avg_loss = abs(sum(p["pnl"] for p in losses) / len(losses))

    if avg_loss <= 0:
        return 0.0
    return avg_win / avg_loss


def expected_value(positions: List[Dict[str, Any]]) -> float:
    """期望值 (EV) = 平均每注盈亏.

    Args:
        positions: 持仓列表

    Returns:
        每注平均期望值
    """
    if not positions:
        return 0.0
    total_pnl = sum(p.get("pnl", 0) for p in positions)
    return total_pnl / len(positions)


def compute_all_metrics(
    equity_curve: List[float],
    positions: List[Dict[str, Any]],
    total_roi_pct: float,
) -> Dict[str, Any]:
    """一键计算全部绩效指标.

    Args:
        equity_curve: 资金净值序列 (如 [10000, 10200, ...])
        positions: 持仓列表 (每项含 result/pnl 字段)
        total_roi_pct: 总收益率 (%)

    Returns:
        {
            "sharpe_ratio": float,
            "max_drawdown_pct": float,
            "calmar_ratio": float,
            "win_rate_pct": float,
            "profit_loss_ratio": float,
            "expected_value": float,
            "total_trades": int,
            "total_roi_pct": float,
        }
    """
    # 从资金曲线提取收益率序列
    returns: List[float] = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        if prev > 0:
            returns.append((equity_curve[i] - prev) / prev)

    dd = max_drawdown(equity_curve)
    wr = win_rate(positions)
    plr = profit_loss_ratio(positions)
    ev = expected_value(positions)

    return {
        "sharpe_ratio": round(sharpe_ratio(returns), 4),
        "max_drawdown_pct": round(dd, 2),
        "calmar_ratio": round(calmar_ratio(total_roi_pct, dd), 4),
        "win_rate_pct": round(wr, 2),
        "profit_loss_ratio": round(plr, 4),
        "expected_value": round(ev, 4),
        "total_trades": len(positions),
        "total_roi_pct": round(total_roi_pct, 2),
    }

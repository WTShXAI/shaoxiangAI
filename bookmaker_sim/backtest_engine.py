#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_engine: 轻量回测引擎 (Phase B).

统一回测事件循环:
  1. 喂入比赛数据序列 (赔率/赛果)
  2. 依次执行策略 (从注册表获取信号)
  3. 对接 bet_core 计算注码
  4. 对接 PortfolioManager 记录持仓/资金曲线
  5. 输出绩效归因 (performance_analyzer)

设计原则:
  - 轻量: 无外部依赖, 纯 in-memory 事件循环
  - 可对接真实数据: match_data 可以是 dict/list/DataFrame
  - 可多策略并行: 支持组合回测 (按权重分配资金)
  - 时序严格: 逐场回放, 禁止前视

用法:
    engine = BacktestEngine(initial_equity=10000.0)
    result = engine.run(
        matches=matches,           # List[Dict] 含 p_vec/odds/winner
        strategy_ids=["MyStrategy"],
    )
    print(result.summary)
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from bookmaker_sim.portfolio_manager import PortfolioManager
from bookmaker_sim.performance_analyzer import compute_all_metrics
from bookmaker_sim.strategy_registry import get_registry

logger = logging.getLogger(__name__)


# ── 结果模型 ──


@dataclass
class BacktestResult:
    """回测结果."""

    initial_equity: float
    final_equity: float
    total_pnl: float
    total_roi_pct: float
    total_bets: int
    wins: int
    losses: int
    metrics: Dict[str, Any] = field(default_factory=dict)
    equity_curve: List[float] = field(default_factory=list)
    trades: List[Dict[str, Any]] = field(default_factory=list)
    strategy_stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def summary(self) -> Dict[str, Any]:
        return {
            "initial_equity": round(self.initial_equity, 2),
            "final_equity": round(self.final_equity, 2),
            "total_pnl": round(self.total_pnl, 2),
            "total_roi_pct": round(self.total_roi_pct, 2),
            "total_bets": self.total_bets,
            "win_rate_pct": round(self.wins / self.total_bets * 100, 2) if self.total_bets > 0 else 0.0,
            **self.metrics,
        }


# ── 回测引擎 ──


class BacktestEngine:
    """轻量回测引擎.

    用法:
        engine = BacktestEngine(initial_equity=10000.0)
        result = engine.run(matches, ["vl_divergence"])
    """

    def __init__(
        self,
        initial_equity: float = 3000.0,
        gate: bool = True,
        frac_kelly: float = 0.25,
        max_stake_frac: float = 0.10,
    ) -> None:
        self._initial_equity = initial_equity
        self._gate = gate
        self._frac_kelly = frac_kelly
        self._max_stake_frac = max_stake_frac
        self._registry = get_registry()

    def run(
        self,
        matches: List[Dict[str, Any]],
        strategy_ids: Optional[List[str]] = None,
    ) -> BacktestResult:
        """运行回测.

        Args:
            matches: 比赛数据列表, 每项含:
                - p_vec: List[float] 模型预测概率 [H, D, A]
                - odds: List[float] 市场赔率 [H, D, A]
                - winner: str 实际赛果 "H"/"D"/"A"
                - match_name: str (可选) 对阵名称
                - league: str (可选) 联赛
            strategy_ids: 策略ID列表 (None=注册表全部启用)

        Returns:
            BacktestResult
        """
        if strategy_ids is None:
            strategy_ids = self._registry.list_enabled()

        pm = PortfolioManager(initial_equity=self._initial_equity)
        trades: List[Dict[str, Any]] = []
        strategy_trades: Dict[str, List[Dict[str, Any]]] = {
            sid: [] for sid in strategy_ids
        }

        for match in matches:
            p_vec = match["p_vec"]
            odds = match["odds"]
            winner = match["winner"]
            match_name = match.get("match_name", "")
            direction_labels = ["H", "D", "A"]

            for sid in strategy_ids:
                strategy = self._registry.get(sid)
                if strategy is None:
                    continue

                # 调用策略获取信号
                signal = self._get_signal(strategy, p_vec, odds, match)

                if signal and signal.get("decision") == "BET":
                    direction = signal["direction"]
                    idx = direction_labels.index(direction)
                    target_odds = odds[idx]
                    p = p_vec[idx]
                    equity_before = pm.equity

                    # 通过 bet_core 算注码
                    stake = self._calc_stake(p, target_odds, equity_before)

                    if stake > 0:
                        # 开仓
                        pos = pm.open_position(
                            match=match_name or f"Match #{len(pm.all_positions) + 1}",
                            direction=direction,
                            odds=target_odds,
                            stake=stake,
                            metadata={"strategy": sid},
                        )

                        # 结算
                        settled = pm.settle_position(pos, winner)

                        trade = {
                            "match": match_name,
                            "strategy": sid,
                            "direction": direction,
                            "odds": target_odds,
                            "stake": round(stake, 2),
                            "pnl": round(settled.pnl, 2) if settled.pnl is not None else 0.0,
                            "result": settled.result,
                            "equity_after": round(pm.equity, 2),
                        }
                        trades.append(trade)
                        strategy_trades[sid].append(trade)

        # 计算绩效
        equity_vals = [pt.equity for pt in pm.equity_curve]
        pos_dicts = [
            {"result": p.result, "pnl": p.pnl} for p in pm.all_positions
        ]
        metrics = compute_all_metrics(equity_vals, pos_dicts, pm.total_roi)

        # 策略级统计
        strategy_stats: Dict[str, Dict[str, Any]] = {}
        for sid, strades in strategy_trades.items():
            meta = self._registry.get_metadata(sid)
            wins = sum(1 for t in strades if t["result"] == "win")
            total = len(strades)
            pnl = sum(t["pnl"] for t in strades)
            strategy_stats[sid] = {
                "name": meta.name if meta else sid,
                "total_bets": total,
                "wins": wins,
                "losses": total - wins,
                "win_rate_pct": round(wins / total * 100, 2) if total > 0 else 0.0,
                "total_pnl": round(pnl, 2),
                "avg_pnl": round(pnl / total, 2) if total > 0 else 0.0,
            }

        result = BacktestResult(
            initial_equity=self._initial_equity,
            final_equity=pm.equity,
            total_pnl=pm.total_pnl,
            total_roi_pct=round(pm.total_roi, 2),
            total_bets=len(trades),
            wins=sum(1 for t in trades if t["result"] == "win"),
            losses=sum(1 for t in trades if t["result"] == "loss"),
            metrics=metrics,
            equity_curve=equity_vals,
            trades=trades,
            strategy_stats=strategy_stats,
        )

        return result

    # ── 内部辅助 ──

    def _get_signal(self, strategy: Any, p_vec: List[float],
                    odds: List[float], match: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """调用策略并获取信号."""
        try:
            result = strategy.signal(match)
            if hasattr(result, "to_dict"):
                return result.to_dict()
            if isinstance(result, dict):
                return result
            # StrategySignal duck-type
            return {
                "decision": getattr(result, "decision", "PASS"),
                "direction": getattr(result, "direction", None),
            }
        except (AttributeError, NotImplementedError):
            # 兼容老式策略: 直接用 decide_argmax
            from scripts.bet_core import decide_argmax
            new_eq, stake, win = decide_argmax(
                p_vec, odds, 10000, match.get("winner", "H"), gate=True
            )
            if stake > 0:
                direction_labels = ["H", "D", "A"]
                idx = max(range(3), key=lambda j: p_vec[j])
                return {"decision": "BET", "direction": direction_labels[idx]}
            return {"decision": "PASS"}

    def _calc_stake(
        self,
        p: float,
        odds: float,
        equity: float,
    ) -> float:
        """通过 bet_core 计算注码."""
        from scripts.bet_core import safe_stake
        stake, _ = safe_stake(
            p, odds, equity,
            frac_kelly=self._frac_kelly,
            max_frac=self._max_stake_frac,
            gate=self._gate,
        )
        return stake

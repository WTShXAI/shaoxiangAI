# -*- coding: utf-8 -*-
"""组合层 (演示): 模拟账户 / 持仓 / 资金曲线. 内存态, 不落 DB."""
from typing import List, Dict, Any
from .types import Position


class Portfolio:
    def __init__(self, init_bankroll: float = 3000.0):
        self.init = init_bankroll
        self.equity = init_bankroll
        self.peak = init_bankroll
        self.positions: List[Position] = []
        self.equity_curve: List[Dict[str, Any]] = [
            {"step": 0, "equity": init_bankroll, "bankroll": init_bankroll}
        ]
        self.step = 0

    def record_equity(self, note: str = ""):
        self.step += 1
        self.peak = max(self.peak, self.equity)
        self.equity_curve.append({
            "step": self.step,
            "equity": round(self.equity, 2),
            "bankroll": round(self.equity, 2),
            "note": note,
        })

    def settle(self, oid, mid, home, away, strategy_id, direction, odds,
               stake, win: bool) -> Position:
        """结算一笔 (模拟盘): 赢则 +stake*(odds-1), 输则 -stake."""
        pnl = stake * (odds - 1) if win else -stake
        self.equity = round(self.equity + pnl, 2)
        pos = Position(
            oid=oid, mid=mid, home=home, away=away,
            strategy_id=strategy_id, direction=direction, odds=odds,
            stake=round(stake, 2), win=win, pnl=round(pnl, 2),
            equity_after=self.equity,
        )
        self.positions.append(pos)
        self.record_equity(note=f"{home} vs {away} [{direction}] {'赢' if win else '输'}")
        return pos

    # ── 绩效指标 (回测/终端展示) ──
    def stats(self) -> Dict[str, Any]:
        n = len(self.positions)
        wins = sum(1 for p in self.positions if p.win)
        pnl_total = sum(p.pnl for p in self.positions)
        max_dd = self._max_drawdown()
        sharpe = self._sharpe()
        return {
            "init_bankroll": self.init,
            "equity": round(self.equity, 2),
            "peak": round(self.peak, 2),
            "return_pct": round((self.equity / self.init - 1) * 100, 2),
            "bets": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate": round(wins / n * 100, 1) if n else 0.0,
            "pnl_total": round(pnl_total, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "sharpe": round(sharpe, 2),
        }

    def _max_drawdown(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]["equity"]
        mdd = 0.0
        for p in self.equity_curve:
            peak = max(peak, p["equity"])
            dd = (peak - p["equity"]) / peak if peak > 0 else 0.0
            mdd = max(mdd, dd)
        return mdd

    def _sharpe(self) -> float:
        if len(self.equity_curve) < 3:
            return 0.0
        rets = []
        for i in range(1, len(self.equity_curve)):
            prev = self.equity_curve[i - 1]["equity"]
            cur = self.equity_curve[i]["equity"]
            if prev > 0:
                rets.append((cur - prev) / prev)
        if not rets:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        std = var ** 0.5
        if std == 0:
            return 0.0
        return (mean / std) * (len(rets) ** 0.5)

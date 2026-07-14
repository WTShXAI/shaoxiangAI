# -*- coding: utf-8 -*-
"""量化模拟引擎 (演示): 编排 行情→策略→组合→执行→绩效.

内存态单例, 不落 DB. 复用:
  - synthetic.make_batch        合成行情/赛果
  - strategies.generate_signals 多策略信号 (compute_value_layer)
  - execution.build_order/confirm_order  自动算注 + 确认闸 (bet_core)
  - portfolio.Portfolio         模拟账户/资金曲线/绩效
"""
from typing import Dict, List, Any, Optional
from .types import StrategyMeta, PendingOrder
from . import synthetic
from . import strategies
from . import execution
from . import portfolio


class QuantDemoEngine:
    def __init__(self, init_bankroll: float = 3000.0, batch_size: int = 24):
        self.pf = portfolio.Portfolio(init_bankroll)
        self.meta: List[StrategyMeta] = [StrategyMeta(m.id, m.name, m.desc, enabled=True)
                                         for m in strategies.META]
        self.matches: Dict[str, synthetic.SyntheticMatch] = {}
        self.pending: List[PendingOrder] = []
        self.cursor = 0
        self._load_batch(batch_size)

    def _load_batch(self, n: int):
        for m in synthetic.make_batch(n=n):
            self.matches[m.mid] = m

    @property
    def enabled_ids(self) -> List[str]:
        return [m.id for m in self.meta if m.enabled]

    # ── 状态快照 (给前端/API) ──
    def snapshot(self) -> Dict[str, Any]:
        return {
            "account": self.pf.stats(),
            "equity_curve": self.pf.equity_curve,
            "strategies": [
                {"id": m.id, "name": m.name, "desc": m.desc, "enabled": m.enabled}
                for m in self.meta
            ],
            "pending": [self._order_dict(o) for o in self.pending],
            "positions": [
                {"oid": p.oid, "home": p.home, "away": p.away,
                 "strategy": p.strategy_id, "direction": p.direction,
                 "odds": p.odds, "stake": p.stake, "win": p.win,
                 "pnl": p.pnl, "equity_after": p.equity_after}
                for p in self.pf.positions[-30:]
            ],
            "matches_done": len(self.pf.positions),
            "matches_total": len(self.matches),
        }

    @staticmethod
    def _order_dict(o: PendingOrder) -> Dict[str, Any]:
        return {
            "oid": o.oid, "mid": o.mid, "home": o.home, "away": o.away,
            "strategy_id": o.strategy_id, "strategy_name": o.strategy_name,
            "direction": o.direction, "odds": o.odds, "stake": o.stake,
            "mode": o.mode, "confidence": o.confidence, "created_at": o.created_at,
        }

    # ── 策略开关 ──
    def toggle_strategy(self, sid: str, enabled: bool) -> Dict[str, Any]:
        for m in self.meta:
            if m.id == sid:
                m.enabled = enabled
        return {"ok": True, "strategies": [{"id": m.id, "enabled": m.enabled} for m in self.meta]}

    # ── 跑下一场比赛: 生成信号 + 订单 (sim 自动结算 / live 留待确认闸) ──
    def step(self, mode: str = "sim") -> Dict[str, Any]:
        """推进到「下一场有可下注信号的比赛」, 生成订单.
        - sim 模式: 生成后立即结算 (演示自动操作闭环, 资金曲线即时移动)
        - live 模式: 仅入 pending, 等待前端手动「确认」(人工闸)
        为演示友好, 自动跳过无信号的比赛, 保证每次点击都有可见反馈.
        """
        new_orders = []
        settled = []
        last_match = None
        last_sigs = []
        while self.cursor < len(self.matches):
            mid = list(self.matches.keys())[self.cursor]
            self.cursor += 1
            m = self.matches[mid]
            sigs = strategies.generate_signals(m, self.enabled_ids)
            last_match = m
            last_sigs = sigs
            for s in sigs:
                if s.decision == "BET" and s.direction:
                    o = execution.build_order(m, s, self.pf.equity, mode=mode)
                    if o.stake > 0:
                        if mode == "sim":
                            # 模拟盘: 立即结算 (用模拟赛果)
                            pos = execution.confirm_order(self.pf, o, m.winner)
                            settled.append({
                                "oid": pos.oid, "home": pos.home, "away": pos.away,
                                "direction": pos.direction, "odds": pos.odds,
                                "stake": pos.stake, "win": pos.win, "pnl": pos.pnl,
                                "equity_after": pos.equity_after,
                            })
                        else:
                            # 真实盘: 留待人工确认闸
                            self.pending.append(o)
                            new_orders.append(self._order_dict(o))
            if new_orders or settled:
                break  # 找到可下注比赛, 返回
        if not new_orders and not settled and self.cursor >= len(self.matches):
            return {"done": True, "message": "全部比赛已处理", "pending": self.snapshot()}
        m = last_match
        return {
            "done": False,
            "mode": mode,
            "match": {"mid": m.mid, "home": m.home, "away": m.away,
                      "league": m.league, "best_odds": m.best_odds,
                      "consensus_prob": m.consensus_prob, "scenarios": m.scenarios,
                      "winner": m.winner},
            "signals": [
                {"strategy_id": s.strategy_id, "strategy_name": s.strategy_name,
                 "decision": s.decision, "direction": s.direction,
                 "edge_pct": s.edge_pct, "ev_pct": s.ev_pct, "note": s.note}
                for s in last_sigs
            ],
            "new_orders": new_orders,
            "settled": settled,
            "pending_count": len(self.pending),
        }

    # ── 确认单笔 ──
    def confirm_one(self, oid: str) -> Dict[str, Any]:
        for i, o in enumerate(self.pending):
            if o.oid == oid:
                m = self.matches.get(o.mid)
                winner = m.winner if m else "D"
                pos = execution.confirm_order(self.pf, o, winner)
                self.pending.pop(i)
                return {"ok": True, "position": {
                    "oid": pos.oid, "home": pos.home, "away": pos.away,
                    "direction": pos.direction, "odds": pos.odds,
                    "stake": pos.stake, "win": pos.win, "pnl": pos.pnl,
                    "equity_after": pos.equity_after}}
        return {"ok": False, "message": "订单不存在或已确认"}

    # ── 一键确认全部 (模拟盘自动结算) ──
    def confirm_all(self) -> Dict[str, Any]:
        settled = []
        remaining = []
        for o in self.pending:
            m = self.matches.get(o.mid)
            winner = m.winner if m else "D"
            pos = execution.confirm_order(self.pf, o, winner)
            settled.append({"oid": pos.oid, "home": pos.home, "away": pos.away,
                            "direction": pos.direction, "stake": pos.stake,
                            "win": pos.win, "pnl": pos.pnl})
        self.pending.clear()
        return {"ok": True, "settled": settled, "account": self.pf.stats()}

    # ── 自动模拟整批 (演示自动操作) ──
    def auto_sim(self, mode: str = "sim") -> Dict[str, Any]:
        while self.cursor < len(self.matches):
            self.step(mode=mode)
            self.confirm_all()  # 模拟盘自动结算
        return {"ok": True, "account": self.pf.stats(),
                "equity_curve_len": len(self.pf.equity_curve)}

    # ── 重置 ──
    def reset(self, init_bankroll: float = 3000.0, batch_size: int = 24):
        self.__init__(init_bankroll=init_bankroll, batch_size=batch_size)
        return {"ok": True}


# 模块级单例 (演示内存态)
_ENGINE: Optional[QuantDemoEngine] = None


def get_engine() -> QuantDemoEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = QuantDemoEngine()
    return _ENGINE

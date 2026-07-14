# -*- coding: utf-8 -*-
"""执行层 (演示): 自动算注码 -> 生成 pending 订单 -> 手动确认闸 -> 落库结算.

设计 (对齐用户选择: 混合=模拟为主 + 手动确认真实下单):
  - 模拟盘(sim): pending 订单可由「一键确认全部」自动结算, 演示自动操作闭环.
  - 真实盘(live): 仅生成 pending 订单, 必须由用户在前端点「确认」才结算 (人工闸).
注码计算复用 SSoT: scripts.bet_core.decide_direction (规范半凯利, 已修 gate).
"""
import time
import uuid
from typing import List
from .types import PendingOrder, StrategySignal, SyntheticMatch
from .portfolio import Portfolio

# 单一事实源: 注码核心
from scripts.bet_core import decide_direction


def build_order(m: SyntheticMatch, sig: StrategySignal, equity: float,
                mode: str = "sim") -> PendingOrder:
    """用规范半凯利算注码, 生成 pending 订单 (不下注).

    注码由 decide_direction(idx, cons, best_odds, equity, winner, gate=True) 算出,
    其内部 safe_stake 走规范半凯利 + 单注封顶(MAX_STAKE_FRAC). winner 传入仅为
    注码数学完整性 (实际 stake 不依赖 winner). 订单额外存 winner 供确认时结算.
    """
    idx = {"H": 0, "D": 1, "A": 2}[sig.direction]
    _new_eq, stake, _win = decide_direction(
        idx, m.consensus_prob, m.best_odds, equity, m.winner, gate=True)
    return PendingOrder(
        oid=str(uuid.uuid4())[:8],
        mid=m.mid, home=m.home, away=m.away,
        strategy_id=sig.strategy_id, strategy_name=sig.strategy_name,
        direction=sig.direction, odds=sig.best_odds or m.best_odds[idx],
        stake=round(stake, 2), equity_before=round(equity, 2),
        mode=mode,
        confidence=round((sig.ev_pct or 0) / 100.0, 3),
        created_at=time.strftime("%H:%M:%S"),
    )


def confirm_order(pf: Portfolio, order: PendingOrder, winner: str) -> object:
    """确认订单 -> 结算 (用模拟赛果). 真实对接不在本演示范围, live 仅人工闸区别."""
    pos = pf.settle(
        oid=order.oid, mid=order.mid, home=order.home, away=order.away,
        strategy_id=order.strategy_id, direction=order.direction,
        odds=order.odds, stake=order.stake,
        win=(order.direction == winner),
    )
    return pos

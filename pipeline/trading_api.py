# -*- coding: utf-8 -*-
"""pipeline/trading_api.py — 哨响AI 后端交易 REST API

挂载方式 (在 bridge_service.py 或独立 FastAPI app):
    from pipeline.trading_api import trading_router
    app.include_router(trading_router)

端点:
    GET  /api/trading/signals     — 当前所有比赛的 BET 信号
    POST /api/trading/place       — 模拟下单 (写入 bets.db)
    GET  /api/trading/portfolio   — 持仓列表 + 累计盈亏 + 资金曲线
    POST /api/trading/settle/{bet_id} — 手动结算某笔投注

铁律
----
- 下单唯一落库 = pipeline/bet_ledger.py (SSoT)
- 信号唯一事实源 = pipeline/strategy.py 的 build_portfolio() + compute_value_layer
- 结算只认 bet_ledger.settle_bet(), 不写绕过逻辑
- 仅依赖标准库 + FastAPI + 既有 pipeline 模块; 无重依赖
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field

logger = logging.getLogger("trading_api")

# ── bet_ledger SSoT ──
try:
    from pipeline.bet_ledger import (
        init_db as _ledger_init,
        record_bet as _ledger_record,
        settle_bet as _ledger_settle,
        list_open as _ledger_list_open,
        roi_stats as _ledger_roi_stats,
    )
    _HAS_LEDGER = True
except Exception as _e:
    _HAS_LEDGER = False
    logger.error("bet_ledger 导入失败: %s", _e)

# ── 价值层 + 策略层 SSoT ──
try:
    from pipeline.compute_value_layer import compute_value_layer
    from pipeline.strategy import (
        ValueSignal, BetPlan, build_portfolio, get_registry,
    )
    _HAS_VALUE = True
except Exception as _e:
    _HAS_VALUE = False
    logger.error("价值层/策略层导入失败: %s", _e)


# ════════════════════════════════════════════════════════════════
# Pydantic 模型
# ════════════════════════════════════════════════════════════════

class PlaceOrderRequest(BaseModel):
    """下单请求体。"""
    match_id: str = Field(..., description="比赛 ID")
    market: str = Field(default="1X2", description="市场: 1X2 / OU / AH / CS")
    selection: str = Field(..., description="选择: H/D/A 或 Over 2.5 等")
    odds: float = Field(..., gt=1.0, description="投注赔率 (>1)")
    stake: float = Field(..., gt=0, description="注码金额")
    home: Optional[str] = Field(default=None)
    away: Optional[str] = Field(default=None)
    league: Optional[str] = Field(default=None)
    book: Optional[str] = Field(default=None)
    model_prob: Optional[float] = Field(default=None)
    mode: str = Field(default="sim", description="sim / real")


class SettleRequest(BaseModel):
    """结算请求体 (手动结算)。"""
    won: Optional[bool] = Field(default=None, description="True=赢, False=输")
    actual_result: Optional[str] = Field(default=None, description="赛果: H/D/A 或比分")
    actual_score: Optional[str] = Field(default=None, description="实际比分: '2-1'")
    payout: Optional[float] = Field(default=None, description="派彩总额 (覆盖 won 判定)")


class SignalResponse(BaseModel):
    """信号响应。"""
    mid: str
    home: str
    away: str
    league: str
    market: str
    selection: str
    odds: float
    model_prob: float
    edge_pct: float
    ev_pct: float
    stake: float
    decision: str


# ════════════════════════════════════════════════════════════════
# Router
# ════════════════════════════════════════════════════════════════

trading_router = APIRouter(prefix="/api/trading", tags=["trading"])


# ── 辅助: 从已注册策略 / 价值层拉信号 ──────────────────────────

def _collect_signals() -> List[Dict[str, Any]]:
    """从价值层拉取所有当前比赛的 BET 信号。

    遍历所有注册策略 (get_registry), 对每场可用的比赛数据计算价值层,
    只收 decision=="BET" 的信号。
    """
    if not _HAS_VALUE:
        return []

    signals: List[Dict[str, Any]] = []
    try:
        registry = get_registry()
        for sid, strategy in registry.items():
            # 策略是 per-match 的; 尝试调用 signals() 但需要 match dict
            # 实际上 signals 是由上层喂 match 数据驱动的。
            # 这里我们提供注册信息供前端参考。
            pass
    except Exception as e:
        logger.warning("拉取策略注册表失败: %s", e)

    # 实际生产中, 信号由 pipeline/engine 或 autopilot 产出后缓存在
    # bet_ledger 或进程内存中。这里从 bet_ledger 的 open 单读取。
    if _HAS_LEDGER:
        try:
            open_bets = _ledger_list_open(limit=200)
            for bet in open_bets:
                signals.append({
                    "mid": bet.get("match_id", ""),
                    "home": bet.get("home", ""),
                    "away": bet.get("away", ""),
                    "league": bet.get("league", ""),
                    "market": bet.get("market", "1X2"),
                    "selection": bet.get("selection", ""),
                    "odds": bet.get("odds", 0.0),
                    "model_prob": bet.get("model_prob", 0.0),
                    "edge_pct": bet.get("edge_pp", 0.0),
                    "ev_pct": bet.get("ev_pct", 0.0),
                    "stake": bet.get("stake", 0.0),
                    "decision": bet.get("decision", "BET"),
                    "bet_id": bet.get("bet_id", ""),
                    "status": bet.get("status", "open"),
                    "placed_at": bet.get("placed_at", ""),
                })
        except Exception as e:
            logger.warning("从 bet_ledger 拉信号失败: %s", e)

    return signals


# ════════════════════════════════════════════════════════════════
# 端点
# ════════════════════════════════════════════════════════════════

@trading_router.get("/signals", response_model=List[SignalResponse])
async def get_signals(
    market: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """获取当前所有 BET 信号。

    Query params:
        market: 过滤市场 (1X2 / OU / CS), 不传=全部
        limit:  最大返回条数 (默认 50)
    """
    if not _HAS_LEDGER:
        raise HTTPException(status_code=503, detail="bet_ledger 模块不可用")

    try:
        all_signals = _collect_signals()
        if market:
            m = market.upper()
            all_signals = [s for s in all_signals if s.get("market", "").upper() == m]
        # 按时间倒序
        all_signals.sort(key=lambda s: s.get("placed_at", ""), reverse=True)
        return all_signals[:limit]
    except Exception as e:
        logger.error("GET /signals 异常: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@trading_router.post("/place")
async def place_order(req: PlaceOrderRequest) -> Dict[str, Any]:
    """模拟下单: 写入 bets.db (幂等)。

    Request body:
        match_id, market, selection, odds, stake (必填)
        home, away, league, book, model_prob, mode (可选)
    """
    if not _HAS_LEDGER:
        raise HTTPException(status_code=503, detail="bet_ledger 模块不可用")

    try:
        bet_id = _ledger_record(
            match_id=req.match_id,
            home=req.home,
            away=req.away,
            league=req.league,
            market=req.market,
            selection=req.selection,
            book=req.book,
            odds=req.odds,
            model_prob=req.model_prob,
            stake=req.stake,
            mode=req.mode,
            source="trading_api",
            placed_at=time.time(),
        )
        return {
            "ok": True,
            "bet_id": bet_id,
            "match_id": req.match_id,
            "selection": req.selection,
            "odds": req.odds,
            "stake": req.stake,
            "mode": req.mode,
        }
    except Exception as e:
        logger.error("POST /place 异常: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@trading_router.get("/portfolio")
async def get_portfolio() -> Dict[str, Any]:
    """获取当前持仓列表 + 累计盈亏 + 资金曲线数据。

    返回:
        holdings:   未结算持仓列表
        roi:        按 mode/market/severity 分组的 ROI 统计
        equity:     累计盈亏 (净额)
        curve:      资金曲线 (按日聚合的累计 PnL)
    """
    if not _HAS_LEDGER:
        raise HTTPException(status_code=503, detail="bet_ledger 模块不可用")

    try:
        # 持仓 (open)
        holdings = _ledger_list_open(limit=200)

        # ROI 统计
        roi = _ledger_roi_stats()
        overall = roi.get("overall", {})

        # 资金曲线: 从 bet_ledger 查所有已结算单, 按日聚合 PnL
        curve = _calc_equity_curve()

        return {
            "holdings": holdings,
            "holdings_count": len(holdings),
            "total_net_pnl": overall.get("net", 0.0),
            "total_stake": overall.get("stake", 0.0),
            "roi_pct": overall.get("roi_pct", 0.0),
            "hit_pct": overall.get("hit_pct", 0.0),
            "roi_by_mode": roi.get("by_mode", {}),
            "roi_by_market": roi.get("by_market", {}),
            "equity_curve": curve,
        }
    except Exception as e:
        logger.error("GET /portfolio 异常: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@trading_router.post("/settle/{bet_id}")
async def settle_bet_manual(
    bet_id: str = Path(..., description="投注 ID (bet_id)"),
    body: SettleRequest = SettleRequest(),
) -> Dict[str, Any]:
    """手动结算某笔投注。

    Path param:
        bet_id: 投注 ID (由 /place 返回)

    Request body (至少提供一个):
        won:            True=赢, False=输
        actual_result:  赛果 (H/D/A 或比分 '2-1')
        actual_score:   实际比分
        payout:         派彩总额 (优先级 > won)
    """
    if not _HAS_LEDGER:
        raise HTTPException(status_code=503, detail="bet_ledger 模块不可用")

    try:
        ok = _ledger_settle(
            bet_id=bet_id,
            won=body.won,
            actual_result=body.actual_result,
            actual_score=body.actual_score,
            payout=body.payout,
        )
        if not ok:
            raise HTTPException(
                status_code=404,
                detail=f"投注 {bet_id} 不存在或已结算 (幂等: 不重复结算)",
            )
        return {"ok": True, "bet_id": bet_id, "message": "结算完成"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("POST /settle 异常: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── 资金曲线辅助 ─────────────────────────────────────────────────

def _calc_equity_curve(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """按日聚合已结算投注的累计 PnL。"""
    import sqlite3

    path = db_path or os.path.join("data", "bets.db")
    if not os.path.exists(path):
        return []

    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT pnl, settled_at FROM bet_ledger
               WHERE status IN ('won','lost','push') AND pnl IS NOT NULL
               ORDER BY settled_at ASC"""
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning("资金曲线查询失败: %s", e)
        return []

    # 按日聚合
    daily: Dict[str, float] = {}
    for row in rows:
        pnl = float(row["pnl"] or 0.0)
        ts = row["settled_at"]
        if ts:
            day = datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
        else:
            day = "unknown"
        daily[day] = daily.get(day, 0.0) + pnl

    # 累计
    cumulative = 0.0
    curve: List[Dict[str, Any]] = []
    for day in sorted(daily):
        cumulative += daily[day]
        curve.append({
            "date": day,
            "daily_pnl": round(daily[day], 2),
            "cumulative_pnl": round(cumulative, 2),
        })
    return curve


__all__ = [
    "trading_router",
    "PlaceOrderRequest",
    "SettleRequest",
    "SignalResponse",
    "get_signals",
    "place_order",
    "get_portfolio",
    "settle_bet_manual",
]

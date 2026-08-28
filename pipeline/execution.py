# -*- coding: utf-8 -*-
"""pipeline/execution.py
================================
执行层 + 手动确认闸 · 单一事实源 (SSoT)  —  A (#20 「底座」)

闭环位置 (ARCHITECTURE.md §3):
    策略层 → BetPlan (#19)
       ├─ 模拟盘 (sim):  自动执行 → 自动结算 → database 落库 → equity 更新
       └─ 真实盘 (real): 手动确认闸 → 确认后才落库 (绝不无确认打出去)

本模块补足「BetPlan → 可执行/已结算」之间的执行层地基：
  - settle_intent():  纯函数, 单注盈亏计算 (支持 1X2 / 大小球 / 通用 won 映射)
  - SimExecutor:      模拟自动结算, 经 database SSoT 落库
  - ManualConfirmationGate: 真实盘手动确认闸 (提交待确认 / 确认落库 / 拒绝),
                            内存 pending store; 真实注 **绝不** 在无明确确认时落库

铁律 (第一性原理)：
  - 只消费 #19 的 BetPlan (pipeline.strategy.BetPlan), 不重造组合逻辑
  - 注码已含在 BetPlan.intent.stake 中 (经 bet_core.safe_stake 封顶), 执行层不再算注码
  - 落库唯一事实源 = database.Database (bets 表, 含 mode 列区分 sim/real)
  - 真实下单必须人工确认: gate.confirm() 是落库唯一入口
  - 仅依赖标准库 + database + pipeline.strategy; 无 numpy / 无重依赖
"""
from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ── 投注库 SSoT (database.Database) ──
try:
    from database import Database
    _HAS_DB = True
except Exception:  # pragma: no cover
    Database = None
    _HAS_DB = False

# ── 组合层输出 (P0 #19) ──
from pipeline.strategy import BetIntent, BetPlan


# ════════════════════════════════════════════════════════════════
# 1. 结算核心 (纯函数, 可单测)
# ════════════════════════════════════════════════════════════════

@dataclass
class SettlementResult:
    """单注结算结果。"""
    mid: str
    home: str
    away: str
    selection: str
    odds: float
    stake: float
    result: str          # win / loss / void / pending
    pnl: float
    mode: str = "sim"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mid": self.mid, "home": self.home, "away": self.away,
            "selection": self.selection, "odds": round(self.odds, 3),
            "stake": round(self.stake, 2), "result": self.result,
            "pnl": round(self.pnl, 2), "mode": self.mode,
        }


def _parse_ou(selection: str) -> Optional[tuple]:
    """从选择串解析大小球方向与盘口线。

    支持 "Over 2.5" / "Under 3.5" / "大 2.5" / "小 3.5"。
    返回 ("Over"/"Under", line:float) 或 None。
    """
    s = selection.strip().lower()
    try:
        if "over" in s or "大" in s:
            # 取最后一个浮点数
            nums = [float(t) for t in s.replace("大", " ").split() if _is_num(t)]
            if nums:
                return ("Over", nums[-1])
        if "under" in s or "小" in s:
            nums = [float(t) for t in s.replace("小", " ").split() if _is_num(t)]
            if nums:
                return ("Under", nums[-1])
    except Exception:
        return None
    return None


def _is_num(t: str) -> bool:
    try:
        float(t)
        return True
    except Exception:
        return False


def _is_win(intent: BetIntent, actual: Dict[str, Any]) -> Optional[bool]:
    """判断单注是否命中 (None = 无法判定 → 留 pending)。"""
    sel = intent.selection.strip().upper()
    # 1X2
    if "winner" in actual:
        w = str(actual["winner"]).strip().upper()
        return (sel == w)
    # 子市场: 直接给 won 布尔 (最可靠)
    if "won" in actual:
        return bool(actual["won"])
    # 子市场: 给比分 → 大小球可判定
    if "home_goals" in actual and "away_goals" in actual and intent.market.upper() in ("OU", "O/U", "OVER/UNDER"):
        try:
            hg = int(actual["home_goals"]); ag = int(actual["away_goals"])
            parsed = _parse_ou(intent.selection)
            if parsed:
                direction, line = parsed
                total = hg + ag
                return (total > line) if direction == "Over" else (total < line)
        except Exception:
            return None
    return None


def settle_intent(intent: BetIntent, actual: Dict[str, Any]) -> SettlementResult:
    """计算单注盈亏 (纯函数)。

    Args:
        intent: BetPlan 中的单注
        actual: 赛果, 支持三种形态:
                {"winner": "H"/"D"/"A"}        (1X2)
                {"won": True/False}            (子市场直接判定)
                {"home_goals": int, "away_goals": int}  (大小球靠比分判定)
    Returns:
        SettlementResult (result ∈ win/loss/pending)
    """
    win = _is_win(intent, actual)
    if win is None:
        return SettlementResult(
            mid=intent.mid, home=intent.home, away=intent.away,
            selection=intent.selection, odds=intent.odds, stake=intent.stake,
            result="pending", pnl=0.0,
        )
    pnl = intent.stake * (intent.odds - 1) if win else -intent.stake
    return SettlementResult(
        mid=intent.mid, home=intent.home, away=intent.away,
        selection=intent.selection, odds=intent.odds, stake=intent.stake,
        result="win" if win else "loss", pnl=pnl,
    )


def expected_pnl(intent: BetIntent) -> float:
    """理论期望盈亏 (model_prob 加权), 用于模拟 dry-run 摘要。"""
    return intent.model_prob * intent.stake * (intent.odds - 1) - (1 - intent.model_prob) * intent.stake


# ════════════════════════════════════════════════════════════════
# 2. 模拟执行器 (sim: 自动执行 + 自动结算)
# ════════════════════════════════════════════════════════════════

def _get_singleton_db() -> "Database":
    from database import db as _singleton
    return _singleton


class SimExecutor:
    """模拟盘执行器: 给定 BetPlan + 赛果 → 自动结算并落库。

    - results 提供 → 逐注结算写库 (result=win/loss, pnl 真实)
    - results 未提供 → dry-run, 仅返回理论期望摘要, **不写库** (避免污染)
    """

    def __init__(self, db: Optional["Database"] = None):
        self.db = db

    def execute(
        self,
        plan: BetPlan,
        results: Optional[Dict[str, Dict[str, Any]]] = None,
        db: Optional["Database"] = None,
    ) -> Dict[str, Any]:
        db = db or self.db or (_get_singleton_db() if _HAS_DB else None)
        results = results or {}
        has_results = bool(results)

        settled: List[Dict[str, Any]] = []
        total_pnl = 0.0
        wins = losses = 0
        exp_pnl = 0.0

        for it in plan.intents:
            exp_pnl += expected_pnl(it)
            actual = results.get(it.mid)
            if has_results and actual:
                sr = settle_intent(it, actual)
                if db is not None:
                    bid = db.add_bet(
                        match=f"{it.home} vs {it.away}", outcome=it.selection,
                        odds=it.odds, stake=it.stake, result=sr.result,
                        pnl=sr.pnl, kelly=it.kelly_frac, ev=it.edge_pct / 100.0,
                        mode="sim",
                    )
                    sr_dict = sr.to_dict()
                    sr_dict["bet_id"] = bid
                    settled.append(sr_dict)
                else:
                    settled.append(sr.to_dict())
                if sr.result == "win":
                    wins += 1; total_pnl += sr.pnl
                elif sr.result == "loss":
                    losses += 1; total_pnl += sr.pnl
            else:
                # dry-run: 不写库
                settled.append({
                    "mid": it.mid, "home": it.home, "away": it.away,
                    "selection": it.selection, "odds": round(it.odds, 3),
                    "stake": round(it.stake, 2), "result": "pending",
                    "pnl": 0.0, "mode": "sim",
                })

        summary = {
            "bet_count": len(plan.intents),
            "settled": wins + losses,
            "wins": wins, "losses": losses,
            "total_pnl": round(total_pnl, 2),
            "expected_pnl": round(exp_pnl, 2),
            "roi_pct": round(total_pnl / plan.total_stake * 100, 2) if plan.total_stake else 0.0,
            "dry_run": not has_results,
        }
        out: Dict[str, Any] = {"mode": "sim", "settled": settled, "summary": summary}
        if db is not None:
            out["equity"] = db.equity()
        return out


# ════════════════════════════════════════════════════════════════
# 3. 手动确认闸 (real: 提交待确认 → 确认才落库)
# ════════════════════════════════════════════════════════════════

@dataclass
class _PendingReal:
    plan_id: str
    plan: BetPlan
    bankroll: float
    created_at: float


class ManualConfirmationGate:
    """真实盘手动确认闸 (进程内 pending store)。

    真实注 **绝不** 在 submit 时落库 —— 必须经 confirm() 显式确认后才写 database。
    单进程服务下内存 store 足够; 多进程/持久化需求后续可改 DB 支撑。
    """

    def __init__(self):
        self._pending: Dict[str, _PendingReal] = {}

    def submit(self, plan: BetPlan, bankroll: float = 3000.0) -> str:
        """提交待确认 (不落库, 不执行)。返回 plan_id。"""
        pid = uuid.uuid4().hex[:12]
        self._pending[pid] = _PendingReal(
            plan_id=pid, plan=plan, bankroll=bankroll, created_at=time.time(),
        )
        return pid

    def confirm(self, plan_id: str, db: Optional["Database"] = None) -> Dict[str, Any]:
        """确认 → 逐注落库 (result=pending, 待后续结算)。返回回执。"""
        pr = self._pending.pop(plan_id, None)
        if pr is None:
            return {"ok": False, "message": "plan_id 不存在或已处理"}
        db = db or (_get_singleton_db() if _HAS_DB else None)
        bet_ids: List[int] = []
        if db is not None:
            for it in pr.plan.intents:
                bid = db.add_bet(
                    match=f"{it.home} vs {it.away}", outcome=it.selection,
                    odds=it.odds, stake=it.stake, result="pending", pnl=0.0,
                    kelly=it.kelly_frac, ev=it.edge_pct / 100.0, mode="real",
                )
                bet_ids.append(bid)
        return {
            "ok": True,
            "plan_id": plan_id,
            "written": len(bet_ids),
            "bet_ids": bet_ids,
            "total_stake": pr.plan.total_stake,
            "equity": db.equity() if db is not None else None,
        }

    def reject(self, plan_id: str) -> Dict[str, Any]:
        """拒绝 → 丢弃 (不落库)。"""
        existed = plan_id in self._pending
        self._pending.pop(plan_id, None)
        return {"ok": True, "rejected": existed}

    def list_pending(self) -> List[Dict[str, Any]]:
        out = []
        for pr in self._pending.values():
            out.append({
                "plan_id": pr.plan_id,
                "created_at": pr.created_at,
                "bankroll": pr.bankroll,
                "total_stake": pr.plan.total_stake,
                "bet_count": len(pr.plan.intents),
                "intents": [
                    {"mid": b.mid, "home": b.home, "away": b.away,
                     "selection": b.selection, "odds": round(b.odds, 3),
                     "stake": round(b.stake, 2)}
                    for b in pr.plan.intents
                ],
            })
        return out


# ════════════════════════════════════════════════════════════════
# 4. 进程内单例 (供 bridge_service 调用; 状态跨请求保留)
# ════════════════════════════════════════════════════════════════

_SIM = SimExecutor()
_GATE = ManualConfirmationGate()


__all__ = [
    "SettlementResult", "settle_intent", "expected_pnl",
    "SimExecutor", "ManualConfirmationGate",
    "_SIM", "_GATE",
]

# -*- coding: utf-8 -*-
"""自动操盘引擎 — 串联 行情→扫描→注码→下单→结算 的完整闭环.

这是系统的「自动驾驶仪」. 两种运行模式:
  - 自动扫描 (run_scan_cycle): 拉真实在跑比赛 → 扫描 → 模拟下单 → 结算
  - 历史回放 (simulate_history): 用 30万行历史可结算数据重放 → 资金曲线 (体现系统价值)

复用 (绝不重造):
  - quant_demo.portfolio.Portfolio  (资金曲线/回撤/夏普)
  - scripts.bet_core.safe_stake     (凯利注码 SSoT)
  - quant_engine.scanner.scan       (全市场价值扫描)
  - quant_engine.market_feeder      (真实行情接入)
"""
from __future__ import annotations
import time, uuid, threading, logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from . import market_feeder as feeder
from .market_feeder import MatchMarket, BookOdds
from .scanner import scan, ScanResult, OptionValuation
from quant_demo.portfolio import Portfolio
from quant_demo.types import Position, StrategyMeta  # 复用既有数据类

# 策略层 (复用 workbuddy 的策略注册表, 数学纯函数, 接真实数据)
from quant_demo import strategies as _strategies
from quant_demo.types import SyntheticMatch

# 注码 SSoT
from scripts.bet_core import safe_stake, _load_betting_config

log = logging.getLogger("quant_engine")
_IDX = {"H": 0, "D": 1, "A": 2}
_SEL_DIR = {"主胜": "H", "平局": "D", "客胜": "A"}


@dataclass
class Order:
    """待确认/已结算订单 (内存态)."""
    oid: str
    mid: str
    home: str
    away: str
    market: str
    selection: str
    direction: str         # H/D/A (1X2方向; 子市场为对应结果)
    odds: float
    stake: float
    equity_before: float
    model_prob: float
    edge_pct: float
    ev_pct: float
    confidence: float      # = ev_pct/100
    mode: str = "sim"      # sim=模拟自动结算 / live=人工确认
    created_at: str = ""
    strategy_id: str = "value_layer"   # 触发策略
    strategy_name: str = "价值层"
    # 结算后填充
    settled: bool = False
    win: Optional[bool] = None
    pnl: Optional[float] = None
    equity_after: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "oid": self.oid, "mid": self.mid, "home": self.home, "away": self.away,
            "market": self.market, "selection": self.selection, "direction": self.direction,
            "odds": round(self.odds, 3), "stake": round(self.stake, 2),
            "equity_before": round(self.equity_before, 2),
            "model_prob": round(self.model_prob, 4), "edge_pct": round(self.edge_pct, 2),
            "ev_pct": round(self.ev_pct, 2), "confidence": round(self.confidence, 3),
            "mode": self.mode, "created_at": self.created_at,
            "strategy_id": self.strategy_id, "strategy_name": self.strategy_name,
            "settled": self.settled, "win": self.win,
            "pnl": round(self.pnl, 2) if self.pnl is not None else None,
            "equity_after": round(self.equity_after, 2) if self.equity_after is not None else None,
        }


class QuantEngine:
    """量化自动操盘引擎 (单例)."""

    def __init__(self, init_bankroll: float = 3000.0):
        cfg = _load_betting_config()
        self.init_bankroll = float(cfg.get("bankroll", init_bankroll))
        self.frac_kelly = float(cfg.get("frac_kelly", 0.5))
        self.max_stake_frac = float(cfg.get("max_stake_frac", 0.10))
        self.pf = Portfolio(self.init_bankroll)
        self.orders: List[Order] = []
        self.pending: List[Order] = []          # 待确认 (live模式)
        self.scans: List[ScanResult] = []       # 最近扫描结果 (前端展示)
        self.signal_log: List[Dict[str, Any]] = []  # 实时信号流
        self.auto_mode = False
        self._lock = threading.Lock()
        self.bet_count = 0
        # 策略开关状态 (复用 workbuddy 策略注册表, 接真实数据)
        self.strategy_metas: List[StrategyMeta] = [
            StrategyMeta(m.id, m.name, m.desc, enabled=True) for m in _strategies.META
        ]

    @property
    def enabled_strategy_ids(self) -> List[str]:
        return [m.id for m in self.strategy_metas if m.enabled]

    def toggle_strategy(self, strategy_id: str, enabled: bool) -> Dict[str, Any]:
        for m in self.strategy_metas:
            if m.id == strategy_id:
                m.enabled = enabled
        return {"strategies": [
            {"id": m.id, "name": m.name, "desc": m.desc, "enabled": m.enabled}
            for m in self.strategy_metas
        ]}

    # ── 注码: 委托 bet_core.safe_stake ──
    def _calc_stake(self, p: float, odds: float) -> float:
        stake, _k = safe_stake(
            p, odds, self.pf.equity,
            frac_kelly=self.frac_kelly, max_frac=self.max_stake_frac,
            source="quant_engine", gate=True, bet_count=self.bet_count,
        )
        return round(stake, 2)

    def _log_signal(self, msg: str, level: str = "info", **extra):
        """记一条信号到流 (前端实时消费)."""
        entry = {"ts": time.strftime("%H:%M:%S"), "level": level, "msg": msg, **extra}
        self.signal_log.append(entry)
        if len(self.signal_log) > 200:
            self.signal_log = self.signal_log[-200:]

    # ── 策略适配器: 把真实 MatchMarket 包装成 SyntheticMatch 供策略层消费 ──
    def _to_synthetic(self, m: MatchMarket) -> SyntheticMatch:
        """真实比赛 → SyntheticMatch (策略层只读 books/best_odds/consensus_prob)."""
        from pipeline.deep_report import consensus_probs
        books_dicts = [{"h": b.h, "d": b.d, "a": b.a} for b in m.books]
        cons = consensus_probs(books_dicts) if m.is_multi_book else \
               [1/o/(1/m.best_h2h[0]+1/m.best_h2h[1]+1/m.best_h2h[2]) for o in m.best_h2h]
        return SyntheticMatch(
            mid=m.mid, home=m.home, away=m.away, league=m.league,
            books=books_dicts, best_odds=list(m.best_h2h),
            consensus_prob=list(cons), scenarios=[], winner=m.actual_result or "D",
        )

    # ── 核心: 对一场比赛扫描 + 策略信号 + 生成订单 ──
    def process_match(self, m: MatchMarket, mode: str = "sim",
                      top_n: int = 3) -> Dict[str, Any]:
        """扫描一场比赛 → 跑策略层 → 对 BET 候选生成订单 → (sim)自动结算."""
        sr = scan(m, bankroll=self.pf.equity, frac_kelly=self.frac_kelly)
        self.scans.append(sr)
        if len(self.scans) > 50:
            self.scans = self.scans[-50:]

        # 跑策略层 (真实数据驱动)
        sigs = []
        if m.is_multi_book:
            try:
                sm = self._to_synthetic(m)
                sigs = _strategies.generate_signals(sm, self.enabled_strategy_ids)
            except Exception as ex:
                log.debug(f"策略层异常 {m.home} vs {m.away}: {ex}")

        # 策略 BET 方向集合
        bet_dirs = {s.direction: s for s in sigs if s.decision == "BET" and s.direction}

        new_orders = []
        # 优先: 策略命中的方向 (多庄才可能有策略信号)
        for direction, sig in bet_dirs.items():
            idx = _IDX[direction]
            opt = next((o for o in sr.options if o.market == "标准胜平负"
                        and _SEL_DIR.get(o.selection) == direction), None)
            if not opt or opt.odds <= 0 or opt.kelly_half <= 0:
                continue
            stake = self._calc_stake(opt.model_prob, opt.odds)
            if stake <= 0:
                continue
            o = Order(
                oid=str(uuid.uuid4())[:8], mid=m.mid, home=m.home, away=m.away,
                market=opt.market, selection=opt.selection, direction=direction,
                odds=opt.odds, stake=stake, equity_before=self.pf.equity,
                model_prob=opt.model_prob, edge_pct=opt.edge_pct, ev_pct=opt.ev_pct,
                confidence=round(opt.ev_pct / 100.0, 3), mode=mode,
                created_at=time.strftime("%H:%M:%S"),
            )
            o.strategy_id = sig.strategy_id
            o.strategy_name = sig.strategy_name
            self.orders.append(o)
            new_orders.append(o)
            self._log_signal(
                f"下单 · {m.home} vs {m.away} [{sig.strategy_name}/{opt.selection}] "
                f"edge+{opt.edge_pct:.1f}% EV+{opt.ev_pct:.1f}% 注¥{stake:.0f}",
                level="order",
                oid=o.oid, market=opt.market, selection=opt.selection,
                stake=stake, edge_pct=opt.edge_pct,
            )
            if mode == "sim" and m.actual_result:
                self._settle_order(o, m.actual_result, m.actual_score)
            elif mode == "live":
                self.pending.append(o)

        # 回退: 无策略信号时用价值层 top 候选 (仅多庄)
        if not new_orders and m.is_multi_book and mode != "live":
            for opt in sr.bet_candidates[:top_n]:
                if opt.odds <= 0 or opt.kelly_half <= 0:
                    continue
                stake = self._calc_stake(opt.model_prob, opt.odds)
                if stake <= 0:
                    continue
                direction = self._sel_to_dir(opt)
                o = Order(
                    oid=str(uuid.uuid4())[:8], mid=m.mid, home=m.home, away=m.away,
                    market=opt.market, selection=opt.selection, direction=direction,
                    odds=opt.odds, stake=stake, equity_before=self.pf.equity,
                    model_prob=opt.model_prob, edge_pct=opt.edge_pct, ev_pct=opt.ev_pct,
                    confidence=round(opt.ev_pct / 100.0, 3), mode=mode,
                    created_at=time.strftime("%H:%M:%S"),
                )
                self.orders.append(o)
                new_orders.append(o)
                self._log_signal(
                    f"下单 · {m.home} vs {m.away} [{opt.market}/{opt.selection}] "
                    f"edge+{opt.edge_pct:.1f}% EV+{opt.ev_pct:.1f}% 注¥{stake:.0f}",
                    level="order",
                    oid=o.oid, market=opt.market, selection=opt.selection,
                    stake=stake, edge_pct=opt.edge_pct,
                )
                if mode == "sim" and m.actual_result:
                    self._settle_order(o, m.actual_result, m.actual_score)

        if not new_orders:
            n_bets = len(bet_dirs)
            self._log_signal(
                f"扫描 · {m.home} vs {m.away} → {len(sr.options)}选项 "
                f"{n_bets}个策略信号 无下单 (PASS)" if m.is_multi_book else
                f"扫描 · {m.home} vs {m.away} → {len(sr.options)}选项 单庄(仅EVAL不下单)",
                level="scan", n_bets=0,
            )
        return {
            "match": {"mid": m.mid, "home": m.home, "away": m.away, "league": m.league,
                      "is_live": m.is_live, "actual_result": m.actual_result},
            "scan": sr.to_dict(),
            "signals": [{"strategy_id": s.strategy_id, "strategy_name": s.strategy_name,
                         "decision": s.decision, "direction": s.direction,
                         "edge_pct": s.edge_pct, "ev_pct": s.ev_pct, "note": s.note}
                        for s in sigs],
            "new_orders": [o.to_dict() for o in new_orders],
        }

    def _sel_to_dir(self, opt: OptionValuation) -> str:
        """选项 → 结算方向 (H/D/A)."""
        sel = opt.selection
        if sel in _SEL_DIR:
            return _SEL_DIR[sel]
        if "主胜" in sel or "主" in sel and "让" in opt.market:
            return "H"
        if "平" in sel:
            return "D"
        if "客胜" in sel or "客" in sel:
            return "A"
        return "H"

    def _settle_order(self, o: Order, actual: str, score: Optional[str]):
        """结算订单 (用真实赛果). actual: H/D/A."""
        win = (o.direction == actual)
        pnl = o.stake * (o.odds - 1) if win else -o.stake
        self.pf.equity = round(self.pf.equity + pnl, 2)
        o.settled = True
        o.win = win
        o.pnl = round(pnl, 2)
        o.equity_after = self.pf.equity
        self.pf.positions.append(Position(
            oid=o.oid, mid=o.mid, home=o.home, away=o.away,
            strategy_id="quant", direction=o.direction, odds=o.odds,
            stake=o.stake, win=win, pnl=round(pnl, 2), equity_after=self.pf.equity,
        ))
        self.pf.record_equity(note=f"{o.home} vs {o.away} [{o.selection}] {'赢' if win else '输'}")
        self.bet_count += 1
        self._log_signal(
            f"结算 · {o.home} vs {o.away} [{o.selection}] {'✅赢' if win else '❌输'} "
            f"{pnl:+.0f} → 权益¥{self.pf.equity:.0f}",
            level="settle" if win else "loss", oid=o.oid, win=win, pnl=pnl,
        )

    # ── 自动扫描周期 (拉真实在跑比赛) ──
    def run_scan_cycle(self, limit: int = 20, mode: str = "sim") -> Dict[str, Any]:
        """跑一个自动扫描周期: 拉真实比赛 → 逐场扫描下单."""
        with self._lock:
            matches = feeder.load_live_matches(limit=limit)
            results = []
            for m in matches:
                r = self.process_match(m, mode=mode)
                results.append(r)
            return {
                "ok": True, "scanned": len(matches),
                "account": self.pf.stats(),
                "results": results,
            }

    # ── 历史回放 (体现系统价值的核心) ──
    def simulate_history(self, n_matches: int = 100, mode: str = "sim") -> Dict[str, Any]:
        """历史回放: 用 odds_features 双庄可结算数据重放 → 资金曲线.

        30万行历史, 每行含真实收盘赔率 + 赛果. 按时间倒序取最近 n 场.
        这是「如果系统一直在跑, 收益曲线长什么样」的诚实答案.
        """
        with self._lock:
            matches = feeder.load_history_matches(limit=n_matches, multi_book_only=True)
            # 按时间正序回放 (旧的先跑)
            matches.reverse()
            settled = 0
            wins = 0
            for m in matches:
                r = self.process_match(m, mode="sim")
                for o in r["new_orders"]:
                    if o.get("settled"):
                        settled += 1
                        if o.get("win"):
                            wins += 1
            self._log_signal(
                f"历史回放完成 · {len(matches)}场 下注{settled}注 赢{wins} "
                f"→ 权益¥{self.pf.equity:.0f} ({self.pf.stats()['return_pct']:+.1f}%)",
                level="replay",
            )
            return {
                "ok": True, "matches": len(matches), "settled": settled, "wins": wins,
                "account": self.pf.stats(),
                "equity_curve": self.pf.equity_curve,
            }

    # ── 手动单场深度分析 (对应图片) ──
    def analyze_single(self, m: MatchMarket) -> Dict[str, Any]:
        """手动单场扫描 (不下单, 只输出价值排序)."""
        sr = scan(m, bankroll=self.pf.equity, frac_kelly=self.frac_kelly)
        self.scans.append(sr)
        self._log_signal(
            f"单场分析 · {m.home} vs {m.away} → {len(sr.options)}选项 {len(sr.bet_candidates)}个价值候选",
            level="analyze", home=m.home, away=m.away, n_bets=len(sr.bet_candidates),
        )
        return sr.to_dict()

    # ── 状态快照 (前端主数据源) ──
    def get_snapshot(self) -> Dict[str, Any]:
        return {
            "account": self.pf.stats(),
            "equity_curve": self.pf.equity_curve[-100:],
            "positions": [self._pos_dict(p) for p in self.pf.positions[-30:]],
            "pending": [o.to_dict() for o in self.pending],
            "recent_orders": [o.to_dict() for o in self.orders[-30:]],
            "signals": self.signal_log[-50:],
            "recent_scans": [s.to_dict() for s in self.scans[-10:]],
            "strategies": [
                {"id": m.id, "name": m.name, "desc": m.desc, "enabled": m.enabled}
                for m in self.strategy_metas
            ],
            "auto_mode": self.auto_mode,
            "bet_count": self.bet_count,
        }

    @staticmethod
    def _pos_dict(p) -> Dict[str, Any]:
        return {"oid": p.oid, "home": p.home, "away": p.away, "direction": p.direction,
                "odds": p.odds, "stake": p.stake, "win": p.win, "pnl": p.pnl,
                "equity_after": p.equity_after}

    # ── live 模式人工确认 ──
    def confirm_order(self, oid: str, actual: str = "D") -> Dict[str, Any]:
        for o in self.pending:
            if o.oid == oid:
                self._settle_order(o, actual, None)
                self.pending.remove(o)
                return {"ok": True, "order": o.to_dict(), "account": self.pf.stats()}
        return {"ok": False, "message": "订单不存在"}

    def reset(self, init_bankroll: Optional[float] = None):
        ib = init_bankroll or self.init_bankroll
        self.__init__(init_bankroll=ib)
        self._log_signal(f"账户重置 · 本金¥{ib:.0f}", level="info")


# ── 模块级单例 ────────────────────────────────────────────────

_ENGINE: Optional[QuantEngine] = None


def get_engine() -> QuantEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = QuantEngine()
    return _ENGINE

"""pipeline/bet_ledger.py — 投注账本 SSoT (P0b, 2026-08-01)

为什么存在
----------
此前全系统"下注不落账": live_pilot 模拟 / quant 终端 / 手动确认真实下单,
都只在运行时产生 BetResult, 不持久化; 实盘 ROI 只能靠"事后结算单截图 OCR"
(settlement_roi.py) 滞后标定。本模块建立"下注即记账"的结构化账本, 实现持续、
可归因的盈亏追踪。

铁律
----
- 本模块是投注账本的唯一事实源(SSoT)。任何下注记录/结算/ROI 统计只在此一处,
  禁止在 deep_report / quant / bridge 平行重造。
- mode: sim(模拟自动) / real(手动确认真实下单)。用户工作流 = 模拟为主 + 手动确认真实下单。
- 幂等: bet_id 唯一(默认 match|market|selection|book|placed_at), 重复记录 INSERT OR IGNORE 拒写。
- 结算只认 status: open→won/lost/push/void; 绝不只信 win_loss 文本(沿用结算铁律)。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

_DB_PATH = os.path.join("data", "bets.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bet_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bet_id TEXT UNIQUE NOT NULL,
  match_id TEXT, home TEXT, away TEXT, league TEXT, kickoff TEXT,
  market TEXT,              -- 1X2 / OU / AH / CS
  selection TEXT,           -- H/D/A 或 O/U 或比分
  book TEXT,                -- 庄家
  odds REAL,
  model_prob REAL,          -- 模型概率
  market_prob REAL,         -- 市场隐含(单庄去水 或 跨庄共识)
  edge_pp REAL,             -- 价值 edge (pp)
  spread_pp REAL,           -- 跨庄价差 pp (P1a)
  severity TEXT,            -- HIGH/MED/LOW (跨庄分级)
  ev_pct REAL, kelly REAL,
  stake REAL, bankroll_at REAL,
  decision TEXT,            -- BET
  gate_flags TEXT,          -- JSON: 各闸门状态 {hot_capped, spread_gate, loss_cooldown, ...}
  mode TEXT DEFAULT 'sim',  -- sim / real
  status TEXT DEFAULT 'open', -- open / won / lost / push / void
  actual_result TEXT, actual_score TEXT,
  pnl REAL, roi_pct REAL,
  source TEXT,              -- live_pilot / quant_engine / bridge_manual / ...
  placed_at REAL, settled_at REAL, notes TEXT,
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ledger_status ON bet_ledger(status);
CREATE INDEX IF NOT EXISTS idx_ledger_mode ON bet_ledger(mode);
CREATE INDEX IF NOT EXISTS idx_ledger_market ON bet_ledger(market);
CREATE INDEX IF NOT EXISTS idx_ledger_placed ON bet_ledger(placed_at);
"""


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or _DB_PATH
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def record_bet(*, bet_id: Optional[str] = None,
               match_id: Optional[str] = None, home: Optional[str] = None,
               away: Optional[str] = None, league: Optional[str] = None,
               kickoff: Optional[str] = None,
               market: Optional[str] = None, selection: Optional[str] = None,
               book: Optional[str] = None, odds: Optional[float] = None,
               model_prob: Optional[float] = None, market_prob: Optional[float] = None,
               edge_pp: Optional[float] = None, spread_pp: Optional[float] = None,
               severity: Optional[str] = None, ev_pct: Optional[float] = None,
               kelly: Optional[float] = None, stake: Optional[float] = None,
               bankroll_at: Optional[float] = None, decision: str = "BET",
               gate_flags: Optional[Dict[str, Any]] = None, mode: str = "sim",
               source: str = "", placed_at: Optional[float] = None,
               notes: str = "", db_path: Optional[str] = None) -> str:
    """记录一笔下注(幂等)。返回 bet_id。重复 bet_id INSERT OR IGNORE 拒写。"""
    init_db(db_path)
    if placed_at is None:
        placed_at = time.time()
    if not bet_id:
        bet_id = f"{match_id or (str(home) + '|' + str(away))}|{market}|{selection}|{book}|{int(placed_at)}"
    conn = _connect(db_path)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO bet_ledger
            (bet_id, match_id, home, away, league, kickoff, market, selection, book,
             odds, model_prob, market_prob, edge_pp, spread_pp, severity, ev_pct, kelly,
             stake, bankroll_at, decision, gate_flags, mode, status, source,
             placed_at, notes, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (bet_id, match_id, home, away, league, kickoff, market, selection, book,
             odds, model_prob, market_prob, edge_pp, spread_pp, severity, ev_pct, kelly,
             stake, bankroll_at, decision,
             json.dumps(gate_flags or {}, ensure_ascii=False), mode, 'open', source,
             placed_at, notes, time.time()))
        conn.commit()
    finally:
        conn.close()
    return bet_id


def settle_bet(bet_id: str, *, won: Optional[bool] = None,
               actual_result: Optional[str] = None, actual_score: Optional[str] = None,
               payout: Optional[float] = None, db_path: Optional[str] = None) -> bool:
    """结算一笔。won=True/False 或直接给 payout(派彩总额)。算 pnl/roi。返回是否成功。"""
    init_db(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT stake, odds, status FROM bet_ledger WHERE bet_id=?",
                           (bet_id,)).fetchone()
        if not row or row["status"] != "open":
            return False  # 不存在 或 已结算(幂等, 不重复结算)
        stake = float(row["stake"] or 0.0)
        odds = float(row["odds"] or 0.0)
        if payout is not None:
            pnl = float(payout) - stake
            status = "won" if pnl > 0 else ("push" if pnl == 0 else "lost")
        elif won is True:
            pnl = stake * (odds - 1.0)
            status = "won"
        elif won is False:
            pnl = -stake
            status = "lost"
        else:
            return False
        roi = (pnl / stake * 100.0) if stake > 0 else 0.0
        conn.execute(
            """UPDATE bet_ledger SET status=?, actual_result=?, actual_score=?,
               pnl=?, roi_pct=?, settled_at=? WHERE bet_id=?""",
            (status, actual_result, actual_score, round(pnl, 2), round(roi, 2),
             time.time(), bet_id))
        conn.commit()
        return True
    finally:
        conn.close()


def roi_stats(mode: Optional[str] = None, market: Optional[str] = None,
              severity: Optional[str] = None, db_path: Optional[str] = None) -> Dict[str, Any]:
    """ROI 统计(仅已结算单 won/lost/push): 总体 + 按 mode/market/severity 分组。"""
    init_db(db_path)
    conn = _connect(db_path)
    try:
        where = ["status IN ('won','lost','push')"]
        args: List[Any] = []
        if mode:
            where.append("mode=?"); args.append(mode)
        if market:
            where.append("market=?"); args.append(market)
        if severity:
            where.append("severity=?"); args.append(severity)
        rows = conn.execute(
            f"SELECT * FROM bet_ledger WHERE {' AND '.join(where)}", args).fetchall()
    finally:
        conn.close()

    def agg(rs) -> Dict[str, Any]:
        st = sum(r["stake"] or 0.0 for r in rs)
        net = sum(r["pnl"] or 0.0 for r in rs)
        nw = sum(1 for r in rs if r["status"] == "won")
        return {"n": len(rs), "n_win": nw, "n_loss": len(rs) - nw,
                "stake": round(st, 2), "net": round(net, 2),
                "roi_pct": round(net / st * 100, 2) if st > 0 else 0.0,
                "hit_pct": round(nw / len(rs) * 100, 2) if rs else 0.0}

    out: Dict[str, Any] = {"overall": agg(rows)}
    for dim in ("mode", "market", "severity"):
        groups: Dict[str, list] = {}
        for r in rows:
            groups.setdefault(r[dim] or "?", []).append(r)
        out[f"by_{dim}"] = {k: agg(v) for k, v in groups.items()}
    return out


def list_open(db_path: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    """列出未结算(open)单。"""
    init_db(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM bet_ledger WHERE status='open' ORDER BY placed_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def summary_text(db_path: Optional[str] = None) -> str:
    """控制台可读 ROI 摘要。"""
    s = roi_stats(db_path=db_path)
    o = s["overall"]
    lines = ["投注账本 ROI (仅已结算单)", "=" * 56,
             f"已结算 {o['n']} 单 (赢{o['n_win']}/输{o['n_loss']}) | "
             f"本金 {o['stake']} 净盈亏 {o['net']:+} | ROI {o['roi_pct']:+}% 命中 {o['hit_pct']}%"]
    for dim in ("by_mode", "by_market", "by_severity"):
        if s.get(dim):
            lines.append(f"[{dim}]")
            for k, v in s[dim].items():
                lines.append(f"  {k:<10} n={v['n']:<3} ROI {v['roi_pct']:+}% 命中 {v['hit_pct']}%")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary_text())

"""
quant_executor.py — 量化交易执行引擎
=========================================
端到端流程: model_decision → Kelly stake → pending order → confirm 闸 → settle

设计原则 (用户需求):
  1. 决策驱动 = 模型 (不是 Kelly) — strategy_id 关联 model_decision 表
  2. 凯利 = 工具 (注码计算) — 复用 scripts/bet_core.py 单一事实源
  3. 操盘手 = 复核 — operator_recommendations 持久化, OperatorTerminal 展示
  4. 风控 = 闸门 — 多庄分歧闸 + 单注封顶 + 容量 NO-GO

模式:
  paper: 模拟资金, pending 订单自动 confirm (灰度)
  live: 真实资金, pending 订单需人工弹窗 confirm (硬闸)

API 入口:
  - submit_decision()        接受模型决策 → 创建 position + order (pending)
  - confirm_order()          确认 pending 订单 → status=confirmed
  - reject_order()           拒绝 + 原因 → status=rejected
  - settle_match()           实际赛果 → status=settled + 资金划拨
  - get_performance()        资金曲线 + 策略绩效 + 银行账户
"""
from __future__ import annotations
import os, sys, json, sqlite3, logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "quant_trading.db")


def _conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    # timeout=10: 允许并发连接(如确认/拒绝时的嵌套占用释放)等待而非立即 database is locked
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════
# ① 资金账户 (单例)
# ═══════════════════════════════════════════════════════
def get_bankroll(db_path: str = DB_PATH) -> Dict[str, Any]:
    conn = _conn(db_path)
    row = conn.execute("SELECT * FROM bankroll WHERE id=1").fetchone()
    conn.close()
    return dict(row) if row else {}


def update_bankroll(pnl: float, db_path: str = DB_PATH) -> Dict[str, Any]:
    """结算后更新账户 (pnl 正=赢, 负=输)"""
    conn = _conn(db_path)
    cur = conn.cursor()
    br = cur.execute("SELECT * FROM bankroll WHERE id=1").fetchone()
    if not br:
        return {"error": "bankroll 单例未初始化"}
    new_balance = round(br["current_balance"] + pnl, 2)
    new_hwm = max(br["high_water_mark"], new_balance)
    new_dd = round((new_hwm - new_balance) / new_hwm * 100, 2) if new_hwm > 0 else 0.0
    is_win = 1 if pnl > 0 else 0
    is_loss = 1 if pnl < 0 else 0
    cur.execute(
        """UPDATE bankroll SET
              current_balance=?, high_water_mark=?, drawdown_pct=?,
              total_pnl=?, total_bets=total_bets+?,
              wins=wins+?, losses=losses+?, updated_at=?
           WHERE id=1""",
        (new_balance, new_hwm, new_dd,
         round(br["total_pnl"] + pnl, 2),
         1, is_win, is_loss, _now()),
    )
    conn.commit()
    updated = cur.execute("SELECT * FROM bankroll WHERE id=1").fetchone()
    conn.close()
    return dict(updated)


def reserve_balance(stake: float, db_path: str = DB_PATH) -> bool:
    """下单时占用资金 (待结算后释放)"""
    conn = _conn(db_path)
    cur = conn.cursor()
    br = cur.execute("SELECT * FROM bankroll WHERE id=1").fetchone()
    if not br or br["current_balance"] - br["reserved_balance"] < stake:
        conn.close()
        return False
    cur.execute(
        "UPDATE bankroll SET reserved_balance=reserved_balance+?, pending_count=pending_count+1, updated_at=? WHERE id=1",
        (stake, _now()),
    )
    conn.commit()
    conn.close()
    return True


def release_balance(stake: float, db_path: str = DB_PATH) -> None:
    """结算后释放占用"""
    conn = _conn(db_path)
    cur = conn.cursor()
    cur.execute(
        "UPDATE bankroll SET reserved_balance=MAX(0, reserved_balance-?), pending_count=MAX(0, pending_count-1), updated_at=? WHERE id=1",
        (stake, _now()),
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════
# ② 凯利注码 (委托 bet_core SSoT)
# ═══════════════════════════════════════════════════════
def compute_kelly_stake(model_prob: float, book_odds: float,
                        current_balance: float,
                        frac_kelly: float = 0.5,
                        max_stake_frac: float = 0.10) -> Dict[str, float]:
    """委托 bet_core 计算凯利注码
    Returns: {kelly_full, kelly_half, stake_pct, stake_amount, max_stake_cap}
    """
    sys.path.insert(0, PROJECT_ROOT)
    try:
        from scripts.bet_core import kelly_fraction, safe_stake
        k_full = kelly_fraction(model_prob, book_odds)
        k_half = max(0.0, k_full * frac_kelly)
        # safe_stake 返回 (stake, kelly) 元组; gate=True 启用下注
        stake, k_returned = safe_stake(
            p=model_prob, o=book_odds,
            equity=current_balance,
            frac_kelly=frac_kelly, max_frac=max_stake_frac,
            source="quant_executor", gate=True,
        )
        stake_pct = round(stake / current_balance, 4) if current_balance > 0 else 0.0
        return {
            "kelly_full": round(k_returned, 4) if k_returned else round(k_full, 4),
            "kelly_half": round(k_half, 4),
            "stake_pct": stake_pct,
            "stake_amount": round(stake, 2),
            "max_stake_cap": round(current_balance * max_stake_frac, 2),
        }
    except Exception as e:
        logger.error(f"凯利计算失败: {e}")
        return {"error": str(e), "kelly_full": 0, "kelly_half": 0, "stake_amount": 0, "stake_pct": 0}


# ═══════════════════════════════════════════════════════
# ③ 提交模型决策 → 创建 position + order (pending)
# ═══════════════════════════════════════════════════════
def submit_decision(decision: Dict[str, Any], db_path: str = DB_PATH) -> Dict[str, Any]:
    """
    接受模型决策 → 创建 position + order (pending)
    决策来源: bridge_service /api/predict/live 的 value_layer + operator_view

    Args:
        decision: {
            strategy_id, match_id, home_team, away_team, league, commence_time,
            direction ('H'/'D'/'A'), model_prob, market_prob,
            book_odds_h, book_odds_d, book_odds_a, book_odds (投注方向),
            edge_pct, expected_value, decision_text,
            sub_markets (dict), operator_action,
            confirmation_required (bool, default=True)
        }
    """
    try:
        conn = _conn(db_path)
        cur = conn.cursor()
        now = _now()

        # 1) 策略校验
        if decision.get("strategy_id"):
            strat = cur.execute(
                "SELECT status, parameters FROM strategies WHERE strategy_id=?",
                (decision["strategy_id"],)
            ).fetchone()
            if not strat:
                return {"error": f"未知策略: {decision.get('strategy_id')}"}
            if strat["status"] != "active":
                return {"error": f"策略非 active: {strat['status']}"}

        # 2) 资金校验
        br = cur.execute("SELECT * FROM bankroll WHERE id=1").fetchone()
        if not br:
            return {"error": "bankroll 单例未初始化"}
        available = br["current_balance"] - br["reserved_balance"]

        # 3) 凯利计算
        kelly = compute_kelly_stake(
            model_prob=decision["model_prob"],
            book_odds=decision["book_odds"],
            current_balance=available,
        )
        if "error" in kelly:
            return kelly
        stake_amount = kelly["stake_amount"]
        if stake_amount < 1.0:
            conn.close()
            return {"skipped": True, "reason": f"凯利注码过小 ¥{stake_amount:.2f} (<¥1.0) → 跳过", **kelly}

        # 4) 写 model_decision
        cur.execute(
            """INSERT INTO model_decisions
               (strategy_id, match_id, home_team, away_team, league, commence_time,
                direction, model_prob, confidence, market_prob,
                book_odds_h, book_odds_d, book_odds_a, book_odds,
                edge_pct, expected_value, decision_text, sub_markets, operator_action, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                decision.get("strategy_id"),
                decision.get("match_id"),
                decision["home_team"], decision["away_team"],
                decision.get("league", ""), decision.get("commence_time", ""),
                decision["direction"], decision["model_prob"],
                decision.get("confidence", 0.0), decision["market_prob"],
                decision.get("book_odds_h"), decision.get("book_odds_d"),
                decision.get("book_odds_a"), decision["book_odds"],
                decision["edge_pct"], decision["expected_value"],
                decision.get("decision_text", ""),
                json.dumps(decision.get("sub_markets", {}), ensure_ascii=False),
                decision.get("operator_action", ""),
                now,
            ),
        )
        decision_id = cur.lastrowid

        # 5) 写 order (pending)
        confirmation_required = decision.get("confirmation_required", True)
        cur.execute(
            """INSERT INTO orders
               (decision_id, bookmaker, side, book_odds, stake_amount,
                status, confirmation_required, created_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                decision_id, decision.get("bookmaker", ""),
                decision["direction"], decision["book_odds"], stake_amount,
                "pending", 1 if confirmation_required else 0,
                now, decision.get("expires_at", ""),
            ),
        )
        order_id = cur.lastrowid

        # 6) 写 position (pending)
        cur.execute(
            """INSERT INTO positions
               (decision_id, order_id, match_id, home_team, away_team, league, commence_time,
                side, book_odds, model_prob, market_prob, edge_pct,
                kelly_full, kelly_half, stake_pct, stake_amount, max_stake_cap,
                status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                decision_id, order_id, decision.get("match_id"),
                decision["home_team"], decision["away_team"],
                decision.get("league", ""), decision.get("commence_time", ""),
                decision["direction"], decision["book_odds"],
                decision["model_prob"], decision["market_prob"], decision["edge_pct"],
                kelly["kelly_full"], kelly["kelly_half"], kelly["stake_pct"],
                kelly["stake_amount"], kelly["max_stake_cap"],
                "pending", now,
            ),
        )
        position_id = cur.lastrowid

        # 7) 占用资金 — 在同一事务内用同一 cursor 完成, 避免嵌套连接写锁死锁
        #    (原实现在打开的外层事务里调用 reserve_balance() 另开连接写 bankroll → database is locked;
        #     live 模式人工确认闸路径会直接失败。已在 tests/test_quant_flow.py 固化回归。)
        if confirmation_required:
            if available < stake_amount:
                # 资金不足 → 标记 cancelled
                cur.execute("UPDATE positions SET status='cancelled', rejection_reason=? WHERE position_id=?",
                            ("可用资金不足", position_id))
                cur.execute("UPDATE orders SET status='rejected', rejection_reason=? WHERE order_id=?",
                            ("可用资金不足", order_id))
                conn.commit()
                conn.close()
                return {"error": "可用资金不足", "position_id": position_id, "order_id": order_id}
            cur.execute(
                "UPDATE bankroll SET reserved_balance=reserved_balance+?, pending_count=pending_count+1, updated_at=? WHERE id=1",
                (stake_amount, now))

        # 8) 写操盘手建议 (持久化)
        if decision.get("operator_view"):
            op = decision["operator_view"]
            cur.execute(
                """INSERT INTO operator_recommendations
                   (decision_id, match_id, home_team, away_team, league, commence_time,
                    stake_hint, confidence_pct, rules_fired, trap_score, trap_verdict,
                    verdict, operator_action, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id, decision.get("match_id"),
                    decision["home_team"], decision["away_team"],
                    decision.get("league", ""), decision.get("commence_time", ""),
                    op.get("stake_hint", ""), op.get("confidence_pct", 0.0),
                    json.dumps(op.get("rules_fired", []), ensure_ascii=False),
                    op.get("trap_score", 0.0), op.get("trap_verdict", ""),
                    op.get("verdict", ""), decision.get("operator_action", ""),
                    now,
                ),
            )

        # 9) paper 模式自动 confirm
        if not confirmation_required and br["mode"] == "paper":
            conn.commit()
            conn.close()
            return confirm_order(order_id, db_path=db_path)

        conn.commit()
        out = {
            "ok": True,
            "decision_id": decision_id,
            "order_id": order_id,
            "position_id": position_id,
            "stake_amount": stake_amount,
            "stake_pct": kelly["stake_pct"],
            "kelly_half": kelly["kelly_half"],
            "status": "pending" if confirmation_required else "auto_confirmed",
            "message": f"模型决策已入账, 待{'人工' if confirmation_required else '自动'}确认",
        }
        conn.close()
        return out
    except Exception as e:
        logger.error(f"submit_decision 失败: {e}", exc_info=True)
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════
# ④ 确认 / 拒绝 pending 订单
# ═══════════════════════════════════════════════════════
def confirm_order(order_id: int, db_path: str = DB_PATH) -> Dict[str, Any]:
    conn = _conn(db_path)
    cur = conn.cursor()
    o = cur.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if not o:
        conn.close()
        return {"error": f"订单不存在: {order_id}"}
    if o["status"] != "pending":
        conn.close()
        return {"error": f"订单状态非 pending: {o['status']}"}
    now = _now()
    cur.execute("UPDATE orders SET status='confirmed', confirmed_at=? WHERE order_id=?", (now, order_id))
    cur.execute("UPDATE positions SET status='confirmed', confirmed_at=? WHERE order_id=?", (now, order_id))
    pos = cur.execute("SELECT position_id FROM positions WHERE order_id=?", (order_id,)).fetchone()
    position_id = pos["position_id"] if pos else None
    conn.commit()
    conn.close()
    return {"ok": True, "order_id": order_id, "position_id": position_id, "status": "confirmed"}


def reject_order(order_id: int, reason: str = "人工拒绝", db_path: str = DB_PATH) -> Dict[str, Any]:
    conn = _conn(db_path)
    cur = conn.cursor()
    o = cur.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if not o:
        conn.close()
        return {"error": f"订单不存在: {order_id}"}
    if o["status"] != "pending":
        conn.close()
        return {"error": f"订单状态非 pending: {o['status']}"}
    now = _now()
    cur.execute("UPDATE orders SET status='rejected', rejection_reason=?, confirmed_at=? WHERE order_id=?",
                (reason, now, order_id))
    cur.execute("UPDATE positions SET status='cancelled', rejection_reason=?, confirmed_at=? WHERE order_id=?",
                (reason, now, order_id))
    # 释放占用 — 同一 cursor, 避免嵌套连接写锁死锁
    cur.execute(
        "UPDATE bankroll SET reserved_balance=MAX(0, reserved_balance-?), pending_count=MAX(0, pending_count-1), updated_at=? WHERE id=1",
        (o["stake_amount"], now))
    conn.commit()
    conn.close()
    return {"ok": True, "order_id": order_id, "status": "rejected", "reason": reason}


# ═══════════════════════════════════════════════════════
# ⑤ 结算: 实际赛果 → 资金划拨 + 绩效
# ═══════════════════════════════════════════════════════
def settle_match(position_id: int, actual_result: str, actual_score: str = "",
                 db_path: str = DB_PATH) -> Dict[str, Any]:
    """
    Args:
        actual_result: 'H' / 'D' / 'A'
    """
    conn = _conn(db_path)
    cur = conn.cursor()
    p = cur.execute("SELECT * FROM positions WHERE position_id=?", (position_id,)).fetchone()
    if not p:
        conn.close()
        return {"error": f"持仓不存在: {position_id}"}
    if p["status"] not in ("confirmed", "pending"):
        conn.close()
        return {"error": f"持仓状态不可结算: {p['status']}"}

    won = (p["side"] == actual_result)
    pnl = round(p["stake_amount"] * (p["book_odds"] - 1), 2) if won else round(-p["stake_amount"], 2)
    roi_pct = round(pnl / p["stake_amount"] * 100, 2) if p["stake_amount"] > 0 else 0.0
    now = _now()

    # 资金划拨
    br_before = cur.execute("SELECT current_balance FROM bankroll WHERE id=1").fetchone()["current_balance"]
    update_bankroll(pnl, db_path)
    release_balance(p["stake_amount"], db_path)
    br_after = br_before + pnl

    # 写 settlement
    cur.execute(
        """INSERT OR REPLACE INTO settlements
           (position_id, match_id, home_team, away_team, actual_result, actual_score,
            pnl, roi_pct, bankroll_before, bankroll_after, settled_at, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (position_id, p["match_id"], p["home_team"], p["away_team"],
         actual_result, actual_score, pnl, roi_pct, br_before, br_after, now,
         f"side={p['side']} won={won}"),
    )
    # 更新 position
    cur.execute("UPDATE positions SET status='settled', settled_at=? WHERE position_id=?", (now, position_id))
    # 更新 order
    cur.execute("UPDATE orders SET status='filled' WHERE order_id=?", (p["order_id"],))
    conn.commit()

    # 更新 performance_daily
    _update_daily_performance(cur, now[:10], pnl, won)

    # 更新 strategy_performance
    _update_strategy_performance(cur, p, won, pnl, now)

    # 绩效更新必须提交, 否则 conn.close() 会丢弃未提交 INSERT (绩效表不落库)
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "position_id": position_id,
        "won": won,
        "pnl": pnl,
        "roi_pct": roi_pct,
        "bankroll_after": round(br_after, 2),
    }


def _update_daily_performance(cur, date: str, pnl: float, won: bool) -> None:
    """更新当日绩效"""
    row = cur.execute("SELECT * FROM performance_daily WHERE date=?", (date,)).fetchone()
    if row:
        cur.execute(
            """UPDATE performance_daily SET
                  pnl_daily=pnl_daily+?,
                  bets_count=bets_count+1,
                  wins=wins+?,
                  losses=losses+?,
                  bankroll_eod=bankroll_eod+?,
                  updated_at=?
               WHERE date=?""",
            (pnl, 1 if won else 0, 0 if won else 1, pnl, _now(), date),
        )
    else:
        br = cur.execute("SELECT current_balance FROM bankroll WHERE id=1").fetchone()["current_balance"]
        cur.execute(
            """INSERT INTO performance_daily
               (date, bankroll_eod, pnl_daily, bets_count, wins, losses, high_water_mark)
               VALUES (?,?,?,1,?,?,?)""",
            (date, br, pnl, 1 if won else 0, 0 if won else 1, br),
        )


def _update_strategy_performance(cur, p, won: bool, pnl: float, now: str) -> None:
    """更新策略绩效"""
    # 查 strategy_id
    dec = cur.execute("SELECT strategy_id FROM model_decisions WHERE decision_id=?",
                      (p["decision_id"],)).fetchone()
    if not dec or not dec["strategy_id"]:
        return
    sid = dec["strategy_id"]
    period = now[:7]  # YYYY-MM
    row = cur.execute("SELECT * FROM strategy_performance WHERE strategy_id=? AND period=?",
                      (sid, period)).fetchone()
    if row:
        new_total = row["total_bets"] + 1
        new_wins = row["wins"] + (1 if won else 0)
        new_pnl = round(row["pnl_total"] + pnl, 2)
        win_rate = round(new_wins / new_total * 100, 2) if new_total > 0 else 0.0
        cur.execute(
            """UPDATE strategy_performance SET
                  total_bets=?, wins=?, losses=?, pnl_total=?, win_rate=?, updated_at=?
               WHERE strategy_id=? AND period=?""",
            (new_total, new_wins, new_total - new_wins, new_pnl, win_rate, now, sid, period),
        )


# ═══════════════════════════════════════════════════════
# ⑥ 读接口
# ═══════════════════════════════════════════════════════
def list_positions(status: str = "", limit: int = 50, db_path: str = DB_PATH) -> List[Dict]:
    conn = _conn(db_path)
    sql = "SELECT * FROM positions"
    params: list = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY position_id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_orders(status: str = "pending", limit: int = 50, db_path: str = DB_PATH) -> List[Dict]:
    conn = _conn(db_path)
    sql = "SELECT * FROM orders"
    params: list = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY order_id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_performance(db_path: str = DB_PATH) -> Dict[str, Any]:
    """综合绩效: 资金曲线 + 银行账户 + 策略汇总"""
    conn = _conn(db_path)
    br = conn.execute("SELECT * FROM bankroll WHERE id=1").fetchone()
    daily = conn.execute("SELECT * FROM performance_daily ORDER BY date DESC LIMIT 30").fetchall()
    strat = conn.execute("SELECT * FROM strategy_performance ORDER BY pnl_total DESC").fetchall()
    open_pos = conn.execute("SELECT COUNT(*) FROM positions WHERE status IN ('pending','confirmed')").fetchone()[0]
    settled = conn.execute("SELECT COUNT(*) FROM positions WHERE status='settled'").fetchone()[0]
    total_pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM settlements").fetchone()[0]
    conn.close()
    return {
        "bankroll": dict(br) if br else {},
        "open_positions": open_pos,
        "settled_positions": settled,
        "total_pnl": round(total_pnl, 2),
        "performance_daily": [dict(r) for r in daily],
        "strategy_performance": [dict(r) for r in strat],
    }


# ═══════════════════════════════════════════════════════
# 自测入口
# ═══════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    print("=" * 60)
    print("  quant_executor 自测")
    print("=" * 60)

    # 0) 校验 DB
    if not os.path.exists(DB_PATH):
        print("DB 不存在, 先建库...")
        os.system(f"{sys.executable} scripts/quant_init_db.py --seed")

    # 1) 账户
    br = get_bankroll()
    print(f"\n[1] 账户: ¥{br.get('current_balance', 0):,.2f} / HWM ¥{br.get('high_water_mark', 0):,.2f}")

    # 2) 凯利计算
    k = compute_kelly_stake(model_prob=0.55, book_odds=2.20, current_balance=br['current_balance'])
    print(f"\n[2] 凯利: p=0.55 o=2.20 → 满仓{k['kelly_full']:.3f} 半仓{k['kelly_half']:.3f} "
          f"注码¥{k['stake_amount']:.0f} ({k['stake_pct']*100:.1f}%)")

    # 3) 提交决策 (paper 自动 confirm)
    dec = {
        "strategy_id": "v71_wc_1x2",
        "home_team": "Manchester City", "away_team": "Liverpool",
        "league": "EPL", "commence_time": "2026-07-14T20:00:00Z",
        "direction": "H", "model_prob": 0.55, "market_prob": 0.45,
        "book_odds_h": 2.20, "book_odds_d": 3.40, "book_odds_a": 3.10,
        "book_odds": 2.20, "edge_pct": 21.0, "expected_value": 0.10,
        "decision_text": "模型倾向主胜, edge=21%, EV=10%",
        "operator_action": "下注",
        "operator_view": {
            "stake_hint": "标准", "confidence_pct": 62.0,
            "rules_fired": [{"id": "R1", "label": "市场argmax", "detail": "方向=主胜", "rule": "R1", "color": "blue"}],
            "trap_score": 15.0, "trap_verdict": "无陷阱", "verdict": "主信号: 主胜"
        },
        "confirmation_required": False,  # paper 模式自动 confirm
    }
    res = submit_decision(dec)
    print(f"\n[3] 提交决策: {res}")

    # 4) 查询 pending → 应为空 (自动 confirm)
    pending = list_orders(status="pending")
    print(f"\n[4] pending 订单: {len(pending)} (paper 模式应为0)")
    if res.get("position_id"):
        # 5) 结算赢
        s = settle_match(res["position_id"], actual_result="H", actual_score="2-1")
        print(f"\n[5] 结算(主胜赢): {s}")
        # 6) 再试一单, 结算输
        dec2 = dict(dec)
        dec2["home_team"], dec2["away_team"] = "Arsenal", "Chelsea"
        dec2["direction"] = "A"
        dec2["model_prob"] = 0.40
        dec2["market_prob"] = 0.50
        dec2["book_odds"] = 3.20
        dec2["book_odds_h"], dec2["book_odds_a"] = 2.10, 3.20
        dec2["edge_pct"] = 28.0
        r2 = submit_decision(dec2)
        print(f"\n[6] 提交第2单: {r2}")
        if r2.get("position_id"):
            s2 = settle_match(r2["position_id"], actual_result="H", actual_score="1-0")
            print(f"  结算(实际主胜, 我方客胜输): {s2}")

    # 7) 综合绩效
    perf = get_performance()
    print(f"\n[7] 绩效: 账户¥{perf['bankroll'].get('current_balance', 0):,.2f} "
          f"已结算{perf['settled_positions']}单 总盈亏¥{perf['total_pnl']:,.2f}")

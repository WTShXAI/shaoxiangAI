"""
pipeline.prediction_ledger — 预测账本 + 错误原因记录 (2026-08-23 用户铁律)

用户指令 (哨响AI 总工铁律):
  - 预测一旦生成即快照, **不可变** (不改初始分析结果 / 不回改左侧标签)。
  - 赛后解析实际赛果; 对的记对, 错的只**单独记原因**, 不回改原结论。
  - 错误数据攒到一定量级 (OPTIMIZE_AT) 后, 才反哺模型优化。

本模块是"诚实边界"铁律的落地:
  - record_prediction: 不可变写入 (同 match+market+phase+model 已存在则跳过, 绝不覆盖)。
  - resolve_match: 赛后填充 actual_goals/settle/correct, 错的生成 error_reason + reason_category。
    预测字段 (direction/line/prob/signal) 永不修改, 只追加结果字段 → 不违反"不可变"。
  - backfill: 重建历史初盘预测 (probe_core(0-0,0)) 并解析, 用于回测错误分布。
  - optimization_status: 按 (market, reason_category) 统计错误量, 达阈值即标记"可优化"。

依赖: pipeline.evaluation.ou_eval.ou_settle_fractional (split 线精确结算)。
"""
from __future__ import annotations
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(PROJECT_ROOT, "data", "events.db")

# 同 (market, reason_category) 错误达此量级 → 标记可反哺模型优化
OPTIMIZE_AT = 50


def _open(db_path: Optional[str] = None) -> sqlite3.Connection:
    c = sqlite3.connect(db_path or DEFAULT_DB)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init_ledger(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS prediction_ledger (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            match_key     TEXT,
            market        TEXT,          -- 'OU_1H' / 'OU'
            line          REAL,
            direction     TEXT,          -- 'OVER' / 'UNDER'
            signal        TEXT,
            prob          REAL,
            model_tag     TEXT,
            phase         TEXT,          -- 'pre' / 'live'
            predicted_at  TEXT,
            actual_goals  INTEGER,      -- 解析后填 (半场/全场实际总球)
            actual_settle REAL,         -- ou_settle_fractional 收益
            correct       INTEGER,      -- 1 对 / 0 错 / 2 走盘 / NULL 未解析
            error_reason  TEXT,
            reason_category TEXT,
            features_json TEXT,
            created_at    TEXT
        )"""
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_mk ON prediction_ledger(match_key, market)"
    )
    con.commit()


# ─────────────────────────────────────────────────────────────────────────────
# 不可变写入
# ─────────────────────────────────────────────────────────────────────────────

def record_prediction(
    con: Optional[sqlite3.Connection],
    match_key: str,
    market: str,
    line: float,
    direction: str,
    signal: Optional[str],
    prob: Optional[float],
    model_tag: str = "probe_core",
    phase: str = "pre",
    features: Optional[Dict[str, Any]] = None,
    predicted_at: Optional[str] = None,
) -> bool:
    """快照一条预测。不可变: 同 (match, market, phase, model) 已存在则跳过。
    返回 True=新写入, False=已存在/跳过。"""
    own = con is None
    if own:
        con = _open()
    try:
        exists = con.execute(
            "SELECT 1 FROM prediction_ledger "
            "WHERE match_key=? AND market=? AND phase=? AND model_tag=? LIMIT 1",
            (match_key, market, phase, model_tag),
        ).fetchone()
        if exists:
            return False
        from datetime import datetime

        con.execute(
            """INSERT INTO prediction_ledger
               (match_key, market, line, direction, signal, prob, model_tag, phase,
                predicted_at, features_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                match_key,
                market,
                float(line),
                direction,
                signal,
                (float(prob) if prob is not None else None),
                model_tag,
                phase,
                predicted_at or datetime.now().isoformat(timespec="seconds"),
                json.dumps(features or {}, ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        con.commit()
        return True
    finally:
        if own:
            con.close()


def _instant_dir_for_line(
    read_con: sqlite3.Connection, match_key: str, market: str, line: float
) -> Optional[str]:
    """同盘口线的最新瞬时去水方向 (轻量读, 用于错误原因归因: 综合模型 vs 瞬时去水是否反向)。"""
    prefix = "OU_1H_" if market == "OU_1H" else "OU_"
    mk = f"{prefix}{line:g}"
    ro = read_con.execute(
        "SELECT odds FROM odds_snapshots WHERE match_key=? AND market=? AND selection='over' "
        "ORDER BY captured_at DESC LIMIT 1",
        (match_key, mk),
    ).fetchone()
    ru = read_con.execute(
        "SELECT odds FROM odds_snapshots WHERE match_key=? AND market=? AND selection='under' "
        "ORDER BY captured_at DESC LIMIT 1",
        (match_key, mk),
    ).fetchone()
    if not ro or not ru:
        return None
    ov, un = ro[0], ru[0]
    if not (ov and un and ov > 1.01 and un > 1.01):
        return None
    p = (1.0 / ov) / (1.0 / ov + 1.0 / un)
    return "OVER" if p >= 0.5 else "UNDER"


def record_from_probe_result(
    read_con: sqlite3.Connection,
    match_key: str,
    res: Dict[str, Any],
    current_minute: int = 0,
    is_halftime: bool = False,
    phase: Optional[str] = None,
    model_tag: str = "probe_core",
) -> int:
    """从 probe_core 返回的 res (含 half/full) 快照半场+全场预测。
    read_con 仅用于读瞬时方向; 写入走 record_prediction 内部独立连接, 不污染调用方事务。
    不可变: 每条 (match, market, phase) 仅首写一次。返回新写入条数。"""
    if phase is None:
        phase = "pre" if (current_minute == 0 and not is_halftime) else "live"
    n = 0
    for market, side in (("OU_1H", res.get("half")), ("OU", res.get("full"))):
        if not isinstance(side, dict):
            continue
        direction = side.get("direction")
        line = side.get("line")
        signal = side.get("signal")
        prob = side.get("prob")
        if direction is None or line is None:
            continue
        inst = _instant_dir_for_line(read_con, match_key, market, line)
        feats = {"instant_direction": inst, "prob": prob, "signal": signal}
        if record_prediction(
            None,
            match_key,
            market,
            line,
            direction,
            signal,
            prob,
            model_tag=model_tag,
            phase=phase,
            features=feats,
        ):
            n += 1
    return n


def record_list_badges_batch(rows: List[Dict[str, Any]]) -> int:
    """批量快照列表徽标预测 (model_tag='list_badge', phase='list')。
    按方向变化去重: 仅当 (match_key, market) 的最近一条 list_badge 方向与当前不同
    (或尚无记录) 才写入, 避免轮询爆炸, 同时保留"模型观点演化"审计痕迹。
    不可变: 与 record_prediction 一致的快照语义 (只追加, 不覆盖)。自有连接, 单事务提交。
    rows: [{match_key, market, line, direction, signal, prob}] (direction 应为 OVER/UNDER)"""
    if not rows:
        return 0
    con = _open()
    try:
        mks = list({r["match_key"] for r in rows})
        # 取每个 (match_key, market) 最近一条 list_badge 的方向 (用 rowid 单调序定"最近")
        last: Dict[Tuple[str, str], Tuple[Optional[str], int]] = {}
        for i in range(0, len(mks), 60):
            chunk = mks[i:i + 60]
            ph = ",".join("?" * len(chunk))
            for mk, market, direction, rid in con.execute(
                f"SELECT match_key, market, direction, rowid FROM prediction_ledger "
                f"WHERE model_tag='list_badge' AND phase='list' AND match_key IN ({ph})",
                chunk,
            ).fetchall():
                key = (mk, market)
                if key not in last or rid > last[key][1]:
                    last[key] = (direction, rid)
        to_write = []
        for r in rows:
            key = (r["match_key"], r["market"])
            prev_dir = last.get(key, (None, 0.0))[0]
            if prev_dir == r["direction"]:
                continue  # 方向未变 → 去重, 不写
            to_write.append(r)
        if not to_write:
            return 0
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")
        for r in to_write:
            con.execute(
                """INSERT INTO prediction_ledger
                   (match_key, market, line, direction, signal, prob, model_tag, phase,
                    predicted_at, features_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    r["match_key"],
                    r["market"],
                    float(r["line"]) if r["line"] is not None else None,
                    r["direction"],
                    r["signal"],
                    float(r["prob"]),
                    "list_badge",
                    "list",
                    now,
                    json.dumps({"source": "list_badge"}, ensure_ascii=False),
                    now,
                ),
            )
        con.commit()
        return len(to_write)
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# 赛后解析 (填充结果, 不修改预测字段)
# ─────────────────────────────────────────────────────────────────────────────

def _build_error_reason(
    market: str,
    line: float,
    direction: str,
    prob: Optional[float],
    instant_direction: Optional[str],
    actual: Optional[float] = None,
) -> str:
    reasons: List[str] = []
    frac = round(float(line) - int(line), 2)
    is_split = abs(frac - 0.25) < 0.01 or abs(frac - 0.75) < 0.01
    if is_split:
        reasons.append("split线(.25/.75)半赢半输边界")
    # 方向性偏差 (真实根因): 实际进球与预测方向相反且差距明显
    if actual is not None:
        if direction == "UNDER" and actual >= line + 0.75:
            reasons.append(f"实际{actual}球远超盘口{line:g}, 模型看小系统性低估进球")
        elif direction == "OVER" and actual <= line - 0.75:
            reasons.append(f"实际{actual}球远低于盘口{line:g}, 模型看大系统性高估进球")
    if instant_direction and instant_direction != direction:
        reasons.append(f"综合模型与瞬时去水方向相反(瞬时={instant_direction})")
    if prob is not None and prob >= 0.6:
        reasons.append("高置信(>=0.6)误判")
    if not reasons:
        reasons.append("模型综合判定偏差")
    return "; ".join(reasons)


def _build_category(
    market: str,
    line: float,
    direction: str,
    prob: Optional[float],
    instant_direction: Optional[str],
    actual: Optional[float] = None,
) -> str:
    # 方向性偏差优先 (真实根因, 决定优化方向: 修先验而非修结算)
    if actual is not None:
        if direction == "UNDER" and actual >= line + 0.75:
            return "direction_bias"
        if direction == "OVER" and actual <= line - 0.75:
            return "direction_bias"
    frac = round(float(line) - int(line), 2)
    is_split = abs(frac - 0.25) < 0.01 or abs(frac - 0.75) < 0.01
    if is_split:
        return "split_boundary"
    if instant_direction and instant_direction != direction:
        return "inst_vs_comprehensive"
    if prob is not None and prob >= 0.6:
        return "high_conf_wrong"
    return "generic"


def resolve_match(con: sqlite3.Connection, match_key: str, force: bool = False) -> int:
    """解析一场已结束比赛的预测对错, 错则记录原因。返回更新条数。
    force=True 时重算已解析行的 error_reason/category (仅结果字段, 不碰预测字段)。
    不可变: direction/line/prob/signal/phase 永远不被本函数修改。"""
    from pipeline.evaluation.ou_eval import ou_settle_fractional

    row = con.execute(
        "SELECT ht_score_home, ht_score_away, score_home, score_away, status "
        "FROM matches WHERE match_key=?",
        (match_key,),
    ).fetchone()
    if not row:
        return 0
    ht_h, ht_a, sh, sa, status = row
    if status != "finished" or ht_h is None or ht_a is None or sh is None or sa is None:
        return 0
    ht_goals = int(ht_h) + int(ht_a)
    full_goals = int(sh) + int(sa)
    updated = 0
    for market, actual in (("OU_1H", ht_goals), ("OU", full_goals)):
        # ── OU_1H 数据完整性闸门 (2026-08-23 根因修复) ──
        # matches.ht_score_* 对 ~78% 的场实际存的是全场比分(ht==full),
        # 不能直接当半场实际进球去结算 OU_1H, 否则标签全错配(半场预测比全场比分)。
        # 仅当 ht_total < full_total (物理上证明是真实半场) 才解析;
        # 否则清空历史错误解析(ht==full 污染), 保持 correct=NULL, 不污染优化/准确率指标。
        if market == "OU_1H" and not (ht_goals < full_goals):
            con.execute(
                "UPDATE prediction_ledger SET actual_goals=NULL, actual_settle=NULL, "
                "correct=NULL, error_reason=NULL, reason_category=NULL "
                "WHERE match_key=? AND market='OU_1H'",
                (match_key,),
            )
            continue
        where = "WHERE match_key=? AND market=?"
        params = [match_key, market]
        if not force:
            where += " AND correct IS NULL"
        rows = con.execute(
            f"SELECT id, line, direction, prob, features_json, correct FROM prediction_ledger {where}",
            params,
        ).fetchall()
        for rid, line, direction, prob, fj, old_correct in rows:
            settle = ou_settle_fractional(actual, line)
            if direction == "OVER":
                correct = 1 if settle > 0 else (2 if settle == 0 else 0)
            else:  # UNDER
                correct = 1 if settle < 0 else (2 if settle == 0 else 0)
            feats = json.loads(fj) if fj else {}
            inst = feats.get("instant_direction")
            reason = None
            cat = None
            if correct == 0:
                reason = _build_error_reason(market, line, direction, prob, inst, actual)
                cat = _build_category(market, line, direction, prob, inst, actual)
            # force 重算时, correct 不变 (实际结果未变); 仅刷新原因归因
            con.execute(
                "UPDATE prediction_ledger SET actual_goals=?, actual_settle=?, correct=?, "
                "error_reason=?, reason_category=? WHERE id=?",
                (actual, settle, correct, reason, cat, rid),
            )
            updated += 1
    con.commit()
    return updated


# ─────────────────────────────────────────────────────────────────────────────
# 回测 / 报告
# ─────────────────────────────────────────────────────────────────────────────

def backfill(con: sqlite3.Connection, limit: int = 200) -> Tuple[int, int]:
    """重建历史初盘预测 (probe_core(0-0,0)) 并解析。返回 (新写入, 已解析)。"""
    import analysis.live_goal_probe as lg

    rows = con.execute(
        "SELECT match_key FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND score_away IS NOT NULL "
        "AND ht_score_home IS NOT NULL AND ht_score_away IS NOT NULL "
        "ORDER BY kickoff DESC LIMIT ?",
        (limit,),
    ).fetchall()
    n_rec = 0
    n_res = 0
    for (mk,) in rows:
        lr = con.execute("SELECT league FROM matches WHERE match_key=?", (mk,)).fetchone()
        league = lr[0] if lr else None
        try:
            res = lg.probe_match_with_con(con, mk, "0-0", 0, league, is_halftime=False)
        except Exception:
            continue
        # phase='pre': 初盘预测 = 初始分析结果
        n_rec += record_from_probe_result(con, mk, res, 0, False, phase="pre")
        n_res += resolve_match(con, mk)
    return n_rec, n_res


def accuracy_report(con: sqlite3.Connection) -> List[Tuple]:
    return con.execute(
        """SELECT market,
                  SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) AS right,
                  SUM(CASE WHEN correct=0 THEN 1 ELSE 0 END) AS wrong,
                  SUM(CASE WHEN correct=2 THEN 1 ELSE 0 END) AS push,
                  COUNT(*) AS total
           FROM prediction_ledger WHERE correct IS NOT NULL
           GROUP BY market"""
    ).fetchall()


def optimization_status(
    con: sqlite3.Connection, threshold: int = OPTIMIZE_AT
) -> List[Dict[str, Any]]:
    """按 (market, reason_category) 统计错误量, 达阈值标记可优化。"""
    rows = con.execute(
        "SELECT market, reason_category, COUNT(*) FROM prediction_ledger "
        "WHERE correct=0 AND reason_category IS NOT NULL "
        "GROUP BY market, reason_category ORDER BY COUNT(*) DESC"
    ).fetchall()
    out = []
    for market, cat, cnt in rows:
        out.append(
            {
                "market": market,
                "reason_category": cat,
                "wrong_count": cnt,
                "ready": cnt >= threshold,
                "threshold": threshold,
            }
        )
    return out

"""
pipeline.analysis_snapshot — 前端完整分析快照 + 赛果回填 + 训练集导出
=====================================================================
用户指令 (2026-08-30): "记录前端中的所有分析，然后结合赛果再进行优化训练"

背景缺口:
  - 现有 prediction_ledger 只记 OU(大小球 OVER/UNDER), 方向/比分/三级判定/诱盘全没落库。
  - 前端"点开比赛"会跑 _live_predict 全链路 + probe 破蛋, 展示方向/比分top3/OU/三级判定/诱盘,
    但这一切从未持久化 → 无法"结合赛果回训"。

本模块落地"分析→赛果→回训"闭环:
  1. record: 每次前端分析(点开比赛)快照一条, 不可变。同 (match_key, phase, score) 仅首写。
  2. resolve: 比赛 finished 后回填真实赛果, 标注 方向命中 / 比分top1/top3命中 / OU命中 / 置信度校准。
  3. training_set: 导出 已解析的分析-赛果对, 供回训脚本消费。

铁律对齐 (prediction_ledger.py 同源):
  - 预测字段(方向/比分/级别/置信度/诱盘)一经写入**永不修改**, 只追加赛果字段。
  - 错误攒够量级才回训, 不回改历史。
"""
from __future__ import annotations
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(PROJECT_ROOT, "data", "events.db")

TABLE = "analysis_snapshot"


def _open(db_path: Optional[str] = None) -> sqlite3.Connection:
    c = sqlite3.connect(db_path or DEFAULT_DB)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init_snapshot(con: sqlite3.Connection) -> None:
    """建表(幂等)。分析快照主表: 前端每次分析的完整维度。"""
    con.execute(
        f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            match_key     TEXT,
            home          TEXT,
            away          TEXT,
            league        TEXT,
            phase         TEXT,           -- pre / live / halftime
            current_score TEXT,           -- 分析时刻比分 '1-0'
            current_minute INTEGER,       -- 分析时刻分钟
            -- 赔率快照
            odds_h REAL, odds_d REAL, odds_a REAL,
            ou_line REAL, ou_over REAL, ou_under REAL,
            -- 模型输出 (不可变)
            direction     TEXT,           -- 模型方向 home/draw/away
            market_direction TEXT,        -- 市场 argmax home/draw/away
            score_top1    TEXT,           -- 比分 top1 '1-0'
            score_top3    TEXT,           -- JSON ['1-0','2-0','1-1']
            score_top3_prob TEXT,         -- JSON [p1,p2,p3]
            sa_level      TEXT,           -- 三级判定: 定方向/软加权/观望
            sa_direction  TEXT,           -- 三级判定方向 home/away/draw/NULL(观望)
            sa_confidence REAL,           -- 三级判定置信度 0~1
            sa_note       TEXT,           -- 分歧标注/诱盘降级标注
            induce_label  TEXT,           -- 诱盘识别: honest_def/fake_def/neutral/None
            model_tag     TEXT,           -- 来源: _live_predict / probe
            predicted_at  TEXT,
            -- 赛果回填 (resolve 时填)
            actual_score  TEXT,
            actual_direction TEXT,        -- home/draw/away
            dir_hit       INTEGER,        -- 方向命中 1/0/NULL
            score_top1_hit INTEGER,
            score_top3_hit INTEGER,
            resolved_at   TEXT,
            created_at    TEXT
        )"""
    )
    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_snapshot_mk ON {TABLE}(match_key, phase, current_score)"
    )
    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_snapshot_resolve ON {TABLE}(resolved_at)"
    )
    con.commit()


# ─────────────────────────────────────────────────────────────────────────────
# 记录 (不可变)
# ─────────────────────────────────────────────────────────────────────────────

def record_snapshot(
    con: Optional[sqlite3.Connection],
    *,
    match_key: str,
    home: str,
    away: str,
    league: Optional[str],
    phase: str,
    current_score: Optional[str],
    current_minute: int,
    odds_h: Optional[float], odds_d: Optional[float], odds_a: Optional[float],
    ou_line: Optional[float], ou_over: Optional[float], ou_under: Optional[float],
    direction: Optional[str],
    market_direction: Optional[str],
    score_top1: Optional[str],
    score_top3: Optional[List[str]],
    score_top3_prob: Optional[List[float]],
    sa_level: Optional[str],
    sa_direction: Optional[str],
    sa_confidence: Optional[float],
    sa_note: Optional[str],
    induce_label: Optional[str],
    model_tag: str = "_live_predict",
    predicted_at: Optional[str] = None,
) -> bool:
    """快照一次前端分析。不可变: 同 (match_key, phase, current_score) 已存在则跳过。
    返回 True=新写入, False=已存在跳过。"""
    own = con is None
    if own:
        con = _open()
    try:
        init_snapshot(con)
        key_score = current_score or ""
        # 方向归一为 home/draw/away (后端 direction 可能是中文"主胜/客胜/平局")
        direction = _norm_dir(direction)
        market_direction = _norm_dir(market_direction)
        sa_direction = _norm_dir(sa_direction)
        exists = con.execute(
            f"SELECT 1 FROM {TABLE} WHERE match_key=? AND phase=? AND current_score=? AND model_tag=? LIMIT 1",
            (match_key, phase, key_score, model_tag),
        ).fetchone()
        if exists:
            return False
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")
        con.execute(
            f"""INSERT INTO {TABLE}
               (match_key, home, away, league, phase, current_score, current_minute,
                odds_h, odds_d, odds_a, ou_line, ou_over, ou_under,
                direction, market_direction, score_top1, score_top3, score_top3_prob,
                sa_level, sa_direction, sa_confidence, sa_note, induce_label,
                model_tag, predicted_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                match_key, home, away, league, phase, key_score, current_minute,
                odds_h, odds_d, odds_a, ou_line, ou_over, ou_under,
                direction, market_direction, score_top1,
                json.dumps(score_top3, ensure_ascii=False) if score_top3 else None,
                json.dumps(score_top3_prob, ensure_ascii=False) if score_top3_prob else None,
                sa_level, sa_direction, sa_confidence, sa_note, induce_label,
                model_tag, predicted_at or now, now,
            ),
        )
        con.commit()
        return True
    finally:
        if own:
            con.close()


# ─────────────────────────────────────────────────────────────────────────────
# 赛果回填
# ─────────────────────────────────────────────────────────────────────────────

def _dir_of(score: str) -> Optional[str]:
    """'1-0'/'1:0' → home/draw/away。"""
    if not score:
        return None
    import re
    m = re.match(r"(\d+)\s*[-:]\s*(\d+)", str(score).strip())
    if not m:
        return None
    h, a = int(m.group(1)), int(m.group(2))
    return "home" if h > a else ("draw" if h == a else "away")


_DIR_CN = {"主胜": "home", "客胜": "away", "平局": "draw", "平": "draw",
           "home": "home", "away": "away", "draw": "draw",
           "H": "home", "A": "away", "D": "draw"}


def _norm_dir(d: Any) -> Optional[str]:
    """方向归一为 home/draw/away (兼容中文/英文/单字母)。"""
    if d is None:
        return None
    return _DIR_CN.get(str(d).strip(), None)


def resolve_snapshot(con: sqlite3.Connection, match_key: str, force: bool = False) -> int:
    """回填一场比赛的赛果到所有未解析快照, 标注各维度命中。返回更新条数。

    干净子集铁律(08-30): score_missing=1 的假 0-0 不回填, 避免污染训练集。
    """
    row = con.execute(
        "SELECT score_home, score_away, status, score_missing FROM matches WHERE match_key=?",
        (match_key,),
    ).fetchone()
    if not row:
        return 0
    sh, sa, status, score_missing = row
    if status != "finished" or sh is None or sa is None:
        return 0
    if score_missing:
        return 0  # 假 0-0, 不污染训练集
    actual_score = f"{int(sh)}-{int(sa)}"
    actual_dir = _dir_of(actual_score)
    if force:
        where = "WHERE match_key=?"
    else:
        where = "WHERE match_key=? AND resolved_at IS NULL"
    params = [match_key]
    rows = con.execute(
        f"SELECT id, direction, score_top1, score_top3, sa_confidence, sa_level FROM {TABLE} {where}",
        params,
    ).fetchall()
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    updated = 0
    for rid, direction, s_top1, s_top3, sa_conf, sa_level in rows:
        dir_hit = None
        _ndir = _norm_dir(direction)
        if _ndir and actual_dir:
            dir_hit = 1 if _ndir == actual_dir else 0
        t1_hit = None
        if s_top1:
            t1_hit = 1 if _dir_of(s_top1) is not None and s_top1 == actual_score else 0
        t3_hit = None
        if s_top3:
            try:
                top3 = json.loads(s_top3)
                t3_hit = 1 if actual_score in top3 else 0
            except Exception:
                t3_hit = None
        con.execute(
            f"""UPDATE {TABLE} SET actual_score=?, actual_direction=?, dir_hit=?,
                score_top1_hit=?, score_top3_hit=?, resolved_at=? WHERE id=?""",
            (actual_score, actual_dir, dir_hit, t1_hit, t3_hit, now, rid),
        )
        updated += 1
    con.commit()
    return updated


# ─────────────────────────────────────────────────────────────────────────────
# 训练集导出 / 报告
# ─────────────────────────────────────────────────────────────────────────────

def training_set(con: sqlite3.Connection, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """导出已解析的分析-赛果对(用于回训阈值/先验/校准)。"""
    q = f"""SELECT match_key, home, away, league, phase, current_score, current_minute,
                   odds_h, odds_d, odds_a, direction, market_direction, score_top1,
                   score_top3, sa_level, sa_direction, sa_confidence, sa_note, induce_label,
                   actual_score, actual_direction, dir_hit, score_top1_hit, score_top3_hit
            FROM {TABLE} WHERE resolved_at IS NOT NULL
            ORDER BY resolved_at DESC"""
    if limit:
        q += f" LIMIT {limit}"
    out = []
    for r in con.execute(q).fetchall():
        out.append({
            "match_key": r[0], "home": r[1], "away": r[2], "league": r[3],
            "phase": r[4], "current_score": r[5], "current_minute": r[6],
            "odds_h": r[7], "odds_d": r[8], "odds_a": r[9],
            "direction": r[10], "market_direction": r[11], "score_top1": r[12],
            "score_top3": json.loads(r[13]) if r[13] else None,
            "sa_level": r[14], "sa_direction": r[15], "sa_confidence": r[16],
            "sa_note": r[17], "induce_label": r[18],
            "actual_score": r[19], "actual_direction": r[20],
            "dir_hit": r[21], "score_top1_hit": r[22], "score_top3_hit": r[23],
        })
    return out


def report(con: sqlite3.Connection) -> Dict[str, Any]:
    """快照表的命中率总览 + 三级判定分层。"""
    n = con.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    resolved = con.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE resolved_at IS NOT NULL").fetchone()[0]
    stats = {}
    if resolved:
        stats["dir_hit"] = con.execute(
            f"SELECT AVG(dir_hit) FROM {TABLE} WHERE dir_hit IS NOT NULL").fetchone()[0]
        stats["score_top1_hit"] = con.execute(
            f"SELECT AVG(score_top1_hit) FROM {TABLE} WHERE score_top1_hit IS NOT NULL").fetchone()[0]
        stats["score_top3_hit"] = con.execute(
            f"SELECT AVG(score_top3_hit) FROM {TABLE} WHERE score_top3_hit IS NOT NULL").fetchone()[0]
    by_level = {}
    for r in con.execute(
        f"SELECT sa_level, COUNT(*), AVG(dir_hit) FROM {TABLE} "
        f"WHERE resolved_at IS NOT NULL AND sa_level IS NOT NULL AND dir_hit IS NOT NULL "
        f"GROUP BY sa_level"):
        by_level[r[0]] = {"n": r[1], "dir_hit": r[2]}
    return {"total": n, "resolved": resolved, "metrics": stats, "by_sa_level": by_level}

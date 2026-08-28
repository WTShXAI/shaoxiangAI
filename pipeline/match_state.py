"""统一比赛状态判定 (WS1 桥接层止血).

问题: GQ `matches.status` 已崩(504 条 live 里真进行中仅 53、僵尸 264), 前端靠
leisu feed 的 `match_state` 整数码判定 live/finished, 但缺少时间兜底 → 僵尸场永远显
"进行中", 也分不清已结束。

本模块提供单一权威判定 resolve_match_status(), 优先级:
  1) odds_snapshots.score_at/minute_at (盘口已在用, 最准)
  2) leisu match_state (整数码)
  3) GQ 清洗后 status
  4) 时间兜底 (now-kickoff > 150min → 强制 finished; kickoff 在未来 → scheduled)

match_state 整数约定 (与前端 FixtureEntry.match_state 一致):
  0    = 未开赛 (scheduled)
  > 0  = 进行中 (取真实分钟, 无则 1)
  -1   = 已结束 (finished)
  None = unknown (调用方保留原值, 不臆造状态, 避免制造假 live/假 finished)
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

# 开赛超过该分钟数且无任何活信号 → 强制判定已结束 (僵尸清理)
FINISHED_THRESHOLD_MIN = 150
# 真实比赛分钟 >= 该值 → 终场, 已结束
FULL_TIME_MIN = 90

_GQ_FINISHED = {"finished", "ft", "over", "end", "完场", "完", "final"}


def _parse_iso(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        # 统一为 aware。带 Z / +HH:MM 后缀的按原时区 (UTC/指定);
        # **naive 无时区标记的统一按北京时间 GMT+8** —— 雷速/乐鱼 feed 的 commence_time
        # 是北京时间本地时 (地面真相: 采集器在"北京时间 17:00 开赛"的比赛于 17:xx 实时标 live)。
        # 误当 UTC 会整体偏移 +8h, 把进行中误判未开赛、把已结束误判进行中(僵尸)。
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt
    except Exception:
        return None


def parse_minute(m: Any) -> Optional[int]:
    """解析各种分钟表示: 45 / '45' / "45'" / 'HT' / 'FT' / 'PA'."""
    if m is None:
        return None
    if isinstance(m, (int, float)) and not isinstance(m, bool):
        v = int(m)
        return v
    s = str(m).replace("′", "'").strip()
    if s in ("HT", "中场", "半场"):
        return 45
    if s in ("FT", "结束", "完场", "AET", "PEN", "终"):
        return 90
    if s in ("PA", "TBD", "", "?"):
        return None
    mm = re.match(r"(\d+)", s)
    if mm:
        return int(mm.group(1))
    return None


def _parse_score(score: Any) -> Tuple[Optional[int], Optional[int]]:
    if not score:
        return None, None
    if isinstance(score, (list, tuple)) and len(score) == 2:
        try:
            return int(score[0]), int(score[1])
        except Exception:
            return None, None
    s = str(score)
    mm = re.match(r"(\d+)\s*[-:]\s*(\d+)", s)
    if mm:
        return int(mm.group(1)), int(mm.group(2))
    return None, None


def resolve_match_status(
    *,
    kickoff_iso: Optional[str] = None,
    now: Optional[datetime] = None,
    leisu_state: Any = None,
    leisu_minute: Any = None,
    snapshot_minute: Any = None,
    gq_status: Optional[str] = None,
) -> Tuple[Optional[int], str]:
    """返回 (match_state_int, status_str).

    match_state_int 约定: 0=未开赛, >0=进行中(分钟), -1=已结束, None=unknown。
    仅在有确定性证据时返回非 None; unknown 时返回 None, 调用方应保留原值。
    """
    now = now or datetime.now(timezone.utc)
    ko = _parse_iso(kickoff_iso)

    # 1) leisu 明确 finished 信号 (match_state < 0)
    if isinstance(leisu_state, int) and not isinstance(leisu_state, bool) and leisu_state < 0:
        return -1, "finished"
    # 2) GQ 清洗后状态 = finished
    if isinstance(gq_status, str) and gq_status.strip().lower() in _GQ_FINISHED:
        return -1, "finished"
    # 3) 真实分钟 >= 90 (odds_snapshots 或 leisu)
    sn_min = parse_minute(snapshot_minute)
    lm_min = parse_minute(leisu_minute)
    eff_min = sn_min if sn_min is not None else lm_min
    if eff_min is not None and eff_min >= FULL_TIME_MIN:
        return -1, "finished"
    # 4) 时间兜底: 开赛 > 150min 无活信号 → 强制已结束 (僵尸清理核心)
    if ko is not None:
        elapsed = (now - ko).total_seconds() / 60.0
        if elapsed > FINISHED_THRESHOLD_MIN:
            return -1, "finished"
        if elapsed < 0:
            return 0, "scheduled"
    # 5) 进行中: leisu state 为正整数
    if isinstance(leisu_state, int) and not isinstance(leisu_state, bool) and leisu_state > 0:
        return leisu_state, "live"
    # 6) 进行中: 真实分钟 1..89
    if eff_min is not None and 1 <= eff_min < FULL_TIME_MIN:
        return eff_min, "live"
    # 7) 未开赛 (kickoff 在未来)
    if ko is not None and ko > now:
        return 0, "scheduled"
    # 8) unknown: 已开赛但窗口内无活信号也无终场证据 → 不臆造, 保留原值
    return None, "unknown"


def enrich_match_state(m: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """给定一条比赛 dict(含 commence_time/match_state/match_minute 等),
    返回带修正 match_state 的新 dict, 并附加 _resolved_status 字段。
    若判定为 unknown, 保留原 match_state 不变 (不臆造)。"""
    out = dict(m)
    st, status = resolve_match_status(
        kickoff_iso=m.get("commence_time"),
        now=now,
        leisu_state=m.get("match_state", m.get("mststi")),
        leisu_minute=m.get("match_minute"),
        snapshot_minute=m.get("snapshot_minute"),
        gq_status=m.get("gq_status"),
    )
    if st is not None:
        out["match_state"] = st
    out["_resolved_status"] = status
    return out


def latest_snapshot_state(db_path: str, match_key: str, now: Optional[datetime] = None):
    """从 odds_snapshots 取某 match_key 最新一条的 (minute_at, score_at)。
    用于桥接层(非采集器路径)以盘口快照为权威推导 live/finished。
    返回 (minute_at_or_None, score_at_str_or_None)。只读, 失败返回 (None, None)。"""
    try:
        import sqlite3

        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        row = cur.execute(
            """SELECT minute_at, score_at FROM odds_snapshots
               WHERE match_key = ? ORDER BY captured_at DESC LIMIT 1""",
            (match_key,),
        ).fetchone()
        conn.close()
        if row:
            return row["minute_at"], row["score_at"]
    except Exception:
        pass
    return None, None

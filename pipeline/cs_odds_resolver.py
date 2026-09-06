"""CS 波胆赔率解析器 (Phase B)
从 data/events.db.odds_snapshots(market='CS') 按队名取最新波胆赔率,
归一化为 {'1-0': 9.0, ...} 喂给 cs_triangulate.triangulate(cs_odds=...)。
兼容 selection 两种格式: '1-0' 与 'home/1-0' / 'draw/1-1' / 'away/0-1'。
"""
from __future__ import annotations
import os
import re
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "events.db")
_SCORE_RE = re.compile(r"^\d+-\d+$")

# 2026-08-06: 内存缓存(60s TTL), 避免每次analyze都查892万行CS表(resolve_cs_odds是超时根因)
_CS_CACHE: dict = {}  # {(home,away): (timestamp, result)}
_CS_CACHE_TTL = 60.0


def _norm_name(s: str) -> str:
    return (s or "").strip()


def _parse_score(selection: str) -> str | None:
    """'1-0' -> '1-0' ; 'home/1-0' -> '1-0' ; 其它返回 None"""
    if not selection:
        return None
    s = selection.strip()
    if "/" in s:
        s = s.rsplit("/", 1)[-1].strip()
    if _SCORE_RE.match(s):
        return s
    return None


def _find_match_key(c: sqlite3.Connection, home: str, away: str):
    h, a = _norm_name(home), _norm_name(away)
    if not h or not a:
        return None
    # 1) 精确 home/away
    row = c.execute(
        "SELECT match_key FROM matches WHERE home=? AND away=?", (h, a)
    ).fetchone()
    if row:
        return row[0]
    # 2) 反向 (feed 主客方向可能不同)
    row = c.execute(
        "SELECT match_key FROM matches WHERE home=? AND away=?", (a, h)
    ).fetchone()
    if row:
        return row[0]
    # 3) 大小写/空白不敏感
    row = c.execute(
        "SELECT match_key, home, away FROM matches"
    ).fetchall()
    hh, aa = h.lower(), a.lower()
    for mk, mh, ma in row:
        if mh and ma and mh.lower() == hh and ma.lower() == aa:
            return mk
    return None


def resolve_cs_odds(home: str, away: str, db_path: str = DB_PATH,
                    window_sec: int = 1200) -> dict | None:
    """按主客队名解析最新 CS 波胆赔率。匹配不到或不足 3 项返回 None。

    稳健性: 采集器在亚盘/波胆页偶发写脏值(JS 未刷新时的陈旧 cell), 表现为
    同一 selection 在 live 价与陈旧低值间剧烈震荡。故取**近期窗口内的中位数**
    而非 "latest", 过滤震荡脏值。窗口默认 1200s(20min)。
    """
    if not os.path.exists(db_path):
        return None
    # 2026-08-06: 缓存命中则直接返回(避免查892万行CS表)
    _ck = (_norm_name(home), _norm_name(away))
    _cached = _CS_CACHE.get(_ck)
    if _cached and (time.time() - _cached[0]) < _CS_CACHE_TTL:
        return _cached[1]
    try:
        with sqlite3.connect(db_path) as c:
            c.row_factory = sqlite3.Row
            mk = _find_match_key(c, home, away)
            if not mk:
                _CS_CACHE[_ck] = (time.time(), None)
                return None
            out = _robust_cs_for_matchkey(c, mk, window_sec)
        result = out if len(out) >= 3 else None
        _CS_CACHE[_ck] = (time.time(), result)
        return result
    except Exception:
        return None


def _robust_cs_for_matchkey(c: sqlite3.Connection, mk: str,
                            window_sec: int = 600) -> dict:
    """取某 match_key 每个 CS selection 的稳健赔率 (近期窗口众数, 过滤震荡脏值)。

    实现: 取最近 window_sec 秒内该 match_key 的全部 CS 行, 按 selection 分桶。
    赔率在对局未开盘调整时会在 "live 稳定价" 与 "JS 未刷新的陈旧低值" 间震荡,
    稳定价出现最频繁 → 取**众数**; 众数不明显(无清晰多數)时退化为中位数。
    若某 selection 在窗口内无数据(老比赛), 退化为该 selection 的全局最新值。
    """
    from collections import defaultdict, Counter
    from statistics import median
    # 最近窗口内的行
    rows = c.execute(
        """SELECT selection, odds FROM odds_snapshots s1
           WHERE match_key=? AND market='CS' AND odds>0
             AND captured_at >= (
               SELECT COALESCE(MAX(captured_at), 0) FROM odds_snapshots s2
               WHERE s2.match_key=? AND s2.market='CS') - ?""",
        (mk, mk, window_sec),
    ).fetchall()
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        sc = _parse_score(r["selection"])
        if not sc:
            continue
        hg, ag = sc.split("-")
        if not (0 <= int(hg) <= 5 and 0 <= int(ag) <= 5):
            continue
        buckets[sc].append(float(r["odds"]))
    out: dict[str, float] = {}
    for sc, vals in buckets.items():
        if not vals:
            continue
        cnt = Counter(vals)
        top_val, top_n = cnt.most_common(1)[0]
        # 众数占样本比 < 40% 视为无清晰众数 → 用中位数
        if top_n >= max(2, len(vals) * 0.4):
            out[sc] = round(top_val, 3)
        else:
            out[sc] = round(median(vals), 3)
    # 退化: 窗口内缺失的 selection, 用其全局最新值补齐
    if len(out) < 3:
        fallback = _latest_cs_for_matchkey(c, mk)
        for sc, o in fallback.items():
            out.setdefault(sc, o)
    return out if len(out) >= 3 else {}


def _latest_cs_for_matchkey(c: sqlite3.Connection, mk: str) -> dict:
    """取某 match_key 每个 CS selection 的最新 odds (>=3项才返回)。退化路径用。"""
    rows = c.execute(
        """SELECT selection, odds FROM odds_snapshots s1
           WHERE match_key=? AND market='CS' AND odds>0
             AND captured_at = (
               SELECT MAX(captured_at) FROM odds_snapshots s2
               WHERE s2.match_key=s1.match_key AND s2.market='CS'
                 AND s2.selection=s1.selection)""",
        (mk,),
    ).fetchall()
    out: dict[str, float] = {}
    for r in rows:
        sc = _parse_score(r[0])
        if not sc:
            continue
        hg, ag = sc.split("-")
        if not (0 <= int(hg) <= 5 and 0 <= int(ag) <= 5):
            continue
        out[sc] = float(r[1])
    return out if len(out) >= 3 else {}


def resolve_cs_odds_live(home: str, away: str, db_path: str = DB_PATH) -> dict | None:
    """OPT-A: 直接按 odds_snapshots.match_key(队名形如 'A vs B') 取最新 CS,
    绕过 resolve_cs_odds 依赖的 matches 表。用于 live 比赛不在 matches 表时仍能
    取到 CS 赔率, 让 _live_predict 的混合波胆排名(cs_triangulate blend)触发,
    而非退化纯 Poisson。匹配不到或不足 3 项返回 None。
    """
    h, a = _norm_name(home), _norm_name(away)
    if not h or not a or not os.path.exists(db_path):
        return None
    try:
        with sqlite3.connect(db_path) as c:
            # 1) 精确 match_key (两种主客顺序)
            for mk in (f"{h} vs {a}", f"{a} vs {h}"):
                out = _robust_cs_for_matchkey(c, mk)
                if len(out) >= 3:
                    return out
            # 2) LIKE 模糊: 两支队名都出现在 match_key 中(大小写不敏感)
            pat_h, pat_a = f"%{h.lower()}%", f"%{a.lower()}%"
            mks = c.execute(
                "SELECT DISTINCT match_key FROM odds_snapshots "
                "WHERE market='CS' AND LOWER(match_key) LIKE ? AND LOWER(match_key) LIKE ?",
                (pat_h, pat_a),
            ).fetchall()
            best: dict = {}
            for (mk,) in mks:
                out = _robust_cs_for_matchkey(c, mk)
                if len(out) > len(best):
                    best = out
            return best if len(best) >= 3 else None
    except Exception:
        return None


def resolve_cs_odds_timeline(home: str, away: str, db_path: str = DB_PATH) -> dict | None:
    """CS 三时点赔率时间线 (实时赔率 + drift 陷阱识别)。

    返回 dict:
      {match_key, open, ht_close, live, drift_live_open, has_open, has_ht, has_live}
    或 None(无 GQ 数据 / 匹配不到 / 无赔率)。

    时点定义 (复用 gq_odds_filter, 用户方法论 2026-07-20):
      open          = 初盘 (每条线 per-selection 首见)
      ht_close      = 中场收盘 (kick+44~52min 每线最后一条)
      live          = 当前最新稳健赔率 (_robust_cs_for_matchkey 众数, 与 resolve_cs_odds 同源)
      drift_live_open = live - open (负=临场更被看好 / 资金站该比分一侧)

    仅对 events.db 已采集比赛有效。低级别联赛(采集器未覆盖)返回 None, 前端据此隐藏该区。
    """
    h, a = _norm_name(home), _norm_name(away)
    if not h or not a or not os.path.exists(db_path):
        return None
    try:
        with sqlite3.connect(db_path) as c:
            c.row_factory = sqlite3.Row
            mk = _find_match_key(c, home, away)
            if not mk:
                # 退化: odds_snapshots.match_key 形如 'A vs B' 直接匹配
                for cand in (f"{h} vs {a}", f"{a} vs {h}"):
                    if c.execute(
                        "SELECT 1 FROM odds_snapshots WHERE market='CS' AND match_key=?",
                        (cand,),
                    ).fetchone():
                        mk = cand
                        break
            if not mk:
                return None
            ko = c.execute("SELECT kickoff FROM matches WHERE match_key=?", (mk,)).fetchone()
            ko = ko[0] if ko else None
            import gq_odds_filter as gq
            op = gq.get_open(mk, "CS")
            ht = gq.get_ht_close(mk, "CS", ko) if ko else {}
            live = _robust_cs_for_matchkey(c, mk)
            if not op and not live:
                return None
            drift: dict[str, float] = {}
            for sc, o in (live or {}).items():
                if sc in op and op[sc]:
                    drift[sc] = round(o - op[sc], 2)
            # 初盘→中场收盘 drift (报告核心信号: 200↓ vs 45↑ = 临场资金站实际比分一侧)
            drift_ht: dict[str, float] = {}
            for sc, o in (ht or {}).items():
                if sc in op and op[sc]:
                    drift_ht[sc] = round(o - op[sc], 2)
            # 顺人性盘读数: 优先 初盘→中场收盘(报告验证), 退化 live-open
            _prim = drift_ht if drift_ht else drift
            downs = sum(1 for v in _prim.values() if v < 0)
            ups = sum(1 for v in _prim.values() if v > 0)
            flats = sum(1 for v in _prim.values() if v == 0)
            lean = 'follow_money' if downs > ups else ('fade' if ups > downs else 'neutral')
            return {
                "match_key": mk,
                "open": op or {},
                "ht_close": ht or {},
                "live": live or {},
                "drift_live_open": drift,
                "drift_ht_open": drift_ht,
                "drift_summary": {"down": downs, "up": ups, "flat": flats, "lean": lean},
                "has_open": bool(op),
                "has_ht": bool(ht),
                "has_live": bool(live),
            }
    except Exception:
        return None


def resolve_ou_odds(home: str, away: str, db_path: str = DB_PATH,
                    window_sec: int = 1800) -> tuple | None:
    """按主客队名从 events.db 解析最新 OU(大小球) 赔率, 返回 (over_water, under_water, ou_line) 或 None.

    用于 _live_predict 在前端未传 OU 时兜底喂给 strategy_signals.compute_signals,
    使 Fade Over 信号在 GQ 已稳定采集 OU(2026-07-21 修复 OU 解析 bug 后)时置信升级 medium.

    OU 快照结构: market='OU_{line:.2f}' (如 "OU_1.75"), selection='over'/'under',
    odds=赔率, line=大小球线 (record_snapshot 持久化). 多线并存时选"同时有 over+under
    且 captured_at 最新"的那条线, 保证 over/under 同源同刻; line 缺失时退化从 market 名解析.
    """
    h, a = _norm_name(home), _norm_name(away)
    if not h or not a or not os.path.exists(db_path):
        return None
    try:
        with sqlite3.connect(db_path) as c:
            c.row_factory = sqlite3.Row
            mk = _find_match_key(c, home, away)
            if not mk:
                for cand in (f"{h} vs {a}", f"{a} vs {h}"):
                    if c.execute(
                        "SELECT 1 FROM odds_snapshots WHERE market LIKE 'OU_%' AND match_key=?",
                        (cand,),
                    ).fetchone():
                        mk = cand
                        break
            if not mk:
                return None
            rows = c.execute(
                """SELECT market, selection, odds, line, captured_at FROM odds_snapshots s1
                   WHERE match_key=? AND market LIKE 'OU_%' AND odds>0
                     AND captured_at >= (
                       SELECT COALESCE(MAX(captured_at),0) FROM odds_snapshots s2
                       WHERE s2.match_key=? AND s2.market LIKE 'OU_%') - ?""",
                (mk, mk, window_sec),
            ).fetchall()
            if not rows:
                return None
            # 按 market(line) 分桶, 每个 selection 取该桶内最新 captured_at 的赔率
            by_mkt: dict[str, dict] = {}
            for r in rows:
                m = r["market"]
                sel = r["selection"]
                bucket = by_mkt.setdefault(m, {})
                cur = bucket.get(sel)
                if cur is None or r["captured_at"] > cur[1]:
                    bucket[sel] = (float(r["odds"]), r["captured_at"], r["line"])
            # 选出同时有 over+under 的 line, 取 captured_at 最新者
            best = None
            for m, sels in by_mkt.items():
                if "over" in sels and "under" in sels:
                    ov, un = sels["over"][0], sels["under"][0]
                    cap = max(sels["over"][1], sels["under"][1])
                    if best is None or cap > best[3]:
                        best = (ov, un, sels["over"][2] or 0.0, cap, m)
            if not best:
                return None
            ou_line = best[2] or 0.0
            if not ou_line:
                try:
                    ou_line = float(best[4].replace("OU_", ""))
                except Exception:
                    ou_line = 0.0
            if not (best[0] and best[1] and ou_line):
                return None
            return (best[0], best[1], ou_line)
    except Exception:
        return None

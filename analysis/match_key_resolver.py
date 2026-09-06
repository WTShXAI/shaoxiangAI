# -*- coding: utf-8 -*-
"""match_key 规范化解析器 (2026-08-28, IR-16 队名透传问题).

背景: GQ H5 实时队名与 DB 历史队名存在音译差异/队名更新, 如:
  - 前端/GQ 实时: "奎瓦特 vs 杜格普利耶" (0' 新登记录入前)
  - DB 历史:      "塞瓦特 vs 杜格普利耶"
  直接按 match_key 精确查询会落空 → probe/analyze/momentum/cs 全部 not found。
  前端能选中比赛但所有端点返回"数据缺失"——即用户反复报的"没数据/没 CS/没即时盘口"。

本模块提供"精确 → 模糊"两级解析:
  1. 精确: matches/odds_snapshots 直接命中 → 原样返回。
  2. 模糊: 拆 home/away, 在 matches 全表找"双方 token 相似度最高"的 match_key,
     相似度 = 0.6·home_sim + 0.4·away_sim (SequenceMatcher, 规范化后)。
     要求双方相似度都 >= 0.55 且加权分 >= 0.7, 否则视为不同比赛返回 None(宁缺勿错, 不伪造)。
  3. TTL 缓存 (30s) 防重复扫全表 (matches 表 ~11.7k 行, 全扫毫秒级, 缓存保险)。

用法: bridge_service.py 端点入口统一调用:
    from analysis.match_key_resolver import resolve_match_key
    mk = resolve_match_key(con, req.get("match_key", "")) or req.get("match_key", "")
"""
import difflib
import re
import threading
import time

_MATCHES_CACHE: dict = {}
_TTL = 30.0
_LOCK = threading.Lock()

_RE_VS = re.compile(r"\s+vs\s+|\s+VS\s+")


def _norm(s: str) -> str:
    """规范化队名: 去空白/去赞助商常见后缀差异仅保字面, 不做复杂归一(避免误配)。"""
    return re.sub(r"\s+", "", s or "").strip().lower()


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def resolve_match_key(con, match_key: str, cache: bool = True):
    """精确→模糊两级解析 DB 真实 match_key。失败返回 None(调用方回退原值)。"""
    if not match_key:
        return None
    mk = match_key.strip()
    # ── 1. 精确命中 (优先 odds_snapshots, 再 matches) ──
    try:
        r = con.execute(
            "SELECT 1 FROM odds_snapshots WHERE match_key=? LIMIT 1", (mk,)
        ).fetchone()
        if r:
            return mk
        r = con.execute(
            "SELECT 1 FROM matches WHERE match_key=? LIMIT 1", (mk,)
        ).fetchone()
        if r:
            return mk
    except Exception:
        return None

    # ── 2. 模糊匹配 ──
    parts = _RE_VS.split(mk)
    if len(parts) != 2:
        return None
    home_q, away_q = parts[0].strip(), parts[1].strip()

    now = time.time()
    with _LOCK:
        if cache and _MATCHES_CACHE.get("_ts") and now - _MATCHES_CACHE["_ts"] < _TTL:
            cands = _MATCHES_CACHE.get("cands")
        else:
            cands = None
    if cands is None:
        try:
            cands = [
                r[0] for r in con.execute(
                    "SELECT match_key FROM matches WHERE match_key LIKE '% vs %'"
                ).fetchall()
            ]
        except Exception:
            return None
        with _LOCK:
            _MATCHES_CACHE["cands"] = cands
            _MATCHES_CACHE["_ts"] = now

    best, best_score = None, 0.0
    for cand in cands:
        cparts = _RE_VS.split(cand)
        if len(cparts) != 2:
            continue
        chome, caway = cparts[0].strip(), cparts[1].strip()
        home_sim = _sim(home_q, chome)
        away_sim = _sim(away_q, caway)
        # 双方都要像; 主队权重高(主队差异是主要问题)
        if home_sim < 0.55 or away_sim < 0.55:
            continue
        score = 0.6 * home_sim + 0.4 * away_sim
        if score > best_score:
            best, best_score = cand, score
    # 宁缺勿错: 加权分 < 0.7 视为不同比赛, 不解析
    return best if best_score >= 0.7 else None


def clear_cache():
    with _LOCK:
        _MATCHES_CACHE.clear()

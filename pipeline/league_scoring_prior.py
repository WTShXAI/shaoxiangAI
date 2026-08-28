"""联赛/赛事进球水平先验 — 独立、校准、零回归的透明特征.

背景 (2026-08-12 实证, 31.2万场 historical_matches):
  - 各赛事场均总球跨度极大: 法甲 2.51 → 欧冠联赛 3.37 (≥0.86 球).
  - 当前核心预期总球 = blend_total(WI总进球 0.7 + JEPА 0.3), 纯赔率反演,
    完全不看联赛/赛事特性. obscure 赛事赔率噪声大, 这一缺口最致命.
  - 用户经验规律"近两场和/2"经 31万场验证不成立 (RMSE 3.60 vs 同联赛均值 1.61,
    corr=0.086, 收缩估计 w=0 最优) → 不采纳; 但"赛事进球水平"是数据强支撑的真实先验.

设计原则 (用户批准: 独立特征 + 校准 + 零回归):
  1. 独立: 本模块不修改任何现有 P(over)/方向/verdict, 只产出"联赛先验总球".
  2. 校准: 先验来自 31万场真实赛果的场均总球, 按样本量加权.
  3. 零回归: 收缩混合 (shrinkage). 赔率已高度有效的高流动性赛事 → 先验权重≈0.05;
     obscure/未知赛事 → 权重升到 0.25, 把噪声总球拉回联赛/全局均值.
     中心 λ 仅被"温和修正", 决策链路 (OU方向/1X2/verdict) 完全不动.

用法:
  from pipeline.league_scoring_prior import blend_total_with_league
  out = blend_total_with_league(odds_total=2.74, live_league="欧洲联赛资格赛")
  # out = {"adjusted": 2.69, "prior_mean": 2.60, "prior_n": 2051,
  #        "matched_league": "欧罗巴杯", "liquidity": "mid", "weight": 0.12}
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from typing import Dict, Optional, Tuple

# ── 全局/兜底常量 (与 31.2万场实测一致) ──
GLOBAL_MEAN_TOTAL = 2.682  # historical_matches 全场均总球 (n=312010)
GLOBAL_MEAN_HOME = 1.46
GLOBAL_MEAN_AWAY = 1.22

# 联赛流动性分级 → 先验收缩权重 (PSEUDO=3 / (PSEUDO + market_strength))
#   liquid : 五大联赛/世界杯/欧冠正赛 — 赔率高度有效, 先验仅微修
#   mid    : 二级主流 (荷甲/葡超/比甲/巴西甲/美职/各资格赛 n>=200) — 中等收缩
#   obscure: 小联赛/未知 — 赔率噪声大, 多收缩向联赛/全局均值
LIQUIDITY_WEIGHT = {"liquid": 0.05, "mid": 0.12, "obscure": 0.25}

# 明确的高流动性"主流"赛事关键词 (命中即 liquid, 无论样本量)
LIQUID_KEYWORDS = (
    "英超", "西甲", "意甲", "德甲", "法甲", "荷甲", "葡超", "比甲",
    "世界杯", "欧洲杯", "欧冠", "欧冠杯", "英超", "西甲", "premier league",
    "la liga", "bundesliga", "serie a", "ligue 1",
)

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "football_data.db",
)

# ── 模块级缓存 (31万行聚合很快, 但避免每次 predict 都查库) ──
_cache: Dict[str, Tuple[float, int]] = {}   # norm_league -> (mean_total, n)
_agg: Dict[str, Tuple[float, int]] = {}     # 欧战聚合 -> (mean, n)
_cache_ts = 0.0
_cache_lock = threading.Lock()
_CACHE_TTL = 3600.0  # 1h

# 标准中文数字/级别归一 (用于模糊匹配)
_LEVEL_MAP = {
    "超级": "div0", "超": "div0", "甲": "div1", "乙": "div2", "丙": "div3",
    "丁": "div4", "U20": "u20", "U19": "u19", "U23": "u23", "后备": "b",
    "预备": "b", "二队": "b",
}
# 赛事性质关键词 → 规范 token (解决 欧联/欧罗巴/欧会 同义)
_EVENT_MAP = {
    "欧冠": "champions", "冠军联赛": "champions", "champions": "champions",
    "欧联": "europa", "欧罗巴": "europa", "europa": "europa",
    "欧会": "conference", "conference": "conference",
    "资格": "qual", "预选": "qual", "qual": "qual",
    "杯": "cup", "联赛": "league", "友谊": "frnd", "世界": "wc",
}
# 国家/地区 token (优先 2 字精确映射, 避免 "阿" 同时命中 阿根廷/阿曼 的歧义)
_REGION_MAP = {
    "英格兰": "eng", "英": "eng", "西班牙": "esp", "西": "esp", "意大利": "ita", "意": "ita",
    "德国": "ger", "德": "ger", "法国": "fra", "法": "fra", "荷兰": "ned", "荷": "ned",
    "葡萄牙": "por", "葡": "por", "比利时": "bel", "比": "bel",
    "中国": "chn", "中超": "chn", "中甲": "chn", "中乙": "chn", "中北": "concacaf",
    "日本": "jpn", "日": "jpn", "韩国": "kor", "韩": "kor", "美国": "usa", "美": "usa",
    "巴西": "bra", "巴": "bra", "阿根廷": "arg", "阿曼": "oman", "阿": "arg", "俄": "rus",
    "土": "tur", "瑞": "swe", "挪": "nor", "丹": "den", "波": "pol",
    "罗": "rou", "希": "gre", "捷": "cze", "克": "cro", "匈": "hun",
    "奥": "aut", "乌": "ukr", "苏": "sco", "爱": "irl", "塞尔": "srb",
    "哥伦": "col", "墨": "mex", "秘": "per", "智": "chi", "哥斯": "crc",
    "委": "ven", "芬": "fin", "立": "ltu", "拉": "lat", "白": "blr",
    "安": "ang", "南": "aus2", "印": "ind", "玻": "bol", "厄": "ecu",
    "乌兹": "uzb", "保": "bul", "巴拉": "par", "乌拉": "uru", "国际": "int",
    "球会": "club",
}


def _normalize_tokens(s: str) -> set:
    """把联赛名拆成规范 token 集合 (用于 Jaccard 相似度)."""
    if not s:
        return set()
    s = s.lower().replace("　", " ").strip()
    toks: set = set()
    # 事件/级别/地区 关键词映射
    for k, v in _EVENT_MAP.items():
        if k in s:
            toks.add(v)
    for k, v in _LEVEL_MAP.items():
        if k in s:
            toks.add(v)
    for k, v in _REGION_MAP.items():
        if k in s:
            toks.add(v)
    # 数字级别 (如 "K1" "K2" "丙级" 已覆盖, 但保留裸数字联赛如 "第1圈" 不给 token)
    return toks


# 过于通用的 token, 不参与模糊匹配 (否则 "X联赛" 会误匹配任意含"联赛"的历史联赛)
_GENERIC_TOKENS = {"league", "cup", "frnd"}


def _load_db() -> None:
    """从 historical_matches 加载各联赛场均总球 + 欧战聚合. 失败则保留空缓存(走全局均值)."""
    global _cache, _agg, _cache_ts
    try:
        con = sqlite3.connect(_DB_PATH, timeout=5)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """SELECT league_name, COUNT(*) n,
                      AVG(CAST(home_score AS REAL)+CAST(away_score AS REAL)) mean_tot
               FROM historical_matches
               WHERE home_score IS NOT NULL AND away_score IS NOT NULL
                 AND league_name IS NOT NULL
               GROUP BY league_name HAVING n>=30"""
        ).fetchall()
        local: Dict[str, Tuple[float, int]] = {}
        for r in rows:
            lg = (r["league_name"] or "").strip()
            if lg:
                local[lg] = (round(float(r["mean_tot"]), 3), int(r["n"]))
        # 欧战聚合 (资格赛+正赛合并, 比单条更稳)
        def _agg_like(pattern: str) -> Optional[Tuple[float, int]]:
            a = con.execute(
                """SELECT COUNT(*) n,
                          AVG(CAST(home_score AS REAL)+CAST(away_score AS REAL)) mean_tot
                   FROM historical_matches
                   WHERE home_score IS NOT NULL AND away_score IS NOT NULL
                     AND league_name LIKE ?""",
                (pattern,),
            ).fetchone()
            if a and a["n"] and a["n"] >= 30:
                return (round(float(a["mean_tot"]), 3), int(a["n"]))
            return None
        agg = {
            "champions": _agg_like("%欧冠%"),
            "europa": _agg_like("%欧罗巴%"),
            "conference": _agg_like("%欧会%"),
        }
        con.close()
        if local:
            _cache = local
            _agg = {k: v for k, v in agg.items() if v}
            _cache_ts = __import__("time").time()
    except Exception:
        # 加载失败: 保留现有缓存 (首次则为空 → 全部走全局均值). 不抛, 不阻断预测.
        pass


def _ensure_loaded() -> None:
    import time as _t
    with _cache_lock:
        if _t.time() - _cache_ts > _CACHE_TTL or not _cache:
            _load_db()


def match_league(live_league: Optional[str]) -> Dict[str, object]:
    """把实时 feed 的联赛名映射到历史库均值.

    返回 {matched_league, mean, n, method}
      method: exact | aggregate | fuzzy | global
    """
    _ensure_loaded()
    live = (live_league or "").strip()
    if not live:
        return {"matched_league": None, "mean": GLOBAL_MEAN_TOTAL, "n": 0, "method": "global"}

    # 1) 精确匹配 (规范化后)
    for lg, (mean, n) in _cache.items():
        if lg == live or lg.replace(" ", "") == live.replace(" ", ""):
            return {"matched_league": lg, "mean": mean, "n": n, "method": "exact"}

    # 2) 欧战聚合匹配
    low = live.lower()
    if "欧冠" in live or "冠军联赛" in live:
        if _agg.get("champions"):
            m, n = _agg["champions"]
            return {"matched_league": "欧冠(聚合)", "mean": m, "n": n, "method": "aggregate"}
    if "欧会" in live or "协会" in live:
        if _agg.get("conference"):
            m, n = _agg["conference"]
            return {"matched_league": "欧会(聚合)", "mean": m, "n": n, "method": "aggregate"}
    if "欧联" in live or "欧罗巴" in live or "欧洲联赛" in live:
        if _agg.get("europa"):
            m, n = _agg["europa"]
            return {"matched_league": "欧罗巴(聚合)", "mean": m, "n": n, "method": "aggregate"}

    # 3) 模糊 token 匹配 (Jaccard) — 必须至少一个"具体" token (地区/赛事/级别) 重合,
    #    避免 "X联赛" 这种仅含通用 token 的野鸡名误匹配任意含"联赛"的历史联赛.
    live_toks = _normalize_tokens(live)
    live_spec = live_toks - _GENERIC_TOKENS
    best, best_score, best_n = None, 0.0, 0
    for lg, (mean, n) in _cache.items():
        lg_toks = _normalize_tokens(lg)
        lg_spec = lg_toks - _GENERIC_TOKENS
        if not live_spec or not lg_spec:
            continue
        inter_spec = live_spec & lg_spec
        if not inter_spec:
            continue
        inter = len(live_toks & lg_toks)
        union = len(live_toks | lg_toks)
        score = inter / union
        # 仅当具体 token 重合数 >= 2 时才接受 (防 "阿" 单字歧义误匹配),
        # 或单 token 但 Jaccard 极高 (>=0.6) 的强信号.
        if len(inter_spec) >= 2 or (len(inter_spec) == 1 and score >= 0.6):
            if score > best_score or (score == best_score and n > best_n):
                best, best_score, best_n = (lg, mean, n), score, n
    if best and best_score >= 0.34:
        lg, mean, n = best
        return {"matched_league": lg, "mean": mean, "n": n, "method": "fuzzy"}

    # 4) 全局兜底
    return {"matched_league": None, "mean": GLOBAL_MEAN_TOTAL, "n": 0, "method": "global"}


def classify_liquidity(live_league: Optional[str], matched: Dict[str, object]) -> str:
    """返回 liquid | mid | obscure."""
    low = (live_league or "").lower()
    for kw in LIQUID_KEYWORDS:
        if kw.lower() in low:
            return "liquid"
    n = int(matched.get("n") or 0)
    if n >= 1000:
        return "liquid"
    if n >= 200:
        return "mid"
    return "obscure"


def blend_total_with_league(
    odds_total: Optional[float],
    live_league: Optional[str],
    override_weight: Optional[float] = None,
) -> Dict[str, object]:
    """把赔率反演的总球与联赛先验做收缩混合.

    参数:
      odds_total      : WI+JEPА 反演的中心 λ (可为 None, 此时纯用先验/全局)
      live_league     : 实时 feed 的联赛名 (如 "欧洲联赛资格赛")
      override_weight : 手动覆盖先验权重 (调试/校准用)

    返回:
      {adjusted, prior_mean, prior_n, matched_league, liquidity, weight,
       method, odds_total, note}
      adjusted = (1-w)*odds_total + w*prior_mean   (clamp [1.5, 4.5])
    """
    matched = match_league(live_league)
    liquidity = classify_liquidity(live_league, matched)
    w = override_weight if override_weight is not None else LIQUIDITY_WEIGHT[liquidity]
    prior_mean = float(matched["mean"])

    if odds_total is None and override_weight is None:
        # 无赔率总球 → 直接用先验 (如 obscure 无 WI)
        adjusted = prior_mean
        note = "无赔率总球, 纯用联赛先验"
    elif odds_total is None:
        adjusted = prior_mean
        note = "无赔率总球, 纯用联赛先验"
    else:
        ot = float(odds_total)
        adjusted = (1.0 - w) * ot + w * prior_mean
        note = f"收缩混合 w={w:.2f}"

    adjusted = max(1.5, min(4.5, adjusted))
    return {
        "adjusted": round(adjusted, 3),
        "prior_mean": round(prior_mean, 3),
        "prior_n": int(matched.get("n") or 0),
        "matched_league": matched.get("matched_league"),
        "liquidity": liquidity,
        "weight": round(w, 3),
        "method": matched.get("method"),
        "odds_total": (round(float(odds_total), 3) if odds_total is not None else None),
        "note": note,
    }


# ── 自测 / 校准验证 ──
if __name__ == "__main__":
    import json
    _load_db()
    print(f"缓存联赛数: {len(_cache)} | 欧战聚合: {_agg}")
    tests = [
        ("英超", 2.71), ("法甲", 2.51), ("德甲", 2.87),
        ("欧洲冠军联赛资格赛", 2.80), ("欧洲联赛资格赛", 2.60),
        ("欧洲协会联赛资格赛", 2.70), ("巴西甲级联赛", 2.38),
        ("挪威丙级联赛", 3.0), ("某未知野鸡联赛XYZ", 3.5),
    ]
    for lg, ot in tests:
        r = blend_total_with_league(ot, lg)
        print(f"{lg:18s} odds={ot} → adj={r['adjusted']} "
              f"(prior={r['prior_mean']} n={r['prior_n']} liq={r['liquidity']} "
              f"w={r['weight']} via={r['method']}/{r['matched_league']})")

# -*- coding: utf-8 -*-
"""操盘手平局验证信号层 (Draw Validation by Bookmaker).

设计依据 (见 deliverables/draw_bookmaker_validation.md, 真实7180场回测):
  - 模型 P(平) 几乎检测不到平局 (lift仅1.08)
  - 操盘手隐含 P(平) 碾压 (lift 1.22~1.31)
  - 双庄家共识(WH×Interwetten)最强 (精确JOIN后诚实lift见deliverables, 原模糊JOIN的1.32含偏差)

本模块提供:
  1. market_draw_prob(oh,od,oa)      去抽水隐含平局概率 (操盘手一手定价)
  2. consensus_draw_signal(...)       跨庄家(WC用赔率源, 联赛用WH+Interwetten)共识平局信号
生产引擎的平局检测应优先用本层, 而非模型softmax的P(平)。
"""
from __future__ import annotations
import sqlite3
from collections import defaultdict
from datetime import timedelta
import numpy as np
import pandas as pd

DB = r"D:\Architecture\data\football_data.db"
_IW_CACHE: dict | None = None  # (home_norm,away_norm) -> [(date, iw_pdraw, league_name)]

# 平局预警阈值 (G5 consensus booster): 单庄基础阈值 0.26; 双庄共识 strong 时降到 0.24
DRAW_ALERT = 0.26
DRAW_ALERT_BOOSTER = 0.24


def demargin(oh, od, oa):
    inv = 1.0 / oh + 1.0 / od + 1.0 / oa
    return (1.0 / oh) / inv, (1.0 / od) / inv, (1.0 / oa) / inv


def market_draw_prob(oh, od, oa):
    """操盘手一手定价 -> 去抽水隐含平局概率. 这是平局检测的主信号."""
    try:
        _, pd_, _ = demargin(float(oh), float(od), float(oa))
        return float(pd_)
    except Exception:
        return 0.0


def _base_league(name):
    """从 '15/16澳超第13轮' / '英超   ' / '19/20英超第18轮' 提取基础联赛名."""
    if not name:
        return None
    import re
    s = re.sub(r"^\d{2}/\d{2}", "", str(name).strip())
    s = re.sub(r"第.*?轮.*$", "", s)
    return s.strip() or None


def multi_bookmaker_consensus(bookmakers):
    """多庄家平局共识分析 (任意数量庄家, 不依赖特定表).

    Args:
      bookmakers: list of (name:str, oh:float, od:float, oa:float)

    Returns dict:
      count          参与庄家数
      mean_pd        均值 P(平)
      min_pd / max_pd 最小/最大 P(平) 及对应庄家
      std_pd         标准差 (分歧度, 越小越一致)
      range_pp       极差(pp)
      direction_dist 方向分布 {H:n, D:n, A:n}
      unanimous      是否全部方向一致
      strong         强信号(均值>=0.30 且 std<0.06)
      draw_alert     平局预警(均值>=0.26)
      details        每家 [(name, od, pd)] 排序by pd
    """
    if not bookmakers:
        return dict(count=0, available=False)

    items = []
    for name, oh, od, oa in bookmakers:
        try:
            _, pd_, _ = demargin(float(oh), float(od), float(oa))
            items.append((str(name), float(od), float(pd_)))
        except Exception:
            continue

    if not items:
        return dict(count=0, available=False)

    items.sort(key=lambda x: x[2])
    pds = [p for (_, _, p) in items]
    n = len(pds)
    mean_pd = sum(pds) / n
    var = sum((p - mean_pd) ** 2 for p in pds) / n
    std_pd = var ** 0.5

    # Direction distribution
    dirs = []
    for name, oh, od, oa in bookmakers:
        try:
            ph, pd_, pa = demargin(float(oh), float(od), float(oa))
            best = "H" if ph >= pd_ and ph >= pa else ("D" if pd_ >= pa else "A")
            dirs.append(best)
        except Exception:
            pass
    from collections import Counter
    dc = Counter(dirs)
    unanimous = len(set(dc)) == 1

    strong = mean_pd >= 0.30 and std_pd < 0.06
    draw_alert = mean_pd >= DRAW_ALERT if 'DRAW_ALERT' in dir() else mean_pd >= 0.26

    return dict(
        count=n,
        available=True,
        mean_pd=round(mean_pd, 4),
        min_pd=(round(min(pds), 4), items[0][0], round(items[0][1], 2)),
        max_pd=(round(max(pds), 4), items[-1][0], round(items[-1][1], 2)),
        std_pd=round(std_pd, 4),
        range_pp=round((max(pds) - min(pds)) * 100, 2),
        direction_dist=dict(dc),
        unanimous=unanimous,
        strong=strong,
        draw_alert=draw_alert,
        details=[(n, round(od, 2), round(pd * 100, 1)) for (n, od, pd) in items],
    )


def _load_iw():
    global _IW_CACHE
    if _IW_CACHE is not None:
        return _IW_CACHE
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT match_date, home_team_norm, away_team_norm, league_name, close_home_odds, "
        "close_draw_odds, close_away_odds FROM interwetten_odds "
        "WHERE close_home_odds>1.01 AND close_draw_odds>1.01 AND close_away_odds>1.01", con)
    con.close()
    df["match_date"] = pd.to_datetime(df["match_date"], errors="coerce")
    df = df.dropna(subset=["match_date"])
    idx = defaultdict(list)
    for _, r in df.iterrows():
        _, pd_, _ = demargin(r["close_home_odds"], r["close_draw_odds"], r["close_away_odds"])
        idx[(r["home_team_norm"], r["away_team_norm"])].append(
            (r["match_date"], float(pd_), r["league_name"]))
    _IW_CACHE = idx
    return idx


def consensus_draw_signal(home_norm, away_norm, wh_oh, wh_od, wh_oa,
                          match_date=None, wh_league=None):
    """跨庄家平局共识信号 (精确日期+联赛键 JOIN, OOS诚实版).

    返回 dict:
      market_pd   操盘手(WH)隐含P(平)
      iw_pd       Interwetten隐含P(平) (无匹配则为None)
      consensus   两庄平均P(平) (仅WH时为market_pd)
      agreement   两庄|P(平)差| (None若无IW)
      strong      共识均值>=0.30 且 两庄分歧<0.06 -> 强平局信号(杜博弈建议)
      available   是否有IW精确匹配
      join        匹配方式: 'exact' | '±1d' | 'none'
    注意: 原实现用 |Δday|<=4 模糊窗口, 会跨轮次把不同比赛当同场 -> 偏差.
          现改为精确日期(同fixture)+联赛基一致, ±1天仅作时区容错.
    """
    m_pd = market_draw_prob(wh_oh, wh_od, wh_oa)
    res = dict(market_pd=m_pd, iw_pd=None, consensus=m_pd, agreement=None,
               strong=False, available=False, join="none")
    idx = _load_iw()
    key = (home_norm, away_norm)
    if key not in idx:
        return res
    rd = pd.to_datetime(match_date, errors="coerce") if match_date else None
    if rd is None or pd.isna(rd):
        return res
    rd_str = rd.strftime("%Y-%m-%d")
    wlbase = _base_league(wh_league)
    best = None
    jmode = "none"
    # 1) 精确日期匹配 (同fixture)
    for (d, pd_, lname) in idx[key]:
        if pd.isna(d):
            continue
        if d.strftime("%Y-%m-%d") == rd_str and _base_league(lname) == wlbase:
            best = pd_; jmode = "exact"; break
    # 2) ±1天 时区容错 (仍要求联赛基一致, 不跨轮次)
    if best is None:
        for (d, pd_, lname) in idx[key]:
            if pd.isna(d):
                continue
            if abs((d - rd).days) <= 1 and _base_league(lname) == wlbase:
                best = pd_; jmode = "±1d"; break
    if best is None:
        return res
    res["iw_pd"] = best
    res["consensus"] = (m_pd + best) / 2.0
    res["agreement"] = abs(m_pd - best)
    res["available"] = True
    res["join"] = jmode
    # 强信号 (杜博弈建议): 共识均值>=0.30 且 两庄分歧<0.06
    res["strong"] = (res["consensus"] >= 0.30 and abs(m_pd - best) < 0.06)
    return res


def draw_alert_with_booster(m_pd, consensus, base=DRAW_ALERT, booster=DRAW_ALERT_BOOSTER):
    """G5 · 双庄共识 booster 接入平局预警.

    逻辑:
      - 基础预警: 单庄隐含 P(平) m_pd >= base (0.26)
      - 当 consensus.strong (双庄共识强, lift 真金) 时, 阈值降到 booster (0.24) → 更易触发防平
      - consensus 不可用(单庄 / WC无IW→available=False / strong=False) 时回退纯市场 P 平(base)

    返回: bool (是否触发 draw_alert)
    """
    alert = m_pd >= base
    if consensus and consensus.get("strong"):
        alert = alert or (m_pd >= booster)
    return alert


# ── 自测 (python draw_signal.py) ──
if __name__ == "__main__":
    # 1) 市场P(平)基础
    print("market_draw_prob(2.0,3.2,3.8) =", round(market_draw_prob(2.0, 3.2, 3.8), 3))
    print("market_draw_prob(1.5,4.0,6.0) =", round(market_draw_prob(1.5, 4.0, 6.0), 3))
    # 2) 跨庄家共识 (用DB真实匹配, 取一场都有数据的)
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT home_team_norm, away_team_norm, close_home_odds, close_draw_odds, "
        "close_away_odds, match_date, league_name FROM william_ht "
        "WHERE league_name='英超' AND close_draw_odds>1.01 LIMIT 1").fetchone()
    con.close()
    if row:
        h, a, oh, od, oa, md, lg = row
        sig = consensus_draw_signal(h, a, oh, od, oa, md, lg)
        print("consensus sample:", {k: (round(v, 3) if isinstance(v, float) else v) for k, v in sig.items()})
    print("draw_signal module OK")

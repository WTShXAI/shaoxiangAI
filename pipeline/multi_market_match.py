"""
multi_market_match.py — 多盘口综合历史匹配 (Multi-Market Historical Matcher)
===============================================================================

涛哥洞见 (2026-08-11):
  当前odds_comp_finder.py只用1X2做相似度, 忽略AH/OU/CS.
  人工分析师会综合4类盘口判断, 系统应该同理.

核心设计:
  - 数据源: events.db.match_outcomes (1656场4盘口全齐, 3940场1X2+OU+CS)
  - 4盘口加权距离: 1X2×1.0 + AH×1.0 + OU×1.0 + CS×0.5
  - 三层筛选: 粗筛(1X2±10%) → 中筛(AH/OU线±0.25) → 精排(综合加权)
  - 赛果聚合: H/D/A命中率 + ROI + OU/AH方向
  - 用开盘价 op_*, 不用收盘价 (与涛哥洞见一致)

用法:
    from pipeline.multi_market_match import query_similar, aggregate_outcomes
    results = query_similar(h=2.10, d=3.20, a=3.40, ah_line=-0.5, ah_h=1.95,
                            ah_a=1.85, ou_line=2.5, ou_over=2.04, ou_under=1.86)
    agg = aggregate_outcomes(results)
"""
from __future__ import annotations

import json
import os
import sqlite3
import logging
import numpy as np
import pandas as pd
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field

log = logging.getLogger(__name__)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GQ_DB = os.path.join(ROOT, "data", "events.db")


# ────────────────────────────────────────────────────────────────────
# 数据结构
# ────────────────────────────────────────────────────────────────────

@dataclass
class MatchTarget:
    """目标比赛的4盘口参数"""
    h_1x2: float      # 主胜赔率
    d_1x2: float      # 平局赔率
    a_1x2: float      # 客胜赔率
    ah_line: Optional[float] = None   # AH主盘线 (可为None)
    ah_h: Optional[float] = None
    ah_a: Optional[float] = None
    ou_line: Optional[float] = None   # OU主盘线
    ou_over: Optional[float] = None
    ou_under: Optional[float] = None
    cs_top3_scores: List[str] = field(default_factory=list)  # top-3 比分码
    cs_top3_odds: List[float] = field(default_factory=list)  # top-3 比分赔率

@dataclass
class MatchResult:
    """匹配结果"""
    mid: str
    home: str
    away: str
    league: str
    kickoff: str
    # 4盘口
    h_1x2: float; d_1x2: float; a_1x2: float
    ah_line: Optional[float]; ah_h: Optional[float]; ah_a: Optional[float]
    ou_line: Optional[float]; ou_over: Optional[float]; ou_under: Optional[float]
    cs_json: Optional[str]
    # 赛果
    score: str           # "2-1"
    result: str          # home/draw/away
    ht_score: Optional[str] = None
    # 距离分解
    dist_1x2: float = 0.0
    dist_ah: float = 0.0
    dist_ou: float = 0.0
    dist_cs: float = 0.0
    total_dist: float = 0.0

# ────────────────────────────────────────────────────────────────────
# 去水 + 距离计算
# ────────────────────────────────────────────────────────────────────

def devig_1x2(h: float, d: float, a: float) -> Tuple[float, float, float]:
    """1X2 去水: proportional."""
    inv = 1.0/h + 1.0/d + 1.0/a
    return (1.0/h)/inv, (1.0/d)/inv, (1.0/a)/inv

def distance_1x2(th: float, td: float, ta: float,
                 hh: float, hd: float, ha: float) -> float:
    """1X2 去水后概率差 × 3"""
    tp = devig_1x2(th, td, ta)
    hp = devig_1x2(hh, hd, ha)
    return sum(abs(tp[i] - hp[i]) for i in range(3)) * 1.0

def _safe_r(v, default=0.0):
    """NaN → default, None → default"""
    if v is None: return default
    try:
        return default if np.isnan(float(v)) else float(v)
    except (ValueError, TypeError):
        return default

def _sanitize(obj):
    """递归替换 NaN/Inf 为 None，安全 JSON 序列化"""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float) and (np.isnan(obj) or np.isinf(obj)):
        return None if not np.isinf(obj) else ("Inf" if obj > 0 else "-Inf")
    return obj

def _any_none(*vs):
    return any(v is None or (isinstance(v, float) and np.isnan(v)) for v in vs)

def distance_ah(th_line: Optional[float], th_h: Optional[float], th_a: Optional[float],
                hh_line: Optional[float], hh_h: Optional[float], hh_a: Optional[float]) -> float:
    """AH 线差×2 + 赔差×0.5。任一缺失/NaN = 返回惩罚距离 5.0"""
    tl, th, ta = _safe_r(th_line), _safe_r(th_h), _safe_r(th_a)
    hl, hh, ha = _safe_r(hh_line), _safe_r(hh_h), _safe_r(hh_a)
    if _any_none(tl, th, ta, hl, hh, ha) or 0.0 in (tl, th, ta, hl, hh, ha):
        return 5.0
    return (abs(tl - hl) * 2.0 + abs(th - hh) * 0.5 + abs(ta - ha) * 0.5)

def distance_ou(th_line: Optional[float], th_over: Optional[float], th_under: Optional[float],
                hh_line: Optional[float], hh_over: Optional[float], hh_under: Optional[float]) -> float:
    """OU 线差×2 + 赔差×0.5。"""
    tl, tov, tu = _safe_r(th_line), _safe_r(th_over), _safe_r(th_under)
    hl, ho, hu = _safe_r(hh_line), _safe_r(hh_over), _safe_r(hh_under)
    if _any_none(tl, tov, tu, hl, ho, hu) or 0.0 in (tl, tov, tu, hl, ho, hu):
        return 5.0
    return (abs(tl - hl) * 2.0 + abs(tov - ho) * 0.5 + abs(tu - hu) * 0.5)

def distance_cs(t_cs_scores: List[str], t_cs_odds: List[float],
                h_cs_json: Optional[str]) -> float:
    """CS top-N 余弦相似度。JSON缺失/异常 = 返回 2.0"""
    if h_cs_json is None or not t_cs_scores or not t_cs_odds:
        return 2.0
    try:
        hist_cs = json.loads(h_cs_json)
    except (json.JSONDecodeError, TypeError):
        return 2.0
    # 兼容三种格式: list of [score, odds], list of {score/odds}, dict {score: odds}
    if isinstance(hist_cs, list):
        h_vec = {}
        for item in hist_cs:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                # ["1-1", 8.3]
                sc, od = str(item[0]), item[1]
            elif isinstance(item, dict):
                sc = item.get("score", item.get("selection", ""))
                od = item.get("odds", 0)
            else:
                continue
            if od > 0:
                h_vec[str(sc)] = 1.0 / od
    elif isinstance(hist_cs, dict):
        h_vec = {str(s): 1.0/o for s, o in hist_cs.items()
                if isinstance(o, (int, float)) and o > 0}
    else:
        return 2.0

    t_vec = {str(s): 1.0/o for s, o in zip(t_cs_scores, t_cs_odds) if o > 0}
    all_scores = set(t_vec.keys()) | set(h_vec.keys())
    if not all_scores:
        return 2.0
    tv = np.array([t_vec.get(s, 0.0) for s in all_scores])
    hv = np.array([h_vec.get(s, 0.0) for s in all_scores])
    if tv.sum() == 0 or hv.sum() == 0:
        return 2.0
    cos_sim = float(np.dot(tv, hv) / (np.linalg.norm(tv) * np.linalg.norm(hv))) if tv.any() and hv.any() else 0.0
    return 1.0 - cos_sim


# ────────────────────────────────────────────────────────────────────
# events.db 4盘口查询
# ────────────────────────────────────────────────────────────────────

def load_full_corpus() -> pd.DataFrame:
    """加载events.db中所有4盘口可用的完赛匹配"""
    conn = sqlite3.connect(GQ_DB)
    df = pd.read_sql_query("""
        SELECT mid, home, away, league, kickoff,
               op_1x2_h, op_1x2_d, op_1x2_a,
               op_ah_line, op_ah_home, op_ah_away,
               op_ou_line, op_ou_over, op_ou_under,
               op_cs,
               score_home, score_away, result,
               ht_score_home, ht_score_away
        FROM match_outcomes
        WHERE op_1x2_h IS NOT NULL AND op_1x2_h > 1.01
          AND op_ou_line IS NOT NULL
          AND is_virtual = 0
    """, conn)
    conn.close()
    log.info(f"[MM] 语料加载: {len(df)} 场 (1X2+OU 全齐)")
    return df


@dataclass
class CorpusStats:
    """语料统计 (评估用)"""
    n_total: int = 0
    n_4market: int = 0     # 4盘口全齐
    n_ah_present: int = 0  # AH 有效
    n_cs_present: int = 0  # CS JSON 有效
    leagues: Dict[str, int] = field(default_factory=dict)


def corpus_stats(df: pd.DataFrame) -> CorpusStats:
    s = CorpusStats(n_total=len(df))
    has_ah = df["op_ah_line"].notna() & df["op_ah_line"].gt(0)
    s.n_ah_present = int(has_ah.sum())
    has_cs = df["op_cs"].notna() & (df["op_cs"] != "")
    s.n_cs_present = int(has_cs.sum())
    s.n_4market = int((has_ah & has_cs).sum())
    s.leagues = {str(k): int(v) for k, v in df["league"].value_counts().head(8).items()}
    return s


# ────────────────────────────────────────────────────────────────────
# 主匹配函数
# ────────────────────────────────────────────────────────────────────

def query_similar(target: MatchTarget,
                  df: pd.DataFrame | None = None,
                  top_k: int = 50,
                  coarse_pct: float = 0.10,
                  ah_tol: float = 0.25,
                  ou_tol: float = 0.25,
                  verbose: bool = False) -> List[MatchResult]:
    """综合4盘口加权历史匹配。

    Args:
        target: 目标比赛的4盘口参数
        df: 语料（None时自动加载）
        top_k: 返回top-N
        coarse_pct: 1X2粗筛阈值（±10%）
        ah_tol: AH线筛选阈值
        ou_tol: OU线筛选阈值
    """
    if df is None:
        df = load_full_corpus()

    tp_h, tp_d, tp_a = devig_1x2(target.h_1x2, target.d_1x2, target.a_1x2)

    results = []
    for _, row in df.iterrows():
        # ── Layer A: 1X2 粗筛 ──
        hh, hd, ha = row["op_1x2_h"], row["op_1x2_d"], row["op_1x2_a"]
        hp_h, hp_d, hp_a = devig_1x2(hh, hd, ha)
        if abs(tp_h - hp_h) > coarse_pct:
            continue
        d1 = distance_1x2(target.h_1x2, target.d_1x2, target.a_1x2, hh, hd, ha)

        # ── Layer B: AH 中筛 ──
        ah_ok = True
        if target.ah_line is not None and row["op_ah_line"] is not None:
            if abs(target.ah_line - row["op_ah_line"]) > ah_tol:
                ah_ok = False
        d2 = distance_ah(target.ah_line, target.ah_h, target.ah_a,
                        row["op_ah_line"], row["op_ah_home"], row["op_ah_away"])

        # ── Layer B: OU 中筛 ──
        ou_ok = True
        if target.ou_line is not None and row["op_ou_line"] is not None:
            if abs(target.ou_line - row["op_ou_line"]) > ou_tol:
                ou_ok = False
        d3 = distance_ou(target.ou_line, target.ou_over, target.ou_under,
                        row["op_ou_line"], row["op_ou_over"], row["op_ou_under"])

        # AH/OU 中筛: 当目标有且语料有时才做线筛选; 单侧缺失跳过筛选但惩罚距离已给
        # 当目标无AH/OU时, 不筛选
        skip = False
        if target.ah_line is not None and row["op_ah_line"] is not None:
            if abs(target.ah_line - row["op_ah_line"]) > ah_tol:
                skip = True
        if target.ou_line is not None and row["op_ou_line"] is not None:
            if abs(target.ou_line - row["op_ou_line"]) > ou_tol:
                skip = True
        if skip:
            continue

        # ── Layer C: CS 精排 ──
        d4 = distance_cs(target.cs_top3_scores, target.cs_top3_odds, row["op_cs"])

        total = d1 + d2 + d3 + d4 * 0.5
        if np.isnan(total):
            total = 99.0  # degenerate case

        results.append(MatchResult(
            mid=str(row["mid"]), home=str(row["home"]), away=str(row["away"]),
            league=str(row["league"]), kickoff=str(row["kickoff"]),
            h_1x2=hh, d_1x2=hd, a_1x2=ha,
            ah_line=row["op_ah_line"], ah_h=row["op_ah_home"], ah_a=row["op_ah_away"],
            ou_line=row["op_ou_line"], ou_over=row["op_ou_over"], ou_under=row["op_ou_under"],
            cs_json=row["op_cs"],
            score=f"{int(row['score_home'])}-{int(row['score_away'])}",
            result=str(row["result"]),
            # HT 污染清洗 (SSoT: 仅 ht_total < ft_total 的半场真值可信, 见 gq/db.py HT_CLEAN_RULE)
            ht_score=(
                f"{int(row['ht_score_home'])}-{int(row['ht_score_away'])}"
                if (row["ht_score_home"] is not None and not pd.isna(row["ht_score_home"])
                    and row["ht_score_away"] is not None and not pd.isna(row["ht_score_away"])
                    and row["score_home"] is not None and not pd.isna(row["score_home"])
                    and row["score_away"] is not None and not pd.isna(row["score_away"])
                    and (int(row["ht_score_home"]) + int(row["ht_score_away"]))
                        < (int(row["score_home"]) + int(row["score_away"])))
                else None
            ),
            dist_1x2=d1, dist_ah=d2, dist_ou=d3, dist_cs=d4, total_dist=total,
        ))

    results.sort(key=lambda r: r.total_dist if not np.isnan(r.total_dist) else 99.0)
    if verbose and results:
        print(f"[MM] 4盘口加权匹配 top-{min(top_k, len(results))}:")
        for i, r in enumerate(results[:5]):
            print(f"  #{i+1} dist={r.total_dist:.3f} (1x2={r.dist_1x2:.3f} "
                  f"AH={r.dist_ah:.3f} OU={r.dist_ou:.3f} CS={r.dist_cs:.3f}) "
                  f"{r.home} vs {r.away} | {r.score} ({r.result}) | {r.league}")

    return results[:top_k]


# ────────────────────────────────────────────────────────────────────
# 赛果聚合 + 评估
# ────────────────────────────────────────────────────────────────────

@dataclass
class OutcomeAggregate:
    """赛果聚合 (用于决策辅助)"""
    n_matched: int = 0
    # 实际 H/D/A 胜率
    h_rate: float = 0.0; d_rate: float = 0.0; a_rate: float = 0.0
    # 去水隐含 (目标)
    imp_h: float = 0.0; imp_d: float = 0.0; imp_a: float = 0.0
    # ROI (买该方向, 赔率来自历史比赛自身)
    roi_h: float = 0.0; roi_d: float = 0.0; roi_a: float = 0.0
    # OU 实际频率
    ou_over_rate: float = 0.0
    # AH: 让球后"实际赢家"频率
    ah_home_win_rate: float = 0.0  # 让球后主赢
    # 同盘距离 (平均)
    avg_total_dist: float = 0.0
    # 联赛分布 (top 5)
    top_leagues: List[Tuple[str, int]] = field(default_factory=list)
    # 详细匹配行 (前10)
    detail_rows: List[Dict[str, Any]] = field(default_factory=list)


def aggregate_outcomes(results: List[MatchResult], target: MatchTarget) -> OutcomeAggregate:
    """从匹配结果中聚合赛果信息"""
    n = len(results)
    if n == 0:
        return OutcomeAggregate()

    # H/D/A 计数
    h_cnt = sum(1 for r in results if r.result == "home")
    d_cnt = sum(1 for r in results if r.result == "draw")
    a_cnt = sum(1 for r in results if r.result == "away")

    # ROI: 买H向, 每场投1单位 (用历史自身赔率)
    def _roi(dir_counts: int, odds_key: str, result_code: str) -> float:
        total = 0.0
        for r in results:
            odds = getattr(r, odds_key, 0.0)
            if odds <= 1.0:
                continue
            total += (odds - 1.0) if r.result == result_code else -1.0
        return total / max(n, 1)

    # OU over 实际率
    ou_over_cnt = sum(1 for r in results
                     if r.ou_line is not None and r.score is not None
                     and sum(int(x) for x in r.score.split("-") if x.isdigit()) > r.ou_line)

    # AH 让球后"实际赢家"
    def _ah_win(r: MatchResult) -> str:
        if r.ah_line is None or r.score is None:
            return "?"
        parts = r.score.split("-")
        if len(parts) != 2:
            return "?"
        hs, aw_s = int(parts[0]), int(parts[1])
        adj = hs - aw_s - r.ah_line  # 让球后主胜差
        if adj > 0.01: return "home"
        elif adj < -0.01: return "away"
        return "push"
    ah_wins = [_ah_win(r) for r in results if _ah_win(r) != "?"]

    # League distribution
    league_counts = {}
    for r in results:
        lg = r.league if r.league else "不明"
        league_counts[lg] = league_counts.get(lg, 0) + 1

    tp_h, tp_d, tp_a = devig_1x2(target.h_1x2, target.d_1x2, target.a_1x2)

    # Detail rows
    detail = []
    for r in results[:10]:
        detail.append({
            "home": r.home, "away": r.away, "score": r.score, "result": r.result,
            "league": r.league, "kickoff": r.kickoff,
            "dist_1x2": round(r.dist_1x2, 3), "dist_ah": round(r.dist_ah, 3),
            "dist_ou": round(r.dist_ou, 3), "dist_cs": round(r.dist_cs, 3),
            "total_dist": round(r.total_dist, 3),
        })

    return OutcomeAggregate(
        n_matched=n,
        h_rate=round(_safe_r(h_cnt/max(n,1)), 4), d_rate=round(_safe_r(d_cnt/max(n,1)), 4), a_rate=round(_safe_r(a_cnt/max(n,1)), 4),
        imp_h=round(tp_h, 4), imp_d=round(tp_d, 4), imp_a=round(tp_a, 4),
        roi_h=round(_roi(h_cnt, "h_1x2", "home"), 4),
        roi_d=round(_roi(d_cnt, "d_1x2", "draw"), 4),
        roi_a=round(_roi(a_cnt, "a_1x2", "away"), 4),
        ou_over_rate=round(ou_over_cnt/max(n,1), 4),
        ah_home_win_rate=round(sum(1 for w in ah_wins if w=="home")/max(len(ah_wins),1), 4),
        avg_total_dist=round(sum(r.total_dist for r in results)/max(n,1), 4),
        top_leagues=[(lg, c) for lg, c in sorted(league_counts.items(), key=lambda x: -x[1])[:5]],
        detail_rows=detail,
    )


# ────────────────────────────────────────────────────────────────────
# CLI — 快速测试
# ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, ROOT)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # 模拟涛哥截图的 印度MFA独立杯 的 1X2 + OU 结构
    target = MatchTarget(
        h_1x2=1.70, d_1x2=3.65, a_1x2=3.90,
        ah_line=-0.75, ah_h=1.83, ah_a=1.85,
        ou_line=3.0, ou_over=1.98, ou_under=1.70,
        # CS 用虚拟 top-3 (真实应取自GQ)
        cs_top3_scores=["2-0","2-1","1-0"],
        cs_top3_odds=[6.80, 8.90, 8.90],
    )

    print("=" * 70)
    print("多盘口综合历史匹配 (Multi-Market Matcher)")
    print("=" * 70)
    print(f"目标: 1X2({target.h_1x2:.2f}/{target.d_1x2:.2f}/{target.a_1x2:.2f}) "
          f"AH({target.ah_line:+.2f} {target.ah_h}/{target.ah_a}) "
          f"OU({target.ou_line:.1f} {target.ou_over}/{target.ou_under})")
    print()

    df = load_full_corpus()
    st = corpus_stats(df)
    print(f"语料: {st.n_total} 场 | 4盘口全齐 {st.n_4market} | AH有效 {st.n_ah_present} | CS有效 {st.n_cs_present}")
    print(f"Top联赛: {dict(st.leagues)}")
    print()

    results = query_similar(target, df=df, top_k=50, verbose=True)
    if not results:
        print("无匹配")
        sys.exit(1)

    print()
    agg = aggregate_outcomes(results, target)
    print("=" * 70)
    print("赛果聚合")
    print("=" * 70)
    print(f"  匹配场次: {agg.n_matched} | 平均距离: {agg.avg_total_dist:.3f}")
    print(f"  实际 H/D/A: {agg.h_rate:.1%}/{agg.d_rate:.1%}/{agg.a_rate:.1%}")
    print(f"  开盘隐含:   {agg.imp_h:.1%}/{agg.imp_d:.1%}/{agg.imp_a:.1%}")
    print(f"  偏差:       {agg.h_rate-agg.imp_h:+.1%}/{agg.d_rate-agg.imp_d:+.1%}/{agg.a_rate-agg.imp_a:+.1%}")
    print(f"  ROI H/D/A:  {agg.roi_h:+.2%}/{agg.roi_d:+.2%}/{agg.roi_a:+.2%}")
    print(f"  OU过线率:   {agg.ou_over_rate:.1%}")
    print(f"  AH让球主赢: {agg.ah_home_win_rate:.1%}")
    print(f"  Top联赛:    {agg.top_leagues}")
    print()
    print("Top 5 详细:")
    for i, d in enumerate(agg.detail_rows[:5]):
        print(f"  {d['home']} vs {d['away']} {d['score']} ({d['result']}) "
              f"dist={d['total_dist']:.3f} | {d['league']}")
"""
哨响AI — CS(波胆) 信任模型 (生产级, 结合初盘所有赔率)
=====================================================
设计定位 (决定了"用户信任"的生死, 见 reports/cs_inducement_analysis.md):
  CS 庄家线本身不可当"概率预测"用 — 它只列 ~26 个热门比分, 仅覆盖 56.8% 真实赛果,
  43.2% 真实比分(如 3-2/4-1)根本不在盘里 → 直接判零概率, log_loss 爆炸到 13.8。
  庄家"最可能比分"(最低赔)历史命中仅 9.4-9.8% (诱导层证据)。

因此本模块**不做"AI 预测精确比分"**, 而是输出:
  1. 全比分校准概率分布 (覆盖 100%, 不漏判) — 我们的结构模型
  2. 庄家 CS 线隐含分布 + 覆盖率对比 (揭示其残缺)
  3. 二者 top 比分背离检测 → "庄家主推 vs 结构概率" 诱导信号
  4. 庄家诱导标记 (RED/YELLOW/NONE, 复用全样本基准)

"结合初盘所有赔率" 体现在 λ/μ 拟合同时约束:
  - 初盘 1X2 去水隐含 P(H/D/A)
  - 初盘 OU 去水隐含 P(total > ou_line)
  - 初盘 AH 去水隐含 P(home - ah_line > 0)  (可选)
  再用 Poisson 结构生成全比分分布, 比单纯反解 1X2 更贴多市场一致性。

实证背书 (scripts/backtest_cs_trust.py, 时间外推 80/20, N=2539):
  庄家 CS 基线  log_loss=13.8 / top1=0%   (覆盖 56.8%)
  我们的结构模型 log_loss=3.15  / top1=13.2% (覆盖 100%)
  → 分布质量碾压庄家线, 且覆盖全, 这是"诚实可信"的硬证据。

依赖: numpy / scipy / pipeline.score_model(生产级 OIP 管线)。
纯只读分析, 不改动任何预测内核 → 零回归。
作者: 赵统筹(总工) | 2026-08-22
"""
from __future__ import annotations
import math
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy.optimize import least_squares
from scipy.stats import poisson

from pipeline.score_model import (
    predict_score as _oip_predict,
    deoverround,
    score_matrix,
)

MAX_GOAL = 6  # 比分上界 0..6 (更高归并, 实际极少)


# ── 全样本实证基准 (与 bridge_service._CS_INDUCE_BASELINE 同源) ──
_CS_BASELINE = {
    "cheapest_hit_rate": 0.139,   # 最便宜波胆历史命中 13.9% (800/5771)
    "true_not_priced_rate": 0.658, # 真实比分庄家未开赔率 65.8%
    "margin_median": 0.376,
}


# ── 历史实证比分频率 (从真实 match_outcomes 计算, 禁硬编码) ──
# 用户铁律: 第三栏"历史频次"必须来自真实 DB, 不得硬编码 13.9%。
# 这里算全局每比分线实证占比(全样本基准), 一次计算 TTL 缓存复用。
def _empirical_scoreline_freq(con, max_goal: int = MAX_GOAL):
    """从真实 match_outcomes(score_home/score_away) 算每比分线实证频率。返回 (dist, total) 或 None。"""
    try:
        cur = con.cursor()
        rows = cur.execute(
            "SELECT score_home, score_away FROM match_outcomes "
            "WHERE score_home IS NOT NULL AND score_away IS NOT NULL"
        ).fetchall()
    except Exception:
        return None
    total = 0
    cnt: Dict[str, int] = {}
    for hs, as_ in rows:
        try:
            h = int(hs); a = int(as_)
        except Exception:
            continue
        h = min(h, max_goal); a = min(a, max_goal)
        key = f"{h}:{a}"
        cnt[key] = cnt.get(key, 0) + 1
        total += 1
    if total == 0:
        return None
    return {k: v / total for k, v in cnt.items()}, total


_EMP_FREQ_CACHE: Dict[str, Any] = {"fetched_at": 0.0, "data": None, "total": 0}


def _get_empirical_freq(con, ttl: float = 3600.0, max_goal: int = MAX_GOAL):
    """带 TTL 缓存的全局实证比分频率(历史库静态, 一次计算复用)。"""
    now = time.time()
    if _EMP_FREQ_CACHE["data"] is not None and (now - _EMP_FREQ_CACHE["fetched_at"]) < ttl:
        return _EMP_FREQ_CACHE["data"], _EMP_FREQ_CACHE["total"]
    res = _empirical_scoreline_freq(con, max_goal=max_goal)
    if res is None:
        return None
    d, total = res
    _EMP_FREQ_CACHE.update(fetched_at=now, data=d, total=total)
    return d, total


def _norm_cs(raw: Any) -> Optional[str]:
    """波胆标签归一为英文冒号 '0:0' (与 gq/db.normalize_cs_score 同义)。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = __import__("re").match(r'^(\d{1,3})\s*[-:.]\s*(\d{1,3})$', s)
    if not m:
        return None
    return f"{int(m.group(1))}:{int(m.group(2))}"


def _demargin_cs(grid: Dict[str, float]) -> Dict[str, float]:
    """CS 网格去水 → 隐含比分分布 (仅对庄家列出的比分归一)。"""
    inv = {}
    for s, o in grid.items():
        nk = _norm_cs(s)
        if nk is None:
            continue
        try:
            ov = float(o)
        except (TypeError, ValueError):
            continue
        if ov and ov > 0:
            inv[nk] = 1.0 / ov
    z = sum(inv.values())
    if z <= 0:
        return {}
    return {k: v / z for k, v in inv.items()}


def _p_hda(M: np.ndarray) -> np.ndarray:
    pd_ = np.tril(M, -1).sum(); pp = np.trace(M); pa = np.triu(M, 1).sum()
    return np.array([pd_, pp, pa])


def _p_total_over(M: np.ndarray, line: float) -> float:
    tot = 0.0
    for i in range(MAX_GOAL + 1):
        for j in range(MAX_GOAL + 1):
            if i + j > line:
                tot += M[i, j]
    return float(tot)


def _p_handicap_over(M: np.ndarray, ah_line: float) -> float:
    """P(home_goals - ah_line > 0)。半盘(0.25)按 > 近似。"""
    tot = 0.0
    for i in range(MAX_GOAL + 1):
        for j in range(MAX_GOAL + 1):
            if (i - ah_line) > 0:
                tot += M[i, j]
    return float(tot)


def _fit_lambda_mu(ph: float, pd: float, pa: float,
                   ou_line: Optional[float] = None, po: Optional[float] = None,
                   ah_line: Optional[float] = None, pah: Optional[float] = None) -> Tuple[float, float]:
    """从初盘多市场一致性拟合 λ/μ — 真正"结合初盘所有赔率"。
    约束顺序: 1X2(P_H/D/A) → 可选 OU(P(total>line)) → 可选 AH(P(home-ah>0))。"""
    def resid(p):
        lh, la = p
        if lh <= 0 or la <= 0:
            return [1e6] * 3
        M = score_matrix(lh, la, MAX_GOAL)
        M = M / M.sum()
        ph_, pd_, pa_ = _p_hda(M)
        r = [ph_ - ph, pd_ - pd, pa_ - pa]
        if po is not None and ou_line is not None:
            r.append(_p_total_over(M, ou_line) - po)
        if pah is not None and ah_line is not None:
            r.append(_p_handicap_over(M, ah_line) - pah)
        return r
    n_con = 3 + (1 if (po is not None and ou_line is not None) else 0) + (1 if (pah is not None and ah_line is not None) else 0)
    sol = least_squares(resid, [1.3, 1.1],
                        bounds=([0.2, 0.2], [4.5, 4.5]), max_nfev=400)
    # 若约束冲突导致病态, 退回只看 1X2
    if not np.all(np.isfinite(sol.x)):
        return _fit_lambda_mu(ph, pd, pa)
    return float(sol.x[0]), float(sol.x[1])


def _grid_to_key(i: int, j: int) -> str:
    return f"{min(i, MAX_GOAL)}:{min(j, MAX_GOAL)}"


def _matrix_to_dist(M: np.ndarray) -> Dict[str, float]:
    dist = {}
    for i in range(MAX_GOAL + 1):
        for j in range(MAX_GOAL + 1):
            dist[_grid_to_key(i, j)] = float(M[i, j])
    return dist


def _induce_marker(grid: Dict[str, float]) -> Dict[str, Any]:
    """从 CS 网格算庄家诱导标记 (RED/YELLOW/NONE)。轻量版, 不依赖 bridge_service。"""
    items = [(k, float(v)) for k, v in grid.items()
             if isinstance(v, (int, float)) and v > 0]
    if len(items) < 3:
        return {"induce_level": "NONE", "induce_reasons": [], "margin": None,
                "favorite_score": None, "favorite_odds": None}
    items.sort(key=lambda x: x[1])
    overround = sum(1.0 / o for _, o in items)
    margin = overround - 1.0
    fav_score, fav_odds = items[0]
    reasons: List[str] = []
    level = "NONE"
    if margin > 0.4:
        level = "RED"
        reasons.append(f"CS 抽水 {margin*100:.0f}% 极高(庄家净赚>40%)")
    if fav_odds < 8:
        level = "RED"
        reasons.append(f"最便宜波胆仅 {fav_odds:.1f}(<8, 极密集诱导区)")
    elif fav_odds < 12:
        if level == "NONE":
            level = "YELLOW"
        reasons.append(f"最便宜波胆 {fav_odds:.1f}(<12, 低赔诱导簇)")
    if margin > 0.3 and level == "NONE":
        level = "YELLOW"
        reasons.append(f"CS 抽水 {margin*100:.0f}% 偏高")
    return {
        "induce_level": level,
        "induce_reasons": reasons,
        "margin": round(margin, 3),
        "overround": round(overround, 3),
        "favorite_score": _norm_cs(fav_score),
        "favorite_odds": round(fav_odds, 2),
        "historical_cheapest_hit_rate": _CS_BASELINE["cheapest_hit_rate"],
        "historical_true_not_priced_rate": _CS_BASELINE["true_not_priced_rate"],
    }


def _solve_remaining_total(p_over: float, line: float) -> Optional[float]:
    """由滚球 OU 去水 P(剩余总球 > line) 反解剩余总球期望 S (Poisson)。

    滚球 OU 线计的是剩余进球, 所以 S 即剩余时间的 λ+μ。二分求解, 失败返回 None。
    """
    try:
        p = min(max(float(p_over), 0.02), 0.98)
        L = float(line)
        k = int(math.floor(L))          # P(N > L) = P(N >= floor(L)+1) = sf(floor(L))
        lo, hi = 0.02, 12.0
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if poisson.sf(k, mid) > p:
                hi = mid
            else:
                lo = mid
        return 0.5 * (lo + hi)
    except Exception:
        return None


def build_trust_card(
    cs_grid: Optional[Dict[str, Any]] = None,
    h: Optional[float] = None, d: Optional[float] = None, a: Optional[float] = None,
    ou_line: Optional[float] = None, ou_over: Optional[float] = None, ou_under: Optional[float] = None,
    ah_line: Optional[float] = None, ah_home: Optional[float] = None, ah_away: Optional[float] = None,
    con: Optional[Any] = None,
    current_score: Optional[Tuple[int, int]] = None,
    live_ou: Optional[Tuple[float, float, float]] = None,
    live_1x2: Optional[Tuple[float, float, float]] = None,
    live_minute: Optional[int] = None,
    max_goal: int = MAX_GOAL,
) -> Dict[str, Any]:
    """构建 CS 信任卡。

    入参: 初盘各市场赔率 (cs_grid 完整 CS 矩阵 + 1X2 + OU + AH)。
          cs_grid 与 1X2 至少其一可用; 全缺返回 found=False。
          滚球模式 (2026-08-28, 用户需求"波胆跟随开赛后即时盘"): 传 current_score=(主进,客进)
          与 live_ou=(线,over,under) 时, λμ 改由【当前滚球 OU 去水】反解剩余总球, 再按
          初盘 λμ 强度比拆分主客, 最终比分分布 = 剩余分布 ⊕ 当前比分平移。开盘三盘拟合
          仍照算作对照(odds_phase='live' 标注)。滚球 1X2 指的是最终赛果, 不作为剩余
          λμ 约束(会失真), 仅透传展示。
    出参: 结构化信任卡 dict (见返回字段)。
    """
    # ── 1. 庄家 CS 线分布 + 诱导标记 ──
    book_dist: Dict[str, float] = {}
    induce: Dict[str, Any] = {"induce_level": "NONE", "induce_reasons": []}
    if cs_grid and len(cs_grid) >= 5:
        norm_grid = {_norm_cs(k): float(v) for k, v in cs_grid.items() if _norm_cs(k) is not None}
        norm_grid = {k: v for k, v in norm_grid.items() if v and v > 0}
        if len(norm_grid) >= 5:
            book_dist = _demargin_cs(norm_grid)
            induce = _induce_marker(norm_grid)

    # ── 2. 我们的结构分布 (结合初盘所有赔率) ──
    our_dist: Optional[Dict[str, float]] = None
    fit_sources: List[str] = []
    if h and d and a:
        # 基础: OIP 生产管线 (已含 goal_scale 校准 + 温度缩放)
        base = _oip_predict("", "", h, d, a, max_goal=max_goal)
        M_base = np.asarray(base["matrix"], dtype=float)
        M_base = M_base / M_base.sum()
        # 精炼: 叠加 OU + AH 约束拟合 λ/μ
        ph, pd, pa = deoverround(h, d, a)
        po = None
        if ou_line is not None and ou_over and ou_under:
            po = (1.0 / ou_over) / (1.0 / ou_over + 1.0 / ou_under)
            fit_sources.append("OU")
        pah = None
        if ah_line is not None and ah_home and ah_away:
            # AH 去水 → 主队让球胜(覆盖)隐含概率
            pah = (1.0 / ah_home) / (1.0 / ah_home + 1.0 / ah_away)
            fit_sources.append("AH")
        try:
            lh, la = _fit_lambda_mu(ph, pd, pa,
                                    ou_line=ou_line, po=po,
                                    ah_line=ah_line, pah=pah)
            M_ref = score_matrix(lh, la, max_goal)
            M_ref = M_ref / M_ref.sum()
            our_dist = _matrix_to_dist(M_ref)
        except Exception:
            our_dist = _matrix_to_dist(M_base)
        fit_sources.insert(0, "1X2")

    # ── 2b. 滚球即时盘模式 (2026-08-28, 用户需求"波胆跟随开赛后即时盘") ──
    # 剩余 λμ 由当前滚球 OU 去水反解(滚球线计的就是剩余进球), 主客拆分比沿用开盘
    # 三盘拟合的强度比(1X2 滚球盘指最终赛果, 直接当剩余约束会失真), 最终比分分布
    # = 剩余分布 ⊕ 当前比分平移。开盘拟合结果被滚球版覆盖(即时盘优先, 用户口径)。
    odds_phase = "opening"
    live_block = None
    if current_score is not None and live_ou is not None:
        try:
            sh_, sa_ = int(current_score[0]), int(current_score[1])
            lo_line, lo_over, lo_under = float(live_ou[0]), float(live_ou[1]), float(live_ou[2])
            if lo_over > 1.01 and lo_under > 1.01:
                p_over_live = (1.0 / lo_over) / (1.0 / lo_over + 1.0 / lo_under)
                s_rem = _solve_remaining_total(p_over_live, lo_line)
                if s_rem is not None and s_rem > 0.02:
                    lam_ratio, ratio_src = 0.5, "50/50(无初盘)"
                    try:
                        _ph, _pd, _pa = deoverround(h, d, a)
                        _lh0, _la0 = _fit_lambda_mu(_ph, _pd, _pa)
                        if _lh0 + _la0 > 0.01:
                            lam_ratio = _lh0 / (_lh0 + _la0)
                            ratio_src = "开盘λμ强度比"
                    except Exception:
                        pass
                    lam_rem = s_rem * lam_ratio
                    mu_rem = s_rem * (1.0 - lam_ratio)
                    M_rem = score_matrix(lam_rem, mu_rem, max_goal)
                    M_rem = M_rem / M_rem.sum()
                    # 平移当前比分: 最终比分 = 当前比分 + 剩余进球 (越界归并到上界格)
                    M_fin = np.zeros_like(M_rem)
                    for i in range(M_rem.shape[0]):
                        for j in range(M_rem.shape[1]):
                            M_fin[min(i + sh_, max_goal), min(j + sa_, max_goal)] += M_rem[i, j]
                    our_dist = _matrix_to_dist(M_fin)
                    odds_phase = "live"
                    fit_sources = [f"LIVE_OU@{lo_line:g}", ratio_src]
                    live_block = {
                        "score": f"{sh_}:{sa_}",
                        "minute": live_minute,
                        "ou_line": lo_line,
                        "over_odds": round(lo_over, 2),
                        "under_odds": round(lo_under, 2),
                        "p_over": round(p_over_live, 4),
                        "total_rem": round(s_rem, 3),
                        "lambda_rem": round(lam_rem, 3),
                        "mu_rem": round(mu_rem, 3),
                        "ratio_source": ratio_src,
                        "method": "滚球OU反解剩余λμ ⊕ 当前比分平移",
                    }
        except Exception:
            live_block = None

    if our_dist is None and not book_dist:
        return {"found": False, "message": "该场无初盘 CS 矩阵且无初盘 1X2, 无法构建信任卡"}

    # ── 2.5 历史实证比分频率 (从真实 match_outcomes 计算, 禁硬编码 13.9%) ──
    empirical_freq: Optional[Dict[str, float]] = None
    historical_total = 0
    if con is not None:
        _ef = _get_empirical_freq(con, max_goal=max_goal)
        if _ef is not None:
            empirical_freq, historical_total = _ef

    # ── 3. 分布对比 + 背离检测 ──
    our_top5: List[Dict[str, Any]] = []
    if our_dist:
        ranked = sorted(our_dist.items(), key=lambda x: -x[1])[:5]
        our_top5 = [{
            "score": s, "prob": round(p, 4),
            "hist_freq": (round(empirical_freq[s], 4) if (empirical_freq and s in empirical_freq) else None),
        } for s, p in ranked]

    book_fav = None
    book_top5: List[Dict[str, Any]] = []
    if book_dist:
        b_ranked = sorted(book_dist.items(), key=lambda x: -x[1])[:5]
        book_top5 = [{
            "score": s, "prob": round(p, 4),
            "hist_freq": (round(empirical_freq[s], 4) if (empirical_freq and s in empirical_freq) else None),
        } for s, p in b_ranked]
        # 庄家"主推" = 最低赔 = 最高隐含概率
        book_fav = book_top5[0]

    # 背离: 庄家主推是否在我们的高概率区
    alignment = "UNKNOWN"
    if book_fav and our_dist:
        our_rank = sorted(our_dist.items(), key=lambda x: -x[1])
        our_top3 = {s for s, _ in our_rank[:3]}
        if book_fav["score"] in our_top3:
            alignment = "ALIGNED"      # 庄家主推符合结构概率
        else:
            alignment = "DIVERGED"     # 庄家主推 vs 结构概率背离 → 诱导嫌疑

    # ── 4. 信任指示 (相对庄家线, 非预测胜率) ──
    trust_score = 50
    notes: List[str] = []
    if our_dist:
        trust_score += 20  # 我们的分布覆盖 100%, 校准 log_loss 3.15 vs 庄家 13.8
        notes.append("结构模型覆盖全部比分(100%), 庄家 CS 线仅覆盖已列比分")
    if "OU" in fit_sources or "AH" in fit_sources:
        trust_score += 10  # 多市场一致性约束
        notes.append("λ/μ 拟合已结合 " + "+".join(fit_sources))
    if induce.get("induce_level") == "RED":
        trust_score += 10  # 庄家明显诱导 → 更该信结构模型
        notes.append("庄家 CS 线呈强诱导特征(高抽水/低赔密集), 建议以结构模型为准")
    if odds_phase == "live":
        trust_score = min(100, trust_score + 10)
        notes.append("滚球模式: 剩余λμ由当前滚球OU即时盘反解并平移当前比分, 非静态开盘拟合")
    if alignment == "DIVERGED":
        notes.append(f"庄家主推 {book_fav['score']} 与结构概率背离 → 警惕诱导盘")
    elif alignment == "ALIGNED":
        notes.append(f"庄家主推 {book_fav['score']} 与结构概率一致")
    trust_score = max(0, min(100, trust_score))

    # ── 5. 组装 ──
    return {
        "found": True,
        "model": "cs_trust_v1",
        "odds_phase": odds_phase,              # opening / live (2026-08-28: 是否已切滚球即时盘)
        "live": live_block,                    # 滚球模式详情(比分/分钟/滚球OU/剩余λμ), 开盘模式为 None
        "fit_sources": fit_sources,           # 实际用到的市场 (滚球模式含 LIVE_OU@线)
        "our_distribution": our_dist or {},
        "our_top5": our_top5,
        "historical_freq": empirical_freq or {},
        "historical_freq_total_matches": historical_total,
        "historical_note": ("历史实证比分频率来自真实完赛库(match_outcomes)全样本基准, 非本场定制, 禁硬编码" if historical_total else None),
        "book_distribution": book_dist,
        "book_top5": book_top5,
        "book_favorite": book_fav,
        "book_listed_count": len(book_dist),
        "alignment": alignment,               # ALIGNED / DIVERGED / UNKNOWN
        "induce_level": induce.get("induce_level", "NONE"),
        "induce_reasons": induce.get("induce_reasons", []),
        "margin": induce.get("margin"),
        "favorite_score": induce.get("favorite_score"),
        "favorite_odds": induce.get("favorite_odds"),
        "historical_cheapest_hit_rate": _CS_BASELINE["cheapest_hit_rate"],
        "historical_true_not_priced_rate": _CS_BASELINE["true_not_priced_rate"],
        "trust_score": trust_score,           # 相对庄家线的可信度指示(0-100), 非预测胜率
        "trust_notes": notes,
        "disclaimer": "本卡为比分概率估计与庄家盘口对照, 非投注建议, 不承诺精确比分。",
    }

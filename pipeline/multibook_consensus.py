"""多庄 sharp 共识引擎 — 哨响AI 破单庄天花板的核心武器 (SSoT)

为什么存在
----------
MEMORY 铁律: 真 edge 来源 = 跨庄/跨市场 soft line 价差; 单庄去水不具此条件。
开源借脑(2026-07-25)确认: worldcup-predictor / wc26-predict / mezzala 跨仓库共识——
  1X2 方向唯一能破单庄天花板的方向 = **多庄 sharp 共识** (sharp vs retail divergence)。
本引擎是 cross_book_edge 的升级版: 不再是"全庄中位数共识", 而是
  **sharp 庄锚定共识 + retail 庄背离检测 + 方向 edge (value/fade side)**。

铁律映射
--------
- 单庄去水无 edge → 本引擎只用跨庄结构, 绝把单庄当真相。
- sharp 庄 (威廉希尔/立博, 未来接 Pinnacle) 收盘线 ≈ 真实概率代理;
  retail 庄偏离 sharp 共识 = 可被利用的 mispricing。
- '官方*' 是聚合代理线(非独立庄), 一律排除, 防双重计数污染共识。

方法来源 (署名致谢, MIT)
------------------------
devig_power / consensus_market / blend / edge / kelly_fraction 改编自
  github.com/rrclaw/worldcup-predictor (skill/model/market.py, MIT License),
  幂法去水比比例法更能抑制 favorite-longshot bias, 与本项目 flb_adjust 一致。

用法
----
  from pipeline.multibook_consensus import analyze_all, to_report
  res = analyze_all()                 # list[MatchConsensus]
  report = to_report(res)             # 可序列化 dict
  # 命令行: python -m pipeline.multibook_consensus
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from pathlib import Path
from statistics import mean

import numpy as np

# ============================================================
# 庄家分类 (基于 雷速 leisu_odds 实际庄名, 2026-07-25 实测)
# ============================================================
# sharp 庄: 收盘线最贴近真实概率, 作共识锚。Pinnacle(平博=平*) 收盘线最 sharp, 待采集到即生效。
SHARP_BOOKS = {"威***", "立*", "平*"}  # 威廉希尔 William Hill / 立博 Ladbrokes / 平博 Pinnacle(sharpest, 待采集)
# 聚合代理线(非独立庄, 雷速自家合成的"市场平均"), 必须排除, 防双重计数。
EXCLUDE_BOOKS = {"官方", "官方(+1)", "官方(-1)"}
# 其余 = retail 庄 (澳* 易** 皇* 3* 10* 厝*** 星* ...)

LEISU_DB_PATH = Path("D:/Architecture/data/football_data.db")

# retail 背离阈值(pp): 单 retail 庄某选项概率与 sharp 共识差超此值 = 软线
DEFAULT_DIVERGE_PP = 3.0


# ============================================================
# 去水 / 共识 / 融合 (改编自 worldcup-predictor market.py, MIT)
# ============================================================
def implied_from_decimal(odds: list[float]) -> np.ndarray:
    return np.array([1.0 / o for o in odds], dtype=float)


def devig_proportional(odds: list[float]) -> np.ndarray:
    """最简去水: 1/o 归一化。"""
    p = implied_from_decimal(odds)
    return p / p.sum()


def devig_power(odds: list[float], tol: float = 1e-9) -> np.ndarray:
    """幂法去水: 找 k 使 sum(p_i**k)=1。比比例法更抑 FLB。失败回退比例法。

    改编自 rrclaw/worldcup-predictor (MIT)。
    """
    raw = implied_from_decimal(odds)
    lo, hi = 0.5, 2.0
    for _ in range(60):
        k = 0.5 * (lo + hi)
        s = np.sum(raw ** k)
        if abs(s - 1) < tol:
            break
        if s > 1:
            lo = k
        else:
            hi = k
    out = raw ** k
    out = out / out.sum()
    return out if np.all(np.isfinite(out)) else devig_proportional(odds)


def consensus_market(books: list[list[float]], method: str = "power") -> np.ndarray | None:
    """多庄去水概率均值 (1X2 序 H,D,A)。"""
    if not books:
        return None
    fn = devig_power if method == "power" else devig_proportional
    probs = np.array([fn(b) for b in books if b and len(b) == 3])
    if probs.size == 0:
        return None
    return probs.mean(axis=0)


def blend(p_market: np.ndarray | None, p_model: np.ndarray, w: float) -> np.ndarray:
    """P_final = w*market + (1-w)*model。无市场则回退模型。

    改编自 rrclaw/worldcup-predictor (MIT)。与市场锚定 ensemble 哲学一致(v7.4)。
    """

    def _safe_norm(p: np.ndarray) -> np.ndarray:
        p = np.clip(np.nan_to_num(np.asarray(p, dtype=float)), 0.0, None)
        s = p.sum()
        return p / s if s > 0 else np.full(len(p), 1.0 / len(p))

    p_model = _safe_norm(p_model)
    if p_market is None:
        return p_model
    out = w * _safe_norm(p_market) + (1.0 - w) * p_model
    return out / out.sum()


def edge(p_final: np.ndarray, p_market: np.ndarray | None) -> np.ndarray | None:
    if p_market is None:
        return None
    return np.asarray(p_final) - np.asarray(p_market)


def kelly_fraction(p: float, decimal_odds: float) -> float:
    """单注凯利 fraction (无 edge 为 0)。"""
    b = decimal_odds - 1.0
    f = (p * b - (1.0 - p)) / b if b > 0 else 0.0
    return max(0.0, float(f))


# ============================================================
# 数据加载 (雷速 leisu_odds)
# ============================================================
@dataclass
class BookRow:
    bookmaker: str
    h: float
    d: float
    a: float
    h_prob: float = 0.0
    d_prob: float = 0.0
    a_prob: float = 0.0
    is_sharp: bool = False


@dataclass
class MatchConsensus:
    home: str
    away: str
    n_books: int
    n_sharp: int
    has_true_sharp: bool
    sharp_books: list = field(default_factory=list)
    sharp_consensus: dict = field(default_factory=dict)     # {h,d,a} 锚定共识
    all_consensus: dict = field(default_factory=dict)       # {h,d,a} 全庄共识(含retail)
    retail_mean: dict = field(default_factory=dict)         # {h,d,a} retail 均值
    max_spread_pp: float = 0.0
    divergences: list = field(default_factory=list)         # retail 背离: {book,sel,pp,prob,consensus}
    value_side: dict = field(default_factory=dict)          # {outcome,pp} retail 最低估=价值侧
    fade_side: dict = field(default_factory=dict)           # {outcome,pp} retail 最高估=该 fade


def load_leisu_groups(db: Path = LEISU_DB_PATH, market: str = "1X2",
                      include_mock: bool = False) -> list[dict]:
    """从 leisu_odds 聚合为 match 列表。market 默认 '1X2'。
    分组键: match_id 非空用 match_id, 否则 (home_raw, away_raw)。
    同 (分组, 庄家) 只保留 capture_at 最新快照。排除 EXCLUDE_BOOKS(官方*)。
    """
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            """SELECT match_id, home_raw, away_raw, market, book,
                      odds_h, odds_d, odds_a, capture_at
               FROM leisu_odds
               WHERE market = ?
                 AND (1 = ? OR source != 'mock')
               ORDER BY COALESCE(match_id, -1), home_raw, away_raw, book, capture_at""",
            (market, 1 if include_mock else 0),
        ).fetchall()
    finally:
        conn.close()

    latest: dict = {}
    for mid, hr, ar, mk, bk, oh, od, oa, cap in rows:
        if bk in EXCLUDE_BOOKS:
            continue
        key = (mid if mid is not None else (hr, ar, mk), bk)
        cap = cap or 0
        if key not in latest or cap > latest[key][-1]:
            latest[key] = (mid, hr, ar, mk, bk, oh, od, oa, cap)

    groups: dict = {}
    for (_gk, _bk), (mid, hr, ar, mk, bk, oh, od, oa, _cap) in latest.items():
        grp = (mid, hr, ar, mk)
        book_row = BookRow(bk, oh or 0.0, od or 0.0, oa or 0.0, is_sharp=(bk in SHARP_BOOKS))
        groups.setdefault(grp, []).append(book_row)

    out = []
    for grp, books in groups.items():
        _mid, hr, ar, _mk = grp
        # 只保留有效赔率
        valid = [b for b in books if b.h > 0 and b.d > 0 and b.a > 0]
        if valid:
            out.append({"home": hr, "away": ar, "books": valid})
    return out


# ============================================================
# 核心分析
# ============================================================
def analyze_match(m: dict, diverge_pp: float = DEFAULT_DIVERGE_PP) -> MatchConsensus:
    books = m["books"]
    sharp = [b for b in books if b.is_sharp]
    retail = [b for b in books if not b.is_sharp]

    # 逐庄去水
    for b in books:
        p = devig_power([b.h, b.d, b.a])
        b.h_prob, b.d_prob, b.a_prob = p[0], p[1], p[2]

    has_true_sharp = len(sharp) >= 1

    if has_true_sharp:
        # sharp 锚定共识 = sharp 庄去水概率均值
        sc = np.array([devig_power([b.h, b.d, b.a]) for b in sharp]).mean(axis=0)
        used_sharp = [b.bookmaker for b in sharp]
    else:
        # 无 true sharp: 全庄共识作 sharp 代理 (标 has_true_sharp=False)
        sc = np.array([devig_power([b.h, b.d, b.a]) for b in books]).mean(axis=0)
        used_sharp = [b.bookmaker for b in books]

    # 全庄共识(含 retail) — 对照用
    all_c = np.array([devig_power([b.h, b.d, b.a]) for b in books]).mean(axis=0)
    # retail 均值
    if retail:
        rm = np.array([devig_power([b.h, b.d, b.a]) for b in retail]).mean(axis=0)
    else:
        rm = sc.copy()

    # retail 背离: 每 retail 庄 vs sharp 共识
    divs = []
    for b in retail:
        for sel, bp, op in (("H", sc[0], b.h_prob), ("D", sc[1], b.d_prob), ("A", sc[2], b.a_prob)):
            diff = (op - bp) * 100.0   # retail 高于 sharp = 正(高估)
            if abs(diff) >= diverge_pp:
                divs.append({
                    "book": b.bookmaker, "sel": sel,
                    "pp": round(diff, 2),
                    "prob": round(op, 4), "consensus": round(bp, 4),
                })

    # 离散度
    spread_h = max(b.h_prob for b in books) - min(b.h_prob for b in books)
    spread_d = max(b.d_prob for b in books) - min(b.d_prob for b in books)
    spread_a = max(b.a_prob for b in books) - min(b.a_prob for b in books)
    max_spread = max(spread_h, spread_d, spread_a)

    # 方向 edge: sharp 共识 - retail 均值 (正=retail 低估=价值侧; 负=retail 高估=该 fade)
    gap = sc - rm
    sels = ["H", "D", "A"]
    val_idx = int(np.argmax(gap))
    fade_idx = int(np.argmin(gap))
    value_side = {"outcome": sels[val_idx], "pp": round(float(gap[val_idx]) * 100, 2)}
    fade_side = {"outcome": sels[fade_idx], "pp": round(float(gap[fade_idx]) * 100, 2)}

    return MatchConsensus(
        home=m["home"], away=m["away"],
        n_books=len(books), n_sharp=len(sharp), has_true_sharp=has_true_sharp,
        sharp_books=used_sharp,
        sharp_consensus={"h": round(float(sc[0]), 4), "d": round(float(sc[1]), 4), "a": round(float(sc[2]), 4)},
        all_consensus={"h": round(float(all_c[0]), 4), "d": round(float(all_c[1]), 4), "a": round(float(all_c[2]), 4)},
        retail_mean={"h": round(float(rm[0]), 4), "d": round(float(rm[1]), 4), "a": round(float(rm[2]), 4)},
        max_spread_pp=round(float(max_spread) * 100, 2),
        divergences=divs,
        value_side=value_side, fade_side=fade_side,
    )


def analyze_all(db: Path = LEISU_DB_PATH, diverge_pp: float = DEFAULT_DIVERGE_PP,
                include_mock: bool = False) -> list[MatchConsensus]:
    return [analyze_match(m, diverge_pp) for m in load_leisu_groups(db, "1X2", include_mock)]


def sharp_consensus_for_match(home: str, away: str, db: Path = LEISU_DB_PATH,
                              include_mock: bool = False) -> dict | None:
    """查 leisu_odds 同场, 若有 true sharp 庄返回 {h,d,a} sharp 共识; 否则 None。
    供 unified_predictor 作跨庄 market prior (有则增强, 无则回退单源)。"""
    for m in load_leisu_groups(db, "1X2", include_mock):
        if m["home"] == home and m["away"] == away:
            res = analyze_match(m)
            if res.has_true_sharp:
                return res.sharp_consensus
    return None


def tilt_score_matrix_to_sharp(home: str, away: str, base_matrix: "np.ndarray",
                                db: Path = LEISU_DB_PATH, include_mock: bool = False):
    """把任意波胆矩阵(OIP/DC)的 HDA 边缘乘性缩放到多庄 sharp 1X2 共识。

    复用 dc_score_model.tilt_to_outcomes。无 true sharp 庄 -> 返回原矩阵 + used=False(安全 no-op)。
    背景: 当前全库无多庄 CS 赔率(leisu 只有 1X2, GQ.cs 为单庄), 故用多庄 1X2 共识锚定波胆 HDA,
          把"多庄 sharp 共识"杠杆落到波胆推荐上(开源共识: outcome-constrained tilt)。
    返回: (tilted_matrix, used: bool)
    """
    from pipeline.dc_score_model import tilt_to_outcomes
    cons = sharp_consensus_for_match(home, away, db, include_mock)
    if cons is None:
        return np.asarray(base_matrix, dtype=float), False
    target = np.array([cons["h"], cons["d"], cons["a"]], dtype=float)
    return tilt_to_outcomes(np.asarray(base_matrix, dtype=float), target), True


def to_report(results: list[MatchConsensus], diverge_pp: float = DEFAULT_DIVERGE_PP) -> dict:
    matches = []
    n_div = sum(1 for r in results if r.divergences)
    n_sharp = sum(1 for r in results if r.has_true_sharp)
    for r in results:
        matches.append({
            "home": r.home, "away": r.away,
            "n_books": r.n_books, "n_sharp": r.n_sharp, "has_true_sharp": r.has_true_sharp,
            "sharp_books": r.sharp_books,
            "sharp_consensus": r.sharp_consensus,
            "all_consensus": r.all_consensus,
            "retail_mean": r.retail_mean,
            "max_spread_pp": r.max_spread_pp,
            "n_divergences": len(r.divergences),
            "divergences": r.divergences,
            "value_side": r.value_side,
            "fade_side": r.fade_side,
        })
    matches.sort(key=lambda m: m["max_spread_pp"], reverse=True)
    return {
        "module": "multibook_consensus",
        "source": "data/football_data.db.leisu_odds",
        "method": "power-devig + sharp-anchored consensus + retail divergence",
        "sharp_books": sorted(SHARP_BOOKS),
        "excluded_books": sorted(EXCLUDE_BOOKS),
        "diverge_threshold_pp": diverge_pp,
        "n_matches": len(results),
        "n_with_true_sharp": n_sharp,
        "n_matches_with_divergence": n_div,
        "matches": matches,
    }


def _console_summary(report: dict) -> str:
    lines = []
    lines.append(f"多庄 sharp 共识 — {report['n_matches']} 场 "
                 f"(含 true sharp {report['n_with_true_sharp']} 场), "
                 f"{report['n_matches_with_divergence']} 场有 retail 背离(阈值 {report['diverge_threshold_pp']}pp)")
    lines.append(f"sharp 庄={report['sharp_books']}  排除={report['excluded_books']}")
    lines.append("=" * 78)
    for m in report["matches"]:
        c = m["sharp_consensus"]
        tag = "★sharp" if m["has_true_sharp"] else "≈全庄代理"
        lines.append(f"{m['home']} vs {m['away']}  | {m['n_books']}庄/{m['n_sharp']}sharp {tag} | 离散 {m['max_spread_pp']}pp")
        lines.append(f"   sharp共识 H/D/A = {c['h']*100:.1f}% / {c['d']*100:.1f}% / {c['a']*100:.1f}%")
        vs = m["value_side"]; fs = m["fade_side"]
        lines.append(f"   方向edge: 价值侧={vs['outcome']}(+{vs['pp']}pp)  该fade={fs['outcome']}({fs['pp']}pp)")
        if m["divergences"]:
            for d in m["divergences"][:4]:
                arrow = "↑高估" if d["pp"] > 0 else "↓低估"
                lines.append(f"   ⚠ {d['book']} {d['sel']} {arrow} {abs(d['pp'])}pp (该庄 {d['prob']*100:.1f}% vs sharp {d['consensus']*100:.1f}%)")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-mock", action="store_true")
    ap.add_argument("--pp", type=float, default=DEFAULT_DIVERGE_PP)
    args = ap.parse_args()
    res = analyze_all(diverge_pp=args.pp, include_mock=args.include_mock)
    report = to_report(res, args.pp)
    print(_console_summary(report))
    out = Path("data/multibook_consensus_report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出 {out}")

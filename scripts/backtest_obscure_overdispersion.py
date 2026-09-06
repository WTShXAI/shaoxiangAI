"""obscure 联赛比分分布肥尾校准回测 (2026-09-02)

目的: 验证 score_model.score_matrix 的负二项过度离散(kappa)是否改善 obscure 联赛
      的比分分布校准(logloss↓), 且不破坏 top-N 命中率; 主流(kappa=0)必须逐位不变(零回归)。

方法:
  - 取 events.db 完场 + 开盘1X2 + 非虚拟 + score_missing=0 的比赛 (约2383场, 几乎全 obscure)。
  - 每场复现生产路径: league=_lookup_league → liquidity → overdisp=overdispersion_for_liquidity。
  - 比分矩阵 = predict_score(overdispersion=overdisp)["matrix"] (goal_scale=1.2, 非WC)。
  - 指标: logloss = -ln P(actual); top1/top3 命中率; mean P(actual)。
  - 扫 obscure kappa ∈ {0.15,0.25,0.35,0.50} (mid/liquid 固定=0或0.12), 选 obscure 子集 logloss 最小者。
  - 主流(overdisp=0)在 current 与所有 proposed 下矩阵相同 → 零回归。

输出: 扫表 + 选定 kappa 后的 obscure 子集明细 + 主流零回归校验。
"""
import os, sys, sqlite3, math, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import bridge_service as bs
from pipeline.score_model import predict_score
from pipeline.league_scoring_prior import (
    overdispersion_for_liquidity, match_league, classify_liquidity,
)

DB = os.path.join("data", "events.db")
SWEEP = [0.15, 0.25, 0.35, 0.50]


def load_rows():
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT mo.home, mo.away, mo.op_1x2_h oh, mo.op_1x2_d od, mo.op_1x2_a oa,
               mo.score_home sh, mo.score_away sa, m.league
        FROM match_outcomes mo
        LEFT JOIN matches m ON m.mid = mo.mid
        WHERE mo.score_home IS NOT NULL AND mo.score_away IS NOT NULL
          AND mo.score_home >= 0 AND mo.score_away >= 0
          AND mo.is_virtual = 0
          AND mo.op_1x2_h > 0 AND mo.op_1x2_d > 0 AND mo.op_1x2_a > 0
          AND (m.score_missing IS NULL OR m.score_missing = 0)
    """).fetchall()
    con.close()
    return rows


def overdisp_for(league):
    if not league:
        return 0.0
    return overdispersion_for_liquidity(classify_liquidity(league, match_league(league)))


def matrix_of(home, away, oh, od, oa, kappa):
    r = predict_score(home, away, oh, od, oa, goal_scale=1.2, overdispersion=kappa)
    return r["matrix"]


def logloss_topn(sh, sa, M):
    mg = M.shape[0] - 1
    if sh > mg or sa > mg:
        return None
    p = max(float(M[sh, sa]), 1e-12)
    ll = -math.log(p)
    flat = M.flatten()
    order = np.argsort(-flat)
    top1 = (sh, sa) == divmod(order[0], mg + 1)
    top3 = (sh, sa) in [divmod(k, mg + 1) for k in order[:3]]
    return dict(ll=ll, p=p, top1=top1, top3=top3)


def main():
    rows = load_rows()
    print(f"加载可回测比赛 = {len(rows)}")

    # 构建明细 + 分类
    detail, non_obsc = [], []
    for r in rows:
        league = r["league"] or bs._lookup_league(r["home"], r["away"])
        od_ = overdisp_for(league)
        rec = dict(home=r["home"], away=r["away"], oh=r["oh"], od=r["od"], oa=r["oa"],
                   sh=int(r["sh"]), sa=int(r["sa"]), league=league, prod_od=od_)
        (non_obsc if od_ == 0 else detail).append(rec)
    print(f"  obscure(overdisp>0) = {len(detail)} | 非obscure(overdisp=0) = {len(non_obsc)}")

    # ── 扫表: 仅 obscure 子集 (非obscure 在所有 kappa 下矩阵相同, 不参与) ──
    print("\n== 扫表: obscure 子集 (n=%d) ==" % len(detail))
    print(f"{'obscure_kappa':>14s} {'logloss':>9s} {'meanP':>8s} {'top1%':>7s} {'top3%':>7s}")
    results = {}
    for k in SWEEP:
        lls, ps, t1, t3 = [], [], 0, 0
        for d in detail:
            M = matrix_of(d["home"], d["away"], d["oh"], d["od"], d["oa"], k)
            m = logloss_topn(d["sh"], d["sa"], M)
            if m is None:
                continue
            lls.append(m["ll"]); ps.append(m["p"])
            if m["top1"]: t1 += 1
            if m["top3"]: t3 += 1
        n = len(lls)
        print(f"{k:>14.2f} {sum(lls)/n:>9.4f} {sum(ps)/n:>8.4f} {100*t1/n:>6.1f}% {100*t3/n:>6.1f}%")
        results[k] = dict(logloss=sum(lls)/n, meanP=sum(ps)/n, top1=100*t1/n, top3=100*t3/n)

    # 对照: 生产 overdisp (current)
    lls, ps, t1, t3 = [], [], 0, 0
    for d in detail:
        M = matrix_of(d["home"], d["away"], d["oh"], d["od"], d["oa"], d["prod_od"])
        m = logloss_topn(d["sh"], d["sa"], M)
        if m is None:
            continue
        lls.append(m["ll"]); ps.append(m["p"])
        if m["top1"]: t1 += 1
        if m["top3"]: t3 += 1
    n = len(lls)
    print(f"{'current(生产)':>14s} {sum(lls)/n:>9.4f} {sum(ps)/n:>8.4f} {100*t1/n:>6.1f}% {100*t3/n:>6.1f}%")
    results["current"] = dict(logloss=sum(lls)/n, meanP=sum(ps)/n, top1=100*t1/n, top3=100*t3/n)

    best_k = min(SWEEP, key=lambda k: results[k]["logloss"])
    print(f"\n== 选定 obscure kappa = {best_k} (logloss 最低) ==")
    print(f"   current: logloss={results['current']['logloss']:.4f} meanP={results['current']['meanP']:.4f} top1={results['current']['top1']:.1f}% top3={results['current']['top3']:.1f}%")
    print(f"   k={best_k}: logloss={results[best_k]['logloss']:.4f} meanP={results[best_k]['meanP']:.4f} top1={results[best_k]['top1']:.1f}% top3={results[best_k]['top3']:.1f}%")
    print(f"   Δlogloss = {results[best_k]['logloss']-results['current']['logloss']:+.4f} (负=校准改善)")

    # 主流零回归校验
    print("\n== 主流零回归校验 ==")
    samp = non_obsc[:50]
    bad = sum(1 for d in samp if overdisp_for(d["league"]) != 0.0)
    print(f"   抽样非obscure {len(samp)} 场, overdisp 非0 的 = {bad} (应为0 → 生产路径恒传 overdisp=0 → 纯Poisson, 零回归)")

    out = dict(sweep=results, best_obscure_kappa=best_k,
               n_obscure=len(detail), n_non_obscure=len(non_obsc), n_total=len(rows))
    with open("scripts/backtest_obscure_overdispersion_report.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n报告已写 scripts/backtest_obscure_overdispersion_report.json")


if __name__ == "__main__":
    main()

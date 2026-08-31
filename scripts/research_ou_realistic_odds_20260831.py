"""OU 融合 选择性策略: 收盘前赔率重新结算 (2026-08-31 研究)
================================================================
上轮纸盘 +12.84% 是用"开盘赔率(opening_ou)"结算的理想化上限(=开盘下注持有到终场)。
真实前向里信号触发时只能拿当时赔率。本研究用 odds_snapshots 中该场主OU盘
"最新赛前快照"作为下注价(收盘前价)重新结算同一选择性策略(|edge|>=0.02 双边),
检验 edge 在"市场修正后"是否仍存活。
- 同时报 opening-odds ROI(复现) 与 closing-odds ROI(现实) 对照。
- bootstrap 95% CI + 5段时间窗 + 按主盘线(2.5 vs 其他)集中度。
- 绝不真实下注。
"""
from __future__ import annotations
import os, sys, json, sqlite3, datetime
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.build_fused_models_20260831 import collect, fl_probs, p_over, FIT_FRAC
import joblib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS = os.path.join(ROOT, "models")
DB = os.path.join(ROOT, "data", "events.db")
REPORTS = os.path.join(ROOT, "reports")
f_ou = joblib.load(os.path.join(MODELS, "fused_ou_20260831.joblib"))
EDGE_MIN = 0.02

def fused_ou_pover(a, b):
    return float(f_ou["meta"].predict_proba(np.array([[a, b]]))[0, 1])

def implied(ov, un):
    return (1/ov)/((1/ov)+(1/un))

def closing_ou(con, mk, line, ko):
    """该场主OU盘最新赛前快照的 over/under 赔(收盘前价)。"""
    rows = con.execute(
        "SELECT selection, odds, captured_at FROM odds_snapshots "
        "WHERE match_key=? AND market LIKE 'OU_%' AND market NOT LIKE 'OU_1H%' "
        "AND line=? AND captured_at <= ? ORDER BY captured_at DESC",
        (mk, line, ko)).fetchall()
    d = {}
    for sel, o, ca in rows:
        if sel not in d and o and o > 1.01:
            d[sel] = o
    return d.get("over"), d.get("under")

def settle_roi(records, price_fn):
    """records: list of (model_p, imp_open, y, line, side_fn...) ; price_fn 返回 (ov,un) 下注价。
    返回 (roi, n_bets, profits) 其中只下 |edge|>=EDGE_MIN 的注, 用 price_fn 给的赔率结算。"""
    profits = []
    for r in records:
        edge = r["model_p"] - r["imp_open"]
        if abs(edge) < EDGE_MIN:
            continue
        ov, un = price_fn(r)
        if ov is None or un is None or ov <= 1.01 or un <= 1.01:
            continue
        side = "over" if edge > 0 else "under"
        odds = ov if side == "over" else un
        win = (side == "over" and r["y"] == 1) or (side == "under" and r["y"] == 0)
        profits.append((odds - 1) if win else -1.0)
    if not profits:
        return float("nan"), 0, []
    return float(np.mean(profits)), len(profits), profits

def bootstrap(bs_list):
    if len(bs_list) < 30:
        return [None, None]
    arr = np.array(bs_list)
    rng = np.random.default_rng(20260831)
    bs = np.array([arr[rng.integers(0, len(arr), len(arr))].mean() for _ in range(1000)])
    return [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]

def seg_rois(profits_sorted):
    if len(profits_sorted) < 5:
        return [float(np.mean(profits_sorted)) if profits_sorted else None]
    segs = np.array_split(np.array(profits_sorted, dtype=object), 5)
    return [float(np.mean([float(x) for x in s])) if len(s) else None for s in segs]

def main():
    recs = collect()
    recs.sort(key=lambda r: r["ko"])
    k = int(len(recs) * FIT_FRAC)
    cutoff = recs[k-1]["ko"]
    paper = sorted([r for r in recs if r["ko"] > cutoff], key=lambda r: r["ko"])
    con = sqlite3.connect(DB, timeout=60); con.row_factory = sqlite3.Row

    recs_rec = []
    skipped_closing = 0
    for r in paper:
        fl = fl_probs(r["oh"], r["od"], r["oa"], r["line"], r["ov"], r["un"],
                      r["ah_line"], r["ah_h"], r["ah_a"], r["league"], r["ko"])
        fl_ou_pover = fl["ou"][0] if fl["ou"] else None  # fl_model_ou 已下线(2026-08-31)
        po = float(p_over(r["lam"][0], r["lam"][1], r["line"]))
        model_p = fused_ou_pover(fl_ou_pover, po)
        imp_open = implied(r["ov"], r["un"])
        tot = r["sh"] + r["sa"]
        y = 1 if tot > r["line"] else 0
        cov, cun = closing_ou(con, r["mk"], r["line"], r["ko"])
        recs_rec.append(dict(model_p=model_p, imp_open=imp_open, y=y, line=r["line"],
                              ov_open=r["ov"], un_open=r["un"], ov_close=cov, un_close=cun,
                              ko=r["ko"], league=r["league"]))
        if cov is None or cun is None:
            skipped_closing += 1
    con.close()
    print(f"纸盘宇宙 n={len(recs_rec)} | 无收盘前快照(跳过结算) {skipped_closing}")

    # opening-odds 结算(复现)
    roi_open, n_open, p_open = settle_roi(recs_rec, lambda r: (r["ov_open"], r["un_open"]))
    # closing-odds 结算(现实)
    roi_close, n_close, p_close = settle_roi(recs_rec, lambda r: (r["ov_close"], r["un_close"]))

    # 仅比较两者都有赔率的子集(同口径)
    both = [r for r in recs_rec if r["ov_close"] and r["un_close"]]
    roi_open_b, n_open_b, p_open_b = settle_roi(both, lambda r: (r["ov_open"], r["un_open"]))
    roi_close_b, n_close_b, p_close_b = settle_roi(both, lambda r: (r["ov_close"], r["un_close"]))

    ci_open = bootstrap(p_open_b)
    ci_close = bootstrap(p_close_b)

    # 低线(2.0-2.75) vs 高线(3.0+) 在收盘前价下的 5段时间窗稳定性(假设可信度检验, 仍同源)
    def seg_by_filter(sub, lo, hi):
        sel = [r for r in sub if lo <= r["line"] <= hi]
        pr = settle_roi(sel, lambda r: (r["ov_close"], r["un_close"]))[2]
        if len(pr) < 5:
            return None
        segs = np.array_split(np.array(pr, dtype=object), 5)
        return [float(np.mean([float(x) for x in s])) if len(s) else None for s in segs]
    low_seg = seg_by_filter(both, 2.0, 2.75)
    high_seg = seg_by_filter(both, 3.0, 9.9)

    # 按主盘线集中度(用同口径子集)
    by_line = {}
    for r in both:
        key = f"{r['line']:.2f}"
        by_line.setdefault(key, []).append(r)
    line_roi = {}
    for key, lst in by_line.items():
        ro, n, pr = settle_roi(lst, lambda r: (r["ov_close"], r["un_close"]))
        if n >= 20:
            line_roi[key] = dict(n=n, roi=round(ro, 4), ci=bootstrap(pr))

    out = dict(
        generated_at=datetime.datetime.now().astimezone().isoformat(),
        edge_min=EDGE_MIN, cutoff_kickoff=cutoff,
        paper_n=len(recs_rec), no_closing_snapshot=skipped_closing,
        same_universe_roi=dict(
            opening=dict(roi=round(roi_open, 4), n=n_open, ci=[round(x,4) if x else None for x in ci_open]),
            closing=dict(roi=round(roi_close, 4), n=n_close),
        ),
        matched_subset_n=len(both),
        matched_comparison=dict(
            opening_roi=round(roi_open_b, 4), closing_roi=round(roi_close_b, 4),
            opening_ci=[round(x,4) if x else None for x in ci_open],
            closing_ci=[round(x,4) if x else None for x in ci_close],
        ),
        by_line_closing=line_roi,
        low_line_2_275_closing_segments=low_seg,
        high_line_3plus_closing_segments=high_seg,
        verdict=("EDGE_SURVIVES_CLOSING" if (roi_close_b > 0 and (ci_close[0] or 0) > 0)
                 else "EDGE_IS_OPENING_PRICE_INEFFICIENCY"),
    )
    with open(os.path.join(REPORTS, "research_ou_realistic_odds_20260831.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\n[全宇宙] opening ROI={roi_open:+.2%}(n={n_open}) | closing ROI={roi_close:+.2%}(n={n_close})")
    print(f"[同口径子集 n={len(both)}] opening={roi_open_b:+.2%} CI[{ci_open[0]:+.2%},{ci_open[1]:+.2%}] | "
          f"closing={roi_close_b:+.2%} CI[{ci_close[0]:+.2%},{ci_close[1]:+.2%}]")
    print("[按主盘线 closing ROI] " + " | ".join(f"{k}:{v['roi']:+.2%}(n={v['n']})" for k, v in line_roi.items()))
    if low_seg:
        print(f"[低线2.0-2.75 closing 5段] " + " | ".join(f"{('—' if v is None else f'{v:+.2%}')}" for v in low_seg))
    if high_seg:
        print(f"[高线3.0+ closing 5段]  " + " | ".join(f"{('—' if v is None else f'{v:+.2%}')}" for v in high_seg))
    print(f"判定: {out['verdict']}")
    print("-> reports/research_ou_realistic_odds_20260831.json")

if __name__ == "__main__":
    main()

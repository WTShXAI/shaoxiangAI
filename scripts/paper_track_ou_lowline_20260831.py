"""OU 融合 低线(2.0-2.75)窄策略 受控纸盘 harness (2026-08-31)
================================================================
研究(research_ou_realistic_odds)发现: OU 融合在收盘前现实价下, 低线(2.0-2.75)
5段时间窗全正(+7%~+38%), 高线(3.0+)全负 → 结构化偏差, 非通用edge。
本 harness 把"低线窄策略"落成前向监控: 宇宙=训练cutoff后干净场且主盘线∈[2.0,2.75];
仅当 |model P(over)-开盘去水隐含| >= 0.02 双边下注; 用收盘前价(odds_snapshots 最新赛前快照)结算(现实)。
输出: 逐注CSV + 汇总JSON(累计ROI/bootstrap CI/5段时间窗/decay_flag)。绝不真实下注。
"""
from __future__ import annotations
import os, sys, json, sqlite3, csv, datetime
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
LOW, HIGH = 2.0, 2.75

def fused_ou_pover(a, b):
    if a is None:
        return float(b)  # fl_model_ou 已下线(2026-08-31): 纯泊松回退
    return float(f_ou["meta"].predict_proba(np.array([[a, b]]))[0, 1])

def implied(ov, un):
    return (1/ov)/((1/ov)+(1/un))

def closing_ou(con, mk, line, ko):
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

def main():
    recs = collect()
    recs.sort(key=lambda r: r["ko"])
    k = int(len(recs) * FIT_FRAC)
    cutoff = recs[k-1]["ko"]
    paper = sorted([r for r in recs if r["ko"] > cutoff], key=lambda r: r["ko"])
    con = sqlite3.connect(DB, timeout=60); con.row_factory = sqlite3.Row

    rows, cum = [], 0.0
    skipped = 0
    for r in paper:
        if not (LOW <= r["line"] <= HIGH):
            continue
        fl = fl_probs(r["oh"], r["od"], r["oa"], r["line"], r["ov"], r["un"],
                      r["ah_line"], r["ah_h"], r["ah_a"], r["league"], r["ko"])
        model_p = fused_ou_pover(fl["ou"][0] if fl["ou"] else None, float(p_over(r["lam"][0], r["lam"][1], r["line"])))  # fl_model_ou 已下线(2026-08-31) → None 回退纯泊松
        imp = implied(r["ov"], r["un"])
        edge = model_p - imp
        tot = r["sh"] + r["sa"]; y = 1 if tot > r["line"] else 0
        cov, cun = closing_ou(con, r["mk"], r["line"], r["ko"])
        if cov is None or cun is None or cov <= 1.01 or cun <= 1.01:
            skipped += 1
            continue
        side = odds = profit = ""
        if abs(edge) >= EDGE_MIN:
            side = "over" if edge > 0 else "under"
            odds = cov if side == "over" else cun
            profit = (odds - 1) if ((side == "over" and y == 1) or (side == "under" and y == 0)) else -1.0
            cum += profit
        rows.append(dict(ko=r["ko"], league=r["league"], line=r["line"],
                         implied=round(imp, 4), model_p=round(model_p, 4), edge=round(edge, 4),
                         side=side, odds=round(odds, 3) if odds != "" else "",
                         y=y, profit=round(profit, 3) if profit != "" else ""))
    con.close()

    bets = [x for x in rows if x["side"]]
    n = len(bets)
    roi = float(np.mean([x["profit"] for x in bets])) if n else float("nan")
    profits = np.array([float(x["profit"]) for x in bets]) if n else np.array([])
    ci = [None, None]
    if n >= 30:
        rng = np.random.default_rng(20260831)
        bs = np.array([profits[rng.integers(0, n, n)].mean() for _ in range(1000)])
        ci = [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]
    bets_sorted = sorted(bets, key=lambda x: x["ko"])
    segs = np.array_split(np.array(bets_sorted, dtype=object), 5) if n >= 5 else [np.array(bets_sorted, dtype=object)]
    seg_roi = [float(np.mean([float(x["profit"]) for x in s])) if len(s) else None for s in segs]
    decay = False
    valid = [v for v in seg_roi if v is not None]
    if len(valid) >= 2 and (valid[-1] < 0 or valid[-1] < 0.5 * float(np.median(valid))):
        decay = True

    csv_path = os.path.join(REPORTS, "paper_track_ou_lowline.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ko","league","line","implied","model_p","edge","side","odds","y","profit"])
        w.writeheader(); [w.writerow(x) for x in rows]
    summary = dict(generated_at=datetime.datetime.now().astimezone().isoformat(),
        strategy="OU低线窄策略 2.0-2.75 |edge|>=0.02 收盘前价结算",
        edge_min=EDGE_MIN, cutoff_kickoff=cutoff,
        paper_universe_lowline=len([r for r in paper if LOW <= r["line"] <= HIGH]),
        no_closing_snapshot=skipped, bets_placed=n,
        roi_per_bet=round(roi, 4) if n else None, roi_bootstrap_ci=[round(x,4) if x else None for x in ci],
        segment_roi=[None if v is None else round(v, 4) for v in seg_roi], decay_flag=decay,
        note="研究发现的候选窄策略前向监控; 同源数据发现, 须前向新数据确认; 绝不真实下注")
    with open(os.path.join(REPORTS, "paper_track_ou_lowline_summary_20260831.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"[低线窄策略] 宇宙(2.0-2.75)={summary['paper_universe_lowline']} 无收盘快照={skipped} | 下注 {n} | ROI={roi:+.2%} CI[{ci[0]:+.2%},{ci[1]:+.2%}]")
    print(f"[5段时间窗] " + " | ".join(f"{('—' if v is None else f'{v:+.2%}')}" for v in seg_roi))
    print(f"[衰减] {'🔴' if decay else '🟢'}")
    print("-> paper_track_ou_lowline.csv + _summary_20260831.json")

if __name__ == "__main__":
    main()

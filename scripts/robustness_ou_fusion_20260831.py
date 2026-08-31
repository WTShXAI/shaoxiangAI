"""OU 融合 ROI 鲁棒性压力测试 (2026-08-31, IR-30 诚实边界)
================================================================
回测显示 OU 融合 AUC 仅 0.619 却平注 ROI +7.55%(对朴素 -1.53%)。
这种"判别力一般但 ROI 转正"最易被单点回测误导, 故独立四重压测:
  1) Bootstrap 95% CI (2000 次重采样)
  2) 时间分段稳定性 (OOS 778 按 kickoff 切 3 段)
  3) 阈值敏感性 (edge margin 0~0.05)
  4) 剔除空名联赛 (306场里取100的那个人造/未知联赛)
结论独立给出, 不依赖训练脚本打印值。
"""
from __future__ import annotations
import os, sys, json, datetime
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.build_fused_models_20260831 import collect, fl_probs, predict_lambdas, p_over, FIT_FRAC
import joblib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS = os.path.join(ROOT, "models")
f_ou = joblib.load(os.path.join(MODELS, "fused_ou_20260831.joblib"))

def fused_ou_pover(a, b):
    return float(f_ou["meta"].predict_proba(np.array([[a, b]]))[0, 1])

def implied_ou(ov, un):
    return (1/ov)/((1/ov)+(1/un))

def roi_at(p, imp, ov, un, y, m):
    """edge margin m: 仅当 p > imp+m 押 over, 否则押 under; 平注1单位"""
    profit = np.where(p > imp + m,
                       np.where(y == 1, ov - 1, -1.0),
                       np.where(y == 0, un - 1, -1.0))
    return float(profit.mean())

def naive_roi(imp, ov, un, y):
    profit = np.where(imp > 0.5,
                      np.where(y == 1, ov - 1, -1.0),
                      np.where(y == 0, un - 1, -1.0))
    return float(profit.mean())

def main():
    recs = collect()
    recs.sort(key=lambda r: r["ko"])
    k = int(len(recs) * FIT_FRAC)
    te = recs[k:]
    P, IMP, OV, UN, Y, LG = [], [], [], [], [], []
    for r in te:
        lam = r["lam"]
        po = float(p_over(lam[0], lam[1], r["line"]))
        fl = fl_probs(r["oh"], r["od"], r["oa"], r["line"], r["ov"], r["un"],
                      r["ah_line"], r["ah_h"], r["ah_a"], r["league"], r["ko"])
        fl_ou_pover = fl["ou"][0] if fl["ou"] else None   # 同 build/backtest 口径: fl_predictor P(over); fl_model_ou 已下线(2026-08-31)
        line, ov, un = r["line"], r["ov"], r["un"]
        imp = implied_ou(ov, un)
        fp = fused_ou_pover(fl_ou_pover, po)
        tot = r["sh"] + r["sa"]
        P.append(fp); IMP.append(imp); OV.append(ov); UN.append(un)
        Y.append(1 if tot > line else 0); LG.append(r["league"])
    p = np.array(P); imp = np.array(IMP); ov = np.array(OV); un = np.array(UN)
    y = np.array(Y); lg = np.array(LG)
    n = len(y)
    print(f"OOS n={n}")

    base = roi_at(p, imp, ov, un, y, 0.0)
    nav = naive_roi(imp, ov, un, y)
    print(f"\n[全量] 融合ROI(m=0)={base:+.2%} | 朴素ROI={nav:+.2%}")

    # 1) Bootstrap 95% CI (同一组下标, 保持行对齐)
    rng = np.random.default_rng(20260831)
    bs = np.empty(2000)
    for i in range(2000):
        ix = rng.integers(0, n, n)
        bs[i] = roi_at(p[ix], imp[ix], ov[ix], un[ix], y[ix], 0.0)
    lo, hi = np.percentile(bs, [2.5, 97.5])
    print(f"[Bootstrap 2000] ROI 95% CI = [{lo:+.2%}, {hi:+.2%}] | 下限>0? {'是' if lo>0 else '否'}")

    # 2) 时间分段
    order = np.argsort(p)  # 任意序即可, 用 kickoff 需重排; 这里按原 te 顺序(已按ko排序)
    idx = np.arange(n)
    thirds = np.array_split(idx, 3)
    print("[时间分段]")
    slice_roi = []
    for i, s in enumerate(thirds):
        r = roi_at(p[s], imp[s], ov[s], un[s], y[s], 0.0)
        slice_roi.append(float(r))
        print(f"  段{i+1} (n={len(s)}): ROI={r:+.2%}")

    # 3) 阈值敏感性
    print("[阈值敏感性 m]")
    sens = {}
    for m in [0.0, 0.01, 0.02, 0.03, 0.04, 0.05]:
        r = roi_at(p, imp, ov, un, y, m)
        sens[f"{m:.2f}"] = float(r)
        print(f"  m={m:.2f}: ROI={r:+.2%}")

    # 4) 剔除空名联赛
    mask = lg != ""
    if mask.sum() > 0:
        r_ex = roi_at(p[mask], imp[mask], ov[mask], un[mask], y[mask], 0.0)
        print(f"[剔除空名联赛] n={int(mask.sum())} ROI={r_ex:+.2%} (空名联赛贡献 {base-r_ex:+.2%})")
    else:
        r_ex = None

    verdict = "ROBUST" if (lo > 0 and all(s > 0 for s in slice_roi) and sens["0.03"] > 0) else "WEAK/NOISE"
    out = dict(
        generated_at=datetime.datetime.now().astimezone().isoformat(),
        n=n, roi_full=base, roi_naive=nav,
        bootstrap_ci=[float(lo), float(hi)], bootstrap_lo_pos=bool(lo > 0),
        time_slices=slice_roi, threshold_sensitivity=sens,
        roi_excl_empty=float(r_ex) if r_ex is not None else None,
        verdict=verdict,
    )
    with open(os.path.join(ROOT, "reports", "robustness_ou_fusion_20260831.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n压测结论: {verdict}")
    print("-> reports/robustness_ou_fusion_20260831.json")

if __name__ == "__main__":
    main()

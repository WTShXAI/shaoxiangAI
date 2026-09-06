#!/usr/bin/env python3
"""波胆模型 walk-forward 验证: OIP(现有) vs DC-tilted(新) vs 集成.
逐年重拟合 DC (as_of=年初, 3年窗口), 在当年 holdout 上比较波胆 top1/top3 命中率.
对照基线: pipeline/score_model.py 的 OIP 独立 Poisson.
"""
import os, sys, time
import numpy as np
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.dc_score_model import load_iw, fit, predict_score_dc, deoverround
from pipeline.score_model import solve_oip, score_matrix

MAXG = 8

def oip_matrix(oh, od, oa, goal_scale=1.2):
    ph, pd, pa = deoverround(oh, od, oa)
    lh, la = solve_oip(ph, pd, pa, MAXG)
    lh, la = lh * goal_scale, la * goal_scale
    M = score_matrix(lh, la, MAXG); return M / M.sum(), (lh, la)

def topk_hit(top, hs, aw, k):
    return 1 if (hs, aw) in [(i, j) for i, j, _ in top[:k]] else 0

def main():
    t0 = time.time()
    df = load_iw()
    print(f"[load] {len(df)} rows, range {df['date'].min().date()}..{df['date'].max().date()}")
    years = [2021, 2022, 2023, 2024, 2025]
    C = {"oip": [0, 0, 0], "dc": [0, 0, 0], "ens": [0, 0, 0]}  # [top1, top3, n]
    for Y in years:
        asof = pd.Timestamp(f"{Y}-01-01")
        hold = df[(df["date"] >= asof) & (df["date"] < pd.Timestamp(f"{Y+1}-01-01"))].copy()
        if len(hold) == 0:
            continue
        try:
            model = fit(df, asof)
        except ValueError as e:
            print(f"  [{Y}] skip fit: {e}"); continue
        inmodel = set(model["teams"])
        c_oip = [0, 0, 0]; c_dc = [0, 0, 0]; c_ens = [0, 0, 0]
        for r in hold.itertuples(index=False):
            if None in (r.ch, r.cd, r.ca):
                continue
            hs, aw = int(r.home_score), int(r.away_score)
            # OIP
            Moip, _ = oip_matrix(r.ch, r.cd, r.ca)
            fo = np.argsort(-Moip.ravel())[:3]
            c_oip[2] += 1
            c_oip[0] += topk_hit([(int(divmod(k, MAXG+1)[0]), int(divmod(k, MAXG+1)[1]), 0) for k in fo], hs, aw, 1)
            c_oip[1] += topk_hit([(int(divmod(k, MAXG+1)[0]), int(divmod(k, MAXG+1)[1]), 0) for k in fo], hs, aw, 3)
            both = (r.home_team in inmodel) and (r.away_team in inmodel)
            if both:
                res = predict_score_dc(model, r.home_team, r.away_team, (r.ch, r.cd, r.ca),
                                       oip_matrix=Moip, w_oip=0.5, maxg=MAXG)
                c_dc[2] += 1; c_ens[2] += 1
                c_dc[0] += topk_hit(res["top_scores"], hs, aw, 1)
                c_dc[1] += topk_hit(res["top_scores"], hs, aw, 3)
                c_ens[0] += topk_hit(res["top_scores"], hs, aw, 1)
                c_ens[1] += topk_hit(res["top_scores"], hs, aw, 3)
        for key, cc in (("oip", c_oip), ("dc", c_dc), ("ens", c_ens)):
            C[key][0] += cc[0]; C[key][1] += cc[1]; C[key][2] += cc[2]
        print(f"  [{Y}] n_oip={c_oip[2]} n_dc={c_dc[2]} | "
              f"OIP top1={c_oip[0]/c_oip[2]*100:.1f}% top3={c_oip[1]/c_oip[2]*100:.1f}% | "
              f"DC top1={c_dc[0]/c_dc[2]*100:.1f}% top3={c_dc[1]/c_dc[2]*100:.1f}% | "
              f"ENS top1={c_ens[0]/c_ens[2]*100:.1f}% top3={c_ens[1]/c_ens[2]*100:.1f}%")
    print("\n=== 汇总 (2021-2025 walk-forward) ===")
    for key in ("oip", "dc", "ens"):
        n = C[key][2]
        if n:
            print(f"  {key.upper():4} n={n}  top1={C[key][0]/n*100:.2f}%  top3={C[key][1]/n*100:.2f}%")
    print(f"[time] {time.time()-t0:.0f}s")

if __name__ == "__main__":
    main()

"""CS 闸门探针: 在 2025 时间外集上比较 当前 OIP score_model 与 poisson_goals 的比分 top1/top3。
OIP 是 ranked_predictor 当前 CS 唯一来源; 若 poisson 显著更准, 则其比分分布可作 CS 混合分量。
"""
import os
import sys
import time
import numpy as np
import pandas as pd
import train_poisson_goals_20260830 as P
import joblib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

df = P.load()
tr = df[df["year"] < 2024]
te = df[df["year"] >= 2025].reset_index(drop=True)
lstats = P.league_stats(tr)
te2, feats = P.build(te, lstats)
d = joblib.load("models/poisson_goals_20260830.joblib")
mh, ma = d["mh"], d["ma"]; rho = float(d["dc_rho"]); boost = float(d["draw_boost"]); P.DC_RHO = rho
lam_h = np.clip(mh.predict(te2[feats].values), 0.05, 6.0)
lam_a = np.clip(ma.predict(te2[feats].values), 0.05, 6.0)

from pipeline.score_model import predict_score

N = len(te2)
oip_t1 = oip_t3 = 0
poi_t1 = poi_t3 = 0
t0 = time.time()
for i in range(N):
    hg = int(te2.iloc[i]["hg"]); ag = int(te2.iloc[i]["ag"]); truth = f"{hg}-{ag}"
    # OIP
    r = predict_score(te2.iloc[i]["home_team"], te2.iloc[i]["away_team"],
                      float(te2.iloc[i]["close_h"]), float(te2.iloc[i]["close_d"]), float(te2.iloc[i]["close_a"]),
                      max_goal=8, goal_scale=1.2)
    tops = [f"{s}-{j}" for s, j, _ in r["top_scores"][:3]]
    if tops and tops[0] == truth: oip_t1 += 1
    if truth in tops: oip_t3 += 1
    # poisson
    tops2 = [s for s, _ in P.joint_top(lam_h[i], lam_a[i], 3)]
    if tops2 and tops2[0] == truth: poi_t1 += 1
    if truth in tops2: poi_t3 += 1
print(f"比分 top1/top3 (n={N}, {time.time()-t0:.0f}s):")
print(f"  OIP(score_model, 当前CS源): top1 {oip_t1/N*100:.2f}% | top3 {oip_t3/N*100:.2f}%")
print(f"  poisson_goals(候选)     : top1 {poi_t1/N*100:.2f}% | top3 {poi_t3/N*100:.2f}%")
print(f"  Δtop1 { (poi_t1-oip_t1)/N*100:+.2f}pp | Δtop3 {(poi_t3-oip_t3)/N*100:+.2f}pp")

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
eval_william_inter_model.py
----------------------------
加载已训模型, 在同样的时间切分测试集上做诚实评估:
  1) 真实校准: 预测 P(H) 分桶 -> 实际 H 频率
  2) Value-bet ROI: 仅当模型概率显著高于庄家隐含概率时才下注, 看是否真有 edge
"""
import os, joblib, warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV = os.path.join(PROJECT_ROOT, "data/william_inter_training.csv")
CUT = "2022-12-31"
FEATURES = ["open_h","open_d","open_a","close_h","close_d","close_a",
            "close_overround","imp_h","imp_d","imp_a",
            "open_overround","imp_open_h","imp_open_d","imp_open_a",
            "drift_h","drift_d","drift_a","ha_ratio","draw_ratio","fav_implied"]

clf = joblib.load(os.path.join(PROJECT_ROOT, "data/wi_1x2_model.joblib"))
rgr = joblib.load(os.path.join(PROJECT_ROOT, "data/wi_total_model.joblib"))

df = pd.read_csv(CSV, low_memory=False)
df["match_date"] = pd.to_datetime(df["match_date"], errors="coerce")
dated = df["match_date"].notna()
test = df[dated & (df["match_date"] > CUT)].reset_index(drop=True)
Xte = test[FEATURES].values
proba = clf.predict_proba(Xte)
yte = test["result_class"].values
imp = test[["imp_h","imp_d","imp_a"]].values
close = test[["close_h","close_d","close_a"]].values

print("测试集:", len(test), "场 (", test['match_date'].min().date(), "~", test['match_date'].max().date(), ")")

# 1) 真实校准
print("\n=== 真实校准: 预测P(H)分桶 -> 实际H频率 ===")
p_h = proba[:,0]
bins = np.linspace(0,1,11)
idx = np.digitize(p_h, bins)-1
for b in range(10):
    m = idx==b
    if m.sum()>50:
        print(f"  P(H)∈[{bins[b]:.1f},{bins[b+1]:.1f}) : 预测{ p_h[m].mean():.3f} 实际H={ (yte[m]==0).mean():.3f} n={int(m.sum())}")

# 2) Value-bet ROI
# 策略: 对每场取模型 top1, 当 model_p[top] - implied_p[top] > delta 时下注1单位, 赔率用 close
print("\n=== Value-bet ROI (模型 vs 庄家隐含, 阈值扫描) ===")
def roi_for(delta):
    stake=win=pick=0.0
    for i in range(len(yte)):
        top = int(proba[i].argmax())
        if proba[i][top] - imp[i][top] > delta:
            stake += 1.0
            pick += 1
            if yte[i]==top:
                win += close[i][top]   # decimal odds payout
    if stake==0: return 0,0,0
    return (win-stake)/stake*100, pick, stake
for d in [0.0, 0.02, 0.05, 0.08, 0.10]:
    r, picks, stk = roi_for(d)
    print(f"  delta>={d:.2f} : ROI={r:+.2f}%  下注{picks:.0f}场/共{len(yte)} (覆盖率{picks/len(yte)*100:.1f}%)")

# 对照: 永远买庄家热门的 ROI (应≈ -vig)
stake=win=0.0
for i in range(len(yte)):
    top=int(imp[i].argmax())
    stake+=1.0
    if yte[i]==top: win+=close[i][top]
print(f"\n  对照 永远买热门 : ROI={(win-stake)/stake*100:+.2f}%")
# 对照: 永远买模型top (同热门几乎一致)
stake=win=0.0
for i in range(len(yte)):
    top=int(proba[i].argmax())
    stake+=1.0
    if yte[i]==top: win+=close[i][top]
print(f"  对照 永远买模型top: ROI={(win-stake)/stake*100:+.2f}%")

# 3) 总进球期望 vs 实际
tg_pred = np.clip(rgr.predict(Xte).round(),0,15)
print(f"\n总进球 模型MAE={np.abs(tg_pred-test['total_goals'].values).mean():.3f}  期望{ tg_pred.mean():.2f} vs 实际{test['total_goals'].mean():.2f}")

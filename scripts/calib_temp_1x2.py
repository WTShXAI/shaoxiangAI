#!/usr/bin/env python3
"""1X2 温度缩放校准: 市场隐含概率(deoverround of close odds) 在 IW 真实赛果上拟合 T.
温度缩放 p -> p^(1/T) / sum, T>1 软化(降过自信), T<1 尖锐化.
验证: 方向(argmax)是否保持; ECE/logloss 是否改善.
"""
import sqlite3, numpy as np, pandas as pd

c = sqlite3.connect("data/football_data.db")
df = pd.read_sql_query(
    "SELECT close_home_odds ch, close_draw_odds cd, close_away_odds ca, final_result, match_date "
    "FROM interwetten_odds WHERE final_result IN ('H','D','A') AND close_home_odds IS NOT NULL",
    c)
c.close()
df["date"] = pd.to_datetime(df["match_date"])
df = df.dropna(subset=["ch", "cd", "ca"])
y = (df["final_result"].map({"H": 0, "D": 1, "A": 2})).to_numpy()
P = np.array([(1/df.ch.iloc[i], 1/df.cd.iloc[i], 1/df.ca.iloc[i]) for i in range(len(df))])
P = P / P.sum(1, keepdims=True)

def temp(P, T):
    p = np.clip(P, 1e-9, None) ** (1.0 / T)
    return p / p.sum(1, keepdims=True)

def ece(P, Y, bins=10):
    conf = P.flatten(); hit = np.zeros_like(P); hit[np.arange(len(Y)), Y] = 1
    hit = hit.flatten(); edges = np.linspace(0, 1, bins + 1); e = 0.0
    for i in range(bins):
        m = (conf >= edges[i]) & (conf <= edges[i+1])
        if m.sum(): e += m.sum()/len(conf) * abs(conf[m].mean() - hit[m].mean())
    return e

def logloss(P, Y):
    return -np.mean(np.log(np.clip(P[np.arange(len(Y)), Y], 1e-12, None)))

def acc(P, Y):
    return (P.argmax(1) == Y).mean()

tr = df["date"] < pd.Timestamp("2023-01-01")
te = ~tr
bestT, bestll = 1.0, 1e9
for T in np.linspace(0.7, 1.8, 56):
    ll = logloss(temp(P[tr], T), y[tr])
    if ll < bestll: bestll, bestT = ll, T

for name, mask in (("TRAIN(2021-22)", tr), ("TEST(2023+)", te)):
    print(f"[{name}] n={mask.sum()} | "
          f"T=1:  ECE={ece(P[mask],y[mask]):.4f} logloss={logloss(P[mask],y[mask]):.4f} acc={acc(P[mask],y[mask])*100:.1f}% | "
          f"T={bestT:.3f}: ECE={ece(temp(P[mask],bestT),y[mask]):.4f} logloss={logloss(temp(P[mask],bestT),y[mask]):.4f} acc={acc(temp(P[mask],bestT),y[mask])*100:.1f}%")
print(f"\nFitted T={bestT:.3f} (on train). TEST acc 变化=方向是否保持, 看上面 acc 行.")

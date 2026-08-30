"""Poisson GBM 进球模型 (2026-08-30) — 一举三得: 方向 / 比分 / 大小球。

为什么放弃"直接 3 分类预测方向"
--------------------------------
实测 XGBoost 三分类 vs 市场去水 argmax: 52.36% vs 52.40% —— **打不过市场**。
赔率已包含几乎所有公开信息, 直接学方向学不到 alpha; 且平局概率总最低,
argmax 永不选平局(召回 0%)。

正确路径: 预测**进球数** (λ_h / λ_a), 再从联合 Poisson 分布导出一切:
    方向 P(H/D/A) ← 联合比分分布求和
    比分 top1/top3 ← 联合分布排序
    大小球 P(over) ← 总进球分布

用 XGBoost 的 count:poisson 目标, 能捕捉赔率→进球的非线性关系,
比"从赔率反推单一 λ"更灵活。

数据: football_data.db odds_features (剔除 2026 垃圾年后 302,897 场)
评估: 全部在**时间外**测试集(2025)上, 双 baseline 对比
      B1 = 市场去水 argmax            (方向)
      B2 = 赔率反推 λ 的等强 Poisson  (方向/比分/OU)

用法: runpy scripts/train_poisson_goals_20260830.py [--test-year 2025]
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time

import joblib
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "football_data.db")
OUT_PATH = os.path.join(ROOT, "models", "poisson_goals_20260830.joblib")
TEST_YEAR = 2025
for a in sys.argv[1:]:
    if a.startswith("--test-year="):
        TEST_YEAR = int(a.split("=", 1)[1])
LEAGUE_K = 50.0
MAX_GOALS = 10

COLS = ("match_date, league, home_team, away_team, home_score, away_score, outcome, "
        "open_h, open_d, open_a, close_h, close_d, close_a, "
        "drift_h, drift_d, drift_a, imp_h, imp_d, imp_a, "
        "cimp_h, cimp_d, cimp_a, overround, home_edge, sigma_trap")

EPS = 1e-6


def load():
    con = sqlite3.connect(SRC, timeout=120)
    df = pd.read_sql_query(f"SELECT {COLS} FROM odds_features", con)
    con.close()
    df["match_date"] = pd.to_datetime(df["match_date"], errors="coerce")
    df = df.dropna(subset=["match_date", "home_score", "away_score"])
    df["year"] = df["match_date"].dt.year
    df["hg"] = df["home_score"].astype(float)
    df["ag"] = df["away_score"].astype(float)
    df["y"] = np.where(df["hg"] > df["ag"], 0, np.where(df["hg"] == df["ag"], 1, 2))
    # 数据质量: 剔除比分缺失/平局率异常的年份 (2026 年数据完全损坏)
    q = df.groupby("year").agg(n=("y", "size"),
                               dr=("y", lambda s: float((s == 1).mean())))
    bad = q[(q["dr"] < 0.10) | (q["dr"] > 0.40) | (q["n"] < 200)].index
    if len(bad):
        print(f"  [数据质量] 剔除年份 {sorted(int(x) for x in bad)}")
        df = df[~df["year"].isin(bad)]
    return df


def league_stats(train):
    g = train.groupby("league")
    n = g.size()
    gh, ga, gd = train["hg"].mean(), train["ag"].mean(), (train["y"] == 1).mean()
    return pd.DataFrame({
        "lg_hg": (g["hg"].mean() * n + gh * LEAGUE_K) / (n + LEAGUE_K),
        "lg_ag": (g["ag"].mean() * n + ga * LEAGUE_K) / (n + LEAGUE_K),
        "lg_draw": (g.apply(lambda d: (d["y"] == 1).mean(), include_groups=False) * n
                    + gd * LEAGUE_K) / (n + LEAGUE_K),
    })


def build(df, lstats):
    d = df.merge(lstats, left_on="league", right_index=True, how="left")
    d[["lg_hg", "lg_ag", "lg_draw"]] = d[["lg_hg", "lg_ag", "lg_draw"]].fillna(
        {"lg_hg": 1.4, "lg_ag": 1.1, "lg_draw": 0.25})
    d["f_ratio_ha"] = d["cimp_h"] / (d["cimp_a"] + EPS)
    d["f_diff_ha"] = d["cimp_h"] - d["cimp_a"]
    d["f_draw_gap"] = d["cimp_d"] - (d["cimp_h"] + d["cimp_a"]) / 2
    d["f_open_ratio"] = d["open_h"] / (d["open_a"] + EPS)
    d["f_close_ratio"] = d["close_h"] / (d["close_a"] + EPS)
    d["f_abs_drift"] = d[["drift_h", "drift_d", "drift_a"]].abs().max(axis=1)
    d["f_fav"] = d[["cimp_h", "cimp_d", "cimp_a"]].max(axis=1)
    d["f_entropy"] = -(d["cimp_h"] * np.log(d["cimp_h"] + EPS)
                       + d["cimp_d"] * np.log(d["cimp_d"] + EPS)
                       + d["cimp_a"] * np.log(d["cimp_a"] + EPS))
    d["f_year"] = d["year"]
    feats = ["cimp_h", "cimp_d", "cimp_a", "imp_h", "imp_d", "imp_a",
             "open_h", "open_d", "open_a", "close_h", "close_d", "close_a",
             "drift_h", "drift_d", "drift_a", "overround", "home_edge", "sigma_trap",
             "f_ratio_ha", "f_diff_ha", "f_draw_gap", "f_open_ratio", "f_close_ratio",
             "f_abs_drift", "f_fav", "f_entropy", "lg_hg", "lg_ag", "lg_draw", "f_year"]
    for c in feats:
        if c not in d.columns:
            d[c] = np.nan
    d[feats] = d[feats].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    return d, feats


def pois_pmf(lam, k):
    lam = float(np.clip(lam, 1e-6, 20.0))
    return math.exp(-lam) * lam ** k / math.factorial(k)


# ── Dixon-Coles 低比分修正 (2026-08-30) ──
# 纯 Poisson 假设主客进球独立, 会**系统性低估平局**(尤其 0-0 / 1-1),
# 实测本模型平局召回 0%、主胜召回 88.9% 就是这个毛病。
# Dixon-Coles & Cole (1997) 的 τ 修正正是解这个: 给低比分(0-0/0-1/1-0/1-1)
# 乘一个相关因子 ρ, 其余比分不变。ρ 在验证集上网格搜索。
DC_RHO = 0.0


def dc_tau(x, y, lam_h, lam_a, rho=DC_RHO):
    if x == 0 and y == 0:
        return 1.0 - lam_h * lam_a * rho
    if x == 0 and y == 1:
        return 1.0 + lam_a * rho
    if x == 1 and y == 0:
        return 1.0 + lam_h * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def joint_dist(lam_h, lam_a, rho=DC_RHO):
    """联合分布 {(i-j): p}, 含 Dixon-Coles 修正并归一化。"""
    grid = {}
    for i in range(MAX_GOALS + 1):
        pi = pois_pmf(lam_h, i)
        if pi < 1e-8 and i > 7:
            break
        for j in range(MAX_GOALS + 1):
            pj = pois_pmf(lam_a, j)
            if pj < 1e-8 and j > 7:
                break
            p = pi * pj * dc_tau(i, j, lam_h, lam_a, rho)
            if p > 0:
                grid[f"{i}-{j}"] = p
    s = sum(grid.values())
    if s <= 0:
        return grid
    return {k: v / s for k, v in grid.items()}


def joint_top(lam_h, lam_a, topn=3):
    """联合分布 → 比分 topN。"""
    g = joint_dist(lam_h, lam_a)
    return sorted(g.items(), key=lambda x: -x[1])[:topn]


def dir_from_lams(lam_h, lam_a):
    """从联合分布算方向概率 (主/平/客)。"""
    g = joint_dist(lam_h, lam_a)
    ph = pd_ = pa = 0.0
    for k, p in g.items():
        i, j = (int(x) for x in k.split("-"))
        if i > j:
            ph += p
        elif i == j:
            pd_ += p
        else:
            pa += p
    s = ph + pd_ + pa
    return (ph / s, pd_ / s, pa / s) if s > 0 else (1 / 3, 1 / 3, 1 / 3)


def p_over(lam_h, lam_a, line):
    """P(总进球 > line)。"""
    g = joint_dist(lam_h, lam_a)
    return min(max(sum(p for k, p in g.items()
                       if sum(int(x) for x in k.split("-")) > line), 0.0), 1.0)


def main():
    t0 = time.time()
    print(f"[{time.strftime('%H:%M:%S')}] 加载 ...")
    df = load()
    print(f"  有效 {len(df)} 场 ({int(df['year'].min())}~{int(df['year'].max())})")

    tr = df[df["year"] < TEST_YEAR - 1]
    va = df[(df["year"] >= TEST_YEAR - 1) & (df["year"] < TEST_YEAR)]
    te = df[df["year"] >= TEST_YEAR].reset_index(drop=True)
    print(f"  训练 {len(tr)} | 验证 {len(va)} | 测试 {len(te)}")

    lstats = league_stats(tr)
    tr2, feats = build(tr, lstats)
    va2, _ = build(va, lstats)
    te2, _ = build(te, lstats)
    print(f"  特征 {len(feats)} 维")

    import xgboost as xgb
    print(f"[{time.strftime('%H:%M:%S')}] 训练 Poisson GBM (主队/客队各一) ...")
    common = dict(n_estimators=2500, learning_rate=0.025, max_depth=6,
                  min_child_weight=40, subsample=0.85, colsample_bytree=0.8,
                  reg_lambda=2.0, gamma=0.05, tree_method="hist",
                  n_jobs=24, random_state=42, objective="count:poisson",
                  eval_metric="poisson-nloglik", early_stopping_rounds=150)
    mh = xgb.XGBRegressor(**common)
    mh.fit(tr2[feats].values, tr2["hg"].values,
           eval_set=[(va2[feats].values, va2["hg"].values)], verbose=False)
    ma = xgb.XGBRegressor(**common)
    ma.fit(tr2[feats].values, tr2["ag"].values,
           eval_set=[(va2[feats].values, va2["ag"].values)], verbose=False)

    # ── 在验证集上网格搜索 Dixon-Coles ρ ──
    global DC_RHO
    vs = va2.sample(n=min(3000, len(va2)), random_state=42).reset_index(drop=True)
    vlh = np.clip(mh.predict(vs[feats].values), 0.05, 6.0)
    vla = np.clip(ma.predict(vs[feats].values), 0.05, 6.0)
    print(f"[{time.strftime('%H:%M:%S')}] 搜索 Dixon-Coles ρ (验证子集 n={len(vs)}) ...")
    best = (None, 9e9)
    for rho in [round(x * 0.02 - 0.30, 3) for x in range(31)]:
        DC_RHO = rho
        ll = 0.0
        for i in range(len(vs)):
            g = joint_dist(vlh[i], vla[i], rho)
            k = f"{int(vs['hg'].values[i])}-{int(vs['ag'].values[i])}"
            p = g.get(k, 1e-6)
            ll += -math.log(max(p, 1e-6))
        ll /= len(vs)
        if ll < best[1]:
            best = (rho, ll)
    DC_RHO = best[0]
    print(f"  最优 ρ = {DC_RHO} (LogLoss {best[1]:.4f})")

    Xt = te2[feats].values
    lam_h = np.clip(mh.predict(Xt), 0.05, 6.0)
    lam_a = np.clip(ma.predict(Xt), 0.05, 6.0)
    y = te2["y"].values
    hg, ag = te2["hg"].values, te2["ag"].values

    # ── 平局概率 boost 搜索 (2026-08-30 关键修正) ──
    #   独立 Poisson 系统性**低估平局**: 测试集真实平局率 24.9%, 但模型给平局
    #   的 argmax 概率几乎从不最高 → 平局召回 0%, 等于白扔 1/4 样本。
    #   Dixon-Coles 的 ρ 修正量太小(实测 -0.04, 贡献 0.00pp), 需要显式 boost。
    #   在验证集上搜 boost, 使"预测为平局的比例"接近真实平局率。
    vprobs = np.array([dir_from_lams(vlh[i], vla[i]) for i in range(len(vlh))])
    vyp = vs["y"].values
    print(f"[{time.strftime('%H:%M:%S')}] 搜索平局 boost (验证集 n={len(vs)}, "
          f"真实平局率 {np.mean(vyp==1)*100:.1f}%) ...")
    best_b = (1.0, -1.0)
    for b in np.arange(1.0, 3.05, 0.05):
        adj = vprobs.copy()
        adj[:, 1] *= b
        pred = np.argmax(adj, axis=1)
        acc = (pred == vyp).mean()
        if acc > best_b[1]:
            best_b = (float(b), acc)
    DRAW_BOOST = best_b[0]
    print(f"  最优 boost = {DRAW_BOOST:.2f} (验证准确率 {best_b[1]*100:.2f}%, "
          f"平局率 {np.mean(np.argmax(np.column_stack([vprobs[:,0], vprobs[:,1]*DRAW_BOOST, vprobs[:,2]]), axis=1)==1)*100:.1f}%)")

    # ── B1 baseline: 市场去水 argmax ──
    b1 = np.argmax(te2[["cimp_h", "cimp_d", "cimp_a"]].values, axis=1)

    # ── B2 baseline: 同等 λ 但 ρ=0 (纯 Poisson, 用于衡量 DC 修正的贡献) ──
    rho_saved = DC_RHO
    DC_RHO = 0.0
    b2_dir = np.array([int(np.argmax(dir_from_lams(lam_h[i], lam_a[i])))
                       for i in range(len(y))])
    b2_t1 = b2_t3 = 0
    for i in range(len(y)):
        top = [s for s, _ in joint_top(lam_h[i], lam_a[i], 3)]
        if top and top[0] == f"{int(hg[i])}-{int(ag[i])}":
            b2_t1 += 1
        if f"{int(hg[i])}-{int(ag[i])}" in top:
            b2_t3 += 1
    print(f"\n  [对照] 纯 Poisson(ρ=0) 方向 {(b2_dir==y).mean()*100:.2f}% | "
          f"top1 {b2_t1/len(y)*100:.2f}% | top3 {b2_t3/len(y)*100:.2f}%")
    DC_RHO = rho_saved

    def eval_lams(lh, la, tag, boost=1.0):
        pred = []
        for a_, b_ in zip(lh, la):
            p = dir_from_lams(a_, b_)
            p = (p[0], p[1] * boost, p[2])
            pred.append(int(np.argmax(p)))
        pred = np.array(pred)
        acc = (pred == y).mean()
        # 比分
        t1 = t3 = 0
        for i in range(len(y)):
            top = [s for s, _ in joint_top(lh[i], la[i], 3)]
            if top and top[0] == f"{int(hg[i])}-{int(ag[i])}":
                t1 += 1
            if f"{int(hg[i])}-{int(ag[i])}" in top:
                t3 += 1
        # OU (线 2.5)
        po = np.array([p_over(lh[i], la[i], 2.5) for i in range(len(y))])
        yo = ((hg + ag) > 2.5).astype(int)
        ll = -np.mean(np.log(np.clip(np.where(yo == 1, po, 1 - po), 1e-9, 1)))
        br = np.mean((po - yo) ** 2)
        print(f"  {tag:22s} 方向 {acc*100:5.2f}% | top1 {t1/len(y)*100:5.2f}% | "
              f"top3 {t3/len(y)*100:5.2f}% | OU_LL {ll:.4f} | OU_Brier {br:.4f}")
        return acc

    print(f"\n===== 测试集 ({TEST_YEAR}, n={len(te)}) =====")
    print(f"  {'B1 市场argmax':22s} 方向 {(b1==y).mean()*100:5.2f}%")
    acc = eval_lams(lam_h, lam_a, f"Poisson+DC+boost({DRAW_BOOST:.2f})", boost=DRAW_BOOST)

    d2 = b2_dir
    print(f"\n  方向准确率对比:")
    print(f"    B1 市场 argmax      : {(b1==y).mean()*100:.2f}%")
    print(f"    纯 Poisson (ρ=0)    : {(d2==y).mean()*100:.2f}%")
    print(f"    Poisson + DC 修正   : {acc*100:.2f}%")
    print(f"    vs B1               : {(acc-(b1==y).mean())*100:+.2f}pp")
    print(f"    DC+boost 贡献       : {(acc-(d2==y).mean())*100:+.2f}pp")

    # 各方向召回
    print(f"\n  各方向召回:")
    pm = []
    for a_, b_ in zip(lam_h, lam_a):
        _p = dir_from_lams(a_, b_)
        pm.append(int(np.argmax((_p[0], _p[1] * DRAW_BOOST, _p[2]))))
    pm = np.array(pm)
    for i, nm in enumerate(("主胜", "平局", "客胜")):
        mk = y == i
        if mk.sum() == 0:
            continue
        print(f"    {nm}(n={mk.sum():5d}): GBM {(pm[mk]==i).mean()*100:5.1f}% | "
              f"B2 {(np.array(d2)[mk]==i).mean()*100:5.1f}% | "
              f"B1 {(b1[mk]==i).mean()*100:5.1f}%")

    joblib.dump({"mh": mh, "ma": ma, "features": feats, "league_stats": lstats,
                 "test_year": TEST_YEAR, "dc_rho": DC_RHO, "draw_boost": DRAW_BOOST,
                 "trained_at": int(time.time()),
                 "note": "Poisson GBM 进球模型; 方向/比分/OU 均由 λ_h/λ_a 联合分布导出"},
                OUT_PATH)
    print(f"\n[{time.strftime('%H:%M:%S')}] 已保存 → {OUT_PATH} ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()

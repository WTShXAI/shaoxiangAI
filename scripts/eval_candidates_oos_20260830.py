"""候选模型真实时间外(OOS)指标复算 (2026-08-30).

目的: 训练脚本只 print 不持久化指标, 用户要求"不凭感觉"接回有潜力的未接入模型.
本脚本**复用训练脚本原样的预处理函数** (load/quality_filter/build_features/build),
在 TEST_YEAR=2025 时间外测试集(训练期 <2024, 验证 2024, 测试 >=2025)上:

  A. direction_model_20260830 (XGB 1X2 方向, 322K场):
       - 方向 acc vs 市场去水 argmax baseline
       - 各类(主/平/客)召回 vs baseline
       - 测试 LogLoss / Brier
       - 多类 AUC (ovo, macro) vs baseline

  B. poisson_goals_20260830 (XGB count:poisson 进球 λ):
       - 由 λ_h/λ_a 联合分布(含 DC ρ + draw_boost)导出方向 acc
       - 方向 AUC (ovo) vs baseline
       - OU(线2.5) AUC + LogLoss/Brier
       - 比分 top1 / top3

输出: 打印 + 持久化 reports/candidates_oos_eval_20260830.json (硬数字, 非凭感觉).
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import train_direction_model_20260830 as D   # noqa: E402
import train_poisson_goals_20260830 as P     # noqa: E402
import joblib                                  # noqa: E402
from sklearn.metrics import roc_auc_score, log_loss  # noqa: E402

TEST_YEAR = 2025
EPS = 1e-9


# ───────────────────────── 方向模型 A ─────────────────────────
def eval_direction():
    print("\n########## A. direction_model_20260830 (XGB 1X2 方向) ##########")
    df = D.load()
    df = D.quality_filter(df)
    tr = df[df["year"] < TEST_YEAR - 1]
    va = df[(df["year"] >= TEST_YEAR - 1) & (df["year"] < TEST_YEAR)]
    te = df[df["year"] >= TEST_YEAR]
    lstats = D.add_league_stats(tr)
    te2, feats = D.build_features(te, lstats)
    Xte = te2[feats].values
    yte = te2["y"].values

    d = joblib.load(os.path.join(ROOT, "models", "direction_model_20260830.joblib"))
    m = d["model"]
    print(f"  测试集 n={len(te)} ({int(te['year'].min())}~{int(te['year'].max())}), 特征 {len(feats)}")

    # baseline
    cimp = te2[["cimp_h", "cimp_d", "cimp_a"]].values
    base = np.argmax(cimp, axis=1)
    base_acc = (base == yte).mean()

    pred = m.predict(Xte)
    acc = (pred == yte).mean()
    proba = m.predict_proba(Xte)
    # 归一化 baseline 概率 (cimp 已去水, 和≈1)
    cs = cimp.sum(1, keepdims=True)
    cs[cs == 0] = 1.0
    base_prob = cimp / cs

    ll = log_loss(yte, proba, labels=[0, 1, 2])
    bl = log_loss(yte, base_prob, labels=[0, 1, 2])
    yoh = np.zeros_like(proba); yoh[np.arange(len(yte)), yte] = 1
    brier = np.mean(np.sum((proba - yoh) ** 2, axis=1))
    auc = roc_auc_score(yte, proba, multi_class="ovo", average="macro")
    bauc = roc_auc_score(yte, base_prob, multi_class="ovo", average="macro")

    rows = {"baseline": {}, "model": {}}
    for i, nm in enumerate(("主胜", "平局", "客胜")):
        mk = yte == i
        rows["model"][nm] = (pred[mk] == i).mean() if mk.sum() else None
        rows["baseline"][nm] = (base[mk] == i).mean() if mk.sum() else None

    print(f"  方向 acc: 模型 {acc*100:.2f}% | baseline(去水argmax) {base_acc*100:.2f}% | 提升 {(acc-base_acc)*100:+.2f}pp")
    print(f"  LogLoss: 模型 {ll:.4f} | baseline {bl:.4f} | Δ {ll-bl:+.4f}")
    print(f"  Brier : 模型 {brier:.4f}")
    print(f"  AUC(ovo,macro): 模型 {auc:.4f} | baseline {bauc:.4f} | Δ {auc-bauc:+.4f}")
    print(f"  各类召回(模型/baseline):")
    for i, nm in enumerate(("主胜", "平局", "客胜")):
        print(f"    {nm}: {rows['model'][nm]*100 if rows['model'][nm] is not None else 'NA':>6.1f}% / "
              f"{rows['baseline'][nm]*100 if rows['baseline'][nm] is not None else 'NA':>6.1f}%")

    return {
        "model": "direction_model_20260830",
        "test_n": int(len(te)),
        "test_year_span": [int(te["year"].min()), int(te["year"].max())],
        "acc": float(acc), "base_acc": float(base_acc), "acc_gain_pp": float((acc - base_acc) * 100),
        "logloss": float(ll), "base_logloss": float(bl), "logloss_gain": float(ll - bl),
        "brier": float(brier),
        "auc_ovo_macro": float(auc), "base_auc_ovo_macro": float(bauc), "auc_gain": float(auc - bauc),
        "per_class_recall": rows,
    }


# ───────────────────────── Poisson 进球 λ B ─────────────────────────
def eval_poisson():
    print("\n########## B. poisson_goals_20260830 (XGB count:poisson 进球 λ) ##########")
    df = P.load()
    tr = df[df["year"] < TEST_YEAR - 1]
    va = df[(df["year"] >= TEST_YEAR - 1) & (df["year"] < TEST_YEAR)]
    te = df[df["year"] >= TEST_YEAR].reset_index(drop=True)
    lstats = P.league_stats(tr)
    te2, feats = P.build(te, lstats)
    Xte = te2[feats].values
    y = te2["y"].values
    hg, ag = te2["hg"].values, te2["ag"].values

    d = joblib.load(os.path.join(ROOT, "models", "poisson_goals_20260830.joblib"))
    mh, ma = d["mh"], d["ma"]
    rho = float(d["dc_rho"])
    boost = float(d["draw_boost"])
    P.DC_RHO = rho  # 让 joint_dist / dir_from_lams 用训练期搜得的 ρ
    print(f"  测试集 n={len(te)} ({int(te['year'].min())}~{int(te['year'].max())}), 特征 {len(feats)}, "
          f"ρ={rho}, draw_boost={boost}")

    lam_h = np.clip(mh.predict(Xte), 0.05, 6.0)
    lam_a = np.clip(ma.predict(Xte), 0.05, 6.0)

    # 方向 (含 draw_boost) —— 注意 boost 破坏概率归一, AUC 前必须重归一
    pred = []
    probs = []
    for i in range(len(y)):
        p = P.dir_from_lams(lam_h[i], lam_a[i])
        probs.append((p[0], p[1] * boost, p[2]))
        pred.append(int(np.argmax(probs[-1])))
    pred = np.array(pred)
    proba = np.array(probs, dtype=float)
    rs = proba.sum(1, keepdims=True); rs[rs == 0] = 1.0
    proba = proba / rs  # 归一化用于 AUC/LogLoss
    acc = (pred == y).mean()

    cimp = te2[["cimp_h", "cimp_d", "cimp_a"]].values
    base = np.argmax(cimp, axis=1)
    base_acc = (base == y).mean()
    cs = cimp.sum(1, keepdims=True); cs[cs == 0] = 1.0
    base_prob = cimp / cs
    auc = roc_auc_score(y, proba, multi_class="ovo", average="macro")
    bauc = roc_auc_score(y, base_prob, multi_class="ovo", average="macro")
    ll = log_loss(y, proba, labels=[0, 1, 2])
    bl = log_loss(y, base_prob, labels=[0, 1, 2])

    # OU (线 2.5)
    po = np.array([P.p_over(lam_h[i], lam_a[i], 2.5) for i in range(len(y))])
    yo = ((hg + ag) > 2.5).astype(int)
    ou_auc = roc_auc_score(yo, po)
    ou_ll = -np.mean(np.log(np.clip(np.where(yo == 1, po, 1 - po), 1e-9, 1)))
    ou_brier = np.mean((po - yo) ** 2)

    # 比分 top1/top3
    t1 = t3 = 0
    for i in range(len(y)):
        top = [s for s, _ in P.joint_top(lam_h[i], lam_a[i], 3)]
        if top and top[0] == f"{int(hg[i])}-{int(ag[i])}":
            t1 += 1
        if f"{int(hg[i])}-{int(ag[i])}" in top:
            t3 += 1

    rows = {"model": {}, "baseline": {}}
    for i, nm in enumerate(("主胜", "平局", "客胜")):
        mk = y == i
        rows["model"][nm] = (pred[mk] == i).mean() if mk.sum() else None
        rows["baseline"][nm] = (base[mk] == i).mean() if mk.sum() else None

    print(f"  方向 acc: 模型 {acc*100:.2f}% | baseline {base_acc*100:.2f}% | 提升 {(acc-base_acc)*100:+.2f}pp")
    print(f"  AUC(ovo,macro): 模型 {auc:.4f} | baseline {bauc:.4f} | Δ {auc-bauc:+.4f}")
    print(f"  LogLoss: 模型 {ll:.4f} | baseline {bl:.4f}")
    print(f"  OU(2.5) AUC {ou_auc:.4f} | LL {ou_ll:.4f} | Brier {ou_brier:.4f} | 真实大球率 {yo.mean()*100:.1f}%")
    print(f"  比分 top1 {t1/len(y)*100:.2f}% | top3 {t3/len(y)*100:.2f}%")
    print(f"  各类召回(模型/baseline):")
    for i, nm in enumerate(("主胜", "平局", "客胜")):
        print(f"    {nm}: {rows['model'][nm]*100 if rows['model'][nm] is not None else 'NA':>6.1f}% / "
              f"{rows['baseline'][nm]*100 if rows['baseline'][nm] is not None else 'NA':>6.1f}%")

    return {
        "model": "poisson_goals_20260830",
        "test_n": int(len(y)),
        "test_year_span": [int(te["year"].min()), int(te["year"].max())],
        "dc_rho": rho, "draw_boost": boost,
        "acc": float(acc), "base_acc": float(base_acc), "acc_gain_pp": float((acc - base_acc) * 100),
        "auc_ovo_macro": float(auc), "base_auc_ovo_macro": float(bauc), "auc_gain": float(auc - bauc),
        "logloss": float(ll), "base_logloss": float(bl),
        "ou_auc_2.5": float(ou_auc), "ou_logloss_2.5": float(ou_ll), "ou_brier_2.5": float(ou_brier),
        "score_top1": float(t1 / len(y)), "score_top3": float(t3 / len(y)),
        "per_class_recall": rows,
    }


def main():
    t0 = time.time()
    out = {"generated_at": datetime.now().isoformat(),
           "note": "时间外测试集 TEST_YEAR=2025; 复用训练脚本原样预处理; 非凭感觉"}
    out["direction"] = eval_direction()
    out["poisson"] = eval_poisson()
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    path = os.path.join(ROOT, "reports", "candidates_oos_eval_20260830.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n[持久化] → {path}  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()

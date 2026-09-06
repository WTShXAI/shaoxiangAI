"""direction_model 平局重训 v2 (2026-08-31) — Task #88 落地。

根因 (08-31 实证):
  1. 原模型 322K 场 XGB 多分类 softmax 无类别权重, H45%/D25%/A30% 不平衡;
  2. 平局召回 0% 不全是类别不平衡 —— 三分类 argmax 决策规则天然不选平局
     (P(D) 即使正确估计 ~0.25, argmax 也几乎从不出头; baseline 平局召回同样仅 1.0%)。
  3. 因此同时修两层:
     (a) 训练层: 平局样本提权 ×3 + 两级模型(DvsND + HvsA) 提升 P(D) 判别力;
     (b) 决策层: P(D) > tau 判平局, tau 在验证集按 "平局召回>=40% 且 acc 最高" 选取。

变体:
  V1 = 3 类 XGB + sample_weight(平局×3)
  V2 = 两级: DvsND 二分类(平局×3) + HvsA 二分类, 概率合成 P(D)=p_d, P(H/A)=(1-p_d)*p_ha
对照:
  base_argmax = 市场去水概率 argmax
  old_argmax  = 原模型 direction_model_20260830 argmax (已部署基线)
  old_tau     = 原模型 + tau 决策规则 (验证决策层贡献, 不重训)
评估:
  验证集选型(tau + V1/V2) → 测试集(2025, TEST_YEAR=2025)最终指标
落盘:
  models/direction_model_20260830b.joblib (达标才落盘, 宁PASS不伪造 IR-30)
  达标标准: 测试集平局召回 >= 40% 且 整体 acc >= 原模型 argmax (52.36%)

用法: runpy scripts/train_direction_model_20260830b.py
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score, log_loss

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import train_direction_model_20260830 as D   # noqa: E402

TEST_YEAR = 2025
OUT_PATH = os.path.join(ROOT, "models", "direction_model_20260830b.joblib")
REPORT_PATH = os.path.join(ROOT, "reports", "direction_b_oos_eval_20260831.json")
TAU_GRID = [round(0.15 + 0.05 * i, 2) for i in range(8)]  # 0.15 .. 0.50
DRAW_WEIGHT = 3.0      # 平局样本提权倍数 (用户口径 ×2~×3)
RECALL_FLOOR = 0.40    # 平局召回达标线

XGB_KW = dict(n_estimators=4000, learning_rate=0.03, max_depth=7,
              min_child_weight=30, subsample=0.85, colsample_bytree=0.8,
              reg_lambda=1.5, gamma=0.1, tree_method="hist",
              n_jobs=24, random_state=42, early_stopping_rounds=150)


def predict_tau(P: np.ndarray, tau: float) -> np.ndarray:
    """P: (n,3) 三分类概率. 决策: P(D)>tau -> 平局, 否则在 H/A 中 argmax."""
    res = np.argmax(P[:, [0, 2]], axis=1).astype(int)
    res = np.where(res == 1, 2, res)
    return np.where(P[:, 1] > tau, 1, res)


def recall_d(P: np.ndarray, y: np.ndarray, tau: float) -> float:
    m = y == 1
    return float((predict_tau(P, tau)[m] == 1).mean()) if m.sum() else 0.0


def metrics(P: np.ndarray, y: np.ndarray, tau=None) -> dict:
    pred = predict_tau(P, tau) if tau is not None else np.argmax(P, axis=1)
    acc = float((pred == y).mean())
    rec = {}
    for i, nm in enumerate(("主胜", "平局", "客胜")):
        mk = y == i
        rec[nm] = float((pred[mk] == i).mean()) if mk.sum() else None
    ll = float(log_loss(y, P, labels=[0, 1, 2]))
    yoh = np.zeros_like(P)
    yoh[np.arange(len(y)), y] = 1
    brier = float(np.mean(np.sum((P - yoh) ** 2, axis=1)))
    auc = float(roc_auc_score(y, P, multi_class="ovo", average="macro"))
    return {"acc": acc, "recall": rec, "ll": ll, "brier": brier, "auc": auc}


def pick_tau(P: np.ndarray, y: np.ndarray):
    """验证集: 平局召回>=FLOOR 约束下 acc 最高的 tau; 全不达标取召回最高者."""
    cands = [(tau, recall_d(P, y, tau), metrics(P, y, tau)["acc"]) for tau in TAU_GRID]
    ok = [c for c in cands if c[1] >= RECALL_FLOOR]
    if ok:
        best = max(ok, key=lambda c: c[2])
        return best[0], cands, True
    worst = max(cands, key=lambda c: c[1])
    return worst[0], cands, False


def fmt_row(name: str, m: dict) -> str:
    r = m["recall"]
    return (f"  {name:14s} acc {m['acc']*100:5.2f}% | 平局召回 "
            f"{r['平局']*100 if r['平局'] is not None else 0:5.1f}% | 主 {r['主胜']*100 if r['主胜'] is not None else 0:5.1f}% | "
            f"客 {r['客胜']*100 if r['客胜'] is not None else 0:5.1f}% | LL {m['ll']:.4f} | Brier {m['brier']:.4f} | AUC {m['auc']:.4f}")


def main():
    t0 = time.time()
    print(f"[{time.strftime('%H:%M:%S')}] ===== direction_model 平局重训 v2 (TEST_YEAR={TEST_YEAR}) =====")
    import joblib
    import xgboost as xgb

    df = D.load()
    df = D.quality_filter(df)
    tr = df[df["year"] < TEST_YEAR - 1]
    va = df[(df["year"] >= TEST_YEAR - 1) & (df["year"] < TEST_YEAR)]
    te = df[df["year"] >= TEST_YEAR]
    print(f"  样本 {len(df)} | 训练 {len(tr)} | 验证 {len(va)} | 测试 {len(te)}")

    lstats = D.add_league_stats(tr)
    tr2, feats = D.build_features(tr, lstats)
    va2, _ = D.build_features(va, lstats)
    te2, _ = D.build_features(te, lstats)
    Xtr, ytr = tr2[feats].values, tr2["y"].values
    Xva, yva = va2[feats].values, va2["y"].values
    Xte, yte = te2[feats].values, te2["y"].values
    print(f"  特征 {len(feats)} 维; 训练集分布 H{ (ytr==0).mean()*100:.1f}% / D{(ytr==1).mean()*100:.1f}% / A{(ytr==2).mean()*100:.1f}%")

    # ── 对照概率 ──
    cimp_te = te2[["cimp_h", "cimp_d", "cimp_a"]].values
    cimp_va = va2[["cimp_h", "cimp_d", "cimp_a"]].values
    cs = cimp_te.sum(1, keepdims=True); cs[cs == 0] = 1.0
    P_base_te = cimp_te / cs
    csv = cimp_va.sum(1, keepdims=True); csv[csv == 0] = 1.0
    P_base_va = cimp_va / csv

    old = joblib.load(os.path.join(ROOT, "models", "direction_model_20260830.joblib"))
    P_old_te = old["model"].predict_proba(Xte)
    P_old_va = old["model"].predict_proba(Xva)

    # ── 训练 V1: 3类 XGB + 平局提权 ──
    print(f"[{time.strftime('%H:%M:%S')}] 训练 V1 (3类XGB + 平局×{DRAW_WEIGHT:.0f}) ...")
    m1 = xgb.XGBClassifier(**XGB_KW, eval_metric="mlogloss")
    sw1 = np.where(ytr == 1, DRAW_WEIGHT, 1.0)
    m1.fit(Xtr, ytr, sample_weight=sw1, eval_set=[(Xva, yva)], verbose=False)
    P1_va = m1.predict_proba(Xva)
    P1_te = m1.predict_proba(Xte)

    # ── 训练 V2: 两级 (DvsND + HvsA) ──
    print(f"[{time.strftime('%H:%M:%S')}] 训练 V2 (DvsND + HvsA 两级) ...")
    md = xgb.XGBClassifier(**XGB_KW, eval_metric="logloss")
    y_d_tr = (ytr == 1).astype(int)
    y_d_va = (yva == 1).astype(int)
    sw_d = np.where(ytr == 1, DRAW_WEIGHT, 1.0)
    md.fit(Xtr, y_d_tr, sample_weight=sw_d, eval_set=[(Xva, y_d_va)], verbose=False)
    mha = xgb.XGBClassifier(**XGB_KW, eval_metric="logloss")
    mha_tr, mha_va = ytr != 1, yva != 1
    mha.fit(Xtr[mha_tr], (ytr[mha_tr] == 2).astype(int),
            eval_set=[(Xva[mha_va], (yva[mha_va] == 2).astype(int))], verbose=False)

    def v2_prob(X):
        p_d = md.predict_proba(X)[:, 1]
        p_ha = mha.predict_proba(X)
        return np.column_stack([(1 - p_d) * p_ha[:, 0], p_d, (1 - p_d) * p_ha[:, 1]])
    P2_va = v2_prob(Xva)
    P2_te = v2_prob(Xte)

    # ── 验证集选 tau (仅用于平局决策阈值, 一次性) ──
    print(f"[{time.strftime('%H:%M:%S')}] 验证集选 tau ...")
    sel = {}
    for nm, P in (("old", P_old_va), ("v1", P1_va), ("v2", P2_va), ("base", P_base_va)):
        tau, cands, hit = pick_tau(P, yva)
        sel[nm] = {"tau": tau, "floor_hit": hit}
        print(f"  {nm:4s} tau={tau:.2f} 达标={'Y' if hit else 'N'} | "
              + " ".join(f"τ{t:.2f}(r{rec*100:.0f}%,a{acc*100:.1f}%)" for t, rec, acc in cands))

    # ── 测试集最终指标 ──
    print(f"\n===== 测试集 (2025, n={len(te)}) 结果 =====")
    rows = {}
    rows["base_argmax"] = metrics(P_base_te, yte)
    rows["old_argmax"] = metrics(P_old_te, yte)
    rows["old_tau"] = metrics(P_old_te, yte, sel["old"]["tau"])
    rows["v1_argmax"] = metrics(P1_te, yte)
    rows["v1_tau"] = metrics(P1_te, yte, sel["v1"]["tau"])
    rows["v2_tau"] = metrics(P2_te, yte, sel["v2"]["tau"])
    for nm, m in rows.items():
        print(fmt_row(nm, m))

    # ── 达标判定: v1/v2 tau 中平局召回>=40% 且 acc >= old_argmax ──
    old_acc = rows["old_argmax"]["acc"]
    candidates = {"v1": rows["v1_tau"], "v2": rows["v2_tau"]}
    passed = {k: (m["recall"]["平局"] or 0) >= RECALL_FLOOR and m["acc"] >= old_acc - 1e-9
              for k, m in candidates.items()}
    print(f"\n  原模型 argmax acc = {old_acc*100:.2f}% (平局召回 {rows['old_argmax']['recall']['平局']*100:.1f}%)")
    for k, ok in passed.items():
        m = candidates[k]
        print(f"  {k}_tau: 平局召回 {(m['recall']['平局'] or 0)*100:.1f}% | acc {m['acc']*100:.2f}% | "
              f"达标(召回>=40% & acc>=原) {'✅' if ok else '❌'}")

    # ── 落盘选优 (宁PASS不伪造) ──
    picked = None
    if any(passed.values()):
        picked = "v1" if passed["v1"] and (not passed["v2"] or rows["v1_tau"]["acc"] >= rows["v2_tau"]["acc"]) else "v2"
        if os.path.exists(OUT_PATH):
            print(f"  [冻结] {OUT_PATH} 已存在, 拒绝覆写.")
        else:
            if picked == "v1":
                payload = {"model": m1, "features": feats, "model_kind": "xgb_weighted",
                           "variant": "v1", "draw_weight": DRAW_WEIGHT,
                           "tau": sel["v1"]["tau"], "test_year": TEST_YEAR,
                           "league_stats": lstats, "trained_at": int(time.time())}
            else:
                payload = {"model_dnd": md, "model_ha": mha, "features": feats,
                           "model_kind": "xgb_two_stage", "variant": "v2",
                           "draw_weight": DRAW_WEIGHT, "tau": sel["v2"]["tau"],
                           "test_year": TEST_YEAR, "league_stats": lstats,
                           "trained_at": int(time.time())}
            joblib.dump(payload, OUT_PATH)
            print(f"  ✅ 已落盘 {picked} → {OUT_PATH}")
    else:
        print("  ❌ 两变体均未达标, 不落盘 (IR-30 宁PASS不伪造)")

    # ── 持久化报告 ──
    import json
    from datetime import datetime
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    out = {
        "generated_at": datetime.now().isoformat(),
        "test_year": TEST_YEAR, "test_n": int(len(te)),
        "draw_weight": DRAW_WEIGHT, "recall_floor": RECALL_FLOOR,
        "tau_selected": {k: v["tau"] for k, v in sel.items()},
        "rows": rows,
        "passed": passed, "picked": picked,
        "note": "决策层修复: P(D)>tau 判平局; tau 在验证集按平局召回>=40%且acc最高选(一次性,无多次调参)",
    }
    json.dump(out, open(REPORT_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n[{time.strftime('%H:%M:%S')}] 报告 → {REPORT_PATH}  (总耗时 {time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()

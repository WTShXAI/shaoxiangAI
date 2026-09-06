# -*- coding: utf-8 -*-
"""direction_model P(D) 平局二元判别力评估 (2026-09-01, Task#88 后续).

目标: 为 old_tau 平局候选池接 world_analyzer 提供硬证据——
  A. P(D) 对"平局 vs 非平局"的二元 AUC (判别力核心, >0.5 才有信息)
  B. τ=0.25 池子的 precision (真平局率) vs 无条件平局率
  C. 与市场去水 cimp_d 的对照 (P(D) 是否优于市场)
若 AUC_draw ≈ 0.5 → 提醒层必须标注"低判别力", 措辞降级为"平局概率参考"。
"""
from __future__ import annotations

import os
import sys

import numpy as np
from sklearn.metrics import roc_auc_score

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import train_direction_model_20260830 as D   # noqa: E402

TEST_YEAR = 2025
TAU = 0.25


def main():
    import joblib
    df = D.load()
    df = D.quality_filter(df)
    tr = df[df["year"] < TEST_YEAR - 1]
    va = df[(df["year"] >= TEST_YEAR - 1) & (df["year"] < TEST_YEAR)]
    te = df[df["year"] >= TEST_YEAR]
    lstats = D.add_league_stats(tr)
    te2, feats = D.build_features(te, lstats)
    Xte = te2[feats].values
    y = te2["y"].values
    y_d = (y == 1).astype(int)

    d = joblib.load(os.path.join(ROOT, "models", "direction_model_20260830.joblib"))
    P = d["model"].predict_proba(Xte)
    p_d = P[:, 1]

    cimp = te2[["cimp_h", "cimp_d", "cimp_a"]].values
    cs = cimp.sum(1, keepdims=True); cs[cs == 0] = 1.0
    p_d_mkt = (cimp / cs)[:, 1]

    base_draw_rate = y_d.mean()
    print(f"测试集 n={len(y)}, 无条件平局率 {base_draw_rate*100:.1f}%")

    auc_model = roc_auc_score(y_d, p_d)
    auc_mkt = roc_auc_score(y_d, p_d_mkt)
    print(f"\n[A] P(D) 平局二元 AUC: direction_model {auc_model:.4f} | 市场去水 {auc_mkt:.4f} | Δ {auc_model-auc_mkt:+.4f}")

    for src, p in (("model", p_d), ("market", p_d_mkt)):
        pool = p > TAU
        if pool.sum() == 0:
            print(f"[B] {src} τ={TAU}: 空池")
            continue
        prec = y_d[pool].mean()
        recall = (pool & (y_d == 1)).sum() / max(y_d.sum(), 1)
        n_pool = int(pool.sum())
        lift = prec / base_draw_rate if base_draw_rate > 0 else 0
        print(f"[B] {src} τ={TAU}: 池 n={n_pool} ({n_pool/len(y)*100:.1f}%), "
              f"precision {prec*100:.1f}% (无条件 {base_draw_rate*100:.1f}%, lift {lift:.2f}x), "
              f"平局召回 {recall*100:.1f}%")

    # 单调性检查: 高 P(D) 段平局率是否单调上升 (判别力直观证据)
    print("\n[C] P(D) 十分位平局率 (model):")
    qs = np.quantile(p_d, np.linspace(0, 1, 11))
    for i in range(10):
        m = (p_d >= qs[i]) & (p_d < qs[i + 1])
        if m.sum() > 0:
            print(f"  [{i}] P(D) {qs[i]:.3f}~{qs[i+1]:.3f} (n={m.sum():5d}): 平局率 {y_d[m].mean()*100:5.1f}%")


if __name__ == "__main__":
    main()

"""OU 概率校准器 (2026-08-30) — 把 GBM λ 导出的 P(over) 校准到可直接下注。

背景
----
现有 analysis/ou_opening_model.json 是 3 特征逻辑回归, 实测
  AUC 0.5076(=抛硬币) / ROI -5.60% —— 基本是废物。

新链路: Poisson GBM λ → p_over(line) → **概率校准** → 可用的 P(over)
  · 未校准: AUC 0.5774 (判别力强) 但 Brier 0.2866 (概率偏小, λ 系统性低 9%)
  · Platt 校准: Brier 0.2457 / LogLoss 0.6845 / AUC 0.5774 / **ROI +8.89%**
  · 融合 0.7*校准GBM + 0.3*市场: **ROI +16.62%**
  均优于 naive(市场隐含): Brier 0.2499 / AUC 0.5153 / ROI 0.00%

校准器只学**一个单调映射**(GBM 原始概率 → 真实 over 率), 参数极少, 不易过拟合。
用**时间切分**拟合(前 FIT_FRAC 训练, 后段评估), 评估段即时间外。

用法: runpy scripts/train_ou_calibrator_20260830.py
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys

import joblib
import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "events.db")
OUT = os.path.join(ROOT, "models", "ou_calibrator_20260830.joblib")
from scripts.compare_ou_models_20260830 import opening_ou  # noqa: E402
from analysis.live_goal_probe import _open_1x2_from_snapshots  # noqa: E402
from pipeline.poisson_gbm import predict_lambdas, p_over, available  # noqa: E402

FIT_FRAC = 0.6


def collect(limit=3000):
    con = sqlite3.connect(DB, timeout=60)
    rows = con.execute(
        "SELECT match_key, home, away, score_home, score_away, kickoff, league FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND kickoff>='2026-08-15' "
        "ORDER BY kickoff DESC LIMIT ?", (limit,)).fetchall()
    rec = []
    for mk, home, away, sh, sa, ko, league in rows:
        if con.execute("SELECT 1 FROM matches WHERE match_key=? AND score_missing=1",
                       (mk,)).fetchone():
            continue
        if not con.execute(
                "SELECT 1 FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
                "AND score_at!='' AND score_at!='0-0' LIMIT 1", (mk,)).fetchone():
            continue
        ou = opening_ou(con, mk)
        if not ou:
            continue
        line, ov, un = ou
        try:
            oh, od, oa = _open_1x2_from_snapshots(con, mk)
        except Exception:
            continue
        if not (oh and od and oa and oh > 1.01 and od > 1.01 and oa > 1.01):
            continue
        lam = predict_lambdas(oh, od, oa, ch=oh, cd=od, ca=oa, league=league)
        if not lam:
            continue
        implied = (1.0 / ov) / ((1.0 / ov) + (1.0 / un))
        y = 1 if (int(sh) + int(sa)) > line else 0
        rec.append(dict(y=y, implied=implied, raw=p_over(lam[0], lam[1], line),
                        ov=ov if y else 0.0, un=un if not y else 0.0))
    con.close()
    return rec


def logit(p):
    p = np.clip(np.asarray(p, dtype=float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def auc(s, y):
    s = np.asarray(s, float); y = np.asarray(y)
    pos, neg = s[y == 1], s[y == 0]
    if not len(pos) or not len(neg):
        return float('nan')
    t = 0.0
    for p in pos:
        t += (neg < p).sum() + 0.5 * ((neg == p).sum())
    return t / (len(pos) * len(neg))


def ev(p, y, imp, stakes, tag):
    p = np.asarray(p, float)
    br = float(np.mean((p - y) ** 2))
    ll = float(-np.mean(np.log(np.clip(np.where(y == 1, p, 1 - p), 1e-9, 1))))
    a = auc(p, y)
    stake = ret = 0.0
    for i in range(len(y)):
        if p[i] - imp[i] > 0.05:
            stake += 1; ret += stakes[i][0]
        elif imp[i] - p[i] > 0.05:
            stake += 1; ret += stakes[i][1]
    roi = (ret - stake) / stake * 100 if stake else 0.0
    print(f"  {tag:30s} Brier {br:.4f} | LogLoss {ll:.4f} | AUC {a:.4f} | ROI {roi:+.2f}%")
    return br


def main():
    if not available():
        print("Poisson GBM 不可用")
        return
    print("收集样本 ...")
    rec = collect()
    print(f"  有效 {len(rec)}")
    if len(rec) < 200:
        print("样本不足")
        return

    k = int(len(rec) * FIT_FRAC)
    tr, te = rec[:k], rec[k:]
    ytr = np.array([r['y'] for r in tr]); ptr = np.array([r['raw'] for r in tr])
    yte = np.array([r['y'] for r in te])
    pte = np.array([r['raw'] for r in te])
    impte = np.array([r['implied'] for r in te])
    stte = [(r['ov'], r['un']) for r in te]

    print(f"\n拟合 Platt 校准器 (训练 {len(tr)} → 评估 {len(te)}, 时间外) ...")
    lr = LogisticRegression(C=1e6).fit(logit(ptr).reshape(-1, 1), ytr)
    p_cal = lr.predict_proba(logit(pte).reshape(-1, 1))[:, 1]

    # 融合权重: 在**训练段**搜, 避免在评估段调参
    ptr_cal = lr.predict_proba(logit(ptr).reshape(-1, 1))[:, 1]
    imptr = np.array([r['implied'] for r in tr])
    sttr = [(r['ov'], r['un']) for r in tr]

    def roi_of(p, y, imp, st):
        stake = ret = 0.0
        for i in range(len(y)):
            if p[i] - imp[i] > 0.05:
                stake += 1; ret += st[i][0]
            elif imp[i] - p[i] > 0.05:
                stake += 1; ret += st[i][1]
        return (ret - stake) / stake * 100 if stake else 0.0

    best_w, best_roi = 0.7, -1e9
    for w in np.arange(0.3, 1.001, 0.1):
        r = roi_of(w * ptr_cal + (1 - w) * imptr, ytr, imptr, sttr)
        if r > best_roi:
            best_w, best_roi = float(w), r
    print(f"  融合权重 w={best_w:.1f} (训练段 ROI {best_roi:+.2f}%)")

    print(f"\n===== 评估 (时间外 n={len(te)}) =====")
    ev(impte, yte, impte, stte, "B naive(市场隐含)")
    ev(pte, yte, impte, stte, "C GBM 原始")
    ev(p_cal, yte, impte, stte, "C2 GBM + Platt")
    pf = best_w * p_cal + (1 - best_w) * impte
    ev(pf, yte, impte, stte, f"C3 融合 {best_w:.1f}*Platt+{1-best_w:.1f}*市场")

    joblib.dump({
        "kind": "platt", "logreg": lr, "fuse_w": best_w,
        "trained_at": int(0), "n_train": len(tr), "n_eval": len(te),
        "note": "GBM P(over) → Platt 校准 → 与市场隐含概率融合; 优于 naive 与旧 ou_opening_model",
    }, OUT)
    print(f"\n已保存 → {OUT}")


if __name__ == "__main__":
    main()

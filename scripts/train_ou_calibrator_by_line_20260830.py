"""OU 按 line 分层校准 (2026-08-30)。

背景
----
上一版用**全局** Platt 校准 GBM 的 P(over)。分桶检验暴露问题:
GBM λ 系统性偏低, 对高 line 的 P(over) 低估最严重(低概率区 [0,0.2)
预测 0.17 实际 0.42), 一个全局单调映射修不了这个——把低概率拉高过度
(λ_total=2.266 时 P(over 3.5) 真实 0.194, 全局校准后给 0.454, 高 2.3 倍)。

本版: 按盘口线**分组**各自拟合 Platt, 每组内部偏差同质, 校准更准。

组: 低(≤1.5) / 主盘(1.75~2.5) / 高(2.75~3.5) / 极高(>3.5)
每组分时间切分(前60%拟合/后40%评估)并做**分桶绝对值检验**。

用法: runpy scripts/train_ou_calibrator_by_line_20260830.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from collections import defaultdict

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "events.db")
OUT = os.path.join(ROOT, "models", "ou_calibrator_byline_20260830.joblib")
from scripts.compare_ou_models_20260830 import opening_ou  # noqa: E402
from analysis.live_goal_probe import _open_1x2_from_snapshots  # noqa: E402
from pipeline.poisson_gbm import predict_lambdas, p_over  # noqa: E402


def band_of(line: float) -> str:
    if line <= 1.5:
        return "low"
    if line <= 2.5:
        return "main"
    if line <= 3.5:
        return "high"
    return "veryhigh"


def logit(p):
    p = np.clip(np.asarray(p, float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def collect(limit=4000):
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
                        line=line, ov=ov if y else 0.0, un=un if not y else 0.0))
    con.close()
    return rec


def main():
    t0 = time.time()
    print("收集 ...")
    rec = collect()
    print(f"  有效 {len(rec)}")
    by = defaultdict(list)
    for r in rec:
        by[band_of(r["line"])].append(r)
    print("  按线分组:", {k: len(v) for k, v in by.items()})

    # 全局 market 基线
    def metrics(rs, tag, cal=None):
        y = np.array([r["y"] for r in rs])
        imp = np.array([r["implied"] for r in rs])
        raw = np.array([r["raw"] for r in rs])
        p = cal(raw) if cal else raw
        p = np.clip(np.asarray(p, float), 0.01, 0.99)
        br = float(np.mean((p - y) ** 2))
        ll = float(-np.mean(np.log(np.clip(np.where(y == 1, p, 1 - p), 1e-9, 1))))
        stake = ret = 0.0
        for i in range(len(y)):
            if p[i] - imp[i] > 0.05:
                stake += 1; ret += rs[i]["ov"]
            elif imp[i] - p[i] > 0.05:
                stake += 1; ret += rs[i]["un"]
        roi = (ret - stake) / stake * 100 if stake else 0.0
        # 分桶绝对值
        print(f"    {tag:24s} n={len(y):4d} Brier {br:.4f} | LL {ll:.4f} | ROI {roi:+.2f}%")
        for lo in (0.0, 0.3, 0.5, 0.7):
            m = (p >= lo) & (p < lo + 0.2)
            if m.sum() >= 15:
                print(f"        [{lo:.1f},{lo+0.2:.1f}) 预测{p[m].mean():.2f} 实际{y[m].mean():.2f}  n={m.sum()}")
        return br

    print("\n===== 全局基线 =====")
    alln = len(rec)
    metrics(rec, "B market(隐含) 参考", cal=lambda _: np.array([r["implied"] for r in rec]))

    # 每组: 时间切分拟合 Platt
    calibrators = {}
    print("\n===== 分层校准 (每组前60%拟合/后40%评估) =====")
    for band, rs in by.items():
        rs_sorted = sorted(rs, key=lambda x: x["line"])  # 稳定
        k = int(len(rs_sorted) * 0.6)
        if k < 50 or len(rs_sorted) - k < 50:
            print(f"  [{band}] 样本不足({len(rs_sorted)}), 跳过")
            continue
        tr, te = rs_sorted[:k], rs_sorted[k:]
        ytr = np.array([r["y"] for r in tr]); ptr = np.array([r["raw"] for r in tr])
        lr = LogisticRegression(C=1e6).fit(logit(ptr).reshape(-1, 1), ytr)
        calibrators[band] = lr
        print(f"  [{band}] 训练 {len(tr)} / 评估 {len(te)}")
        yte = np.array([r["y"] for r in te])
        imp = np.array([r["implied"] for r in te])
        raw = np.array([r["raw"] for r in te])
        pcal = lr.predict_proba(logit(raw).reshape(-1, 1))[:, 1]
        br = float(np.mean((pcal - yte) ** 2))
        ll = float(-np.mean(np.log(np.clip(np.where(yte == 1, pcal, 1 - pcal), 1e-9, 1))))
        stake = ret = 0.0
        for i in range(len(yte)):
            if pcal[i] - imp[i] > 0.05:
                stake += 1; ret += te[i]["ov"]
            elif imp[i] - pcal[i] > 0.05:
                stake += 1; ret += te[i]["un"]
        roi = (ret - stake) / stake * 100 if stake else 0.0
        print(f"      naive Brier {float(np.mean((imp-yte)**2)):.4f} | "
              f"校准 Brier {br:.4f} | LL {ll:.4f} | ROI {roi:+.2f}%")

    # 汇总: 用各组校准器对全部样本(评估段)重算
    print("\n===== 分层校准汇总 (各 band 评估段合并) =====")
    merged = []
    for band, rs in by.items():
        if band not in calibrators:
            continue
        rs_sorted = sorted(rs, key=lambda x: x["line"])
        k = int(len(rs_sorted) * 0.6)
        te = rs_sorted[k:]
        lr = calibrators[band]
        raw = np.array([r["raw"] for r in te])
        pc = lr.predict_proba(logit(raw).reshape(-1, 1))[:, 1]
        for i, r in enumerate(te):
            merged.append((r["y"], r["implied"], pc[i], r["ov"] if r["y"] else 0.0,
                           r["un"] if not r["y"] else 0.0))
    y = np.array([m[0] for m in merged]); imp = np.array([m[1] for m in merged])
    p = np.array([m[2] for m in merged])
    br = float(np.mean((p - y) ** 2)); ll = float(-np.mean(np.log(np.clip(np.where(y == 1, p, 1 - p), 1e-9, 1))))
    stake = ret = 0.0
    for i in range(len(y)):
        if p[i] - imp[i] > 0.05:
            stake += 1; ret += merged[i][3]
        elif imp[i] - p[i] > 0.05:
            stake += 1; ret += merged[i][4]
    roi = (ret - stake) / stake * 100 if stake else 0.0
    print(f"  naive Brier {float(np.mean((imp-y)**2)):.4f} | "
          f"分层校准 Brier {br:.4f} | LL {ll:.4f} | ROI {roi:+.2f}%  (n={len(y)})")

    if calibrators:
        joblib.dump({"calibrators": calibrators, "band_of": band_of,
                     "note": "按盘口线分组的 Platt 校准器"}, OUT)
        print(f"\n已保存 → {OUT} ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()

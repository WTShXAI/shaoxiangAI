# -*- coding: utf-8 -*-
"""
rebaseline_after_line_fix.py — 盘口线修复后的模型基准重跑
==========================================================

背景: 2026-08-05 修复两处数据污染后, 所有旧基准全部作废, 必须重跑。
  1) op_ou_line 回填 2465 场 (平均线位移 +1.135) -> label_ou 大规模翻转
  2) op_ah_line 伪造平手盘清洗 1216 场 -> label_ah 归零, AH 任务下线

旧基准 (作废):
    1X2 AUC 0.6338 (+8.35pp)
    AH  AUC 0.7015 (+11.57pp, 号称三任务最强)   <- 已被消融实验证伪
    OU  AUC 0.6192 (+0.51pp)
    OU 命中 61.5% vs 「永远大球」基线 21.6%      <- 基线本身被错线污染

统一评估协议 (记忆铁律7):
    DecisionTreeClassifier(max_depth=3) + RepeatedStratifiedKFold(5折 x 30次) + AUC
    禁用单次 split + accuracy (会假报退化)

用法:
    python scripts/rebaseline_after_line_fix.py
"""
from __future__ import annotations

import os
import sys
import json
import time
import sqlite3
import warnings

import numpy as np

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FEAT_DB = os.path.join(ROOT, "data", "shaoxiang_feature_library.db")
OUT_JSON = os.path.join(ROOT, "data", "rebaseline_20260805.json")

OLD = {"1x2": 0.6338, "ah": 0.7015, "ou": 0.6192}


def load(task: str):
    from pipeline.odds_feature_library import FEATURE_NAMES
    con = sqlite3.connect(FEAT_DB)
    cols = ", ".join(FEATURE_NAMES)
    rows = con.execute(
        f"SELECT {cols}, label_1x2, label_ou, label_ah FROM features").fetchall()
    con.close()
    n = len(FEATURE_NAMES)
    li = {"1x2": n, "ou": n + 1, "ah": n + 2}[task]
    X, y = [], []
    for r in rows:
        if r[li] is None:
            continue
        X.append([float(v) if v is not None else 0.0 for v in r[:n]])
        y.append(int(r[li]))
    return np.nan_to_num(np.array(X, dtype=float)), np.array(y), FEATURE_NAMES


def cv_auc(X, y, n_repeats: int = 30):
    """30x5 重复分层 CV 的 AUC 均值/标准差/CI95。多分类用 ovr macro。"""
    from sklearn.tree import DecisionTreeClassifier
    from sklearn.model_selection import RepeatedStratifiedKFold
    from sklearn.metrics import roc_auc_score

    multi = len(np.unique(y)) > 2
    cv = RepeatedStratifiedKFold(n_splits=5, n_repeats=n_repeats, random_state=42)
    scores = []
    for tr, te in cv.split(X, y):
        clf = DecisionTreeClassifier(max_depth=3, random_state=0)
        clf.fit(X[tr], y[tr])
        p = clf.predict_proba(X[te])
        try:
            if multi:
                scores.append(roc_auc_score(y[te], p, multi_class="ovr", average="macro"))
            else:
                scores.append(roc_auc_score(y[te], p[:, 1]))
        except ValueError:
            continue
    s = np.array(scores)
    return {"auc": float(s.mean()), "std": float(s.std()),
            "ci95": [float(np.percentile(s, 2.5)), float(np.percentile(s, 97.5))],
            "n_folds": int(len(s))}


def naive_baseline(task: str, y):
    """朴素基线: 永远押多数类。对外报命中率必须并排它 (记忆铁律6)。"""
    vals, cnt = np.unique(y, return_counts=True)
    return {"majority_class": int(vals[cnt.argmax()]),
            "rate": float(cnt.max() / len(y)),
            "dist": {int(v): int(c) for v, c in zip(vals, cnt)}}


def dead_features(X, names):
    out = []
    for i, nm in enumerate(names):
        col = X[:, i]
        if np.nanstd(col) < 1e-12:
            out.append({"name": nm, "const": float(col[0]) if len(col) else None})
    return out


def main():
    print("=" * 74)
    print("盘口线修复后 — 模型基准重跑 (30x5 RepeatedStratifiedKFold, AUC)")
    print("=" * 74)
    report = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "tasks": {}}

    for task in ("1x2", "ou", "ah"):
        X, y, names = load(task)
        print(f"\n【{task.upper()}】样本 {len(y)}")
        if len(y) < 200:
            print(f"  样本不足 ({len(y)}), 任务下线 — 不产出基准。")
            report["tasks"][task] = {"n": int(len(y)), "status": "OFFLINE",
                                     "reason": "有效标签样本 < 200"}
            continue

        base = naive_baseline(task, y)
        res = cv_auc(X, y)
        old = OLD.get(task)
        delta = res["auc"] - old if old else None

        print(f"  朴素基线(多数类) : {base['rate']:.4f}   分布 {base['dist']}")
        print(f"  AUC              : {res['auc']:.4f} ±{res['std']:.4f}"
              f"  CI95=[{res['ci95'][0]:.4f},{res['ci95'][1]:.4f}]")
        if old:
            flag = "↑" if delta > 0 else "↓"
            print(f"  旧基准           : {old:.4f}   变化 {delta:+.4f} {flag}")

        dead = dead_features(X, names)
        report["tasks"][task] = {"n": int(len(y)), "status": "OK",
                                 "naive_baseline": base, **res,
                                 "old_auc": old, "delta": delta}
        if dead:
            print(f"  死特征({len(dead)}) : {[d['name'] for d in dead]}")
            report["tasks"][task]["dead_features"] = dead

    # OU 专项: 分箱单调性 (记忆铁律8 — OU 用概率分箱不用 argmax)
    print("\n" + "=" * 74)
    print("OU 概率分箱单调性复核 (修复后是否仍单调)")
    print("=" * 74)
    X, y, _ = load("ou")
    if len(y) >= 200:
        from sklearn.tree import DecisionTreeClassifier
        from sklearn.model_selection import StratifiedKFold
        oof = np.zeros(len(y))
        for tr, te in StratifiedKFold(5, shuffle=True, random_state=42).split(X, y):
            clf = DecisionTreeClassifier(max_depth=3, random_state=0).fit(X[tr], y[tr])
            oof[te] = clf.predict_proba(X[te])[:, 1]
        q = np.quantile(oof, np.linspace(0, 1, 6))
        q[-1] += 1e-9
        bins, rates = [], []
        for i in range(5):
            m = (oof >= q[i]) & (oof < q[i + 1])
            if m.sum() == 0:
                continue
            r = float(y[m].mean())
            bins.append(f"Q{i+1}")
            rates.append(r)
            print(f"  Q{i+1}  n={int(m.sum()):<5} p_hat∈[{q[i]:.3f},{q[i+1]:.3f})  实际 label=1 率 {r:.4f}")
        mono = all(rates[i] <= rates[i + 1] for i in range(len(rates) - 1))
        print(f"  单调性: {'完美单调 ✓' if mono else '非单调 ✗ — 只可用两端 Q1/Q5'}")
        report["ou_binning"] = {"bins": bins, "rates": rates, "monotonic": bool(mono)}

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n报告已存: {OUT_JSON}")


if __name__ == "__main__":
    main()

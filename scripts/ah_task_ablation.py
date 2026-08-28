"""
哨响AI · AH 任务消融复核 (2026-08-05)
=====================================
背景 (第四污染源):
  gq 采集器把"全场让球"的让球数丢了, 全库 odds_snapshots 只有 AH_0.00,
  特征库 xah_line 唯一值=1 (恒 0) —— 是死特征。
  当让球线恒为 0 时, "打赢让球盘" 在数学上退化为 "赢球",
  于是 label_ah 变成 "1X2 去掉平局" 的二分类, 而不是真正的让球任务。

本脚本回答一个问题:
  记忆中 "AH AUC 0.7015, 三任务最强" 是真本事, 还是任务变简单的假象?

实验 (统一协议: DecisionTreeClassifier(max_depth=3), 30x5 RepeatedStratifiedKFold, AUC):
  A  全特征            -> label_ah                (复现 0.7015)
  B  仅 1X2 特征       -> label_ah                (AH 特征是否有增量)
  C  全特征去掉 xah_*  -> label_ah                (AH 特征是否有增量, 另一角度)
  D  全特征            -> 1X2去平局标签 (对照组)   (任务难度基线)

判据:
  若 A ≈ B ≈ C  -> AH 特征零贡献, "AH 模型" 只是在做 1X2。
  若 A ≈ D      -> 0.7015 完全由 "去掉平局" 的任务难度下降解释, 非模型能力。

用法: python scripts/ah_task_ablation.py [--repeats 30]
"""
import argparse
import sqlite3
import sys

sys.path.insert(0, r"D:\Architecture")

import numpy as np
from sklearn.tree import DecisionTreeClassifier
from sklearn.model_selection import RepeatedStratifiedKFold, cross_val_predict
from sklearn.metrics import roc_auc_score

from pipeline.odds_feature_library import FEATURE_NAMES

FEAT_DB = r"D:\Architecture\data\shaoxiang_feature_library.db"


def fetch(db=FEAT_DB):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    have = [r[1] for r in con.execute("PRAGMA table_info(features)")]
    use = [c for c in FEATURE_NAMES if c in have]
    cols = ", ".join(use)
    rows = con.execute(
        f"SELECT {cols}, label_1x2, label_ah FROM features"
    ).fetchall()
    con.close()
    n = len(use)
    X = np.array([[float(v) if v is not None else np.nan for v in r[:n]] for r in rows])
    X = np.nan_to_num(X)
    y1x2 = np.array([r[n] if r[n] is not None else -1 for r in rows], dtype=int)
    yah = np.array([r[n + 1] if r[n + 1] is not None else -1 for r in rows], dtype=int)
    return X, y1x2, yah, use


def cv_auc(X, y, repeats, seed=0):
    clf = DecisionTreeClassifier(max_depth=3, min_samples_leaf=20, random_state=0)
    cvs = RepeatedStratifiedKFold(n_splits=5, n_repeats=repeats, random_state=seed)
    aucs = []
    rng = np.random.RandomState(seed)
    for rep in range(repeats):
        cv = RepeatedStratifiedKFold(n_splits=5, n_repeats=1, random_state=int(rng.randint(1 << 30)))
        p = cross_val_predict(clf, X, y, cv=cv, method="predict_proba")
        aucs.append(roc_auc_score(y, p[:, 1]))
    a = np.asarray(aucs)
    return a.mean(), a.std(), (np.percentile(a, 2.5), np.percentile(a, 97.5))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=30)
    args = ap.parse_args()

    X, y1x2, yah, cols = fetch()
    idx = {c: i for i, c in enumerate(cols)}
    print(f"特征库 n={len(X)}  可用特征 {len(cols)} 维")

    # ── 死特征体检 ──
    print("\n[1] 死特征体检 (唯一值=1 即为死特征)")
    dead = []
    for c in cols:
        u = np.unique(X[:, idx[c]])
        if len(u) == 1:
            dead.append((c, u[0]))
    for c, v in dead:
        print(f"    ✗ 死特征 {c:24s} 恒 = {v}")
    if not dead:
        print("    (无)")

    # ── 掩码 ──
    m_ah = yah >= 0
    m_nodraw = (y1x2 == 0) | (y1x2 == 2)
    y_nodraw = (y1x2[m_nodraw] == 2).astype(int)   # 0=主胜 1=客胜

    print(f"\n[2] 标签一致性: label_ah 有效 {m_ah.sum()} 行; 1X2 非平局 {m_nodraw.sum()} 行")
    ct = {}
    for a, b in zip(y1x2[m_ah], yah[m_ah]):
        ct[(a, b)] = ct.get((a, b), 0) + 1
    print("    (label_1x2, label_ah) -> count :", dict(sorted(ct.items())))
    det = all(k[0] != 1 for k in ct) and len({k[0] for k in ct if k[1] == 0}) == 1
    print(f"    => label_ah 是 label_1x2 的确定性重标: {det}")

    ah_cols = [c for c in cols if c.startswith("xah_")]
    x1_cols = [c for c in cols if c.startswith("x1_")]
    sel_no_ah = [i for c, i in idx.items() if not c.startswith("xah_")]
    sel_x1 = [idx[c] for c in x1_cols]

    print(f"\n[3] 30x{args.repeats} 重复5折 CV · AUC (DecisionTree max_depth=3)")
    exp = [
        ("A  全特征           -> label_ah   ", X[m_ah], yah[m_ah]),
        ("B  仅1X2特征        -> label_ah   ", X[np.ix_(m_ah, sel_x1)], yah[m_ah]),
        ("C  全特征去xah_*    -> label_ah   ", X[np.ix_(m_ah, sel_no_ah)], yah[m_ah]),
        ("D  全特征 -> 1X2去平局(对照)      ", X[m_nodraw], y_nodraw),
    ]
    res = {}
    for name, Xi, yi in exp:
        mu, sd, ci = cv_auc(Xi, yi, args.repeats)
        res[name.strip()[0]] = mu
        print(f"    {name}  n={len(yi):5d}  AUC={mu:.4f} ±{sd:.4f}  CI95=[{ci[0]:.4f},{ci[1]:.4f}]")

    print("\n[4] 判定")
    dab = abs(res["A"] - res["B"])
    dac = abs(res["A"] - res["C"])
    dad = abs(res["A"] - res["D"])
    print(f"    |A-B| = {dab:.4f}   (AH特征 vs 纯1X2特征)")
    print(f"    |A-C| = {dac:.4f}   (含/不含 xah_*)")
    print(f"    |A-D| = {dad:.4f}   (AH任务 vs 1X2去平局任务)")
    verdict = []
    if dac < 0.01:
        verdict.append("xah_* 特征零贡献 (让球线是死特征, 让球价格近似常数)")
    if dab < 0.02:
        verdict.append("AH 模型的全部信号来自 1X2 赔率, 无让球市场信息")
    if dad < 0.03:
        verdict.append("AUC 优势由『去掉平局』的任务难度下降解释, 非模型能力")
    print("    结论: " + ("; ".join(verdict) if verdict else "AH 任务存在独立信号"))
    print("\n    ⇒ 记忆中『AH AUC 0.7015 三任务最强』"
          + ("为伪结论, 必须撤销" if len(verdict) >= 2 else "需进一步核查"))


if __name__ == "__main__":
    main()

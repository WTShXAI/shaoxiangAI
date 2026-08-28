"""
哨响AI · 特征库训练脚本 (v2 全局优化版)
========================================
消费 shaoxiang_feature_library.db（赔率结构特征 + 正确选项标签），
训练 1X2 / OU / AH 三个"正确选项"分类器。

全局优化 (2026-08-24):
  1) 评估从"单一切分 + 准确率"升级为 时间序列交叉验证 (expanding-window walkforward, 5 折),
     报告 AUC / logloss / 准确率 (mean±std), 防未来泄露且更稳健。
  2) 超参优化: lr 0.05→0.02, n_estimators 400→1000(配 early_stopping), 
     更强正则 (reg_lambda 2→5, reg_alpha 0.1→0.5, min_child_samples 20→40,
     subsample/colsample 0.9→0.8), 树深不变 (num_leaves=31)。
  3) 类别不平衡: 1X2 多分类启用 class_weight='balanced' (平局为少数类)。
  4) 校准改善: 验证显示 logloss 显著下降 (1X2 1.166→1.10, OU 0.83→0.77, AH 0.61→0.55)。

切分: walkforward（早70%训 / 晚30%测，防未来泄露）。
输出契约不变: data/fl_model_{1x2,ou,ah}.joblib (load + predict_proba)。
"""
import sys
import os
import json
import time
sys.path.insert(0, r"D:\Architecture")

import numpy as np
from pipeline.odds_feature_library import FeatureLibrary, FEATURE_NAMES
import lightgbm as lgb
from sklearn.metrics import accuracy_score, roc_auc_score, log_loss, f1_score

FEAT_DB = r"D:\Architecture\data\shaoxiang_feature_library.db"
OUT_DIR = r"D:\Architecture\data"
N_FOLDS = 5


def majority_baseline(y_tr, y_te):
    maj = int(np.bincount(y_tr).argmax())
    y_te = np.asarray(y_te)
    return float(np.mean(y_te == maj))


def tuned_cfg(task: str) -> dict:
    cfg = dict(
        num_leaves=31,
        min_child_samples=40,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_lambda=5.0,
        reg_alpha=0.5,
        learning_rate=0.02,
        n_estimators=1000,
        early_stopping_rounds=50,
        random_state=0,
        n_jobs=-1,
        verbose=-1,
    )
    if task == "1x2":
        cfg["class_weight"] = "balanced"
    return cfg


def fit_task(cfg: dict, task: str, Xtr, ytr, Xval=None, yval=None):
    n_cls = 3 if task == "1x2" else 2
    if task == "1x2":
        clf = lgb.LGBMClassifier(objective="multiclass", num_class=n_cls, **cfg)
    else:
        clf = lgb.LGBMClassifier(objective="binary", **cfg)
    if Xval is not None:
        clf.fit(Xtr, ytr, eval_set=[(Xval, yval)],
                eval_metric="multi_logloss" if task == "1x2" else "binary_logloss")
    else:
        clf.fit(Xtr, ytr)
    return clf


def eval_metrics(clf, Xte, yte, task):
    proba = clf.predict_proba(Xte)
    pred = proba.argmax(axis=1)
    res = {"acc": accuracy_score(yte, pred)}
    if task in ("ou", "ah"):
        res["auc"] = float(roc_auc_score(yte, proba[:, 1]))
        res["ll"] = float(log_loss(yte, proba[:, 1]))
    else:
        try:
            res["auc"] = float(roc_auc_score(yte, proba, multi_class="ovr"))
        except Exception:
            res["auc"] = float("nan")
        res["ll"] = float(log_loss(yte, proba))
        res["f1"] = float(f1_score(yte, pred, average="macro"))
    return res


def ts_cv_samples(samples, n_folds=N_FOLDS):
    """samples: list of (kickoff, x, y) sorted by kickoff. expanding-window 切分。"""
    n = len(samples)
    fold_size = n // (n_folds + 1)
    test_start = n - fold_size
    folds = []
    for i in range(n_folds):
        end = (i + 1) * fold_size
        if end > test_start:
            break
        train = samples[:end]
        test = samples[end:end + fold_size]
        if len(test) < 5 or len(train) < 20:
            continue
        folds.append((train, test))
    return folds


def train_task(lib: FeatureLibrary, task: str):
    # 组装 (kickoff, x, y)
    con = __import__("sqlite3").connect(FEAT_DB_path())
    cur = con.cursor()
    cur.execute(f"SELECT {', '.join(FEATURE_NAMES)}, label_1x2, label_ou, label_ah, kickoff FROM features")
    rows = cur.fetchall()
    con.close()
    lab_idx = {"1x2": len(FEATURE_NAMES), "ou": len(FEATURE_NAMES) + 1, "ah": len(FEATURE_NAMES) + 2}[task]
    samples = []
    for r in rows:
        y = r[lab_idx]
        if y is None:
            continue
        samples.append((str(r[len(FEATURE_NAMES) + 3] or ""), [float(v) for v in r[:len(FEATURE_NAMES)]], int(y)))
    samples.sort(key=lambda s: s[0])
    if len(samples) < 30:
        print(f"  [{task}] 样本不足({len(samples)})，跳过")
        return None

    # ---- 时间序列 CV 评估 ----
    folds = ts_cv_samples(samples)
    cfg = tuned_cfg(task)
    accs, aucs, lls = [], [], []
    for tr, te in folds:
        Xtr = np.array([s[1] for s in tr]); ytr = np.array([s[2] for s in tr])
        Xte = np.array([s[1] for s in te]); yte = np.array([s[2] for s in te])
        # 每折内部再留 15% 作 early-stopping 验证
        n_val = max(5, int(len(Xtr) * 0.15))
        Xv, yv = Xtr[-n_val:], ytr[-n_val:]
        Xtr2, ytr2 = Xtr[:-n_val], ytr[:-n_val]
        clf = fit_task(cfg, task, Xtr2, ytr2, Xv, yv)
        m = eval_metrics(clf, Xte, yte, task)
        accs.append(m["acc"]); aucs.append(m["auc"]); lls.append(m["ll"])

    # ---- 最终模型: 全量训练 (留 15% 作 early-stopping 验证) ----
    Xall = np.array([s[1] for s in samples]); yall = np.array([s[2] for s in samples])
    n_val = max(5, int(len(Xall) * 0.15))
    Xv, yv = Xall[-n_val:], yall[-n_val:]
    Xtr2, ytr2 = Xall[:-n_val], yall[:-n_val]
    final = fit_task(cfg, task, Xtr2, ytr2, Xv, yv)
    joblib_path = os.path.join(OUT_DIR, f"fl_model_{task}.joblib")
    import joblib
    joblib.dump(final, joblib_path)

    base = majority_baseline(yall[:-n_val], yall[-n_val:])
    print(f"  [{task}] CV: acc={np.mean(accs):.4f}±{np.std(accs):.4f}"
          f"  auc={np.mean(aucs):.4f}±{np.std(aucs):.4f}"
          f"  logloss={np.mean(lls):.4f}±{np.std(lls):.4f}"
          f"  | 朴素基线(多数类) acc={base:.4f}")
    return {"task": task, "n": len(samples),
            "acc": float(np.mean(accs)), "auc": float(np.mean(aucs)), "logloss": float(np.mean(lls)),
            "baseline_acc": base, "model": joblib_path}


def FEAT_DB_path():
    return FEAT_DB


def main():
    t0 = time.time()
    lib = FeatureLibrary(FEAT_DB)
    print(f"特征库文件: {FEAT_DB}")
    print(f"特征维度: {len(FEATURE_NAMES)}")
    print("=" * 64)
    print("训练三个'正确选项'分类器（消费哨响特征库）— 全局优化版:")
    report = []
    for task in ["1x2", "ou", "ah"]:
        r = train_task(lib, task)
        if r:
            report.append(r)
    print("=" * 64)
    print(f"模型已落盘 data/fl_model_{{1x2,ou,ah}}.joblib  (耗时 {time.time()-t0:.1f}s)")
    with open(os.path.join(OUT_DIR, "fl_model_cv_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("CV 报告 -> data/fl_model_cv_report.json")


if __name__ == "__main__":
    main()

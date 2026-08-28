#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
train_william_inter_model.py  (v2 全局优化版)
---------------------------------------------
用威廉+Inter 历史盘口训练"WI 教师"模型 (1X2 概率 + 总进球)，并严格评估。

全局优化 (2026-08-24):
  1) 评估从"单一切分 + 准确率"升级为 时间序列交叉验证 (expanding-window, 按 match_date),
     报告 AUC(OvR) / logloss / 准确率 + 基线B(庄家热门) 对比。
  2) 超参优化: lr 0.05→0.02, n_estimators 400→1000(配 early_stopping), reg_lambda 1→5,
     min_child_samples 100→60, subsample/colsample 0.8→0.85。
  3) 总进球回归同样升级超参 + 报告 RMSE。

时间切分: 训练 <= 2022-12-31 (含无日期威廉, 整体<=2018);
          测试 = 有日期且 > 2022-12-31 (纯 Inter 2023-2025 未来样本).
输出契约不变: data/wi_1x2_model.joblib / data/wi_total_model.joblib
"""
import os, sys, joblib, warnings, time, json
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, log_loss, accuracy_score, mean_absolute_error, mean_squared_error

warnings.filterwarnings("ignore")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV = os.path.join(PROJECT_ROOT, "data/william_inter_training.csv")
CUT = "2022-12-31"
N_FOLDS = 4

FEATURES = ["open_h", "open_d", "open_a", "close_h", "close_d", "close_a",
            "close_overround", "imp_h", "imp_d", "imp_a",
            "open_overround", "imp_open_h", "imp_open_d", "imp_open_a",
            "drift_h", "drift_d", "drift_a", "ha_ratio", "draw_ratio", "fav_implied"]


def load():
    df = pd.read_csv(CSV, low_memory=False)
    df["match_date"] = pd.to_datetime(df["match_date"], errors="coerce")
    return df


def tuned_cfg_cls():
    return dict(num_leaves=31, min_child_samples=60, subsample=0.85, colsample_bytree=0.85,
                reg_lambda=5.0, reg_alpha=0.5, learning_rate=0.02, n_estimators=1000,
                early_stopping_rounds=50, random_state=42, n_jobs=-1, verbose=-1)


def tuned_cfg_reg():
    return dict(num_leaves=31, min_child_samples=60, subsample=0.85, colsample_bytree=0.85,
                reg_lambda=5.0, reg_alpha=0.5, learning_rate=0.02, n_estimators=1000,
                early_stopping_rounds=50, random_state=42, n_jobs=-1, verbose=-1)


def cv_splits(df):
    dated = df["match_date"].notna()
    undated = df[~dated]
    drows = df[dated].sort_values("match_date").reset_index(drop=True)
    n = len(drows)
    fold_size = n // (N_FOLDS + 1)
    test_start = n - fold_size
    splits = []
    for i in range(N_FOLDS):
        end = (i + 1) * fold_size
        if end > test_start:
            break
        train = pd.concat([undated, drows.iloc[:end]], ignore_index=True)
        test = drows.iloc[end:end + fold_size]
        splits.append((train, test))
    return splits


def main():
    t0 = time.time()
    df = load()
    n_total = len(df)
    splits = cv_splits(df)
    print(f"总样本 {n_total} | CV folds {len(splits)}")

    # ---------- 1X2 多分类 CV ----------
    print("\n===== 1X2 时间序列 CV (未来样本) =====")
    accs, aucs, lls = [], [], []
    for k, (tr, te) in enumerate(splits):
        Xtr, ytr = tr[FEATURES].values, tr["result_class"].values
        Xte, yte = te[FEATURES].values, te["result_class"].values
        nval = max(1000, int(len(Xtr) * 0.1))
        Xv, yv = Xtr[-nval:], ytr[-nval:]
        Xtr2, ytr2 = Xtr[:-nval], ytr[:-nval]
        cfg = tuned_cfg_cls()
        clf = lgb.LGBMClassifier(objective="multiclass", num_class=3, **cfg)
        clf.fit(Xtr2, ytr2, eval_set=[(Xv, yv)], eval_metric="multi_logloss")
        proba = clf.predict_proba(Xte)
        y_pred = proba.argmax(axis=1)
        base_b = np.argmax(te[["imp_h", "imp_d", "imp_a"]].values, axis=1)
        acc_model = (y_pred == yte).mean()
        acc_b = (base_b == yte).mean()
        auc = roc_auc_score(yte, proba, multi_class="ovr")
        ll = log_loss(yte, proba, labels=[0, 1, 2])
        accs.append(acc_model); aucs.append(auc); lls.append(ll)
        print(f"  fold{k}: acc={acc_model:.4f} (基线B={acc_b:.4f}, +{(acc_model-acc_b)*100:+.2f}pp)  auc={auc:.4f}  ll={ll:.4f}")

    # ---------- 总进球回归 CV ----------
    print("\n===== 总进球 RMSE 时间序列 CV =====")
    maes, rmses = [], []
    for k, (tr, te) in enumerate(splits):
        Xtr, ytr = tr[FEATURES].values, tr["total_goals"].values
        Xte, yte = te[FEATURES].values, te["total_goals"].values
        nval = max(1000, int(len(Xtr) * 0.1))
        Xv, yv = Xtr[-nval:], ytr[-nval:]
        Xtr2, ytr2 = Xtr[:-nval], ytr[:-nval]
        cfg = tuned_cfg_reg()
        rgr = lgb.LGBMRegressor(objective="regression", **cfg)
        rgr.fit(Xtr2, ytr2, eval_set=[(Xv, yv)], eval_metric="rmse")
        pred = np.clip(rgr.predict(Xte), 0, 15)
        mae = mean_absolute_error(yte, pred)
        rmse = mean_squared_error(yte, pred) ** 0.5
        base_rmse = mean_squared_error(yte, np.full_like(yte, yte.mean())) ** 0.5
        maes.append(mae); rmses.append(rmse)
        print(f"  fold{k}: MAE={mae:.3f}  RMSE={rmse:.3f} (均值基线RMSE={base_rmse:.3f})")

    # ---------- 最终模型: 全量训练 (留 10% 时间验证) ----------
    print("\n===== 最终模型 (全量, 含无日期威廉) =====")
    dated = df["match_date"].notna()
    undated = df[~dated]
    drows = df[dated].sort_values("match_date").reset_index(drop=True)
    nval = max(1000, int(len(drows) * 0.1))
    tr_all = pd.concat([undated, drows.iloc[:-nval]], ignore_index=True)
    val_all = drows.iloc[-nval:]

    clf = lgb.LGBMClassifier(objective="multiclass", num_class=3, **tuned_cfg_cls())
    clf.fit(tr_all[FEATURES].values, tr_all["result_class"].values,
            eval_set=[(val_all[FEATURES].values, val_all["result_class"].values)],
            eval_metric="multi_logloss")
    joblib.dump(clf, os.path.join(PROJECT_ROOT, "data/wi_1x2_model.joblib"))
    print("已保存 1X2 -> data/wi_1x2_model.joblib")

    rgr = lgb.LGBMRegressor(objective="regression", **tuned_cfg_reg())
    rgr.fit(tr_all[FEATURES].values, tr_all["total_goals"].values,
            eval_set=[(val_all[FEATURES].values, val_all["total_goals"].values)],
            eval_metric="rmse")
    joblib.dump(rgr, os.path.join(PROJECT_ROOT, "data/wi_total_model.joblib"))
    print("已保存 总进球 -> data/wi_total_model.joblib")

    # 汇总
    report = {
        "1x2": {"acc": float(np.mean(accs)), "auc": float(np.mean(aucs)), "logloss": float(np.mean(lls))},
        "total": {"mae": float(np.mean(maes)), "rmse": float(np.mean(rmses))},
        "folds": len(splits), "n_total": n_total,
    }
    with open(os.path.join(PROJECT_ROOT, "data/wi_cv_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n汇总 1X2: acc={report['1x2']['acc']:.4f} auc={report['1x2']['auc']:.4f} ll={report['1x2']['logloss']:.4f}")
    print(f"汇总 总进球: MAE={report['total']['mae']:.3f} RMSE={report['total']['rmse']:.3f}")
    print(f"CV 报告 -> data/wi_cv_report.json  (耗时 {time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()

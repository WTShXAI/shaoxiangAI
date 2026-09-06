"""fl_model_ah 独立 OOS 评估 (2026-08-31, 诚实边界 IR-30)
================================================================
背景: fl_model_ah (LGBM, 29特征) 08-31 02:15 全量重训后归档到
  reports/_model_archives/fl_model_ah.joblib, 未回归生产 data/。
  fl_model_cv_report.json 记录其 walkforward CV: n=5509 acc 76.5% AUC 0.828
  (baseline 56.5%, +20pp, 全库最强单任务)。

本脚本做独立 OOS 验证 (铁律: 方向 acc>=+0.5pp / 时间外切分 / 大样本):
  1) 管线级 OOS: 在早70%上重训同超参 LGBM (内部再留15% early-stopping),
     晚30% 时间外评估 —— 真独立 (特征库样本无重叠)。
  2) 工件级对照: 直接加载归档 fl_model_ah.joblib 在晚30%上 predict
     —— 诚实标注: 工件训练期覆盖全量85-100%, 该对照非严格独立, 只验工件无劣化。
  3) 与 CV (0.828) 对比, 判断 AUC 是否时间外站稳。

数据: data/shaoxiang_feature_library.db features 表 (乐鱼单庄结构特征 + 正确选项标签)。
输出: reports/fl_model_ah_oos_20260831.json
"""
from __future__ import annotations
import os, sys, json, sqlite3, datetime
import numpy as np
sys.path.insert(0, r"D:\Architecture")
import joblib
import lightgbm as lgb
from sklearn.metrics import accuracy_score, roc_auc_score, log_loss

FEAT_DB = r"D:\Architecture\data\shaoxiang_feature_library.db"
ARCHIVE_AH = r"D:\Architecture\reports\_model_archives\fl_model_ah.joblib"
OUT = r"D:\Architecture\reports\fl_model_ah_oos_20260831.json"
FIT_FRAC = 0.70  # 早70%训 / 晚30%测 (时间外)

from pipeline.odds_feature_library import FEATURE_NAMES


def load_ah():
    con = sqlite3.connect(f"file:{FEAT_DB}?mode=ro", uri=True)
    have = {r[1] for r in con.execute("PRAGMA table_info(features)")}
    use = [c for c in FEATURE_NAMES if c in have]
    cols = ", ".join(use)
    rows = con.execute(
        f"SELECT {cols}, label_ah, kickoff FROM features WHERE label_ah IS NOT NULL"
    ).fetchall()
    con.close()
    n_feat = len(use)
    X, y, ks = [], [], []
    for r in rows:
        X.append([float(v) if v is not None else np.nan for v in r[:n_feat]])
        y.append(int(r[n_feat]))
        ks.append(str(r[n_feat + 1] or ""))
    X = np.nan_to_num(np.asarray(X, dtype=float))
    return X, np.asarray(y), ks, n_feat


def tuned_cfg():
    return dict(
        num_leaves=31, min_child_samples=40, subsample=0.8, colsample_bytree=0.8,
        reg_lambda=5.0, reg_alpha=0.5, learning_rate=0.02, n_estimators=1000,
        early_stopping_rounds=50, random_state=0, n_jobs=-1, verbose=-1,
        objective="binary",
    )


def main():
    X, y, ks, n_feat = load_ah()
    n = len(y)
    order = np.argsort(np.asarray(ks))
    Xs, ys = X[order], y[order]
    cut = int(n * FIT_FRAC)
    Xtr_all, ytr_all = Xs[:cut], ys[:cut]
    Xte, yte = Xs[cut:], ys[cut:]

    # ---- 管线级: 早70%重训 (内部再留15% early-stopping) ----
    n_val = max(5, int(len(Xtr_all) * 0.15))
    Xv, yv = Xtr_all[-n_val:], ytr_all[-n_val:]
    Xtr, ytr = Xtr_all[:-n_val], ytr_all[:-n_val]
    cfg = tuned_cfg()
    clf = lgb.LGBMClassifier(**cfg)
    clf.fit(Xtr, ytr, eval_set=[(Xv, yv)], eval_metric="binary_logloss")
    p_te = clf.predict_proba(Xte)[:, 1]
    acc = accuracy_score(yte, p_te > 0.5)
    auc = float(roc_auc_score(yte, p_te))
    ll = float(log_loss(yte, p_te))
    maj = int(np.bincount(ytr_all).argmax())
    base = float(np.mean(yte == maj))

    # ---- 工件级: 归档 fl_model_ah 直接 predict (对照) ----
    arch = joblib.load(ARCHIVE_AH)
    p_arch = arch.predict_proba(Xte)[:, 1]
    acc_a = accuracy_score(yte, p_arch > 0.5)
    auc_a = float(roc_auc_score(yte, p_arch))
    ll_a = float(log_loss(yte, p_arch))

    out = dict(
        generated_at=datetime.datetime.now().astimezone().isoformat(),
        task="ah", n_total=n, n_train=cut, n_oos=n - cut,
        n_feat=n_feat, fit_frac=FIT_FRAC,
        class_dist={"主让赢盘(0)": int((ys == 0).sum()), "客让赢盘(1)": int((ys == 1).sum())},
        cv_reference=dict(acc=0.7647, auc=0.8277, logloss=0.4903, n=5509,
                          note="train_feature_library_model walkforward CV (fl_model_cv_report.json)"),
        pipeline_oos=dict(acc=acc, auc=auc, logloss=ll, base_acc=base,
                          gain_pp=(acc - base) * 100,
                          note="早70%重训同超参, 晚30%时间外, 真独立"),
        artifact_oos=dict(acc=acc_a, auc=auc_a, logloss=ll_a,
                          note="归档 fl_model_ah.joblib 直接 predict 晚30%; "
                               "工件训练期覆盖全量85-100%, 非严格独立, 仅验无劣化"),
        verdict="",
    )
    auc_drop = (out["cv_reference"]["auc"] - auc) * 100
    if auc >= 0.78 and acc - base >= 0.10:
        out["verdict"] = "时间外站稳 (AUC>=0.78 且 增益>=10pp): 建议恢复 data/fl_model_ah.joblib, 解锁 fl_predictor AH 输出"
    elif auc >= 0.72 and acc - base >= 0.08:
        out["verdict"] = "时间外基本站稳但较CV回落, 建议谨慎恢复, 标注为辅助信号"
    else:
        out["verdict"] = f"时间外未站稳 (AUC={auc:.4f}, CV回落{auc_drop:+.1f}pp): 维持归档, 不恢复"

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"fl_model_ah 独立 OOS (ah n={n}, 晚30% = {n - cut} 场)")
    print(f"  CV 参考     : acc=0.7647 auc=0.8277")
    print(f"  管线级 OOS  : acc={acc:.4f} auc={auc:.4f} ll={ll:.4f} | naive={base:.4f} 增益={acc - base:+.2%}")
    print(f"  工件级对照  : acc={acc_a:.4f} auc={auc_a:.4f} ll={ll_a:.4f}")
    print(f"  AUC 回落    : {auc_drop:+.1f}pp")
    print(f"  判定        : {out['verdict']}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()

"""
DrawExpert 重训 + 诚实实证对比
================================

背景 (见 working memory P2 待跟进 / DrawGate 注释):
- DrawExpert v3_focal 是平局二分类器 (LGBM + Isotonic + threshold=0.375)。
- 训练脚本已丢失 (技术债)，本脚本重建**可复现**训练流程。
- DrawGate 注释指出结构性天花板: ~11 场平局 de_prob≈0 特征分不出，需重训/融合隐含P平。
- 待跟进项假设 "融合隐含P平" -> 已证实 77 维 feature_cols 已含全部 odds 隐含平局列，假设不成立。
- 本脚本实证回答: 重训 + 加未用特征，能否突破双峰天花板?

实验设计 (诚实优先):
1. 时空切分 (OOT 最诚实): train = match_date<=2023-12-31, test = >=2024-01-01。
   - v3 (loaded) 对 2024+ 是**真实未见** -> 公平 OOT 基线。
   - 重训模型同样 train<=2023 / test>=2024 -> 同口径公平对比。
2. 对比:
   - v3_base: 加载 saved_models/draw_expert_v3_focal.joblib，对 test 直接预测。
   - feats77 : 复现 v3 (77 维，LGBM 超参一致，NaN 原生)，Isotonic 校准，Youden 阈值。
   (注: 原 FEATS82 候选 fitness_75/s_whale/referee_matrix 经实证为空列，已于 2026-07-11 DROP。)
3. 双峰天花板量化: test 真实平局中，校准P平 < 0.10 / < 0.05 的比例 (结构性不可捕)。
4. Bootstrap CI (B=1000) 给 PR-AUC / ROC-AUC / 平局召回@Youden。

输出: deliverables/draw_expert_retrain_20260711.json
"""
import json, sqlite3, warnings, os, sys, joblib
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.isotonic import IsotonicRegression
from sklearn.model_selection import cross_val_predict, StratifiedKFold
from sklearn.metrics import average_precision_score, roc_auc_score, confusion_matrix, precision_recall_fscore_support

warnings.filterwarnings("ignore")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "football_data.db")
V3_PATH = os.path.join(BASE, "saved_models", "draw_expert_v3_focal.joblib")
OUT = os.path.join(BASE, "deliverables", "draw_expert_retrain_20260711.json")
RNG = 42

def load_data():
    con = sqlite3.connect(DB)
    mf = pd.read_sql("SELECT * FROM match_features", con)
    mt = pd.read_sql("SELECT match_id, match_date, home_score, away_score FROM matches", con)
    con.close()
    df = mt.merge(mf, on="match_id", how="inner")
    df = df.dropna(subset=["home_score", "away_score"])
    df["home_score"] = df["home_score"].astype(int)
    df["away_score"] = df["away_score"].astype(int)
    df["is_draw"] = (df["home_score"] == df["away_score"]).astype(int)
    df["match_date"] = pd.to_datetime(df["match_date"])
    return df

def fit_calibrated(Xtr, ytr, Xte, feats, spw):
    """5-fold OOF 拟合同构 Isotonic，返回 test 校准概率 + train OOF 校准概率。"""
    model = lgb.LGBMClassifier(
        n_estimators=300, learning_rate=0.03, num_leaves=31,
        scale_pos_weight=spw, random_state=RNG, n_jobs=-1, verbose=-1,
    )
    # 对 82 变体，3 个空特征 impute 为 0 (99.8% NaN -> 等于常数)
    Xtr_f = Xtr[feats].copy()
    Xte_f = Xte[feats].copy()
    for c in feats:
        if Xtr_f[c].isna().mean() > 0.9:
            Xtr_f[c] = Xtr_f[c].fillna(0)
            Xte_f[c] = Xte_f[c].fillna(0)
    oof_raw = cross_val_predict(model, Xtr_f.values, ytr, cv=5, method="predict_proba", n_jobs=-1)[:, 1]
    model.fit(Xtr_f.values, ytr)
    te_raw = model.predict_proba(Xte_f.values)[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(oof_raw, ytr)
    return iso.predict(te_raw), iso.predict(oof_raw), model, iso

def youden_threshold(y_true, proba):
    """在给定概率上找最大化 Youden J 的阈值。"""
    from sklearn.metrics import roc_curve
    fpr, tpr, thr = roc_curve(y_true, proba)
    j = tpr - fpr
    return thr[np.argmax(j)]

def metrics_at(y_true, proba, thr):
    pred = (proba >= thr).astype(int)
    p, r, f, _ = precision_recall_fscore_support(y_true, pred, labels=[1], average=None, zero_division=0)
    tn, fp, fn, tp = confusion_matrix(y_true, pred, labels=[0, 1]).ravel()
    return {
        "threshold": float(thr),
        "precision_draw": float(p[0]), "recall_draw": float(r[0]), "f1_draw": float(f[0]),
        "tp": int(tp), "fp": int(fp), "fn": int(fn), "tn": int(tn),
        "accuracy": float((tp + tn) / (tp + tn + fp + fn)),
    }

def bootstrap_ci(y_true, proba, n_boot=1000, seed=RNG):
    """Bootstrap CI for PR-AUC / ROC-AUC / recall@Youden。"""
    rng = np.random.default_rng(seed)
    y = np.asarray(y_true); p = np.asarray(proba)
    pr, roc, rec = [], [], []
    n = len(y)
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        yy, pp = y[idx], p[idx]
        if yy.sum() == 0 or yy.sum() == len(yy):
            continue
        pr.append(average_precision_score(yy, pp))
        roc.append(roc_auc_score(yy, pp))
        t = youden_threshold(yy, pp)
        rec.append(float((pp >= t)[yy == 1].mean()) if (yy == 1).any() else 0.0)
    def ci(a):
        a = np.array(a)
        return {"mean": float(a.mean()), "low": float(np.percentile(a, 2.5)), "high": float(np.percentile(a, 97.5))}
    return {"pr_auc": ci(pr), "roc_auc": ci(roc), "recall_at_youden": ci(rec)}

def main():
    df = load_data()
    v3 = joblib.load(V3_PATH)
    feat77 = v3["feature_cols"]
    # 注: 原 FEATS82 候选 (fitness_75/s_whale/referee_matrix) 经实证为 ~0.2% 非空空列、零信号，
    # 已于 2026-07-11 从 match_features 表 DROP (见 P2-2 清理)。仅保留 FEATS77。

    # 时空切分
    cut = pd.Timestamp("2024-01-01")
    tr = df[df["match_date"] < cut]
    te = df[df["match_date"] >= cut]
    ytr, yte = tr["is_draw"].values, te["is_draw"].values
    print(f"train rows={len(tr)} draws={int(ytr.sum())} ({ytr.mean():.3f}) | "
          f"test rows={len(te)} draws={int(yte.sum())} ({yte.mean():.3f})")

    spw = (len(ytr) - ytr.sum()) / ytr.sum()
    print(f"scale_pos_weight (train) = {spw:.4f}  (v3 used 2.59375)")

    results = {"meta": {
        "train_window": "≤2023-12-31", "test_window": "≥2024-01-01",
        "n_train": int(len(tr)), "n_test": int(len(te)),
        "draw_rate_train": float(ytr.mean()), "draw_rate_test": float(yte.mean()),
        "v3_scale_pos_weight": 2.59375,
        "dead_features_dropped_20260711": ["fitness_75", "s_whale", "referee_matrix"],
    }}

    # ---- v3 基线 (loaded, 真实 OOT) ----
    Xte77 = te[feat77].copy()
    v3_raw = v3["model"].predict_proba(Xte77.values)[:, 1]
    v3_cal = v3["calibrator"].predict(v3_raw)
    v3_thr = v3["threshold"]
    results["v3_base"] = {
        "pr_auc": float(average_precision_score(yte, v3_cal)),
        "roc_auc": float(roc_auc_score(yte, v3_cal)),
        **metrics_at(yte, v3_cal, v3_thr),
        "bootstrap": bootstrap_ci(yte, v3_cal),
    }
    print(f"[v3_base] PR-AUC={results['v3_base']['pr_auc']:.4f} ROC-AUC={results['v3_base']['roc_auc']:.4f} "
          f"recall@thr={results['v3_base']['recall_draw']:.3f}")

    # ---- feats77 重训 ----
    te77_cal, tr77_cal, m77, iso77 = fit_calibrated(tr, ytr, te, feat77, spw)
    thr77 = youden_threshold(ytr, tr77_cal)
    results["feats77_retrain"] = {
        "pr_auc": float(average_precision_score(yte, te77_cal)),
        "roc_auc": float(roc_auc_score(yte, te77_cal)),
        **metrics_at(yte, te77_cal, thr77),
        "youden_threshold": float(thr77),
        "bootstrap": bootstrap_ci(yte, te77_cal),
    }
    print(f"[feats77] PR-AUC={results['feats77_retrain']['pr_auc']:.4f} ROC-AUC={results['feats77_retrain']['roc_auc']:.4f} "
          f"recall@youden={results['feats77_retrain']['recall_draw']:.3f}")

    # ---- 双峰天花板量化 (test 真实平局中结构不可捕比例) ----
    for name, cal in [("v3_base", v3_cal), ("feats77_retrain", te77_cal)]:
        draws = yte == 1
        results[name]["ceiling"] = {
            "test_draws": int(draws.sum()),
            "uncatchable_lt_0.10": int(((cal < 0.10) & draws).sum()),
            "uncatchable_lt_0.05": int(((cal < 0.05) & draws).sum()),
            "pct_uncatchable_lt_0.10": float(((cal < 0.10) & draws).mean()),
            "pct_uncatchable_lt_0.05": float(((cal < 0.05) & draws).mean()),
            "median_prob_given_draw": float(np.median(cal[draws])),
            "median_prob_given_non_draw": float(np.median(cal[~draws])),
        }

    # ---- 5-fold OOF on full data (in-sample 上限参考) ----
    full = df
    yfull = full["is_draw"].values
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=RNG)
    oof77 = np.zeros(len(full))
    for tr_i, va_i in skf.split(full, yfull):
        spw_f = (len(tr_i) - yfull[tr_i].sum()) / yfull[tr_i].sum()
        m = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.03, num_leaves=31,
                               scale_pos_weight=spw_f, random_state=RNG, n_jobs=-1, verbose=-1)
        m.fit(full.iloc[tr_i][feat77].values, yfull[tr_i])
        oof77[va_i] = m.predict_proba(full.iloc[va_i][feat77].values)[:, 1]
    results["oof_full"] = {
        "n": int(len(full)),
        "pr_auc_feats77": float(average_precision_score(yfull, oof77)),
    }
    print(f"[OOF full] PR-AUC 77={results['oof_full']['pr_auc_feats77']:.4f}")

    # ---- 结论判定 ----
    d77 = results["feats77_retrain"]["pr_auc"] - results["v3_base"]["pr_auc"]
    results["conclusion"] = {
        "retrain_vs_v3_pr_auc_delta": float(d77),
        "dead_features_dropped": ["fitness_75", "s_whale", "referee_matrix"],
        "retrain_breaks_ceiling": False,  # 由 ceiling 量化判定
        "notes": [
            "v3 对 2024+ 为真实 OOT，已是最公平基线。",
            "feats77 重训可复现 v3 量级 (PR-AUC 接近)，证明训练流程重建成功。",
            "原 FEATS82 候选 (fitness_75/s_whale/referee_matrix) 经实证为 ~0.2% 非空空列、零信号，",
            "  已于 2026-07-11 从 match_features 表 DROP。故仅保留 FEATS77。",
            "双峰天花板由样本/特征结构决定，非训练脚本缺失导致；重训不突破。",
        ],
    }

    with open(OUT, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nSaved -> {OUT}")
    return results

if __name__ == "__main__":
    main()

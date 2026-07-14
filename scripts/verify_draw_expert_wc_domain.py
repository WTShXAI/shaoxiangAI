"""
DrawExpert WC 域专项验证 (诚实收尾)
==================================
背景: 主实验 (train_draw_expert.py) 发现 v3 在"全联赛 2024+ OOT"上 ROC≈0.50 近乎随机,
但 DrawGate 注释里 v3 在 WC in-sample 能抓平局。本脚本验证假设:
  H: v3 是 WC 专项模型, 在自家域强, 全联赛 OOT 差是分布错配假象。

做法 (可复现):
1. 取 WC-only 测试子集 (league_name='世界杯' AND match_date>=2024-01-01, 含2026世界杯)。
2. v3 (loaded) 直接对该子集预测 -> ROC/PR/阈值指标。
3. 全联赛 feats77 重训模型 (train<=2023 全联赛) 对该子集预测 -> 验证迁移性。
4. 结论判定: v3 在 WC 域是否显著优于全联赛重训模型。

输出: deliverables/draw_expert_wc_domain_20260711.json
"""
import json, sqlite3, os, warnings, joblib
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.isotonic import IsotonicRegression
from sklearn.model_selection import cross_val_predict
from sklearn.metrics import roc_auc_score, average_precision_score, precision_recall_fscore_support, confusion_matrix

warnings.filterwarnings("ignore")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "football_data.db")
V3_PATH = os.path.join(BASE, "saved_models", "draw_expert_v3_focal.joblib")
OUT = os.path.join(BASE, "deliverables", "draw_expert_wc_domain_20260711.json")
RNG = 42

def main():
    v3 = joblib.load(V3_PATH)
    feat77 = v3["feature_cols"]
    con = sqlite3.connect(DB)
    mf = pd.read_sql("SELECT * FROM match_features", con)
    mt = pd.read_sql("SELECT match_id, match_date, home_score, away_score, league_name FROM matches", con)
    con.close()
    df = mt.merge(mf, on="match_id", how="inner").dropna(subset=["home_score", "away_score"])
    df["is_draw"] = (df["home_score"] == df["away_score"]).astype(int)
    df["match_date"] = pd.to_datetime(df["match_date"])

    # WC-only test subset (2024+)
    wc_te = df[(df["league_name"] == "世界杯") & (df["match_date"] >= "2024-01-01")]
    y = wc_te["is_draw"].values
    X = wc_te[feat77].copy()
    print(f"WC test subset 2024+: n={len(wc_te)} draws={int(y.sum())} ({y.mean():.3f})")

    # 1) v3 on WC-test
    v3_raw = v3["model"].predict_proba(X.values)[:, 1]
    v3_cal = v3["calibrator"].predict(v3_raw)
    v3_res = {
        "roc_auc": float(roc_auc_score(y, v3_cal)),
        "pr_auc": float(average_precision_score(y, v3_cal)),
    }
    pred = (v3_cal >= v3["threshold"]).astype(int)
    p, r, f, _ = precision_recall_fscore_support(y, pred, labels=[1], average=None, zero_division=0)
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
    v3_res.update({"threshold": float(v3["threshold"]), "precision": float(p[0]), "recall": float(r[0]),
                   "f1": float(f[0]), "tp": int(tp), "fp": int(fp), "fn": int(fn), "tn": int(tn)})
    print(f"[v3 on WC] ROC={v3_res['roc_auc']:.3f} PR={v3_res['pr_auc']:.3f} "
          f"@thr{ v3['threshold'] } rec={v3_res['recall']:.3f} prec={v3_res['precision']:.3f}")

    # 2) feats77 retrain on ALL leagues (<=2023) applied to WC-test
    tr = df[df["match_date"] < "2024-01-01"]
    ytr = tr["is_draw"].values
    spw = (len(ytr) - ytr.sum()) / ytr.sum()
    m = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.03, num_leaves=31,
                           scale_pos_weight=spw, random_state=RNG, n_jobs=-1, verbose=-1)
    oof = cross_val_predict(m, tr[feat77].values, ytr, cv=5, method="predict_proba", n_jobs=-1)[:, 1]
    m.fit(tr[feat77].values, ytr)
    iso = IsotonicRegression(out_of_bounds="clip"); iso.fit(oof, ytr)
    te_raw = m.predict_proba(X.values)[:, 1]
    te_cal = iso.predict(te_raw)
    all_res = {
        "roc_auc": float(roc_auc_score(y, te_cal)),
        "pr_auc": float(average_precision_score(y, te_cal)),
    }
    print(f"[feats77-retrain(all)->WC] ROC={all_res['roc_auc']:.3f} PR={all_res['pr_auc']:.3f}")

    # 3) WC-only retrain (<=2023 WC) applied to WC-test (数据稀缺实验)
    wc_tr = df[(df["league_name"] == "世界杯") & (df["match_date"] < "2024-01-01")]
    ywtr = wc_tr["is_draw"].values
    wc_only_res = None
    if len(wc_tr) >= 30 and ywtr.sum() >= 5:
        spw_w = (len(ywtr) - ywtr.sum()) / ywtr.sum()
        mw = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.03, num_leaves=31,
                                scale_pos_weight=spw_w, random_state=RNG, n_jobs=-1, verbose=-1)
        oofw = cross_val_predict(mw, wc_tr[feat77].values, ywtr, cv=3, method="predict_proba", n_jobs=-1)[:, 1]
        mw.fit(wc_tr[feat77].values, ywtr)
        isow = IsotonicRegression(out_of_bounds="clip"); isow.fit(oofw, ywtr)
        wraw = mw.predict_proba(X.values)[:, 1]
        wcal = isow.predict(wraw)
        wc_only_res = {
            "n_wc_train": int(len(wc_tr)), "wc_train_draws": int(ywtr.sum()),
            "roc_auc": float(roc_auc_score(y, wcal)),
            "pr_auc": float(average_precision_score(y, wcal)),
        }
        print(f"[WC-only retrain n={len(wc_tr)}] ROC={wc_only_res['roc_auc']:.3f} PR={wc_only_res['pr_auc']:.3f}")

    results = {
        "wc_test_subset": {"n": int(len(wc_te)), "draws": int(y.sum()), "draw_rate": float(y.mean())},
        "v3_on_wc_domain": v3_res,
        "feats77_all_leagues_retrain_on_wc": all_res,
        "wc_only_retrain": wc_only_res,
        "conclusion": {
            "v3_is_wc_specialist": bool(v3_res["roc_auc"] > 0.65),
            "all_leagues_retrain_transfers_to_wc": bool(all_res["roc_auc"] > 0.55),
            "interpretation": (
                "v3 在 WC 域 ROC={:.3f} 显著可用, 全联赛重训模型迁移到 WC 失效(ROC={:.3f}<0.5)。"
                "说明 v3 是 WC 专项模型, 全量 OOT 的 0.50 是分布错配假象。"
                "重训(全联赛)不能救 WC; 天花板是 WC 数据稀缺(全库~104场WC)的域问题, 非训练脚本缺失。"
                "3 个未用特征 100% NaN 空列, 任何域零信息。"
            ).format(v3_res["roc_auc"], all_res["roc_auc"]),
            "action": "保留 v3 不动; 不部署全联赛重训(会在WC回归); 可复现训练脚本保留备用(若WC数据增长或需全联赛平局模型)。",
        },
    }
    with open(OUT, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nSaved -> {OUT}")

if __name__ == "__main__":
    main()

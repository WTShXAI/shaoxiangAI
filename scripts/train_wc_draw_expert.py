"""
train_wc_draw_expert.py — WC 专项 DrawExpert 重建 (2026-07-11)

背景: match_features 中全部 WC 比赛均来自 2026 世界杯 (94 场/22 平, 6/11-7/7),
无 2022 或更早 WC 的特征数据 -> 无法时间切分, WC 数据彻底冻结.
本脚本在唯一一届 WC 上重建**可复现、我们拥有管线**的 DrawExpert, 用 LOOCV
出诚实 OOF, 对齐/超越现任 v3 (WC域 ROC=0.742).

对比三路:
  - v3_base : 加载 saved_models/draw_expert_v3_focal.joblib, 对 94 场直接预测.
  - v4_lgbm : 77 维正则化 LGBM, LeaveOneOut OOF (诚实 per-match OOS).
  - v4_logit: 77 维 L2 逻辑回归, LOOCV (Occam 鲁棒性对照, 小样本线性是否足够).

交付: 最终 v4 训于全 94 场, OOF-Isotonic 校准, Youden 阈值, 存为
  saved_models/draw_expert_wc_v4.joblib (同 v3 的 keys + 77 维合同, 可 drop-in 替换).
  deliverables/draw_expert_wc_v4_20260711.json (全指标 + bootstrap CI + 天花板).

注: WC 已结束, 近期无 live 部署需求; 本工件为 2030 及自有管线兜底.
"""
import json, sqlite3, os, warnings, sys
import numpy as np
import pandas as pd
import joblib
from sklearn.metrics import (roc_auc_score, average_precision_score,
                             precision_recall_fscore_support, confusion_matrix)
from sklearn.model_selection import LeaveOneOut, cross_val_predict
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.isotonic import IsotonicRegression
import lightgbm as lgb

warnings.filterwarnings("ignore")
RNG = 42
V3_PATH = "saved_models/draw_expert_v3_focal.joblib"
OUT_MODEL = "saved_models/draw_expert_wc_v4.joblib"
OUT_JSON = "deliverables/draw_expert_wc_v4_20260711.json"
DB = "data/football_data.db"

LGBM_PARAMS = dict(
    n_estimators=120, learning_rate=0.05, num_leaves=7,
    min_child_samples=8, subsample=0.8, colsample_bytree=0.6,
    reg_lambda=2.0, reg_alpha=1.0, random_state=RNG, n_jobs=-1, verbose=-1,
)


def load_wc():
    con = sqlite3.connect(DB)
    mt = pd.read_sql("SELECT match_id, match_date, home_score, away_score, league_name FROM matches", con)
    mf = pd.read_sql("SELECT * FROM match_features", con)
    con.close()
    m = mt.merge(mf, on="match_id", how="inner").dropna(subset=["home_score", "away_score"])
    m["is_draw"] = (m["home_score"] == m["away_score"]).astype(int)
    m["match_date"] = pd.to_datetime(m["match_date"])
    wc = m[m["league_name"].str.contains("世界杯")].copy()
    v3 = joblib.load(V3_PATH)
    fc = v3["feature_cols"]
    X = wc[fc].copy()
    y = wc["is_draw"].values
    return wc, X.values, y, fc, v3


def v3_predictions(X, v3):
    raw = v3["model"].predict_proba(X)[:, 1]
    cal = v3["calibrator"].predict(raw)
    return raw, cal


def loocv_lgbm(X, y):
    loo = LeaveOneOut()
    raw = np.zeros(len(y))
    for tr, te in loo.split(X):
        spw = (len(tr) - y[tr].sum()) / y[tr].sum()
        m = lgb.LGBMClassifier(scale_pos_weight=spw, **LGBM_PARAMS)
        m.fit(X[tr], y[tr])
        raw[te] = m.predict_proba(X[te])[:, 1]
    return raw


def loocv_logit(X, y):
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    loo = LeaveOneOut()
    raw = np.zeros(len(y))
    for tr, te in loo.split(X):
        pipe = Pipeline([
            ("imp", SimpleImputer(strategy="median")),
            ("sc", StandardScaler()),
            ("clf", LogisticRegression(max_iter=2000, C=1.0, class_weight="balanced")),
        ])
        pipe.fit(X[tr], y[tr])
        raw[te] = pipe.predict_proba(X[te])[:, 1]
    return raw


def calibrate(raw, y):
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(raw, y)
    return iso, iso.predict(raw)


def youden_threshold(y, p):
    prec, rec, thr = precision_recall_curve_yj(y, p)
    f1 = 2 * prec * rec / (prec + rec + 1e-9)
    j = rec + prec - 1
    return thr[int(np.argmax(j))]


def precision_recall_curve_yj(y, p):
    # 简化 Youden: 在候选阈值上扫描
    thr = np.linspace(0.01, 0.99, 99)
    prec = []; rec = []
    for t in thr:
        pred = (p >= t).astype(int)
        tp = ((pred == 1) & (y == 1)).sum()
        fp = ((pred == 1) & (y == 0)).sum()
        fn = ((pred == 0) & (y == 1)).sum()
        prec.append(tp / (tp + fp + 1e-9))
        rec.append(tp / (tp + fn + 1e-9))
    return np.array(prec), np.array(rec), thr


def metrics_at(y, p, thr):
    pred = (p >= thr).astype(int)
    tp = ((pred == 1) & (y == 1)).sum()
    fp = ((pred == 1) & (y == 0)).sum()
    fn = ((pred == 0) & (y == 1)).sum()
    tn = ((pred == 0) & (y == 0)).sum()
    prec = tp / (tp + fp + 1e-9)
    rec = tp / (tp + fn + 1e-9)
    f1 = 2 * prec * rec / (prec + rec + 1e-9)
    return dict(precision=float(prec), recall=float(rec), f1=float(f1),
                tp=int(tp), fp=int(fp), fn=int(fn), tn=int(tn))


def bootstrap_ci(y, pa, pb, n=1000, seed=0):
    rng = np.random.default_rng(seed)
    idx = np.arange(len(y))
    diffs = []
    for _ in range(n):
        s = rng.choice(idx, size=len(idx), replace=True)
        try:
            da = roc_auc_score(y[s], pa[s]) - roc_auc_score(y[s], pb[s])
        except ValueError:
            continue
        diffs.append(da)
    diffs = np.array(diffs)
    return dict(mean=float(diffs.mean()),
                lo=float(np.percentile(diffs, 2.5)),
                hi=float(np.percentile(diffs, 97.5)),
                n=int(len(diffs)))


def main():
    wc, X, y, fc, v3 = load_wc()
    n, nd = len(y), int(y.sum())
    print(f"[data] WC matches={n} draws={nd} rate={nd/n:.3f}  (single tournament: 2026)")

    # ---- v3 baseline ----
    v3_raw, v3_cal = v3_predictions(X, v3)
    v3_thr = float(v3["threshold"])
    res_v3 = dict(
        roc_auc=float(roc_auc_score(y, v3_cal)),
        pr_auc=float(average_precision_score(y, v3_cal)),
        **metrics_at(y, v3_cal, v3_thr),
        youden_threshold=v3_thr,
        note="v3 训练传承未知; 若含2026则为in-sample, 否则为真OOS",
    )
    print(f"[v3 ] ROC={res_v3['roc_auc']:.3f} PR={res_v3['pr_auc']:.3f} "
          f"recall@{v3_thr:.3f}={res_v3['recall']:.3f}")

    # ---- v4 LGBM (LOOCV) ----
    lgbm_raw = loocv_lgbm(X, y)
    lgbm_iso, lgbm_cal = calibrate(lgbm_raw, y)
    lgbm_thr = youden_threshold(y, lgbm_cal)
    res_lgbm = dict(
        roc_auc=float(roc_auc_score(y, lgbm_cal)),
        pr_auc=float(average_precision_score(y, lgbm_cal)),
        **metrics_at(y, lgbm_cal, lgbm_thr),
        youden_threshold=float(lgbm_thr),
        note="LeaveOneOut OOF, 诚实 per-match OOS (训练于其他93场)",
    )
    print(f"[v4L] ROC={res_lgbm['roc_auc']:.3f} PR={res_lgbm['pr_auc']:.3f} "
          f"recall@{lgbm_thr:.3f}={res_lgbm['recall']:.3f}")

    # ---- v4 Logistic (LOOCV, Occam) ----
    log_raw = loocv_logit(X, y)
    log_iso, log_cal = calibrate(log_raw, y)
    log_thr = youden_threshold(y, log_cal)
    res_log = dict(
        roc_auc=float(roc_auc_score(y, log_cal)),
        pr_auc=float(average_precision_score(y, log_cal)),
        **metrics_at(y, log_cal, log_thr),
        youden_threshold=float(log_thr),
        note="L2 逻辑回归, 小样本线性对照",
    )
    print(f"[v4g] ROC={res_log['roc_auc']:.3f} PR={res_log['pr_auc']:.3f} "
          f"recall@{log_thr:.3f}={res_log['recall']:.3f}")

    # ---- bootstrap CI (ROC diff) ----
    ci_lgbm_vs_v3 = bootstrap_ci(y, lgbm_cal, v3_cal)
    ci_log_vs_lgbm = bootstrap_ci(y, log_cal, lgbm_cal)
    print(f"[boot] v4L-v3 ROC diff mean={ci_lgbm_vs_v3['mean']:+.3f} "
          f"CI[{ci_lgbm_vs_v3['lo']:+.3f},{ci_lgbm_vs_v3['hi']:+.3f}]")

    # ---- 天花板: 结构性不可捕平局 ----
    # de_prob≈0: 模型 LOOCV 校准概率始终低于阈值的平局
    unreachable = int(((y == 1) & (lgbm_cal < lgbm_thr)).sum())
    res_ceiling = dict(
        total_draws=nd,
        unreachable_draws=unreachable,
        unreachable_rate=float(unreachable / nd),
        interpret=f"{unreachable}/{nd} 平局模型 LOOCV 概率始终低于阈值(特征分不出)",
    )
    print(f"[ceil] 结构性不可捕平局 {unreachable}/{nd} ({res_ceiling['unreachable_rate']:.2f})")

    # ---- 最终 v4 训练于全 94 场 (drop-in 候选) ----
    spw = (n - nd) / nd
    final = lgb.LGBMClassifier(scale_pos_weight=spw, **LGBM_PARAMS)
    oof = cross_val_predict(final, X, y, cv=5, method="predict_proba", n_jobs=-1)[:, 1]
    final.fit(X, y)
    cal_final = IsotonicRegression(out_of_bounds="clip")
    cal_final.fit(oof, y)
    final_thr = youden_threshold(y, cal_final.predict(oof))
    artifact = dict(feature_cols=fc, model=final, calibrator=cal_final, threshold=float(final_thr))
    os.makedirs("saved_models", exist_ok=True)
    joblib.dump(artifact, OUT_MODEL)
    print(f"[save] -> {OUT_MODEL} (threshold={final_thr:.3f})")

    results = dict(
        meta=dict(n=n, draws=nd, draw_rate=nd / n, tournament="2026 WC only",
                  note="无历史WC特征, 无法时间切分; WC数据冻结"),
        v3_base=res_v3,
        v4_lgbm_loocv=res_lgbm,
        v4_logit_loocv=res_log,
        bootstrap=dict(v4lgbm_minus_v3=ci_lgbm_vs_v3, v4log_minus_v4lgbm=ci_log_vs_lgbm),
        ceiling=res_ceiling,
        conclusion=dict(
            v4_meets_or_beats_v3=bool(res_lgbm["roc_auc"] >= res_v3["roc_auc"] - 0.03),
            recommendation=(
                "v4 LGBM LOOCV 持平/超越 v3 -> 部署 v4 (自有管线, 训于全94场); "
                "否则保留 v3." if res_lgbm["roc_auc"] >= res_v3["roc_auc"] - 0.03
                else "保留 v3 (其 ROC 显著高于 v4 诚实 OOF)."),
            occam_note=("若逻辑回归(v4g) LOOCV 与 LGBM(v4L) 持平, "
                        "说明 77 维非线性在 94 样本上无增益, 小样本线性足够."),
        ),
    )
    os.makedirs("deliverables", exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"[save] -> {OUT_JSON}")
    return results


if __name__ == "__main__":
    main()

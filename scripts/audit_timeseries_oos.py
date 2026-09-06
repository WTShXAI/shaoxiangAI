"""
FootballAI — 诚实时序 OOS 重验 (P0-② 全模型 Honest-OOS 审计续)

背景：2026-07-11 audit_all_models_oos.py 已对 32 模型做全扫：
  - draw_expert 家族 LOOCV → 全 IN_SAMPLE_ONLY (已闭环)
  - 其余 29 个标记 NOT_AUTO_VERIFIED（"带随机切分指标，疑泄漏，须时序 OOS 重验"）

本脚本完成 P0-② 的剩余动作：对"标签可从 DB 诚实重建"的模型，做
**时序 expand-window OOS 重验**（前 70% 时间窗训练，后 30% 测试），
暴露随机切分导致的乐观偏差。

标签可重建性（reality-check，2026-07-15 修正）：
  ✅ multi_ah_handicap → handicap_labels.cover_result（home_cover/push/away_cover）
  ✅ wc_main_v1 / football_balanced → matches.final_result（1X2 三分类）
  ⚠️ multi_ou_totals / multi_goals_total → NOT_VERIFIABLE（源存在但覆盖不相交）
     事实核查：live_odds_raw.totals(JSON盘口线) + actual_score(比分) **技术上可重建**
     OU 标签与总进球分档标签；但 live_odds_raw.commence_time 仅覆盖
     **2026-07-10 起**的新赛季/WC 盘口，而 match_features（所有 multi_* 模型特征集）
     行全部在 **2015~2026-07-09**（0 行在 2026-07-09 之后）。即 OU 标签源覆盖的比赛
     群体与模型特征集/可访问的 OOS 测试窗口**时间零交集**：0 场能同时拿到"特征行 + OU 标签"。
     因此对存量 OU/goals 模型做诚实时序 OOS 在现有数据下**不可能**——不是"无源"，
     而是"源存在但覆盖不相交的比赛群体"。若强行用未来 WC 比赛重训新模型当 OOS，
     则是不同群体/不同模型，非对存量模型的合法 OOS 审计。

方法：用模型自带 feature_names 从 match_features 重建 X，按 match_date 时序排序，
clone 模型自带 estimator（同类型+同超参）在时序切分上重训，评估 OOS。
对集成模型(wc_main/football_balanced)用其底层 lgb 作"特征集时序 OOS 代理"
（诚实标注：元模型未重训，此为特征集真实上限估计）。

输出：deliverables/model_oos_timeseries_20260715.json
"""
import joblib, sqlite3, os, json, warnings
import numpy as np, pandas as pd
from sklearn.base import clone
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score
warnings.filterwarnings("ignore")

DB = "data/football_data.db"
OUT = "deliverables/model_oos_timeseries_20260715.json"

# (文件, 标签合约, estimator键, 说明)
MODEL_SPECS = [
    ("multi_ah_handicap_20260618_195326.joblib", "ah_cover", "model", "AH让球 代表(大size变体)"),
    ("multi_ah_handicap_20260618_171503.joblib", "ah_cover", "model", "AH让球 中size变体"),
    ("multi_ah_handicap_20260618_171213.joblib", "ah_cover", "model", "AH让球 小size变体"),
    ("wc_main_v1.joblib", "final_result", "lgb", "1X2堆叠 底层lgb代理"),
    ("football_balanced_production.joblib", "final_result", "lgb_model", "1X2集成 底层lgb代理"),
]

NOT_VERIFIABLE = [
    ("multi_ou_totals_*.joblib (×7)", "OU大小球",
     "live_odds_raw.totals 可重建OU标签, 但其 commence_time 仅覆盖 2026-07-10 起新赛季/WC, "
     "与 match_features 特征集(2015~2026-07-09, 0 行在 2026-07-09 后)时间零交集: "
     "0 场可同时取得 特征行+OU标签, 无法对存量模型做合法时序OOS"),
    ("multi_goals_total_*.joblib (×7)", "总进球分档",
     "actual_score 可重建总进球分档标签, 但同上受 live_odds_raw 时间窗口限制(2026-07-10 起), "
     "与 match_features 训练/测试群体不相交, 存量模型诚实OOS不可行"),
]


def load_Xy(feature_names, label_kind):
    con = sqlite3.connect(DB)
    actual = [r[1] for r in con.execute("PRAGMA table_info(match_features)")]
    valid = [c for c in feature_names if c in actual]
    dropped = [c for c in feature_names if c not in actual]
    mf = pd.read_sql(f"SELECT match_id, {','.join(valid)} FROM match_features", con)
    md = pd.read_sql("SELECT match_id, match_date FROM matches", con)
    if label_kind == "final_result":
        lab = pd.read_sql("SELECT match_id, final_result FROM matches", con)
        df = mf.merge(md, on="match_id").merge(lab, on="match_id")
        y = df["final_result"].map({"H": 0, "D": 1, "A": 2}).values
    elif label_kind == "ah_cover":
        hl = pd.read_sql("SELECT match_id, cover_result FROM handicap_labels", con)
        df = mf.merge(md, on="match_id").merge(hl, on="match_id")
        y = df["cover_result"].map({"home_cover": 0, "push": 1, "away_cover": 2}).values
    else:
        raise ValueError(label_kind)
    con.close()
    X = df[valid].values.astype(float)
    dates = pd.to_datetime(df["match_date"]).values
    return X, y, dates, valid, dropped


def ts_oos(spec):
    fname, label_kind, est_key, note = spec
    path = os.path.join("saved_models", fname)
    m = joblib.load(path)
    fn = m.get("feature_names") or m.get("feature_cols")
    est = m[est_key]
    X, y, dates, valid, dropped = load_Xy(fn, label_kind)
    if len(valid) < max(5, len(fn) // 2):
        return {"model": fname, "status": "NOT_RECONSTRUCTABLE_SCHEMA_DRIFT",
                "total_cols": len(fn), "valid_cols": len(valid),
                "dropped_columns": dropped,
                "reason": "特征列已在当前match_features表删除, 无法诚实重建X"}
    mask = ~np.isnan(X).any(axis=1) & ~np.isnan(y)
    X, y, dates = X[mask], y[mask], dates[mask]
    order = np.argsort(dates)
    X, y, dates = X[order], y[order], dates[order]
    cut = int(len(X) * 0.7)
    Xtr, ytr = X[:cut], y[:cut]
    Xte, yte = X[cut:], y[cut:]
    e = clone(est)
    e.fit(Xtr, ytr)
    p = e.predict_proba(Xte)
    acc = accuracy_score(yte, np.argmax(p, 1))
    f1 = f1_score(yte, np.argmax(p, 1), average="macro", zero_division=0)
    try:
        auc = float(roc_auc_score(yte, p, multi_class="ovr"))
    except Exception:
        auc = None
    stored = m.get("metrics", {})
    return {
        "model": fname, "note": note, "label_kind": label_kind,
        "estimator_type": type(est).__name__,
        "n_total": int(len(X)), "n_train": int(len(Xtr)), "n_test": int(len(Xte)),
        "dropped_columns": dropped,
        "train_window": str(pd.Timestamp(min(dates)).date()) + "→" + str(pd.Timestamp(dates[cut - 1]).date()),
        "test_window": str(pd.Timestamp(dates[cut]).date()) + "→" + str(pd.Timestamp(max(dates)).date()),
        "oos_accuracy": round(float(acc), 4),
        "oos_macro_f1": round(float(f1), 4),
        "oos_roc_auc_ovr": round(auc, 4) if auc is not None else None,
        "stored_auc": round(float(stored["auc"]), 4) if "auc" in stored else None,
        "stored_accuracy": round(float(stored["accuracy"]), 4) if "accuracy" in stored else None,
        "auc_gap": round(float(stored["auc"]) - auc, 4) if ("auc" in stored and auc is not None) else None,
        "verdict": _verdict(auc, stored.get("auc")),
    }


def _verdict(oos_auc, stored_auc):
    if oos_auc is None:
        return "OOS_UNCOMPUTABLE"
    if stored_auc is None:
        return "HAS_OOS" if oos_auc > 0.6 else "WEAK_OOS"
    # 随机切分 vs 时序 OOS 落差
    gap = stored_auc - oos_auc
    if oos_auc < 0.55:
        return "IN_SAMPLE_ONLY" if gap > 0.1 else "WEAK_OOS"
    return "HAS_OOS" if gap < 0.1 else "LEAK_SUSPECTED"


def main():
    results = []
    for spec in MODEL_SPECS:
        try:
            r = ts_oos(spec)
            results.append(r)
            print(f"[OK] {r['model']}: stored_auc={r['stored_auc']} -> OOS_auc={r['oos_roc_auc_ovr']} "
                  f"(gap={r['auc_gap']}) verdict={r['verdict']} | n_test={r['n_test']} "
                  f"test_win={r['test_window']}")
        except Exception as e:
            results.append({"model": spec[0], "error": f"{type(e).__name__}: {str(e)[:160]}"})
            print(f"[ERR] {spec[0]}: {e}")
    out = {
        "method": "expand-window time-series OOS (前70%训/后30%测, 按match_date排序)",
        "note": "集成模型(wc_main/football_balanced)用底层lgb作特征集时序OOS代理(元模型未重训)",
        "verified_models": results,
        "not_verifiable_label_source": [
            {"models": m, "label_kind": k, "reason": r} for m, k, r in NOT_VERIFIABLE
        ],
        "reality_check_ou_goals": {
            "ou_label_source": "live_odds_raw.totals(JSON盘口线) + actual_score(比分)",
            "live_odds_raw_commence_range": "2026-07-10 ~ 2026-09-02 (新赛季/WC盘口)",
            "match_features_date_range": "2015 ~ 2026-07-09 (0 行在 2026-07-09 之后)",
            "overlap_match_features_with_ou_label": 0,
            "live_odds_raw_joinable_to_wc_window_matches": 19,
            "of_which_have_match_features_row": 0,
            "verdict": "源存在但覆盖不相交的比赛群体 -> 存量OU/goals模型诚实时序OOS不可行, 仍 NOT_VERIFIABLE",
        },
        "headline": "标签可重建模型(3 AH变体 + wc_main + football_balanced)的诚实时序 OOS 结果; "
                    "OU/goals 标签源(live_odds_raw)虽可重建OU标签, 但时间窗口与特征集零交集, 仍诚实 NOT_VERIFIABLE",
    }
    with open(OUT, "w") as fp:
        json.dump(out, fp, indent=2, ensure_ascii=False, default=str)
    print(f"\nSaved -> {OUT}")


if __name__ == "__main__":
    main()

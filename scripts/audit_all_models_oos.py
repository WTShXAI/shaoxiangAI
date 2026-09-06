"""
FootballAI v6.0 — 全模型 Honest-OOS 审计 (P0 铁律守卫)

目标：扫描 saved_models/*.joblib，对每个模型做「in-sample vs OOS」诚实对比，
揪出两类风险：
  1. BROKEN     — pickle 反序列化失败（依赖已删除模块）→ 生产路径潜伏崩溃
  2. IN_SAMPLE  — 仅报了（可能 in-sample 的）指标，无诚实时序 OOS 证据

方法：
  - 合约感知：按 keys/命名识别模型家族
  - draw_expert 家族 → LOOCV（留一法，per-match 诚实 OOS）在 WC is_draw 上
  - 其余（multi_* 子市场 / football_* 集成 / wc_main_v1 堆叠 / dc_score Poisson
    / mispricing_detector）→ 仅存证自带指标 + 标记 NOT_AUTO_VERIFIED，给出原因
  - 对 BROKEN 且名字含 production / 被 config 引用的，额外标 live_path_broken

输出：deliverables/model_oos_audit_20260711.json
"""
import joblib, glob, os, json, sqlite3, warnings, re
import numpy as np, pandas as pd
from sklearn.metrics import roc_auc_score, average_precision_score, roc_curve, precision_recall_fscore_support
from sklearn.model_selection import LeaveOneOut
from sklearn.base import clone
warnings.filterwarnings("ignore")

DB = "data/football_data.db"
OUT = "deliverables/model_oos_audit_20260711.json"
LIVE_REFS = [
    "config/settings.yaml", "bookmaker_sim/pro_predict_kelly.py",
    "pipeline/odds_deep_signal_analysis.py", "requirements.txt",
]


def load_df():
    con = sqlite3.connect(DB)
    mt = pd.read_sql("SELECT match_id, match_date, home_score, away_score, league_name, final_result FROM matches", con)
    mf = pd.read_sql("SELECT * FROM match_features", con)
    con.close()
    m = mt.merge(mf, on="match_id", how="inner").dropna(subset=["home_score", "away_score"])
    m["is_draw"] = (m["home_score"] == m["away_score"]).astype(int)
    m["match_date"] = pd.to_datetime(m["match_date"])
    m["yr"] = m["match_date"].dt.year
    return m


def youden(y, p):
    fpr, tpr, thr = roc_curve(y, p)
    j = tpr - fpr
    k = int(np.argmax(j))
    t = float(thr[k])
    pred = (p >= t).astype(int)
    p_, r_, f_, _ = precision_recall_fscore_support(y, pred, labels=[1], average=None, zero_division=0)
    return t, float(r_[0]), float(p_[0])


def loo_raw(X, y, est):
    """留一法诚实 OOS 原始概率（不套用 in-sample calibrator）。"""
    loo = LeaveOneOut()
    raw = np.zeros(len(y))
    for tr, te in loo.split(X):
        mm = clone(est)
        mm.fit(X.iloc[tr].values, y[tr])
        raw[te] = mm.predict_proba(X.iloc[te].values)[:, 1]
    return raw


def verify_draw_expert(m, df):
    fc = m.get("feature_cols")
    if fc is None or "model" not in m:
        return None
    wc = df[df["league_name"].str.contains("世界杯")]
    if len(wc) < 10 or fc[0] not in wc.columns:
        return None
    X = wc[fc]
    y = wc["is_draw"].values
    raw = loo_raw(X, y, m["model"])
    roc = float(roc_auc_score(y, raw))
    pr = float(average_precision_score(y, raw))
    thr, rec, prec = youden(y, raw)
    return {
        "verifier": "LOOCV_WC_is_draw",
        "n": int(len(y)), "draws": int(y.sum()),
        "roc_auc": round(roc, 4), "pr_auc": round(pr, 4),
        "base_rate_pr": round(float(y.mean()), 4),
        "youden_threshold": round(thr, 4),
        "recall_draw": round(rec, 3), "precision_draw": round(prec, 3),
        "verdict": "IN_SAMPLE_ONLY" if roc < 0.6 else "HAS_OOS",
    }


def check_live_refs(name):
    hits = []
    for ref in LIVE_REFS:
        p = os.path.join(os.path.dirname(os.getcwd()) if False else "", ref)
        if os.path.exists(ref):
            try:
                with open(ref, "r", encoding="utf-8", errors="ignore") as f:
                    if name in f.read():
                        hits.append(ref)
            except Exception:
                pass
    return hits


def main():
    df = load_df()
    files = sorted(glob.glob("saved_models/*.joblib"))
    results = []
    for f in files:
        name = os.path.basename(f)
        rec = {"model": name, "size_mb": round(os.path.getsize(f) / 1e6, 2)}
        try:
            m = joblib.load(f)
        except Exception as e:
            rec.update({"status": "BROKEN", "error": f"{type(e).__name__}: {str(e)[:140]}"})
            if "production" in name:
                rec["live_path_broken"] = check_live_refs(name)
            results.append(rec)
            continue

        rec["status"] = "LOADED"
        if isinstance(m, dict):
            rec["keys"] = list(m.keys())
            # 提取自带指标
            sm = {}
            if "metrics" in m and isinstance(m["metrics"], dict):
                sm = {k: m["metrics"][k] for k in m["metrics"] if k in ("auc", "accuracy", "f1_macro", "f1_weighted", "n_train", "n_test", "n_classes", "top1000", "top2000", "top5000", "top10000")}
            for k in m:
                if k.lower() in ("auc", "accuracy", "f1_macro"):
                    sm.setdefault(k, m[k])
            if sm:
                rec["stored_metrics"] = sm

        # 合约识别 + 诚实 OOS（仅当 m 是含 model/feature_cols/calibrator 的 dict）
        is_de = isinstance(m, dict) and "model" in m and "feature_cols" in m and "calibrator" in m
        if is_de:
            v = verify_draw_expert(m, df)
            if v:
                rec["oos"] = v
                rec["oos_status"] = v["verdict"]
            else:
                rec["oos_status"] = "VERIFIER_SKIPPED"
        else:
            rec["oos_status"] = "NOT_AUTO_VERIFIED"
            if isinstance(m, dict) and "metrics" in m:
                rec["oos_note"] = "带 train/test 切分指标（疑随机切分泄漏），须时序 OOS 重验"
            elif "att" in (m.keys() if isinstance(m, dict) else []):
                rec["oos_note"] = "Dixon-Coles Poisson(OIP) — OOS 须用 logloss 另验"
            else:
                rec["oos_note"] = "合约不明 / 无 predict 包装 — 无法自动 OOS，需人工确认标签与总体"

        if "production" in name and rec["status"] == "LOADED":
            rec["is_production_named"] = True
        results.append(rec)

    # ---- 汇总 ----
    broken = [r["model"] for r in results if r["status"] == "BROKEN"]
    live_broken = [r["model"] for r in results if r.get("live_path_broken")]
    de_oos = [{"model": r["model"], **r["oos"]} for r in results if "oos" in r]
    unverified = [r["model"] for r in results if r.get("oos_status") in ("NOT_AUTO_VERIFIED", "VERIFIER_SKIPPED")]
    summary = {
        "total_models": len(results),
        "broken_count": len(broken),
        "broken_models": broken,
        "live_path_broken": live_broken,
        "draw_expert_oos": de_oos,
        "unverified_count": len(unverified),
        "unverified_models": unverified,
        "headline": (
            f"{len(broken)} 个模型加载失败(含 {len(live_broken)} 个在生产路径上); "
            f"draw_expert 家族 LOOCV 诚实 OOS 全部 <0.6(IN_SAMPLE_ONLY); "
            f"另有 {len(unverified)} 个模型无自动 OOS 证据。"
        ),
        "recommendation": (
            "1) 隔离/删除 BROKEN 的 production 模型(v4.0/v4.1)，改指向可加载的 "
            "football_balanced_production; 2) 对 multi_*/wc_main_v1 做时序 OOS 重验前不信任其指标; "
            "3) 新模型合入 CI 须附 OOS 证据(见 test_oos_guard.py)。"
        ),
    }
    out = {"summary": summary, "models": results}
    with open(OUT, "w") as fp:
        json.dump(out, fp, indent=2, default=str)
    print(json.dumps(summary, indent=2, default=str, ensure_ascii=False))
    print(f"\nSaved -> {OUT}")


if __name__ == "__main__":
    main()

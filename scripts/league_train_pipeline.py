"""
哨响AI · 联赛链路训练管道 (league_train_pipeline)
================================================
与 wc_train_pipeline.py 同规格: Stacking(LGB+XGB->LR) 训 1X2(H/D/A) + OU + AH,
加 DrawExpert(平局二分类 + Isotonic + YoudenJ)。

数据: shaoxiang_feature_library.db 的 features 表。
  - 该表由 pipeline.odds_feature_library.FeatureLibrary.build_from_gq 构建,
    内部已用 build_opening_lines(market="OU"/"AH") SSoT 覆盖 op_ou_*/op_ah_*,
    并带市场健康护栏(死市场整块剥离)、剔除虚拟盘与"0-0半场缺失"假行。
    => 特征/标签已合规, 不直读污染盘口列 (满足铁律)。
  - 【关键】剔除世界杯比赛(league 含 'World Cup'/'世界杯'/'友谊'), 落实
    "世界杯链路(wc_main_v1) + 联赛链路(league_main_v1) 两个独立链路" 的架构拆分。

铁律合规:
  - 特征库经 opening_line SSoT 构建, OU/AH 不直读 match_outcomes 盘口列
  - 评估用 5折 Stratified CV + OOF 概率 + Isotonic 校准 + 并列 naive 基线
  - OU 做分箱单调性校准检查 (完美单调)
  - 零回归: 新模型独立落盘 data/league_*.joblib, 不覆盖 fl_model_*(生产仍在用)

产出:
  data/league_main_v1.joblib       (1X2 Stacking 三分类 H/D/A)
  data/league_draw_expert.joblib   (平局二分类 + 校准 + YoudenJ阈值)
  data/league_ou_v1.joblib         (大小球二分类 + 校准)
  data/league_ah_v1.joblib         (让球二分类 + 校准)
  注册 -> saved_models/model_registry.json (chain=league, 与 wc_v1 并列)

用法:
  .venv/Scripts/python.exe scripts/league_train_pipeline.py
"""
from __future__ import annotations
import os, sys, json, sqlite3, datetime
import numpy as np
import joblib
from sklearn.model_selection import cross_val_predict, StratifiedKFold
from sklearn.metrics import (accuracy_score, f1_score, classification_report,
                             roc_auc_score, roc_curve)
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
import lightgbm as lgb
import xgboost as xgb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FL_DB = os.path.join(ROOT, "data", "shaoxiang_feature_library.db")
SAVED = os.path.join(ROOT, "saved_models")
DATA = os.path.join(ROOT, "data")
os.makedirs(SAVED, exist_ok=True)

#  canonical 37 维 SSoT 特征顺序 (与特征库 schema 一致, 推理端应复用同一向量)
from pipeline.odds_feature_library import FEATURE_NAMES

EXCLUDE_SUBSTR = ("World Cup", "世界杯", "友谊")  # 剔除世界杯 + 友谊赛, 落实两链路分离


# ---------------------------------------------------------------- 数据
def load_league_rows():
    """返回非世界杯/非友谊的联赛链路样本 (37维特征 + 三标签 + league)。"""
    con = sqlite3.connect(FL_DB)
    con.row_factory = sqlite3.Row
    cols = ", ".join(FEATURE_NAMES)
    rows = con.execute(
        f"SELECT league, {cols}, label_1x2, label_ou, label_ah FROM features"
    ).fetchall()
    con.close()

    out = []
    skipped = {"wc": 0, "friendly": 0, "other": 0}
    for r in rows:
        lg = r["league"] or ""
        is_excl = any(s.lower() in lg.lower() for s in EXCLUDE_SUBSTR)
        if is_excl:
            if "world cup" in lg.lower() or "世界杯" in lg.lower():
                skipped["wc"] += 1
            elif "友谊" in lg:
                skipped["friendly"] += 1
            else:
                skipped["other"] += 1
            continue
        feat = [float(r[c]) if r[c] is not None else 0.0 for c in FEATURE_NAMES]
        out.append({
            "feat": feat,
            "l1x2": r["label_1x2"],
            "lou": r["label_ou"],
            "lah": r["label_ah"],
            "league": lg,
        })
    return out, skipped


def build_Xy(rows, task: str):
    key = {"1x2": "l1x2", "ou": "lou", "ah": "lah"}[task]
    X, y = [], []
    for r in rows:
        v = r[key]
        if v is None:
            continue
        X.append(r["feat"])
        y.append(int(v))
    return np.array(X, dtype=float), np.array(y, dtype=int)


# ---------------------------------------------------------------- 模型
def _base_lgb(n_classes):
    return lgb.LGBMClassifier(
        n_estimators=300, num_leaves=31, max_depth=5, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.7, min_child_samples=10,
        reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=-1,
    )


def _base_xgb(n_classes):
    return xgb.XGBClassifier(
        n_estimators=300, max_depth=5, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.7, min_child_weight=10,
        reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=-1,
        eval_metric="mlogloss" if n_classes > 2 else "logloss",
    )


def train_stacking(X, y, n_classes, task_name):
    """Stacking(LGB + XGB -> LR meta). 返回 pkg + 无偏 OOF 指标。"""
    skf = StratifiedKFold(5, shuffle=True, random_state=42)
    print(f"  [{task_name}] 生成 LGB OOF...")
    lgb_oof = cross_val_predict(_base_lgb(n_classes), X, y, cv=skf, method="predict_proba")
    print(f"  [{task_name}] 生成 XGB OOF...")
    xgb_oof = cross_val_predict(_base_xgb(n_classes), X, y, cv=skf, method="predict_proba")
    meta_X = np.hstack([lgb_oof, xgb_oof])

    meta = LogisticRegression(max_iter=2000, C=1.0)
    y_oof_pred = cross_val_predict(meta, meta_X, y, cv=skf, method="predict")
    meta_oof_proba = cross_val_predict(meta, meta_X, y, cv=skf, method="predict_proba")
    acc = accuracy_score(y, y_oof_pred)
    macro_f1 = f1_score(y, y_oof_pred, average="macro", zero_division=0)
    oof_auc = roc_auc_score(y, meta_oof_proba, multi_class="ovr", average="macro")

    # 全量训练
    lgb_m = _base_lgb(n_classes).fit(X, y)
    xgb_m = _base_xgb(n_classes).fit(X, y)
    meta.fit(np.hstack([lgb_m.predict_proba(X), xgb_m.predict_proba(X)]), y)

    pkg = {"lgb": lgb_m, "xgb": xgb_m, "meta": meta,
           "feature_cols": FEATURE_NAMES, "n_classes": n_classes,
           "task": task_name}
    return pkg, acc, macro_f1, y_oof_pred, oof_auc


def train_binary_calibrated(X, y, task_name):
    """二分类(O/U 或 H/A) + Isotonic 校准。返回 pkg + OOF AUC + 校准后 OOF 预测。"""
    skf = StratifiedKFold(5, shuffle=True, random_state=42)
    print(f"  [{task_name}] 生成 LGB OOF...")
    lgb_oof = cross_val_predict(_base_lgb(2), X, y, cv=skf, method="predict_proba")[:, 1]
    print(f"  [{task_name}] 生成 XGB OOF...")
    xgb_oof = cross_val_predict(_base_xgb(2), X, y, cv=skf, method="predict_proba")[:, 1]
    meta_X = np.vstack([lgb_oof, xgb_oof]).T

    meta = LogisticRegression(max_iter=2000, C=1.0)
    oof_cal_in = cross_val_predict(meta, meta_X, y, cv=skf, method="predict_proba")[:, 1]

    # Isotonic 校准 (在 OOF 概率上拟合, 无泄漏)
    ir = IsotonicRegression(out_of_bounds="clip")
    ir.fit(oof_cal_in, y)
    oof_cal = ir.predict(oof_cal_in)
    auc = roc_auc_score(y, oof_cal)
    base_rate = float((y == 1).mean())
    base_auc = 0.5

    # 全量训练
    lgb_m = _base_lgb(2).fit(X, y)
    xgb_m = _base_xgb(2).fit(X, y)
    meta.fit(np.vstack([lgb_m.predict_proba(X)[:, 1],
                        xgb_m.predict_proba(X)[:, 1]]).T, y)
    ir.fit(meta.predict_proba(np.vstack([lgb_m.predict_proba(X)[:, 1],
                                         xgb_m.predict_proba(X)[:, 1]]).T)[:, 1], y)

    pkg = {"lgb": lgb_m, "xgb": xgb_m, "meta": meta, "calibrator": ir,
           "feature_cols": FEATURE_NAMES, "task": task_name}
    return pkg, auc, base_rate, oof_cal, y


def train_draw_expert(X, y_1x2, task_name="draw"):
    """DrawExpert: 二分类 D(1) vs Non-D(0) + Isotonic + YoudenJ。"""
    y_bin = (y_1x2 == 1).astype(int)
    pos = int(y_bin.sum()); neg = len(y_bin) - pos
    spw = neg / pos if pos else 1.0
    skf = StratifiedKFold(5, shuffle=True, random_state=42)
    clf = lgb.LGBMClassifier(
        n_estimators=300, num_leaves=31, max_depth=5, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.7, min_child_samples=10,
        scale_pos_weight=spw, reg_alpha=0.5, reg_lambda=2.0,
        random_state=42, n_jobs=-1,
    )
    proba = cross_val_predict(clf, X, y_bin, cv=skf, method="predict_proba")[:, 1]
    ir = IsotonicRegression(out_of_bounds="clip")
    ir.fit(proba, y_bin)
    cal = ir.predict(proba)
    fpr, tpr, thr = roc_curve(y_bin, cal)
    j = tpr - fpr
    best_idx = int(np.argmax(j))
    best_thr = float(thr[best_idx])
    pred = (cal >= best_thr).astype(int)
    draw_f1 = f1_score(y_bin, pred, zero_division=0)
    auc = roc_auc_score(y_bin, cal)

    clf.fit(X, y_bin)
    ir.fit(clf.predict_proba(X)[:, 1], y_bin)
    pkg = {"model": clf, "calibrator": ir, "threshold": best_thr,
           "feature_cols": FEATURE_NAMES, "task": task_name}
    return pkg, draw_f1, auc, best_thr


def binning_monotonic_check(y_true, y_cal, n_bins=5):
    """OU/AH 校准分箱单调性检查 (铁律: 完美单调)。返回 (monotonic, rows)。"""
    order = np.argsort(y_cal)
    y_cal_s = y_cal[order]; y_true_s = y_true[order]
    n = len(y_true_s); sz = n // n_bins
    rows = []
    rates = []
    for b in range(n_bins):
        s = b * sz; e = (b + 1) * sz if b < n_bins - 1 else n
        if e <= s:
            continue
        seg_true = y_true_s[s:e]
        seg_rate = float(seg_true.mean())
        rates.append(seg_rate)
        rows.append((b + 1, round(float(y_cal_s[s:e].mean()), 4),
                     round(seg_rate, 4), int(e - s)))
    mono = all(rates[i] <= rates[i + 1] + 1e-9 for i in range(len(rates) - 1))
    return mono, rows


# ---------------------------------------------------------------- 注册
def update_registry(metrics: dict):
    reg_path = os.path.join(SAVED, "model_registry.json")
    reg = {"active": "wc_v1", "current": None, "versions": []}
    if os.path.exists(reg_path):
        try:
            reg = json.load(open(reg_path, encoding="utf-8"))
        except Exception:
            pass
    reg.setdefault("versions", [])
    reg.setdefault("chains", {})
    entry = {
        "version": "league_v1",
        "chain": "league",
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
        "engine": "League Stacking(LGB+XGB->LR) + DrawExpert + OU/AH calibrated",
        "n_features": len(FEATURE_NAMES),
        "league_samples_1x2": metrics["n_1x2"],
        "league_samples_ou": metrics["n_ou"],
        "league_samples_ah": metrics["n_ah"],
        "excluded": metrics["excluded"],
        "metrics": {
            "1x2_acc": round(metrics["acc_1x2"], 4),
            "1x2_macro_f1": round(metrics["macro_f1_1x2"], 4),
            "1x2_auc": round(metrics["auc_1x2"], 4),
            "1x2_baseline_acc": round(metrics["base_acc_1x2"], 4),
            "1x2_acc_uplift": round(metrics["acc_1x2"] - metrics["base_acc_1x2"], 4),
            "draw_f1": round(metrics["draw_f1"], 4),
            "draw_auc": round(metrics["draw_auc"], 4),
            "ou_auc": round(metrics["ou_auc"], 4),
            "ou_base_rate": round(metrics["ou_base"], 4),
            "ou_monotonic": metrics["ou_mono"],
            "ah_auc": round(metrics["ah_auc"], 4),
            "ah_base_rate": round(metrics["ah_base"], 4),
        },
        "models": ["league_main_v1", "league_draw_expert",
                   "league_ou_v1", "league_ah_v1"],
    }
    # 不覆盖 wc 的 active; 仅追加 league 版本
    reg["chains"]["league"] = "league_v1"
    reg["chains"]["wc"] = reg.get("chains", {}).get("wc", "wc_v1")
    reg["versions"].append(entry)
    reg["league_current"] = entry
    json.dump(reg, open(reg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[registry] league_v1 已注册 (wc_v1 保留为 active, 两链路并列)")


# ---------------------------------------------------------------- main
def main():
    print("=" * 64)
    print("联赛链路训练管道启动 (镜像 wc_train_pipeline 纪律)")
    print("=" * 64)
    rows, skipped = load_league_rows()
    print(f"[data] 剔除: 世界杯={skipped['wc']} 友谊赛={skipped['friendly']} 其他={skipped['other']}")
    print(f"[data] 联赛链路可用样本: {len(rows)} 场 (特征维度={len(FEATURE_NAMES)})")

    metrics = {"excluded": dict(skipped)}

    # ---- 1X2 (H/D/A) ----
    print("\n" + "-" * 64)
    print("1X2 主模型 (Stacking LGB+XGB->LR, 3分类)")
    print("-" * 64)
    X1, y1 = build_Xy(rows, "1x2")
    metrics["n_1x2"] = len(y1)
    base_acc = float(np.bincount(y1).max() / len(y1))
    metrics["base_acc_1x2"] = base_acc
    pkg1, acc, macro_f1, y_oof_pred, oof_auc = train_stacking(X1, y1, 3, "1x2")
    metrics["acc_1x2"] = acc
    metrics["macro_f1_1x2"] = macro_f1
    metrics["auc_1x2"] = oof_auc
    print(f"[1x2] CV acc={acc:.4f} macroF1={macro_f1:.4f} AUC(macro-OVR)={oof_auc:.4f} | 朴素基线(多数类)={base_acc:.4f} | 增益{(acc-base_acc)*100:+.1f}pp")
    print(classification_report(y1, y_oof_pred, target_names=["H", "D", "A"], zero_division=0))
    joblib.dump(pkg1, os.path.join(DATA, "league_main_v1.joblib"))
    print("[save] league_main_v1.joblib")

    # ---- DrawExpert ----
    print("\n" + "-" * 64)
    print("DrawExpert (平局二分类 + Isotonic + YoudenJ)")
    print("-" * 64)
    de_pkg, draw_f1, draw_auc, thr = train_draw_expert(X1, y1)
    metrics["draw_f1"] = draw_f1
    metrics["draw_auc"] = draw_auc
    print(f"[draw] CV drawF1={draw_f1:.4f} AUC={draw_auc:.4f} YoudenJ阈值={thr:.3f}")
    joblib.dump(de_pkg, os.path.join(DATA, "league_draw_expert.joblib"))
    print("[save] league_draw_expert.joblib")

    # ---- OU ----
    print("\n" + "-" * 64)
    print("OU 大小球 (二分类 + Isotonic 校准)")
    print("-" * 64)
    Xo, yo = build_Xy(rows, "ou")
    metrics["n_ou"] = len(yo)
    ou_pkg, ou_auc, ou_base, ou_cal, ou_y = train_binary_calibrated(Xo, yo, "ou")
    metrics["ou_auc"] = ou_auc
    metrics["ou_base"] = ou_base
    mono, bins = binning_monotonic_check(ou_y, ou_cal, 5)
    metrics["ou_mono"] = bool(mono)
    print(f"[ou] CV AUC={ou_auc:.4f} | 基准率(Under)={ou_base:.4f} | 分箱单调={mono}")
    for b, p, r, n in bins:
        print(f"   分箱{b}: 预测概率均={p} 实际Under率={r} n={n}")
    joblib.dump(ou_pkg, os.path.join(DATA, "league_ou_v1.joblib"))
    print("[save] league_ou_v1.joblib")

    # ---- AH ----
    print("\n" + "-" * 64)
    print("AH 让球 (二分类 + Isotonic 校准)")
    print("-" * 64)
    Xa, ya = build_Xy(rows, "ah")
    metrics["n_ah"] = len(ya)
    if len(ya) < 50:
        print(f"[ah] 样本仅 {len(ya)} (历史退化), 仍训但指标仅供参考")
    ah_pkg, ah_auc, ah_base, ah_cal, ah_y = train_binary_calibrated(Xa, ya, "ah")
    metrics["ah_auc"] = ah_auc
    metrics["ah_base"] = ah_base
    mono_a, bins_a = binning_monotonic_check(ah_y, ah_cal, 5)
    print(f"[ah] CV AUC={ah_auc:.4f} | 基准率(Away)={ah_base:.4f} | 分箱单调={mono_a}")
    for b, p, r, n in bins_a:
        print(f"   分箱{b}: 预测概率均={p} 实际Away率={r} n={n}")
    joblib.dump(ah_pkg, os.path.join(DATA, "league_ah_v1.joblib"))
    print("[save] league_ah_v1.joblib")

    # ---- 注册 ----
    print("\n" + "=" * 64)
    update_registry(metrics)
    print("=" * 64)
    print("联赛链路训练完成。模型已落盘 data/league_*.joblib，注册 league_v1(chain=league)。")
    print(f"1X2 acc={metrics['acc_1x2']:.4f}(基线{base_acc:.4f}) | "
          f"drawF1={draw_f1:.4f} | OU AUC={ou_auc:.4f}(单调{mono}) | AH AUC={ah_auc:.4f}")


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
哨响AI · 世界杯2026 链路训练管道 (wc2026_train_pipeline)
========================================================
数据: 经规范化合并的 wc2026_merged.json
  - 来源 SSoT = football_data.db.wc_all_matches(edition=2026), 挂截图详细盘口
  - 队名已规范化(48 支 = 2026 世界杯 48 队赛制), 输出层已修正

特征: 由 1X2 临盘赔率派生的 ~11 维(无外部特征库, 因该表无 84 维特征)
  inv_h/d/a = 1/odds ; margin = sum(inv)-1 ; p_h/d/a = inv/sum(inv)
  + fav_prob / draw_prob / dog_prob / gap_hd / gap_ha / gap_da / log_odds_h / fav_dog_ratio

防泄漏: GroupKFold(按 对阵分组, 同两队永不在训练/测试两侧) -> 杜绝同对阵
        group+ko 重复行泄漏; 训练前再剔除 9 组完全重复行。

模型(镜像 league_train_pipeline 纪律):
  wc2026_main_v1    : 1X2 Stacking(LGB+XGB -> LR) 3分类 H/D/A
  wc2026_draw_expert: 平局二分类 + Isotonic + YoudenJ
  wc2026_ou_v1      : 大小球(over 2.5) 二分类 + Isotonic 校准 + 分箱单调检查
  (AH 暂缺: wc_all_matches 无一致 AH 盘口线/标签, 截图 AH 线变参, 留待后续)

评估: 5 折 GroupKFold + OOF 概率 + 并列 naive 基线(多数类 / 押最热方)
注册: model_registry.json 追加 wc2026_v1(chain=wc), **不改动生产 active**, 部署需单独激活。

用法:
  .venv/Scripts/python.exe scripts/wc2026_train_pipeline.py
"""
from __future__ import annotations
import os, sys, json, math, datetime
import numpy as np
import joblib
from sklearn.model_selection import cross_val_predict, GroupKFold
from sklearn.metrics import (accuracy_score, f1_score, classification_report,
                             roc_auc_score, roc_curve)
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
import lightgbm as lgb
import xgboost as xgb
import warnings
# 静音 LightGBM 冗余警告与 feature-name 提示(不影响训练结果)
warnings.filterwarnings("ignore", category=UserWarning)
lgb.basic.config_verbosity = 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SAVED = os.path.join(ROOT, "saved_models")
os.makedirs(SAVED, exist_ok=True)
MERGED = os.path.join(DATA, "wc2026_merged.json")

FEATURE_NAMES = [
    "p_h", "p_d", "p_a", "margin", "fav_prob", "draw_prob", "dog_prob",
    "gap_hd", "gap_ha", "gap_da", "log_odds_h", "fav_dog_ratio",
]


# ---------------------------------------------------------------- 数据
def build_features(oh, od, oa):
    inv_h, inv_d, inv_a = 1.0 / oh, 1.0 / od, 1.0 / oa
    s = inv_h + inv_d + inv_a
    margin = s - 1.0
    p_h, p_d, p_a = inv_h / s, inv_d / s, inv_a / s
    fav = max(p_h, p_d, p_a)
    dog = min(p_h, p_d, p_a)
    lo = min(oh, od, oa)
    hi = max(oh, od, oa)
    return [
        p_h, p_d, p_a, margin, fav, p_d, dog,
        p_h - p_d, p_h - p_a, p_d - p_a,
        math.log(oh), (lo / hi) if hi else 0.0,
    ]


def load():
    blob = json.load(open(MERGED, encoding="utf-8"))
    rows = blob["matches"]
    out = []
    seen = set()
    n_dup = 0
    for r in rows:
        if not r.get("has_result"):
            continue
        oh, od, oa = r.get("o_h"), r.get("o_d"), r.get("o_a")
        if oh is None or od is None or oa is None:
            continue
        hg, ag, fr = r.get("hg"), r.get("ag"), r.get("fr")
        if hg is None or ag is None or fr not in ("H", "D", "A"):
            continue
        key = (r["home"], r["away"], r.get("stage"), hg, ag, oh, od, oa)
        if key in seen:
            n_dup += 1
            continue
        seen.add(key)
        feat = build_features(float(oh), float(od), float(oa))
        out.append({
            "home": r["home"], "away": r["away"], "stage": r.get("stage"),
            "feat": feat, "hg": int(hg), "ag": int(ag), "fr": fr,
            "total": int(hg) + int(ag),
        })
    return out, n_dup


def ymaps(rows):
    y1 = np.array([{"H": 0, "D": 1, "A": 2}[r["fr"]] for r in rows])
    yd = np.array([1 if r["fr"] == "D" else 0 for r in rows])
    you = np.array([1 if r["total"] > 2.5 else 0 for r in rows])
    return y1, yd, you


# ---------------------------------------------------------------- 模型
def _base_lgb(n_classes):
    return lgb.LGBMClassifier(
        n_estimators=300, num_leaves=31, max_depth=4, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.7, min_child_samples=10,
        reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=-1,
        verbose=-1,
    )


def _base_xgb(n_classes):
    return xgb.XGBClassifier(
        n_estimators=300, max_depth=4, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.7, min_child_weight=10,
        reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=-1,
        eval_metric="mlogloss" if n_classes > 2 else "logloss",
    )


def train_stacking(X, y, groups, n_classes, task_name):
    gkf = GroupKFold(5)
    print(f"  [{task_name}] 生成 LGB OOF...")
    lgb_oof = cross_val_predict(_base_lgb(n_classes), X, y, cv=gkf,
                                groups=groups, method="predict_proba")
    print(f"  [{task_name}] 生成 XGB OOF...")
    xgb_oof = cross_val_predict(_base_xgb(n_classes), X, y, cv=gkf,
                                groups=groups, method="predict_proba")
    meta_X = np.hstack([lgb_oof, xgb_oof])
    meta = LogisticRegression(max_iter=2000, C=1.0)
    y_oof_pred = cross_val_predict(meta, meta_X, y, cv=gkf,
                                   groups=groups, method="predict")
    meta_oof_proba = cross_val_predict(meta, meta_X, y, cv=gkf,
                                       groups=groups, method="predict_proba")
    acc = accuracy_score(y, y_oof_pred)
    macro_f1 = f1_score(y, y_oof_pred, average="macro", zero_division=0)
    oof_auc = roc_auc_score(y, meta_oof_proba, multi_class="ovr", average="macro")

    lgb_m = _base_lgb(n_classes).fit(X, y)
    xgb_m = _base_xgb(n_classes).fit(X, y)
    meta.fit(np.hstack([lgb_m.predict_proba(X), xgb_m.predict_proba(X)]), y)
    pkg = {"lgb": lgb_m, "xgb": xgb_m, "meta": meta,
           "feature_cols": FEATURE_NAMES, "n_classes": n_classes, "task": task_name}
    return pkg, acc, macro_f1, y_oof_pred, oof_auc


def train_binary_calibrated(X, y, groups, task_name):
    gkf = GroupKFold(5)
    print(f"  [{task_name}] 生成 LGB OOF...")
    lgb_oof = cross_val_predict(_base_lgb(2), X, y, cv=gkf,
                                groups=groups, method="predict_proba")[:, 1]
    print(f"  [{task_name}] 生成 XGB OOF...")
    xgb_oof = cross_val_predict(_base_xgb(2), X, y, cv=gkf,
                                groups=groups, method="predict_proba")[:, 1]
    meta_X = np.vstack([lgb_oof, xgb_oof]).T
    meta = LogisticRegression(max_iter=2000, C=1.0)
    oof_cal_in = cross_val_predict(meta, meta_X, y, cv=gkf,
                                   groups=groups, method="predict_proba")[:, 1]
    ir = IsotonicRegression(out_of_bounds="clip")
    ir.fit(oof_cal_in, y)
    oof_cal = ir.predict(oof_cal_in)
    auc = roc_auc_score(y, oof_cal)
    base_rate = float((y == 1).mean())

    lgb_m = _base_lgb(2).fit(X, y)
    xgb_m = _base_xgb(2).fit(X, y)
    meta.fit(np.vstack([lgb_m.predict_proba(X)[:, 1],
                        xgb_m.predict_proba(X)[:, 1]]).T, y)
    ir.fit(meta.predict_proba(np.vstack([lgb_m.predict_proba(X)[:, 1],
                                         xgb_m.predict_proba(X)[:, 1]]).T)[:, 1], y)
    pkg = {"lgb": lgb_m, "xgb": xgb_m, "meta": meta, "calibrator": ir,
           "feature_cols": FEATURE_NAMES, "task": task_name}
    return pkg, auc, base_rate, oof_cal, y


def train_draw_expert(X, y_1x2, groups):
    y_bin = (y_1x2 == 1).astype(int)
    pos = int(y_bin.sum()); neg = len(y_bin) - pos
    spw = neg / pos if pos else 1.0
    gkf = GroupKFold(5)
    clf = lgb.LGBMClassifier(
        n_estimators=300, num_leaves=31, max_depth=4, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.7, min_child_samples=10,
        scale_pos_weight=spw, reg_alpha=0.5, reg_lambda=2.0,
        random_state=42, n_jobs=-1,
    )
    proba = cross_val_predict(clf, X, y_bin, cv=gkf,
                              groups=groups, method="predict_proba")[:, 1]
    ir = IsotonicRegression(out_of_bounds="clip")
    ir.fit(proba, y_bin)
    cal = ir.predict(proba)
    fpr, tpr, thr = roc_curve(y_bin, cal)
    best_idx = int(np.argmax(tpr - fpr))
    best_thr = float(thr[best_idx])
    pred = (cal >= best_thr).astype(int)
    draw_f1 = f1_score(y_bin, pred, zero_division=0)
    auc = roc_auc_score(y_bin, cal)
    clf.fit(X, y_bin)
    ir.fit(clf.predict_proba(X)[:, 1], y_bin)
    pkg = {"model": clf, "calibrator": ir, "threshold": best_thr,
           "feature_cols": FEATURE_NAMES, "task": "draw"}
    return pkg, draw_f1, auc, best_thr


def binning_monotonic(y_true, y_cal, n_bins=5):
    order = np.argsort(y_cal)
    y_cal_s = y_cal[order]; y_true_s = y_true[order]
    n = len(y_true_s); sz = n // n_bins
    rates, rows = [], []
    for b in range(n_bins):
        s = b * sz; e = (b + 1) * sz if b < n_bins - 1 else n
        if e <= s:
            continue
        seg_rate = float(y_true_s[s:e].mean())
        rates.append(seg_rate)
        rows.append((b + 1, round(float(y_cal_s[s:e].mean()), 4),
                     round(seg_rate, 4), int(e - s)))
    mono = all(rates[i] <= rates[i + 1] + 1e-9 for i in range(len(rates) - 1))
    return mono, rows


# ---------------------------------------------------------------- 注册
def update_registry(metrics):
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
        "version": "wc2026_v1",
        "chain": "wc",
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
        "engine": "WC2026 Stacking(LGB+XGB->LR) + DrawExpert + OU(over2.5) calibrated",
        "n_features": len(FEATURE_NAMES),
        "wc2026_samples": metrics["n"],
        "data_source": "football_data.db.wc_all_matches(edition=2026) + 截图盘口",
        "name_canonical": "48 队 (2026 世界杯 48 队赛制)",
        "metrics": {
            "1x2_acc": round(metrics["acc_1x2"], 4),
            "1x2_macro_f1": round(metrics["macro_f1_1x2"], 4),
            "1x2_auc": round(metrics["auc_1x2"], 4),
            "1x2_baseline_majority": round(metrics["base_maj"], 4),
            "1x2_baseline_favorite": round(metrics["base_fav"], 4),
            "1x2_acc_uplift_vs_favorite": round(metrics["acc_1x2"] - metrics["base_fav"], 4),
            "draw_f1": round(metrics["draw_f1"], 4),
            "draw_auc": round(metrics["draw_auc"], 4),
            "ou_auc": round(metrics["ou_auc"], 4),
            "ou_base_rate": round(metrics["ou_base"], 4),
            "ou_monotonic": metrics["ou_mono"],
        },
        "models": ["wc2026_main_v1", "wc2026_draw_expert", "wc2026_ou_v1"],
        "note": "AH 暂未训: 数据集无一致 AH 盘口线/标签",
    }
    # 同版本覆盖(幂等), 避免重跑产生重复条目
    reg["versions"] = [v for v in reg["versions"] if v.get("version") != entry["version"]]
    reg["versions"].append(entry)
    reg["wc2026_current"] = entry
    # 不动全局 active / chains.wc(保持生产 wc_v1), 部署需单独激活
    json.dump(reg, open(reg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[registry] 已追加 wc2026_v1 (未改动 active={reg.get('active')})")


# ---------------------------------------------------------------- main
def main():
    print("=" * 64)
    print("世界杯2026 链路训练管道启动 (基于 136 场规范化合并数据)")
    print("=" * 64)
    rows, n_dup = load()
    print(f"[data] 完全重复行剔除: {n_dup}")
    print(f"[data] 可用训练样本(有赛果+有赔率): {len(rows)} 场")
    y1, yd, you = ymaps(rows)
    X = np.array([r["feat"] for r in rows], dtype=float)
    groups = np.array(["|".join(sorted((r["home"], r["away"]))) for r in rows])

    metrics = {"n": len(rows), "dup_removed": n_dup}
    print(f"[data] 标签分布: H={(y1==0).sum()} D={(y1==1).sum()} A={(y1==2).sum()} | "
          f"平局率={(y1==1).mean():.1%} | over2.5率={(you==1).mean():.1%}")
    print(f"[data] 对阵分组数(GroupKFold): {len(set(groups))}")

    # naive 基线
    inv = X[:, :3]  # p_h,p_d,p_a (隐含概率)
    fav_pred = np.argmax(inv, axis=1)  # 最热方 = 隐含概率最高 = 赔率最低
    base_fav = accuracy_score(y1, fav_pred)
    base_maj = float(np.bincount(y1).max() / len(y1))
    metrics["base_fav"] = base_fav
    metrics["base_maj"] = base_maj
    print(f"[baseline] 押最热方 acc={base_fav:.4f} | 多数类 acc={base_maj:.4f}")

    # ---- 1X2 ----
    print("\n" + "-" * 64)
    print("1X2 主模型 (Stacking LGB+XGB->LR, GroupKFold 防泄漏)")
    print("-" * 64)
    pkg1, acc, macro_f1, y_oof_pred, oof_auc = train_stacking(X, y1, groups, 3, "1x2")
    metrics["acc_1x2"] = acc
    metrics["macro_f1_1x2"] = macro_f1
    metrics["auc_1x2"] = oof_auc
    print(f"[1x2] CV acc={acc:.4f} macroF1={macro_f1:.4f} AUC(ovr)={oof_auc:.4f} | "
          f"押最热方基线={base_fav:.4f} | 增益{(acc-base_fav)*100:+.1f}pp")
    print(classification_report(y1, y_oof_pred, target_names=["H", "D", "A"], zero_division=0))
    joblib.dump(pkg1, os.path.join(DATA, "wc2026_main_v1.joblib"))
    print("[save] wc2026_main_v1.joblib")

    # ---- DrawExpert ----
    print("\n" + "-" * 64)
    print("DrawExpert (平局二分类 + Isotonic + YoudenJ)")
    print("-" * 64)
    de_pkg, draw_f1, draw_auc, thr = train_draw_expert(X, y1, groups)
    metrics["draw_f1"] = draw_f1
    metrics["draw_auc"] = draw_auc
    print(f"[draw] CV drawF1={draw_f1:.4f} AUC={draw_auc:.4f} YoudenJ阈值={thr:.3f}")
    joblib.dump(de_pkg, os.path.join(DATA, "wc2026_draw_expert.joblib"))
    print("[save] wc2026_draw_expert.joblib")

    # ---- OU (over 2.5) ----
    print("\n" + "-" * 64)
    print("OU 大小球 (over 2.5 二分类 + Isotonic 校准)")
    print("-" * 64)
    ou_pkg, ou_auc, ou_base, ou_cal, ou_y = train_binary_calibrated(X, you, groups, "ou")
    metrics["ou_auc"] = ou_auc
    metrics["ou_base"] = ou_base
    mono, bins = binning_monotonic_check = binning_monotonic(ou_y, ou_cal, 5)
    metrics["ou_mono"] = bool(mono)
    print(f"[ou] CV AUC={ou_auc:.4f} | over2.5基准率={ou_base:.4f} | 分箱单调={mono}")
    for b, p, r, n in bins:
        print(f"   分箱{b}: 预测概率均={p} 实际over率={r} n={n}")
    joblib.dump(ou_pkg, os.path.join(DATA, "wc2026_ou_v1.joblib"))
    print("[save] wc2026_ou_v1.joblib")

    # ---- AH: 暂缺 ----
    print("\n[AH] 本轮未训: wc_all_matches 无一致 AH 盘口线/标签, 截图 AH 线变参, 留待后续。")

    update_registry(metrics)
    print("\n" + "=" * 64)
    print(f"世界杯2026 链路训练完成 (wc2026_v1)。")
    print(f"1X2 acc={metrics['acc_1x2']:.4f}(押最热方基线{base_fav:.4f}, "
          f"增益{(metrics['acc_1x2']-base_fav)*100:+.1f}pp) | "
          f"drawF1={draw_f1:.4f} | OU AUC={ou_auc:.4f}(单调{mono})")
    print("模型落盘: data/wc2026_*.joblib | 注册: wc2026_v1 (未改动生产 active)")


if __name__ == "__main__":
    raise SystemExit(main())

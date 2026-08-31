# -*- coding: utf-8 -*-
"""
train_independent_model_v2_plate.py — 扩展最强单模型 (Phase E, 2026-08-30)

基线: independent_model.joblib (LightGBM 3类 + DrawExpert, OOF 宏AUC 0.6807,
      特征 = 17 独立 + 7 收盘赔率衍生, 仅用收盘赔率)。

扩展点 (本次新增, 全来自 开盘+收盘 赔率, 分析期可获取):
  1. 开盘赔率衍生特征 (_devig/overround/lambda/dc_draw/entropy/margin 共 7 维)
     + 原始开盘赔率 3 维 + 开盘热门方赔率 fav_open 1 维
  2. 盘口类 one-hot: 热门盘(开盘热门≤1.80)/均衡盘(1.80-2.50]/冷门盘(>2.50)
  3. 漂移方向 one-hot: 优盘(热门相对压低≤-2%)/逆盘(≥+2%)/稳盘
     + 各方向相对漂移 drift_h/d/a 3 维 + 热门方相对漂移 fav_drift 1 维

公平对比: 同数据(matching join)、同时序拆分(train<2023/OOF>=2023)、同算法超参。
  - 载入生产 independent_model.joblib 在同一 OOF 样本打分 作为基线对照
  - 训练 v2 (base 24 + 新增 ~20 维)
  - 报告 Acc / 宏AUC / LogLoss / Brier + McNemar(v2 vs 生产)

不覆盖生产模型; 仅产出 v2 候选 joblib 供人工审批 (IR-09/IR-15)。

运行: D:/Architecture/.venv/Scripts/python.exe scripts/train_independent_model_v2_plate.py
"""
from __future__ import annotations
import sqlite3, os, json, sys, math
import numpy as np
import pandas as pd
import joblib
from sklearn.metrics import log_loss, roc_auc_score, accuracy_score
from sklearn.calibration import CalibratedClassifierCV
import lightgbm as lgb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import train_independent_model as base  # 复用 odds_extra/_dc_inv/combine/multiclass_brier/mcnemar

DB = os.path.join(ROOT, "data", "football_data.db")
PROD_MODEL = os.path.join(ROOT, "pipeline", "predictors", "saved_models", "independent_model.joblib")
OUT_DIR = os.path.join(ROOT, "pipeline", "predictors", "saved_models")
os.makedirs(OUT_DIR, exist_ok=True)
V2_PATH = os.path.join(OUT_DIR, "independent_model_v2_plate.joblib")

REL_TH = 0.02
DRAW_WEIGHT = 2.2

# ── 新增特征列名 ────────────────────────────────────────────────
OPEN_MAP = {  # odds_extra 输出键 -> 新前缀 op_
    "odds_close_h": "op_odds_h", "odds_close_d": "op_odds_d", "odds_close_a": "op_odds_a",
    "devig_h": "op_devig_h", "devig_d": "op_devig_d", "devig_a": "op_devig_a",
    "overround": "op_overround", "lambda_home": "op_lambda_home", "lambda_away": "op_lambda_away",
    "dc_draw": "op_dc_draw", "draw_dev": "op_draw_dev", "entropy": "op_entropy",
    "margin_impl": "op_margin_impl",
}
PLATE_FEATS = ["plate_hot", "plate_bal", "plate_cold"]
DRIFT_FEATS = ["drift_h", "drift_d", "drift_a", "fav_drift",
               "drift_smart", "drift_reverse", "drift_stable"]
NEW_FEATS = list(OPEN_MAP.values()) + ["fav_open"] + PLATE_FEATS + DRIFT_FEATS
FEATURES_V2 = base.FEATURES + NEW_FEATS


def build_alias_map(cur):
    amap = {}
    for canon, aj in cur.execute("SELECT canonical, aliases_json FROM team_canonical"):
        amap[canon.strip().lower()] = canon
        if aj:
            try:
                lst = json.loads(aj) if aj.strip().startswith("[") else ast.literal_eval(aj)
                for a in lst:
                    amap[str(a).strip().lower()] = canon
            except Exception:
                pass
    return amap


import ast  # for alias parsing


def new_features(oh, od, oa, ch, cd, ca):
    """oh/od/oa=开盘; ch/cd/ca=收盘。返回新增特征 dict。"""
    # 开盘衍生
    op_extra = base.odds_extra(oh, od, oa)
    d = {NEW: op_extra[OLD] for OLD, NEW in OPEN_MAP.items()}
    # 开盘热门方
    op = np.array([oh, od, oa])
    fav = int(np.argmin(op))
    fav_open = float(op[fav])
    d["fav_open"] = fav_open
    # 盘口类
    if fav_open <= 1.80:
        ph, pb, pc = 1, 0, 0
    elif fav_open <= 2.50:
        ph, pb, pc = 0, 1, 0
    else:
        ph, pb, pc = 0, 0, 1
    d["plate_hot"], d["plate_bal"], d["plate_cold"] = ph, pb, pc
    # 漂移 (收盘-开盘)/开盘 各方向
    cl = np.array([ch, cd, ca])
    drift = (cl - op) / op
    d["drift_h"], d["drift_d"], d["drift_a"] = float(drift[0]), float(drift[1]), float(drift[2])
    fav_drift = float(drift[fav])
    d["fav_drift"] = fav_drift
    if fav_drift <= -REL_TH:
        ds, dr, dst = 1, 0, 0
    elif fav_drift >= REL_TH:
        ds, dr, dst = 0, 1, 0
    else:
        ds, dr, dst = 0, 0, 1
    d["drift_smart"], d["drift_reverse"], d["drift_stable"] = ds, dr, dst
    return d


def main():
    print("=== [1/8] 加载数据 (独立特征 + 开盘/收盘赔率) ===")
    conn = sqlite3.connect(DB)
    amap = build_alias_map(conn)
    dfi = pd.read_sql_query("SELECT * FROM indep_features", conn)

    def canon(t):
        t = str(t).strip().lower()
        return amap.get(t, t)

    indep_idx = {}
    for _, r in dfi.iterrows():
        indep_idx[(r["home"], r["away"], str(r["match_date"])[:10])] = r

    df = pd.read_sql_query(
        "SELECT m.home_team_name, m.away_team_name, m.match_date, m.final_result, "
        "mf.odds_close_h, mf.odds_close_d, mf.odds_close_a, "
        "mf.odds_open_h, mf.odds_open_d, mf.odds_open_a "
        "FROM matches m JOIN match_features mf ON m.match_id=mf.match_id "
        "WHERE m.final_result IN ('H','D','A') "
        "AND mf.odds_close_h>0 AND mf.odds_close_d>0 AND mf.odds_close_a>0 "
        "AND mf.odds_open_h>0 AND mf.odds_open_d>0 AND mf.odds_open_a>0", conn)
    conn.close()
    print(f"  有开盘+收盘赔率样本: {len(df)}")

    rows = []
    for _, r in df.iterrows():
        ch, ca = canon(r["home_team_name"]), canon(r["away_team_name"])
        key = (ch, ca, str(r["match_date"])[:10])
        if key not in indep_idx:
            continue
        ir = indep_idx[key]
        oh, od, oa = float(r["odds_open_h"]), float(r["odds_open_d"]), float(r["odds_open_a"])
        ch2, cd2, ca2 = float(r["odds_close_h"]), float(r["odds_close_d"]), float(r["odds_close_a"])
        feat = {c: float(ir[c]) for c in base.INDEP_FEATS}
        feat.update(base.odds_extra(ch2, cd2, ca2))      # 7 收盘衍生 (base.FEATURES 内含)
        feat.update(new_features(oh, od, oa, ch2, cd2, ca2))  # 新增 ~20 维
        rows.append((feat, {"H": 0, "D": 1, "A": 2}[r["final_result"]], str(r["match_date"])[:10]))
    print(f"  成功对齐(独立特征+开盘+收盘): {len(rows)}")

    X = np.array([[r[0][c] for c in FEATURES_V2] for r in rows], dtype=np.float64)
    y = np.array([r[1] for r in rows])
    dates = np.array([r[2] for r in rows])
    print(f"  特征维度: {X.shape[1]} (base {len(base.FEATURES)} + 新增 {len(NEW_FEATS)})")

    print("=== [2/8] 时序拆分 (train<2023 / OOF>=2023) ===")
    train_mask = dates < "2023-01-01"
    test_mask = dates >= "2023-01-01"
    Xtr, ytr = X[train_mask], y[train_mask]
    Xte, yte = X[test_mask], y[test_mask]
    print(f"  训练: {int(train_mask.sum())} | OOF: {int(test_mask.sum())}")

    print("=== [3/8] 训练 v2 主模型 + Platt ===")
    main_est = lgb.LGBMClassifier(objective="multiclass", num_class=3, n_estimators=1000,
                                  learning_rate=0.01, num_leaves=63, min_child_samples=60,
                                  subsample=0.9, colsample_bytree=0.9, reg_lambda=3.0, reg_alpha=0.2,
                                  class_weight={0: 1.0, 1: DRAW_WEIGHT, 2: 1.0},
                                  random_state=42, n_jobs=-1, verbose=-1)
    main_cal = CalibratedClassifierCV(main_est, method="sigmoid", cv=5)
    main_cal.fit(Xtr, ytr)

    print("=== [4/8] 训练 v2 DrawExpert + Platt ===")
    y_draw = (y == 1).astype(int)
    draw_est = lgb.LGBMClassifier(objective="binary", n_estimators=1000,
                                 learning_rate=0.01, num_leaves=63, min_child_samples=60,
                                 subsample=0.9, colsample_bytree=0.9, reg_lambda=3.0, reg_alpha=0.2,
                                 class_weight="balanced", random_state=42, n_jobs=-1, verbose=-1)
    draw_cal = CalibratedClassifierCV(draw_est, method="sigmoid", cv=5)
    draw_cal.fit(Xtr, y_draw[train_mask])

    def evaluate(name, p, yy):
        ll = log_loss(yy, p)
        auc = roc_auc_score(yy, p, multi_class="ovr", average="macro")
        acc = accuracy_score(yy, p.argmax(1))
        br = base.multiclass_brier(p, yy)
        pred = p.argmax(1)
        rec = [float(np.mean(pred[yy == k] == k)) if np.sum(yy == k) > 0 else 0.0 for k in (0, 1, 2)]
        print(f"  [{name}] Acc={acc:.4f} 宏AUC={auc:.4f} LogLoss={ll:.4f} Brier={br:.4f} "
              f"recall H/D/A={rec[0]:.3f}/{rec[1]:.3f}/{rec[2]:.3f}")
        return dict(acc=float(acc), auc=float(auc), ll=float(ll), brier=float(br),
                    rec_h=rec[0], rec_d=rec[1], rec_a=rec[2])

    print("=== [5/8] v2 OOF 评估 ===")
    v2_proba = base.combine(main_cal.predict_proba(Xte), draw_cal.predict_proba(Xte))
    v2_m = evaluate("V2(扩展)", v2_proba, yte)

    # ── 生产模型同样本基线 ──────────────────────────────────────
    print("=== [6/8] 载入生产 independent_model.joblib 同 OOF 样本打分 ===")
    prod = joblib.load(PROD_MODEL)
    base_feats = prod["feat_cols"]
    Xte_base = np.array([[r[0][c] for c in base_feats] for r in rows])[test_mask]
    prod_main = prod["model_main"].predict_proba(Xte_base)
    prod_draw = prod["model_draw"].predict_proba(Xte_base)
    prod_proba = base.combine(prod_main, prod_draw)
    prod_m = evaluate("生产(基线)", prod_proba, yte)

    print("=== [7/8] McNemar (V2 vs 生产) ===")
    v2_fav = v2_proba.argmax(1)
    prod_fav = prod_proba.argmax(1)
    mc = v2_fav == yte
    mk = prod_fav == yte
    stat, p, n10, n01 = base.mcnemar(mc, mk)
    print(f"  McNemar: chi2={stat:.3f} p={p:.4f} (V2对生产对={n10}, V2错生产对={n01})")

    print("=== [8/8] 保存 v2 候选 ===")
    meta = {
        "model_main": main_cal, "model_draw": draw_cal,
        "feat_cols": FEATURES_V2, "indep_feats": base.INDEP_FEATS,
        "odds_feats": base.ODDS_FEATS, "new_feats": NEW_FEATS,
        "draw_weight": DRAW_WEIGHT, "version": "independent_v2_plate",
        "trained_on": "独立17 + 收盘衍生7 + 开盘衍生+盘口类+漂移(新增~20), 时序 train<2023/OOF>=2023",
        "oof_metrics": v2_m, "baseline_metrics": prod_m,
        "combines": "P(D)=DrawExpert; P(H/A) 主模型归一",
    }
    joblib.dump(meta, V2_PATH)
    print(f"  已保存候选: {V2_PATH}")

    da = (v2_m["auc"] - prod_m["auc"]) / prod_m["auc"] * 100
    dll = (prod_m["ll"] - v2_m["ll"]) / prod_m["ll"] * 100
    dacc = (v2_m["acc"] - prod_m["acc"]) * 100
    print(f"\n  V2 vs 生产: 宏AUC {da:+.2f}%  LogLoss {dll:+.2f}%(越低越好)  Acc {dacc:+.2f}pp")
    verdict = "✅ V2 优于生产, 可审批上线" if (v2_m["auc"] > prod_m["auc"] and dll > 0) else \
              "⚠️ V2 未严格优于生产, 维持生产模型"
    print(f"  结论: {verdict}")
    return dict(v2=v2_m, prod=prod_m, mcnemar=(stat, p, n10, n01))


if __name__ == "__main__":
    main()

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
train_historical_1x2.py — 在历史库 odds_features(312K 真实行) 上现训 1X2 三分类模型
====================================================================================
目的: 作为 historical_comp_system.py 的"第三层模型信号", 替代在历史上不可运行的
      wc_main_v1(WC专用/115样本/77维实时特征缺失)。

特征(全部为 odds_features 表中每行真实存在的值, 零伪造):
  imp_h, imp_d, imp_a       去水隐含概率(核心预测因子)
  drift_h, drift_d, drift_a 开盘->收盘 漂移
  overround, home_edge       盘口形状
  sigma_trap                 陷阱指数
标签: outcome H/D/A (NULL 弃用)

评估纪律(遵循项目铁律: 重复CV + OOS AUC + 分箱校准 + naive基线 + 押热门ROI):
  - OOS AUC: cross_val_predict(5x5 重复分层) 全样本 OOS 概率算单一 AUC, 无泄漏
  - naive 基线: 永远押庄家隐含 argmax(imp)
  - 校准: 按模型 P(热门) 十分位 vs 实际热门命中率
  - 押热门 ROI: 每场以收盘赔率押隐含热门, 期望应≈破平(诚实, 不声称击败庄家)
"""
import sqlite3, warnings, json, datetime, sys, os
import numpy as np
from sklearn.model_selection import RepeatedStratifiedKFold
from sklearn.base import clone
from sklearn.metrics import roc_auc_score, accuracy_score
import lightgbm as lgb

# 确保 analysis 包可导入(报告 P2#3 RPS 序数评估)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analysis.scoring_metrics import rps_ordinal

warnings.filterwarnings('ignore')
DB = 'D:/Architecture/data/football_data.db'
OUT = 'D:/Architecture/data/historical_1x2_model.joblib'

FEATURES = ['imp_h', 'imp_d', 'imp_a',
            'drift_h', 'drift_d', 'drift_a',
            'overround', 'home_edge', 'sigma_trap']
LABEL_MAP = {'H': 0, 'D': 1, 'A': 2}
LAB = ['H', 'D', 'A']


def load():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cols = ['outcome'] + FEATURES + ['close_h', 'close_d', 'close_a']
    q = "SELECT {} FROM odds_features WHERE outcome IN ('H','D','A')".format(','.join(cols))
    cur.execute(q)
    rows = cur.fetchall()
    con.close()
    import pandas as pd
    df = pd.DataFrame(rows, columns=cols)
    # 丢弃任何特征缺失行
    before = len(df)
    df = df.dropna(subset=FEATURES + ['outcome'])
    df = df[(df[['imp_h', 'imp_d', 'imp_a']] > 0).all(axis=1)]
    print(f"[load] rows raw={before} -> clean={len(df)} (dropped NULL/zero={before-len(df)})")
    X = df[FEATURES].astype(float).to_numpy()
    y = df['outcome'].map(LABEL_MAP).astype(int).to_numpy()
    close = df[['close_h', 'close_d', 'close_a']].astype(float).to_numpy()
    return X, y, close


def main():
    X, y, close = load()
    n = len(y)
    print(f"[data] n={n}  dist H/D/A = {np.bincount(y)}")

    # ---- OOS 预测 (5x5 重复分层, 手动累加全样本无泄漏) ----
    skf = RepeatedStratifiedKFold(n_splits=5, n_repeats=5, random_state=42)
    model = lgb.LGBMClassifier(
        n_estimators=400, learning_rate=0.05, num_leaves=31,
        min_child_samples=200, subsample=0.8, colsample_bytree=0.8,
        reg_lambda=1.0, random_state=42, n_jobs=-1, verbose=-1)
    print("[cv] manual repeated CV 5x5 (25 folds) ...")
    proba = np.zeros((n, 3))
    cnt = np.zeros(n)
    for i, (tr, te) in enumerate(skf.split(X, y), 1):
        m = clone(model)
        m.fit(X[tr], y[tr])
        p = m.predict_proba(X[te])
        proba[te] += p
        cnt[te] += 1
    proba /= cnt[:, None]
    oos_pred = proba.argmax(1)
    auc = roc_auc_score(y, proba, multi_class='ovo')
    acc = accuracy_score(y, oos_pred)
    print(f"[cv] OOS AUC(ovo) = {auc:.4f}   OOS acc = {acc:.4f}")

    # ---- naive 基线: 永远押庄家隐含 argmax(imp) ----
    imp = X[:, :3]
    naive_pred = imp.argmax(1)
    naive_acc = accuracy_score(y, naive_pred)
    naive_auc = roc_auc_score(y, imp, multi_class='ovo')
    print(f"[naive] argmax(imp) acc = {naive_acc:.4f}  AUC = {naive_auc:.4f}")

    # ---- RPS (序数评估, 替代纯准确率; 惩罚高置信但错误, 越低越好) ----
    rps_model = rps_ordinal(y, proba)
    rps_naive = rps_ordinal(y, imp)
    print(f"[rps] model RPS = {rps_model:.4f}  naive(argmax imp) RPS = {rps_naive:.4f}  (越低越好)")

    # ---- 押热门 ROI (收盘赔率押隐含热门, 诚实校准) ----
    fav = naive_pred
    odds_fav = close[np.arange(n), fav]
    win = (y == fav).astype(float)
    roi = float((win * odds_fav - 1).mean())
    print(f"[roi] 押隐含热门 ROI = {roi:+.4f}  (期望应≈破平, 即 -(vig))")

    # ---- 校准: 按模型 P(热门) 十分位 vs 实际热门命中 ----
    p_fav = proba[np.arange(n), oos_pred]
    fav_actual = (y == oos_pred).astype(float)
    bins = np.linspace(0, 1, 11)
    calib = []
    for i in range(10):
        lo, hi = bins[i], bins[i + 1]
        m = (p_fav >= lo) & (p_fav < hi if i < 9 else p_fav <= hi)
        if m.sum() > 0:
            calib.append(dict(bin=f"{lo:.1f}-{hi:.1f}", n=int(m.sum()),
                              pred=float(p_fav[m].mean()),
                              actual=float(fav_actual[m].mean())))
    for c in calib:
        print(f"   bin {c['bin']:<7s} n={c['n']:>6d} pred={c['pred']:.3f} actual={c['actual']:.3f}")

    # ---- 全量训练最终模型 ----
    print("[fit] training final model on full data ...")
    model.fit(X, y)

    meta = dict(
        features=FEATURES, label_map=LABEL_MAP,
        n=int(n), oos_auc=float(auc), oos_acc=float(acc),
        naive_acc=float(naive_acc), naive_auc=float(naive_auc),
        roi_favorite=roi, calib=calib,
        trained_at=datetime.datetime.now().isoformat(timespec='seconds'),
        note="在历史 odds_features 上现训, 替代不可用的 wc_main_v1(WC专用/77维实时特征缺失)")
    import joblib
    joblib.dump(dict(model=model, meta=meta), OUT)
    print(f"[save] -> {OUT}")
    print(json.dumps({k: v for k, v in meta.items() if k != 'calib'}, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()

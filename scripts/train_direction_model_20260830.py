"""方向模型训练 (2026-08-30) — 用 football_data.db 32 万场把方向准确率拉满。

数据源: football_data.db `odds_features` 322,972 场, 关键字段 100% 非空,
        跨度 2012-08-25 ~ 2026-07-12, 标签 outcome ∈ {H, D, A}。

方法:
  特征 = 市场去水概率(cimp) + 原始赔率 + 漂移 + 抽水 + 联赛历史统计(K收缩)
  模型 = XGBoost 多分类 (softmax)
  评估 = **时间外**切分, 并与 naive baseline(去水概率 argmax) 对比

⚠ 反泄漏: 联赛历史统计只用**训练期**数据聚合; 测试期沿用训练期的联赛表。

用法:
  runpy scripts/train_direction_model_20260830.py [--model xgb|lgb] [--test-year 2025]
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "football_data.db")
OUT_DIR = os.path.join(ROOT, "models")
OUT_PATH = os.path.join(OUT_DIR, "direction_model_20260830.joblib")

MODEL = "xgb"
for a in sys.argv[1:]:
    if a.startswith("--model="):
        MODEL = a.split("=", 1)[1]
    elif a.startswith("--test-year="):
        TEST_YEAR = int(a.split("=", 1)[1])
TEST_YEAR = int(os.environ.get("TEST_YEAR", "2025"))
LEAGUE_K = 50.0          # 联赛统计贝叶斯收缩系数 (IR-19 FLB 同口径)

COLS = ("match_date, league, home_team, away_team, home_score, away_score, outcome, "
        "open_h, open_d, open_a, close_h, close_d, close_a, "
        "drift_h, drift_d, drift_a, imp_h, imp_d, imp_a, "
        "cimp_h, cimp_d, cimp_a, overround, home_edge, sigma_trap")


def quality_filter(df: pd.DataFrame) -> pd.DataFrame:
    """按年剔除坏数据 (2026-08-30 实测必需)。

    实测 odds_features 的 **2026 年数据完全损坏**:
      20,072 场中平局率 **0.0%**(真实应 25%), 比分非空率 **0.0%**,
      outcome 只剩 H/A 两值(主胜 65.9% 虚高)。
    若不剔除, 这批垃圾会污染训练并让测试集指标失真
    (实测未剔除时"测试准确率 89.59%" 是假象 —— 平局全丢了)。

    判据: 比分非空率 < 50% 或 平局率 < 10% 或 > 40% 或 样本 < 200 → 该年整体剔除。
    """
    q = df.groupby("year").agg(
        n=("y", "size"),
        sc=("home_score", lambda s: float(s.notna().mean())),
        dr=("y", lambda s: float((s == 1).mean())),
    )
    bad = q[(q["sc"] < 0.5) | (q["dr"] < 0.10) | (q["dr"] > 0.40) | (q["n"] < 200)].index.tolist()
    if bad:
        print(f"  [数据质量] 剔除年份 {sorted(int(x) for x in bad)}: "
              + "; ".join(f"{int(y)}(n={int(q.loc[y,'n'])},比分{int(q.loc[y,'sc']*100)}%,"
                          f"平{int(q.loc[y,'dr']*100)}%)" for y in sorted(bad)))
    return df[~df["year"].isin(bad)].copy()


def load() -> pd.DataFrame:
    con = sqlite3.connect(SRC, timeout=120)
    df = pd.read_sql_query(f"SELECT {COLS} FROM odds_features", con)
    con.close()
    df = df[df["outcome"].isin(("H", "D", "A"))].copy()
    df["match_date"] = pd.to_datetime(df["match_date"], errors="coerce")
    df = df.dropna(subset=["match_date"])
    df["year"] = df["match_date"].dt.year
    df["y"] = df["outcome"].map({"H": 0, "D": 1, "A": 2}).astype(int)
    return df


def add_league_stats(train: pd.DataFrame) -> pd.DataFrame:
    """用**训练期**数据聚合联赛统计, K 收缩。返回 league -> (home_rate, draw_rate, avg_goals)。"""
    g = train.groupby("league")
    tot = g.size()
    hr = g.apply(lambda d: (d["y"] == 0).mean(), include_groups=False)
    dr = g.apply(lambda d: (d["y"] == 1).mean(), include_groups=False)
    goals = train.assign(tg=train["home_score"].fillna(0) + train["away_score"].fillna(0)) \
                 .groupby("league")["tg"].mean()
    gh, gd, gg = (train["y"] == 0).mean(), (train["y"] == 1).mean(), \
                 (train["home_score"].fillna(0) + train["away_score"].fillna(0)).mean()
    out = pd.DataFrame({
        "lg_home_rate": (hr * tot + gh * LEAGUE_K) / (tot + LEAGUE_K),
        "lg_draw_rate": (dr * tot + gd * LEAGUE_K) / (tot + LEAGUE_K),
        "lg_avg_goals": (goals * tot + gg * LEAGUE_K) / (tot + LEAGUE_K),
    })
    return out


def build_features(df: pd.DataFrame, lstats: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    eps = 1e-6
    # 联赛统计 (缺失填全局)
    d = d.merge(lstats, left_on="league", right_index=True, how="left")
    d[["lg_home_rate", "lg_draw_rate", "lg_avg_goals"]] = \
        d[["lg_home_rate", "lg_draw_rate", "lg_avg_goals"]].fillna(
            {"lg_home_rate": 0.45, "lg_draw_rate": 0.25, "lg_avg_goals": 2.6})
    # 派生
    d["f_ratio_ha"] = d["cimp_h"] / (d["cimp_a"] + eps)
    d["f_diff_ha"] = d["cimp_h"] - d["cimp_a"]
    d["f_draw_gap"] = d["cimp_d"] - (d["cimp_h"] + d["cimp_a"]) / 2.0
    d["f_open_ratio"] = d["open_h"] / (d["open_a"] + eps)
    d["f_close_ratio"] = d["close_h"] / (d["close_a"] + eps)
    d["f_open_close_h"] = d["close_h"] - d["open_h"]
    d["f_abs_drift"] = d[["drift_h", "drift_d", "drift_a"]].abs().max(axis=1)
    d["f_fav_prob"] = d[["cimp_h", "cimp_d", "cimp_a"]].max(axis=1)
    d["f_entropy"] = -(d["cimp_h"] * np.log(d["cimp_h"] + eps)
                       + d["cimp_d"] * np.log(d["cimp_d"] + eps)
                       + d["cimp_a"] * np.log(d["cimp_a"] + eps))
    d["f_year"] = d["year"]
    feats = ["cimp_h", "cimp_d", "cimp_a", "imp_h", "imp_d", "imp_a",
             "open_h", "open_d", "open_a", "close_h", "close_d", "close_a",
             "drift_h", "drift_d", "drift_a", "overround", "home_edge", "sigma_trap",
             "f_ratio_ha", "f_diff_ha", "f_draw_gap", "f_open_ratio", "f_close_ratio",
             "f_open_close_h", "f_abs_drift", "f_fav_prob", "f_entropy",
             "lg_home_rate", "lg_draw_rate", "lg_avg_goals", "f_year"]
    for c in feats:
        if c not in d.columns:
            d[c] = np.nan
    d[feats] = d[feats].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    return d, feats


def main():
    t0 = time.time()
    print(f"[{time.strftime('%H:%M:%S')}] 加载数据 ...")
    df = load()
    df = quality_filter(df)
    print(f"  有效样本 {len(df)} 场, 年份 {int(df['year'].min())}~{int(df['year'].max())}")

    # ── 时间外切分 ──
    tr = df[df["year"] < TEST_YEAR - 1]
    va = df[(df["year"] >= TEST_YEAR - 1) & (df["year"] < TEST_YEAR)]
    te = df[df["year"] >= TEST_YEAR]
    print(f"  训练集 {len(tr)} ({tr['year'].min()}~{tr['year'].max()}) | "
          f"验证集 {len(va)} | 测试集 {len(te)} ({te['year'].min()}~{te['year'].max()})")

    print(f"[{time.strftime('%H:%M:%S')}] 联赛统计(仅训练期, K={LEAGUE_K}) ...")
    lstats = add_league_stats(tr)
    tr2, feats = build_features(tr, lstats)
    va2, _ = build_features(va, lstats)
    te2, _ = build_features(te, lstats)
    print(f"  特征维度 {len(feats)}")

    Xtr, ytr = tr2[feats].values, tr2["y"].values
    Xva, yva = va2[feats].values, va2["y"].values
    Xte, yte = te2[feats].values, te2["y"].values

    # ── baseline: 去水概率 argmax ──
    base_te = np.argmax(te2[["cimp_h", "cimp_d", "cimp_a"]].values, axis=1)
    base_acc = (base_te == yte).mean()
    base_va = np.argmax(va2[["cimp_h", "cimp_d", "cimp_a"]].values, axis=1)
    print(f"\n[baseline] 市场去水概率 argmax: 验证 {((base_va == yva).mean())*100:.2f}% | "
          f"测试 {base_acc*100:.2f}%")

    print(f"[{time.strftime('%H:%M:%S')}] 训练 {MODEL} ...")
    if MODEL == "lgb":
        import lightgbm as lgb
        m = lgb.LGBMClassifier(n_estimators=3000, learning_rate=0.03, num_leaves=63,
                               min_child_samples=50, subsample=0.85, colsample_bytree=0.85,
                               reg_lambda=1.0, n_jobs=24, random_state=42, verbose=-1)
        m.fit(Xtr, ytr, eval_set=[(Xva, yva)],
              callbacks=[lgb.early_stopping(150, verbose=False), lgb.log_evaluation(0)])
    else:
        import xgboost as xgb
        m = xgb.XGBClassifier(n_estimators=4000, learning_rate=0.03, max_depth=7,
                              min_child_weight=30, subsample=0.85, colsample_bytree=0.8,
                              reg_lambda=1.5, gamma=0.1, tree_method="hist",
                              n_jobs=24, random_state=42, eval_metric="mlogloss",
                              early_stopping_rounds=150)
        m.fit(Xtr, ytr, eval_set=[(Xva, yva)], verbose=False)

    pva = m.predict(Xva); pte = m.predict(Xte)
    acc_va = (pva == yva).mean(); acc_te = (pte == yte).mean()
    print(f"\n===== 结果 =====")
    print(f"  {'':10s}{'验证':>10s}{'测试':>10s}")
    print(f"  {'baseline':10s}{((base_va == yva).mean())*100:>9.2f}%{base_acc*100:>9.2f}%")
    print(f"  {MODEL:10s}{acc_va*100:>9.2f}%{acc_te*100:>9.2f}%")
    print(f"  {'提升':10s}{(acc_va-(base_va==yva).mean())*100:>+9.2f}pp"
          f"{(acc_te-base_acc)*100:>+9.2f}pp")

    # 各方向召回
    print(f"\n  测试集各类准确率:")
    for i, nm in enumerate(("主胜", "平局", "客胜")):
        msk = yte == i
        if msk.sum() == 0:
            continue
        print(f"    {nm}(n={msk.sum():6d}): 模型 {(pte[msk]==i).mean()*100:5.1f}% | "
              f"baseline {(base_te[msk]==i).mean()*100:5.1f}%")

    # 概率质量
    try:
        prob = m.predict_proba(Xte)
        ll = -np.mean(np.log(np.clip(prob[np.arange(len(yte)), yte], 1e-9, 1)))
        yoh = np.zeros_like(prob); yoh[np.arange(len(yte)), yte] = 1
        brier = np.mean(np.sum((prob - yoh) ** 2, axis=1))
        print(f"\n  测试 LogLoss {ll:.4f} | Brier {brier:.4f}")
    except Exception as e:
        print(f"  概率指标计算失败: {e}")

    os.makedirs(OUT_DIR, exist_ok=True)
    import joblib
    joblib.dump({"model": m, "features": feats, "model_kind": MODEL,
                 "test_year": TEST_YEAR, "league_stats": lstats,
                 "trained_at": int(time.time())}, OUT_PATH)
    print(f"\n[{time.strftime('%H:%M:%S')}] 已保存 → {OUT_PATH}  (耗时 {time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()

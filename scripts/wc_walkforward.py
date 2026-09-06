"""
WC 模型 walk-forward 泛化验证 (时序外推, 非 in-sample)
========================================================
目的: 验证 A 路径 (DrawExpert 77维) 的真实泛化能力。
   原 96.2% "optimized" 用的是 shuffle StratifiedKFold (in-sample 乐观) +
   且 honest_backtest 80 场与训练集重叠 → 有泄漏嫌疑。

本脚本做法 (诚实版):
- 加载 带标签 + 有特征 的 世界杯 比赛 (league_name='世界杯', final_result NOT NULL, 有 match_features)
- 全部为 2026 单届 (DB 无历史世界杯), 故只能做 WITHIN-2026 时序外推
- expanding-window: 用更早的比赛训练, 预测更晚的比赛 (无未来泄漏)
- DB matches 表无赔率列 → 本验证测的是「纯 ML 特征→赛果」独立泛化
  (比 odds 混合的 96.2% 更干净, 是泄漏探针)

对照:
- in-sample: 全量 shuffle 5-fold CV acc (乐观上界)
- walk-forward: 逐折时序外推 acc (真实泛化)
- 差距 = 泄漏量级估计

用法:
  .venv/Scripts/python.exe scripts/wc_walkforward.py
"""
import sqlite3, os, sys, json, warnings
import numpy as np
from sklearn.metrics import accuracy_score, f1_score
warnings.filterwarnings("ignore")

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import wc_train_pipeline as TP

ROOT = os.path.dirname(SCRIPTS)
DB = os.path.join(ROOT, "data", "football_data.db")
OUT_JSON = os.path.join(ROOT, "deliverables", "wc2026_walkforward.json")


def load_wc_dated():
    conn = sqlite3.connect(DB); cur = conn.cursor()
    cur.execute("PRAGMA table_info(match_features)")
    all_cols = [r[1] for r in cur.fetchall()]
    skip = {"feature_id", "match_id", "created_at"}
    cur.execute(
        "SELECT m.match_id, m.match_date, m.final_result FROM matches m "
        "WHERE m.league_name='世界杯' AND m.final_result IS NOT NULL"
    )
    rows = cur.fetchall()
    mids = [r[0] for r in rows]
    dates = {r[0]: r[1] for r in rows}
    ymap = {"H": 0, "D": 1, "A": 2}
    y_all = np.array([ymap[r[2]] for r in rows])

    placeholders = ",".join("?" * len(mids))
    col_str = ",".join(all_cols)
    cur.execute(
        f"SELECT match_id, {col_str} FROM match_features WHERE match_id IN ({placeholders})", mids
    )
    raw = {r[0]: dict(zip(all_cols, r[1:])) for r in cur.fetchall()}
    conn.close()

    feat_cols = [c for c in all_cols if c not in skip]
    clean_cols = []
    for c in feat_cols:
        sample = next((raw[m][c] for m in mids if m in raw and raw[m][c] is not None), None)
        if sample is None:
            continue
        try:
            float(sample)
            clean_cols.append(c)
        except (ValueError, TypeError):
            continue

    valid = [m for m in mids if m in raw]
    X = np.array([[float(raw[m][c]) for c in clean_cols] for m in valid], dtype=float)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    y = y_all[[mids.index(m) for m in valid]]
    yd = np.array([dates[m] for m in valid])
    print(f"[load] n={len(y)} | H={(y==0).sum()} D={(y==1).sum()} A={(y==2).sum()} | feat={len(clean_cols)}")
    return X, y, yd, clean_cols


def predict_main(pkg, X):
    lgb_p = pkg["lgb"].predict_proba(X)
    xgb_p = pkg["xgb"].predict_proba(X)
    meta_X = np.hstack([lgb_p, xgb_p])
    return pkg["meta"].predict(meta_X), pkg["meta"].predict_proba(meta_X)


def predict_draw(pkg, X):
    p = pkg["model"].predict_proba(X)[:, 1]
    cal = pkg["calibrator"].predict(p)
    return (cal >= pkg["threshold"]).astype(int), cal


def evaluate(Xtr, ytr, Xte, yte):
    """训练主模型 + DrawExpert, 在测试集评估。返回指标 dict。"""
    main_pkg, *_ = TP.train_main_model(Xtr, ytr, COLS)
    de_pkg, de_f1 = TP.train_draw_expert(Xtr, ytr, COLS)

    main_pred, main_proba = predict_main(main_pkg, Xte)
    main_acc = accuracy_score(yte, main_pred)

    draw_flag, draw_cal = predict_draw(de_pkg, Xte)
    # 融合: 主模型 argmax, 若 DrawExpert 标定概率>=阈值 则翻 D
    fused = main_pred.copy()
    flip = draw_flag & (main_pred != 1)
    fused[flip] = 1
    fused_acc = accuracy_score(yte, fused)

    # D-recall (测试集真实 D 中被 DrawExpert 标中)
    true_d = (yte == 1)
    d_recall = (draw_flag[true_d].sum() / true_d.sum()) if true_d.sum() > 0 else None
    # 多数类基线
    majority = np.bincount(ytr).argmax()
    base_acc = (yte == majority).mean()
    return {
        "main_acc": round(float(main_acc), 4),
        "fused_acc": round(float(fused_acc), 4),
        "draw_recall": round(float(d_recall), 4) if d_recall is not None else None,
        "majority_baseline": round(float(base_acc), 4),
        "n_test": int(len(yte)),
        "n_trueD": int(true_d.sum()),
    }


def in_sample_cv(X, y):
    """全量 shuffle 5-fold CV (乐观上界, 与原管道一致)"""
    main_pkg, acc, mf1, df1 = TP.train_main_model(X, y, COLS)
    return round(float(acc), 4)


if __name__ == "__main__":
    print("=" * 64)
    print("WC walk-forward 泛化验证 (expanding window, 纯 ML)")
    print("=" * 64)
    X, y, yd, COLS = load_wc_dated()

    # in-sample 乐观上界
    insample = in_sample_cv(X, y)
    print(f"\n[in-sample] shuffle 5-fold CV acc = {insample:.4f} (乐观上界)")

    # expanding-window 折: (cut_train<=, cut_test<=, label)
    FOLDS = [
        ("2026-06-16", "2026-06-22", "Fold1: 训练MD1 → 测试MD2"),
        ("2026-06-22", "2026-06-27", "Fold2: 训练MD1+2 → 测试MD3"),
        ("2026-06-27", "2026-12-31", "Fold3: 训练小组赛 → 测试淘汰赛"),
    ]
    results = []
    tot_test = 0
    wacc_num = 0.0
    for cut_tr, cut_te, label in FOLDS:
        tr_mask = yd <= cut_tr
        te_mask = (yd > cut_tr) & (yd <= cut_te)
        if te_mask.sum() == 0:
            print(f"\n[{label}] 跳过: 测试集为空")
            continue
        print(f"\n[{label}] train={tr_mask.sum()} test={te_mask.sum()}")
        res = evaluate(X[tr_mask], y[tr_mask], X[te_mask], y[te_mask])
        res["label"] = label
        res["n_train"] = int(tr_mask.sum())
        results.append(res)
        tot_test += res["n_test"]
        wacc_num += res["main_acc"] * res["n_test"]
        print(f"  main_acc={res['main_acc']:.3f} fused_acc={res['fused_acc']:.3f} "
              f"draw_recall={res['draw_recall']} majority_base={res['majority_baseline']:.3f}")

    agg_acc = wacc_num / tot_test if tot_test else 0
    print("\n" + "=" * 64)
    print(f"汇总: in-sample={insample:.4f} | walk-forward(加权)={agg_acc:.4f} | "
          f"差距(泄漏估计)={insample-agg_acc:+.4f}")
    print(f"总测试样本(时序外推)={tot_test}")
    print("=" * 64)

    out = {
        "method": "expanding-window walk-forward, pure-ML (no odds in DB)",
        "note": "all WC matches dated 2026; within-tournament temporal extrapolation only",
        "in_sample_cv_acc": insample,
        "walkforward_weighted_acc": round(agg_acc, 4),
        "leakage_gap": round(insample - agg_acc, 4),
        "total_oos_test": tot_test,
        "folds": results,
    }
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"Saved {OUT_JSON}")

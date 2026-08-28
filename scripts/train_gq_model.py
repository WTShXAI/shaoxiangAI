#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
train_gq_model.py — 用 GQ 已标注盘口数据训练 gq_model
数据: data/events.db.match_outcomes (盘口+赛果同行, 干净无串场)
切分: 按 captured_at 时间升序, 早70%训练 / 晚30%测试 (walkforward, 杜绝未来泄露)
评估: 必并排 naive 基线(永远买主胜 / 永远买小 / 庄家热门), 不孤立报模型命中率
"""
import os, sys, sqlite3, json
import numpy as np
from collections import Counter

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.gq_model import build_features, _devig, GQModel

DB = os.path.join(_ROOT, "data", "events.db")


def load_rows():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM match_outcomes WHERE score_home IS NOT NULL AND score_away IS NOT NULL")
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    rows.sort(key=lambda r: r.get("captured_at") or 0)
    return rows


def y_1x2(r):
    h, a = r["score_home"], r["score_away"]
    return 0 if h > a else (1 if h == a else 2)


def y_ou(r):
    line = float(r["op_ou_line"])
    total = r["score_home"] + r["score_away"]
    if total == line:
        return None  # 走水, 排除
    return 0 if total < line else 1


def mask_1x2(r):
    return (r.get("op_1x2_h") is not None and r.get("op_1x2_d") is not None
            and r.get("op_1x2_a") is not None)


def mask_ou(r):
    return (r.get("op_ou_line") is not None and r.get("op_ou_over") is not None
            and r.get("op_ou_under") is not None)


def make_xy(subset, mask_fn, y_fn, lf):
    sub = [r for r in subset if mask_fn(r)]
    X = [build_features(r, lf) for r in sub]
    y = [y_fn(r) for r in sub]
    return X, y, sub


def acc(y_true, y_pred):
    return sum(1 for a, b in zip(y_true, y_pred) if a == b) / max(1, len(y_true))


def main():
    rows = load_rows()
    n = len(rows)
    k = int(n * 0.7)
    train_rows, test_rows = rows[:k], rows[k:]
    print(f"总标注场: {n} | 训练(早70%): {len(train_rows)} | 测试(晚30%): {len(test_rows)}")

    # 联赛频率仅从训练集算 (防泄露)
    lc = Counter(r["league"] for r in train_rows)
    league_freq = {lg: c / len(train_rows) for lg, c in lc.items()}

    # ---- 按目标特征选择 (梯度提升在单纯形上失效, 单棵决策树只需干净特征) ----
    FEAT_1X2 = [0, 1, 2]               # 仅 imp_h/d/a: 小样本下 league/kick 是噪声, 拖低命中率
    FEAT_OU = [3, 4, 5, 0, 1, 2]       # imp_over/under + ou_line + 1X2上下文
    FEAT_TOT = list(range(16))         # 回归用全特征

    def build_sub(subset, mask_fn, y_fn, fidx):
        sub = [r for r in subset if mask_fn(r)]
        X = np.array([[build_features(r, league_freq)[i] for i in fidx] for r in sub], dtype=float)
        y = [y_fn(r) for r in sub]
        return X, y, sub

    # ---- 1X2 ----
    Xtr, ytr, _ = build_sub(train_rows, mask_1x2, y_1x2, FEAT_1X2)
    Xte, yte, test1 = build_sub(test_rows, mask_1x2, y_1x2, FEAT_1X2)
    # ---- OU (排除走水 total==line) ----
    Xtr_o, ytr_o, _ = build_sub(train_rows, mask_ou, y_ou, FEAT_OU)
    Xtr_o = np.array([x for x, v in zip(Xtr_o, ytr_o) if v is not None]); ytr_o = [v for v in ytr_o if v is not None]
    Xte_o, yte_o, test_o = build_sub(test_rows, mask_ou, y_ou, FEAT_OU)
    Xte_o = np.array([x for x, v in zip(Xte_o, yte_o) if v is not None]); yte_o = [v for v in yte_o if v is not None]
    # ---- 总进球 ----
    Xtr_t = np.array([build_features(r, league_freq) for r in train_rows], dtype=float)[:, FEAT_TOT]
    ytr_t = [r["score_home"] + r["score_away"] for r in train_rows]
    Xte_t = np.array([build_features(r, league_freq) for r in test_rows], dtype=float)[:, FEAT_TOT]
    yte_t = [r["score_home"] + r["score_away"] for r in test_rows]

    print(f"1X2 可用: 训练 {len(ytr)} / 测试 {len(yte)}")
    print(f"OU   可用: 训练 {len(ytr_o)} / 测试 {len(yte_o)}")
    print(f"总进球可用: 训练 {len(ytr_t)} / 测试 {len(yte_t)}")

    # 训练
    model = GQModel()
    model.fit(Xtr, ytr, Xtr_o, ytr_o, Xtr_t, ytr_t, league_freq,
              feat_idx={"1x2": FEAT_1X2, "ou": FEAT_OU, "tot": FEAT_TOT},
              meta={"n_train": len(train_rows), "n_test": len(test_rows)})

    # ---- 评估 1X2 ----
    yhat = []
    for r in test1:
        p = model.predict_1x2(r)
        yhat.append(p["cls"] if p else 0)
    base_home = acc(yte, [0] * len(yte))
    base_fav = acc(yte, [max(range(3), key=lambda i: _devig(r["op_1x2_h"], r["op_1x2_d"], r["op_1x2_a"])[i]) for r in test1])
    model_acc = acc(yte, yhat)
    print("\n=== 1X2 命中率(测试集, 晚30%) ===")
    print(f"  永远买主胜(基线A):   {base_home:.3%}")
    print(f"  庄家热门(基线B):     {base_fav:.3%}")
    print(f"  GQ模型:              {model_acc:.3%}  (较基线A {model_acc-base_home:+.2f}pp)")

    # ---- 评估 OU ----
    if len(yte_o) > 0 and model.ou is not None:
        you_hat = []
        for r in test_o:
            p = model.predict_ou(r)
            you_hat.append(p["cls"] if p else 0)
        base_under = acc(yte_o, [0] * len(yte_o))
        ou_acc = acc(yte_o, you_hat)
        print("\n=== OU 大小(测试集) ===")
        print(f"  永远买小(基线):      {base_under:.3%}")
        print(f"  GQ模型:              {ou_acc:.3%}  (较基线 {ou_acc-base_under:+.2f}pp)")

    # ---- 评估 总进球 MAE ----
    if model.tot is not None:
        yt_hat = [model.predict_total(r) for r in test_rows]
        mae = sum(abs(a - b) for a, b in zip(yte_t, yt_hat)) / len(yte_t)
        base_mae = sum(abs(a - (sum(ytr_t) / len(ytr_t))) for a in yte_t) / len(yte_t)
        print("\n=== 总进球 MAE(测试集) ===")
        print(f"  均值基线 MAE: {base_mae:.3f} | GQ模型 MAE: {mae:.3f}")

    model.save()
    print("\n模型已保存: data/gq_1x2_model.joblib / gq_ou_model.joblib / gq_total_model.joblib / gq_model_meta.json")


if __name__ == "__main__":
    main()

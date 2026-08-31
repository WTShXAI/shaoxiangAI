# -*- coding: utf-8 -*-
"""
open_eye_20260831.py — 打开"天眼": 在开盘价下注, 只用开盘时刻可得信号选边

诚实边界(IR-30):
  - 推理特征 ONLY: op_h/op_d/op_a(开盘价) + 派生(去水隐含/margin/盘口类别) + league(开盘时已知)
  - 绝不使用 cl_*(收盘) / drift_*(收盘漂移) 在推理时 —— 那是未来信息
  - drift 模型训练用历史标签 y_drift=argmin(drift), 但 OOS 推理只喂 op 特征
  - 结算用真实 result(H/D/A), 在开盘价 op 下注

天眼含义: 开盘价对"后来会缩水那一方"系统性便宜(PART C 前视 +3.28%~+8.89%)。
          本项目差的就是"开盘时用独立信息预测谁缩水"。本脚本用盘口结构+开盘赔率训练
          drift-direction / result 两个轻量模型充当这个"天眼", 验证它能否在开盘价 +EV。

信号:
  BASE_MARKET  押 devig(op) argmax(市场最可能方)            — 基线
  BASE_FAV     押最低 op(热门)                              — 基线
  EYE_DRIFT    drift 模型 argmax(预测谁缩水)@op             — 天眼A
  EYE_RESULT   赛果模型 argmax @op                           — 天眼B
  EYE_RESID    押 (模型P - 开盘隐含) 最大方 @op             — 天眼C(残差)
  CHEAT_DRIFT  用真实 drift 标签选边 @op                    — 前视对照(PART C 重演, 不可交易)
输出: scripts/open_eye_out.json
"""
from __future__ import annotations
import sqlite3, json, math, re
import numpy as np
import lightgbm as lgb
from sklearn.metrics import roc_auc_score

DB = "data/rollball_training.db"
OUT = "scripts/open_eye_out.json"
N_BOOT = 2000
RNG = 7
SEL = {"H": 0, "D": 1, "A": 2}
IDX = ["H", "D", "A"]


def devig(h, d, a):
    s = 1.0 / h + 1.0 / d + 1.0 / a
    return np.stack([(1.0 / h) / s, (1.0 / d) / s, (1.0 / a) / s], axis=1)  # (N,3)


def cat(op_h, op_a):
    fav = min(op_h, op_a)
    return 0 if fav <= 1.80 else (1 if fav <= 2.50 else 2)   # 热门/均衡/冷门


con = sqlite3.connect(DB); cur = con.cursor()
rows = cur.execute(
    "SELECT op_h,op_d,op_a,cl_h,cl_d,cl_a,drift_h,drift_d,drift_a,result,league "
    "FROM rb_matches WHERE op_h>1.01 AND op_d>1.01 AND op_a>1.01 "
    "AND cl_h>1.01 AND result IN ('H','D','A')").fetchall()
con.close()
print(f"[load] {len(rows):,} 场 (开盘价+收盘+赛果齐全)")

# league 低频合并
from collections import Counter
lg_cnt = Counter(r[10] for r in rows)
RARE = {k for k, v in lg_cnt.items() if v < 50}


def feat(h, d, a, lg):
    s = 1.0 / h + 1.0 / d + 1.0 / a
    return [h, d, a, math.log(h), math.log(d), math.log(a),
            (1.0 / h) / s, (1.0 / d) / s, (1.0 / a) / s,
            cat(h, a), (0.0 if lg in RARE else 1.0)]


X = np.array([feat(r[0], r[1], r[2], r[9]) for r in rows], dtype=float)
Yres = np.array([SEL[r[9]] for r in rows], dtype=int)
Ydrift = np.array([int(np.argmin([r[6], r[7], r[8]])) for r in rows], dtype=int)
op = np.array([[r[0], r[1], r[2]] for r in rows], dtype=float)
cl = np.array([[r[3], r[4], r[5]] for r in rows], dtype=float)
res = Yres
print(f"[feat] X={X.shape}  漂移标签分布 H/D/A = {np.bincount(Ydrift)}  赛果分布 = {np.bincount(Yres)}")

# 随机 80/20 切分 (研究用, 非严格时间外 -> 须前向验证才信)
rng = np.random.default_rng(RNG)
perm = rng.permutation(len(X))
n_tr = int(len(X) * 0.8)
tr, te = perm[:n_tr], perm[n_tr:]
print(f"[split] train={len(tr):,} test={len(te):,}")


def train_model(y):
    m = lgb.LGBMClassifier(n_estimators=400, max_depth=4, learning_rate=0.05,
                           num_leaves=15, min_child_samples=100, subsample=0.8,
                           colsample_bytree=0.8, n_jobs=-1, random_state=RNG,
                           class_weight="balanced")
    m.fit(X[tr], y[tr])
    return m


m_res = train_model(Yres)
m_dr = train_model(Ydrift)
P_res = m_res.predict_proba(X[te])
P_dr = m_dr.predict_proba(X[te])
print("[train] 赛果模型 OOS acc = %.2f%%   漂移模型 OOS acc = %.2f%%"
      % (100 * (P_res.argmax(1) == res[te]).mean(),
         100 * (P_dr.argmax(1) == Ydrift[te]).mean()))

# ---- 天眼核心: 预测 PART C 的"开盘->收盘压低>2%"条件(前视信号)能否在开盘时预测 ----
# 这些条件是 PART C 前视 +EV 的来源; 用开盘特征训练二分类器预测, 推理无前视
drop_h = (op[:, 0] - cl[:, 0]) / op[:, 0] > 0.02     # 主家压低
drop_a = (op[:, 2] - cl[:, 2]) / op[:, 2] > 0.02     # 客家压低
rise_d = (cl[:, 1] - op[:, 1]) / op[:, 1] > 0.02     # 平局升
cond_models = {}
cond_defs = {
    "主压低->押H": (drop_h, 0),
    "客压低->押A": (drop_a, 2),
    "平升->押H": (rise_d, 0),
    "主压低+平升->押H": (drop_h & rise_d, 0),
    "主升+客压低->押A": (((op[:, 0] - cl[:, 0]) / op[:, 0] > 0.02) & drop_a, 2),
}
for name, (mask, side) in cond_defs.items():
    y = mask.astype(int)
    if y.sum() < 200:
        print(f"[skip] {name} 正样本过少 {y.sum()}")
        continue
    mc = lgb.LGBMClassifier(n_estimators=300, max_depth=4, learning_rate=0.05,
                            num_leaves=15, min_child_samples=100, subsample=0.8,
                            colsample_bytree=0.8, n_jobs=-1, random_state=RNG)
    mc.fit(X[tr], y[tr])
    p = mc.predict_proba(X[te])[:, 1]
    cond_models[name] = (p, side, float(y[te].mean()))
    auc = roc_auc_score(y[te], p)
    print(f"[train] 条件 {name}: 触发率={100*float(y[te].mean()):.1f}%  OOS AUC={auc:.3f}")


def settle(side_idx, tag):
    s = np.asarray(side_idx, dtype=int)
    row = np.arange(len(te))
    px = op[te][row, s]
    win = (res[te] == s)
    pnl = np.where(win, px - 1.0, -1.0)
    n = len(pnl)
    roi = float(pnl.mean())
    wr = float(win.mean())
    imp = float((1.0 / px).mean())
    br = np.random.default_rng(RNG)
    b = br.integers(0, n, size=(N_BOOT, n))
    rr = pnl[b].mean(axis=1)
    lo, hi = float(np.percentile(rr, 2.5)), float(np.percentile(rr, 97.5))
    return {"n": n, "roi": round(100 * roi, 2), "roi_CI": [round(100 * lo, 2), round(100 * hi, 2)],
            "win_rate": round(100 * wr, 2), "implied": round(100 * imp, 2),
            "edge_pp": round(100 * (wr - imp), 2),
            "pos_ev": bool(roi > 0 and lo > 0 and n >= 300), "tag": tag}


def base_market():
    return devig(op[te, 0], op[te, 1], op[te, 2]).argmax(1)


def base_fav():
    return np.argmin(op[te], axis=1)


def cheat_drift():
    # 前视: 用真实收盘漂移标签 (argmin drift) 选边 @开盘价 —— PART C 重演
    return Ydrift[te]


def eye_resid():
    # 残差 = 模型P - 开盘隐含, 押残差最大方
    imp = devig(op[te, 0], op[te, 1], op[te, 2])
    resid = P_res - imp
    return resid.argmax(1)


print("\n=== 天眼结果 (开盘价下注, 无前视, OOS n=%d) ===" % len(te))
res_all = {
    "BASE_MARKET": settle(base_market(), "市场最可能方@开盘"),
    "BASE_FAV": settle(base_fav(), "押热门@开盘"),
    "EYE_DRIFT": settle(P_dr.argmax(1), "天眼A: 漂移模型预测谁缩水@开盘"),
    "EYE_RESULT": settle(P_res.argmax(1), "天眼B: 赛果模型argmax@开盘"),
    "EYE_RESID": settle(eye_resid(), "天眼C: 模型-开盘隐含残差最大方@开盘"),
    "CHEAT_DRIFT_LOOKAHEAD": settle(cheat_drift(), "前视对照: 真实漂移标签@开盘(不可交易)"),
}
# 天眼D: 条件预测器(PART C 前视信号 -> 开盘时刻预测)
for name, (p, side, base_rate) in cond_models.items():
    # 只在模型预测触发概率高于阈值(>开盘基准触发率)时下注, 押该方 @开盘
    th = base_rate
    bet = p > th
    if bet.sum() < 300:
        res_all[f"EYE_D_{name}"] = {"n": int(bet.sum()), "roi": None,
                                     "note": "样本不足(<300)"}
        continue
    px = op[te][np.arange(len(te)), np.full(len(te), side)][bet]
    win = (res[te] == side)[bet]
    pnl = np.where(win, px - 1.0, -1.0)
    n = int(bet.sum()); roi = float(pnl.mean()); wr = float(win.mean())
    imp = float((1.0 / px).mean())
    br = np.random.default_rng(RNG); b = br.integers(0, n, size=(N_BOOT, n))
    rr = pnl[b].mean(axis=1); lo, hi = float(np.percentile(rr, 2.5)), float(np.percentile(rr, 97.5))
    res_all[f"EYE_D_{name}"] = {"n": n, "roi": round(100 * roi, 2),
        "roi_CI": [round(100 * lo, 2), round(100 * hi, 2)], "win_rate": round(100 * wr, 2),
        "implied": round(100 * imp, 2), "edge_pp": round(100 * (wr - imp), 2),
        "pos_ev": bool(roi > 0 and lo > 0 and n >= 300),
        "tag": f"天眼D: 预测'{name}'触发@开盘押{side}"}
for k, v in res_all.items():
    if "roi" not in v or v.get("roi") is None:
        print(f"  {k:26s} {v.get('note','-')}")
        continue
    flag = "✅+EV" if v["pos_ev"] else ("❌" if v["roi"] < 0 else "⚠️")
    print(f"  {k:26s} n={v['n']:>6} ROI={v['roi']:>7.2f}%  CI={v['roi_CI']}  "
          f"wr={v['win_rate']}% imp={v['implied']}% edge={v['edge_pp']:+.2f}pp  {flag}")

open_eye = [k for k, v in res_all.items()
            if (k.startswith("EYE") and isinstance(v, dict) and v.get("pos_ev"))]
out = {"meta": {"db": DB, "n_total": len(rows), "n_oos": len(te),
                "note": "天眼=开盘价下注+仅开盘可得特征; drift/赛果模型推理不喂cl/drift; 随机80/20非时间外",
                "models": {"result_oos_acc": round(100 * (P_res.argmax(1) == res[te]).mean(), 2),
                           "drift_oos_acc": round(100 * (P_dr.argmax(1) == Ydrift[te]).mean(), 2)}},
       "signals": res_all, "eye_opened": open_eye}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"\n[done] {OUT}")
print("[判定]", ("天眼打开(EYE 有 +EV): " + ", ".join(open_eye)) if open_eye
      else "天眼未打开: 现有开盘信号不足以跨过抽水(模型视力 < 市场)。需补独立实力特征(ELO/状态/交锋)或前向验证。")

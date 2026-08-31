# -*- coding: utf-8 -*-
"""
train_independent_model_open_eye_20260831.py — 真·天眼: independent_model 用开盘派生特征重训, OOF 在开盘价下注验证 +EV

复用 production train_independent_model.py 的装配与特征工程(canon 对齐 + indep_features 13维独立实力),
唯一改动: 赔率派生特征用 odds_open_* (而非 odds_close_*)。这是"在开盘时刻预测"的天眼。

验证(严格无前视): 模型在 train<2023 训练, 对 OOF(>=2023) 预测, 在 OOF 的 odds_open 价下注。
  - BASE_MARKET: devig(odds_open) argmax @open
  - EYE_OPEN_ARGMAX: 开盘天眼模型 argmax @open
  - EYE_OPEN_RESID: 押 模型P - devig(open) 最大方 @open
  - EYE_OPEN_RESID_TH: 仅 |残差|>=th 下注 @open (选择性 overlay)
对照:
  - CHEAT_CLOSE_ARGMAX: 生产模型(收盘派生) argmax @open (含前视泄漏的对照)
输出: scripts/train_independent_model_open_eye_out.json
"""
from __future__ import annotations
import sqlite3, json, math, os
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "football_data.db")
OUT = "scripts/train_independent_model_open_eye_out.json"
N_BOOT = 2000
RNG = 7
SEL = {"H": 0, "D": 1, "A": 2}

# ---- 复刻 train_independent_model.py 的特征工程 ----
INDEP_FEATS = ["elo_home", "elo_away", "elo_diff", "form_home", "form_away", "form_diff",
               "rest_home", "rest_away", "rest_diff", "h2h_home_win", "h2h_draw",
               "h2h_away_win", "league_strength"]


def _pois(l, k):
    if k < 0:
        return 0.0
    return math.exp(-l) * (l ** k) / math.factorial(k)


def _dc_probs(lh, la, n=12):
    H = D = A = 0.0
    for i in range(n + 1):
        for j in range(n + 1):
            p = _pois(lh, i) * _pois(la, j)
            H += p if i > j else (D if i == j else A)
    return H, D, A


def _dc_inv(ph, pd, pa, iters=80, lr=0.08):
    lh, la = 1.4, 1.1
    for _ in range(iters):
        H, D, A = _dc_probs(lh, la)
        lh -= lr * (H - ph); la -= lr * (A - pa)
        lh = max(0.05, min(lh, 6.0)); la = max(0.05, min(la, 6.0))
    return lh, la


def odds_extra(oh, od, oa):
    inv = 1.0 / oh + 1.0 / od + 1.0 / oa
    dh, dd, da = (1.0 / oh) / inv, (1.0 / od) / inv, (1.0 / oa) / inv
    lh, la = _dc_inv(dh, dd, da)
    _, dcD, _ = _dc_probs(lh, la)
    ent = sum(-p * math.log(p) for p in (dh, dd, da) if p > 0)
    return [dh, dd, da, inv - 1.0, lh, la, dcD, dd - dcD, ent, dh - da]


ODDS_OPEN_FEATS = ["devig_h", "devig_d", "devig_a", "overround", "lambda_home",
                   "lambda_away", "dc_draw", "draw_dev", "entropy", "margin_impl"]
FEATURES = INDEP_FEATS + ODDS_OPEN_FEATS


def build_alias_map(cur):
    amap = {}
    for canon, aj in cur.execute("SELECT canonical, aliases_json FROM team_canonical"):
        amap[canon.strip().lower()] = canon
        if aj:
            try:
                for a in (json.loads(aj) if aj.strip().startswith("[") else ast.literal_eval(aj)):
                    amap[str(a).strip().lower()] = canon
            except Exception:
                pass
    return amap


import ast
con = sqlite3.connect(DB)
amap = build_alias_map(con)
dfi = pd.read_sql_query("SELECT * FROM indep_features", con)
indep_idx = {(r["home"], r["away"], str(r["match_date"])[:10]): r for _, r in dfi.iterrows()}
df = pd.read_sql_query(
    "SELECT m.home_team_name, m.away_team_name, m.match_date, m.final_result, "
    "mf.odds_open_h, mf.odds_open_d, mf.odds_open_a, mf.odds_close_h, mf.odds_close_d, mf.odds_close_a "
    "FROM matches m JOIN match_features mf ON m.match_id=mf.match_id "
    "WHERE m.final_result IN ('H','D','A') "
    "AND mf.odds_open_h>0 AND mf.odds_open_d>0 AND mf.odds_open_a>0 "
    "AND mf.odds_close_h>0 AND mf.odds_close_d>0 AND mf.odds_close_a>0", con)
con.close()


def canon(t):
    return amap.get(str(t).strip().lower(), str(t).strip().lower())


rows = []
for _, r in df.iterrows():
    ch, ca = canon(r["home_team_name"]), canon(r["away_team_name"])
    ir = indep_idx.get((ch, ca, str(r["match_date"])[:10]))
    if ir is None:
        continue
    oh, od, oa = float(r["odds_open_h"]), float(r["odds_open_d"]), float(r["odds_open_a"])
    ch2, cd2, ca2 = float(r["odds_close_h"]), float(r["odds_close_d"]), float(r["odds_close_a"])
    feat = [float(ir[c]) for c in INDEP_FEATS] + odds_extra(oh, od, oa)
    feat_close = [float(ir[c]) for c in INDEP_FEATS] + odds_extra(ch2, cd2, ca2)
    rows.append({"feat_open": feat, "feat_close": feat_close, "y": SEL[r["final_result"]],
                 "date": str(r["match_date"])[:10],
                 "op": [oh, od, oa], "cl": [ch2, cd2, ca2]})
print(f"[align] 成功对齐(独立实力+开盘/收盘赔率): {len(rows)} 场")

Xo = np.array([r["feat_open"] for r in rows], dtype=float)
Xc = np.array([r["feat_close"] for r in rows], dtype=float)
y = np.array([r["y"] for r in rows])
dates = np.array([r["date"] for r in rows])
op = np.array([r["op"] for r in rows]); cl = np.array([r["cl"] for r in rows])
tr = dates < "2023-01-01"; te = dates >= "2023-01-01"
print(f"[split] train={int(tr.sum())} OOF={int(te.sum())}  特征维={Xo.shape[1]}")


def devig(o):
    s = 1.0 / o[:, 0] + 1.0 / o[:, 1] + 1.0 / o[:, 2]
    return np.stack([(1.0 / o[:, 0]) / s, (1.0 / o[:, 1]) / s, (1.0 / o[:, 2]) / s], axis=1)


def train_predict(Xtr, ytr, Xte):
    m = lgb.LGBMClassifier(objective="multiclass", num_class=3, n_estimators=1000,
                          learning_rate=0.01, num_leaves=63, min_child_samples=60,
                          subsample=0.9, colsample_bytree=0.9, reg_lambda=3.0, reg_alpha=0.2,
                          n_jobs=-1, random_state=RNG)
    m.fit(Xtr, ytr)
    return m.predict_proba(Xte)


Po = train_predict(Xo[tr], y[tr], Xo[te])      # 开盘天眼
Pc = train_predict(Xc[tr], y[tr], Xc[te])      # 生产模型(收盘派生, 前视对照)
print(f"[train] 开盘天眼 OOS acc={100*(Po.argmax(1)==y[te]).mean():.2f}%  "
      f"macroAUC={roc_auc_score(y[te],Po,multi_class='ovo',average='macro'):.4f}")
print(f"[train] 生产模型 OOS acc={100*(Pc.argmax(1)==y[te]).mean():.2f}%")


def settle(side, px, tag):
    s = np.asarray(side); row = np.arange(len(px))
    p = px[row, s]; win = (y[te] == s)
    pnl = np.where(win, p - 1.0, -1.0); n = len(pnl)
    roi = float(pnl.mean()); wr = float(win.mean()); imp = float((1.0 / p).mean())
    br = np.random.default_rng(RNG); b = br.integers(0, n, size=(N_BOOT, n))
    rr = pnl[b].mean(axis=1); lo, hi = float(np.percentile(rr, 2.5)), float(np.percentile(rr, 97.5))
    return {"n": n, "roi": round(100 * roi, 2), "roi_CI": [round(100 * lo, 2), round(100 * hi, 2)],
            "win_rate": round(100 * wr, 2), "implied": round(100 * imp, 2),
            "edge_pp": round(100 * (wr - imp), 2),
            "pos_ev": bool(roi > 0 and lo > 0 and n >= 300), "tag": tag}


imp_o = devig(op[te])
base = imp_o.argmax(1)
resid_o = Po - imp_o
resid_c = Pc - imp_o


def resid_thr(P, th):
    s = P.argmax(1); mag = P[np.arange(len(P)), s] - imp_o[np.arange(len(P)), s]
    bet = mag >= th
    if bet.sum() < 300:
        return {"n": int(bet.sum()), "roi": None, "note": f"th={th}样本不足"}
    px = op[te][np.arange(len(op[te])), s][bet]; win = (y[te] == s)[bet]
    pnl = np.where(win, px - 1.0, -1.0); n = int(bet.sum()); roi = float(pnl.mean())
    wr = float(win.mean()); imp = float((1.0 / px).mean())
    br = np.random.default_rng(RNG); b = br.integers(0, n, size=(N_BOOT, n))
    rr = pnl[b].mean(axis=1); lo, hi = float(np.percentile(rr, 2.5)), float(np.percentile(rr, 97.5))
    return {"n": n, "roi": round(100 * roi, 2), "roi_CI": [round(100 * lo, 2), round(100 * hi, 2)],
            "win_rate": round(100 * wr, 2), "implied": round(100 * imp, 2),
            "edge_pp": round(100 * (wr - imp), 2),
            "pos_ev": bool(roi > 0 and lo > 0 and n >= 300), "tag": f"开盘天眼残差>={th}@open"}


print("\n=== 真·天眼 OOF 验证 (开盘价下注, 无前视, n=%d) ===" % int(te.sum()))
res_all = {
    "BASE_MARKET": settle(base, op[te], "开盘隐含argmax@open"),
    "EYE_OPEN_ARGMAX": settle(Po.argmax(1), op[te], "开盘天眼argmax@open"),
    "EYE_OPEN_RESID": settle(resid_o.argmax(1), op[te], "开盘天眼残差最大方@open"),
    "EYE_OPEN_RESID_002": resid_thr(Po, 0.02),
    "EYE_OPEN_RESID_003": resid_thr(Po, 0.03),
    "EYE_OPEN_RESID_004": resid_thr(Po, 0.04),
    "CHEAT_CLOSE_ARGMAX": settle(Pc.argmax(1), op[te], "生产模型(收盘派生)@open前视对照"),
}
for k, v in res_all.items():
    if v.get("roi") is None:
        print(f"  {k:22s} {v.get('note')}"); continue
    flag = "✅+EV" if v["pos_ev"] else ("❌" if v["roi"] < 0 else "⚠️")
    print(f"  {k:22s} n={v['n']:>6} ROI={v['roi']:>7.2f}%  CI={v['roi_CI']}  "
          f"wr={v['win_rate']}% imp={v['implied']}% edge={v['edge_pp']:+.2f}pp  {flag}")

open_eye = [k for k, v in res_all.items() if isinstance(v, dict) and v.get("pos_ev")]
out = {"meta": {"db": DB, "n_aligned": len(rows), "n_oof": int(te.sum()),
                "note": "真天眼=independent_model用odds_open派生特征+13维独立实力; OOF在开盘价下注, 无前视",
                "eye_open_oos_acc": round(100 * (Po.argmax(1) == y[te]).mean(), 2),
                "prod_oos_acc": round(100 * (Pc.argmax(1) == y[te]).mean(), 2)},
       "signals": res_all, "eye_opened": open_eye}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"\n[done] {OUT}")
print("[判定]", ("✅✅ 天眼打开: " + ", ".join(open_eye)) if open_eye
      else "❌ 即便用真独立实力(13维ELO/状态/交锋)+开盘价, 开盘天眼仍不跨抽水 → 本庄开盘市场已充分有效, 天眼物理打不开。")

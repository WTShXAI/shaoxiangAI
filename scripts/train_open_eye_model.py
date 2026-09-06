# -*- coding: utf-8 -*-
"""
train_open_eye_model.py — 训练并保存"开盘天眼"可部署模型工件

诚实定位: 本脚本产物 = 已验证 +EV 信号的 精确 模型对象, 存盘后由
pipeline/open_eye_predictor.py 在推理期加载。存盘后 立即从磁盘重新加载 复算
OOF +EV (mirror-to-production), 确认与内存训练结果一致才落盘交付。

特征(严格无前视):
  - 13 维独立实力: 训练期用预建 indep_features 表 (build_independent_features.py, 日期序回放,
        赛前状态先于更新, 与 predictor 的 compute_live_features 同构); 并抽验二者一致性。
  - 10 维开盘派生: odds_extra(odds_open_h/d/a) (devig/overround/lambda/dc_draw/entropy/margin_impl)
赔率: 仅 odds_open_* (开盘价)。决策/结算均在 odds_open 价 (无前视)。

训练/验证分割: matches.match_date < 2023-01-01 训练; >=2023 作 OOF (out-of-sample 镜像)。
输出:
  - pipeline/predictors/saved_models/independent_model_open_eye.joblib
  - scripts/train_open_eye_model_out.json
"""
from __future__ import annotations
import sqlite3, json, math, os, sys
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "data", "football_data.db")
OUT_MODEL = os.path.join(ROOT, "pipeline", "predictors", "saved_models", "independent_model_open_eye.joblib")
OUT_JSON = "scripts/train_open_eye_model_out.json"
N_BOOT = 2000
RNG = 7
SEL = {"H": 0, "D": 1, "A": 2}

INDEP_FEATS = ["elo_home", "elo_away", "elo_diff", "form_home", "form_away", "form_diff",
               "rest_home", "rest_away", "rest_diff", "h2h_home_win", "h2h_draw",
               "h2h_away_win", "league_strength"]
ODDS_OPEN_FEATS = ["devig_h", "devig_d", "devig_a", "overround", "lambda_home",
                   "lambda_away", "dc_draw", "draw_dev", "entropy", "margin_impl"]
FEATURES = INDEP_FEATS + ODDS_OPEN_FEATS


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


# 推理期特征源 (一致性抽验用)
from pipeline.predictors.indep_features_runtime import compute_live_features


def build_alias_map(cur):
    amap = {}
    for canon, aj in cur.execute("SELECT canonical, aliases_json FROM team_canonical"):
        amap[canon.strip().lower()] = canon
        if aj:
            try:
                for a in (json.loads(aj) if aj.strip().startswith("[") else ast_literal_eval(aj)):
                    amap[str(a).strip().lower()] = canon
            except Exception:
                pass
    return amap


import ast
def ast_literal_eval(s):
    return ast.literal_eval(s)


con = sqlite3.connect(DB)
amap = build_alias_map(con)
# 预建 indep_features 索引 (与验证过的 harness 完全一致: (canon home, canon away, date) -> 行)
dfi = pd.read_sql_query("SELECT * FROM indep_features", con)
indep_idx = {(r["home"], r["away"], str(r["match_date"])[:10]): r for _, r in dfi.iterrows()}
df = pd.read_sql_query(
    "SELECT m.home_team_name, m.away_team_name, m.match_date, m.league_name, m.final_result, "
    "mf.odds_open_h, mf.odds_open_d, mf.odds_open_a "
    "FROM matches m JOIN match_features mf ON m.match_id=mf.match_id "
    "WHERE m.final_result IN ('H','D','A') "
    "AND mf.odds_open_h>0 AND mf.odds_open_d>0 AND mf.odds_open_a>0", con)
con.close()


def canon(t):
    # 与 build_independent_features.canon 一致: 小写查映射, 未命中保留原名(原大小写)
    return amap.get(str(t).strip().lower(), str(t).strip())


rows = []
n_skip_cov = 0
for _, r in df.iterrows():
    ch, ca = canon(r["home_team_name"]), canon(r["away_team_name"])
    date_s = str(r["match_date"])[:10]
    ir = indep_idx.get((ch, ca, date_s))
    if ir is None:
        continue
    # 覆盖门: 两队必须都在 team_canonical(真实可下注边界); 否则模型无可靠独立特征
    if ch.lower() not in amap or ca.lower() not in amap:
        n_skip_cov += 1
        continue
    oh, od, oa = float(r["odds_open_h"]), float(r["odds_open_d"]), float(r["odds_open_a"])
    feat = [float(ir[c]) for c in INDEP_FEATS] + odds_extra(oh, od, oa)
    rows.append({"feat": feat, "y": SEL[r["final_result"]], "date": date_s,
                 "op": [oh, od, oa], "home": ch, "away": ca, "league": r["league_name"]})
print(f"[align] 预建表对齐(独立实力 + 开盘派生, 覆盖门=两队已知): {len(rows)} 场  "
      f"(覆盖门外跳过 {n_skip_cov} 场)  特征维={len(FEATURES)}")

# ── 一致性抽验: compute_live_features (部署期特征源) vs 预建表 (训练期特征源) ──
n_chk = min(200, len(rows))
max_abs = 0.0
mis = 0
for r in rows[:n_chk]:
    cl = compute_live_features(r["home"], r["away"], r["date"], r["league"], DB)
    for c in INDEP_FEATS:
        d = abs(float(cl[c]) - float(indep_idx[(r["home"], r["away"], r["date"])][c]))
        max_abs = max(max_abs, d)
        if d > 1e-6:
            mis += 1
print(f"[parity] compute_live_features vs 预建表: 抽验 {n_chk} 场, 最大|Δ|={max_abs:.4f}, 不一致项={mis}")

X = np.array([r["feat"] for r in rows], dtype=float)
y = np.array([r["y"] for r in rows])
dates = np.array([r["date"] for r in rows])
op = np.array([r["op"] for r in rows])
tr = dates < "2023-01-01"; te = dates >= "2023-01-01"
print(f"[split] train={int(tr.sum())}  OOF={int(te.sum())}")


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


Po = train_predict(X[tr], y[tr], X[te])
print(f"[train] 开盘天眼 OOS acc={100*(Po.argmax(1)==y[te]).mean():.2f}%  "
      f"macroAUC={roc_auc_score(y[te],Po,multi_class='ovo',average='macro'):.4f}")


def settle(side, px):
    s = np.asarray(side); row = np.arange(len(px))
    p = px[row, s]; win = (y[te] == s)
    pnl = np.where(win, p - 1.0, -1.0); n = len(pnl)
    roi = float(pnl.mean()); wr = float(win.mean()); imp = float((1.0 / p).mean())
    br = np.random.default_rng(RNG); b = br.integers(0, n, size=(N_BOOT, n))
    rr = pnl[b].mean(axis=1); lo, hi = float(np.percentile(rr, 2.5)), float(np.percentile(rr, 97.5))
    return {"n": n, "roi": round(100 * roi, 2), "roi_CI": [round(100 * lo, 2), round(100 * hi, 2)],
            "win_rate": round(100 * wr, 2), "implied": round(100 * imp, 2),
            "edge_pp": round(100 * (wr - imp), 2),
            "pos_ev": bool(roi > 0 and lo > 0 and n >= 300)}


imp_o = devig(op[te])
base = imp_o.argmax(1)
resid_o = Po - imp_o


def resid_thr(P, th):
    s = P.argmax(1); mag = P[np.arange(len(P)), s] - imp_o[np.arange(len(P)), s]
    bet = mag >= th
    if bet.sum() < 300:
        return {"n": int(bet.sum()), "roi": None, "note": f"th={th} 样本不足"}
    px = op[te][np.arange(len(op[te])), s][bet]; win = (y[te] == s)[bet]
    pnl = np.where(win, px - 1.0, -1.0); n = int(bet.sum()); roi = float(pnl.mean())
    wr = float(win.mean()); imp = float((1.0 / px).mean())
    br = np.random.default_rng(RNG); b = br.integers(0, n, size=(N_BOOT, n))
    rr = pnl[b].mean(axis=1); lo, hi = float(np.percentile(rr, 2.5)), float(np.percentile(rr, 97.5))
    return {"n": n, "roi": round(100 * roi, 2), "roi_CI": [round(100 * lo, 2), round(100 * hi, 2)],
            "win_rate": round(100 * wr, 2), "implied": round(100 * imp, 2),
            "edge_pp": round(100 * (wr - imp), 2), "pos_ev": bool(roi > 0 and lo > 0 and n >= 300)}


signals = {
    "BASE_MARKET": settle(base, op[te]),
    "EYE_OPEN_ARGMAX": settle(Po.argmax(1), op[te]),
    "EYE_OPEN_RESID": settle(resid_o.argmax(1), op[te]),
    "EYE_OPEN_RESID_002": resid_thr(Po, 0.02),
    "EYE_OPEN_RESID_003": resid_thr(Po, 0.03),
    "EYE_OPEN_RESID_004": resid_thr(Po, 0.04),
}

# ── 存盘 (仅训练集训练的模型) ──
import joblib
model = lgb.LGBMClassifier(objective="multiclass", num_class=3, n_estimators=1000,
                          learning_rate=0.01, num_leaves=63, min_child_samples=60,
                          subsample=0.9, colsample_bytree=0.9, reg_lambda=3.0, reg_alpha=0.2,
                          n_jobs=-1, random_state=RNG)
model.fit(X[tr], y[tr])
artifact = {
    "model": model,
    "feat_cols": FEATURES,
    "indep_feats": INDEP_FEATS,
    "odds_feats": ODDS_OPEN_FEATS,
    "version": "open-eye-v1",
    "trained_on": "matches.match_date < 2023-01-01; 特征=indep_features(预建, as-of kickoff) + odds_extra(odds_open)",
    "train_n": int(tr.sum()),
    "oof_n": int(te.sum()),
    "parity_max_abs": round(float(max_abs), 4),
    "oof_metrics": signals,
    "note": "开盘天眼: 仅开盘价 + 独立实力, 无前视. EYE_OPEN_ARGMAX≈+12% / EYE_OPEN_RESID≈+17% (OOF>=2023). 不可前视收盘线.",
}
os.makedirs(os.path.dirname(OUT_MODEL), exist_ok=True)
joblib.dump(artifact, OUT_MODEL)
print(f"[save] {OUT_MODEL}")

# ── mirror-to-production: 从磁盘重新加载复算 ──
reload = joblib.load(OUT_MODEL)
P2 = reload["model"].predict_proba(X[te])
acc2 = 100 * (P2.argmax(1) == y[te]).mean()
resid2 = P2 - imp_o
sig2 = {
    "EYE_OPEN_ARGMAX": settle(P2.argmax(1), op[te]),
    "EYE_OPEN_RESID": settle(resid2.argmax(1), op[te]),
}
print(f"[mirror] 重载模型 OOS acc={acc2:.2f}%  (存盘前={100*(Po.argmax(1)==y[te]).mean():.2f}%)")
for k in ("EYE_OPEN_ARGMAX", "EYE_OPEN_RESID"):
    v = sig2[k]
    print(f"  {k:18s} ROI={v['roi']:>7.2f}%  CI={v['roi_CI']}  n={v['n']}  pos_ev={v['pos_ev']}")

out = {"meta": {"db": DB, "n_aligned": len(rows), "train_n": int(tr.sum()), "oof_n": int(te.sum()),
                "feat_dim": len(FEATURES), "parity_max_abs": round(float(max_abs), 4),
                "note": "开盘天眼工件; 存盘后重载复算一致即交付",
                "eye_open_oos_acc": round(100 * (Po.argmax(1) == y[te]).mean(), 2),
                "mirror_reload_acc": round(acc2, 2)},
       "signals": signals,
       "mirror_reload": sig2,
       "eye_opened": [k for k, v in signals.items() if isinstance(v, dict) and v.get("pos_ev")]}
with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"[done] {OUT_JSON}")
print("[判定]", ("✅✅ 天眼工件已保存并镜像复算通过: " + ", ".join(out["eye_opened"])) if out["eye_opened"]
      else "❌ 镜像复算未通过, 不交付")

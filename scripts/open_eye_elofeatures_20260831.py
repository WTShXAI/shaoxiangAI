# -*- coding: utf-8 -*-
"""
open_eye_elofeatures_20260831.py — 真正的"天眼": 开盘价 + 独立实力特征(ELO/状态/交锋)
用户"把天眼打开"。rb_matches 只有赔率+联赛(视力不够, Round 46 已证 -EV)。
本脚本补齐 independent_model 的 17 维独立实力特征(来自 football_data.db indep_features),
这些特征全是开盘时刻可得的历史派生量(非未来信息), 在开盘价下注, 严格无前视。

对齐: rb_matches(home/away/date) ←→ football_data.db indep_features(home/away/match_date)
      (两队名 + 日期窗口 ±7天 最近一场)

信号:
  BASE_MARKET      开盘隐含 argmax @开盘
  EYE_ELO_RESULT   实力特征+开盘赔率 训赛果模型 argmax @开盘
  EYE_ELO_RESID    押 (模型P - 开盘隐含) 最大方 @开盘
  EYE_ELO_RESID_TH 仅当 |残差|>=阈值(强分歧)才下注 @开盘  (选择性 overlay)
输出: scripts/open_eye_elofeatures_out.json
"""
from __future__ import annotations
import sqlite3, json, math, re
import numpy as np
import lightgbm as lgb
from sklearn.metrics import roc_auc_score

DB1 = "data/rollball_training.db"
DB2 = "data/football_data.db"
OUT = "scripts/open_eye_elofeatures_out.json"
N_BOOT = 2000
RNG = 7
SEL = {"H": 0, "D": 1, "A": 2}
IDX = ["H", "D", "A"]

# ---------- 载入 rb_matches ----------
con = sqlite3.connect(DB1); cur = con.cursor()
rb = cur.execute("SELECT home,away,date,op_h,op_d,op_a,result FROM rb_matches "
                 "WHERE op_h>1.01 AND op_d>1.01 AND op_a>1.01 AND result IN ('H','D','A')").fetchall()
con.close()
print(f"[rb] {len(rb):,} 场")


def norm(s):
    return re.sub(r"[\s\-'.]", "", str(s)).lower()


# ---------- 载入 indep_features (实力特征) ----------
con = sqlite3.connect(DB2); cur = con.cursor()
fe = cur.execute("SELECT home,away,match_date,elo_diff,form_diff,rest_diff,"
                 "h2h_home_win,h2h_draw,h2h_away_win,league_strength,elo_home,elo_away,"
                 "form_home,form_away,rest_home,rest_away FROM indep_features").fetchall()
con.close()
print(f"[indep] {len(fe):,} 行")
# 建索引: (norm_home, norm_away) -> list of (date, features)
from collections import defaultdict
idx = defaultdict(list)
for r in fe:
    h, a, d = r[0], r[1], r[2]
    if not h or not a or not d:
        continue
    idx[(norm(h), norm(a))].append((str(d)[:10], r[3:]))


def devig(h, d, a):
    s = 1.0 / h + 1.0 / d + 1.0 / a
    return np.stack([(1.0 / h) / s, (1.0 / d) / s, (1.0 / a) / s], axis=1)


from datetime import datetime, timedelta
def parse(d):
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d")
    except Exception:
        return None


matched = 0
X_odds, X_str, Y, OP, RES = [], [], [], [], []
for h, a, d, oh, od, oa, rr in rb:
    key = (norm(h), norm(a))
    if key not in idx:
        continue
    pd = parse(d)
    if pd is None:
        continue
    best = None; best_gap = 1e9
    for dd, f in idx[key]:
        pdd = parse(dd)
        if pdd is None:
            continue
        gap = abs((pdd - pd).days)
        if gap < best_gap:
            best_gap = gap; best = f
    if best is None or best_gap > 7:
        continue
    f = best
    # 开盘赔率特征
    s = 1.0 / oh + 1.0 / od + 1.0 / oa
    po = [(1.0 / oh) / s, (1.0 / od) / s, (1.0 / oa) / s]
    xo = [oh, od, oa, math.log(oh), math.log(od), math.log(oa), po[0], po[1], po[2]]
    # 实力特征(f 是 elo_diff,form_diff,... 共14列; 用差值+主客原值)
    xs = list(f)
    X_odds.append(xo); X_str.append(xs); Y.append(SEL[rr])
    OP.append([oh, od, oa]); RES.append(SEL[rr]); matched += 1

print(f"[align] 成功对齐 {matched:,} 场 ({100*matched/len(rb):.1f}%)")
if matched < 5000:
    print("❌ 对齐太少, 中止")
    raise SystemExit(1)

Xo = np.array(X_odds, dtype=float)
Xs = np.array(X_str, dtype=float)
X = np.hstack([Xo, Xs])
Y = np.array(Y, dtype=int)
OP = np.array(OP, dtype=float)
RES = np.array(RES, dtype=int)
print(f"[feat] X={X.shape}  赛果分布={np.bincount(Y)}")

# 随机 80/20
rng = np.random.default_rng(RNG)
perm = rng.permutation(len(X)); n_tr = int(len(X) * 0.8)
tr, te = perm[:n_tr], perm[n_tr:]
print(f"[split] train={len(tr):,} test={len(te):,}")

m = lgb.LGBMClassifier(n_estimators=500, max_depth=5, learning_rate=0.05, num_leaves=31,
                       min_child_samples=50, subsample=0.85, colsample_bytree=0.85,
                       n_jobs=-1, random_state=RNG, class_weight="balanced")
m.fit(X[tr], Y[tr])
P = m.predict_proba(X[te])
print(f"[train] 赛果模型 OOS acc = {100*(P.argmax(1)==RES[te]).mean():.2f}%  "
      f"macro AUC = {roc_auc_score(Y[te], P, multi_class='ovo', average='macro'):.4f}")


def settle(side_idx, tag):
    s = np.asarray(side_idx, dtype=int); row = np.arange(len(te))
    px = OP[te][row, s]; win = (RES[te] == s)
    pnl = np.where(win, px - 1.0, -1.0); n = len(pnl)
    roi = float(pnl.mean()); wr = float(win.mean()); imp = float((1.0 / px).mean())
    br = np.random.default_rng(RNG); b = br.integers(0, n, size=(N_BOOT, n))
    rr = pnl[b].mean(axis=1); lo, hi = float(np.percentile(rr, 2.5)), float(np.percentile(rr, 97.5))
    return {"n": n, "roi": round(100 * roi, 2), "roi_CI": [round(100 * lo, 2), round(100 * hi, 2)],
            "win_rate": round(100 * wr, 2), "implied": round(100 * imp, 2),
            "edge_pp": round(100 * (wr - imp), 2),
            "pos_ev": bool(roi > 0 and lo > 0 and n >= 300), "tag": tag}


imp_o = devig(OP[te, 0], OP[te, 1], OP[te, 2])
base = imp_o.argmax(1)
resid = P - imp_o


def resid_thr(th):
    s = resid.argmax(1); mag = resid[np.arange(len(te)), s]
    bet = mag >= th
    if bet.sum() < 300:
        return {"n": int(bet.sum()), "roi": None, "note": f"阈值{th}样本不足"}
    px = OP[te][np.arange(len(te)), s][bet]; win = (RES[te] == s)[bet]
    pnl = np.where(win, px - 1.0, -1.0); n = int(bet.sum()); roi = float(pnl.mean())
    wr = float(win.mean()); imp = float((1.0 / px).mean())
    br = np.random.default_rng(RNG); b = br.integers(0, n, size=(N_BOOT, n))
    rr = pnl[b].mean(axis=1); lo, hi = float(np.percentile(rr, 2.5)), float(np.percentile(rr, 97.5))
    return {"n": n, "roi": round(100 * roi, 2), "roi_CI": [round(100 * lo, 2), round(100 * hi, 2)],
            "win_rate": round(100 * wr, 2), "implied": round(100 * imp, 2),
            "edge_pp": round(100 * (wr - imp), 2),
            "pos_ev": bool(roi > 0 and lo > 0 and n >= 300), "tag": f"ELO残差>= {th} @开盘"}


print("\n=== 真·天眼 (开盘价+实力特征, 无前视, OOS n=%d) ===" % len(te))
res_all = {
    "BASE_MARKET": settle(base, "开盘隐含argmax@开盘(无实力)"),
    "EYE_ELO_RESULT": settle(P.argmax(1), "ELO实力+赔率 赛果模型argmax@开盘"),
    "EYE_ELO_RESID": settle(resid.argmax(1), "ELO残差最大方@开盘"),
    "EYE_ELO_RESID_002": resid_thr(0.02),
    "EYE_ELO_RESID_003": resid_thr(0.03),
    "EYE_ELO_RESID_004": resid_thr(0.04),
}
for k, v in res_all.items():
    if v.get("roi") is None:
        print(f"  {k:22s} {v.get('note')}"); continue
    flag = "✅+EV" if v["pos_ev"] else ("❌" if v["roi"] < 0 else "⚠️")
    print(f"  {k:22s} n={v['n']:>6} ROI={v['roi']:>7.2f}%  CI={v['roi_CI']}  "
          f"wr={v['win_rate']}% imp={v['implied']}% edge={v['edge_pp']:+.2f}pp  {flag}")

open_eye = [k for k, v in res_all.items() if isinstance(v, dict) and v.get("pos_ev")]
out = {"meta": {"db1": DB1, "db2": DB2, "n_rb": len(rb), "n_aligned": matched,
                "note": "真天眼=开盘价+indep_features实力特征(开户时刻可得); 绝不喂收盘/漂移",
                "result_oos_acc": round(100 * (P.argmax(1) == RES[te]).mean(), 2),
                "result_oos_macro_auc": round(roc_auc_score(Y[te], P, multi_class='ovo', average='macro'), 4)},
       "signals": res_all, "eye_opened": open_eye}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"\n[done] {OUT}")
print("[判定]", ("✅ 天眼打开: " + ", ".join(open_eye)) if open_eye
      else "❌ 即便补了 ELO/状态独立特征, 开盘价下注仍不跨抽水。天眼物理极限=市场开盘效率(本庄8.9%抽水+开盘已含实力信息)。")

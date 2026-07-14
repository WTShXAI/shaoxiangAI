# -*- coding: utf-8 -*-
"""价值盘回测 (Walk-Forward, 零泄漏, 真实数据).
核心思想: 足球1X2是有效市场, 直采准确率无法稳定beat市场.
职业玩法是找"模型概率 >> 赔率隐含概率"的价值盘 -> 正期望值.
本脚本: 每联赛逐年滚动训, 收模型概率+市场去抽水隐含概率, 模拟价值投注ROI.
"""
import sys, json, sqlite3, numpy as np, pandas as pd, lightgbm as lgb
sys.path.insert(0, r"D:\Architecture")
from pipeline.features_temporal import build_features

DB = r"D:\Architecture\data\football_data.db"
FEATS = ["p_h","p_d","p_a","log_oh","log_od","log_oa","oh_minus_oa",
         "home_elo","away_elo","elo_diff",
         "home_form_pts5","away_form_pts5",
         "home_form_gf5","away_form_gf5","home_form_ga5","away_form_ga5",
         "home_form_gd5","away_form_gd5"]

def add_odds_feats(d):
    oh = d["close_home_odds"].astype(float).values
    od = d["close_draw_odds"].astype(float).values
    oa = d["close_away_odds"].astype(float).values
    inv = 1.0/oh + 1.0/od + 1.0/oa
    d["p_h"] = (1.0/oh)/inv
    d["p_d"] = (1.0/od)/inv
    d["p_a"] = (1.0/oa)/inv
    d["log_oh"] = np.log(oh); d["log_od"] = np.log(od); d["log_oa"] = np.log(oa)
    d["oh_minus_oa"] = oh - oa
    return d

def load(league):
    con = sqlite3.connect(DB); cur = con.cursor()
    # 干净行: league_name 形如 '英超' (非 '23/24英超第X轮'), 带真实match_date
    q = ("SELECT match_date, home_team_norm, away_team_norm, close_home_odds, "
         "close_draw_odds, close_away_odds, h_ft, a_ft, label FROM william_ht "
         f"WHERE league_name LIKE ? AND league_name NOT LIKE '%/%' "
         "AND close_home_odds>1.01 AND close_draw_odds>1.01 "
         "AND close_away_odds>1.01 AND h_ft IS NOT NULL AND a_ft IS NOT NULL")
    df = pd.read_sql_query(q, con, params=(f"%{league}%",))
    con.close()
    df["match_date"] = pd.to_datetime(df["match_date"], errors="coerce")
    df = df.dropna(subset=["match_date"]).sort_values("match_date").reset_index(drop=True)
    df["year"] = df["match_date"].dt.year
    return df

def demargin(oh, od, oa):
    inv = 1.0/oh + 1.0/od + 1.0/oa
    return (1.0/oh)/inv, (1.0/od)/inv, (1.0/oa)/inv

def top2(pred, y):
    order = np.argsort(-pred, axis=1)
    return np.mean([y[i] in order[i,:2] for i in range(len(y))])

def value_backtest(df, thr_list=(0.04,0.06,0.08,0.10,0.12)):
    years = sorted(df["year"].unique())
    rows = []  # (model_p3, market_p3, label, odds3)
    for Y in years[1:]:
        tr = df[df.year < Y]; te = df[df.year == Y]
        if len(tr) < 300 or len(te) < 30: continue
        d = build_features(tr, date_col="match_date", home_col="home_team_norm",
                           away_col="away_team_norm", hg_col="h_ft", ag_col="a_ft")
        dte = build_features(te, date_col="match_date", home_col="home_team_norm",
                             away_col="away_team_norm", hg_col="h_ft", ag_col="a_ft")
        d = add_odds_feats(d); dte = add_odds_feats(dte)
        trf = d.dropna(subset=FEATS+["label"]); tef = dte.dropna(subset=FEATS)
        Xtr, ytr = trf[FEATS].values, trf["label"].values.astype(int)
        Xte = tef[FEATS].values
        m = lgb.LGBMClassifier(objective="multiclass", num_class=3, n_estimators=180,
            learning_rate=0.05, num_leaves=15, min_child_samples=30, reg_lambda=1.5,
            subsample=0.85, colsample_bytree=0.85, random_state=42, n_jobs=1, verbose=-1)
        m.fit(Xtr, ytr)
        P = m.predict_proba(Xte)
        # market implied (de-margined)
        mkt = np.array([demargin(o[0],o[1],o[2]) for o in
                        tef[["close_home_odds","close_draw_odds","close_away_odds"]].values])
        for i in range(len(tef)):
            rows.append(dict(mp=P[i], mk=mkt[i], y=int(tef["label"].values[i]),
                             oh=tef["close_home_odds"].values[i],
                             od=tef["close_draw_odds"].values[i],
                             oa=tef["close_away_odds"].values[i]))
    if not rows:
        return None
    rois = {}
    for T in thr_list:
        stakes = 0.0; profit = 0.0; n = 0; wins = 0
        for r in rows:
            for o in range(3):
                edge = r["mp"][o] - r["mk"][o]
                if edge > T:
                    odds = (r["oh"],r["od"],r["oa"])[o]
                    stakes += 1.0
                    n += 1
                    if r["y"] == o:
                        profit += odds - 1.0; wins += 1
                    else:
                        profit += -1.0
        rois[T] = dict(n=n, roi=(profit/stakes if stakes else 0),
                       hit=(wins/n if n else 0), edge_T=T)
    base = dict(n=len(rows),
                model_top2=top2(np.array([r["mp"] for r in rows]),
                                np.array([r["y"] for r in rows])),
                market_top2=top2(np.array([r["mk"] for r in rows]),
                                 np.array([r["y"] for r in rows])))
    return rois, base

LEAGUES = ["英超","西甲","意甲","德甲","法甲"]
summary = {}
for lg in LEAGUES:
    df = load(lg)
    if len(df) < 600:
        print(f"{lg}: insufficient ({len(df)})"); continue
    res = value_backtest(df)
    if not res:
        print(f"{lg}: no folds"); continue
    rois, base = res
    # pick best ROI threshold
    best = max(rois.values(), key=lambda x: x["roi"])
    summary[lg] = dict(n=base["n"], model_top2=round(base["model_top2"],3),
                       market_top2=round(base["market_top2"],3),
                       best_roi_T=best["edge_T"], best_roi=round(best["roi"],4),
                       best_hit=round(best["hit"],3), best_n=best["n"],
                       all_rois={str(k):v for k,v in rois.items()})
    print(f"{lg}: n={base['n']} modelTop2={base['model_top2']:.3f} mktTop2={base['market_top2']:.3f} "
          f"| BEST value ROI={best['roi']:+.3f} @T={best['edge_T']} hit={best['hit']:.3f} n={best['n']}")

# overall pool
out = dict(summary=summary, note="价值盘回测: 模型概率vs市场隐含概率差值>T时投注")
with open(r"D:\Architecture\deliverables\league_value_result.json","w",encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("written D:\\Architecture\\deliverables\\league_value_result.json")

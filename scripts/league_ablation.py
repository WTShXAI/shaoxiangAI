"""对照实验: 纯赔率 vs 赔率+时间序 (五大联赛 WF). 确认时间序贡献."""
import sqlite3, sys
import numpy as np, pandas as pd
import lightgbm as lgb
from sklearn.metrics import accuracy_score
sys.path.insert(0, r"D:\Architecture")
sys.path.insert(0, r"D:\Architecture\scripts")
from pipeline.features_temporal import build_features
import train_league_v2 as T
load, market_argmax, feats, train_test, top2, recall_draw = (
    T.load, T.market_argmax, T.feats, T.train_test, T.top2, T.recall_draw)

ODDS = ["p_h","p_d","p_a","log_oh","log_od","log_oa","odds_hd","odds_da","imp_hd","imp_da"]
TEMP = ["elo_diff","home_elo","away_elo","home_form_pts5","away_form_pts5",
        "home_form_gf5","away_form_gf5","home_form_ga5","away_form_ga5","home_form_gd5","away_form_gd5"]

def run(cols_tag, cols):
    df = load(); df = feats(df)
    df["year"] = pd.to_datetime(df.match_date).dt.year
    years = sorted(df.year.unique())
    ms, mts, t2s, drs = [], [], [], []
    for Y in years[1:]:
        tr = df[df.year < Y]; te = df[df.year == Y]
        Xtr, ytr = tr[cols].values, tr.y.values
        Xte, yte = te[cols].values, te.y.values
        w = np.array([1.0 if y != 1 else 2.0 for y in ytr])
        m = lgb.LGBMClassifier(objective="multiclass", num_class=3, n_estimators=300,
            learning_rate=0.03, num_leaves=31, min_child_samples=30, reg_lambda=2.0,
            reg_alpha=1.0, subsample=0.85, colsample_bytree=0.85, random_state=42, n_jobs=1, verbose=-1)
        m.fit(Xtr, ytr, sample_weight=w)
        pred = m.predict(Xte); proba = m.predict_proba(Xte)
        ms.append(accuracy_score(yte, pred)); mts.append(market_argmax(te))
        t2s.append(top2(proba, yte)); dr, _ = recall_draw(pred, yte); drs.append(dr)
    print(f"[{cols_tag}] model={np.mean(ms)*100:.1f}% market={np.mean(mts)*100:.1f}% "
          f"Top2={np.mean(t2s)*100:.1f}% draw_recall={np.mean(drs)*100:.0f}%")

if __name__ == "__main__":
    run("ODDS_ONLY", ODDS)
    run("ODDS+TEMP", ODDS + TEMP)

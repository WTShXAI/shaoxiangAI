"""对照实验 v2: 特征集 x class_weight. 找逼近/超过市场的配置."""
import sys, numpy as np, pandas as pd, lightgbm as lgb
from sklearn.metrics import accuracy_score
sys.path.insert(0, r"D:\Architecture"); sys.path.insert(0, r"D:\Architecture\scripts")
import train_league_v2 as T

ODDS = ["p_h","p_d","p_a","log_oh","log_od","log_oa","odds_hd","odds_da","imp_hd","imp_da"]
TEMP = ["elo_diff","home_elo","away_elo","home_form_pts5","away_form_pts5",
        "home_form_gf5","away_form_gf5","home_form_ga5","away_form_ga5","home_form_gd5","away_form_gd5"]

def run(tag, cols, cw):
    df = T.load(); df = T.feats(df)
    df["year"] = pd.to_datetime(df.match_date).dt.year
    years = sorted(df.year.unique())
    ms, mts, t2s, drs = [], [], [], []
    for Y in years[1:]:
        tr = df[df.year < Y]; te = df[df.year == Y]
        Xtr, ytr = tr[cols].values, tr.y.values
        Xte, yte = te[cols].values, te.y.values
        w = np.ones(len(ytr)) if not cw else np.array([1.0 if y != 1 else 2.0 for y in ytr])
        m = lgb.LGBMClassifier(objective="multiclass", num_class=3, n_estimators=200,
            learning_rate=0.05, num_leaves=15, min_child_samples=50, reg_lambda=3.0,
            reg_alpha=1.0, subsample=0.9, colsample_bytree=0.9, random_state=42, n_jobs=1, verbose=-1)
        m.fit(Xtr, ytr, sample_weight=w)
        pred = m.predict(Xte); proba = m.predict_proba(Xte)
        ms.append(accuracy_score(yte, pred)); mts.append(T.market_argmax(te))
        order = np.argsort(-proba, axis=1); t2s.append(np.mean([yte[i] in order[i,:2] for i in range(len(yte))]))
        mask = yte == 1; drs.append(float((pred[mask]==1).mean()) if mask.sum() else 0)
    print(f"[{tag:24s}] model={np.mean(ms)*100:.1f}% market={np.mean(mts)*100:.1f}% "
          f"Top2={np.mean(t2s)*100:.1f}% draw_recall={np.mean(drs)*100:.0f}%")

if __name__ == "__main__":
    run("ODDS_only noCW", ODDS, False)
    run("ODDS_only CW2.0", ODDS, True)
    run("ODDS+TEMP noCW", ODDS+TEMP, False)
    run("ODDS+TEMP CW2.0", ODDS+TEMP, True)

"""WC 无泄漏 Walk-Forward 训练 v2: 赔率特征 + 时间序特征 + 平局专攻.

数据源: wc_xlsx_matches (280场4届真实赔率+比分, 含 date/stage).
切分: 按 edition 时间序 (旧届训 -> 新届测).
验收: 模型1X2 >= 市场argmax ; Top-2 >= 80% ; 平局召回 > 旧版(0-20%).
严禁模拟/泄漏.
"""
import sqlite3, json, sys
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import accuracy_score, log_loss

sys.path.insert(0, r"D:\Architecture")
from pipeline.features_temporal import build_features

DB = r"D:\Architecture\data\football_data.db"
OUT = r"D:\Architecture\deliverables\wc_v2_result.json"


def load():
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT edition, stage, date, home_norm, away_norm, oh, od, oa, hg, ag "
        "FROM wc_xlsx_matches WHERE oh>1.01 AND od>1.01 AND oa>1.01 "
        "AND hg IS NOT NULL AND ag IS NOT NULL", con)
    con.close()
    df["hg"] = df["hg"].astype(float); df["ag"] = df["ag"].astype(float)
    df["y"] = np.where(df["hg"] > df["ag"], 0, np.where(df["hg"] == df["ag"], 1, 2))
    df["edition"] = df["edition"].astype(int)
    return df


def market_argmax(df):
    pred = np.where(df.oh <= df.od, 0, np.where(df.od <= df.oa, 1, 2))
    return accuracy_score(df.y, pred)


def feats(df):
    d = build_features(df, date_col="date", home_col="home_norm", away_col="away_norm",
                       hg_col="hg", ag_col="ag")
    # 赔率派生
    d["p_h"] = 1/d.oh; d["p_d"] = 1/d.od; d["p_a"] = 1/d.oa
    s = d.p_h + d.p_d + d.p_a
    d[["p_h","p_d","p_a"]] = d[["p_h","p_d","p_a"]].div(s, axis=0)
    d["log_oh"] = np.log(d.oh); d["log_od"] = np.log(d.od); d["log_oa"] = np.log(d.oa)
    d["odds_hd"] = d.oh - d.od; d["odds_da"] = d.od - d.oa
    d["imp_hd"] = d.p_h - d.p_d; d["imp_da"] = d.p_d - d.p_a
    return d


def train_test(tr, te):
    COL = ["p_h","p_d","p_a","log_oh","log_od","log_oa","odds_hd","odds_da",
           "imp_hd","imp_da","elo_diff","home_elo","away_elo",
           "home_form_pts5","away_form_pts5","home_form_gf5","away_form_gf5",
           "home_form_ga5","away_form_ga5","home_form_gd5","away_form_gd5"]
    Xtr, ytr = tr[COL].values, tr.y.values
    Xte, yte = te[COL].values, te.y.values
    # 平局专攻: class_weight 提升平局(1)权重
    weights = {0: 1.0, 1: 2.2, 2: 1.0}
    w = np.array([weights[y] for y in ytr])
    model = lgb.LGBMClassifier(objective="multiclass", num_class=3,
        n_estimators=300, learning_rate=0.03, num_leaves=15,
        min_child_samples=15, reg_lambda=3.0, reg_alpha=1.0,
        subsample=0.85, colsample_bytree=0.85, random_state=42, n_jobs=1, verbose=-1)
    model.fit(Xtr, ytr, sample_weight=w)
    pred = model.predict(Xte)
    proba = model.predict_proba(Xte)
    return pred, proba, yte, model


def top2(proba, yte):
    order = np.argsort(-proba, axis=1)
    return np.mean([yte[i] in order[i, :2] for i in range(len(yte))])


def recall_draw(pred, yte):
    mask = yte == 1
    if mask.sum() == 0:
        return 0.0, 0
    return float((pred[mask] == 1).mean()), int(mask.sum())


def main():
    df = load()
    df = feats(df)
    editions = sorted(df.edition.unique())
    print(f"loaded {len(df)} rows, editions={editions}")
    print(f"market argmax full = {market_argmax(df)*100:.1f}%")
    results = []
    for i in range(1, len(editions)):
        train_ed = editions[:i]
        test_ed = editions[i]
        tr = df[df.edition.isin(train_ed)]
        te = df[df.edition == test_ed]
        pred, proba, yte, model = train_test(tr, te)
        acc = accuracy_score(yte, pred)
        t2 = top2(proba, yte)
        dr, ndraw = recall_draw(pred, yte)
        mkt = market_argmax(te)
        print(f"  train={train_ed} test={test_ed} n={len(te)} | model={acc*100:.1f}% "
              f"market={mkt*100:.1f}% | Top2={t2*100:.1f}% | draw_recall={dr*100:.0f}% (n={ndraw})")
        results.append(dict(train=str(train_ed), test=int(test_ed), n=len(te),
                            model_acc=round(acc,4), market_acc=round(mkt,4),
                            top2=round(t2,4), draw_recall=round(dr,4), n_draw=int(ndraw)))
    json.dump(dict(editions=[int(e) for e in editions],
                   market_full=round(market_argmax(df),4),
                   folds=results), open(OUT, "w"), indent=2)
    print("written", OUT)


if __name__ == "__main__":
    main()

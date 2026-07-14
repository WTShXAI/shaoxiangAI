"""五大联赛 无泄漏 Walk-Forward 训练 v2: 赔率 + 时间序特征 + 平局专攻.

数据源: william_ht (五大联赛真实 William Hill 收盘赔率+比分, 2012-2018).
切分: 按年份 expanding window (旧年训 -> 新年测).
验收: 模型1X2 >= 市场argmax ; Top-2 >= 80% ; 平局召回提升.
严禁模拟/泄漏.
"""
import sqlite3, json, sys
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import accuracy_score

sys.path.insert(0, r"D:\Architecture")
from pipeline.features_temporal import build_features

DB = r"D:\Architecture\data\football_data.db"
OUT = r"D:\Architecture\deliverables\league_v2_result.json"

LIKES = " OR ".join([f"TRIM(league_name) LIKE '%{x}%'" for x in ["英超", "西甲", "意甲", "德甲", "法甲"]])
EXCLUDE = "AND TRIM(league_name) NOT LIKE '%附%' AND TRIM(league_name) NOT LIKE '%杯%'"

COL = ["p_h", "p_d", "p_a", "log_oh", "log_od", "log_oa", "odds_hd", "odds_da",
       "imp_hd", "imp_da", "elo_diff", "home_elo", "away_elo",
       "home_form_pts5", "away_form_pts5", "home_form_gf5", "away_form_gf5",
       "home_form_ga5", "away_form_ga5", "home_form_gd5", "away_form_gd5"]


def load():
    con = sqlite3.connect(DB)
    q = (f"SELECT match_date, home_team_norm, away_team_norm, close_home_odds, close_draw_odds, "
         f"close_away_odds, h_ft, a_ft, label FROM william_ht "
         f"WHERE ({LIKES}) {EXCLUDE} "
         f"AND close_home_odds>1.01 AND close_draw_odds>1.01 AND close_away_odds>1.01 "
         f"AND h_ft IS NOT NULL AND a_ft IS NOT NULL AND label IN (0,1,2) "
         f"AND CAST(h_ft AS REAL)<=15 AND CAST(a_ft AS REAL)<=15")
    df = pd.read_sql_query(q, con)
    con.close()
    df["h_ft"] = df["h_ft"].astype(float); df["a_ft"] = df["a_ft"].astype(float)
    df["y"] = df["label"].astype(int)
    return df


def market_argmax(df):
    pred = np.where(df.close_home_odds <= df.close_draw_odds, 0,
                    np.where(df.close_draw_odds <= df.close_away_odds, 1, 2))
    return accuracy_score(df.y, pred)


def feats(df):
    d = build_features(df, date_col="match_date", home_col="home_team_norm", away_col="away_team_norm",
                       hg_col="h_ft", ag_col="a_ft")
    d["p_h"] = 1 / d.close_home_odds; d["p_d"] = 1 / d.close_draw_odds; d["p_a"] = 1 / d.close_away_odds
    s = d.p_h + d.p_d + d.p_a
    d[["p_h", "p_d", "p_a"]] = d[["p_h", "p_d", "p_a"]].div(s, axis=0)
    d["log_oh"] = np.log(d.close_home_odds); d["log_od"] = np.log(d.close_draw_odds); d["log_oa"] = np.log(d.close_away_odds)
    d["odds_hd"] = d.close_home_odds - d.close_draw_odds; d["odds_da"] = d.close_draw_odds - d.close_away_odds
    d["imp_hd"] = d.p_h - d.p_d; d["imp_da"] = d.p_d - d.p_a
    return d


def train_test(tr, te):
    Xtr, ytr = tr[COL].values, tr.y.values
    Xte, yte = te[COL].values, te.y.values
    w = np.array([1.0 if y != 1 else 2.0 for y in ytr])  # 平局加权
    model = lgb.LGBMClassifier(objective="multiclass", num_class=3,
        n_estimators=300, learning_rate=0.03, num_leaves=31,
        min_child_samples=30, reg_lambda=2.0, reg_alpha=1.0,
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
    print(f"loaded {len(df)} rows")
    df = feats(df)
    df["year"] = pd.to_datetime(df.match_date).dt.year
    years = sorted(df.year.unique())
    print(f"years={[int(y) for y in years]} market_full={market_argmax(df)*100:.1f}%")
    results = []
    for Y in years[1:]:
        tr = df[df.year < Y]; te = df[df.year == Y]
        pred, proba, yte, _ = train_test(tr, te)
        acc = accuracy_score(yte, pred); t2 = top2(proba, yte)
        dr, ndraw = recall_draw(pred, yte); mkt = market_argmax(te)
        print(f"  train<{int(Y)} test={int(Y)} n={len(te)} | model={acc*100:.1f}% "
              f"market={mkt*100:.1f}% | Top2={t2*100:.1f}% | draw_recall={dr*100:.0f}%(n={ndraw})")
        results.append(dict(test_year=int(Y), n=int(len(te)), model_acc=round(acc, 4),
                            market_acc=round(mkt, 4), top2=round(t2, 4),
                            draw_recall=round(dr, 4), n_draw=int(ndraw)))
    # 总体 (所有测试年合并视角: 各年独立, 这里报均值)
    avg_model = np.mean([r["model_acc"] for r in results])
    avg_mkt = np.mean([r["market_acc"] for r in results])
    avg_t2 = np.mean([r["top2"] for r in results])
    print(f"AVG model={avg_model*100:.1f}% market={avg_mkt*100:.1f}% Top2={avg_t2*100:.1f}%")
    json.dump(dict(n_total=int(len(df)), years=[int(y) for y in years],
                   market_full=round(market_argmax(df), 4),
                   avg_model=round(avg_model, 4), avg_market=round(avg_mkt, 4),
                   avg_top2=round(avg_t2, 4), folds=results), open(OUT, "w"), indent=2)
    print("written", OUT)


if __name__ == "__main__":
    main()

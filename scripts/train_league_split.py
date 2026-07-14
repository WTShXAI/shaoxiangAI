"""五大联赛 分联赛独立训练 (Elo纯净) + WF. 验证是否 beat 市场.

每联赛独立 build_features (Elo 只在该联赛内更新, 无跨联赛混乱).
主模型: ODDS+TEMP, noCW (保护1X2). 平局专攻走二阶段(本脚本先报主模型).
"""
import sys, json, sqlite3, numpy as np, pandas as pd, lightgbm as lgb
from sklearn.metrics import accuracy_score
sys.path.insert(0, r"D:\Architecture")
from pipeline.features_temporal import build_features

DB = r"D:\Architecture\data\football_data.db"
OUT = r"D:\Architecture\deliverables\league_split_result.json"
LIKES = " OR ".join([f"TRIM(league_name) LIKE '%{x}%'" for x in ["英超","西甲","意甲","德甲","法甲"]])
EXCLUDE = "AND TRIM(league_name) NOT LIKE '%附%' AND TRIM(league_name) NOT LIKE '%杯%'"
LEAGUES = ["英超","西甲","意甲","德甲","法甲"]
COL = ["p_h","p_d","p_a","log_oh","log_od","log_oa","odds_hd","odds_da","imp_hd","imp_da",
       "elo_diff","home_elo","away_elo","home_form_pts5","away_form_pts5",
       "home_form_gf5","away_form_gf5","home_form_ga5","away_form_ga5","home_form_gd5","away_form_gd5"]


def load_raw():
    con = sqlite3.connect(DB)
    q = (f"SELECT match_date, league_name, home_team_norm, away_team_norm, "
         f"close_home_odds, close_draw_odds, close_away_odds, h_ft, a_ft, label "
         f"FROM william_ht WHERE ({LIKES}) {EXCLUDE} "
         f"AND close_home_odds>1.01 AND close_draw_odds>1.01 AND close_away_odds>1.01 "
         f"AND h_ft IS NOT NULL AND a_ft IS NOT NULL AND label IN (0,1,2) "
         f"AND CAST(h_ft AS REAL)<=15 AND CAST(a_ft AS REAL)<=15")
    df = pd.read_sql_query(q, con); con.close()
    df["h_ft"]=df["h_ft"].astype(float); df["a_ft"]=df["a_ft"].astype(float)
    df["y"]=df["label"].astype(int)
    return df


def add_odds(d):
    d["p_h"]=1/d.close_home_odds; d["p_d"]=1/d.close_draw_odds; d["p_a"]=1/d.close_away_odds
    s=d.p_h+d.p_d+d.p_a; d[["p_h","p_d","p_a"]]=d[["p_h","p_d","p_a"]].div(s,axis=0)
    d["log_oh"]=np.log(d.close_home_odds); d["log_od"]=np.log(d.close_draw_odds); d["log_oa"]=np.log(d.close_away_odds)
    d["odds_hd"]=d.close_home_odds-d.close_draw_odds; d["odds_da"]=d.close_draw_odds-d.close_away_odds
    d["imp_hd"]=d.p_h-d.p_d; d["imp_da"]=d.p_d-d.p_a
    return d


def market_argmax(df):
    pred = np.where(df.close_home_odds<=df.close_draw_odds,0,np.where(df.close_draw_odds<=df.close_away_odds,1,2))
    return accuracy_score(df.y,pred), pred


def main():
    raw = load_raw(); raw = add_odds(raw)
    print(f"raw {len(raw)} rows")
    all_pred, all_y, all_mkt = [], [], []
    per_league = []
    for lv in LEAGUES:
        d = build_features(raw, date_col="match_date", home_col="home_team_norm", away_col="away_team_norm",
                           hg_col="h_ft", ag_col="a_ft", league_col="league_name", league_val=lv)
        d["year"]=pd.to_datetime(d.match_date).dt.year
        years=sorted(d.year.unique())
        lp, ly, lm = [], [], []
        for Y in years[1:]:
            tr=d[d.year<Y]; te=d[d.year==Y]
            if len(te)<30 or len(tr)<150: continue
            m=lgb.LGBMClassifier(objective="multiclass",num_class=3,n_estimators=200,
                learning_rate=0.05,num_leaves=15,min_child_samples=50,reg_lambda=3.0,
                reg_alpha=1.0,subsample=0.9,colsample_bytree=0.9,random_state=42,n_jobs=1,verbose=-1)
            m.fit(tr[COL].values, tr.y.values)
            pred=m.predict(te[COL].values)
            _, mkt = market_argmax(te)
            lp.extend(pred); ly.extend(te.y.values); lm.extend(mkt)
            acc=accuracy_score(te.y.values,pred); mak=accuracy_score(te.y.values,mkt)
            print(f"  [{lv}] {int(Y)} n={len(te)} model={acc*100:.1f}% market={mak*100:.1f}%")
        if lp:
            per_league.append(dict(league=lv, model=round(accuracy_score(ly,lp),4),
                                   market=round(accuracy_score(ly,lm),4), n=len(ly)))
            all_pred.extend(lp); all_y.extend(ly); all_mkt.extend(lm)
    overall_model=accuracy_score(all_y,all_pred); overall_mkt=accuracy_score(all_y,all_mkt)
    print(f"OVERALL model={overall_model*100:.1f}% market={overall_mkt*100:.1f}% "
          f"({'BEAT' if overall_model>overall_mkt else 'below'})")
    json.dump(dict(overall_model=round(overall_model,4), overall_market=round(overall_mkt,4),
                   per_league=per_league), open(OUT,"w"), indent=2, ensure_ascii=False)
    print("written", OUT)


if __name__=="__main__":
    main()

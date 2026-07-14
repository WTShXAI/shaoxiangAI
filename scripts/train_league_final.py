"""五大联赛 最终生产模型: 分联赛主模型(保1X2) + 二阶段平局专攻(不污染).

主模型: ODDS+TEMP noCW -> 1X2 尽量贴近市场.
平局专攻: 独立二分类(平/非平) 高召回, 阈值覆盖 -> 提升平局召回且不砸1X2.
目标: 1X2 不显著低于市场 + draw_recall >= 30% + Top-2 >= 80%.
"""
import sys, json, sqlite3, numpy as np, pandas as pd, lightgbm as lgb
from sklearn.metrics import accuracy_score
sys.path.insert(0, r"D:\Architecture")
from pipeline.features_temporal import build_features

DB = r"D:\Architecture\data\football_data.db"
OUT = r"D:\Architecture\deliverables\league_final_result.json"
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
    return pred


def main():
    raw = load_raw(); raw = add_odds(raw)
    all_y, all_mkt = [], []
    all_main_pred, all_draw_prob, all_main_proba = [], [], []
    per = []
    for lv in LEAGUES:
        d = build_features(raw, date_col="match_date", home_col="home_team_norm", away_col="away_team_norm",
                           hg_col="h_ft", ag_col="a_ft", league_col="league_name", league_val=lv)
        d["year"]=pd.to_datetime(d.match_date).dt.year
        years=sorted(d.year.unique())
        yp, mk, mp, dp, mproba = [], [], [], [], []
        for Y in years[1:]:
            tr=d[d.year<Y]; te=d[d.year==Y]
            if len(te)<30 or len(tr)<150: continue
            # 主模型
            m=lgb.LGBMClassifier(objective="multiclass",num_class=3,n_estimators=200,
                learning_rate=0.05,num_leaves=15,min_child_samples=50,reg_lambda=3.0,
                reg_alpha=1.0,subsample=0.9,colsample_bytree=0.9,random_state=42,n_jobs=1,verbose=-1)
            m.fit(tr[COL].values, tr.y.values)
            main_pred=m.predict(te[COL].values); main_proba=m.predict_proba(te[COL].values)
            # 平局二分类
            tr_d=tr.copy(); tr_d["yd"]=(tr_d.y==1).astype(int)
            te_d=te.copy(); te_d["yd"]=(te_d.y==1).astype(int)
            dm=lgb.LGBMClassifier(objective="binary",n_estimators=200,learning_rate=0.05,
                num_leaves=15,min_child_samples=50,reg_lambda=3.0,reg_alpha=1.0,
                subsample=0.9,colsample_bytree=0.9,random_state=42,n_jobs=1,verbose=-1)
            dm.fit(tr_d[COL].values, tr_d.yd.values)
            draw_prob=dm.predict_proba(te_d[COL].values)[:,1]
            yp.extend(te.y.values); mk.extend(market_argmax(te))
            mp.extend(main_pred); dp.extend(draw_prob); mproba.extend(main_proba)
        if yp:
            all_y.extend(yp); all_mkt.extend(mk); all_main_pred.extend(mp)
            all_draw_prob.extend(dp); all_main_proba.extend(mproba)
            per.append(dict(league=lv, n=len(yp)))
    y=np.array(all_y); mkt=np.array(all_mkt); mp=np.array(all_main_pred); dp=np.array(all_draw_prob)
    mproba=np.array(all_main_proba)
    base_acc=accuracy_score(y,mp); mkt_acc=accuracy_score(y,mkt)
    print(f"主模型(base) 1X2={base_acc*100:.1f}% market={mkt_acc*100:.1f}% "
          f"Top2={np.mean([y[i] in np.argsort(-mproba[i])[:2] for i in range(len(y))])*100:.1f}%")
    # 扫描二阶段阈值
    print("--- 二阶段平局阈值扫描 (覆盖主模型) ---")
    best=None
    for thr in [0.30,0.35,0.40,0.45,0.50]:
        final=mp.copy()
        final[dp>=thr]=1  # 覆盖为平局
        acc=accuracy_score(y,final)
        dr=float((final[y==1]==1).mean()) if (y==1).sum() else 0
        t2=np.mean([y[i] in np.argsort(-mproba[i])[:2] for i in range(len(y))])  # Top2用主模型概率
        ncov=int((dp>=thr).sum())
        print(f"  thr={thr}: 1X2={acc*100:.1f}% draw_recall={dr*100:.0f}% n_covered={ncov}")
        # 选: 1X2尽量高且draw_recall>=30 的最优
        if dr>=0.30 and (best is None or acc>best[1]):
            best=(thr,acc,dr)
    if best:
        print(f"SELECTED thr={best[0]} 1X2={best[1]*100:.1f}% draw_recall={best[2]*100:.0f}%")
    json.dump(dict(base_1x2=round(base_acc,4), market_1x2=round(mkt_acc,4),
                   selected=best and dict(thr=best[0],acc=round(best[1],4),draw_recall=round(best[2],4)),
                   per_league=per), open(OUT,"w"), indent=2, ensure_ascii=False)
    print("written", OUT)


if __name__=="__main__":
    main()

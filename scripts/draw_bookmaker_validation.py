# -*- coding: utf-8 -*-
"""验证涛哥的设想: 模型检测不到平局 -> 用操盘手(赔率)验证平局.
对比三种平局检测信号在真实数据上的 recall/precision:
  A) 模型 P(平)   (LightGBM, 赔率+时间序, Walk-Forward)
  B) 操盘手 P(平) (WH收盘赔率去抽水 -> 隐含平局概率)
  C) 双庄家共识   (WH + Interwetten 独立定价, 两者都高且一致 -> 强平局信号)
严禁模拟/泄漏. 真实william_ht(21k场) + interwetten_odds(14万场).
"""
import sys, json, sqlite3, numpy as np, pandas as pd, lightgbm as lgb
sys.path.insert(0, r"D:\Architecture")
from pipeline.features_temporal import build_features

DB = r"D:\Architecture\data\football_data.db"
LEAGUES = ["英超","西甲","意甲","德甲","法甲"]
FEATS = ["p_h","p_d","p_a","log_oh","log_od","log_oa","oh_minus_oa",
         "home_elo","away_elo","elo_diff",
         "home_form_pts5","away_form_pts5",
         "home_form_gf5","away_form_gf5","home_form_ga5","away_form_ga5",
         "home_form_gd5","away_form_gd5"]

def demargin(oh, od, oa):
    inv = 1.0/oh + 1.0/od + 1.0/oa
    return (1.0/oh)/inv, (1.0/od)/inv, (1.0/oa)/inv

def add_odds_feats(d):
    oh=d["close_home_odds"].astype(float).values; od=d["close_draw_odds"].astype(float).values; oa=d["close_away_odds"].astype(float).values
    inv=1.0/oh+1.0/od+1.0/oa
    d["p_h"]=(1.0/oh)/inv; d["p_d"]=(1.0/od)/inv; d["p_a"]=(1.0/oa)/inv
    d["log_oh"]=np.log(oh); d["log_od"]=np.log(od); d["log_oa"]=np.log(oa); d["oh_minus_oa"]=oh-oa
    return d

def load_wh(lg):
    con=sqlite3.connect(DB)
    df=pd.read_sql_query(
        "SELECT match_date, home_team_norm, away_team_norm, close_home_odds, close_draw_odds, "
        "close_away_odds, h_ft, a_ft, label FROM william_ht "
        "WHERE league_name LIKE ? AND league_name NOT LIKE '%/%' "
        "AND close_home_odds>1.01 AND close_draw_odds>1.01 AND close_away_odds>1.01 "
        "AND h_ft IS NOT NULL AND a_ft IS NOT NULL",
        con, params=(f"%{lg}%",))
    con.close()
    df["match_date"]=pd.to_datetime(df["match_date"],errors="coerce")
    return df.dropna(subset=["match_date"]).sort_values("match_date").reset_index(drop=True)

def load_iw():
    con=sqlite3.connect(DB)
    df=pd.read_sql_query(
        "SELECT match_date, home_team_norm, away_team_norm, league_name, close_home_odds, close_draw_odds, "
        "close_away_odds, home_score, away_score FROM interwetten_odds "
        "WHERE close_home_odds>1.01 AND close_draw_odds>1.01 AND close_away_odds>1.01 "
        "AND home_score IS NOT NULL AND away_score IS NOT NULL", con)
    con.close()
    df["match_date"]=pd.to_datetime(df["match_date"],errors="coerce")
    df["is_draw"]=(df["home_score"].astype(float)==df["away_score"].astype(float)).astype(int)
    return df.dropna(subset=["match_date"]).reset_index(drop=True)

def draw_metrics(sig, y, base_rate, ks=(0.10,0.15,0.20,0.25,0.30)):
    """sig: 平局信号(越大越可能是平); y: 1=实际平局. 返回各阈值下 recall/precision/lift."""
    order=np.argsort(-sig)
    n=len(y); ndraw=int(y.sum())
    out=[]
    for k in ks:
        topk=max(1,int(n*k))
        flagged=order[:topk]
        tp=int(y[flagged].sum())
        rec=tp/ndraw if ndraw else 0
        prec=tp/topk if topk else 0
        lift=prec/base_rate if base_rate else 0
        out.append(dict(k=k, recall=round(rec,3), precision=round(prec,3), lift=round(lift,2)))
    return out

# ---------- 主流程 ----------
wh_all=[]; test_df_rows=[]  # test_df_rows: 逐场WFOF测试行(供精确JOIN)
for lg in LEAGUES:
    df=load_wh(lg)
    if len(df)<600: 
        print(f"{lg}: insufficient ({len(df)})"); continue
    df["year"]=df["match_date"].dt.year
    years=sorted(df["year"].unique())
    rows_sig=[]  # (model_pd, mkt_pd, is_draw)
    for Y in years[1:]:
        tr=df[df.year<Y]; te=df[df.year==Y]
        if len(tr)<300 or len(te)<30: continue
        d=build_features(tr,date_col="match_date",home_col="home_team_norm",away_col="away_team_norm",hg_col="h_ft",ag_col="a_ft")
        dte=build_features(te,date_col="match_date",home_col="home_team_norm",away_col="away_team_norm",hg_col="h_ft",ag_col="a_ft")
        d=add_odds_feats(d); dte=add_odds_feats(dte)
        trf=d.dropna(subset=FEATS+["label"]); tef=dte.dropna(subset=FEATS)
        m=lgb.LGBMClassifier(objective="multiclass",num_class=3,n_estimators=180,learning_rate=0.05,
            num_leaves=15,min_child_samples=30,reg_lambda=1.5,subsample=0.85,colsample_bytree=0.85,
            random_state=42,n_jobs=1,verbose=-1)
        m.fit(trf[FEATS].values, trf["label"].values.astype(int))
        P=m.predict_proba(tef[FEATS].values)
        mp=P[:,1]
        oh=tef["close_home_odds"].astype(float).values; od=tef["close_draw_odds"].astype(float).values; oa=tef["close_away_odds"].astype(float).values
        M=np.array([demargin(o[0],o[1],o[2]) for o in np.stack([oh,od,oa],1)])
        mkd=M[:,1]
        # 收集共识JOIN逐场测试行 (WH原始表, 含league/date/odds/result)
        oh_t=te["close_home_odds"].astype(float).values; od_t=te["close_draw_odds"].astype(float).values; oa_t=te["close_away_odds"].astype(float).values
        M_t=np.array([demargin(x[0],x[1],x[2]) for x in np.stack([oh_t,od_t,oa_t],1)])
        for i in range(len(te)):
            test_df_rows.append((
                te["home_team_norm"].values[i], te["away_team_norm"].values[i],
                te["match_date"].values[i], 1 if int(te["label"].values[i])==1 else 0,
                float(M_t[i,1]), lg))
        for i in range(len(tef)):
            rows_sig.append((mp[i], mkd[i], 1 if int(tef["label"].values[i])==1 else 0))
    if rows_sig:
        mp_arr=np.array([r[0] for r in rows_sig]); mk_arr=np.array([r[1] for r in rows_sig]); yd=np.array([r[2] for r in rows_sig])
        wh_all.append((lg, mp_arr, mk_arr, yd))
        print(f"{lg}: n={len(yd)} base_draw_rate={yd.mean():.3f}")

# 汇总
allmp=np.concatenate([a[1] for a in wh_all]); allmk=np.concatenate([a[2] for a in wh_all]); ally=np.concatenate([a[3] for a in wh_all])
base=ally.mean()
print(f"\n==== 汇总 (n={len(ally)}, 基础平局率={base:.3f}) ====")
print("【A) 模型 P(平)】")
for m in draw_metrics(allmp, ally, base): print("  ", m)
print("【B) 操盘手 P(平) (WH去抽水)】")
for m in draw_metrics(allmk, ally, base): print("  ", m)

# ---------- 跨庄家共识 (WH ∩ IW) — 精确日期+联赛键 JOIN (OOS诚实版) ----------
print("\n==== 跨庄家平局共识 (WH × Interwetten, 精确JOIN) ====")
iw=load_iw()
from collections import defaultdict
iw_idx=defaultdict(list)
for _,r in iw.iterrows():
    ph,pd_,pa_=demargin(r["close_home_odds"],r["close_draw_odds"],r["close_away_odds"])
    iw_idx[(r["home_team_norm"],r["away_team_norm"])].append((r["match_date"], pd_, r["league_name"]))

def _base_league(name):
    if not name: return None
    import re
    s=re.sub(r'^\d{2}/\d{2}','',str(name).strip())
    s=re.sub(r'第.*?轮.*$','',s)
    return s.strip() or None

matched=0; cons_list=[]; draw_list=[]
for (h,a,md,isd,mkpd,lg) in test_df_rows:
    key=(h,a)
    if key not in iw_idx: continue
    if pd.isna(md): continue
    md_ts=pd.Timestamp(md)
    md_str=md_ts.strftime("%Y-%m-%d"); wlbase=_base_league(lg)
    best=None
    # 1) 精确日期匹配 (同fixture)
    for (d,pd_,lname) in iw_idx[key]:
        if pd.isna(d): continue
        if d.strftime("%Y-%m-%d")==md_str and _base_league(lname)==wlbase:
            best=pd_; break
    # 2) ±1天 时区容错 (仍要求联赛基一致, 不跨轮次)
    if best is None:
        for (d,pd_,lname) in iw_idx[key]:
            if pd.isna(d): continue
            if abs((d-md_ts).days)<=1 and _base_league(lname)==wlbase:
                best=pd_; break
    if best is None: continue
    cons=(mkpd+best)/2.0
    cons_list.append(cons); draw_list.append(isd); matched+=1
print(f"WH∩IW 精确匹配: {matched} 场 (of {len(test_df_rows)} WFOF测试行)")
cm_metrics=None
if matched>0:
    cm=pd.DataFrame({"cons":cons_list,"is_draw":draw_list})
    base_c=cm["is_draw"].mean()
    print(f"匹配子集基础平局率={base_c:.3f}")
    cm_metrics=[]
    for T in (0.28,0.30,0.32,0.34):
        sub=cm[cm["cons"]>=T]
        if len(sub)>20:
            print(f"  共识P(平)≥{T}: n={len(sub)} 实际平局率={sub['is_draw'].mean():.3f} lift={sub['is_draw'].mean()/base_c:.2f}")
            cm_metrics.append(dict(T=T, n=int(len(sub)), draw_rate=round(float(sub['is_draw'].mean()),3),
                                   lift=round(float(sub['is_draw'].mean()/base_c),2)))

out=dict(
    base_draw_rate=round(float(base),3),
    n_total=int(len(ally)),
    model_pd_metrics=draw_metrics(allmp,ally,base),
    market_pd_metrics=draw_metrics(allmk,ally,base),
    cross_bookmaker_matched=matched,
    cross_bookmaker_test_rows=len(test_df_rows),
    cross_bookmaker_base_rate=round(float(cm["is_draw"].mean()),3) if matched>0 else None,
    cross_bookmaker_metrics=cm_metrics,
    note="操盘手P(平)去抽水隐含概率 vs 模型P(平) 平局检测力对比; 跨庄家共识(精确日期+联赛键JOIN, OOS诚实版)作为增强信号")
with open(r"D:\Architecture\deliverables\draw_bookmaker_validation.json","w",encoding="utf-8") as f:
    json.dump(out,f,ensure_ascii=False,indent=2)
print("\nwritten D:\\Architecture\\deliverables\\draw_bookmaker_validation.json")

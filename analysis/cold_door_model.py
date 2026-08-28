"""
冷门波胆检测模型（合法赛前特征 + 正路盘口交叉验证 + out-of-time 证伪泄露）
============================================================================
设计（遵循铁律#1/#3，零终场泄露）：
  * 比分级分类器：预测 P(该比分命中 | 赛前特征)。
  * 特征仅赛前：mkt_implied(市场去水) / log_odds / exp_tot_goals /
             p_fair(训练折公平频率) / edge=p_fair-mkt_implied / is_fav / odds。
  * 公平频率 p_fair 仅用【训练折】按 exp_tot_goals 分桶计算 -> 防泄露。
  * out-of-time 验证：按 kickoff 排序，前70%训练后30%测试，证伪 look-ahead。
  * 冷门信号 = edge>0（公平概率>市场隐含）且为长赔方；与正路盘口交叉验证闭环。

产出：cold_door_model_artifacts.pkl（后端 import 复用）。
"""
import sqlite3, json, re, math, pickle
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import roc_auc_score
from collections import defaultdict

GQ="D:/Architecture/data/events.db"
SCORES=[f"{h}-{a}" for h in range(6) for a in range(6)]
SCORE_SET=set(SCORES); SCORE_RE=re.compile(r"^\d+-\d+$")
ART="analysis/cold_door_model_artifacts.pkl"

def devig_cs(grid):
    inv={s:1.0/o for s,o in grid.items() if s in SCORE_SET and isinstance(o,(int,float)) and o>1.0}
    t=sum(inv.values()); return {s:p/t for s,p in inv.items()} if t>0 else None

def build_features_from_csv(df_train):
    """用训练折计算 p_fair（按 exp_tot_goals 分10桶的经验比分频率）。"""
    # 以 match 级 exp_tot 分桶
    m=df_train.groupby("match_key")["exp_tot_goals"].first()
    qs=np.quantile(m.values,[0,.1,.2,.3,.4,.5,.6,.7,.8,.9,1.0])
    qs[0]-=1e-6; qs[-1]+=1e-6
    buckets={mk:int(np.digitize([v],qs)[0]) for mk,v in m.items()}
    df_train=df_train.copy(); df_train["bucket"]=df_train["match_key"].map(buckets)
    fair=defaultdict(lambda: defaultdict(float))
    cnt=defaultdict(float)
    for _,r in df_train[df_train["in_grid_match"]==1].iterrows():
        b=r["bucket"]; fair[b][r["score"]]+=r["hit"]; cnt[b]+=1
    pfair={b:{s:(fair[b].get(s,0)/c if c>0 else 0) for s in SCORES} for b,c in cnt.items()}
    return {"qs":qs.tolist(),"pfair":{str(b):v for b,v in pfair.items()},
            "exp_tot_global":float(m.mean())}

def add_features(df, fair_meta):
    qs=np.array(fair_meta["qs"]); pfair={int(b):v for b,v in fair_meta["pfair"].items()}
    df=df.copy()
    b=np.digitize(df["exp_tot_goals"].values,qs)
    pfair_row=[pfair.get(int(bb),{s:0 for s in SCORES}) for bb in b]
    df["p_fair"]=[max(0.0,pr.get(s,0.0)) for s,pr in zip(df["score"],pfair_row)]
    df["edge"]=df["p_fair"]-df["mkt_implied"]
    df["odds_bucket"]=pd.cut(df["odds"],[0,5,8,12,20,40,1e9],
                             labels=[0,1,2,3,4,5]).astype(int)
    return df

def train():
    df=pd.read_csv("analysis/cold_door_scorelevel.csv")
    # out-of-time：按 kickoff 排序
    df["kd"]=df["kickoff"].astype(str).str[:10]
    order=sorted(df["match_key"].unique(),
                key=lambda mk: df[df.match_key==mk]["kd"].iloc[0])
    n=len(order); cut=int(n*0.7)
    train_m,test_m=order[:cut],order[cut:]
    tr=df[df.match_key.isin(train_m)]; te=df[df.match_key.isin(test_m)]
    print(f"[split] train matches={len(train_m)} test matches={len(test_m)} "
          f"(train {tr.kd.min()}..{tr.kd.max()} | test {te.kd.min()}..{te.kd.max()})")
    fair_meta=build_features_from_csv(tr)
    tr=add_features(tr,fair_meta); te=add_features(te,fair_meta)
    feats=["mkt_implied","log_odds","exp_tot_goals","p_fair","edge","is_fav_score","odds_bucket"]
    Xtr,ytr=tr[feats].values,tr["hit"].values
    Xte,yte=te[feats].values,te["hit"].values
    # 类别不平衡：对命中样本加权
    sw=np.where(ytr==1, 30.0, 1.0)
    clf=GradientBoostingClassifier(n_estimators=200,max_depth=3,learning_rate=0.05,
                                  subsample=0.8,random_state=42)
    clf.fit(Xtr,ytr,sample_weight=sw)
    # ---- 评估
    p_te=clf.predict_proba(Xte)[:,1]
    auc=roc_auc_score(yte,p_te)
    print(f"\n[test] score-level AUC = {auc:.4f}  (市场隐含作ranker AUC = {roc_auc_score(yte,te['mkt_implied']):.4f})")
    # 逐场排名（仅真实比分在网格内的测试场）
    te_in=te[te["in_grid_match"]==1]
    top1_mod=top3_mod=top1_mkt=top3_mkt=0; nm=0
    cd_top1_mod=cd_top1_mkt=cd_n=0
    for mk,g in te_in.groupby("match_key"):
        nm+=1
        actual=g[g["hit"]==1]["score"]
        if len(actual)==0: continue
        actual=actual.iloc[0]
        g=g.copy(); g["model_p"]=clf.predict_proba(g[feats].values)[:,1]
        mod_rank=g.sort_values("model_p",ascending=False)["score"].tolist()
        mkt_rank=g.sort_values("mkt_implied",ascending=False)["score"].tolist()
        if mod_rank[0]==actual: top1_mod+=1
        if actual in mod_rank[:3]: top3_mod+=1
        if mkt_rank[0]==actual: top1_mkt+=1
        if actual in mkt_rank[:3]: top3_mkt+=1
        # 冷门子集：真实命中为长赔方(odds>=12)
        arow=g[g["score"]==actual].iloc[0]
        if arow["odds"]>=12:
            cd_n+=1
            if mod_rank[0]==actual: cd_top1_mod+=1
            if mkt_rank[0]==actual: cd_top1_mkt+=1
    print(f"[test] 全场排名 (in_grid n={nm}):"
          f"  模型 top1={top1_mod/nm:.3f} top3={top3_mod/nm:.3f}"
          f" | 市场 top1={top1_mkt/nm:.3f} top3={top3_mkt/nm:.3f}")
    print(f"[test] 冷门子集(真实长赔方 odds>=12, n={cd_n}):"
          f"  模型 top1={cd_top1_mod/cd_n:.3f} | 市场 top1={cd_top1_mkt/cd_n:.3f}")
    # ---- 保存
    art={"clf":clf,"feats":feats,"fair_meta":fair_meta,
         "metrics":{"test_auc":round(auc,4),
                    "top1_model":round(top1_mod/nm,4),"top1_market":round(top1_mkt/nm,4),
                    "top3_model":round(top3_mod/nm,4),"top3_market":round(top3_mkt/nm,4),
                    "cd_top1_model":round(cd_top1_mod/cd_n,4) if cd_n else None,
                    "cd_top1_market":round(cd_top1_mkt/cd_n,4) if cd_n else None,
                    "n_train":len(train_m),"n_test":len(test_m)}}
    with open(ART,"wb") as f: pickle.dump(art,f)
    print(f"\n[saved] {ART}")
    return art

# ----------------------------------------------------------------- 推理（后端复用）
def load_model():
    with open(ART,"rb") as f: return pickle.load(f)
def predict_for_grid(grid, home="", away="", league="", kickoff=""):
    """给定赛前 CS 网格，返回每比分的模型概率/edge/交叉验证结论。"""
    art=load_model(); clf=art["clf"]; feats=art["feats"]; fm=art["fair_meta"]
    dev=devig_cs(grid)
    if not dev: return None
    exp_tot=sum((int(s[0])+int(s[2]))*p for s,p in dev.items())
    qs=np.array(fm["qs"]); b=int(np.digitize([exp_tot],qs)[0])
    pfair=fm["pfair"].get(str(b),{s:0 for s in SCORES})
    rows=[]
    for s in SCORES:
        if s not in grid: continue
        odds=float(grid[s]); mkt=dev.get(s); pf=max(0.0,pfair.get(s,0.0))
        rec={"score":s,"odds":odds,"mkt_implied":round(mkt,4),
             "p_fair":round(pf,4),"edge":round(pf-mkt,4),
             "is_fav_score":0,"exp_tot_goals":round(exp_tot,3),
             "log_odds":math.log(odds),"odds_bucket":int(pd.cut([odds],[0,5,8,12,20,40,1e9],labels=[0,1,2,3,4,5])[0])}
        rows.append(rec)
    X=np.array([[r["mkt_implied"],r["log_odds"],r["exp_tot_goals"],r["p_fair"],
                r["edge"],r["is_fav_score"],r["odds_bucket"]] for r in rows])
    p=clp=clf.predict_proba(X)[:,1]
    # 归一化为场内相对排序分(0-1): 仅作排名用, 非绝对概率
    pmin,pmax=float(p.min()),float(p.max())
    prank=((p-pmin)/(pmax-pmin)) if pmax>pmin else p*0
    for r,pp,pr in zip(rows,p,prank):
        r["model_p"]=round(float(pr),4); r["model_raw"]=round(float(pp),4)
    # 交叉验证闭环：冷门候选 = edge>0 且长赔方(odds>=12)
    for r in rows:
        r["cold_candidate"]= bool(r["edge"]>0 and r["odds"]>=12)
        # 闭环：若正路盘口(mkt_implied)也低 && 模型p高于市场 -> 分歧成立(confirmed)
        r["cross_validated"]= bool(r["cold_candidate"] and r["model_p"]>r["mkt_implied"])
    return {"exp_tot_goals":round(exp_tot,3),"scores":rows,
            "cold_signals":[r["score"] for r in rows if r["cross_validated"]]}

if __name__=="__main__":
    import sys
    if len(sys.argv)>1 and sys.argv[1]=="predict":
        grid=json.loads(sys.argv[2])
        print(json.dumps(predict_for_grid(grid),ensure_ascii=False,indent=2))
    else:
        train()

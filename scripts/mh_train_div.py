"""
Task #6b — 精确检验"市场未含独立信息": 用 市场vs独立 的分歧(divergence)特征, 而非原始 elo.
独立胜率由 elo_diff 推导(home=1/(1+10^-dr/400)), 直接衡量"市场对独立评估的偏离".
若 divergence 让模型 beat 市场 -> 真 edge; 否则 -> 负结论铁证.
特征 = [imp(3), cur(2), minute, elo_diff, form_diff, rest_diff, league_strength, div_home, div_away]
"""
import csv, math, collections
import numpy as np
import lightgbm as lgb

ROWS = list(csv.DictReader(open("data/mh_dataset_x.csv", encoding="utf-8")))
IND = ["elo_diff","form_diff","rest_diff","league_strength"]
def f(v): return float(v)
def indep_home_prob(dr): return 1.0/(1.0+10.0**(-dr/400.0))

samples=[]
for r in ROWS:
    dr=f(r["elo_diff"]); ihp=indep_home_prob(dr); iap=1.0-ihp
    div_home=f(r["imp_home"])-ihp; div_away=f(r["imp_away"])-iap
    feats=[f(r["imp_home"]),f(r["imp_draw"]),f(r["imp_away"]),
           float(r["cur_sh"]),float(r["cur_sa"]),float(r["minute"]),
           f(r["elo_diff"]),f(r["form_diff"]),f(r["rest_diff"]),f(r["league_strength"]),
           div_home, div_away]
    samples.append({"x":feats,"y":int(r["ridx"]),"mk":r["match_key"],"kt":float(r["kickoff_epoch"]),
                    "cp":int(r["cp"]),"imp":[f(r["imp_home"]),f(r["imp_draw"]),f(r["imp_away"])],
                    "csh":int(r["cur_sh"]),"csa":int(r["cur_sa"])})

def ll(p,y):
    p=max(min(p[y],0.999),0.001); return -math.log(p)
def sp(csh,csa): return 0 if csh>csa else (2 if csa>csh else 1)
def ev(P,items):
    m=mo=s=0.0; ma=moa=sa=0; n=len(items)
    for pm,it in zip(P,items):
        y=it["y"]; m+=ll(pm,y); mo+=ll(it["imp"],y)
        sv=[0,0,0]; sv[sp(it["csh"],it["csa"])]=1.0; s+=ll(sv,y)
        ma+=(int(np.argmax(pm))==y); moa+=(int(np.argmax(it["imp"]))==y); sa+=(sp(it["csh"],it["csa"])==y)
    return dict(n=n,model_ll=m/n,market_ll=mo/n,scoreonly_ll=s/n,model_acc=ma/n,market_acc=moa/n,scoreonly_acc=sa/n)

mks=sorted({s["mk"] for s in samples},key=lambda m:min(s["kt"] for s in samples if s["mk"]==m))
by_mk=collections.defaultdict(list)
for s in samples: by_mk[s["mk"]].append(s)
folds=[(mks[:int(len(mks)*c)],mks[int(len(mks)*c):]) for c in (0.60,0.80,0.90) if mks[int(len(mks)*c):]]
print(f"[INFO] matches={len(mks)} samples={len(samples)} folds={len(folds)}")
agg={k:0 for k in ("model_ll","market_ll","scoreonly_ll","model_acc","market_acc","scoreonly_acc","n")}
for fi,(trm,tem) in enumerate(folds,1):
    tr=[s for m in trm for s in by_mk[m]]; te=[s for m in tem for s in by_mk[m]]
    Xtr=np.array([s["x"] for s in tr]);Ytr=np.array([s["y"] for s in tr]);Xte=np.array([s["x"] for s in te])
    clf=lgb.LGBMClassifier(n_estimators=400,max_depth=5,learning_rate=0.05,num_leaves=31,
                           min_child_samples=20,subsample=0.9,collinear_treatment="dropy",verbose=-1,random_state=42)
    clf.fit(Xtr,Ytr);P=clf.predict_proba(Xte);e=ev([p.tolist() for p in P],te)
    print(f"\n=== Fold {fi}: train={len(tr)} test={len(te)} ===")
    print(f"  LL model={e['model_ll']:.4f} market={e['market_ll']:.4f} scoreonly={e['scoreonly_ll']:.4f}")
    print(f"  acc model={e['model_acc']*100:.1f}% market={e['market_acc']*100:.1f}%")
    for cp in [0,45,60,75,85]:
        sub=[s for s in te if s["cp"]==cp]
        if not sub: continue
        idx=[te.index(s) for s in sub];ec=ev([P[i].tolist() for i in idx],sub)
        d=ec["model_ll"]-ec["market_ll"]
        print(f"   cp={cp:>2}: n={ec['n']:>4} modelLL={ec['model_ll']:.4f} mktLL={ec['market_ll']:.4f} Δ={d:+.4f}[{'BEAT' if d<-0.001 else ('~' if abs(d)<=0.001 else 'WORSE')}]")
    for k in ("model_ll","market_ll","scoreonly_ll","model_acc","market_acc","scoreonly_acc"): agg[k]+=e[k]*e["n"]
    agg["n"]+=e["n"]
print("\n=== AGGREGATE ===")
for k in ("model_ll","market_ll","scoreonly_ll","model_acc","market_acc","scoreonly_acc"): agg[k]/=agg["n"]
print(f"  LL model={agg['model_ll']:.4f} market={agg['market_ll']:.4f} scoreonly={agg['scoreonly_ll']:.4f}")
print(f"  acc model={agg['model_acc']*100:.1f}% market={agg['market_acc']*100:.1f}%")
d=agg["model_ll"]-agg["market_ll"]
print(f"  VERDICT(divergence vs market): ΔLL={d:+.4f} -> {'BEATS MARKET' if d<-0.001 else ('TIE' if abs(d)<=0.001 else 'NO EDGE')}")

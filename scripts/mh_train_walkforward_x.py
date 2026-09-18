"""
Task #6 — 扩展多时点模型 walk-forward 验证(注入市场未含独立信息).
特征 = [去水1X2隐含(3), 当前比分(2), minute(1)] + 13 独立特征(elo/form/rest/h2h/league, 源于赛果非赔率).
基线: market(同份 odds 去水隐含) / scoreonly(纯机械).
对比基准: 无独立特征模型(model LL=0.8891, 见原型报告). 真门槛: market LL=0.8079.
诚实口径: 扩展模型 LL < market LL 才算 beat; 加独立信息后 LL 是否下降(>market 仍无 edge).
附加: 打印独立特征 feature importance, 看其是否真贡献(冗余则≈0, 解释无 edge).
"""
import csv, math, collections
import numpy as np
import lightgbm as lgb

ROWS = list(csv.DictReader(open("data/mh_dataset_x.csv", encoding="utf-8")))
IND_FEATS = ["elo_home","elo_away","elo_diff","form_home","form_away","form_diff",
             "rest_home","rest_away","rest_diff","h2h_home_win","h2h_draw","h2h_away_win","league_strength"]
FEATS = ["imp_home","imp_draw","imp_away","cur_sh","cur_sa","minute"] + IND_FEATS

def f(v): return float(v)
samples = []
for r in ROWS:
    x = [f(r[k]) for k in FEATS]
    samples.append({"x": x, "y": int(r["ridx"]), "mk": r["match_key"],
                    "kt": float(r["kickoff_epoch"]), "cp": int(r["cp"]),
                    "imp": [f(r["imp_home"]), f(r["imp_draw"]), f(r["imp_away"])],
                    "csh": int(r["cur_sh"]), "csa": int(r["cur_sa"])})

def ll(probs, y):
    p = max(min(probs[y], 0.999), 0.001); return -math.log(p)
def scoreonly_pred(csh, csa):
    return 0 if csh > csa else (2 if csa > csh else 1)

def evaluate(preds_model, items):
    m_ll = s_ll = mo_ll = 0.0; m_acc = s_acc = mo_acc = 0; n = len(items)
    for pm, it in zip(preds_model, items):
        y = it["y"]; m_ll += ll(pm, y); mo_ll += ll(it["imp"], y)
        sp = scoreonly_pred(it["csh"], it["csa"]); svec = [0,0,0]; svec[sp]=1.0
        s_ll += ll(svec, y)
        m_acc += (int(np.argmax(pm)) == y); mo_acc += (int(np.argmax(it["imp"])) == y)
        s_acc += (sp == y)
    return {"n": n, "model_ll": m_ll/n, "market_ll": mo_ll/n, "scoreonly_ll": s_ll/n,
            "model_acc": m_acc/n, "market_acc": mo_acc/n, "scoreonly_acc": s_acc/n}

mks = sorted({s["mk"] for s in samples}, key=lambda m: min(s["kt"] for s in samples if s["mk"]==m))
by_mk = collections.defaultdict(list)
for s in samples: by_mk[s["mk"]].append(s)
cuts = [0.60, 0.80, 0.90]; folds = []
for c in cuts:
    k = int(len(mks)*c); tr = mks[:k]; te = mks[k:]
    if te: folds.append((tr, te))

print(f"[INFO] matches={len(mks)} samples={len(samples)} folds={len(folds)} feats={len(FEATS)}")
agg = {kk:0 for kk in ("model_ll","market_ll","scoreonly_ll","model_acc","market_acc","scoreonly_acc","n")}
fold_imp = []
for fi,(train_mk,test_mk) in enumerate(folds,1):
    tr = [s for m in train_mk for s in by_mk[m]]; te = [s for m in test_mk for s in by_mk[m]]
    Xtr = np.array([s["x"] for s in tr]); Ytr = np.array([s["y"] for s in tr]); Xte = np.array([s["x"] for s in te])
    clf = lgb.LGBMClassifier(n_estimators=400, max_depth=5, learning_rate=0.05, num_leaves=31,
                             min_child_samples=20, subsample=0.9, collinear_treatment="dropy",
                             verbose=-1, random_state=42)
    clf.fit(Xtr, Ytr)
    P = clf.predict_proba(Xte); ev = evaluate([p.tolist() for p in P], te)
    print(f"\n=== Fold {fi}: train={len(tr)} test={len(te)} ===")
    print(f"  log-loss  model={ev['model_ll']:.4f}  market={ev['market_ll']:.4f}  scoreonly={ev['scoreonly_ll']:.4f}")
    print(f"  top1-acc  model={ev['model_acc']*100:.1f}%  market={ev['market_acc']*100:.1f}%  scoreonly={ev['scoreonly_acc']*100:.1f}%")
    for cp in [0,45,60,75,85]:
        sub=[s for s in te if s["cp"]==cp]
        if not sub: continue
        idx=[te.index(s) for s in sub]; evc=evaluate([P[i].tolist() for i in idx], sub)
        d=evc["model_ll"]-evc["market_ll"]
        print(f"   cp={cp:>2}: n={evc['n']:>4} modelLL={evc['model_ll']:.4f} mktLL={evc['market_ll']:.4f} Δ={d:+.4f}[{'BEAT' if d<-0.001 else ('~' if abs(d)<=0.001 else 'WORSE')}]")
    for kk in ("model_ll","market_ll","scoreonly_ll","model_acc","market_acc","scoreonly_acc"):
        agg[kk]+=ev[kk]*ev["n"]
    agg["n"]+=ev["n"]
    fold_imp.append(clf.feature_importances_)

print("\n=== AGGREGATE (N-weighted) ===")
for kk in ("model_ll","market_ll","scoreonly_ll","model_acc","market_acc","scoreonly_acc"):
    agg[kk]/=agg["n"]
print(f"  log-loss  model={agg['model_ll']:.4f}  market={agg['market_ll']:.4f}  scoreonly={agg['scoreonly_ll']:.4f}")
print(f"  top1-acc  model={agg['model_acc']*100:.1f}%  market={agg['market_acc']*100:.1f}%  scoreonly={agg['scoreonly_acc']*100:.1f}%")
d=agg["model_ll"]-agg["market_ll"]
print(f"  VERDICT(ext vs market): ΔLL={d:+.4f} -> {'BEATS MARKET' if d<-0.001 else ('TIE' if abs(d)<=0.001 else 'NO EDGE (model>=market)')}")
print(f"  REF 无独立特征模型 LL=0.8891 (原型); 市场 LL=0.8079")
imp_avg = np.mean(fold_imp, axis=0)
print("\n=== Feature importance (avg over folds) ===")
order = sorted(range(len(FEATS)), key=lambda i: -imp_avg[i])
for i in order:
    tag = " [独立]" if FEATS[i] in IND_FEATS else ""
    print(f"  {FEATS[i]:<14} {imp_avg[i]:6.1f}{tag}")
tot_ind = sum(imp_avg[i] for i in range(len(FEATS)) if FEATS[i] in IND_FEATS)
print(f"  独立特征总重要性占比 = {tot_ind/sum(imp_avg)*100:.1f}%")

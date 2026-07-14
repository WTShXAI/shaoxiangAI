# -*- coding: utf-8 -*-
"""
WC honest walk-forward trainer (REAL data only, NO leakage).
Data : wc_xlsx_matches (280 real WC matches, 4 editions, odds + final score)
Feat : derived ONLY from each match's own pre-match odds (no result leakage)
Split: temporal walk-forward by edition (train older -> test newer)
Baseline: market argmax (de-margined implied probabilities)
Goal: beat market argmax on honest OOS, report Top-2 containment >= 80% target.
"""
import sqlite3, json, numpy as np, pandas as pd
from sklearn.metrics import accuracy_score
import lightgbm as lgb

DB = r'D:\Architecture\data\football_data.db'
OUT = r'D:\Architecture\deliverables\wc_honest_train_20260708.json'

con = sqlite3.connect(DB)
df = pd.read_sql_query(
    "SELECT edition, home, away, oh, od, oa, hg, ag FROM wc_xlsx_matches "
    "WHERE oh>1.01 AND od>1.01 AND oa>1.01 AND hg IS NOT NULL AND ag IS NOT NULL",
    con)
con.close()
print("loaded real matches:", len(df))

# ---- target ----
def outcome(r):
    if r.hg > r.ag: return 0  # H
    if r.hg < r.ag: return 2  # A
    return 1                   # D
df['y'] = df.apply(outcome, axis=1)

# ---- leakage-free features from own odds ----
def feats(row):
    oh, od, oa = row.oh, row.od, row.oa
    s = 1/oh + 1/od + 1/oa
    ph, pd_, pa = (1/oh)/s, (1/od)/s, (1/oa)/s   # de-margined implied probs
    over = s - 1.0
    return [ph, pd_, pa, over,
            np.log(ph/pd_), np.log(pa/pd_),
            max(ph, pd_, pa),          # favorite strength
            (ph+pa)-pd_,               # win-prob minus draw
            (1/oh), (1/od), (1/oa)]    # raw implied
COL = ['ph','pd','pa','over','logHD','logAD','fav','win_minus_draw','rawH','rawD','rawA']
def build(X):
    M = np.column_stack([f for f in X.apply(lambda r: feats(r), axis=1)])
    return pd.DataFrame(M.T, columns=COL)
F = build(df)
df = pd.concat([df, F], axis=1)

# ---- market argmax baseline ----
df['argmax'] = df[['ph','pd','pa']].values.argmax(1)
acc_argmax_all = accuracy_score(df['y'], df['argmax'])
print("market argmax (all 280): %.1f%%" % (100*acc_argmax_all))

# ---- walk-forward by edition ----
editions = sorted(df['edition'].unique())   # e.g. 2014,2018,2022,2026
print("editions:", editions)

folds = []
for i in range(1, len(editions)):
    tr = df[df['edition'].isin(editions[:i])]
    te = df[df['edition'] == editions[i]]
    if len(te) < 5: continue
    Xtr, ytr = tr[COL].values.astype(float), np.asarray(tr['y'].values, dtype=int)
    Xte, yte = te[COL].values.astype(float), np.asarray(te['y'].values, dtype=int)
    print("  fold test=%s n_tr=%d n_te=%d ytr_shape=%s" % (editions[i], len(Xtr), len(Xte), ytr.shape))
    # LightGBM multiclass, regularized (anti-overfit)
    model = lgb.LGBMClassifier(objective='multiclass', num_class=3,
        n_estimators=150, learning_rate=0.05, num_leaves=7,
        min_child_samples=20, reg_lambda=2.0, reg_alpha=1.0,
        subsample=0.8, colsample_bytree=0.8, random_state=42, n_jobs=1,
        verbose=-1)
    model.fit(Xtr, ytr)
    pred = model.predict(Xte)
    proba = model.predict_proba(Xte)
    top2 = (proba.argsort(1)[:,-2:] == yte.reshape(-1,1)).any(1)
    acc_m = accuracy_score(yte, pred)
    acc_a = accuracy_score(yte, te['argmax'].values)
    draw_rec = ((yte==1) & (pred==1)).sum() / max(1,(yte==1).sum())
    folds.append(dict(test_edition=int(editions[i]), n=int(len(te)),
        model_acc=round(100*acc_m,1), argmax_acc=round(100*acc_a,1),
        top2=round(100*top2.mean(),1), draw_recall=round(100*draw_rec,1)))

# combined OOS (all test folds)
all_te = pd.concat([df[df['edition']==e] for e in editions[1:]])
print("\n=== WALK-FORWARD OOS ===")
for f in folds:
    print("  test %s (n=%d): model=%.1f%%  argmax=%.1f%%  top2=%.1f%%  drawR=%.1f%%" % (
        f['test_edition'], f['n'], f['model_acc'], f['argmax_acc'], f['top2'], f['draw_recall']))

summary = dict(
    n_real_matches=int(len(df)),
    argmax_all=round(100*acc_argmax_all,1),
    walk_forward=folds,
    note="Features derived ONLY from each match's own pre-match odds. Temporal split by edition. No result leakage.",
)
with open(OUT,'w',encoding='utf-8') as fp:
    json.dump(summary, fp, ensure_ascii=False, indent=2)
print("\n[written]", OUT)

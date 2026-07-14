# -*- coding: utf-8 -*-
"""
将 cross_book_favorite_disagree 落成 v7.1 训练特征并验证边际价值.
严格时序切分: 仅用 pre-2019 双庄窗口(WH+IW 同场), train=2016 / test=2017-2018.
目标: argmax_hit (共识/市场argmax是否命中赛果).
对比: 基线9特征 vs +跨庄分歧特征, 看 AUC / Top-K ROI 增量.
"""
import os, sys, json, sqlite3, warnings
import numpy as np, pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'football_data.db')
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'deliverables', 'cross_book_feature_validation_20260711.json')

# ── 1. 取双庄同场(同对阵同日期)的IW特征 + WH特征, 算分歧 ──
c = sqlite3.connect(DB)
iw = pd.read_sql_query(
    "SELECT home_team,away_team,match_date,outcome,open_h,open_d,open_a,close_h,close_d,close_a,"
    "drift_h,drift_d,drift_a,overround,home_edge,cimp_h,cimp_d,cimp_a,imp_d "
    "FROM odds_features WHERE source='interwetten' AND open_h>0 AND close_h>0 AND outcome IS NOT NULL", c)
wh = pd.read_sql_query(
    "SELECT home_team,away_team,match_date,close_h,close_d,close_a "
    "FROM odds_features WHERE source='william_hill' AND open_h>0 AND close_h>0", c)
c.close()

def fav_row(r):
    h,d,a = r['close_h'],r['close_d'],r['close_a']
    m = min(h,d,a)
    return 'H' if m==h else ('A' if m==a else 'D')

wh = wh.rename(columns={'close_h':'wh_h','close_d':'wh_d','close_a':'wh_a'})
m = iw.merge(wh, on=['home_team','away_team','match_date'], how='inner')
print(f"双庄同场(IW有赛果 ∩ WH有盘): {len(m)} 场")

m['iw_fav'] = m.apply(fav_row, axis=1)
m['wh_fav'] = m.apply(lambda r: fav_row({'close_h':r['wh_h'],'close_d':r['wh_d'],'close_a':r['wh_a']}), axis=1)
m['cross_book_fav_disagree'] = (m['iw_fav'] != m['wh_fav']).astype(int)
print(f"  其中分歧: {m['cross_book_fav_disagree'].sum()} 场 ({100*m['cross_book_fav_disagree'].mean():.1f}%)")

# 目标: 共识argmax(用IW收盘, 即主盘)是否命中
m['close_argmax'] = m.apply(lambda r: fav_row({'close_h':r['close_h'],'close_d':r['close_d'],'close_a':r['close_a']}), axis=1)
m['argmax_hit'] = (m['close_argmax'] == m['outcome']).astype(int)
m['match_date'] = pd.to_datetime(m['match_date'])
m['drift_mag'] = m[['drift_h','drift_d','drift_a']].abs().max(axis=1)
m['argmax_imp'] = m[['cimp_h','cimp_d','cimp_a']].values[np.arange(len(m)),
                                                       m['close_argmax'].map({'H':0,'D':1,'A':2}).values]

# ── 2. 严格时序切分: train 2016 / test 2017-2018 ──
tr = m['match_date'] < '2017-01-01'
te = ~tr
print(f"train(2016)={tr.sum()}  test(2017-18)={te.sum()}")

base_feats = ['drift_h','drift_d','drift_a','drift_mag','overround','home_edge','argmax_imp','cimp_d','imp_d']
all_feats = base_feats + ['cross_book_fav_disagree']

from lightgbm import LGBMClassifier
from sklearn.metrics import roc_auc_score

def topk_roi(model, X, y, close_odds, sa, k=1000):
    if len(y) < k: k = len(y)
    p = model.predict_proba(X)[:,1]
    order = np.argsort(-p)[:k]
    ao = close_odds[order][np.arange(k), sa[order]]
    win = (sa[order] == y[order]).astype(float)
    return float((win*ao - 1).mean())

def run(feats):
    model = LGBMClassifier(n_estimators=300, max_depth=6, num_leaves=47,
                           learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
                           min_child_samples=50, reg_alpha=0.1, reg_lambda=1.0,
                           random_state=42, n_jobs=-1, verbose=-1)
    model.fit(m[feats].values[tr], m['argmax_hit'].values[tr])
    p = model.predict_proba(m[all_feats if 'cross_book_fav_disagree' in feats else feats].values[te])[:,1]
    auc = roc_auc_score(m['argmax_hit'].values[te], p)
    odds = m[['close_h','close_d','close_a']].values
    sa = m['close_argmax'].map({'H':0,'D':1,'A':2}).values
    roi1k = topk_roi(model, m[feats].values[te], m['argmax_hit'].values[te], odds[te], sa[te], 1000)
    return auc, roi1k

auc_base, roi_base = run(base_feats)
auc_feat, roi_feat = run(all_feats)
print(f"\n[基线 9特征]        AUC={auc_base:.4f}  Top1000 ROI={roi_base:+.2%}")
print(f"[+跨庄分歧特征]     AUC={auc_feat:.4f}  Top1000 ROI={roi_feat:+.2%}")
print(f"[增量]              AUC={auc_feat-auc_base:+.4f}  ROI={roi_feat-roi_base:+.2%}")

# 分歧子集单独验证(测试集内)
dis_te = te & (m['cross_book_fav_disagree']==1)
if dis_te.sum() > 0:
    base_rate = m['argmax_hit'].values[dis_te].mean()
    print(f"\n[测试集分歧子集 {dis_te.sum()}场] 共识argmax命中基线={base_rate:.1%} (应≈32%)")

# 特征重要性(加特征模型)
model = LGBMClassifier(n_estimators=300, max_depth=6, num_leaves=47, learning_rate=0.05,
                       subsample=0.8, colsample_bytree=0.8, min_child_samples=50,
                       reg_alpha=0.1, reg_lambda=1.0, random_state=42, n_jobs=-1, verbose=-1)
model.fit(m[all_feats].values[tr], m['argmax_hit'].values[tr])
imp = dict(zip(all_feats, model.feature_importances_))
print("\n[特征重要性]")
for k,v in sorted(imp.items(), key=lambda x:-x[1]):
    print(f"  {k:28s} {v}")

result = {
    'cross_book_same_fixture': int(len(m)),
    'disagreement_n': int(m['cross_book_fav_disagree'].sum()),
    'disagreement_rate': round(float(m['cross_book_fav_disagree'].mean()), 4),
    'split': 'train=2016 / test=2017-2018 (strict temporal)',
    'auc_base': round(auc_base,4), 'auc_with_feature': round(auc_feat,4),
    'auc_delta': round(auc_feat-auc_base,4),
    'roi1000_base': round(roi_base,4), 'roi1000_with_feature': round(roi_feat,4),
    'disagreement_consensus_hit_test': round(float(base_rate),4) if dis_te.sum()>0 else None,
    'feature_importance': {k:int(v) for k,v in imp.items()},
    'note': '2023+ odds_features 全单庄→该特征仅pre-2019双庄窗口可用; 训练须严格时序切分, 不能混入2023+',
}
with open(OUT,'w',encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(f"\n[JSON 已导出: {OUT}]")

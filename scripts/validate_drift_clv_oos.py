"""P3-ext OOS: 双庄(WH+IW)跨庄信号 窗口内时序 holdout.
事实约束: 2023+ odds_features 无双庄同场(仅 interwetten), 双庄仅存 2016-2018.
故严格 OOS = 窗口内切分: 2016=train(定义/基线), 2017-2018=test(纯外推, 不重调).
验证: (1)跨庄方向性分歧 共识热门命中是否仍崩 (2)honest_def drift 对齐是否仍高于基线.
"""
import os, json, sqlite3
import numpy as np
import pandas as pd

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'football_data.db')
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'deliverables')
os.makedirs(OUT, exist_ok=True)

def norm2(a):
    s = a.sum(axis=1, keepdims=True)
    return a / s

def binom_p(n, k, p0):
    try:
        from scipy.stats import binomtest
        return float(binomtest(k, n, p0).pvalue)
    except Exception:
        se = (p0*(1-p0)/n)**0.5
        z = (k/n - p0)/se if se>0 else 0
        from math import erf, sqrt
        return float(2*(1-0.5*(1+erf(abs(z)/sqrt(2)))))

TH = 0.02
PATTERN_MAP = {
    (-1,1,1): ('honest_defH','H'), (1,1,-1): ('honest_defA','A'),
    (-1,-1,1): ('fake_defH','A'),  (1,-1,-1): ('fake_defA','H'),
    (-1,-1,-1): ('all_down',None), (1,1,1): ('all_up',None),
}
TARGET = {'honest_defH':'H','honest_defA':'A','fake_defH':'A','fake_defA':'H'}
def classify(dh,dd,da):
    s = tuple(-1 if x<-TH else (1 if x>TH else 0) for x in (dh,dd,da))
    return PATTERN_MAP.get(s, ('neutral',None))

def load(year_cond):
    c = sqlite3.connect(DB)
    q = f"""
    SELECT match_date,
      MAX(CASE WHEN source='william_hill' THEN outcome END) outcome,
      MAX(CASE WHEN source='william_hill' THEN close_h END) wh_h,
      MAX(CASE WHEN source='william_hill' THEN close_d END) wh_d,
      MAX(CASE WHEN source='william_hill' THEN close_a END) wh_a,
      MAX(CASE WHEN source='interwetten' THEN close_h END) iw_h,
      MAX(CASE WHEN source='interwetten' THEN close_d END) iw_d,
      MAX(CASE WHEN source='interwetten' THEN close_a END) iw_a,
      MAX(CASE WHEN source='william_hill' THEN drift_h END) wh_dh,
      MAX(CASE WHEN source='william_hill' THEN drift_d END) wh_dd,
      MAX(CASE WHEN source='william_hill' THEN drift_a END) wh_da
    FROM odds_features
    WHERE open_h>0 AND close_h>0 AND outcome IS NOT NULL
      AND {year_cond}
    GROUP BY match_date, home_team, away_team
    HAVING SUM(source='william_hill')>0 AND SUM(source='interwetten')>0
    """
    m = pd.read_sql_query(q, c)
    c.close()
    wh = np.array([1/m['wh_h'].values, 1/m['wh_d'].values, 1/m['wh_a'].values]).T.astype(float)
    iw = np.array([1/m['iw_h'].values, 1/m['iw_d'].values, 1/m['iw_a'].values]).T.astype(float)
    wh = norm2(wh); iw = norm2(iw)
    m['wh_fav'] = np.argmax(wh, axis=1)
    m['iw_fav'] = np.argmax(iw, axis=1)
    cons = (wh+iw)/2
    m['cons_fav'] = np.argmax(cons, axis=1)
    m['agree'] = (m['wh_fav']==m['iw_fav'])
    m['out'] = m['outcome'].map({'H':0,'D':1,'A':2}).values
    # drift 意图(用 WH 的 drift)
    intents, targets = [], []
    for dh,dd,da in m[['wh_dh','wh_dd','wh_da']].values:
        if dh is None or dd is None or da is None:
            intents.append('neutral'); targets.append(None); continue
        it,tg = classify(dh,dd,da); intents.append(it); targets.append(tg)
    m['intent']=intents; m['target']=targets
    return m

def metrics(m, label):
    n = len(m)
    cons_acc = float((m['cons_fav'].values==m['out'].values).mean())
    dis = m[~m['agree']]
    dis_n = len(dis)
    dis_cons_acc = float((dis['cons_fav'].values==dis['out'].values).mean()) if dis_n else None
    # 少数派(非共识favorite)命中: 分歧时 cons_fav 之外的那个 book favorite
    if dis_n:
        minor = []
        for wf,ifv,cf,o in zip(dis['wh_fav'].values, dis['iw_fav'].values, dis['cons_fav'].values, dis['out'].values):
            mnpick = ifv if wf==cf else wf   # 与 cons_fav 不同的那个 favorite
            minor.append(int(mnpick==o))
        minor_acc = float(np.mean(minor))
    else:
        minor_acc = None
    # honest_def 对齐 (target 是字符串 'H'/'A', outcome 也是字符串, 直接比)
    hon = m[m['intent'].isin(['honest_defH','honest_defA'])]
    fak = m[m['intent'].isin(['fake_defH','fake_defA'])]
    hon_align = float((hon['target'].values==hon['outcome'].values).mean()) if len(hon) else None
    fak_align = float((fak['target'].values==fak['outcome'].values).mean()) if len(fak) else None
    return {
        'label': label, 'n': n,
        'consensus_acc': round(cons_acc,4),
        'disagree_n': dis_n, 'disagree_share': round(dis_n/n,4) if n else None,
        'disagree_cons_acc': round(dis_cons_acc,4) if dis_cons_acc is not None else None,
        'disagree_cons_acc_p_vs_overall': round(binom_p(dis_n, int((dis['cons_fav'].values==dis['out'].values).sum()), cons_acc),4) if dis_n else None,
        'minority_pick_acc': round(minor_acc,4) if minor_acc is not None else None,
        'honest_align': round(hon_align,4) if hon_align is not None else None, 'honest_n': int(len(hon)),
        'fake_align': round(fak_align,4) if fak_align is not None else None, 'fake_n': int(len(fak)),
    }

print("[OOS] 加载 2016(train) / 2017-2018(test) ...")
tr = load("match_date BETWEEN '2016-01-01' AND '2016-12-31'")
te = load("match_date BETWEEN '2017-01-01' AND '2018-12-31'")
print(f"  train 2016: n={len(tr)} | test 2017-2018: n={len(te)}")

tr_m = metrics(tr, 'train_2016')
te_m = metrics(te, 'test_2017_2018')
# 全量(2016-2018)作为参考基线, 与先前 validate_drift_clv 一致
allm = metrics(pd.concat([tr,te], ignore_index=True), 'full_2016_2018')

# 关键 OOS 判定: test 分歧共识命中 是否仍显著低于 test 整体共识命中
te_dis_p = te_m['disagree_cons_acc_p_vs_overall']
signal_survives = (te_m['disagree_cons_acc'] is not None
                   and te_m['disagree_cons_acc'] < te_m['consensus_acc'] - 0.10
                   and te_dis_p is not None and te_dis_p < 0.05)
hon_oos_ok = (te_m['honest_align'] is not None and te_m['honest_align'] > te_m['consensus_acc'])

RES = {
    'constraint_note': '2023+ odds_features 无 WH+IW 双庄同场(仅 interwetten), 双庄仅存 2016-2018; 严格 OOS 用窗口内切分 2016->2017-2018',
    'train': tr_m, 'test': te_m, 'full': allm,
    'oos_verdict': {
        'crossbook_disagreement_survives': bool(signal_survives),
        'honest_def_oos_above_baseline': bool(hon_oos_ok),
        'test_disagree_cons_acc': te_m['disagree_cons_acc'],
        'test_consensus_acc': te_m['consensus_acc'],
        'test_disagree_p': te_dis_p,
    }
}
stamp = pd.Timestamp.now().strftime('%Y%m%d')
jp = os.path.join(OUT, f'validate_drift_clv_oos_{stamp}.json')
with open(jp,'w',encoding='utf-8') as f:
    json.dump(RES, f, ensure_ascii=False, indent=2)
print("\n=== OOS 结果 ===")
for k,v in RES['oos_verdict'].items():
    print(f"  {k}: {v}")
print(f"\n  TRAIN 2016: 共识={tr_m['consensus_acc']:.3%} 分歧共识命中={tr_m['disagree_cons_acc']:.3%}(n={tr_m['disagree_n']}) 少数派={tr_m['minority_pick_acc']:.3%} honest={tr_m['honest_align']:.3%}")
print(f"  TEST  2017-18: 共识={te_m['consensus_acc']:.3%} 分歧共识命中={te_m['disagree_cons_acc']:.3%}(n={te_m['disagree_n']}, p={te_m['disagree_cons_acc_p_vs_overall']}) 少数派={te_m['minority_pick_acc']:.3%} honest={te_m['honest_align']:.3%}")
print(f"\nJSON -> {jp}")
print("DONE")

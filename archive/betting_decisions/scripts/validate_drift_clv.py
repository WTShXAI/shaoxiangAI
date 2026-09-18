"""P3-ext: drift/CLV 跨庄特征独立验证 (先验证后训练).
样本: odds_features 全部有效盘(302k) + 双庄家同场(WH+IW, n=16,109).
输出: 结构化结果 + deliverables/validate_drift_clv_<date>.json
不碰生产, 不靠外部源, 纯 odds_features 既有列.
"""
import os, sys, json, sqlite3
import numpy as np
import pandas as pd

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'football_data.db')
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'deliverables')
os.makedirs(OUT, exist_ok=True)

# ── 工具 ──
def norm(p):
    s = p.sum()
    return p / s if s > 0 else p

def argmax3(a):
    return int(np.argmax(a))

def binom_p(n, k, p0):
    """双侧二项检验 p 值 (精确, 小样本也稳). 用 scipy 否则正态近似."""
    try:
        from scipy.stats import binomtest
        return float(binomtest(k, n, p0).pvalue)
    except Exception:
        # 正态近似
        se = (p0*(1-p0)/n)**0.5
        z = (k/n - p0)/se if se>0 else 0
        from math import erf, sqrt
        return float(2*(1-0.5*(1+erf(abs(z)/sqrt(2)))))

# drift 意图(对齐 reverse_odds_engine.PATTERN_MAP)
TH = 0.02
PATTERN_MAP = {
    (-1,1,1): ('honest_defH','H'),
    (1,1,-1): ('honest_defA','A'),
    (-1,-1,1): ('fake_defH','A'),   # 诱盘假H, 实防A
    (1,-1,-1): ('fake_defA','H'),   # 诱盘假A, 实防H
    (-1,-1,-1): ('all_down',None),
    (1,1,1): ('all_up',None),
}
TARGET = {'honest_defH':'H','honest_defA':'A','fake_defH':'A','fake_defA':'H'}
def classify(dh,dd,da):
    s = tuple(-1 if x<-TH else (1 if x>TH else 0) for x in (dh,dd,da))
    return PATTERN_MAP.get(s, ('neutral',None))

RES = {}

# ════════════════════════════════════════════════════
# 任务30: drift 模式 → 赛果 对齐 (全量 302k, 单庄每行)
# ════════════════════════════════════════════════════
print("[T30] drift 模式 → 赛果 对齐 (全量) ...")
c = sqlite3.connect(DB)
df = pd.read_sql_query("""
    SELECT drift_h, drift_d, drift_a, cimp_h, cimp_d, cimp_a, outcome
    FROM odds_features
    WHERE open_h>0 AND close_h>0 AND outcome IS NOT NULL
""", c)
c.close()
imp = df[['cimp_h','cimp_d','cimp_a']].values
df['argmax'] = np.argmax(imp, axis=1)
base_acc = float((df['argmax'].values == df['outcome'].map({'H':0,'D':1,'A':2}).values).mean())
n = len(df)
# 分类意图
intents, targets = [], []
for dh,dd,da in df[['drift_h','drift_d','drift_a']].values:
    it, tg = classify(dh,dd,da)
    intents.append(it); targets.append(tg)
df['intent'] = intents; df['target'] = targets
out_num = df['outcome'].map({'H':0,'D':1,'A':2}).values

# 仅 4 个防守型意图有 target
def_rows = df[df['target'].notna()]
def_cnt = len(def_rows)
def_align = float((def_rows['target'].values == def_rows['outcome'].values).mean())
# 拆分 honest vs fake
hon = df[df['intent'].isin(['honest_defH','honest_defA'])]
fak = df[df['intent'].isin(['fake_defH','fake_defA'])]
hon_align = float((hon['target'].values == hon['outcome'].values).mean()) if len(hon) else None
fak_align = float((fak['target'].values == fak['outcome'].values).mean()) if len(fak) else None
# 分层: honest_def 对齐 vs 目标侧边际频率
marginal = df['outcome'].value_counts(normalize=True).to_dict()  # H/D/A 边际
hon_hits = int((hon['target'].values == hon['outcome'].values).sum())
# honest 目标侧混合边际(按 honest 行实际目标分布加权)
hon_tg_marg = float(np.mean([marginal[t] for t in hon['target'].values]))
hon_p_vs_marg = binom_p(len(hon), hon_hits, hon_tg_marg)
# 无显著 drift (neutral) 基线
neu = df[df['intent']=='neutral']
neu_acc = float((neu['argmax'].values == neu['outcome'].map({'H':0,'D':1,'A':2}).values).mean())

RES['T30_drift_pattern'] = {
    'n_total': n, 'baseline_argmax_acc': round(base_acc,4),
    'neutral_acc': round(neu_acc,4), 'neutral_n': int(len(neu)),
    'defensive_intent_n': def_cnt, 'defensive_align': round(def_align,4),
    'defensive_align_p_vs_1over3': round(binom_p(def_cnt, int((def_rows['target'].values==def_rows['outcome'].values).sum()), 1/3),4),
    'honest_align': round(hon_align,4) if hon_align is not None else None, 'honest_n': int(len(hon)),
    'fake_align': round(fak_align,4) if fak_align is not None else None, 'fake_n': int(len(fak)),
    'honest_align_p_vs_marginal': round(hon_p_vs_marg,4),
    'outcome_marginal': {k: round(float(v),4) for k,v in marginal.items()},
}
print(f"  基线 argmax 准确率 = {base_acc:.3%} | 防守意图对齐 = {def_align:.3%} (n={def_cnt}) | honest={hon_align:.3%} fake={fak_align:.3%}")

# ════════════════════════════════════════════════════
# 任务29: 跨庄发散 (CLV类) — WH+IW 同场 n=16,109
# ════════════════════════════════════════════════════
print("[T29] 跨庄发散 (CLV类) 验证 ...")
c = sqlite3.connect(DB)
q = """
SELECT match_date, home_team, away_team,
  MAX(CASE WHEN source='william_hill' THEN outcome END) outcome,
  MAX(CASE WHEN source='william_hill' THEN close_h END) wh_h,
  MAX(CASE WHEN source='william_hill' THEN close_d END) wh_d,
  MAX(CASE WHEN source='william_hill' THEN close_a END) wh_a,
  MAX(CASE WHEN source='interwetten' THEN close_h END) iw_h,
  MAX(CASE WHEN source='interwetten' THEN close_d END) iw_d,
  MAX(CASE WHEN source='interwetten' THEN close_a END) iw_a
FROM odds_features
WHERE open_h>0 AND close_h>0 AND outcome IS NOT NULL
GROUP BY match_date, home_team, away_team
HAVING SUM(source='william_hill')>0 AND SUM(source='interwetten')>0
"""
m = pd.read_sql_query(q, c)
c.close()
# 直接由赔率列构造 2D 隐含概率矩阵 (避免 list 列)
wh = np.array([1/m['wh_h'].values, 1/m['wh_d'].values, 1/m['wh_a'].values]).T.astype(float)
wh = wh / wh.sum(axis=1, keepdims=True)
iw = np.array([1/m['iw_h'].values, 1/m['iw_d'].values, 1/m['iw_a'].values]).T.astype(float)
iw = iw / iw.sum(axis=1, keepdims=True)
m['wh_imp'] = list(wh); m['iw_imp'] = list(iw)
cons = (wh + iw)/2
m['cons_fav'] = np.argmax(cons, axis=1)
m['wh_fav'] = np.argmax(wh, axis=1)
m['iw_fav'] = np.argmax(iw, axis=1)
m['agree'] = (m['wh_fav']==m['iw_fav'])
tvd = 0.5*np.sum(np.abs(wh - iw), axis=1)
m['tvd'] = tvd
maxdiff = np.max(np.abs(wh - iw), axis=1)
m['maxdiff'] = maxdiff
m['out'] = m['outcome'].map({'H':0,'D':1,'A':2}).values

n2 = len(m)
cons_acc = float((m['cons_fav'].values==m['out'].values).mean())
wh_acc = float((m['wh_fav'].values==m['out'].values).mean())
iw_acc = float((m['iw_fav'].values==m['out'].values).mean())

# 发散四分位: cons_fav 准确率趋势
m['tvd_q'] = pd.qcut(m['tvd'], 4, labels=['Q1低','Q2','Q3','Q4高'])
qacc = m.groupby('tvd_q', observed=True).apply(lambda g: (g['cons_fav'].values==g['out'].values).mean())
qn = m.groupby('tvd_q', observed=True).size()
# 线性趋势检验(accuracy vs tvd 分位中点)
qmid = m.groupby('tvd_q', observed=True)['tvd'].mean()
import scipy.stats as ss
if 'scipy' in sys.modules or True:
    try:
        slope, intercept, r, p_trend, se = ss.linregress(qmid.values, qacc.values)
        trend_p = float(p_trend)
    except Exception:
        trend_p = None

# 分歧子集 (agree=False): 共识 favorite 在分歧时命中率
dis = m[~m['agree']]
dis_n = len(dis)
dis_cons_acc = float((dis['cons_fav'].values==dis['out'].values).mean())
# 分歧时: 押 iw_fav 的命中率
dis_iw_acc = float((dis['iw_fav'].values==dis['out'].values).mean())
# 分歧时: 押 "非共识favorite"(即两庄中少数派) 的命中率 = 押 iw_fav(若与cons不同) 实际已是
# 更直接: 分歧时 cons_fav 命中率 vs 整体 cons_acc
dis_p = binom_p(dis_n, int((dis['cons_fav'].values==dis['out'].values).sum()), cons_acc)

RES['T29_crossbook'] = {
    'n_dual': n2,
    'consensus_acc': round(cons_acc,4), 'wh_acc': round(wh_acc,4), 'iw_acc': round(iw_acc,4),
    'tvd_median': round(float(m['tvd'].median()),4), 'tvd_max': round(float(m['tvd'].max()),4),
    'tvd_q_acc': {k: round(float(v),4) for k,v in qacc.items()},
    'tvd_q_n': {k: int(v) for k,v in qn.items()},
    'trend_slope_per_unit_tvd': round(float(slope),4) if 'slope' in dir() else None,
    'trend_p': trend_p,
    'disagree_n': dis_n, 'disagree_share': round(dis_n/n2,4),
    'disagree_cons_acc': round(dis_cons_acc,4),
    'disagree_cons_acc_p_vs_overall': round(dis_p,4),
    'disagree_iw_fav_acc': round(dis_iw_acc,4),
}
print(f"  双庄共识准确率 = {cons_acc:.3%} | WH={wh_acc:.3%} IW={iw_acc:.3%}")
print(f"  TVD 中位={m['tvd'].median():.3f} 分歧占比={dis_n/n2:.1%} 分歧时共识命中={dis_cons_acc:.3%} (vs 整体 {cons_acc:.3%}, p={dis_p:.3f})")
print(f"  发散四分位准确率: {dict(qacc.round(4))}")

# ════════════════════════════════════════════════════
# 保存
# ════════════════════════════════════════════════════
stamp = pd.Timestamp.now().strftime('%Y%m%d')
json_path = os.path.join(OUT, f'validate_drift_clv_{stamp}.json')
with open(json_path,'w',encoding='utf-8') as f:
    json.dump(RES, f, ensure_ascii=False, indent=2)
print(f"\nJSON -> {json_path}")
print("DONE")

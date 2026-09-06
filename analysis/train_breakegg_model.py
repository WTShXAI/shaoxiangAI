# -*- coding: utf-8 -*-
"""
破蛋模型 v1 — 学习"开盘结构 → 半场破蛋(HT进≥1球)条件"
特征 = 开盘赔率结构(OU/AH/1X2 全场 + OU半场) 仅取开赛前最早快照(captured_at < kickoff+300s)
目标 = HT_goalless (半场是否 0-0), 1=0-0未破蛋
评估 = GroupKFold-by-kickoff-date (防同日风格泄漏), AUC vs 基线
"""
import sqlite3, math, json
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import GradientBoostingClassifier
GBM = GradientBoostingClassifier  # 别名, 供 eval 复用
from sklearn.model_selection import GroupKFold
from sklearn.metrics import roc_auc_score, brier_score_loss

GQ = 'data/events.db'
OUDB = 'data/ou_opening_analysis.db'
GRACE = 300

def demarg(h, d, a):
    inv = 1.0/h + 1.0/d + 1.0/a
    return (1.0/h)/inv, (1.0/d)/inv, (1.0/a)/inv

def demarg2(o, u):
    inv = 1.0/o + 1.0/u
    return (1.0/o)/inv, (1.0/u)/inv

con = sqlite3.connect(GQ)
con.execute(f"ATTACH DATABASE '{OUDB}' AS ou")
cur = con.cursor()

# kickoff parse: 双格式 -> unix
KICK = """
WITH kick AS (
  SELECT match_key,
    CASE
      WHEN kickoff LIKE '%Z' THEN strftime('%s', REPLACE(kickoff,'Z',''))
      WHEN kickoff LIKE '%:%' THEN strftime('%s', kickoff||':00') - 8*3600
      ELSE NULL END AS kots
  FROM matches
)
"""

# --- 1X2 开盘(开赛前最早) ---
q1x2 = KICK + """
SELECT k.match_key, s.selection, s.odds, s.captured_at
FROM odds_snapshots s JOIN kick k ON s.match_key=k.match_key
WHERE s.market='1X2' AND s.selection IN ('home','draw','away')
  AND k.kots IS NOT NULL AND s.captured_at < k.kots + ?
ORDER BY s.match_key, s.selection, s.captured_at ASC
"""
rows = cur.execute(q1x2, (GRACE,)).fetchall()
x1 = {}
for mk, sel, odds, cap in rows:
    if mk not in x1:
        x1[mk] = {}
    if sel not in x1[mk]:
        x1[mk][sel] = odds

# --- OU 半场开盘 ---
q1h = KICK + """
SELECT k.match_key, s.market, s.selection, s.odds, s.captured_at
FROM odds_snapshots s JOIN kick k ON s.match_key=k.match_key
WHERE s.market LIKE 'OU_1H_%' AND s.selection IN ('over','under')
  AND k.kots IS NOT NULL AND s.captured_at < k.kots + ?
ORDER BY s.match_key, s.market, s.selection, s.captured_at ASC
"""
rows = cur.execute(q1h, (GRACE,)).fetchall()
ou1h = {}
for mk, mkt, sel, odds, cap in rows:
    if mk not in ou1h:
        ou1h[mk] = {}
    if mkt not in ou1h[mk]:
        ou1h[mk][mkt] = {}
    if sel not in ou1h[mk][mkt]:
        ou1h[mk][mkt][sel] = odds

# --- AH 开盘(取最早一条 AH 行, 记录 line + 两侧赔率) ---
qah = KICK + """
SELECT k.match_key, s.market, s.selection, s.odds, s.captured_at
FROM odds_snapshots s JOIN kick k ON s.match_key=k.match_key
WHERE s.market LIKE 'AH_%' AND s.market NOT LIKE 'AH_1H%' AND s.market NOT LIKE 'AH_2H%'
  AND s.selection IN ('home','away')
  AND k.kots IS NOT NULL AND s.captured_at < k.kots + ?
ORDER BY s.match_key, s.captured_at ASC
"""
rows = cur.execute(qah, (GRACE,)).fetchall()
ah = {}
for mk, mkt, sel, odds, cap in rows:
    if mk not in ah:
        line = float(mkt.replace('AH_', ''))
        ah[mk] = {'line': line, 'home': None, 'away': None}
    if sel == 'home' and ah[mk]['home'] is None:
        ah[mk]['home'] = odds
    if sel == 'away' and ah[mk]['away'] is None:
        ah[mk]['away'] = odds

# --- 主表: ou_clean(已含真实开盘OU) + GQ matches(HT) ---
qmain = """
SELECT c.match_key, c.line, c.over_odds, c.under_odds, c.league, m.kickoff,
       m.ht_score_home, m.ht_score_away, m.score_home, m.score_away
FROM ou.ou_clean c
JOIN matches m ON m.match_key=c.match_key
WHERE m.ht_score_home IS NOT NULL AND m.ht_score_away IS NOT NULL
  AND m.score_home IS NOT NULL AND m.score_away IS NOT NULL
  -- HT 污染清洗(2026-08-27): 半场总进球须 < 全场总进球, 否则 ht 被回填为全场
  AND (m.ht_score_home + m.ht_score_away) < (m.score_home + m.score_away)
  AND m.status IN ('finished','FT','complete','ended')
"""
rows = cur.execute(qmain).fetchall()

X, y, groups, meta = [], [], [], []
for mk, ou_line, ou_o, ou_u, league, ko, hth, hta, sh, sa in rows:
    if ou_line is None or ou_o is None or ou_u is None:
        continue
    x1d = x1.get(mk)
    if not x1d or not all(k in x1d for k in ('home','draw','away')):
        continue  # 必须三件套齐全
    ph, pd, pa = demarg(x1d['home'], x1d['draw'], x1d['away'])
    pro, pru = demarg2(ou_o, ou_u)
    # 半场 OU: 取第一条 available 线
    ht_line = ht_po = ht_pu = None
    for mkt, d in sorted(ou1h.get(mk, {}).items()):
        if 'over' in d and 'under' in d and d['over'] and d['under']:
            ht_line = float(mkt.replace('OU_1H_', ''))
            ht_po, ht_pu = demarg2(d['over'], d['under'])
            break
    ah_line = ah_fav = None
    ad = ah.get(mk)
    if ad and ad['home'] and ad['away']:
        ah_line = ad['line']
        # favorite = 低赔一方
        if ad['home'] <= ad['away']:
            pf, _ = demarg2(ad['home'], ad['away']); ah_fav = pf
        else:
            _, pf = demarg2(ad['home'], ad['away']); ah_fav = pf
    ht_goalless = 1 if ((hth or 0) + (hta or 0)) == 0 else 0
    feats = [ou_line, pro, pru, ph, pd, pa,
             max(ph, pd, pa),       # 1X2 最大概率(热门强度)
             pd,                    # 平局概率
             (max(ph, pd, pa) - sorted([ph, pd, pa])[1]),  # 热门-次热 间隙(强弱差)
             (ht_line if ht_line else 0.0),
             (ht_po if ht_po else 0.0),
             (ah_line if ah_line is not None else 0.0),
             (ah_fav if ah_fav else 0.0)]
    X.append(feats)
    y.append(ht_goalless)
    groups.append((ko or '')[:10])
    meta.append({'match_key': mk, 'league': league, 'total': (sh or 0)+(sa or 0)})

X = np.array(X); y = np.array(y)
print("样本:", len(y), " HT 0-0 占比(正类):", round(100*y.mean(), 1), "%")

# --- GroupKFold by kickoff date ---
# 联赛目标编码(按折内训练集均值, 防泄漏)
leagues = np.array([m['league'] for m in meta])
gkf = GroupKFold(n_splits=5)
def eval_model(clf, name, use_league=False):
    aucs, briers, base_aucs = [], [], []
    for tr, te in gkf.split(X, y, groups):
        Xtr, Xte = X[tr].copy(), X[te].copy()
        if use_league:
            # 折内联赛均值编码
            lg_mean = {}
            from collections import defaultdict
            acc = defaultdict(lambda:[0,0])
            for lm, ly in zip(leagues[tr], y[tr]):
                acc[lm][0]+=1; acc[lm][1]+=ly
            gmean = y[tr].mean()
            lg_mean = {l: (v[1]/v[0] if v[0]>0 else gmean) for l,v in acc.items()}
            enc_tr = np.array([lg_mean.get(l, gmean) for l in leagues[tr]]).reshape(-1,1)
            enc_te = np.array([lg_mean.get(l, gmean) for l in leagues[te]]).reshape(-1,1)
            Xtr = np.hstack([Xtr, enc_tr]); Xte = np.hstack([Xte, enc_te])
        clf.fit(Xtr, y[tr])
        p = clf.predict_proba(Xte)[:, 1]
        aucs.append(roc_auc_score(y[te], p))
        briers.append(brier_score_loss(y[te], p))
        base_aucs.append(roc_auc_score(y[te], [y[tr].mean()] * len(te)))
    print(f"{name:32s} AUC={np.mean(aucs):.4f}±{np.std(aucs):.4f}  Brier={np.mean(briers):.4f}  常量基线AUC={np.mean(base_aucs):.4f}")

eval_model(LogisticRegression(max_iter=1000, C=1.0), "Logistic(结构9特征)")
eval_model(LogisticRegression(max_iter=1000, C=1.0), "Logistic(结构+联赛编码)", use_league=True)
eval_model(GradientBoostingClassifier(n_estimators=200, max_depth=3, learning_rate=0.05, subsample=0.8), "GBM(结构9特征)")
eval_model(GBM(n_estimators=200, max_depth=3, learning_rate=0.05, subsample=0.8), "GBM(结构+联赛编码)", use_league=True)

# 对比: 仅用 OU line 的单特征模型
Xline = X[:, [0]]
eval_model(LogisticRegression(max_iter=1000), "Logistic(仅OU线)")

# 保存最终模型(全量训练)
from sklearn.ensemble import GradientBoostingClassifier as GBM
final = GBM(n_estimators=200, max_depth=3, learning_rate=0.05, subsample=0.8).fit(X, y)
import pickle
with open('models/breakegg_ht_model.pkl', 'wb') as f:
    pickle.dump({'model': final, 'features': ['ou_line','ou_p_over','ou_p_under','x1_home','x1_draw','x1_away','x1_fav','x1_draw_p','x1_gap','ht_ou_line','ht_p_over','ah_line','ah_fav']}, f)
print("saved models/breakegg_ht_model.pkl")

# 联赛维度破蛋率(供前端参考)
from collections import defaultdict
lg = defaultdict(lambda: [0,0])
for m, yy in zip(meta, y):
    lg[m['league']][0] += 1; lg[m['league']][1] += yy
print("\n联赛 HT 0-0 率(样本>=50):")
for l,(n,zz) in sorted(lg.items(), key=lambda kv:-kv[1][0]):
    if n>=50:
        print(f"  {str(l)[:24]:24s} n={n:5d}  HT0-0={100*zz/n:.1f}%")
con.close()
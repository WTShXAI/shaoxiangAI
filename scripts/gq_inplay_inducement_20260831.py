# -*- coding: utf-8 -*-
"""GQ 滚球诱盘检验 v2 (采集器数据, IR-04 干净子集)
假设: 庄家进球后用即时线"诱盘"(过度压低 over / 诱导 over 注) → 押 under 现 +EV。
方法: 每场首粒球 G; 取主 OU 窗口 [G-12, G-15] 全部 OU 快照, 拆:
   进球前稳态 [G-12, G-3] (baseline) | 进球后即时 [G+2, G+15] (诱盘态)
over/under 允许 ±1 分钟配对, 任意 OU 线(各注按自身线结算)。
"""
import sqlite3, numpy as np, random
from collections import defaultdict
random.seed(31); np.random.seed(31)
g = sqlite3.connect('data/GQ.db'); g.execute('PRAGMA busy_timeout=60000')
g.execute('CREATE TEMP TABLE IF NOT EXISTS ck(k TEXT PRIMARY KEY)')
clean = [r[0] for r in g.execute(
    "SELECT DISTINCT match_key FROM odds_snapshots WHERE score_at IS NOT NULL AND score_at!='' AND score_at!='0-0'")]
g.executemany('INSERT OR IGNORE INTO ck VALUES (?)', [(k,) for k in clean])

finals = {}
for mk, sh, sa in g.execute("SELECT m.match_key, m.score_home, m.score_away FROM matches m JOIN ck ON m.match_key=ck.k"):
    try: finals[mk] = int(sh)+int(sa)
    except Exception: pass
first_goal = dict(g.execute(
    "SELECT match_key, MIN(minute_at) FROM odds_snapshots WHERE match_key IN (SELECT k FROM ck) "
    "AND score_at IS NOT NULL AND score_at!='' AND score_at!='0-0' GROUP BY match_key"))

def best_pair(rows, lo, hi):
    cand = [r for r in rows if lo <= r[0] <= hi]
    by_min = defaultdict(dict)
    for mn, sel, odds, line in cand:
        try: by_min[mn][sel] = (float(odds), float(line))
        except Exception: pass
    cands = []
    for mn in by_min:
        if 'over' in by_min[mn] and 'under' in by_min[mn]:
            cands.append((mn, mn, by_min[mn]['over'], by_min[mn]['under']))
        else:
            for adj in (mn-1, mn+1):
                if adj in by_min:
                    if 'over' in by_min[mn] and 'under' in by_min[adj]:
                        cands.append((mn, adj, by_min[mn]['over'], by_min[adj]['under'])); break
                    if 'under' in by_min[mn] and 'over' in by_min[adj]:
                        cands.append((adj, mn, by_min[adj]['over'], by_min[mn]['under'])); break
    if not cands: return None
    center = (lo+hi)/2.0
    best = min(cands, key=lambda c: abs(((c[0]+c[1])/2)-center))
    return best  # (mn_o, mn_u, (o_odds,L), (u_odds,_))

pre_over, pre_under, post_over, post_under = [], [], [], []
pre_buck = defaultdict(lambda: ([], [])); post_buck = defaultdict(lambda: ([], []))
cnt_pre = cnt_post = 0
for mk in list(first_goal):
    G = first_goal[mk]
    if mk not in finals: continue
    tot = finals[mk]
    lo_q, hi_q = max(0, G-12), G+15
    rows = g.execute("""SELECT minute_at, selection, odds, line FROM odds_snapshots
        WHERE match_key=? AND market LIKE 'OU%' AND minute_at BETWEEN ? AND ?""", (mk, lo_q, hi_q)).fetchall()
    # 进球前
    if G >= 12:
        p = best_pair(rows, G-12, G-3)
        if p:
            cnt_pre += 1
            (o_odds, L), (u_odds, _) = p[2], p[3]
            if o_odds > 0 and u_odds > 0:
                inv = 1/o_odds + 1/u_odds; po = (1/o_odds)/inv
                ow = 1 if tot > L else 0
                pre_over.append((o_odds, ow==1)); pre_under.append((u_odds, ow==0))
                lab = 'a.<.45' if po<0.45 else ('b.45-50' if po<0.50 else ('c.50-55' if po<0.55 else 'd.>=.55'))
                pre_buck[lab][0].append(po); pre_buck[lab][1].append(ow)
    # 进球后
    if G <= 85:
        p = best_pair(rows, G+2, G+15)
        if p:
            cnt_post += 1
            (o_odds, L), (u_odds, _) = p[2], p[3]
            if o_odds > 0 and u_odds > 0:
                inv = 1/o_odds + 1/u_odds; po = (1/o_odds)/inv
                ow = 1 if tot > L else 0
                post_over.append((o_odds, ow==1)); post_under.append((u_odds, ow==0))
                lab = 'a.<.45' if po<0.45 else ('b.45-50' if po<0.50 else ('c.50-55' if po<0.55 else 'd.>=.55'))
                post_buck[lab][0].append(po); post_buck[lab][1].append(ow)

def roi_bootstrap(bets, n=2000):
    if len(bets) < 30: return None, 0, None, None
    prof = np.array([(o-1) if w else -1.0 for o, w in bets])
    N = prof.shape[0]; rois = np.empty(n)
    for i in range(n): rois[i] = np.random.choice(prof, size=N, replace=True).mean()
    rois.sort(); return float(rois.mean()), N, float(rois[50]), float(rois[1950])

def pr(t, over_bets, under_bets, buck):
    print(f"\n=== {t} (over n={len(over_bets)}, under n={len(under_bets)}) ===")
    print(f"{'bucket':<10}{'n':>6}{'impO':>8}{'actO':>8}{'bias':>8}")
    for k in sorted(buck):
        imp, act = buck[k]
        if not imp: continue
        print(f"{k:<10}{len(imp):>6}{sum(imp)/len(imp)*100:>7.1f}%{sum(act)/len(act)*100:>7.1f}%{(sum(act)/len(act)-sum(imp)/len(imp))*100:>+7.1f}%")
    ro, n, lo, hi = roi_bootstrap(over_bets)
    if ro is not None: print(f"  OVER  ROI={ro*100:+.2f}% CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")
    ru, n, lo, hi = roi_bootstrap(under_bets)
    if ru is not None: print(f"  UNDER ROI={ru*100:+.2f}% CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")

print(f"[matches w/ pre pair={cnt_pre}, post pair={cnt_post}]")
print("############ 进球前稳态 (baseline) ############")
pr('PRE', pre_over, pre_under, pre_buck)
print("\n############ 进球后即时 (诱盘态) ############")
pr('POST', post_over, post_under, post_buck)
print("[done]")

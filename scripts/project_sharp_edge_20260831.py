# -*- coding: utf-8 -*-
"""项目化收尾:
1. favorite-longshot 在"锐庄/低抽水"下的投影 ROI (用户"打穿盘口"需对的地方)
2. 强热门(implied>=0.70) 单独 ROI (本庄 + 锐庄投影)
3. GQ.db 滚球 OU 可行性 (in-play 线 + 干净赛果子集)
"""
import sqlite3, numpy as np, random
random.seed(11); np.random.seed(11)
con = sqlite3.connect('data/rollball_training.db')
cur = con.cursor()
rows = cur.execute("""SELECT cl_h,cl_d,cl_a, p_h,p_d,p_a, result, total_goals
    FROM rb_matches WHERE result IN ('H','D','A') AND total_goals IS NOT NULL
    AND cl_h IS NOT NULL AND p_h IS NOT NULL""").fetchall()

def roi_bootstrap(bets, n=2000):
    if len(bets) < 30: return None, 0, None, None
    prof = np.array([(o-1) if w else -1.0 for o, w in bets])
    N = prof.shape[0]; rois = np.empty(n)
    for i in range(n): rois[i] = np.random.choice(prof, size=N, replace=True).mean()
    rois.sort(); return float(profits_mean(prof)), N, float(rois[50]), float(rois[1950])
def profits_mean(prof): return float(prof.mean())

THIS_MARGIN = 0.089
SHARP_MARGIN = 0.03
SCALE = (1-THIS_MARGIN)/(1-SHARP_MARGIN)  # 锐庄 decimal odds 更高系数 ~1.065

def top_fav_roi(min_imp):
    bets_this, bets_sharp = [], []
    for r in rows:
        ph, pd, pa = r[3], r[4], r[5]; ch, cd, ca = r[0], r[1], r[2]
        fav = max(range(3), key=lambda i: [ph,pd,pa][i])
        imp = [ph,pd,pa][fav]; odds = [ch,cd,ca][fav]
        if imp >= min_imp:
            win = r[6] == ['H','D','A'][fav]
            bets_this.append((odds, win))
            bets_sharp.append((odds*SCALE, win))
    ri, n, lo, hi = roi_bootstrap(bets_this)
    rs, ns, los, his = roi_bootstrap(bets_sharp)
    print(f"  fav implied>={min_imp}: n={n} THISbook ROI={ri*100:+.2f}% | SHARPbook ROI={rs*100:+.2f}% CI[{los*100:+.1f}%,{his*100:+.1f}%]")

print("=== FAVORITE-LONGSHOT @ sharp book projection ===")
top_fav_roi(0.55)
top_fav_roi(0.65)
top_fav_roi(0.70)
top_fav_roi(0.80)

# 全样本强热门(>=0.60) 不分桶
bets = []
for r in rows:
    ph,pd,pa=r[3],r[4],r[5]; ch,cd,ca=r[0],r[1],r[2]
    fav=max(range(3),key=lambda i:[ph,pd,pa][i]); imp=[ph,pd,pa][fav]
    if imp>=0.60:
        bets.append((ch if fav==0 else (cd if fav==1 else ca), r[6]==['H','D','A'][fav]))
ri,n,lo,hi=roi_bootstrap(bets)
print(f"  ALL fav>=0.60: THIS={ri*100:+.2f}% n={n}")

print("\n=== GQ.db in-play OU 可行性 ===")
g = sqlite3.connect('data/GQ.db')
gt = [r[0] for r in g.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("GQ tables:", gt[:20])
# odds_snapshots 结构
try:
    cols=[c[1] for c in g.execute('PRAGMA table_info(odds_snapshots)').fetchall()]
    print("odds_snapshots cols:", cols[:30])
    nsnap=g.execute('SELECT COUNT(*) FROM odds_snapshots').fetchone()[0]
    print("odds_snapshots rows:", nsnap)
    # OU market 样本
    samp=g.execute("SELECT market, selection, odds, captured_at FROM odds_snapshots WHERE market LIKE 'OU%' LIMIT 5").fetchall()
    print("OU sample:", samp)
except Exception as e:
    print("GQ err:", e)
print("[done]")

# -*- coding: utf-8 -*-
"""OU UNDER 边 完整性核验 (防 data artifact) + 修正 draw bug
1. 校验 over_ou 定义是否与 total_goals>ou_line 一致
2. ou_line 分布 / 子集联赛·日期构成 (代表性)
3. 干净规则: implied over>=0.50 时 BACK-UNDER, bootstrap CI
4. 修正: 真·no-draw(同时押 H 与 A 两头, 非 draw 才赢一边) ROI
"""
import sqlite3, numpy as np, random
random.seed(7); np.random.seed(7)
con = sqlite3.connect('data/rollball_training.db')
cur = con.cursor()
rows = cur.execute("""
    SELECT op_h,op_d,op_a, cl_h,cl_d,cl_a, p_h,p_d,p_a, drift_h,drift_d,drift_a,
           result, is_draw, total_goals, ou_line, ou_over, ou_under, p_over,p_under, over_ou, league, date
    FROM rb_matches
    WHERE result IN ('H','D','A') AND total_goals IS NOT NULL
""").fetchall()

# ── 1. over_ou 定义校验 ──
mism = 0; chk = 0
for r in rows:
    ou_line, tg, over_ou = r[15], r[14], r[20]
    if ou_line is None or over_ou is None: continue
    chk += 1
    correct = 1 if tg > ou_line else 0
    if correct != over_ou: mism += 1
print(f"[def-check] over_ou vs (total>line): checked={chk} mismatches={mism} ({100*mism/max(chk,1):.2f}%)")

# ── 2. 子集构成 ──
ou_rows = [r for r in rows if r[15] is not None and r[19] is not None]
from collections import Counter
lines = Counter(r[15] for r in ou_rows)
leag = Counter(r[21] for r in ou_rows)
dates = [r[22] for r in ou_rows if r[22]]
print(f"[subset] OU n={len(ou_rows)} lines={dict(lines)}")
print(f"[subset] top leagues={leag.most_common(8)}")
if dates:
    print(f"[subset] date range: {min(dates)} .. {max(dates)}")

# ── 3. 干净规则: implied over>=0.50 → BACK-UNDER ──
def roi_bootstrap(bets, n=2000):
    if len(bets) < 30: return None, 0, None, None
    prof = np.array([(o-1) if w else -1.0 for o, w in bets])
    N = prof.shape[0]
    rois = np.empty(n)
    for i in range(n):
        rois[i] = np.random.choice(prof, size=N, replace=True).mean()
    rois.sort()
    return float(rois.mean()), N, float(rois[50]), float(rois[1950])

under_bets = []
for r in ou_rows:
    p_over, un, over_ou = r[19], r[17], r[20]
    if None in (p_over, un, over_ou): continue
    if p_over >= 0.50:
        under_bets.append((un, over_ou == 0))
roi, n, lo, hi = roi_bootstrap(under_bets)
print(f"\n[rule] BACK-UNDER when implied over>=0.50: ROI={roi*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")

# 也试 implied over>=0.52
under_bets2 = [(r[17], r[20]==0) for r in ou_rows if r[19] is not None and r[17] is not None and r[20] is not None and r[19]>=0.52]
roi, n, lo, hi = roi_bootstrap(under_bets2)
print(f"[rule] BACK-UNDER when implied over>=0.52: ROI={roi*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")

# 分 line 看 under ROI
for ln in sorted(lines):
    bets = [(r[17], r[20]==0) for r in ou_rows if r[15]==ln and r[17] is not None and r[20] is not None]
    if len(bets) < 50: continue
    roi, n, lo, hi = roi_bootstrap(bets)
    print(f"   line={ln}: BACK-UNDER ROI={roi*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")

# ── 4. 修正 no-draw (两头同押, 各 0.5 stake; 非 draw 时赢一边) ──
nodraw = []
for r in rows:
    ch, cd, ca = r[3], r[4], r[5]
    if None in (ch, cd, ca): continue
    # stake 0.5 on H, 0.5 on A. if H wins: +0.5*(ch-1); if A wins: +0.5*(ca-1); if D: -1.0
    if r[12] == 'H': p = 0.5*(ch-1) - 0.5
    elif r[12] == 'A': p = 0.5*(ca-1) - 0.5
    else: p = -1.0
    nodraw.append(p)
nodraw = np.array(nodraw)
print(f"\n[fix] NO-DRAW (0.5H+0.5A) ROI={nodraw.mean()*100:+.2f}% n={len(nodraw)}")
print("[done]")

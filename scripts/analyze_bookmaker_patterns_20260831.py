# -*- coding: utf-8 -*-
"""盘口结构偏态拟合验证 (2026-08-31, 用户挑战: "AI连盘口模式都分析不出?")
=====================================================================
用 data/rollball_training.db (rb_matches, 318,941 场, 真实 H/D/A 赛果) 验证四类
可结构化盘口偏态是否给出可验证 edge:
  A. favorite-longshot bias (冷热赔率偏态)
  B. draw inflation (平局溢价)
  C. drift / smart-money (优盘: 开盘→收盘缩短的一方)
  D. home bias (主场溢价)
  + OU efficiency (大小球, 仅 ~6k 场有盘口)
所有 ROI 用收盘 decimal odds (cl_*) 结算; bias = actual - implied (无抽水信号)。
bootstrap 95% CI (1000 resample) 判定显著性。
"""
import sqlite3, math, json, random
import numpy as np
random.seed(42)
np.random.seed(42)
con = sqlite3.connect('data/rollball_training.db')
cur = con.cursor()

rows = cur.execute("""
    SELECT op_h,op_d,op_a, cl_h,cl_d,cl_a, p_h,p_d,p_a, drift_h,drift_d,drift_a,
           result, is_draw, total_goals, ou_line, ou_over, ou_under, p_over,p_under, over_ou
    FROM rb_matches
    WHERE result IN ('H','D','A') AND total_goals IS NOT NULL
""").fetchall()
print(f"[load] {len(rows)} matches with outcome")

def devig(h, d, a):
    inv = 1/h + 1/d + 1/a
    return (1/h)/inv, (1/d)/inv, (1/a)/inv

def roi_of(bets):
    """bets = list of (odds, win_bool). flat 1-unit stake. returns ROI, n."""
    if not bets: return None, 0
    profit = sum((o - 1) if w else -1 for o, w in bets)
    return profit / len(bets), len(bets)

def bootstrap_roi(bets, n=1000):
    if len(bets) < 30: return (None, None)
    profits = np.array([(o - 1) if w else -1.0 for o, w in bets], dtype=float)
    N = profits.shape[0]
    rois = np.empty(n, dtype=float)
    for i in range(n):
        rois[i] = np.random.choice(profits, size=N, replace=True).mean()
    rois.sort()
    return float(rois[25]), float(rois[975])

def bias_table(title, buckets):
    """buckets = list of (label, implied_list, actual_list). print actual vs implied."""
    print(f"\n=== {title} ===")
    print(f"{'bucket':<14}{'n':>8}{'implied':>9}{'actual':>9}{'bias':>9}")
    for lab, imp, act in buckets:
        if not imp: continue
        mi = sum(imp)/len(imp); ma = sum(act)/len(act)
        print(f"{lab:<14}{len(imp):>8}{mi*100:>8.1f}%{ma*100:>8.1f}%{(ma-mi)*100:>+8.1f}%")

# ───────────────────────── A. FAVORITE-LONGSHOT (closing) ─────────────────────────
# 用收盘隐含概率 p_* 定 favorite(最短赔率=最高隐含). bucket by favorite implied.
fl_buckets = {}  # label -> (implied_probs, actual_win)
fl_roi_bets = []  # back favorite at closing
ls_roi_bets = []  # back longshot at closing
for r in rows:
    ph, pd, pa, ch, cd, ca = r[6], r[7], r[8], r[3], r[4], r[5]
    if None in (ph, pd, pa, ch, cd, ca): continue
    probs = [ph, pd, pa]; odds = [ch, cd, ca]
    fav = max(range(3), key=lambda i: probs[i])  # favorite = highest implied
    lon = min(range(3), key=lambda i: probs[i])  # longshot = lowest implied
    # bucket by favorite implied
    imp = probs[fav]
    if imp < 0.30: lab = 'A.<0.30'
    elif imp < 0.40: lab = 'B.0.30-0.40'
    elif imp < 0.50: lab = 'C.0.40-0.50'
    elif imp < 0.60: lab = 'D.0.50-0.60'
    elif imp < 0.70: lab = 'E.0.60-0.70'
    elif imp < 0.80: lab = 'F.0.70-0.80'
    else: lab = 'G.>=0.80'
    fl_buckets.setdefault(lab, ([], []))
    fl_buckets[lab][0].append(imp)
    fl_buckets[lab][1].append(1.0 if r[12] == ['H','D','A'][fav] else 0.0)
    fl_roi_bets.append((odds[fav], r[12] == ['H','D','A'][fav]))
    ls_roi_bets.append((odds[lon], r[12] == ['H','D','A'][lon]))

bias_table("A. FAVORITE-LONGSHOT BIAS (closing implied vs actual win%)",
           [(k, v[0], v[1]) for k, v in sorted(fl_buckets.items())])
roi, n = roi_of(fl_roi_bets); lo, hi = bootstrap_roi(fl_roi_bets)
print(f"[A] BACK-FAVORITE@close  ROI={roi*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")
roi, n = roi_of(ls_roi_bets); lo, hi = bootstrap_roi(ls_roi_bets)
print(f"[A] BACK-LONGSHOT@close ROI={roi*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")

# ───────────────────────── B. DRAW INFLATION ─────────────────────────
dr_buckets = {}
nodraw_roi = []  # back 'not draw' = back the higher-odds of H/A at closing
for r in rows:
    pd, cd, ca, ch = r[7], r[4], r[5], r[3]
    if None in (pd, cd, ca, ch): continue
    imp = pd
    if imp < 0.22: lab = 'a.<0.22'
    elif imp < 0.26: lab = 'b.0.22-0.26'
    elif imp < 0.30: lab = 'c.0.26-0.30'
    elif imp < 0.34: lab = 'd.0.30-0.34'
    else: lab = 'e.>=0.34'
    dr_buckets.setdefault(lab, ([], []))
    dr_buckets[lab][0].append(imp)
    dr_buckets[lab][1].append(1.0 if r[13] == 1 else 0.0)  # actual draw rate
    # no-draw: back better of H/A (higher odds = lower implied), settle if not draw
    o_h, o_a = ch, ca
    best = o_h if o_h >= o_a else o_a
    nodraw_roi.append((best, r[13] == 0))  # win if not draw

bias_table("B. DRAW INFLATION (implied draw% vs actual draw%)",
           [(k, v[0], v[1]) for k, v in sorted(dr_buckets.items())])
roi, n = roi_of(nodraw_roi); lo, hi = bootstrap_roi(nodraw_roi)
print(f"[B] BACK-NO-DRAW@close  ROI={roi*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")

# ───────────────────────── C. DRIFT / SMART-MONEY (优盘) ─────────────────────────
# drift_h = (cl-op)/op ; negative = odds shortened (steam on). Bucket by min drift side.
drift_bets_neg = []  # back the side whose odds shortened most (most negative drift)
drift_bets_pos = []  # back the side whose odds lengthened most
for r in rows:
    oh, od, oa, ch, cd, ca = r[0], r[1], r[2], r[3], r[4], r[5]
    dh, dd, da = r[9], r[10], r[11]
    if None in (oh, od, oa, ch, cd, ca, dh, dd, da): continue
    drifts = [dh, dd, da]; odds = [ch, cd, ca]
    # side with most negative drift (shortened) = smart money
    short_i = min(range(3), key=lambda i: drifts[i])
    long_i = max(range(3), key=lambda i: drifts[i])
    drift_bets_neg.append((odds[short_i], r[12] == ['H','D','A'][short_i]))
    drift_bets_pos.append((odds[long_i], r[12] == ['H','D','A'][long_i]))

roi, n = roi_of(drift_bets_neg); lo, hi = bootstrap_roi(drift_bets_neg)
print(f"\n=== C. DRIFT / SMART-MONEY (优盘) ===")
print(f"[C] BACK-SHORTENED(closing) ROI={roi*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")
roi, n = roi_of(drift_bets_pos); lo, hi = bootstrap_roi(drift_bets_pos)
print(f"[C] BACK-LENGTHENED(closing) ROI={roi*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")

# sharper: only strong drift (|drift|>=0.05)
drift_strong = []
for r in rows:
    oh, od, oa, ch, cd, ca = r[0], r[1], r[2], r[3], r[4], r[5]
    dh, dd, da = r[9], r[10], r[11]
    if None in (oh, od, oa, ch, cd, ca, dh, dd, da): continue
    drifts = [dh, dd, da]; odds = [ch, cd, ca]
    short_i = min(range(3), key=lambda i: drifts[i])
    if drifts[short_i] <= -0.05:
        drift_strong.append((odds[short_i], r[12] == ['H','D','A'][short_i]))
roi, n = roi_of(drift_strong); lo, hi = bootstrap_roi(drift_strong)
print(f"[C] BACK-SHORTENED(strong drift<=-5%) ROI={roi*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")

# ───────────────────────── D. HOME BIAS ─────────────────────────
home_buckets = {}
back_home = []
for r in rows:
    ph, ch = r[6], r[3]
    if None in (ph, ch): continue
    imp = ph
    if imp < 0.35: lab = 'a.<0.35'
    elif imp < 0.45: lab = 'b.0.35-0.45'
    elif imp < 0.55: lab = 'c.0.45-0.55'
    else: lab = 'd.>=0.55'
    home_buckets.setdefault(lab, ([], []))
    home_buckets[lab][0].append(imp)
    home_buckets[lab][1].append(1.0 if r[12] == 'H' else 0.0)
    back_home.append((ch, r[12] == 'H'))

bias_table("D. HOME BIAS (implied home% vs actual home%)",
           [(k, v[0], v[1]) for k, v in sorted(home_buckets.items())])
roi, n = roi_of(back_home); lo, hi = bootstrap_roi(back_home)
print(f"[D] BACK-HOME@close ROI={roi*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")

# ───────────────────────── E. OU EFFICIENCY (仅 ~6k 场) ─────────────────────────
ou_rows = [r for r in rows if r[14] is not None and r[15] is not None and r[19] is not None]
print(f"\n=== E. OU EFFICIENCY (n={len(ou_rows)}, 仅含盘口场次) ===")
ou_buckets = {}
back_over, back_under = [], []
for r in ou_rows:
    ovp, ov, un, p_over, over_ou = r[15], r[16], r[17], r[19], r[20]
    if None in (ovp, ov, un, p_over, over_ou): continue
    imp = p_over
    if imp < 0.45: lab = 'a.<0.45'
    elif imp < 0.50: lab = 'b.0.45-0.50'
    elif imp < 0.55: lab = 'c.0.50-0.55'
    else: lab = 'd.>=0.55'
    ou_buckets.setdefault(lab, ([], []))
    ou_buckets[lab][0].append(imp)
    ou_buckets[lab][1].append(1.0 if over_ou == 1 else 0.0)
    back_over.append((ov, over_ou == 1))
    back_under.append((un, over_ou == 0))

bias_table("E. OU (implied over% vs actual over%)",
           [(k, v[0], v[1]) for k, v in sorted(ou_buckets.items())])
roi, n = roi_of(back_over); lo, hi = bootstrap_roi(back_over)
print(f"[E] BACK-OVER@close  ROI={roi*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")
roi, n = roi_of(back_under); lo, hi = bootstrap_roi(back_under)
print(f"[E] BACK-UNDER@close ROI={roi*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")

print("\n[done]")

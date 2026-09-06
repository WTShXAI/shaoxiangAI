# -*- coding: utf-8 -*-
"""GQ 滚球(开赛后) OU 诚实压测 — 仅 IR-04 干净子集(有非零 live score)
参考点: 中场前(minute 40-50) 的 OU 盘口(开赛后已定价态) vs 最终总进球。
结算: 各自盘口线 L, over 胜当且仅当 终总球 > L。
bootstrap CI 判定显著性。
"""
import sqlite3, numpy as np, random
random.seed(23); np.random.seed(23)
g = sqlite3.connect('data/GQ.db')
g.execute('PRAGMA busy_timeout=60000')

# 1. 干净 match_key (IR-04)
clean = [r[0] for r in g.execute(
    "SELECT DISTINCT match_key FROM odds_snapshots WHERE score_at IS NOT NULL AND score_at!='' AND score_at!='0-0'")]
print(f"[clean] {len(clean)} matches")
# 临时表加速
g.execute("CREATE TEMP TABLE clean_keys(k TEXT PRIMARY KEY)")
g.executemany("INSERT OR IGNORE INTO clean_keys VALUES (?)", [(k,) for k in clean])

# 2. 干净场最终总进球
finals = {}
for mk, sh, sa in g.execute(
    "SELECT m.match_key, m.score_home, m.score_away FROM matches m JOIN clean_keys c ON m.match_key=c.k"):
    try:
        finals[mk] = int(sh) + int(sa)
    except Exception:
        pass
print(f"[finals] {len(finals)} with score")

# 3. 中场前 OU 快照(每场取 minute 最接近 45 的 over/under 配对)
snaps = g.execute("""
    SELECT s.match_key, s.market, s.selection, s.odds, s.line, s.minute_at
    FROM odds_snapshots s JOIN clean_keys c ON s.match_key=c.k
    WHERE s.market LIKE 'OU%' AND s.minute_at BETWEEN 40 AND 50
""").fetchall()
print(f"[snap] OU@40-50 rows={len(snaps)}")

from collections import defaultdict
by_match = defaultdict(list)
for mk, mkt, sel, odds, line, minu in snaps:
    if mk not in finals: continue
    by_match[mk].append((minu, mkt, sel, odds, line))

bets_over, bets_under = [], []
buckets = defaultdict(lambda: ([], []))  # implied_over bucket -> (imp, act)
for mk, lst in by_match.items():
    if not lst: continue
    # 取最接近 45 的那组(同 minute 取 over+under)
    best_min = min(abs(m-45) for m, *_ in lst)
    pair = [x for x in lst if abs(x[0]-45) == best_min]
    ov = next((x for x in pair if x[2] == 'over'), None)
    un = next((x for x in pair if x[2] == 'under'), None)
    if not ov or not un: continue
    try:
        o_odds = float(ov[3]); u_odds = float(un[3]); L = float(ov[4])
    except Exception:
        continue
    if o_odds <= 0 or u_odds <= 0: continue
    inv = 1/o_odds + 1/u_odds
    p_over = (1/o_odds)/inv
    total = finals[mk]
    over_win = 1 if total > L else 0
    bets_over.append((o_odds, over_win == 1))
    bets_under.append((u_odds, over_win == 0))
    lab = 'a.<0.45' if p_over<0.45 else ('b.0.45-0.50' if p_over<0.50 else ('c.0.50-0.55' if p_over<0.55 else 'd.>=0.55'))
    buckets[lab][0].append(p_over); buckets[lab][1].append(over_win)

def roi_bootstrap(bets, n=2000):
    if len(bets) < 30: return None, 0, None, None
    prof = np.array([(o-1) if w else -1.0 for o, w in bets])
    N = prof.shape[0]; rois = np.empty(n)
    for i in range(n): rois[i] = np.random.choice(prof, size=N, replace=True).mean()
    rois.sort(); return float(rois.mean()), N, float(rois[50]), float(rois[1950])

print("\n=== GQ 滚球 OU @中场(干净子集) ===")
print(f"{'bucket':<14}{'n':>7}{'impOver':>9}{'actOver':>9}{'bias':>9}")
for k in sorted(buckets):
    imp, act = buckets[k]
    if not imp: continue
    print(f"{k:<14}{len(imp):>7}{sum(imp)/len(imp)*100:>8.1f}%{sum(act)/len(act)*100:>8.1f}%{(sum(act)/len(act)-sum(imp)/len(imp))*100:>+8.1f}%")

roi, n, lo, hi = roi_bootstrap(bets_over)
print(f"\n[OVER]  ROI={roi*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")
roi, n, lo, hi = roi_bootstrap(bets_under)
print(f"[UNDER] ROI={roi*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")

# 按 line 细分 under
by_line = defaultdict(list)
for mk, lst in by_match.items():
    best_min = min(abs(m-45) for m, *_ in lst)
    pair = [x for x in lst if abs(x[0]-45) == best_min]
    ov = next((x for x in pair if x[2]=='over'), None); un = next((x for x in pair if x[2]=='under'), None)
    if not ov or not un: continue
    try: L=float(ov[4]); u_odds=float(un[3])
    except: continue
    if u_odds <= 0: continue
    by_line[L].append((u_odds, finals[mk] > L))
print("\n[UNDER by line]")
for L in sorted(by_line):
    bs = by_line[L]
    if len(bs) < 40: continue
    roi, n, lo, hi = roi_bootstrap(bs)
    print(f"  line={L}: UNDER ROI={roi*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")
print("[done]")

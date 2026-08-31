# -*- coding: utf-8 -*-
"""GQ 滚球诱盘检验 v3b — odds_changes 一次性批量拉取(避免逐场扫描)
假设: 滚球中 over 被压低(steam)=诱 over → 反向 BACK UNDER; over 被拉高 → BACK OVER。
每场主盘全场比赛 OU 线, 取滚球(minute 1-89) over 首/末赔率定方向, 末次 to_odds 结算, 终总球>线判 over。
"""
import sqlite3, numpy as np, random
from collections import defaultdict
random.seed(7); np.random.seed(7)
g = sqlite3.connect('data/GQ.db'); g.execute('PRAGMA busy_timeout=60000')
g.execute('CREATE TEMP TABLE IF NOT EXISTS ck(k TEXT PRIMARY KEY)')
clean = [r[0] for r in g.execute(
    "SELECT DISTINCT match_key FROM odds_snapshots WHERE score_at IS NOT NULL AND score_at!='' AND score_at!='0-0'")]
g.executemany('INSERT OR IGNORE INTO ck VALUES (?)', [(k,) for k in clean])
finals = {}
for mk, sh, sa in g.execute("SELECT m.match_key, m.score_home, m.score_away FROM matches m JOIN ck ON m.match_key=ck.k"):
    try: finals[mk] = int(sh)+int(sa)
    except Exception: pass

print("bulk fetch clean OU changes ...", flush=True)
rows = g.execute("""SELECT match_key, market, selection, from_odds, to_odds, minute_at
    FROM odds_changes WHERE match_key IN (SELECT k FROM ck) AND market LIKE 'OU_%' AND minute_at BETWEEN 1 AND 89""").fetchall()
print(f"fetched {len(rows)} change rows", flush=True)
by_match = defaultdict(list)
for r in rows: by_match[r[0]].append(r)

def parse_line(mkt):
    if not mkt or not mkt.startswith('OU_') or '1H' in mkt: return None
    try: return float(mkt.split('_')[1])
    except Exception: return None

bets_all, bets_big = [], []
buck_move = defaultdict(lambda: ([], []))
alln = 0
dbg = defaultdict(int)
for mk, lst in by_match.items():
    dbg['matches'] += 1
    if mk not in finals:
        dbg['no_final'] += 1; continue
    cnt = defaultdict(int)
    for r in lst:
        mkt = r[1]
        if parse_line(mkt) is not None: cnt[mkt] += 1
    if not cnt:
        dbg['no_cnt'] += 1; continue
    main = max(cnt, key=cnt.get); L = parse_line(main)
    if L is None: continue
    ov = [r for r in lst if r[1]==main and r[2]=='over']
    un = [r for r in lst if r[1]==main and r[2]=='under']
    if not ov or not un:
        dbg['no_ovun'] += 1; continue
    ov.sort(key=lambda r: r[5]); un.sort(key=lambda r: r[5])
    try:
        o_first=float(ov[0][3]); o_last=float(ov[-1][4]); u_last=float(un[-1][4])
    except Exception:
        dbg['bad_odds'] += 1; continue
    if o_first<=0 or o_last<=0 or u_last<=0:
        dbg['nonpos'] += 1; continue
    net = o_last - o_first
    tot = finals[mk]; over_win = 1 if tot > L else 0
    if net < 0:
        bet_odds, bet_win = u_last, (over_win==0)
    else:
        bet_odds, bet_win = o_last, (over_win==1)
    alln += 1
    bets_all.append((bet_odds, bet_win))
    mag = abs(net)
    lab = 'a.small(<.10)' if mag<0.10 else ('b.mid(.10-.20)' if mag<0.20 else 'c.big(>=.20)')
    buck_move[lab][0].append(mag); buck_move[lab][1].append(1 if bet_win else 0)
    if mag >= 0.15: bets_big.append((bet_odds, bet_win))

def roi_bootstrap(bets, n=2000):
    if len(bets) < 30: return None, 0, None, None
    prof = np.array([(o-1) if w else -1.0 for o, w in bets])
    N = prof.shape[0]; rois = np.empty(n)
    for i in range(n): rois[i] = np.random.choice(prof, size=N, replace=True).mean()
    rois.sort(); return float(rois.mean()), N, float(rois[50]), float(rois[1950])

print(f"[debug skips] {dict(dbg)}")
print(f"[matches with in-play OU move + final: {alln}]")
ro, n, lo, hi = roi_bootstrap(bets_all)
print(f"[FADE-NET-MOVE all] ROI={ro*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")
ro, n, lo, hi = roi_bootstrap(bets_big)
if ro is not None:
    print(f"[FADE-NET-MOVE big(|.|>=0.15)] ROI={ro*100:+.2f}% n={n} CI[{lo*100:+.1f}%,{hi*100:+.1f}%]")
print("\n[by move magnitude]")
for k in sorted(buck_move):
    imp, act = buck_move[k]
    if not imp: continue
    print(f"  {k:<18} n={len(imp):>5} avg|move|={sum(imp)/len(imp):.3f} fade_win_rate={sum(act)/len(act)*100:.1f}%")
print("[done]")

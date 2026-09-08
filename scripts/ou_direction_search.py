# -*- coding: utf-8 -*-
"""OU 方向 · 增量搜索 v2 (2026-09-09): 10 方向一次对比, 基线 60.6%"""
import math
import os
from collections import defaultdict
import sqlite3
import sys
import time

import joblib
import lightgbm as lgb
import numpy as np

sys.path.insert(0, r'D:\Architecture')
sys.path.insert(0, r'D:\aiqiuke\model')
from pipeline.dc_model import solve_mu_from_line, poisson_sf  # noqa: E402

DB = r'D:\Architecture\data\events.db'
WIN_LO, WIN_HI = 55, 65
BASELINE_T3 = 60.6


def window_odds(con, mk):
    snaps = con.execute("""
        SELECT market, selection, odds FROM odds_snapshots
        WHERE match_key=? AND minute_at BETWEEN ? AND ?
          AND odds>1.01 AND odds<1000
          AND (market LIKE 'OU_%')
          AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%' AND market != 'OU'
        ORDER BY captured_at DESC LIMIT 500""", (mk, WIN_LO, WIN_HI)).fetchall()
    d = {}
    streams = defaultdict(dict)
    for mkt, sel, o in snaps:
        try:
            line = float(mkt.split('_')[1])
        except Exception:
            continue
        if not (0.5 <= line <= 10.0):
            continue
        sd = streams.setdefault(line, {})
        if sel not in sd:
            sd[sel] = float(o)
    for line, sides in streams.items():
        if 'over' in sides and 'under' in sides:
            d[f'OU_{line:g}__over'] = sides['over']
            d[f'OU_{line:g}__under'] = sides['under']
    return d


def solve_mu_list(odds, total_now):
    mus, ref_line, best_gap, ref_p = [], None, 0.0, None
    for k in odds:
        if not k.startswith('OU_') or not k.endswith('__over'):
            continue
        try:
            line = float(k[3:-6])
        except Exception:
            continue
        if total_now >= line:
            continue
        ov, un = odds.get(f'OU_{line:g}__over'), odds.get(f'OU_{line:g}__under')
        if not (ov and un):
            continue
        mu, p_over = solve_mu_from_line(line, ov, un)
        if 0.1 <= mu <= 12:
            mus.append(mu)
        if abs(p_over - 0.5) > best_gap:
            best_gap, ref_line, ref_p = abs(p_over - 0.5), line, p_over
    return mus, ref_line, ref_p


def main():
    t0 = time.time()
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA busy_timeout=20000')
    rows = con.execute("""
        SELECT m.match_key, m.score_home, m.score_away FROM matches m
        WHERE m.status='finished' AND m.score_home IS NOT NULL AND m.score_away IS NOT NULL
          AND EXISTS (SELECT 1 FROM odds_snapshots o WHERE o.match_key=m.match_key
                      AND o.minute_at BETWEEN ? AND ? AND o.market LIKE 'OU_%')
        ORDER BY m.kickoff DESC LIMIT 2000""", (WIN_LO, WIN_HI)).fetchall()
    print(f'样本: {len(rows)}', flush=True)

    st = defaultdict(lambda: [0, 0])
    skip = 0
    for i, (mk, sh, sa) in enumerate(rows):
        trow = con.execute("""
            SELECT score_at FROM odds_snapshots WHERE match_key=? AND score_at != ''
              AND minute_at BETWEEN ? AND ? ORDER BY minute_at DESC LIMIT 1""", (mk, WIN_LO, WIN_HI)).fetchone()
        try:
            h, a = (int(x) for x in trow[0].replace(':', '-').split('-')[:2]) if trow else (0, 0)
        except Exception:
            h = a = 0
        total_now = h + a
        odds = window_odds(con, mk)
        mus, ref_line, ref_p = solve_mu_list(odds, total_now)
        if not mus or ref_line is None:
            skip += 1
            continue
        actual_over = (sh + sa) > total_now
        want = 'OVER' if actual_over else 'UNDER'
        need = ref_line - total_now
        if need <= 0:
            skip += 1
            continue

        # 方法1: 中位数+泊松尾 (当前基线 B)
        mu_med = float(np.median(mus))
        p1 = poisson_sf(need, mu_med)
        d1 = 'OVER' if p1 >= 0.5 else 'UNDER'
        st['B_median'][0] += (d1 == want)
        st['B_median'][1] += 1

        # 方法2: 截尾均值
        mu_trim = float(np.mean(sorted(mus)[1:-1])) if len(mus) > 4 else mu_med
        p2 = poisson_sf(need, mu_trim)
        d2 = 'OVER' if p2 >= 0.5 else 'UNDER'
        st['B_trim'][0] += (d2 == want)
        st['B_trim'][1] += 1

        # 方法3: 均值
        mu_mean = float(np.mean(mus))
        p3 = poisson_sf(need, mu_mean)
        d3 = 'OVER' if p3 >= 0.5 else 'UNDER'
        st['B_mean'][0] += (d3 == want)
        st['B_mean'][1] += 1

        if (i + 1) % 250 == 0:
            print(f'进度 {i+1}/{len(rows)} {time.time()-t0:.0f}s', flush=True)

    print(f'\n===== OU 增量搜索结论 (跳过 {skip}) =====', flush=True)
    for k in ('B_median', 'B_trim', 'B_mean'):
        n = st[k][1]
        if n:
            print(f'{k:10}: {st[k][0]}/{n} = {st[k][0]/n*100:.1f}%', flush=True)
    print(f'耗时 {time.time()-t0:.0f}s', flush=True)


if __name__ == '__main__':
    main()

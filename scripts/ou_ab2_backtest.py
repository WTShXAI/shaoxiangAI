# -*- coding: utf-8 -*-
"""OU 方向 · 第二轮增量实验 (2026-09-09, 用户指令: 沿多线联合方向继续优化)

基线 B(第一轮最优): 全线反解 μ → 中位数 → 泊松尾 (60.2%)
本轮变体:
  C1  μ聚合改进: 截尾均值 (去最高最低各 10%)
  C2  负二项厚尾: 总球分布过离散, 高线的泊松尾偏薄 → 尾部修正
  C3  市场分歧收缩: 多线 μ 离散度(std)大 → 置信收缩
  C4  分线段最优混合: 低线(≤1.5)用A单线, 其余用B  (第一轮 A 低线略优)
  C5  C1+C2+C3 叠加
"""
import math
import sqlite3
import sys
import time
from collections import defaultdict

import numpy as np

sys.path.insert(0, r'D:\Architecture')
from pipeline.dc_model import solve_mu_from_line, poisson_sf  # noqa: E402

DB = r'D:\Architecture\data\events.db'
WIN_LO, WIN_HI = 55, 65


def window_market(con, mk):
    snaps = con.execute("""
        SELECT market, selection, odds FROM odds_snapshots
        WHERE match_key=? AND minute_at BETWEEN ? AND ?
          AND odds>1.01 AND odds<1000
          AND (market='1X2' OR market LIKE 'OU_%')
          AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%' AND market != 'OU'
        ORDER BY captured_at DESC LIMIT 800""", (mk, WIN_LO, WIN_HI)).fetchall()
    d = {}
    streams = defaultdict(dict)
    for mkt, sel, o in snaps:
        if mkt == '1X2':
            d.setdefault(f'1X2__{sel}', float(o))
        else:
            try:
                line = float(mkt.split('_')[1])
            except Exception:
                continue
            if not (0.5 <= line <= 10.0):
                continue
            sd = streams[line]
            if sel not in sd:
                sd[sel] = float(o)
    for line, sides in streams.items():
        if 'over' in sides and 'under' in sides:
            d[f'OU_{line:g}__over'] = sides['over']
            d[f'OU_{line:g}__under'] = sides['under']
    return d


def collect_mus(odds, total_now):
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
    return mus, ref_line, ref_p, best_gap


def nb_sf(k, mu, phi):
    """负二项尾部近似: 方差 = φμ (过离散)。用 Poisson-Gamma 混合闭式。"""
    k = int(k)
    r = mu / max(phi - 1, 1e-6)
    p = r / (r + k + 1e-9)
    # P(X > k) = I_p(r, k+1) 逆 — 数值: 用 1 - CDF via sum (k 小, 可接受)
    cdf = 0.0
    term = 1.0
    for i in range(k + 1):
        cdf += term
        term *= (i + r) / (i + 1) * (1 - p)
    return max(0.0, 1.0 - cdf)


def main():
    t0 = time.time()
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA busy_timeout=20000')
    rows = con.execute("""
        SELECT m.match_key, m.score_home, m.score_away
        FROM matches m
        WHERE m.status='finished' AND m.score_home IS NOT NULL AND m.score_away IS NOT NULL
          AND EXISTS (SELECT 1 FROM odds_snapshots o WHERE o.match_key=m.match_key
                      AND o.minute_at BETWEEN ? AND ? AND o.market LIKE 'OU_%')
        ORDER BY m.kickoff DESC LIMIT 2000""", (WIN_LO, WIN_HI)).fetchall()
    print(f'样本: {len(rows)}', flush=True)

    methods = ('B', 'C1', 'C2', 'C3', 'C4', 'C5')
    stats = {k: [0, 0] for k in methods}
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
        odds = window_market(con, mk)
        mus, ref_line, ref_p, gap = collect_mus(odds, total_now)
        if not mus or ref_line is None:
            skip += 1
            continue
        actual_over = (sh + sa) > total_now
        want = 'OVER' if actual_over else 'UNDER'
        mu_med = float(np.median(mus))
        mu_trim = float(np.mean(sorted(mus)[1:-1])) if len(mus) > 4 else mu_med
        need = ref_line - total_now
        if need <= 0:
            skip += 1
            continue

        preds = {}
        # B 基线
        pb = poisson_sf(need, mu_med)
        preds['B'] = 'OVER' if pb >= 0.5 else 'UNDER'
        # C1 截尾均值
        pc1 = poisson_sf(need, mu_trim)
        preds['C1'] = 'OVER' if pc1 >= 0.5 else 'UNDER'
        # C2 负二项厚尾
        pc2 = nb_sf(need, mu_med, phi=1.35)
        preds['C2'] = 'OVER' if pc2 >= 0.5 else 'UNDER'
        # C3 分歧收缩
        std = float(np.std(mus))
        p3 = poisson_sf(need, mu_med)
        shrink = 0.5 + (p3 - 0.5) * max(0.3, 1.0 - std)
        preds['C3'] = 'OVER' if shrink >= 0.5 else 'UNDER'
        # C4 分线段混合: 参考线 ≤1.5 → 用单线 p_over (A), 否则 B
        if ref_line <= 1.5 and ref_p is not None:
            preds['C4'] = 'OVER' if ref_p >= 0.5 else 'UNDER'
        else:
            preds['C4'] = preds['B']
        # C5 C1+C2+C3 多数投票
        votes = [preds['C1'], preds['C2'], preds['C3']]
        preds['C5'] = 'OVER' if votes.count('OVER') >= 2 else 'UNDER'

        for k in methods:
            if k in preds:
                stats[k][0] += (preds[k] == want)
                stats[k][1] += 1
        if (i + 1) % 250 == 0:
            print(f'进度 {i+1}/{len(rows)} {time.time()-t0:.0f}s', flush=True)

    print(f'\n===== 第二轮增量结论 (跳过 {skip}) =====', flush=True)
    for k in methods:
        n = stats[k][1]
        if n:
            print(f'{k:4}: {stats[k][0]}/{n} = {stats[k][0]/n*100:.1f}%', flush=True)
    print(f'耗时 {time.time()-t0:.0f}s', flush=True)


if __name__ == '__main__':
    main()

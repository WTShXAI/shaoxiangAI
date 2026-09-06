# -*- coding: utf-8 -*-
"""① OU 方向 A/B 回测 (2026-09-09, 用户指令: OU→1X2→让球→比分 挨个优化)

A(现行): 单线 pgap 最大选线 → p_over>=0.5 定方向
B(数学改进): 多线联合 — 全部活线去水 p_over 与泊松结构融合:
    ① 每线隐含剩余总球 μ_r = max(0.1, λ̂ - total_now), λ̂ 由该线去水+线值反解
    ② 多线 μ_r 中位数 (稳健聚合, 抗单线异常)
    ③ P(总球>line_ref) = 泊松尾 SF(line_ref - total_now; μ_r_median)
    ④ 方向 = P>=0.5; 置信 = |P-0.5|
数据: 55-65' 滚球时刻快照 (与统一工作台回测同源)。
"""
import sys
import time
import sqlite3
from collections import defaultdict

import math
import numpy as np

sys.path.insert(0, r'D:\Architecture')
from analysis.live_goal_probe import probe_core  # noqa: E402
from pipeline.dc_model import fit_lambda_from_1x2  # noqa: E402

DB = r'D:\Architecture\data\events.db'
WIN_LO, WIN_HI = 55, 65


def window_market(mk):
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA busy_timeout=15000')
    snaps = con.execute("""
        SELECT market, selection, odds, captured_at FROM odds_snapshots
        WHERE match_key=? AND minute_at BETWEEN ? AND ?
          AND odds>1.01 AND odds<1000
          AND (market='1X2' OR market LIKE 'OU_%')
          AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%' AND market != 'OU'
        ORDER BY captured_at DESC LIMIT 800""", (mk, WIN_LO, WIN_HI)).fetchall()
    con.close()
    d = {}
    ou_streams = defaultdict(lambda: defaultdict(dict))
    for mkt, sel, o, ts in snaps:
        if mkt == '1X2':
            d.setdefault(f'1X2__{sel}', float(o))
        else:
            try:
                line = float(mkt.split('_')[1])
            except Exception:
                continue
            if not (0.5 <= line <= 10.0):
                continue
            sd = ou_streams[line]
            if sel not in sd:
                sd[sel] = float(o)
    # OU 规范 key: 每线取最新完整 over/under 对
    for line, sides in ou_streams.items():
        if 'over' in sides and 'under' in sides:
            d[f'OU_{line:g}__over'] = sides['over']
            d[f'OU_{line:g}__under'] = sides['under']
    return d


def solve_mu_from_line(line, over, under):
    """单线去水 p_over → 隐含总球 λ̂ (泊松反解, 粗网格)。"""
    p_over = (1 / over) / (1 / over + 1 / under)
    best, best_err = 2.5, 1e9
    for mu in np.arange(0.2, 8.01, 0.05):
        # P(> line) 用泊松 CDF, k = floor(line)
        k = int(line)
        cdf = sum(np.exp(-mu) * mu ** i / math.factorial(i) for i in range(k + 1))
        p_hi = 1 - cdf
        err = abs(p_hi - p_over)
        if err < best_err:
            best_err, best = err, mu
    return best, p_over


def old_direction(odds, total_now):
    """A: 现行逻辑 — pgap 最大选线。"""
    cands = []
    for k, v in odds.items():
        if not k.startswith('OU_') or not k.endswith('__over'):
            continue
        try:
            line = float(k[3:-6])
        except Exception:
            continue
        if total_now >= line:
            continue
        ov = odds.get(f'OU_{line:g}__over')
        un = odds.get(f'OU_{line:g}__under')
        if not (ov and un):
            continue
        p_over = (1 / ov) / (1 / ov + 1 / un)
        cands.append((abs(p_over - 0.5), p_over, line))
    if not cands:
        return None, None, None
    cands.sort(reverse=True)
    _, p_over, line = cands[0]
    return ('OVER' if p_over >= 0.5 else 'UNDER'), abs(p_over - 0.5), line


def new_direction(odds, total_now):
    """B: 多线联合泊松尾。"""
    mus = []
    line_ref = None
    best_gap = 0.0
    line_pick = None
    for k, v in odds.items():
        if not k.startswith('OU_') or not k.endswith('__over'):
            continue
        try:
            line = float(k[3:-6])
        except Exception:
            continue
        if total_now >= line:
            continue
        ov = odds.get(f'OU_{line:g}__over')
        un = odds.get(f'OU_{line:g}__under')
        if not (ov and un):
            continue
        mu, p_over = solve_mu_from_line(line, ov, un)
        if 0.1 <= mu <= 12:
            mus.append(mu)
        if abs(p_over - 0.5) > best_gap:
            best_gap, line_pick = abs(p_over - 0.5), line
    if not mus:
        return None, None, None
    mu_med = float(np.median(mus))
    # 参考线: 选存活线中最接近 μ 的 (方向语义即"总球会不会过该线")
    line_ref = line_pick if line_pick else 2.5
    need = line_ref - total_now
    if need <= 0:
        return None, None, None
    k = int(need)
    cdf = sum(np.exp(-mu_med) * mu_med ** i / math.factorial(i) for i in range(k + 1))
    p_over = 1 - cdf
    return ('OVER' if p_over >= 0.5 else 'UNDER'), abs(p_over - 0.5), line_ref


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

    stats = {k: [0, 0] for k in ('A', 'B')}
    by_line = defaultdict(lambda: {'A': [0, 0], 'B': [0, 0]})
    skip = 0
    for i, (mk, sh, sa) in enumerate(rows):
        total_now = 0  # 统一 55-65' 时刻以开赛累计总球近似: 用该窗口最新 score_at
        trow = con.execute("""
            SELECT score_at FROM odds_snapshots WHERE match_key=? AND score_at != ''
              AND minute_at BETWEEN ? AND ? ORDER BY minute_at DESC LIMIT 1""", (mk, WIN_LO, WIN_HI)).fetchone()
        try:
            h, a = (int(x) for x in trow[0].replace(':', '-').split('-')[:2]) if trow else (0, 0)
        except Exception:
            h = a = 0
        total_now = h + a
        odds = window_market(mk)
        if len(odds) < 2:
            skip += 1
            continue
        dA, cA, lA = old_direction(odds, total_now)
        dB, cB, lB = new_direction(odds, total_now)
        actual_over = (sh + sa) > total_now   # 剩余是否进球 (相对该时刻)
        if dA:
            stats['A'][0] += (dA == ('OVER' if actual_over else 'UNDER'))
            stats['A'][1] += 1
            if lA is not None:
                by_line[round(lA * 2) / 2]['A'][0] += (dA == ('OVER' if actual_over else 'UNDER'))
                by_line[round(lA * 2) / 2]['A'][1] += 1
        if dB:
            stats['B'][0] += (dB == ('OVER' if actual_over else 'UNDER'))
            stats['B'][1] += 1
            if lB is not None:
                by_line[round(lB * 2) / 2]['B'][0] += (dB == ('OVER' if actual_over else 'UNDER'))
                by_line[round(lB * 2) / 2]['B'][1] += 1
        if (i + 1) % 250 == 0:
            print(f'进度 {i+1}/{len(rows)} A {stats["A"][0]}/{stats["A"][1]} B {stats["B"][0]}/{stats["B"][1]} {time.time()-t0:.0f}s', flush=True)
    print(f'\n===== OU A/B 结论 (跳过 {skip}) =====', flush=True)
    for k in ('A', 'B'):
        n = stats[k][1]
        if n:
            print(f'{k}: {stats[k][0]}/{n} = {stats[k][0]/n*100:.1f}%', flush=True)
    print('—— 按线分层 (线: A命中/An B命中/Bn):', flush=True)
    for ln, v in sorted(by_line.items()):
        if v['A'][1] >= 20:
            print(f'  线 {ln}: A {v["A"][0]}/{v["A"][1]} = {v["A"][0]/v["A"][1]*100:.1f}% | '
                  f'B {v["B"][0]}/{v["B"][1]} = {v["B"][0]/v["B"][1]*100:.1f}%', flush=True)
    print(f'耗时 {time.time()-t0:.0f}s', flush=True)


if __name__ == '__main__':
    main()

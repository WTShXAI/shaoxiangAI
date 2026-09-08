# -*- coding: utf-8 -*-
"""OU 方向概率 · isotonic 校准 (2026-09-09, 继续优化: 概率质量而非仅方向)

流程:
  1) 收集回测样本: 多线联合泊松尾的 p_over 与实际 over 结果 (55-65' 滚球时刻)
  2) 按 kickoff 日期切分, 训练段拟合 IsotonicRegression(p_over → p_over_cal)
  3) 测试段验证: 校准后可靠性(分桶预测 vs 实际) + 方向命中率不降
  4) 校准映射存 config/ou_joint_calib.json 供线上 probe_core 查表
"""
import json
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
OUT = r'D:\Architecture\config\ou_joint_calib.json'


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


def joint_p(odds, total_now):
    """多线联合泊松尾 (与线上落地逻辑一致): μ 中位数 → P(总球>参考线)。"""
    mus = []
    ref_line, best_gap, ref_p = None, 0.0, None
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
    if not mus or ref_line is None:
        return None, None
    mu_med = float(np.median(mus))
    need = ref_line - total_now
    if need <= 0:
        return None, None
    return poisson_sf(need, mu_med), ref_line


def main():
    t0 = time.time()
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA busy_timeout=20000')
    rows = con.execute("""
        SELECT m.match_key, m.kickoff, m.score_home, m.score_away
        FROM matches m
        WHERE m.status='finished' AND m.score_home IS NOT NULL AND m.score_away IS NOT NULL
          AND EXISTS (SELECT 1 FROM odds_snapshots o WHERE o.match_key=m.match_key
                      AND o.minute_at BETWEEN ? AND ? AND o.market LIKE 'OU_%')
        ORDER BY m.kickoff DESC LIMIT 3000""", (WIN_LO, WIN_HI)).fetchall()
    print(f'样本: {len(rows)}', flush=True)

    samples = []   # (kickoff, p_joint, actual_over_bool, total_now, need)
    skip = 0
    for i, (mk, ko, sh, sa) in enumerate(rows):
        trow = con.execute("""
            SELECT score_at FROM odds_snapshots WHERE match_key=? AND score_at != ''
              AND minute_at BETWEEN ? AND ? ORDER BY minute_at DESC LIMIT 1""", (mk, WIN_LO, WIN_HI)).fetchone()
        try:
            h, a = (int(x) for x in trow[0].replace(':', '-').split('-')[:2]) if trow else (0, 0)
        except Exception:
            h = a = 0
        total_now = h + a
        odds = window_market(con, mk)
        p, ref = joint_p(odds, total_now)
        if p is None:
            skip += 1
            continue
        actual_over = (sh + sa) > total_now
        samples.append((ko, p, 1 if actual_over else 0, need_of(ref, total_now)))
        if (i + 1) % 300 == 0:
            print(f'进度 {i+1}/{len(rows)}', flush=True)
    print(f'有效样本: {len(samples)} (跳过 {skip})', flush=True)

    # 日期切分: 前 70% 校准拟合 / 后 30% 验证
    samples.sort(key=lambda s: s[0])
    cut_i = int(len(samples) * 0.7)
    cal = samples[:cut_i]
    val = samples[cut_i:]

    # 等频分桶 isotonic (纯 numpy 实现, 免 sklearn 依赖)
    def fit_isotonic(pairs):
        ps = np.array([p for _, p, _ in pairs])
        ys = np.array([a for _, _, a in pairs], dtype=float)
        order = np.argsort(ps, kind='mergesort')
        ps, ys = ps[order], ys[order]
        # PAVA (pool adjacent violators)
        blocks = [[p, y, 1] for p, y in zip(ps, ys)]
        i = 0
        while i < len(blocks) - 1:
            if blocks[i][1] > blocks[i + 1][1]:
                p_ = (blocks[i][1] * blocks[i][2] + blocks[i+1][1] * blocks[i+1][2]) / (blocks[i][2] + blocks[i+1][2])
                blocks[i] = [(blocks[i][0] + blocks[i+1][0]) / 2, p_, blocks[i][2] + blocks[i+1][2]]
                del blocks[i+1]
                i = max(0, i - 1)
            else:
                i += 1
        mapping = [(b[0], b[1], b[2]) for b in blocks]   # (p_train_mean, cal_p, weight)
        return mapping

    mapping = fit_isotonic([(k, p, a) for k, p, a, n in
                            [(s[0], s[1], s[2], s[3]) for s in cal]])
    print(f'校准映射块数: {len(mapping)}', flush=True)

    def calibrate(p):
        # 查最近训练均值锚点的校准值
        best = min(mapping, key=lambda m: abs(m[0] - p))
        return best[1]

    # 验证(后 30%): 校准前 vs 校准后的可靠性与方向命中率
    by_pred = defaultdict(lambda: [0.0, 0, 0.0, 0])   # bucket → [sum_cal, n_cal, sum_raw, n_raw]
    dir_raw = dir_cal = 0
    brier_raw = brier_cal = 0.0
    for ko, p, a, _ in val:
        pc = calibrate(p)
        b = round(p * 10) / 10
        by_pred[b][0] += pc; by_pred[b][1] += 1
        by_pred[b][2] += p; by_pred[b][3] += 1
        dir_raw += ((p >= 0.5) == (a == 1))
        dir_cal += ((pc >= 0.5) == (a == 1))
        brier_raw += (p - a) ** 2
        brier_cal += (pc - a) ** 2
    nv = max(1, len(val))
    print(f'验证集 {len(val)}: 方向命中 raw {dir_raw/nv*100:.1f}% → cal {dir_cal/nv*100:.1f}%', flush=True)
    print(f'Brier: raw {brier_raw/nv:.4f} → cal {brier_cal/nv:.4f}', flush=True)
    print('可靠性(预测桶 → 实际频率):')
    for b in sorted(by_pred):
        sc, nc, sr, nr = by_pred[b]
        print(f'  预测 {b:.1f}: 校准后实际 {sc/nc*100:.1f}% (n={nc}) | 原始 {sr/nr*100:.1f}%', flush=True)

    # 固化: 校准表 (锚点 → 校准值)
    table = [{'anchor': round(m[0], 4), 'cal': round(m[1], 4), 'w': m[2]} for m in mapping]
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump({'version': 'ou_joint_calib_v1', 'method': 'isotonic',
                   'samples': len(samples), 'cut': cut_i and samples[cut_i][0],
                   'table': table}, f, ensure_ascii=False, indent=1)
    print(f'校准表已存: {OUT}', flush=True)
    print(f'总耗时 {time.time()-t0:.0f}s', flush=True)


def need_of(ref_line, total_now):
    return ref_line - total_now


if __name__ == '__main__':
    main()

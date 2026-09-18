#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M4: 期望进球按联赛收缩试验 (2026-09-18, 模型升级路线图④).
================================================================================
背景: OIP 诚实锚的期望总进球在本库宇宙残余 -0.59 球系统性偏差 (市场让水)。
方案: 联赛分组收缩系数 s_l = shrink( Σactual/Σimplied_l → 全局 s ), λ_adj = λ·s_l。
守成判据 (预声明): O2.5 LogLoss 相对 s=1 劣化 ≤0.002 (不伤现有最优源),
  且期望总进球 |bias| 或 MAE 改善 ≥0.15 球 → 才接入 predict_export。
"""
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, r'D:\Architecture')
import numpy as np

EPS = 1e-15
REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')


def o25_from_matrix(oh, od, oa, implied_total):
    from pipeline.score_model import predict_score
    M = predict_score('H', 'A', oh, od, oa, goal_scale=1.0,
                      implied_total=implied_total)['matrix']
    mg = M.shape[0] - 1
    return float(1.0 - sum(M[i, j] for i in range(mg + 1) for j in range(mg + 1) if i + j <= 2)), \
        float(sum(M[i, j] for i in range(1, mg + 1) for j in range(1, mg + 1)))


def main():
    from gq.db import conn
    with conn(readonly=True) as con:
        rows = con.execute("""SELECT d.payload, m.score_home, m.score_away, m.kickoff
            FROM daily_predictions d JOIN matches m ON m.match_key = d.match_key
            WHERE m.status='finished' AND m.score_home IS NOT NULL
            ORDER BY m.kickoff""").fetchall()
    data = []
    for payload, fsh, fsa, ko in rows:
        try:
            p = json.loads(payload)
            mi = p['market_implied']
            oh, od, oa = mi['odds_1x2']
            it = mi['ou_line'] + 2.0 * (mi['p_over'] - 0.5) if mi.get('ou_line') else None
            if not (it and 1.0 < it < 6.0):
                continue
            from pipeline.odds_candles import parse_kickoff_ts
            data.append({'ko_ts': parse_kickoff_ts(ko), 'league': p.get('league') or '未知',
                         'oh': oh, 'od': od, 'oa': oa, 'it': it,
                         'total': fsh + fsa, 'btts': 1 if (fsh > 0 and fsa > 0) else 0})
        except Exception:
            continue
    n = len(data)
    print(f'n={n}')

    # 展开窗: 前 70% 定系数, 后 30% 评估 (时序严格)
    cut = int(n * 0.7)
    tr, te = data[:cut], data[cut:]

    def league_factors(train, k=30):
        agg = defaultdict(lambda: [0.0, 0.0, 0])
        g_i = g_a = 0.0
        for r in train:
            agg[r['league']][0] += r['total']
            agg[r['league']][1] += r['it']
            agg[r['league']][2] += 1
            g_i += r['it']; g_a += r['total']
        g_s = (g_a / g_i) if g_i > 0 else 1.0
        out = {}
        for lg, (a, i, cnt) in agg.items():
            raw = (a / i) if i > 0 else g_s
            out[lg] = (cnt * raw + k * g_s) / (cnt + k)
        return out, g_s

    for mode, s_map in [('s=1 (现状)', None),
                        ('全局s', None),
                        ('联赛收缩s_l', None)]:
        if mode == '全局s':
            _, gs = league_factors(tr)
            factors = {}
            apply_g = gs
        elif mode == '联赛收缩s_l':
            factors, gs = league_factors(tr)
            apply_g = None
        else:
            factors, apply_g = {}, None
        o_ll_pts, et_err, et_bias = [], [], []
        for r in te:
            if mode == '全局s':
                s = apply_g
            elif mode == '联赛收缩s_l':
                s = factors.get(r['league'], gs)
            else:
                s = 1.0
            it_adj = r['it'] * s
            o25, btts = o25_from_matrix(r['oh'], r['od'], r['oa'], it_adj)
            o_ll_pts.append((o25, 1 if r['total'] > 2.5 else 0))
            et_err.append(abs(it_adj - r['total']))
            et_bias.append(r['total'] - it_adj)
        ll = round(-np.mean([w * math.log(max(p, EPS)) + (1 - w) * math.log(max(1 - p, EPS))
                             for p, w in o_ll_pts]), 5)
        brier = round(np.mean([(p - w) ** 2 for p, w in o_ll_pts]), 5)
        print(f'{mode:<12} O2.5 LL={ll} Brier={brier} | 期望总球 bias={np.mean(et_bias):+.3f} '
              f'MAE={np.mean(et_err):.3f}')
        if mode == '联赛收缩s_l':
            shrink_res = {'ll': ll, 'brier': brier,
                          'bias': round(float(np.mean(et_bias)), 3),
                          'mae': round(float(np.mean(et_err)), 3)}
        if mode == 's=1 (现状)':
            base_res = {'ll': ll, 'brier': brier,
                        'bias': round(float(np.mean(et_bias)), 3),
                        'mae': round(float(np.mean(et_err)), 3)}
    verdict = ('采纳' if (shrink_res['ll'] - base_res['ll'] <= 0.002
                          and min(base_res['bias'], shrink_res['bias']) == shrink_res['bias']
                          and abs(shrink_res['bias']) + 0.15 <= abs(base_res['bias']))
               else '不采纳')
    out = {'n': n, 'n_test': len(te), 'baseline': base_res, 'league_shrink': shrink_res,
           'gate': {'o25_ll_degrade_max': 0.002, 'bias_improve_min': 0.15, 'verdict': verdict}}
    print('判定:', verdict)
    with open(os.path.join(REPORT_DIR, 'league_shrink_eval.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()

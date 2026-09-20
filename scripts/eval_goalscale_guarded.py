#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""卡5门禁: goal_scale 守卫后 A/B + 多切分稳健性 (2026-09-21)
结论落 reports/goalscale_guarded_ab.json; 采纳判决在此脚本输出。"""
import json
import sqlite3
import sys

sys.path.insert(0, r'D:\Architecture')
import numpy as np
from pipeline.settle import result_1x2, credible_1x2
from pipeline.odds_candles import parse_kickoff_ts
from pipeline.score_model import predict_score

EV_DB = r'D:\Architecture\data\events.db'
OUT = r'D:\Architecture\reports\goalscale_guarded_ab.json'
EPS = 1e-15


def load():
    con = sqlite3.connect(EV_DB)
    rows = con.execute(
        "SELECT dp.match_key, dp.payload, dp.kickoff, m.score_home, m.score_away "
        "FROM daily_predictions dp JOIN matches m ON m.match_key=dp.match_key "
        "WHERE m.status='finished' AND m.score_home IS NOT NULL").fetchall()
    out = []
    for mk, payload, ko, sh, sa in rows:
        if result_1x2(sh, sa) is None:
            continue
        kts = parse_kickoff_ts(ko or '')
        lrow = con.execute('SELECT MAX(captured_at) FROM odds_changes WHERE match_key=?', (mk,)).fetchone()
        if not credible_1x2(sh, sa, lrow[0] if lrow else None, kts):
            continue
        try:
            p = json.loads(payload)
        except Exception:
            continue
        mi = p.get('market_implied') or {}
        odds = mi.get('odds_1x2')
        if not odds or mi.get('ou_line') != 2.5 or mi.get('p_over') is None:
            continue
        it = 2.5 + 2.0 * (mi['p_over'] - 0.5)
        if not (1.0 < it < 6.0):
            continue
        out.append((kts or 0, p['home'], p['away'], tuple(odds), it,
                    1 if (sh + sa) > 2.5 else 0, 1 if (sh > 0 and sa > 0) else 0))
    con.close()
    out.sort(key=lambda x: x[0])
    return out


def eval_scale(data, scale, lo, hi):
    seg = data[lo:hi]
    s_o = s_b = s_m = 0.0
    n = 0
    for _, home, away, odds, it, y_o, y_b in seg:
        try:
            r = predict_score(home, away, *odds, goal_scale=scale, implied_total=it)
        except Exception:
            continue
        M = r['matrix']
        p_o = 1.0 - sum(M[i, j] for i in range(3) for j in range(3) if i + j <= 2)
        p_b = float(sum(M[i, j] for i in range(1, M.shape[0]) for j in range(1, M.shape[1])))
        s_o += -np.log(np.clip(p_o if y_o else 1 - p_o, EPS, 1))
        s_b += -np.log(np.clip(p_b if y_b else 1 - p_b, EPS, 1))
        s_m += abs((r['lh'] + r['la']) - (0 if False else 0) or 0)
        n += 1
    return s_o / max(1, n), s_b / max(1, n), n


def main():
    data = load()
    print(f'守卫样本: {len(data)} 场')
    splits = [(0.5, 0.6, 0.7, 0.8)]
    rob = []
    for tr in (0.5, 0.6, 0.7, 0.8):
        cut = int(len(data) * tr)
        l10 = eval_scale(data, 1.0, cut, len(data))
        l11 = eval_scale(data, 1.1, cut, len(data))
        d = l11[0] - l10[0]
        rob.append({'train_frac': tr, 'll_1.0': round(l10[0], 4), 'll_1.1': round(l11[0], 4),
                    'delta': round(d, 4), 'better': bool(d < 0), 'n_val': l10[2]})
        print(f'  训练{int(tr*100)}%: 1.0={l10[0]:.4f} 1.1={l11[0]:.4f} d={d:+.4f} n={l10[2]}')
    ok = sum(1 for r in rob if r['better'])
    full70 = int(len(data) * 0.7)
    b10 = eval_scale(data, 1.0, full70, len(data))
    b11 = eval_scale(data, 1.1, full70, len(data))
    rep = {
        'n_guarded': len(data),
        'robustness': rob,
        'splits_better': ok,
        'final_70': {'ll_1.0': round(b10[0], 4), 'll_1.1': round(b11[0], 4),
                     'btts_1.0': round(b10[1], 4), 'btts_1.1': round(b11[1], 4)},
        'verdict': ('✓ 采纳 goal_scale=1.1 (predict_export 派生市场)' if ok == 4 and b11[0] < b10[0] - 0.003
                    else '△ 不一致/不过线 — 维持 1.0'),
    }
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(f'→ {OUT}')


if __name__ == '__main__':
    main()

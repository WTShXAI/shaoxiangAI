#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""联赛校准下钻生成器 (2026-09-21, 守卫口径重生成 — 09-19 首版为守卫前污染口径)
================================================================================
从 daily_predictions × matches (credible_1x2 守卫) 生成按联赛 LL/TOP1 下钻,
供 /api/predictions/league-drilldown 与预测中心页联赛徽标使用。只读诊断+写报告文件。
用法: python scripts/league_drilldown.py
"""
import json
import math
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, r'D:\Architecture')
from pipeline.settle import result_1x2, credible_1x2
from pipeline.odds_candles import parse_kickoff_ts

EV_DB = r'D:\Architecture\data\events.db'
OUT = r'D:\Architecture\reports\league_calibration_drilldown.json'


def main():
    con = sqlite3.connect(f'file:{EV_DB}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("""SELECT dp.payload, m.score_home, m.score_away, m.kickoff,
        (SELECT MAX(captured_at) FROM odds_changes oc WHERE oc.match_key = m.match_key) AS lo
        FROM daily_predictions dp JOIN matches m ON m.match_key = dp.match_key""").fetchall()
    con.close()
    agg = defaultdict(lambda: {'n': 0, 'll': 0.0, 'hit': 0})
    overall = defaultdict(lambda: {'n': 0, 'll': 0.0, 'hit': 0})
    n_guarded_out = 0
    for r in rows:
        try:
            p = json.loads(r['payload'])
        except Exception:
            continue
        pv = [p.get('p_home'), p.get('p_draw'), p.get('p_away')]
        if not all(isinstance(v, (int, float)) for v in pv):
            continue
        kts = parse_kickoff_ts(r['kickoff'] or '')
        if not credible_1x2(r['score_home'], r['score_away'], r['lo'], kts):
            n_guarded_out += 1
            continue
        actual = result_1x2(r['score_home'], r['score_away'])
        if not actual:
            continue
        ai = {'home': 0, 'draw': 1, 'away': 2}[actual]
        clip = [min(max(float(v), 1e-15), 1.0) for v in pv]
        ll = -math.log(clip[ai])
        hit = int(max(range(3), key=lambda i: pv[i]) == ai)
        lg = p.get('league') or '未知'
        for k in (agg[lg], overall['ALL']):
            k['n'] += 1
            k['ll'] += ll
            k['hit'] += hit
    out = {'generated_at': __import__('time').strftime('%Y-%m-%d %H:%M:%S'),
           'guard': 'credible_1x2 (假0-0守卫, 2026-09-21 重生成)',
           'n_excluded_by_guard': n_guarded_out,
           'overall': {'n': overall['ALL']['n'],
                       'log_loss': round(overall['ALL']['ll'] / overall['ALL']['n'], 4),
                       'accuracy': round(overall['ALL']['hit'] / overall['ALL']['n'], 4)},
           'leagues': []}
    for lg, k in sorted(agg.items(), key=lambda x: -x[1]['n']):
        if k['n'] >= 30:
            out['leagues'].append({'league': lg, 'n': k['n'],
                                   'log_loss': round(k['ll'] / k['n'], 4),
                                   'accuracy': round(k['hit'] / k['n'], 4)})
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"守卫剔除 {n_guarded_out} 场 | 样本 {out['overall']['n']} | 联赛 {len(out['leagues'])} 个 (n>=30)")
    print(f"→ {OUT}")


if __name__ == '__main__':
    main()

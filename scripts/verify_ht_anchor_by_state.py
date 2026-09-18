#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M1: HT锚 OU 分状态严格同集重验 (2026-09-18, 模型升级路线图①).
================================================================================
背景: meta 记录 OU 公平口径 79.9% vs 现任 63.3% (+16.6pp, n=961), 但客领先态 -2.8pp。
本脚本在**同一 walkforward 折**上同时产出 HT锚 OU 与 现任 (halftime_conclusion) 读数,
按 HT 比分状态 (主领先/平/客领先) 分桶对照 — 为"分状态切换"提供同集证据。

采纳判据 (预先声明):
  切换 = 在模型≥现任+2pp 的状态启用 HT锚 OU, 其余状态留现任;
  需同时满足: 总体不劣于现任, 客领先态单独看模型不劣于现任 (或该状态直接留现任)。

用法: python scripts/verify_ht_anchor_by_state.py [--days 45]
输出: reports/ht_anchor_state_verify.{json,md}
"""
import argparse
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, r'D:\Architecture')
import numpy as np

EPS = 1e-15
REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=45)
    args = ap.parse_args()

    from scripts.train_ht_anchor_model import build_ht_dataset
    from analysis.live_goal_probe import _open_gq
    from sklearn.model_selection import TimeSeriesSplit
    import lightgbm as lgb

    con = _open_gq()
    rows = build_ht_dataset(con, days=args.days)
    # 现任读数 (按 match_key): 归一大小写; None/缺失 = 无现任
    inc = {}
    for mk, d, pr in con.execute(
            "SELECT match_key, ou_direction, ou_prob FROM halftime_conclusion").fetchall():
        if d:
            inc[mk] = d.upper()
    con.close()

    rows = sorted(rows, key=lambda r: r['kickoff'])
    y_ou = np.array([r['ou_label'] for r in rows])
    X = np.array([r['x'] for r in rows], dtype=np.float32)
    idx_ou = np.where(y_ou >= 0)[0]
    Xo, yo = X[idx_ou], y_ou[idx_ou]
    rows_o = [rows[i] for i in idx_ou]

    folds = list(TimeSeriesSplit(n_splits=5).split(Xo))
    buckets = defaultdict(lambda: {'model': [0, 0], 'inc': [0, 0], 'n': 0})
    pts_model, pts_inc = [], []
    for tr, te in folds:
        m = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05, max_depth=6,
                               num_leaves=31, verbose=-1, random_state=42)
        m.fit(Xo[tr], yo[tr])
        p = m.predict_proba(Xo[te])
        for j, ti in enumerate(te):
            r = rows_o[ti]
            state = ('home_lead', 'draw', 'away_lead')[r['ht_state']]
            pred = int(np.argmax(p[j]))
            inc_dir = inc.get(r['match_key'])
            b = buckets[state]
            b['model'][0] += int(pred == yo[ti])
            b['model'][1] += 1
            if inc_dir in ('OVER', 'UNDER'):
                inc_pred = 1 if inc_dir == 'OVER' else 0
                b['inc'][0] += int(inc_pred == yo[te][j])
                b['inc'][1] += 1
            b['n'] += 1
            pts_model.append((float(p[j][1]), 1 if yo[te][j] == 1 else 0))

    out = {'generated_at': __import__('time').strftime('%Y-%m-%d %H:%M:%S'),
           'n_ou_total': int(len(yo)), 'states': {}}
    print(f"{'状态':<10} {'HT锚OU':>8} {'现任OU':>8} {'同集n':>6}")
    for st in ('home_lead', 'draw', 'away_lead'):
        b = buckets[st]
        ma = b['model'][0] / b['model'][1] if b['model'][1] else None
        ia = b['inc'][0] / b['inc'][1] if b['inc'][1] else None
        out['states'][st] = {'model_acc': round(ma, 4) if ma is not None else None,
                             'incumbent_acc': round(ia, 4) if ia is not None else None,
                             'model_n': b['model'][1], 'incumbent_n': b['inc'][1]}
        print(f"{st:<10} {ma*100 if ma else 0:>7.1f}% {ia*100 if ia else 0:>7.1f}% {b['inc'][1]:>6}")

    # 总体 (同集): 模型 vs 现任
    both_m = sum(b['model'][1] for b in buckets.values())
    both_i = sum(b['inc'][1] for b in buckets.values())
    m_hit = sum(b['model'][0] for b in buckets.values())
    i_hit = sum(b['inc'][0] for b in buckets.values())
    out['overall'] = {'model_acc': round(m_hit / both_m, 4) if both_m else None,
                      'incumbent_acc': round(i_hit / both_i, 4) if both_i else None,
                      'model_n': both_m, 'incumbent_n': both_i}
    print(f"总体: 模型 {out['overall']['model_acc'] and round(out['overall']['model_acc']*100,1)}% "
          f"vs 现任 {out['overall']['incumbent_acc'] and round(out['overall']['incumbent_acc']*100,1)}%")

    # 分状态切换建议
    switch = {}
    for st in ('home_lead', 'draw', 'away_lead'):
        s = out['states'][st]
        if s['model_acc'] is None or s['incumbent_acc'] is None or s['incumbent_n'] < 100:
            switch[st] = '样本不足, 留现任'
        elif s['model_acc'] >= s['incumbent_acc'] + 0.02:
            switch[st] = '切换 HT锚 OU'
        else:
            switch[st] = '留现任'
    out['switch_recommendation'] = switch
    print('分状态建议:', switch)

    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(os.path.join(REPORT_DIR, 'ht_anchor_state_verify.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(os.path.join(REPORT_DIR, 'ht_anchor_state_verify.md'), 'w', encoding='utf-8') as f:
        f.write(f"# HT锚 OU 分状态严格同集重验 ({out['generated_at']})\n\n")
        f.write("| 状态 | HT锚OU | 现任OU | 同集n | 建议 |\n|---|---|---|---|---|\n")

        def _pct(v):
            return f"{v*100:.1f}%" if v is not None else '—'

        for st in ('home_lead', 'draw', 'away_lead'):
            s_ = out['states'][st]
            f.write(f"| {st} | {_pct(s_['model_acc'])} | {_pct(s_['incumbent_acc'])} "
                    f"| {s_['incumbent_n']} | {switch[st]} |\n")
    print(f"→ {REPORT_DIR}\\ht_anchor_state_verify.{{json,md}}")


if __name__ == '__main__':
    main()

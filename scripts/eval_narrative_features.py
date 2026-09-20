#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""叙事特征入模检验 (2026-09-21, 自主研发卡①)
================================================================================
假设: 球队级叙事画像 (近N场绝平/逆转/补时失球/干旱倾向) 携带赔率与K线之外的信息,
能改善 K线集成 1X2 概率。这是负结论存档 (清单§五) 里唯一未检验过的特征族。
特征 (每队近10场, 仅 kickoff 严格早于当场的已验证叙事行):
  draw_ft_rate / draw_at_ht_rate / late_equalizer_rate / comeback_rate /
  fav_failed_rate / scoreless_last15_rate / drought_rate / avg_goals /
  h1_goal_share / sample_weight(有效样本数)  → 主客各10维 + 差值9维 = 29维
协议: 复用 build_dataset (70d 语料) + 同一 TimeSeriesSplit(5) 时间折;
      A = 现任特征 (candles+static), B = A + 叙事29维。只比 LGB (E族), 同折同评。
采纳线: ΔLL ≤ -0.003 且 5 折中 ≥4 折不劣 (70d 采纳惯例)。
用法: python scripts/eval_narrative_features.py [--days 70]
"""
import argparse
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict

sys.path.insert(0, r'D:\Architecture')
import numpy as np

EV_DB = r'D:\Architecture\data\events.db'
OUT = r'D:\Architecture\reports\narrative_feature_eval.json'


def load_team_histories():
    """verified=1 叙事行 → {team: [(ko_ts, feats10)]} 时间升序。"""
    con = sqlite3.connect(f'file:{EV_DB}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    from pipeline.odds_candles import parse_kickoff_ts
    hist = defaultdict(list)
    for r in con.execute("SELECT * FROM match_narrative WHERE verified=1"):
        ko = parse_kickoff_ts(r['kickoff'] or '')
        if not ko:
            continue
        n = r['n_goals'] or 0
        f = (
            1.0 if r['draw_ft'] else 0.0,
            1.0 if r['draw_at_ht'] else 0.0,
            1.0 if r['late_equalizer'] else 0.0,
            1.0 if r['comeback'] else 0.0,
            1.0 if r['fav_failed'] else 0.0,
            1.0 if r['scoreless_last15'] else 0.0,
            1.0 if r['drought_after_first'] else 0.0,
            float(n),
            (float(r['h1_goals']) / n) if n else 0.5,
            1.0,
        )
        hist[r['home']].append((ko, f, r['fav_side'] == 'home'))
        hist[r['away']].append((ko, f, r['fav_side'] == 'away'))
    con.close()
    for t in hist:
        hist[t].sort(key=lambda x: x[0])
    return hist


def rolling_feats(hist, team, ko_ts, n=10):
    """kickoff 严格早于 ko_ts 的近 n 场均值; 样本不足返回 None (调用方填中性)。"""
    rows = [f for k, f, _ in hist.get(team, []) if k < ko_ts]
    if len(rows) < 5:
        return None
    rows = rows[-n:]
    arr = np.array(rows, dtype=np.float32)
    return arr.mean(axis=0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=70)
    args = ap.parse_args()

    from scripts.train_odds_candles_model import build_dataset, lgb_walkforward
    from analysis.live_goal_probe import _open_gq
    con = _open_gq()
    t0 = time.time()
    rows = build_dataset(con, days=args.days, max_matches=20000)
    con.close()
    y = np.array([r['label'] for r in rows])
    X_base = np.hstack([
        np.array([r['candles'] for r in rows], dtype=np.float32),
        np.array([r['static'] for r in rows], dtype=np.float32),
    ])
    print(f'语料 {len(rows)} 场 [{time.time()-t0:.0f}s]; 基础特征 {X_base.shape[1]} 维')

    hist = load_team_histories()
    narr = np.zeros((len(rows), 29), dtype=np.float32)
    neutral = np.array([0.25, 0.25, 0.1, 0.1, 0.35, 0.25, 0.3, 2.7, 0.45, 0.0], dtype=np.float32)
    n_hit = 0
    for i, r in enumerate(rows):
        mk = r['match_key']
        if ' vs ' not in mk:
            continue
        home, away = mk.split(' vs ', 1)
        ko = __import__('time').mktime(__import__('datetime').datetime.strptime(
            r['kickoff'][:16].replace('T', ' '), '%Y-%m-%d %H:%M').timetuple())
        fh0 = rolling_feats(hist, home, ko)
        fa0 = rolling_feats(hist, away, ko)
        if fh0 is not None and fa0 is not None:
            n_hit += 1
        fh = fh0 if fh0 is not None else neutral
        fa = fa0 if fa0 is not None else neutral
        diff = fh[:9] - fa[:9]
        narr[i] = np.concatenate([fh, fa, diff])
    print(f'叙事覆盖: {n_hit}/{len(rows)} ({100*n_hit/len(rows):.0f}%) | 叙事特征 29 维')

    from sklearn.model_selection import TimeSeriesSplit
    folds = list(TimeSeriesSplit(n_splits=5).split(np.zeros(len(y))))

    def clean_ll_mask():
        return np.array([not r.get('dirty', False) for r in rows])

    keep = clean_ll_mask()
    eps = 1e-15

    def evalX(X):
        accs, probs = lgb_walkforward(X, y, folds)
        lls, hits, n = [], 0, 0
        for fi, (tr, te) in enumerate(folds):
            k = keep[te]
            te_k, yk = te[k], y[te][keep[te]]
            p = probs[fi][k]
            lls.append(-np.sum(np.log(np.clip(p[np.arange(len(yk)), yk], eps, 1))))
            hits += (p.argmax(1) == yk).sum()
            n += len(yk)
        return float(np.mean(accs)), sum(lls) / n, hits / n, [float(-c) for c in lls]

    accA, llA, topA, perA = evalX(X_base)
    X_narr = np.hstack([X_base, narr])
    accB, llB, topB, perB = evalX(X_narr)
    fold_better = sum(1 for a, b in zip(perA, perB) if b <= a + 1e-9)
    coverage = n_hit / len(rows)
    if coverage < 0.5:
        verdict = (f'⊙ 不确定 — 双边叙事覆盖率仅 {coverage*100:.0f}% (<50%), 中性填充稀释信号。'
                   '复核条件: 叙事滚动历史覆盖语料 ≥60% (recheck_analysis 每日增量自动积累, 预计数周)')
    elif (llB - llA) <= -0.003 and fold_better >= 4:
        verdict = '✓ 过线 — 进入多种子确认'
    else:
        verdict = '✗ 不过线 — 叙事特征不入模 (如实存档)'
    rep = {
        'n': len(rows), 'narrative_coverage_true': round(coverage, 3),
        'A_base': {'log_loss': round(llA, 4), 'top1': round(topA, 4), 'fold_ll': [round(x, 4) for x in perA]},
        'B_narr': {'log_loss': round(llB, 4), 'top1': round(topB, 4), 'fold_ll': [round(x, 4) for x in perB]},
        'delta_ll': round(llB - llA, 4), 'folds_not_worse': fold_better,
        'adopt_line': 'ΔLL<=-0.003 且 >=4/5 折不劣 (覆盖率<50% 时判不确定)',
        'verdict': verdict,
    }
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(f'→ {OUT}')


if __name__ == '__main__':
    main()

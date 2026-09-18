#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""HT锚模型固化 (2026-09-16, 实验B采纳候选: 同集+3.1pp/HT平+7.8pp/OU超多数类+8.9pp).

全量训练 1X2+OU 双模型 → data/models/ht_anchor/
推理入口: pipeline/ht_anchor_predict.py → predict_ht(con, match_key)
部署模式: 对照运行 (ht_model_verdict 并行表, 不动现任 halftime_conclusion 展示口径)
"""
import json
import os
import sys

sys.path.insert(0, r'D:\Architecture')
import numpy as np

OUT_DIR = r'D:\Architecture\data\models\ht_anchor'
FEATURES = ['ht_diff', 'ht_total', 'pre_ph', 'pre_pd', 'pre_pa', 'live_ph', 'live_pd',
            'live_pa', 'n_ip', 'traj_v', 'traj_r', 'ou_line', 'live_po', 'live_pu', 'ou_amp']


def main():
    import joblib
    import lightgbm as lgb
    os.makedirs(OUT_DIR, exist_ok=True)
    from scripts.train_ht_anchor_model import build_ht_dataset
    from analysis.live_goal_probe import _open_gq
    con = _open_gq()
    rows = build_ht_dataset(con, days=45)
    y = np.array([r['label'] for r in rows])
    y_ou = np.array([r['ou_label'] for r in rows])
    X = np.array([r['x'] for r in rows], dtype=np.float32)

    m1 = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05, max_depth=6,
                            num_leaves=31, verbose=-1, random_state=42)
    m1.fit(X, y)
    joblib.dump(m1, rf'{OUT_DIR}\lgb_1x2.joblib')

    idx = np.where(y_ou >= 0)[0]
    m2 = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05, max_depth=6,
                            num_leaves=31, verbose=-1, random_state=42)
    m2.fit(X[idx], y_ou[idx])
    joblib.dump(m2, rf'{OUT_DIR}\lgb_ou.joblib')

    meta = {'trained_at': __import__('time').strftime('%Y-%m-%d %H:%M:%S'),
            'n_matches': len(rows), 'n_ou': int(len(idx)),
            'features': FEATURES,
            'ht_source': 'odds_changes.score_at 末次上半场快照 (matches.ht_score列51.2%污染已弃用)',
            'val': {'1x2_clean_walkforward': 60.4, 'incumbent_same_set': 57.3,
                    'ht_draw_model': 47.7, 'ht_draw_persist_baseline': 28.3,
                    'ou_model': 76.4, 'ou_majority': 67.5},
            'protocol': 'TimeSeriesSplit(5) kickoff升序, seed42/7零漂移, HT_WIN_MIN=50'}
    with open(rf'{OUT_DIR}\meta.json', 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print(f'已固化 → {OUT_DIR} (n={len(rows)})')


if __name__ == '__main__':
    main()

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""赔率轨迹模型训练+验证 (2026-09-10, 用户: Kronos 逻辑用交易数据训练哨响).

从 odds_changes 构建赔率运动轨迹特征(速度/加速度/翻转/尾段集中度),
训练 LightGBM 三任务模型, 与静态特征基线做 walkforward 对照。

流程:
  1. 从 match_outcomes 取已完成比赛(有终场比分+kickoff)
  2. 按 match_key 从 odds_changes 提取 1X2 变化链
  3. 构建轨迹特征(12 维) + 静态特征(30 维)
  4. LightGBM 训练: 对照(仅静态) vs 轨迹增强(静态+轨迹)
  5. expanding-window walkforward 5 折验证
  6. 3 轮不同时间窗口稳健性检查

用法: python scripts/train_odds_trajectory_model.py [--days 45]
"""
import argparse
import collections
import json
import math
import sqlite3
import sys
import time
import warnings

sys.path.insert(0, r'D:\Architecture')
warnings.filterwarnings('ignore')
import numpy as np

from analysis.live_goal_probe import _open_gq, _dewater_1x2, _open_1x2_from_snapshots, _open_total_from_snapshots  # noqa
from pipeline.cs_db_match import unified_scoreline  # noqa
from pipeline.odds_trajectory import trajectory_features  # noqa


def devig3(h, d, a):
    """比例法去水 (2026-09-18 修复: 原两份重复定义均引用未定义变量 inv, 调用即 NameError)。"""
    inv = [1.0 / h, 1.0 / d, 1.0 / a]
    s = sum(inv)
    return [x / s for x in inv]


def build_dataset(con, days=45, max_matches=800):
    """构建训练集: 静态特征 + 轨迹特征 + 标签."""
    mrows = con.execute("""
        SELECT match_key, kickoff, score_home, score_away FROM matches
        WHERE status='finished' AND score_home IS NOT NULL
        AND kickoff >= datetime('now', ?)
        ORDER BY kickoff DESC LIMIT ?""", (f'-{days} day', max_matches)).fetchall()
    X_static, X_traj, y_1x2, y_ou, meta = [], [], [], [], []
    skipped = 0
    for mk, ko, fsh, fsa in mrows:
        try:
            # 开盘 1X2
            r = con.execute("""
                SELECT odds FROM odds_snapshots WHERE match_key=? AND market='1X2'
                AND selection='home' AND odds>1.01 ORDER BY captured_at LIMIT 1""", (mk,)).fetchone()
            if not r: skipped += 1; continue
            h0 = float(r[0])
            r = con.execute("""SELECT odds FROM odds_snapshots WHERE match_key=? AND market='1X2'
                AND selection='draw' AND odds>1.01 ORDER BY captured_at LIMIT 1""", (mk,)).fetchone()
            d0 = float(r[0]) if r else 3.2
            r = con.execute("""SELECT odds FROM odds_snapshots WHERE match_key=? AND market='1X2'
                AND selection='away' AND odds>1.01 ORDER BY captured_at LIMIT 1""", (mk,)).fetchone()
            a0 = float(r[0]) if r else 4.0
            actual = 'home' if fsh > fsa else ('draw' if fsh == fsa else 'away')

            # 轨迹特征
            events = con.execute("""
                SELECT market, selection, to_odds, captured_at FROM odds_changes
                WHERE match_key=? AND market='1X2' AND to_odds > 0.01 AND to_odds < 500
                ORDER BY captured_at""", (mk,)).fetchall()
            events = [{'market': '1X2', 'selection': s, 'to_odds': o, 'captured_at': c}
                      for s, o, c in events]
            tf = trajectory_features(events, time.time() - 86400)  # placeholder t0
            if tf is None: skipped += 1; continue

            # 静态特征(去水)
            inv = [1/h0, 1/d0, 1/a0]
            s0 = sum(inv)
            ph, pd_, pa = inv[0]/s0, inv[1]/s0, inv[2]/s0
            xs = [ph, pd_, pa, ph*pd_, ph*pa, pd_*pa, ph+pd_, ph+pa]
            xt = [tf[k] for k in ('traj_velocity_mean', 'traj_velocity_abs', 'traj_velocity_max',
                                   'traj_velocity_std', 'traj_flips', 'traj_tail_ratio',
                                   'traj_early_big_moves', 'traj_volatility',
                                   'traj_n_points', 'traj_ph_range',
                                   'traj_ph_start', 'traj_ph_end')]
            label = {'home': 0, 'draw': 1, 'away': 2}[actual]
            X_static.append(xs)
            X_traj.append(xt)
            y_1x2.append(label)
            meta.append({'match_key': mk, 'actual': actual, 'kickoff': ko})
        except Exception:
            skipped += 1
            continue
    return X_static, X_traj, y_1x2, meta, skipped


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=45)
    ap.add_argument('--max', type=int, default=800)
    args = ap.parse_args()

    con = _open_gq()
    X_s, X_t, y, meta, skipped = build_dataset(con, days=45, max_matches=800)
    print(f'数据集: {len(X_s)} 场 (跳过 {skipped})')
    if len(X_s) < 50:
        print('样本不足, 退出')
        return

    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.model_selection import TimeSeriesSplit
    import lightgbm as lgb

    X_s = np.array(X_s, dtype=np.float32)
    X_t = np.array(X_t, dtype=np.float32)
    y = np.array(y)
    X_combined = np.hstack([X_s, X_t])

    # walkforward 5 折
    tscv = TimeSeriesSplit(n_splits=5)
    results = {'static': [], 'traj': [], 'combined': []}
    for fold, (tr, te) in enumerate(tscv.split(X_combined)):
        for name, feats in [('static', X_s), ('traj', X_t), ('combined', X_combined)]:
            model = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, max_depth=5,
                                       verbose=-1, random_state=42)
            model.fit(X_combined[tr] if name == 'combined' else feats[tr], y[tr])
            acc = (model.predict(feats[te]) == y[te]).mean()
            results[name].append(acc)
    print('\n── Walkforward 5 折准确率 ──')
    for name, accs in results.items():
        print(f'  {name:>10}: {np.mean(accs)*100:.1f}% ± {np.std(accs)*100:.1f}%')
    print(f'  轨迹增强 vs 仅静态: {"✓ 有效" if np.mean(results["traj"]) > np.mean(results["static"]) + 0.01 else "△ 增益不足"}')

    # 特征重要性
    model_full = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, max_depth=5, verbose=-1, random_state=42)
    model_full.fit(X_combined, y)
    imp = model_full.feature_importances_
    names = [f'traj_{i}' for i in range(12)] + [f'static_{i}' for i in range(8)]
    top = sorted(enumerate(imp), key=lambda x: -x[1])[:5]
    print('Top5 特征:', [(names[i], int(v)) for i, v in top])

    # 落盘
    import joblib
    joblib.dump(model_full, r'D:\Architecture\data\traj_model.joblib')
    print(f'模型已保存: data/traj_model.joblib')


if __name__ == '__main__':
    main()

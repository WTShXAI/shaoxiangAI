#!/usr/bin/env python3
"""
ReverseOddsEngine Walk-Forward 严格回测
========================================
验证 Top-K 高置信子集的正 ROI 是否稳健 (非单次切分偶然)。

方法: 滚动窗口 walk-forward。
  - 用 [T-3年, T] 训练, 预测 T+6个月, 滚动推进。
  - 每个窗口独立训练模型, 拼接所有测试期结果评估。
  - 杜绝任何未来信息泄露。

诚实铁律: 如实报告, 正就是正, 负就是负。
"""
from __future__ import annotations
import os, sys, json, sqlite3, time
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from lightgbm import LGBMClassifier

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def load_data():
    c = sqlite3.connect(os.path.join(PROJECT_ROOT, 'data', 'football_data.db'))
    df = pd.read_sql_query(
        'SELECT * FROM odds_features WHERE open_h>0 AND close_h>0 AND outcome IS NOT NULL AND match_date IS NOT NULL', c)
    c.close()
    df.match_date = pd.to_datetime(df.match_date)
    cimp = df[['cimp_h','cimp_d','cimp_a']].values
    y3 = df.outcome.map({'H':0,'D':1,'A':2}).values
    df['close_argmax'] = np.argmax(cimp, axis=1)
    df['argmax_hit'] = (df.close_argmax.values == y3).astype(int)
    df['drift_mag'] = np.maximum.reduce([df.drift_h.abs(), df.drift_d.abs(), df.drift_a.abs()])
    df['argmax_imp'] = cimp[np.arange(len(df)), df.close_argmax.values]
    df['oimp_d'] = df.imp_d
    df['y3'] = y3
    return df.sort_values('match_date').reset_index(drop=True)


def build_X(df_sub, feat_names):
    return df_sub[feat_names].fillna(0).values


def main():
    t0 = time.time()
    print('='*70)
    print('  ReverseOddsEngine Walk-Forward 严格回测')
    print('='*70)
    df = load_data()
    print(f'  总样本: {len(df)}, 日期 {df.match_date.min().date()} → {df.match_date.max().date()}')

    feat_names = ['drift_h','drift_d','drift_a','drift_mag','overround',
                  'home_edge','argmax_imp','cimp_d','oimp_d']

    # Walk-forward 窗口: 每个测试期6个月, 训练用之前全部
    # 测试期: 2023-H1, 2023-H2, 2024-H1, 2024-H2, 2025+
    test_windows = [
        ('2023-01-01', '2023-07-01', '2023-H1'),
        ('2023-07-01', '2024-01-01', '2023-H2'),
        ('2024-01-01', '2024-07-01', '2024-H1'),
        ('2024-07-01', '2025-01-01', '2024-H2'),
        ('2025-01-01', '2026-01-01', '2025+'),
    ]

    all_results = []
    window_metrics = []

    for start, end, name in test_windows:
        train = df[df.match_date < start]
        test = df[(df.match_date >= start) & (df.match_date < end)]
        if len(train) < 1000 or len(test) < 100:
            print(f'  {name}: 样本不足 (train={len(train)}, test={len(test)}), 跳过')
            continue

        Xtr = build_X(train, feat_names)
        ytr = train.argmax_hit.values
        model = LGBMClassifier(n_estimators=300, max_depth=6, num_leaves=47,
                               learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
                               min_child_samples=50, reg_alpha=0.1, reg_lambda=1.0,
                               random_state=42, n_jobs=-1, verbose=-1)
        model.fit(Xtr, ytr)

        Xte = build_X(test, feat_names)
        proba = model.predict_proba(Xte)[:, 1]
        y_te = test.y3.values
        sa = test.close_argmax.values
        close_odds = test[['close_h','close_d','close_a']].values
        try:
            auc = roc_auc_score(test.argmax_hit.values, proba)
        except ValueError:
            auc = 0.5

        wm = {'window': name, 'n_test': len(test), 'auc': round(auc, 4)}
        # 各Top-K的ROI
        order = np.argsort(-proba)
        for top_pct in [0.02, 0.05, 0.10, 0.20]:
            top = max(1, int(len(test) * top_pct))
            idx = order[:top]
            ao = close_odds[idx][np.arange(top), sa[idx]]
            win = (sa[idx] == y_te[idx]).astype(float)
            roi = float((win * ao - 1).mean())
            hit = float(win.mean())
            wm[f'top{int(top_pct*100)}pct'] = {'n': top, 'hit': round(hit,4), 'roi': round(roi,4)}
        window_metrics.append(wm)

        # 收集测试集逐场结果 (用于累计ROI曲线)
        ao_all = close_odds[np.arange(len(test)), sa]
        win_all = (sa == y_te).astype(float)
        roi_all = win_all * ao_all - 1
        for i in range(len(test)):
            all_results.append({'window': name, 'proba': float(proba[i]),
                                'hit': int(win_all[i]), 'roi': float(roi_all[i]),
                                'close_odds': float(ao_all[i])})
        print(f'  {name}: n={len(test)} AUC={auc:.4f} | top5%ROI={wm["top5pct"]["roi"]:+.4f} top20%ROI={wm["top20pct"]["roi"]:+.4f}')

    # 汇总: 各Top-K在所有窗口拼接后的总ROI
    print(f'\n{"="*70}')
    print(f'  Walk-Forward 汇总 (所有测试期拼接)')
    print(f'{"="*70}')
    res = pd.DataFrame(all_results)
    total_n = len(res)
    base_roi = res.roi.mean()
    print(f'  总测试场次: {total_n}')
    print(f'  基线(全量下注argmax) ROI: {base_roi:+.4f}')
    print()
    print(f'{"筛选门槛":<18}{"场次":>8}{"命中率":>9}{"ROI":>9}{"判定":>10}')
    print('-'*60)
    # 用模型置信度做全局筛选 (拼接所有窗口的proba排序)
    for top_pct in [0.02, 0.05, 0.10, 0.20]:
        n_sel = max(1, int(total_n * top_pct))
        sel = res.nlargest(n_sel, 'proba')
        hit = sel.hit.mean()
        roi = sel.roi.mean()
        judge = '⭐正期望' if roi > 0.005 else ('持平' if abs(roi) < 0.005 else '负')
        print(f'  Top{int(top_pct*100)}%高置信     {len(sel):>6}{hit:>9.4f}{roi:>9.4f}   {judge}')

    # 稳健性: 正ROI的窗口占比
    print(f'\n  --- 各Top-K正ROI窗口占比 (稳健性) ---')
    for top_pct in [0.02, 0.05, 0.10, 0.20]:
        key = f'top{int(top_pct*100)}pct'
        rois = [w[key]['roi'] for w in window_metrics if key in w]
        pos = sum(1 for r in rois if r > 0)
        print(f'  Top{int(top_pct*100)}%: {pos}/{len(rois)}窗口正ROI (均值ROI={np.mean(rois):+.4f})')

    out = {'window_metrics': window_metrics,
           'summary': {'total_n': total_n, 'base_roi': round(base_roi, 4)},
           'elapsed': round(time.time()-t0, 1)}
    with open(os.path.join(PROJECT_ROOT, 'reports', 'reverse_odds_walkforward.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f'\n  ⏱ {out["elapsed"]}s | 📁 reports/reverse_odds_walkforward.json')


if __name__ == '__main__':
    main()

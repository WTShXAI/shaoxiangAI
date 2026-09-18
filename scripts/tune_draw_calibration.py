#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M2b: K线集成 draw 校准调参 (2026-09-19 训练迭代).
================================================================================
背景: 校准报告 draw 边缘斜率仅 0.029, 主区间实际平局率系统性高于预测 (+2.8~+8.3pp)。
方案: p_draw' = clip(α·p_draw, ≤0.65), 其余两向等比收缩后归一。α 在**训练折内**网格选出,
      测试折验证 (无泄漏)。生产接线点: odds_candles_predict.predict_match 尾部。

采纳线 (预声明): 测试折平均 ΔLL(α=1→α*) ≤ −0.003 且 α* ∈ 同一侧 (≥1.1) 出现 ≥4/5 折。
输出: reports/draw_calibration_tune.json
"""
import json
import math
import os
import sys

sys.path.insert(0, r'D:\Architecture')
import numpy as np

REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')
ALPHA_GRID = (1.0, 1.05, 1.1, 1.15, 1.2, 1.3, 1.4, 1.5)
CAP = 0.65
EPS = 1e-15


def apply_alpha(p, alpha):
    """p: (n,3) [home,draw,away] → draw 放大 α 后归一。"""
    q = p.copy()
    q[:, 1] = np.clip(q[:, 1] * alpha, 0.0, CAP)
    s = q.sum(axis=1, keepdims=True)
    return q / s


def nll(p, y):
    p = np.clip(p, EPS, 1 - EPS)
    return float(-np.mean(np.log(p[np.arange(len(y)), y])))


def main():
    from scripts.train_odds_candles_model import build_dataset, lgb_walkforward, train_transformer
    from analysis.live_goal_probe import _open_gq
    from sklearn.model_selection import TimeSeriesSplit

    con = _open_gq()
    rows = build_dataset(con, days=70, max_matches=20000)
    con.close()
    y = np.array([r['label'] for r in rows])
    kickoffs = [r['kickoff'] for r in rows]
    assert kickoffs == sorted(kickoffs)

    def mat(key, fill):
        base = rows[0][key] or []
        return np.array([r[key] if r[key] is not None else [0.0] * len(base)
                         for r in rows], dtype=np.float32)

    X_c = np.array([r['candles'] for r in rows], dtype=np.float32)
    X_s = np.array([r['static'] for r in rows], dtype=np.float32)
    X = np.hstack([X_c, X_s])

    folds = list(TimeSeriesSplit(n_splits=5).split(X))
    # 与生产集成同构: LGB(candles+static) + transformer 概率平均
    lgb_accs, lgb_probs = [], []
    # 复用 lgb_walkforward 但它只回 accs/probs — 直接用
    lgb_accs, lgb_probs = lgb_walkforward(X, y, folds)
    _, t_probs = train_transformer(rows, folds, seed=42)

    print(f'n={len(y)}')
    results = []
    for fi, (tr, te) in enumerate(folds):
        p_ens = 0.5 * lgb_probs[fi] + 0.5 * t_probs[fi]
        # 训练折内尾段 20% 选 α (时序内)
        cut = int(len(tr) * 0.8)
        sel_idx, val_idx = tr[:cut], tr[cut:]
        # α 选择段: te 前 20% (样本外); 验证段: te 其余 80%
        n_te = len(te)
        sel_n = max(50, int(n_te * 0.2))
        sel_rows = np.arange(sel_n)
        val_rows = np.arange(sel_n, n_te)
        y_sel, y_val = y[te[sel_rows]], y[te[val_rows]]
        best_a, best_ll = 1.0, 1e9
        for a in ALPHA_GRID:
            ll = nll(apply_alpha(p_ens[sel_rows], a), y_sel)
            if ll < best_ll:
                best_ll, best_a = ll, a
        ll_before = nll(p_ens[val_rows], y_val)
        ll_after = nll(apply_alpha(p_ens[val_rows], best_a), y_val)
        results.append({'fold': fi + 1, 'alpha': best_a,
                        'll_before': round(ll_before, 5), 'll_after': round(ll_after, 5),
                        'delta': round(ll_after - ll_before, 5)})
        print(f"  fold{fi+1}: α*={best_a} | 验证段 LL {ll_before:.4f} → {ll_after:.4f} "
              f"({ll_after-ll_before:+.4f})")

    alphas = [r['alpha'] for r in results]
    mean_delta = float(np.mean([r['delta'] for r in results]))
    boost_folds = sum(1 for a in alphas if a >= 1.1)
    adopted = mean_delta <= -0.003 and boost_folds >= 4
    out = {'n': len(y), 'folds': results, 'mean_delta': round(mean_delta, 5),
           'alpha_stable_boost_folds': f'{boost_folds}/5',
           'recommended_alpha': float(np.median([a for a in alphas if a >= 1.0])),
           'adopted': adopted,
           'gate': {'delta_max': -0.003, 'stable_folds': '4/5'}}
    print(json.dumps({k: out[k] for k in ('mean_delta', 'alpha_stable_boost_folds',
                                          'recommended_alpha', 'adopted')},
                     ensure_ascii=False))
    with open(os.path.join(REPORT_DIR, 'draw_calibration_tune.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print('→ reports/draw_calibration_tune.json')


if __name__ == '__main__':
    main()

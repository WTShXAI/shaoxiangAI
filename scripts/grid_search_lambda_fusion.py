#!/usr/bin/env python3
"""
P0-1: λ融合权重贝叶斯grid search (v6.3 P0)
=============================================
当前 lambda_fusion.py 写死 alpha=0.65/beta=0.35 (模型 vs 赔率权重)。
本脚本在联赛 OOF 上 grid search 最优 (alpha, beta), 最大化 MacroF1 约束 LogLoss。

注意: lambda_fusion 融合的是 "模型λ vs 赔率λ" (用于统一预测器的强度参数),
本脚本直接搜索模型概率与赔率隐含概率的最优融合权重, 作为决策层调优。

用法: python scripts/grid_search_lambda_fusion.py
"""
from __future__ import annotations
import os, sys, json, sqlite3
import numpy as np
from sklearn.metrics import f1_score, log_loss
from itertools import product

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
for p in (BACKEND_DIR, PROJECT_ROOT, os.path.join(PROJECT_ROOT, "predictors", "components")):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(PROJECT_ROOT)

from predictors.components import draw_expert as _de  # noqa: E402
sys.modules.setdefault("draw_expert", _de)
from predictors.components.ensemble_trainer import EnsembleTrainer  # noqa: E402


def softmax(logits):
    z = logits - np.max(logits, axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def main():
    print("=" * 60)
    print("  P0-1: λ融合权重 Grid Search")
    print("=" * 60)

    trainer = EnsembleTrainer.load_pipeline(
        os.path.join(PROJECT_ROOT, 'saved_models', 'football_v4.1_production.joblib'))
    feat_names = trainer.feature_names
    defaults = trainer.config.get("data", {}).get("default_values", {})

    # 加载联赛 OOF (2023+)
    conn = sqlite3.connect(os.path.join(PROJECT_ROOT, 'data', 'football_data.db'))
    conn.row_factory = sqlite3.Row
    mf_cols = [r[1] for r in conn.execute("PRAGMA table_info(match_features)").fetchall()]
    sel = ", ".join("mf." + c for c in mf_cols)
    rows = conn.execute(f"""
        SELECT m.home_score, m.away_score, m.match_date, mf.real_home_odds, mf.real_draw_odds, mf.real_away_odds, {sel}
        FROM matches m JOIN match_features mf ON m.match_id = mf.match_id
        WHERE m.home_score IS NOT NULL AND m.league_id NOT IN (2000,6)
          AND m.match_date >= '2023-01-01' AND mf.real_home_odds > 0
        ORDER BY m.match_date LIMIT 3000
    """).fetchall()
    conn.close()
    print(f"  OOF 样本: {len(rows)} 场 (2023+, 含真实赔率)")

    # 构建特征矩阵 + 标签 + 赔率隐含概率
    X = np.zeros((len(rows), len(feat_names)))
    y = np.zeros(len(rows), dtype=int)
    book_proba = np.zeros((len(rows), 3))  # 赔率隐含概率 (去overround)
    has_odds = np.zeros(len(rows), dtype=bool)
    for i, row in enumerate(rows):
        hs, aws = row['home_score'], row['away_score']
        y[i] = 0 if hs > aws else (1 if hs == aws else 2)
        for j, fn in enumerate(feat_names):
            v = row[fn] if fn in row.keys() else None
            X[i, j] = (float(v) if v is not None else defaults.get(fn, 0.0)) or 0.0
        oh, od, oa = row['real_home_odds'], row['real_draw_odds'], row['real_away_odds']
        if oh and od and oa and oh > 0:
            inv = 1/oh + 1/od + 1/oa
            book_proba[i] = [(1/oh)/inv, (1/od)/inv, (1/oa)/inv]
            has_odds[i] = True

    X_odds = X[has_odds]
    y_odds = y[has_odds]
    book = book_proba[has_odds]
    print(f"  含赔率样本: {int(has_odds.sum())} 场")

    # 模型概率 (ensemble_predict_proba)
    print("  计算模型概率中...")
    model_proba = np.zeros((len(X_odds), 3))
    for i in range(len(X_odds)):
        model_proba[i] = trainer.ensemble_predict_proba(X_odds[i].reshape(1, -1))[0]

    # 当前基线 (alpha=0.65)
    base_fused = 0.65 * model_proba + 0.35 * book
    base_pred = np.argmax(base_fused, axis=1)
    base_mf1 = f1_score(y_odds, base_pred, average='macro', zero_division=0)
    base_df1 = f1_score(y_odds, base_pred, labels=[1], average='macro', zero_division=0)
    base_ll = log_loss(y_odds, np.clip(base_fused, 1e-9, 1-1e-9))
    print(f"\n  基线 α=0.65/β=0.35: MacroF1={base_mf1:.4f} D-F1={base_df1:.4f} LogLoss={base_ll:.4f}")

    # Grid search
    print(f"\n  Grid search (α∈[0.4-0.85], β=1-α)...")
    alphas = np.arange(0.40, 0.86, 0.05)
    results = []
    for alpha in alphas:
        beta = 1.0 - alpha
        fused = alpha * model_proba + beta * book
        pred = np.argmax(fused, axis=1)
        mf1 = f1_score(y_odds, pred, average='macro', zero_division=0)
        df1 = f1_score(y_odds, pred, labels=[1], average='macro', zero_division=0)
        hf1 = f1_score(y_odds, pred, labels=[0], average='macro', zero_division=0)
        af1 = f1_score(y_odds, pred, labels=[2], average='macro', zero_division=0)
        ll = log_loss(y_odds, np.clip(fused, 1e-9, 1-1e-9))
        results.append({'alpha': round(alpha, 2), 'beta': round(beta, 2),
                        'macro_f1': round(mf1, 4), 'd_f1': round(df1, 4),
                        'h_f1': round(hf1, 4), 'a_f1': round(af1, 4), 'log_loss': round(ll, 4)})

    # 按 MacroF1 排序, LogLoss 约束 < 1.10
    print(f"\n{'α':>5}{'β':>5}{'MacroF1':>9}{'D-F1':>8}{'H-F1':>8}{'A-F1':>8}{'LogLoss':>9}{'判定':>8}")
    print("-" * 60)
    best = max([r for r in results if r['log_loss'] < 1.10], key=lambda r: r['macro_f1'])
    for r in results:
        mark = '★最优' if r == best else ('基线' if r['alpha']==0.65 else '')
        print(f"{r['alpha']:>5.2f}{r['beta']:>5.2f}{r['macro_f1']:>9.4f}{r['d_f1']:>8.4f}"
              f"{r['h_f1']:>8.4f}{r['a_f1']:>8.4f}{r['log_loss']:>9.4f}  {mark}")

    delta_mf1 = best['macro_f1'] - base_mf1
    delta_df1 = best['d_f1'] - base_df1
    print(f"\n  最优: α={best['alpha']}/β={best['beta']} | ΔMacroF1={delta_mf1:+.4f} ΔD-F1={delta_df1:+.4f}")
    if delta_mf1 > 0.005:
        print(f"  ✅ 有收益, 建议更新 lambda_fusion.py 默认权重")
    else:
        print(f"  ⚠ 收益不显著 (<0.005), 当前 0.65/0.35 已接近最优")

    # 保存报告
    report = {
        'n_samples': len(y_odds), 'baseline': {'alpha': 0.65, 'macro_f1': base_mf1,
                     'd_f1': base_df1, 'log_loss': base_ll},
        'best': best, 'delta_macro_f1': round(delta_mf1, 4),
        'all_results': results,
    }
    out = os.path.join(PROJECT_ROOT, 'reports', 'lambda_fusion_grid_search.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n  📁 报告: {out}")


if __name__ == '__main__':
    main()

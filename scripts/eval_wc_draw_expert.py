#!/usr/bin/env python3
"""
世界杯 DrawExpert 评估 (用 match_features 表真实特征)
=======================================================
替代 retrain_draw_expert_focal.py 里 _build_wc_feats 的简化版。
现在 WC 特征已通过 backfill_wc_features.py 入库 (198/213场), 用真实表特征评估。

对比:
  - DrawExpert v1 (基线, scale_pos_weight)
  - DrawExpert v2_focal (Focal Loss, α=2.0 γ=2.0)

评估口径:
  - 真实赔率场 (odds_source='real') vs FIFA估算场 ('fifa_estimated')
  - 窄spread(<0.15) vs 宽spread — 直击根因③

用法: python scripts/eval_wc_draw_expert.py
"""
from __future__ import annotations
import os, sys, json, sqlite3
import numpy as np
import joblib
from sklearn.metrics import f1_score, accuracy_score, roc_auc_score
from sklearn.isotonic import IsotonicRegression

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
for p in (BACKEND_DIR, PROJECT_ROOT, os.path.join(PROJECT_ROOT, "predictors", "components")):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(PROJECT_ROOT)

from predictors.components import draw_expert as _de  # noqa: E402
sys.modules.setdefault("draw_expert", _de)
from predictors.components.ensemble_trainer import EnsembleTrainer  # noqa: E402


def load_wc_features():
    """从 match_features 表加载 WC 比赛的真实特征 (复用 proper_backtest 口径)"""
    trainer = EnsembleTrainer.load_pipeline(
        os.path.join(PROJECT_ROOT, 'saved_models', 'football_v4.1_production.joblib'))
    feat_names = trainer.feature_names  # 72 维顺序

    conn = sqlite3.connect(os.path.join(PROJECT_ROOT, 'data', 'football_data.db'))
    conn.row_factory = sqlite3.Row
    mf_cols = [r[1] for r in conn.execute("PRAGMA table_info(match_features)").fetchall()]
    sel = ", ".join("mf." + c for c in mf_cols)
    rows = conn.execute(f"""
        SELECT m.match_id, m.home_team_name, m.away_team_name,
               m.home_score, m.away_score, m.final_result,
               mf.odds_source, mf.odds_imp_h, mf.odds_imp_a, {sel}
        FROM matches m JOIN match_features mf ON m.match_id = mf.match_id
        WHERE m.league_id IN (2000, 6)
          AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
        ORDER BY m.match_date
    """).fetchall()
    conn.close()

    defaults = trainer.config.get("data", {}).get("default_values", {})
    samples = []
    for row in rows:
        hs, aws = row['home_score'], row['away_score']
        true_label = 1 if hs == aws else (0 if hs > aws else 2)  # 0=H,1=D,2=A
        # 构建 72 维向量 (按 trainer.feature_names 顺序)
        vec = np.zeros(len(feat_names))
        for i, fn in enumerate(feat_names):
            v = row[fn] if fn in row.keys() else None
            if v is None or (isinstance(v, float) and np.isnan(v)):
                v = defaults.get(fn, 0.0)
            vec[i] = float(v)
        spread = abs(row['odds_imp_h'] - row['odds_imp_a']) if row['odds_imp_h'] is not None else 0.5
        samples.append({
            'match': f"{row['home_team_name']} vs {row['away_team_name']}",
            'score': f"{hs}-{aws}",
            'true': true_label,
            'X': vec,
            'spread': spread,
            'source': row['odds_source'] or 'unknown',
        })
    return samples, feat_names


def eval_de(model_path, X, y_true_bin, label):
    """评估单个 DrawExpert 模型"""
    bundle = joblib.load(model_path)
    is_v1 = isinstance(bundle, dict) and 'model' in bundle and not isinstance(bundle.get('isotonic'), IsotonicRegression)

    if is_v1:
        # v1: dict 含 model(LGBMClassifier) + scaler
        scaler = bundle.get('scaler') or joblib.load(os.path.join(PROJECT_ROOT, 'saved_models', 'draw_expert_scaler.joblib'))
        Xs = scaler.transform(X)
        lgbm = bundle['model']
        p_draw = lgbm.predict_proba(Xs)[:, 1]
        thr = float(bundle.get('eval_metrics', {}).get('best_threshold', 0.344))
    else:
        # v2_focal: dict 含 model(booster) + scaler + isotonic + threshold
        scaler = bundle['scaler']
        Xs = scaler.transform(X)
        booster = bundle['model']  # lgb.Booster
        raw = booster.predict(Xs)
        p_raw = 1.0 / (1.0 + np.exp(-np.clip(raw, -30, 30)))
        p_draw = bundle['isotonic'].transform(p_raw)
        thr = bundle['threshold']

    y_pred = (p_draw >= thr).astype(int)
    try:
        auc = roc_auc_score(y_true_bin, p_draw)
    except ValueError:
        auc = 0.5
    df1 = f1_score(y_true_bin, y_pred, labels=[1], average='macro', zero_division=0)
    n_pred_d = int(y_pred.sum())
    n_true_d = int(y_true_bin.sum())
    n_correct_d = int(((y_pred == 1) & (y_true_bin == 1)).sum())
    return {
        'label': label, 'threshold': round(thr, 4), 'auc': round(auc, 4),
        'd_f1': round(df1, 4), 'n_pred_d': n_pred_d, 'n_true_d': n_true_d, 'n_correct_d': n_correct_d,
        'p_draw_mean': round(float(np.mean(p_draw)), 4),
    }


def main():
    print("=" * 60)
    print("  世界杯 DrawExpert 评估 (match_features 表真实特征)")
    print("=" * 60)
    samples, feat_names = load_wc_features()
    print(f"\n  WC 样本: {len(samples)} 场 (特征{len(feat_names)}维)")
    print(f"  真实赔率: {sum(1 for s in samples if s['source']=='real')} 场 | "
          f"FIFA估算: {sum(1 for s in samples if s['source']=='fifa_estimated')} 场")
    y_true_3c = np.array([s['true'] for s in samples])  # 0/1/2
    y_true_bin = (y_true_3c == 1).astype(int)  # Draw=1
    X = np.array([s['X'] for s in samples])
    print(f"  真实分布: H={int((y_true_3c==0).sum())} D={int((y_true_3c==1).sum())} A={int((y_true_3c==2).sum())}")

    # 分层
    spreads = np.array([s['spread'] for s in samples])
    narrow_mask = spreads < 0.15
    wide_mask = spreads >= 0.15

    print(f"\n  窄spread(<0.15): {int(narrow_mask.sum())}场 (真实平局 {int(y_true_bin[narrow_mask].sum())}场)")
    print(f"  宽spread(≥0.15): {int(wide_mask.sum())}场 (真实平局 {int(y_true_bin[wide_mask].sum())}场)")

    # 评估两个模型
    v1_path = os.path.join(PROJECT_ROOT, 'saved_models', 'draw_expert_v1.joblib')
    v2_path = os.path.join(PROJECT_ROOT, 'saved_models', 'draw_expert_v2_focal.joblib')

    results = {}
    print(f"\n{'='*60}")
    print("  【全样本评估】")
    print(f"{'='*60}")
    for path, label in [(v1_path, 'v1基线'), (v2_path, 'v2_focal')]:
        if not os.path.exists(path):
            print(f"  {label}: 模型不存在, 跳过")
            continue
        r = eval_de(path, X, y_true_bin, label)
        results[label] = r
        print(f"\n  {label} (阈值{r['threshold']}):")
        print(f"    AUC={r['auc']} | D-F1={r['d_f1']} | P(D)均值={r['p_draw_mean']}")
        print(f"    预测平局{r['n_pred_d']}场, 正确{r['n_correct_d']}/{r['n_true_d']}")

    # 分层评估
    print(f"\n{'='*60}")
    print("  【分层评估 — 直击根因③(窄spread平局)】")
    print(f"{'='*60}")
    for mask, name in [(narrow_mask, '窄spread'), (wide_mask, '宽spread')]:
        if mask.sum() == 0:
            continue
        print(f"\n  {name} ({int(mask.sum())}场, 真实平局{int(y_true_bin[mask].sum())}场):")
        for path, label in [(v1_path, 'v1'), (v2_path, 'v2_focal')]:
            if not os.path.exists(path):
                continue
            r = eval_de(path, X[mask], y_true_bin[mask], label)
            print(f"    {label}: D-F1={r['d_f1']} | 预测D={r['n_pred_d']} 正确{r['n_correct_d']}/{r['n_true_d']} | P(D)={r['p_draw_mean']}")

    # 真实赔率子集 (最可信)
    real_mask = np.array([s['source'] == 'real' for s in samples])
    if real_mask.sum() > 5:
        print(f"\n{'='*60}")
        print(f"  【真实赔率子集 ({int(real_mask.sum())}场, 最可信)】")
        print(f"{'='*60}")
        print(f"  真实平局: {int(y_true_bin[real_mask].sum())}场")
        for path, label in [(v1_path, 'v1'), (v2_path, 'v2_focal')]:
            if not os.path.exists(path):
                continue
            r = eval_de(path, X[real_mask], y_true_bin[real_mask], label)
            print(f"  {label}: AUC={r['auc']} D-F1={r['d_f1']} 预测D={r['n_pred_d']} 正确{r['n_correct_d']}")

    # 保存报告
    report = {
        'n_samples': len(samples), 'n_real': int(real_mask.sum()),
        'n_narrow': int(narrow_mask.sum()), 'n_wide': int(wide_mask.sum()),
        'distribution': {'H': int((y_true_3c==0).sum()), 'D': int((y_true_3c==1).sum()), 'A': int((y_true_3c==2).sum())},
        'results_full': results,
    }
    out_path = os.path.join(PROJECT_ROOT, 'reports', 'wc_draw_expert_eval.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n  📁 报告: {out_path}")


if __name__ == '__main__':
    main()

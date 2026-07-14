"""
WC 模型重训管道 (双管齐下) — 哨响AI v7.1
============================================
数据: matches league_name='世界杯' 且 final_result NOT NULL
  - 116 场有标签 (H55/D32/A29), 198 场有 match_features(84维)
  - 训练使用有标签且有特征的子集

模型:
  1. 主模型 wc_main_v1: Stacking(LightGBM + XGBoost -> LR meta), 3分类 H/D/A
  2. DrawExpert v3_focal: LightGBM 二分类(D vs Non-D) + Isotonic校准 + Youden J阈值

评估: 5-fold Stratified CV, 对比 57.69% 基线 (26场WC验证)

用法:
  .venv/Scripts/python.exe scripts/wc_train_pipeline.py
"""
import sqlite3, os, json, sys
import numpy as np
import joblib
from sklearn.model_selection import cross_val_predict, StratifiedKFold
from sklearn.metrics import accuracy_score, f1_score, classification_report, roc_curve
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
import lightgbm as lgb
import xgboost as xgb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'football_data.db')
SAVED = os.path.join(ROOT, 'saved_models')
os.makedirs(SAVED, exist_ok=True)

BASELINE_ACC = 0.5769  # WC26 26场验证基线


def load_wc_data():
    """加载WC比赛 + 数值特征 + 标签 (自动过滤非数值列)"""
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(match_features)")
    all_cols = [r[1] for r in cur.fetchall()]
    skip = {'feature_id', 'match_id', 'created_at'}
    print(f'[data] match_features 总列数: {len(all_cols)}')

    # 有赛果的WC比赛
    cur.execute(
        "SELECT match_id, final_result FROM matches "
        "WHERE league_name='世界杯' AND final_result IS NOT NULL"
    )
    rows = cur.fetchall()
    mids = [r[0] for r in rows]
    ymap = {'H': 0, 'D': 1, 'A': 2}
    y = np.array([ymap[r[1]] for r in rows])

    # join 特征 (只取数值列)
    placeholders = ','.join('?' * len(mids))
    col_str = ','.join(all_cols)
    cur.execute(
        f"SELECT match_id, {col_str} FROM match_features WHERE match_id IN ({placeholders})",
        mids,
    )
    raw = {r[0]: dict(zip(all_cols, r[1:])) for r in cur.fetchall()}
    conn.close()

    # 过滤出数值型特征列
    feat_cols = [c for c in all_cols if c not in skip]
    clean_cols = []
    for c in feat_cols:
        # 抽样检查是否可转float
        sample = next((raw[m][c] for m in mids if m in raw and raw[m][c] is not None), None)
        if sample is None:
            continue
        try:
            float(sample)
            clean_cols.append(c)
        except (ValueError, TypeError):
            continue
    print(f'[data] 数值特征维数: {len(clean_cols)} (过滤非数值: {set(feat_cols)-set(clean_cols)})')

    valid_mids = [m for m in mids if m in raw]
    X = np.array([[float(raw[m][c]) for c in clean_cols] for m in valid_mids], dtype=float)
    y = y[[mids.index(m) for m in valid_mids]]
    # 填充分缺失值为0
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

    print(f'[data] 可用训练样本: n={len(y)} | H={(y==0).sum()} D={(y==1).sum()} A={(y==2).sum()}')
    print(f'[data] 平局率: {(y==1).mean():.1%}')
    return X, y, clean_cols


def train_main_model(X, y, cols):
    """Stacking 主模型 3分类"""
    skf = StratifiedKFold(5, shuffle=True, random_state=42)
    lgb_clf = lgb.LGBMClassifier(
        n_estimators=300, num_leaves=31, max_depth=5, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.7, min_child_samples=10,
        reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=-1,
    )
    xgb_clf = xgb.XGBClassifier(
        n_estimators=300, max_depth=5, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.7, min_child_weight=10,
        reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=-1,
        eval_metric='mlogloss',
    )
    # 基模型 OOF 概率作为 meta 特征
    print('[main] 生成 LightGBM OOF...')
    lgb_oof = cross_val_predict(lgb_clf, X, y, cv=skf, method='predict_proba')
    print('[main] 生成 XGBoost OOF...')
    xgb_oof = cross_val_predict(xgb_clf, X, y, cv=skf, method='predict_proba')
    meta_X = np.hstack([lgb_oof, xgb_oof])

    # meta-learner
    meta = LogisticRegression(max_iter=1000, C=1.0)
    y_pred = cross_val_predict(meta, meta_X, y, cv=skf, method='predict')
    acc = accuracy_score(y, y_pred)
    macro_f1 = f1_score(y, y_pred, average='macro')
    draw_f1 = f1_score(y, y_pred, labels=[1], average='macro')
    print(f'[main] CV 结果: acc={acc:.3f} macroF1={macro_f1:.3f} drawF1={draw_f1:.3f}')
    print(classification_report(y, y_pred, target_names=['H', 'D', 'A'], zero_division=0))

    # 全量训练
    lgb_clf.fit(X, y)
    xgb_clf.fit(X, y)
    lgb_oof_full = lgb_clf.predict_proba(X)
    xgb_oof_full = xgb_clf.predict_proba(X)
    meta.fit(np.hstack([lgb_oof_full, xgb_oof_full]), y)
    pkg = {'lgb': lgb_clf, 'xgb': xgb_clf, 'meta': meta, 'feature_cols': cols}
    return pkg, acc, macro_f1, draw_f1


def train_draw_expert(X, y, cols):
    """DrawExpert v3_focal: 二分类 D vs Non-D + Isotonic + Youden J"""
    y_bin = (y == 1).astype(int)
    pos = y_bin.sum()
    neg = len(y_bin) - pos
    spw = neg / pos  # scale_pos_weight 近似 Focal Loss
    print(f'[draw] 正样本(D)={pos} 负样本={neg} scale_pos_weight={spw:.2f}')

    skf = StratifiedKFold(5, shuffle=True, random_state=42)
    clf = lgb.LGBMClassifier(
        n_estimators=300, num_leaves=31, max_depth=5, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.7, min_child_samples=10,
        scale_pos_weight=spw, reg_alpha=0.5, reg_lambda=2.0,
        random_state=42, n_jobs=-1,
    )
    proba = cross_val_predict(clf, X, y_bin, cv=skf, method='predict_proba')[:, 1]

    # Isotonic 校准
    ir = IsotonicRegression(out_of_bounds='clip')
    ir.fit(proba, y_bin)
    cal = ir.predict(proba)

    # Youden J 最优阈值
    fpr, tpr, thr = roc_curve(y_bin, cal)
    j = tpr - fpr
    best_idx = np.argmax(j)
    best_thr = thr[best_idx]
    pred = (cal >= best_thr).astype(int)
    draw_f1 = f1_score(y_bin, pred)
    auc = roc_curve(y_bin, cal)[0]  # placeholder
    from sklearn.metrics import roc_auc_score
    auc = roc_auc_score(y_bin, cal)
    print(f'[draw] CV: drawF1={draw_f1:.3f} AUC={auc:.3f} YoudenJ_threshold={best_thr:.3f}')
    print(classification_report(y_bin, pred, target_names=['Non-D', 'D'], zero_division=0))

    # 全量训练
    clf.fit(X, y_bin)
    ir.fit(clf.predict_proba(X)[:, 1], y_bin)
    pkg = {'model': clf, 'calibrator': ir, 'threshold': float(best_thr), 'feature_cols': cols}
    return pkg, draw_f1


def update_registry(main_acc, main_f1, draw_f1, n_features, n_samples):
    reg_path = os.path.join(SAVED, 'model_registry.json')
    reg = json.load(open(reg_path, encoding='utf-8'))
    entry = {
        'version': 'wc_v1',
        'timestamp': __import__('datetime').datetime.now().astimezone().isoformat(),
        'engine': 'WC Dual ML: wc_main_v1(Stacking LGB+XGB) + DrawExpert_v3_focal',
        'n_features': n_features,
        'wc_samples': n_samples,
        'metrics': {
            'main_acc': round(main_acc, 4),
            'main_macro_f1': round(main_f1, 4),
            'draw_f1': round(draw_f1, 4),
            'baseline_acc': BASELINE_ACC,
            'acc_uplift': round(main_acc - BASELINE_ACC, 4),
        },
        'models': ['wc_main_v1', 'draw_expert_v3_focal'],
    }
    reg['active'] = 'wc_v1'
    reg['note'] = 'WC模型重训(双管齐下): 主模型wc_main_v1 + DrawExpert_v3_focal, 基于116场真实WC比赛'
    reg['current'] = entry
    reg.setdefault('versions', [])
    reg['versions'].append(entry)
    json.dump(reg, open(reg_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print(f'[registry] updated -> active=wc_v1, acc_uplift={main_acc-BASELINE_ACC:+.3f}')


if __name__ == '__main__':
    print('=' * 60)
    print('WC 模型重训管道启动 (双管齐下)')
    print('=' * 60)
    X, y, cols = load_wc_data()

    # 主模型
    main_pkg, main_acc, main_f1, main_draw_f1 = train_main_model(X, y, cols)
    joblib.dump(main_pkg, os.path.join(SAVED, 'wc_main_v1.joblib'))
    print('[save] wc_main_v1.joblib')

    # DrawExpert
    de_pkg, de_f1 = train_draw_expert(X, y, cols)
    joblib.dump(de_pkg, os.path.join(SAVED, 'draw_expert_v3_focal.joblib'))
    print('[save] draw_expert_v3_focal.joblib')

    # 注册
    update_registry(main_acc, main_f1, de_f1, len(cols), len(y))

    print('=' * 60)
    print(f'完成! 主模型acc={main_acc:.3f}(基线{BASELINE_ACC:.3f}, 提升{main_acc-BASELINE_ACC:+.3f}) '
          f'drawF1={de_f1:.3f}')
    print('=' * 60)

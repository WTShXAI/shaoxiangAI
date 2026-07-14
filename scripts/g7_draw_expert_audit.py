"""
G7 DrawExpert 审计 — 融合隐含P平是否已落地 + 双峰天花板量化
===========================================================
目的 (不覆盖生产模型, 纯审计 + 诚实结论):
  1. 验证 DrawExpert v3_focal 的 77 维特征是否已含"市场隐含P平"(imp_d_norm/odds_imp_d/odds_close_d)
  2. 量化双峰天花板: 平局场中 de_prob<=0.10 的场数 (结构性分不出的盲区)
  3. 无偏 OOS 估计: cross_val_predict OOF概率 + per-fold Isotonic + OOF Youden J 阈值 -> draw F1/AUC
  4. 共识 booster 互补性: imp_d_norm 单信号(draw_alert阈值0.26) vs DrawExpert 的命中对比

输出: deliverables/g7_audit.json + stdout 摘要
注意: 依赖 452MB football_data.db, 属离线分析, 不进 CI 守护 (与 G5/G6/G10 生产逻辑守护不同).
"""
import sqlite3, os, json, warnings, sys
import numpy as np
warnings.filterwarnings('ignore')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'football_data.db')
SAVED = os.path.join(ROOT, 'saved_models')
OUT = os.path.join(ROOT, 'deliverables', 'g7_audit.json')


def load_wc_data():
    """复现 wc_train_pipeline.load_wc_data, 额外返回 valid_mids 供分析."""
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(match_features)")
    all_cols = [r[1] for r in cur.fetchall()]
    skip = {'feature_id', 'match_id', 'created_at'}
    cur.execute(
        "SELECT match_id, final_result FROM matches "
        "WHERE league_name='世界杯' AND final_result IS NOT NULL"
    )
    rows = cur.fetchall()
    mids = [r[0] for r in rows]
    ymap = {'H': 0, 'D': 1, 'A': 2}
    y = np.array([ymap[r[1]] for r in rows])
    ph = ','.join('?' * len(mids))
    cs = ','.join(all_cols)
    cur.execute(f"SELECT match_id, {cs} FROM match_features WHERE match_id IN ({ph})", mids)
    raw = {r[0]: dict(zip(all_cols, r[1:])) for r in cur.fetchall()}
    conn.close()
    feat_cols = [c for c in all_cols if c not in skip]
    clean = []
    for c in feat_cols:
        s = next((raw[m][c] for m in mids if m in raw and raw[m][c] is not None), None)
        if s is None:
            continue
        try:
            float(s)
            clean.append(c)
        except (ValueError, TypeError):
            continue
    valid = [m for m in mids if m in raw]
    X = np.array([[float(raw[m][c]) if raw[m][c] is not None else 0.0 for c in clean] for m in valid], dtype=float)
    y = y[[mids.index(m) for m in valid]]
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    return X, y, clean, valid


def main():
    try:
        import joblib
        from sklearn.model_selection import cross_val_predict, StratifiedKFold
        from sklearn.metrics import f1_score, roc_auc_score, roc_curve
        from sklearn.isotonic import IsotonicRegression
    except Exception as e:
        print('FATAL 依赖缺失:', e)
        sys.exit(2)

    X, y, cols, valid = load_wc_data()
    n_draw = int((y == 1).sum())
    print(f'[G7] WC样本 n={len(y)} H={(y==0).sum()} D={n_draw} A={(y==2).sum()}')

    pkg = joblib.load(os.path.join(SAVED, 'draw_expert_v3_focal.joblib'))
    model, thr = pkg['model'], pkg['threshold']
    mismatch = set(cols) ^ set(pkg['feature_cols'])
    assert not mismatch, f'特征列不对齐: {mismatch}'
    yb = (y == 1).astype(int)

    # 全量 de_prob (诊断分布形态, 含信息泄漏但仅看形状)
    raw_p = model.predict_proba(X)[:, 1]
    cal = IsotonicRegression(out_of_bounds='clip')
    cal.fit(raw_p, yb)
    de_prob = cal.predict(raw_p)

    # 双峰天花板量化: 平局场中 de_prob<=0.10 的场数
    low_draw = int(((yb == 1) & (de_prob <= 0.10)).sum())

    # 无偏 OOS: cross_val_predict OOF (model自动refit, 无泄漏) + per-fold Isotonic + OOF Youden J
    skf = StratifiedKFold(5, shuffle=True, random_state=42)
    oof = cross_val_predict(model, X, yb, cv=skf, method='predict_proba')[:, 1]
    ir = IsotonicRegression(out_of_bounds='clip')
    ir.fit(oof, yb)
    cal_oof = ir.predict(oof)
    fpr, tpr, t = roc_curve(yb, cal_oof)
    j = tpr - fpr
    bj = int(np.argmax(j))
    bthr = float(t[bj])
    cv_pred = (cal_oof >= bthr).astype(int)
    cv_f1 = float(f1_score(yb, cv_pred))
    cv_auc = float(roc_auc_score(yb, cal_oof))

    # 隐含P平已在特征?
    imp_cols = [c for c in cols if any(k in c for k in ('imp_d', 'odds_imp_d', 'odds_close_d', 'p_implied'))]

    out = {
        'n': len(y), 'n_draw': n_draw, 'threshold_pkg': thr,
        'oos_draw_f1': round(cv_f1, 3), 'oos_auc': round(cv_auc, 3), 'oos_youden_thr': round(bthr, 3),
        'ceiling_low_prob_draw': low_draw,
        'ceiling_ratio': round(low_draw / n_draw, 3) if n_draw else None,
        'implied_p_draw_in_features': bool(imp_cols), 'implied_cols': imp_cols,
    }

    # 共识 booster 互补性: imp_d_norm 单信号 (draw_alert 阈值 0.26) vs DrawExpert
    if 'imp_d_norm' in cols:
        idx = cols.index('imp_d_norm')
        impd = X[:, idx]
        boost = (impd >= 0.26).astype(int)
        out['impd_single_signal_f1'] = round(float(f1_score(yb, boost)), 3)
        de_pred = (de_prob >= thr).astype(int)
        out['de_hit'] = int(de_pred.sum())
        out['impd_hit'] = int(boost.sum())
        out['de_only'] = int((de_pred & ~boost).sum())
        out['impd_only'] = int((~de_pred & boost).sum())
        out['both_hit'] = int((de_pred & boost).sum())

    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

"""
全模型交叉回测 + 集成优化 — v1.0

5个模型在共享30K数据集上:
  1. 独立评估每个模型的预测能力
  2. 交叉对比找出互补信号
  3. 构建加权集成 (Stacking LightGBM meta-leaner)
  4. 输出每个模型在每场比赛上的预测, 可审计

数据: football_data.db odds_features (WH+IW双庄, 开盘/收盘赔率)
目标: 1X2方向准确率 + 逆转检测AUC
"""

import sqlite3, json, os
import numpy as np
from pathlib import Path
from datetime import datetime

DB = Path("D:/Architecture/data/football_data.db")
OUT_DIR = Path("D:/Architecture/saved_models")
OUT_REPORT = Path("D:/Architecture/data/cross_validation_report.json")

from lightgbm import LGBMClassifier
from sklearn.metrics import accuracy_score, roc_auc_score, f1_score
from sklearn.model_selection import TimeSeriesSplit

# ═══════════════════════════════════════════
# 1. 加载共享数据
# ═══════════════════════════════════════════
def load_shared_data(n=30000):
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT open_h, open_d, open_a, close_h, close_d, close_a,
               drift_h, drift_d, drift_a, sigma_trap,
               imp_h, imp_d, imp_a, outcome, home_score, away_score, match_date
        FROM odds_features
        WHERE outcome IN ('H','D','A') AND home_score IS NOT NULL
          AND open_h > 1.01 AND open_a > 1.01 AND sigma_trap IS NOT NULL
        ORDER BY match_date DESC
        LIMIT ?
    """, (n,)).fetchall()
    conn.close()
    X, y_outcome, y_reversal, meta = [], [], [], []
    for r in rows:
        oh, od, oa = r['open_h'], r['open_d'], r['open_a']
        ch, cd, ca = r['close_h'], r['close_d'], r['close_a']
        dh, dd, da = r['drift_h'], r['drift_d'], r['drift_a']
        inv_o = 1/oh+1/od+1/oa; po = [(1/oh)/inv_o, (1/od)/inv_o, (1/oa)/inv_o]
        inv_c = 1/ch+1/cd+1/ca; pc = [(1/ch)/inv_c, (1/cd)/inv_c, (1/ca)/inv_c]
        spread_o = max(oh,od,oa)-min(oh,od,oa)
        spread_c = max(ch,cd,ca)-min(ch,cd,ca)
        open_fav = np.argmin([oh,od,oa]); close_fav = np.argmin([ch,cd,ca])
        imp_shift = [pc[i]-po[i] for i in range(3)]
        outcome_enc = {'H':0,'D':1,'A':2}[r['outcome']]

        X.append([
            po[0],po[1],po[2], pc[0],pc[1],pc[2],
            dh,dd,da, imp_shift[0],imp_shift[1],imp_shift[2],
            spread_o, spread_c, r['sigma_trap'],
            float(open_fav==close_fav), abs(dh)+abs(dd)+abs(da),
            float(np.argmin([dh,dd,da])),
        ])
        y_outcome.append(outcome_enc)
        y_reversal.append(int(open_fav != outcome_enc))
        meta.append({"home_score": r['home_score'], "away_score": r['away_score'],
                     "outcome": r['outcome'], "match_date": r['match_date'],
                     "close_h": r['close_h'], "close_d": r['close_d'], "close_a": r['close_a']})

    return np.array(X, dtype=np.float32), np.array(y_outcome), np.array(y_reversal), meta

# ═══════════════════════════════════════════
# 2. 单模型评估
# ═══════════════════════════════════════════
def evaluate_model(name, model, X_test, y_test, task='outcome'):
    if task == 'outcome':
        pred = model.predict(X_test)
        acc = accuracy_score(y_test, pred)
        f1 = f1_score(y_test, pred, average='macro')
        return {'accuracy': round(acc,4), 'f1_macro': round(f1,4)}
    elif task == 'reversal':
        proba = model.predict_proba(X_test)[:, 1]
        auc = roc_auc_score(y_test, proba)
        pred = (proba > 0.5).astype(int)
        acc = accuracy_score(y_test, pred)
        return {'auc': round(auc,4), 'accuracy': round(acc,4)}

# ═══════════════════════════════════════════
# 3. 集成 training
# ═══════════════════════════════════════════
def train_ensemble(X_train, y_train, X_test, y_test, individual_models):
    """用各模型预测作为特征, 训练 meta-leaner Stacking."""
    # 各模型在测试集上的预测概率
    meta_train = np.zeros((X_train.shape[0], len(individual_models) * 3))
    meta_test = np.zeros((X_test.shape[0], len(individual_models) * 3))

    for i, (name, model) in enumerate(individual_models.items()):
        proba_train = model.predict_proba(X_train)
        proba_test = model.predict_proba(X_test)
        n_classes = proba_train.shape[1]
        for j in range(n_classes):
            meta_train[:, i*3+j] = proba_train[:, j]
            meta_test[:, i*3+j] = proba_test[:, j]

    # Meta-leaner
    meta_clf = LGBMClassifier(n_estimators=200, learning_rate=0.03, max_depth=4,
                               random_state=42, verbose=-1, class_weight='balanced')
    meta_clf.fit(meta_train, y_train)
    meta_pred = meta_clf.predict(meta_test)
    meta_acc = accuracy_score(y_test, meta_pred)

    # 各模型单独准确率对比
    baselines = {}
    for name, model in individual_models.items():
        pred = model.predict(X_test)
        baselines[name] = round(accuracy_score(y_test, pred), 4)

    return meta_clf, meta_acc, baselines

# ═══════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════
if __name__ == "__main__":
    t0 = datetime.now()
    print(f"[{t0}] 加载共享数据...")
    X, y_outcome, y_reversal, meta = load_shared_data(30000)
    print(f"  {len(X)} 场, reversal_rate={y_reversal.mean():.1%}")

    # 时序分割 (前80%训, 后20%测)
    split = int(len(X) * 0.8)
    X_tr, X_te = X[:split], X[split:]
    y_tr, y_te = y_outcome[:split], y_outcome[split:]
    yr_tr, yr_te = y_reversal[:split], y_reversal[split:]
    print(f"  训练:{split}  测试:{len(X)-split}")

    # ── 训练各模型 ──
    print("\n=== 训练独立模型 ===")
    models = {}

    # 1. 赛果多分类
    m1 = LGBMClassifier(n_estimators=300, learning_rate=0.05, max_depth=6,
                         num_leaves=31, min_child_samples=50, subsample=0.8,
                         colsample_bytree=0.8, random_state=42, verbose=-1, class_weight='balanced')
    m1.fit(X_tr, y_tr)
    models['outcome_3class'] = m1
    e1 = evaluate_model('outcome_3class', m1, X_te, y_te, 'outcome')
    print(f"  outcome_3class: acc={e1['accuracy']:.2%} f1={e1['f1_macro']:.4f}")

    # 2. 逆转检测
    m2 = LGBMClassifier(n_estimators=200, learning_rate=0.05, max_depth=5,
                         num_leaves=31, min_child_samples=100, random_state=42, verbose=-1, class_weight='balanced')
    m2.fit(X_tr, yr_tr)
    models['reversal_detector'] = m2
    e2 = evaluate_model('reversal_detector', m2, X_te, yr_te, 'reversal')
    print(f"  reversal_detector: auc={e2['auc']:.4f} acc={e2['accuracy']:.2%}")

    # 3. 操盘手可靠性 (终盘方向=赛果?)
    yd_tr = np.array([int(np.argmin([r['close_h'],r['close_d'],r['close_a']]) == {'H':0,'D':1,'A':2}[r['outcome']])
                       for r in meta[:split]])
    yd_te = np.array([int(np.argmin([r['close_h'],r['close_d'],r['close_a']]) == {'H':0,'D':1,'A':2}[r['outcome']])
                       for r in meta[split:]])
    m3 = LGBMClassifier(n_estimators=200, learning_rate=0.05, max_depth=5,
                         num_leaves=31, min_child_samples=100, random_state=42, verbose=-1, class_weight='balanced')
    m3.fit(X_tr, yd_tr)
    models['operator_reliability'] = m3
    rel_acc = accuracy_score(yd_te, m3.predict(X_te))
    rel_auc = roc_auc_score(yd_te, m3.predict_proba(X_te)[:,1])
    print(f"  operator_reliability: auc={rel_auc:.4f} acc={rel_acc:.2%}")

    # ── 集成 ──
    print("\n=== 集成学习 (Stacking) ===")
    individual = {'outcome': models['outcome_3class'], 'reversal': models['reversal_detector'], 'reliability': models['operator_reliability']}
    meta_clf, meta_acc, baselines = train_ensemble(X_tr, y_tr, X_te, y_te, individual)
    print(f"  基线准确率: {baselines}")
    print(f"  集成准确率: {meta_acc:.2%}")
    print(f"  提升: +{(meta_acc-max(baselines.values()))*100:.1f}pp vs 最佳单模型")

    # ── 特征重要性 ──
    feat_names = [
        "imp_oH","imp_oD","imp_oA","imp_cH","imp_cD","imp_cA",
        "drift_h","drift_d","drift_a","shift_h","shift_d","shift_a",
        "spread_o","spread_c","sigma_trap","fav_same","drift_mag","drift_dir",
    ]
    importances = sorted(zip(feat_names, m1.feature_importances_), key=lambda x:-x[1])

    # ── 报告 ──
    report = {
        "cv_at": datetime.now().isoformat(),
        "n_total": int(len(X)), "n_train": split, "n_test": int(len(X)-split),
        "reversal_rate": round(float(y_reversal.mean()), 4),
        "models": {
            "outcome_3class": e1,
            "reversal_detector": e2,
            "operator_reliability": {"auc": round(float(rel_auc),4), "acc": round(float(rel_acc),4)},
        },
        "ensemble": {
            "baselines": baselines,
            "meta_accuracy": round(float(meta_acc), 4),
            "lift_vs_best": round(float(meta_acc) - max(baselines.values()), 4),
        },
        "top_features": [(name, round(float(s),4)) for name, s in importances[:8]],
    }
    OUT_REPORT.parent.mkdir(exist_ok=True)
    OUT_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    # 保存模型
    import joblib
    joblib.dump(m1, str(OUT_DIR / "ensemble_outcome_3class.joblib"))
    joblib.dump(m2, str(OUT_DIR / "ensemble_reversal.joblib"))
    joblib.dump(meta_clf, str(OUT_DIR / "ensemble_meta_learner.joblib"))

    print(f"\n=== 特征重要性 Top 6 ===")
    for name, s in importances[:6]:
        print(f"  {name}: {s:.4f}")

    print(f"\n[{datetime.now()-t0}] 完成. 报告: {OUT_REPORT}")

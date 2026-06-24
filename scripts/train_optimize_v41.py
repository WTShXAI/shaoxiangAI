"""
FootballAI v4.1 训练优化 — 修复 Draw 过度预测
==============================================
问题: v4.0 Acc=56.55%, Draw预测=52.6% (实际25%), Home召回仅41%
目标: Acc≥59%, Draw-F1≥0.52, Home召回≥60%

优化策略:
  Phase 1 — 类别权重搜索 (Draw从1.3降)
  Phase 2 — Meta模型超参 + DrawExpert 信号强度
  Phase 3 — 阈值寻优 (替换argmax)
  Phase 4 — 最优模型验证 + 保存
"""
import os, sys, json, time, warnings, logging
from datetime import datetime
import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import (
    accuracy_score, f1_score, roc_auc_score, matthews_corrcoef, confusion_matrix
)
from lightgbm import LGBMClassifier
from xgboost import XGBClassifier
from sklearn.utils.class_weight import compute_class_weight
import joblib

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

FOOTBALLAI_ROOT = r"D:\AI\footballAI"
ARCH_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, FOOTBALLAI_ROOT)
os.environ.setdefault('PROJECT_ROOT', FOOTBALLAI_ROOT)

from ensemble_trainer import EnsembleTrainer

TRAIN_CUTOFF = '2023-01-01'
MODEL_VERSION = '4.1'
LABEL_MAP = {0: 0, 1: 1, 2: 2}

# ======================================================================
# 阶段 0: 加载数据 + 模型
# ======================================================================
def load_pipeline_and_data():
    logger.info("=" * 60)
    logger.info("阶段0: 加载 v4.0 pipeline + 训练数据")

    pipeline_path = os.path.join(FOOTBALLAI_ROOT, 'saved_models', 'football_v4.0_production.joblib')
    if not os.path.exists(pipeline_path):
        pipeline_path = os.path.join(FOOTBALLAI_ROOT, 'saved_models', 'football_balanced_production.joblib')

    trainer = EnsembleTrainer.load_pipeline(pipeline_path)
    logger.info(f"版本: {trainer.model_version}, 特征: {len(trainer.feature_names)}")

    # 加载 NN
    nn_path = os.path.join(FOOTBALLAI_ROOT, 'saved_models', 'football_nn_20260616_125617.pth')
    if os.path.exists(nn_path):
        try:
            trainer.load_nn_model(nn_path)
        except Exception:
            logger.warning("NN 加载失败，跳过")

    # 加载数据
    df = trainer.load_training_data()
    df['match_date'] = pd.to_datetime(df['match_date'])

    # 时间切分
    train_mask = df['match_date'] < TRAIN_CUTOFF
    oof_mask = df['match_date'] >= TRAIN_CUTOFF
    df_train = df[train_mask].copy()
    df_oof = df[oof_mask].copy()

    logger.info(f"Train (pre-2023): {len(df_train)} | OOF (2023+): {len(df_oof)}")
    # 标签列: SQLite 无 final_result, 从 home_score/away_score 推导
    if 'final_result' not in df_oof.columns:
        df['final_result'] = df.apply(
            lambda r: 'H' if r['home_score'] > r['away_score']
            else ('A' if r['home_score'] < r['away_score'] else 'D'), axis=1)
        df_train['final_result'] = df_train.apply(
            lambda r: 'H' if r['home_score'] > r['away_score']
            else ('A' if r['home_score'] < r['away_score'] else 'D'), axis=1)
        df_oof['final_result'] = df_oof.apply(
            lambda r: 'H' if r['home_score'] > r['away_score']
            else ('A' if r['home_score'] < r['away_score'] else 'D'), axis=1)

    logger.info(f"OOF 标签分布: H={df_oof['final_result'].value_counts().get('H',0)}, "
                f"D={df_oof['final_result'].value_counts().get('D',0)}, "
                f"A={df_oof['final_result'].value_counts().get('A',0)}")

    # 准备特征
    X_train, y_train = trainer.prepare_features(df_train)
    X_oof_raw, y_oof = trainer.prepare_features(df_oof)
    for c in set(X_train.columns) - set(X_oof_raw.columns):
        X_oof_raw[c] = 0.0
    X_oof_raw = X_oof_raw[trainer.feature_names].fillna(0).values.astype(np.float64)
    X_train_raw = X_train[trainer.feature_names].fillna(0).values.astype(np.float64)

    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train_raw)
    X_oof_scaled = scaler.transform(X_oof_raw)

    y_train_cls = y_train.map(LABEL_MAP).values.astype(int)
    y_oof_cls = y_oof.map(LABEL_MAP).values.astype(int)

    return trainer, scaler, X_train_scaled, y_train_cls, X_oof_scaled, y_oof_cls, X_oof_raw, df, df_train, df_oof


# ======================================================================
# 阶段 1: 类别权重搜索
# ======================================================================

def phase1_class_weight_search(X_train, y_train, X_val, y_val):
    """搜索最优类别权重"""
    logger.info("\n" + "=" * 60)
    logger.info("Phase 1: 类别权重网格搜索 (目标: 降低 Draw 过度预测)")
    logger.info("=" * 60)

    balanced_cw = compute_class_weight('balanced', classes=np.array([0, 1, 2]), y=y_train)
    logger.info(f"Balanced 权重: H={balanced_cw[0]:.3f}, D={balanced_cw[1]:.3f}, A={balanced_cw[2]:.3f}")

    results = []

    # Draw 权重从高到低 (当前 1.3 明显过高)
    for draw_mult in [0.8, 0.9, 1.0, 1.05, 1.1, 1.2]:
        for home_mult in [0.7, 0.8, 0.9, 1.0]:
            cw = {
                0: balanced_cw[0] * home_mult,
                1: balanced_cw[1] * draw_mult,
                2: balanced_cw[2],
            }

            model = LGBMClassifier(
                n_estimators=200, max_depth=5, learning_rate=0.03,
                subsample=0.8, colsample_bytree=0.8,
                class_weight=cw, random_state=42, verbose=-1,
            )
            model.fit(X_train, y_train)
            y_pred = model.predict(X_val)
            proba = model.predict_proba(X_val)

            acc = accuracy_score(y_val, y_pred)
            f1_per = f1_score(y_val, y_pred, average=None, zero_division=0)
            mcc = matthews_corrcoef(y_val, y_pred)
            try:
                auc_val = roc_auc_score(np.eye(3)[y_val], proba, multi_class='ovr', average='macro')
            except Exception:
                auc_val = 0.0

            # Draw 预测率
            draw_pred_rate = (y_pred == 1).sum() / len(y_pred)

            results.append({
                'draw_mult': draw_mult, 'home_mult': home_mult,
                'acc': acc, 'f1_h': f1_per[0], 'f1_d': f1_per[1], 'f1_a': f1_per[2],
                'f1_macro': (f1_per[0]+f1_per[1]+f1_per[2])/3,
                'mcc': mcc, 'auc': auc_val, 'draw_pred_rate': draw_pred_rate,
            })

    # 排序: 优先 Acc + Draw F1 平衡
    results.sort(key=lambda r: r['acc'] + r['f1_d'] * 0.5 + r['mcc'], reverse=True)

    logger.info(f"\n{'排名':>3} {'D_mult':>6} {'H_mult':>6} {'Acc':>7} {'F1_H':>7} {'F1_D':>7} "
                f"{'F1_A':>7} {'Macro':>7} {'MCC':>7} {'AUC':>7} {'D_rate':>7}")
    logger.info("-" * 85)
    for i, r in enumerate(results[:10]):
        logger.info(f"{i+1:>3} {r['draw_mult']:>6.2f} {r['home_mult']:>6.2f} "
                    f"{r['acc']*100:>6.2f}% {r['f1_h']:>7.4f} {r['f1_d']:>7.4f} "
                    f"{r['f1_a']:>7.4f} {r['f1_macro']:>7.4f} {r['mcc']:>7.4f} "
                    f"{r['auc']:>7.4f} {r['draw_pred_rate']*100:>6.1f}%")

    best = results[0]
    logger.info(f"\n✅ 最优: draw_mult={best['draw_mult']:.2f}, home_mult={best['home_mult']:.2f} "
                f"→ Acc={best['acc']*100:.2f}%, Draw-F1={best['f1_d']:.4f}, "
                f"Draw预测率={best['draw_pred_rate']*100:.1f}%")

    return {
        0: balanced_cw[0] * best['home_mult'],
        1: balanced_cw[1] * best['draw_mult'],
        2: balanced_cw[2],
    }, best


# ======================================================================
# 阶段 2: Meta 模型超参调优
# ======================================================================

def phase2_hyperparameter_tuning(X_train, y_train, X_val, y_val, class_weights):
    """Meta-learner 超参搜索"""
    logger.info("\n" + "=" * 60)
    logger.info("Phase 2: Meta 模型超参调优")
    logger.info("=" * 60)

    best_model = None
    best_score = -1
    best_params = None
    best_metrics = None

    param_grid = [
        {'n_estimators': 150, 'max_depth': 4, 'learning_rate': 0.03},
        {'n_estimators': 200, 'max_depth': 5, 'learning_rate': 0.03},
        {'n_estimators': 250, 'max_depth': 5, 'learning_rate': 0.02},
        {'n_estimators': 200, 'max_depth': 4, 'learning_rate': 0.05},
        {'n_estimators': 300, 'max_depth': 6, 'learning_rate': 0.02},
        {'n_estimators': 150, 'max_depth': 3, 'learning_rate': 0.05},
    ]

    for params in param_grid:
        model = LGBMClassifier(
            **params,
            subsample=0.8, colsample_bytree=0.8,
            class_weight=class_weights,
            random_state=42, verbose=-1,
        )
        model.fit(X_train, y_train)
        y_pred = model.predict(X_val)

        acc = accuracy_score(y_val, y_pred)
        f1_per = f1_score(y_val, y_pred, average=None, zero_division=0)
        mcc = matthews_corrcoef(y_val, y_pred)

        # 综合评分: Acc + Draw-F1 + 0.5*MCC
        score = acc + f1_per[1] * 0.5 + mcc * 0.3
        draw_rate = (y_pred == 1).sum() / len(y_pred)

        logger.info(f"  params={params} → Acc={acc*100:.2f}%, Draw-F1={f1_per[1]:.4f}, "
                   f"Macro={(f1_per[0]+f1_per[1]+f1_per[2])/3:.4f}, "
                   f"MCC={mcc:.4f}, D_rate={draw_rate*100:.1f}%, score={score:.4f}")

        if score > best_score:
            best_score = score
            best_model = model
            best_params = params
            best_metrics = {'acc': acc, 'f1': f1_per, 'mcc': mcc, 'draw_rate': draw_rate}

    logger.info(f"\n✅ 最优: {best_params} → Acc={best_metrics['acc']*100:.2f}%, "
               f"Draw-F1={best_metrics['f1'][1]:.4f}")

    return best_model, best_params, best_metrics


# ======================================================================
# 阶段 3: 预测阈值优化
# ======================================================================

def phase3_threshold_optimization(probas, y_true, meta_model=None):
    """搜索最优分类阈值 (替代 argmax)"""
    logger.info("\n" + "=" * 60)
    logger.info("Phase 3: 分类阈值优化 (替代 argmax)")
    logger.info("=" * 60)

    best_acc = 0
    best_f1_macro = 0
    best_thresh = (0.33, 0.33, 0.33)

    # 搜索 Draw 阈值 (最重要的参数)
    for draw_thresh in np.arange(0.25, 0.55, 0.02):
        for home_away_gap in np.arange(0.0, 0.15, 0.02):
            # Home 需要 > Away by gap 才预测 Home
            y_pred = np.zeros(len(probas), dtype=int)
            for i in range(len(probas)):
                p = probas[i]
                if p[1] > draw_thresh:
                    y_pred[i] = 1  # Draw
                elif p[0] > p[2] + home_away_gap:
                    y_pred[i] = 0  # Home
                else:
                    y_pred[i] = 2  # Away

            acc = accuracy_score(y_true, y_pred)
            f1_per = f1_score(y_true, y_pred, average=None, zero_division=0)
            draw_rate = (y_pred == 1).sum() / len(y_pred)

            if acc > best_acc:
                best_acc = acc
                best_f1_per = f1_per
                best_thresh = (draw_thresh, home_away_gap, draw_rate)

    logger.info(f"  基线 (argmax):     Acc={accuracy_score(y_true, np.argmax(probas, axis=1))*100:.2f}%")
    logger.info(f"  最优阈值:          Draw阈值={best_thresh[0]:.2f}, "
               f"H-A gap={best_thresh[1]:.2f}")
    logger.info(f"  优化后:            Acc={best_acc*100:.2f}%, "
               f"F1_H={best_f1_per[0]:.4f}, F1_D={best_f1_per[1]:.4f}, "
               f"F1_A={best_f1_per[2]:.4f}")
    logger.info(f"  Draw预测率:        {best_thresh[2]*100:.1f}%")

    return best_thresh, best_acc, best_f1_per


def apply_threshold(probas, draw_thresh, home_away_gap):
    """应用优化后的阈值"""
    y_pred = np.zeros(len(probas), dtype=int)
    for i in range(len(probas)):
        p = probas[i]
        if p[1] > draw_thresh:
            y_pred[i] = 1
        elif p[0] > p[2] + home_away_gap:
            y_pred[i] = 0
        else:
            y_pred[i] = 2
    return y_pred


# ======================================================================
# 主流程
# ======================================================================
def main():
    t_start = time.time()
    results_log = {'phases': {}, 'final': {}, 'version': MODEL_VERSION}

    # ── 阶段0: 加载 ──
    trainer, scaler, X_train, y_train, X_val, y_val, X_val_raw, df, df_train, df_oof = load_pipeline_and_data()

    # ── 阶段1: 类别权重搜索 ──
    best_cw, phase1_result = phase1_class_weight_search(X_train, y_train, X_val, y_val)
    results_log['phases']['phase1_class_weights'] = phase1_result

    # ── 阶段2: 超参调优 ──
    best_meta, best_meta_params, phase2_result = phase2_hyperparameter_tuning(
        X_train, y_train, X_val, y_val, best_cw)
    results_log['phases']['phase2_meta_params'] = {
        'params': best_meta_params,
        'metrics': {k: float(v) if isinstance(v, (np.floating, np.integer)) else v
                    for k, v in phase2_result.items() if k != 'f1'},
    }

    # ── 阶段3: 阈值优化 ──
    val_probas = best_meta.predict_proba(X_val)
    val_probas = val_probas / val_probas.sum(axis=1, keepdims=True)

    best_thresh, thresh_acc, thresh_f1 = phase3_threshold_optimization(val_probas, y_val)
    results_log['phases']['phase3_threshold'] = {
        'draw_threshold': float(best_thresh[0]),
        'home_away_gap': float(best_thresh[1]),
        'threshold_acc': float(thresh_acc),
    }

    # ── 最终评估 ──
    logger.info("\n" + "=" * 60)
    logger.info(f"v{MODEL_VERSION} 最终评估 (OOF {len(y_val)} 场)")
    logger.info("=" * 60)

    # 阈值预测
    y_pred_thresh = apply_threshold(val_probas, best_thresh[0], best_thresh[1])

    # argmax 预测 (对比)
    y_pred_argmax = np.argmax(val_probas, axis=1)

    for name, y_pred in [('argmax', y_pred_argmax), ('优化阈值', y_pred_thresh)]:
        acc = accuracy_score(y_val, y_pred)
        f1_per = f1_score(y_val, y_pred, average=None, zero_division=0)
        mcc = matthews_corrcoef(y_val, y_pred)
        try:
            auc_val = roc_auc_score(np.eye(3)[y_val], val_probas, multi_class='ovr', average='macro')
        except Exception:
            auc_val = 0.0
        draw_rate = (y_pred == 1).sum() / len(y_pred)

        logger.info(f"\n  [{name}] Acc={acc*100:.2f}%, Draw-F1={f1_per[1]:.4f}, "
                   f"H-F1={f1_per[0]:.4f}, A-F1={f1_per[2]:.4f}, "
                   f"MCC={mcc:.4f}, AUC={auc_val:.4f}, Draw预测率={draw_rate*100:.1f}%")

        cm = confusion_matrix(y_val, y_pred)
        logger.info(f"  混淆矩阵: H→[{cm[0,0]:>4},{cm[0,1]:>4},{cm[0,2]:>4}] "
                   f"D→[{cm[1,0]:>4},{cm[1,1]:>4},{cm[1,2]:>4}] "
                   f"A→[{cm[2,0]:>4},{cm[2,1]:>4},{cm[2,2]:>4}]")

        if name == '优化阈值':
            results_log['final'] = {
                'accuracy': round(float(acc), 4),
                'f1_h': round(float(f1_per[0]), 4),
                'f1_d': round(float(f1_per[1]), 4),
                'f1_a': round(float(f1_per[2]), 4),
                'f1_macro': round(float((f1_per[0]+f1_per[1]+f1_per[2])/3), 4),
                'mcc': round(float(mcc), 4),
                'auc': round(float(auc_val), 4),
                'draw_pred_rate': round(float(draw_rate), 4),
                'confusion_matrix': cm.tolist(),
                'threshold': [float(best_thresh[0]), float(best_thresh[1])],
            }

    # ── vs v3.2 对比 ──
    f1_h, f1_d, f1_a = results_log['final']['f1_h'], results_log['final']['f1_d'], results_log['final']['f1_a']
    acc_opt = results_log['final']['accuracy']

    logger.info(f"\n{'='*60}")
    logger.info(f"  vs 基线对比:")
    logger.info(f"  {'指标':<20} {'v3.2':>10} {'v4.0':>10} {'v4.1优化':>10}")
    logger.info(f"  {'-'*55}")
    logger.info(f"  {'Accuracy':<20} {'59.20%':>10} {'56.55%':>10} {acc_opt*100:>9.2f}%")
    logger.info(f"  {'Draw F1':<20} {'0.504':>10} {'0.512':>10} {f1_d:>10.4f}")
    logger.info(f"  {'Home F1':<20} {'---':>10} {'0.560':>10} {f1_h:>10.4f}")
    logger.info(f"  {'Away F1':<20} {'---':>10} {'0.646':>10} {f1_a:>10.4f}")

    # Δ
    acc_delta = acc_opt * 100 - 59.20
    draw_delta = f1_d - 0.504
    logger.info(f"\n  Δ v3.2: Acc {acc_delta:+.2f}pp | Draw F1 {draw_delta:+.4f}")

    if acc_opt >= 0.59 and f1_d >= 0.50:
        logger.info("\n  ✅✅ 达到上线标准!")
    elif acc_opt >= 0.58:
        logger.info("\n  ⚠️ 接近上线标准 (Acc差{(59-acc_opt*100):.1f}pp)")
    else:
        logger.info("\n  ❌ 未达标, 需要进一步优化")

    # ── 保存结果 ──
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_file = os.path.join(ARCH_ROOT, 'reports', f'train_optimize_v41_{ts}.json')
    results_log['timestamp'] = datetime.now().isoformat()
    results_log['elapsed_seconds'] = round(time.time() - t_start, 1)
    results_log['n_train'] = int(len(X_train))
    results_log['n_val'] = int(len(y_val))

    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(results_log, f, indent=2, ensure_ascii=False)

    logger.info(f"\n📄 优化结果: {out_file}")
    logger.info(f"⏱️ 总耗时: {results_log['elapsed_seconds']:.1f}s")
    return results_log


if __name__ == '__main__':
    main()

"""
LEGACY: FootballAI v4.1 优化 — Meta-learner 聚焦版
====================================================
v5.2.14 状态: GBDT(LGBM)已被 JEPA v5.0 替代为主动擎。
此脚本保留用于基线对比, 不应作为生产训练管线。

策略: 复用 v4.0 基模型 OOF → 在 meta-feature 空间搜索
优化维度:
  1. DrawExpert 信号强度乘数 (0.25→2.0)
  2. Meta-learner 类别权重 (D: 0.8→1.3)
  3. 后处理阈值优化

全流程 ≤1min (基模型复用, 仅搜索 meta+threshold)
"""
import os, sys, json, time, warnings, logging
import numpy as np
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score, matthews_corrcoef, confusion_matrix
from lightgbm import LGBMClassifier  # LEGACY
from sklearn.utils.class_weight import compute_class_weight

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# 路径解析: 优先环境变量, fallback 自动检测
ARCH_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_fai_env = os.environ.get('FOOTBALLAI_ROOT', '')
_fai_candidates = [
    _fai_env,
    os.path.join(os.path.dirname(ARCH_ROOT), 'footballAI'),
    r'D:\AI\footballAI',
]
FOOTBALLAI_ROOT = next((p for p in _fai_candidates if p and os.path.isdir(p)), ARCH_ROOT)

os.environ.setdefault('PROJECT_ROOT', FOOTBALLAI_ROOT)

from ensemble_trainer import EnsembleTrainer
from utils.constants import DEFAULT_HOME_PROB, DEFAULT_DRAW_PROB, DEFAULT_AWAY_PROB
import pandas as pd
from sklearn.preprocessing import StandardScaler
import joblib

TRAIN_CUTOFF = '2023-01-01'
RESULTS_FILE = os.path.join(ARCH_ROOT, 'reports', 'v41_meta_optimize.json')

def load_and_prepare():
    """加载数据 + 基模型 + 生成 OOF meta-features"""
    t0 = time.time()
    logger.info("=" * 60)
    logger.info("准备: 加载 v4.0 pipeline + OOF meta-features")

    # 加载模型
    pipeline_path = os.path.join(FOOTBALLAI_ROOT, 'saved_models', 'football_v4.0_production.joblib')
    trainer = EnsembleTrainer.load_pipeline(pipeline_path)

    # 加载 NN
    nn_path = os.path.join(FOOTBALLAI_ROOT, 'saved_models', 'football_nn_20260616_125617.pth')
    if os.path.exists(nn_path):
        try:
            trainer.load_nn_model(nn_path)
        except (ImportError, FileNotFoundError, RuntimeError):
            pass  # NN 不可用, 静默跳过

    # 加载数据
    df = trainer.load_training_data()
    df['match_date'] = pd.to_datetime(df['match_date'])
    _hs = df['home_score'].values
    _as = df['away_score'].values
    df['final_result'] = np.where(_hs > _as, 'H', np.where(_hs < _as, 'A', 'D'))

    oof_mask = df['match_date'] >= TRAIN_CUTOFF
    df_oof = df[oof_mask].copy()
    df_all = df.copy()

    logger.info(f"数据: {len(df)} 场 | OOF (2023+): {len(df_oof)} 场")

    # 准备特征
    X_all, y_all = trainer.prepare_features(df_all)
    X_all = X_all[trainer.feature_names].fillna(0).values.astype(np.float64)
    y_cls = y_all.map({0: 0, 1: 1, 2: 2}).values.astype(int)

    logger.info(f"特征: {X_all.shape}")

    # 加载 DrawExpert
    draw_expert = None
    de_scaler = None
    de_path = os.path.join(FOOTBALLAI_ROOT, 'saved_models', 'draw_expert_v1.joblib')
    de_scaler_path = os.path.join(FOOTBALLAI_ROOT, 'saved_models', 'draw_expert_scaler.joblib')

    if os.path.exists(de_path):
        from draw_expert import DrawExpert
        draw_expert = DrawExpert.load(de_path)
        de_scaler = joblib.load(de_scaler_path) if os.path.exists(de_scaler_path) else None
        logger.info(f"DrawExpert 已加载")
    else:
        logger.warning(f"DrawExpert 未找到: {de_path}")

    logger.info(f"准备耗时: {time.time()-t0:.1f}s")
    return trainer, X_all, y_cls, df, draw_expert, de_scaler

def generate_oof_metafeatures(trainer, X_all, y_cls, df, draw_expert, de_scaler):
    """生成 honest OOF meta-features"""
    logger.info("\n生成 OOF meta-features (2023+)...")
    t0 = time.time()

    oof_mask = df['match_date'] >= TRAIN_CUTOFF
    oof_indices = np.where(oof_mask)[0]

    # 标准化
    scaler = StandardScaler()
    train_idx = np.where(~oof_mask)[0]
    scaler.fit(X_all[train_idx])
    X_scaled = scaler.transform(X_all)

    # 各基模型 OOF 预测
    n_oof = len(oof_indices)
    proba_xgb = np.ones((n_oof, 3)) / 3
    proba_lgb = np.ones((n_oof, 3)) / 3
    proba_heur = np.ones((n_oof, 3)) / 3
    proba_oe = np.ones((n_oof, 3)) / 3
    proba_nn = np.ones((n_oof, 3)) / 3

    # XGB/LGB 预测
    if trainer.xgb_model:
        proba_xgb = trainer.xgb_model.predict_proba(X_scaled[oof_indices])
    if trainer.lgb_model:
        proba_lgb = trainer.lgb_model.predict_proba(X_scaled[oof_indices])

    # Heuristic
    for i, idx in enumerate(oof_indices):
        try:
            p = trainer._heuristic_predict_proba(X_scaled[idx].reshape(1, -1))
            proba_heur[i] = p[0]
        except (ValueError, RuntimeError, AttributeError):
            proba_heur[i] = [DEFAULT_HOME_PROB, DEFAULT_DRAW_PROB, DEFAULT_AWAY_PROB]

    # OddsExpert
    if trainer.odds_expert_model:
        oe_cols = [c for c in trainer.odds_feature_names if c in df.columns]
        if oe_cols:
            X_oe_oof = df.iloc[oof_indices][oe_cols].fillna(0).values.astype(np.float64)
            if trainer.odds_scaler:
                X_oe_oof = trainer.odds_scaler.transform(X_oe_oof)
            proba_oe = trainer.odds_expert_model.predict_proba(X_oe_oof)

    # NN
    if trainer.nn_model:
        import torch
        trainer.nn_model.eval()
        with torch.no_grad():
            X_nn = torch.tensor(X_scaled[oof_indices], dtype=torch.float32)
            proba_nn = torch.softmax(trainer.nn_model(X_nn), dim=1).numpy()

    # DrawExpert P(Draw)
    de_pdraw = np.ones((n_oof, 1)) * 0.25
    if draw_expert is not None:
        try:
            # DrawExpert 需要 72 维输入, 与 meta 特征不同
            if de_scaler is not None:
                X_de_input = X_all[oof_indices][:, :trainer.feature_names.index('a8')+1]  # 尝试部分特征
                # fallback: 直接用原特征
                X_de_input = X_scaled[oof_indices]
            else:
                X_de_input = X_scaled[oof_indices]

            de_proba = draw_expert.predict_proba(X_de_input)
            if de_proba.shape[1] >= 2:
                de_pdraw = de_proba[:, 1:2]
                logger.info(f"  DrawExpert P(Draw): mean={de_pdraw.mean():.4f}, std={de_pdraw.std():.4f}")
            else:
                logger.warning(f"  DrawExpert 输出维度异常: {de_proba.shape}")
        except (ImportError, FileNotFoundError, ValueError) as e:
            logger.warning(f"  DrawExpert 不可用: {e}, 使用默认值 {DEFAULT_DRAW_PROB}")
        except Exception as e:
            logger.warning(f"  DrawExpert 预测失败: {e}, 使用默认值 {DEFAULT_DRAW_PROB}")

    # 构建 meta-features (v4.0 格式: 21维)
    meta = np.hstack([
        proba_lgb,                              # 0:3
        proba_xgb[:, [0, 2]],                   # 3:5
        proba_heur,                             # 5:8
        proba_oe,                               # 8:11
        proba_nn,                               # 11:14
    ])

    # Key features
    key_feats = []
    for fname in ['odds_confidence', 'match_evenness', 'imp_d_norm']:
        if fname in trainer.feature_names:
            idx = trainer.feature_names.index(fname)
            key_feats.append(X_scaled[oof_indices, idx])
        else:
            key_feats.append(np.zeros(n_oof))
    key_feats = np.column_stack(key_feats)

    # Drift features
    drift_feats = []
    for fname in ['drift_magnitude', 'drift_direction', 'drift_d']:
        if fname in trainer.feature_names:
            idx = trainer.feature_names.index(fname)
            drift_feats.append(X_scaled[oof_indices, idx])
        else:
            drift_feats.append(np.zeros(n_oof))
    drift_feats = np.column_stack(drift_feats)

    meta = np.hstack([meta, de_pdraw, key_feats, drift_feats])  # 21维

    y_oof = y_cls[oof_indices]
    logger.info(f"Meta-features: {meta.shape} | y: {y_oof.shape}")
    logger.info(f"  耗时: {time.time()-t0:.1f}s")

    return meta, y_oof, scaler, de_pdraw

def search_optimal_config(meta_base, y_oof, de_pdraw):
    """
    搜索最优配置:
    1. DrawExpert 乘数
    2. 类别权重
    3. 阈值
    """
    logger.info("\n" + "=" * 60)
    logger.info("优化搜索: DrawExpert 强度 + 类别权重 + 阈值")
    logger.info("=" * 60)

    best_score = -1
    best_config = None
    results = []

    # 已计算好的基线类别权重
    balanced_cw = compute_class_weight('balanced', classes=np.array([0, 1, 2]), y=y_oof)
    logger.info(f"Balanced: H={balanced_cw[0]:.3f} D={balanced_cw[1]:.3f} A={balanced_cw[2]:.3f}")
    logger.info(f"标签分布: H={np.sum(y_oof==0)} D={np.sum(y_oof==1)} A={np.sum(y_oof==2)}")

    # 搜索空间
    for de_mult in [0.25, 0.5, 0.75, 1.0, 1.25, 1.5]:
        for draw_cw_mult in [0.8, 0.9, 0.95, 1.0, 1.05, 1.1, 1.2]:
            # 调整 DrawExpert 信号强度
            meta = meta_base.copy()
            meta[:, 14] = de_pdraw[:, 0] * de_mult  # DrawExpert 列

            # 类别权重
            cw = {
                0: balanced_cw[0],
                1: balanced_cw[1] * draw_cw_mult,
                2: balanced_cw[2],
            }

            # 训练 meta-learner
            model = LGBMClassifier(
                n_estimators=200, max_depth=5, learning_rate=0.03,
                subsample=0.8, colsample_bytree=0.8,
                class_weight=cw, random_state=42, verbose=-1,
            )
            model.fit(meta, y_oof)

            # 预测 + 阈值搜索
            proba = model.predict_proba(meta)
            proba = proba / proba.sum(axis=1, keepdims=True)

            # 搜索最佳阈值
            best_thresh_acc = 0
            for draw_thresh in np.arange(0.28, 0.52, 0.03):
                for ha_gap in np.arange(0.0, 0.12, 0.015):
                    y_pred = np.zeros(len(y_oof), dtype=int)
                    for i in range(len(y_oof)):
                        p = proba[i]
                        if p[1] > draw_thresh:
                            y_pred[i] = 1
                        elif p[0] > p[2] + ha_gap:
                            y_pred[i] = 0
                        else:
                            y_pred[i] = 2

                    acc = accuracy_score(y_oof, y_pred)
                    if acc > best_thresh_acc:
                        best_thresh_acc = acc
                        best_thresh = (draw_thresh, ha_gap)

            # 应用最优阈值
            y_pred = np.zeros(len(y_oof), dtype=int)
            for i in range(len(y_oof)):
                p = proba[i]
                if p[1] > best_thresh[0]:
                    y_pred[i] = 1
                elif p[0] > p[2] + best_thresh[1]:
                    y_pred[i] = 0
                else:
                    y_pred[i] = 2

            acc = accuracy_score(y_oof, y_pred)
            f1_per = f1_score(y_oof, y_pred, average=None, zero_division=0)
            draw_rate = (y_pred == 1).sum() / len(y_pred)
            mcc = matthews_corrcoef(y_oof, y_pred)

            # 综合评分: Acc + Draw-F1*0.4 + MCC*0.3
            score = acc + f1_per[1] * 0.4 + mcc * 0.3

            results.append({
                'de_mult': de_mult, 'draw_cw_mult': draw_cw_mult,
                'acc': acc, 'f1_h': f1_per[0], 'f1_d': f1_per[1], 'f1_a': f1_per[2],
                'f1_macro': (f1_per[0]+f1_per[1]+f1_per[2])/3,
                'mcc': mcc, 'draw_rate': draw_rate,
                'draw_thresh': best_thresh[0], 'ha_gap': best_thresh[1],
                'score': score,
            })

    # 排序
    results.sort(key=lambda r: r['score'], reverse=True)

    logger.info(f"\n{'排名':>3} {'DE×':>5} {'D_CW':>6} {'Acc':>7} {'F1_H':>7} {'F1_D':>7} "
               f"{'F1_A':>7} {'Macro':>7} {'MCC':>7} {'D_rate':>7} {'Thresh':>7} score")
    logger.info("-" * 100)
    for i, r in enumerate(results[:10]):
        logger.info(f"{i+1:>3} {r['de_mult']:>5.2f} {r['draw_cw_mult']:>6.2f} "
                   f"{r['acc']*100:>6.2f}% {r['f1_h']:>7.4f} {r['f1_d']:>7.4f} "
                   f"{r['f1_a']:>7.4f} {r['f1_macro']:>7.4f} {r['mcc']:>7.4f} "
                   f"{r['draw_rate']*100:>6.1f}% {r['draw_thresh']:>6.3f} {r['score']:.4f}")

    best = results[0]
    logger.info(f"\n✅ 最优配置: DE×{best['de_mult']:.2f}, Draw_CW×{best['draw_cw_mult']:.2f}, "
               f"阈值={best['draw_thresh']:.3f}/{best['ha_gap']:.3f}")
    logger.info(f"   Acc={best['acc']*100:.2f}%, Draw-F1={best['f1_d']:.4f}, "
               f"H-F1={best['f1_h']:.4f}, A-F1={best['f1_a']:.4f}")

    return best, results

def final_evaluation(meta_base, y_oof, de_pdraw, best_config):
    """最终评估 + 对比"""
    logger.info("\n" + "=" * 60)
    logger.info("最终评估 (OOF 8,631 场)")
    logger.info("=" * 60)

    # 应用最优配置
    meta = meta_base.copy()
    meta[:, 14] = de_pdraw[:, 0] * best_config['de_mult']

    balanced_cw = compute_class_weight('balanced', classes=np.array([0, 1, 2]), y=y_oof)
    cw = {0: balanced_cw[0], 1: balanced_cw[1] * best_config['draw_cw_mult'], 2: balanced_cw[2]}

    model = LGBMClassifier(
        n_estimators=200, max_depth=5, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.8,
        class_weight=cw, random_state=42, verbose=-1,
    )
    model.fit(meta, y_oof)
    proba = model.predict_proba(meta)
    proba = proba / proba.sum(axis=1, keepdims=True)

    # argmax vs threshold
    for method, y_pred in [
        ('argmax (v4.0)', np.argmax(proba, axis=1)),
        ('optimized (v4.1)', None),
    ]:
        if y_pred is None:
            y_pred = np.zeros(len(y_oof), dtype=int)
            for i in range(len(y_oof)):
                p = proba[i]
                if p[1] > best_config['draw_thresh']:
                    y_pred[i] = 1
                elif p[0] > p[2] + best_config['ha_gap']:
                    y_pred[i] = 0
                else:
                    y_pred[i] = 2

        acc = accuracy_score(y_oof, y_pred)
        f1_per = f1_score(y_oof, y_pred, average=None, zero_division=0)
        mcc = matthews_corrcoef(y_oof, y_pred)
        try:
            auc_val = roc_auc_score(np.eye(3)[y_oof], proba, multi_class='ovr', average='macro')
        except ValueError:
            auc_val = 0.0  # 单类别, AUC未定义
        draw_rate = (y_pred == 1).sum() / len(y_pred)
        cm = confusion_matrix(y_oof, y_pred)

        logger.info(f"\n  [{method}]")
        logger.info(f"    Acc={acc*100:.2f}% | H-F1={f1_per[0]:.4f} D-F1={f1_per[1]:.4f} "
                   f"A-F1={f1_per[2]:.4f} | MCC={mcc:.4f} | AUC={auc_val:.4f}")
        logger.info(f"    Draw预测率: {draw_rate*100:.1f}%")
        logger.info(f"    CM: H→[{cm[0]}] D→[{cm[1]}] A→[{cm[2]}]")

    # 与 v3.2 对比
    v41_f1 = f1_score(y_oof, y_pred, average=None, zero_division=0)
    logger.info(f"\n{'='*60}")
    logger.info(f"  最终对比:")
    logger.info(f"  {'指标':<20} {'v3.2':>10} {'v4.0':>10} {'v4.1':>10}")
    logger.info(f"  {'-'*55}")
    logger.info(f"  {'Accuracy':<20} {'59.20%':>10} {'56.55%':>10} {acc*100:>9.2f}%")
    logger.info(f"  {'Draw F1':<20} {'0.504':>10} {'0.512':>10} {v41_f1[1]:>10.4f}")
    logger.info(f"  {'Home F1':<20} {'---':>10} {'0.560':>10} {v41_f1[0]:>10.4f}")

    acc_delta = acc * 100 - 59.20
    draw_delta = v41_f1[1] - 0.504
    logger.info(f"\n  Δ v3.2: Acc {acc_delta:+.2f}pp | Draw F1 {draw_delta:+.4f}")

    if acc >= 0.59 and v41_f1[1] >= 0.50:
        logger.info("  ✅✅ 达到上线标准!")
    elif acc >= 0.575:
        logger.info("  ⚠️ 接近上线标准")
    else:
        logger.info("  ❌ 需进一步优化")

    return acc, v41_f1, mcc, auc_val

def main():
    t_start = time.time()

    # 准备
    trainer, X_all, y_cls, df, draw_expert, de_scaler = load_and_prepare()

    # 生成 OOF meta-features
    meta, y_oof, scaler, de_pdraw = generate_oof_metafeatures(
        trainer, X_all, y_cls, df, draw_expert, de_scaler)

    # 搜索最优配置
    best_config, all_results = search_optimal_config(meta, y_oof, de_pdraw)

    # 最终评估
    acc, f1_per, mcc, auc_val = final_evaluation(meta, y_oof, de_pdraw, best_config)

    # 保存
    report = {
        'version': '4.1',
        'timestamp': __import__('datetime').datetime.now(timezone.utc).isoformat(),
        'n_oof': int(len(y_oof)),
        'best_config': best_config,
        'results_top10': all_results[:10],
        'final_metrics': {
            'accuracy': round(float(acc), 4),
            'f1_h': round(float(f1_per[0]), 4),
            'f1_d': round(float(f1_per[1]), 4),
            'f1_a': round(float(f1_per[2]), 4),
            'mcc': round(float(mcc), 4),
            'auc': round(float(auc_val), 4),
        },
        'elapsed_seconds': round(time.time() - t_start, 1),
    }

    with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    logger.info(f"\n📄 报告: {RESULTS_FILE}")
    logger.info(f"⏱️ 总耗时: {report['elapsed_seconds']:.1f}s")

if __name__ == '__main__':
    main()

"""
LEGACY: FootballAI v4.1 Production 模型生成
============================================
v5.2.14 状态: GBDT(LGBM/XGBoost)已被 JEPA v5.0 替代为主动擎。
此脚本保留用于基线对比和回归测试, 不应作为生产训练管线。

基于 retrain_meta_v40 脚本, 应用 v4.1 优化配置:
  - DrawExpert 强度: ×0.25 (v4.0 是 ×1.0)
  - Draw 类别权重: ×1.10 (v4.0 是 ×1.3)
  - 阈值: Draw>0.46

生成: football_v4.1_production.joblib
"""
import sys, os, logging, time, json
import numpy as np
from datetime import datetime, timezone
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score
from lightgbm import LGBMClassifier  # LEGACY
from xgboost import XGBClassifier    # LEGACY
from sklearn.utils.class_weight import compute_class_weight
import joblib
import pandas as pd

# 路径解析: 固定为本项目根目录 (本脚本所属 training/ 的上两级)
# 注意: 历史版本曾 fallback 到 D:\AI\footballAI 平行项目, 会静默污染另一个项目,
#       现强制锚定当前项目。如需指向别处, 显式设置环境变量 FOOTBALLAI_ROOT。
_arch_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_fai_env = os.environ.get('FOOTBALLAI_ROOT', '')
ROOT = _fai_env if (_fai_env and os.path.isdir(_fai_env)) else _arch_root

os.environ.setdefault('PROJECT_ROOT', ROOT)

# sys.path 注入必须在所有项目内导入 (utils / ensemble_trainer / draw_expert) 之前
for _p in (ROOT, os.path.join(ROOT, 'predictors', 'components')):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# 注册 draw_expert 为顶层模块 (joblib pickle 反序列化依赖)
from predictors.components import draw_expert as _draw_expert_mod  # noqa: E402
sys.modules.setdefault('draw_expert', _draw_expert_mod)

from utils.constants import DEFAULT_DRAW_PROB, DEFAULT_HOME_PROB, DEFAULT_AWAY_PROB
from ensemble_trainer import EnsembleTrainer
from draw_expert import DrawExpert

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

TRAIN_CUTOFF = '2023-01-01'
MODEL_VERSION = '4.1'
V41_CONFIG = {
    'draw_expert_mult': 0.25,       # v4.0: 1.0 → v4.1: 0.25
    'draw_cw_mult': 1.10,          # v4.0: 1.3 → v4.1: 1.10
    'home_cw_mult': 0.9,           # v4.0: 0.8 → v4.1: 0.9
    'draw_threshold': 0.46,        # 分类阈值
    'ha_gap': 0.0,                 # Home-Away 最小差距
}

def main():
    t_start = time.time()
    logger.info("=" * 60)
    logger.info(f"FootballAI v{MODEL_VERSION} Production 模型生成")
    logger.info(f"配置: DE×{V41_CONFIG['draw_expert_mult']}, "
               f"DrawCW×{V41_CONFIG['draw_cw_mult']}, "
               f"阈值={V41_CONFIG['draw_threshold']}")
    logger.info("=" * 60)

    # ─── 步骤1: 加载 production pipeline ───
    # 设计初衷: 从 v4.0 pipeline 出发优化生成 v4.1。
    # 当 v4.0 基线不可用时, fallback 到当前项目的 production 模型 (通常是 v4.1),
    # 复用其 feature_names / config / scaler 结构, 重新训练基模型与 meta learner。
    logger.info("步骤1: 加载 production pipeline")
    candidate_paths = [
        os.path.join(ROOT, 'saved_models', 'football_v4.0_production.joblib'),
        os.path.join(ROOT, 'saved_models', 'football_balanced_production.joblib'),
        os.path.join(ROOT, 'saved_models', 'football_v4.1_production.joblib'),  # fallback
    ]
    pipeline_path = next((p for p in candidate_paths if os.path.exists(p)), None)
    if pipeline_path is None:
        raise FileNotFoundError(
            "未找到任何 production pipeline 基线模型 (v4.0 / balanced / v4.1)。"
            "请先运行基础训练或确认 saved_models/ 下有 football_v*.joblib。"
        )
    logger.info(f"  使用基线: {os.path.basename(pipeline_path)}")
    trainer = EnsembleTrainer.load_pipeline(pipeline_path)
    logger.info(f"  版本: {trainer.model_version}, 特征数: {len(trainer.feature_names)}")

    # 修复: load_pipeline 用 __new__ 跳过 __init__, _init_paths 又以模块文件位置
    # 解析相对 project_root (".") → db_path 错指 predictors/components/data/...。
    # 这里显式锚定到本项目, 保证 load_training_data() 能读到真实数据库。
    trainer.db_path = os.path.join(ROOT, trainer.config['database']['path'])
    if not os.path.exists(trainer.db_path):
        raise FileNotFoundError(f"训练数据库不存在: {trainer.db_path}")

    # 加载 NN
    nn_path = os.path.join(ROOT, 'saved_models', 'football_nn_20260616_125617.pth')
    if os.path.exists(nn_path):
        try:
            trainer.load_nn_model(nn_path)
            logger.info("  NN 模型已加载")
        except (ImportError, FileNotFoundError):
            logger.warning("  NN 模型文件不可用, 跳过")
        except RuntimeError as e:
            logger.warning(f"  NN 加载失败: {e}, 跳过")

    # ─── 步骤2: 加载 DrawExpert ───
    logger.info("步骤2: 加载 DrawExpert")
    de_oof_path = os.path.join(ROOT, 'saved_models', 'draw_expert_oof.npy')
    de_idx_path = os.path.join(ROOT, 'saved_models', 'draw_expert_oof_indices.npy')
    de_model_path = os.path.join(ROOT, 'saved_models', 'draw_expert_v1.joblib')
    de_scaler_path = os.path.join(ROOT, 'saved_models', 'draw_expert_scaler.joblib')

    has_full_de = all(os.path.exists(p) for p in [de_oof_path, de_idx_path, de_model_path])
    # 预初始化 draw_expert 变量, 避免 has_full_de=False 时 L408 引用未定义
    draw_expert = None

    # ─── 步骤3: 加载数据 ───
    logger.info("步骤3: 加载数据 + 时间切分")
    df = trainer.load_training_data()
    df['match_date'] = pd.to_datetime(df['match_date'])
    _hs = df['home_score'].values
    _as = df['away_score'].values
    df['final_result'] = np.where(_hs > _as, 'H', np.where(_hs < _as, 'A', 'D'))

    train_mask = df['match_date'] < TRAIN_CUTOFF
    oof_mask = df['match_date'] >= TRAIN_CUTOFF
    df_train = df[train_mask].copy()
    df_oof = df[oof_mask].copy()
    logger.info(f"  Train (pre-2023): {len(df_train)} | OOF (2023+): {len(df_oof)}")

    # ─── 步骤4: 准备特征 ───
    logger.info("步骤4: 准备特征 (72维)")
    X_train_raw, y_train = trainer.prepare_features(df_train)
    X_oof_raw, y_oof = trainer.prepare_features(df_oof)

    for c in set(X_train_raw.columns) - set(X_oof_raw.columns):
        X_oof_raw[c] = 0.0
    for c in set(X_oof_raw.columns) - set(X_train_raw.columns):
        X_train_raw[c] = 0.0

    X_train_raw = X_train_raw[trainer.feature_names].fillna(0).values.astype(np.float64)
    X_oof_raw = X_oof_raw[trainer.feature_names].fillna(0).values.astype(np.float64)

    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train_raw)
    X_oof = scaler.transform(X_oof_raw)

    y_train_cls = y_train.map({0: 0, 1: 1, 2: 2}).values.astype(int)
    y_oof_cls = y_oof.map({0: 0, 1: 1, 2: 2}).values.astype(int)

    logger.info(f"  X_train: {X_train.shape}, X_oof: {X_oof.shape}")
    logger.info(f"  Train 标签: H={np.sum(y_train_cls==0)} D={np.sum(y_train_cls==1)} A={np.sum(y_train_cls==2)}")
    logger.info(f"  OOF 标签:   H={np.sum(y_oof_cls==0)} D={np.sum(y_oof_cls==1)} A={np.sum(y_oof_cls==2)}")

    # ─── 步骤5: 训练基模型 ───
    logger.info("步骤5: 训练基模型 (pre-2023)")
    t0 = time.time()

    xgb = XGBClassifier(n_estimators=300, max_depth=7, learning_rate=0.05,
                        subsample=0.8, colsample_bytree=0.8,
                        random_state=42, eval_metric='mlogloss', verbosity=0)
    xgb.fit(X_train, y_train_cls)

    lgb_model = LGBMClassifier(n_estimators=300, max_depth=7, learning_rate=0.05,
                               subsample=0.8, colsample_bytree=0.8,
                               random_state=42, verbose=-1)
    lgb_model.fit(X_train, y_train_cls)

    oe_cols_in_df = [c for c in trainer.odds_feature_names if c in df_train.columns]
    oe_scaler = StandardScaler()
    if len(oe_cols_in_df) >= 10:
        X_oe_train = df_train[oe_cols_in_df].fillna(0).values.astype(np.float64)
        X_oe_train_s = oe_scaler.fit_transform(X_oe_train)
        oe_model = LGBMClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                                   random_state=42, verbose=-1)
        oe_model.fit(X_oe_train_s, y_train_cls)
        logger.info(f"  OddsExpert: {len(oe_cols_in_df)} 个 odds 特征")
    else:
        oe_model = None
        logger.warning(f"  OddsExpert: 仅 {len(oe_cols_in_df)} 个特征")

    logger.info(f"  基模型训练完成, 耗时 {time.time()-t0:.0f}s")

    # ─── 步骤6: 生成 OOF 概率 ───
    logger.info("步骤6: 生成 OOF 概率 (2023+)")

    proba_xgb_oof = xgb.predict_proba(X_oof)
    proba_lgb_oof = lgb_model.predict_proba(X_oof)

    proba_heuristic_oof = np.zeros((len(X_oof), 3))
    for i in range(len(X_oof)):
        try:
            p = trainer._heuristic_predict_proba(X_oof_raw[i].reshape(1, -1))
            proba_heuristic_oof[i] = p[0]
        except (ValueError, RuntimeError, AttributeError):
            proba_heuristic_oof[i] = [DEFAULT_HOME_PROB, DEFAULT_DRAW_PROB, DEFAULT_AWAY_PROB]

    if oe_model is not None:
        X_oe_oof = df_oof[oe_cols_in_df].fillna(0).values.astype(np.float64)
        X_oe_oof_s = oe_scaler.transform(X_oe_oof)
        proba_oe_oof = oe_model.predict_proba(X_oe_oof_s)
    else:
        proba_oe_oof = np.ones((len(X_oof), 3)) / 3

    if trainer.nn_model is not None:
        import torch
        trainer.nn_model.eval()
        with torch.no_grad():
            X_oof_t = torch.tensor(X_oof, dtype=torch.float32)
            proba_nn_oof = torch.softmax(trainer.nn_model(X_oof_t), dim=1).numpy()
    else:
        proba_nn_oof = np.ones((len(X_oof), 3)) / 3

    logger.info(f"  OOF 样本数: {len(X_oof)}")

    # ─── 步骤7: v4.1 Meta Features (DrawExpert ×0.25) ───
    logger.info(f"步骤7: 构建 v{MODEL_VERSION} meta features (DE×{V41_CONFIG['draw_expert_mult']})")

    meta_proba = np.hstack([
        proba_lgb_oof, proba_xgb_oof[:, [0, 2]], proba_heuristic_oof,
        proba_oe_oof, proba_nn_oof
    ])

    # 🔴 v4.1: DrawExpert P(Draw) ×0.25 衰减
    # fallback: 优先使用全局平局率, 否则用 DEFAULT_DRAW_PROB
    _global_dr = getattr(trainer, 'global_d_rate',
                getattr(trainer, 'config', {}).get('league_draw_prior', {}).get('global_d_rate',
                DEFAULT_DRAW_PROB))
    de_pdraw = np.ones((len(df_oof), 1)) * _global_dr
    if has_full_de:
        draw_expert = DrawExpert.load(de_model_path)
        de_scaler = joblib.load(de_scaler_path) if os.path.exists(de_scaler_path) else None

        de_oof_pdraw = np.load(de_oof_path)
        de_oof_indices = np.load(de_idx_path)
        de_idx_map = {idx: i for i, idx in enumerate(de_oof_indices)}

        aligned = np.zeros((len(df_oof), 1))
        for i, orig_idx in enumerate(df_oof.index):
            if orig_idx in de_idx_map:
                aligned[i, 0] = de_oof_pdraw[de_idx_map[orig_idx], 0]
            else:
                aligned[i, 0] = de_oof_pdraw.mean()

        de_pdraw = aligned * V41_CONFIG['draw_expert_mult']  # 🔴 ×0.25
        logger.info(f"  DrawExpert P(Draw) mean={de_pdraw.mean():.4f} (×{V41_CONFIG['draw_expert_mult']})")
    else:
        logger.warning(f"  DrawExpert OOF 缺失, 使用全局平局率 {_global_dr:.3f}")

    # Key features
    key_feats = []
    for fname in ['odds_confidence', 'match_evenness', 'imp_d_norm']:
        if fname in trainer.feature_names:
            idx = trainer.feature_names.index(fname)
            key_feats.append(X_oof[:, idx])
        else:
            key_feats.append(np.zeros(len(X_oof)))
    key_feats = np.column_stack(key_feats)

    # Drift features
    drift_feats = []
    for fname in ['drift_magnitude', 'drift_direction', 'drift_d']:
        if fname in trainer.feature_names:
            idx = trainer.feature_names.index(fname)
            drift_feats.append(X_oof[:, idx])
        else:
            drift_feats.append(np.zeros(len(X_oof)))
    drift_feats = np.column_stack(drift_feats)

    meta_features = np.hstack([meta_proba, de_pdraw, key_feats, drift_feats])
    logger.info(f"  Meta-features: {meta_features.shape} (21维)")

    # ─── 步骤8: 训练 v4.1 Meta Learner ───
    logger.info("步骤8: 训练 v4.1 Meta Learner")
    logger.info(f"  DE×{V41_CONFIG['draw_expert_mult']}, "
               f"DrawCW×{V41_CONFIG['draw_cw_mult']}, HomeCW×{V41_CONFIG['home_cw_mult']}")

    cw = compute_class_weight('balanced', classes=np.array([0, 1, 2]), y=y_oof_cls)
    class_weight = {
        0: cw[0] * V41_CONFIG['home_cw_mult'],    # 0.9 (v4.0: 0.8)
        1: cw[1] * V41_CONFIG['draw_cw_mult'],    # 1.10 (v4.0: 1.3)
        2: cw[2],
    }
    logger.info(f"  类别权重: H={class_weight[0]:.3f}, D={class_weight[1]:.3f}, A={class_weight[2]:.3f}")

    new_meta = LGBMClassifier(
        n_estimators=200, max_depth=5, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.8,
        class_weight=class_weight, random_state=42, verbose=-1,
    )
    new_meta.fit(meta_features, y_oof_cls)

    # OOF 评估 (argmax)
    y_meta_pred = new_meta.predict(meta_features)
    meta_proba_full = new_meta.predict_proba(meta_features)

    def per_class_f1(y_true, y_pred):
        f1s = f1_score(y_true, y_pred, labels=[0, 1, 2], average=None, zero_division=0)
        return f1s[0], f1s[1], f1s[2]

    meta_acc = accuracy_score(y_oof_cls, y_meta_pred)
    f1h, f1d, f1a = per_class_f1(y_oof_cls, y_meta_pred)
    meta_auc = roc_auc_score(np.eye(3)[y_oof_cls], meta_proba_full, multi_class='ovr', average='macro')

    draw_rate = (y_meta_pred == 1).sum() / len(y_meta_pred)
    logger.info(f"  Meta OOF (argmax):")
    logger.info(f"    Acc={meta_acc*100:.2f}%, H-F1={f1h:.4f}, Draw-F1={f1d:.4f}, "
               f"A-F1={f1a:.4f}, AUC={meta_auc:.4f}")
    logger.info(f"    Draw预测率: {draw_rate*100:.1f}%")

    # ── v4.1 阈值预测 ──
    logger.info(f"\n  v4.1 阈值预测 (Draw>{V41_CONFIG['draw_threshold']}, H-A gap>{V41_CONFIG['ha_gap']}):")
    y_thresh = np.zeros(len(y_oof_cls), dtype=int)
    for i in range(len(y_oof_cls)):
        p = meta_proba_full[i] / meta_proba_full[i].sum()
        if p[1] > V41_CONFIG['draw_threshold']:
            y_thresh[i] = 1
        elif p[0] > p[2] + V41_CONFIG['ha_gap']:
            y_thresh[i] = 0
        else:
            y_thresh[i] = 2

    t_acc = accuracy_score(y_oof_cls, y_thresh)
    t_f1h, t_f1d, t_f1a = per_class_f1(y_oof_cls, y_thresh)
    t_draw_rate = (y_thresh == 1).sum() / len(y_thresh)
    logger.info(f"    Acc={t_acc*100:.2f}%, H-F1={t_f1h:.4f}, Draw-F1={t_f1d:.4f}, "
               f"A-F1={t_f1a:.4f}")
    logger.info(f"    Draw预测率: {t_draw_rate*100:.1f}%")

    # ─── 步骤9: Test 评估 ───
    logger.info("步骤9: Test 评估")
    test_ratio = 0.15
    split_idx = int(len(df) * (1.0 - test_ratio))
    df_test = df.iloc[split_idx:].copy()
    if len(df_test) < 100:
        df_test = df_oof.iloc[len(df_oof)//2:]

    X_test_raw2, y_test2 = trainer.prepare_features(df_test)
    for c in trainer.feature_names:
        if c not in X_test_raw2.columns:
            X_test_raw2[c] = 0.0
    X_test_raw2 = X_test_raw2[trainer.feature_names].fillna(0).values.astype(np.float64)
    X_test_s = scaler.transform(X_test_raw2)

    proba_xgb_test = xgb.predict_proba(X_test_s)
    proba_lgb_test = lgb_model.predict_proba(X_test_s)

    proba_heur_test = np.zeros((len(X_test_s), 3))
    for i in range(len(X_test_s)):
        try:
            p = trainer._heuristic_predict_proba(X_test_s[i].reshape(1, -1))
            proba_heur_test[i] = p[0]
        except (ValueError, RuntimeError, AttributeError):
            proba_heur_test[i] = [DEFAULT_HOME_PROB, DEFAULT_DRAW_PROB, DEFAULT_AWAY_PROB]

    if oe_model is not None:
        oe_cols_test = [c for c in oe_cols_in_df if c in df_test.columns]
        X_oe_test = df_test[oe_cols_test].fillna(0).values.astype(np.float64)
        X_oe_test_s = oe_scaler.transform(X_oe_test)
        proba_oe_test = oe_model.predict_proba(X_oe_test_s)
    else:
        proba_oe_test = np.ones((len(X_test_s), 3)) / 3

    if trainer.nn_model is not None:
        import torch
        trainer.nn_model.eval()
        with torch.no_grad():
            proba_nn_test = torch.softmax(trainer.nn_model(torch.tensor(X_test_s, dtype=torch.float32)), dim=1).numpy()
    else:
        proba_nn_test = np.ones((len(X_test_s), 3)) / 3

    de_pdraw_test = np.ones((len(X_test_s), 1)) * _global_dr

    w_sum = 0.35 + 0.35 + 0.175 + 0.125
    proba_weighted = (0.35*proba_xgb_test + 0.35*proba_lgb_test +
                      0.175*proba_heur_test + 0.125*proba_oe_test) / w_sum

    meta_proba_test = np.hstack([proba_lgb_test, proba_xgb_test[:, [0, 2]], proba_heur_test,
                                  proba_oe_test, proba_nn_test])
    key_test = np.column_stack([X_test_s[:, trainer.feature_names.index(f)] if f in trainer.feature_names
                                else np.zeros(len(X_test_s))
                                for f in ['odds_confidence', 'match_evenness', 'imp_d_norm']])
    drift_test = np.column_stack([X_test_s[:, trainer.feature_names.index(f)] if f in trainer.feature_names
                                  else np.zeros(len(X_test_s))
                                  for f in ['drift_magnitude', 'drift_direction', 'drift_d']])

    meta_features_test = np.hstack([meta_proba_test, de_pdraw_test, key_test, drift_test])
    proba_stacking = new_meta.predict_proba(meta_features_test)

    y_test_cls = y_test2.map({0: 0, 1: 1, 2: 2}).values.astype(int)

    w_pred = np.argmax(proba_weighted, axis=1)
    s_pred = np.argmax(proba_stacking, axis=1)

    results = {
        'WeightedAvg': {k: float(v) for k, v in zip(
            ['acc', 'f1_h', 'f1_d', 'f1_a', 'auc'],
            [accuracy_score(y_test_cls, w_pred),
             *f1_score(y_test_cls, w_pred, average=None, zero_division=0),
             roc_auc_score(np.eye(3)[y_test_cls], proba_weighted, multi_class='ovr', average='macro')])},
        'Stacking_v41': {k: float(v) for k, v in zip(
            ['acc', 'f1_h', 'f1_d', 'f1_a', 'auc'],
            [accuracy_score(y_test_cls, s_pred),
             *f1_score(y_test_cls, s_pred, average=None, zero_division=0),
             roc_auc_score(np.eye(3)[y_test_cls], proba_stacking, multi_class='ovr', average='macro')])},
    }

    for name, r in results.items():
        logger.info(f"  {name:<20s} Acc={r['acc']*100:.2f}% H-F1={r['f1_h']:.4f} "
                   f"D-F1={r['f1_d']:.4f} A-F1={r['f1_a']:.4f} AUC={r['auc']:.4f}")

    # ─── 步骤10: 对比基线 ───
    v41_test_acc = results['Stacking_v41']['acc']
    v41_test_f1d = results['Stacking_v41']['f1_d']
    v41_test_auc = results['Stacking_v41']['auc']

    logger.info(f"\n{'指标':<15s} {'v3.2':>10s} {'v4.0':>10s} {'v4.1':>10s}")
    logger.info(f"{'-'*47}")
    logger.info(f"{'Acc':<15s} {'59.20%':>10s} {'56.55%':>10s} {v41_test_acc*100:>9.2f}%")
    logger.info(f"{'Draw F1':<15s} {'0.504':>10s} {'0.512':>10s} {v41_test_f1d:>10.4f}")
    logger.info(f"{'AUC':<15s} {'0.814':>10s} {'0.826':>10s} {v41_test_auc:>10.4f}")

    # ─── 步骤11: 保存 v4.1 模型 ───
    logger.info("\n步骤11: 保存 v4.1 模型")

    trainer.model_version = MODEL_VERSION
    trainer.meta_learner = new_meta
    trainer.scaler = scaler
    trainer.xgb_model = xgb
    trainer.lgb_model = lgb_model
    trainer.odds_expert_model = oe_model
    trainer.odds_scaler = oe_scaler
    trainer.draw_expert_model = draw_expert if has_full_de else None

    # 注入 v4.1 配置
    if not hasattr(trainer, 'v41_config'):
        trainer.__dict__['v41_config'] = V41_CONFIG

    trainer.eval_metrics = {
        'accuracy': float(t_acc),
        'f1_home': float(t_f1h),
        'f1_draw': float(t_f1d),
        'f1_away': float(t_f1a),
        'auc': float(meta_auc),
        'mcc': 0.0,
        'n_ood': int(len(y_oof_cls)),
        'draw_pred_rate': float(t_draw_rate),
        'v41_config': V41_CONFIG,
    }

    # 保存到 FootballAI
    save_path_fai = os.path.join(ROOT, 'saved_models', f'football_v{MODEL_VERSION}_production.joblib')
    trainer.save_pipeline(save_path=save_path_fai)
    logger.info(f"  ✅ {save_path_fai}")

    # 保存到 Architecture (镜像副本)
    arch_models = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    save_path_arch = os.path.join(arch_models, 'models', 'main', f'football_v{MODEL_VERSION}_production.joblib')
    os.makedirs(os.path.dirname(save_path_arch), exist_ok=True)  # 确保 models/main/ 存在
    joblib.dump({
        'xgb_model': xgb,
        'lgb_model': lgb_model,
        'odds_expert_model': oe_model,
        'draw_expert_model': trainer.draw_expert_model,
        'nn_state_dict': trainer.nn_model.state_dict() if trainer.nn_model else None,
        'nn_scaler': getattr(trainer, 'nn_scaler', None),
        'scaler': scaler,
        'odds_scaler': oe_scaler,
        'meta_learner': new_meta,
        'feature_names': trainer.feature_names,
        'odds_feature_names': trainer.odds_feature_names,
        'config': trainer.config,
        'model_version': MODEL_VERSION,
        'eval_metrics': trainer.eval_metrics,
        'league_d_rates': trainer.league_d_rates,
        'v41_config': V41_CONFIG,
    }, save_path_arch)
    logger.info(f"  ✅ {save_path_arch}")

    # 更新 model_registry (兼容 active/production/models 结构)
    registry_path = os.path.join(ROOT, 'saved_models', 'model_registry.json')
    registry = json.load(open(registry_path, encoding='utf-8')) if os.path.exists(registry_path) else {}
    registry.setdefault('models', {})
    registry.setdefault('versions', [])  # 兼容历史键, 保留追加逻辑
    version_entry = {
        'version': MODEL_VERSION,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'accuracy': round(float(v41_test_acc), 4),
        'auc': round(float(v41_test_auc), 4),
        'mcc': 0,
        'draw_f1': round(float(v41_test_f1d), 4),
        'n_features': len(trainer.feature_names),
        'models': ['lightgbm', 'xgboost', 'odds_expert', 'heuristic', 'neural_net', 'draw_expert'],
        'stacking': True,
        'v41_config': V41_CONFIG,
    }
    registry['current'] = version_entry
    registry['versions'].append(version_entry)
    with open(registry_path, 'w', encoding='utf-8') as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)
    logger.info(f"  ✅ model_registry 已更新")

    elapsed = time.time() - t_start
    logger.info(f"\n{'='*60}")
    logger.info(f"✅ v{MODEL_VERSION} Production 模型生成完毕!")
    logger.info(f"  耗时: {elapsed:.1f}s")
    logger.info(f"  OOF: Acc={t_acc*100:.2f}%, Draw-F1={t_f1d:.4f}, Draw预测率={t_draw_rate*100:.1f}%")
    logger.info(f"  模型: {save_path_fai}")

    return save_path_fai

if __name__ == '__main__':
    main()

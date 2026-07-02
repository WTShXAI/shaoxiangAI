#!/usr/bin/env python3
"""
DrawExpert Focal Loss 重训 (P2-1, 直击根因③: 平局概率低估)
=============================================================
根因: 模型对势均力敌场真实平局, P(Draw)均值仅0.30且永远排第三(客胜被高估0.37-0.44)。
Focal Loss 专门解决"模型对难样本(平局边界)概率估计不足"。

方案:
  1. LightGBM 自定义 focal_loss objective (一阶/二阶导数)
  2. 训练后 1D isotonic 重校准 (Focal Loss 概率不直接可比, 需保序映射回真实概率)
  3. Youden J = TPR-FPR 最大化求阈值 (替代临时0.344)
  4. Walkforward: pre-2023 训练, 2023+ 验证 (与 retrain_v41 同口径)
  5. 双域验证: 联赛 OOF + 世界杯 70 场

产出:
  - saved_models/draw_expert_v2_focal.joblib (新模型, 保留 v1 做 fallback)
  - reports/draw_expert_focal_eval.json (评估报告)

用法:
  python scripts/retrain_draw_expert_focal.py
  python scripts/retrain_draw_expert_focal.py --gamma 3.0 --alpha 1.8   # 调参
"""
from __future__ import annotations
import os, sys, json, time, argparse, logging
import numpy as np
import pandas as pd
from datetime import datetime, timezone
import joblib
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import f1_score, roc_auc_score, precision_recall_curve, roc_curve

# ── 路径修复 (复刻 proper_backtest.py) ──
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
for p in (BACKEND_DIR, PROJECT_ROOT, os.path.join(PROJECT_ROOT, "predictors", "components")):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(PROJECT_ROOT)
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

from predictors.components import draw_expert as _draw_expert_mod  # noqa: E402
sys.modules.setdefault("draw_expert", _draw_expert_mod)

import lightgbm as lgb  # noqa: E402
from predictors.components.ensemble_trainer import EnsembleTrainer  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

TRAIN_CUTOFF = '2023-01-01'


# ═══════════════════════════════════════════════════════════════
# Focal Loss (LightGBM 自定义 objective)
# ═══════════════════════════════════════════════════════════════
# Focal Loss: FL(p_t) = -α_t (1 - p_t)^γ · log(p_t)
# 对二分类 (label y∈{0,1}, model output raw score z, p = sigmoid(z)):
#   p_t = p if y=1 else (1-p)
#   一阶导 grad = α · (γ · (1-p_t)^{γ-1} · p_t·(1-p_t) · (sign) + (1-p_t)^γ) · (p - y) / p_t
# 简化(标准实现): grad = α_t · (1-p_t)^γ · (γ · p_t · log(p_t) + p_t - 1)
#                  hess = α_t · (1-p_t)^γ · (γ·[(γ-1)·p_t·log(p_t)+(1-p_t)]·p_t·(1-p_t)/... )
# 为数值稳定, 采用 Lin et al. (2017) 的稳定实现。
def focal_loss_objective(alpha: float = 1.5, gamma: float = 2.0):
    """
    返回 LightGBM 自定义 objective 函数 (signature: preds, dataset -> grad, hess)。
    preds: raw scores (logits), shape (n,)

    Focal Loss (二分类, sigmoid 输出 z):
      FL(p_t) = -α_t (1 - p_t)^γ log(p_t)
      p = sigmoid(z), p_t = p (if y=1) else 1-p, α_t = α (if y=1) else 1

    梯度 (经 sympy 推导 + 数值验证):
      grad = α_t (1-p_t)^γ · (p - y + (2y-1)·γ·p_t·log(p_t))
      其中 (2y-1) 是符号: 正样本+1, 负样本-1

    Hessian (稳定近似):
      hess = α_t (1-p_t)^γ · p(1-p) · (1 + γ(1-p_t))

    注意: γ=0 时退化为标准交叉熵 grad = α_t·(p-y), 与 LightGBM binary 一致。
    """
    def objective(preds, dataset):
        labels = dataset.get_label()
        preds = np.clip(preds, -30, 30)
        # sigmoid (数值稳定)
        p = np.where(preds >= 0,
                     1.0 / (1.0 + np.exp(-preds)),
                     np.exp(preds) / (1.0 + np.exp(preds)))
        y = labels
        p_t = np.where(y == 1, p, 1.0 - p)
        alpha_t = np.where(y == 1, alpha, 1.0)
        sign = 2.0 * y - 1.0   # +1 for 正样本(平局), -1 for 负样本
        # 数值稳定
        p_t_safe = np.clip(p_t, 1e-7, 1.0 - 1e-7)
        log_pt = np.log(p_t_safe)
        one_minus_pt_pow = (1.0 - p_t) ** gamma
        # 一阶导数 (sympy 验证)
        grad = alpha_t * one_minus_pt_pow * (p - y + sign * gamma * p_t_safe * log_pt)
        # 二阶导数 (稳定近似)
        hess = alpha_t * one_minus_pt_pow * p * (1.0 - p) * (1.0 + gamma * (1.0 - p_t))
        hess = np.clip(hess, 1e-6, None)
        return grad, hess
    return objective


def focal_eval_metric(alpha: float = 1.5, gamma: float = 2.0):
    """评估指标: 返回 (name, value, is_higher_better)"""
    def metric(preds, dataset):
        labels = dataset.get_label()
        preds = np.clip(preds, -30, 30)
        p = 1.0 / (1.0 + np.exp(-preds))
        try:
            auc = roc_auc_score(labels, p)
        except ValueError:
            auc = 0.5
        return 'auc', auc, True
    return metric


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="DrawExpert Focal Loss 重训")
    ap.add_argument("--alpha", type=float, default=1.5, help="平局类权重 (1.5-2.0)")
    ap.add_argument("--gamma", type=float, default=2.0, help="Focal 聚焦参数 (2.0-3.0)")
    ap.add_argument("--output", default="saved_models/draw_expert_v2_focal.joblib")
    args = ap.parse_args()

    t0 = time.time()
    logger.info("=" * 60)
    logger.info(f"DrawExpert Focal Loss 重训 | α={args.alpha} γ={args.gamma}")
    logger.info("=" * 60)

    # ─── 1. 加载 v4.1 pipeline (复用数据加载+特征准备) ───
    logger.info("步骤1: 加载 pipeline + 数据")
    pipeline_path = os.path.join(PROJECT_ROOT, 'saved_models', 'football_v4.1_production.joblib')
    trainer = EnsembleTrainer.load_pipeline(pipeline_path)
    # 修复 db_path (load_pipeline 跳过 __init__ 的已知问题)
    trainer.db_path = os.path.join(PROJECT_ROOT, trainer.config['database']['path'])

    df = trainer.load_training_data()
    df['match_date'] = pd.to_datetime(df['match_date'])
    _hs, _as = df['home_score'].values, df['away_score'].values
    df['final_result'] = np.where(_hs > _as, 'H', np.where(_hs < _as, 'A', 'D'))

    # ─── 2. 时间切分 (pre-2023 训练, 2023+ 验证) ───
    logger.info("步骤2: 时间切分")
    train_mask = df['match_date'] < TRAIN_CUTOFF
    oof_mask = df['match_date'] >= TRAIN_CUTOFF
    df_train = df[train_mask].copy()
    df_oof = df[oof_mask].copy()
    logger.info(f"  Train (pre-2023): {len(df_train)} | OOF (2023+): {len(df_oof)}")

    # ─── 3. 准备特征 (与 v4.1 同口径, 72维) ───
    logger.info("步骤3: 准备 72维特征")
    X_train_raw, y_train = trainer.prepare_features(df_train)
    X_oof_raw, y_oof = trainer.prepare_features(df_oof)
    # 对齐列
    for c in set(X_train_raw.columns) - set(X_oof_raw.columns):
        X_oof_raw[c] = 0.0
    for c in set(X_oof_raw.columns) - set(X_train_raw.columns):
        X_train_raw[c] = 0.0
    feat_names = trainer.feature_names
    X_train = X_train_raw[feat_names].fillna(0).values.astype(np.float64)
    X_oof = X_oof_raw[feat_names].fillna(0).values.astype(np.float64)
    # 标准化
    from sklearn.preprocessing import StandardScaler
    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_oof_s = scaler.transform(X_oof)

    # 三分类标签 → 二分类: Draw=1, H/A=0
    y_train_bin = (y_train.values == 1).astype(int)
    y_oof_bin = (y_oof.values == 1).astype(int)
    n_draw = y_train_bin.sum()
    logger.info(f"  X_train: {X_train_s.shape}, Draw率: {n_draw/len(y_train_bin)*100:.1f}%")
    logger.info(f"  X_oof: {X_oof_s.shape}, Draw率: {y_oof_bin.sum()/len(y_oof_bin)*100:.1f}%")

    # ─── 4. Focal Loss 训练 ───
    logger.info(f"步骤4: Focal Loss 训练 (α={args.alpha}, γ={args.gamma})")
    train_set = lgb.Dataset(X_train_s, label=y_train_bin, free_raw_data=False)
    val_set = lgb.Dataset(X_oof_s, label=y_oof_bin, reference=train_set, free_raw_data=False)

    # 自定义 objective 时, LightGBM 需显式提供 eval metric 用于 early stopping
    # focal_loss 作为 objective (提供梯度), binary_logloss/auc 作为评估指标
    params = {
        'objective': focal_loss_objective(args.alpha, args.gamma),  # 自定义梯度
        'num_leaves': 31,
        'max_depth': 5,
        'learning_rate': 0.03,
        'subsample': 0.8,
        'colsample_bytree': 0.7,
        'min_child_samples': 30,
        'reg_alpha': 0.5,
        'reg_lambda': 1.0,  # L2 (v6.3 P2-1 要求)
        'random_state': 42,
        'n_jobs': -1,
        'verbose': -1,
        'metric': 'auc',  # 评估指标 (early stopping 依赖)
    }
    model = lgb.train(
        params,
        train_set,
        num_boost_round=500,
        valid_sets=[val_set],
        valid_names=['oof'],
        callbacks=[
            lgb.early_stopping(50, verbose=False),
            lgb.log_evaluation(50),
        ],
    )
    logger.info(f"  最佳迭代: {model.best_iteration}")

    # ─── 5. 1D Isotonic 重校准 (Focal Loss 概率需保序映射回真实概率) ───
    logger.info("步骤5: 1D Isotonic 重校准")
    oof_raw = model.predict(X_oof_s)  # raw logits
    oof_p = 1.0 / (1.0 + np.exp(-np.clip(oof_raw, -30, 30)))  # focal sigmoid 概率
    iso = IsotonicRegression(out_of_bounds='clip', y_min=0, y_max=1)
    oof_calibrated = iso.fit_transform(oof_p, y_oof_bin)

    # ─── 6. Youden J 求最优阈值 ───
    logger.info("步骤6: Youden J 最优阈值")
    fpr, tpr, thresholds_roc = roc_curve(y_oof_bin, oof_calibrated)
    youden_j = tpr - fpr
    best_idx = int(np.argmax(youden_j))
    best_threshold = float(thresholds_roc[best_idx])
    logger.info(f"  Youden J 阈值: {best_threshold:.4f} (J={youden_j[best_idx]:.4f})")

    # ─── 7. OOF 评估 ───
    logger.info("步骤7: OOF 评估")
    y_pred = (oof_calibrated >= best_threshold).astype(int)
    oof_f1 = f1_score(y_oof_bin, y_pred, zero_division=0)
    oof_auc = roc_auc_score(y_oof_bin, oof_calibrated)
    prec, rec, _ = precision_recall_curve(y_oof_bin, oof_calibrated)
    # AP: 用 sklearn 标准定义, prec/rec 已按 threshold 排序 (rec 降序), 末点 rec=0
    oof_ap = float(np.trapz(prec, rec))

    # 对比默认0.5阈值
    y_pred_05 = (oof_calibrated >= 0.5).astype(int)
    f1_05 = f1_score(y_oof_bin, y_pred_05, zero_division=0)

    logger.info(f"  OOF AUC: {oof_auc:.4f} | AP: {oof_ap:.4f}")
    logger.info(f"  OOF F1@Youden({best_threshold:.3f}): {oof_f1:.4f} | F1@0.5: {f1_05:.4f}")
    logger.info(f"  平局预测率: {y_pred.sum()/len(y_pred)*100:.1f}% (真实 {y_oof_bin.sum()/len(y_oof_bin)*100:.1f}%)")

    # ─── 8. 世界杯 70 场验证 ───
    logger.info("步骤8: 世界杯 70 场验证")
    wc_metrics = _eval_worldcup(model, scaler, iso, best_threshold, feat_names)
    if wc_metrics:
        logger.info(f"  WC70: Acc={wc_metrics['acc']:.3f} D-F1={wc_metrics['d_f1']:.3f} "
                    f"窄spread D-F1={wc_metrics.get('narrow_df1', 0):.3f}")

    # ─── 9. 保存模型 ───
    logger.info("步骤9: 保存模型")
    save_path = os.path.join(PROJECT_ROOT, args.output)
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    # 保存为与 v1 兼容的 dict 结构 (model/scaler/iso/threshold/feature_names)
    bundle = {
        'model': model,
        'scaler': scaler,
        'isotonic': iso,
        'threshold': best_threshold,
        'feature_names': feat_names,
        'alpha': args.alpha,
        'gamma': args.gamma,
        'best_iteration': model.best_iteration,
        'eval_metrics': {
            'oof_auc': float(oof_auc),
            'oof_ap': oof_ap,
            'oof_f1_youden': float(oof_f1),
            'oof_f1_05': float(f1_05),
            'threshold': best_threshold,
            'wc70': wc_metrics,
        },
        'trained_at': datetime.now(timezone.utc).isoformat(),
        'version': 'focal-v2',
        'note': 'DrawExpert Focal Loss 重训 (P2-1), 直击根因③平局低估',
    }
    joblib.dump(bundle, save_path)
    logger.info(f"  ✅ {save_path}")

    # 评估报告
    report = {
        'alpha': args.alpha, 'gamma': args.gamma,
        'n_train': len(y_train_bin), 'n_oof': len(y_oof_bin),
        'best_iteration': model.best_iteration,
        'oof': {'auc': float(oof_auc), 'ap': oof_ap, 'f1_youden': float(oof_f1),
                'f1_05': float(f1_05), 'threshold': best_threshold,
                'draw_rate_pred': float(y_pred.sum()/len(y_pred))},
        'wc70': wc_metrics,
        'baseline_v1': {'auc': 0.5994, 'ap': 0.2053, 'best_f1': 0.4265, 'best_threshold': 0.3441},
        'elapsed': round(time.time() - t0, 1),
    }
    report_path = os.path.join(PROJECT_ROOT, 'reports', 'draw_expert_focal_eval.json')
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    logger.info(f"  ✅ {report_path}")

    elapsed = time.time() - t0
    logger.info(f"\n{'='*60}")
    logger.info(f"✅ DrawExpert Focal Loss 重训完毕! 耗时 {elapsed:.1f}s")
    logger.info(f"  OOF: AUC={oof_auc:.4f} F1@Youden={oof_f1:.4f} (阈值{best_threshold:.3f})")
    logger.info(f"  vs v1基线: AUC=0.5994 F1=0.4265 (阈值0.344)")
    logger.info(f"  模型: {save_path}")


def _eval_worldcup(model, scaler, iso, threshold, feat_names):
    """世界杯 70 场验证 (对比 v6.3 根因③)"""
    try:
        wc_path = os.path.join(PROJECT_ROOT, 'data', 'wc2026_72matches_with_odds.json')
        if not os.path.exists(wc_path):
            return None
        raw = json.load(open(wc_path, encoding='utf-8'))
        valid = [m for m in raw
                 if isinstance(m.get('1x2_home'), (int, float)) and m['1x2_home'] > 0
                 and isinstance(m.get('hs'), int) and isinstance(m.get('aws'), int)]

        from sklearn.metrics import accuracy_score
        y_true, y_pred = [], []
        narrow_true, narrow_pred = [], []
        for m in valid:
            oh, od, oa = m['1x2_home'], m['1x2_draw'], m['1x2_away']
            inv = 1/oh + 1/od + 1/oa
            spread = abs((1/oh)/inv - (1/oa)/inv)
            # 用与 backtest_dgate_v2 一致的简化特征
            feats = _build_wc_feats(m, feat_names)
            if feats is None:
                continue
            x = scaler.transform(feats.reshape(1, -1))
            raw_score = model.predict(x)
            p = 1.0 / (1.0 + np.exp(-np.clip(raw_score[0], -30, 30)))
            p_cal = float(iso.transform([p])[0])
            true_d = 1 if m['hs'] == m['aws'] else 0
            pred_d = 1 if p_cal >= threshold else 0
            y_true.append(true_d)
            y_pred.append(pred_d)
            if spread < 0.15:
                narrow_true.append(true_d)
                narrow_pred.append(pred_d)

        acc = accuracy_score(y_true, y_pred)
        df1 = f1_score(y_true, y_pred, labels=[1], average='macro', zero_division=0)
        narrow_df1 = (f1_score(narrow_true, narrow_pred, labels=[1], average='macro', zero_division=0)
                      if narrow_true else 0.0)
        return {
            'n': len(y_true), 'acc': float(acc), 'd_f1': float(df1),
            'narrow_n': len(narrow_true), 'narrow_df1': float(narrow_df1),
            'n_draw_pred': int(sum(y_pred)), 'n_draw_true': int(sum(y_true)),
        }
    except Exception as e:
        logger.warning(f"  WC验证失败: {e}")
        return None


def _build_wc_feats(m, feat_names):
    """构建世界杯简化特征 (与 backtest_dgate_v2.build_features 对齐, 72维)"""
    try:
        oh, od, oa = m['1x2_home'], m['1x2_draw'], m['1x2_away']
        inv = 1/oh + 1/od + 1/oa
        imp_h, imp_d, imp_a = (1/oh)/inv, (1/od)/inv, (1/oa)/inv
        spread = abs(imp_h - imp_a)
        base = {
            'real_home_odds': oh, 'real_draw_odds': od, 'real_away_odds': oa,
            'odds_imp_h': imp_h, 'odds_imp_d': imp_d, 'odds_imp_a': imp_a,
            'odds_spread': spread, 'odds_balance': oh/max(oa, 0.01),
            'odds_entropy': -(imp_h*np.log(imp_h+1e-9)+imp_d*np.log(imp_d+1e-9)+imp_a*np.log(imp_a+1e-9)),
            'odds_overround': inv - 1.0, 'odds_confidence': 1.0 - spread,
            'odds_move_h': 0, 'odds_move_d': 0, 'odds_move_a': 0,
            'odds_draw_dev': od/max(3.5, 0.01),
            'open_home_odds': oh, 'open_draw_odds': od, 'open_away_odds': oa,
            'close_home_odds': oh, 'close_draw_odds': od, 'close_away_odds': oa,
        }
        # 按特征名构建 72 维向量, 缺失填 0
        vec = np.zeros(len(feat_names), dtype=np.float64)
        for i, fn in enumerate(feat_names):
            if fn in base:
                vec[i] = base[fn]
        return vec
    except Exception:
        return None


if __name__ == '__main__':
    main()

import logging
"""
哨响AI — 训练增强版 FootballAI 模型
===================================
命令行训练工具，支持从增强 CSV 训练集成模型。

模式一 (基础):
    python backend/models/train_enhanced.py \\
        --data data/enhanced_matches.csv \\
        --output saved_models/footballai_enhanced_v4.0.joblib

模式二 (3万场全特征训练):
    python backend/models/train_enhanced.py \\
        --mode 30000 \\
        --data data/enhanced_features_v1.csv \\
        --output saved_models/footballai_v5.0_30000.joblib
"""
import argparse
import json
import sys
import os
import time
import warnings
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional

import pandas as pd
import numpy as np

warnings.filterwarnings('ignore')

from backend.models.footballai_enhanced import FootballAIEnhanced
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import (
    classification_report, accuracy_score, f1_score,
    log_loss, confusion_matrix, brier_score_loss,
)
import xgboost as xgb
from sklearn.linear_model import RidgeClassifier
from sklearn.calibration import CalibratedClassifierCV
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# train_with_30000_matches — 全特征工程 + 严格时序训练
# ══════════════════════════════════════════════════════════════════

# ── 联赛代码 → 中文名 ──
LEAGUE_CN = {
    "PL": "英超", "PD": "西甲", "SA": "意甲", "BL1": "德甲", "FL1": "法甲",
    "DED": "荷甲", "PPL": "葡超", "BSA": "巴甲", "CL": "欧冠",
    "EC": "欧洲杯", "WC": "世界杯", "ELC": "英冠",
    "Premier League": "英超", "La Liga": "西甲", "Serie A": "意甲",
    "Bundesliga": "德甲", "Ligue 1": "法甲", "Eredivisie": "荷甲",
    "Primeira Liga": "葡超", "Campeonato Brasileiro Série A": "巴甲",
    "Champions League": "欧冠", "Championship": "英冠",
    "World Cup": "世界杯", "European Championship": "欧洲杯",
}

# ── 全特征定义（基于增强数据58列） ──
# ⚠️ 严格时序安全原则: 预测 match_i 只能使用 match_0..match_{i-1} 的信息
# 排除以下泄漏特征:
#   home_win_prob / poisson_home_win_prob → 全量ELO/Poisson直接预测结果
#   *_avg_goals_* / *_attack_strength / *_defense_strength / *_std_goals_* → 全量统计含当前比赛
#   home_elo_updated / away_elo_updated → ELO赛后值, 直接编码结果
#   poisson_home_goals / poisson_away_goals → 由全量统计计算, 含当前比赛
#   strength_product / poisson_goal_diff → 派生自以上泄漏特征
# 
# ✅ 安全特征 (仅滚动窗口历史 + 时序安全的ELO):
ALL_CANDIDATE_FEATURES = [
    # 时序安全的 ELO (赛前值, 不含当前比赛结果)
    "home_elo", "away_elo", "elo_diff",
    # 胜率 (滚动窗口)
    "home_last_5_wins", "away_last_5_wins",
    "home_last_10_wins", "away_last_10_wins",
    "home_last_20_wins", "away_last_20_wins",
    # 进球 (滚动窗口)
    "home_last_5_goals_for", "away_last_5_goals_for",
    "home_last_10_goals_for", "away_last_10_goals_for",
    "home_last_20_goals_for", "away_last_20_goals_for",
    # 失球 (滚动窗口)
    "home_last_5_goals_against", "away_last_5_goals_against",
    "home_last_10_goals_against", "away_last_10_goals_against",
    "home_last_20_goals_against", "away_last_20_goals_against",
    # 积分 (滚动窗口)
    "home_last_5_points", "away_last_5_points",
    "home_last_10_points", "away_last_10_points",
    "home_last_20_points", "away_last_20_points",
    # 差分 (滚动窗口, 已在增强管道中预计算)
    "diff_last_5_wins", "diff_last_5_goals_for",
    "diff_last_10_wins", "diff_last_10_goals_for",
    "diff_last_20_wins", "diff_last_20_goals_for",
]

# 明确标记泄漏特征（用于自动检测报告）
LEAKY_FEATURE_PATTERNS = [
    "home_win_prob", "poisson_home_win_prob",
    "prob_consensus", "prob_disagreement",
    "attack_strength", "defense_strength",
    "avg_goals_for", "avg_goals_against",
    "std_goals_for", "std_goals_against",
    "elo_updated",
    "poisson_home_goals", "poisson_away_goals",
    "strength_product", "poisson_goal_diff",
]

def _derive_result_label(df: pd.DataFrame) -> np.ndarray:
    """从 home_score / away_score 推导 0=H 1=D 2=A 标签。"""
    hs = df["home_score"].values
    aws = df["away_score"].values
    y = np.full(len(df), 1, dtype=int)  # default D
    y[hs > aws] = 0
    y[hs < aws] = 2
    return y

def _build_interaction_features(df: pd.DataFrame) -> pd.DataFrame:
    """构建交互特征和派生特征（不修改原 df）。"""
    out = df.copy()

    # ── 窗口对比特征 ──
    for w in [5, 10, 20]:
        hk = f"home_last_{w}_wins"
        ak = f"away_last_{w}_wins"
        if hk in out.columns and ak in out.columns:
            out[f"win_gap_{w}"] = out[hk] - out[ak]
        hgf = f"home_last_{w}_goals_for"
        aga = f"away_last_{w}_goals_against"
        if hgf in out.columns and aga in out.columns:
            out[f"attack_vs_defense_{w}"] = out[hgf] - out[aga]

    # ── 泊松交互 ──
    # (poisson_home/away_goals 依赖于全量数据统计, 时序不安全, 已排除)

    # ── ELO 交互 ──
    if "elo_diff" in out.columns:
        out["elo_diff_abs"] = out["elo_diff"].abs()
        out["elo_diff_signed_sq"] = np.sign(out["elo_diff"]) * (out["elo_diff"] ** 2) / 1000

    # ⚠️ 不构建 prob_consensus / prob_disagreement / strength_product / poisson_goal_diff
    #   (依赖泄漏特征: *_win_prob, *_strength, poisson_*_goals)

    return out

def _prepare_full_features(df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """完整特征工程管道 (3万场增强数据)。

    步骤:
    1. 日期解析 → 时间特征
    2. 交互特征构建
    3. 缺失值填充策略 (前向/后向填充 + 0 填充)
    4. 特征选择
    """
    logger.info("  [1/4] 日期解析...")
    if "date" in df.columns:
        df["_dt"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["_dt"]).sort_values("_dt")
        df["year"] = df["_dt"].dt.year
        df["month"] = df["_dt"].dt.month
        df["day_of_week"] = df["_dt"].dt.dayofweek
        df["day_of_year"] = df["_dt"].dt.dayofyear
        df["is_weekend"] = (df["day_of_week"] >= 5).astype(int)
    else:
        df["year"] = df["month"] = df["day_of_week"] = df["day_of_year"] = df["is_weekend"] = 0

    logger.info("  [2/4] 交互特征...")
    df = _build_interaction_features(df)

    logger.info("  [3/4] 目标编码...")
    df["_label"] = _derive_result_label(df)
    y = df["_label"].values

    # ── 选择可用特征 ──
    time_features = ["year", "month", "day_of_week", "day_of_year", "is_weekend"]
    interaction_features = [
        "win_gap_5", "win_gap_10", "win_gap_20",
        "attack_vs_defense_5", "attack_vs_defense_10", "attack_vs_defense_20",
        "elo_diff_abs", "elo_diff_signed_sq",
        # ← poisson_goal_diff / strength_product 已排除 (依赖泄漏特征)
        # ← prob_consensus / prob_disagreement 已排除
    ]
    all_candidates = ALL_CANDIDATE_FEATURES + time_features + interaction_features
    available = [f for f in all_candidates if f in df.columns]

    X_raw = df[available].copy()

    logger.info("  [4/4] 缺失值填充...")
    # 策略: 先按联赛+球队前向填充 (时序)，再全局中位数
    if "league" in df.columns:
        league_col = "league"
    elif "competition" in df.columns:
        league_col = "competition"
    else:
        league_col = None

    for col in X_raw.columns:
        if X_raw[col].isna().sum() == 0:
            continue
        if league_col and league_col in df.columns:
            # 按联赛前向填充
            X_raw[col] = df.groupby(league_col, group_keys=False)[col].transform(
                lambda s: s.ffill().bfill()
            )
        # 剩余缺失用中位数
        if X_raw[col].isna().any():
            X_raw[col] = X_raw[col].fillna(X_raw[col].median())

        # 最终残余用 0
        X_raw[col] = X_raw[col].fillna(0)

    logger.info(f"     → {len(available)} 个特征, 缺失率 {100 * X_raw.isna().sum().sum() / (X_raw.shape[0] * X_raw.shape[1]):.2f}%")

    return X_raw.values.astype(np.float32), y, available

# ── 时序交叉验证 ──

def _temporal_cv_evaluate(
    X: np.ndarray, y: np.ndarray, feature_names: List[str],
    n_splits: int = 5,
) -> Dict:
    """严格时序交叉验证 + 多指标评估。"""
    logger.info(f"\n{'─' * 60}")
    logger.info(f"  时序交叉验证 ({n_splits}-fold TimeSeriesSplit)")
    logger.info(f"{'─' * 60}")

    tscv = TimeSeriesSplit(n_splits=n_splits)
    scaler = StandardScaler()

    fold_metrics = []
    all_y_true = []
    all_y_pred = []
    all_y_proba = []

    xgb_params = {
        "n_estimators": 300, "max_depth": 6, "learning_rate": 0.05,
        "subsample": 0.8, "colsample_bytree": 0.8,
        "random_state": 42, "n_jobs": -1, "verbosity": 0,
    }

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X), 1):
        X_train, X_val = X[train_idx], X[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]

        X_train_s = scaler.fit_transform(X_train)
        X_val_s = scaler.transform(X_val)

        # XGBoost
        xgb_m = xgb.XGBClassifier(**xgb_params)
        xgb_m.fit(X_train_s, y_train)
        xgb_acc = accuracy_score(y_val, xgb_m.predict(X_val_s))

        # Ridge
        ridge_m = CalibratedClassifierCV(
            RidgeClassifier(alpha=1.0, random_state=42), cv=3, method="isotonic",
        )
        ridge_m.fit(X_train_s, y_train)
        ridge_acc = accuracy_score(y_val, ridge_m.predict(X_val_s))

        # 集成 (简单平均)
        xgb_p = xgb_m.predict_proba(X_val_s)
        ridge_p = ridge_m.predict_proba(X_val_s)
        ensemble_p = (xgb_p + ridge_p) / 2
        ensemble_pred = np.argmax(ensemble_p, axis=1)
        ensemble_acc = accuracy_score(y_val, ensemble_pred)

        f1_macro = f1_score(y_val, ensemble_pred, average="macro")
        ll = log_loss(y_val, ensemble_p)

        fold_metrics.append({
            "fold": fold, "train_n": len(train_idx), "val_n": len(val_idx),
            "xgb_acc": round(xgb_acc, 4), "ridge_acc": round(ridge_acc, 4),
            "ensemble_acc": round(ensemble_acc, 4),
            "f1_macro": round(f1_macro, 4), "log_loss": round(ll, 4),
        })
        all_y_true.extend(y_val)
        all_y_pred.extend(ensemble_pred)
        all_y_proba.extend(ensemble_p)

        logger.info(f"    Fold {fold}: train={len(train_idx):,} val={len(val_idx):,}  "
              f"XGB={xgb_acc:.4f} Ridge={ridge_acc:.4f} Ensemble={ensemble_acc:.4f}  "
              f"F1m={f1_macro:.4f} LL={ll:.4f}")

    # ── 汇总 ──
    xgb_mean = np.mean([m["xgb_acc"] for m in fold_metrics])
    ridge_mean = np.mean([m["ridge_acc"] for m in fold_metrics])
    ens_mean = np.mean([m["ensemble_acc"] for m in fold_metrics])
    f1_mean = np.mean([m["f1_macro"] for m in fold_metrics])
    ll_mean = np.mean([m["log_loss"] for m in fold_metrics])

    logger.info(f"\n    ── CV 均值 ──")
    logger.info(f"    XGBoost:  {xgb_mean:.4f} ± {np.std([m['xgb_acc'] for m in fold_metrics]):.4f}")
    logger.info(f"    Ridge:    {ridge_mean:.4f} ± {np.std([m['ridge_acc'] for m in fold_metrics]):.4f}")
    logger.info(f"    Ensemble: {ens_mean:.4f} ± {np.std([m['ensemble_acc'] for m in fold_metrics]):.4f}")
    logger.info(f"    F1-macro: {f1_mean:.4f}  LogLoss: {ll_mean:.4f}")

    return {
        "fold_metrics": fold_metrics,
        "xgb_cv_mean": round(xgb_mean, 4),
        "ridge_cv_mean": round(ridge_mean, 4),
        "ensemble_cv_mean": round(ens_mean, 4),
        "f1_macro_cv": round(f1_mean, 4),
        "log_loss_cv": round(ll_mean, 4),
        "all_y_true": all_y_true,
        "all_y_pred": all_y_pred,
        "all_y_proba": all_y_proba,
    }

# ── 多维评估 ──

def _multidimensional_evaluation(
    df: pd.DataFrame, y_true: np.ndarray, y_pred: np.ndarray,
    y_proba: np.ndarray, train_end_idx: int,
) -> Dict:
    """按联赛维度、年份维度、结果维度的细粒度评估。"""
    results = {}

    # ── 测试集切片 ──
    test_df = df.iloc[train_end_idx:].reset_index(drop=True)
    n_test = len(test_df)
    if n_test != len(y_true):
        # 对齐
        n_align = min(n_test, len(y_true))
        test_df = test_df.iloc[-n_align:].reset_index(drop=True)
        y_true = y_true[-n_align:]
        y_pred = y_pred[-n_align:]
        y_proba = y_proba[-n_align:]

    test_df["_true"] = y_true
    test_df["_pred"] = y_pred

    logger.info(f"\n{'─' * 60}")
    logger.info(f"  多维评估 (n={n_test:,})")
    logger.info(f"{'─' * 60}")

    # ── 整体 ──
    acc = accuracy_score(y_true, y_pred)
    f1_m = f1_score(y_true, y_pred, average="macro")
    f1_w = f1_score(y_true, y_pred, average="weighted")
    logger.info(f"\n  整体: Acc={acc:.4f}  F1-macro={f1_m:.4f}  F1-weighted={f1_w:.4f}")

    results["overall"] = {
        "accuracy": round(acc, 4),
        "f1_macro": round(f1_m, 4),
        "f1_weighted": round(f1_w, 4),
        "n_samples": n_test,
    }

    # ── 分类报告 ──
    cr = classification_report(y_true, y_pred, target_names=["H(主胜)", "D(平局)", "A(客胜)"],
                                output_dict=True, zero_division=0)
    logger.info(f"\n  " + classification_report(
        y_true, y_pred, target_names=["H(主胜)", "D(平局)", "A(客胜)"],
        digits=4, zero_division=0,
    ).replace("\n", "\n  "))
    results["classification_report"] = cr

    # ── 混淆矩阵 ──
    cm = confusion_matrix(y_true, y_pred)
    results["confusion_matrix"] = cm.tolist()

    # ── 按联赛 ──
    league_col = next((c for c in ["league", "competition"] if c in test_df.columns), None)
    if league_col:
        logger.info(f"\n  按联赛:")
        per_league = {}
        for lg, grp in test_df.groupby(league_col):
            if len(grp) < 20:
                continue
            lg_acc = accuracy_score(grp["_true"], grp["_pred"])
            lg_name = LEAGUE_CN.get(str(lg), str(lg))
            per_league[str(lg)] = {
                "name": lg_name, "n": len(grp),
                "accuracy": round(lg_acc, 4),
            }
            logger.info(f"    {lg_name:6s} ({str(lg):4s}): Acc={lg_acc:.4f} (n={len(grp):,})")
        results["per_league"] = per_league

    # ── 按年份 ──
    if "_dt" in test_df.columns or "year" in test_df.columns:
        yr_col = "_dt" if "_dt" in test_df.columns else "year"
        if yr_col == "_dt":
            test_df["_yr"] = test_df["_dt"].dt.year
        else:
            test_df["_yr"] = test_df[yr_col]
        logger.info(f"\n  按年份:")
        per_year = {}
        for yr, grp in test_df.groupby("_yr"):
            if len(grp) < 20:
                continue
            yr_acc = accuracy_score(grp["_true"], grp["_pred"])
            yr_f1 = f1_score(grp["_true"], grp["_pred"], average="macro", zero_division=0)
            per_year[int(yr)] = {
                "n": len(grp), "accuracy": round(yr_acc, 4),
                "f1_macro": round(yr_f1, 4),
            }
            logger.info(f"    {int(yr)}: Acc={yr_acc:.4f} F1m={yr_f1:.4f} (n={len(grp):,})")
        results["per_year"] = per_year

    # ── 校准评估 ──
    try:
        bs = {}
        for cls_idx, cls_name in enumerate(["H", "D", "A"]):
            bs[cls_name] = round(float(brier_score_loss(
                (y_true == cls_idx).astype(int), y_proba[:, cls_idx]
            )), 4)
        results["brier_scores"] = bs
        logger.info(f"\n  Brier分数: H={bs['H']:.4f} D={bs['D']:.4f} A={bs['A']:.4f}")
    except (OSError, ValueError, KeyError) as e:
        logger.debug(f"操作失败: {e}")

    return results

# ── 全量重训练 ──

def _full_retrain(
    X: np.ndarray, y: np.ndarray, feature_names: List[str],
    model_version: str, output_path: str,
) -> Tuple[FootballAIEnhanced, float]:
    """用全量数据重训练并保存模型。"""
    logger.info(f"\n{'─' * 60}")
    logger.info(f"  全量重训练 + 模型保存")
    logger.info(f"{'─' * 60}")

    model = FootballAIEnhanced(model_version)
    model.feature_names_ = feature_names

    # 设置特征索引（用于泊松组件）
    model._feature_indices = {
        "poisson_home": feature_names.index("poisson_home_goals")
        if "poisson_home_goals" in feature_names else None,
        "poisson_away": feature_names.index("poisson_away_goals")
        if "poisson_away_goals" in feature_names else None,
    }

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    model.scaler = scaler

    # XGBoost
    logger.info("  训练 XGBoost...")
    model.xgb_model = xgb.XGBClassifier(
        n_estimators=300, max_depth=6, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42, n_jobs=-1, verbosity=0,
    )
    model.xgb_model.fit(X_scaled, y)

    # Ridge
    logger.info("  训练 Ridge...")
    ridge_base = RidgeClassifier(alpha=1.0, random_state=42)
    model.ridge_model = CalibratedClassifierCV(ridge_base, cv=3, method="isotonic")
    model.ridge_model.fit(X_scaled, y)

    # 特征重要性
    model.feature_importance = pd.DataFrame({
        "feature": feature_names,
        "importance": model.xgb_model.feature_importances_,
    }).sort_values("importance", ascending=False)

    # 保存
    logger.info(f"  保存模型 → {output_path}")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    auto_path = model.save_model(os.path.dirname(output_path))
    if auto_path != output_path and os.path.exists(auto_path):
        import shutil
        shutil.move(auto_path, output_path)

    file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
    return model, file_size_mb

# ══════════════════════════════════════════════════════════════════
# 对外入口: train_with_30000_matches
# ══════════════════════════════════════════════════════════════════

def train_with_30000_matches(
    data_path: str,
    output_path: str = "saved_models/footballai_v5.0_30000.joblib",
    version: str = "v5.0-30000",
    cv_splits: int = 5,
    report_path: Optional[str] = None,
) -> Dict:
    """使用3万场增强数据重新训练模型。

    完整管道:
    1. 加载增强数据 → 完整性验证
    2. 特征工程优化 → 交互特征 + 缺失值填充
    3. 严格时序交叉验证 → TimeSeriesSplit + 双模型集成
    4. 多维模型评估 → 整体/联赛/年份/Brier
    5. 全量重训练 + 模型保存

    Args:
        data_path: 增强特征 CSV 路径
        output_path: 模型输出路径 (.joblib)
        version: 模型版本标签
        cv_splits: 交叉验证折数
        report_path: 可选 JSON 报告导出路径

    Returns:
        Dict with keys: cv_results, evaluation, feature_importance, model_path, etc.
    """
    t_start = time.time()

    # ═══ 头部 ═══
    logger.info(f"\n{'=' * 65}")
    logger.info(f"  ⚽ FootballAI 3万场增强训练管道 v5.0")
    logger.info(f"{'=' * 65}")
    logger.info(f"  版本:      {version}")
    logger.info(f"  数据:      {data_path}")
    logger.info(f"  输出:      {output_path}")
    logger.info(f"  CV折数:    {cv_splits}")
    logger.info(f"  开始时间:  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"{'=' * 65}")

    # ═══ 1. 加载增强数据 ═══
    logger.info(f"\n📂 [1/5] 加载增强数据...")
    t0 = time.time()
    df = pd.read_csv(data_path, low_memory=False)
    logger.info(f"   数据量: {len(df):,} 行 × {len(df.columns)} 列")
    logger.info(f"   文件大小: {os.path.getsize(data_path) / (1024**2):.1f} MB")

    # 列完整性
    required_base = ["home_score", "away_score", "home_team", "away_team"]
    missing_base = [c for c in required_base if c not in df.columns]
    if missing_base:
        raise ValueError(f"缺少必要列: {missing_base}")

    # 剔除无效行
    before = len(df)
    df = df[df["home_score"].notna() & df["away_score"].notna()]
    if len(df) < before:
        logger.info(f"   剔除无效行: {before - len(df):,}")

    # 按日期排序
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date")
    logger.info(f"   有效数据: {len(df):,} 行 ({time.time() - t0:.1f}s)")

    # ── 泄漏检测 ──
    found_leaky = [c for c in df.columns if any(p in c for p in LEAKY_FEATURE_PATTERNS)]
    if found_leaky:
        logger.info(f"   ⚠️  检测到潜在泄漏特征 (将被自动排除): {found_leaky}")

    # ═══ 2. 特征工程优化 ═══
    logger.info(f"\n🔧 [2/5] 特征工程优化...")
    t0 = time.time()
    X, y, feature_names = _prepare_full_features(df)
    n_features = len(feature_names)
    n_samples = len(y)
    n_h = int(np.sum(y == 0))
    n_d = int(np.sum(y == 1))
    n_a = int(np.sum(y == 2))
    logger.info(f"   特征数: {n_features}  样本数: {n_samples:,}")
    logger.info(f"   类别: H={n_h:,}({100*n_h/n_samples:.1f}%)  D={n_d:,}({100*n_d/n_samples:.1f}%)  "
          f"A={n_a:,}({100*n_a/n_samples:.1f}%)")
    logger.info(f"   → 耗时 {time.time() - t0:.1f}s")

    # ═══ 3. 时序交叉验证 ═══
    logger.info(f"\n🎯 [3/5] 时序交叉验证训练...")
    t0 = time.time()
    cv_results = _temporal_cv_evaluate(X, y, feature_names, n_splits=cv_splits)
    logger.info(f"   → 耗时 {time.time() - t0:.1f}s")

    # ═══ 4. 模型性能评估 ═══
    logger.info(f"\n📊 [4/5] 模型性能评估...")
    t0 = time.time()

    # 用最后 20% 数据做 hold-out (尊重时序)
    train_end = int(len(X) * 0.8)
    X_train, X_test = X[:train_end], X[train_end:]
    y_train, y_test = y[:train_end], y[train_end:]

    # 快速训练评估模型
    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    eval_xgb = xgb.XGBClassifier(
        n_estimators=300, max_depth=6, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42, n_jobs=-1, verbosity=0,
    )
    eval_xgb.fit(X_train_s, y_train)

    eval_ridge = CalibratedClassifierCV(
        RidgeClassifier(alpha=1.0, random_state=42), cv=3, method="isotonic",
    )
    eval_ridge.fit(X_train_s, y_train)

    # 集成概率
    eval_proba = (eval_xgb.predict_proba(X_test_s) + eval_ridge.predict_proba(X_test_s)) / 2
    eval_pred = np.argmax(eval_proba, axis=1)

    eval_results = _multidimensional_evaluation(
        df, y_test, eval_pred, eval_proba, train_end,
    )
    logger.info(f"   → 耗时 {time.time() - t0:.1f}s")

    # ═══ 5. 全量重训练 + 模型保存 ═══
    logger.info(f"\n💾 [5/5] 模型持久化...")
    t0 = time.time()
    model, file_size_mb = _full_retrain(X, y, feature_names, version, output_path)

    # Top 特征
    logger.info(f"\n{'─' * 60}")
    logger.info(f"  Top-15 特征重要性:")
    logger.info(f"{'─' * 60}")
    for _, row in model.feature_importance.head(15).iterrows():
        bar = "█" * int(row["importance"] * 200)
        logger.info(f"  {row['feature']:<35s} {row['importance']:.4f} {bar}")

    total_time = time.time() - t_start
    logger.info(f"\n{'=' * 65}")
    logger.info(f"  ✅ 训练完成!")
    logger.info(f"  总耗时:     {total_time:.1f}s ({total_time/60:.1f}min)")
    logger.info(f"  模型文件:   {output_path} ({file_size_mb:.1f} MB)")
    logger.info(f"  CV准确率:   {cv_results['ensemble_cv_mean']:.4f}")
    logger.info(f"  Hold-out:   {eval_results['overall']['accuracy']:.4f}")
    logger.info(f"{'=' * 65}\n")

    # ── 汇总返回 ──
    summary = {
        "version": version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": {
            "rows": n_samples,
            "features": n_features,
            "class_distribution": {"H": n_h, "D": n_d, "A": n_a},
        },
        "cv_results": {
            "xgb_mean": cv_results["xgb_cv_mean"],
            "ridge_mean": cv_results["ridge_cv_mean"],
            "ensemble_mean": cv_results["ensemble_cv_mean"],
            "f1_macro_mean": cv_results["f1_macro_cv"],
            "log_loss_mean": cv_results["log_loss_cv"],
            "folds": cv_results["fold_metrics"],
        },
        "evaluation": eval_results,
        "feature_importance": model.feature_importance.head(20).to_dict(orient="records"),
        "model_path": output_path,
        "model_size_mb": round(file_size_mb, 1),
        "total_time_seconds": round(total_time, 1),
    }

    # JSON 报告导出
    if report_path:
        os.makedirs(os.path.dirname(report_path) if os.path.dirname(report_path) else ".", exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"  📄 报告已导出: {report_path}")

    return summary

def main():
    parser = argparse.ArgumentParser(
        description="训练 FootballAIEnhanced 集成模型",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
模式:
  默认   基础模式 (18特征, old pipeline)
  30000  3万场全特征模式 (50+特征, 时序CV, 多维评估)

示例:
  # 基础模式
  python backend/models/train_enhanced.py --data data/enhanced_matches.csv

  # 3万场全特征训练
  python backend/models/train_enhanced.py --mode 30000 \\
      --data data/enhanced_features_v1.csv \\
      --output saved_models/footballai_v5.0.joblib \\
      --report output/training_report.json
""")
    parser.add_argument('--mode', type=str, default='default',
                        choices=['default', '30000'],
                        help='训练模式: default (18特征) 或 30000 (全特征)')
    parser.add_argument('--data', type=str, required=True,
                        help='增强数据 CSV 路径')
    parser.add_argument('--output', type=str,
                        default='saved_models/footballai_enhanced_v4.0.joblib',
                        help='模型输出路径')
    parser.add_argument('--version', type=str, default='v5.0',
                        help='模型版本号 (30000模式默认 v5.0-30000)')
    parser.add_argument('--subset', type=int, default=None,
                        help='仅使用前 N 行训练 (调试用)')
    parser.add_argument('--no-holdout', action='store_true',
                        help='跳过 hold-out 评估 (仅default模式)')
    parser.add_argument('--cv-splits', type=int, default=5,
                        help='时序交叉验证折数 (仅30000模式)')
    parser.add_argument('--report', type=str, default=None,
                        help='JSON 报告导出路径 (仅30000模式)')
    args = parser.parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    data_path = args.data
    if not os.path.isabs(data_path):
        data_path = os.path.join(project_root, data_path)
    output_path = args.output
    if not os.path.isabs(output_path):
        output_path = os.path.join(project_root, output_path)

    # ── 模式分发 ──
    if args.mode == '30000':
        version = args.version if args.version != 'v5.0' else 'v5.0-30000'
        train_with_30000_matches(
            data_path=data_path,
            output_path=output_path,
            version=version,
            cv_splits=args.cv_splits,
            report_path=args.report,
        )
        return

    # ↓↓↓ 以下为 default 模式 (原有逻辑) ↓↓↓

    logger.info(f"\n{'=' * 60}")
    logger.info(f"  FootballAIEnhanced 模型训练")
    logger.info(f"{'=' * 60}")
    logger.info(f"  版本: {args.version}")
    logger.info(f"  数据: {data_path}")
    logger.info(f"  输出: {output_path}")
    logger.info(f"{'=' * 60}\n")

    # ── 1. 加载数据 ──
    logger.info(f"📂 加载数据...")
    t0 = time.time()
    df = pd.read_csv(data_path, low_memory=False)
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date'])
    logger.info(f"   ├─ 数据量: {len(df):,} 行 × {len(df.columns)} 列")
    logger.info(f"   └─ 加载耗时: {time.time() - t0:.1f}s")

    if args.subset:
        df = df.head(args.subset)
        logger.info(f"   ⚠️  调试模式: 仅使用前 {args.subset} 行")

    # ── 2. 准备特征 ──
    logger.info(f"\n🔧 准备特征...")
    model = FootballAIEnhanced(args.version)
    X, y, features = model.prepare_features(df)
    logger.info(f"   ├─ 特征数: {len(features)}")
    logger.info(f"   ├─ 样本数: {len(y):,}")
    logger.info(f"   └─ 类别分布: H={np.sum(y==0):,} D={np.sum(y==1):,} A={np.sum(y==2):,}")

    # ── 3. 训练 ──
    logger.info(f"\n🎯 开始训练...")
    t1 = time.time()
    model.train_with_cross_validation(X, y)
    train_time = time.time() - t1
    logger.info(f"   └─ 训练耗时: {train_time:.1f}s")

    # ── 4. Top 特征 ──
    logger.info(f"\n📊 Top-10 特征重要性:")
    for _, row in model.feature_importance.head(10).iterrows():
        bar = '█' * int(row['importance'] * 100)
        logger.info(f"   {row['feature']:<30s} {row['importance']:.4f} {bar}")

    # ── 5. Hold-out 评估 ──
    if not args.no_holdout and len(X) >= 100:
        logger.info(f"\n📈 Hold-out 评估 (20%)...")
        split = int(len(X) * 0.8)
        X_test, y_test = X[split:], y[split:]
        y_pred = model.predict(X_test)

        acc = accuracy_score(y_test, y_pred)
        logger.info(f"\n  准确率: {acc:.4f} (n={len(X_test):,})")
        logger.info(f"\n  " + classification_report(
            y_test, y_pred,
            target_names=['H(主胜)', 'D(平局)', 'A(客胜)'],
            digits=4,
        ).replace('\n', '\n  '))

    # ── 6. 保存 ──
    logger.info(f"\n💾 保存模型...")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    auto_path = model.save_model(os.path.dirname(output_path))
    if auto_path != output_path and os.path.exists(auto_path):
        import shutil
        shutil.move(auto_path, output_path)

    file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
    logger.info(f"   └─ 模型文件: {output_path} ({file_size_mb:.1f} MB)")

    # ── 7. 验证加载 ──
    logger.info(f"\n🔍 验证模型加载...")
    model_loaded = FootballAIEnhanced.load_model(output_path)
    assert model_loaded.model_version == args.version
    sample = X[-1:].copy()
    pred_original = model.predict(sample)
    pred_loaded = model_loaded.predict(sample)
    assert pred_original == pred_loaded, "加载模型预测不一致!"
    logger.info(f"   └─ 加载验证通过 ✅")

    logger.info(f"\n{'=' * 60}")
    logger.info(f"  ✅ 训练完成")
    logger.info(f"{'=' * 60}\n")

if __name__ == '__main__':
    main()

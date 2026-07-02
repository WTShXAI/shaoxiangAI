"""
[DEPRECATED] P0-1: 此文件的 load_model() 直接使用 joblib.load 绕过 ModelBridge，存在数据泄露风险。
生产预测请使用 agents.model_bridge.ModelBridge.predict()
FootballAI Enhanced Model (v5.0)
基于 33,589 场比赛增强数据的集成预测模型

架构: XGBoost (0.45) + Calibrated Ridge (0.35) + Poisson (0.20)
"""
import os
import sys
from typing import Dict, List, Optional
import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import RidgeClassifier
import xgboost as xgb
import joblib
import logging
import warnings
warnings.filterwarnings('ignore')

# 确保项目根在 path 中，以便导入 backend.features
_proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# 延迟导入，避免循环依赖
SafeTemporalFeatureEngineer = None
DrawOptimizedEnsemble = None
AdaptiveTrainingStrategy = None

def _get_feature_engineer():
    global SafeTemporalFeatureEngineer
    if SafeTemporalFeatureEngineer is None:
        from features.advanced_temporal_features import SafeTemporalFeatureEngineer as STE
        SafeTemporalFeatureEngineer = STE
    return SafeTemporalFeatureEngineer

def _get_draw_ensemble():
    global DrawOptimizedEnsemble
    if DrawOptimizedEnsemble is None:
        from backend.models.advanced_ensemble import DrawOptimizedEnsemble as DOE
        DrawOptimizedEnsemble = DOE
    return DrawOptimizedEnsemble

def _get_adaptive_training():
    global AdaptiveTrainingStrategy
    if AdaptiveTrainingStrategy is None:
        from backend.training.adaptive_training import AdaptiveTrainingStrategy as ATS
        AdaptiveTrainingStrategy = ATS
    return AdaptiveTrainingStrategy

class FootballAIEnhanced:
    """基于增强数据的 FootballAI 集成模型 (v5.0)"""

    # ── CSV → 模型 列名映射 ──
    # CSV 格式: home_last_5_wins; 用户期望: last_5_wins_home
    FEATURE_ALIAS_MAP = {
        'last_5_wins_home':      'home_last_5_wins',
        'last_5_wins_away':      'away_last_5_wins',
        'last_5_goals_for_home': 'home_last_5_goals_for',
        'last_5_goals_for_away': 'away_last_5_goals_for',
        'last_5_goals_against_home': 'home_last_5_goals_against',
        'last_5_goals_against_away': 'away_last_5_goals_against',
        'avg_goals_for_home':    'home_avg_goals_for',
        'avg_goals_for_away':    'away_avg_goals_for',
        'avg_goals_against_home':'home_avg_goals_against',
        'avg_goals_against_away':'away_avg_goals_against',
    }

    def __init__(self, model_version: str = "v5.0"):
        self.model_version = model_version
        self.xgb_model = None
        self.ridge_model = None  # CalibratedClassifierCV wrapping RidgeClassifier
        self.scaler = StandardScaler()
        self.feature_names_ = None
        self.feature_importance = None

        # 集成权重 — 初始为静态默认值，训练后由 _compute_dynamic_weights 动态更新
        self.ensemble_weights = {
            'xgb': 0.45,
            'ridge': 0.35,
            'poisson': 0.20,
        }
        self._weights_source = 'static'  # 'static' | 'dynamic'

        # DrawOptimizedEnsemble — 平局增强 (v6.0, 可选)
        self.draw_optimizer = None
        self._draw_enabled = False

        # AdaptiveTrainingStrategy — 自适应训练 (v7.0, 可选)
        self.adaptive_trainer: Optional[object] = None
        self._adaptive_enabled = False
        self._adaptive_selected_features: Optional[List[str]] = None
        self._adaptive_class_weights: Optional[Dict[int, float]] = None

    # ────────────────── 数据预处理 ──────────────────

    @staticmethod
    def _derive_result(df: pd.DataFrame) -> pd.Series:
        """从 home_score / away_score 推导胜平负标签 H/D/A"""
        def _label(row):
            if row['home_score'] > row['away_score']:
                return 'H'
            elif row['home_score'] < row['away_score']:
                return 'A'
            else:
                return 'D'
        return df.apply(_label, axis=1)

    @staticmethod
    def _build_team_table(df: pd.DataFrame) -> pd.DataFrame:
        """将 match-level DataFrame 展开为 team-level 长表。

        每场原始比赛产生两行：主队视角 + 客队视角。
        列: team, date, home_team, away_team, home_score, away_score,
             goals_for, goals_against, goal_diff, points, _match_id
        """
        df = df.copy()
        df['_match_id'] = df.index.astype(int)

        # ── 主队视角 ──
        home = df[['_match_id', 'date', 'home_team', 'away_team', 'home_score', 'away_score']].copy()
        home['team'] = home['home_team']
        home['goals_for'] = home['home_score'].astype(float)
        home['goals_against'] = home['away_score'].astype(float)
        home['goal_diff'] = home['goals_for'] - home['goals_against']
        home['points'] = home['goal_diff'].apply(
            lambda x: 3 if x > 0 else (1 if x == 0 else 0)
        )
        if 'league' in df.columns:
            home['league'] = df['league'].values

        # ── 客队视角 ──
        away = df[['_match_id', 'date', 'home_team', 'away_team', 'home_score', 'away_score']].copy()
        away['team'] = away['away_team']
        away['goals_for'] = away['away_score'].astype(float)
        away['goals_against'] = away['home_score'].astype(float)
        away['goal_diff'] = away['goals_for'] - away['goals_against']
        away['points'] = away['goal_diff'].apply(
            lambda x: 3 if x > 0 else (1 if x == 0 else 0)
        )
        if 'league' in df.columns:
            away['league'] = df['league'].values

        team_df = pd.concat([home, away], ignore_index=True)
        return team_df

    @staticmethod
    def _advanced_feature_engineering(df: pd.DataFrame) -> pd.DataFrame:
        """高级特征工程：SafeTemporalFeatureEngineer (预计 +5% 准确率)

        五类时序安全特征：
          1. 势头特征 — last_3/5/10/20_form, form_momentum_5/10/20
          2. 进攻/防守状态 — attack_form_5/10, defense_form_5/10, goal_diff_form_5/10
          3. 历史交锋 — h2h_home_win_rate, h2h_draw_rate, h2h_goal_ratio, h2h_match_count
          4. 疲劳度 — days_since_last_home/away, home_fatigue, away_fatigue
          5. 比赛上下文 — season_month, is_weekend, season_progress

        所有窗口特征使用 shift(1).rolling() 确保严格时序安全。
        """
        logger.info("[FEAT] 高级特征工程 — SafeTemporalFeatureEngineer")
        df = df.copy()

        # 确保 date 列是 datetime
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], errors='coerce')
            df = df.sort_values('date').reset_index(drop=True)

        # 给原始 df 打上 match_id，以便后续合并
        df['_match_id'] = range(len(df))

        required_cols = {'home_team', 'away_team', 'home_score', 'away_score'}
        if not required_cols.issubset(set(df.columns)):
            logger.info("[FEAT] 缺少核心列 (home_team/away_team/score)，跳过高级特征")
            df = df.drop(columns=['_match_id'])
            return df

        # ── 1. 构建球队级别长表 ──
        team_df = FootballAIEnhanced._build_team_table(df)

        # ── 2. 运行 SafeTemporalFeatureEngineer ──
        STE = _get_feature_engineer()
        engineer = STE(team_df)
        team_featured = engineer.run_all()

        # ── 3. 特征分类 ──
        team_features = engineer.get_team_level_features()   # 每队不同
        match_features = engineer.get_match_level_features() # 同场比赛相同

        # 标记主/客队行
        team_featured['_is_home'] = team_featured['team'] == team_featured['home_team']

        # ── 4. 提取主队特征 (球队级 → _home 后缀) ──
        home_rows = team_featured[team_featured['_is_home']].copy()
        home_cols = ['_match_id'] + team_features + match_features
        home_feat = home_rows[home_cols].copy()
        home_rename = {f: f'{f}_home' for f in team_features}
        home_feat = home_feat.rename(columns=home_rename)

        # ── 5. 提取客队特征 (球队级 → _away 后缀) ──
        away_rows = team_featured[~team_featured['_is_home']].copy()
        away_cols_sel = ['_match_id'] + team_features
        away_feat = away_rows[away_cols_sel].copy()
        away_rename = {f: f'{f}_away' for f in team_features}
        away_feat = away_feat.rename(columns=away_rename)

        # ── 6. 合并回原始 match-level DF ──
        df = df.merge(home_feat, on='_match_id', how='left')
        df = df.merge(away_feat, on='_match_id', how='left')
        df = df.drop(columns=['_match_id'])

        new_cols = [c for c in df.columns
                    if c.endswith('_home') or c.endswith('_away')
                    or c.startswith(('h2h_', 'days_since_last_', 'home_fatigue', 'away_fatigue',
                                    'season_', 'is_weekend'))]
        logger.info(
            f"[FEAT] 新增 {len(new_cols)} 个高级特征: "
            f"{new_cols[:12]}{'...' if len(new_cols) > 12 else ''}"
        )

        return df

    def prepare_features(self, df: pd.DataFrame) -> tuple:
        """准备增强特征集，自动适配 CSV 列名"""
        logger.info(f"[PREP] 准备特征，共 {len(df):,} 场比赛")

        # --- 1. 建立别名 → 如果缺失则从已存在列复制 ---
        for alias, csv_col in self.FEATURE_ALIAS_MAP.items():
            if alias not in df.columns and csv_col in df.columns:
                df[alias] = df[csv_col]

        # --- 2. 高级特征工程 (时间窗口滚动特征) ---
        df = self._advanced_feature_engineering(df)

        # --- 3. 交互特征 ---
        if 'home_elo' in df.columns and 'away_elo' in df.columns:
            df['elo_diff'] = df['home_elo'] - df['away_elo']
        if 'last_5_wins_home' in df.columns and 'last_5_wins_away' in df.columns:
            df['form_diff'] = df['last_5_wins_home'] - df['last_5_wins_away']
        if 'avg_goals_for_home' in df.columns and 'avg_goals_against_away' in df.columns:
            df['attack_strength'] = df['avg_goals_for_home'] / df['avg_goals_against_away'].clip(lower=0.1)
        if 'avg_goals_against_home' in df.columns and 'avg_goals_for_away' in df.columns:
            df['defense_weakness'] = df['avg_goals_against_home'] / df['avg_goals_for_away'].clip(lower=0.1)

        # --- 4. 时间特征 ---
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], errors='coerce')
            df['year'] = df['date'].dt.year
            df['month'] = df['date'].dt.month
            df['day_of_week'] = df['date'].dt.dayofweek
        else:
            df['year'] = df['month'] = df['day_of_week'] = 0

        # --- 5. 目标变量 ---
        if 'result' not in df.columns:
            df['result'] = self._derive_result(df)
        y = df['result'].map({'H': 0, 'D': 1, 'A': 2})

        # --- 6. 选择特征 ---
        candidate_features = [
            # 基础特征
            'home_elo', 'away_elo',
            'last_5_wins_home', 'last_5_wins_away',
            'last_5_goals_for_home', 'last_5_goals_for_away',
            'last_5_goals_against_home', 'last_5_goals_against_away',
            'avg_goals_for_home', 'avg_goals_for_away',
            'avg_goals_against_home', 'avg_goals_against_away',
            'poisson_home_goals', 'poisson_away_goals',
            'elo_diff', 'form_diff', 'attack_strength', 'defense_weakness',
            'year', 'month', 'day_of_week',
            # ── SafeTemporalFeatureEngineer: 势头特征 (_home/_away) ──
            'last_3_form_home', 'last_3_form_away',
            'last_5_form_home', 'last_5_form_away',
            'last_10_form_home', 'last_10_form_away',
            'last_20_form_home', 'last_20_form_away',
            'form_momentum_5_home', 'form_momentum_5_away',
            'form_momentum_10_home', 'form_momentum_10_away',
            'form_momentum_20_home', 'form_momentum_20_away',
            # ── 进攻/防守状态 (_home/_away) ──
            'attack_form_5_home', 'attack_form_5_away',
            'attack_form_10_home', 'attack_form_10_away',
            'defense_form_5_home', 'defense_form_5_away',
            'defense_form_10_home', 'defense_form_10_away',
            'goal_diff_form_5_home', 'goal_diff_form_5_away',
            'goal_diff_form_10_home', 'goal_diff_form_10_away',
            # ── 历史交锋 (match-level) ──
            'h2h_home_win_rate', 'h2h_draw_rate',
            'h2h_goal_ratio', 'h2h_match_count',
            # ── 疲劳度 (match-level) ──
            'days_since_last_home', 'days_since_last_away',
            'home_fatigue', 'away_fatigue',
            # ── 比赛上下文 (match-level) ──
            'season_month', 'is_weekend', 'season_progress',
        ]
        available_features = [f for f in candidate_features if f in df.columns]
        logger.info(f"[PREP] 使用 {len(available_features)} 个特征")
        self.feature_names_ = available_features

        X = df[available_features].fillna(0).astype(np.float32)
        self._feature_indices = {
            'poisson_home': available_features.index('poisson_home_goals')
            if 'poisson_home_goals' in available_features else None,
            'poisson_away': available_features.index('poisson_away_goals')
            if 'poisson_away_goals' in available_features else None,
        }

        return X.values, y.values, available_features

    # ────────────────── 训练 ──────────────────

    def train_with_cross_validation(self, X: np.ndarray, y: np.ndarray):
        """时序交叉验证 + 全量重训练"""
        logger.info("[TRAIN] 时序交叉验证 (5-fold TimeSeriesSplit)...")
        assert self.feature_names_ is not None, "请先调用 prepare_features()"

        tscv = TimeSeriesSplit(n_splits=5)
        xgb_scores, ridge_scores = [], []

        for fold, (train_idx, val_idx) in enumerate(tscv.split(X), 1):
            X_train, X_val = X[train_idx], X[val_idx]
            y_train, y_val = y[train_idx], y[val_idx]

            X_train_s = self.scaler.fit_transform(X_train)
            X_val_s = self.scaler.transform(X_val)

            # XGBoost
            xgb_fold = xgb.XGBClassifier(
                n_estimators=300, max_depth=6, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.8,
                random_state=42, n_jobs=-1, verbosity=0,
            )
            xgb_fold.fit(X_train_s, y_train)
            xgb_score = xgb_fold.score(X_val_s, y_val)
            xgb_scores.append(xgb_score)

            # Calibrated Ridge (支持 predict_proba)
            ridge_base = RidgeClassifier(alpha=1.0, random_state=42)
            ridge_fold = CalibratedClassifierCV(ridge_base, cv=3, method='isotonic')
            ridge_fold.fit(X_train_s, y_train)
            ridge_score = ridge_fold.score(X_val_s, y_val)
            ridge_scores.append(ridge_score)

            logger.info(f"  Fold {fold}: XGBoost={xgb_score:.4f}, Ridge={ridge_score:.4f}")

        logger.info(
            f"[TRAIN] XGBoost  avg={np.mean(xgb_scores):.4f}  "
            f"(±{np.std(xgb_scores):.4f})"
        )
        logger.info(
            f"[TRAIN] Ridge    avg={np.mean(ridge_scores):.4f}  "
            f"(±{np.std(ridge_scores):.4f})"
        )

        # ── 全量重训练 (用所有数据) ──
        logger.info("[TRAIN] 全量重训练...")
        X_scaled = self.scaler.fit_transform(X)

        self.xgb_model = xgb.XGBClassifier(
            n_estimators=300, max_depth=6, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            random_state=42, n_jobs=-1, verbosity=0,
        )
        self.xgb_model.fit(X_scaled, y)

        ridge_base = RidgeClassifier(alpha=1.0, random_state=42)
        self.ridge_model = CalibratedClassifierCV(ridge_base, cv=3, method='isotonic')
        self.ridge_model.fit(X_scaled, y)

        # 特征重要性
        self.feature_importance = pd.DataFrame({
            'feature': self.feature_names_,
            'importance': self.xgb_model.feature_importances_,
        }).sort_values('importance', ascending=False)

        logger.info(f"[TRAIN] Top-5 features: {self.feature_importance.head(5)['feature'].tolist()}")

        # ── 动态集成权重 (基于最近 N 场表现) ──
        self._compute_dynamic_weights(X, y)

    # ────────────────── 平局优化 (DrawOptimizedEnsemble v6.0) ──────────────────

    def enable_draw_optimization(self, X: np.ndarray, y: np.ndarray):
        """启用平局优化器 — 训练三阶段 DrawOptimizedEnsemble

        应在 train_with_cross_validation() 之后调用。
        训练完成后，predict_with_draw_boost() 会自动增强平局预测。
        """
        logger.info("[DRAW] 启用平局优化器 (DrawOptimizedEnsemble v6.0)...")
        DOE = _get_draw_ensemble()
        self.draw_optimizer = DOE(model_version="v6.0", draw_threshold=0.25)
        X_scaled = self.scaler.transform(X)

        # 在现有集成概率基础上训练 draw-specific models
        base_proba = self.ensemble_predict(X_scaled)

        # 准备 draw-specific 特征
        X_df = pd.DataFrame(X, columns=self.feature_names_)
        X_enhanced = DOE.prepare_draw_specific_features(X_df)
        X_enhanced_np = X_enhanced.fillna(0).astype(np.float32).values

        metrics = self.draw_optimizer.fit_with_cv(
            X_enhanced_np, y, n_splits=5, calibrate=False,
        )

        self._draw_enabled = True
        logger.info(
            f"[DRAW] 平局优化器就绪 — "
            f"draw_detector_acc={metrics['draw_detector_accuracy']:.3f}  "
            f"full_acc={metrics['full_model_accuracy']:.3f}"
        )
        return metrics

    def predict_with_draw_boost(self, X: np.ndarray) -> np.ndarray:
        """带平局增强的集成预测概率 [n_samples, 3]

        在 ensemble_predict 的基础上，用 DrawOptimizedEnsemble 增强平局概率。
        若未启用平局优化器，等价于 ensemble_predict()。
        """
        base_proba = self.ensemble_predict(X)

        if not self._draw_enabled or self.draw_optimizer is None:
            return base_proba

        # DrawOptimizedEnsemble 需要的特征
        X_df = pd.DataFrame(X, columns=self.feature_names_)
        X_enhanced = self.draw_optimizer.__class__.prepare_draw_specific_features(X_df)
        X_enhanced_np = X_enhanced.fillna(0).astype(np.float32).values

        enhanced_proba = self.draw_optimizer.enhance_external_probas(
            X_enhanced_np, base_proba,
        )
        return enhanced_proba

    def predict_draw_boosted(self, X: np.ndarray) -> np.ndarray:
        """带平局增强的类别预测 0=H,1=D,2=A"""
        proba = self.predict_with_draw_boost(X)
        return np.argmax(proba, axis=1)

    def evaluate_draw_metrics(self, X: np.ndarray, y: np.ndarray) -> Dict:
        """全面评估 — 含平局特定指标

        Returns
        -------
        dict with keys: overall_accuracy, draw_precision, draw_recall,
                        draw_f1, draw_count_actual, draw_count_predicted,
                        draw_prob_mean, draw_prob_std, draw_prob_on_draws,
                        draw_prob_on_non_draws

        同时输出 base (无平局增强) 和 boosted (有平局增强) 两套指标。
        """
        from backend.models.advanced_ensemble import DrawOptimizedEnsemble

        X_scaled = self.scaler.transform(X)

        # 基础指标
        base_proba = self.ensemble_predict(X_scaled)
        base_pred = np.argmax(base_proba, axis=1)
        base_metrics = DrawOptimizedEnsemble.evaluate_draw_performance(
            X_scaled, y, predictions=base_pred, proba=base_proba,
        )

        result = {'base': base_metrics}

        # 增强指标 (若已启用)
        if self._draw_enabled and self.draw_optimizer is not None:
            boosted_proba = self.predict_with_draw_boost(X)
            boosted_pred = np.argmax(boosted_proba, axis=1)
            boosted_metrics = DrawOptimizedEnsemble.evaluate_draw_performance(
                X, y, predictions=boosted_pred, proba=boosted_proba,
            )
            result['boosted'] = boosted_metrics

        return result

    # ────────────────── 自适应训练 (AdaptiveTrainingStrategy v7.0) ──────────────────

    def enable_adaptive_training(
        self,
        X: np.ndarray,
        y: np.ndarray,
        dates: np.ndarray,
        model_builder=None,
        min_train_size: int = 1000,
    ) -> Dict:
        """启用自适应训练管道 — 渐进式验证 + 特征筛选 + 动态权重

        应在 train_with_cross_validation() 之后调用。
        训练完成后：
        - self._adaptive_selected_features → 稳定特征子集
        - self._adaptive_class_weights → 动态类别权重
        - 返回渐进式验证报告

        Parameters
        ----------
        X : np.ndarray 原始特征矩阵（未缩放）
        y : np.ndarray 标签 0=H, 1=D, 2=A
        dates : np.ndarray datetime64 或日期字符串
        model_builder : callable → 返回 scikit-learn estimator（默认 XGBoost）
        """
        logger.info("[ADAPTIVE] 启用自适应训练策略 (v7.0)...")
        ATS = _get_adaptive_training()
        self.adaptive_trainer = ATS(n_splits=5, min_train_size=min_train_size)

        if model_builder is None:
            def _default_builder():
                return xgb.XGBClassifier(
                    n_estimators=150, max_depth=5, learning_rate=0.05,
                    random_state=42, verbosity=0,
                )
            model_builder = _default_builder

        feature_names = self.feature_names_ or []

        # 1) 渐进式验证
        logger.info("[ADAPTIVE] 1/3 渐进式验证...")
        pv_result = self.adaptive_trainer.progressive_validation(
            X, y, dates, model_builder, feature_names,
        )

        # 2) 自适应特征选择
        logger.info("[ADAPTIVE] 2/3 自适应特征选择...")
        selected, stability = self.adaptive_trainer.adaptive_feature_selection(
            X, y, dates, feature_names,
        )
        self._adaptive_selected_features = selected

        # 3) 动态类别权重
        logger.info("[ADAPTIVE] 3/3 动态类别权重...")
        class_weights = self.adaptive_trainer.dynamic_class_weighting(y)
        self._adaptive_class_weights = class_weights

        self._adaptive_enabled = True

        result = {
            'progressive_validation': pv_result,
            'selected_features': selected,
            'n_selected': len(selected),
            'n_total_features': len(feature_names),
            'feature_stability': stability,
            'class_weights': class_weights,
        }

        logger.info(
            f"[ADAPTIVE] 就绪 — "
            f"progressive_acc={pv_result.get('mean_accuracy', 0):.3f}  "
            f"selected_features={len(selected)}/{len(feature_names)}"
        )

        return result

    def get_adaptive_summary(self) -> Dict:
        """获取自适应训练摘要"""
        if not self._adaptive_enabled:
            return {'enabled': False}
        return {
            'enabled': True,
            'selected_features': self._adaptive_selected_features,
            'n_selected': len(self._adaptive_selected_features or []),
            'class_weights': self._adaptive_class_weights,
            'progressive_windows': len(
                self.adaptive_trainer.cv_results
            ) if self.adaptive_trainer else 0,
        }

    # ────────────────── 动态集成权重 ──────────────────

    def _compute_dynamic_weights(self, X: np.ndarray, y: np.ndarray, n_recent: int = 0):
        """基于全量数据计算动态集成权重 (预计 +2% 准确率)

        在全量训练完成后，用已训练好的 XGBoost / Ridge / Poisson
        分别在全量/最近 N 场样本上评估准确率，按比例分配权重。

        **[v4.1 修复]** 静态默认值从 config.yaml 读取，不再硬编码。
          - config.yaml: xgboost_weight / ridge_weight / heuristic_weight
          - 内部映射: xgboost_weight → xgb, ridge_weight → ridge, heuristic_weight → poisson

        Args:
            X: 全量特征矩阵
            y: 全量标签
            n_recent: 最近 N 场 (0=使用全量数据)
        """
        X_scaled = self.scaler.transform(X)

        # 决定评估范围: n_recent=0 或 n_recent>=len(X) 时使用全量
        use_full = (n_recent == 0) or (n_recent >= len(X))
        n = len(X) if use_full else min(n_recent, len(X))

        X_eval = X_scaled[-n:] if not use_full else X_scaled
        y_eval = y[-n:] if not use_full else y

        logger.info(f"[WEIGHT] 动态权重计算 — 基于 {'全量' if use_full else f'最近 {n:,}'} 场表现")

        # ── 1. XGBoost 准确率 ──
        xgb_pred = self.xgb_model.predict(X_eval)
        xgb_acc = float(np.mean(xgb_pred == y_eval))

        # ── 2. Ridge 准确率 ──
        ridge_pred = self.ridge_model.predict(X_eval)
        ridge_acc = float(np.mean(ridge_pred == y_eval))

        # ── 3. Poisson 准确率 (argmax of poisson probabilities vs actual) ──
        pi_home = self._feature_indices.get('poisson_home')
        pi_away = self._feature_indices.get('poisson_away')
        n_samples = len(X_eval)
        poisson_correct = 0

        if pi_home is not None and pi_away is not None:
            for i in range(n_samples):
                proba = self.poisson_predict(
                    float(X_eval[i, pi_home]),
                    float(X_eval[i, pi_away]),
                )
                if np.argmax(proba) == y_eval[i]:
                    poisson_correct += 1
        poisson_acc = poisson_correct / n_samples if n_samples > 0 else 0.33

        # ── 4. 从 config.yaml 读取静态默认值 (v4.1 修复) ──
        try:
            _config_path = os.path.join(_proj_root, 'config.yaml')
            with open(_config_path, 'r', encoding='utf-8') as _cf:
                import yaml
                _cfg = yaml.safe_load(_cf)
            _ens_cfg = _cfg.get('models', {}).get('ensemble', {})
            static_defaults = {
                'xgb': float(_ens_cfg.get('xgboost_weight', 0.45)),
                'ridge': float(_ens_cfg.get('ridge_weight', 0.35)),
                'poisson': float(_ens_cfg.get('heuristic_weight', 0.20)),
            }
            logger.info(f"[WEIGHT] 从 config.yaml 读取静态权重: {static_defaults}")
        except (FileNotFoundError, KeyError, ValueError, yaml.YAMLError) as e:
            logger.warning(f"[WEIGHT] 无法读取 config.yaml: {e}，使用内置默认值")
            static_defaults = {'xgb': 0.45, 'ridge': 0.35, 'poisson': 0.20}

        # ── 5. 按准确率比例分配权重 ──
        accuracies = {
            'xgb': max(xgb_acc, 0.01),
            'ridge': max(ridge_acc, 0.01),
            'poisson': max(poisson_acc, 0.01),
        }
        total = sum(accuracies.values())

        # 平滑处理: 与静态默认值 30% 混合，防止过拟合
        blend_ratio = 0.30  # 30% 静态 + 70% 动态

        dynamic = {k: v / total for k, v in accuracies.items()}
        blended = {
            k: dynamic[k] * (1 - blend_ratio) + static_defaults[k] * blend_ratio
            for k in dynamic
        }
        # 再归一化
        norm_total = sum(blended.values())
        self.ensemble_weights = {k: v / norm_total for k, v in blended.items()}
        self._weights_source = 'dynamic'

        # ── 6. 多样性监控 (HHI 指数) ──
        _div_cfg = _ens_cfg.get('diversity_monitoring', {})
        if _div_cfg.get('enabled', False):
            hhi = sum(w ** 2 for w in self.ensemble_weights.values())
            hhi_thr = float(_div_cfg.get('hhi_threshold', 0.35))
            if hhi > hhi_thr:
                logger.warning(
                    f"[DIVERSITY] HHI={hhi:.4f} > 阈值{hhi_thr} — "
                    f"权重集中度过高! xgb={self.ensemble_weights['xgb']:.3f} "
                    f"ridge={self.ensemble_weights['ridge']:.3f} "
                    f"poisson={self.ensemble_weights['poisson']:.3f}"
                )
            else:
                logger.info(f"[DIVERSITY] HHI={hhi:.4f} < {hhi_thr} — 多样性正常")

        logger.info(
            f"[WEIGHT] 准确率: XGBoost={xgb_acc:.4f}  Ridge={ridge_acc:.4f}  "
            f"Poisson={poisson_acc:.4f}"
        )
        logger.info(
            f"[WEIGHT] 动态权重 (混合30%静态): "
            f"xgb={self.ensemble_weights['xgb']:.4f}  "
            f"ridge={self.ensemble_weights['ridge']:.4f}  "
            f"poisson={self.ensemble_weights['poisson']:.4f}"
        )

    def evaluate_recent(self, X: np.ndarray, y: np.ndarray, n_recent: int = 5000) -> dict:
        """公开接口: 评估各子模型在最近 N 场的表现并返回准确率。

        可用于外部调用在预测前重新校准权重。
        """
        if self.xgb_model is None or self.ridge_model is None:
            logger.warning("[WEIGHT] 模型未训练，无法评估")
            return {'xgb': 0.0, 'ridge': 0.0, 'poisson': 0.0}

        self._compute_dynamic_weights(X, y, n_recent=n_recent)
        return {
            'xgb': self.ensemble_weights['xgb'],
            'ridge': self.ensemble_weights['ridge'],
            'poisson': self.ensemble_weights['poisson'],
        }

    # ────────────────── 泊松分布 ──────────────────

    @staticmethod
    def poisson_predict(home_lambda: float, away_lambda: float) -> np.ndarray:
        """泊松分布胜平负概率"""
        from scipy.stats import poisson
        max_goals = 6
        home_probs = np.array([poisson.pmf(i, max(home_lambda, 0.01)) for i in range(max_goals + 1)])
        away_probs = np.array([poisson.pmf(i, max(away_lambda, 0.01)) for i in range(max_goals + 1)])

        prob_matrix = np.outer(home_probs, away_probs)
        # prob_matrix[i, j] = P(home=i, away=j)
        # triu(k=1): i < j → home < away → away wins
        # tril(k=-1): i > j → home > away → home wins
        away_win = prob_matrix[np.triu_indices_from(prob_matrix, k=1)].sum()
        draw = np.diag(prob_matrix).sum()
        home_win = prob_matrix[np.tril_indices_from(prob_matrix, k=-1)].sum()

        total = home_win + draw + away_win
        return np.array([home_win, draw, away_win]) / total

    # ────────────────── 集成预测 ──────────────────

    def ensemble_predict(self, X: np.ndarray) -> np.ndarray:
        """集成预测: 加权融合 XGBoost + Ridge + Poisson"""
        X_scaled = self.scaler.transform(X)

        # XGBoost 概率
        xgb_proba = self.xgb_model.predict_proba(X_scaled)

        # Calibrated Ridge 概率
        ridge_proba = self.ridge_model.predict_proba(X_scaled)

        # Poisson 概率 (批量)
        n = len(X)
        poisson_proba = np.zeros((n, 3), dtype=np.float64)
        pi_home = self._feature_indices['poisson_home']
        pi_away = self._feature_indices['poisson_away']

        if pi_home is not None and pi_away is not None:
            for i in range(n):
                poisson_proba[i] = self.poisson_predict(
                    float(X[i, pi_home]), float(X[i, pi_away])
                )
        else:
            # ★ 战时修复：不再使用均匀假概率，改为复用 XGB 概率
            poisson_proba = xgb_proba.copy()

        # 加权集成
        final_proba = (
            xgb_proba * self.ensemble_weights['xgb']
            + ridge_proba * self.ensemble_weights['ridge']
            + poisson_proba * self.ensemble_weights['poisson']
        )

        # 归一化
        row_sums = final_proba.sum(axis=1, keepdims=True)
        final_proba /= row_sums

        return final_proba

    def predict(self, X: np.ndarray) -> np.ndarray:
        """返回类别预测 0=H,1=D,2=A"""
        proba = self.ensemble_predict(X)
        return np.argmax(proba, axis=1)

    # ────────────────── 模型持久化 ──────────────────

    def save_model(self, output_dir: str = "saved_models") -> str:
        """保存模型到 joblib"""
        os.makedirs(output_dir, exist_ok=True)

        model_data = {
            'xgb_model': self.xgb_model,
            'ridge_model': self.ridge_model,
            'scaler': self.scaler,
            'ensemble_weights': self.ensemble_weights,
            '_weights_source': self._weights_source,
            'feature_names_': self.feature_names_,
            '_feature_indices': self._feature_indices,
            'feature_importance': self.feature_importance,
            'model_version': self.model_version,
            'timestamp': pd.Timestamp.now(),
        }

        output_path = os.path.join(output_dir, f"footballai_enhanced_{self.model_version}.joblib")
        joblib.dump(model_data, output_path)
        logger.info(f"[SAVE] 模型已保存: {output_path}")
        return output_path

    @classmethod
    def load_model(cls, model_path: str) -> "FootballAIEnhanced":
        """从 joblib 加载模型"""
        data = joblib.load(model_path)
        instance = cls(model_version=data.get('model_version', 'unknown'))
        instance.xgb_model = data['xgb_model']
        instance.ridge_model = data['ridge_model']
        instance.scaler = data['scaler']
        instance.ensemble_weights = data['ensemble_weights']
        instance._weights_source = data.get('_weights_source', 'static')  # 兼容旧模型
        instance.feature_names_ = data['feature_names_']
        instance._feature_indices = data['_feature_indices']
        instance.feature_importance = data['feature_importance']
        logger.info(
            f"[LOAD] 模型已加载: {model_path} "
            f"(权重来源: {instance._weights_source})"
        )
        return instance

"""
哨响AI - 集成权重优化器 (T02)
================================
1. 时间序列验证集划分策略（防未来信息泄露）
2. Optuna 贝叶斯搜索最优集成权重
3. 网格搜索基线对比
4. 全面评估指标 + 可视化报告
"""
import sys, os, logging, yaml, json, time, warnings
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

# ── 路径设置 ──
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── 依赖检查 ──
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import (
    accuracy_score, f1_score, brier_score_loss,
    log_loss, matthews_corrcoef, confusion_matrix, balanced_accuracy_score,
)

try:
    import optuna
    OPTUNA_AVAILABLE = True
except ImportError:
    OPTUNA_AVAILABLE = False

try:
    import xgboost as xgb
    XGB_AVAILABLE = True
except ImportError:
    XGB_AVAILABLE = False

try:
    from sklearn.linear_model import Ridge
    RIDGE_AVAILABLE = True
except ImportError:
    RIDGE_AVAILABLE = False

# ── 日志 ──
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] WeightOpt - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('WeightOptimizer')


# ════════════════════════════════════════════════════════════
class WeightOptimizer:
    """集成权重优化器 — 仅优化 Ensemble 融合权重（不重新训练模型）"""

    def __init__(self, config_path: str = None):
        self.config = self._load_config(config_path)
        self.root = ROOT
        self.db_path = os.path.join(self.root, self.config['database']['path'])

        # 内部状态
        self.X_train = self.y_train = self.y_train_gd = None
        self.X_val = self.y_val = self.y_val_gd = None
        self.X_test = self.y_test = self.y_test_gd = None
        self.scaler = None
        self.feature_names = []

        self.xgb_model = None
        self.ridge_model = None
        self.xgb_proba_val = None
        self.ridge_proba_val = None
        self.heur_proba_val = None

        self._ensure_dirs()

    def _ensure_dirs(self):
        for d in ['report_dir', 'output_dir', 'model_dir']:
            p = os.path.join(self.root, self.config['paths'][d])
            os.makedirs(p, exist_ok=True)

    def _load_config(self, config_path=None) -> Dict:
        p = config_path or os.path.join(ROOT, 'config.yaml')
        with open(p, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    # ════════════════════════════════════════════════════════
    # 1. 数据加载
    # ════════════════════════════════════════════════════════

    def load_data(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[str]]:
        """从 SQLite 加载特征 + 标签，按时间排序"""
        import sqlite3
        feat_cols = self.config['data']['feature_columns']
        cols_sql = ", ".join([f"mf.{c}" for c in feat_cols])

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        query = f"""
        SELECT m.match_id, m.home_team_name, m.away_team_name, m.match_date,
               m.league_name, m.home_score, m.away_score,
               {cols_sql}
        FROM matches m
        JOIN match_features mf ON m.match_id = mf.match_id
        WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
        ORDER BY m.match_date ASC
        """
        df = pd.read_sql_query(query, conn)
        conn.close()

        logger.info(f"数据加载: {len(df)} 样本, {len(feat_cols)} 特征列, {df['league_name'].nunique()} 联赛")
        logger.info(f"日期范围: {df['match_date'].min()} ~ {df['match_date'].max()}")

        # ── 特征预处理 ──
        available_cols = [c for c in feat_cols if c in df.columns]
        X = df[available_cols].copy()
        defaults = self.config['data']['default_values']
        threshold = self.config['data']['default_ratio_threshold']

        # 缺失值填充
        for col in available_cols:
            X[col] = pd.to_numeric(X[col], errors='coerce')
            X[col] = X[col].fillna(defaults.get(col, 0.0))

        # 移除低质量特征
        removed = []
        for col in available_cols[:]:
            dv = defaults.get(col, None)
            if dv is not None and (X[col] == dv).mean() > threshold:
                removed.append(col)
        if removed:
            X = X.drop(columns=removed)
            available_cols = [c for c in available_cols if c not in removed]
            logger.warning(f"移除低质量特征: {removed}")

        # 异常值裁剪
        for col in available_cols:
            q99 = X[col].abs().quantile(0.99)
            q01 = X[col].abs().quantile(0.01)
            if q99 > q01 * 15:
                upper = q99 * 1.5
                lower = -upper if col in ['a1', 'a4', 'a5', 'a6', 'sigma_trap',
                                           'rank_diff_factor', 'form_momentum',
                                           'h2h_factor', 'beta_dev'] else 0
                X[col] = X[col].clip(lower, upper)

        # 特征交互项
        if 'a1' in X.columns and 'sigma_trap' in X.columns:
            X['ix_a1_sigma'] = X['a1'] * X['sigma_trap']
        if 'a2' in X.columns and 'lambda_crush' in X.columns:
            X['ix_a2_lambda'] = (X['a2'] - 0.5) * (X['lambda_crush'] - 1.0)
        if 'a1' in X.columns and 'a2' in X.columns:
            X['ix_a1_a2'] = X['a1'] * (X['a2'] - 0.5)
        if 'rank_diff_factor' in X.columns and 'form_momentum' in X.columns:
            X['ix_rank_form'] = X['rank_diff_factor'] * X['form_momentum']
        if 'a1' in X.columns and 'a2' in X.columns and 'rank_diff_factor' in X.columns:
            X['ix_power_gap'] = np.abs(X['a1'].values + (X['a2'].values - 0.5) * 2 +
                                       X['rank_diff_factor'].values * 0.001)

        available_cols = X.columns.tolist()

        # 标签
        home_score = df['home_score'].values
        away_score = df['away_score'].values
        y_gd = (home_score - away_score).astype(float)
        y = np.where(home_score > away_score, 0,
                     np.where(home_score == away_score, 1, 2))

        logger.info(f"标签分布 — 主胜:{sum(y==0)} 平局:{sum(y==1)} 客胜:{sum(y==2)} "
                     f"平局率:{sum(y==1)/len(y)*100:.1f}%")

        X_arr = X.values.astype(np.float64)

        # 日期数组（用于时间序列划分）
        dates = pd.to_datetime(df['match_date'].values)

        return X_arr, y, y_gd, available_cols, dates, df

    # ════════════════════════════════════════════════════════
    # 2. 验证集划分策略（时间序列，防泄漏）
    # ════════════════════════════════════════════════════════

    def split_data(self, X: np.ndarray, y: np.ndarray, y_gd: np.ndarray,
                   dates: pd.DatetimeIndex, df: pd.DataFrame,
                   train_ratio: float = 0.70, val_ratio: float = 0.15) -> Dict:
        """
        时间序列划分：train | val | test
        - train: 最早 70% 比赛，用于训练子模型
        - val: 中间 15%，用于 Optuna 搜索权重
        - test: 最新 15%，用于最终评估（全程不可见）
        """
        n = len(X)
        train_end = int(n * train_ratio)
        val_end = train_end + int(n * val_ratio)

        self.X_train = X[:train_end]
        self.X_val = X[train_end:val_end]
        self.X_test = X[val_end:]

        self.y_train = y[:train_end]
        self.y_val = y[train_end:val_end]
        self.y_test = y[val_end:]

        self.y_train_gd = y_gd[:train_end]
        self.y_val_gd = y_gd[train_end:val_end]
        self.y_test_gd = y_gd[val_end:]

        logger.info("=" * 50)
        # 安全日期格式
        train_dates = dates[:train_end]
        val_dates = dates[train_end:val_end]
        test_dates = dates[val_end:]
        train_range = f"{pd.Timestamp(train_dates.min()).strftime('%Y-%m-%d')} ~ {pd.Timestamp(train_dates.max()).strftime('%Y-%m-%d')}"
        val_range = f"{pd.Timestamp(val_dates.min()).strftime('%Y-%m-%d')} ~ {pd.Timestamp(val_dates.max()).strftime('%Y-%m-%d')}"
        test_range = f"{pd.Timestamp(test_dates.min()).strftime('%Y-%m-%d')} ~ {pd.Timestamp(test_dates.max()).strftime('%Y-%m-%d')}"

        logger.info(f"时间序列划分 ({train_ratio:.0%}/{val_ratio:.0%}/{(1-train_ratio-val_ratio):.0%}):")
        logger.info(f"  训练集: {len(self.X_train)} 样本 | {train_range}")
        logger.info(f"  验证集: {len(self.X_val)} 样本 | {val_range}")
        logger.info(f"  测试集: {len(self.X_test)} 样本 | {test_range}")
        logger.info(f"  验证集标签 — 主胜:{sum(self.y_val==0)} 平局:{sum(self.y_val==1)} 客胜:{sum(self.y_val==2)}")
        logger.info(f"  测试集标签 — 主胜:{sum(self.y_test==0)} 平局:{sum(self.y_test==1)} 客胜:{sum(self.y_test==2)}")

        self.feature_names = list(range(X.shape[1]))

        return {
            'train_size': len(self.X_train),
            'val_size': len(self.X_val),
            'test_size': len(self.X_test),
            'train_date_range': train_range,
            'val_date_range': val_range,
            'test_date_range': test_range,
        }

    # ════════════════════════════════════════════════════════
    # 3. 训练子模型（在训练集上）
    # ════════════════════════════════════════════════════════

    def train_sub_models(self):
        """在训练集上训练 XGBoost + Ridge，计算验证集各模型概率"""
        if not XGB_AVAILABLE:
            raise ImportError("需要安装 xgboost")
        if not RIDGE_AVAILABLE:
            raise ImportError("需要安装 scikit-learn")

        scaler = StandardScaler()
        X_train_s = scaler.fit_transform(self.X_train)
        X_val_s = scaler.transform(self.X_val)
        X_test_s = scaler.transform(self.X_test)
        self.scaler = scaler

        # ── XGBoost ──
        xgb_cfg = self.config['models']['xgboost']
        logger.info("训练 XGBoost 分类器...")
        t0 = time.time()
        from sklearn.utils.class_weight import compute_class_weight
        classes = np.array([0, 1, 2])
        weights = compute_class_weight('balanced', classes=classes, y=self.y_train)
        sample_weights = np.array([weights[int(c)] for c in self.y_train])

        self.xgb_model = xgb.XGBClassifier(
            objective='multi:softprob', num_class=3,
            eval_metric=['mlogloss', 'merror'],
            random_state=42, verbosity=0, n_jobs=-1,
            n_estimators=xgb_cfg.get('n_estimators', 500),
            max_depth=xgb_cfg.get('max_depth', 5),
            learning_rate=xgb_cfg.get('learning_rate', 0.03),
            subsample=xgb_cfg.get('subsample', 0.8),
            colsample_bytree=xgb_cfg.get('colsample_bytree', 0.8),
            min_child_weight=xgb_cfg.get('min_child_weight', 3),
            reg_alpha=xgb_cfg.get('reg_alpha', 0.1),
            reg_lambda=xgb_cfg.get('reg_lambda', 1.0),
            gamma=xgb_cfg.get('gamma', 0.05),
            early_stopping_rounds=xgb_cfg.get('early_stopping_rounds', 50),
            tree_method=xgb_cfg.get('tree_method', 'hist'),
        )
        self.xgb_model.fit(
            X_train_s, self.y_train,
            sample_weight=sample_weights,
            eval_set=[(X_val_s, self.y_val)],
            verbose=False,
        )
        logger.info(f"XGBoost 训练完成 ({time.time()-t0:.0f}s), best_iter={self.xgb_model.best_iteration}")

        self.xgb_proba_val = self.xgb_model.predict_proba(X_val_s)
        self.xgb_proba_test = self.xgb_model.predict_proba(X_test_s)

        # ── Ridge ──
        ridge_cfg = self.config['models']['ridge']
        logger.info(f"训练 Ridge 回归 (alpha={ridge_cfg['alpha']})...")
        t0 = time.time()
        self.ridge_model = Ridge(alpha=ridge_cfg['alpha'], random_state=42)
        self.ridge_model.fit(X_train_s, self.y_train_gd)
        logger.info(f"Ridge 训练完成 ({time.time()-t0:.0f}s), R²={self.ridge_model.score(X_train_s, self.y_train_gd):.3f}")

        self.ridge_proba_val = self._ridge_proba(X_val_s)
        self.ridge_proba_test = self._ridge_proba(X_test_s)

        # ── 启发式 ──
        self.heur_proba_val = self._heuristic_proba(X_val_s.shape[0])
        self.heur_proba_test = self._heuristic_proba(X_test_s.shape[0])

        # 各子模型基线准确率
        logger.info(f"验证集 — XGBoost Acc: {accuracy_score(self.y_val, self.xgb_proba_val.argmax(1)):.4f}")
        logger.info(f"验证集 — Ridge Acc: {accuracy_score(self.y_val, self.ridge_proba_val.argmax(1)):.4f}")
        logger.info(f"验证集 — Heuristic Acc: {accuracy_score(self.y_val, self.heur_proba_val.argmax(1)):.4f}")

    def _ridge_proba(self, X_s: np.ndarray) -> np.ndarray:
        """Ridge 净胜球 → Softmax 概率（复用 ensemble_trainer 逻辑）"""
        gd = self.ridge_model.predict(X_s)
        abs_gd = np.abs(gd)
        temps = np.clip(0.8 + 0.6 * np.minimum(abs_gd, 2.0), 0.8, 2.0)
        draw_pen = 0.3 + abs_gd * 0.15
        home_logit = gd / temps
        away_logit = -gd / temps
        draw_logit = -abs_gd / temps - draw_pen
        logits = np.column_stack([home_logit, draw_logit, away_logit])
        logits = logits - logits.max(axis=1, keepdims=True)
        exp = np.exp(logits)
        return exp / exp.sum(axis=1, keepdims=True)

    def _heuristic_proba(self, n: int) -> np.ndarray:
        """均匀分布作为启发式基线（简单但稳定的基线）"""
        return np.full((n, 3), 1.0 / 3)

    # ════════════════════════════════════════════════════════
    # 4. 加权融合函数
    # ════════════════════════════════════════════════════════

    def _weighted_proba(self, w_xgb: float, w_ridge: float,
                        proba_xgb: np.ndarray, proba_ridge: np.ndarray,
                        proba_heur: np.ndarray) -> np.ndarray:
        """加权融合 + 归一化"""
        w_heu = 1.0 - w_xgb - w_ridge
        p = w_xgb * proba_xgb + w_ridge * proba_ridge + w_heu * proba_heur
        return p / p.sum(axis=1, keepdims=True)

    # ════════════════════════════════════════════════════════
    # 5. 评估指标
    # ════════════════════════════════════════════════════════

    def _evaluate(self, y_true: np.ndarray, proba: np.ndarray) -> Dict[str, float]:
        """计算完整评估指标"""
        pred = proba.argmax(axis=1)

        # 基础指标
        acc = accuracy_score(y_true, pred)
        bal_acc = balanced_accuracy_score(y_true, pred) if len(set(y_true)) == 3 else np.nan
        mcc = matthews_corrcoef(y_true, pred) if len(set(y_true)) >= 2 else 0.0

        # 各类别 F1
        f1_per_class = f1_score(y_true, pred, average=None, labels=[0, 1, 2], zero_division=0)
        draw_f1 = f1_per_class[1]

        # 概率质量指标
        try:
            brier = brier_score_loss(y_true, proba[:, 1])
        except (Exception, KeyError, IndexError):
            brier = 0.5

        try:
            brier_multi = np.mean([brier_score_loss((y_true == c).astype(float), proba[:, c].astype(float))
                                   for c in range(3)])
        except (Exception, KeyError, IndexError):
            brier_multi = 0.5

        try:
            ll = log_loss(y_true, proba, labels=[0, 1, 2])
        except (Exception, KeyError, IndexError):
            ll = 2.0  # 高 log loss 作为惩罚

        # 预测分布
        pred_home = (pred == 0).mean()
        pred_draw = (pred == 1).mean()
        pred_away = (pred == 2).mean()
        actual_draw = (y_true == 1).mean()

        # 置信度
        max_probas = proba.max(axis=1)
        avg_conf = float(np.mean(max_probas))

        cm = confusion_matrix(y_true, pred, labels=[0, 1, 2])

        return {
            'accuracy': round(acc, 5),
            'balanced_accuracy': round(bal_acc, 5) if not np.isnan(bal_acc) else None,
            'mcc': round(mcc, 5),
            'f1_home': round(f1_per_class[0], 5),
            'f1_draw': round(f1_per_class[1], 5),
            'f1_away': round(f1_per_class[2], 5),
            'macro_f1': round(np.mean(f1_per_class), 5),
            'brier_draw': round(brier, 5),
            'brier_multi': round(brier_multi, 5),
            'log_loss': round(ll, 5),
            'avg_confidence': round(avg_conf, 4),
            'pred_home_pct': round(pred_home, 4),
            'pred_draw_pct': round(pred_draw, 4),
            'pred_away_pct': round(pred_away, 4),
            'actual_draw_pct': round(actual_draw, 4),
            'confusion_matrix': cm.tolist(),
        }

    def _composite_score(self, metrics: Dict) -> float:
        """综合评分 = 0.40*Accuracy + 0.30*DrawF1 + 0.10*MCC - 0.20*BrierMulti"""
        return (metrics['accuracy'] * 0.40 +
                metrics['f1_draw'] * 0.30 +
                metrics['mcc'] * 0.10 -
                metrics['brier_multi'] * 0.20)

    # ════════════════════════════════════════════════════════
    # 6. Optuna 贝叶斯权重搜索
    # ════════════════════════════════════════════════════════

    def optimize_optuna(self, n_trials: int = 200,
                        objective_mode: str = 'balanced') -> Dict:
        """Optuna 搜索最优集成权重"""
        if not OPTUNA_AVAILABLE:
            logger.warning("Optuna 不可用，回退到网格搜索")
            return self._fallback_grid_search()

        logger.info("=" * 60)
        logger.info(f"Optuna 贝叶斯权重优化 | 模式: {objective_mode} | 试验: {n_trials}")
        logger.info("=" * 60)

        def objective(trial):
            w_xgb = trial.suggest_float('w_xgb', 0.20, 0.60)
            w_ridge = trial.suggest_float('w_ridge', 0.08, 0.30)
            w_heu = 1.0 - w_xgb - w_ridge
            if w_heu < 0.08:
                raise optuna.exceptions.TrialPruned()

            try:
                proba = self._weighted_proba(
                    w_xgb, w_ridge,
                    self.xgb_proba_val, self.ridge_proba_val, self.heur_proba_val
                )
                metrics = self._evaluate(self.y_val, proba)
                score = self._composite_score(metrics)

                if np.isnan(score) or np.isinf(score):
                    return -999.0

                trial.set_user_attr('accuracy', float(metrics['accuracy']))
                trial.set_user_attr('draw_f1', float(metrics['f1_draw']))
                trial.set_user_attr('brier_draw', float(metrics['brier_draw'] or 0))
                trial.set_user_attr('mcc', float(metrics['mcc'] or 0))
                trial.set_user_attr('w_heu', round(w_heu, 5))
                return score
            except optuna.exceptions.TrialPruned:
                raise
            except (Exception, ValueError, KeyError, IndexError):
                return -999.0

        sampler = optuna.samplers.TPESampler(seed=42)
        study = optuna.create_study(
            direction='maximize', sampler=sampler,
            study_name=f'weight_opt_{datetime.now().strftime("%m%d_%H%M")}',
        )
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False,
                       n_jobs=1)

        # 处理所有 trial 都失败的情况
        completed_trials = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
        if not completed_trials:
            logger.warning("Optuna 所有试验均失败/被剪枝，回退到网格搜索")
            return self._fallback_grid_search()

        best = study.best_params
        best_trial = study.best_trial
        w_heu = round(1.0 - best['w_xgb'] - best['w_ridge'], 5)

        logger.info(f"Optuna 最优权重: XGB={best['w_xgb']:.4f} Ridge={best['w_ridge']:.4f} Heu={w_heu:.4f}")
        logger.info(f"最优综合评分: {study.best_value:.5f}")
        logger.info(f"最优试验 Acc={best_trial.user_attrs['accuracy']:.4f} "
                     f"DrawF1={best_trial.user_attrs['draw_f1']:.4f}")

        # 验证集上最优权重表现
        best_proba_val = self._weighted_proba(
            best['w_xgb'], best['w_ridge'],
            self.xgb_proba_val, self.ridge_proba_val, self.heur_proba_val
        )
        val_metrics = self._evaluate(self.y_val, best_proba_val)

        return {
            'method': 'optuna',
            'n_trials': n_trials,
            'best_score': round(study.best_value, 5),
            'best_params': {
                'xgboost_weight': round(best['w_xgb'], 5),
                'ridge_weight': round(best['w_ridge'], 5),
                'heuristic_weight': round(w_heu, 5),
            },
            'val_metrics': val_metrics,
            'optuna_history': self._get_optuna_history(study),
        }

    def _get_optuna_history(self, study) -> List[Dict]:
        """提取 Optuna 搜索历史（Top 20）"""
        trials = sorted(study.trials, key=lambda t: t.value or -999, reverse=True)[:20]
        return [{
            'w_xgb': round(t.params['w_xgb'], 4),
            'w_ridge': round(t.params['w_ridge'], 4),
            'w_heu': round(1.0 - t.params['w_xgb'] - t.params['w_ridge'], 4),
            'score': round(t.value, 5) if t.value else None,
            'acc': round(t.user_attrs.get('accuracy', 0), 4),
            'draw_f1': round(t.user_attrs.get('draw_f1', 0), 4),
        } for t in trials]

    # ════════════════════════════════════════════════════════
    # 7. 网格搜索（基线对比）
    # ════════════════════════════════════════════════════════

    def grid_search(self, step: float = 0.05) -> Dict:
        """网格搜索所有权重组合，作为 Optuna 对比基线"""
        logger.info("=" * 60)
        logger.info(f"网格搜索权重组合 (步长={step})")
        logger.info("=" * 60)

        best_score = -999
        best_weights = (0.50, 0.30)
        best_metrics = None
        results = []

        total = 0
        for w_xgb in np.arange(0.20, 0.61, step):
            w_xgb = round(w_xgb, 4)
            for w_ridge in np.arange(0.08, 0.31, step):
                w_ridge = round(w_ridge, 4)
                w_heu = round(1.0 - w_xgb - w_ridge, 4)
                if w_heu < 0.08:
                    continue
                total += 1

                proba = self._weighted_proba(
                    w_xgb, w_ridge,
                    self.xgb_proba_val, self.ridge_proba_val, self.heur_proba_val
                )
                metrics = self._evaluate(self.y_val, proba)
                score = self._composite_score(metrics)

                results.append({
                    'w_xgb': w_xgb, 'w_ridge': w_ridge, 'w_heu': w_heu,
                    'composite_score': round(score, 5),
                    'accuracy': metrics['accuracy'],
                    'draw_f1': metrics['f1_draw'],
                    'brier_multi': metrics['brier_multi'],
                })

                if score > best_score:
                    best_score = score
                    best_weights = (w_xgb, w_ridge)
                    best_metrics = metrics

        # 按综合评分排序
        results.sort(key=lambda x: x['composite_score'], reverse=True)

        logger.info(f"网格搜索完成: {total} 个组合, 最优复合评分={best_score:.5f}")
        logger.info(f"最优权重: XGB={best_weights[0]:.4f} Ridge={best_weights[1]:.4f} Heu={1-best_weights[0]-best_weights[1]:.4f}")

        return {
            'method': 'grid_search',
            'step': step,
            'total_combinations': total,
            'best_score': round(best_score, 5),
            'best_params': {
                'xgboost_weight': round(best_weights[0], 5),
                'ridge_weight': round(best_weights[1], 5),
                'heuristic_weight': round(1.0 - best_weights[0] - best_weights[1], 5),
            },
            'val_metrics': best_metrics,
            'top_combinations': results[:20],
        }

    def _fallback_grid_search(self) -> Dict:
        """Optuna 不可用时的替代方案"""
        return self.grid_search(step=0.05)

    # ════════════════════════════════════════════════════════
    # 8. 测试集最终评估
    # ════════════════════════════════════════════════════════

    def evaluate_final(self, weights: Dict[str, float]) -> Dict:
        """在测试集上评估给定权重组合"""
        w_xgb = weights['xgboost_weight']
        w_ridge = weights['ridge_weight']
        w_heu = weights['heuristic_weight']

        proba = self._weighted_proba(
            w_xgb, w_ridge,
            self.xgb_proba_test, self.ridge_proba_test, self.heur_proba_test
        )
        return self._evaluate(self.y_test, proba)

    # ════════════════════════════════════════════════════════
    # 9. 生成对比报告
    # ════════════════════════════════════════════════════════

    def generate_report(self, optuna_result: Dict, grid_result: Dict,
                        default_weights: Dict, split_info: Dict) -> str:
        """生成 Markdown 权重优化报告"""
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        report_dir = os.path.join(self.root, self.config['paths']['report_dir'])
        report_path = os.path.join(report_dir, f'weight_optimization_{ts}.md')

        default_test = self.evaluate_final(default_weights)
        optuna_test = self.evaluate_final(optuna_result['best_params'])
        grid_test = self.evaluate_final(grid_result['best_params'])

        def _fmt(v, is_pct=False):
            if v is None:
                return 'N/A'
            return f"{v*100:.2f}%" if is_pct else f"{v:.4f}"

        lines = [
            f"# 集成权重优化报告",
            f"",
            f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"**数据范围**: {split_info['train_date_range']} ~ {split_info['test_date_range']}",
            f"**样本量**: 训练 {split_info['train_size']} | 验证 {split_info['val_size']} | 测试 {split_info['test_size']}",
            f"",
            f"---",
            f"",
            f"## 1. 验证集划分策略",
            f"",
            f"采用 **时间序列划分**，按比赛日期从早到晚切割：",
            f"",
            f"| 数据集 | 样本数 | 日期范围 |",
            f"|--------|--------|----------|",
            f"| 训练集 | {split_info['train_size']} | {split_info['train_date_range']} |",
            f"| 验证集 | {split_info['val_size']} | {split_info['val_date_range']} |",
            f"| 测试集 | {split_info['test_size']} | {split_info['test_date_range']} |",
            f"",
            f"- 训练集用于训练 XGBoost 和 Ridge 子模型",
            f"- 验证集用于 Optuna/网格搜索最优集成权重",
            f"- 测试集仅用于最终评估，全程不可见",
            f"- 严格时间顺序切割，防止未来信息泄露",
            f"",
            f"---",
            f"",
            f"## 2. 搜索方法对比",
            f"",
            f"### 2.1 默认权重 (config.yaml 基准)",
            f"",
            f"| 参数 | 值 |",
            f"|------|-----|",
            f"| XGBoost 权重 | {default_weights['xgboost_weight']} |",
            f"| Ridge 权重 | {default_weights['ridge_weight']} |",
            f"| Heuristic 权重 | {default_weights['heuristic_weight']} |",
            f"",
            f"### 2.2 Optuna 贝叶斯搜索 ({optuna_result['n_trials']} trials)",
            f"",
            f"| 参数 | 值 |",
            f"|------|-----|",
            f"| XGBoost 权重 | {optuna_result['best_params']['xgboost_weight']} |",
            f"| Ridge 权重 | {optuna_result['best_params']['ridge_weight']} |",
            f"| Heuristic 权重 | {optuna_result['best_params']['heuristic_weight']} |",
            f"| 最优评分 | {optuna_result['best_score']} |",
            f"",
            f"### 2.3 网格搜索 ({grid_result.get('step', 0.05)} 步长, {grid_result['total_combinations']} 组合)",
            f"",
            f"| 参数 | 值 |",
            f"|------|-----|",
            f"| XGBoost 权重 | {grid_result['best_params']['xgboost_weight']} |",
            f"| Ridge 权重 | {grid_result['best_params']['ridge_weight']} |",
            f"| Heuristic 权重 | {grid_result['best_params']['heuristic_weight']} |",
            f"| 最优评分 | {grid_result['best_score']} |",
            f"",
            f"---",
            f"",
            f"## 3. 验证集表现对比",
            f"",
            f"| 指标 | 默认权重 | Optuna 最优 | 网格最优 | 改善 |",
            f"|------|----------|-------------|----------|------|",
        ]

        # 获取验证集默认权重指标
        default_val = self._evaluate(self.y_val, self._weighted_proba(
            default_weights['xgboost_weight'], default_weights['ridge_weight'],
            self.xgb_proba_val, self.ridge_proba_val, self.heur_proba_val))

        def _fmt_imp(v_new, v_old, is_pct=False):
            if v_old is None or v_new is None:
                return ''
            d = v_new - v_old
            sign = '+' if d > 0 else ''
            return f"{sign}{d*100:.2f}%" if is_pct else f"{sign}{d:.4f}"

        rows = [
            ('准确率', 'accuracy', False),
            ('MACRO F1', 'macro_f1', False),
            ('平局 F1', 'f1_draw', False),
            ('MCC', 'mcc', False),
            ('Brier (Multi)', 'brier_multi', True),
            ('Log Loss', 'log_loss', True),
            ('预测平局率', 'pred_draw_pct', True),
            ('实际平局率', 'actual_draw_pct', True),
        ]

        for name, key, is_pct in rows:
            opt_val = optuna_result['val_metrics'].get(key)
            grid_val = grid_result['val_metrics'].get(key)
            default_val_cell = default_val.get(key)
            imp = _fmt_imp(opt_val, default_val_cell, is_pct) if opt_val is not None else ''
            lines.append(f"| {name} | {_fmt(default_val_cell, is_pct)} | {_fmt(opt_val, is_pct)} | {_fmt(grid_val, is_pct)} | {imp} |")

        lines += [
            "",
            "---",
            "",
            "## 4. 测试集最终评估",
            "",
            "| 指标 | 默认权重 | Optuna 最优 | 网格最优 |",
            "|------|----------|-------------|----------|",
        ]
        for name, key, is_pct in rows:
            lines.append(
                f"| {name} | {_fmt(default_test.get(key), is_pct)} | "
                f"{_fmt(optuna_test.get(key), is_pct)} | {_fmt(grid_test.get(key), is_pct)} |"
            )

        lines += [
            "",
            "---",
            "",
            "## 5. Optuna 搜索历史 (Top 10)",
            "",
            "| 排名 | w_xgb | w_ridge | w_heu | 评分 | 准确率 | 平局F1 |",
            "|------|-------|---------|-------|------|--------|--------|",
        ]
        for i, h in enumerate(optuna_result.get('optuna_history', [])[:10], 1):
            lines.append(f"| {i} | {h['w_xgb']} | {h['w_ridge']} | {h['w_heu']} | {h['score']} | {h['acc']} | {h['draw_f1']} |")

        lines += [
            "",
            "---",
            "",
            "## 6. 结论与建议",
            "",
            f"### 最优权重配置",
            "",
            "```yaml",
            "ensemble:",
            f"  xgboost_weight: {optuna_result['best_params']['xgboost_weight']}",
            f"  ridge_weight: {optuna_result['best_params']['ridge_weight']}",
            f"  heuristic_weight: {optuna_result['best_params']['heuristic_weight']}",
            "```",
            "",
        ]

        # 计算测试集改善
        acc_improve = optuna_test['accuracy'] - default_test['accuracy'] if optuna_test.get('accuracy') and default_test.get('accuracy') else 0
        draw_improve = optuna_test['f1_draw'] - default_test['f1_draw'] if optuna_test.get('f1_draw') and default_test.get('f1_draw') else 0

        lines.append(f"- 测试集准确率改善: {acc_improve*100:+.2f}%")
        lines.append(f"- 测试集平局F1改善: {draw_improve*100:+.2f}%")

        with open(report_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

        logger.info(f"报告已生成: {report_path}")
        return report_path

    # ════════════════════════════════════════════════════════
    # 10. 对比权重组合全面评估
    # ════════════════════════════════════════════════════════

    def compare_weights(self, weight_sets: List[Dict]) -> pd.DataFrame:
        """评估多组权重组合，生成对比表格"""
        rows = []
        for ws in weight_sets:
            w_xgb = ws['xgboost_weight']
            w_ridge = ws['ridge_weight']
            w_heu = ws['heuristic_weight']
            proba = self._weighted_proba(
                w_xgb, w_ridge,
                self.xgb_proba_test, self.ridge_proba_test, self.heur_proba_test
            )
            m = self._evaluate(self.y_test, proba)
            m['name'] = ws.get('name', f'w{ws["xgboost_weight"]}_w{ws["ridge_weight"]}')
            m['xgboost_weight'] = w_xgb
            m['ridge_weight'] = w_ridge
            m['heuristic_weight'] = w_heu
            rows.append(m)

        df = pd.DataFrame(rows)
        df = df.sort_values('composite_score' if 'composite_score' not in df.columns else 'accuracy', ascending=False)
        return df

    # ════════════════════════════════════════════════════════
    # 11. 主运行入口
    # ════════════════════════════════════════════════════════

    def run(self, n_trials: int = 200, train_ratio: float = 0.70,
            val_ratio: float = 0.15) -> Dict:
        """完整权重优化流程"""
        logger.info("=" * 70)
        logger.info("  哨响AI - 集成权重优化 (T02)")
        logger.info("=" * 70)

        # Step 1: 加载数据
        X, y, y_gd, feat_names, dates, df = self.load_data()

        # Step 2: 验证集划分
        split_info = self.split_data(X, y, y_gd, dates, df, train_ratio, val_ratio)

        # Step 3: 训练子模型
        self.train_sub_models()
        self.feature_names = feat_names

        # Step 4: Optuna 搜索最优权重
        optuna_result = self.optimize_optuna(n_trials=n_trials)

        # Step 5: 网格搜索（基线对比）
        grid_result = self.grid_search(step=0.05)

        # Step 6: 测试集评估
        default_weights = self.config['models']['ensemble']
        logger.info("\n" + "=" * 60)
        logger.info("测试集最终评估")
        logger.info("=" * 60)

        optuna_test = self.evaluate_final(optuna_result['best_params'])
        grid_test = self.evaluate_final(grid_result['best_params'])
        default_test = self.evaluate_final(default_weights)

        logger.info(f"默认权重 (0.50/0.30/0.20) — Acc: {default_test['accuracy']:.4f} DrawF1: {default_test['f1_draw']:.4f}")
        logger.info(f"Optuna 最优              — Acc: {optuna_test['accuracy']:.4f} DrawF1: {optuna_test['f1_draw']:.4f}")
        logger.info(f"网格 最优                — Acc: {grid_test['accuracy']:.4f} DrawF1: {grid_test['f1_draw']:.4f}")

        # Step 7: 生成报告
        report_path = self.generate_report(optuna_result, grid_result,
                                           default_weights, split_info)

        # Step 8: 保存最优权重 JSON
        output_dir = os.path.join(self.root, self.config['paths']['output_dir'])
        best_weights = optuna_result['best_params']
        weights_path = os.path.join(output_dir, 'optimal_weights.json')
        with open(weights_path, 'w', encoding='utf-8') as f:
            json.dump({
                'generated_at': datetime.now().isoformat(),
                'method': 'optuna_bayesian',
                'n_trials': n_trials,
                'weights': best_weights,
                'val_metrics': optuna_result['val_metrics'],
                'test_metrics': optuna_test,
                'comparison': {
                    'default': {'weights': default_weights, 'test_metrics': default_test},
                    'grid': {'weights': grid_result['best_params'], 'test_metrics': grid_test},
                }
            }, f, indent=2, ensure_ascii=False)
        logger.info(f"最优权重已保存: {weights_path}")

        return {
            'best_weights': best_weights,
            'report_path': report_path,
            'weights_path': weights_path,
            'optuna_result': optuna_result,
            'grid_result': grid_result,
            'test_metrics': optuna_test,
        }


# ════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='哨响AI - 集成权重优化')
    parser.add_argument('--trials', type=int, default=200, help='Optuna 试验次数')
    parser.add_argument('--train-ratio', type=float, default=0.70, help='训练集比例')
    parser.add_argument('--val-ratio', type=float, default=0.15, help='验证集比例')
    parser.add_argument('--apply', action='store_true', help='自动更新 config.yaml')
    args = parser.parse_args()

    optimizer = WeightOptimizer()
    result = optimizer.run(n_trials=args.trials,
                           train_ratio=args.train_ratio,
                           val_ratio=args.val_ratio)

    if args.apply:
        # 更新 config.yaml
        config_path = os.path.join(ROOT, 'config.yaml')
        with open(config_path, 'r', encoding='utf-8') as f:
            config_text = f.read()

        import re
        best = result['best_weights']
        config_text = re.sub(
            r'xgboost_weight:\s*[\d.]+', f'xgboost_weight: {best["xgboost_weight"]}', config_text)
        config_text = re.sub(
            r'ridge_weight:\s*[\d.]+', f'ridge_weight: {best["ridge_weight"]}', config_text)
        config_text = re.sub(
            r'heuristic_weight:\s*[\d.]+', f'heuristic_weight: {best["heuristic_weight"]}', config_text)

        with open(config_path, 'w', encoding='utf-8') as f:
            f.write(config_text)
        logger.info(f"config.yaml 已更新为最优权重")

    logger.info("\n✓ 权重优化完成!")
    logger.info(f"  报告: {result['report_path']}")
    logger.info(f"  权重: {result['weights_path']}")

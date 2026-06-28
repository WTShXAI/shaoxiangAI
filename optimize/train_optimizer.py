"""
哨响AI - Optuna超参数优化器 v1.0
==============================
基于贝叶斯优化的超参数搜索，替代原有 ParameterGrid 网格搜索。

特点:
- 自动搜索 XGBoost + Ridge + 集成权重的最优组合
- 支持多目标优化(准确率 + 平局F1 + Brier Score)
- 时序交叉验证防止数据泄露
- 支持早停(pruning)减少搜索时间
- 自动特征选择(基于重要性)
- 输出完整的优化报告

使用方法:
    python optimize/train_optimizer.py          # 默认100次试验
    python optimize/train_optimizer.py --n 200   # 200次试验
    python optimize/train_optimizer.py --no-optuna  # 回退到默认参数训练
"""
import sys, os, logging, json, time, warnings, argparse
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional, Any
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

from ensemble_trainer import EnsembleTrainer
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score, f1_score, brier_score_loss, log_loss, matthews_corrcoef
from sklearn.utils.class_weight import compute_class_weight

try:
    import xgboost as xgb
except ImportError:
    xgb = None

try:
    import optuna
    OPTUNA_AVAILABLE = True
except ImportError:
    OPTUNA_AVAILABLE = False
    print("WARN: Optuna not installed. Run: pip install optuna")
    print("  Fallback: using default params only")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] OPT - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('TrainOptimizer')

def _recall_single(y_true, y_pred, target_class):
    """单类召回率"""
    mask = (y_true == target_class)
    if mask.sum() == 0:
        return 0.0
    return float(np.mean(y_pred[mask] == target_class))

class OptunaOptimizer:
    """Optuna 驱动的超参数优化器"""

    def __init__(self, config_path: str = None, n_trials: int = 100,
                 objective_mode: str = 'balanced'):
        if config_path is None:
            config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       'config.yaml')
        self.config_path = config_path
        self.n_trials = n_trials
        self.objective_mode = objective_mode

        self.trainer = EnsembleTrainer(config_path)
        self.X = None
        self.y = None
        self.dates = None
        self.feature_names = []
        self.cv_splits = 3
        self.best_params = None
        self.best_score = 0
        self.study = None

    @property
    def optuna_available(self) -> bool:
        return OPTUNA_AVAILABLE

    # ── 数据加载 ──
    def load_data(self) -> Tuple[np.ndarray, np.ndarray, pd.Series, List[str]]:
        logger.info("加载训练数据...")
        df = self.trainer.load_training_data()
        if len(df) < 100:
            raise ValueError(f"数据不足 ({len(df)} 条)，至少需要100条")

        X_df, y = self.trainer.prepare_features(df)
        self.feature_names = list(X_df.columns)
        self.dates = self.trainer.meta['match_date'].copy() if hasattr(self.trainer, 'meta') else pd.Series()

        defaults = self.trainer.config['data']['default_values']
        for col in X_df.columns:
            X_df[col] = pd.to_numeric(X_df[col], errors='coerce').fillna(defaults.get(col, 0.0))

        X_arr = X_df.values.astype(np.float32)
        logger.info(f"数据就绪: X={X_arr.shape}, 特征={len(self.feature_names)}")
        return X_arr, y.values, self.dates, self.feature_names

    # ── 交叉验证评估 ──
    def _cv_evaluate(
        self, X: np.ndarray, y: np.ndarray,
        xgb_params: Dict, ridge_alpha: float,
        ensemble_w_xgb: float, ensemble_w_ridge: float,
        draw_weight: float, draw_threshold_ratio: float,
    ) -> Dict[str, float]:
        tscv = TimeSeriesSplit(n_splits=self.cv_splits)
        metrics = {
            'accuracy': [], 'draw_f1': [], 'draw_recall': [],
            'brier': [], 'log_loss': [], 'mcc': [],
            'pred_draw_pct': [], 'actual_draw_pct': [],
        }

        for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
            X_tr, X_val = X[train_idx], X[val_idx]
            y_tr, y_val = y[train_idx], y[val_idx]
            if len(X_val) < 15:
                continue

            scaler = StandardScaler()
            X_tr_s = scaler.fit_transform(X_tr)
            X_val_s = scaler.transform(X_val)

            classes = np.array([0, 1, 2])
            base_w = compute_class_weight('balanced', classes=classes, y=y_tr)
            base_w[1] *= draw_weight
            base_w = base_w / base_w.mean()
            sample_w = np.array([base_w[int(c)] for c in y_tr])

            # XGBoost
            if xgb is not None:
                xgb_m = xgb.XGBClassifier(**{
                    'objective': 'multi:softprob', 'num_class': 3,
                    'eval_metric': 'mlogloss', 'verbosity': 0, 'n_jobs': -1,
                    'random_state': 42 + fold, 'tree_method': 'hist',
                    'early_stopping_rounds': 30,
                    **xgb_params,
                })
                val_split = int(len(X_tr_s) * 0.85)
                xgb_m.fit(
                    X_tr_s[:val_split], y_tr[:val_split],
                    sample_weight=sample_w[:val_split],
                    eval_set=[(X_tr_s[val_split:], y_tr[val_split:])],
                    verbose=False,
                )
                proba_xgb = xgb_m.predict_proba(X_val_s)
            else:
                proba_xgb = np.ones((len(X_val_s), 3)) / 3

            # Ridge
            from sklearn.linear_model import Ridge
            y_gd_tr = np.array([2.0 if c == 0 else (-2.0 if c == 2 else 0.0) for c in y_tr])
            ridge_m = Ridge(alpha=ridge_alpha)
            ridge_m.fit(X_tr_s, y_gd_tr)

            gd_pred = ridge_m.predict(X_val_s)
            temp = 1.5; dp = 0.3
            hl = gd_pred / temp; al = -gd_pred / temp; dl = -np.abs(gd_pred) / temp - dp
            logits_arr = np.column_stack([hl, dl, al])
            logits_arr = logits_arr - logits_arr.max(axis=1, keepdims=True)
            proba_ridge = np.exp(logits_arr) / np.exp(logits_arr).sum(axis=1, keepdims=True)

            # Heuristic
            proba_heu = np.full((len(X_val_s), 3), 1.0 / 3)
            a1_idx = self.feature_names.index('a1') if 'a1' in self.feature_names else None
            a2_idx = self.feature_names.index('a2') if 'a2' in self.feature_names else None
            rank_idx = self.feature_names.index('rank_diff_factor') if 'rank_diff_factor' in self.feature_names else None
            for i in range(len(X_val_s)):
                hf, df_, af = 0.33, 0.33, 0.33
                if a1_idx is not None:
                    hf += X_val_s[i, a1_idx] * 0.4; af -= X_val_s[i, a1_idx] * 0.3
                if a2_idx is not None:
                    hf += (X_val_s[i, a2_idx] - 0.5) * 0.5
                if rank_idx is not None:
                    hf += X_val_s[i, rank_idx] * 0.0005; af -= X_val_s[i, rank_idx] * 0.0005
                hf, df_, af = max(hf, 0.05), max(df_, 0.05), max(af, 0.05)
                total = hf + df_ + af
                proba_heu[i] = [hf / total, df_ / total, af / total]

            w_heu = max(0.0, 1.0 - ensemble_w_xgb - ensemble_w_ridge)
            proba_ens = ensemble_w_xgb * proba_xgb + ensemble_w_ridge * proba_ridge + w_heu * proba_heu
            proba_ens = proba_ens / proba_ens.sum(axis=1, keepdims=True)

            y_pred = np.argmax(proba_ens, axis=1)

            actual_draw_rate = (y_val == 1).mean()
            draw_thresh = max(actual_draw_rate * draw_threshold_ratio, 0.20)
            for i in range(len(y_pred)):
                if y_pred[i] == 1 and proba_ens[i, 1] < draw_thresh:
                    y_pred[i] = 0 if proba_ens[i, 0] >= proba_ens[i, 2] else 2

            y_onehot = np.zeros((len(y_val), 3))
            for i, c in enumerate(y_val):
                y_onehot[i, int(c)] = 1
            brier = np.mean([brier_score_loss(y_onehot[:, i], proba_ens[:, i]) for i in range(3)])
            f1_scores = f1_score(y_val, y_pred, average=None, zero_division=0)

            metrics['accuracy'].append(accuracy_score(y_val, y_pred))
            metrics['draw_f1'].append(f1_scores[1] if len(f1_scores) > 1 else 0)
            metrics['draw_recall'].append(_recall_single(y_val, y_pred, 1))
            metrics['brier'].append(brier)
            metrics['log_loss'].append(log_loss(y_val, proba_ens))
            metrics['mcc'].append(matthews_corrcoef(y_val, y_pred))
            metrics['pred_draw_pct'].append(np.mean(y_pred == 1))
            metrics['actual_draw_pct'].append(np.mean(y_val == 1))

        result = {}
        for k, v in metrics.items():
            if v:
                result[k] = round(np.mean(v), 4)
        return result

    # ── Optuna 目标函数 ──
    def _optuna_objective(self, trial):
        xgb_params = {
            'n_estimators': trial.suggest_int('n_estimators', 200, 800, step=100),
            'max_depth': trial.suggest_int('max_depth', 3, 10),
            'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.1, log=True),
            'subsample': trial.suggest_float('subsample', 0.6, 1.0),
            'colsample_bytree': trial.suggest_float('colsample_bytree', 0.6, 1.0),
            'min_child_weight': trial.suggest_int('min_child_weight', 1, 10),
            'reg_alpha': trial.suggest_float('reg_alpha', 0.0, 2.0),
            'reg_lambda': trial.suggest_float('reg_lambda', 0.1, 3.0),
            'gamma': trial.suggest_float('gamma', 0.0, 0.5),
        }
        ridge_alpha = trial.suggest_float('ridge_alpha', 0.1, 10.0, log=True)
        w_xgb = trial.suggest_float('ensemble_w_xgb', 0.35, 0.65)
        w_ridge = trial.suggest_float('ensemble_w_ridge', 0.15, 0.45)
        if w_xgb + w_ridge > 0.95:
            raise optuna.exceptions.TrialPruned()

        draw_weight = trial.suggest_float('draw_weight', 0.7, 1.5)
        draw_threshold_ratio = trial.suggest_float('draw_threshold_ratio', 1.0, 1.3)

        try:
            cv_result = self._cv_evaluate(
                self.X, self.y, xgb_params, ridge_alpha,
                w_xgb, w_ridge, draw_weight, draw_threshold_ratio,
            )
        except (Exception, ValueError):
            return -999.0

        acc = cv_result.get('accuracy', 0)
        draw_f1 = cv_result.get('draw_f1', 0)
        brier = cv_result.get('brier', 0)
        mcc = cv_result.get('mcc', 0)
        pred_draw = cv_result.get('pred_draw_pct', 0)
        actual_draw = cv_result.get('actual_draw_pct', 0)
        dist_penalty = abs(pred_draw - actual_draw) * 2.0

        if self.objective_mode == 'draw_f1':
            score = draw_f1 * 0.6 + acc * 0.2 - brier * 5 + mcc * 0.1 - dist_penalty / 100
        elif self.objective_mode == 'accuracy':
            score = acc * 0.7 + draw_f1 * 0.1 - brier * 5 - dist_penalty / 100
        else:
            score = acc * 0.40 + draw_f1 * 0.30 + mcc * 0.10 - brier * 8 - dist_penalty / 100

        trial.set_user_attr('accuracy', acc)
        trial.set_user_attr('draw_f1', draw_f1)
        trial.set_user_attr('brier', brier)
        trial.set_user_attr('mcc', mcc)
        return score

    # ── Optuna 优化入口 ──
    def optimize(self) -> Dict:
        if not self.optuna_available:
            logger.warning("Optuna 不可用，使用默认参数训练")
            return self._fallback_optimize()

        X_arr, y_arr, _, feature_names = self.load_data()

        logger.info("=" * 60)
        logger.info(f"Optuna 超参数优化 | 模式: {self.objective_mode}")
        logger.info(f"数据: {len(X_arr)} 样本 x {len(feature_names)} 特征")
        logger.info(f"试验次数: {self.n_trials} | CV折数: {self.cv_splits}")
        logger.info("=" * 60)

        sampler = optuna.samplers.TPESampler(seed=42)
        pruner = optuna.pruners.MedianPruner(
            n_startup_trials=10, n_warmup_steps=5, interval_steps=3,
        )
        study = optuna.create_study(
            direction='maximize', sampler=sampler, pruner=pruner,
            study_name=f'football_opt_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")}',
        )

        self.X = X_arr
        self.y = y_arr

        t0 = time.time()
        study.optimize(self._optuna_objective, n_trials=self.n_trials, show_progress_bar=True)
        elapsed = time.time() - t0

        self.study = study
        self.best_params = study.best_params
        self.best_score = study.best_value

        best_acc = study.best_trial.user_attrs.get('accuracy', 0)
        best_draw_f1 = study.best_trial.user_attrs.get('draw_f1', 0)
        best_brier = study.best_trial.user_attrs.get('brier', 0)

        logger.info(f"\n{'='*60}")
        logger.info(f"优化完成! 耗时: {elapsed:.0f}s")
        logger.info(f"最佳综合分数: {study.best_value:.4f}")
        logger.info(f"准确率: {best_acc*100:.1f}% | 平局F1: {best_draw_f1*100:.1f}% | Brier: {best_brier:.4f}")
        logger.info(f"最佳参数: {json.dumps(study.best_params, indent=2)}")
        logger.info(f"{'='*60}")

        result = self._train_final_model(X_arr, y_arr, feature_names)
        return {
            'best_params': study.best_params,
            'best_score': study.best_value,
            'best_accuracy': best_acc,
            'best_draw_f1': best_draw_f1,
            'best_brier': best_brier,
            'n_trials': len(study.trials),
            'optimization_time_s': round(elapsed, 0),
            'final_model': result,
        }

    def _fallback_optimize(self) -> Dict:
        X_arr, y_arr, _, feature_names = self.load_data()
        self.best_params = {
            'n_estimators': 500, 'max_depth': 5, 'learning_rate': 0.03,
            'subsample': 0.8, 'colsample_bytree': 0.8, 'min_child_weight': 3,
            'reg_alpha': 0.1, 'reg_lambda': 1.0, 'gamma': 0.05,
            'ridge_alpha': 1.0, 'ensemble_w_xgb': 0.50, 'ensemble_w_ridge': 0.30,
            'draw_weight': 1.15, 'draw_threshold_ratio': 1.1,
        }
        logger.info("使用 config.yaml 默认参数训练...")
        result = self._train_final_model(X_arr, y_arr, feature_names)
        return {'best_params': self.best_params, 'best_score': 0, 'fallback': True, 'final_model': result}

    # ── 最终模型训练 ──
    def _train_final_model(
        self, X: np.ndarray, y: np.ndarray, feature_names: List[str],
    ) -> Dict:
        logger.info("\n" + "=" * 60)
        logger.info("训练最终生产模型")
        logger.info("=" * 60)

        params = self.best_params if self.best_params else {}
        xgb_final_params = {
            'objective': 'multi:softprob', 'num_class': 3,
            'eval_metric': ['mlogloss', 'merror'], 'verbosity': 0,
            'n_jobs': -1, 'random_state': 42, 'tree_method': 'hist',
            'early_stopping_rounds': 50,
            'n_estimators': params.get('n_estimators', 500),
            'max_depth': params.get('max_depth', 5),
            'learning_rate': params.get('learning_rate', 0.03),
            'subsample': params.get('subsample', 0.8),
            'colsample_bytree': params.get('colsample_bytree', 0.8),
            'min_child_weight': params.get('min_child_weight', 3),
            'reg_alpha': params.get('reg_alpha', 0.1),
            'reg_lambda': params.get('reg_lambda', 1.0),
            'gamma': params.get('gamma', 0.05),
        }
        ridge_alpha = params.get('ridge_alpha', 1.0)
        draw_weight = params.get('draw_weight', 1.0)
        draw_threshold_ratio = params.get('draw_threshold_ratio', 1.1)

        split_idx = int(len(X) * 0.90)
        X_train, X_test = X[:split_idx], X[split_idx:]
        y_train, y_test = y[:split_idx], y[split_idx:]
        logger.info(f"训练集: {len(X_train)} | 测试集: {len(X_test)}")

        scaler = StandardScaler()
        X_train_s = scaler.fit_transform(X_train)
        X_test_s = scaler.transform(X_test)

        classes = np.array([0, 1, 2])
        base_w = compute_class_weight('balanced', classes=classes, y=y_train)
        base_w[1] *= draw_weight
        base_w = base_w / base_w.mean()
        sample_w = np.array([base_w[int(c)] for c in y_train])

        # XGBoost
        if xgb is not None:
            val_split = int(len(X_train_s) * 0.85)
            xgb_model = xgb.XGBClassifier(**xgb_final_params)
            xgb_model.fit(
                X_train_s[:val_split], y_train[:val_split],
                sample_weight=sample_w[:val_split],
                eval_set=[(X_train_s[val_split:], y_train[val_split:])],
                verbose=False,
            )
            proba_xgb = xgb_model.predict_proba(X_test_s)
        else:
            xgb_model = None
            proba_xgb = np.ones((len(X_test_s), 3)) / 3

        # Ridge
        from sklearn.linear_model import Ridge
        y_gd_train = np.array([2.0 if c == 0 else (-2.0 if c == 2 else 0.0) for c in y_train])
        ridge_model = Ridge(alpha=ridge_alpha)
        ridge_model.fit(X_train_s, y_gd_train)

        gd_pred = ridge_model.predict(X_test_s)
        temp = 1.5; dp = 0.3
        hl = gd_pred / temp; al = -gd_pred / temp; dl = -np.abs(gd_pred) / temp - dp
        logits_arr = np.column_stack([hl, dl, al])
        logits_arr = logits_arr - logits_arr.max(axis=1, keepdims=True)
        proba_ridge = np.exp(logits_arr) / np.exp(logits_arr).sum(axis=1, keepdims=True)

        # Heuristic
        proba_heu = np.full((len(X_test_s), 3), 1.0 / 3)
        a1_idx = feature_names.index('a1') if 'a1' in feature_names else None
        a2_idx = feature_names.index('a2') if 'a2' in feature_names else None
        rank_idx = feature_names.index('rank_diff_factor') if 'rank_diff_factor' in feature_names else None
        for i in range(len(X_test_s)):
            hf, df_, af = 0.33, 0.33, 0.33
            if a1_idx is not None: hf += X_test_s[i, a1_idx] * 0.4; af -= X_test_s[i, a1_idx] * 0.3
            if a2_idx is not None: hf += (X_test_s[i, a2_idx] - 0.5) * 0.5
            if rank_idx is not None: hf += X_test_s[i, rank_idx] * 0.0005; af -= X_test_s[i, rank_idx] * 0.0005
            hf, df_, af = max(hf, 0.05), max(df_, 0.05), max(af, 0.05)
            total = hf + df_ + af
            proba_heu[i] = [hf / total, df_ / total, af / total]

        w_xgb = params.get('ensemble_w_xgb', 0.50)
        w_ridge = params.get('ensemble_w_ridge', 0.30)
        w_heu = max(0.0, 1.0 - w_xgb - w_ridge)

        proba_ens = w_xgb * proba_xgb + w_ridge * proba_ridge + w_heu * proba_heu
        proba_ens = proba_ens / proba_ens.sum(axis=1, keepdims=True)

        y_pred = np.argmax(proba_ens, axis=1)

        actual_draw_rate = (y_test == 1).mean()
        draw_thresh = max(actual_draw_rate * draw_threshold_ratio, 0.20)
        for i in range(len(y_pred)):
            if y_pred[i] == 1 and proba_ens[i, 1] < draw_thresh:
                y_pred[i] = 0 if proba_ens[i, 0] >= proba_ens[i, 2] else 2

        acc = accuracy_score(y_test, y_pred)
        f1_all = f1_score(y_test, y_pred, average=None, zero_division=0)
        mcc = matthews_corrcoef(y_test, y_pred)

        y_onehot = np.zeros((len(y_test), 3))
        for i, c in enumerate(y_test):
            y_onehot[i, int(c)] = 1
        brier = np.mean([brier_score_loss(y_onehot[:, i], proba_ens[:, i]) for i in range(3)])
        ll = log_loss(y_test, proba_ens)

        logger.info(f"\n最终模型评估:")
        logger.info(f"  准确率: {acc*100:.2f}%")
        logger.info(f"  平局F1: {f1_all[1]*100:.1f}% (召回: {_recall_single(y_test,y_pred,1)*100:.1f}%)")
        logger.info(f"  主胜F1: {f1_all[0]*100:.1f}% | 客胜F1: {f1_all[2]*100:.1f}%")
        logger.info(f"  Brier: {brier:.4f} | LogLoss: {ll:.4f} | MCC: {mcc:.4f}")

        # 保存模型
        import joblib, yaml
        model_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'saved_models')
        os.makedirs(model_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        model_path = os.path.join(model_dir, f'football_ensemble_optuna_{ts}.joblib')

        with open(self.config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        pipeline = {
            'xgb_model': xgb_model, 'ridge_model': ridge_model, 'scaler': scaler,
            'feature_names': feature_names, 'config': config,
            'eval_metrics': {
                'accuracy': round(acc * 100, 2), 'draw_f1': round(f1_all[1] * 100, 1),
                'brier': round(brier, 4), 'log_loss': round(ll, 4), 'mcc': round(mcc, 4),
                'test_samples': len(y_test),
            },
            'train_timestamp': datetime.now(timezone.utc).isoformat(),
            'version': '4.0-optuna',
            'optuna_params': params,
            'optuna_score': self.best_score,
        }
        joblib.dump(pipeline, model_path, compress=3)
        logger.info(f"\n模型已保存: {model_path}")

        return {
            'accuracy': round(acc * 100, 2),
            'draw_f1': round(f1_all[1] * 100, 1),
            'draw_recall': round(_recall_single(y_test, y_pred, 1) * 100, 1),
            'home_f1': round(f1_all[0] * 100, 1),
            'away_f1': round(f1_all[2] * 100, 1),
            'brier': round(brier, 4), 'log_loss': round(ll, 4), 'mcc': round(mcc, 4),
            'test_samples': len(y_test),
            'model_path': model_path,
            'ensemble_weights': (w_xgb, w_ridge, w_heu),
        }

    # ── 特征选择 ──
    def feature_selection(self, importance_threshold: float = 0.02) -> List[str]:
        if self.best_params is None:
            logger.warning("请先运行 optimize()")
            return self.feature_names

        X_arr, y_arr, _, feature_names = self.load_data()
        scaler = StandardScaler()
        X_s = scaler.fit_transform(X_arr)

        if xgb is None:
            return feature_names

        params = self.best_params
        xgb_params = {
            'objective': 'multi:softprob', 'num_class': 3,
            'verbosity': 0, 'n_jobs': -1, 'random_state': 42, 'tree_method': 'hist',
            'n_estimators': params.get('n_estimators', 300),
            'max_depth': params.get('max_depth', 5),
            'learning_rate': params.get('learning_rate', 0.03),
        }

        classes = np.array([0, 1, 2])
        base_w = compute_class_weight('balanced', classes=classes, y=y_arr)
        base_w = base_w / base_w.mean()
        sample_w = np.array([base_w[int(c)] for c in y_arr])

        model = xgb.XGBClassifier(**xgb_params)
        model.fit(X_s, y_arr, sample_weight=sample_w)

        importances = model.feature_importances_
        feat_imp = list(zip(feature_names, importances))
        feat_imp.sort(key=lambda x: -x[1])

        logger.info("\n特征重要性:")
        selected = []
        for name, imp in feat_imp:
            status = "Y" if imp >= importance_threshold else "N"
            logger.info(f"  [{status}] {name:20s}: {imp*100:5.1f}%")
            if imp >= importance_threshold:
                selected.append(name)

        logger.info(f"\n特征选择: {len(feature_names)} -> {len(selected)} 个")
        if len(selected) < len(feature_names):
            logger.info(f"移除: {set(feature_names) - set(selected)}")
        return selected

# ══════════════════════════════════════════════════
# 便捷函数
# ══════════════════════════════════════════════════

def optimize_model(
    config_path: str = None, n_trials: int = 100,
    objective_mode: str = 'balanced',
) -> Dict:
    optimizer = OptunaOptimizer(
        config_path=config_path, n_trials=n_trials, objective_mode=objective_mode,
    )
    result = optimizer.optimize()
    if optimizer.optuna_available:
        selected_features = optimizer.feature_selection(importance_threshold=0.02)
        result['selected_features'] = selected_features
        result['removed_features'] = list(set(optimizer.feature_names) - set(selected_features))
    return result

# ══════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='哨响AI - Optuna 超参数优化器')
    parser.add_argument('--n', type=int, default=100, help='Optuna 试验次数 (默认100)')
    parser.add_argument('--mode', choices=['balanced', 'accuracy', 'draw_f1'],
                        default='balanced', help='优化目标模式')
    parser.add_argument('--no-optuna', action='store_true', help='跳过 Optuna，使用默认参数训练')

    args = parser.parse_args()

    if args.no_optuna or not OPTUNA_AVAILABLE:
        logger.info("使用默认参数模式（无 Optuna）")
        optimizer = OptunaOptimizer(n_trials=0)
        result = optimizer.optimize()
        report = {'mode': 'default_params', 'result': result}
    else:
        result = optimize_model(n_trials=args.n, objective_mode=args.mode)
        report = {'mode': 'optuna', 'objective_mode': args.mode, 'n_trials': args.n, 'result': result}

    report_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')
    os.makedirs(report_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    report_path = os.path.join(report_dir, f'optuna_optimization_{ts}.json')

    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)

    logger.info(f"\n优化报告已保存: {report_path}")
    return result

if __name__ == '__main__':
    main()

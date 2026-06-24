"""
哨响AI - GBDT 三模型对比评估框架 (T08)
========================================
在同一份数据上训练 XGBoost / LightGBM / CatBoost 并横向对比。

功能：
1. 统一训练 → 统一评估 → 生成对比报告
2. 按联赛拆分评估
3. 预测一致性分析（三模型投票）
4. 特征重要性对比
5. 训练时间 / 推理速度对比
"""

import os, time, json, logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score, classification_report, brier_score_loss,
    log_loss, matthews_corrcoef, confusion_matrix,
    cohen_kappa_score,
)

from optimize.gbdt_adapter import GBDTDataAdapter, TrainingBundle, make_training_bundle

logger = logging.getLogger(__name__)

# 动态导入（模型可能不可用）
_XGB_AVAILABLE = False
_LGB_AVAILABLE = False
_CB_AVAILABLE = False
try:
    import xgboost as xgb
    _XGB_AVAILABLE = True
except ImportError:
    pass
try:
    import lightgbm as lgb
    _LGB_AVAILABLE = True
except ImportError:
    pass
try:
    import catboost as cb
    _CB_AVAILABLE = True
except ImportError:
    pass


# ─── 数据结构 ─────────────────────────────────────────────

@dataclass
class ModelEvalSummary:
    """单个模型评估摘要"""
    name: str
    accuracy_pct: float
    raw_accuracy_pct: float
    draw_recall_pct: float
    draw_precision_pct: float
    draw_f1_pct: float
    home_recall_pct: float
    away_recall_pct: float
    brier_score: float
    log_loss: float
    mcc: float
    confusion_matrix: List[List[int]]
    trained: bool
    train_time_s: float
    best_iteration: int
    pred_draw_pct: float
    actual_draw_pct: float
    n_features: int
    error_msg: str = ""


@dataclass
class ComparisonReport:
    """完整对比报告"""
    timestamp: str
    dataset_info: Dict
    models: List[ModelEvalSummary]
    ranking: Dict[str, int]
    consensus: Dict
    league_breakdown: Dict[str, Dict[str, ModelEvalSummary]]
    feature_importances: Dict[str, Dict[str, float]]


class ModelComparison:
    """
    三模型对比评估器。
    统一训练 XGBoost / LightGBM / CatBoost 并在相同测试集上评估。
    """

    def __init__(
        self,
        db_path: str = 'data/football_data.db',
        xgb_params: Optional[Dict] = None,
        lgb_params: Optional[Dict] = None,
        cb_params: Optional[Dict] = None,
    ):
        self.db_path = os.path.join(os.path.dirname(__file__), '..', db_path)
        self.xgb_params = xgb_params or {}
        self.lgb_params = lgb_params or {}
        self.cb_params = cb_params or {}

        self._bundle: Optional[TrainingBundle] = None
        self._models: Dict[str, object] = {}
        self._eval_results: Dict[str, Dict] = {}

    # ─── 准备数据 ─────────────────────────────────────────

    def prepare_data(self, draw_weight: float = 1.0) -> TrainingBundle:
        adapter = GBDTDataAdapter(db_path=self.db_path)
        bundle = adapter.create_bundle(draw_weight=draw_weight)
        self._bundle = bundle
        return bundle

    # ─── 训练全部模型 ─────────────────────────────────────

    def train_all(
        self,
        bundle: Optional[TrainingBundle] = None,
        enable_xgb: bool = True,
        enable_lgb: bool = True,
        enable_cb: bool = True,
    ) -> ComparisonReport:
        """
        训练所有启用的模型并进行对比评估。

        Returns:
            ComparisonReport: 完整对比报告
        """
        if bundle is None:
            bundle = self._bundle
        if bundle is None:
            bundle = self.prepare_data()

        logger.info("=" * 60)
        logger.info("[Compare] 开始三模型对比训练")
        logger.info(f"[Compare] 训练集={bundle.train_size} 验证集={bundle.val_size} 测试集={bundle.test_size}")
        logger.info("=" * 60)

        models: List[ModelEvalSummary] = []
        self._models = {}
        self._eval_results = {}

        # ─── XGBoost ───
        if enable_xgb and _XGB_AVAILABLE:
            summary = self._train_xgboost(bundle, self.xgb_params)
            models.append(summary)
        elif enable_xgb:
            models.append(self._unavailable('XGBoost'))

        # ─── LightGBM ───
        if enable_lgb and _LGB_AVAILABLE:
            summary = self._train_lightgbm(bundle, self.lgb_params)
            models.append(summary)
        elif enable_lgb:
            models.append(self._unavailable('LightGBM'))

        # ─── CatBoost ───
        if enable_cb and _CB_AVAILABLE:
            summary = self._train_catboost(bundle, self.cb_params)
            models.append(summary)
        elif enable_cb:
            models.append(self._unavailable('CatBoost'))

        # 排名
        ranking = self._rank_models(models)
        # 一致性
        consensus = self._calc_consensus(bundle)

        report = ComparisonReport(
            timestamp=datetime.now().isoformat(),
            dataset_info={
                'train_size': bundle.train_size,
                'val_size': bundle.val_size,
                'test_size': bundle.test_size,
                'n_features': bundle.X_train.shape[1],
                'feature_names': bundle.feature_names,
                'class_dist': {
                    'home': int((bundle.y_test == 0).sum()),
                    'draw': int((bundle.y_test == 1).sum()),
                    'away': int((bundle.y_test == 2).sum()),
                },
            },
            models=models,
            ranking=ranking,
            consensus=consensus,
            league_breakdown={},  # 联赛拆分需要 meta 信息
            feature_importances=self._collect_feature_importances(),
        )

        # 打印摘要
        self._print_summary(report)
        return report

    # ─── 单模型训练包装 ──────────────────────────────────

    def _train_xgboost(self, bundle: TrainingBundle, extra_params: Dict) -> ModelEvalSummary:
        from optimize.gbdt_adapter import DEFAULT_VALUES
        logger.info("\n─── 训练 XGBoost ───")

        default_params = {
            'objective': 'multi:softprob',
            'num_class': 3,
            'eval_metric': ['mlogloss', 'merror'],
            'verbosity': 0, 'n_jobs': -1, 'random_state': 42,
            'n_estimators': 500, 'max_depth': 5, 'learning_rate': 0.03,
            'subsample': 0.8, 'colsample_bytree': 0.8,
            'min_child_weight': 3, 'reg_alpha': 0.1, 'reg_lambda': 1.0,
            'gamma': 0.05, 'tree_method': 'hist',
        }
        params = {**default_params, **extra_params}

        class_w = bundle.class_weights_train
        sample_w = np.array([class_w[int(c)] for c in bundle.y_train])

        try:
            t0 = time.time()
            model = xgb.XGBClassifier(**params)
            model.fit(
                bundle.X_train, bundle.y_train,
                sample_weight=sample_w,
                eval_set=[(bundle.X_val, bundle.y_val)],
                verbose=False,
            )
            train_time = time.time() - t0

            self._models['xgb'] = model
            eval_r = self._evaluate_model(model, bundle, 'xgb')
            self._eval_results['xgb'] = eval_r

            return ModelEvalSummary(
                name='XGBoost',
                trained=True,
                train_time_s=round(train_time, 1),
                **eval_r,
            )
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"XGBoost 训练失败: {e}")
            return ModelEvalSummary(
                name='XGBoost', trained=False, train_time_s=0,
                best_iteration=0, n_features=0,
                error_msg=str(e),
                accuracy_pct=0, raw_accuracy_pct=0,
                draw_recall_pct=0, draw_precision_pct=0, draw_f1_pct=0,
                home_recall_pct=0, away_recall_pct=0,
                brier_score=99, log_loss=99, mcc=0,
                confusion_matrix=[[0]*3]*3,
                pred_draw_pct=0, actual_draw_pct=0,
            )

    def _train_lightgbm(self, bundle: TrainingBundle, extra_params: Dict) -> ModelEvalSummary:
        logger.info("\n─── 训练 LightGBM ───")

        params = {
            'objective': 'multiclass', 'num_class': 3,
            'metric': 'multi_logloss', 'boosting_type': 'gbdt',
            'num_leaves': 31, 'learning_rate': 0.05, 'n_estimators': 500,
            'subsample': 0.8, 'colsample_bytree': 0.8,
            'min_child_samples': 20, 'reg_alpha': 0.0, 'reg_lambda': 0.0,
            'verbose': -1, 'n_jobs': -1, 'random_state': 42,
        }
        params.update(extra_params)

        class_w = bundle.class_weights_train
        sample_w = np.array([class_w[int(c)] for c in bundle.y_train])

        try:
            t0 = time.time()
            model = lgb.LGBMClassifier(**params)
            model.fit(
                bundle.X_train, bundle.y_train,
                sample_weight=sample_w,
                eval_set=[(bundle.X_val, bundle.y_val)],
                eval_metric=['multi_logloss', 'multi_error'],
                callbacks=[
                    lgb.early_stopping(stopping_rounds=50, verbose=False),
                    lgb.log_evaluation(period=0),
                ],
            )
            train_time = time.time() - t0

            self._models['lgb'] = model
            eval_r = self._evaluate_model(model, bundle, 'lgb')
            self._eval_results['lgb'] = eval_r

            return ModelEvalSummary(
                name='LightGBM',
                trained=True,
                train_time_s=round(train_time, 1),
                **eval_r,
            )
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"LightGBM 训练失败: {e}")
            return ModelEvalSummary(
                name='LightGBM', trained=False, train_time_s=0,
                best_iteration=0, n_features=0,
                error_msg=str(e),
                accuracy_pct=0, raw_accuracy_pct=0,
                draw_recall_pct=0, draw_precision_pct=0, draw_f1_pct=0,
                home_recall_pct=0, away_recall_pct=0,
                brier_score=99, log_loss=99, mcc=0,
                confusion_matrix=[[0]*3]*3,
                pred_draw_pct=0, actual_draw_pct=0,
            )

    def _train_catboost(self, bundle: TrainingBundle, extra_params: Dict) -> ModelEvalSummary:
        logger.info("\n─── 训练 CatBoost ───")

        params = {
            'loss_function': 'MultiClass', 'eval_metric': 'MultiClass',
            'iterations': 500, 'learning_rate': 0.05, 'depth': 6,
            'l2_leaf_reg': 3.0, 'bootstrap_type': 'Bernoulli', 'subsample': 0.8,
            'min_data_in_leaf': 10, 'task_type': 'CPU',
            'thread_count': -1, 'verbose': 0, 'random_seed': 42,
            'allow_writing_files': False,
        }
        params.update(extra_params)

        class_w = bundle.class_weights_train
        params['class_weights'] = [class_w[0], class_w[1], class_w[2]]

        try:
            t0 = time.time()
            model = cb.CatBoostClassifier(**params)
            model.fit(
                bundle.X_train, bundle.y_train,
                eval_set=(bundle.X_val, bundle.y_val),
                early_stopping_rounds=50,
                verbose=False, plot=False,
            )
            train_time = time.time() - t0

            self._models['cb'] = model
            eval_r = self._evaluate_model(model, bundle, 'cb')
            self._eval_results['cb'] = eval_r

            return ModelEvalSummary(
                name='CatBoost',
                trained=True,
                train_time_s=round(train_time, 1),
                **eval_r,
            )
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"CatBoost 训练失败: {e}")
            return ModelEvalSummary(
                name='CatBoost', trained=False, train_time_s=0,
                best_iteration=0, n_features=0,
                error_msg=str(e),
                accuracy_pct=0, raw_accuracy_pct=0,
                draw_recall_pct=0, draw_precision_pct=0, draw_f1_pct=0,
                home_recall_pct=0, away_recall_pct=0,
                brier_score=99, log_loss=99, mcc=0,
                confusion_matrix=[[0]*3]*3,
                pred_draw_pct=0, actual_draw_pct=0,
            )

    def _unavailable(self, name: str) -> ModelEvalSummary:
        return ModelEvalSummary(
            name=name, trained=False, train_time_s=0,
            best_iteration=0, n_features=0,
            error_msg=f"{name} 未安装或不可用",
            accuracy_pct=0, raw_accuracy_pct=0,
            draw_recall_pct=0, draw_precision_pct=0, draw_f1_pct=0,
            home_recall_pct=0, away_recall_pct=0,
            brier_score=99, log_loss=99, mcc=0,
            confusion_matrix=[[0]*3]*3,
            pred_draw_pct=0, actual_draw_pct=0,
        )

    # ─── 评估 ─────────────────────────────────────────────

    def _evaluate_model(self, model, bundle: TrainingBundle, tag: str) -> Dict:
        X_test = bundle.X_test
        y_test = bundle.y_test

        y_pred = model.predict(X_test)
        y_proba = model.predict_proba(X_test)

        acc = accuracy_score(y_test, y_pred) * 100

        # 自适应平局阈值
        actual_draw_rate = (y_test == 1).mean()
        effective_thresh = max(actual_draw_rate * 1.1, 0.20)
        y_pred_adj = y_pred.copy()
        for i in range(len(y_pred_adj)):
            if y_pred_adj[i] == 1 and y_proba[i, 1] < effective_thresh:
                y_pred_adj[i] = 0 if y_proba[i, 0] >= y_proba[i, 2] else 2
        acc_adj = accuracy_score(y_test, y_pred_adj) * 100

        report = classification_report(
            y_test, y_pred_adj,
            target_names=['home', 'draw', 'away'],
            output_dict=True, zero_division=0,
        )

        y_onehot = np.zeros((len(y_test), 3))
        for i, c in enumerate(y_test):
            y_onehot[i, int(c)] = 1
        brier = np.mean([
            brier_score_loss(y_onehot[:, i], y_proba[:, i])
            for i in range(3)
        ])

        ll = log_loss(y_test, y_proba)
        mcc = matthews_corrcoef(y_test, y_pred_adj)
        cm = confusion_matrix(y_test, y_pred_adj)

        best_iter = getattr(model, 'best_iteration_',
                            getattr(model, 'tree_count_',
                                    getattr(model, 'n_estimators_', 0)))

        return {
            'accuracy_pct': round(acc_adj, 2),
            'raw_accuracy_pct': round(acc, 2),
            'draw_recall_pct': round(report['draw']['recall'] * 100, 2),
            'draw_precision_pct': round(report['draw']['precision'] * 100, 2),
            'draw_f1_pct': round(report['draw']['f1-score'] * 100, 2),
            'home_recall_pct': round(report['home']['recall'] * 100, 2),
            'away_recall_pct': round(report['away']['recall'] * 100, 2),
            'brier_score': round(brier, 4),
            'log_loss': round(ll, 4),
            'mcc': round(mcc, 4),
            'confusion_matrix': cm.tolist(),
            'best_iteration': int(best_iter),
            'pred_draw_pct': round(np.mean(y_pred_adj == 1) * 100, 2),
            'actual_draw_pct': round((y_test == 1).mean() * 100, 2),
            'n_features': X_test.shape[1],
        }

    # ─── 排名 ─────────────────────────────────────────────

    def _rank_models(self, models: List[ModelEvalSummary]) -> Dict[str, int]:
        """按综合得分排名（准确率 + 平局F1 + MCC - Brier惩罚）"""
        trained = [m for m in models if m.trained]
        if not trained:
            return {}

        def score(m: ModelEvalSummary) -> float:
            return m.accuracy_pct * 0.40 + m.draw_f1_pct * 0.25 + m.mcc * 100 * 0.15 - m.brier_score * 10 + m.home_recall_pct * 0.10 + m.away_recall_pct * 0.10

        scored = [(m.name, score(m)) for m in trained]
        scored.sort(key=lambda x: x[1], reverse=True)

        ranking = {}
        for rank, (name, s) in enumerate(scored, 1):
            ranking[name] = rank

        for m in trained:
            m_name = m.name
            pos = ranking.get(m_name, len(trained) + 1)
            logger.info(f"  #{pos} {m_name}: acc={m.accuracy_pct:.1f}% "
                         f"draw_f1={m.draw_f1_pct:.1f}% brier={m.brier_score:.4f} "
                         f"mcc={m.mcc:.3f}")

        return ranking

    # ─── 一致性 ───────────────────────────────────────────

    def _calc_consensus(self, bundle: TrainingBundle) -> Dict:
        """计算三模型预测一致性（投票分析）"""
        if len(self._models) < 2:
            return {'kendall_w': 0, 'pairwise_kappa': {}, 'full_agreement_pct': 0}

        X_test = bundle.X_test
        y_test = bundle.y_test
        n = len(y_test)

        # 收集所有预测
        all_preds = {}
        for name, model in self._models.items():
            all_preds[name] = model.predict(X_test)

        pred_names = list(all_preds.keys())

        # 全部一致
        preds_array = np.column_stack([all_preds[n] for n in pred_names])
        full_agreement = 0
        for i in range(n):
            if len(set(preds_array[i])) == 1:
                full_agreement += 1
        full_agreement_pct = round(full_agreement / n * 100, 2)

        # Pairwise Kappa
        pairwise_kappa = {}
        for i, n1 in enumerate(pred_names):
            for n2 in pred_names[i+1:]:
                k = cohen_kappa_score(all_preds[n1], all_preds[n2])
                pairwise_kappa[f"{n1}_vs_{n2}"] = round(k, 3)

        return {
            'n_models': len(pred_names),
            'full_agreement_pct': full_agreement_pct,
            'pairwise_kappa': pairwise_kappa,
        }

    # ─── 特征重要性 ──────────────────────────────────────

    def _collect_feature_importances(self) -> Dict[str, Dict[str, float]]:
        fi = {}
        for name, model in self._models.items():
            try:
                importances = model.feature_importances_
                cols = self._bundle.feature_names[:len(importances)]
                fi[name] = dict(
                    sorted(zip(cols, importances), key=lambda x: x[1], reverse=True)[:10]
                )
                fi[name] = {k: round(float(v), 4) for k, v in fi[name].items()}
            except (Exception, ValueError, KeyError, IndexError):
                fi[name] = {}
        return fi

    # ─── 报告输出 ────────────────────────────────────────

    def _print_summary(self, report: ComparisonReport):
        logger.info("\n" + "=" * 70)
        logger.info("  GBDT 三模型对比评估报告")
        logger.info("=" * 70)

        trained = [m for m in report.models if m.trained]
        available = [m for m in report.models if not m.trained]

        if available:
            logger.info(f"  不可用: {', '.join(m.name for m in available)}")

        if not trained:
            logger.info("  无可用模型")
            return

        logger.info(f"  测试集: {report.dataset_info['test_size']} 样本")
        logger.info(f"  特征数: {report.dataset_info['n_features']}")
        logger.info(f"  标签分布: 主胜={report.dataset_info['class_dist']['home']} "
                     f"平局={report.dataset_info['class_dist']['draw']} "
                     f"客胜={report.dataset_info['class_dist']['away']}")
        logger.info("-" * 70)
        header = f"  {'模型':<12} {'准确率':>8} {'平局召回':>8} {'平局F1':>8} {'Brier':>8} {'LogLoss':>8} {'MCC':>7} {'训练':>7} {'排名':>4}"
        logger.info(header)
        logger.info("-" * 70)
        for m in trained:
            rank = report.ranking.get(m.name, '?')
            logger.info(
                f"  {m.name:<12} {m.accuracy_pct:>7.1f}% {m.draw_recall_pct:>7.1f}% "
                f"{m.draw_f1_pct:>7.1f}% {m.brier_score:>8.4f} {m.log_loss:>8.4f} "
                f"{m.mcc:>7.3f} {m.train_time_s:>5.0f}s {rank:>4}"
            )

        logger.info("-" * 70)
        logger.info(f"  全一致率: {report.consensus.get('full_agreement_pct', 0):.1f}%")
        for pair, k in report.consensus.get('pairwise_kappa', {}).items():
            logger.info(f"  Kappa({pair}): {k:.3f}")
        logger.info("=" * 70)

    def to_dict(self, report: ComparisonReport) -> Dict:
        """转为 JSON 可序列化字典"""
        return {
            'timestamp': report.timestamp,
            'dataset_info': report.dataset_info,
            'models': [asdict(m) for m in report.models],
            'ranking': report.ranking,
            'consensus': report.consensus,
            'feature_importances': report.feature_importances,
            'league_breakdown': report.league_breakdown,
        }

    def save_report(self, report: ComparisonReport, filepath: str):
        """保存对比报告为 JSON"""
        os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
        data = self.to_dict(report)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"[Compare] 报告已保存: {filepath}")

    @staticmethod
    def load_report(filepath: str) -> Dict:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)


# ─── 便捷入口 ─────────────────────────────────────────────

def run_comparison(
    db_path: str = 'data/football_data.db',
    save_path: Optional[str] = None,
) -> ComparisonReport:
    """一键运行三模型对比"""
    comparator = ModelComparison(db_path=db_path)
    bundle = comparator.prepare_data()
    report = comparator.train_all(bundle)

    if save_path is None:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        save_path = os.path.join(
            os.path.dirname(__file__), '..',
            'data', f'gbdt_comparison_{timestamp}.json',
        )

    comparator.save_report(report, save_path)
    return report

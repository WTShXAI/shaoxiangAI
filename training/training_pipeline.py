"""
哨响AI - 自动化训练流水线 v1.0
===============================
集成模型注册表（ModelRegistry v2.0），每轮训练自动：
1. 计算训练数据哈希
2. 训练/评估
3. 自动注册到注册表
4. 与当前生产模型对比
5. 若性能提升超过阈值，自动晋升为生产版本

用法:
    # 手动触发训练
    python training/training_pipeline.py --data latest

    # 守护模式（每次数据更新后自动触发）
    python training/training_pipeline.py --daemon --interval 3600

    # 仅评估已注册模型
    python training/training_pipeline.py --evaluate-only
"""
import os
import sys
import json
import time
import hashlib
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

import pandas as pd
import numpy as np

# 将项目根加入路径
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from ensemble_trainer import EnsembleTrainer
from optimize.model_registry import ModelRegistry, get_registry
from optimize.calibration import compute_ece

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [Pipeline] %(message)s'
)
logger = logging.getLogger('TrainingPipeline')

class TrainingPipeline:
    """自动化训练流水线 — 集成版本管理"""

    def __init__(self, auto_promote_threshold: float = 0.3):
        self.registry = get_registry()
        self.auto_promote_threshold = auto_promote_threshold
        self._last_data_hash: Optional[str] = None

    # ══════════════════════════════════════════════════
    # 工具
    # ══════════════════════════════════════════════════

    @staticmethod
    def _compute_data_hash(df: pd.DataFrame) -> str:
        """计算训练数据哈希"""
        # 基于 shape + 最后 N 行的内容摘要
        n = min(len(df), 100)
        sample = df.tail(n).to_json(orient='records', default_handler=str)
        combined = f"{len(df)}:{len(df.columns)}:{sample}"
        return hashlib.sha256(combined.encode()).hexdigest()[:16]

    @staticmethod
    def _suggest_semver(registry: ModelRegistry) -> str:
        """自动建议下一个语义化版本"""
        # 获取现有最大 semver
        best = None
        for m in registry._data['models'].values():
            sv = m.get('semver')
            if sv and ModelRegistry.validate_semver(sv):
                t = ModelRegistry._semver_tuple(sv)
                if best is None or t > best:
                    best = t
        if best is None:
            return '3.0.0'
        # PATCH 递增
        return f"{best[0]}.{best[1]}.{best[2]+1}"

    def _evaluate_model(self, trainer: EnsembleTrainer,
                        df_test: pd.DataFrame,
                        draw_threshold: float = 0.32) -> Dict[str, Any]:
        """全面评估模型 (P1: 使用阈值判型, 与生产对齐)
        
        Args:
            draw_threshold: 平局判型阈值 (0.32=P0最优, 与生产一致)
        """
        X_test, y_test = trainer.prepare_features(df_test)
        # prepare_features 返回 DataFrame, ensemble_predict_proba 需要 numpy
        X_np = X_test.values if hasattr(X_test, 'values') else np.asarray(X_test)
        proba = trainer.ensemble_predict_proba(X_np)
        y_true = np.asarray(y_test, dtype=int).ravel()

        # P1: 阈值判型 (与生产一致), 替代原 argmax
        # argmax 模式下 pD 永远不是最大值 → 平局F1=0 (结构性缺陷)
        y_pred = self._predict_with_threshold(proba, draw_threshold=draw_threshold)
        # 同时保留 argmax 结果用于对比诊断
        y_pred_argmax = proba.argmax(axis=1)

        acc = (y_pred == y_true).mean()
        ece = compute_ece(proba, y_true, n_bins=10)

        # 各类 F1
        from sklearn.metrics import f1_score, recall_score, log_loss, matthews_corrcoef
        f1_per = f1_score(y_true, y_pred, average=None, labels=[0, 1, 2], zero_division=0)
        recall_per = recall_score(y_true, y_pred, average=None, labels=[0, 1, 2], zero_division=0)
        ll = log_loss(y_true, proba, labels=[0, 1, 2])
        mcc = matthews_corrcoef(y_true, y_pred)

        return {
            'accuracy': round(float(acc * 100), 2),
            'home_recall': round(float(recall_per[0] * 100), 2),
            'draw_recall': round(float(recall_per[1] * 100), 2),
            'away_recall': round(float(recall_per[2] * 100), 2),
            'draw_f1': round(float(f1_per[1] * 100), 2),
            'f1_macro': round(float(f1_score(y_true, y_pred, average='macro', zero_division=0) * 100), 2),
            'brier': round(float(((proba - np.eye(3)[y_true])**2).mean()), 6),
            'log_loss': round(float(ll), 6),
            'ece': round(float(ece), 6),
            'mcc': round(float(mcc), 4),
            'test_samples': len(y_test),
            'n_features': X_test.shape[1],
            'pred_distribution': proba.mean(axis=0).tolist(),
            'actual_distribution': (np.bincount(y_true, minlength=3) / len(y_true)).tolist(),
            'classification_mode': f'threshold({draw_threshold})',
            # P1: 同时保留 argmax 指标用于对比诊断
            'argmax_accuracy': round(float((y_pred_argmax == y_true).mean() * 100), 2),
            'argmax_draw_f1': round(float(f1_score(y_true, y_pred_argmax, labels=[1], average='macro', zero_division=0) * 100), 2),
        }

    @staticmethod
    def _predict_with_threshold(proba: np.ndarray, draw_threshold: float = 0.32) -> np.ndarray:
        """P1: 与生产一致的阈值判型 (替代 argmax)
        
        pD > threshold → Draw
        elif pH > pA → Home
        else → Away
        """
        y_pred = np.zeros(len(proba), dtype=int)
        for i in range(len(proba)):
            if proba[i, 1] > draw_threshold:
                y_pred[i] = 1  # Draw
            elif proba[i, 0] > proba[i, 2]:
                y_pred[i] = 0  # Home
            else:
                y_pred[i] = 2  # Away
        return y_pred

    def _build_high_draw_validation_set(self, df_test: pd.DataFrame) -> pd.DataFrame:
        """P1: 构建高平局率场景验证集
        
        策略: 从 OOF 数据中筛选"均衡赛"子集 (赔率 spread 小 → 平局率偏高)
        世界杯平局率 38.5%, 联赛均衡赛子集平局率可达 35-40%
        """
        if 'odds_spread' not in df_test.columns or len(df_test) == 0:
            return pd.DataFrame()

        # 筛选条件: 赔率 spread < 0.8 (均衡赛, 平局率高)
        # odds_spread = away_odds - home_odds, 值越小越均衡
        if 'odds_spread' in df_test.columns:
            balanced_mask = df_test['odds_spread'].abs() < 0.8
            df_balanced = df_test[balanced_mask].copy()
        else:
            return pd.DataFrame()

        # 如果均衡赛数据不足, 放宽条件
        if len(df_balanced) < 50:
            balanced_mask = df_test['odds_spread'].abs() < 1.5
            df_balanced = df_test[balanced_mask].copy()

        logger.info(f"[P1] 高平局率验证集: {len(df_balanced)} 场均衡赛 (spread<0.8)")
        return df_balanced

    def _evaluate_multi_scenario(self, trainer: EnsembleTrainer,
                                  df_test: pd.DataFrame) -> Dict[str, Any]:
        """P1: 多场景评估 — 联赛 OOF + 高平局率均衡赛子集"""
        # 场景1: 标准 OOF (全量联赛数据)
        metrics_league = self._evaluate_model(trainer, df_test)

        # 场景2: 高平局率均衡赛子集
        df_balanced = self._build_high_draw_validation_set(df_test)
        metrics_balanced = None
        if len(df_balanced) >= 50:
            metrics_balanced = self._evaluate_model(trainer, df_balanced)
            metrics_balanced['scenario'] = 'high_draw_rate_balanced'

        # 综合评分: 联赛 60% + 均衡赛 40% (如果有)
        if metrics_balanced:
            composite_acc = metrics_league['accuracy'] * 0.6 + metrics_balanced['accuracy'] * 0.4
            composite_df1 = metrics_league['draw_f1'] * 0.5 + metrics_balanced['draw_f1'] * 0.5
        else:
            composite_acc = metrics_league['accuracy']
            composite_df1 = metrics_league['draw_f1']

        return {
            'league_oof': metrics_league,
            'high_draw_rate': metrics_balanced,
            'composite_accuracy': round(composite_acc, 2),
            'composite_draw_f1': round(composite_df1, 2),
        }

    # ══════════════════════════════════════════════════
    # 核心流水线
    # ══════════════════════════════════════════════════

    def run(self, df_train: pd.DataFrame, df_test: pd.DataFrame,
            description: str = '', n_estimators: int = 500) -> Dict[str, Any]:
        """
        运行完整训练流水线: 训练 → 评估 → 注册 → 对比 → 晋升

        Returns:
            pipeline 结果摘要
        """
        start_time = time.time()
        data_hash = self._compute_data_hash(df_train)

        # 如果数据未变化且不是首次训练，跳过
        if self._last_data_hash == data_hash:
            logger.info("训练数据未变化，跳过训练")
            return {'status': 'skipped', 'reason': 'data_unchanged'}

        self._last_data_hash = data_hash

        # ── 1. 训练 ──
        logger.info(f"开始训练 ({len(df_train)} 训练 / {len(df_test)} 测试)")
        trainer = EnsembleTrainer()
        trainer.config['models']['xgboost']['n_estimators'] = n_estimators
        trainer.train(df_train)

        train_seconds = time.time() - start_time
        logger.info(f"训练完成 ({train_seconds:.0f}s)")

        # ── 2. 评估 (P1: 多场景评估) ──
        multi_metrics = self._evaluate_multi_scenario(trainer, df_test)
        metrics = multi_metrics['league_oof']  # 向后兼容: 主指标用联赛 OOF
        logger.info(f"评估 [联赛OOF]: Acc={metrics['accuracy']:.1f}% | "
                     f"D-F1={metrics['draw_f1']:.1f}% | ECE={metrics['ece']:.4f} | "
                     f"判型={metrics.get('classification_mode', '?')}")
        if multi_metrics['high_draw_rate']:
            hdr = multi_metrics['high_draw_rate']
            logger.info(f"评估 [高平局率]: Acc={hdr['accuracy']:.1f}% | D-F1={hdr['draw_f1']:.1f}% | "
                        f"样本={hdr['test_samples']}")
        logger.info(f"评估 [综合]: Acc={multi_metrics['composite_accuracy']:.1f}% | "
                     f"D-F1={multi_metrics['composite_draw_f1']:.1f}%")

        # ── 3. 保存模型 ──
        semver = self._suggest_semver(self.registry)
        saved_models_dir = os.path.join(_project_root, 'saved_models')
        os.makedirs(saved_models_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        model_filename = f"football_ensemble_{timestamp}.joblib"
        model_path = os.path.join(saved_models_dir, model_filename)

        import joblib
        joblib.dump(trainer, model_path)

        # ── 4. 注册模型 ──
        model_id = self.registry.register(
            model_path=model_path,
            metrics=metrics,
            model_type='ensemble',
            source='auto_pipeline',
            semver=semver,
            description=description or f"自动训练 {timestamp}",
            tags=['auto'],
            training_data_info={
                'hash': data_hash,
                'n_samples': len(df_train),
                'n_features': metrics['n_features'],
                'date_range': str(df_train['match_date'].agg(['min', 'max']).to_dict())
                if 'match_date' in df_train.columns else None,
            },
        )
        logger.info(f"模型已注册: {model_id} (v{semver})")

        # ── 5. 与生产版本对比 ──
        prod = self.registry.get_production_version()
        comparison = None
        if prod and prod['model_id'] != model_id:
            comparison = self.registry.compare_versions(prod['model_id'], model_id)
            logger.info(f"版本对比: {comparison['verdict']}")

            # ── 6. 自动晋升 (P1: 增加 draw_f1 不下降约束) ──
            accuracy_gain = metrics['accuracy'] - prod['metrics']['accuracy']
            prod_draw_f1 = prod['metrics'].get('draw_f1', 0)
            draw_f1_change = metrics['draw_f1'] - prod_draw_f1

            # P1: 准确率提升 AND 平局 F1 不下降 (容忍 -2pp)
            if accuracy_gain >= self.auto_promote_threshold and draw_f1_change >= -2.0:
                self.registry.deploy(model_id)
                logger.info(f"✅ 自动晋升: {prod['model_id']} → {model_id} "
                           f"(+{accuracy_gain:.1f}pp Acc, ΔF1={draw_f1_change:+.1f}pp)")
            elif accuracy_gain >= self.auto_promote_threshold and draw_f1_change < -2.0:
                logger.warning(f"⛔ 拒绝晋升: Acc +{accuracy_gain:.1f}pp 但 Draw-F1 "
                               f"{prod_draw_f1:.1f}→{metrics['draw_f1']:.1f} ({draw_f1_change:+.1f}pp)")

        elif not prod:
            # 无生产版本，直接晋升
            self.registry.deploy(model_id)
            logger.info(f"✅ 首次部署: {model_id}")

        elapsed = time.time() - start_time
        result = {
            'status': 'success',
            'model_id': model_id,
            'semver': semver,
            'model_path': model_path,
            'metrics': metrics,
            'multi_scenario_metrics': multi_metrics,  # P1: 多场景评估结果
            'train_seconds': round(train_seconds, 1),
            'total_seconds': round(elapsed, 1),
            'comparison': comparison,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }

        logger.info(f"流水线完成 ({elapsed:.0f}s)")

        # ── 自动触发 E1-E7 评估管道 ──
        self._trigger_evaluation()

        return result

    def _trigger_evaluation(self):
        """训练/回测后自动运行E1-E7七维评估管道"""
        try:
            from agents.evaluator.evaluation_pipeline import EvaluationPipeline  # pyright: ignore[reportImplicitRelativeImport]
            db_path = os.path.join(_project_root, "data", "football_data.db")
            pipeline = EvaluationPipeline(db_path=db_path)
            eval_result = pipeline.run(trigger_type="auto", skip_if_unchanged=False)
            logger.info(
                f"[评估] E1-E7完成: 综合评分 {eval_result['overall_score']}/100 "
                f"{eval_result['overall_rating']} | 紧迫度: {eval_result['urgency']}"
            )
            if eval_result.get('action_items'):
                for item in eval_result['action_items'][:5]:
                    logger.warning(f"  [评估] ⚠️ {item}")
        except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
            logger.warning(f"[评估] E1-E7管道执行异常(非致命): {e}")

    # ══════════════════════════════════════════════════
    # 守护模式
    # ══════════════════════════════════════════════════

    def run_daemon(self, interval_seconds: int = 3600,
                   n_estimators: int = 500):
        """守护模式: 定期检查新数据并自动训练"""
        logger.info(f"启动守护模式 (间隔 {interval_seconds}s)")

        trainer = EnsembleTrainer()
        while True:
            try:
                df = trainer.load_training_data()

                # 时间序列切分
                cutoff = '2024-01-01' if len(df) > 5000 else '2023-01-01'
                train_mask = df['match_date'] < cutoff
                test_mask = df['match_date'] >= cutoff

                df_train = df[train_mask].copy()
                df_test = df[test_mask].copy()

                data_hash = self._compute_data_hash(df_train)
                if self._last_data_hash != data_hash:
                    self.run(df_train, df_test, n_estimators=n_estimators,
                             description=f"守护模式自动训练 {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")

            except (Exception, KeyError, IndexError) as e:
                logger.error(f"守护模式异常: {e}", exc_info=True)

            time.sleep(interval_seconds)

# ══════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='哨响AI — 自动化训练流水线')
    parser.add_argument('--data', type=str, default='latest',
                        choices=['latest', 'all', 'recent'],
                        help='训练数据范围')
    parser.add_argument('--daemon', action='store_true', help='守护模式')
    parser.add_argument('--interval', type=int, default=3600, help='守护模式间隔 (秒)')
    parser.add_argument('--trees', type=int, default=500, help='XGBoost 树数')
    parser.add_argument('--threshold', type=float, default=0.3,
                        help='自动晋升准确率增益阈值 (pp)')
    parser.add_argument('--desc', type=str, default='', help='版本描述')
    parser.add_argument('--evaluate-only', action='store_true', help='仅评估+注册已加载模型')

    args = parser.parse_args()
    pipeline = TrainingPipeline(auto_promote_threshold=args.threshold)

    if args.evaluate_only:
        print("评估模式: 加载当前数据并评估（不训练）")
        trainer = EnsembleTrainer()
        df = trainer.load_training_data()
        metrics = pipeline._evaluate_model(trainer, df.tail(1000))
        print(json.dumps(metrics, indent=2))
        exit(0)

    if args.daemon:
        pipeline.run_daemon(interval_seconds=args.interval, n_estimators=args.trees)
    else:
        trainer = EnsembleTrainer()
        df = trainer.load_training_data()

        # 时间序列切分
        cutoff = '2024-01-01' if len(df) > 5000 else '2023-01-01'
        train_mask = df['match_date'] < cutoff
        test_mask = df['match_date'] >= cutoff

        df_train = df[train_mask].copy()
        df_test = df[test_mask].copy()

        logger.info(f"数据划分: 训练={len(df_train)} (截止{cutoff}) 测试={len(df_test)}")
        result = pipeline.run(df_train, df_test, description=args.desc,
                              n_estimators=args.trees)

        # 输出结果摘要
        print(f"\n{'='*60}")
        print(f"  Pipeline 完成")
        print(f"  模型ID: {result.get('model_id', '?')}")
        print(f"  版本: v{result.get('semver', '?')}")
        print(f"  耗时: {result.get('total_seconds', 0):.0f}s")
        if result.get('metrics'):
            m = result['metrics']
            print(f"  准确率: {m['accuracy']:.1f}%")
            print(f"  平局F1: {m['draw_f1']:.1f}%")
            print(f"  客胜召回: {m['away_recall']:.1f}%")
            print(f"  ECE: {m['ece']:.4f}")
        print(f"{'='*60}")

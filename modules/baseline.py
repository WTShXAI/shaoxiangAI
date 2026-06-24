#!/usr/bin/env python3
"""
哨响AI - 基准模型体系
=====================
提供多种简单基准模型，用于评估智能体系统是否"显著优于基准"。
遵循"设立简单基准 → 验证显著优于基准"原则。

基准类型:
1. AlwaysHomeBaseline   — 恒定预测主胜 (足球领域最朴素基准)
2. LogisticBaseline     — sklearn LogisticRegression 多分类
3. RankOnlyBaseline     — 仅用排名差做逻辑回归
4. BaselineComparator   — 对比评估器

用法:
    from modules.baseline import BaselineComparator
    comp = BaselineComparator(db_path='data/football_data.db')
    report = comp.compare_all()
"""
import sqlite3
import logging
import json
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import (
    accuracy_score, classification_report, confusion_matrix,
    log_loss, brier_score_loss, matthews_corrcoef
)

logger = logging.getLogger(__name__)


# ============================================================
# 基准模型 1: 恒定主胜
# ============================================================
class AlwaysHomeBaseline:
    """
    最朴素基准：总是预测主队胜。
    足球比赛中主胜约45%，平局约25%，客胜约30%。
    此基准准确率约40-48%（取决于联赛）。
    系统准确率必须 >48% 才算"显著优于基准"。
    """
    def __init__(self):
        self.name = "AlwaysHome"
        self.version = "1.0"

    def predict(self, features=None) -> Dict:
        return {'home': 1.0, 'draw': 0.0, 'away': 0.0, 'prediction': 'home'}

    def predict_proba(self, X=None) -> np.ndarray:
        n = X.shape[0] if X is not None else 1
        return np.tile([1.0, 0.0, 0.0], (n, 1))

    def predict_batch(self, feature_list: List) -> List[Dict]:
        return [self.predict() for _ in feature_list]


# ============================================================
# 基准模型 2: 逻辑回归
# ============================================================
class LogisticBaseline:
    """
    简单逻辑回归基准。
    使用少量核心特征（rank_diff, form, h2h）训练多分类LR。
    这代表了"最简单但有学习能力"的基准。
    """
    def __init__(self):
        self.name = "LogisticRegression"
        self.version = "1.0"
        self.model: Optional[LogisticRegression] = None
        self.scaler: Optional[StandardScaler] = None
        self.feature_names: List[str] = []
        self.is_fitted = False

    def fit(self, X: np.ndarray, y: np.ndarray, feature_names: List[str] = None):
        """训练逻辑回归模型"""
        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X)
        self.model = LogisticRegression(
            multi_class='multinomial',
            solver='lbfgs',
            max_iter=1000,
            class_weight='balanced',
            random_state=42
        )
        self.model.fit(X_scaled, y)
        self.feature_names = feature_names or [f'f{i}' for i in range(X.shape[1])]
        self.is_fitted = True
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        if not self.is_fitted:
            return np.full(X.shape[0], 0)  # 默认 home
        return self.model.predict(self.scaler.transform(X))

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if not self.is_fitted:
            n = X.shape[0]
            return np.tile([1.0, 0.0, 0.0], (n, 1))
        return self.model.predict_proba(self.scaler.transform(X))


# ============================================================
# 基准模型 3: 仅排名差逻辑回归
# ============================================================
class RankOnlyBaseline:
    """
    极简基准：仅用排名差一个特征。
    用于验证"复杂特征工程是否有增量价值"。
    """
    def __init__(self):
        self.name = "RankOnly"
        self.version = "1.0"
        self.model: Optional[LogisticRegression] = None
        self.is_fitted = False

    def fit(self, rank_diffs: np.ndarray, y: np.ndarray):
        X = rank_diffs.reshape(-1, 1)
        self.model = LogisticRegression(
            multi_class='multinomial',
            solver='lbfgs',
            max_iter=500,
            class_weight='balanced',
            random_state=42
        )
        self.model.fit(X, y)
        self.is_fitted = True
        return self

    def predict_proba(self, rank_diffs: np.ndarray) -> np.ndarray:
        if not self.is_fitted:
            n = len(rank_diffs)
            return np.tile([1.0, 0.0, 0.0], (n, 1))
        return self.model.predict_proba(rank_diffs.reshape(-1, 1))


# ============================================================
# 基准对比评估器
# ============================================================
class BaselineComparator:
    """
    基准对比评估器 —— 核心工具。

    功能:
    - 从数据库加载可训练数据
    - 训练/评估多种基准模型
    - 与集成模型对比
    - 输出结构化评估报告

    用法:
        comp = BaselineComparator()
        report = comp.compare_all()
        print(report['summary'])
    """

    # 核心特征列表 (与 ensemble_trainer 对齐)
    CORE_FEATURES = [
        'rank_diff_factor', 'form_momentum', 'h2h_factor',
        'a1', 'a2', 'a3', 'sigma_trap', 'lambda_crush', 'epsilon_senti',
        'delta_fatigue', 'beta_dev', 'card_risk', 'aerial_advantage',
        'press_intensity', 'home_advantage', 'power_gap',
        'market_consensus', 'value_gap', 'league_strength'
    ]

    # 最少特征集 (即使大部分缺失也能用的)
    MINIMAL_FEATURES = [
        'rank_diff_factor', 'form_momentum', 'h2h_factor'
    ]

    def __init__(self, db_path: str = None):
        self.db_path = db_path or 'data/football_data.db'
        self.baselines = {
            'always_home': AlwaysHomeBaseline(),
            'logistic': LogisticBaseline(),
            'rank_only': RankOnlyBaseline(),
        }
        self.results: Dict = {}

    def load_data(self) -> Tuple[pd.DataFrame, np.ndarray]:
        """从数据库加载可训练数据"""
        conn = sqlite3.connect(self.db_path)
        try:
            df = pd.read_sql_query("""
                SELECT m.match_id, m.home_team, m.away_team,
                       m.home_score, m.away_score, m.competition_name,
                       mf.*
                FROM matches m
                JOIN match_features mf ON m.match_id = mf.match_id
                WHERE m.status = 'finished'
                  AND m.home_score IS NOT NULL
                  AND m.away_score IS NOT NULL
                  AND m.home_score != m.away_score OR m.home_score = m.away_score
                ORDER BY m.match_date ASC
            """, conn)
        finally:
            conn.close()

        # 构建标签
        df['result'] = df.apply(
            lambda r: 0 if r['home_score'] > r['away_score']
            else (1 if r['home_score'] == r['away_score'] else 2),
            axis=1
        )
        labels = df['result'].values
        return df, labels

    def evaluate_baseline(self, name: str, model, X: np.ndarray, y: np.ndarray,
                          n_splits: int = 5) -> Dict:
        """使用时序交叉验证评估单个基线"""
        tscv = TimeSeriesSplit(n_splits=n_splits)

        fold_results = []
        all_y_true = []
        all_y_pred = []

        for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
            X_train, X_test = X[train_idx], X[test_idx]
            y_train, y_test = y[train_idx], y[test_idx]

            if hasattr(model, 'fit'):
                model.fit(X_train, y_train)
                y_pred = model.predict(X_test)
            else:
                # AlwaysHome 无 fit
                y_pred = np.full(len(y_test), 0)

            acc = accuracy_score(y_test, y_pred)
            fold_results.append(acc)
            all_y_true.extend(y_test)
            all_y_pred.extend(y_pred)

        macro_acc = np.mean(fold_results)
        report = classification_report(
            all_y_true, all_y_pred,
            target_names=['home', 'draw', 'away'],
            output_dict=True, zero_division=0
        )

        return {
            'model': name,
            'cv_folds': n_splits,
            'fold_accuracies': fold_results,
            'overall_accuracy': float(macro_acc),
            'std_accuracy': float(np.std(fold_results)),
            'per_class': report,
            'confusion_matrix': confusion_matrix(all_y_true, all_y_pred).tolist(),
            'timestamp': datetime.now().isoformat()
        }

    def evaluate_all_baselines(self) -> Dict:
        """评估所有基准模型"""
        df, labels = self.load_data()
        logger.info(f"加载 {len(df)} 条可训练数据")

        # 准备特征矩阵
        feature_columns = [c for c in self.CORE_FEATURES if c in df.columns]
        if len(feature_columns) < 3:
            feature_columns = [c for c in self.MINIMAL_FEATURES if c in df.columns]

        logger.info(f"使用 {len(feature_columns)} 个特征: {feature_columns}")

        X = df[feature_columns].fillna(0).values
        y = labels

        results = {}
        for name, model in self.baselines.items():
            logger.info(f"评估 {name}...")
            if name == 'rank_only':
                rank_col = 'rank_diff_factor' if 'rank_diff_factor' in df.columns else feature_columns[0]
                X_rank = df[rank_col].fillna(0).values
                result = self.evaluate_baseline(name, model, X_rank.reshape(-1, 1), y)
            else:
                result = self.evaluate_baseline(name, model, X, y)
            results[name] = result
            logger.info(f"  {name}: 准确率 {result['overall_accuracy']:.4f} ± {result['std_accuracy']:.4f}")

        self.results = results
        return results

    def compare_with_ensemble(self, ensemble_accuracy: float = None) -> Dict:
        """与集成模型对比"""
        if not self.results:
            self.evaluate_all_baselines()

        baseline_accuracies = {
            name: r['overall_accuracy']
            for name, r in self.results.items()
        }

        best_baseline = max(baseline_accuracies, key=baseline_accuracies.get)
        best_baseline_acc = baseline_accuracies[best_baseline]

        comparison = {
            'baselines': baseline_accuracies,
            'best_baseline': best_baseline,
            'best_baseline_accuracy': best_baseline_acc,
            'ensemble_accuracy': ensemble_accuracy,
        }

        if ensemble_accuracy is not None:
            delta = ensemble_accuracy - best_baseline_acc
            comparison['delta_vs_best'] = float(delta)
            comparison['significantly_better'] = delta > 0.03  # >3pp 显著
            comparison['passes_threshold'] = ensemble_accuracy > 0.40  # >40%

        return comparison

    def generate_report(self, ensemble_accuracy: float = None) -> Dict:
        """生成完整评估报告"""
        if not self.results:
            self.evaluate_all_baselines()

        comparison = self.compare_with_ensemble(ensemble_accuracy)

        summary_lines = []
        summary_lines.append("=" * 60)
        summary_lines.append("  哨响AI 基准模型评估报告")
        summary_lines.append("=" * 60)
        summary_lines.append(f"  评估时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        summary_lines.append(f"  数据样本: {len(self.results.get('always_home', {}).get('fold_accuracies', [0])) * 5 if self.results else 'N/A'}")

        summary_lines.append("")
        summary_lines.append("  📊 基准模型准确率:")
        for name, acc in comparison.get('baselines', {}).items():
            bar = "█" * int(acc * 50)
            summary_lines.append(f"    {name:<20} {acc:.4f}  {bar}")

        if ensemble_accuracy is not None:
            best_acc = comparison.get('best_baseline_accuracy', 0)
            delta = comparison.get('delta_vs_best', 0)
            summary_lines.append("")
            summary_lines.append("  🔬 集成模型对比:")
            summary_lines.append(f"    Ensemble v3.0:    {ensemble_accuracy:.4f}")
            summary_lines.append(f"    最佳基准:         {best_acc:.4f}")
            summary_lines.append(f"    差值:             {delta:+.4f}")

            if comparison.get('significantly_better'):
                summary_lines.append(f"    ✅ 显著优于基准 (>{best_acc+0.03:.4f})")
            else:
                summary_lines.append(f"    ⚠️ 未显著优于基准")

            if comparison.get('passes_threshold'):
                summary_lines.append(f"    ✅ 超过 40% 上线阈值")
            else:
                summary_lines.append(f"    ❌ 未达到 40% 上线阈值")

        summary_lines.append("")
        summary_lines.append("=" * 60)

        report = {
            'summary': '\n'.join(summary_lines),
            'details': self.results,
            'comparison': comparison,
            'generated_at': datetime.now().isoformat()
        }

        return report

    def save_report(self, filepath: str = None):
        """保存报告到文件"""
        if filepath is None:
            filepath = f"reports/baseline_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        if not self.results:
            self.evaluate_all_baselines()

        report = self.generate_report()

        import os
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)

        logger.info(f"报告已保存: {filepath}")
        return filepath


# ============================================================
# 便捷函数
# ============================================================
def create_baselines() -> Dict[str, Any]:
    """创建所有基准模型"""
    return {
        'always_home': AlwaysHomeBaseline(),
        'logistic': LogisticBaseline(),
        'rank_only': RankOnlyBaseline(),
    }


def quick_benchmark(db_path: str = None) -> Dict:
    """快速基准测试"""
    comparator = BaselineComparator(db_path or 'data/football_data.db')
    return comparator.generate_report()


# ============================================================
# CLI
# ============================================================
if __name__ == '__main__':
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )

    parser = argparse.ArgumentParser(description='哨响AI 基准模型评估')
    parser.add_argument('--db', default='data/football_data.db', help='数据库路径')
    parser.add_argument('--ensemble-acc', type=float, default=None,
                       help='集成模型准确率 (用于对比)')
    parser.add_argument('--output', default=None, help='输出JSON报告路径')
    parser.add_argument('--compare-only', action='store_true',
                       help='仅对比，不重新评估基线')

    args = parser.parse_args()

    comparator = BaselineComparator(db_path=args.db)

    if not args.compare_only:
        print("🔄 评估所有基准模型...")
        comparator.evaluate_all_baselines()

    report = comparator.generate_report(ensemble_accuracy=args.ensemble_acc)
    print(report['summary'])

    if args.output or not args.compare_only:
        out = comparator.save_report(args.output)
        print(f"\n📁 报告已保存: {out}")

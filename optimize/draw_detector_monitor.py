"""
[DEPRECATED] P0-1: 此文件直接使用 joblib.load 绕过 ModelBridge，存在数据泄露风险。
生产预测请使用 agents.model_bridge.ModelBridge.predict()
draw_detector_monitor.py — 平局检测器性能监控与持续优化 (v1.0)
================================================================
基于5步优化的第1步(DrawOptimizedEnsemble)和第5步(动态权重)，
建立完整的平局预测监控体系。

功能:
  1. 按联赛评估平局检测性能
  2. 识别弱检测区域 (低召回率/高假阳性)
  3. 自动调参建议 (draw_threshold / boost_factor)
  4. 生成优化报告 + 历史趋势

用法:
    python optimize/draw_detector_monitor.py --data data/enhanced_features_v1.csv

选项:
    --model saved_models/footballai_expert_latest.joblib  指定模型
    --output reports/draw_monitor                          输出目录
"""
import os
import sys
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from collections import defaultdict

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score, f1_score, precision_score, recall_score,
    confusion_matrix, classification_report, log_loss,
)

# 项目根路径
PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))

DATA_PATH = PROJ_ROOT / 'data' / 'enhanced_features_v1.csv'

LEAGUE_CN_MAP = {
    'Premier League': '英超', 'La Liga': '西甲', 'Bundesliga': '德甲',
    'Serie A': '意甲', 'Ligue 1': '法甲', 'Eredivisie': '荷甲',
    'Primeira Liga': '葡超', 'Championship': '英冠',
    'Campeonato Brasileiro Série A': '巴西甲级',
    'MLS': '美职联', 'Champions League': '欧冠', 'Europa League': '欧联',
}

HIGH_DRAW_LEAGUES = {'Campeonato Brasileiro Série A', 'Serie A', 'Ligue 1', 'Primeira Liga'}


class DrawDetectorMonitor:
    """平局检测器性能监控系统"""

    def __init__(self, model_path: Optional[str] = None):
        self.model = None
        self.model_path = model_path
        self.results: Dict = {}

    def load_model(self):
        """加载训练好的 FootballAIEnhanced 模型"""
        import joblib
        path = self.model_path or str(PROJ_ROOT / 'saved_models' / 'footballai_expert_latest.joblib')
        if not os.path.exists(path):
            print(f"[WARN] Model not found at {path}, using fresh model for evaluation")
            return False
        self.model = joblib.load(path)
        print(f"[OK] Model loaded from {path}")
        print(f"  Draw enabled: {getattr(self.model, '_draw_enabled', False)}")
        print(f"  Adaptive enabled: {getattr(self.model, '_adaptive_enabled', False)}")
        if hasattr(self.model, 'ensemble_weights'):
            print(f"  Weights: {self.model.ensemble_weights}")
        return True

    def evaluate_global(
        self,
        X_test: np.ndarray,
        y_test: np.ndarray,
        feat_df: pd.DataFrame = None,
    ) -> Dict:
        """全局平局检测评估"""
        if self.model is None:
            return {}

        # 基础预测
        proba_base = self.model.ensemble_predict(X_test)
        y_pred_base = np.argmax(proba_base, axis=1)

        # 增强预测 (如果启用)
        proba_boosted = None
        y_pred_boosted = None
        if getattr(self.model, '_draw_enabled', False):
            try:
                proba_boosted = self.model.predict_with_draw_boost(X_test)
                y_pred_boosted = np.argmax(proba_boosted, axis=1)
            except (Exception) as e:
                print(f"[WARN] predict_with_draw_boost failed: {e}")

        # 计算指标
        result = {
            'base': self._compute_draw_metrics(y_test, proba_base, y_pred_base),
            'boosted': (
                self._compute_draw_metrics(y_test, proba_boosted, y_pred_boosted)
                if proba_boosted is not None else None
            ),
            'n_samples': len(y_test),
            'actual_draw_rate': float(np.mean(y_test == 1)),
            'mean_draw_proba_base': float(np.mean(proba_base[:, 1])),
            'mean_draw_proba_boosted': (
                float(np.mean(proba_boosted[:, 1])) if proba_boosted is not None else None
            ),
        }

        # 改进量
        if result['boosted'] is not None:
            result['improvement'] = {
                'draw_f1_delta': result['boosted']['draw_f1'] - result['base']['draw_f1'],
                'draw_recall_delta': result['boosted']['draw_recall'] - result['base']['draw_recall'],
                'accuracy_delta': result['boosted']['accuracy'] - result['base']['accuracy'],
            }

        # 概率校准分析
        result['calibration'] = self._analyze_calibration(y_test, proba_base)

        return result

    def evaluate_per_league(
        self,
        X_test: np.ndarray,
        y_test: np.ndarray,
        league_labels: List[str],
    ) -> Dict:
        """按联赛评估平局检测"""
        per_league = {}
        unique_leagues = set(league_labels)

        for league in sorted(unique_leagues):
            mask = [l == league for l in league_labels]
            lg_X = X_test[mask]
            lg_y = y_test[mask]

            if len(lg_y) < 20:
                continue

            cn = LEAGUE_CN_MAP.get(league, league)
            proba_base = self.model.ensemble_predict(lg_X)
            y_pred_base = np.argmax(proba_base, axis=1)

            base_metrics = self._compute_draw_metrics(lg_y, proba_base, y_pred_base)

            boosted_metrics = None
            if getattr(self.model, '_draw_enabled', False):
                try:
                    proba_b = self.model.predict_with_draw_boost(lg_X)
                    y_pred_b = np.argmax(proba_b, axis=1)
                    boosted_metrics = self._compute_draw_metrics(lg_y, proba_b, y_pred_b)
                except (Exception):
                    pass

            per_league[league] = {
                'cn': cn,
                'is_high_draw': league in HIGH_DRAW_LEAGUES,
                'n_samples': len(lg_y),
                'actual_draw_rate': float(np.mean(lg_y == 1)),
                'base': base_metrics,
                'boosted': boosted_metrics,
            }

        return per_league

    def identify_weak_zones(
        self,
        global_result: Dict,
        per_league_result: Dict,
    ) -> Dict:
        """识别平局检测的薄弱区域"""
        weak_zones = {
            'low_recall_leagues': [],      # 召回率低于平均值的联赛
            'high_fp_leagues': [],         # 高误报率的联赛
            'calibration_issues': [],      # 概率校准偏差大的联赛
            'recommendations': [],
        }

        avg_recall = np.mean([
            v['base']['draw_recall']
            for v in per_league_result.values()
        ]) if per_league_result else 0.25

        avg_precision = np.mean([
            v['base']['draw_precision']
            for v in per_league_result.values()
        ]) if per_league_result else 0.30

        for league, info in per_league_result.items():
            base = info['base']

            # 低召回率 (< 平均 - 5%)
            if base['draw_recall'] < avg_recall - 0.05:
                weak_zones['low_recall_leagues'].append({
                    'league': league,
                    'cn': info['cn'],
                    'recall': base['draw_recall'],
                    'gap_from_avg': round(base['recall'] - avg_recall, 4),
                    'actual_draw_rate': info['actual_draw_rate'],
                })

            # 低精确率
            if base['draw_precision'] < avg_precision - 0.05:
                weak_zones['high_fp_leagues'].append({
                    'league': league,
                    'cn': info['cn'],
                    'precision': base['draw_precision'],
                })

            # 校准问题: 预测概率与实际频率偏差 > 15%
            if abs(base.get('calibration_error', 0)) > 0.15:
                weak_zones['calibration_issues'].append({
                    'league': league,
                    'cn': info['cn'],
                    'calibration_error': round(base.get('calibration_error', 0), 4),
                })

        # 生成推荐
        recommendations = []
        if weak_zones['low_recall_leagues']:
            n_weak = len(weak_zones['low_recall_leagues'])
            worst = weak_zones['low_recall_leagues'][0]
            recommendations.append(
                f"[R1] {n_weak} 个联赛平局召回率偏低。"
                f"最差: {worst['cn']} ({worst['recall']:.1%})，"
                f"建议降低 draw_threshold (当前0.25 → 尝试 0.20-0.22)"
            )

        if weak_zones['high_fp_leagues']:
            n_fp = len(weak_zones['high_fp_leagues'])
            recommendations.append(
                f"[R2] {n_fp} 个联赛存在过度预测平局问题，"
                f"建议提高 draw_threshold 或增加 consensus_count"
            )

        high_draw_results = {
            k: v for k, v in per_league_result.items() if v['is_high_draw']
        }
        if high_draw_results:
            hd_avg_f1 = np.mean([v['base']['draw_f1'] for v in high_draw_results.values()])
            all_avg_f1 = np.mean([v['base']['draw_f1'] for v in per_league_result.values()])
            if hd_avg_f1 < all_avg_f1:
                recommendations.append(
                    f"[R3] 高平局联赛Draw-F1({hd_avg_f1:.3f})低于平均水平({all_avg_f1:.3f})，"
                    f"需要联赛特定调参或独立建模"
                )

        # Boost 效果分析
        boost_improved = sum(
            1 for v in per_league_result.values()
            if v.get('boosted') and v['boosted'].get('draw_f1', 0) > v['base']['draw_f1']
        )
        total_with_boost = sum(
            1 for v in per_league_result.values() if v.get('boosted') is not None
        )
        if total_with_boost > 0:
            boost_ratio = boost_improved / total_with_boost
            if boost_ratio < 0.6:
                recommendations.append(
                    f"[R4] DrawBoost 仅对 {boost_improved}/{total_with_boost} 联赛有效 "
                    f"({boost_ratio:.0%})，建议检查 draw_signal 特征质量"
                )

        weak_zones['recommendations'] = recommendations

        return weak_zones

    def suggest_parameter_tuning(
        self,
        global_result: Dict,
        weak_zones: Dict,
    ) -> Dict:
        """自动参数调优建议"""
        suggestions = {
            'draw_threshold': {'current': 0.25, 'suggested': 0.25},
            'boost_factor': {'current': 1.5, 'suggested': 1.5},
            'max_draw_cap': {'current': 0.45, 'suggested': 0.45},
            'rationale': [],
        }

        base_draw = global_result.get('base', {})
        boosted = global_result.get('boosted')

        # 分析召回/精确权衡
        recall = base_draw.get('draw_recall', 0)
        prec = base_draw.get('draw_precision', 0)
        actual_dr = global_result.get('actual_draw_rate', 0.25)

        if recall < actual_dr * 0.8:
            # 召回率太低 → 降低阈值
            suggestions['draw_threshold']['suggested'] = max(0.18, 0.25 - 0.05)
            suggestions['rationale'].append(
                f"召回率({recall:.1%})远低于实际平局率({actual_dr:.1%})的80%，"
                f"建议 draw_threshold: 0.25→{suggestions['draw_threshold']['suggested']}"
            )

        if prec < 0.25:
            # 精确率太低 → 提高阈值或降低cap
            suggestions['max_draw_cap']['suggested'] = min(prec * 1.8, 0.40)
            suggestions['rationale'].append(
                f"精确率太低({prec:.1%})，建议 max_draw_cap: 0.45→"
                f"{suggestions['max_draw_cap']['suggested']:.2f} 以减少误报"
            )

        # Boost效果微调
        if boosted and boosted.get('draw_f1', 0) <= base_draw.get('draw_f1', 0):
            suggestions['boost_factor']['suggested'] = 1.3
            suggestions['rationale'].append(
                "Boost未带来F1增益，可能 boost_factor 过高导致噪声放大，"
                "建议从1.5降至1.3"
            )

        # 类别权重调整建议
        cw = {}
        if hasattr(self.model, '_adaptive_class_weights') and self.model._adaptive_class_weights:
            cw = self.model._adaptive_class_weights
        if cw:
            d_weight = cw.get('1', 1.0)
            h_weight = cw.get('0', 1.0)
            a_weight = cw.get('2', 1.0)
            if d_weight < 2.0 and recall < 0.35:
                suggestions['class_weights'] = {
                    'current': {'H': h_weight, 'D': d_weight, 'A': a_weight},
                    'suggested': {'H': h_weight, 'D': round(d_weight * 1.3, 2), 'A': a_weight},
                }
                suggestions['rationale'].append(
                    f"平局类别权重({d_weight:.2f})偏小而召回率({recall:.1%})偏低，"
                    f"建议提升至{d_weight*1.3:.2f}"
                )

        return suggestions

    def generate_report(
        self,
        global_result: Dict,
        per_league_result: Dict,
        weak_zones: Dict,
        tuning_suggestions: Dict,
        ts: str,
    ) -> Dict:
        """生成完整报告"""
        report = {
            'timestamp': ts,
            'monitor_version': 'v1.0',
            'model_info': {
                'path': str(self.model_path or 'fresh'),
                'draw_enabled': bool(getattr(self.model, '_draw_enabled', False)),
                'adaptive_enabled': bool(getattr(self.model, '_adaptive_enabled', False)),
                'weights': dict(getattr(self.model, 'ensemble_weights', {}))
                if hasattr(self.model, 'ensemble_weights') else {},
            },
            'global_performance': global_result,
            'per_league': per_league_result,
            'weak_zones': weak_zones,
            'tuning_suggestions': tuning_suggestions,
            'action_items': self._generate_action_items(weak_zones, tuning_suggestions),
        }
        return report

    @staticmethod
    def _compute_draw_metrics(y_true, y_proba, y_pred) -> Dict:
        """计算平局专项指标"""
        cm = confusion_matrix(y_true, y_pred, labels=[0, 1, 2])
        ds = int(cm[1].sum())
        dc = int(cm[1, 1]) if cm.shape[1] >= 2 else 0
        dp = dc / max(int(cm[:, 1].sum()), 1) if cm.shape[1] >= 2 else 0
        dr = dc / max(ds, 1)
        df1 = 2 * dp * dr / max(dp + dr, 0.001)

        acc = accuracy_score(y_true, y_pred)
        ll = log_loss(y_true, y_proba) if y_proba is not None else 1.0
        mean_dp = float(np.mean(y_proba[:, 1])) if y_proba is not None and y_proba.shape[1] >= 2 else 0.0

        # 校准误差: 预测概率均值 vs 实际比例
        calibration_err = mean_dp - float(np.mean(y_true == 1))

        return {
            'accuracy': float(acc),
            'logloss': float(ll),
            'draw_precision': float(dp),
            'draw_recall': float(dr),
            'draw_f1': float(df1),
            'draw_support': ds,
            'mean_draw_proba': mean_dp,
            'calibration_error': float(calibration_err),
        }

    @staticmethod
    def _analyze_calibration(y_true, y_proba, n_bins: int = 10) -> Dict:
        """概率校准分析 — 将样本按预测概率分桶，对比实际频率"""
        if y_proba is None or y_proba.shape[1] < 2:
            return {'status': 'no_proba'}

        draw_probas = y_proba[:, 1]
        bin_edges = np.linspace(0, 1, n_bins + 1)
        bins = []
        for i in range(n_bins):
            mask = (draw_probas >= bin_edges[i]) & (draw_probas < bin_edges[i+1])
            if mask.sum() > 0:
                bins.append({
                    'bin_range': f'{bin_edges[i]:.2f}-{bin_edges[i+1]:.2f}',
                    'predicted_mean': float(draw_probas[mask].mean()),
                    'actual_frequency': float((y_true[mask] == 1).mean()),
                    'count': int(mask.sum()),
                })
        return {'bins': bins}

    @staticmethod
    def _generate_action_items(weak_zones: Dict, tuning: Dict) -> List[str]:
        """生成可执行的行动项"""
        items = []

        items.extend(weak_zones.get('recommendations', []))
        items.extend(tuning.get('rationale', []))

        # 综合行动项
        items.append("[A1] 定期运行本脚本监控平局检测性能趋势")
        items.append("[A2] 对高平局率联赛使用 train_league_models.py 独立建模")
        items.append("[A3] 将调优参数写入 config.yaml 的 draw_optimization 段")

        return items


def run_monitor(data_path: str = None, model_path: str = None, output_dir: str = None):
    """运行完整监控流程"""

    t_total = time.time()
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    print("=" * 64)
    print("[DRAW-MONITOR] 平局检测器性能监控系统 v1.0")
    print("=" * 64)

    monitor = DrawDetectorMonitor(model_path=model_path)

    # 加载数据
    data_path = data_path or str(DATA_PATH)
    df = pd.read_csv(data_path)
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.sort_values('date').reset_index(drop=True)
    print(f"\n[DATA] {len(df):,} matches loaded")

    # 时序分割: 使用最后 15% 作为测试集
    split_point = int(len(df) * 0.85)
    df_train = df.iloc[:split_point]
    df_test = df.iloc[split_point:]

    print(f"  Train: {len(df_train):,}, Test: {len(df_test):,}")

    # 特征提取 (仅测试集用于评估)
    from backend.models.footballai_enhanced import FootballAIEnhanced
    temp_model = FootballAIEnhanced(model_version='monitor_eval')
    X_test, y_test, feat_names = temp_model.prepare_features(df_test)
    league_labels = df_test['league'].tolist() if 'league' in df_test.columns else ['unknown'] * len(df_test)

    print(f"  Features: {X_test.shape[1]}")

    # 加载或创建模型
    has_model = monitor.load_model()

    if not has_model:
        print("\n[TRAIN-EVAL] 无已训练模型，在训练集上快速训练用于评估...")
        X_train, y_tr, fn = temp_model.prepare_features(df_train)
        temp_model.train_with_cross_validation(X_train, y_tr)
        try:
            temp_model.enable_draw_optimization(X_train, y_tr)
        except (Exception, ValueError, KeyError, IndexError) as e:
            print(f"  [WARN] Draw optimization failed: {e}")
        monitor.model = temp_model

    # 1. 全局评估
    print("\n[STEP 1/4] Global evaluation...")
    global_result = monitor.evaluate_global(X_test, y_test)
    print(f"  Base Acc={global_result['base']['accuracy']:.4f}, "
          f"DrawF1={global_result['base']['draw_f1']:.4f}")
    if global_result.get('boosted'):
        imp = global_result['improvement']
        print(f"  Boosted Acc={global_result['boosted']['accuracy']:.4f} "
              f"(ΔAcc={imp['accuracy_delta']:+.4f}), "
              f"DrawF1={global_result['boosted']['draw_f1']:.4f} "
              f"(ΔF1={imp['draw_f1_delta']:+.4f})")

    # 2. 按联赛评估
    print("\n[STEP 2/4] Per-league evaluation...")
    per_league = monitor.evaluate_per_league(X_test, y_test, league_labels)
    for lg, info in sorted(per_league.items()):
        b = info['base']
        tag = " ★HD" if info['is_high_draw'] else ""
        boost_tag = ""
        if info.get('boosted'):
            delta = info['boosted']['draw_f1'] - b['draw_f1']
            boost_tag = f" [B:{delta:+.3f}]"
        print(f"  {info['cn']:<12}{tag}: Acc={b['accuracy']:.4f}, "
              f"D-F1={b['draw_f1']:.4f}, D-Rec={b['draw_recall']:.4f}"
              f"{boost_tag}")

    # 3. 弱区识别
    print("\n[STEP 3/4] Identifying weak zones...")
    weak_zones = monitor.identify_weak_zones(global_result, per_league)
    print(f"  Low-recall leagues: {len(weak_zones['low_recall_leagues'])}")
    print(f"  High-FP leagues:   {len(weak_zones['high_fp_leagues'])}")
    print(f"  Calibration issues: {len(weak_zones['calibration_issues'])}")

    # 4. 参数建议
    print("\n[STEP 4/4] Generating tuning suggestions...")
    tuning = monitor.suggest_parameter_tuning(global_result, weak_zones)

    # 报告
    report = monitor.generate_report(global_result, per_league, weak_zones, tuning, ts)

    # 保存
    out_dir = Path(output_dir or str(PROJ_ROOT / 'reports'))
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f'draw_detector_report_{ts}.json'
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    # 打印摘要
    print_summary(monitor, report)

    elapsed = time.time() - t_total
    print(f"\n{'='*64}")
    print(f"[DONE] Monitor complete! ({elapsed:.1f}s)")
    print(f"  Report: {report_path.name}")
    print(f"{'='*64}")


def print_summary(monitor: DrawDetectorMonitor, report: Dict):
    """打印美观的监控摘要"""
    g = report['global_performance']
    wz = report['weak_zones']
    t = report['tuning_suggestions']

    print(f"\n{'═'*70}")
    print("  平局检测器监控报告")
    print(f"{'═'*70}")
    print(f"  样本量:       {g['n_samples']:,}")
    print(f"  实际平局率:   {g['actual_draw_rate']:.1%}")
    print(f"  基础准确率:   {g['base']['accuracy']:.4f}")
    print(f"  基础Draw-F1:  {g['base']['draw_f1']:.4f}")
    print(f"  基础Draw-Rec: {g['base']['draw_recall']:.4f}")

    if g.get('boosted'):
        imp = g['improvement']
        print(f"  增强准确率:   {g['boosted']['accuracy']:.4f} ({imp['accuracy_delta']:+.4f})")
        print(f"  增强Draw-F1:  {g['boosted']['draw_f1']:.4f} ({imp['draw_f1_delta']:+.4f})")

    print(f"\n  ── 薄弱区域 ──")
    if wz['low_recall_leagues']:
        for z in wz['low_recall_leagues'][:3]:
            print(f"  ⚠ {z['cn']}: Recall={z['recall']:.1%} (avg差距{z['gap_from_avg']:+.2%})")
    else:
        print(f"  ✓ 所有联赛召回率正常")

    print(f"\n  ── 调参建议 ──")
    for param, vals in [('draw_threshold', t.get('draw_threshold')),
                        ('boost_factor', t.get('boost_factor')),
                        ('max_draw_cap', t.get('max_draw_cap'))]:
        if vals and vals.get('current') != vals.get('suggested'):
            print(f"  {param}: {vals['current']} → {vals['suggested']}")

    if t.get('rationale'):
        print(f"\n  ── 推荐理由 ──")
        for r in t['rationale']:
            print(f"  • {r}")

    print(f"{'═'*70}")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Draw Detector Monitor')
    parser.add_argument('--data', type=str, default=None, help='Data file path')
    parser.add_argument('--model', type=str, default=None, help='Model joblib path')
    parser.add_argument('--output', type=str, default=None, help='Output directory')
    args = parser.parse_args()

    run_monitor(
        data_path=args.data,
        model_path=args.model,
        output_dir=args.output,
    )

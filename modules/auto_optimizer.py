"""
哨响AI v4.0 — 自主优化引擎 (Autonomous Optimization Engine)
===============================================================
P3 自主进化核心模块。性能监控 + A/B实验 + 漂移检测 + 自我诊断 + 自动重训触发。

核心能力:
    1. PerformanceTracker — 实时性能追踪 (Acc/F1/AUC/各类别召回)
    2. DriftDetector — 特征漂移检测 (PSI/KS/重要性漂移)
    3. ABExperiment — 轻量级A/B实验框架
    4. SelfDiagnoser — 自我诊断 + 告警生成
    5. RetrainTrigger — 自动重训触发逻辑

集成到 v4.0 编排器:
    orchestrator.track_performance(prediction, actual)  # 赛后反馈
    orchestrator.check_drift(features)                   # 特征漂移检测
    orchestrator.self_diagnose()                         # 自我诊断

作者: Architecture v4.0 · P3 Phase
日期: 2026-06-18
"""
from __future__ import annotations
import logging
import time
import math
import json
from typing import Dict, List, Optional, Any, Tuple, Callable
from dataclasses import dataclass, field
from enum import Enum
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 1. 核心数据结构
# ═══════════════════════════════════════════════════════════════

class HealthStatus(Enum):
    HEALTHY = "healthy"        # 一切正常
    WATCHING = "watching"      # 需关注 (轻微漂移/趋势)
    DEGRADING = "degrading"    # 正在退化
    CRITICAL = "critical"      # 严重退化, 需立即处理

class DriftLevel(Enum):
    NONE = "none"
    MILD = "mild"              # PSI 0.1-0.25
    SIGNIFICANT = "significant"  # PSI 0.25-0.5
    SEVERE = "severe"          # PSI > 0.5

@dataclass
class MetricSnapshot:
    """单次性能快照"""
    timestamp: str
    accuracy: float
    macro_f1: float
    h_f1: float
    d_f1: float
    a_f1: float
    auc: Optional[float] = None
    sample_count: int = 0

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "accuracy": round(self.accuracy, 4),
            "macro_f1": round(self.macro_f1, 4),
            "h_f1": round(self.h_f1, 4),
            "d_f1": round(self.d_f1, 4),
            "a_f1": round(self.a_f1, 4),
            "auc": round(self.auc, 4) if self.auc else None,
            "n": self.sample_count,
        }

@dataclass
class DriftReport:
    """漂移检测报告"""
    psi_score: float                    # 总体PSI
    drift_level: str                    # 漂移等级
    drifted_features: List[Dict]        # 漂移特征列表
    importance_drift: Dict[str, float]  # 重要性漂移Top5
    triggered_retrain: bool = False
    summary: str = ""

    def to_dict(self) -> Dict:
        return {
            "psi_score": round(self.psi_score, 4),
            "drift_level": self.drift_level,
            "drifted_features": self.drifted_features[:10],
            "importance_drift": {k: round(v, 4) for k, v in self.importance_drift.items()},
            "triggered_retrain": self.triggered_retrain,
            "summary": self.summary,
        }

@dataclass
class ABResult:
    """A/B实验结果"""
    experiment_name: str
    control_metric: float
    variant_metric: float
    delta: float
    delta_pct: float
    significant: bool
    winner: str        # "control" | "variant" | "none"
    sample_count: int
    recommendation: str = ""

    def to_dict(self) -> Dict:
        return {
            "experiment": self.experiment_name,
            "control": round(self.control_metric, 4),
            "variant": round(self.variant_metric, 4),
            "delta": round(self.delta, 4),
            "delta_pct": f"{self.delta_pct:+.1f}%",
            "significant": self.significant,
            "winner": self.winner,
            "n": self.sample_count,
            "recommendation": self.recommendation,
        }

@dataclass
class DiagnoseReport:
    """自我诊断报告"""
    overall_health: str
    issues: List[Dict]              # [{severity, component, description, suggestion}]
    metrics_trend: Dict             # 指标趋势
    drift_summary: Optional[Dict] = None
    retrain_recommended: bool = False
    retrain_urgency: str = "none"   # none/low/medium/high/immediate
    generated_at: str = ""

    def to_dict(self) -> Dict:
        return {
            "health": self.overall_health,
            "issues": self.issues,
            "metrics_trend": self.metrics_trend,
            "drift": self.drift_summary,
            "retrain": {
                "recommended": self.retrain_recommended,
                "urgency": self.retrain_urgency,
            },
            "generated_at": self.generated_at,
        }

# ═══════════════════════════════════════════════════════════════
# 2. 性能追踪器
# ═══════════════════════════════════════════════════════════════

class PerformanceTracker:
    """实时性能追踪器"""

    def __init__(self, window_size: int = 50):
        self.window_size = window_size
        self.snapshots: List[MetricSnapshot] = []
        self.recent_predictions: deque = deque(maxlen=window_size)
        self._total_correct = 0
        self._total_count = 0

        # 分类统计
        self._class_stats = {
            "H": {"correct": 0, "total": 0},
            "D": {"correct": 0, "total": 0},
            "A": {"correct": 0, "total": 0},
        }

    def record(self, prediction_h: float, prediction_d: float, prediction_a: float,
               actual: str):
        """记录一次预测+实际结果"""
        pred = max(("H", prediction_h), ("D", prediction_d), ("A", prediction_a), key=lambda x: x[1])[0]
        self.recent_predictions.append((pred, actual))

        self._total_count += 1
        if pred == actual:
            self._total_correct += 1

        if actual in self._class_stats:
            self._class_stats[actual]["total"] += 1
            if pred == actual:
                self._class_stats[actual]["correct"] += 1

        # 每 window_size 次记录一个快照
        if self._total_count % self.window_size == 0:
            self._take_snapshot()

    def _take_snapshot(self) -> MetricSnapshot:
        """生成快照"""
        # 从最近的滚动窗口计算指标
        recent = list(self.recent_predictions)[-self.window_size:]
        n = len(recent)
        if n == 0:
            snapshot = MetricSnapshot(
                timestamp=datetime.now(timezone.utc).isoformat(),
                accuracy=0, macro_f1=0, h_f1=0, d_f1=0, a_f1=0,
                sample_count=0,
            )
        else:
            correct = sum(1 for p, a in recent if p == a)
            acc = correct / n

            # 每类F1 (简化: 2*prec*recall/(prec+recall))
            class_stats = {"H": {"tp": 0, "fp": 0, "fn": 0},
                           "D": {"tp": 0, "fp": 0, "fn": 0},
                           "A": {"tp": 0, "fp": 0, "fn": 0}}
            for pred, actual in recent:
                for cls in ["H", "D", "A"]:
                    if pred == cls and actual == cls:
                        class_stats[cls]["tp"] += 1
                    elif pred == cls and actual != cls:
                        class_stats[cls]["fp"] += 1
                    elif pred != cls and actual == cls:
                        class_stats[cls]["fn"] += 1

            def calc_f1(cls):
                tp = class_stats[cls]["tp"]
                fp = class_stats[cls]["fp"]
                fn = class_stats[cls]["fn"]
                prec = tp / (tp + fp) if (tp + fp) > 0 else 0
                rec = tp / (tp + fn) if (tp + fn) > 0 else 0
                return 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0

            h_f1 = calc_f1("H")
            d_f1 = calc_f1("D")
            a_f1 = calc_f1("A")
            macro = (h_f1 + d_f1 + a_f1) / 3

            snapshot = MetricSnapshot(
                timestamp=datetime.now(timezone.utc).isoformat(),
                accuracy=acc, macro_f1=macro,
                h_f1=h_f1, d_f1=d_f1, a_f1=a_f1,
                sample_count=n,
            )

        self.snapshots.append(snapshot)
        return snapshot

    def get_current_metrics(self) -> Dict:
        """获取当前性能指标"""
        if not self.snapshots:
            self._take_snapshot()
        latest = self.snapshots[-1]
        return latest.to_dict()

    def get_trend(self, window: int = 5) -> Dict:
        """获取最近N个快照的趋势"""
        recent = self.snapshots[-window:] if len(self.snapshots) >= window else self.snapshots
        if not recent:
            return {"direction": "stable", "change": 0}

        first_acc = recent[0].accuracy
        last_acc = recent[-1].accuracy
        change = last_acc - first_acc

        if change > 0.02:
            direction = "improving"
        elif change < -0.02:
            direction = "declining"
        else:
            direction = "stable"

        return {
            "direction": direction,
            "change": round(change, 4),
            "start_acc": round(first_acc, 4),
            "end_acc": round(last_acc, 4),
            "snapshots": len(recent),
        }

    def detect_degradation(self) -> Tuple[bool, str]:
        """检测性能退化"""
        if len(self.snapshots) < 3:
            return False, "数据不足"

        trend = self.get_trend(5)
        if trend["direction"] == "declining" and trend["change"] < -0.02:
            return True, f"Acc下降{abs(trend['change']):.1%}, 从{trend['start_acc']:.1%}→{trend['end_acc']:.1%}"

        if len(self.snapshots) >= 5:
            d_f1s = [s.d_f1 for s in self.snapshots[-5:]]
            if d_f1s[-1] < d_f1s[0] - 0.05:
                return True, f"Draw-F1连续下降: {d_f1s[0]:.3f}→{d_f1s[-1]:.3f}"

        return False, ""

# ═══════════════════════════════════════════════════════════════
# 3. 漂移检测器
# ═══════════════════════════════════════════════════════════════

class DriftDetector:
    """特征漂移检测器 — PSI + 重要性漂移"""

    def __init__(self):
        self._baseline_distributions: Dict[str, Tuple[float, float]] = {}
        self._baseline_importance: Dict[str, float] = {}
        self._last_check: Optional[datetime] = None

    def set_baseline(self, feature_stats: Dict[str, Tuple[float, float]],
                     importance: Dict[str, float] = None):
        """设置基线分布: {feature_name: (mean, std)}"""
        self._baseline_distributions = feature_stats
        if importance:
            self._baseline_importance = importance

    def calculate_psi(self, current_dist: Dict[str, Tuple[float, float]]) -> float:
        """
        计算总体PSI (Population Stability Index)

        PSI = Σ[(Actual% - Expected%) * ln(Actual% / Expected%)]
        简化版: 基于均值+标准差, 假设正态分布
        """
        if not self._baseline_distributions:
            return 0.0

        total_psi = 0.0
        count = 0

        for feat, (cur_mean, cur_std) in current_dist.items():
            if feat not in self._baseline_distributions:
                continue
            base_mean, base_std = self._baseline_distributions[feat]

            # 简化PSI: |z-score change| 的归一化
            if base_std > 0 and cur_std > 0:
                effect_size = abs(cur_mean - base_mean) / base_std
                # 映射到 PSI 类量级
                psi_contrib = min(1.0, effect_size / 3.0) * 0.1
                total_psi += psi_contrib
                count += 1

        return total_psi / count if count > 0 else 0.0

    def detect_drift(self, current_dist: Dict[str, Tuple[float, float]],
                     current_importance: Dict[str, float] = None
                     ) -> DriftReport:
        """执行漂移检测"""
        psi = self.calculate_psi(current_dist)

        if psi >= 0.50:
            level = DriftLevel.SEVERE.value
        elif psi >= 0.25:
            level = DriftLevel.SIGNIFICANT.value
        elif psi >= 0.10:
            level = DriftLevel.MILD.value
        else:
            level = DriftLevel.NONE.value

        # 漂移最大的特征
        drifted = []
        for feat, (cur_mean, cur_std) in current_dist.items():
            if feat not in self._baseline_distributions:
                continue
            base_mean, base_std = self._baseline_distributions[feat]
            if base_std > 0:
                z_change = abs(cur_mean - base_mean) / base_std
                if z_change > 0.5:
                    drifted.append({
                        "feature": feat,
                        "base_mean": round(base_mean, 4),
                        "current_mean": round(cur_mean, 4),
                        "z_change": round(z_change, 3),
                    })
        drifted.sort(key=lambda x: x["z_change"], reverse=True)

        # 重要性漂移
        imp_drift = {}
        if current_importance and self._baseline_importance:
            for feat, cur_imp in current_importance.items():
                base_imp = self._baseline_importance.get(feat, 0)
                if base_imp > 0.001:
                    change = abs(cur_imp - base_imp) / base_imp
                    if change > 0.30:
                        imp_drift[feat] = round(change, 3)
            imp_drift = dict(sorted(imp_drift.items(), key=lambda x: x[1], reverse=True)[:5])

        # 触发重训?
        trigger_retrain = psi >= 0.25 or (drifted and len(drifted) > 5)

        summary = f"PSI={psi:.3f} ({level})"
        if drifted:
            summary += f", {len(drifted)} features drifted"
        if trigger_retrain:
            summary += ", ⚠️ 建议触发重训"

        self._last_check = datetime.now(timezone.utc)

        return DriftReport(
            psi_score=psi, drift_level=level,
            drifted_features=drifted, importance_drift=imp_drift,
            triggered_retrain=trigger_retrain, summary=summary,
        )

# ═══════════════════════════════════════════════════════════════
# 4. A/B 实验框架
# ═══════════════════════════════════════════════════════════════

class ABExperiment:
    """轻量级A/B实验框架"""

    def __init__(self, name: str, min_samples: int = 200):
        self.name = name
        self.min_samples = min_samples
        self.control_results: List[Tuple[str, str]] = []  # (pred, actual)
        self.variant_results: List[Tuple[str, str]] = []

    def record_control(self, pred: str, actual: str):
        self.control_results.append((pred, actual))

    def record_variant(self, pred: str, actual: str):
        self.variant_results.append((pred, actual))

    def evaluate(self) -> Optional[ABResult]:
        """评估实验结果"""
        if len(self.control_results) < self.min_samples or len(self.variant_results) < self.min_samples:
            return None

        control_acc = sum(1 for p, a in self.control_results if p == a) / len(self.control_results)
        variant_acc = sum(1 for p, a in self.variant_results if p == a) / len(self.variant_results)

        delta = variant_acc - control_acc
        delta_pct = delta * 100

        # 简化显著性: |delta| > 2/sqrt(n) 近似p<0.05
        n = min(len(self.control_results), len(self.variant_results))
        threshold = 2.0 / math.sqrt(n)
        significant = abs(delta) > threshold

        if significant and delta > 0:
            winner = "variant"
            rec = f"variant Acc↑{delta_pct:+.1f}pp (显著), 建议采纳"
        elif significant and delta < 0:
            winner = "control"
            rec = f"variant Acc↓{abs(delta_pct):.1f}pp (显著), 保持control"
        else:
            winner = "none"
            rec = f"差异{delta_pct:+.1f}pp不显著, 需更多样本"

        return ABResult(
            experiment_name=self.name,
            control_metric=control_acc, variant_metric=variant_acc,
            delta=delta, delta_pct=delta_pct,
            significant=significant, winner=winner,
            sample_count=n, recommendation=rec,
        )

# ═══════════════════════════════════════════════════════════════
# 5. 自我诊断器
# ═══════════════════════════════════════════════════════════════

class SelfDiagnoser:
    """
    自我诊断器 — 综合所有监控信号生成诊断报告

    诊断维度:
        1. 性能趋势 (Acc/F1 是否下降)
        2. 校准质量 (概率是否可靠)
        3. 特征健康 (是否有漂移)
        4. 各类别平衡 (D是否被忽视)
    """

    def __init__(self, perf_tracker: PerformanceTracker,
                 drift_detector: DriftDetector):
        self.perf = perf_tracker
        self.drift = drift_detector

    def diagnose(self) -> DiagnoseReport:
        """执行全维度自我诊断"""
        issues = []

        # 1. 性能趋势
        trend = self.perf.get_trend(5)
        metrics_trend = trend

        if trend["direction"] == "declining":
            issues.append({
                "severity": "warning",
                "component": "accuracy",
                "description": f"Acc趋势下降 ({trend['change']:+.1%})",
                "suggestion": "检查特征漂移 + 评估是否需要重训",
            })

        degraded, reason = self.perf.detect_degradation()
        if degraded:
            issues.append({
                "severity": "critical",
                "component": "degradation",
                "description": reason,
                "suggestion": "立即触发全链路诊断 + 启动重训流程",
            })

        # 2. 各类别评估
        current = self.perf.get_current_metrics()
        d_f1 = current.get("d_f1", 0)
        if d_f1 < 0.40:
            issues.append({
                "severity": "warning",
                "component": "draw_performance",
                "description": f"Draw-F1={d_f1:.3f} < 0.40, 平局预测严重退化",
                "suggestion": "检查D-Gate配置 + Draw特征有效性 + 考虑重训",
            })
        elif d_f1 < 0.48:
            issues.append({
                "severity": "info",
                "component": "draw_performance",
                "description": f"Draw-F1={d_f1:.3f} 低于v3.2基线0.504",
                "suggestion": "监控趋势, 评估是否需要Draw专项优化",
            })

        # 3. 漂移检查 (如果有最近检测)
        drift_summary = None
        retrain_recommended = False
        retrain_urgency = "none"

        if self.drift._last_check:
            drift_summary = {
                "last_check": self.drift._last_check.isoformat(),
                "baseline_features": len(self.drift._baseline_distributions),
            }

        # 4. 综合健康判断
        critical_count = sum(1 for i in issues if i["severity"] == "critical")
        warning_count = sum(1 for i in issues if i["severity"] == "warning")

        if critical_count > 0:
            health = HealthStatus.CRITICAL.value
            retrain_urgency = "immediate"
        elif warning_count >= 2 or degraded:
            health = HealthStatus.DEGRADING.value
            retrain_recommended = True
            retrain_urgency = "high"
        elif warning_count == 1:
            health = HealthStatus.WATCHING.value
            retrain_recommended = True
            retrain_urgency = "medium"
        else:
            health = HealthStatus.HEALTHY.value

        return DiagnoseReport(
            overall_health=health,
            issues=issues,
            metrics_trend=metrics_trend,
            drift_summary=drift_summary,
            retrain_recommended=retrain_recommended,
            retrain_urgency=retrain_urgency,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

# ═══════════════════════════════════════════════════════════════
# 6. 自主优化引擎 (整合)
# ═══════════════════════════════════════════════════════════════

class AutoOptimizer:
    """
    自主优化引擎 — P3 核心

    整合性能追踪、漂移检测、A/B实验、自我诊断。
    提供统一接口供 Orchestrator 调用。
    """

    def __init__(self, window_size: int = 50):
        self.perf_tracker = PerformanceTracker(window_size)
        self.drift_detector = DriftDetector()
        self.diagnoser = SelfDiagnoser(self.perf_tracker, self.drift_detector)
        self.experiments: Dict[str, ABExperiment] = {}

    # ── 性能追踪 ──

    def record_result(self, h_prob: float, d_prob: float, a_prob: float,
                      actual: str):
        """记录预测结果 (赛后反馈)"""
        self.perf_tracker.record(h_prob, d_prob, a_prob, actual)

    # ── 漂移检测 ──

    def set_feature_baseline(self, stats: Dict[str, Tuple[float, float]],
                              importance: Dict[str, float] = None):
        """设置特征基线"""
        self.drift_detector.set_baseline(stats, importance)

    def check_drift(self, current_stats: Dict[str, Tuple[float, float]],
                    current_importance: Dict[str, float] = None) -> DriftReport:
        """检查特征漂移"""
        return self.drift_detector.detect_drift(current_stats, current_importance)

    # ── A/B实验 ──

    def create_experiment(self, name: str, min_samples: int = 200) -> ABExperiment:
        """创建A/B实验"""
        exp = ABExperiment(name, min_samples)
        self.experiments[name] = exp
        return exp

    def get_experiment(self, name: str) -> Optional[ABExperiment]:
        return self.experiments.get(name)

    # ── 自我诊断 ──

    def diagnose(self) -> DiagnoseReport:
        """执行自我诊断"""
        return self.diagnoser.diagnose()

    # ── 综合状态 ──

    def status_summary(self) -> Dict:
        """综合状态概览"""
        perf = self.perf_tracker.get_current_metrics()
        trend = self.perf_tracker.get_trend(5)
        degraded, reason = self.perf_tracker.detect_degradation()
        diagnosis = self.diagnose()

        summary = {
            "health": diagnosis.overall_health,
            "performance": {
                "current": perf,
                "trend": trend,
                "degraded": degraded,
                "degradation_reason": reason,
            },
            "drift": {
                "baseline_set": len(self.drift_detector._baseline_distributions) > 0,
                "last_check": self.drift_detector._last_check.isoformat() if self.drift_detector._last_check else None,
            },
            "experiments": {
                "active": len(self.experiments),
                "names": list(self.experiments.keys()),
            },
            "diagnosis": {
                "issues": len(diagnosis.issues),
                "retrain_recommended": diagnosis.retrain_recommended,
                "retrain_urgency": diagnosis.retrain_urgency,
            },
            "health_advice": self._health_advice(diagnosis),
        }
        return summary

    def _health_advice(self, diagnosis: DiagnoseReport) -> str:
        """健康建议"""
        if diagnosis.overall_health == "critical":
            return "🔴 系统状态危急! 建议: 1)暂停自动预测 2)全链路诊断 3)立即触发重训"
        elif diagnosis.overall_health == "degrading":
            return "🟠 系统在退化。建议: 1)检查最近漂移 2)启动A/B测试验证 3)规划重训"
        elif diagnosis.overall_health == "watching":
            return "🟡 需关注。建议: 1)持续监控趋势 2)准备重训数据"
        else:
            return "✅ 系统健康。保持当前监控频率"

# ═══════════════════════════════════════════════════════════════
# 7. 全局单例
# ═══════════════════════════════════════════════════════════════

_optimizer: Optional[AutoOptimizer] = None

def get_optimizer() -> AutoOptimizer:
    global _optimizer
    if _optimizer is None:
        _optimizer = AutoOptimizer()
    return _optimizer

def reset_optimizer():
    global _optimizer
    _optimizer = None

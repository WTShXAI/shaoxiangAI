"""
哨响AI v4.0 — L6 自主优化反馈闭环 (Feedback Loop)
=====================================================
v4.0 6层架构的第6层。记录每次预测和实际结果，追踪性能趋势，
当性能退化时自动触发优化建议。

核心能力:
  1. QueryLogger        — 记录每次用户查询与预测
  2. ResultRecorder     — 赛后记录实际结果
  3. PerformanceTracker — 滚动窗口性能追踪
  4. DriftMonitor       — 预测分布漂移检测
  5. OptimizationAdvisor — 自动生成优化建议

集成到六层引擎:
  engine.process() 自动记录查询
  engine.feedback_loop.record_result() 赛后反馈
  engine.feedback_loop.get_optimization_suggestions() 获取建议

作者: Architecture v4.0 · L6 Phase
日期: 2026-06-18
"""
from __future__ import annotations
import os, json, logging, time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

# 存储路径
PROJECT_ROOT = Path(__file__).parent.parent
FEEDBACK_DIR = PROJECT_ROOT / "data" / "feedback"
FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════════
# 1. 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class QueryRecord:
    """单次查询记录"""
    timestamp: str
    user_input: str
    intent: str
    prediction: Dict[str, float]  # {H, D, A}
    actual: Optional[str] = None  # 'H'/'D'/'A' (赛后填入)
    correct: Optional[bool] = None
    confidence: float = 0.0
    model_version: str = "v4.1"
    extra: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "intent": self.intent,
            "prediction": self.prediction,
            "actual": self.actual,
            "correct": self.correct,
            "confidence": self.confidence,
            "model_version": self.model_version,
        }


@dataclass
class PerformanceWindow:
    """滚动性能窗口"""
    window_size: int = 50
    records: deque = field(default_factory=lambda: deque(maxlen=50))
    accuracy: float = 0.0
    d_recall: float = 0.0
    d_precision: float = 0.0
    total_correct: int = 0
    total_records: int = 0

    def add(self, record: QueryRecord):
        if record.correct is not None:
            self.records.append(record)
            self._recalculate()

    def _recalculate(self):
        if not self.records:
            return
        total = len(self.records)
        correct = sum(1 for r in self.records if r.correct)
        self.total_correct = correct
        self.total_records = total
        self.accuracy = correct / total if total > 0 else 0

        # D召回率
        d_actual = [r for r in self.records if r.actual == "D"]
        d_predicted = [r for r in self.records if max(r.prediction, key=r.prediction.get) == "D"]
        d_correct = [r for r in d_actual if max(r.prediction, key=r.prediction.get) == "D"]
        self.d_recall = len(d_correct) / len(d_actual) if d_actual else 0
        self.d_precision = len(d_correct) / len(d_predicted) if d_predicted else 0


@dataclass
class DriftAlert:
    """漂移告警"""
    metric: str
    baseline: float
    current: float
    change: float
    level: str  # mild / significant / severe
    suggestion: str


# ═══════════════════════════════════════════════════════════════
# 2. 反馈闭环
# ═══════════════════════════════════════════════════════════════

class FeedbackLoop:
    """
    L6 自主优化反馈闭环

    生命周期:
      1. record_query()    → 记录每次预测
      2. record_result()   → 赛后记录实际结果
      3. check_drift()     → 检测性能漂移
      4. get_suggestions() → 生成优化建议
    """

    def __init__(self, window_size: int = 50):
        self.window = PerformanceWindow(window_size=window_size)
        self.all_records: List[QueryRecord] = []

        # 基线 (初始为空，随数据积累自动建立)
        self.baseline_accuracy: Optional[float] = None
        self.baseline_d_recall: Optional[float] = None
        self.baseline_distribution: Optional[Dict[str, float]] = None

        # 漂移阈值
        self.drift_thresholds = {
            "accuracy_drop": 0.03,    # 准确率下降3pp触发
            "d_recall_drop": 0.05,    # D召回率下降5pp触发
            "distribution_shift": 0.10,  # 分布偏移10%触发
        }

        # 持久化
        self._load_state()

        logger.info(f"[FeedbackLoop] L6反馈闭环初始化 (窗口={window_size})")

    # ═══════════════════════════════════════════════════════════
    # 核心API
    # ═══════════════════════════════════════════════════════════

    def record_query(self, user_input: str, intent: str,
                     prediction: Dict[str, float], actual: str = None,
                     extra: Dict = None):
        """
        记录一次查询 (赛前)

        Args:
            user_input: 用户输入
            intent: 意图类别
            prediction: 预测概率 {H, D, A}
            actual: 实际结果 (赛后通过 record_result 填入)
        """
        record = QueryRecord(
            timestamp=datetime.now().isoformat(),
            user_input=user_input[:200],
            intent=intent,
            prediction=prediction,
            actual=actual,
            extra=extra or {},
        )
        self.all_records.append(record)

        # 限制总记录数
        if len(self.all_records) > 10000:
            self.all_records = self.all_records[-5000:]

    def record_result(self, prediction_id: int = None,
                      actual: str = None,
                      home_team: str = None, away_team: str = None):
        """
        赛后记录实际结果

        找到最近一条匹配的预测记录，标记实际结果。
        如果提供了球队名，优先匹配。

        Args:
            prediction_id: 预测记录索引 (None=最近一条)
            actual: 实际结果 'H'/'D'/'A'
            home_team: 用于匹配的主队名
            away_team: 用于匹配的客队名
        """
        if not actual or actual not in ("H", "D", "A"):
            logger.warning(f"无效的实际结果: {actual}")
            return

        # 找到待反馈的记录
        target = None
        if prediction_id is not None and prediction_id < len(self.all_records):
            target = self.all_records[prediction_id]
        else:
            # 找最近一条未标记的记录
            for r in reversed(self.all_records):
                if r.actual is None:
                    target = r
                    break

        if target is None:
            logger.warning("没有待反馈的预测记录")
            return

        # 标记结果
        target.actual = actual
        pred_class = max(target.prediction, key=target.prediction.get) if target.prediction else "?"
        target.correct = (pred_class == actual)
        target.confidence = target.prediction.get(
            {"H": "home", "D": "draw", "A": "away"}.get(actual, "draw"), 0
        )

        # 更新滚动窗口
        self.window.add(target)

        # 更新基线
        self._update_baseline()

        # 持久化
        self._save_state()

        status = "✅" if target.correct else "❌"
        logger.info(
            f"[FeedbackLoop] 结果记录: 预测{pred_class} vs 实际{actual} {status} "
            f"(Acc={self.window.accuracy:.1%}, D-Recall={self.window.d_recall:.1%})"
        )

    def check_drift(self) -> List[DriftAlert]:
        """检测性能漂移"""
        alerts = []

        if self.baseline_accuracy is None or self.window.total_records < 10:
            return alerts  # 样本不足

        # 准确率漂移
        acc_drop = self.baseline_accuracy - self.window.accuracy
        if acc_drop > self.drift_thresholds["accuracy_drop"]:
            level = "significant" if acc_drop > 0.05 else "mild"
            alerts.append(DriftAlert(
                metric="accuracy",
                baseline=self.baseline_accuracy,
                current=self.window.accuracy,
                change=-acc_drop,
                level=level,
                suggestion=f"准确率下降{acc_drop:.1%}。建议检查特征漂移或重新训练。"
            ))

        # D召回率漂移
        if self.baseline_d_recall is not None:
            d_drop = self.baseline_d_recall - self.window.d_recall
            if d_drop > self.drift_thresholds["d_recall_drop"]:
                alerts.append(DriftAlert(
                    metric="d_recall",
                    baseline=self.baseline_d_recall,
                    current=self.window.d_recall,
                    change=-d_drop,
                    level="significant",
                    suggestion=f"平局召回率下降{d_drop:.1%}。建议检查D-Gate阈值或DrawExpert衰减系数。"
                ))

        # 预测分布漂移
        if self.baseline_distribution:
            current_dist = self._get_current_distribution()
            shift = self._distribution_distance(self.baseline_distribution, current_dist)
            if shift > self.drift_thresholds["distribution_shift"]:
                alerts.append(DriftAlert(
                    metric="distribution",
                    baseline=0,
                    current=shift,
                    change=shift,
                    level="mild",
                    suggestion=f"预测分布偏移{shift:.1%}。可能数据分布变化，建议检查输入特征。"
                ))

        if alerts:
            logger.warning(f"[FeedbackLoop] 检测到{len(alerts)}个漂移告警")
            for a in alerts:
                logger.warning(f"  - {a.metric}: {a.suggestion}")

        return alerts

    def get_suggestions(self) -> List[str]:
        """获取优化建议"""
        suggestions = []

        # 检查漂移
        alerts = self.check_drift()
        for alert in alerts:
            suggestions.append(f"[{alert.level}] {alert.suggestion}")

        # 基于窗口统计的建议
        if self.window.total_records >= 20:
            if self.window.accuracy < 0.55:
                suggestions.append("准确率<55%: 建议检查模型是否过时，考虑重新训练")
            if self.window.d_recall < 0.3:
                suggestions.append("D召回率<30%: 建议增大DrawExpert权重或降低D-Gate阈值")
            if self.window.d_precision < 0.2:
                suggestions.append("D精确率<20%: 建议提高D-Gate阈值减少假平局预测")

        # 样本量建议
        if self.window.total_records < 30:
            suggestions.append(f"当前仅有{self.window.total_records}条有标签记录，建议积累至少50条后评估")

        return suggestions

    def status_summary(self) -> Dict:
        """系统状态摘要"""
        return {
            "window": {
                "size": self.window.total_records,
                "accuracy": round(self.window.accuracy, 4),
                "d_recall": round(self.window.d_recall, 4),
                "d_precision": round(self.window.d_precision, 4),
            },
            "baseline": {
                "accuracy": round(self.baseline_accuracy, 4) if self.baseline_accuracy else None,
                "d_recall": round(self.baseline_d_recall, 4) if self.baseline_d_recall else None,
            },
            "total_queries": len(self.all_records),
            "total_labeled": sum(1 for r in self.all_records if r.actual is not None),
            "drift_alerts": len(self.check_drift()),
            "suggestions": self.get_suggestions(),
        }

    # ═══════════════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════════════

    def _update_baseline(self):
        """当积累足够数据后建立基线"""
        if self.window.total_records >= 20:
            self.baseline_accuracy = self.window.accuracy
            self.baseline_d_recall = self.window.d_recall
            self.baseline_distribution = self._get_current_distribution()
            logger.info(f"[FeedbackLoop] 基线建立: Acc={self.baseline_accuracy:.1%}, "
                       f"D-Recall={self.baseline_d_recall:.1%}")

    def _get_current_distribution(self) -> Dict[str, float]:
        """当前预测分布"""
        counts = {"H": 0, "D": 0, "A": 0}
        labeled = [r for r in self.window.records if r.prediction]
        if not labeled:
            return {"H": 0.33, "D": 0.34, "A": 0.33}
        for r in labeled:
            pred_class = max(r.prediction, key=r.prediction.get)
            counts[pred_class] += 1
        total = sum(counts.values())
        return {k: v/total for k, v in counts.items()}

    def _distribution_distance(self, d1: Dict, d2: Dict) -> float:
        """计算两个分布的距离 (Total Variation Distance)"""
        keys = set(d1.keys()) | set(d2.keys())
        return 0.5 * sum(abs(d1.get(k, 0) - d2.get(k, 0)) for k in keys)

    def _save_state(self):
        """持久化状态到磁盘"""
        try:
            state = {
                "window_accuracy": self.window.accuracy,
                "window_d_recall": self.window.d_recall,
                "window_total": self.window.total_records,
                "baseline_accuracy": self.baseline_accuracy,
                "baseline_d_recall": self.baseline_d_recall,
                "total_queries": len(self.all_records),
                "total_labeled": sum(1 for r in self.all_records if r.actual is not None),
                "updated_at": datetime.now().isoformat(),
            }
            state_path = FEEDBACK_DIR / "l6_state.json"
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)

            # 保存最近100条记录
            records_path = FEEDBACK_DIR / "recent_records.json"
            recent = [r.to_dict() for r in self.all_records[-100:] if r.actual is not None]
            with open(records_path, "w", encoding="utf-8") as f:
                json.dump(recent, f, ensure_ascii=False, indent=2)

        except Exception as e:
            logger.debug(f"[FeedbackLoop] 状态持久化失败: {e}")

    def _load_state(self):
        """从磁盘恢复状态"""
        try:
            state_path = FEEDBACK_DIR / "l6_state.json"
            if state_path.exists():
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                self.baseline_accuracy = state.get("baseline_accuracy")
                self.baseline_d_recall = state.get("baseline_d_recall")
                logger.info(f"[FeedbackLoop] 状态恢复: {state.get('total_queries', 0)}条查询, "
                           f"基线Acc={self.baseline_accuracy}")

            records_path = FEEDBACK_DIR / "recent_records.json"
            if records_path.exists():
                with open(records_path, "r", encoding="utf-8") as f:
                    raw_records = json.load(f)
                for r in raw_records[-50:]:
                    record = QueryRecord(
                        timestamp=r.get("timestamp", ""),
                        user_input="",
                        intent=r.get("intent", ""),
                        prediction=r.get("prediction", {}),
                        actual=r.get("actual"),
                        correct=r.get("correct"),
                    )
                    self.window.add(record)
                logger.info(f"[FeedbackLoop] 恢复{len(raw_records)}条历史记录")

        except Exception as e:
            logger.debug(f"[FeedbackLoop] 状态恢复失败: {e}")


# ═══════════════════════════════════════════════════════════════
# 3. 单例
# ═══════════════════════════════════════════════════════════════

_feedback_instance: Optional[FeedbackLoop] = None


def get_feedback_loop(**kwargs) -> FeedbackLoop:
    global _feedback_instance
    if _feedback_instance is None:
        _feedback_instance = FeedbackLoop(**kwargs)
    return _feedback_instance

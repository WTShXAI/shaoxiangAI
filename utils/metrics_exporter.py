"""
哨响AI - 监控指标导出器 (Prometheus 格式) v1.0
===============================================
将模型预测、数据质量、系统健康等信息导出为 Prometheus 指标格式，
供 Prometheus + Grafana 可视化监控。

支持的指标:
    - football_predictions_total (Counter)
    - football_prediction_accuracy (Gauge)
    - football_prediction_latency_seconds (Histogram)
    - football_data_freshness_hours (Gauge)
    - football_data_quality_score (Gauge)
    - football_model_registry_models (Gauge)
    - football_drift_detected (Gauge)
    - football_calibration_ece (Gauge)

用法 (作为独立 HTTP 端点):
    from utils.metrics_exporter import MetricsExporter
    exporter = MetricsExporter(port=9091)
    exporter.start()

    # or in Flask:
    @app.route('/metrics')
    def metrics():
        return exporter.render()
"""
from __future__ import annotations

import threading
import logging
import time
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# 尝试导入 prometheus_client，若不可用则使用 SimpleMetrics 降级
try:
    from prometheus_client import (
        Counter, Gauge, Histogram, Summary,
        start_http_server, generate_latest, CollectorRegistry,
    )
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logger.info("prometheus_client 未安装，使用内置 SimpleMetrics")


class SimpleGauge:
    """简易指标实现 (prometheus_client 不可用时的降级方案)"""
    def __init__(self, name, doc, labelnames=None):
        self.name = name
        self.doc = doc
        self.labelnames = labelnames or []
        self._value = 0.0
        self._labels: dict[tuple, float] = {}

    def set(self, value):
        self._value = value

    def labels(self, **kwargs) -> "SimpleGauge":
        key = tuple(sorted(kwargs.items()))
        if key not in self._labels:
            self._labels[key] = SimpleGauge(self.name, self.doc)
        return self._labels[key]

    def inc(self, amount=1):
        self._value += amount

    def dec(self, amount=1):
        self._value -= amount

    def observe(self, value):
        self._value = value


class SimpleCounter(SimpleGauge):
    """简易计数器"""
    pass


class SimpleHistogram(SimpleGauge):
    """简易直方图"""
    pass


class MetricsExporter:
    """模型和数据监控指标导出器 (Prometheus + 内置降级)"""

    def __init__(self, port: int = 9091, prefix: str = "football"):
        self.prefix = prefix
        self.port = port
        self._started = False

        if PROMETHEUS_AVAILABLE:
            self._registry = CollectorRegistry()
            self._init_prometheus_metrics()
        else:
            self._init_simple_metrics()
            logger.warning("使用内置 SimpleMetrics (无 Prometheus 集成)")

    def _init_prometheus_metrics(self):
        """初始化 Prometheus 指标"""
        reg = self._registry

        self.predictions_total = Counter(
            f'{self.prefix}_predictions_total',
            'Total predictions count',
            ['league', 'result'],
            registry=reg,
        )
        self.prediction_accuracy = Gauge(
            f'{self.prefix}_prediction_accuracy',
            'Rolling prediction accuracy (7-day)',
            ['league'],
            registry=reg,
        )
        self.prediction_confidence = Histogram(
            f'{self.prefix}_prediction_confidence',
            'Prediction confidence distribution',
            buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
            registry=reg,
        )
        self.prediction_latency = Histogram(
            f'{self.prefix}_prediction_latency_seconds',
            'Prediction latency in seconds',
            buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
            registry=reg,
        )
        self.data_freshness_hours = Gauge(
            f'{self.prefix}_data_freshness_hours',
            'Hours since last data update',
            ['table'],
            registry=reg,
        )
        self.data_quality_score = Gauge(
            f'{self.prefix}_data_quality_score',
            'Overall data quality health score (0-100)',
            registry=reg,
        )
        self.model_registry_models = Gauge(
            f'{self.prefix}_model_registry_models',
            'Number of registered models',
            ['status'],
            registry=reg,
        )
        self.drift_detected = Gauge(
            f'{self.prefix}_drift_detected',
            'Data drift detected flag (0/1)',
            registry=reg,
        )
        self.calibration_ece = Gauge(
            f'{self.prefix}_calibration_ece',
            'Expected Calibration Error',
            ['model_id'],
            registry=reg,
        )

    def _init_simple_metrics(self):
        """降级 — 简易指标"""
        self.predictions_total = SimpleCounter()
        self.prediction_accuracy = SimpleGauge()
        self.prediction_confidence = SimpleHistogram()
        self.prediction_latency = SimpleHistogram()
        self.data_freshness_hours = SimpleGauge()
        self.data_quality_score = SimpleGauge()
        self.model_registry_models = SimpleGauge()
        self.drift_detected = SimpleGauge()
        self.calibration_ece = SimpleGauge()

    # ══════════════════════════════════════════════════
    # 公开接口
    # ══════════════════════════════════════════════════

    def record_prediction(self, league: str, result: str,
                          confidence: float, latency: float,
                          is_correct: bool | None = None):
        """记录一次预测"""
        self.predictions_total.labels(league=league, result=result).inc()

        if PROMETHEUS_AVAILABLE:
            self.prediction_confidence.labels().observe(confidence)
            self.prediction_latency.labels().observe(latency)
        else:
            self.prediction_confidence.observe(confidence)
            self.prediction_latency.observe(latency)

    def update_accuracy(self, league: str, accuracy: float):
        """更新滑动准确率"""
        if PROMETHEUS_AVAILABLE:
            self.prediction_accuracy.labels(league=league).set(accuracy)
        else:
            self.prediction_accuracy.set(accuracy)

    def update_freshness(self, table: str, hours_ago: float):
        """更新数据新鲜度"""
        if PROMETHEUS_AVAILABLE:
            self.data_freshness_hours.labels(table=table).set(hours_ago)
        else:
            self.data_freshness_hours.set(hours_ago)

    def update_quality_score(self, score: int):
        """更新数据质量分数"""
        self.data_quality_score.set(score)

    def update_registry_stats(self, active: int, deprecated: int, production: int):
        """更新模型注册表统计"""
        if PROMETHEUS_AVAILABLE:
            self.model_registry_models.labels(status='active').set(active)
            self.model_registry_models.labels(status='deprecated').set(deprecated)
            self.model_registry_models.labels(status='production').set(production)
        else:
            self.model_registry_models.set(active + deprecated + production)

    def update_drift_status(self, detected: bool):
        """更新漂移状态"""
        self.drift_detected.set(1 if detected else 0)

    def update_ece(self, model_id: str, ece: float):
        """更新校准误差"""
        if PROMETHEUS_AVAILABLE:
            self.calibration_ece.labels(model_id=model_id).set(ece)
        else:
            self.calibration_ece.set(ece)

    # ══════════════════════════════════════════════════
    # HTTP / Prometheus 导出
    # ══════════════════════════════════════════════════

    def start(self, background: bool = True):
        """启动 Prometheus HTTP endpoint"""
        if self._started:
            return
        self._started = True

        if PROMETHEUS_AVAILABLE:
            if background:
                t = threading.Thread(
                    target=start_http_server,
                    kwargs={'port': self.port, 'registry': self._registry},
                    daemon=True,
                )
                t.start()
            else:
                start_http_server(self.port, registry=self._registry)
            logger.info(f"Prometheus metrics endpoint: http://0.0.0.0:{self.port}/")
        else:
            logger.info(f"SimpleMetrics mode (no Prometheus endpoint)")

    def render(self) -> str:
        """渲染 Prometheus 格式指标 (用于 Flask 端点)"""
        if PROMETHEUS_AVAILABLE:
            return generate_latest(self._registry).decode('utf-8')
        else:
            # 简易文本格式
            lines = [
                f"# HELP {self.prefix}_data_quality_score Data quality score",
                f"# TYPE {self.prefix}_data_quality_score gauge",
                f"{self.prefix}_data_quality_score {self.data_quality_score._value}",
                f"# HELP {self.prefix}_drift_detected Data drift flag",
                f"# TYPE {self.prefix}_drift_detected gauge",
                f"{self.prefix}_drift_detected {self.drift_detected._value}",
            ]
            return "\n".join(lines) + "\n"

    def get_all_values(self) -> dict[str, Any]:
        """获取所有指标的快照 (非 Prometheus 格式)"""
        return {
            "timestamp": datetime.now().isoformat(),
            "data_quality_score": self.data_quality_score._value if not PROMETHEUS_AVAILABLE else None,
            "drift_detected": bool(self.drift_detected._value) if not PROMETHEUS_AVAILABLE else None,
        }


# ══════════════════════════════════════════════════
# 全局单例
# ══════════════════════════════════════════════════

_exporter: MetricsExporter | None = None


def get_metrics_exporter() -> MetricsExporter:
    """获取全局 MetricsExporter 单例"""
    global _exporter
    if _exporter is None:
        _exporter = MetricsExporter()
    return _exporter


# ══════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════

if __name__ == '__main__':
    print("启动 Prometheus metrics exporter...")
    exporter = MetricsExporter(port=9091)
    exporter.start(background=False)

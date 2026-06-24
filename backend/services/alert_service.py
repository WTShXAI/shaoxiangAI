"""
告警服务 — 多通道告警通知
支持: 日志记录 / Webhook (Slack/企业微信) / 内部告警列表
"""
import json
import logging
import time
from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum

logger = logging.getLogger(__name__)


class AlertLevel(Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertRule:
    """告警规则"""
    def __init__(
        self,
        name: str,
        metric: str,
        condition: str,  # "gt"/"lt"/"gte"/"lte"
        threshold: float,
        level: AlertLevel = AlertLevel.WARNING,
        cooldown_minutes: int = 30,
        description: str = "",
    ):
        self.name = name
        self.metric = metric
        self.condition = condition
        self.threshold = threshold
        self.level = level
        self.cooldown_minutes = cooldown_minutes
        self.description = description
        self._last_triggered: Optional[float] = None

    def evaluate(self, value: float) -> bool:
        """评估是否触发告警"""
        if self._last_triggered and (time.time() - self._last_triggered) < self.cooldown_minutes * 60:
            return False

        ops = {
            "gt": lambda a, b: a > b,
            "lt": lambda a, b: a < b,
            "gte": lambda a, b: a >= b,
            "lte": lambda a, b: a <= b,
        }
        op = ops.get(self.condition)
        if op and op(value, self.threshold):
            self._last_triggered = time.time()
            return True
        return False


class AlertService:
    """告警服务"""

    # ── 默认告警规则 ──────────────────────
    DEFAULT_RULES = [
        AlertRule("低准确率", "accuracy", "lt", 35, AlertLevel.ERROR, 60,
                  "模型准确率低于35%"),
        AlertRule("高Brier分数", "brier", "gt", 0.35, AlertLevel.WARNING, 120,
                  "Brier分数过高，校准质量下降"),
        AlertRule("高延迟", "prediction_latency_ms", "gt", 2000, AlertLevel.WARNING, 30,
                  "预测延迟超过2秒"),
        AlertRule("数据新鲜度过低", "data_freshness_hours", "gt", 48, AlertLevel.ERROR, 360,
                  "数据超过48小时未更新"),
        AlertRule("模型ECC过高", "ece", "gt", 0.10, AlertLevel.WARNING, 120,
                  "期望校准误差超过0.10"),
        AlertRule("数据漂移检测", "drift_detected", "gt", 0, AlertLevel.WARNING, 720,
                  "检测到数据分布漂移"),
    ]

    def __init__(self):
        self.rules: List[AlertRule] = list(self.DEFAULT_RULES)
        self.alerts: List[Dict] = []
        self._webhook_url: Optional[str] = None
        self.max_alerts = 1000

    def add_rule(self, rule: AlertRule):
        self.rules.append(rule)

    def set_webhook(self, url: str):
        self._webhook_url = url

    def check_metrics(self, metrics: Dict[str, float]) -> List[Dict]:
        """批量检查指标"""
        triggered = []
        for rule in self.rules:
            if rule.metric in metrics:
                value = metrics[rule.metric]
                if rule.evaluate(value):
                    alert = {
                        "rule": rule.name,
                        "metric": rule.metric,
                        "value": value,
                        "threshold": rule.threshold,
                        "condition": rule.condition,
                        "level": rule.level.value,
                        "description": rule.description,
                        "timestamp": datetime.now().isoformat(),
                    }
                    self.alerts.append(alert)
                    triggered.append(alert)
                    self._notify(alert)

        # 限制告警列表大小
        if len(self.alerts) > self.max_alerts:
            self.alerts = self.alerts[-self.max_alerts:]

        return triggered

    def _notify(self, alert: Dict):
        """发送告警通知"""
        level = alert["level"].upper()
        msg = f"[{level}] {alert['description']} | {alert['metric']}={alert['value']} (阈值: {alert['threshold']})"

        logger.warning(f"🚨 {msg}")

        # Webhook 通知
        if self._webhook_url:
            try:
                import httpx
                payload = {
                    "text": f"哨响AI 告警\n{msg}\n时间: {alert['timestamp']}",
                }
                httpx.post(self._webhook_url, json=payload, timeout=5)
            except (OSError, ValueError, KeyError) as e:
                logger.debug(f"操作失败: {e}")

    def get_recent_alerts(self, limit: int = 50, level: Optional[str] = None) -> List[Dict]:
        """获取最近告警"""
        alerts = self.alerts
        if level:
            alerts = [a for a in alerts if a["level"] == level]
        return alerts[-limit:][::-1]

    def get_rules(self) -> List[Dict]:
        """获取所有告警规则"""
        return [
            {
                "name": r.name,
                "metric": r.metric,
                "condition": r.condition,
                "threshold": r.threshold,
                "level": r.level.value,
                "cooldown_minutes": r.cooldown_minutes,
                "description": r.description,
            }
            for r in self.rules
        ]

    def clear_alerts(self):
        self.alerts = []


# 全局单例
_alert_service: Optional[AlertService] = None


def get_alert_service() -> AlertService:
    global _alert_service
    if _alert_service is None:
        _alert_service = AlertService()
    return _alert_service

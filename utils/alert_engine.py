"""
哨响AI - 智能告警引擎
====================
规则驱动的告警系统，支持多级告警、冷却期、多渠道通知。

告警规则类型:
- accuracy_drop: 准确率低于阈值
- error_rate_spike: 错误率突增
- data_staleness: 数据过期
- model_drift: 模型性能漂移
- api_degradation: API 可用性下降
- feature_missing_rate: 特征缺失率过高

用法:
    engine = AlertEngine(notifier_callback=send_notification)
    engine.check_all(db_manager, monitor)
    alerts = engine.get_recent_alerts(hours=24)
"""

from __future__ import annotations

import os
import json
import time
import threading
import logging
from datetime import datetime, timedelta
from typing import Any, Callable
from collections import deque

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════
# 告警规则定义
# ══════════════════════════════════════════════════

ALERT_RULES = {
    "accuracy_drop": {
        "name": "准确率下降",
        "level": "error",
        "description": "24小时滑动准确率低于阈值",
        "threshold": 0.35,        # 准确率低于35%告警
        "critical_threshold": 0.30,
        "cooldown_minutes": 120,  # 冷却期2小时
    },
    "error_rate_spike": {
        "name": "错误率突增",
        "level": "warning",
        "description": "1小时内错误数超过阈值",
        "threshold": 10,          # 1小时内10+错误
        "critical_threshold": 25,
        "cooldown_minutes": 60,
    },
    "data_staleness": {
        "name": "数据过期",
        "level": "warning",
        "description": "数据源超过N小时未更新",
        "threshold": 6.0,         # 6小时未更新
        "critical_threshold": 12.0,
        "cooldown_minutes": 180,
    },
    "model_drift": {
        "name": "模型漂移",
        "level": "warning",
        "description": "近期准确率与历史基线偏差超过阈值",
        "threshold": 0.08,        # 准确率下降8%以上
        "critical_threshold": 0.12,
        "cooldown_minutes": 240,
    },
    "api_degradation": {
        "name": "API服务降级",
        "level": "warning",
        "description": "API成功率低于阈值",
        "threshold": 0.80,        # 成功率低于80%
        "critical_threshold": 0.50,
        "cooldown_minutes": 30,
    },
    "feature_missing_rate": {
        "name": "特征缺失率过高",
        "level": "warning",
        "description": "特征缺失比例超过阈值",
        "threshold": 0.20,        # 20%特征缺失
        "critical_threshold": 0.40,
        "cooldown_minutes": 120,
    },
    "prediction_volume_anomaly": {
        "name": "预测量异常",
        "level": "info",
        "description": "预测量偏离正常范围",
        "threshold": 0.50,        # 低于平均的50%
        "critical_threshold": 0.20,
        "cooldown_minutes": 240,
    },
}


# ══════════════════════════════════════════════════
# 告警事件
# ══════════════════════════════════════════════════

class AlertEvent:
    """单个告警事件"""

    def __init__(
        self,
        rule_id: str,
        level: str,
        title: str,
        message: str,
        value: float,
        threshold: float,
    ):
        self.rule_id = rule_id
        self.level = level
        self.title = title
        self.message = message
        self.value = value
        self.threshold = threshold
        self.timestamp = datetime.now()
        self.acknowledged = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "level": self.level,
            "title": self.title,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
            "timestamp": self.timestamp.isoformat(),
            "acknowledged": self.acknowledged,
        }


# ══════════════════════════════════════════════════
# 告警引擎
# ══════════════════════════════════════════════════

class AlertEngine:
    """
    智能告警引擎

    功能:
    - 规则驱动多级告警 (info/warning/error/critical)
    - 冷却期防刷屏
    - 历史告警查询
    - 可选持久化到文件
    """

    def __init__(
        self,
        notifier_callback: Callable[..., bool] | None = None,
        persist_dir: str | None = None,
        max_history: int = 200,
    ):
        self._notifier = notifier_callback
        self._persist_dir = persist_dir or os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "metrics"
        )
        os.makedirs(self._persist_dir, exist_ok=True)

        self._alert_history: deque[AlertEvent] = deque(maxlen=max_history)
        self._last_fired: dict[str, float] = {}  # rule_id -> last fire timestamp
        self._baseline_accuracy: float | None = None
        self._baseline_error_rate: float | None = None
        self._lock = threading.Lock()

        # 加载历史告警
        self._load_history()

    # ─── 规则检查 ─────────────────────────────────

    def check_accuracy_drop(
        self, current_accuracy: float, predictions_total: int
    ) -> AlertEvent | None:
        """检查准确率是否低于阈值"""
        rule = ALERT_RULES["accuracy_drop"]
        if predictions_total < 5:
            return None  # 样本不足跳过

        threshold = rule["threshold"]
        critical = rule["critical_threshold"]

        if current_accuracy <= critical:
            level = "critical"
        elif current_accuracy <= threshold:
            level = "error"
        else:
            return None

        return AlertEvent(
            rule_id="accuracy_drop",
            level=level,
            title=f"⚠️ {rule['name']}: {current_accuracy * 100:.1f}%",
            message=(
                f"近24小时准确率 {current_accuracy * 100:.1f}% 低于阈值 {threshold * 100:.0f}%\n"
                f"总预测数: {predictions_total}\n"
                f"建议: 检查特征管道、数据源质量、考虑重新训练模型"
            ),
            value=current_accuracy,
            threshold=threshold,
        )

    def check_error_rate_spike(
        self, recent_errors: int, window_minutes: int = 60
    ) -> AlertEvent | None:
        """检查错误率是否突增"""
        rule = ALERT_RULES["error_rate_spike"]
        threshold = rule["threshold"]
        critical = rule["critical_threshold"]

        if recent_errors >= critical:
            level = "critical"
        elif recent_errors >= threshold:
            level = "warning"
        else:
            return None

        return AlertEvent(
            rule_id="error_rate_spike",
            level=level,
            title=f"⚠️ {rule['name']}: {recent_errors}次/{window_minutes}分钟",
            message=(
                f"近{window_minutes}分钟内发生 {recent_errors} 次错误，超过阈值 {threshold}\n"
                f"建议: 检查日志文件 logs/、API连通性、数据库状态"
            ),
            value=float(recent_errors),
            threshold=float(threshold),
        )

    def check_data_staleness(self, freshness_hours: dict[str, float]) -> AlertEvent | None:
        """检查数据新鲜度"""
        rule = ALERT_RULES["data_staleness"]
        threshold = rule["threshold"]
        critical = rule["critical_threshold"]

        stale_items = []
        max_age = 0.0
        for data_type, hours in freshness_hours.items():
            if hours >= threshold:
                stale_items.append(f"  - {data_type}: {hours:.1f}小时前")
                max_age = max(max_age, hours)

        if not stale_items:
            return None

        level = "critical" if max_age >= critical else "warning"
        return AlertEvent(
            rule_id="data_staleness",
            level=level,
            title=f"📡 {rule['name']}: 最长 {max_age:.1f}小时",
            message=(
                f"以下数据源已过期:\n" + "\n".join(stale_items) +
                f"\n\n建议: 手动触发数据同步 /api/sync-data"
            ),
            value=max_age,
            threshold=threshold,
        )

    def check_model_drift(
        self, current_accuracy: float, total_preds: int
    ) -> AlertEvent | None:
        """检查模型是否有性能漂移"""
        if total_preds < 10:
            return None

        rule = ALERT_RULES["model_drift"]
        threshold = rule["threshold"]
        critical = rule["critical_threshold"]

        # 更新基线
        if self._baseline_accuracy is None:
            self._baseline_accuracy = current_accuracy
            logger.info(f"[AlertEngine] 设定准确率基线: {current_accuracy:.4f}")
            return None

        drift = self._baseline_accuracy - current_accuracy
        if drift <= 0:
            # 模型变好了，更新基线
            if current_accuracy > self._baseline_accuracy + 0.02:
                self._baseline_accuracy = current_accuracy
                logger.info(f"[AlertEngine] 更新准确率基线: {current_accuracy:.4f}")
            return None

        if drift >= critical:
            level = "critical"
        elif drift >= threshold:
            level = "warning"
        else:
            return None

        return AlertEvent(
            rule_id="model_drift",
            level=level,
            title=f"📉 {rule['name']}: {drift * 100:.1f}% 下降",
            message=(
                f"准确率从基线 {self._baseline_accuracy * 100:.1f}% 降至 {current_accuracy * 100:.1f}%\n"
                f"下降幅度: {drift * 100:.1f}%（阈值 {threshold * 100:.0f}%）\n"
                f"总预测数: {total_preds}\n"
                f"建议: 检查数据分布变化，可能需启动重新训练"
            ),
            value=drift,
            threshold=threshold,
        )

    def check_api_degradation(
        self, api_name: str, success_rate: float, total_calls: int
    ) -> AlertEvent | None:
        """检查API是否降级"""
        if total_calls < 5:
            return None

        rule = ALERT_RULES["api_degradation"]
        threshold = rule["threshold"]
        critical = rule["critical_threshold"]

        if success_rate <= critical / 100:
            level = "critical"
        elif success_rate <= threshold:
            level = "warning"
        else:
            return None

        return AlertEvent(
            rule_id="api_degradation",
            level=level,
            title=f"🔌 {rule['name']}: {api_name} {success_rate * 100:.0f}%",
            message=(
                f"API '{api_name}' 成功率降至 {success_rate * 100:.1f}%\n"
                f"总调用: {total_calls}次\n"
                f"建议: 检查 API Key 是否过期、切换备用数据源"
            ),
            value=success_rate,
            threshold=threshold,
        )

    def check_feature_missing_rate(
        self, missing_rate: float, feature_name: str = ""
    ) -> AlertEvent | None:
        """检查特征缺失率"""
        rule = ALERT_RULES["feature_missing_rate"]
        threshold = rule["threshold"]
        critical = rule["critical_threshold"]

        if missing_rate >= critical:
            level = "critical"
        elif missing_rate >= threshold:
            level = "warning"
        else:
            return None

        return AlertEvent(
            rule_id="feature_missing_rate",
            level=level,
            title=f"📊 {rule['name']}: {missing_rate * 100:.1f}%",
            message=(
                f"特征 '{feature_name}' 缺失率 {missing_rate * 100:.1f}% 超过阈值\n"
                f"建议: 检查数据采集管道、API数据源是否正常"
            ),
            value=missing_rate,
            threshold=threshold,
        )

    def check_prediction_volume(
        self, current_volume: int, baseline_volume: float
    ) -> AlertEvent | None:
        """检查预测量是否异常"""
        if baseline_volume < 3:
            return None

        ratio = current_volume / max(baseline_volume, 1)
        rule = ALERT_RULES["prediction_volume_anomaly"]
        threshold = rule["threshold"]
        critical = rule["critical_threshold"]

        if ratio <= critical:
            level = "warning"
        elif ratio <= threshold:
            level = "info"
        else:
            return None

        return AlertEvent(
            rule_id="prediction_volume_anomaly",
            level=level,
            title=f"📊 {rule['name']}: {current_volume} vs 基线 {baseline_volume:.0f}",
            message=(
                f"当前预测数 {current_volume} 远低于基线 {baseline_volume:.0f}\n"
                f"比例: {ratio * 100:.0f}%\n"
                f"建议: 检查数据采集是否正常、是否有联赛停赛期"
            ),
            value=ratio,
            threshold=threshold,
        )

    # ─── 综合检查 ────────────────────────────────

    def check_all(self, db_manager=None, monitor=None) -> list[AlertEvent]:
        """
        运行所有规则，返回新触发的告警列表。
        自动处理冷却期（同规则在冷却期内不重复触发）。
        """
        new_alerts: list[AlertEvent] = []
        now = time.time()

        # 1. 准确率检查
        if monitor:
            accuracy_trend = monitor.get_accuracy_trend(window_hours=24)
            if accuracy_trend.get("accuracy") is not None:
                alert = self.check_accuracy_drop(
                    accuracy_trend["accuracy"],
                    accuracy_trend.get("total", 0),
                )
                if alert and self._can_fire("accuracy_drop", now):
                    new_alerts.append(alert)

                # 模型漂移检查
                drift_alert = self.check_model_drift(
                    accuracy_trend["accuracy"],
                    accuracy_trend.get("total", 0),
                )
                if drift_alert and self._can_fire("model_drift", now):
                    new_alerts.append(drift_alert)

            # 2. 错误率检查
            error_summary = monitor.get_error_summary(minutes=60)
            recent_errors = error_summary.get("recent_errors", 0)
            if recent_errors > 0:
                alert = self.check_error_rate_spike(recent_errors, 60)
                if alert and self._can_fire("error_rate_spike", now):
                    new_alerts.append(alert)

            # 3. 数据新鲜度
            data_report = monitor.get_data_quality_report()
            freshness = data_report.get("freshness", {})
            if freshness:
                alert = self.check_data_staleness(freshness)
                if alert and self._can_fire("data_staleness", now):
                    new_alerts.append(alert)

            # 4. 特征缺失率
            missing_rates = data_report.get("feature_missing_rate", {})
            for fname, fmiss in missing_rates.items():
                if fmiss > ALERT_RULES["feature_missing_rate"]["threshold"]:
                    alert = self.check_feature_missing_rate(fmiss, fname)
                    if alert and self._can_fire(f"feature_missing_rate_{fname}", now):
                        new_alerts.append(alert)
                        break  # 仅触发一次

        # 5. API 降级检查
        if monitor:
            sys_health = monitor.get_system_health()
            api_health = sys_health.get("api_health", {})
            for api_name, stats in api_health.items():
                total = stats.get("total_calls", 0)
                rate = stats.get("success_rate")
                if rate is not None and total > 0:
                    alert = self.check_api_degradation(api_name, rate, total)
                    if alert and self._can_fire(f"api_degradation_{api_name}", now):
                        new_alerts.append(alert)

        # 记录告警
        with self._lock:
            for alert in new_alerts:
                self._alert_history.append(alert)

        # 发送通知
        if new_alerts and self._notifier:
            for alert in new_alerts:
                try:
                    self._notifier(
                        title=alert.title,
                        body=alert.message,
                        level=alert.level,
                    )
                except (Exception) as e:
                    logger.error(f"[AlertEngine] 通知发送失败: {e}")

        # 持久化
        if new_alerts:
            self._persist()

        return new_alerts

    def _can_fire(self, rule_id: str, now: float) -> bool:
        """检查是否在冷却期内"""
        rule_id_base = rule_id.split("_")[0] if "_" in rule_id else rule_id
        rule = ALERT_RULES.get(rule_id_base)
        if not rule:
            return True

        cooldown = rule.get("cooldown_minutes", 60) * 60
        if rule_id not in self._last_fired:
            self._last_fired[rule_id] = now
            return True

        if now - self._last_fired[rule_id] >= cooldown:
            self._last_fired[rule_id] = now
            return True

        return False

    # ─── 查询接口 ─────────────────────────────────

    def get_recent_alerts(
        self, hours: int = 24, level: str | None = None
    ) -> list[dict[str, Any]]:
        """获取近期告警"""
        cutoff = datetime.now() - timedelta(hours=hours)
        with self._lock:
            alerts = [
                a.to_dict()
                for a in self._alert_history
                if a.timestamp >= cutoff
            ]
            if level:
                alerts = [a for a in alerts if a["level"] == level]
            return sorted(alerts, key=lambda x: x["timestamp"], reverse=True)

    def acknowledge_all(self) -> int:
        """确认所有告警"""
        count = 0
        with self._lock:
            for a in self._alert_history:
                if not a.acknowledged:
                    a.acknowledged = True
                    count += 1
        return count

    def get_summary(self) -> dict[str, Any]:
        """告警摘要"""
        with self._lock:
            total = len(self._alert_history)
            active = sum(1 for a in self._alert_history if not a.acknowledged)
            by_level: dict[str, int] = {}
            for a in self._alert_history:
                by_level[a.level] = by_level.get(a.level, 0) + 1

        return {
            "total_alerts": total,
            "active_alerts": active,
            "by_level": by_level,
            "last_check": (
                self._alert_history[-1].timestamp.isoformat()
                if self._alert_history
                else None
            ),
        }

    # ─── 持久化 ───────────────────────────────────

    def _persist_path(self) -> str:
        return os.path.join(self._persist_dir, "alerts_history.json")

    def _persist(self) -> None:
        """持久化告警历史"""
        try:
            path = self._persist_path()
            with self._lock:
                data = [a.to_dict() for a in self._alert_history]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except (Exception, KeyError, IndexError, IOError, FileNotFoundError) as e:
            logger.error(f"[AlertEngine] 持久化失败: {e}")

    def _load_history(self) -> None:
        """从文件加载告警历史"""
        path = self._persist_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                for item in data[-200:]:  # 最多200条
                    alert = AlertEvent(
                        rule_id=item["rule_id"],
                        level=item["level"],
                        title=item["title"],
                        message=item["message"],
                        value=item["value"],
                        threshold=item["threshold"],
                    )
                    try:
                        alert.timestamp = datetime.fromisoformat(item["timestamp"])
                    except (ValueError, KeyError):
                        pass
                    alert.acknowledged = item.get("acknowledged", False)
                    self._alert_history.append(alert)
            logger.info(f"[AlertEngine] 加载 {len(self._alert_history)} 条历史告警")
        except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
            logger.error(f"[AlertEngine] 加载历史失败: {e}")

    def shutdown(self) -> None:
        """关闭引擎"""
        self._persist()
        logger.info("[AlertEngine] 已关闭")


# ══════════════════════════════════════════════════
# 全局单例
# ══════════════════════════════════════════════════

_alert_engine: AlertEngine | None = None


def get_alert_engine(notifier: Callable[..., bool] | None = None) -> AlertEngine:
    """获取全局告警引擎单例"""
    global _alert_engine
    if _alert_engine is None:
        _alert_engine = AlertEngine(notifier_callback=notifier or _default_notifier)
    return _alert_engine


def _default_notifier(title: str, body: str, level: str) -> bool:
    """默认通知器（仅日志）"""
    level_map = {
        "info": logger.info,
        "warning": logger.warning,
        "error": logger.error,
        "critical": logger.critical,
    }
    log_fn = level_map.get(level, logger.warning)
    log_fn(f"[告警] {title}: {body[:200]}")
    return True

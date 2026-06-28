"""
哨响AI - 四维监控系统
=====================
1. 业务指标：预测准确率、置信度分布、按联赛/时间窗口
2. 系统指标：CPU/内存使用率、预测耗时、API响应时间
3. 数据指标：数据新鲜度、异常值比例、特征缺失率
4. 错误指标：失败率、重试次数、API错误分类

用法:
    monitor = Monitor()
    monitor.record_prediction(prediction_result, latency_ms)
    monitor.record_api_call(api_name, success, latency_ms)
    report = monitor.get_report()
"""

import os
import time
import json
import threading
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════
# 指标收集器
# ══════════════════════════════════════════════════

class MetricsCollector:
    """通用指标收集器（滑动窗口）"""

    def __init__(self, window_size: int = 1000):
        self._lock = threading.Lock()
        self._data = deque(maxlen=window_size)
        self._counters = defaultdict(int)
        self._gauges: Dict[str, float] = {}

    def push(self, record: Dict):
        """添加一条记录"""
        record["_ts"] = time.time()
        with self._lock:
            self._data.append(record)

    def set_gauge(self, name: str, value: float):
        """设置瞬时值"""
        self._gauges[name] = value

    def increment(self, counter_name: str, delta: int = 1):
        """递增计数器"""
        with self._lock:
            self._counters[counter_name] += delta

    def get_recent(self, seconds: int = 300) -> List[Dict]:
        """获取最近 N 秒的记录"""
        cutoff = time.time() - seconds
        with self._lock:
            return [r for r in self._data if r.get("_ts", 0) >= cutoff]

    def get_stats(self, seconds: int = 3600) -> Dict:
        """获取统计摘要"""
        recent = self.get_recent(seconds)
        count = len(recent)
        if count == 0:
            return {"count": 0}

        # 数值字段的平均值和分位数
        num_fields = defaultdict(list)
        for r in recent:
            for k, v in r.items():
                if isinstance(v, (int, float)) and not k.startswith("_"):
                    num_fields[k].append(v)

        stats = {"count": count}
        for field, values in num_fields.items():
            if len(values) < 2:
                continue
            values.sort()
            stats[f"{field}_avg"] = round(sum(values) / len(values), 4)
            stats[f"{field}_p50"] = round(values[len(values) // 2], 4)
            stats[f"{field}_p95"] = round(values[int(len(values) * 0.95)], 4)
            stats[f"{field}_p99"] = round(values[int(len(values) * 0.99)], 4)
            stats[f"{field}_max"] = round(max(values), 4)
            stats[f"{field}_min"] = round(min(values), 4)

        return stats

    @property
    def counters(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counters)

    @property
    def gauges(self) -> Dict[str, float]:
        return dict(self._gauges)

# ══════════════════════════════════════════════════
# 四维监控器
# ══════════════════════════════════════════════════

class Monitor:
    """
    哨响AI 监控系统

    用法:
        monitor = Monitor(db_path="data/football_data.db")
        # 记录预测
        monitor.record_prediction(match_id, home_prob, draw_prob, away_prob,
                                  prediction, actual, latency_ms)
        # 记录 API 调用
        monitor.record_api_call("football_data_org", success=True, latency_ms=234)
        # 获取报告
        report = monitor.get_report()
    """

    def __init__(self, db_path: str = None, metrics_dir: str = None):
        self.db_path = db_path
        self.metrics_dir = metrics_dir or os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "metrics"
        )
        os.makedirs(self.metrics_dir, exist_ok=True)

        # 四维收集器
        self.business = MetricsCollector(window_size=5000)   # 业务指标
        self.system = MetricsCollector(window_size=2000)     # 系统指标
        self.data_quality = MetricsCollector(window_size=1000) # 数据指标
        self.errors = MetricsCollector(window_size=1000)     # 错误指标

        # 定时持久化
        self._last_persist = time.time()
        self._persist_interval = 300  # 每5分钟持久化

        # 系统资源监控线程
        self._resource_thread = None
        self._stop_resource_thread = threading.Event()

        # 启动资源监控
        self._start_resource_monitor()

    # ══════════════════════════════════════════════════
    # 1. 业务指标
    # ══════════════════════════════════════════════════

    def record_prediction(self, match_id: int, home_prob: float,
                          draw_prob: float, away_prob: float,
                          prediction: str, actual: str = None,
                          latency_ms: float = None, league: str = None):
        """记录一次预测"""
        is_correct = None
        if actual:
            is_correct = (prediction == actual)

        confidence = max(home_prob, draw_prob, away_prob)

        record = {
            "match_id": match_id,
            "home_prob": home_prob,
            "draw_prob": draw_prob,
            "away_prob": away_prob,
            "prediction": prediction,
            "actual": actual,
            "is_correct": is_correct,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "league": league,
        }

        self.business.push(record)
        self._maybe_persist()

        # 更新计数器
        self.business.increment("total_predictions")
        if is_correct is True:
            self.business.increment("correct_predictions")
        elif is_correct is False:
            self.business.increment("incorrect_predictions")

        # 更新平局专项统计
        if prediction == "D":
            self.business.increment("draw_predictions")
            if is_correct is True:
                self.business.increment("draw_correct")
            elif is_correct is False:
                self.business.increment("draw_incorrect")

        # 系统延迟记录
        if latency_ms is not None:
            self.system.push({"type": "prediction_latency", "ms": latency_ms})

    def get_accuracy_trend(self, window_hours: int = 24) -> Dict:
        """获取近期准确率趋势"""
        recent = self.business.get_recent(seconds=window_hours * 3600)
        total = len(recent)
        correct = sum(1 for r in recent if r.get("is_correct") is True)
        incorrect = sum(1 for r in recent if r.get("is_correct") is False)
        unknown = total - correct - incorrect

        # 按联赛分组
        by_league = defaultdict(lambda: {"total": 0, "correct": 0})
        for r in recent:
            lg = r.get("league", "unknown")
            by_league[lg]["total"] += 1
            if r.get("is_correct"):
                by_league[lg]["correct"] += 1

        league_accuracy = {}
        for lg, stats in by_league.items():
            league_accuracy[lg] = round(
                stats["correct"] / max(stats["total"], 1), 4
            )

        # 置信度分布
        conf_bins = {"0.3-0.5": 0, "0.5-0.7": 0, "0.7-0.85": 0, "0.85-1.0": 0}
        for r in recent:
            c = r.get("confidence", 0)
            if c < 0.5:
                conf_bins["0.3-0.5"] += 1
            elif c < 0.7:
                conf_bins["0.5-0.7"] += 1
            elif c < 0.85:
                conf_bins["0.7-0.85"] += 1
            else:
                conf_bins["0.85-1.0"] += 1

        return {
            "total": total,
            "correct": correct,
            "incorrect": incorrect,
            "unknown": unknown,
            "accuracy": round(correct / max(total - unknown, 1), 4) if total > 0 else None,
            "by_league": league_accuracy,
            "confidence_distribution": conf_bins,
            "window_hours": window_hours,
        }

    # ══════════════════════════════════════════════════
    # 2. 系统指标
    # ══════════════════════════════════════════════════

    def record_api_call(self, api_name: str, success: bool,
                        latency_ms: float, status_code: int = None):
        """记录 API 调用"""
        self.system.push({
            "type": "api_call",
            "api": api_name,
            "success": success,
            "latency_ms": latency_ms,
            "status_code": status_code,
        })

        self.system.increment(f"api_{api_name}_total")
        if success:
            self.system.increment(f"api_{api_name}_success")
        else:
            self.system.increment(f"api_{api_name}_failure")

        # 错误记录
        if not success:
            self.errors.push({
                "type": "api_failure",
                "api": api_name,
                "latency_ms": latency_ms,
                "status_code": status_code,
            })
            self.errors.increment("api_failures")

    def record_model_load_time(self, ms: float):
        """记录模型加载时间"""
        self.system.set_gauge("model_load_time_ms", ms)

    def _start_resource_monitor(self):
        """启动后台资源监控线程"""
        def _monitor_loop():
            while not self._stop_resource_thread.wait(timeout=30):
                try:
                    import psutil
                    cpu = psutil.cpu_percent(interval=1)
                    mem = psutil.virtual_memory()
                    self.system.set_gauge("cpu_percent", cpu)
                    self.system.set_gauge("memory_percent", mem.percent)
                    self.system.set_gauge("memory_used_mb",
                                          mem.used / (1024 * 1024))
                except ImportError:
                    # psutil 未安装，跳过
                    pass
                except (Exception) as e:
                    logger.debug(f"[Monitor] 资源采集失败: {e}")

        self._resource_thread = threading.Thread(
            target=_monitor_loop, daemon=True, name="resource_monitor"
        )
        self._resource_thread.start()

    def get_system_health(self) -> Dict:
        """系统健康检查"""
        gauges = self.system.gauges
        api_stats = self.system.get_stats(seconds=1800)

        # 各 API 健康状况
        api_health = {}
        for api_name in ["football_data_org", "the_odds_api", "api_football"]:
            total = self.system.counters.get(f"api_{api_name}_total", 0)
            success = self.system.counters.get(f"api_{api_name}_success", 0)
            api_health[api_name] = {
                "total_calls": total,
                "success_rate": round(success / max(total, 1), 4) if total > 0 else None,
            }

        return {
            "cpu_percent": gauges.get("cpu_percent"),
            "memory_percent": gauges.get("memory_percent"),
            "memory_used_mb": gauges.get("memory_used_mb"),
            "api_health": api_health,
            "prediction_latency_stats": self.system.get_stats(seconds=600),
        }

    # ══════════════════════════════════════════════════
    # 3. 数据指标
    # ══════════════════════════════════════════════════

    def record_data_freshness(self, data_type: str, last_update: str,
                              match_count: int = 0):
        """记录数据新鲜度"""
        if isinstance(last_update, str):
            try:
                last_dt = datetime.fromisoformat(last_update)
            except ValueError:
                last_dt = datetime.now(timezone.utc)
        else:
            last_dt = last_update

        hours_ago = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600

        self.data_quality.set_gauge(f"freshness_{data_type}_hours", hours_ago)
        self.data_quality.push({
            "type": "freshness",
            "data_type": data_type,
            "hours_ago": hours_ago,
            "match_count": match_count,
        })

    def record_anomaly(self, feature_name: str, value: float,
                       expected_range: str = ""):
        """记录异常值"""
        self.data_quality.push({
            "type": "anomaly",
            "feature": feature_name,
            "value": value,
            "expected_range": expected_range,
        })
        self.data_quality.increment(f"anomaly_{feature_name}")

    def record_missing_feature(self, match_id: int, feature_name: str):
        """记录特征缺失"""
        self.data_quality.push({
            "type": "missing_feature",
            "match_id": match_id,
            "feature": feature_name,
        })
        self.data_quality.increment("missing_features")

    def get_data_quality_report(self) -> Dict:
        """数据质量报告"""
        # 计算特征缺失率（从数据库）
        missing_rate = {}
        if self.db_path:
            try:
                conn = sqlite3.connect(self.db_path)
                conn.row_factory = sqlite3.Row
                total = conn.execute(
                    "SELECT COUNT(*) FROM match_features"
                ).fetchone()[0]
                if total > 0:
                    cols = conn.execute(
                        "PRAGMA table_info(match_features)"
                    ).fetchall()
                    for col in cols:
                        cname = col[1]
                        if cname in ("match_id", "id", "created_at"):
                            continue
                        nulls = conn.execute(
                            f"SELECT COUNT(*) FROM match_features WHERE "
                            f"{cname} IS NULL OR {cname} = ''"
                        ).fetchone()[0]
                        missing_rate[cname] = round(nulls / total, 4)
                conn.close()
            except (Exception, KeyError, IndexError, sqlite3.Error) as e:
                logger.error(f"[Monitor] 数据质量查询失败: {e}")

        # 异常值统计
        anomaly_counters = {
            k: v for k, v in self.data_quality.counters.items()
            if k.startswith("anomaly_")
        }

        return {
            "feature_missing_rate": missing_rate,
            "total_anomalies_detected": sum(anomaly_counters.values()),
            "anomaly_by_feature": anomaly_counters,
            "freshness": {
                k: round(v, 1) for k, v in self.data_quality.gauges.items()
                if k.startswith("freshness_")
            },
        }

    # ══════════════════════════════════════════════════
    # 4. 错误指标
    # ══════════════════════════════════════════════════

    def record_error(self, error_type: str, source: str,
                     message: str = "", retry_count: int = 0):
        """记录错误"""
        self.errors.push({
            "type": "error",
            "error_type": error_type,
            "source": source,
            "message": message[:200],
            "retry_count": retry_count,
        })
        self.errors.increment(f"error_{error_type}")
        self.errors.increment("total_errors")

    def get_error_summary(self, minutes: int = 60) -> Dict:
        """错误摘要"""
        recent = self.errors.get_recent(seconds=minutes * 60)
        by_type = defaultdict(int)
        by_source = defaultdict(int)
        for r in recent:
            by_type[r.get("error_type", "unknown")] += 1
            by_source[r.get("source", "unknown")] += 1

        total_retries = self.errors.counters.get("retry_total", 0)
        total_errors = self.errors.counters.get("total_errors", 0)

        return {
            "recent_errors": len(recent),
            "total_errors": total_errors,
            "total_retries": total_retries,
            "retry_rate": round(
                total_retries / max(total_errors, 1), 2
            ),
            "by_type": dict(by_type),
            "by_source": dict(by_source),
            "window_minutes": minutes,
        }

    # ══════════════════════════════════════════════════
    # 综合报告
    # ══════════════════════════════════════════════════

    def get_report(self) -> Dict:
        """生成完整四维监控报告"""
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "business": self.get_accuracy_trend(),
            "system": self.get_system_health(),
            "data_quality": self.get_data_quality_report(),
            "errors": self.get_error_summary(),
            "counters": {
                "predictions": self.business.counters.get("total_predictions", 0),
                "correct": self.business.counters.get("correct_predictions", 0),
                "draw_predictions": self.business.counters.get("draw_predictions", 0),
                "draw_correct": self.business.counters.get("draw_correct", 0),
            },
        }

    def _maybe_persist(self):
        """定时持久化指标到文件"""
        now = time.time()
        if now - self._last_persist < self._persist_interval:
            return
        self._last_persist = now

        try:
            report = self.get_report()
            date_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            path = os.path.join(self.metrics_dir, f"monitor_{date_str}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)

            # 只保留最近50个报告
            files = sorted(os.listdir(self.metrics_dir))
            for old in files[:-50]:
                os.remove(os.path.join(self.metrics_dir, old))

        except (Exception, KeyError, IndexError, IOError, FileNotFoundError) as e:
            logger.error(f"[Monitor] 持久化失败: {e}")

    def save_report(self) -> str:
        """立即保存一份报告"""
        self._last_persist = 0
        self._maybe_persist()
        # 返回最新的文件路径
        files = sorted(
            [f for f in os.listdir(self.metrics_dir) if f.startswith("monitor_")]
        )
        if files:
            return os.path.join(self.metrics_dir, files[-1])
        return ""

    def shutdown(self):
        """关闭监控器"""
        self._stop_resource_thread.set()
        if self._resource_thread:
            self._resource_thread.join(timeout=2)
        self.save_report()

# ══════════════════════════════════════════════════
# Python 装饰器：自动记录性能/错误
# ══════════════════════════════════════════════════

_monitor_instance: Optional[Monitor] = None

def get_monitor(db_path: str = None) -> Monitor:
    """获取全局 Monitor 单例"""
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = Monitor(db_path=db_path)
    return _monitor_instance

def track_performance(api_name: str = None):
    """装饰器：自动记录函数执行时间和错误"""
    def decorator(func):
        from functools import wraps
        @wraps(func)
        def wrapper(*args, **kwargs):
            monitor = get_monitor()
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
                elapsed = (time.perf_counter() - start) * 1000
                name = api_name or func.__name__
                monitor.record_api_call(name, True, elapsed)
                return result
            except (Exception) as e:
                elapsed = (time.perf_counter() - start) * 1000
                name = api_name or func.__name__
                monitor.record_api_call(name, False, elapsed)
                monitor.record_error(
                    type(e).__name__, name, str(e)
                )
                raise
        return wrapper
    return decorator

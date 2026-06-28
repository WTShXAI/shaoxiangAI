"""OpenTelemetry 可观测性基础设施

三大支柱: Metrics + Logs + Traces
用法:
    from utils.observability import setup_observability, get_tracer, get_meter, trace_agent

    # 应用启动时
    setup_observability(service_name="footballai", endpoint="http://localhost:4317")

    # Agent 追踪
    @trace_agent("trend_analyzer")
    def analyze(match_data):
        ...
"""

import os
import time
import functools
import logging
from contextlib import contextmanager
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# ============================================================
# 可观测性核心 - 降级设计 (无 OTel 依赖时仍可工作)
# ============================================================

_tracer = None
_meter = None
_metrics: Dict[str, Any] = {}

# Agent 追踪计数器
_agent_call_count: Dict[str, int] = {}
_agent_call_duration: Dict[str, float] = {}
_agent_error_count: Dict[str, int] = {}

def setup_observability(
    service_name: str = "footballai",
    endpoint: Optional[str] = None,
    enable_traces: bool = True,
    enable_metrics: bool = True,
) -> None:
    """初始化 OpenTelemetry 可观测性

    降级设计: 如果 opentelemetry 未安装，自动降级为内存指标
    """
    global _tracer, _meter

    otlp_endpoint = endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    try:
        from opentelemetry import trace, metrics
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create({"service.name": service_name})

        # Traces
        if enable_traces:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
                provider = TracerProvider(resource=resource)
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
                trace.set_tracer_provider(provider)
                _tracer = trace.get_tracer(service_name)
                logger.info(f"OTel Traces → {otlp_endpoint}")
            except ImportError:
                _tracer = trace.get_tracer(service_name)
                logger.info("OTel Traces (local only, no exporter)")

        # Metrics
        if enable_metrics:
            try:
                from opentelemetry.exporter.prometheus import PrometheusMetricReader
                reader = PrometheusMetricReader()
                provider = MeterProvider(resource=resource, metric_readers=[reader])
                metrics.set_meter_provider(provider)
                _meter = metrics.get_meter(service_name)
                logger.info("OTel Metrics → Prometheus :8889")
            except ImportError:
                _meter = metrics.get_meter(service_name)
                logger.info("OTel Metrics (local only)")

    except ImportError:
        logger.info("OpenTelemetry not installed, using in-memory metrics fallback")
        _tracer = None
        _meter = None

def get_tracer():
    """获取 tracer (可能为 None)"""
    return _tracer

def get_meter():
    """获取 meter (可能为 None)"""
    return _meter

# ============================================================
# Agent 追踪装饰器
# ============================================================

def trace_agent(agent_name: str, version: str = "1.0"):
    """Agent 执行追踪装饰器

    自动记录:
    - 调用次数
    - 执行耗时
    - 错误率
    - OTel Span (如果可用)
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()

            # 内存指标
            _agent_call_count[agent_name] = _agent_call_count.get(agent_name, 0) + 1

            # OTel Span
            if _tracer:
                with _tracer.start_as_current_span(f"agent.{agent_name}") as span:
                    span.set_attribute("agent.name", agent_name)
                    span.set_attribute("agent.version", version)
                    try:
                        result = func(*args, **kwargs)
                        span.set_attribute("agent.status", "ok")
                        return result
                    except (Exception) as e:
                        span.set_attribute("agent.status", "error")
                        span.set_attribute("agent.error", str(e))
                        span.record_exception(e)
                        _agent_error_count[agent_name] = _agent_error_count.get(agent_name, 0) + 1
                        raise
            else:
                try:
                    return func(*args, **kwargs)
                except (Exception, KeyError, IndexError, requests.exceptions.RequestException):
                    _agent_error_count[agent_name] = _agent_error_count.get(agent_name, 0) + 1
                    raise
                finally:
                    elapsed = time.perf_counter() - start
                    _agent_call_duration[agent_name] = _agent_call_duration.get(agent_name, 0) + elapsed

            elapsed = time.perf_counter() - start
            _agent_call_duration[agent_name] = _agent_call_duration.get(agent_name, 0) + elapsed

        return wrapper
    return decorator

# ============================================================
# 上下文管理器
# ============================================================

@contextmanager
def trace_operation(operation_name: str, **attributes):
    """操作追踪上下文管理器"""
    start = time.perf_counter()
    if _tracer:
        with _tracer.start_as_current_span(operation_name) as span:
            for k, v in attributes.items():
                span.set_attribute(k, str(v))
            try:
                yield span
            except (Exception) as e:
                span.record_exception(e)
                raise
    else:
        yield None
    elapsed = time.perf_counter() - start
    _agent_call_duration[operation_name] = _agent_call_duration.get(operation_name, 0) + elapsed

# ============================================================
# 指标查询
# ============================================================

def get_agent_metrics() -> Dict[str, Any]:
    """获取所有 Agent 运行指标"""
    result = {}
    for name in set(list(_agent_call_count.keys()) + list(_agent_error_count.keys())):
        calls = _agent_call_count.get(name, 0)
        errors = _agent_error_count.get(name, 0)
        total_time = _agent_call_duration.get(name, 0)
        avg_time = total_time / calls if calls > 0 else 0
        result[name] = {
            "calls": calls,
            "errors": errors,
            "error_rate": errors / calls if calls > 0 else 0,
            "total_duration_s": round(total_time, 3),
            "avg_duration_ms": round(avg_time * 1000, 2),
        }
    return result

def reset_metrics():
    """重置所有指标"""
    _agent_call_count.clear()
    _agent_call_duration.clear()
    _agent_error_count.clear()

# ============================================================
# Flask 集成
# ============================================================

def instrument_flask_app(app):
    """为 Flask 应用添加可观测性中间件

    自动添加:
    - /api/metrics — Prometheus 指标端点
    - /api/traces — Agent 追踪指标
    - 请求计时中间件
    """
    try:
        from opentelemetry.instrumentation.flask import FlaskInstrumentor
        FlaskInstrumentor().instrument_app(app)
        logger.info("Flask OTel instrumentation enabled")
    except ImportError:
        logger.info("Flask OTel instrumentation not available, using middleware")

    @app.before_request
    def before_request():
        from flask import g
        g.request_start = time.perf_counter()

    @app.after_request
    def after_request(response):
        from flask import g
        elapsed = time.perf_counter() - getattr(g, 'request_start', time.perf_counter())
        _agent_call_duration['http_request'] = _agent_call_duration.get('http_request', 0) + elapsed
        _agent_call_count['http_request'] = _agent_call_count.get('http_request', 0) + 1
        return response

    @app.route('/api/metrics')
    def prometheus_metrics():
        """Prometheus 格式指标"""
        lines = []
        for name, m in get_agent_metrics().items():
            safe = name.replace('-', '_').replace('.', '_')
            lines.append(f'footballai_agent_calls{{agent="{safe}"}} {m["calls"]}')
            lines.append(f'footballai_agent_errors{{agent="{safe}"}} {m["errors"]}')
            lines.append(f'footballai_agent_avg_duration_ms{{agent="{safe}"}} {m["avg_duration_ms"]}')
        return '\n'.join(lines) + '\n', 200, {'Content-Type': 'text/plain'}

    @app.route('/api/traces')
    def trace_metrics():
        """JSON 格式追踪指标"""
        import json
        return json.dumps(get_agent_metrics(), indent=2), 200, {'Content-Type': 'application/json'}

"""结构化 JSON 日志配置 + trace_id 注入 (REQ-10).

契约 (docs/system_design.md §7):
    统一 JSON 行日志, UTF-8, 字段至少含 ``ts / level / logger / trace_id / msg``。

实现要点:
    * 优先使用 ``python-json-logger``; 缺失时用内置 ``JsonFormatter``(标准库 json),
      **输出字段与顺序完全一致**, 上层无感。
    * ``trace_id`` 走 ``contextvars.ContextVar``, 天然支持 asyncio 任务/线程隔离;
      通过 ``TraceIdFilter`` 注入到每条 ``LogRecord``。
    * 所有 handler 使用 ``core.safe_log.SafeStreamHandler``: 写入前按流编码
      ``backslashreplace`` 转义, 中文/emoji 不再崩轮 (REQ-05)。
    * ``configure_logging()`` 幂等, 重复调用不会叠加 handler。
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar, Token
from typing import Any, Dict, Iterator, Optional

from core.config import Config, get_config
from core.safe_log import (
    DEFAULT_ERRORS,
    SafeStreamHandler,
    install_utf8,
    safe_str,
)

__all__ = [
    "TRACE_ID_KEY",
    "JsonFormatter",
    "TraceIdFilter",
    "StructuredLogger",
    "configure_logging",
    "get_logger",
    "new_trace_id",
    "get_trace_id",
    "set_trace_id",
    "reset_trace_id",
    "trace_context",
    "HAS_JSON_LOGGER",
]

#: LogRecord / JSON 中 trace_id 字段名。
TRACE_ID_KEY = "trace_id"

#: 无 trace 上下文时的占位值 (保证字段永远存在, 便于日志检索不漏行)。
NO_TRACE = "-"

_trace_id_var: ContextVar[str] = ContextVar("shaoxiang_trace_id", default=NO_TRACE)

_configured = False
_configured_level = logging.INFO

HAS_JSON_LOGGER = False
try:  # pragma: no cover - 取决于运行环境是否装了 python-json-logger
    from pythonjsonlogger import jsonlogger as _jsonlogger  # type: ignore

    HAS_JSON_LOGGER = True
except Exception:  # 缺依赖时走标准库实现
    _jsonlogger = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# trace_id 管理
# ---------------------------------------------------------------------------


def new_trace_id(prefix: str = "") -> str:
    """生成一个新的短 trace_id。

    Args:
        prefix: 可选前缀 (如 ``"collect"``), 便于日志肉眼归类。

    Returns:
        形如 ``collect-4f9a2c1b8e7d`` 或 ``4f9a2c1b8e7d`` 的字符串。
    """
    token = uuid.uuid4().hex[:12]
    clean_prefix = safe_str(prefix, 32).strip()
    return f"{clean_prefix}-{token}" if clean_prefix else token


def get_trace_id() -> str:
    """获取当前上下文的 trace_id (无则返回 ``"-"``)。"""
    try:
        return _trace_id_var.get()
    except Exception:
        return NO_TRACE


def set_trace_id(trace_id: Optional[str] = None) -> Token:
    """设置当前上下文的 trace_id。

    Args:
        trace_id: 指定值; ``None`` 时自动生成。

    Returns:
        可交给 ``reset_trace_id()`` 的 token。
    """
    value = safe_str(trace_id, 128).strip() if trace_id else new_trace_id()
    return _trace_id_var.set(value or NO_TRACE)


def reset_trace_id(token: Token) -> None:
    """还原 trace_id 上下文 (吞异常, 不影响业务流)。"""
    try:
        _trace_id_var.reset(token)
    except Exception:
        pass


@contextlib.contextmanager
def trace_context(trace_id: Optional[str] = None) -> Iterator[str]:
    """trace_id 作用域上下文管理器。

    Args:
        trace_id: 指定 trace_id; ``None`` 自动生成。

    Yields:
        本作用域内生效的 trace_id。
    """
    token = set_trace_id(trace_id)
    try:
        yield get_trace_id()
    finally:
        reset_trace_id(token)


class TraceIdFilter(logging.Filter):
    """把当前上下文的 ``trace_id`` 注入 LogRecord。

    已显式带 ``trace_id`` 的记录 (通过 ``extra``) 不会被覆盖。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """注入 trace_id; 永远返回 True (不过滤任何记录)。"""
        try:
            existing = getattr(record, TRACE_ID_KEY, None)
            if not existing:
                setattr(record, TRACE_ID_KEY, get_trace_id())
        except Exception:
            try:
                setattr(record, TRACE_ID_KEY, NO_TRACE)
            except Exception:
                pass
        return True


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------

#: LogRecord 内建字段, 不作为业务 extra 输出。
_STD_RECORD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """标准库实现的 JSON 行 formatter (无第三方依赖)。

    输出字段:
        ``ts``(ISO8601 本地时间), ``level``, ``logger``, ``trace_id``, ``msg``,
        ``module``, ``line``, ``pid``; 有异常时附 ``exc``; ``extra`` 字段平铺。

    保证:
        ``format()`` 绝不抛出 — 序列化失败时降级成一行最小 JSON。
    """

    def __init__(self, ensure_ascii: bool = False, include_location: bool = True) -> None:
        """初始化。

        Args:
            ensure_ascii: 是否把非 ASCII 转义 (默认 False, 直出 UTF-8 中文, 更易读;
                写入安全由 ``SafeStreamHandler`` 负责)。
            include_location: 是否包含 module/line/pid 定位字段。
        """
        super().__init__()
        self.ensure_ascii = bool(ensure_ascii)
        self.include_location = bool(include_location)

    def format(self, record: logging.LogRecord) -> str:
        """把 LogRecord 序列化成单行 JSON, 绝不抛出。"""
        try:
            payload = self._build_payload(record)
        except Exception as exc:
            payload = {
                "ts": _iso_now(),
                "level": "ERROR",
                "logger": "core.logging_config",
                TRACE_ID_KEY: get_trace_id(),
                "msg": f"<JsonFormatter build failed: {type(exc).__name__}>",
            }
        try:
            return json.dumps(payload, ensure_ascii=self.ensure_ascii, default=safe_str)
        except Exception:
            try:
                minimal = {
                    "ts": _iso_now(),
                    "level": safe_str(getattr(record, "levelname", "INFO"), 16),
                    "logger": safe_str(getattr(record, "name", "unknown"), 128),
                    TRACE_ID_KEY: get_trace_id(),
                    "msg": safe_str(getattr(record, "msg", ""), 4000),
                }
                return json.dumps(minimal, ensure_ascii=True, default=str)
            except Exception:
                return '{"level":"ERROR","msg":"<json log serialization failed>"}'

    def _build_payload(self, record: logging.LogRecord) -> Dict[str, Any]:
        """构造 JSON 字典。"""
        try:
            message = record.getMessage()
        except Exception:
            message = safe_str(getattr(record, "msg", ""))
        payload: Dict[str, Any] = {
            "ts": _iso_from_record(record),
            "level": safe_str(record.levelname, 16),
            "logger": safe_str(record.name, 200),
            TRACE_ID_KEY: safe_str(getattr(record, TRACE_ID_KEY, None) or get_trace_id(), 128),
            "msg": safe_str(message, 20000),
        }
        if self.include_location:
            payload["module"] = safe_str(getattr(record, "module", ""), 128)
            payload["line"] = int(getattr(record, "lineno", 0) or 0)
            payload["pid"] = int(getattr(record, "process", 0) or 0)
        if record.exc_info:
            try:
                payload["exc"] = safe_str(self.formatException(record.exc_info), 20000)
            except Exception:
                payload["exc"] = "<exception formatting failed>"
        if getattr(record, "stack_info", None):
            payload["stack"] = safe_str(record.stack_info, 8000)
        for key, value in getattr(record, "__dict__", {}).items():
            if key in _STD_RECORD_FIELDS or key in payload or key.startswith("_"):
                continue
            payload[key] = value if isinstance(value, (int, float, bool, type(None))) else safe_str(value, 4000)
        return payload


def _iso_now() -> str:
    """当前时间 ISO8601 字符串 (毫秒精度)。"""
    return _dt.datetime.now().isoformat(timespec="milliseconds")


def _iso_from_record(record: logging.LogRecord) -> str:
    """从 LogRecord.created 生成 ISO8601 时间戳。"""
    try:
        return _dt.datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds")
    except Exception:
        return _iso_now()


def _build_formatter() -> logging.Formatter:
    """构造 formatter: 有 python-json-logger 用它, 否则用内置实现。"""
    if HAS_JSON_LOGGER and _jsonlogger is not None:  # pragma: no cover - 依赖环境
        try:
            fmt = "%(asctime)s %(levelname)s %(name)s %(trace_id)s %(message)s"
            rename = {
                "asctime": "ts",
                "levelname": "level",
                "name": "logger",
                "message": "msg",
            }
            try:
                return _jsonlogger.JsonFormatter(
                    fmt, json_ensure_ascii=False, rename_fields=rename
                )
            except TypeError:
                # 老版本不支持 rename_fields
                return _jsonlogger.JsonFormatter(fmt, json_ensure_ascii=False)
        except Exception:
            pass
    return JsonFormatter()


# ---------------------------------------------------------------------------
# 配置入口
# ---------------------------------------------------------------------------


def configure_logging(
    level: Optional[str] = None,
    config: Optional[Config] = None,
    stream: Any = None,
    log_file: Optional[str] = None,
    force: bool = False,
    install_utf8_streams: bool = True,
) -> logging.Logger:
    """配置根 logger 为 JSON 结构化输出 (幂等)。

    Args:
        level: 日志级别字符串; ``None`` 时取 ``Config.log_level``。
        config: 配置对象; ``None`` 时取全局单例。
        stream: 输出流; ``None`` 时用 ``sys.stdout``。
        log_file: 可选的日志文件路径 (UTF-8, 追加写); 失败仅告警不抛。
        force: 为 True 时先清空已有 handler 再装 (用于测试/重配)。
        install_utf8_streams: 是否顺带执行 ``install_utf8()``。

    Returns:
        配置后的根 logger。
    """
    global _configured, _configured_level

    cfg = config if config is not None else get_config()
    level_name = (level or cfg.log_level or "INFO").upper()
    level_no = logging.getLevelName(level_name)
    if not isinstance(level_no, int):
        level_no = logging.INFO

    if install_utf8_streams:
        install_utf8()

    root = logging.getLogger()

    if _configured and not force:
        root.setLevel(level_no)
        _configured_level = level_no
        return root

    if force:
        for handler in list(root.handlers):
            try:
                root.removeHandler(handler)
                handler.close()
            except Exception:
                pass

    formatter = _build_formatter()
    trace_filter = TraceIdFilter()

    target_stream = stream if stream is not None else getattr(sys, "stdout", None)
    console = SafeStreamHandler(target_stream)
    console.setFormatter(formatter)
    console.addFilter(trace_filter)
    console.setLevel(level_no)
    root.addHandler(console)

    if log_file:
        try:
            directory = os.path.dirname(os.path.abspath(log_file))
            if directory:
                os.makedirs(directory, exist_ok=True)
            file_handler = logging.FileHandler(
                log_file, encoding="utf-8", errors=DEFAULT_ERRORS, delay=True
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(trace_filter)
            file_handler.setLevel(level_no)
            root.addHandler(file_handler)
        except Exception as exc:
            # 文件日志装不上不能拖垮进程, 仅在 console 记一条
            root.warning("file log handler unavailable: %s", safe_str(exc, 500))

    root.setLevel(level_no)
    _configured = True
    _configured_level = level_no
    return root


def is_configured() -> bool:
    """是否已执行过 ``configure_logging()``。"""
    return _configured


class StructuredLogger:
    """结构化 logger 包装 (class diagram: StructuredLogger)。

    与 ``SafeLog`` 的分工:
        * ``StructuredLogger`` 面向"带字段的结构化日志"(``info(msg, k=v)`` → JSON 字段);
        * ``SafeLog`` 面向"绝不崩"的兜底出口 (print/编码安全)。
        * 本类内部同样吞异常, 日志失败不影响业务流。
    """

    __slots__ = ("_logger", "_bound")

    def __init__(self, name: str, bound: Optional[Dict[str, Any]] = None) -> None:
        """初始化。

        Args:
            name: logger 名称。
            bound: 固定附加字段。
        """
        self._logger = logging.getLogger(safe_str(name, 200) or "shaoxiang")
        self._bound: Dict[str, Any] = dict(bound or {})

    @property
    def name(self) -> str:
        """logger 名称。"""
        return self._logger.name

    @property
    def raw(self) -> logging.Logger:
        """底层标准库 logger。"""
        return self._logger

    def bind(self, **fields: Any) -> "StructuredLogger":
        """派生带附加固定字段的新实例。"""
        merged = dict(self._bound)
        merged.update(fields)
        return StructuredLogger(self._logger.name, merged)

    def _emit(self, level: int, msg: Any, exc_info: Any = None, **fields: Any) -> None:
        """内部输出实现, 吞掉所有异常。"""
        extra: Dict[str, Any] = {}
        for source in (self._bound, fields):
            for key, value in source.items():
                if not isinstance(key, str) or not key or key in _STD_RECORD_FIELDS:
                    continue
                extra[key] = value if isinstance(value, (int, float, bool, type(None))) else safe_str(value, 4000)
        extra.setdefault(TRACE_ID_KEY, get_trace_id())
        try:
            self._logger.log(level, safe_str(msg, 20000), exc_info=exc_info, extra=extra)
        except Exception:
            from core.safe_log import safe_write  # 局部导入避免顶层循环

            safe_write(getattr(sys, "stderr", None), f"[log-failed] {safe_str(msg, 2000)}\n")

    def debug(self, msg: Any, **fields: Any) -> None:
        """DEBUG 级结构化日志。"""
        self._emit(logging.DEBUG, msg, **fields)

    def info(self, msg: Any, **fields: Any) -> None:
        """INFO 级结构化日志。"""
        self._emit(logging.INFO, msg, **fields)

    def warning(self, msg: Any, **fields: Any) -> None:
        """WARNING 级结构化日志。"""
        self._emit(logging.WARNING, msg, **fields)

    warn = warning

    def error(self, msg: Any, **fields: Any) -> None:
        """ERROR 级结构化日志。"""
        self._emit(logging.ERROR, msg, **fields)

    def critical(self, msg: Any, **fields: Any) -> None:
        """CRITICAL 级结构化日志。"""
        self._emit(logging.CRITICAL, msg, **fields)

    def exception(self, msg: Any, exc: Optional[BaseException] = None, **fields: Any) -> None:
        """ERROR 级日志 + 堆栈。"""
        if exc is not None:
            exc_info: Any = (type(exc), exc, exc.__traceback__)
        else:
            exc_info = sys.exc_info()
            if exc_info[0] is None:
                exc_info = None
        self._emit(logging.ERROR, msg, exc_info=exc_info, **fields)


_logger_cache: Dict[str, StructuredLogger] = {}


def get_logger(name: str = "shaoxiang", **bound: Any) -> StructuredLogger:
    """获取结构化 logger。

    Args:
        name: logger 名称。
        **bound: 固定附加字段 (给定时不走缓存)。

    Returns:
        ``StructuredLogger`` 实例。
    """
    if bound:
        return StructuredLogger(name, bound)
    cached = _logger_cache.get(name)
    if cached is None:
        cached = StructuredLogger(name)
        _logger_cache[name] = cached
    return cached

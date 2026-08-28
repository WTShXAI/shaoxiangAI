"""统一错误信封 + FastAPI 异常中间件 (REQ-03, 事故③ 根治).

事故背景 (docs/system_design.md §1.1 事故③):
    后端异常返回 ``{error:{code, message}}`` 但 ``message`` 是**对象/嵌套结构** →
    前端 ``setError(obj)`` 塞进 state → React render 抛错 → 白屏。

本模块的**唯一职责**: 保证任何异常最终落到线上的 payload 形如
    ``{"ok": false, "error": {"code": "<str>", "message": "<str>"}}``
且 ``message`` **必定是字符串**, 绝不可能是 dict / list / 自定义对象。

对外 API:
    ErrorDetail / ErrorEnvelope / SuccessEnvelope   schema
    coerce_message(value) -> str                    任意对象 → 安全字符串
    classify_error(exc) -> (code, http_status)       异常分类
    to_envelope(exc) -> ErrorEnvelope               异常 → 信封
    to_error_dict(exc) -> dict                      异常 → 纯 dict (可直接 JSON 化)
    success(data) -> dict                           成功信封
    make_error_middleware()                         FastAPI/Starlette 中间件工厂
    make_error_middleware_class()                   BaseHTTPMiddleware 子类工厂
    register_exception_handlers(app)                 一次性挂全部异常处理器
    is_safe_error_payload(payload) -> bool          自检 (供测试/回归使用)

依赖策略: pydantic / fastapi / starlette 全为**可选**依赖, 缺失时自动降级到 dataclass +
纯 dict, 保证本模块在任何解释器上都能 import (基础设施不许成为新的崩溃源)。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Dict, Optional, Tuple

from core.safe_log import safe_str

__all__ = [
    "MAX_MESSAGE_LEN",
    "CODE_INTERNAL",
    "CODE_SERVICE_UNAVAILABLE",
    "ErrorDetail",
    "ErrorEnvelope",
    "SuccessEnvelope",
    "coerce_message",
    "classify_error",
    "to_envelope",
    "to_error_dict",
    "success",
    "make_error_middleware",
    "make_error_middleware_class",
    "register_exception_handlers",
    "is_safe_error_payload",
    "HAS_PYDANTIC",
]

#: ``message`` 最大长度 (超长截断, 避免异常文本撑爆响应体/前端渲染)。
MAX_MESSAGE_LEN = 2000

CODE_INTERNAL = "INTERNAL"
CODE_SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
CODE_TIMEOUT = "TIMEOUT"
CODE_DB_LOCKED = "DB_LOCKED"
CODE_DB_ERROR = "DB_ERROR"
CODE_NOT_FOUND = "NOT_FOUND"
CODE_FORBIDDEN = "FORBIDDEN"
CODE_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
CODE_VALIDATION = "VALIDATION_ERROR"

_MISSING = object()


# ---------------------------------------------------------------------------
# 消息强制字符串化 — 本模块的核心不变量
# ---------------------------------------------------------------------------


def coerce_message(value: Any, max_len: int = MAX_MESSAGE_LEN) -> str:
    """把任意值强制转成安全字符串, **绝不返回非 str, 绝不抛出**。

    转换规则:
        * ``str``            原样 (仅截断);
        * ``bytes``          UTF-8 解码 (``backslashreplace``);
        * ``dict/list/tuple/set``  JSON 序列化 (``ensure_ascii=False``), 失败则 ``str()``;
        * ``BaseException``  取 ``.message`` / ``args`` / ``str(exc)`` 后递归 coerce;
        * 其他对象           ``str()`` → ``repr()`` → 类型占位符。

    Args:
        value: 任意值 (含"message 是 dict 的对象型异常")。
        max_len: 截断长度; ``<=0`` 不截断。

    Returns:
        非空安全字符串。
    """
    text = _coerce_message_inner(value, depth=0)
    if not isinstance(text, str):  # 双保险: 任何路径都不允许漏出非 str
        text = safe_str(text, 0)
    text = text.strip()
    if not text:
        text = "unspecified error"
    if max_len > 0 and len(text) > max_len:
        text = text[:max_len] + f"...<truncated {len(text) - max_len} chars>"
    return text


def _coerce_message_inner(value: Any, depth: int) -> str:
    """``coerce_message`` 的递归实现 (深度受限, 防自引用死循环)。"""
    if depth > 3:
        return safe_str(value, 0)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8", "backslashreplace")
        except Exception:
            return safe_str(value, 0)
    if isinstance(value, BaseException):
        return _message_from_exception(value, depth)
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        payload: Any = list(value) if isinstance(value, (set, frozenset)) else value
        if isinstance(payload, tuple):
            payload = list(payload)
        try:
            return json.dumps(payload, ensure_ascii=False, default=safe_str)
        except Exception:
            return safe_str(payload, 0)
    if isinstance(value, (int, float, bool)):
        return str(value)
    return safe_str(value, 0)


def _message_from_exception(exc: BaseException, depth: int) -> str:
    """从异常对象中抽取消息文本。

    优先级: 显式 ``.message`` 属性(非可调用) → 单一 ``args[0]`` → 全部 ``args`` →
    ``str(exc)`` → 类型名。三种"对象型 message"(dict / list / 自定义类) 全部走
    ``_coerce_message_inner`` 转字符串。
    """
    raw: Any = _MISSING
    try:
        candidate = getattr(exc, "message", _MISSING)
        if candidate is not _MISSING and not callable(candidate):
            raw = candidate
    except Exception:
        raw = _MISSING

    def _empty(val: Any) -> bool:
        return val is _MISSING or val is None or (isinstance(val, str) and not val.strip())

    if _empty(raw):
        try:
            args = tuple(getattr(exc, "args", ()) or ())
        except Exception:
            args = ()
        if len(args) == 1:
            raw = args[0]
        elif len(args) > 1:
            raw = list(args)
        else:
            raw = _MISSING

    if _empty(raw):
        raw = safe_str(exc, 0)

    text = _coerce_message_inner(raw, depth + 1)
    if not isinstance(text, str):
        text = safe_str(text, 0)
    text = text.strip()
    if not text:
        text = type(exc).__name__
    return text


# ---------------------------------------------------------------------------
# 异常分类
# ---------------------------------------------------------------------------


def classify_error(exc: BaseException) -> Tuple[str, int]:
    """把异常映射为 ``(error_code, http_status)``。

    映射保守优先: 无法确定的一律 ``("INTERNAL", 500)``, 避免误报业务语义。

    Args:
        exc: 异常对象。

    Returns:
        ``(code, http_status)``, 二者均为安全值 (code 为大写字符串)。
    """
    try:
        # 1) 显式 status_code (FastAPI HTTPException / 自定义业务异常)
        status_attr = getattr(exc, "status_code", None)
        explicit_status: Optional[int] = None
        if isinstance(status_attr, int) and 100 <= status_attr <= 599:
            explicit_status = status_attr

        # 2) 显式错误码
        explicit_code: Optional[str] = None
        for attr in ("error_code", "code"):
            candidate = getattr(exc, attr, None)
            if isinstance(candidate, str) and candidate.strip():
                explicit_code = candidate.strip().upper()[:64]
                break

        type_name = type(exc).__name__

        if explicit_code and explicit_status:
            return explicit_code, explicit_status
        if explicit_status:
            return explicit_code or f"HTTP_{explicit_status}", explicit_status
        if explicit_code:
            return explicit_code, 500

        # 3) 熔断器 (pybreaker.CircuitBreakerError 或自建同名类)
        if "circuitbreaker" in type_name.lower().replace("_", ""):
            return CODE_SERVICE_UNAVAILABLE, 503

        # 4) 具体异常类型
        if isinstance(exc, sqlite3.OperationalError):
            message = safe_str(exc, 500).lower()
            if "lock" in message or "busy" in message:
                return CODE_DB_LOCKED, 503
            return CODE_DB_ERROR, 500
        if isinstance(exc, sqlite3.DatabaseError):
            return CODE_DB_ERROR, 500
        if isinstance(exc, TimeoutError):
            return CODE_TIMEOUT, 504
        if isinstance(exc, PermissionError):
            return CODE_FORBIDDEN, 403
        if isinstance(exc, FileNotFoundError):
            return CODE_NOT_FOUND, 404
        if isinstance(exc, NotImplementedError):
            return CODE_NOT_IMPLEMENTED, 501
        if isinstance(exc, (ConnectionError, BrokenPipeError)):
            return CODE_SERVICE_UNAVAILABLE, 503
        if type_name in ("ValidationError", "RequestValidationError"):
            return CODE_VALIDATION, 422
        if type_name in ("CancelledError",):
            return CODE_SERVICE_UNAVAILABLE, 503
    except Exception:
        pass
    return CODE_INTERNAL, 500


# ---------------------------------------------------------------------------
# Schema (pydantic 优先, dataclass 降级)
# ---------------------------------------------------------------------------

HAS_PYDANTIC = False

try:  # pragma: no cover - 取决于运行环境
    from pydantic import BaseModel, Field, field_validator

    HAS_PYDANTIC = True

    class ErrorDetail(BaseModel):  # type: ignore[no-redef]
        """错误明细。``message`` 由 validator 强制字符串化, schema 层兜底。"""

        code: str = Field(default=CODE_INTERNAL, description="机器可读错误码")
        message: str = Field(default="unspecified error", description="人类可读错误信息(强制 str)")

        @field_validator("code", mode="before")
        @classmethod
        def _v_code(cls, value: Any) -> str:
            text = coerce_message(value, 64)
            return text.upper() if text != "unspecified error" else CODE_INTERNAL

        @field_validator("message", mode="before")
        @classmethod
        def _v_message(cls, value: Any) -> str:
            # 关键不变量: 无论传进来什么 (dict/对象/异常), 出去必是 str
            return coerce_message(value)

    class ErrorEnvelope(BaseModel):  # type: ignore[no-redef]
        """错误信封: ``{ok:false, error:{code, message}}``。"""

        ok: bool = Field(default=False, description="固定 false")
        error: Optional[ErrorDetail] = Field(default=None, description="错误明细")

        @field_validator("ok", mode="before")
        @classmethod
        def _v_ok(cls, value: Any) -> bool:
            return False  # 错误信封的 ok 永远 false

    class SuccessEnvelope(BaseModel):  # type: ignore[no-redef]
        """成功信封: ``{ok:true, data:...}``。"""

        ok: bool = Field(default=True, description="固定 true")
        data: Any = Field(default=None, description="业务数据")

        @field_validator("ok", mode="before")
        @classmethod
        def _v_ok(cls, value: Any) -> bool:
            return True

except Exception:  # pragma: no cover - 无 pydantic 时的降级路径
    HAS_PYDANTIC = False
    from dataclasses import dataclass, field

    @dataclass
    class ErrorDetail:  # type: ignore[no-redef]
        """错误明细 (dataclass 降级实现, ``__post_init__`` 强制 str)。"""

        code: str = CODE_INTERNAL
        message: str = "unspecified error"

        def __post_init__(self) -> None:
            """强制字段字符串化。"""
            code_text = coerce_message(self.code, 64)
            self.code = code_text.upper() if code_text != "unspecified error" else CODE_INTERNAL
            self.message = coerce_message(self.message)

        def model_dump(self) -> Dict[str, Any]:
            """返回 dict (pydantic API 兼容)。"""
            return {"code": self.code, "message": self.message}

    @dataclass
    class ErrorEnvelope:  # type: ignore[no-redef]
        """错误信封 (dataclass 降级实现)。"""

        ok: bool = False
        error: Optional[ErrorDetail] = None

        def __post_init__(self) -> None:
            """``ok`` 恒为 False; ``error`` 为 dict 时自动转 ErrorDetail。"""
            self.ok = False
            if isinstance(self.error, dict):
                self.error = ErrorDetail(
                    code=self.error.get("code", CODE_INTERNAL),
                    message=self.error.get("message", "unspecified error"),
                )

        def model_dump(self) -> Dict[str, Any]:
            """返回 dict (pydantic API 兼容)。"""
            return {
                "ok": False,
                "error": self.error.model_dump() if self.error is not None else None,
            }

    @dataclass
    class SuccessEnvelope:  # type: ignore[no-redef]
        """成功信封 (dataclass 降级实现)。"""

        ok: bool = True
        data: Any = field(default=None)

        def __post_init__(self) -> None:
            """``ok`` 恒为 True。"""
            self.ok = True

        def model_dump(self) -> Dict[str, Any]:
            """返回 dict (pydantic API 兼容)。"""
            return {"ok": True, "data": self.data}


# ---------------------------------------------------------------------------
# 信封构造
# ---------------------------------------------------------------------------


def to_envelope(exc: Any, code: Optional[str] = None) -> ErrorEnvelope:
    """把任意异常/对象转成 ``ErrorEnvelope``, **绝不抛出**。

    Args:
        exc: 异常对象, 或任意可作为错误信息的值 (dict/str/自定义对象)。
        code: 显式错误码; ``None`` 时由 ``classify_error`` 推断。

    Returns:
        ``ErrorEnvelope``, 其 ``error.message`` 保证是 ``str``。
    """
    try:
        if isinstance(exc, BaseException):
            inferred_code, _status = classify_error(exc)
        else:
            inferred_code = CODE_INTERNAL
        final_code = coerce_message(code, 64).upper() if code else inferred_code
        message = coerce_message(exc)
        return ErrorEnvelope(error=ErrorDetail(code=final_code, message=message))
    except Exception as inner:  # 连信封都构造失败时的最后兜底
        try:
            return ErrorEnvelope(
                error=ErrorDetail(
                    code=CODE_INTERNAL,
                    message=f"error envelope build failed: {type(inner).__name__}",
                )
            )
        except Exception:
            # ErrorDetail 本身不可用属于不可能分支, 仍不允许抛出
            return ErrorEnvelope()  # type: ignore[call-arg]


def to_error_dict(exc: Any, code: Optional[str] = None) -> Dict[str, Any]:
    """异常 → 可直接 JSON 化的 dict ``{"ok":false,"error":{"code","message"}}``。

    Args:
        exc: 异常对象或任意值。
        code: 显式错误码。

    Returns:
        纯 dict, ``error.message`` 保证是 str。
    """
    envelope = to_envelope(exc, code=code)
    try:
        dumped = envelope.model_dump()
        if isinstance(dumped, dict):
            error = dumped.get("error")
            if not isinstance(error, dict):
                error = {"code": CODE_INTERNAL, "message": coerce_message(exc)}
            error["code"] = coerce_message(error.get("code"), 64).upper()
            error["message"] = coerce_message(error.get("message"))
            return {"ok": False, "error": error}
    except Exception:
        pass
    return {"ok": False, "error": {"code": CODE_INTERNAL, "message": coerce_message(exc)}}


def success(data: Any = None) -> Dict[str, Any]:
    """构造成功信封 dict ``{"ok":true,"data":...}``。

    Args:
        data: 业务数据 (不做强制转换, 由各 handler 的 response_model 约束)。

    Returns:
        成功信封 dict。
    """
    return {"ok": True, "data": data}


def is_safe_error_payload(payload: Any) -> bool:
    """自检: payload 是否符合"前端永不白屏"契约。

    条件: 是 dict、``ok is False``、``error`` 是 dict 且 ``code``/``message`` 均为
    **非空字符串**。

    Args:
        payload: 待检查对象。

    Returns:
        是否合规。
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("ok") is not False:
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    message = error.get("message")
    if not isinstance(code, str) or not code.strip():
        return False
    if not isinstance(message, str) or not message.strip():
        return False
    return True


# ---------------------------------------------------------------------------
# FastAPI / Starlette 集成 (全部懒导入, 无框架也能 import 本模块)
# ---------------------------------------------------------------------------


def _json_response(payload: Dict[str, Any], status: int) -> Any:
    """构造 JSONResponse; starlette 不可用时抛 RuntimeError (仅在集成路径调用)。"""
    try:
        from starlette.responses import JSONResponse
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "starlette/fastapi is required for HTTP error middleware; "
            f"import failed: {safe_str(exc, 200)}"
        ) from exc
    return JSONResponse(status_code=status, content=payload)


def make_error_middleware(
    logger: Optional[Any] = None,
    code_mapper: Optional[Callable[[BaseException], Tuple[str, int]]] = None,
    expose_trace_id: bool = True,
) -> Callable[..., Any]:
    """FastAPI 中间件工厂 (REQ-03)。

    用法::

        from core.error_envelope import make_error_middleware
        app.middleware("http")(make_error_middleware())

    保证: 任何未处理异常都被转成 ``{"ok":false,"error":{"code":str,"message":str}}``,
    ``message`` 绝不可能是对象; 中间件自身出错也返回合法信封而非 500 空响应。

    Args:
        logger: 具备 ``.error(msg, **kw)`` 的 logger (如 ``core.logging_config.get_logger()``)。
        code_mapper: 自定义 ``exc -> (code, status)`` 映射; 默认 ``classify_error``。
        expose_trace_id: 是否在响应头 ``X-Trace-Id`` 回带 trace_id, 便于事故定位。

    Returns:
        ``async def middleware(request, call_next)`` 可调用对象。
    """
    mapper = code_mapper or classify_error

    async def error_envelope_middleware(request: Any, call_next: Callable[..., Any]) -> Any:
        """捕获下游所有异常并返回统一错误信封。"""
        try:
            return await call_next(request)
        except Exception as exc:
            try:
                code, status = mapper(exc)
            except Exception:
                code, status = CODE_INTERNAL, 500
            payload = to_error_dict(exc, code=code)
            if logger is not None:
                try:
                    logger.error(
                        "unhandled exception -> error envelope",
                        error_code=code,
                        http_status=status,
                        path=safe_str(getattr(getattr(request, "url", None), "path", ""), 500),
                        detail=payload["error"]["message"],
                    )
                except Exception:
                    pass
            response = _json_response(payload, status)
            if expose_trace_id:
                try:
                    from core.logging_config import get_trace_id

                    response.headers["X-Trace-Id"] = get_trace_id()
                except Exception:
                    pass
            return response

    return error_envelope_middleware


def make_error_middleware_class(
    logger: Optional[Any] = None,
    code_mapper: Optional[Callable[[BaseException], Tuple[str, int]]] = None,
) -> Any:
    """返回 ``BaseHTTPMiddleware`` 子类 (供 ``app.add_middleware(cls)`` 使用)。

    Args:
        logger: 结构化 logger。
        code_mapper: 自定义异常映射。

    Returns:
        ``ErrorEnvelopeMiddleware`` 类。

    Raises:
        RuntimeError: starlette 不可用。
    """
    try:
        from starlette.middleware.base import BaseHTTPMiddleware
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            f"starlette is required for make_error_middleware_class: {safe_str(exc, 200)}"
        ) from exc

    dispatch_impl = make_error_middleware(logger=logger, code_mapper=code_mapper)

    class ErrorEnvelopeMiddleware(BaseHTTPMiddleware):  # type: ignore[misc]
        """统一错误信封中间件 (类形式)。"""

        async def dispatch(self, request: Any, call_next: Callable[..., Any]) -> Any:
            """委托给 ``make_error_middleware()`` 生成的实现。"""
            return await dispatch_impl(request, call_next)

    return ErrorEnvelopeMiddleware


def register_exception_handlers(app: Any, logger: Optional[Any] = None) -> None:
    """给 FastAPI app 一次性挂上全部异常处理器 (REQ-03)。

    覆盖: ``HTTPException`` / ``RequestValidationError`` / 兜底 ``Exception``。
    三者响应体统一为 ``{"ok":false,"error":{"code":str,"message":str}}``。

    Args:
        app: FastAPI 实例。
        logger: 结构化 logger (可选)。
    """

    async def _handle_generic(request: Any, exc: Exception) -> Any:
        code, status = classify_error(exc)
        payload = to_error_dict(exc, code=code)
        if logger is not None:
            try:
                logger.error(
                    "exception handler -> error envelope",
                    error_code=code,
                    http_status=status,
                    detail=payload["error"]["message"],
                )
            except Exception:
                pass
        return _json_response(payload, status)

    try:
        app.add_exception_handler(Exception, _handle_generic)
    except Exception:
        pass

    try:
        from starlette.exceptions import HTTPException as StarletteHTTPException

        async def _handle_http(request: Any, exc: Any) -> Any:
            status = int(getattr(exc, "status_code", 500) or 500)
            detail = getattr(exc, "detail", None)
            payload = to_error_dict(detail if detail is not None else exc, code=f"HTTP_{status}")
            return _json_response(payload, status)

        app.add_exception_handler(StarletteHTTPException, _handle_http)
    except Exception:
        pass

    try:
        from fastapi.exceptions import RequestValidationError

        async def _handle_validation(request: Any, exc: Any) -> Any:
            # exc.errors() 是 list[dict] —— 典型"对象型 message"来源, 必须 JSON 转字符串
            try:
                detail: Any = exc.errors()
            except Exception:
                detail = exc
            payload = to_error_dict(detail, code=CODE_VALIDATION)
            return _json_response(payload, 422)

        app.add_exception_handler(RequestValidationError, _handle_validation)
    except Exception:
        pass

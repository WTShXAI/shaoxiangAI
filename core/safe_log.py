"""UTF-8 安全日志适配层 (REQ-05, 事故④ 根治).

事故背景 (docs/system_design.md §1.1 事故④):
    Windows 默认 stdout 编码为 GBK/cp936; 采集器 ``print`` / ``log`` 含中文或 emoji 时
    抛 ``UnicodeEncodeError``, 未捕获 → 整轮采集崩溃 → 提前 ``return`` 跳过 live 翻页 →
    ``scheduled`` 永不翻 live → 前端"刚开赛不显示"。

本模块提供的护栏:
    1. ``install_utf8()``  — 入口调用一次, 把 stdout/stderr reconfigure 成
       ``encoding='utf-8', errors='backslashreplace'``。**只 reconfigure, 不强改环境变量**
       (进程内改 ``PYTHONUTF8`` 对已启动解释器无效, 只输出提示由运维在启动脚本设置)。
    2. ``SafeLog``        — 所有 print/log 的唯一出口。写入前按目标流编码做
       ``backslashreplace`` 转义; 任何阶段异常都被吞掉并逐级降级, **绝不抛出**。
    3. ``SafeStreamHandler`` — logging 用的 handler, emit 路径同样做转义 + 不抛。

铁律: 本模块任何公开函数都不允许向调用方抛出异常 (含 UnicodeEncodeError / OSError)。
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from typing import Any, Dict, IO, Optional

__all__ = [
    "DEFAULT_ERRORS",
    "install_utf8",
    "utf8_env_hint",
    "safe_str",
    "escape_for_encoding",
    "sanitize_for_stream",
    "safe_write",
    "safe_print",
    "SafeLog",
    "SafeStreamHandler",
    "safe_log",
    "get_safe_log",
]

#: 统一的编码错误处理策略: 非 ASCII / 不可编码字符转义为 ``\\uXXXX`` 而非抛错。
DEFAULT_ERRORS = "backslashreplace"

#: ``safe_str`` 默认截断长度 (防止超长对象 repr 撑爆日志/信封)。
DEFAULT_MAX_LEN = 8000

_INSTALL_STATE: Dict[str, str] = {}


# ---------------------------------------------------------------------------
# 编码安装 / 提示
# ---------------------------------------------------------------------------


def utf8_env_hint() -> str:
    """返回建议在**启动脚本**中设置的环境变量提示文本。

    进程内修改 ``PYTHONUTF8`` / ``PYTHONIOENCODING`` 对当前解释器无效, 因此本模块
    只做 ``reconfigure`` + 输出提示, 由运维在 bat/sh 启动脚本里设置。

    Returns:
        人类可读的提示字符串。
    """
    current_utf8 = os.environ.get("PYTHONUTF8", "<unset>")
    current_ioenc = os.environ.get("PYTHONIOENCODING", "<unset>")
    return (
        "[safe_log] hint: set PYTHONUTF8=1 and PYTHONIOENCODING=utf-8 in the launcher "
        f"script (current PYTHONUTF8={current_utf8}, PYTHONIOENCODING={current_ioenc}); "
        "in-process env mutation does not affect an already-started interpreter."
    )


def install_utf8(emit_hint: bool = False) -> Dict[str, str]:
    """把 ``sys.stdout`` / ``sys.stderr`` reconfigure 为 UTF-8 + backslashreplace。

    幂等: 重复调用安全。任何流不支持 ``reconfigure``(如被替换成 StringIO) 时静默跳过。

    Args:
        emit_hint: 是否额外打印 ``PYTHONUTF8=1`` 环境变量提示。

    Returns:
        每个流的处理结果, 形如 ``{"stdout": "utf-8", "stderr": "skipped:no-reconfigure"}``。
    """
    result: Dict[str, str] = {}
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            result[name] = "skipped:none"
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            result[name] = "skipped:no-reconfigure"
            continue
        try:
            reconfigure(encoding="utf-8", errors=DEFAULT_ERRORS)
            result[name] = str(getattr(stream, "encoding", "utf-8") or "utf-8")
        except Exception as exc:  # 绝不因为装护栏本身而崩
            result[name] = f"failed:{type(exc).__name__}"
    _INSTALL_STATE.update(result)
    if emit_hint:
        safe_write(getattr(sys, "stderr", None), utf8_env_hint() + "\n")
    return result


# ---------------------------------------------------------------------------
# 字符串安全化
# ---------------------------------------------------------------------------


def safe_str(obj: Any, max_len: int = DEFAULT_MAX_LEN) -> str:
    """把任意对象安全转成字符串, **绝不抛出**。

    转换顺序: 原样(str) → ``str(obj)`` → ``repr(obj)`` → 类型占位符。

    Args:
        obj: 任意对象 (可能是异常、dict、自定义类, 甚至 ``__str__`` 会抛错的对象)。
        max_len: 最大长度, 超出则截断并追加省略标记; ``<=0`` 表示不截断。

    Returns:
        安全字符串。
    """
    text: Optional[str] = None
    if isinstance(obj, str):
        text = obj
    else:
        try:
            text = str(obj)
        except Exception:
            try:
                text = repr(obj)
            except Exception:
                try:
                    text = f"<unprintable {type(obj).__name__}>"
                except Exception:
                    text = "<unprintable object>"
    if text is None:
        text = ""
    if max_len > 0 and len(text) > max_len:
        text = text[:max_len] + f"...<truncated {len(text) - max_len} chars>"
    return text


#: 超过该长度时放弃逐字符转义, 走批量 ``backslashreplace`` (性能兜底)。
_PER_CHAR_ESCAPE_LIMIT = 65536


def escape_for_encoding(text: str, encoding: Optional[str]) -> str:
    """按目标编码能力对文本做 ``backslashreplace`` 风格转义。

    可编码的字符原样保留 (UTF-8 下中文照常输出); 不可编码的字符 (如 GBK/ASCII 下的
    emoji) 转义为 ``\\uXXXX`` 字面量。

    ⚠ 与标准库 ``errors='backslashreplace'`` 的**一处刻意差异**:
        标准库对 BMP 外字符输出 ``\\U0001f600``(大写 U + 8 位十六进制), 而 JSON 只认
        ``\\uXXXX``(小写 u + 4 位)。若日志本身是 JSON 行, 标准库写法会让转义后的日志
        **不再可被 json.loads 解析**。因此这里对 BMP 外字符输出 UTF-16 代理对
        (``\\ud83d\\ude00``), 与 ``json.dumps(ensure_ascii=True)`` 一致 ——
        既保证写得出去, 又保证 JSON 日志仍可解析 (REQ-10 可检索性)。

    Args:
        text: 原始文本。
        encoding: 目标编码; ``None``/未知时按 ``utf-8`` 处理。

    Returns:
        对该编码而言 100% 可写出的文本。
    """
    enc = (encoding or "utf-8").strip() or "utf-8"
    try:
        text.encode(enc)
        return text  # 快路径: 全部可编码, 无需转义
    except UnicodeEncodeError:
        pass
    except (LookupError, TypeError, ValueError):
        enc = "utf-8"  # 未知编码名 → 退回 utf-8 再试
        try:
            text.encode(enc)
            return text
        except UnicodeEncodeError:
            pass
        except Exception:
            return text
    except Exception:
        return text

    # 慢路径: 逐字符判定, 只转义真正写不出去的字符
    try:
        if len(text) <= _PER_CHAR_ESCAPE_LIMIT:
            pieces = []
            for char in text:
                try:
                    char.encode(enc)
                    pieces.append(char)
                except (UnicodeEncodeError, UnicodeError):
                    pieces.append(_escape_char(char))
                except Exception:
                    pieces.append(_escape_char(char))
            return "".join(pieces)
    except Exception:
        pass

    # 性能/异常兜底: 批量 backslashreplace
    try:
        return text.encode(enc, DEFAULT_ERRORS).decode(enc, "replace")
    except Exception:
        try:
            return text.encode("ascii", DEFAULT_ERRORS).decode("ascii", "replace")
        except Exception:
            return "<unencodable text>"


def _escape_char(char: str) -> str:
    """把单个字符转成 JSON 兼容的 ``\\uXXXX`` 转义 (BMP 外用代理对)。"""
    try:
        code_point = ord(char)
    except Exception:
        return "\\ufffd"
    if code_point > 0xFFFF:
        offset = code_point - 0x10000
        high = 0xD800 + (offset >> 10)
        low = 0xDC00 + (offset & 0x3FF)
        return f"\\u{high:04x}\\u{low:04x}"
    return f"\\u{code_point:04x}"


def sanitize_for_stream(text: Any, stream: Optional[IO[str]]) -> str:
    """把任意对象转成"对该流一定写得出去"的字符串。

    Args:
        text: 任意对象。
        stream: 目标文本流 (读取其 ``encoding`` 属性)。

    Returns:
        安全字符串。
    """
    raw = safe_str(text)
    encoding = None
    if stream is not None:
        try:
            encoding = getattr(stream, "encoding", None)
        except Exception:
            encoding = None
    return escape_for_encoding(raw, encoding)


def safe_write(stream: Optional[IO[str]], text: Any) -> bool:
    """向流写入文本, 全链路吞异常。

    降级链: 目标流 → ``sys.__stderr__``(ASCII 转义) → 放弃。

    Args:
        stream: 目标流; ``None`` 时直接走降级链。
        text: 任意对象。

    Returns:
        是否成功写出 (含降级写出算成功)。
    """
    payload = sanitize_for_stream(text, stream)
    if stream is not None:
        try:
            stream.write(payload)
            try:
                stream.flush()
            except Exception:
                pass
            return True
        except Exception:
            pass
    fallback = getattr(sys, "__stderr__", None)
    if fallback is not None and fallback is not stream:
        try:
            ascii_payload = safe_str(text).encode("ascii", DEFAULT_ERRORS).decode("ascii")
            fallback.write(ascii_payload)
            try:
                fallback.flush()
            except Exception:
                pass
            return True
        except Exception:
            pass
    return False  # 彻底写不出去也不抛, 静默丢弃


def safe_print(
    *args: Any,
    sep: str = " ",
    end: str = "\n",
    file: Optional[IO[str]] = None,
    flush: bool = True,
) -> None:
    """``print`` 的安全替代品: 签名兼容, 但**绝不抛 UnicodeEncodeError**。

    Args:
        *args: 待打印对象。
        sep: 分隔符。
        end: 结尾符。
        file: 目标流, 默认 ``sys.stdout``。
        flush: 是否立即 flush (默认 True, 便于崩溃现场保留日志)。
    """
    stream = file if file is not None else getattr(sys, "stdout", None)
    try:
        body = sep.join(safe_str(a) for a in args)
    except Exception:
        body = "<safe_print: failed to join args>"
    safe_write(stream, body + end)
    if flush and stream is not None:
        try:
            stream.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# logging handler
# ---------------------------------------------------------------------------


class SafeStreamHandler(logging.StreamHandler):
    """StreamHandler 的安全版本: format/write 全链路转义 + 不抛。

    与标准 ``StreamHandler`` 的差异:
        * emit 前把最终文本按目标流编码 ``backslashreplace`` 转义;
        * 任何异常都被吞掉 (降级到 ``sys.__stderr__`` 一行提示), 不调用
          ``handleError`` 打印堆栈污染 stdout, 也绝不向上冒泡。
    """

    def emit(self, record: logging.LogRecord) -> None:
        """输出一条日志记录, 绝不抛出。"""
        try:
            message = self.format(record)
        except Exception as exc:
            message = f"<SafeStreamHandler: format failed: {type(exc).__name__}: {exc}>"
        try:
            stream = self.stream
        except Exception:
            stream = None
        terminator = getattr(self, "terminator", "\n") or "\n"
        safe_write(stream, message + terminator)


# ---------------------------------------------------------------------------
# SafeLog
# ---------------------------------------------------------------------------


class SafeLog:
    """所有 print/log 的统一安全出口 (class diagram: SafeLog)。

    行为保证:
        * 任何方法都不抛异常 (包括 ``UnicodeEncodeError`` / 底层流已关闭 / logger 配置异常);
        * 消息与 kwargs 全部经 ``safe_str`` 字符串化后再交给 logging;
        * ``echo=True`` 时额外把消息安全写到 stdout, 便于无 handler 场景仍可见。

    Example:
        >>> log = SafeLog("gq.auto_collector")
        >>> log.info("翻页完成 ✅ 场次=%s", extra_count=12)   # 不会抛错
    """

    __slots__ = ("_name", "_logger", "_echo", "_extra")

    def __init__(
        self,
        name: str = "shaoxiang",
        logger: Optional[logging.Logger] = None,
        echo: bool = False,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """初始化。

        Args:
            name: logger 名称。
            logger: 直接注入的 logger; 默认 ``logging.getLogger(name)``。
            echo: 是否同时安全打印到 stdout。
            extra: 每条日志附带的固定字段 (如 ``{"trace_id": ...}``)。
        """
        self._name = safe_str(name, 200) or "shaoxiang"
        try:
            self._logger = logger if logger is not None else logging.getLogger(self._name)
        except Exception:
            self._logger = None  # type: ignore[assignment]
        self._echo = bool(echo)
        self._extra: Dict[str, Any] = dict(extra or {})

    # -- 属性 -----------------------------------------------------------
    @property
    def name(self) -> str:
        """logger 名称。"""
        return self._name

    @property
    def logger(self) -> Optional[logging.Logger]:
        """底层 logger (可能为 None)。"""
        return self._logger

    def bind(self, **fields: Any) -> "SafeLog":
        """派生一个附带额外固定字段的新 SafeLog (不修改自身)。

        Args:
            **fields: 附加字段, 如 ``trace_id="abc"``。

        Returns:
            新的 ``SafeLog`` 实例。
        """
        merged = dict(self._extra)
        merged.update(fields)
        return SafeLog(self._name, logger=self._logger, echo=self._echo, extra=merged)

    # -- 内部实现 -------------------------------------------------------
    def _merge_extra(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """合并固定字段与本次调用字段, 并全部字符串安全化。"""
        merged: Dict[str, Any] = {}
        for source in (self._extra, fields):
            for key, value in source.items():
                if not isinstance(key, str) or not key:
                    continue
                # 避免与 LogRecord 内建属性冲突导致 logging 抛 KeyError
                if key in _RESERVED_RECORD_KEYS:
                    key = f"x_{key}"
                merged[key] = value if isinstance(value, (int, float, bool, type(None))) else safe_str(value, 2000)
        return merged

    def _emit(self, level: int, msg: Any, exc_info: Any = None, **fields: Any) -> None:
        """核心输出实现, 吞掉所有异常。"""
        text = safe_str(msg)
        extra = self._merge_extra(fields)
        logger = self._logger
        delivered = False
        if logger is not None:
            try:
                logger.log(level, text, exc_info=exc_info, extra=extra or None)
                delivered = True
            except Exception:
                delivered = False
        if self._echo or not delivered:
            prefix = f"[{logging.getLevelName(level)}] [{self._name}] "
            suffix = ""
            if extra:
                try:
                    suffix = " " + " ".join(f"{k}={safe_str(v, 500)}" for k, v in extra.items())
                except Exception:
                    suffix = ""
            stream = getattr(sys, "stderr" if level >= logging.WARNING else "stdout", None)
            safe_write(stream, prefix + text + suffix + "\n")
            if exc_info:
                try:
                    tb = "".join(traceback.format_exception(*_normalize_exc_info(exc_info)))
                except Exception:
                    tb = ""
                if tb:
                    safe_write(stream, tb)

    # -- 公开 API (class diagram) ---------------------------------------
    def log(self, msg: Any, level: int = logging.INFO, **fields: Any) -> None:
        """以指定级别输出日志 (默认 INFO)。"""
        self._emit(level, msg, **fields)

    def debug(self, msg: Any, **fields: Any) -> None:
        """DEBUG 级日志。"""
        self._emit(logging.DEBUG, msg, **fields)

    def info(self, msg: Any, **fields: Any) -> None:
        """INFO 级日志。"""
        self._emit(logging.INFO, msg, **fields)

    def warning(self, msg: Any, **fields: Any) -> None:
        """WARNING 级日志。"""
        self._emit(logging.WARNING, msg, **fields)

    #: ``warning`` 的短别名 (兼容既有代码习惯)。
    warn = warning

    def error(self, msg: Any, **fields: Any) -> None:
        """ERROR 级日志。"""
        self._emit(logging.ERROR, msg, **fields)

    def critical(self, msg: Any, **fields: Any) -> None:
        """CRITICAL 级日志。"""
        self._emit(logging.CRITICAL, msg, **fields)

    def exception(self, msg: Any, exc: Optional[BaseException] = None, **fields: Any) -> None:
        """ERROR 级日志 + 堆栈 (在 except 块内调用)。

        Args:
            msg: 日志消息。
            exc: 显式异常对象; 为 None 时用当前 ``sys.exc_info()``。
            **fields: 附加字段。
        """
        exc_info: Any
        if exc is not None:
            exc_info = (type(exc), exc, exc.__traceback__)
        else:
            exc_info = sys.exc_info()
            if exc_info[0] is None:
                exc_info = None
        self._emit(logging.ERROR, msg, exc_info=exc_info, **fields)

    def print(self, *args: Any, **kwargs: Any) -> None:
        """``print`` 的安全替代 (直接走 stdout, 不经 logging)。"""
        safe_print(*args, **kwargs)


#: LogRecord 内建属性名, 作为 extra key 会让 logging 抛 KeyError, 需重命名规避。
_RESERVED_RECORD_KEYS = frozenset(
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


def _normalize_exc_info(exc_info: Any) -> tuple:
    """把各种 exc_info 表达形式规范成 3 元组, 失败时返回当前异常信息。"""
    if isinstance(exc_info, tuple) and len(exc_info) == 3:
        return exc_info
    if isinstance(exc_info, BaseException):
        return (type(exc_info), exc_info, exc_info.__traceback__)
    current = sys.exc_info()
    if current[0] is not None:
        return current
    return (type(None), None, None)


#: 进程级默认实例, 供 ``from core.safe_log import safe_log`` 直接使用。
safe_log = SafeLog("shaoxiang")

_named_cache: Dict[str, SafeLog] = {}


def get_safe_log(name: str = "shaoxiang", echo: bool = False) -> SafeLog:
    """按名字获取缓存的 ``SafeLog`` 实例。

    Args:
        name: logger 名称。
        echo: 是否同时打印到 stdout。

    Returns:
        ``SafeLog`` 实例 (同名同 echo 复用)。
    """
    key = f"{name}|{int(bool(echo))}"
    instance = _named_cache.get(key)
    if instance is None:
        instance = SafeLog(name, echo=echo)
        _named_cache[key] = instance
    return instance

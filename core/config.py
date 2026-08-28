"""中央配置模块 (T01, 单一配置源).

设计约束 (来自 docs/system_design.md §7 共享知识):
    唯一配置来源 = 本模块。禁止散落常量; 新增开关/阈值一律走 Config 字段。

实现要点:
    * 首选 ``pydantic-settings``(BaseSettings); 若运行时缺该依赖, 自动降级到纯标准库
      实现, **对外 API 完全一致** (字段名/类型/默认值/``load()``/``model_dump()``)。
    * 全部字段可由环境变量覆盖, 变量名 = 字段名大写 (如 ``GQ_DB_PATH``)。
    * ``Config.load()`` 返回进程内单例 (线程安全), ``reload()`` 用于测试/热更。

字段契约 (system_design.md §3.2(b)):
    gq_db_path          str            events.db 路径
    log_level           str   INFO     日志级别
    redis_url           str|None None  异步队列(ARQ); None = 进程内兜底
    queue_enabled       bool  False    是否启用异步队列 (REQ-11)
    roi_delta_threshold float 5.0      双源 ROI 偏差告警阈值(pp), TBC-3
    busy_timeout_ms     int   30000    sqlite busy_timeout
    wal_checkpoint_min  int   15       WAL checkpoint 周期(分钟)
    health_port         int   9001     独立 health server 端口 (REQ-01)
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = [
    "PROJECT_ROOT",
    "DEFAULT_GQ_DB_PATH",
    "Config",
    "get_config",
    "reload_config",
    "HAS_PYDANTIC_SETTINGS",
]

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

#: 项目根目录 = 本文件的上一级 (D:\Architecture)
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

#: 完整赛事库 events.db 默认路径(表结构与 events.db 完全对齐, events.db 仅留作历史归档)。
DEFAULT_GQ_DB_PATH: str = str(PROJECT_ROOT / "data" / "events.db")

_TRUE_TOKENS = frozenset({"1", "true", "yes", "y", "on", "t"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "n", "off", "f", ""})

_VALID_LOG_LEVELS = frozenset(
    {"CRITICAL", "FATAL", "ERROR", "WARN", "WARNING", "INFO", "DEBUG", "NOTSET"}
)

# 字段声明: name -> (type, default)。降级实现与校验逻辑共用这一份声明。
_FIELD_SPEC: Dict[str, Any] = {
    "gq_db_path": (str, DEFAULT_GQ_DB_PATH),
    "log_level": (str, "INFO"),
    "redis_url": (Optional[str], None),
    "queue_enabled": (bool, False),
    "roi_delta_threshold": (float, 5.0),
    "busy_timeout_ms": (int, 30000),
    "wal_checkpoint_min": (int, 15),
    "health_port": (int, 9001),
}


def _coerce_bool(raw: Any, default: bool = False) -> bool:
    """把任意输入安全转 bool, 无法识别时返回 ``default``。"""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    token = str(raw).strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    return default


def _coerce_int(raw: Any, default: int) -> int:
    """把任意输入安全转 int, 失败时返回 ``default``。"""
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _coerce_float(raw: Any, default: float) -> float:
    """把任意输入安全转 float, 失败时返回 ``default``。"""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _coerce_optional_str(raw: Any, default: Optional[str] = None) -> Optional[str]:
    """空字符串视为未设置 (None), 便于 ``REDIS_URL=`` 表达"关闭"。"""
    if raw is None:
        return default
    text = str(raw).strip()
    return text or None


def _normalize_log_level(raw: Any, default: str = "INFO") -> str:
    """规范化日志级别为大写字符串, 非法值回落到 ``default``。"""
    level = str(raw or "").strip().upper()
    if level in _VALID_LOG_LEVELS:
        return "WARNING" if level == "WARN" else ("CRITICAL" if level == "FATAL" else level)
    return default


def _normalize_port(raw: Any, default: int) -> int:
    """端口范围校验 (1..65535), 越界回落默认值。"""
    port = _coerce_int(raw, default)
    if 1 <= port <= 65535:
        return port
    return default


def _env(name: str) -> Optional[str]:
    """读取环境变量 (大写优先, 兼容小写写法)。"""
    value = os.environ.get(name.upper())
    if value is None:
        value = os.environ.get(name.lower())
    return value


# ---------------------------------------------------------------------------
# 首选实现: pydantic-settings
# ---------------------------------------------------------------------------

HAS_PYDANTIC_SETTINGS = False

try:  # pragma: no cover - 依赖是否存在取决于运行环境
    from pydantic import Field, field_validator
    from pydantic_settings import BaseSettings, SettingsConfigDict

    HAS_PYDANTIC_SETTINGS = True

    class Config(BaseSettings):  # type: ignore[no-redef]
        """中央配置 (pydantic-settings 实现)。

        环境变量名 = 字段名大写, 大小写不敏感。未知环境变量一律忽略 (现网 ``.env``
        含大量业务键, 不能让配置层因为"多了键"而启动失败)。
        """

        model_config = SettingsConfigDict(
            env_prefix="",
            case_sensitive=False,
            extra="ignore",
            validate_default=True,
        )

        gq_db_path: str = Field(default=DEFAULT_GQ_DB_PATH, description="events.db 路径")
        log_level: str = Field(default="INFO", description="日志级别")
        redis_url: Optional[str] = Field(default=None, description="ARQ Redis URL; None=进程内兜底")
        queue_enabled: bool = Field(default=False, description="是否启用异步队列 (REQ-11)")
        roi_delta_threshold: float = Field(default=5.0, description="双源 ROI 偏差阈值(pp), TBC-3")
        busy_timeout_ms: int = Field(default=30000, description="sqlite busy_timeout(ms)")
        wal_checkpoint_min: int = Field(default=15, description="WAL checkpoint 周期(分钟)")
        health_port: int = Field(default=9001, description="独立 health server 端口")

        @field_validator("log_level", mode="before")
        @classmethod
        def _v_log_level(cls, value: Any) -> str:
            return _normalize_log_level(value, "INFO")

        @field_validator("redis_url", mode="before")
        @classmethod
        def _v_redis_url(cls, value: Any) -> Optional[str]:
            return _coerce_optional_str(value, None)

        @field_validator("gq_db_path", mode="before")
        @classmethod
        def _v_gq_db_path(cls, value: Any) -> str:
            text = _coerce_optional_str(value, None)
            return text or DEFAULT_GQ_DB_PATH

        @field_validator("busy_timeout_ms", mode="before")
        @classmethod
        def _v_busy_timeout(cls, value: Any) -> int:
            timeout = _coerce_int(value, 30000)
            return timeout if timeout >= 0 else 30000

        @field_validator("wal_checkpoint_min", mode="before")
        @classmethod
        def _v_checkpoint_min(cls, value: Any) -> int:
            minutes = _coerce_int(value, 15)
            return minutes if minutes > 0 else 15

        @field_validator("health_port", mode="before")
        @classmethod
        def _v_health_port(cls, value: Any) -> int:
            return _normalize_port(value, 9001)

        @field_validator("roi_delta_threshold", mode="before")
        @classmethod
        def _v_roi_delta(cls, value: Any) -> float:
            threshold = _coerce_float(value, 5.0)
            return threshold if threshold >= 0 else 5.0

        # -- 便捷派生属性 ------------------------------------------------
        @property
        def busy_timeout_sec(self) -> float:
            """sqlite3.connect(timeout=) 用的秒级超时。"""
            return max(self.busy_timeout_ms, 0) / 1000.0

        @property
        def gq_db_dir(self) -> str:
            """events.db 所在目录 (绝对路径)。"""
            return str(Path(self.gq_db_path).resolve().parent)

        @property
        def use_redis_queue(self) -> bool:
            """是否真的走 Redis 队列 (需同时开启开关且配置 URL)。"""
            return bool(self.queue_enabled and self.redis_url)

        @classmethod
        def load(cls) -> "Config":
            """返回进程内单例配置 (等价 ``get_config()``)。"""
            return get_config()

except Exception:  # pragma: no cover - 无 pydantic-settings 时的降级路径
    HAS_PYDANTIC_SETTINGS = False

    class Config:  # type: ignore[no-redef]
        """中央配置 (纯标准库降级实现)。

        与 pydantic-settings 版本保持一致的字段/默认值/API, 便于在缺依赖的解释器
        (如托管 python) 上跑 smoke 测试, 不需要额外安装。
        """

        __slots__ = tuple(_FIELD_SPEC.keys())

        def __init__(self, **overrides: Any) -> None:
            env_or = overrides.get

            def pick(name: str) -> Any:
                if name in overrides:
                    return env_or(name)
                return _env(name)

            self.gq_db_path: str = (
                _coerce_optional_str(pick("gq_db_path"), None) or DEFAULT_GQ_DB_PATH
            )
            self.log_level: str = _normalize_log_level(pick("log_level"), "INFO")
            self.redis_url: Optional[str] = _coerce_optional_str(pick("redis_url"), None)
            self.queue_enabled: bool = _coerce_bool(pick("queue_enabled"), False)
            roi = _coerce_float(pick("roi_delta_threshold"), 5.0)
            self.roi_delta_threshold: float = roi if roi >= 0 else 5.0
            busy = _coerce_int(pick("busy_timeout_ms"), 30000)
            self.busy_timeout_ms: int = busy if busy >= 0 else 30000
            ckpt = _coerce_int(pick("wal_checkpoint_min"), 15)
            self.wal_checkpoint_min: int = ckpt if ckpt > 0 else 15
            self.health_port: int = _normalize_port(pick("health_port"), 9001)

        # -- pydantic 兼容 API ------------------------------------------
        def model_dump(self) -> Dict[str, Any]:
            """返回字段字典 (与 pydantic ``model_dump()`` 同名同义)。"""
            return {name: getattr(self, name) for name in _FIELD_SPEC}

        def dict(self) -> Dict[str, Any]:  # noqa: A003 - 兼容 pydantic v1 习惯
            """``model_dump()`` 的别名。"""
            return self.model_dump()

        # -- 便捷派生属性 ------------------------------------------------
        @property
        def busy_timeout_sec(self) -> float:
            """sqlite3.connect(timeout=) 用的秒级超时。"""
            return max(self.busy_timeout_ms, 0) / 1000.0

        @property
        def gq_db_dir(self) -> str:
            """events.db 所在目录 (绝对路径)。"""
            return str(Path(self.gq_db_path).resolve().parent)

        @property
        def use_redis_queue(self) -> bool:
            """是否真的走 Redis 队列 (需同时开启开关且配置 URL)。"""
            return bool(self.queue_enabled and self.redis_url)

        @classmethod
        def load(cls) -> "Config":
            """返回进程内单例配置 (等价 ``get_config()``)。"""
            return get_config()

        def __repr__(self) -> str:
            fields = ", ".join(f"{k}={v!r}" for k, v in self.model_dump().items())
            return f"Config({fields})"


# ---------------------------------------------------------------------------
# 单例管理
# ---------------------------------------------------------------------------

_config_lock = threading.RLock()
_config_singleton: Optional[Config] = None


def get_config() -> Config:
    """获取进程内配置单例 (线程安全, 首次调用时从环境变量构建)。"""
    global _config_singleton
    if _config_singleton is not None:
        return _config_singleton
    with _config_lock:
        if _config_singleton is None:
            _config_singleton = Config()
        return _config_singleton


def reload_config(**overrides: Any) -> Config:
    """重建配置单例 (测试/热更新场景)。

    Args:
        **overrides: 直接覆盖字段值; 未提供的字段仍从环境变量读取。

    Returns:
        新的配置单例。
    """
    global _config_singleton
    with _config_lock:
        _config_singleton = Config(**overrides) if overrides else Config()
        return _config_singleton

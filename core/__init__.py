"""哨响AI 稳定化下一代足球系统 — 跨切面基础设施层 (core).

本包为**新增**基础设施, 零侵入现有运行模块 (bridge_service / gq / pipeline / analysis)。
所有模块遵循两条硬约束:

1. **可降级导入**: pydantic / pydantic-settings / python-json-logger / fastapi 均为
   *可选* 依赖。缺失时自动回退到标准库实现, 保证在任意 Python 3.9+ 解释器下可导入,
   避免"基础设施本身把运行时拖崩"。
2. **绝不抛出**: 日志/编码/错误序列化路径上的任何异常都被吞掉并降级, 不允许基础设施
   成为新的崩溃源 (事故④ 的教训)。

模块清单:
    config.py          中央配置 (单一配置源, env 驱动)
    logging_config.py  JSON 结构化日志 + trace_id
    safe_log.py        UTF-8 安全日志适配层 SafeLog
    error_envelope.py  统一错误信封 {ok, error:{code, message:str}}
    db_manager.py      SQLite WAL + 单写者 + 有界连接池 + 坏页自检
    collector_step.py  采集器 per-step 隔离运行器
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"

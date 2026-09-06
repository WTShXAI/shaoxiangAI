"""采集器 per-step 隔离运行器 (REQ-06, 事故④/⑥ 根治).

事故背景 (docs/system_design.md §1.1 事故④ + §7 采集器禁项):
    采集器把 fetch / parse / persist / live-flip 写在同一个大 try 里 (或干脆没有 try),
    任何一步抛异常就 ``return`` 掉整轮 → live 翻页被跳过 → ``scheduled`` 永不翻 live →
    前端"刚开赛的比赛不显示"。

本模块的硬约束 (**禁项固化**):
    * ``CollectorRound.run_all()`` 对**每一步**独立 ``try/except``;
    * 单步失败只产出 ``StepResult(ok=False, error_msg=<str>)`` 并**继续后续步骤**,
      任何情况下都不提前 ``return``、不向上抛;
    * ``error_msg`` 恒为安全字符串 (复用 ``core.error_envelope.coerce_message``,
      与前端错误信封同一套不变量, 对象型异常绝不透传);
    * 每步携带 ``trace_id``, 全链路可检索 (REQ-10)。

唯一例外: ``KeyboardInterrupt`` / ``SystemExit`` 等 ``BaseException`` 仍然向上传播 ——
运维主动停机不应该被"隔离"掉。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from core.error_envelope import coerce_message
from core.logging_config import get_logger, new_trace_id
from core.safe_log import safe_str

__all__ = [
    "StepResult",
    "CollectorContext",
    "CollectorStep",
    "FunctionStep",
    "CollectorRound",
    "summarize",
]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    """单步执行结果 (class diagram: StepResult)。

    Attributes:
        step: 步骤名。
        ok: 是否成功。
        error_msg: 失败原因, **恒为字符串** (成功时为空串)。
        trace_id: 本轮 trace_id。
        duration_ms: 耗时 (毫秒)。
        data: 步骤产出的轻量元数据 (如 ``{"rows": 42}``), 仅用于日志/观测。
    """

    step: str = ""
    ok: bool = True
    error_msg: str = ""
    trace_id: str = ""
    duration_ms: float = 0.0
    data: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """字段安全化: 保证 ``step``/``error_msg``/``trace_id`` 均为 str。"""
        self.step = safe_str(self.step, 200)
        self.ok = bool(self.ok)
        self.error_msg = "" if not self.error_msg else coerce_message(self.error_msg)
        self.trace_id = safe_str(self.trace_id, 128)
        try:
            self.duration_ms = float(self.duration_ms)
        except (TypeError, ValueError):
            self.duration_ms = 0.0
        if not isinstance(self.data, dict):
            self.data = {"value": safe_str(self.data, 2000)}

    def to_dict(self) -> Dict[str, Any]:
        """转成可 JSON 化的 dict。"""
        return {
            "step": self.step,
            "ok": self.ok,
            "error_msg": self.error_msg,
            "trace_id": self.trace_id,
            "duration_ms": round(self.duration_ms, 3),
            "data": self.data,
        }


@dataclass
class CollectorContext:
    """一轮采集的共享上下文。

    Attributes:
        trace_id: 本轮 trace_id (未给则自动生成)。
        round_no: 轮次编号。
        started_at: 起始时间戳 (epoch 秒)。
        shared: 步骤间传递数据的字典 (如 fetch 的原始报文给 parse 用)。
        results: 已完成步骤的结果列表 (由 ``CollectorRound`` 填充)。
    """

    trace_id: str = ""
    round_no: int = 0
    started_at: float = field(default_factory=time.time)
    shared: Dict[str, Any] = field(default_factory=dict)
    results: List[StepResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        """未指定 trace_id 时自动生成。"""
        self.trace_id = safe_str(self.trace_id, 128) or new_trace_id("collect")
        try:
            self.round_no = int(self.round_no)
        except (TypeError, ValueError):
            self.round_no = 0

    def get(self, key: str, default: Any = None) -> Any:
        """读取共享数据。"""
        return self.shared.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """写入共享数据。"""
        self.shared[key] = value

    def failed_steps(self) -> List[str]:
        """返回已失败步骤名列表。"""
        return [r.step for r in self.results if not r.ok]

    def has_failure(self) -> bool:
        """本轮是否出现过失败步骤。"""
        return any(not r.ok for r in self.results)


# ---------------------------------------------------------------------------
# Step 抽象
# ---------------------------------------------------------------------------


class CollectorStep(ABC):
    """采集步骤抽象基类 (class diagram: CollectorStep)。

    子类只需实现 ``run(ctx)``。抛出的异常会被 ``CollectorRound`` 捕获并转成
    ``StepResult(ok=False)``, 因此**子类内部不需要为了"别崩轮"而吞异常** ——
    该抛就抛, 隔离由运行器负责。

    Attributes:
        name: 步骤名 (用于日志/结果), 默认取类名。
        critical: 标记该步是否关键。仅作为**观测元数据**, 不改变"继续跑后续步骤"的行为。
    """

    name: str = ""
    critical: bool = False

    def __init__(self, name: str = "", critical: bool = False) -> None:
        """初始化。

        Args:
            name: 步骤名; 空则用类的 ``name`` 属性或类名。
            critical: 是否关键步骤 (仅观测用)。
        """
        resolved = safe_str(name or self.__class__.name or type(self).__name__, 200)
        self.name = resolved or type(self).__name__
        self.critical = bool(critical or self.__class__.critical)

    @abstractmethod
    def run(self, ctx: CollectorContext) -> Optional[StepResult]:
        """执行本步骤。

        Args:
            ctx: 本轮共享上下文。

        Returns:
            ``StepResult``; 返回 ``None`` 视为成功 (运行器自动补 ok 结果)。
        """
        raise NotImplementedError

    # -- 便捷构造 -------------------------------------------------------
    def ok_result(self, trace_id: str = "", **data: Any) -> StepResult:
        """构造成功结果。"""
        return StepResult(step=self.name, ok=True, trace_id=trace_id, data=dict(data))

    def fail_result(self, error: Any, trace_id: str = "", **data: Any) -> StepResult:
        """构造失败结果 (``error`` 任意类型, 自动转安全字符串)。"""
        return StepResult(
            step=self.name,
            ok=False,
            error_msg=coerce_message(error),
            trace_id=trace_id,
            data=dict(data),
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, critical={self.critical})"


class FunctionStep(CollectorStep):
    """把普通函数包装成 ``CollectorStep``。

    Example:
        >>> ctx = CollectorContext()
        >>> step = FunctionStep("fetch", lambda c: c.set("rows", 3))
        >>> step.run(ctx) is None
        True
        >>> ctx.get("rows")
        3
    """

    def __init__(
        self,
        name: str,
        func: Callable[[CollectorContext], Any],
        critical: bool = False,
    ) -> None:
        """初始化。

        Args:
            name: 步骤名。
            func: 接收 ``ctx`` 的可调用对象; 返回 ``StepResult`` 时原样采用,
                返回其他值时写入 ``StepResult.data["value"]``。
            critical: 是否关键步骤。

        Raises:
            TypeError: ``func`` 不可调用。
        """
        super().__init__(name=name, critical=critical)
        if not callable(func):
            raise TypeError(f"FunctionStep(func=) must be callable, got {type(func).__name__}")
        self._func = func

    def run(self, ctx: CollectorContext) -> Optional[StepResult]:
        """调用被包装的函数。"""
        outcome = self._func(ctx)
        if isinstance(outcome, StepResult):
            return outcome
        if outcome is None:
            return None
        return self.ok_result(trace_id=ctx.trace_id, value=safe_str(outcome, 2000))


# ---------------------------------------------------------------------------
# Round 运行器
# ---------------------------------------------------------------------------


class CollectorRound:
    """一轮采集的隔离运行器 (class diagram: CollectorRound)。

    行为契约 (REQ-06):
        * 顺序执行 ``steps``, **每步独立 try/except**;
        * 单步失败 → 记 ``StepResult(ok=False, error_msg=str)`` → **继续下一步**;
        * 整个 ``run_all()`` 不向调用方抛 ``Exception`` (``BaseException`` 除外);
        * 返回与 ``steps`` **一一对应**的结果列表 (长度必然相等)。

    Example:
        >>> def boom(ctx):
        ...     raise ValueError({"why": "对象型异常"})
        >>> steps = [
        ...     FunctionStep("s1", lambda c: c.set("a", 1)),
        ...     FunctionStep("s2", boom),
        ...     FunctionStep("s3", lambda c: c.set("b", 2)),
        ... ]
        >>> results = CollectorRound(steps).run_all()
        >>> [r.ok for r in results]
        [True, False, True]
        >>> isinstance(results[1].error_msg, str)
        True
    """

    def __init__(
        self,
        steps: Iterable[CollectorStep],
        logger: Optional[Any] = None,
        on_result: Optional[Callable[[StepResult], None]] = None,
    ) -> None:
        """初始化。

        Args:
            steps: 步骤序列 (按顺序执行)。非 ``CollectorStep`` 的可调用对象会被
                自动包装成 ``FunctionStep``。
            logger: 结构化 logger; ``None`` 时用 ``get_logger("core.collector_step")``。
            on_result: 每步完成后的回调 (回调自身异常也被吞掉)。
        """
        self.steps: List[CollectorStep] = [self._normalize_step(s, i) for i, s in enumerate(steps)]
        self._logger = logger if logger is not None else get_logger("core.collector_step")
        self._on_result = on_result

    @staticmethod
    def _normalize_step(step: Any, index: int) -> CollectorStep:
        """把任意输入规范成 ``CollectorStep``。

        Raises:
            TypeError: 既不是 ``CollectorStep`` 也不可调用。
        """
        if isinstance(step, CollectorStep):
            return step
        if callable(step):
            name = safe_str(getattr(step, "__name__", "") or f"step_{index}", 200)
            return FunctionStep(name, step)
        raise TypeError(
            f"steps[{index}] must be CollectorStep or callable, got {type(step).__name__}"
        )

    def run_all(self, ctx: Optional[CollectorContext] = None) -> List[StepResult]:
        """顺序执行全部步骤, **绝不因单步失败而中断**。

        Args:
            ctx: 共享上下文; ``None`` 时自动创建。

        Returns:
            与 ``self.steps`` 一一对应的 ``StepResult`` 列表。
        """
        context = ctx if ctx is not None else CollectorContext()
        results: List[StepResult] = []

        for index, step in enumerate(self.steps):
            started = time.perf_counter()
            step_name = safe_str(getattr(step, "name", "") or f"step_{index}", 200)
            result: StepResult
            try:
                outcome = step.run(context)
                if isinstance(outcome, StepResult):
                    result = outcome
                    if not result.step:
                        result.step = step_name
                    if not result.trace_id:
                        result.trace_id = context.trace_id
                elif outcome is None:
                    result = StepResult(step=step_name, ok=True, trace_id=context.trace_id)
                else:
                    # 步骤返回了非 StepResult 的值: 视为成功, 值放进 data 便于排查
                    result = StepResult(
                        step=step_name,
                        ok=True,
                        trace_id=context.trace_id,
                        data={"value": safe_str(outcome, 2000)},
                    )
            except Exception as exc:
                # ★ REQ-06 核心: 只记录, 不 return, 不 raise —— 后续步骤照常执行
                result = StepResult(
                    step=step_name,
                    ok=False,
                    error_msg=coerce_message(exc),
                    trace_id=context.trace_id,
                    data={"exc_type": type(exc).__name__},
                )
                self._log_failure(step, result, exc)
            else:
                self._log_success(result)

            try:
                result.duration_ms = (time.perf_counter() - started) * 1000.0
            except Exception:
                result.duration_ms = 0.0

            results.append(result)
            context.results.append(result)
            self._fire_callback(result)

        self._log_summary(context, results)
        return results

    # -- 日志/回调 (全部吞异常, 日志失败不许影响采集) ---------------------
    def _log_failure(self, step: CollectorStep, result: StepResult, exc: BaseException) -> None:
        """记录单步失败。"""
        try:
            self._logger.error(
                "collector step failed (isolated, round continues)",
                step=result.step,
                trace_id=result.trace_id,
                critical=bool(getattr(step, "critical", False)),
                exc_type=type(exc).__name__,
                error_msg=result.error_msg,
            )
        except Exception:
            pass

    def _log_success(self, result: StepResult) -> None:
        """记录单步成功。"""
        try:
            self._logger.debug(
                "collector step ok",
                step=result.step,
                trace_id=result.trace_id,
            )
        except Exception:
            pass

    def _log_summary(self, ctx: CollectorContext, results: List[StepResult]) -> None:
        """记录本轮汇总。"""
        try:
            stats = summarize(results)
            self._logger.info(
                "collector round finished",
                trace_id=ctx.trace_id,
                round_no=ctx.round_no,
                total=stats["total"],
                ok=stats["ok"],
                failed=stats["failed"],
                failed_steps=",".join(stats["failed_steps"]),
            )
        except Exception:
            pass

    def _fire_callback(self, result: StepResult) -> None:
        """触发 ``on_result`` 回调。"""
        if self._on_result is None:
            return
        try:
            self._on_result(result)
        except Exception:
            try:
                self._logger.warning("on_result callback failed", step=result.step)
            except Exception:
                pass

    def __repr__(self) -> str:
        names = ",".join(s.name for s in self.steps)
        return f"CollectorRound(steps=[{names}])"


def summarize(results: Iterable[StepResult]) -> Dict[str, Any]:
    """汇总一轮结果。

    Args:
        results: ``StepResult`` 序列。

    Returns:
        ``{"total", "ok", "failed", "failed_steps", "total_ms"}``。
    """
    items = list(results)
    failed = [r for r in items if not r.ok]
    total_ms = 0.0
    for item in items:
        try:
            total_ms += float(item.duration_ms)
        except (TypeError, ValueError):
            continue
    return {
        "total": len(items),
        "ok": len(items) - len(failed),
        "failed": len(failed),
        "failed_steps": [r.step for r in failed],
        "total_ms": round(total_ms, 3),
    }

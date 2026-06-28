"""
哨响AI v3.0 — Tool 基类 + 预测上下文 + 全链路 Tracer
=====================================================
借鉴 Agent 系统的 Tool Use 设计模式，将预测管线的每个阶段抽象为可组合的 Tool。

核心抽象：
  Tool          — 可组合的预测能力单元
  PredictionContext — 流经管线的共享上下文
  ToolResult    — Tool 执行结果（含降级标记）
  Tracer        — 全链路结构化日志

设计目标：
  1. 每个 Tool 可独立测试、替换、组合
  2. 支持 Graceful Degradation（失败自动降级到备用 Tool）
  3. 全链路 Trace（每个 stage 的输入/输出/耗时）
  4. 新因子接入成本降低 90%（只需加一个 Tool）
"""

import time
import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════
# Degradation Level
# ════════════════════════════════════════════════════════════

class DegradationLevel(Enum):
    """降级等级"""
    FULL = 0         # 全特征 ML 管线
    ODDS_DRIVEN = 1  # 赔率驱动（跳过 ML）
    HISTORICAL = 2   # 历史先验（联赛级 D 率）
    UNPREDICTABLE = 3  # 不可预测

# ════════════════════════════════════════════════════════════
# Trace Entry
# ════════════════════════════════════════════════════════════

@dataclass
class TraceEntry:
    """单次 Tool 执行的 Trace 记录"""
    tool_name: str
    phase: str                # 阶段名: "features", "model", "fusion", etc.
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    duration_ms: float = 0
    success: bool = True
    degradation: bool = False
    input_summary: Dict = field(default_factory=dict)   # 输入摘要（不存全量特征）
    output_summary: Dict = field(default_factory=dict)  # 输出摘要
    error: Optional[str] = None
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            'tool': self.tool_name,
            'phase': self.phase,
            'ts': self.timestamp,
            'duration_ms': round(self.duration_ms, 1),
            'ok': self.success,
            'degraded': self.degradation,
            'input': self.input_summary,
            'output': self.output_summary,
            'error': self.error,
            'meta': self.metadata,
        }

# ════════════════════════════════════════════════════════════
# Tracer
# ════════════════════════════════════════════════════════════

class Tracer:
    """
    全链路 Trace 日志系统

    记录每个 Tool 的：输入摘要、输出摘要、耗时、成功/失败
    最终输出结构化 JSON trace 用于调试和回放。
    """

    def __init__(self, match_label: str = "unknown"):
        self.match_label = match_label
        self.entries: List[TraceEntry] = []
        self._start_time = time.time()

    def start(self, tool_name: str, phase: str, input_summary: Dict = None) -> TraceEntry:
        """开始一个 Tool 调用"""
        entry = TraceEntry(
            tool_name=tool_name,
            phase=phase,
            input_summary=input_summary or {},
        )
        entry.metadata['_start'] = time.time()
        return entry

    def finish(self, entry: TraceEntry, output_summary: Dict = None,
               success: bool = True, error: str = None, degradation: bool = False):
        """结束一个 Tool 调用"""
        start = entry.metadata.pop('_start', time.time())
        entry.duration_ms = (time.time() - start) * 1000
        entry.success = success
        entry.error = error
        entry.degradation = degradation
        entry.output_summary = output_summary or {}
        self.entries.append(entry)

    def to_dict(self) -> Dict:
        """导出完整 trace 为 dict"""
        total_ms = (time.time() - self._start_time) * 1000
        return {
            'match': self.match_label,
            'total_duration_ms': round(total_ms, 1),
            'n_steps': len(self.entries),
            'all_passed': all(e.success for e in self.entries),
            'degradation_count': sum(1 for e in self.entries if e.degradation),
            'steps': [e.to_dict() for e in self.entries],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)

    def print_trace(self):
        """打印可读的 trace 摘要"""
        print(f"\n{'='*65}")
        print(f"🔍 Trace: {self.match_label}  ({len(self.entries)} steps, {self.to_dict()['total_duration_ms']:.0f}ms)")
        print(f"{'='*65}")
        for e in self.entries:
            status = '✅' if e.success else ('⚠️' if e.degradation else '❌')
            dur = f"{e.duration_ms:.0f}ms"
            print(f"  {status} {e.tool_name:<25s} {dur:>6s}  "
                  f"in={e.input_summary}  out={e.output_summary}")
            if e.error:
                print(f"     └─ {e.error}")
        print(f"{'='*65}")

# ════════════════════════════════════════════════════════════
# PredictionContext
# ════════════════════════════════════════════════════════════

@dataclass
class PredictionContext:
    """
    预测上下文 — 流经整个 Tool 管线的共享数据容器。

    Tool 之间不直接通信，而是读/写这个上下文对象。
    每个 Tool 负责填充自己负责的字段。
    """
    # ── 输入 ──
    home_team: str
    away_team: str
    league: Optional[str] = None

    # ── 特征 ──
    features: Optional[Dict[str, float]] = None
    odds_features: Optional[Dict[str, float]] = None

    # ── 质量元数据 ──
    quality_meta: Dict[str, Any] = field(default_factory=lambda: {
        'home_match_count': 0,
        'away_match_count': 0,
        'feature_coverage_ratio': 0.0,
        'is_cold_start': True,
    })

    # ── 模型输出 ──
    model_probs: Optional[Tuple[float, float, float]] = None   # (H, D, A)
    heuristic_probs: Optional[Tuple[float, float, float]] = None  # HeuristicPredictor (H, D, A)
    odds_implied: Optional[Dict[str, float]] = None            # {'H': ..., 'D': ..., 'A': ...}

    # ── 融合结果 ──
    fusion: Optional[Dict[str, float]] = None                  # {'H': ..., 'D': ..., 'A': ...}
    fusion_weights: Optional[Dict[str, float]] = None

    # ── 预测结果 ──
    prediction: Optional[str] = None         # 'H' | 'D' | 'A'
    confidence: Optional[float] = None
    prediction_mode: str = "pending"

    # ── 附加分析 ──
    score_prediction: Optional[Dict] = None
    over_under: Optional[Dict] = None
    risk_assessment: Optional[Dict] = None

    # ── 降级状态 ──
    degradation_level: DegradationLevel = DegradationLevel.FULL
    degradation_reason: str = ""
    skip_ml: bool = False

    # ── Trace ──
    tracer: Optional[Tracer] = None

    @property
    def is_cold_start(self) -> bool:
        return self.quality_meta.get('is_cold_start', False)

    @property
    def feat_cov_ratio(self) -> float:
        return self.quality_meta.get('feature_coverage_ratio', 0.0)

    def describe(self) -> str:
        """人类可读摘要"""
        return (f"{self.home_team} vs {self.away_team}"
                f" | cov={self.feat_cov_ratio:.1%}"
                f" | cold={self.is_cold_start}"
                f" | level={self.degradation_level.name}"
                f" | mode={self.prediction_mode}")

# ════════════════════════════════════════════════════════════
# ToolResult
# ════════════════════════════════════════════════════════════

@dataclass
class ToolResult:
    """Tool 执行结果"""
    success: bool
    tool_name: str = ""
    data: Any = None
    error: Optional[str] = None
    degraded: bool = False          # 是否触发降级
    degradation_reason: str = ""
    next_tools: Optional[List[str]] = None  # 建议的下一步 Tool（可选）

# ════════════════════════════════════════════════════════════
# Tool — 抽象基类
# ════════════════════════════════════════════════════════════

class Tool(ABC):
    """
    预测管线 Tool 抽象基类

    每个 Tool 代表管线中的一个能力单元。
    Tool 之间通过 PredictionContext 共享数据。

    用法:
      class MyTool(Tool):
          name = "my_tool"
          description = "Does something useful"
          phase = "custom"

          def execute(self, ctx: PredictionContext) -> ToolResult:
              # read from ctx, compute, write back to ctx
              return ToolResult(success=True, data=..., tool_name=self.name)

    关键约定:
      - execute() 只读 ctx 的输入字段，写入 ctx 的输出字段
      - 失败时返回 ToolResult(success=False) 而非抛异常
      - 降级时设置 ToolResult(degraded=True, degradation_reason="...")
    """

    name: str = "base_tool"
    description: str = "Base tool"
    phase: str = "unknown"
    version: str = "1.0"

    @abstractmethod
    def execute(self, ctx: PredictionContext) -> ToolResult:
        ...

    def can_handle(self, ctx: PredictionContext) -> bool:
        """此 Tool 在当前上下文中是否可用"""
        return True

    def _trace(self, ctx: PredictionContext, result: ToolResult):
        """记录 trace 条目"""
        if ctx.tracer is None:
            return
        entry = ctx.tracer.start(self.name, self.phase,
                                 input_summary={'teams': ctx.describe()})
        output = result.data if isinstance(result.data, dict) else {'result': str(result.data)[:100]}
        ctx.tracer.finish(entry, output_summary=output,
                          success=result.success,
                          error=result.error,
                          degradation=result.degraded)

    def run(self, ctx: PredictionContext) -> ToolResult:
        """执行 Tool 并自动记录 trace"""
        t0 = time.time()
        result = self.execute(ctx)
        result.tool_name = self.name
        self._trace(ctx, result)
        return result

    def __repr__(self):
        return f"Tool({self.name} v{self.version})"

# ════════════════════════════════════════════════════════════
# 内置通用 Tool
# ════════════════════════════════════════════════════════════

class NoOpTool(Tool):
    """空操作 Tool — 用于占位或跳过阶段"""
    name = "noop"
    description = "No operation / placeholder"
    phase = "pass"

    def execute(self, ctx: PredictionContext) -> ToolResult:
        return ToolResult(success=True, tool_name=self.name, data={'skipped': True})

class FailSafeTool(Tool):
    """
    终极降级 Tool — 当所有策略都失败时，返回"不可预测"
    """
    name = "failsafe"
    description = "Ultimate fallback: mark as unpredictable"
    phase = "fallback"

    def execute(self, ctx: PredictionContext) -> ToolResult:
        ctx.prediction = "SKIP"
        ctx.confidence = 0.0
        ctx.prediction_mode = "unpredictable"
        ctx.degradation_level = DegradationLevel.UNPREDICTABLE
        ctx.degradation_reason = "所有预测策略均失败"
        ctx.fusion = {"H": 0.333, "D": 0.334, "A": 0.333}
        return ToolResult(
            success=True,
            tool_name=self.name,
            degraded=True,
            degradation_reason=ctx.degradation_reason,
            data={
                'prediction': 'SKIP',
                'reason': 'All strategies exhausted',
                'recommendation': '手动分析'
            }
        )

"""
哨响AI v3.0 — Tools 包
======================
预测管线 Tool 化架构。每个 Tool 是可组合的预测能力单元。

核心组件:
  base.py               — Tool基类 / PredictionContext / ToolResult / Tracer
  degradation.py        — Graceful Degradation降级链
  tool_pipeline.py      — ToolPipeline主编排器
  plan_execute_review.py — Plan-Execute-Review 自适应修正循环
"""

from .base import (
    Tool, PredictionContext, ToolResult, Tracer, TraceEntry,
    DegradationLevel, NoOpTool, FailSafeTool,
)
from .degradation import DegradationChain, HistoricalPriorTool
from .tool_pipeline import (
    ToolPipeline,
    FeatureBuilderTool,
    OddsAnalyzerTool,
    ModelPredictorTool,
    FusionEngineTool,
    OddsOnlyFusionTool,
    InvestGateTool,
    HarvestingGuardTool,
    ScorePredictorTool,
)
from .plan_execute_review import PlanExecuteReview, PERResult

__all__ = [
    # Base
    "Tool", "PredictionContext", "ToolResult", "Tracer", "TraceEntry",
    "DegradationLevel", "NoOpTool", "FailSafeTool",
    # Degradation
    "DegradationChain", "HistoricalPriorTool",
    # Pipeline
    "ToolPipeline",
    # Tools
    "FeatureBuilderTool", "OddsAnalyzerTool", "ModelPredictorTool",
    "FusionEngineTool", "OddsOnlyFusionTool",
    "InvestGateTool", "HarvestingGuardTool", "ScorePredictorTool",
    # P-E-R
    "PlanExecuteReview", "PERResult",
]

"""
哨响AI v3.0 — Plan-Execute-Review 闭环
=======================================
在 ToolPipeline 之上添加自适应重试层。

当 Guard 检测到问题后，不满足于"报警"，而是：
  1. 分析失败原因
  2. 调整预测参数（降级等级、融合权重、跳过特定 Tool）
  3. 重新执行预测
  4. 最多 3 轮自动修正

适用场景:
  - 冷启动时 ML 输出坍缩 → 自动降级到赔率驱动
  - 特征覆盖率低 → 提高赔率融合权重
  - Guard 发现异常概率分布 → 回退到纯赔率模式
  - 数据源缺失 → 降级到历史先验

用法:
  loop = PlanExecuteReview(max_retries=3)
  result = loop.execute("卡塔尔", "瑞士", pipeline, ctx)
"""

import logging
from typing import Any, Dict, Optional, List
from datetime import datetime
from dataclasses import dataclass, field

from .base import PredictionContext, ToolResult, DegradationLevel

logger = logging.getLogger(__name__)


@dataclass
class PERResult:
    """Plan-Execute-Review 最终结果"""
    success: bool
    prediction: Optional[str] = None
    confidence: float = 0.0
    mode: str = "unknown"
    degradation_level: int = 0
    retries: int = 0
    actions_taken: List[str] = field(default_factory=list)
    final_ctx: Optional[PredictionContext] = None
    error: Optional[str] = None
    trace: Optional[Dict] = None


class PlanExecuteReview:
    """
    自适应预测修正循环

    Plan    → 分析上下文，选择策略
    Execute → 运行 ToolPipeline
    Review  → 检查结果质量，决定是否重试
    """

    MAX_RETRIES = 3

    # 质量问题阈值
    QUALITY_THRESHOLDS = {
        'max_confidence_gap': 0.50,    # 预测方向两概率差 < 0.05 → 方向不清晰
        'min_confidence': 0.30,        # 置信度 < 30% → 太弱
        'entropy_min': 0.90,           # 熵 > 0.90 → 接近均匀分布
        'extreme_confidence': 0.95,    # 置信度 > 95% → 可能过拟合
    }

    def __init__(self, pipeline=None):
        self._pipeline = pipeline
        self._actions_log: List[str] = []

    def execute(self, home_team: str, away_team: str,
                league: Optional[str] = None) -> PERResult:
        """
        Plan-Execute-Review 主循环

        最多重试 3 轮，每轮可以做以下调整:
          Round 1: 标准预测
          Round 2: 强制赔率降级
          Round 3: 历史先验
        """
        from .tool_pipeline import ToolPipeline

        pipeline = self._pipeline or ToolPipeline()
        ctx = PredictionContext(home_team=home_team, away_team=away_team, league=league)

        for attempt in range(self.MAX_RETRIES + 1):
            logger.info(f"[PER] Round {attempt + 1}/{self.MAX_RETRIES + 1} — {ctx.describe()}")

            # ── Plan: 根据 attempt 调整策略 ──
            if attempt == 0:
                action = "标准预测"
            elif attempt == 1:
                action = "强制赔率降级"
                ctx.degradation_level = DegradationLevel.ODDS_DRIVEN
                ctx.skip_ml = True
            elif attempt == 2:
                action = "历史先验降级"
                ctx.degradation_level = DegradationLevel.HISTORICAL
                ctx.skip_ml = True
            else:
                action = "不可预测"
                ctx.degradation_level = DegradationLevel.UNPREDICTABLE

            self._actions_log.append(f"R{attempt+1}: {action}")
            logger.info(f"[PER] Plan: {action}")

            # ── Execute ──
            try:
                result = pipeline.run(home_team, away_team, league)
            except (Exception, KeyError, IndexError) as e:
                logger.error(f"[PER] Execute 失败: {e}")
                if attempt < self.MAX_RETRIES:
                    continue
                return PERResult(
                    success=False,
                    error=str(e),
                    retries=attempt,
                    actions_taken=self._actions_log,
                    trace=getattr(pipeline, 'tracer', None) and pipeline.tracer.to_dict(),
                )

            # ── Review: 检查结果质量 ──
            ctx = self._sync_ctx_from_result(ctx, result)
            quality_ok, reason = self._review_quality(result)

            if quality_ok:
                logger.info(f"[PER] ✅ 质量检查通过")
                return PERResult(
                    success=True,
                    prediction=result.get('prediction'),
                    confidence=result.get('confidence', 0),
                    mode=result.get('prediction_mode', 'unknown'),
                    degradation_level=result.get('degradation_level', 0),
                    retries=attempt,
                    actions_taken=self._actions_log,
                    trace=result.get('trace'),
                )
            else:
                logger.warning(f"[PER] ⚠️ 质量问题: {reason}")
                if attempt >= self.MAX_RETRIES:
                    # 已用完重试次数，接受最后的结果
                    logger.warning(f"[PER] 已用完{self.MAX_RETRIES}次重试，接受当前结果")
                    return PERResult(
                        success=True,
                        prediction=result.get('prediction'),
                        confidence=result.get('confidence', 0),
                        mode=f"{result.get('prediction_mode', 'unknown')}_degraded",
                        degradation_level=result.get('degradation_level', 3),
                        retries=attempt,
                        actions_taken=self._actions_log,
                        trace=result.get('trace'),
                    )

        # 不应到达这里
        return PERResult(success=False, error="所有重试均失败",
                         retries=self.MAX_RETRIES,
                         actions_taken=self._actions_log)

    def _review_quality(self, result: Dict) -> tuple:
        """
        检查预测结果的质量

        Returns:
            (ok: bool, reason: str)
        """
        probs = result.get('probabilities', {})
        if not probs:
            return False, "无概率输出"

        h, d, a = probs.get('H', 0), probs.get('D', 0), probs.get('A', 0)
        values = [h, d, a]

        # 检查1: 概率分布熵 (太均匀 → 无信息量)
        import math
        entropy = -sum(p * math.log(p) if p > 0 else 0 for p in values) / math.log(3)
        if entropy > self.QUALITY_THRESHOLDS['entropy_min']:
            return False, f"概率接近均匀(entropy={entropy:.3f} > {self.QUALITY_THRESHOLDS['entropy_min']})"

        # 检查2: 置信度太低
        conf = result.get('confidence', 0)
        if conf < self.QUALITY_THRESHOLDS['min_confidence']:
            return False, f"置信度太低({conf:.1%} < {self.QUALITY_THRESHOLDS['min_confidence']:.0%})"

        # 检查3: Top1和Top2差距太小 → 方向不明确
        sorted_probs = sorted(values, reverse=True)
        gap = sorted_probs[0] - sorted_probs[1]
        if gap < 0.03:
            return False, f"方向不明确(gap={gap:.3f})"

        # 检查4: 过度自信
        if conf > self.QUALITY_THRESHOLDS['extreme_confidence']:
            return False, f"过度自信({conf:.1%} > {self.QUALITY_THRESHOLDS['extreme_confidence']:.0%})"

        return True, "OK"

    def _sync_ctx_from_result(self, ctx: PredictionContext, result: Dict) -> PredictionContext:
        """从结果 dict 同步回 ctx"""
        ctx.prediction = result.get('prediction')
        ctx.confidence = result.get('confidence', 0)
        ctx.prediction_mode = result.get('prediction_mode', 'unknown')
        ctx.fusion = result.get('probabilities')
        return ctx

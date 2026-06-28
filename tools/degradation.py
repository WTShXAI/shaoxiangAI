"""
哨响AI v3.0 — Graceful Degradation 降级链
==========================================
四档降级策略，确保任何极端情况下都有输出而不崩溃。

降级链:
  Level 0 (FULL):        全特征 ML 管线 — 正常路径
  Level 1 (ODDS_DRIVEN): 赔率驱动 — ML 不可用时（冷启动/模型损坏）
  Level 2 (HISTORICAL):  历史先验 — 无赔率时（联赛D率/历史交锋）
  Level 3 (UNPREDICTABLE): 不可预测 — 所有信息源均不可用

降级触发条件:
  - 模型加载失败 → Level 1
  - 特征覆盖率 < 50% → Level 1（冷启动）
  - 赔率查询失败 → Level 2
  - 联赛无历史数据 → Level 3

用法:
  chain = DegradationChain()
  result = chain.execute(ctx)
"""

import logging
from typing import Dict, Optional, Tuple
from .base import (
    PredictionContext, ToolResult, DegradationLevel, Tool
)

logger = logging.getLogger(__name__)

class DegradationChain:
    """
    降级链 — 按优先级尝试不同策略

    策略注册表：每个 Level 对应一组 Tool 列表。
    当某个 Level 的 Tool 失败时，自动降到下一级。
    """

    def __init__(self):
        self._strategies: Dict[DegradationLevel, list] = {}
        self._level_names = {
            DegradationLevel.FULL: "全特征ML",
            DegradationLevel.ODDS_DRIVEN: "赔率驱动",
            DegradationLevel.HISTORICAL: "历史先验",
            DegradationLevel.UNPREDICTABLE: "不可预测",
        }

    def register(self, level: DegradationLevel, tools: list):
        """注册一个降级级别的 Tool 列表"""
        self._strategies[level] = tools

    def determine_initial_level(self, ctx: PredictionContext) -> DegradationLevel:
        """
        根据上下文判断初始降级等级

        Returns:
            初始 DegradationLevel
        """
        # 规则1: 冷启动 → 直接用赔率驱动
        if ctx.is_cold_start and ctx.feat_cov_ratio < 0.50:
            logger.info(f"[Degradation] 冷启动检测(cov={ctx.feat_cov_ratio:.1%}) → Level 1 赔率驱动")
            return DegradationLevel.ODDS_DRIVEN

        # 规则2: 特征构建完全失败
        if ctx.features is None:
            logger.info("[Degradation] 特征构建失败 → Level 1 赔率驱动")
            return DegradationLevel.ODDS_DRIVEN

        # 规则3: 正常路径
        return DegradationLevel.FULL

    def execute(self, ctx: PredictionContext) -> ToolResult:
        """
        按降级链顺序执行预测

        从初始 Level 开始，失败则降到下一级，直到 UNPREDICTABLE。
        """
        initial_level = self.determine_initial_level(ctx)
        current_level = initial_level
        last_error = None

        while current_level.value <= DegradationLevel.UNPREDICTABLE.value:
            tools = self._strategies.get(current_level, [])
            if not tools:
                current_level = DegradationLevel(
                    min(current_level.value + 1, DegradationLevel.UNPREDICTABLE.value)
                )
                continue

            level_name = self._level_names.get(current_level, current_level.name)
            logger.info(f"[Degradation] 尝试 Level {current_level.value} ({level_name}), "
                        f"{len(tools)} tools: {[t.name for t in tools]}")

            ctx.degradation_level = current_level
            all_passed = True

            for tool in tools:
                try:
                    result = tool.run(ctx)
                    if not result.success:
                        all_passed = False
                        last_error = result.error or f"{tool.name} 失败"
                        logger.warning(f"[Degradation] {tool.name} 失败: {last_error}")
                        # 非致命失败: 继续当前 Level 的下一个 tool
                        if not result.degraded:
                            break  # 致命失败 → 降级
                except (Exception, KeyError, IndexError) as e:
                    all_passed = False
                    last_error = str(e)
                    logger.error(f"[Degradation] {tool.name} 异常: {e}", exc_info=True)
                    break

            if all_passed and ctx.prediction is not None:
                logger.info(f"[Degradation] Level {current_level.value} ({level_name}) 成功: "
                            f"{ctx.prediction} conf={ctx.confidence:.1%}")
                return ToolResult(
                    success=True,
                    tool_name=f"degradation_chain(L{current_level.value})",
                    data={
                        'prediction': ctx.prediction,
                        'confidence': ctx.confidence,
                        'level': current_level.value,
                        'level_name': level_name,
                        'mode': ctx.prediction_mode,
                    }
                )

            # 降级
            next_level = DegradationLevel(
                min(current_level.value + 1, DegradationLevel.UNPREDICTABLE.value)
            )
            logger.warning(f"[Degradation] Level {current_level.value} 未完成 → 降级到 Level {next_level.value}")
            current_level = next_level
            ctx.degradation_reason = (ctx.degradation_reason + f" | L{current_level.value-1}失败: {last_error}")

        # 所有级别都失败
        ctx.prediction = "SKIP"
        ctx.confidence = 0.0
        ctx.prediction_mode = "all_failed"
        ctx.degradation_level = DegradationLevel.UNPREDICTABLE

        return ToolResult(
            success=True,  # 不是 crash，是优雅降级到底
            tool_name="degradation_chain(FINAL_FALLBACK)",
            degraded=True,
            degradation_reason=ctx.degradation_reason,
            data={
                'prediction': 'SKIP',
                'confidence': 0.0,
                'level': DegradationLevel.UNPREDICTABLE.value,
                'reason': ctx.degradation_reason,
                'recommendation': '所有策略均失败，建议人工分析',
            }
        )

    def get_level_description(self, level: DegradationLevel) -> str:
        return self._level_names.get(level, f"Level {level.value}")

# ════════════════════════════════════════════════════════════
# 历史先验 Tool (Level 2 fallback)
# ════════════════════════════════════════════════════════════

class HistoricalPriorTool(Tool):
    """
    历史先验预测 — 当 ML 和赔率都不可用时使用

    策略:
      1. 查询联赛历史 H/D/A 分布
      2. 查询两队历史交锋记录
      3. 综合给出保守估计
    """
    name = "historical_prior"
    description = "Predict using league-level historical distribution and H2H records"
    phase = "prediction"
    version = "1.0"

    def execute(self, ctx: PredictionContext) -> ToolResult:
        try:
            import sqlite3
            db_path = self._find_db_path()
            if not db_path:
                return ToolResult(success=False, tool_name=self.name,
                                  error="数据库不可用", degraded=True)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row

            # 1. 联赛历史分布
            league_probs = {'H': 0.40, 'D': 0.28, 'A': 0.32}  # 默认
            if ctx.league:
                row = conn.execute("""
                    SELECT final_result, COUNT(*) as cnt
                    FROM training_extended
                    WHERE league_name = ? AND odds_spread IS NOT NULL
                    GROUP BY final_result
                """, (ctx.league,)).fetchall()
                total = sum(r['cnt'] for r in row)
                if total > 50:  # 足够样本
                    result_map = {r['final_result']: r['cnt'] for r in row}
                    league_probs = {
                        'H': result_map.get('H', 0) / total,
                        'D': result_map.get('D', 0) / total,
                        'A': result_map.get('A', 0) / total,
                    }

            # 2. H2H 历史
            h2h = conn.execute("""
                SELECT home_team, away_team, home_score, away_score, final_result
                FROM training_extended
                WHERE (home_team = ? AND away_team = ?)
                   OR (home_team = ? AND away_team = ?)
                ORDER BY match_date DESC
                LIMIT 5
            """, (ctx.home_team, ctx.away_team, ctx.away_team, ctx.home_team)).fetchall()

            h2h_probs = {'H': 0.40, 'D': 0.25, 'A': 0.35}
            if h2h:
                h2h_results = [r['final_result'] for r in h2h if r['final_result']]
                if h2h_results:
                    n = len(h2h_results)
                    h2h_probs = {
                        'H': h2h_results.count('H') / n,
                        'D': h2h_results.count('D') / n,
                        'A': h2h_results.count('A') / n,
                    }

            conn.close()

            # 3. 融合联赛先验 + H2H (50/50)
            fusion = {
                'H': round(league_probs['H'] * 0.5 + h2h_probs['H'] * 0.5, 4),
                'D': round(league_probs['D'] * 0.5 + h2h_probs['D'] * 0.5, 4),
                'A': round(league_probs['A'] * 0.5 + h2h_probs['A'] * 0.5, 4),
            }
            # 归一化
            total = sum(fusion.values())
            for k in fusion:
                fusion[k] = round(fusion[k] / total, 4)

            labels = ["H", "D", "A"]
            probs = [fusion["H"], fusion["D"], fusion["A"]]
            pred_idx = max(range(3), key=lambda i: probs[i])

            ctx.fusion = fusion
            ctx.prediction = labels[pred_idx]
            ctx.confidence = probs[pred_idx]
            ctx.prediction_mode = "historical_prior"

            logger.info(f"[HistoricalPrior] {ctx.describe()} → "
                        f"league={league_probs} h2h={h2h_probs} → {ctx.prediction} {ctx.confidence:.1%}")

            return ToolResult(
                success=True,
                tool_name=self.name,
                data={
                    'prediction': ctx.prediction,
                    'confidence': ctx.confidence,
                    'fusion': fusion,
                    'league_probs': league_probs,
                    'h2h_probs': h2h_probs,
                    'n_h2h': len(h2h),
                }
            )

        except (Exception) as e:
            logger.error(f"[HistoricalPrior] 失败: {e}")
            return ToolResult(success=False, tool_name=self.name,
                              error=str(e), degraded=True)

    def _find_db_path(self) -> Optional[str]:
        import os
        candidates = [
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'data', 'football_data.db'),
        ]
        for p in candidates:
            if os.path.exists(p):
                return p
        return None

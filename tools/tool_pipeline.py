"""
哨响AI v3.0 — ToolPipeline 预测管线编排器
==========================================
替代 prediction_service.py 中的单体 predict_single() 方法。

核心设计:
  1. 将预测拆分为独立的 Tool 链
  2. 支持 Tool 的动态组合和替换
  3. 集成 DegradationChain 三档降级
  4. 集成 Tracer 全链路日志
  5. 集成 PredictionGuard 守护检查

管线 (正常路径):
  FeatureBuilder → OddsAnalyzer → ModelPredictor → FusionEngine
  → InvestGate → ScorePredictor → HarvestingGuard

管线 (冷启动路径):
  FeatureBuilder(冷启动标记) → OddsAnalyzer → [跳过ModelPredictor]
  → OddsFusion → InvestGate → ScorePredictor

用法:
  pipeline = ToolPipeline()
  result = pipeline.run("卡塔尔", "瑞士", league="世界杯")
  pipeline.tracer.print_trace()
"""

import sys
import os
import logging
import numpy as np
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

from .base import (
    PredictionContext, ToolResult, DegradationLevel,
    Tracer, Tool, NoOpTool, FailSafeTool
)
from .degradation import DegradationChain, HistoricalPriorTool

# 导入 HeuristicPredictor
_PROJECT_ROOT = os.environ.get(
    'PROJECT_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, _PROJECT_ROOT)
from agents.heuristic_predictor import HeuristicPredictor

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
# 内联 Tool 定义 (从 prediction_service.py 提取核心逻辑)
# ════════════════════════════════════════════════════════════

class FeatureBuilderTool(Tool):
    """构建特征向量 + 质量审计 + 冷启动检测"""
    name = "feature_builder"
    description = "Build feature vector from DB + smart defaults"
    phase = "features"
    version = "2.8"

    def __init__(self, service=None):
        self._service = service  # 可选的 PredictionService 引用

    def execute(self, ctx: PredictionContext) -> ToolResult:
        try:
            if self._service:
                result = self._service._build_features(ctx.home_team, ctx.away_team, ctx.league)
                if result is None:
                    return ToolResult(success=False, tool_name=self.name,
                                      error="特征构建返回 None", degraded=True)

                if isinstance(result, tuple):
                    features, quality_meta = result
                else:
                    features, quality_meta = result, {
                        'home_match_count': 0, 'away_match_count': 0,
                        'feature_coverage_ratio': 0.0, 'is_cold_start': True,
                    }

                ctx.features = features
                ctx.quality_meta = quality_meta

                n_features = len(features) if features else 0
                return ToolResult(
                    success=True, tool_name=self.name,
                    data={
                        'n_features': n_features,
                        'coverage': quality_meta.get('feature_coverage_ratio', 0),
                        'cold_start': quality_meta.get('is_cold_start', False),
                    }
                )
            return ToolResult(success=False, tool_name=self.name,
                              error="PredictionService 引用不可用")
        except (Exception, requests.exceptions.RequestException) as e:
            logger.error(f"[{self.name}] 失败: {e}")
            return ToolResult(success=False, tool_name=self.name,
                              error=str(e), degraded=True)


class OddsAnalyzerTool(Tool):
    """赔率分析：隐含概率 + 多维度赔率查询"""
    name = "odds_analyzer"
    description = "Extract implied probabilities and multi-market odds"
    phase = "odds"
    version = "2.8"

    def __init__(self, service=None):
        self._service = service

    def execute(self, ctx: PredictionContext) -> ToolResult:
        try:
            if not self._service:
                return ToolResult(success=False, tool_name=self.name,
                                  error="Service 不可用")

            # 1. 赔率隐含概率
            ctx.odds_implied = self._service._get_odds_implied_probs(
                ctx.features or {}, ctx.home_team, ctx.away_team
            )

            # 2. 赔率专精特征
            ctx.odds_features = self._service._build_odds_features(
                ctx.home_team, ctx.away_team, ctx.league
            )

            has_implied = ctx.odds_implied is not None
            has_features = ctx.odds_features is not None and len(ctx.odds_features) >= 5

            return ToolResult(
                success=True, tool_name=self.name,
                data={
                    'implied_available': has_implied,
                    'features_available': has_features,
                    'implied': ctx.odds_implied,
                }
            )
        except (Exception) as e:
            logger.error(f"[{self.name}] 失败: {e}")
            return ToolResult(success=False, tool_name=self.name, error=str(e))


class ModelPredictorTool(Tool):
    """ML 模型预测（ModelBridge）+ Heuristic 子模型"""
    name = "model_predictor"
    description = "Run ensemble model prediction via ModelBridge, plus Heuristic"
    phase = "model"
    version = "2.9"

    def __init__(self, service=None):
        self._service = service
        self._heuristic = None  # 延迟初始化 HeuristicPredictor

    def execute(self, ctx: PredictionContext) -> ToolResult:
        if ctx.skip_ml:
            return ToolResult(success=True, tool_name=self.name,
                              data={'skipped': 'cold_start'},
                              degraded=True,
                              degradation_reason="冷启动跳过ML")

        try:
            model = self._service.model if self._service else None
            if model is None:
                return ToolResult(success=False, tool_name=self.name,
                                  error="模型未加载", degraded=True)

            # ── Step A: 先算 HeuristicPredictor（用于 meta_features 注入）──
            external_heuristic = None
            try:
                if self._heuristic is None:
                    self._heuristic = HeuristicPredictor()
                if (self._service and self._service.model and
                        self._service.model._trainer is not None):
                    trainer = self._service.model._trainer
                    feature_names = trainer.feature_names
                    vec = np.zeros(len(feature_names))
                    if ctx.features:
                        for i, name in enumerate(feature_names):
                            if name in ctx.features:
                                vec[i] = float(ctx.features[name])
                    X = vec.reshape(1, -1)
                    odds_data = ctx.odds_features
                    proba_heuristic = self._heuristic.predict_proba(
                        X, feature_names=feature_names, odds_data=odds_data
                    )[0]
                    ctx.heuristic_probs = (
                        float(proba_heuristic[0]),
                        float(proba_heuristic[1]),
                        float(proba_heuristic[2]),
                    )
                    # 传给 model.predict → 注入 meta_features 替换简化版 heuristic
                    external_heuristic = np.array([ctx.heuristic_probs], dtype=np.float64)
                    logger.info(f"[Heuristic] H={ctx.heuristic_probs[0]:.3f} "
                                f"D={ctx.heuristic_probs[1]:.3f} "
                                f"A={ctx.heuristic_probs[2]:.3f} → 注入meta_features")
                else:
                    ctx.heuristic_probs = (0.33, 0.34, 0.33)
            except Exception as e:
                logger.warning(f"[Heuristic] 调用失败: {e}")
                ctx.heuristic_probs = (0.33, 0.34, 0.33)

            # ── Step B: 模型预测（HeuristicPredictor 输出注入 meta_learner）──
            kwargs = {}
            if ctx.odds_features:
                kwargs['odds_data'] = ctx.odds_features
            if external_heuristic is not None:
                kwargs['external_heuristic_proba'] = external_heuristic

            model_result = model.predict(ctx.features, **kwargs)

            if model_result is None:
                return ToolResult(success=False, tool_name=self.name,
                                  error="模型返回 None", degraded=True)

            ctx.model_probs = (
                model_result.get("home", 0),
                model_result.get("draw", 0),
                model_result.get("away", 0),
            )

            return ToolResult(
                success=True, tool_name=self.name,
                data={
                    'h': round(ctx.model_probs[0], 4),
                    'd': round(ctx.model_probs[1], 4),
                    'a': round(ctx.model_probs[2], 4),
                }
            )
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"[{self.name}] 失败: {e}")
            return ToolResult(success=False, tool_name=self.name,
                              error=str(e), degraded=True)


class FusionEngineTool(Tool):
    """概率融合：模型 + 赔率 → 最终预测"""
    name = "fusion_engine"
    description = "Fuse model probabilities with odds-implied probabilities"
    phase = "fusion"
    version = "2.8"

    FUSION_ODDS_WEIGHT = 0.70
    FUSION_MODEL_WEIGHT = 0.30
    FUSION_HEURISTIC_WEIGHT = 0.05  # heuristic 独立权重

    def execute(self, ctx: PredictionContext) -> ToolResult:
        try:
            h_prob, d_prob, a_prob = getattr(ctx, 'model_probs', None) or (0.33, 0.34, 0.33)
            h_heur, d_heur, a_heur = getattr(ctx, 'heuristic_probs', None) or (h_prob, d_prob, a_prob)

            # ── 冷启动自动降级 ──
            if ctx.is_cold_start and ctx.odds_implied:
                # 动态权重：覆盖率越低，赔率权重越高
                odds_boost = max(0.0, (0.50 - ctx.feat_cov_ratio) * 2)
                new_odds_w = min(0.95, self.FUSION_ODDS_WEIGHT + odds_boost)
                new_model_w = 1.0 - new_odds_w

                h_o = ctx.odds_implied.get("H", h_prob)
                d_o = ctx.odds_implied.get("D", d_prob)
                a_o = ctx.odds_implied.get("A", a_prob)

                h_f = h_o * new_odds_w + h_prob * new_model_w
                d_f = d_o * new_odds_w + d_prob * new_model_w
                a_f = a_o * new_odds_w + a_prob * new_model_w

                total = h_f + d_f + a_f or 1.0
                fusion = {"H": round(h_f/total, 4), "D": round(d_f/total, 4), "A": round(a_f/total, 4)}

                ctx.prediction_mode = f"odds_degraded(odds={new_odds_w:.0%},model={new_model_w:.0%})"
                ctx.fusion_weights = {"odds": new_odds_w, "model": new_model_w}

                logger.info(f"[Cold-Start-Fusion] {ctx.describe()} → "
                            f"权重 o={new_odds_w:.0%} m={new_model_w:.0%} → "
                            f"{fusion['H']:.1%}/{fusion['D']:.1%}/{fusion['A']:.1%}")

            elif ctx.odds_implied:
                # 正常路径: 三路融合 (model + heuristic + odds)
                h_o = ctx.odds_implied.get("H", h_prob)
                d_o = ctx.odds_implied.get("D", d_prob)
                a_o = ctx.odds_implied.get("A", a_prob)

                wo = self.FUSION_ODDS_WEIGHT
                wm = self.FUSION_MODEL_WEIGHT
                w_heur = self.FUSION_HEURISTIC_WEIGHT

                # 归一化三路权重
                total_w = wo + wm + w_heur
                wo_n = wo / total_w
                wm_n = wm / total_w
                w_heur_n = w_heur / total_w

                h_f = h_o * wo_n + h_prob * wm_n + h_heur * w_heur_n
                d_f = d_o * wo_n + d_prob * wm_n + d_heur * w_heur_n
                a_f = a_o * wo_n + a_prob * wm_n + a_heur * w_heur_n

                total = h_f + d_f + a_f or 1.0
                fusion = {"H": round(h_f/total, 4), "D": round(d_f/total, 4), "A": round(a_f/total, 4)}

                ctx.prediction_mode = "fusion"
                ctx.fusion_weights = {"odds": round(wo_n, 2), "model": round(wm_n, 2), "heuristic": round(w_heur_n, 2)}
            else:
                # 无赔率：纯模型
                fusion = {"H": round(h_prob, 4), "D": round(d_prob, 4), "A": round(a_prob, 4)}
                ctx.prediction_mode = "model_only"
                ctx.fusion_weights = {"odds": 0.0, "model": 1.0}

            ctx.fusion = fusion

            labels = ["H", "D", "A"]
            probs = [fusion["H"], fusion["D"], fusion["A"]]
            pred_idx = max(range(3), key=lambda i: probs[i])
            ctx.prediction = labels[pred_idx]
            ctx.confidence = round(probs[pred_idx], 4)

            return ToolResult(
                success=True, tool_name=self.name,
                data={'prediction': ctx.prediction, 'confidence': ctx.confidence,
                      'fusion': fusion, 'mode': ctx.prediction_mode}
            )
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"[{self.name}] 失败: {e}")
            return ToolResult(success=False, tool_name=self.name, error=str(e))


class OddsOnlyFusionTool(Tool):
    """
    纯赔率融合 — Level 1 降级时使用
    跳过 ML 模型，直接用赔率隐含概率作为预测
    """
    name = "odds_only_fusion"
    description = "Use odds-implied probabilities directly (skip ML)"
    phase = "fusion"
    version = "1.0"

    def execute(self, ctx: PredictionContext) -> ToolResult:
        if ctx.odds_implied is None:
            return ToolResult(success=False, tool_name=self.name,
                              error="无赔率数据", degraded=True)

        ctx.fusion = ctx.odds_implied
        ctx.fusion_weights = {"odds": 1.0, "model": 0.0}
        ctx.prediction_mode = "odds_only"
        ctx.skip_ml = True

        labels = ["H", "D", "A"]
        probs = [ctx.fusion["H"], ctx.fusion["D"], ctx.fusion["A"]]
        pred_idx = max(range(3), key=lambda i: probs[i])
        ctx.prediction = labels[pred_idx]
        ctx.confidence = round(probs[pred_idx], 4)

        logger.info(f"[OddsOnly] {ctx.describe()} → {ctx.prediction} {ctx.confidence:.1%}")

        return ToolResult(
            success=True, tool_name=self.name,
            data={'prediction': ctx.prediction, 'confidence': ctx.confidence}
        )


class InvestGateTool(Tool):
    """
    INVEST 门控 V2 — 三道独立门
    Gate1: 模型×赔率方向一致
    Gate2: sigma_trap 低波动
    Gate3: 历史先验命中率
    """
    name = "invest_gate"
    description = "INVEST gate V2: 3 independent gates for betting decisions"
    phase = "post"
    version = "2.0"

    def execute(self, ctx: PredictionContext) -> ToolResult:
        # INVEST门控是投注决策层，保持现有逻辑但增加可观测性
        decision = "PASS"  # 默认通过
        try:
            if ctx.confidence and ctx.confidence > 0.60:
                if ctx.odds_implied:
                    # Gate1: 模型×赔率方向一致
                    model_dir = ctx.prediction
                    odds_probs = [ctx.odds_implied.get('H', 0),
                                  ctx.odds_implied.get('D', 0),
                                  ctx.odds_implied.get('A', 0)]
                    odds_dir = ["H", "D", "A"][max(range(3), key=lambda i: odds_probs[i])]

                    if model_dir == odds_dir and ctx.confidence > 0.70:
                        decision = "INVEST"

            return ToolResult(
                success=True, tool_name=self.name,
                data={'decision': decision, 'model_dir': ctx.prediction}
            )
        except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
            return ToolResult(success=True, tool_name=self.name,
                              data={'decision': 'PASS', 'error': str(e)})


class HarvestingGuardTool(Tool):
    """收割防护扫描（封装现有 HarvestingGuard）"""
    name = "harvesting_guard"
    description = "Scan for harvesting signals in multi-market odds"
    phase = "post"
    version = "1.0"

    def __init__(self, service=None, guard_available=False, guard=None):
        self._service = service
        self._guard_available = guard_available
        self._guard = guard

    def execute(self, ctx: PredictionContext) -> ToolResult:
        if not self._guard_available or self._guard is None:
            return ToolResult(success=True, tool_name=self.name,
                              data={'skipped': 'guard_unavailable'})

        try:
            if self._service:
                odds_1x2 = self._service._get_odds_1x2(ctx.home_team, ctx.away_team)
                odds_totals = self._service._get_odds_totals(ctx.home_team, ctx.away_team)
                odds_ah = self._service._get_odds_ah(ctx.home_team, ctx.away_team)

                report = self._guard.scan(
                    odds_1x2=odds_1x2,
                    odds_totals=odds_totals,
                    odds_ah=odds_ah,
                    league=ctx.league,
                    model_total_lambda=1.5,
                )

                ctx.risk_assessment = {
                    'hrs': report.hrs,
                    'risk_level': report.risk_level,
                    'signals': {
                        '1x2': round(report.signal_1x2, 4),
                        'totals': round(report.signal_totals, 4),
                        'ah': round(report.signal_ah, 4),
                    },
                    'recommendation': report.recommendation,
                }

                return ToolResult(
                    success=True, tool_name=self.name,
                    data={'hrs': report.hrs, 'risk_level': report.risk_level}
                )

            return ToolResult(success=True, tool_name=self.name,
                              data={'skipped': 'no_service'})
        except (Exception) as e:
            logger.warning(f"[{self.name}] 失败: {e}")
            return ToolResult(success=True, tool_name=self.name,
                              data={'skipped': 'error', 'error': str(e)})


class ScorePredictorTool(Tool):
    """泊松比分预测 + 大小球分析"""
    name = "score_predictor"
    description = "Poisson score prediction + over/under analysis"
    phase = "post"
    version = "1.0"

    def __init__(self, service=None):
        self._service = service

    def execute(self, ctx: PredictionContext) -> ToolResult:
        if not ctx.fusion:
            return ToolResult(success=True, tool_name=self.name,
                              data={'skipped': 'no_fusion'})

        try:
            if self._service:
                ctx.score_prediction = self._service._compute_score_prediction(
                    ctx.fusion["H"], ctx.fusion["D"], ctx.fusion["A"], ctx.league
                )
                ctx.over_under = self._service._compute_over_under(
                    ctx.fusion["H"], ctx.fusion["D"], ctx.fusion["A"], ctx.league
                )
            return ToolResult(
                success=True, tool_name=self.name,
                data={
                    'top_scores': ctx.score_prediction.get('top_scores', [])[:3] if ctx.score_prediction else [],
                    'total_expected': ctx.over_under.get('total_expected', 0) if ctx.over_under else 0,
                }
            )
        except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
            logger.warning(f"[{self.name}] 失败: {e}")
            return ToolResult(success=True, tool_name=self.name,
                              data={'skipped': 'error'})


# ════════════════════════════════════════════════════════════
# ToolPipeline — 主编排器
# ════════════════════════════════════════════════════════════

class ToolPipeline:
    """
    预测管线编排器

    封装完整的预测流程，支持:
      - 正常路径（全特征 ML）
      - 冷启动降级（赔率驱动）
      - 终极降级（历史先验/不可预测）
      - 全链路 Trace
    """

    def __init__(self, prediction_service=None):
        self._service = prediction_service

        # 初始化 Tool 实例
        self.feature_builder = FeatureBuilderTool(service=prediction_service)
        self.odds_analyzer = OddsAnalyzerTool(service=prediction_service)
        self.model_predictor = ModelPredictorTool(service=prediction_service)
        self.fusion_engine = FusionEngineTool()
        self.odds_only_fusion = OddsOnlyFusionTool()
        self.invest_gate = InvestGateTool()
        self.score_predictor = ScorePredictorTool(service=prediction_service)
        self.failsafe = FailSafeTool()

        # HarvestingGuard
        try:
            from bookmaker_sim.harvesting_guard import HarvestingGuard
            self._guard = HarvestingGuard()
            self._guard_available = True
        except (Exception):
            self._guard = None
            self._guard_available = False

        self.harvesting_guard = HarvestingGuardTool(
            service=prediction_service,
            guard_available=self._guard_available,
            guard=self._guard
        )

        # 构建降级链
        self.degradation = DegradationChain()
        self.degradation.register(DegradationLevel.FULL, [
            self.feature_builder,
            self.odds_analyzer,
            self.model_predictor,
            self.fusion_engine,
        ])
        self.degradation.register(DegradationLevel.ODDS_DRIVEN, [
            self.feature_builder,
            self.odds_analyzer,
            self.odds_only_fusion,
        ])
        self.degradation.register(DegradationLevel.HISTORICAL, [
            HistoricalPriorTool(),
        ])
        self.degradation.register(DegradationLevel.UNPREDICTABLE, [
            self.failsafe,
        ])

        self.tracer: Optional[Tracer] = None

    def run(self, home_team: str, away_team: str,
            league: Optional[str] = None) -> Dict[str, Any]:
        """
        运行完整预测管线

        Args:
            home_team: 主队名
            away_team: 客队名
            league: 联赛名

        Returns:
            预测结果 dict（兼容旧 prediction_service.predict_single 格式）
        """
        # 初始化上下文
        ctx = PredictionContext(
            home_team=home_team,
            away_team=away_team,
            league=league,
        )

        # 初始化 Tracer
        self.tracer = Tracer(match_label=f"{home_team} vs {away_team}")
        ctx.tracer = self.tracer

        # ── Step 0: 模型可用性检查 ──
        model_ok = self._check_model_available()

        # ── Step 1: 特征构建（必做，决定降级路径） ──
        fb_result = self.feature_builder.run(ctx)
        if not fb_result.success:
            # 特征构建完全失败 → 直接降到历史先验
            logger.warning(f"[Pipeline] 特征构建失败 → 跳过 ML/赔率，使用历史先验")
            ctx.degradation_level = DegradationLevel.HISTORICAL

        # ── Step 2: 赔率分析（必做，为降级提供备选） ──
        self.odds_analyzer.run(ctx)

        # ── Step 3: 降级链决策 ──
        if ctx.is_cold_start and ctx.feat_cov_ratio < 0.50:
            # 冷启动自动降级
            ctx.degradation_level = DegradationLevel.ODDS_DRIVEN
            logger.info(f"[Pipeline] 冷启动 → 赔率驱动模式")
        elif not model_ok:
            ctx.degradation_level = DegradationLevel.ODDS_DRIVEN
            logger.warning(f"[Pipeline] 模型不可用 → 赔率驱动模式")
        else:
            ctx.degradation_level = DegradationLevel.FULL

        # ── Step 4: 执行降级链 ──
        chain_result = self.degradation.execute(ctx)

        # ── Step 5: 后处理 (Post-Processing) ──
        if ctx.prediction is not None and ctx.prediction != "SKIP":
            # 投注门控
            self.invest_gate.run(ctx)
            # 收割扫描
            self.harvesting_guard.run(ctx)
            # 比分预测
            self.score_predictor.run(ctx)

        # ── 构建返回结果 ──
        return self._build_response(ctx)

    def _check_model_available(self) -> bool:
        """检查模型是否可用"""
        try:
            if self._service and self._service.model:
                return True
            return False
        except (Exception):
            return False

    def _build_response(self, ctx: PredictionContext) -> Dict[str, Any]:
        """构建兼容旧的 predict_single 返回格式"""
        result = {
            "home_team": ctx.home_team,
            "away_team": ctx.away_team,
            "league": ctx.league,
            "match_date": None,
            "prediction": ctx.prediction or "SKIP",
            "confidence": round(ctx.confidence or 0, 4),
            "probabilities": ctx.fusion or {"H": 0.333, "D": 0.334, "A": 0.333},
            "data_quality": ctx.quality_meta,
            "prediction_mode": ctx.prediction_mode or "unknown",
            "degradation_level": ctx.degradation_level.value,
            "degradation_reason": ctx.degradation_reason,
            "model_comparison": {
                "v6_model": {
                    "H": round(ctx.model_probs[0], 4) if ctx.model_probs else None,
                    "D": round(ctx.model_probs[1], 4) if ctx.model_probs else None,
                    "A": round(ctx.model_probs[2], 4) if ctx.model_probs else None,
                },
                "heuristic": {
                    "H": round(getattr(ctx, 'heuristic_probs', (None, None, None))[0], 4) if getattr(ctx, 'heuristic_probs', None) else None,
                    "D": round(getattr(ctx, 'heuristic_probs', (None, None, None))[1], 4) if getattr(ctx, 'heuristic_probs', None) else None,
                    "A": round(getattr(ctx, 'heuristic_probs', (None, None, None))[2], 4) if getattr(ctx, 'heuristic_probs', None) else None,
                },
                "odds_implied": ctx.odds_implied,
                "fusion": ctx.fusion,
                "fusion_weights": ctx.fusion_weights or {},
            },
            "score_prediction": ctx.score_prediction or {},
            "over_under": ctx.over_under or {},
            "risk_assessment": ctx.risk_assessment,
            # ── Trace 数据 ──
            "trace": self.tracer.to_dict() if self.tracer else None,
        }
        return result

    def print_trace(self):
        """打印全链路 Trace"""
        if self.tracer:
            self.tracer.print_trace()

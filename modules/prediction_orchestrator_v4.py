"""
哨响AI v5.0 — 预测编排器 (Prediction Orchestrator V4)
=======================================================
P1 核心协同模块。将 v5.0 五大 P0 模块串联为完整预测管线。

管线流程:
    NL/API 输入
      ↓
    IntentClassifierV2 → 意图识别 + 路由
      ↓
    ExpertHubV2 → 协同模式选择 + 专家调度规划
      ↓
    PredictionService → 实际模型推理 (复用 v3.2 管线)
      ↓
    KnowledgeBase → 上下文注入 (历史规律/经验教训)
      ↓
    TerminologyInjector → 专业术语注入
      ↓
    UnifiedPrediction → 标准化五元组输出

两种输入模式:
    1. NL 模式: 用户自然语言 → 意图分类 → 路由 → 预测
    2. API 模式: 结构化参数 (home/away/league/odds) → 直接预测

设计原则:
    1. 复用现有 PredictionService (不改动 v3.2 管线)
    2. v5.0 层作为增强壳 (非破坏性升级)
    3. 降级保障: v5.0 层故障 → 自动回退原始 v3.2 输出

作者: Architecture · P1 Phase
日期: 2026-06-18
"""
from __future__ import annotations
import os, sys, time, json, logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# v5.0 模块
from modules.intent_classifier_v2 import (
    IntentClassifierV2, RouteResult, classify_intent,
)
from modules.expert_hub_v2 import (
    ExpertHubV2, CollaborationScheduler, CollaborationMode,
    get_hub, describe_experts,
)
from modules.output_schema import (
    UnifiedPrediction, FusedPrediction, ExpertContribution,
    ThreeWayProbability, DistributionExtension,
    ReasoningChain, ConfidenceAssessment, EvidencePackage,
    create_simple_prediction, create_fallback_prediction,
    create_from_v3_output, TerminologyInjector, SchemaValidator,
)
from knowledge_base import KnowledgeBase, get_knowledge_base
from utils.constants import DEFAULT_HOME_PROB, DEFAULT_DRAW_PROB, DEFAULT_AWAY_PROB, DEFAULT_CONFIDENCE

# ═══════════════════════════════════════════════════════════════
# 1. 编排结果
# ═══════════════════════════════════════════════════════════════

@dataclass
class OrchestrationResult:
    """一次完整的 v5.0 预测编排结果"""
    # 意图信息
    intent: Optional[RouteResult] = None
    is_nl_input: bool = False

    # 协同信息
    collaboration_mode: str = ""
    experts_scheduled: List[str] = field(default_factory=list)
    knowledge_used: List[str] = field(default_factory=list)

    # 预测输出
    prediction: Optional[UnifiedPrediction] = None
    fused_prediction: Optional[FusedPrediction] = None

    # 元信息
    pipeline_version: str = "v5.0-p1"
    total_time_ms: float = 0.0
    fallback_triggered: bool = False
    fallback_reason: str = ""
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        """完整输出 (含 v5.0 增强信息)"""
        result = {}
        if self.prediction:
            result.update(self.prediction.to_dict())

        # v5.0 增强层
        result["v4_enhancement"] = {
            "intent": self.intent.to_dict() if self.intent else None,
            "collaboration_mode": self.collaboration_mode,
            "experts_scheduled": self.experts_scheduled,
            "knowledge_used": self.knowledge_used,
            "pipeline_version": self.pipeline_version,
        }

        # 兼容层
        if self.prediction:
            result["prediction_v3_compat"] = self.prediction.to_v3_compat()

        result["meta"] = {
            "total_time_ms": round(self.total_time_ms, 2),
            "fallback_triggered": self.fallback_triggered,
            "errors": self.errors,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return result

    def to_v3_compat(self) -> Dict:
        """纯 v3.2 兼容输出 (去掉 v5.0 增强层)"""
        if self.prediction:
            return self.prediction.to_v3_compat()
        return {"prediction": {"home": DEFAULT_HOME_PROB, "draw": DEFAULT_DRAW_PROB, "away": DEFAULT_AWAY_PROB}, "confidence": DEFAULT_CONFIDENCE}

# ═══════════════════════════════════════════════════════════════
# 2. v5.0 预测编排器
# ═══════════════════════════════════════════════════════════════

class PredictionOrchestratorV4:
    """
    v5.0 预测编排器 — 全链路协调中心

    串联: 意图识别 → 专家调度 → 模型推理 → 知识增强 → 术语注入 → 标准化输出
    """

    def __init__(self,
                 intent_classifier: IntentClassifierV2 = None,
                 expert_hub: ExpertHubV2 = None,
                 knowledge_base: KnowledgeBase = None,
                 enable_terminology: bool = True,
                 enable_knowledge: bool = True,
                 enable_odds_deep: bool = True):
        """
        Args:
            intent_classifier: 意图分类器 (默认自动创建)
            expert_hub: 专家调度中心 (默认自动创建)
            knowledge_base: 知识底座 (默认自动创建)
            enable_terminology: 是否启用术语注入
            enable_knowledge: 是否启用知识增强
        """
        self.intent_classifier = intent_classifier or IntentClassifierV2()
        self.expert_hub = expert_hub or ExpertHubV2()
        self.knowledge_base = knowledge_base or get_knowledge_base()
        self.scheduler = CollaborationScheduler()

        self.enable_terminology = enable_terminology
        self.enable_knowledge = enable_knowledge
        self.enable_odds_deep = enable_odds_deep

        # 延迟加载 TerminologyInjector YAML
        self._terminology_loaded = False
        self._odds_analyzer = None

    def _ensure_terminology(self):
        """确保术语词典已加载"""
        if not self._terminology_loaded:
            TerminologyInjector.load_from_yaml()
            self._terminology_loaded = True

    # ═══════════════════════════════════════════════════════════
    # API 模式: 结构化输入 → 直接预测
    # ═══════════════════════════════════════════════════════════

    def predict_structured(
        self,
        home_team: str,
        away_team: str,
        league: str = None,
        h_prob: float = None,
        d_prob: float = None,
        a_prob: float = None,
        confidence: float = None,
        odds: Dict[str, float] = None,
        expert_mode: str = "A",
    ) -> OrchestrationResult:
        """
        API 模式: 结构化参数直接预测

        适用场景: 后端 API 调用, 已有预测结果需要 v5.0 增强包装

        Args:
            home_team: 主队名
            away_team: 客队名
            league: 联赛名
            h_prob/d_prob/a_prob: 模型预测概率 (可选, 未提供则调用 PredictionService)
            confidence: 置信度
            odds: 赔率字典 {home, draw, away}
            expert_mode: 协同模式 A/B/C/D

        Returns:
            OrchestrationResult: 完整编排结果
        """
        start = time.perf_counter()
        result = OrchestrationResult()

        try:
            # 1. 意图: API 模式固定为 predict
            result.intent = RouteResult(
                intent_category="predict", intent_subtype="match_result",
                confidence=1.0, action="execute",
                collaboration_mode=expert_mode,
                primary_expert="郝优算",
                support_experts=["季泊松", "荣合众", "杜博弈", "曾均衡", "毕建模"],
                matched_keywords=["API"],
            )
            result.collaboration_mode = expert_mode

            # 2. 专家调度规划
            mode = self._mode_from_string(expert_mode)
            experts = self.expert_hub.get_mode_experts(mode)
            result.experts_scheduled = [e.name for e in experts]

            # 3. 构建预测
            if h_prob is not None and d_prob is not None and a_prob is not None:
                result.prediction = create_simple_prediction(
                    home=h_prob, draw=d_prob, away=a_prob,
                    expert_id="prediction_service",
                    confidence=confidence or 0.5,
                    summary=f"{home_team} vs {away_team} v5.0 预测",
                )
            else:
                # 无概率 → 尝试调用实际 PredictionService
                result.prediction = self._call_prediction_service(
                    home_team, away_team, league, odds
                )

            # 4. 知识增强
            if self.enable_knowledge and result.prediction:
                result.knowledge_used = self._inject_knowledge(
                    result.prediction, home_team, away_team, league, odds
                )

            # 4.5 模式A: 全栈执行 (运行所有分析器)
            if result.prediction and result.prediction.probability:
                p = result.prediction.probability
                if expert_mode == "A" and odds:
                    self._run_odds_deep(result, home_team, away_team, odds, league)
                    self._run_draw_upset(result, home_team, away_team,
                                         p.home, p.draw, p.away, odds, league)
                elif self.enable_odds_deep and expert_mode == "B" and odds:
                    self._run_odds_deep(result, home_team, away_team, odds, league)
                elif expert_mode == "C":
                    self._run_draw_upset(result, home_team, away_team,
                                         p.home, p.draw, p.away, odds, league)

            # 5. 术语注入
            if self.enable_terminology and result.prediction:
                self._ensure_terminology()
                self._inject_terminology(result.prediction, result.collaboration_mode)

        except ImportError as e:
            logger.warning(f"[V4 Orchestrator] 模块缺失, 降级: {e}")
            result.fallback_triggered = True
            result.fallback_reason = f"import:{e}"
            result.errors.append(str(e))
            result.prediction = create_fallback_prediction(f"模块缺失: {e}")
        except (ValueError, RuntimeError, TypeError) as e:
            logger.error(f"[V4 Orchestrator] 预测失败: {e}", exc_info=True)
            result.fallback_triggered = True
            result.fallback_reason = str(e)
            result.errors.append(str(e))
            result.prediction = create_fallback_prediction(f"v5.0 编排异常: {e}")
        except Exception as e:
            logger.critical(f"[V4 Orchestrator] 未预期错误: {e}", exc_info=True)
            result.fallback_triggered = True
            result.fallback_reason = str(e)
            result.errors.append(str(e))
            result.prediction = create_fallback_prediction(f"v5.0 编排异常: {e}")

        result.total_time_ms = (time.perf_counter() - start) * 1000
        return result

    # ═══════════════════════════════════════════════════════════
    # NL 模式: 自然语言输入 → 意图分类 → 路由 → 预测
    # ═══════════════════════════════════════════════════════════

    def predict_nl(
        self,
        user_input: str,
        home_team: str = None,
        away_team: str = None,
        league: str = None,
        odds: Dict[str, float] = None,
        h_prob: float = None,
        d_prob: float = None,
        a_prob: float = None,
    ) -> OrchestrationResult:
        """
        NL 模式: 自然语言输入 → 完整管线

        适用场景: 操盘手对话式交互, 聊天机器人

        Args:
            user_input: 用户自然语言
            home_team/away_team/league: 已知的上下文 (可选)
            odds: 赔率 (可选)
            h_prob/d_prob/a_prob: 已计算的概率 (可选)

        Returns:
            OrchestrationResult: 完整编排结果
        """
        start = time.perf_counter()
        result = OrchestrationResult(is_nl_input=True)

        try:
            # 1. 意图分类
            result.intent = self.intent_classifier.classify(user_input)
            if result.intent.action == "reject":
                result.prediction = create_fallback_prediction(
                    f"无法理解意图: {user_input}"
                )
                result.total_time_ms = (time.perf_counter() - start) * 1000
                return result

            # 2. 路由到协同模式
            mode = self.expert_hub.route_by_mode(
                result.intent.intent_category, result.intent.intent_subtype
            )
            result.collaboration_mode = mode.value

            # 3. 专家调度规划
            experts = self.expert_hub.get_mode_experts(mode)
            result.experts_scheduled = [e.name for e in experts]

            # 4. 构建/获取预测
            if h_prob is not None and d_prob is not None and a_prob is not None:
                result.prediction = create_simple_prediction(
                    home=h_prob, draw=d_prob, away=a_prob,
                    expert_id="prediction_service",
                    confidence=result.intent.confidence,
                    summary=f"[{result.intent.intent_category}] {user_input}",
                )
            elif home_team and away_team:
                result.prediction = self._call_prediction_service(
                    home_team, away_team, league, odds
                )
            else:
                # 纯分析/解释类意图: 不需要概率预测
                result.prediction = create_fallback_prediction(
                    f"NL意图 '{result.intent.intent_category}' 无需预测, 请提供具体比赛信息"
                )

            # 5. 知识增强
            if self.enable_knowledge and result.prediction and home_team:
                result.knowledge_used = self._inject_knowledge(
                    result.prediction, home_team, away_team, league, odds
                )

            # 6. 术语注入
            if self.enable_terminology and result.prediction:
                self._ensure_terminology()
                self._inject_terminology(result.prediction, result.collaboration_mode)

        except Exception as e:
            logger.error(f"[V4 Orchestrator] NL预测失败: {e}")
            result.fallback_triggered = True
            result.fallback_reason = str(e)
            result.errors.append(str(e))
            result.prediction = create_fallback_prediction(f"v5.0 编排异常: {e}")


        result.total_time_ms = (time.perf_counter() - start) * 1000
        return result

    # ═══════════════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════════════

    def _call_prediction_service(
        self, home_team: str, away_team: str, league: str = None,
        odds: Dict[str, float] = None,
    ) -> UnifiedPrediction:
        """调用现有 PredictionService 获取预测，失败时从赔率反推"""
        try:
            from backend.services.prediction_service import PredictionService
            svc = PredictionService()
            raw = svc.predict_single(home_team, away_team, league, custom_odds=odds)

            if raw and isinstance(raw.get("prediction"), dict):
                return create_from_v3_output(raw)

        except ImportError:
            logger.debug("PredictionService 不可用")
        except (ValueError, RuntimeError, TypeError) as e:
            logger.warning(f"PredictionService 调用失败: {e}")
        except Exception as e:
            logger.error(f"PredictionService 意外错误: {e}", exc_info=True)

        # 降级: 从赔率反推概率
        if odds and all(k in odds for k in ["home", "draw", "away"]):
            h, d, a = odds["home"], odds["draw"], odds["away"]
            inv_sum = 1.0/h + 1.0/d + 1.0/a
            h_p = (1.0/h) / inv_sum
            d_p = (1.0/d) / inv_sum
            a_p = (1.0/a) / inv_sum
            return create_simple_prediction(
                home=h_p, draw=d_p, away=a_p,
                expert_id="odds_implied",
                confidence=0.4,
                summary=f"从赔率反推: {home_team} vs {away_team}",
            )

        return create_fallback_prediction("无赔率数据")

    def _inject_knowledge(
        self, pred: UnifiedPrediction,
        home_team: str, away_team: str,
        league: str = None, odds: Dict = None,
    ) -> List[str]:
        """注入知识底座信息"""
        used = []

        # 1. 根据联赛查找领域知识
        if league:
            league_kb = self.knowledge_base.search(league, limit=3)
            for entry in league_kb:
                pred.reasoning.key_factors.append(f"[{entry.title}] {entry.content[:80]}")
                used.append(entry.key)

        # 2. 查找相关历史规律
        if odds:
            spread = abs(1.0 / odds.get("home", 2.0) - 1.0 / odds.get("away", 2.0))
            if spread > 3:
                patterns = self.knowledge_base.search("spread", category="pattern", limit=2)
            else:
                patterns = self.knowledge_base.search("draw", category="pattern", limit=2)
            for entry in patterns:
                used.append(entry.key)

        # 3. 注入相关教训 (仅 warning 级别)
        lessons = self.knowledge_base.get_lessons(severity="warning")
        for lesson in lessons[:2]:
            pred.evidence.degradation_indicators.append(
                f"参考教训: {lesson.title}"
            )
            used.append(lesson.key)

        return used

    def _run_odds_deep(self, result: OrchestrationResult,
                       home_team: str, away_team: str,
                       odds: Dict, league: str = None):
        """运行赔率深度分析 (模式B)"""
        try:
            if self._odds_analyzer is None:
                from modules.odds_deep_analyzer import get_odds_analyzer
                self._odds_analyzer = get_odds_analyzer()

            report = self._odds_analyzer.analyze(home_team, away_team, odds, league)

            # 将赔率分析结果嵌入 prediction 的 reasoning 链
            if result.prediction:
                rc = result.prediction.reasoning
                rc.steps.append({
                    "expert": "杜博弈",
                    "finding": f"总抽水率{report.margin.total_margin:.1%}",
                    "impact": report.bookmaker_signal,
                })
                rc.steps.append({
                    "expert": "杜博弈",
                    "finding": f"陷阱评分{report.trap.trap_score:.2f} ({report.trap.risk_level})",
                    "impact": report.trap.recommendation,
                })
                rc.summary = report.summary if report.summary else rc.summary

                # 将完整报告附加到 evidence
                result.prediction.evidence.data_sources.append(
                    f"OddsDeepAnalyzer v5.0 (risk={report.overall_risk})"
                )
                result.prediction.evidence.degradation_indicators.extend(
                    [f"[庄家信号] {report.bookmaker_signal}"]
                )

            # 存储完整报告到 result (供 API 消费)
            result.__dict__['odds_deep_report'] = report.to_dict()

        except ImportError as e:
            logger.warning(f"赔率分析 模块缺失: {e}")
        except (ValueError, RuntimeError) as e:
            logger.error(f"赔率深度分析失败: {e}")
        except Exception as e:
            logger.error(f"赔率分析 意外错误: {e}", exc_info=True)

    def _run_draw_upset(self, result: OrchestrationResult,
                        home_team: str, away_team: str,
                        h_prob: float, d_prob: float, a_prob: float,
                        odds: Dict = None, league: str = None):
        """运行平局/冷门分析 (模式C)"""
        try:
            from modules.draw_upset_analyzer import get_draw_analyzer
            analyzer = get_draw_analyzer()

            spread = None
            if odds:
                h = odds.get("home", 2.0)
                a = odds.get("away", 3.8)
                if h and a:
                    spread = (1.0/a - 1.0/h) * 100  # 粗略spread估计

            report = analyzer.analyze(
                home_team, away_team, h_prob, d_prob, a_prob,
                odds=odds, league=league, spread=spread,
            )

            if result.prediction:
                rc = result.prediction.reasoning
                da = report.draw_analysis
                rc.steps.append({
                    "expert": "曾均衡",
                    "finding": f"D-Gate区段={da.d_gate_zone} (margin={da.d_margin:.3f})",
                    "impact": da.d_gate_recommendation,
                })
                rc.steps.append({
                    "expert": "曾均衡",
                    "finding": f"冷门评分{report.upset.upset_score:.0f}/100 ({report.upset.upset_level})",
                    "impact": report.upset.recommendation,
                })
                rc.summary = report.summary if report.summary else rc.summary

                result.prediction.evidence.degradation_indicators.append(
                    f"[Draw分析] {da.d_gate_zone} precision={da.d_gate_precision:.0%}"
                )

            result.__dict__['draw_upset_report'] = report.to_dict()

        except ImportError as e:
            logger.warning(f"Draw/Upset 模块缺失: {e}")
        except (ValueError, RuntimeError) as e:
            logger.error(f"平局/冷门分析失败: {e}")
        except Exception as e:
            logger.error(f"Draw分析 意外错误: {e}", exc_info=True)

    # ═══════════════════════════════════════════════════════════
    # P3 自主进化方法
    # ═══════════════════════════════════════════════════════════

    def record_result(self, h_prob: float, d_prob: float, a_prob: float,
                      actual: str):
        """赛后反馈: 记录预测结果到自主优化引擎"""
        try:
            from modules.auto_optimizer import get_optimizer
            opt = get_optimizer()
            opt.record_result(h_prob, d_prob, a_prob, actual)
        except ImportError:
            pass  # P3模块未就绪, 静默跳过
        except Exception as e:
            logger.warning(f"记录预测结果失败: {e}")

    def check_health(self) -> Dict:
        """系统健康检查 (P3)"""
        try:
            from modules.auto_optimizer import get_optimizer
            return get_optimizer().status_summary()
        except ImportError:
            return {"health": "p3_unavailable", "error": "auto_optimizer 模块未就绪"}
        except Exception as e:
            return {"health": "unknown", "error": str(e)}

    def set_feature_baseline(self, stats, importance=None):
        """设置特征基线 (P3)"""
        try:
            from modules.auto_optimizer import get_optimizer
            get_optimizer().set_feature_baseline(stats, importance)
        except ImportError:
            pass  # P3模块未就绪, 静默跳过
        except Exception as e:
            logger.warning(f"设置特征基线失败: {e}")

    # ═══════════════════════════════════════════════════════════
    # P4 增强方法
    # ═══════════════════════════════════════════════════════════

    def ingest_match_result(self, home_team: str, away_team: str, league: str,
                            result: str, spread: float = 0, odds: Dict = None):
        """摄入比赛结果 → 知识库自动更新 (P4)"""
        try:
            from modules.p4_enhancement import get_updater, MatchRecord
            updater = get_updater()
            record = MatchRecord(
                home_team=home_team, away_team=away_team,
                league=league or "未知", result=result,
                spread=spread, odds=odds or {},
                date=datetime.now(timezone.utc).isoformat(),
            )
            knowledge = updater.ingest(record)
            changes = updater.compare_with_baseline()
            if changes["needs_update"]:
                logger.info(f"知识库需更新: {len(changes['changes'])}项变化")
                for s in changes["suggestions"]:
                    logger.info(s)
        except ImportError:
            pass  # P4模块未就绪, 静默跳过
        except Exception as e:
            logger.warning(f"知识库更新失败: {e}")

    def adapt_for_league(self, h_prob: float, d_prob: float, a_prob: float,
                          league: str) -> Tuple[float, float, float, float]:
        """跨联赛概率适配 (P4)"""
        try:
            from modules.p4_enhancement import get_transfer
            return get_transfer().adapt(h_prob, d_prob, a_prob, league)
        except ImportError:
            logger.debug("P4联赛适配器未就绪, 返回原概率")
            return h_prob, d_prob, a_prob, 1.0
        except Exception as e:
            logger.warning(f"联赛适配失败: {e}")
            return h_prob, d_prob, a_prob, 1.0

    def _inject_terminology(self, pred: UnifiedPrediction, mode: str):
        """注入专业术语"""
        # 根据协同模式确定主领域
        mode_domains = {
            "A": "quantization",   # 全栈预测 → 量化术语
            "B": "game_theory",    # 赔率深挖 → 博弈术语
            "C": "imbalance",      # 平局攻坚 → 分类术语
            "D": "math",           # 系统迭代 → 统计术语
        }
        domain = mode_domains.get(mode, "quantization")
        TerminologyInjector.inject(pred.reasoning, domain)

    def _mode_from_string(self, mode_str: str) -> CollaborationMode:
        """字符串 → CollaborationMode"""
        mapping = {
            "A": CollaborationMode.FULL_STACK,
            "B": CollaborationMode.ODDS_DEEP,
            "C": CollaborationMode.DRAW_FOCUS,
            "D": CollaborationMode.SYSTEM_ITERATE,
        }
        return mapping.get(mode_str, CollaborationMode.FULL_STACK)

    # ═══════════════════════════════════════════════════════════
    # 便捷方法
    # ═══════════════════════════════════════════════════════════

    def get_workflow_summary(self, mode: str) -> str:
        """获取协同模式工作流描述"""
        return self.expert_hub.get_workflow_description(
            self._mode_from_string(mode)
        )

    def get_expert_roster(self) -> str:
        """获取专家名册"""
        return describe_experts()

    def get_knowledge_stats(self) -> Dict:
        """获取知识底座统计"""
        return self.knowledge_base.get_stats()

# ═══════════════════════════════════════════════════════════════
# 3. 全局单例
# ═══════════════════════════════════════════════════════════════

_orchestrator_instance: Optional[PredictionOrchestratorV4] = None

def get_orchestrator(**kwargs) -> PredictionOrchestratorV4:
    """获取编排器单例"""
    global _orchestrator_instance
    if _orchestrator_instance is None:
        _orchestrator_instance = PredictionOrchestratorV4(**kwargs)
    return _orchestrator_instance

def reset_orchestrator():
    """重置单例 (测试用)"""
    global _orchestrator_instance
    _orchestrator_instance = None

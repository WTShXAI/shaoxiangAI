"""
哨响AI v4.0 — 6层AI对话引擎 (Six-Layer Conversation Engine)
==============================================================
全自动链路：用户一句话 → 意图识别 → 专家分析 → 操盘解读 → 完整报告

6层架构:
  L1 用户输入层  — 4种入口类型 (预测/赔率分析/庄家意图/综合诊断)
  L2 意图路由层  — 贝叶斯意图识别 → 专家协同模式选择
  L3 专家协同层  — 12人专家团并行分析 (A/B/C/D四模式)
  L4 执行引擎层  — 模型推理 + D-Gate过滤 + PredictionGuard安全检测
  L5 输出呈现层  — 多维度预测报告 + 专业操盘解读
  L6 自主优化层  — 赛后反馈 → 性能追踪 → 自动优化建议

场景验证:
  用户: "这场赔率有问题，庄家在诱盘"
  系统: 意图识别→博弈论专家→赔率逆向→陷阱检测→操盘解读→完整报告

用法:
  python six_layer_conversation.py                    # 交互式对话
  python six_layer_conversation.py --query "..."      # 单次查询
  python six_layer_conversation.py --demo             # 演示模式

作者: Architecture · 2026-06-18
"""
from __future__ import annotations
from utils.constants import DEFAULT_DRAW_PROB
import os, sys, json, logging, re, time, math, argparse
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s - %(message)s',
)
logger = logging.getLogger('SixLayer')

# ── 加载全局配置 ──
try:
    from config.settings import load_config, get_setting
    _cfg = load_config()
    DEFAULT_DRAW_THRESHOLD = get_setting('prediction.draw_threshold', 0.46)
    DEFAULT_HA_GAP = get_setting('prediction.ha_gap', 0.0)
    PURE_V32 = get_setting('global_switches.pure_v32_mode', False)
    logger.info(f"[SixLayer] 配置: Draw阈值={DEFAULT_DRAW_THRESHOLD} 纯净模式={PURE_V32}")
except (ImportError, KeyError, AttributeError) as e:
    logger.warning("加载配置失败, 使用默认值: %s", e)
    DEFAULT_DRAW_THRESHOLD = 0.32
    DEFAULT_HA_GAP = 0.0
    PURE_V32 = False

# ═══════════════════════════════════════════════════════════════
# 1. 核心数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class ConversationContext:
    """对话上下文 — 多轮对话状态管理"""
    current_match: Optional[Dict] = None       # 当前讨论的比赛
    home_team: str = ""
    away_team: str = ""
    league: str = ""
    odds: Dict[str, float] = field(default_factory=dict)
    last_prediction: Optional[Dict] = None     # 上次预测结果
    last_intent: str = ""                      # 上次意图
    history: List[Dict] = field(default_factory=list)  # 对话历史
    feedback_pending: List[Dict] = field(default_factory=list)  # 待反馈记录

    def clear_match(self):
        self.current_match = None
        self.home_team = ""
        self.away_team = ""
        self.league = ""

    def set_match(self, home: str, away: str, league: str = ""):
        self.home_team = home
        self.away_team = away
        self.league = league

    def add_history(self, role: str, content: str):
        self.history.append({
            "role": role, "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        if len(self.history) > 50:
            self.history = self.history[-50:]

@dataclass
class SixLayerResult:
    """6层架构完整输出"""
    # L1-L2 元信息
    user_input: str = ""
    intent_category: str = ""
    intent_subtype: str = ""
    intent_confidence: float = 0.0

    # L3 专家协同
    collaboration_mode: str = ""
    experts_activated: List[str] = field(default_factory=list)

    # L4 模型推理
    prediction_raw: Optional[Dict] = None      # 原始模型输出
    h_prob: float = 0.0
    d_prob: float = 0.0
    a_prob: float = 0.0
    d_gate_result: Optional[str] = None        # D-Gate过滤结果
    guard_status: str = "pass"                 # PredictionGuard状态
    predictions_triple: Dict[str, Dict] = field(default_factory=dict)  # 双线预测: unified/vip

    # L4.5 赔率分析
    odds_analysis: Optional[Dict] = None       # 赔率深度分析
    trap_detection: Optional[Dict] = None      # 陷阱检测

    # v4.2: VIP完整分析
    vip_analysis: Optional[Dict] = None        # VIP数字人+数学融合完整输出

    # L5 输出
    report_type: str = "standard"              # standard / odds_focus / bookmaker_focus
    analysis_report: str = ""                  # 主报告文本
    expert_insights: List[str] = field(default_factory=list)  # 各专家洞察
    recommendation: str = ""                   # 最终建议

    # L6 反馈
    feedback_recorded: bool = False
    optimization_suggestions: List[str] = field(default_factory=list)

    # 元信息
    pipeline_version: str = "v4.0-six-layer"
    total_time_ms: float = 0.0
    errors: List[str] = field(default_factory=list)
    fallback_triggered: bool = False

    def to_dict(self) -> Dict:
        return {
            "user_input": self.user_input,
            "intent": {
                "category": self.intent_category,
                "subtype": self.intent_subtype,
                "confidence": self.intent_confidence,
            },
            "collaboration": {
                "mode": self.collaboration_mode,
                "experts": self.experts_activated,
            },
            "prediction": {
                "home": round(self.h_prob, 4),
                "draw": round(self.d_prob, 4),
                "away": round(self.a_prob, 4),
            } if self.h_prob + self.d_prob + self.a_prob > 0 else None,
            "d_gate": self.d_gate_result,
            "odds_analysis": self.odds_analysis,
            "trap_detection": self.trap_detection,
            "report": self.analysis_report[:500] + "..." if len(self.analysis_report) > 500 else self.analysis_report,
            "recommendation": self.recommendation,
            "experts_insights": self.expert_insights,
            "pipeline": self.pipeline_version,
            "time_ms": round(self.total_time_ms, 1),
            "fallback": self.fallback_triggered,
        }

# ═══════════════════════════════════════════════════════════════
# 2. 6层对话引擎
# ═══════════════════════════════════════════════════════════════

class SixLayerConversationEngine:
    """
    6层AI对话引擎 — FootballAI v4.0 核心交互入口

    能力:
      - 自然语言理解: "这场赔率有问题，庄家在诱盘" → 自动路由到博弈论专家
      - 多轮对话: 上下文保持，可追问、追问细节
      - 全链路输出: 从意图识别到操盘解读一站式
      - 自主优化: 赛后反馈驱动持续改进

    阈值配置 (从回测数据优化):
      - draw_threshold: P(D) > X → 预测平局 (P0判型优化: 0.32, 网格搜索最优)
      - ha_gap: P(H) > P(A) + X → 预测主胜 (默认0)

    杯赛/联赛属性差异 (v4.1):
      - 杯赛平局率系统性高于联赛 (小组赛出线压力 → 保守踢法)
      - 杯赛第一场冷启动: 双方积分0, 无历史对阵参考
      - 杯赛射手榜: 明星球员状态直接影响预期进球
    """

    # 杯赛校准参数 (基于世界杯20场回测)
    TOURNAMENT_LEAGUES = ['世界杯', 'World Cup', '欧洲杯', 'Euro', '亚洲杯', 'Asian Cup',
                          '美洲杯', 'Copa America', '非洲杯', 'AFCON', '欧冠', 'Champions League']
    TOURNAMENT_D_TARGET = 0.375     # 杯赛目标D率 (16场6平=37.5% vs 联赛~25%)
    TOURNAMENT_CONFIDENCE_CUT = 0.85  # 杯赛置信度打折
    COLD_START_ROUNDS = {1}         # 第一轮=冷启动 (无历史积分)

    def __init__(self, model_path: str = None, enable_l6: bool = True,
                 draw_threshold: float = 0.32, ha_gap: float = 0.0):
        """
        Args:
            model_path: 模型文件路径 (默认自动查找)
            enable_l6: 是否启用 L6 自主优化层
            draw_threshold: P(D)阈值 (P0判型优化: 0.46→0.32, MacroF1 0.465→0.507)
            ha_gap: 主客预测gap
        """
        self.model_path = model_path
        self.enable_l6 = enable_l6
        self.draw_threshold = draw_threshold
        self.ha_gap = ha_gap

        # 延迟加载的组件
        self._orchestrator = None
        self._intent_classifier = None
        self._unified_predictor = None
        self._feedback_loop = None
        self._guard = None
        self._knowledge_layer = None
        self._degradation_guard = None

        # 对话状态
        self.context = ConversationContext()

        # 统计数据
        self.stats = {
            "total_queries": 0,
            "total_time_ms": 0.0,
            "by_intent": {},
            "fallback_count": 0,
        }

        logger.info("[SixLayer] 6层对话引擎初始化完成")

    # ═══════════════════════════════════════════════════════════
    # 组件懒加载
    # ═══════════════════════════════════════════════════════════

    @property
    def orchestrator(self):
        if self._orchestrator is None:
            try:
                from modules.prediction_orchestrator_v4 import get_orchestrator
                self._orchestrator = get_orchestrator()
                logger.info("[SixLayer] PredictionOrchestratorV4 加载成功")
            except Exception as e:
                logger.warning(f"[SixLayer] Orchestrator 加载失败: {e}")
                self._orchestrator = None
        return self._orchestrator

    @property
    def intent_classifier(self):
        if self._intent_classifier is None:
            try:
                from modules.intent_classifier_v2 import IntentClassifierV2
                self._intent_classifier = IntentClassifierV2()
                logger.info("[SixLayer] IntentClassifierV2 加载成功")
            except Exception as e:
                logger.warning(f"[SixLayer] IntentClassifier 加载失败: {e}")
                self._intent_classifier = None
        return self._intent_classifier

    @property
    def unified_predictor(self):
        if self._unified_predictor is None:
            try:
                from predictors.unified_predictor import get_unified_predictor
                self._unified_predictor = get_unified_predictor()
                logger.info("[SixLayer] UnifiedPredictor 加载成功")
            except Exception as e:
                logger.warning(f"[SixLayer] UnifiedPredictor 加载失败: {e}")
                self._unified_predictor = None
        return self._unified_predictor

    @property
    def feedback_loop(self):
        if self._feedback_loop is None and self.enable_l6:
            try:
                from modules.feedback_loop import FeedbackLoop
                self._feedback_loop = FeedbackLoop()
                logger.info("[SixLayer] FeedbackLoop 加载成功")
            except Exception as e:
                logger.warning(f"[SixLayer] FeedbackLoop 加载失败: {e}")
                self._feedback_loop = None
        return self._feedback_loop

    @property
    def guard(self):
        if self._guard is None:
            try:
                from predictors.prediction_guard import PredictionGuard
                self._guard = PredictionGuard()
                logger.info("[SixLayer] PredictionGuard 加载成功")
            except Exception as e:
                logger.warning(f"[SixLayer] PredictionGuard 加载失败: {e}")
                self._guard = None
        return self._guard

    @property
    def knowledge_layer(self):
        """L0 知识记忆层 — 统一知识检索"""
        if self._knowledge_layer is None:
            try:
                # 确保 modules/ 可导入
                modules_path = str(PROJECT_ROOT)

                import modules.knowledge_layer as _kl
                self._knowledge_layer = _kl.KnowledgeLayer()
                logger.info("[SixLayer] L0知识记忆层 加载成功")
            except Exception as e:
                logger.warning(f"[SixLayer] L0知识层加载失败: {e}")
                self._knowledge_layer = None
        return self._knowledge_layer

    @property
    def risk_barrier(self):
        """L4 庄家风控防线引擎"""
        if getattr(self, '_risk_barrier', None) is None:
            try:
                # 多种路径尝试
                import importlib
                _tried = False
                for path in [str(PROJECT_ROOT), str(PROJECT_ROOT / 'bookmaker_sim'), '.']:

                try:
                    import bookmaker_sim.risk_barrier_engine as _rbe
                    self._risk_barrier = _rbe.RiskBarrierEngine()
                    _tried = True
                except (ImportError, AttributeError):
                    pass
                if not _tried:
                    # 回退: 直接用exec加载
                    _rbe = importlib.import_module('bookmaker_sim.risk_barrier_engine')
                    self._risk_barrier = _rbe.RiskBarrierEngine()
                logger.info("[SixLayer] 风控防线引擎 加载成功")
            except Exception as e:
                logger.debug(f"[SixLayer] 风控防线引擎加载失败: {e}")
                self._risk_barrier = None
        return self._risk_barrier

    @property
    def degradation_guard(self):
        """容错降级守护 — 三级回退链"""
        if self._degradation_guard is None:
            try:
                import modules.degradation_guard as _dg
                self._degradation_guard = _dg.DegradationGuard()
                logger.info("[SixLayer] 容错降级守护 加载成功")
            except Exception as e:
                logger.warning(f"[SixLayer] 容错守护加载失败: {e}")
                self._degradation_guard = None
        return self._degradation_guard

    @property
    def vip_predictor(self):
        """VIP Final v1.1 — 数字人+数学融合"""
        if getattr(self, '_vip', None) is None:
            try:
                from predictors.vip.vip_final import VIPFinalPredictor
                self._vip = VIPFinalPredictor()
                logger.info("[SixLayer] VIP Final v1.1 加载成功")
            except Exception as e:
                logger.warning(f"[SixLayer] VIP Final 加载失败: {e}")
                self._vip = None
        return self._vip

    # ═══════════════════════════════════════════════════════════
    # 主入口: 处理用户输入
    # ═══════════════════════════════════════════════════════════

    def process(self, user_input: str,
                home_team: str = None, away_team: str = None,
                league: str = None, odds: Dict[str, float] = None) -> SixLayerResult:
        """
        处理用户输入 — 完整的6层链路

        用户说一句 → 自动走完6层 → 返回完整报告

        Args:
            user_input: 用户自然语言
            home_team: 主队名 (可从上下文获取)
            away_team: 客队名
            league: 联赛名
            odds: 赔率字典 {home, draw, away}

        Returns:
            SixLayerResult: 完整6层输出
        """
        start_time = time.perf_counter()
        result = SixLayerResult(user_input=user_input)

        # ── P0: 创建统一上下文 (全链路唯一数据载体) ──
        try:
            from core.context import MatchContext
            ctx = MatchContext(
                user_input=user_input,
                home_team=home_team or self.context.home_team or "",
                away_team=away_team or self.context.away_team or "",
                league=league or self.context.league or "",
                matchday=1)
            if odds: ctx.odds_1x2 = odds
            ctx.start_time = start_time
            result.predictions_triple['_ctx'] = ctx
        except Exception as e:
            logger.debug("创建MatchContext失败: %s", e)
            ctx = None  # 兼容: 不在FastAPI上下文中运行时不创建MatchContext
        self.stats["total_queries"] += 1

        # 上下文继承 (仅当无新数据时才沿用旧上下文)
        # 关键修复: 有新赔率 + 主客队空 → 禁止继承, 防止"A队赔率+B队队名"错配
        if odds and not home_team:
            logger.warning(f"[SixLayer] 赔率已提供但主队名为空, 禁用上下文继承")
            home_team = "?"
            away_team = "?"
        else:
            home_team = home_team or self.context.home_team
            away_team = away_team or self.context.away_team
        league = league or self.context.league

        try:
            # ─────────── 纯净模式: 快速路径 ───────────
            if PURE_V32:
                result.intent_category = "predict"
                result.intent_subtype = "match_result"
                result.collaboration_mode = "v3.2"
                result.experts_activated = ["v3.2基线(纯净模式)"]
                if odds:
                    oh, od_, oa = odds.get('home',2.5), odds.get('draw',3.2), odds.get('away',2.8)
                    inv = 1/oh + 1/od_ + 1/oa
                    result.h_prob = (1/oh)/inv
                    result.d_prob = (1/od_)/inv
                    result.a_prob = (1/oa)/inv
                else:
                    result.h_prob = result.d_prob = result.a_prob = 0.333
                result.analysis_report = (
                    f"🔵 v3.2 纯净模式\n\n"
                    f"赔率反推: H={result.h_prob:.1%} D={result.d_prob:.1%} A={result.a_prob:.1%}\n"
                    f"所有扩展功能已关闭, 使用v3.2基线。\n"
                    f"如需启用6层引擎, 修改 config/settings.yaml: pure_v32_mode: false"
                )
                return result

            # ─────────── L0: 知识记忆层查询 ───────────
            l0_ctx = None
            if self.knowledge_layer:
                l0_ctx = self.knowledge_layer.consult(
                    home=home_team or "", away=away_team or "",
                    odds=odds, league=league, intent=""
                )

            # ─────────── L2: 意图识别与路由 ───────────
            intent_result = self._classify_intent(user_input)
            result.intent_category = intent_result["category"]
            result.intent_subtype = intent_result["subtype"]
            result.intent_confidence = intent_result["confidence"]
            self.context.last_intent = result.intent_category

            # ── P0: 同步到上下文 ──
            if ctx:
                ctx.set_intent(result.intent_category, result.intent_subtype, result.intent_confidence)
                ctx.add_trace('L2', '意图路由', f'{result.intent_category}/{result.intent_subtype}')

            # L0反馈: 用知识修正意图置信度
            if l0_ctx and l0_ctx.loaded and result.intent_confidence > 0.7:
                # 杯赛+极热门 → 降低预测置信度
                if l0_ctx.league_draw_rate > 0.30 and result.intent_category == "predict":
                    result.intent_confidence *= 0.90
                    result.expert_insights.append(
                        "[L0知识层] 杯赛高D率+预测意图 → 置信度×0.90")

            # 意图统计
            cat = result.intent_category
            self.stats["by_intent"][cat] = self.stats["by_intent"].get(cat, 0) + 1

            # 提取比赛信息 (如果输入中包含)
            teams = self._extract_teams_from_input(user_input)
            if teams["home"] or teams["away"]:
                home_team = teams["home"] or home_team
                away_team = teams["away"] or away_team
                if home_team and away_team:
                    self.context.set_match(home_team, away_team, league)

            # ─────────── L3: 确定协同模式 + 专家调度 ───────────
            mode = self._determine_collaboration_mode(result.intent_category, result.intent_subtype)
            result.collaboration_mode = mode
            result.experts_activated = self._get_experts_for_mode(mode, result.intent_category)

            # ─────────── L4: 根据意图执行 ───────────
            if result.intent_category == "predict":
                self._execute_prediction(result, home_team, away_team, league, odds)
            elif result.intent_category == "analyze" and result.intent_subtype == "odds":
                self._execute_odds_analysis(result, home_team, away_team, league, odds)
            elif result.intent_category == "analyze" and "bookmaker" in result.intent_subtype:
                self._execute_bookmaker_analysis(result, home_team, away_team, league, odds)
            elif result.intent_category == "analyze":
                self._execute_comprehensive_analysis(result, home_team, away_team, league, odds)
            elif result.intent_category == "explain":
                self._execute_explanation(result, user_input, home_team, away_team)
            elif result.intent_category == "backtest":
                self._execute_backtest_query(result, user_input)
            elif result.intent_category == "simulate":
                self._execute_balance_simulation(result, home_team or "巴西", away_team or "阿根廷", odds)
            else:
                # 通用处理
                self._execute_general_query(result, user_input)

            # ─────────── L5: 生成报告 ───────────
            # ── P0: 同步最终结果到上下文 ──
            if ctx:
                ctx.set_prediction(result.h_prob, result.d_prob, result.a_prob)
                ctx.d_gate_result = result.d_gate_result or ""
                ctx.add_trace('L4', '预测完成', f'H={result.h_prob:.0%} D={result.d_prob:.0%} A={result.a_prob:.0%}')
                ctx.record_layer_time('L4')

            self._generate_report(result, home_team, away_team, league)

            # ─────────── L6: 记录反馈 ───────────
            if self.enable_l6 and self.feedback_loop:
                try:
                    self.feedback_loop.record_query(
                        user_input=user_input,
                        intent=result.intent_category,
                        prediction={"H": result.h_prob, "D": result.d_prob, "A": result.a_prob},
                        actual=None  # 赛前无实际结果
                    )
                    result.feedback_recorded = True
                except Exception as e:
                    logger.debug(f"L6反馈记录失败: {e}")

        except Exception as e:
            logger.error(f"[SixLayer] 处理异常: {e}", exc_info=True)
            result.fallback_triggered = True
            result.errors.append(str(e))
            result.analysis_report = f"处理异常: {e}\n\n请检查系统日志或重新输入。"

        result.total_time_ms = (time.perf_counter() - start_time) * 1000
        self.stats["total_time_ms"] += result.total_time_ms
        if result.fallback_triggered:
            self.stats["fallback_count"] += 1

        # 保存上下文
        if result.h_prob > 0:
            self.context.last_prediction = {
                "H": result.h_prob, "D": result.d_prob, "A": result.a_prob
            }

        return result

    # ═══════════════════════════════════════════════════════════
    # L2: 意图分类
    # ═══════════════════════════════════════════════════════════

    def _classify_intent(self, user_input: str) -> Dict:
        """意图分类 — 使用 IntentClassifierV2 + 规则增强"""
        result = {
            "category": "analyze",
            "subtype": "general",
            "confidence": 0.5,
        }

        # 规则快速匹配 (高优先级关键词)
        input_lower = user_input.lower()

        # ── 0. 隐含意图: "队名 vs 队名 + 赔率数字" → 预测意图 ──
        has_vs = bool(re.search(r'.+?\s+(?:vs|VS|对|vs\.)\s+.+', user_input.strip()))
        odds_count = len(re.findall(r'\d+\.\d{2}', user_input))
        if has_vs and odds_count >= 2:
            result["category"] = "predict"
            result["subtype"] = "match_result"
            result["confidence"] = 0.72
            return result

        # 庄家意图/诱盘关键词 (最高优先级)
        bookmaker_keywords = [
            "庄家", "诱盘", "收割", "陷阱", "操盘", "杀猪", "抽水",
            "bookmaker", "trap", "harvesting", "manipulate",
            "庄家想", "博彩公司", "盘口有鬼", "赔率异常",
            "诱盘手法", "水位异常", "对冲",
            "平衡窗口", "平衡操盘", "平衡赔率", "平衡模拟", "操盘手模拟",  # ← 平衡模拟
        ]
        if any(kw in input_lower for kw in bookmaker_keywords):
            # 区分: 平衡模拟 vs 诱盘检测
            if any(kw in input_lower for kw in ["平衡窗口", "平衡操盘", "平衡赔率", "平衡模拟", "操盘手模拟"]):
                result["category"] = "simulate"
                result["subtype"] = "balance_window"
                result["confidence"] = 0.90
            else:
                result["category"] = "analyze"
                result["subtype"] = "bookmaker_intent"
                result["confidence"] = 0.85
            return result

        # 解释关键词 (必须在赔率之前，避免"凯利怎么算"被误判为赔率)
        explain_keywords = ["怎么算", "什么意思", "解释", "什么是", "定义", "概念", "term"]
        if any(kw in input_lower for kw in explain_keywords):
            result["category"] = "explain"
            result["subtype"] = "terminology"
            result["confidence"] = 0.85
            return result

        # 赔率分析关键词
        odds_keywords = [
            "赔率", "水位", "盘口", "让球", "大小球",
            "spread", "overround", "隐含概率", "返赔率",
            "赔率变了", "水位变化", "临场赔率",
            "凯利指数",  # 不含"怎么算"时才触发(已被上面拦截)
        ]
        if any(kw in input_lower for kw in odds_keywords):
            result["category"] = "analyze"
            result["subtype"] = "odds"
            result["confidence"] = 0.8
            return result

        # 预测关键词
        predict_keywords = [
            "预测", "谁赢", "怎么看", "结果", "胜平负",
            "推荐", "买", "下注", "波胆", "比分预测",
            "会赢", "会输", "会平", "怎么样",
            "内战", "德比", "平局概率", "平局可能", "爆冷",  # ← 场景化预测
        ]
        if any(kw in input_lower for kw in predict_keywords):
            result["category"] = "predict"
            result["subtype"] = "match_result"
            result["confidence"] = 0.8
            return result

        # 复盘关键词
        review_keywords = ["复盘", "为什么错", "漏了什么", "回顾", "分析下这场"]
        if any(kw in input_lower for kw in review_keywords):
            result["category"] = "backtest"
            result["subtype"] = "review"
            result["confidence"] = 0.75
            return result

        # 解释关键词
        explain_keywords = ["什么意思", "怎么算", "解释", "什么是", "定义", "概念"]
        if any(kw in input_lower for kw in explain_keywords):
            result["category"] = "explain"
            result["subtype"] = "terminology"
            result["confidence"] = 0.75
            return result

        # 尝试使用 IntentClassifierV2
        try:
            if self.intent_classifier:
                cls_result = self.intent_classifier.classify(user_input)
                if cls_result and hasattr(cls_result, 'intent_category') and cls_result.intent_category:
                    result["category"] = cls_result.intent_category
                    result["subtype"] = cls_result.intent_subtype or "general"
                    result["confidence"] = cls_result.confidence or 0.5
                    return result
        except (AttributeError, TypeError) as e:
            logger.debug("IntentClassifierV2失败: %s", e)

        # 兜底: 尝试 bayesian_commander
        try:
            from bookmaker_sim.bayesian_commander import BayesianCommander
            bc = BayesianCommander()
            bc_result = bc.classify(user_input)
            if bc_result and bc_result.get("code"):
                # 映射到 v2 意图
                code_map = {
                    "PREDICT": "predict",
                    "ODDS_ANALYSIS": "analyze",
                    "BOOKMAKER_INTENT": "analyze",
                    "RISK_ASSESS": "analyze",
                    "COMPARE": "analyze",
                    "REVIEW": "backtest",
                    "DATA_QUERY": "analyze",
                    "STRATEGY": "analyze",
                }
                result["category"] = code_map.get(bc_result["code"], "analyze")
                result["subtype"] = bc_result.get("code", "").lower()
                result["confidence"] = bc_result.get("confidence", 0.5)
                return result
        except Exception as e:
            logger.debug("BayesianCommander分类失败: %s", e)

        return result

    def _extract_teams_from_input(self, user_input: str) -> Dict[str, str]:
        """从用户输入中提取球队信息"""
        result = {"home": "", "away": ""}

        # 常见模式: "A vs B", "A对B", "A和B"
        patterns = [
            r'(.+?)\s+vs\s+(.+?)(?:\s|$|，|,)',
            r'(.+?)\s+对\s+(.+?)(?:\s|$|，|,)',
            r'(.+?)\s+和\s+(.+?)(?:的|比赛|这场)',
            r'(.+?)对(.+?)(?:的|比赛|这场)',
        ]

        for pattern in patterns:
            match = re.search(pattern, user_input, re.IGNORECASE)
            if match:
                h = match.group(1).strip()
                a = match.group(2).strip()
                # 清除尾部干扰词
                noise_words = ['谁赢', '怎么看', '预测', '怎么样', '会赢', '会输', '会平', '分析',
                              '主场', '客场', '什么', '如何', '好不好']
                for nw in noise_words:
                    a = re.sub(rf'\s*{nw}\s*$', '', a)
                result["home"] = h
                result["away"] = a
                return result

        # 单队名提取 (常见队名)
        known_teams = [
            "巴西", "阿根廷", "德国", "法国", "西班牙", "英格兰", "意大利",
            "荷兰", "葡萄牙", "比利时", "克罗地亚", "乌拉圭", "墨西哥",
            "美国", "日本", "韩国", "摩洛哥", "塞内加尔", "加纳",
            "喀麦隆", "尼日利亚", "埃及", "突尼斯", "澳大利亚",
            "皇家马德里", "巴塞罗那", "拜仁", "曼城", "利物浦",
        ]
        found = []
        for team in known_teams:
            if team in user_input:
                found.append(team)
        if len(found) >= 2:
            result["home"] = found[0]
            result["away"] = found[1]
        elif len(found) == 1:
            result["home"] = found[0]

        return result

    # ═══════════════════════════════════════════════════════════
    # L3: 协同模式与专家调度
    # ═══════════════════════════════════════════════════════════

    def _determine_collaboration_mode(self, intent_category: str, intent_subtype: str) -> str:
        """根据意图确定协同模式"""
        if intent_category == "predict":
            return "A"  # 全栈预测
        elif intent_subtype == "odds":
            return "B"  # 赔率深挖
        elif "bookmaker" in intent_subtype:
            return "B"  # 庄家意图 → 赔率深挖
        elif intent_subtype in ("draw", "upset"):
            return "C"  # 平局/冷门攻坚
        elif intent_category == "backtest":
            return "D"  # 系统迭代
        else:
            return "A"  # 默认全栈

    def _get_experts_for_mode(self, mode: str, intent: str) -> List[str]:
        """获取该模式下应激活的专家列表"""
        experts_map = {
            "A": ["郝优算(总工)", "季泊松(量化)", "杜博弈(博弈)", "荣合众(集成)",
                  "曾均衡(平局)", "毕建模(数学)", "施时序(时序)"],
            "B": ["杜博弈(博弈·主导)", "季泊松(量化)", "毕建模(数学)", "郝优算(总工)"],
            "C": ["曾均衡(平局·主导)", "季泊松(量化)", "荣合众(集成)", "郝优算(总工)"],
            "D": ["郝优算(总工·主导)", "荣合众(集成)", "毕建模(数学)", "齐优化(优化)"],
        }
        return experts_map.get(mode, experts_map["A"])

    # ═══════════════════════════════════════════════════════════
    # L4: 执行引擎 — 各意图具体执行
    # ═══════════════════════════════════════════════════════════

    def _execute_prediction(self, result: SixLayerResult,
                            home: str, away: str, league: str,
                            odds: Dict[str, float]):
        """执行预测 — 双线并行: UnifiedPredictor + VIP"""
        probs = None
        triple = {}  # {name: {H, D, A, prediction, confidence}}

        oh = odds.get("home", 0) if odds else 0
        od = odds.get("draw", 0) if odds else 0
        oa = odds.get("away", 0) if odds else 0
        handicap = odds.get("_handicap", 0.0) if odds else 0.0
        ou = odds.get("_ou_line", 2.5) if odds else 2.5
        ow = odds.get("_over_water", 1.90) if odds else 1.90
        uw = odds.get("_under_water", 1.92) if odds else 1.92

        # ── 线1: UnifiedPredictor v4.1 (主模型) ──
        if self.unified_predictor and home and away:
            try:
                pred = self.unified_predictor.predict(
                    home=home, away=away, odds_h=oh, odds_d=od, odds_a=oa,
                    asian_handicap=handicap, ou_line=ou,
                    over_water=ow, under_water=uw,
                )
                if pred and "probabilities" in pred:
                    p = pred["probabilities"]
                    # v4.7: 兼容大写键(H/D/A)和小写键(home/draw/away)
                    hp_val = p.get("home", p.get("H", 0.33))
                    dp_val = p.get("draw", p.get("D", 0.33))
                    ap_val = p.get("away", p.get("A", 0.33))
                    uc = pred.get("confidence", 0)
                    top = max(p, key=p.get) if p else "H"
                    triple["unified"] = {
                        "name": "Unified v4.1", "H": hp_val, "D": dp_val,
                        "A": ap_val, "prediction": top, "confidence": uc,
                    }
                    # 主通道概率
                    result.h_prob = hp_val
                    result.d_prob = dp_val
                    result.a_prob = ap_val
                    result.prediction_raw = pred
                    probs = (result.h_prob, result.d_prob, result.a_prob)
                    logger.info(f"[SixLayer] Unified: H={result.h_prob:.3f} D={result.d_prob:.3f} A={result.a_prob:.3f} → {top}")
            except Exception as e:
                logger.warning(f"[SixLayer] UnifiedPredictor 失败: {e}")

        # ── 线2: VIP Final (数字人+数学融合) ──
        if self.vip_predictor and home and away:
            try:
                vip_match = {
                    "home": home, "away": away, "league": league or "未知",
                    "odds_h": oh, "odds_d": od, "odds_a": oa,
                    "asian_handicap": handicap, "ou_line": ou,
                    "over_water": ow, "under_water": uw,
                }
                # 传递半场赔率 (如有)
                ht_odds = odds.get("_ht_odds", {}) if odds else {}
                if ht_odds:
                    vip_match["ht_home_odds"] = ht_odds.get("home", ht_odds.get("H"))
                    vip_match["ht_draw_odds"] = ht_odds.get("draw", ht_odds.get("D"))
                    vip_match["ht_away_odds"] = ht_odds.get("away", ht_odds.get("A"))
                
                vip_result = self.vip_predictor.predict(match=vip_match)
                if vip_result:
                    # 概率
                    vp = vip_result.get("probabilities") or vip_result.get("probs", {})
                    vc = vip_result.get("confidence", 0)
                    top_v = max(vp, key=vp.get) if vp else "?"
                    triple["vip"] = {
                        "name": "VIP Final v1.1", "H": vp.get("home", vp.get("H", 0)),
                        "D": vp.get("draw", vp.get("D", 0)),
                        "A": vp.get("away", vp.get("A", 0)),
                        "prediction": top_v, "confidence": vc,
                    }
                    # v4.2: 存储VIP完整分析
                    result.vip_analysis = {
                        "dh_probs": vip_result.get("dh_probs", {}),
                        "math_probs": vip_result.get("math_probs", {}),
                        "scores": vip_result.get("scores", vip_result.get("all_scores", [])),
                        "recommendation": vip_result.get("recommendation", ""),
                        "bookmaker_view": vip_result.get("bookmaker_view", ""),
                        "trap": vip_result.get("trap", {}),
                        "math_fusion_λ": vip_result.get("math_fusion_λ", {}),
                        "dh_analysis": vip_result.get("dh_analysis", ""),
                    }
                    logger.info(f"[SixLayer] VIP: {vp.get('home',0):.3f}/{vp.get('draw',0):.3f}/{vp.get('away',0):.3f} → {top_v}")
            except Exception as e:
                logger.warning(f"[SixLayer] VIP Predictor 失败: {e}")

        result.predictions_triple = triple

        # 降级: 从赔率反推 (模型冷启动或概率平坦时)
        if probs is None or (abs(result.h_prob - 0.33) < 0.02 and abs(result.d_prob - 0.33) < 0.02):
            if odds and oh > 1.0:
                inv_sum = 1.0/oh + 1.0/od + 1.0/oa
                h = (1.0/oh) / inv_sum
                d = (1.0/od) / inv_sum
                a = (1.0/oa) / inv_sum

                # 杯赛校准 (世界杯/欧洲杯等)
                cal_h, cal_d, cal_a, cal_info = self._apply_tournament_calibration(
                    h, d, a, league, matchday=1, home_team=home, away_team=away)
                if cal_info:
                    logger.info(f"[SixLayer] 杯赛校准: {cal_info}")
                    result.h_prob = cal_h
                    result.d_prob = cal_d
                    result.a_prob = cal_a
                    if 'tournament_d_calib' in cal_info:
                        result.expert_insights.append(
                            f"[曾均衡] 杯赛D修正: 目标{self.TOURNAMENT_D_TARGET:.0%} "
                            f"(16场小组赛6平=37.5% | 联赛~25%)")
                    if 'cold_start' in cal_info:
                        result.expert_insights.append(
                            f"[施时序] 杯赛冷启动R1: 无历史积分, 向均匀分布混合")
                    if 'scorer_boost' in cal_info:
                        result.expert_insights.append(
                            f"[杜博弈] 射手榜: {cal_info.get('scorer_h', '')} | {cal_info.get('scorer_a', '')}")
                    elif 'scorer_note' in cal_info:
                        result.expert_insights.append(
                            f"[杜博弈] 射手榜: {cal_info['scorer_note']}")
                else:
                    result.h_prob = h
                    result.d_prob = d
                    result.a_prob = a
                probs = (result.h_prob, result.d_prob, result.a_prob)
                logger.info(f"[SixLayer] 赔率反推(冷启动降级): H={result.h_prob:.3f} D={result.d_prob:.3f} A={result.a_prob:.3f}")

        # D-Gate 过滤 (内部原始margin评估, 覆盖状态由调用方main.py追加)
        if result.d_prob > 0:
            result.d_gate_result = self._apply_d_gate(
                result.h_prob, result.d_prob, result.a_prob
            )

        # PredictionGuard
        if self.guard:
            try:
                guard_input = {
                    "prediction": {"home": result.h_prob, "draw": result.d_prob, "away": result.a_prob},
                }
                guard_result = self.guard.validate(guard_input)
                if guard_result and not guard_result.get("passed", True):
                    result.guard_status = "blocked"
                    result.errors.append(f"Guard: {guard_result.get('reason', '未知')}")
            except Exception as e:
                logger.debug("Guard执行失败: %s", e)

    def _execute_odds_analysis(self, result: SixLayerResult,
                               home: str, away: str, league: str,
                               odds: Dict[str, float]):
        """执行赔率分析 (模式B)"""
        if not odds:
            result.analysis_report = "需要提供赔率数据才能进行赔率分析。请提供主胜/平局/客胜赔率。"
            return

        # 使用 PredictionOrchestratorV4 的模式B
        if self.orchestrator and home and away:
            try:
                orch_result = self.orchestrator.predict_structured(
                    home_team=home, away_team=away, league=league,
                    odds=odds, expert_mode="B",
                )
                if orch_result and orch_result.prediction:
                    p = orch_result.prediction.probability
                    result.h_prob = p.home
                    result.d_prob = p.draw
                    result.a_prob = p.away
            except Exception as e:
                logger.warning(f"[SixLayer] Orchestrator模式B失败: {e}")

        # 调用 OddsDeepAnalyzer
        try:
            from modules.odds_deep_analyzer import get_odds_analyzer
            analyzer = get_odds_analyzer()
            report = analyzer.analyze(home, away, odds, league)
            result.odds_analysis = report.to_dict()
            logger.info(f"[SixLayer] 赔率深度分析完成: 风险={report.overall_risk}")
        except Exception as e:
            logger.warning(f"[SixLayer] OddsDeepAnalyzer失败: {e}")
            result.odds_analysis = {"error": str(e)}

    def _execute_bookmaker_analysis(self, result: SixLayerResult,
                                    home: str, away: str, league: str,
                                    odds: Dict[str, float]):
        """执行庄家意图分析 — 核心场景: "这场赔率有问题，庄家在诱盘" """
        # 先做赔率分析
        self._execute_odds_analysis(result, home, away, league, odds)

        # 从赔率分析结果中提取陷阱检测 (OddsDeepReport 已包含)
        if result.odds_analysis and "error" not in result.odds_analysis:
            td = result.odds_analysis.get("trap_detection")
            if td and isinstance(td, dict):
                result.trap_detection = td

        # 额外: 庄家风控防线扫描
        if self.risk_barrier and odds:
            try:
                open_odds = {'home': oh, 'draw': od, 'away': oa}
                barrier_report = self.risk_barrier.scan(
                    home=home, away=away, odds_1x2=odds, league=league,
                    odds_open=open_odds)
                result.expert_insights.append(
                    f"[杜博弈·风控] {barrier_report.summary}")
                # 存储到result供报告使用
                result.predictions_triple['_barrier'] = {
                    'risk_score': barrier_report.risk_score,
                    'risk_label': barrier_report.risk_label,
                    'barrier_level': barrier_report.barrier_level,
                    'intent': barrier_report.bookmaker_intent.value,
                    'd_adj': barrier_report.d_prob_adjust,
                    'conf_mult': barrier_report.confidence_multiplier,
                }
            except Exception as e:
                logger.debug(f"风控防线扫描失败: {e}")

        # 额外: 贝叶斯赔率逆推
        if odds:
            try:
                from bookmaker_sim.bayesian_odds_inverter import BayesianOddsInverter
                inverter = BayesianOddsInverter()
                lambda_result = inverter.invert(home, away, odds)
                if lambda_result and "lambda_h" in lambda_result:
                    lh = lambda_result.get('lambda_h', 0)
                    la = lambda_result.get('lambda_a', 0)
                    result.expert_insights.append(
                        f"[季泊松] 泊松反推: λ_H={lh:.2f}, λ_A={la:.2f}"
                    )
            except Exception as e:
                logger.debug("泊松反推失败: %s", e)

        # 格式化庄家信号 (OddsDeepReport.to_dict() 中 bookmaker_assessment 是 dict)
        if result.odds_analysis:
            ba = result.odds_analysis.get("bookmaker_assessment")
            if isinstance(ba, dict):
                result.odds_analysis["bookmaker_signal"] = ba.get("signal", "")
                result.odds_analysis["bookmaker_confidence"] = ba.get("confidence", "normal")

    def _execute_comprehensive_analysis(self, result: SixLayerResult,
                                        home: str, away: str, league: str,
                                        odds: Dict[str, float]):
        """执行综合分析 — 全栈模式A"""
        # 先预测
        self._execute_prediction(result, home, away, league, odds)
        # 再分析赔率
        if odds:
            self._execute_odds_analysis(result, home, away, league, odds)

    def _execute_explanation(self, result: SixLayerResult,
                             user_input: str, home: str, away: str):
        """处理解释类查询"""
        # 足球术语知识库
        term_explanations = {
            "凯利": (
                "凯利指数 (Kelly Criterion)\n\n"
                "公式: f* = (bp - q) / b\n"
                "  其中 b=赔率-1, p=胜率, q=1-p\n"
                "简化版: KI = (隐含概率 × 赔率 - 1) / (赔率 - 1)\n\n"
                "含义: 衡量投注的正期望值\n"
                "  KI > 0: 正期望，有投注价值\n"
                "  KI < 0: 负期望，庄家优势\n"
                "应用: 用于资金管理，不是预测工具"
            ),
            "抽水": (
                "抽水率 (Overround / Margin)\n\n"
                "公式: margin = 1/odds_H + 1/odds_D + 1/odds_A - 1\n\n"
                "含义: 庄家的理论利润率\n"
                "  典型值: 5-8% (主流联赛)\n"
                "  偏高(>10%): 市场不活跃\n"
                "  偏低(<3%): 竞品激烈或信息充分\n"
                "逆向: 通过抽水分解反推庄家真实概率估计"
            ),
            "诱盘": (
                "诱盘 (Trap / 诱使投注)\n\n"
                "定义: 庄家通过调整赔率引导资金流向的行为\n\n"
                "特征:\n"
                "  1. 赔率异常偏离市场共识\n"
                "  2. 早盘与临盘水位大幅波动\n"
                "  3. 抽水率异常升高(诱)或降低(吸)\n"
                "  4. 多个庄家赔率分歧加大\n\n"
                "检测: 16引擎陷阱检测系统 (R-Barrier/R-Vol/R-Water/...) "
                "需要开盘→收盘赔率变化数据"
            ),
            "D-Gate": (
                "D-Gate (平局检测门控)\n\n"
                "定义: 精度过滤机制，减少假平局预测\n\n"
                "等级:\n"
                "  P(D) margin < 0.02 → 垃圾区(建议降级)\n"
                "  P(D) margin < 0.05 → 模糊区(需谨慎)\n"
                "  P(D) margin < 0.08 → 可用区(可参考)\n"
                "  P(D) margin > 0.20 → 高置信区(强D信号)\n\n"
                "原理: 平局预测易过拟合，通过margin过滤提升精确率"
            ),
            "spread": (
                "Spread (实力差距)\n\n"
                "公式: spread = λ_H - λ_A  (泊松强度差)\n"
                "反推: spread ≈ 1/P(H) - 1/P(A) (简化)\n\n"
                "含义: 两队实力量化差距\n"
                "  spread > 1.5: 强队明显优势\n"
                "  spread < 0.3: 实力接近\n"
                "应用: 交叉验证庄家赔率与真实实力"
            ),
        }
        # 通用正则可触发多个术语
        found_terms = []
        for term, explanation in term_explanations.items():
            if term in user_input:
                found_terms.append((term, explanation))
        
        if found_terms:
            lines = [f"## 📖 术语解释\n"]
            for term, explanation in found_terms:
                lines.append(explanation)
                lines.append("")  # 空行分隔
            result.analysis_report = "\n".join(lines)
        else:
            result.analysis_report = (
                f"## 📖 术语查询\n\n"
                f"关于「{user_input[:30]}...」\n\n"
                f"可用术语库:\n"
                f"• 凯利指数 • 抽水率 • 诱盘 • D-Gate • Spread\n"
                f"• 隐含概率 • 泊松强度 • 让球 • 大小球\n"
                f"• λ融合 • 陷阱检测 • 贝叶斯校准\n\n"
                f"输入具体术语获取详细解释。"
            )

    def _execute_backtest_query(self, result: SixLayerResult, user_input: str):
        """处理回测/复盘类查询"""
        result.analysis_report = (
            "## 回测查询\n\n"
            "回测功能需要提供具体比赛信息。\n"
            "用法示例:\n"
            "- '回测巴西对阿根廷' — 查看历史交锋回测\n"
            "- '复盘昨天那场' — 对最近一场比赛进行复盘\n"
            "- '我的预测准确率' — 查看L6记录的预测统计\n"
        )

    def _execute_balance_simulation(self, result: SixLayerResult,
                                    home: str, away: str,
                                    odds: Dict[str, float] = None):
        """执行操盘手平衡模拟 — 演示庄家如何动态调盘保证利润"""
        try:
            # 直接文件导入, 避免 package cache 问题
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                'balance_simulator',
                str(PROJECT_ROOT / 'bookmaker_sim' / 'balance_simulator.py')
            )
            bsm = importlib.util.module_from_spec(spec)
            sys.modules['balance_simulator'] = bsm
            spec.loader.exec_module(bsm)
            BookmakerBalanceSimulator = bsm.BookmakerBalanceSimulator

            # 从赔率反推泊松强度
            lambda_h = 1.60
            lambda_a = 1.10
            if odds:
                oh = odds.get("home", 2.0)
                od = odds.get("draw", 3.3)
                oa = odds.get("away", 3.6)
                # 粗略反推: λ ≈ 1/(odds - 1) * 修正因子
                inv_sum = 1.0/oh + 1.0/od + 1.0/oa
                p_h = (1.0/oh) / inv_sum
                p_a = (1.0/oa) / inv_sum
                # 从概率反推泊松强度 (近似)
                if p_h > 0 and p_a > 0:
                    lambda_h = max(0.5, -math.log(1 - p_h) * 1.8)
                    lambda_a = max(0.3, -math.log(1 - p_a) * 1.8)

            sim = BookmakerBalanceSimulator()
            match_name = f"{home} vs {away}"
            if self.context.league:
                match_name += f" ({self.context.league})"

            report = sim.simulate(
                lambda_h=lambda_h, lambda_a=lambda_a,
                total_pool=100000.0,
                steps=4, match=match_name,
            )

            result.analysis_report = sim.format_table(report) + sim.format_analysis(report)
            result.report_type = "balance_simulation"
            result.collaboration_mode = "B"
            result.experts_activated = ["杜博弈(博弈·主导)", "毕建模(数学)", "季泊松(量化)"]

        except Exception as e:
            logger.error(f"[SixLayer] 平衡模拟失败: {e}", exc_info=True)
            result.analysis_report = f"## ⚠️ 平衡模拟异常\n\n{e}\n\n请确保 bookmaker_sim/balance_simulator.py 存在。"

    def _execute_general_query(self, result: SixLayerResult, user_input: str):
        """处理通用查询"""
        result.analysis_report = (
            f"## 哨响AI v4.0\n\n"
            f"收到您的查询:「{user_input[:60]}」\n\n"
            f"我可以帮您:\n"
            f"1. **预测比赛** — '巴西对阿根廷谁赢'\n"
            f"2. **赔率分析** — '分析这场赔率是否有问题'\n"
            f"3. **庄家意图** — '庄家在诱盘吗' / '操盘手在干什么'\n"
            f"4. **综合分析** — '全面诊断这场比赛'\n\n"
            f"请提供具体的比赛信息 (队名+赔率)。"
        )

    # ═══════════════════════════════════════════════════════════
    # L4辅助: D-Gate Precision Filter
    # ═══════════════════════════════════════════════════════════

    def _apply_d_gate(self, h: float, d: float, a: float,
                       d_gate_override: bool = False,
                       gate_mode: str = "",
                       gate_confidence: float = 0.0) -> str:
        """D-Gate 精度过滤器 (v5.0: 支持四模式覆盖 + 置信度)

        参数说明:
            h, d, a: 模型输出的主胜/平局/客胜概率
            d_gate_override: 外部D-Gate是否已触发 (True/False)
            gate_mode: 触发的模式编号:
                      - 'A' 中等热门翻车检测 (联赛常规场景)
                      - 'B' 均衡赛事平局检测 (实力接近场景)
                      - 'C' 杯赛/世界杯专用 [v5.0新增]
                      - 'D' 操盘手确认型 [v5.0新增]
                      - '' 无触发
            gate_confidence: 置信度分数 [v5.0新增] (0.0~1.0)
        """
        margin = d - max(h, a)

        # v5.0: 当外部D-Gate已覆盖时, 根据模式生成对应的描述文本
        if d_gate_override and gate_mode:
            mode_labels = {
                'A': '中热门翻车检测',
                'B': '均衡赛平局检测',
                'C': '杯赛/世界杯专用',
                'D': '操盘手确认型'
            }
            mode_label = mode_labels.get(gate_mode, 'D-Gate')

            # 根据置信度分级输出
            conf_level = "高" if gate_confidence >= 0.75 else ("中" if gate_confidence >= 0.60 else "低")

            if margin < -0.10:
                return f"{mode_label}[积极区|{conf_level}信] margin={margin:.4f} 置信度={gate_confidence:.2f} — 覆盖激活, 建议继续关注平局"
            elif margin < 0:
                return f"{mode_label}[过渡区|{conf_level}信] margin={margin:.4f} 置信度={gate_confidence:.2f} — 覆盖激活, 平局具有竞争力"
            else:
                return f"{mode_label}[高置信区|{conf_level}信] margin={margin:.4f} 置信度={gate_confidence:.2f} — 覆盖激活且模型数据同步, 强烈看好平局"

        # 原始margin分级 (无外部覆盖时的默认判断)
        if margin < -0.15:
            return f"极弱区 margin={margin:.4f} — 平局概率显著低于主结果, 需要极强的外部信号才能覆盖此判断"
        elif margin < -0.05:
            return f"弱信号区 margin={margin:.4f} — 平局低于主胜或客胜概率, 不建议作为首选"
        elif margin < 0.02:
            return f"垃圾区 margin={margin:.4f} — 平局概率过低, 建议降级处理或不选平局"
        elif margin < 0.05:
            return f"模糊区 margin={margin:.4f} — 平局处于临界值, 需谨慎评估后决定"
        elif margin < 0.08:
            return f"可用区 margin={margin:.4f} — 平局可作为备选方案参考"
        elif margin < 0.20:
            return f"高置信区 margin={margin:.4f} — 平局信号明确, 可优先考虑"
        else:
            return f"极高置信区 margin={margin:.4f} — 平局概率远超其他结果, 强烈推荐选择"

    def _classify_result(self, h: float, d: float, a: float) -> str:
        """阈值分类 (使用回测优化参数)"""
        if d > self.draw_threshold:
            return 'D'
        elif h > a + self.ha_gap:
            return 'H'
        else:
            return 'A'

    def _is_tournament(self, league: str) -> bool:
        """检测是否为杯赛"""
        if not league:
            return False
        league_lower = league.lower()
        return any(t.lower() in league_lower for t in self.TOURNAMENT_LEAGUES)

    def _apply_tournament_calibration(self, h: float, d: float, a: float,
                                       league: str, matchday: int = 1,
                                       home_team: str = "", away_team: str = "") -> Tuple[float, float, float, Dict]:
        """
        杯赛校准: 基于世界杯20场回测的实证修正

        核心发现:
          - 16场小组赛6场平局=37.5% (vs 联赛~25%)
          - 赔率系统性高估强队: 客胜预测全错(0/7)
          - 第一轮8场全部冷门或平局
        """
        info = {}
        if not self._is_tournament(league):
            return h, d, a, info

        # ── 1. D概率修正: 向杯赛目标D率37.5%拉近 ──
        # 原始D vs 目标D差距 → 按比例从H和A各抽一半给D
        d_target = self.TOURNAMENT_D_TARGET
        d_gap = d_target - d
        if d_gap > 0:
            # 从H和A各抽一半差额给D (上限: D不超过0.50)
            steal = min(d_gap, 0.50 - d) * 0.5
            h = max(0.02, h - steal)
            a = max(0.02, a - steal)
            d = d + steal * 2
            # 归一化
            total = h + d + a
            h, d, a = h/total, d/total, a/total
            boost_pct = (d - (1 - h - a)) if False else d_gap
            info['tournament_d_calib'] = f'D→{d_target:.0%}(+{d_gap:+.0%})'

        # ── 2. 冷启动: 第一轮无历史积分, 双方实力基于"纸面" → 高不确定性 ──
        if matchday in self.COLD_START_ROUNDS:
            info['cold_start'] = True
            info['confidence_adj'] = f'×{self.TOURNAMENT_CONFIDENCE_CUT}'
            # 向均匀分布轻微拉近 (冷启动时三分类更均匀)
            uniform = 1.0/3.0
            alpha = 0.15  # 冷启动混合系数
            h = h * (1 - alpha) + uniform * alpha
            d = d * (1 - alpha) + uniform * alpha
            a = a * (1 - alpha) + uniform * alpha
            total = h + d + a
            h, d, a = h/total, d/total, a/total

        # ── 3. 射手榜因子 (从Sporting News获取) ──
        if home_team and away_team:
            try:
                import importlib.util as _iu2
                _spec2 = _iu2.spec_from_file_location(
                    'scorer_tracker',
                    str(PROJECT_ROOT / 'modules' / 'scorer_tracker.py')
                )
                _stmod = _iu2.module_from_spec(_spec2)
                sys.modules['scorer_tracker'] = _stmod
                _spec2.loader.exec_module(_stmod)
                st = _stmod.get_scorer_tracker()
                h_boost = st.get_attack_boost(home_team)
                a_boost = st.get_attack_boost(away_team)
                if h_boost != 0 or a_boost != 0:
                    h = h * (1 + h_boost * 0.5)
                    a = a * (1 + a_boost * 0.5)
                    total = h + d + a
                    h, d, a = h/total, d/total, a/total
                    info['scorer_boost'] = f'H{h_boost:+.2f}/A{a_boost:+.2f}'
                    info['scorer_h'] = st.get_attack_summary(home_team)
                    info['scorer_a'] = st.get_attack_summary(away_team)
            except Exception as e:
                info['scorer_note'] = f'射手榜加载失败: {e}'

        return h, d, a, info

    # ═══════════════════════════════════════════════════════════
    # L5: 报告生成
    # ═══════════════════════════════════════════════════════════

    def _generate_report(self, result: SixLayerResult,
                         home: str, away: str, league: str):
        """生成多维度分析报告 — v4.2: 预测意图使用简洁模板"""
        # 如果 L4 已经生成了纯文本报告 (如 explain), 不覆盖
        if result.analysis_report:
            return

        # ── v4.2: 预测意图使用简洁模板 ──
        if result.intent_category == "predict":
            result.analysis_report = self._build_concise_prediction(result, home, away, league)
            return

        # ── 其他意图使用标准模板 ──
        self._generate_full_report(result, home, away, league)

    def _build_concise_prediction(self, result: SixLayerResult,
                                   home: str, away: str, league: str) -> str:
        """v4.2: 简洁预测报告 — 直接说结论, 不绕弯"""
        from modules.prediction_report import build_prediction_report
        
        # 确定预测
        top = max(("H", result.h_prob), ("D", result.d_prob), ("A", result.a_prob), key=lambda x: x[1])
        prediction = {"H": "主胜", "D": "平局", "A": "客胜"}[top[0]]
        
        # 提取D-Gate信息
        dg_active = bool(result.d_gate_result and "激活" in (result.d_gate_result or ""))
        dg_mode = ""
        if dg_active:
            for m in ["A", "B", "C"]:
                if f"模式{m}" in (result.d_gate_result or ""):
                    dg_mode = m
                    break
        
        # 收集陷阱警告
        trap_warnings = []
        if hasattr(result, 'trap_detection') and result.trap_detection:
            td = result.trap_detection
            if isinstance(td, dict):
                for sig in td.get("active_traps", td.get("signals", [])):
                    if isinstance(sig, dict):
                        trap_warnings.append({
                            "type": sig.get("trap_type", sig.get("pattern", "")),
                            "confidence": sig.get("confidence", 0),
                            "direction": sig.get("direction", ""),
                        })
        
        # λ与进球
        lambda_fusion = None
        goal_prediction = None
        vip_analysis = getattr(result, 'vip_analysis', None)
        if hasattr(result, 'prediction_raw') and result.prediction_raw:
            pr = result.prediction_raw
            lambda_fusion = pr.get("lambda_fusion")
            goal_prediction = pr.get("goal_prediction")
        
        # VIP分数
        vip_scores = vip_analysis.get("scores", []) if vip_analysis else []
        vip_view = vip_analysis.get("bookmaker_view", "") if vip_analysis else ""
        vip_rec = vip_analysis.get("recommendation", "") if vip_analysis else ""

        # v4.3: 比分矛盾信号 — 从D-Gate引擎获取
        score_conflict = 0
        if vip_scores and len(vip_scores) >= 3:
            try:
                from rules.d_gate_engine import apply_dgate
                # 构造最小参数来检测比分矛盾
                # 不需要完整的赔率参数，只需要score_predictions
                dg_check = apply_dgate(
                    imp_h=result.h_prob, imp_d=result.d_prob, imp_a=result.a_prob,
                    odds={'home': 2.0, 'draw': 3.2, 'away': 3.0},  # 占位
                    score_predictions=vip_scores,
                    match_type=getattr(result, 'match_type', 'tournament'),
                )
                score_conflict = dg_check.get('score_contradiction', 0)
                if score_conflict >= 2:
                    vip_signal = f"比分矛盾: Top比分{score_conflict}个平局"
                    trap_warnings.append({
                        "type": "score_conflict", "confidence": 0.75,
                        "direction": "draw", "detail": vip_signal,
                    })
            except Exception as e:
                logger.debug("VIP比重信号分析失败: %s", e)

        # 通道分解
        channel_breakdown = None
        if hasattr(result, 'prediction_raw') and result.prediction_raw:
            channel_breakdown = result.prediction_raw.get("channel_breakdown")
        
        return build_prediction_report(
            home=home, away=away, league=league,
            h_prob=result.h_prob, d_prob=result.d_prob, a_prob=result.a_prob,
            prediction=prediction,
            d_gate_result=result.d_gate_result or "",
            d_gate_active=dg_active,
            d_gate_mode=dg_mode,
            trap_warnings=trap_warnings,
            lambda_fusion=lambda_fusion,
            goal_prediction=goal_prediction,
            match_type=getattr(result, 'match_type', ''),
            elapsed_ms=result.total_time_ms,
            channel_breakdown=channel_breakdown,
            vip_scores=vip_scores,
            vip_view=vip_view,
            vip_rec=vip_rec,
        )

    def _generate_full_report(self, result: SixLayerResult,
                              home: str, away: str, league: str):
        """标准完整报告 — 保留给 analyze/explain 等非预测意图"""
        lines = []

        # 标题
        match_str = f"{home} vs {away}" if home and away else "比赛分析"
        league_str = f"({league})" if league else ""
        lines.append(f"{'═' * 60}")
        lines.append(f"  哨响AI v4.0 | 6层AI分析报告")
        lines.append(f"  {match_str} {league_str}")
        lines.append(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"{'═' * 60}")

        # 意图标识
        intent_labels = {
            "predict": "赛果预测", "analyze": "专项分析",
            "backtest": "复盘回测", "explain": "术语解释",
        }
        intent_label = intent_labels.get(result.intent_category, result.intent_category)
        lines.append(f"\n📋 意图识别: {intent_label} (置信度 {result.intent_confidence:.0%})")
        lines.append(f"🎯 协同模式: 模式{result.collaboration_mode} | 专家: {', '.join(result.experts_activated[:4])}")

        # ── 场景适配 ──
        if league:
            try:
                import modules.scenario_engine as _se
                engine_se = _se.ScenarioEngine()
                odds_spread = (1/(result.h_prob or 0.34)) - (1/(result.a_prob or 0.34))
                config = engine_se.adapt(
                    home=home, away=away, league=league, matchday=1,
                    odds={'home': 1/(result.h_prob or DEFAULT_DRAW_PROB), 'draw': 1/(result.d_prob or DEFAULT_DRAW_PROB), 'away': 1/(result.a_prob or DEFAULT_DRAW_PROB)})
                lines.append(_se.ScenarioEngine.format_for_report(config))
            except Exception as e:
                logger.debug("场景引擎适配失败: %s", e)

        # ── L0 知识记忆层 ──
        if self.knowledge_layer:
            try:
                approx_odds = {
                    'home': 1/(result.h_prob or 0.34) if result.h_prob > 0 else 2.5,
                    'draw': 1/(result.d_prob or DEFAULT_DRAW_PROB) if result.d_prob > 0 else 3.2,
                    'away': 1/(result.a_prob or 0.34) if result.a_prob > 0 else 3.0,
                }
                l0_ctx = self.knowledge_layer.consult(
                    home=home, away=away, odds=approx_odds,
                    league=league, intent=result.intent_category)
                # 用类方法格式化 (静态方法, 不需要实例)
                import modules.knowledge_layer as _kl
                l0_report = _kl.KnowledgeLayer.format_for_report(l0_ctx)
                if l0_report:
                    lines.append(l0_report)
            except Exception as e:
                logger.debug(f"L0报告生成失败: {e}")

        # ── 预测结果 (如有) ──
        if result.h_prob + result.d_prob + result.a_prob > 0:
            lines.append('\n<div class="card-section"><div class="card-section-title">📊 三线预测对比</div>')
            lines.append('<table class="pred-table"><thead><tr><th>管线</th><th>主胜(H)</th><th>平局(D)</th><th>客胜(A)</th><th>判定</th></tr></thead><tbody>')

            # 主通道 (UnifiedPredictor)
            top = max(("主胜", result.h_prob), ("平局", result.d_prob), ("客胜", result.a_prob), key=lambda x: x[1])
            h_pct = f"{result.h_prob:.1%}"; d_pct = f"{result.d_prob:.1%}"; a_pct = f"{result.a_prob:.1%}"
            lines.append(f'<tr class="pred-main"><td>Unified v4.1 ◀</td><td>{h_pct}</td><td>{d_pct}</td><td>{a_pct}</td><td><b>{top[0]}</b></td></tr>')

            # 支线 VIP
            triple = result.predictions_triple
            for key in ["vip"]:
                pred = triple.get(key, {})
                if pred:
                    h = pred.get("H", 0); d = pred.get("D", 0); a = pred.get("A", 0)
                    tag = pred.get("prediction", "?"); label = pred.get("name", key)
                    mark = " ✓" if tag == top[0] else ""
                    lines.append(f'<tr><td>{label}</td><td>{h:.1%}</td><td>{d:.1%}</td><td>{a:.1%}</td><td>{tag}{mark}</td></tr>')

            lines.append('</tbody></table>')

            # 共识度
            all_preds = [top[0]]
            for key in ["vip"]:
                p = triple.get(key, {}).get("prediction")
                if p:
                    all_preds.append({"H": "主胜", "D": "平局", "A": "客胜"}.get(p, p))
            agree = len(set(all_preds))
            consensus = "🤝 双线一致" if agree == 1 else "🔴 双线分歧"
            lines.append(f'<div class="consensus-bar">共识: {consensus} | {" + ".join(all_preds)}</div>')

            # D-Gate
            if result.d_gate_result:
                lines.append(f'<div class="dgate-note">⚠️ {result.d_gate_result}</div>')

            lines.append('</div>')  # .card-section

        # ── 赔率分析 (如有) ──
        if result.odds_analysis and "error" not in result.odds_analysis:
            lines.append('\n<div class="card-section"><div class="card-section-title">🔍 赔率深度分析</div>')
            lines.append('<div class="odds-analysis">')
            oa = result.odds_analysis

            # 隐含概率
            implied = oa.get("implied_probabilities", {})
            if implied:
                raw = implied.get("raw_implied", {})
                if raw:
                    h, d, a = raw.get("home", 0), raw.get("draw", 0), raw.get("away", 0)
                    lines.append(f'<div class="oa-row"><span class="oa-label">隐含概率</span>H={h:.1%} D={d:.1%} A={a:.1%}</div>')
                fair = implied.get("fair_odds", {})
                if fair:
                    lines.append(f'<div class="oa-row"><span class="oa-label">公平赔率</span>{fair.get("home",0):.2f} / {fair.get("draw",0):.2f} / {fair.get("away",0):.2f}</div>')

            # 抽水率
            margin_decomp = oa.get("margin_decomposition", {})
            if margin_decomp:
                total_m = margin_decomp.get("total_margin", 0)
                if total_m:
                    lines.append(f'<div class="oa-row"><span class="oa-label">总抽水率</span>{total_m:.2%}</div>')
                non_uniform = margin_decomp.get("non_uniform_extra", {})
                if non_uniform:
                    lines.append(f'<div class="oa-row"><span class="oa-label">非均匀抽水</span>H={non_uniform.get("home",0):.3%} D={non_uniform.get("draw",0):.3%} A={non_uniform.get("away",0):.3%}</div>')

            # 庄家信号
            signal = oa.get("bookmaker_signal") or ""
            confidence = oa.get("bookmaker_confidence", "")
            if signal:
                lines.append(f'<div class="oa-row"><span class="oa-label">庄家信号</span>{signal}</div>')
            if confidence:
                lines.append(f'<div class="oa-row"><span class="oa-label">庄家自信</span>{confidence}</div>')

            # 综合风险
            risk = oa.get("overall_risk") or oa.get("conclusion", {}).get("overall_risk", "")
            if risk:
                risk_icons = {"normal": "🟢", "elevated": "🟡", "high": "🟠", "extreme": "🔴"}
                lines.append(f'<div class="oa-row"><span class="oa-label">风险等级</span>{risk_icons.get(risk, "⚪")} {risk}</div>')

            lines.append('</div></div>')

        # ── 陷阱检测 (如有) ──
        if result.trap_detection and "error" not in result.trap_detection:
            td = result.trap_detection
            # 检查是否是来自 OddsDeepReport 的 trap_detection 子对象
            if isinstance(td, dict):
                risk = td.get("risk_level", "")
                score = td.get("trap_score", 0)
                active = td.get("active_traps", [])
                warnings_list = td.get("warning_signals", [])

                if risk or score or active or warnings_list:
                    lines.append(f"\n{'─' * 40}")
                    lines.append(f"⚠️ 陷阱/诱盘检测")

                    if risk:
                        risk_icons = {"safe": "🟢", "suspicious": "🟡", "danger": "🟠", "harvesting": "🔴"}
                        icon = risk_icons.get(risk, "⚪")
                        lines.append(f"  风险等级: {icon} {risk}")
                    if score:
                        lines.append(f"  陷阱评分: {score:.2f} / 1.0")

                    for trap_info in active[:3]:
                        if isinstance(trap_info, dict):
                            pattern = trap_info.get("pattern", trap_info.get("trap_type", "未知"))
                            engine_name = trap_info.get("engine", "")
                            desc = trap_info.get("description", trap_info.get("detail", ""))
                            conf = trap_info.get("confidence", 0)
                            line = f"  ⚡ {pattern}"
                            if engine_name:
                                line += f" [{engine_name}]"
                            if conf:
                                line += f" ({conf:.0%})"
                            if desc:
                                line += f": {desc}"
                            lines.append(line)

                    for w in warnings_list[:2]:
                        lines.append(f"  ⚠ {w}")

        # ── 专家洞察 ──
        if result.expert_insights:
            lines.append(f"\n{'─' * 40}")
            lines.append(f"💡 专家洞察")
            for insight in result.expert_insights:
                lines.append(f"  • {insight}")

        # ── 推荐比分 (泊松模型) ──
        if result.h_prob + result.d_prob + result.a_prob > 0:
            try:
                from optimize.poisson_predictor import PoissonPredictor
                pp = PoissonPredictor()
                scores = pp.predict_scores(
                    home_prob=result.h_prob,
                    draw_prob=result.d_prob,
                    away_prob=result.a_prob,
                    league_name=league or "default",
                    top_k=3
                )
                if scores:
                    lines.append(f"\n{'─' * 40}")
                    lines.append(f"🎯 推荐比分")
                    for rank, s in enumerate(scores):
                        star = "⭐⭐⭐" if rank == 0 else ("⭐⭐" if rank == 1 else "⭐")
                        sc = s.get("score", "?-?")
                        prob = s.get("probability", 0)
                        outcome = s.get("outcome", "")
                        labels_map = {"home": "主胜", "draw": "平局", "away": "客胜"}
                        label = labels_map.get(outcome, outcome)
                        lines.append(f"  {star} {sc:<6} {prob:>5.1%}  ({label})")
            except Exception as e:
                logger.debug(f"比分预测失败: {e}")

        # ── 庄家风控防线 (RP Barrier) ──
        if home and away and result.h_prob > 0:
            try:
                inv = 1/(result.h_prob+0.001) + 1/(result.d_prob+0.001) + 1/(result.a_prob+0.001)
                oh_approx = 1/result.h_prob if result.h_prob > 0 else 2.5
                od_approx = 1/result.d_prob if result.d_prob > 0 else 3.2
                oa_approx = 1/result.a_prob if result.a_prob > 0 else 3.0
                overround = (1/oh_approx + 1/od_approx + 1/oa_approx) - 1

                barriers = []
                # R-Barrier: 抽水率异常
                if overround > 0.10:
                    barriers.append(f"R-Barrier¹: 抽水率{overround:.1%}>10%, 庄家不确定性高")
                # R-Barrier: 平局异常
                league_dr = 0.375 if ('世界' in (league or '') or 'World' in (league or '')) else 0.25
                if result.d_prob < league_dr * 0.6:
                    barriers.append(f"R-Barrier¹: P(D)={result.d_prob:.1%}远低于杯赛均值{league_dr:.0%}, 庄家压制平局")
                # R-Water: 非均匀抽水
                if result.d_prob < 0.20 and result.h_prob > 0.50:
                    barriers.append(f"R-Water³: 平局被额外抽水, 庄家对平局最谨慎")

                if barriers:
                    lines.append(f"\n{'─' * 40}")
                    lines.append(f"🛡️ 庄家风控防线 (RP Barrier)")
                    for b in barriers:
                        lines.append(f"  ⚡ {b}")
                    risk = "🟡 谨慎" if len(barriers) >= 2 else "🟢 安全"
                    lines.append(f"  综合: {risk} | {len(barriers)}条防线触发")
                    if result.d_prob < 0.20:
                        lines.append(f"  💡 建议: P(D)被压低, 平局概率可能被低估")
            except Exception as e:
                logger.debug(f"防线检测失败: {e}")

        # ── 共同对手交叉对比 ──
        if home and away:
            try:
                # 直接路径导入 (避免 package cache 问题)
                import importlib.util as _iu
                _spec = _iu.spec_from_file_location(
                    'cross_opponent',
                    str(PROJECT_ROOT / 'modules' / 'cross_opponent.py')
                )
                _co = _iu.module_from_spec(_spec)
                sys.modules['cross_opponent'] = _co
                _spec.loader.exec_module(_co)
                CrossOpponentAnalyzer = _co.CrossOpponentAnalyzer
                get_known_common_opponents = _co.get_known_common_opponents
                h_results, a_results = get_known_common_opponents(home, away)
                if h_results and a_results:
                    coa = CrossOpponentAnalyzer()
                    co_result = coa.analyze(home, away, h_results, a_results)
                    if co_result.common_opponents:
                        lines.append(f"\n{'─' * 40}")
                        lines.append(f"🔄 共同对手交叉对比")
                        for comp in co_result.common_opponents[:3]:
                            icon = {"主": "🔴", "客": "🟢", "相当": "⚪"}.get(
                                "主" if comp.advantage == home else ("客" if comp.advantage == away else "相当"), "⚪")
                            lines.append(f"  {icon} vs {comp.opponent}: {comp.key_diff}")
                        if co_result.hidden_strength_team:
                            lines.append(f"  ⚡ 隐藏实力: {co_result.hidden_strength_desc}")
                        if co_result.upset_alert:
                            lines.append(f"  ⚠️ 冷门预警: {co_result.upset_reason[:100]}")
                        if co_result.alternative_scores:
                            lines.append(f"  🎲 修正比分:")
                            for as_ in co_result.alternative_scores[:3]:
                                src_short = as_.get('source', '')[:40]
                                lines.append(f"     {as_['score']} ({as_.get('outcome','')}) [{src_short}]")
                        if co_result.p_draw_boost > 0:
                            lines.append(f"  📈 P(D)修正: +{co_result.p_draw_boost:.0%}")
            except Exception as e:
                logger.warning(f"交叉对比失败: {e}", exc_info=True)

        # ── 让球/大小球/射手/操盘手操作模拟 ──
        if home and away and result.h_prob > 0:
            try:
                import importlib.util as _iu3
                _spec3 = _iu3.spec_from_file_location(
                    'match_analyzer',
                    str(PROJECT_ROOT / 'modules' / 'match_analyzer.py')
                )
                _ma = _iu3.module_from_spec(_spec3)
                sys.modules['match_analyzer'] = _ma
                _spec3.loader.exec_module(_ma)
                analyzer = _ma.get_match_analyzer()
                # 从概率反推近似赔率
                if result.h_prob > 0 and result.d_prob > 0 and result.a_prob > 0:
                    total = result.h_prob + result.d_prob + result.a_prob
                    approx_odds = {
                        'home': 1.0 / (result.h_prob / total) if result.h_prob > 0 else 2.5,
                        'draw': 1.0 / (result.d_prob / total) if result.d_prob > 0 else 3.2,
                        'away': 1.0 / (result.a_prob / total) if result.a_prob > 0 else 3.0,
                    }
                else:
                    approx_odds = {'home': 2.0, 'draw': 3.2, 'away': 3.0}
                mreport = analyzer.analyze(home, away, league or '世界杯', approx_odds)
                lines.append(analyzer.format_report(mreport))
            except Exception as e:
                logger.warning(f"全维度分析失败: {e}")

        if result.recommendation:
            lines.append(f"\n{'─' * 40}")
            lines.append(f"📌 最终建议")
            lines.append(f"  {result.recommendation}")

        # ── L6 优化建议 ──
        if result.optimization_suggestions:
            lines.append(f"\n{'─' * 40}")
            lines.append(f"🔄 L6 优化建议")
            for sug in result.optimization_suggestions:
                lines.append(f"  • {sug}")

        # ── 容错守护 & D-Gate 状态 ──
        if self.degradation_guard:
            try:
                gate, d_flags = self.degradation_guard.d_gate_assess(
                    result.h_prob, result.d_prob, result.a_prob)
                d_margin = result.d_prob - max(result.h_prob, result.a_prob)
                lines.append(f"\n{'─' * 40}")
                lines.append(f"🛡️ 容错守护 (Degradation Guard)")
                lines.append(f"  🔗 链路: v4.1全功能 | 健康: {'✅' if not result.fallback_triggered else '⚠️降级'}")
                lines.append(f"  🚦 D-Gate: {gate} (margin={d_margin:+.4f})")
                for flag in d_flags[:2]:
                    lines.append(f"    ⚠ {flag}")
                if result.fallback_triggered:
                    lines.append(f"  ⚡ 降级原因: {result.errors[:2]}")
            except Exception as e:
                logger.debug("容错守护报告失败: %s", e)

        # 尾部
        lines.append(f"\n{'═' * 60}")
        lines.append(f"  推理耗时: {result.total_time_ms:.0f}ms | 管线: {result.pipeline_version}")
        if result.fallback_triggered:
            lines.append(f"  ⚠ 部分降级: {result.errors}")
        lines.append(f"{'═' * 60}")

        result.analysis_report = "\n".join(lines)

    # ═══════════════════════════════════════════════════════════
    # 对话循环
    # ═══════════════════════════════════════════════════════════

    def run_conversation(self):
        """启动交互式对话模式"""
        print(f"""
╔══════════════════════════════════════════════════╗
║    哨响AI v4.0 — 6层AI对话引擎                   ║
║    Six-Layer Conversation Engine                 ║
║                                                  ║
║  输入示例:                                        ║
║    "巴西对阿根廷谁赢"           → 赛果预测        ║
║    "这场赔率有问题，庄家在诱盘"  → 庄家意图分析    ║
║    "凯利指数怎么算"             → 术语解释        ║
║    "全面分析这场比赛"           → 综合诊断         ║
║                                                  ║
║  命令: /help /stats /clear /exit                 ║
╚══════════════════════════════════════════════════╝
""")

        while True:
            try:
                user_input = input("\n🫵 您: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n\n👋 再见！")
                break

            if not user_input:
                continue

            # 特殊命令
            if user_input.startswith("/"):
                cmd = user_input[1:].lower()
                if cmd == "exit" or cmd == "quit":
                    print("👋 再见！")
                    break
                elif cmd == "help":
                    self._print_help()
                elif cmd == "stats":
                    self._print_stats()
                elif cmd == "clear":
                    self.context = ConversationContext()
                    print("✅ 上下文已清除")
                else:
                    print(f"未知命令: /{cmd}。可用: /help /stats /clear /exit")
                continue

            # 处理用户输入
            print("\n🤖 哨响AI 分析中...")
            result = self.process(user_input)

            # 输出结果
            print(f"\n{result.analysis_report}")

            # 保存对话历史
            self.context.add_history("user", user_input)
            self.context.add_history("assistant", result.analysis_report[:200])

    def process_single(self, user_input: str,
                       home: str = None, away: str = None,
                       league: str = None,
                       odds: Dict[str, float] = None) -> str:
        """单次查询模式 — 返回纯文本报告"""
        result = self.process(user_input, home, away, league, odds)
        return result.analysis_report

    def _print_help(self):
        print("""
📖 哨响AI v4.0 使用指南
────────────────────────────────────────
1. 赛果预测: "巴西对阿根廷谁赢" / "预测这场"
2. 赔率分析: "这个赔率正常吗" / "抽水率多少"
3. 庄家意图: "庄家在诱盘吗" / "操盘手在干什么"
4. 综合诊断: "全面分析巴西vs阿根廷"
5. 术语解释: "凯利指数怎么算" / "什么是D-Gate"

特殊命令:
  /help   — 显示此帮助
  /stats  — 显示对话统计
  /clear  — 清除上下文
  /exit   — 退出
""")

    def _print_stats(self):
        total = self.stats["total_queries"]
        avg_time = self.stats["total_time_ms"] / max(total, 1)
        print(f"""
📊 对话统计
─────────────────────────
总查询数: {total}
平均耗时: {avg_time:.0f}ms
降级次数: {self.stats['fallback_count']}
意图分布: {json.dumps(self.stats['by_intent'], ensure_ascii=False)}
当前上下文: {self.context.home_team} vs {self.context.away_team}
""")

# ═══════════════════════════════════════════════════════════════
# 3. 便捷初始化
# ═══════════════════════════════════════════════════════════════

_engine_instance: Optional[SixLayerConversationEngine] = None

def get_engine(**kwargs) -> SixLayerConversationEngine:
    """获取6层引擎单例"""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = SixLayerConversationEngine(**kwargs)
    return _engine_instance

# ═══════════════════════════════════════════════════════════════
# 4. CLI入口
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="哨响AI v4.0 — 6层AI对话引擎",
    )
    parser.add_argument("--query", "-q", type=str, default=None,
                        help="单次查询 (非交互模式)")
    parser.add_argument("--home", type=str, default=None, help="主队名")
    parser.add_argument("--away", type=str, default=None, help="客队名")
    parser.add_argument("--league", "-l", type=str, default=None, help="联赛名")
    parser.add_argument("--odds-home", type=float, default=None, help="主胜赔率")
    parser.add_argument("--odds-draw", type=float, default=None, help="平局赔率")
    parser.add_argument("--odds-away", type=float, default=None, help="客胜赔率")
    parser.add_argument("--demo", action="store_true", help="运行演示")
    parser.add_argument("--no-l6", action="store_true", help="禁用L6自主优化")

    args = parser.parse_args()

    engine = SixLayerConversationEngine(enable_l6=not args.no_l6)

    if args.demo:
        _run_demo(engine)
    elif args.query:
        odds = None
        if args.odds_home and args.odds_draw and args.odds_away:
            odds = {"home": args.odds_home, "draw": args.odds_draw, "away": args.odds_away}
        report = engine.process_single(
            args.query, args.home, args.away, args.league, odds
        )
        print(report)
    elif args.home or args.away:
        query = f"预测{args.home or '?'} vs {args.away or '?'}"
        odds = None
        if args.odds_home and args.odds_draw and args.odds_away:
            odds = {"home": args.odds_home, "draw": args.odds_draw, "away": args.odds_away}
        report = engine.process_single(
            query, args.home, args.away, args.league, odds
        )
        print(report)
    else:
        engine.run_conversation()

def _run_demo(engine: SixLayerConversationEngine):
    """运行演示"""
    demo_queries = [
        ("巴西对阿根廷谁赢",
         "巴西", "阿根廷", "世界杯",
         {"home": 2.10, "draw": 3.30, "away": 3.60}),
        ("这场赔率有问题，庄家在诱盘",
         "巴西", "阿根廷", "世界杯",
         {"home": 1.80, "draw": 3.50, "away": 4.50}),
        ("凯利指数怎么算的",
         None, None, None, None),
        ("演示操盘手平衡赔率窗口",
         "巴西", "阿根廷", "世界杯",
         {"home": 2.10, "draw": 3.30, "away": 3.60}),
    ]

    print(f"""
╔══════════════════════════════════════════════════╗
║    哨响AI v4.0 — 6层架构演示模式                  ║
╚══════════════════════════════════════════════════╝
""")

    for query, home, away, league, odds in demo_queries:
        print(f"\n{'─' * 60}")
        print(f"🫵 用户: {query}")
        print(f"{'─' * 60}")
        result = engine.process(query, home, away, league, odds)
        print(f"\n{result.analysis_report}")
        time.sleep(0.5)

if __name__ == "__main__":
    main()

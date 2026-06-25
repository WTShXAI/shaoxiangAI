"""
哨响AI v4.0 — 专家调度框架 v2 (ExpertHub v2)
===============================================
v4.0架构核心模块。在 v3.0 ExpertHub 基础上:
    1. 新增 WorkBuddyExpertSpec — 11位WorkBuddy专家声明式描述
    2. 新增 CollaborationScheduler — 四类协同模式(A/B/C/D)调度逻辑
    3. 新增 DomainDispatcher — 按领域域名组并行调度
    4. 集成 output_schema — 所有输出走统一格式
    5. 降级保障 — 单专家故障不中断整体服务

四类协同模式:
    模式A (全栈预测): 6算法专家并行 → 融合 → 验证 → 输出
    模式B (赔率深挖): 杜博弈主导 → 季泊松+毕建模辅助 → 串行验证
    模式C (平局攻坚): 曾均衡主导 → 季泊松+荣合众辅助
    模式D (系统迭代): 全团联动 → 并行诊断 → 交叉评审 → 优化

作者: Architecture v4.0
日期: 2026-06-18
"""
from __future__ import annotations
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable, Tuple
from enum import Enum

from modules.expert_protocol import (
    ExpertProtocol, ExpertState, ExpertMeta,
    InputSchema, OutputSchema, ExpertAdapter,
)
from modules.expert_registry import ExpertRegistry
from modules.output_schema import (
    UnifiedPrediction, FusedPrediction, ExpertContribution,
    ThreeWayProbability, DistributionExtension,
    ReasoningChain, ConfidenceAssessment, EvidencePackage,
    ConfidenceLevel, create_fallback_prediction,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 1. WorkBuddy 专家声明式规范
# ═══════════════════════════════════════════════════════════════

class ExpertDomain(Enum):
    """专家领域枚举"""
    QUANTIZATION = "quantization"     # 量化建模 (季泊松)
    GAME_THEORY = "game_theory"       # 博弈论 (杜博弈)
    ENSEMBLE = "ensemble"             # 集成学习 (荣合众)
    IMBALANCE = "imbalance"           # 不平衡分类 (曾均衡)
    TEMPORAL = "temporal"             # 时序深度学习 (施时序)
    MATH = "math"                     # 数学统计 (毕建模)
    ALGO_OPT = "algo_opt"             # 算法优化 (齐优化)
    COORDINATION = "coordination"     # 总协调 (郝优算)
    BACKEND = "backend"               # 后端工程 (傅稳当)
    DATA = "data"                     # 数据工程 (舒治理)
    PERFORMANCE = "performance"       # 性能工程 (孙加速)
    FRONTEND = "frontend"             # 前端工程 (杨界面)


class CollaborationMode(Enum):
    """四类协同模式"""
    FULL_STACK = "A"      # 全栈预测: 6算法专家并行
    ODDS_DEEP = "B"        # 赔率深挖: 博弈主导 + 量化辅助
    DRAW_FOCUS = "C"       # 平局攻坚: 不平衡主导 + 量化辅助
    SYSTEM_ITERATE = "D"   # 系统迭代: 全团联动


@dataclass
class WorkBuddyExpertSpec:
    """
    WorkBuddy专家声明式规范

    描述一位WorkBuddy专家的角色、能力、输入输出。
    用于 ExpertHub 调度时确定调用参数和结果处理。
    """
    agent_id: str                           # WorkBuddy Agent ID
    name: str                               # 花名
    domain: ExpertDomain                    # 所属领域
    sequence: str                           # "algorithm" | "engineering"
    description: str                        # 能力描述
    # 输入输出
    required_inputs: List[str] = field(default_factory=list)   # 必须输入字段
    optional_inputs: List[str] = field(default_factory=list)    # 可选输入字段
    produces: List[str] = field(default_factory=lambda: ["probability"])  # 产出类型
    # 调度参数
    timeout_s: float = 15.0                 # 单次调用超时
    priority: int = 0                       # 调度优先级 (越大越优先)
    is_fallbackable: bool = True            # 故障时是否可降级
    # 协同模式归属 (可为空=所有模式可用)
    modes: List[CollaborationMode] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# 2. 11 + 1 位专家完整注册表
# ═══════════════════════════════════════════════════════════════

WORKBUDDY_EXPERTS: Dict[str, WorkBuddyExpertSpec] = {
    # ── 算法技术序列 (8人) ──
    "郝优算": WorkBuddyExpertSpec(
        agent_id="footballai-fullstack-team-team-lead",
        name="郝优算", domain=ExpertDomain.COORDINATION, sequence="algorithm",
        description="算法总工。问题拆解、任务调度、方案汇编、跨领域协调、交付把关。",
        required_inputs=["user_intent", "task_description"],
        produces=["route_plan", "final_report"],
        timeout_s=30.0, priority=10,
        modes=[CollaborationMode.FULL_STACK, CollaborationMode.SYSTEM_ITERATE],
    ),
    "季泊松": WorkBuddyExpertSpec(
        agent_id="footballai-sports-quant-expert",
        name="季泊松", domain=ExpertDomain.QUANTIZATION, sequence="algorithm",
        description="足球量化建模专家。泊松分布、赔率漂移微积分、xG/xGA、联赛风格嵌入。",
        required_inputs=["home_team", "away_team", "odds"],
        optional_inputs=["league", "match_context"],
        produces=["probability", "score_distribution", "goal_expectation", "league_adjustment"],
        timeout_s=10.0, priority=8,
        modes=[CollaborationMode.FULL_STACK, CollaborationMode.ODDS_DEEP, CollaborationMode.DRAW_FOCUS, CollaborationMode.SYSTEM_ITERATE],
    ),
    "杜博弈": WorkBuddyExpertSpec(
        agent_id="footballai-game-theory-expert",
        name="杜博弈", domain=ExpertDomain.GAME_THEORY, sequence="algorithm",
        description="博弈论赔率逆向专家。庄家赔率逆向、诱盘识别、多机构分歧、凯利指数。",
        required_inputs=["home_team", "away_team", "odds"],
        optional_inputs=["multi_bookmaker_odds", "odds_timeline"],
        produces=["true_probability", "trap_alert", "bookmaker_signal", "margin_decomposition"],
        timeout_s=10.0, priority=8,
        modes=[CollaborationMode.FULL_STACK, CollaborationMode.ODDS_DEEP, CollaborationMode.SYSTEM_ITERATE],
    ),
    "荣合众": WorkBuddyExpertSpec(
        agent_id="footballai-ensemble-expert",
        name="荣合众", domain=ExpertDomain.ENSEMBLE, sequence="algorithm",
        description="集成学习专家。Stacking优化、动态权重、MoE门控、D-Gate融合。",
        required_inputs=["base_model_outputs", "oof_metrics"],
        optional_inputs=["feature_importance", "drift_indicators"],
        produces=["fused_probability", "fusion_weights", "model_contribution"],
        timeout_s=10.0, priority=7,
        modes=[CollaborationMode.FULL_STACK, CollaborationMode.DRAW_FOCUS, CollaborationMode.SYSTEM_ITERATE],
    ),
    "曾均衡": WorkBuddyExpertSpec(
        agent_id="footballai-imbalance-expert",
        name="曾均衡", domain=ExpertDomain.IMBALANCE, sequence="algorithm",
        description="不平衡分类专家。Draw特征、冷门检测、Focal Loss、阈值寻优。",
        required_inputs=["home_team", "away_team", "base_prediction", "odds"],
        optional_inputs=["league", "spread_category"],
        produces=["draw_refined_probability", "upset_alert", "threshold_recommendation"],
        timeout_s=10.0, priority=7,
        modes=[CollaborationMode.FULL_STACK, CollaborationMode.DRAW_FOCUS, CollaborationMode.SYSTEM_ITERATE],
    ),
    "施时序": WorkBuddyExpertSpec(
        agent_id="footballai-timeseries-dl-expert",
        name="施时序", domain=ExpertDomain.TEMPORAL, sequence="algorithm",
        description="时序深度学习专家。Transformer/LSTM序列建模、赔率时序漂移、NN升级。",
        required_inputs=["home_team", "away_team", "sequence_features"],
        optional_inputs=["odds_timeline", "team_form"],
        produces=["sequence_features", "temporal_signal", "nn_prediction"],
        timeout_s=12.0, priority=6,
        modes=[CollaborationMode.FULL_STACK, CollaborationMode.SYSTEM_ITERATE],
    ),
    "毕建模": WorkBuddyExpertSpec(
        agent_id="footballai-math-expert",
        name="毕建模", domain=ExpertDomain.MATH, sequence="algorithm",
        description="数学统计专家。概率推导、凸优化、统计检验、全团数学底座。",
        required_inputs=["predictions", "validation_data"],
        optional_inputs=["hypothesis"],
        produces=["statistical_test_report", "confidence_interval", "effect_size"],
        timeout_s=8.0, priority=6,
        modes=[CollaborationMode.FULL_STACK, CollaborationMode.ODDS_DEEP, CollaborationMode.DRAW_FOCUS, CollaborationMode.SYSTEM_ITERATE],
    ),
    "齐优化": WorkBuddyExpertSpec(
        agent_id="footballai-algo-expert",
        name="齐优化", domain=ExpertDomain.ALGO_OPT, sequence="algorithm",
        description="算法优化专家。特征筛选/VIF/SHAP、集成调优、训练规范、问题诊断。",
        required_inputs=["model_metrics", "feature_importance"],
        optional_inputs=["training_log", "validation_results"],
        produces=["optimization_plan", "feature_selection", "hyperparameter_suggestion"],
        timeout_s=10.0, priority=7,
        modes=[CollaborationMode.SYSTEM_ITERATE],
    ),
    # ── 工程落地序列 (4人) ──
    "傅稳当": WorkBuddyExpertSpec(
        agent_id="footballai-backend-expert",
        name="傅稳当", domain=ExpertDomain.BACKEND, sequence="engineering",
        description="后端工程专家。FastAPI架构、降级链路、并发优化、服务监控。",
        required_inputs=["deployment_config", "service_requirements"],
        produces=["api_spec", "degradation_plan", "performance_report"],
        timeout_s=15.0, priority=5,
        modes=[CollaborationMode.SYSTEM_ITERATE],
    ),
    "舒治理": WorkBuddyExpertSpec(
        agent_id="footballai-data-expert",
        name="舒治理", domain=ExpertDomain.DATA, sequence="engineering",
        description="数据工程专家。特征管线、Parquet/SQLite优化、数据质量、冷启动。",
        required_inputs=["data_requirements", "feature_spec"],
        produces=["data_pipeline", "quality_report", "storage_plan"],
        timeout_s=10.0, priority=5,
        modes=[CollaborationMode.SYSTEM_ITERATE],
    ),
    "孙加速": WorkBuddyExpertSpec(
        agent_id="footballai-perf-expert",
        name="孙加速", domain=ExpertDomain.PERFORMANCE, sequence="engineering",
        description="性能工程专家。推理加速、INT8量化、GPU适配、缓存设计。",
        required_inputs=["model_info", "performance_target"],
        produces=["optimization_result", "benchmark_report"],
        timeout_s=10.0, priority=5,
        modes=[CollaborationMode.SYSTEM_ITERATE],
    ),
    "杨界面": WorkBuddyExpertSpec(
        agent_id="footballai-frontend-expert",
        name="杨界面", domain=ExpertDomain.FRONTEND, sequence="engineering",
        description="前端开发专家。Vue 3组件、ECharts可视化、Vite构建、响应式布局。",
        required_inputs=["ui_requirements", "data_schema"],
        produces=["vue_component", "visualization", "build_output"],
        timeout_s=15.0, priority=5,
        modes=[],
    ),
}


# ═══════════════════════════════════════════════════════════════
# 3. 领域域分组
# ═══════════════════════════════════════════════════════════════

DOMAIN_GROUPS = {
    "量化分析域": [
        ExpertDomain.QUANTIZATION,
        ExpertDomain.MATH,
        ExpertDomain.TEMPORAL,
        ExpertDomain.IMBALANCE,
        ExpertDomain.ALGO_OPT,
    ],
    "博弈分析域": [
        ExpertDomain.GAME_THEORY,
        ExpertDomain.ENSEMBLE,
    ],
    "工程执行域": [
        ExpertDomain.BACKEND,
        ExpertDomain.DATA,
        ExpertDomain.PERFORMANCE,
        ExpertDomain.FRONTEND,
    ],
}

# 模式 → 参与的专家名
MODE_EXPERTS: Dict[CollaborationMode, List[str]] = {
    CollaborationMode.FULL_STACK: ["季泊松", "杜博弈", "荣合众", "曾均衡", "施时序", "毕建模"],
    CollaborationMode.ODDS_DEEP: ["杜博弈", "季泊松", "毕建模"],
    CollaborationMode.DRAW_FOCUS: ["曾均衡", "季泊松", "荣合众"],
    CollaborationMode.SYSTEM_ITERATE: ["郝优算", "季泊松", "杜博弈", "荣合众", "曾均衡", "施时序", "毕建模", "齐优化"],
}


# ═══════════════════════════════════════════════════════════════
# 4. 协同调度器
# ═══════════════════════════════════════════════════════════════

@dataclass
class CollaborationResult:
    """一次协同调度的结果"""
    mode: CollaborationMode
    fused_prediction: Optional[FusedPrediction] = None
    expert_results: Dict[str, Dict] = field(default_factory=dict)
    fallback_triggered: bool = False
    fallback_reason: str = ""
    total_time_ms: float = 0.0
    errors: List[str] = field(default_factory=list)


class CollaborationScheduler:
    """
    协同调度器 — 按模式调度专家并行/串行执行

    调度规则:
        模式A: 6专家并行 → 汇总 → 融合 → 输出
        模式B: 杜博弈先执行 → 季泊松+毕建模并行 → 汇总
        模式C: 曾均衡先执行 → 季泊松+荣合众并行 → 汇总
        模式D: 8专家并行诊断 → 郝优算汇总 → 优化方案
    """

    def __init__(self, max_workers: int = 10, timeout_s: float = 15.0):
        self.max_workers = max_workers
        self.timeout_s = timeout_s
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

    def execute_mode(self, mode: CollaborationMode,
                     match_data: Dict,
                     expert_executor: Callable[[str, Dict], Dict] = None
                     ) -> CollaborationResult:
        """
        按模式执行专家协同

        Args:
            mode: 协同模式
            match_data: 比赛数据
            expert_executor: 专家执行函数 (name, data) → result dict
                             如果不提供，返回空结果(仅做调度规划)

        Returns:
            CollaborationResult: 完整的协同结果
        """
        start = time.perf_counter()
        expert_names = MODE_EXPERTS.get(mode, [])
        if not expert_names:
            return CollaborationResult(
                mode=mode, fallback_triggered=True,
                fallback_reason=f"模式{mode.value}无可用专家",
            )

        result = CollaborationResult(mode=mode)
        expert_outputs = {}

        if mode == CollaborationMode.FULL_STACK:
            expert_outputs = self._execute_parallel(expert_names, match_data, expert_executor)

        elif mode == CollaborationMode.ODDS_DEEP:
            # 杜博弈 先执行
            lead_results = self._execute_serial(["杜博弈"], match_data, expert_executor)
            expert_outputs.update(lead_results)
            # 季泊松 + 毕建模 并行
            support_results = self._execute_parallel(["季泊松", "毕建模"], match_data, expert_executor)
            expert_outputs.update(support_results)

        elif mode == CollaborationMode.DRAW_FOCUS:
            lead_results = self._execute_serial(["曾均衡"], match_data, expert_executor)
            expert_outputs.update(lead_results)
            support_results = self._execute_parallel(["季泊松", "荣合众"], match_data, expert_executor)
            expert_outputs.update(support_results)

        elif mode == CollaborationMode.SYSTEM_ITERATE:
            expert_outputs = self._execute_parallel(expert_names, match_data, expert_executor)

        result.expert_results = expert_outputs
        result.total_time_ms = (time.perf_counter() - start) * 1000

        # 检查是否有降级
        errors = [k for k, v in expert_outputs.items() if v.get("status") == "error"]
        if len(errors) >= len(expert_names) / 2:
            result.fallback_triggered = True
            result.fallback_reason = f"超过半数专家失败: {errors}"
        result.errors = errors

        return result

    def _execute_parallel(self, names: List[str], data: Dict,
                          executor_fn: Callable = None) -> Dict[str, Dict]:
        """并行执行多个专家"""
        if executor_fn is None:
            return {name: {"status": "planning_only", "output": None} for name in names}

        results = {}
        futures = {}
        for name in names:
            spec = WORKBUDDY_EXPERTS.get(name)
            timeout = spec.timeout_s if spec else self.timeout_s
            future = self.executor.submit(executor_fn, name, data)
            futures[future] = (name, timeout)

        for future in as_completed(futures):
            name, timeout = futures[future]
            try:
                result = future.result(timeout=timeout)
                results[name] = result
            except TimeoutError:
                logger.warning(f"[{name}] 执行超时({timeout}s)，降级")
                results[name] = {
                    "status": "error",
                    "error": f"timeout after {timeout}s",
                    "fallback": True,
                }
            except ImportError as e:
                logger.warning(f"[{name}] 模块缺失: {e}")
                results[name] = {"status": "error", "error": str(e), "fallback": True}
            except Exception as e:
                logger.error(f"[{name}] 执行异常: {e}")
                results[name] = {
                    "status": "error",
                    "error": str(e),
                    "fallback": True,
                }

        return results

    def _execute_serial(self, names: List[str], data: Dict,
                        executor_fn: Callable = None) -> Dict[str, Dict]:
        """串行执行多个专家"""
        if executor_fn is None:
            return {name: {"status": "planning_only", "output": None} for name in names}

        results = {}
        for name in names:
            spec = WORKBUDDY_EXPERTS.get(name)
            timeout = spec.timeout_s if spec else self.timeout_s
            try:
                result = executor_fn(name, data)
                results[name] = result
            except ImportError as e:
                logger.warning(f"[{name}-serial] 模块缺失: {e}")
                results[name] = {"status": "error", "error": str(e), "fallback": True}
            except Exception as e:
                logger.error(f"[{name}] 执行异常: {e}")
                results[name] = {"status": "error", "error": str(e), "fallback": True}
        return results


# ═══════════════════════════════════════════════════════════════
# 5. v4.0 ExpertHub 扩展
# ═══════════════════════════════════════════════════════════════

class ExpertHubV2:
    """
    v4.0 专家中心调度器 — 兼容 v3.0 ExpertHub

    新增能力:
        - WorkBuddy专家注册与管理
        - 四类协同模式调度
        - 统一输出Schema
        - 领域域分组并行
    """

    def __init__(self):
        self.scheduler = CollaborationScheduler()
        self.wb_experts: Dict[str, WorkBuddyExpertSpec] = dict(WORKBUDDY_EXPERTS)
        self._active_mode: Optional[CollaborationMode] = None

    def get_expert(self, name: str) -> Optional[WorkBuddyExpertSpec]:
        """获取专家规范"""
        return self.wb_experts.get(name)

    def get_domain_experts(self, domain: ExpertDomain) -> List[str]:
        """获取某领域的所有专家名"""
        return [name for name, spec in self.wb_experts.items() if spec.domain == domain]

    def get_mode_experts(self, mode: CollaborationMode) -> List[WorkBuddyExpertSpec]:
        """获取某模式下的所有专家"""
        names = MODE_EXPERTS.get(mode, [])
        return [self.wb_experts[n] for n in names if n in self.wb_experts]

    def get_algorithm_experts(self) -> List[str]:
        """获取所有算法专家名"""
        return [name for name, spec in self.wb_experts.items() if spec.sequence == "algorithm"]

    def get_engineering_experts(self) -> List[str]:
        """获取所有工程专家名"""
        return [name for name, spec in self.wb_experts.items() if spec.sequence == "engineering"]

    def plan_collaboration(self, mode: CollaborationMode, match_data: Dict) -> CollaborationResult:
        """
        规划协同调度 (不实际执行，仅规划)

        用于: 生成专家调度计划，供郝优算审阅或显示给用户
        """
        return self.scheduler.execute_mode(mode, match_data)

    def status_report(self) -> Dict:
        """系统状态报告"""
        algo_experts = self.get_algorithm_experts()
        eng_experts = self.get_engineering_experts()
        return {
            "version": "v4.0",
            "total_experts": len(self.wb_experts),
            "algorithm_experts": len(algo_experts),
            "engineering_experts": len(eng_experts),
            "experts": {name: {
                "domain": spec.domain.value,
                "sequence": spec.sequence,
                "priority": spec.priority,
                "modes": [m.value for m in spec.modes],
            } for name, spec in self.wb_experts.items()},
            "collaboration_modes": {
                m.value: {
                    "experts": MODE_EXPERTS.get(m, []),
                    "expert_count": len(MODE_EXPERTS.get(m, [])),
                }
                for m in CollaborationMode
            },
        }

    def route_by_mode(self, intent_category: str, intent_subtype: str = "") -> CollaborationMode:
        """
        根据意图路由到协同模式

        意图 → 模式映射:
            PREDICT → A (全栈预测)
            ANALYZE/odds → B (赔率深挖)
            ANALYZE/team → C (平局攻坚)
            BACKTEST → D (系统迭代)
            OPTIMIZE → D (系统迭代)
            EXPLAIN → C (平局攻坚)
        """
        routing = {
            "predict": CollaborationMode.FULL_STACK,
            "analyze": {
                "odds_analysis": CollaborationMode.ODDS_DEEP,
                "market_analysis": CollaborationMode.ODDS_DEEP,
                "team_analysis": CollaborationMode.DRAW_FOCUS,
                "tactical_analysis": CollaborationMode.DRAW_FOCUS,
            },
            "backtest": CollaborationMode.SYSTEM_ITERATE,
            "optimize": CollaborationMode.SYSTEM_ITERATE,
            "explain": CollaborationMode.DRAW_FOCUS,
        }

        if intent_category in routing:
            route = routing[intent_category]
            if isinstance(route, dict):
                return route.get(intent_subtype, CollaborationMode.FULL_STACK)
            return route

        return CollaborationMode.FULL_STACK  # 默认全栈

    def get_workflow_description(self, mode: CollaborationMode) -> str:
        """获取模式的工作流描述"""
        descriptions = {
            CollaborationMode.FULL_STACK: (
                "【全栈预测模式 A】\n"
                "6位算法专家并行分析：\n"
                "  季泊松 → 泊松概率建模 + 赔率漂移特征\n"
                "  杜博弈 → 庄家赔率逆向 + 诱盘检测\n"
                "  荣合众 → 集成融合 + 动态权重\n"
                "  曾均衡 → 平局精细化 + 冷门预警\n"
                "  施时序 → 序列状态分析\n"
                "  毕建模 → 统计验证 + 置信区间\n"
                "→ 荣合众主导融合 → 郝优算把关输出"
            ),
            CollaborationMode.ODDS_DEEP: (
                "【赔率深挖模式 B】\n"
                "杜博弈主导 → 庄家赔率逆向工程 (诱盘检测/凯利分析/RP屏障)\n"
                "  季泊松辅助 → 赔率漂移微积分量化\n"
                "  毕建模辅助 → 异常显著性检验\n"
                "→ 杜博弈汇总输出赔率深度报告"
            ),
            CollaborationMode.DRAW_FOCUS: (
                "【平局攻坚模式 C】\n"
                "曾均衡主导 → Draw专属特征 + 代价敏感 + 阈值寻优\n"
                "  季泊松辅助 → 泊松λ_H≈λ_A条件验证\n"
                "  荣合众辅助 → D-Gate权重调整\n"
                "→ 曾均衡汇总输出平局优化方案"
            ),
            CollaborationMode.SYSTEM_ITERATE: (
                "【系统迭代模式 D】\n"
                "全团8位算法专家联动：\n"
                "  Phase 1: 并行诊断 (各专家独立分析)\n"
                "  Phase 2: 郝优算交叉评审 → 拆解优化任务\n"
                "  Phase 3: 并行优化 (各专家输出方案)\n"
                "  Phase 4: 毕建模统计验证 → 工程落地评估\n"
                "→ 郝优算汇编最终优化方案"
            ),
        }
        return descriptions.get(mode, "未知模式")


# ═══════════════════════════════════════════════════════════════
# 6. 便捷函数
# ═══════════════════════════════════════════════════════════════

_hub_instance: Optional[ExpertHubV2] = None


def get_hub() -> ExpertHubV2:
    """获取 ExpertHubV2 单例"""
    global _hub_instance
    if _hub_instance is None:
        _hub_instance = ExpertHubV2()
    return _hub_instance


def reset_hub():
    """重置单例(测试用)"""
    global _hub_instance
    _hub_instance = None


def describe_experts() -> str:
    """生成专家团描述"""
    hub = get_hub()
    lines = ["=== FootballAI v4.0 专家团 ===\n"]
    lines.append("【算法技术序列 8人】")
    for name in ["郝优算", "季泊松", "杜博弈", "荣合众", "曾均衡", "施时序", "毕建模", "齐优化"]:
        spec = hub.get_expert(name)
        if spec:
            lines.append(f"  {name} ({spec.domain.value}): {spec.description[:50]}...")
    lines.append("\n【工程落地序列 4人】")
    for name in ["傅稳当", "舒治理", "孙加速", "杨界面"]:
        spec = hub.get_expert(name)
        if spec:
            lines.append(f"  {name} ({spec.domain.value}): {spec.description[:50]}...")
    return "\n".join(lines)

"""
哨响AI - 增强模块包 v3.0
==========================
从Football项目迁移并适配的独立分析模块
v3.0 新增: 可扩展多专家系统架构

核心架构:
    ExpertProtocol    — 统一专家接口协议 + 状态机
    ExpertRegistry    — 中心注册表 + 动态发现
    ModuleRouter      — 可插拔动态路由
    ExpertHub         — 中心调度器
    ProgressiveOptimizer — 渐进式优化管道
"""
# ---- 业务模块 (容错导入: 模块不存在时不阻塞) ----
import logging as _logging
_logger = _logging.getLogger(__name__)

_try_imports = {
    'RefereeInfluenceModel': ('referee_model', 'RefereeInfluenceModel'),
    'RefereeProfile': ('referee_model', 'RefereeProfile'),
    'TimeSpaceFaultZoneDetector': ('timespace_detector', 'TimeSpaceFaultZoneDetector'),
    'TimeSpaceFault': ('timespace_detector', 'TimeSpaceFault'),
    'CrossMarketArbitrage': ('arbitrage_detector', 'CrossMarketArbitrage'),
    'ArbitrageOpportunity': ('arbitrage_detector', 'ArbitrageOpportunity'),
    'EnhancedUpsetDetector': ('upset_detector', 'EnhancedUpsetDetector'),
    'UpsetAnalysis': ('upset_detector', 'UpsetAnalysis'),
    'UpsetSignal': ('upset_detector', 'UpsetSignal'),
    'detect_upset_enhanced': ('upset_detector', 'detect_upset_enhanced'),
    'GoalTimingAnalyzer': ('goal_timing', 'GoalTimingAnalyzer'),
    'CornerStats': ('goal_timing', 'CornerStats'),
    'KellyCalculator': ('goal_timing', 'KellyCalculator'),
    'HalfTimeAnalyzer': ('goal_timing', 'HalfTimeAnalyzer'),
    'KeeperRiskModel': ('goalkeeper_model', 'KeeperRiskModel'),
    'AttackEfficiencyModel': ('attack_efficiency', 'AttackEfficiencyModel'),
    'AlwaysHomeBaseline': ('baseline', 'AlwaysHomeBaseline'),
    'LogisticBaseline': ('baseline', 'LogisticBaseline'),
    'RankOnlyBaseline': ('baseline', 'RankOnlyBaseline'),
    'BaselineComparator': ('baseline', 'BaselineComparator'),
    'ExpertManager': ('expert_manager', 'ExpertManager'),
    'ExpertStatus': ('expert_manager', 'ExpertStatus'),
    'ExpertRecord': ('expert_manager', 'ExpertRecord'),
    'PhaseConfig': ('expert_manager', 'PhaseConfig'),
    'create_expert_registry': ('expert_manager', 'create_expert_registry'),
}

for _name, (_mod, _cls) in _try_imports.items():
    try:
        _m = __import__(f'modules.{_mod}', fromlist=[_cls])
        globals()[_name] = getattr(_m, _cls)
    except (ImportError, AttributeError) as _e:
        globals()[_name] = None
        _logger.debug(f"Optional module {_mod}.{_cls} not available: {_e}")

# ---- v3.0 多专家系统架构 ----
from .expert_protocol import (
    ExpertProtocol,
    ExpertState,
    ExpertMeta,
    InputSchema,
    OutputSchema,
    TrainingConfig,
    ExpertPerformance,
    RuleBasedExpert,
    LearnableExpert,
    ExpertAdapter,
    create_expert_from_config,
    StateTransition,
)
from .expert_registry import ExpertRegistry
from .module_router import ModuleRouter, build_preset_scorers, create_default_scorer_from_meta
from .expert_hub import ExpertHub           # LEGACY v3.0 — 新代码请用 ExpertHubV2
from .progressive_optimizer import ProgressiveOptimizer

# ---- v4.0 架构升级模块 (2026-06-18) ----
_v4_imports = {
    'UnifiedPrediction': ('output_schema', 'UnifiedPrediction'),
    'FusedPrediction': ('output_schema', 'FusedPrediction'),
    'ThreeWayProbability': ('output_schema', 'ThreeWayProbability'),
    'create_simple_prediction': ('output_schema', 'create_simple_prediction'),
    'create_fallback_prediction': ('output_schema', 'create_fallback_prediction'),
    'IntentClassifierV2': ('intent_classifier_v2', 'IntentClassifierV2'),
    'RouteResult': ('intent_classifier_v2', 'RouteResult'),
    'classify_intent': ('intent_classifier_v2', 'classify_intent'),
    'ExpertHubV2': ('expert_hub_v2', 'ExpertHubV2'),
    'CollaborationScheduler': ('expert_hub_v2', 'CollaborationScheduler'),
    'CollaborationMode': ('expert_hub_v2', 'CollaborationMode'),
    'describe_experts': ('expert_hub_v2', 'describe_experts'),
}

for _name, (_mod, _cls) in _v4_imports.items():
    try:
        _m = __import__(f'modules.{_mod}', fromlist=[_cls])
        globals()[_name] = getattr(_m, _cls)
    except (ImportError, AttributeError) as _e:
        globals()[_name] = None
        _logger.debug(f"V4 module {_mod}.{_cls} not available: {_e}")

__all__ = [
    # 业务模块
    'RefereeInfluenceModel', 'RefereeProfile',
    'TimeSpaceFaultZoneDetector', 'TimeSpaceFault',
    'CrossMarketArbitrage', 'ArbitrageOpportunity',
    'EnhancedUpsetDetector', 'UpsetAnalysis', 'UpsetSignal', 'detect_upset_enhanced',
    'GoalTimingAnalyzer', 'CornerStats', 'KellyCalculator', 'HalfTimeAnalyzer',
    'KeeperRiskModel', 'AttackEfficiencyModel',
    'AlwaysHomeBaseline', 'LogisticBaseline', 'RankOnlyBaseline', 'BaselineComparator',
    'ExpertManager', 'ExpertStatus', 'ExpertRecord', 'PhaseConfig', 'create_expert_registry',
    # v3.0 多专家架构
    'ExpertProtocol', 'ExpertState', 'ExpertMeta', 'InputSchema', 'OutputSchema',
    'TrainingConfig', 'ExpertPerformance',
    'RuleBasedExpert', 'LearnableExpert', 'ExpertAdapter',
    'create_expert_from_config', 'StateTransition',
    'ExpertRegistry', 'ModuleRouter', 'ExpertHub', 'ProgressiveOptimizer',
    'build_preset_scorers', 'create_default_scorer_from_meta',
]

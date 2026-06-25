#!/usr/bin/env python3
"""
LEGACY v3.0 — 哨响AI 专家中心调度器 (ExpertHub)
=================================================
v5.2.14 状态: 已被 expert_hub_v2 替代。此模块保留用于 v3 旧版 ExpertAgent
向后兼容。新代码请使用 ExpertHubV2 + CollaborationScheduler。

职责:
    1. 统一入口: 所有预测请求通过 Hub 分发
    2. 中心调度: 自动路由 → 并行执行 → 结果聚合
    3. 集成管理: 管理注册表、路由器、优化器的协同
    4. 监控面板: 统一的系统状态和性能报告

架构:
    predict() 流程:
        请求 → FeatureExtractor(特征提取)
             → ModuleRouter(适用性评分+选择)
             → 并行执行(选中专家)
             → ResultAggregator(结果融合)
             → 输出

对比旧版:
    旧版 AgentOrchestrator 中硬编码了10个agent + 固定3层架构
    新版 ExpertHub 完全可插拔: 专家/评分器/聚合器均可配置

用法:
    hub = ExpertHub()
    hub.load_default_experts()        # 加载10个旧版专家
    hub.register_new_expert(...)      # 注册新专家
    result = hub.predict(match_data)
    hub.feedback(match_data, actual)  # 赛后反馈
"""
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable

from .expert_protocol import (
    ExpertProtocol, ExpertState, ExpertMeta,
    InputSchema, OutputSchema,
    ExpertAdapter, RuleBasedExpert, LearnableExpert,
    StateTransition,
)
from .expert_registry import ExpertRegistry
from .module_router import ModuleRouter, build_preset_scorers

logger = logging.getLogger(__name__)


class ExpertHub:
    """
    专家中心调度器 — 整个多专家系统的唯一入口

    属性:
        registry: ExpertRegistry — 专家注册中心
        router: ModuleRouter — 动态路由
        optimizer: ProgressiveOptimizer | None — 渐进式优化器
    """

    # 调度配置默认值
    DEFAULT_CONFIG = {
        'max_workers': 10,              # 并行执行线程数
        'execution_timeout_s': 15.0,    # 单专家超时
        'min_active_experts': 2,        # 最少需要活跃专家
        'enable_parallel': True,        # 是否并行执行
        'enable_fallback': True,        # 是否启用后备专家
        'result_aggregation': 'weighted_vote',  # 聚合策略: weighted_vote | soft_voting | stacking
        'track_performance': True,      # 是否追踪性能
    }

    def __init__(self, config: Dict = None):
        """
        Args:
            config: 调度配置字典(覆盖默认值)
        """
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self.registry = ExpertRegistry()
        self.router = ModuleRouter()
        self.optimizer = None  # 延迟初始化

        # 运行时状态
        self._feature_extractor = None  # 特征提取器(可注入)
        self._result_aggregator = None  # 结果聚合器(可注入)
        self._call_count = 0
        self._total_predictions = 0
        self._last_full_result = None
        self._started_at = datetime.now()

        # 注册统计
        self._expert_stats: Dict[str, Dict] = {}

        logger.info("[ExpertHub] 初始化完成")

    # ================================================================
    # 初始化
    # ================================================================

    def load_default_experts(self, use_legacy_adapter: bool = True) -> int:
        """
        加载默认10个旧版专家(通过 ExpertAdapter 包装)

        这是向后兼容的快速启动方法。
        新系统上线后可以逐步迁移到原生 ExpertProtocol。
        """
        if not use_legacy_adapter:
            return 0

        legacy_map = {
            'trend_analyzer':      ('agents.experts.trend_agent',      'TrendAgent',      'trend', 1, "趋势分析"),
            'alpha_decision':      ('agents.experts.alpha_agent',      'AlphaAgent',      'value', 2, "Alpha决策"),
            'referee_model':       ('agents.experts.referee_agent',    'RefereeAgent',    'risk', 2, "裁判分析"),
            'upset_detector':      ('agents.experts.upset_agent',      'UpsetAgent',      'risk', 4, "冷门检测"),
            'media_intelligence':  ('agents.experts.media_agent',      'MediaAgent',       'auxiliary', 4, "媒体情报"),
            'coach_tactics':       ('agents.experts.coach_agent',      'CoachAgent',       'tactical', 2, "教练战术"),
            'quant_trader':        ('agents.experts.quant_agent',      'QuantAgent',       'value', 2, "量化交易"),
            'timespace_detector':  ('agents.experts.timespace_agent',  'TimeSpaceAgent',   'temporal', 3, "时空检测"),
            'arbitrage_detector':  ('agents.experts.arbitrage_agent',  'ArbitrageAgent',   'value', 4, "套利检测"),
            'goal_timing':         ('agents.experts.goal_timing_agent','GoalTimingAgent',  'temporal', 3, "进球时序"),
        }

        count = 0
        for expert_id, (module_path, class_name, category, phase, display_name) in legacy_map.items():
            try:
                import importlib
                module = importlib.import_module(module_path)
                cls = getattr(module, class_name)
                legacy_agent = cls()

                meta = ExpertMeta(
                    expert_id=expert_id,
                    display_name=display_name,
                    category=category,
                    phase=phase,
                    state=ExpertState.ACTIVE,
                    description=f'Legacy adapter: {display_name}',
                    tags=[category, f'phase{phase}'],
                )
                adapter = ExpertAdapter(legacy_agent, expert_id, meta)
                self.registry.register(adapter)
                count += 1
            except (Exception, KeyError, IndexError) as e:
                logger.warning(f"加载旧版专家失败 {expert_id}: {e}")

        logger.info(f"[ExpertHub] 加载 {count}/{len(legacy_map)} 个旧版专家")

        # 加载预设评分器
        for expert_id, scorer in build_preset_scorers().items():
            self.router.register_scorer(expert_id, scorer)

        return count

    def register_new_expert(self, expert: ExpertProtocol,
                            scorer: Callable = None) -> bool:
        """
        注册新专家(完整流程)

        1. 注册到 Registry
        2. 注册评分器到 Router
        3. 如果是 COLD_START 状态，启动渐进式优化

        Args:
            expert: 实现了 ExpertProtocol 的专家
            scorer: 适用性评分函数 (可选，未提供则用默认)
        """
        # 注册到注册表
        self.registry.register(expert)

        # 注册评分器
        if scorer:
            self.router.register_scorer(expert.meta.expert_id, scorer)
        else:
            from .module_router import create_default_scorer_from_meta
            self.router.register_scorer(
                expert.meta.expert_id,
                create_default_scorer_from_meta(expert.meta)
            )

        # 初始化专家统计
        self._expert_stats[expert.meta.expert_id] = {
            'calls': 0, 'errors': 0, 'fallbacks': 0,
            'total_time_ms': 0, 'first_called': None,
        }

        # 如果是 COLD_START，关联优化器
        if expert.meta.state == ExpertState.COLD_START and self.optimizer:
            self.optimizer.register_expert(expert)

        logger.info(f"[ExpertHub] 新专家已注册: {expert.meta.expert_id}")
        return True

    def set_optimizer(self, optimizer):
        """注入渐进式优化器"""
        from .progressive_optimizer import ProgressiveOptimizer
        self.optimizer = optimizer
        optimizer._hub = self
        logger.info("[ExpertHub] 优化器已注入")

    def set_feature_extractor(self, extractor):
        """注入特征提取器"""
        self._feature_extractor = extractor

    def set_result_aggregator(self, aggregator):
        """注入结果聚合器"""
        self._result_aggregator = aggregator

    # ================================================================
    # 预测 — 核心调度
    # ================================================================

    def predict(self, match_data: Dict, context: Dict = None) -> Dict:
        """
        对一场比赛执行完整的多专家预测

        流程:
            1. 特征提取
            2. 适用性评分 + 专家选择
            3. 并行执行选中的专家
            4. 结果聚合
            5. 返回统一结果

        Args:
            match_data: 比赛数据
            context: 额外上下文(可选)

        Returns:
            {
                'prediction': {'home': float, 'draw': float, 'away': float},
                'confidence': float,
                'selected_experts': [...],
                'all_predictions': {...},
                'metadata': {...},
            }
        """
        self._call_count += 1
        start_time = time.perf_counter()
        context = context or {}

        # Step 1: 特征提取
        features = match_data
        if self._feature_extractor:
            try:
                features = self._feature_extractor(match_data)
            except (Exception) as e:
                logger.warning(f"特征提取失败: {e}")
        match_data['_features'] = features

        # Step 2: 适用性评分 + 选择
        scores = self.router.score_all(features)
        active_experts = list(self.registry.get_active().keys())
        selected = self.router.select(scores, active_experts)

        # 确保有足够的专家
        if len(selected) < self.config['min_active_experts']:
            logger.warning(f"活跃专家不足: {len(selected)}/{self.config['min_active_experts']}")
            # 尝试增加后备
            for fb_id in self.router.fallback_modules:
                if fb_id in active_experts and fb_id not in selected:
                    selected.append(fb_id)
                if len(selected) >= self.config['min_active_experts']:
                    break

        # Step 3: 并行执行专家
        expert_results = self._execute_parallel(match_data, features, selected)

        # Step 4: 结果聚合
        final = self._aggregate_results(expert_results, scores)

        # Step 5: 构建返回
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        self._total_predictions += 1
        self._last_full_result = {
            'prediction': final['prediction'],
            'confidence': final['confidence'],
            'selected_experts': selected,
            'all_predictions': expert_results,
            'scores': {k: round(v, 4) for k, v in scores.items() if k in selected},
            'metadata': {
                'call_count': self._call_count,
                'total_experts': len(active_experts),
                'selected_count': len(selected),
                'execution_time_ms': round(elapsed_ms, 2),
                'timestamp': datetime.now().isoformat(),
            },
        }
        return self._last_full_result

    def _execute_parallel(self, match_data: Dict, features: Dict,
                          expert_ids: List[str]) -> Dict[str, Dict]:
        """并行执行选中的专家"""
        results = {}

        if not self.config['enable_parallel'] or len(expert_ids) <= 1:
            # 串行执行
            for eid in expert_ids:
                results[eid] = self._execute_single(eid, match_data, features)
        else:
            # 并行执行
            with ThreadPoolExecutor(max_workers=self.config['max_workers']) as executor:
                futures = {
                    executor.submit(self._execute_single, eid, match_data, features): eid
                    for eid in expert_ids
                }
                for future in as_completed(futures, timeout=self.config['execution_timeout_s'] + 5):
                    eid = futures[future]
                    try:
                        results[eid] = future.result(timeout=self.config['execution_timeout_s'])
                    except (Exception, KeyError, IndexError) as e:
                        logger.error(f"专家 {eid} 执行异常: {e}")
                        results[eid] = self._build_error_result(eid, str(e))

        return results

    def _execute_single(self, expert_id: str, match_data: Dict,
                        features: Dict) -> Dict:
        """执行单个专家预测(带超时+统计)"""
        expert = self.registry.get(expert_id)
        if expert is None:
            return self._build_error_result(expert_id, "expert not found")

        # 初始化统计
        if expert_id not in self._expert_stats:
            self._expert_stats[expert_id] = {
                'calls': 0, 'errors': 0, 'fallbacks': 0,
                'total_time_ms': 0, 'first_called': datetime.now().isoformat(),
            }

        stats = self._expert_stats[expert_id]
        stats['calls'] += 1
        if stats['first_called'] is None:
            stats['first_called'] = datetime.now().isoformat()

        start = time.perf_counter()
        try:
            context = {
                'applicability_score': features.get('_scores', {}).get(expert_id, 0.5),
                'features': features,
            }
            result = expert.predict(match_data, context)
            elapsed_ms = (time.perf_counter() - start) * 1000

            stats['total_time_ms'] += elapsed_ms
            if result.get('status') == 'fallback':
                stats['fallbacks'] += 1

            return result
        except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
            elapsed_ms = (time.perf_counter() - start) * 1000
            stats['errors'] += 1
            stats['total_time_ms'] += elapsed_ms
            logger.error(f"专家 {expert_id} 异常: {e}")
            return self._build_error_result(expert_id, str(e))

    def _aggregate_results(self, expert_results: Dict[str, Dict],
                           scores: Dict[str, float]) -> Dict:
        """
        聚合多个专家预测结果

        策略: weighted_vote (默认) | soft_voting | stacking
        """
        strategy = self.config['result_aggregation']

        if self._result_aggregator:
            return self._result_aggregator(expert_results, scores)

        if strategy == 'weighted_vote':
            return self._weighted_vote_aggregate(expert_results, scores)
        elif strategy == 'soft_voting':
            return self._soft_voting_aggregate(expert_results)
        else:
            return self._weighted_vote_aggregate(expert_results, scores)

    def _weighted_vote_aggregate(self, expert_results: Dict[str, Dict],
                                  scores: Dict[str, float]) -> Dict:
        """加权投票聚合"""
        if not expert_results:
            logger.warning("[ExpertHub] 无专家可用，返回无预测")
            return {
                'prediction': None,   # ★ 战时修复：不再返回假概率
                'confidence': 0.0,
                'explanation': '无专家可用',
            }

        total_weight = 0.0
        weighted = {'home': 0.0, 'draw': 0.0, 'away': 0.0}
        confidence_sum = 0.0
        n_valid = 0

        for eid, result in expert_results.items():
            if result.get('status') == 'error':
                continue

            pred = result.get('prediction', {})
            if not isinstance(pred, dict):
                continue

            # 权重 = 适用性分数 × 专家置信度
            applicability = scores.get(eid, 0.5)
            expert_conf = result.get('confidence', 0.5)
            weight = applicability * expert_conf

            for key in ['home', 'draw', 'away']:
                weighted[key] += pred.get(key, 0.33) * weight

            total_weight += weight
            confidence_sum += expert_conf
            n_valid += 1

        if total_weight > 0:
            prediction = {k: v / total_weight for k, v in weighted.items()}
        else:
            logger.warning("[ExpertHub] 所有权重为 0，返回无预测")
            prediction = None   # ★ 战时修复：不再返回假概率

        avg_confidence = confidence_sum / n_valid if n_valid > 0 else 0.1

        # 判断结果
        outcome = max(prediction, key=prediction.get)
        cn_map = {'home': '主胜', 'draw': '平局', 'away': '客胜'}

        return {
            'prediction': prediction,
            'confidence': round(avg_confidence, 4),
            'outcome': outcome,
            'outcome_cn': cn_map.get(outcome, outcome),
            'explanation': f'{n_valid}个专家参与聚合，预测{cn_map.get(outcome, outcome)}',
            'contributor_count': n_valid,
        }

    def _soft_voting_aggregate(self, expert_results: Dict[str, Dict]) -> Dict:
        """软投票聚合(无权重)"""
        if not expert_results:
            return self._weighted_vote_aggregate({}, {})  # fallback

        summed = {'home': 0.0, 'draw': 0.0, 'away': 0.0}
        n = 0

        for result in expert_results.values():
            pred = result.get('prediction', {})
            if isinstance(pred, dict):
                for key in summed:
                    summed[key] += pred.get(key, 0.33)
                n += 1

        if n > 0:
            prediction = {k: v / n for k, v in summed.items()}
        else:
            logger.warning("[ExpertHub] soft_vote 无有效专家，返回无预测")
            prediction = None   # ★ 战时修复：不再返回假概率

        return {
            'prediction': prediction,
            'confidence': round(min(0.9, n / max(1, len(expert_results))), 4),
            'contributor_count': n,
        }

    def _build_error_result(self, expert_id: str, reason: str) -> Dict:
        return {
            'expert_id': expert_id,
            'prediction': None,   # ★ 战时修复：错误结果不再携带假概率
            'confidence': 0.0,
            'reasoning': f'Error: {reason}',
            'execution_time_ms': 0,
            'status': 'error',
        }

    # ================================================================
    # 赛后反馈
    # ================================================================

    def feedback(self, match_data: Dict, actual_result: str,
                 prediction_result: Dict = None) -> Dict:
        """
        赛后反馈 — 更新专家性能

        Args:
            match_data: 原始比赛数据
            actual_result: 实际结果 (home/draw/away)
            prediction_result: 之前 predict() 的返回(可选，默认用缓存)

        Returns:
            学习结果
        """
        if prediction_result is None:
            prediction_result = self._last_full_result

        if prediction_result is None:
            return {'error': '无可用预测结果'}

        expert_results = prediction_result.get('all_predictions', {})

        # 更新每个专家的性能
        feedbacks = {}
        for eid, result in expert_results.items():
            expert = self.registry.get(eid)
            if expert:
                expert.update_performance(actual_result, result)

                # 检查退化
                perf = expert.meta.performance
                if perf.degradation_triggered and \
                   expert.meta.state == ExpertState.ACTIVE:
                    self.registry.transition_state(eid, StateTransition.DEGRADE)
                    feedbacks[eid] = {'degraded': True,
                                     'recent_acc': sum(perf.last_n_accuracy[-20:]) / 20}

        # 聚合结果是否正确
        final_pred = prediction_result.get('prediction', {})
        pred_outcome = max(final_pred, key=final_pred.get) if final_pred else 'draw'
        is_correct = (pred_outcome == actual_result)

        return {
            'is_correct': is_correct,
            'predicted': pred_outcome,
            'actual': actual_result,
            'expert_feedbacks': feedbacks,
            'timestamp': datetime.now().isoformat(),
        }

    # ================================================================
    # 系统状态
    # ================================================================

    def get_system_status(self) -> Dict:
        """获取系统完整状态"""
        registry_stats = self.registry.get_statistics()

        expert_details = {}
        for eid, expert in self.registry.get_all().items():
            expert_details[eid] = expert.get_status_report()
            if eid in self._expert_stats:
                expert_details[eid].update({
                    'calls': self._expert_stats[eid]['calls'],
                    'errors': self._expert_stats[eid]['errors'],
                })

        return {
            'uptime_seconds': (datetime.now() - self._started_at).total_seconds(),
            'total_predictions': self._total_predictions,
            'registry': registry_stats,
            'experts': expert_details,
            'router': {
                'scorers': self.router.get_all_scorers(),
                'min_modules': self.router.min_modules,
                'max_modules': self.router.max_modules,
            },
            'optimizer': self.optimizer.get_status() if self.optimizer else None,
        }

    def print_status(self):
        """打印格式化的系统状态"""
        status = self.get_system_status()
        reg = status['registry']

        print("=" * 70)
        print("  哨响AI 多专家系统 — ExpertHub 状态")
        print("=" * 70)
        print(f"  运行时间: {status['uptime_seconds']:.0f}s")
        print(f"  总预测数: {status['total_predictions']}")
        print(f"  注册专家: {reg['total_experts']}  活跃: {reg['active_count']}  可训练: {reg['trainable_count']}")
        print(f"\n  按状态分布:")
        for state, count in reg.get('by_state', {}).items():
            print(f"    {state}: {count}")
        print(f"\n  按分类分布:")
        for cat, count in reg.get('by_category', {}).items():
            print(f"    {cat}: {count}")
        print(f"\n  专家详情:")
        print(f"  {'ID':<25} {'状态':<18} {'准确率':<8} {'调用':<6} {'分类':<10}")
        print(f"  {'-'*68}")
        for eid, detail in status['experts'].items():
            acc = detail.get('accuracy', 0)
            calls = detail.get('calls', 0)
            state = detail.get('state', '?')
            cat = detail.get('category', '?')
            print(f"  {eid:<25} {state:<18} {acc:<8.4f} {calls:<6} {cat:<10}")
        print("=" * 70)

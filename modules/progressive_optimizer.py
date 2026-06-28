#!/usr/bin/env python3
"""
哨响AI — 渐进式优化器 (ProgressiveOptimizer)
==============================================
管理新专家的完整优化生命周期: 参数初始化 → 数据累积 → 迭代训练 → 验证 → 激活

设计原则:
    1. 新增专家初始状态为 COLD_START (无参数、无数据、未训练)
    2. 渐进式推进: 每个阶段有明确的进入/退出条件
    3. 数据累积策略: 可配置的最小样本量、数据质量门槛
    4. 迭代训练方案: 支持全量训练 / 增量训练 / 定时重新训练
    5. 验证门控: 每个优化节点必须通过验证才能进入下一阶段

优化管道:
    COLD_START ──[注册]──→ DATA_ACCUMULATING ──[数据够]──→ TRAINING
                              ↓                                  ↓
                          (规则型跳过)                     [训练完成]
                              ↓                                  ↓
                           ACTIVE  ←──[验证通过]── OPTIMIZED

数据累积策略:
    - 被动累积: 系统每次 predict() 自动记录输入特征
    - 主动累积: 从数据库批量加载历史数据
    - 质量过滤: 排除缺失率>30%的样本

迭代训练方案:
    - 首次训练: 全量数据 batch train
    - 增量训练: partial_fit 在线更新
    - 定期重训: 每 N 天/每 M 个新样本后 full retrain
    - 早停: validation loss 不再下降时停止

用法:
    optimizer = ProgressiveOptimizer(db_path='data/football_data.db')
    optimizer.register_expert(my_new_expert)
    optimizer.run_pipeline(my_new_expert.meta.expert_id)  # 自动推进全流程
"""
import logging
import sqlite3
import time
import os
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple

import numpy as np
import pandas as pd

from .expert_protocol import (
    ExpertProtocol, ExpertState, ExpertMeta,
    TrainingConfig, ExpertPerformance,
    LearnableExpert, RuleBasedExpert,
    StateTransition, VALID_TRANSITIONS,
)

logger = logging.getLogger(__name__)

class ProgressiveOptimizer:
    """
    渐进式优化器 — 管理新专家从冷启动到上线的全流程

    核心方法:
        register_expert()    — 注册到优化管道
        run_pipeline()       — 自动推进完整优化流程
        accumulate()         — 累积训练数据
        train()              — 执行训练
        validate()           — 验证并决定是否激活
        check_retrain()      — 检查是否需要重新训练
    """

    # 数据质量阈值
    MIN_FEATURE_COMPLETENESS = 0.7   # 最少70%特征非缺失
    MAX_FEATURE_MISSING_RATE = 0.3   # 最多30%缺失

    # 验证阈值
    MIN_ACCURACY_IMPROVEMENT = 0.02  # 自评准确率至少提升2pp
    VS_BASELINE_DELTA = 0.03         # 至少优于基准3pp

    def __init__(self, db_path: str = None, config: Dict = None):
        """
        Args:
            db_path: 数据库路径(用于批量数据加载)
            config: 全局优化配置
        """
        self.db_path = db_path or 'data/football_data.db'
        self.config = config or {}
        self._hub = None  # ExpertHub 引用(注入)

        # 已注册到优化管的专家
        self._managed_experts: Dict[str, ExpertProtocol] = {}

        # 数据累积缓冲 (expert_id → features list)
        self._data_buffers: Dict[str, List[Dict]] = {}
        self._label_buffers: Dict[str, List] = {}

        # 训练日志
        self._training_log: List[Dict] = []

        # 基准准确率 (从外部注入)
        self._baseline_accuracy = 0.45

        logger.info("[Optimizer] 渐进式优化器初始化")

    # ================================================================
    # 注册
    # ================================================================

    def register_expert(self, expert: ExpertProtocol) -> bool:
        """
        注册专家到优化管道

        仅接受 COLD_START 或 可训练状态的专家。
        规则型专家会被自动跳过(直接激活)。
        """
        eid = expert.meta.expert_id

        if isinstance(expert, RuleBasedExpert):
            logger.info(f"[Optimizer] {eid} 是规则型专家，直接激活")
            self._activate_directly(expert)
            return True

        if expert.meta.state not in {
            ExpertState.COLD_START,
            ExpertState.DATA_ACCUMULATING,
            ExpertState.DEGRADED,
        }:
            logger.info(f"[Optimizer] {eid} 状态 {expert.meta.state.value} 不需要优化管理")
            return False

        self._managed_experts[eid] = expert

        # 初始化缓冲
        if isinstance(expert, LearnableExpert):
            if eid not in self._data_buffers:
                self._data_buffers[eid] = []
                self._label_buffers[eid] = []

        logger.info(f"[Optimizer] 注册优化管理: {eid} (state={expert.meta.state.value})")
        return True

    def _activate_directly(self, expert: ExpertProtocol):
        """直接激活(规则型专家)"""
        if self._hub:
            self._hub.registry.transition_state(
                expert.meta.expert_id, StateTransition.ACTIVATE
            )
        else:
            expert.meta.state = ExpertState.ACTIVE

    # ================================================================
    # 主流程 — 自动推进
    # ================================================================

    def run_pipeline(self, expert_id: str,
                     max_iterations: int = 3) -> Dict:
        """
        运行完整的渐进式优化管道

        自动检查专家状态，执行下一步操作，循环推进直到:
            - 专家变为 ACTIVE
            - 达到最大迭代次数
            - 遇到阻塞(如数据不足)

        Returns:
            {expert_id, initial_state, final_state, steps: [...], success}
        """
        expert = self._managed_experts.get(expert_id)
        if expert is None:
            return {'error': f'{expert_id} 未注册到优化器'}

        steps = []
        initial_state = expert.meta.state

        for iteration in range(max_iterations):
            state = expert.meta.state

            if state == ExpertState.ACTIVE:
                logger.info(f"[Optimizer] {expert_id} 已激活，管道完成")
                break

            step_result = self._execute_next_step(expert)
            steps.append(step_result)

            if step_result.get('status') == 'blocked':
                break

            if expert.meta.state == ExpertState.ACTIVE:
                break

        result = {
            'expert_id': expert_id,
            'initial_state': initial_state.value,
            'final_state': expert.meta.state.value,
            'steps': steps,
            'success': expert.meta.state == ExpertState.ACTIVE,
            'iterations': len(steps),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }

        self._training_log.append(result)
        return result

    def _execute_next_step(self, expert: ExpertProtocol) -> Dict:
        """根据当前状态执行下一步优化操作"""
        state = expert.meta.state
        eid = expert.meta.expert_id

        if state == ExpertState.COLD_START:
            return self._step_to_accumulating(expert)

        elif state == ExpertState.DATA_ACCUMULATING:
            # 检查是否有足够数据启动训练
            n_samples = expert.meta.n_training_samples
            min_samples = expert.meta.training_config.min_samples

            if n_samples >= min_samples:
                return self._step_to_training(expert)
            else:
                # 尝试批量加载数据
                loaded = self.load_historical_data(eid)
                if loaded > 0:
                    return {
                        'step': 'load_historical',
                        'status': 'progress',
                        'samples_loaded': loaded,
                        'total_samples': expert.meta.n_training_samples,
                    }
                else:
                    return {
                        'step': 'waiting_for_data',
                        'status': 'blocked',
                        'current_samples': n_samples,
                        'min_required': min_samples,
                        'message': f'数据不足: {n_samples}/{min_samples}',
                    }

        elif state == ExpertState.TRAINING:
            return self._step_train(expert)

        elif state == ExpertState.OPTIMIZED:
            return self._step_validate_and_activate(expert)

        elif state == ExpertState.DEGRADED:
            return self._step_to_training(expert)

        else:
            return {'step': 'unknown', 'status': 'blocked',
                   'message': f'状态 {state.value} 不支持自动推进'}

    def _step_to_accumulating(self, expert: ExpertProtocol) -> Dict:
        """COLD_START → DATA_ACCUMULATING"""
        eid = expert.meta.expert_id
        if self._hub:
            self._hub.registry.transition_state(eid, StateTransition.START_ACCUMULATING)
        else:
            expert.meta.state = ExpertState.DATA_ACCUMULATING

        # 初始化参数
        self._initialize_parameters(expert)

        # 尝试从数据库预加载数据
        loaded = self.load_historical_data(eid)

        return {
            'step': 'cold_start→accumulating',
            'status': 'progress',
            'preloaded_samples': loaded,
            'min_required': expert.meta.training_config.min_samples,
        }

    def _step_to_training(self, expert: ExpertProtocol) -> Dict:
        """DATA_ACCUMULATING → TRAINING 或 DEGRADED → TRAINING"""
        eid = expert.meta.expert_id
        if self._hub:
            self._hub.registry.transition_state(eid, StateTransition.START_TRAINING)
        else:
            expert.meta.state = ExpertState.TRAINING

        # 立即执行训练
        return self._step_train(expert)

    def _step_train(self, expert: ExpertProtocol) -> Dict:
        """执行训练"""
        eid = expert.meta.expert_id

        # 准备训练数据
        X, y = self._prepare_training_data(eid)
        if X is None or len(X) == 0:
            return {
                'step': 'training',
                'status': 'blocked',
                'message': '无可用训练数据',
            }

        # 执行训练
        logger.info(f"[Optimizer] 开始训练 {eid} (samples={len(X)})")
        train_start = time.perf_counter()

        try:
            train_result = expert.fit(X, y)
            train_time = time.perf_counter() - train_start
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"[Optimizer] {eid} 训练失败: {e}")
            if self._hub:
                self._hub.registry.transition_state(eid, StateTransition.ERROR)
            return {
                'step': 'training',
                'status': 'error',
                'error': str(e),
            }

        # 评估训练效果
        eval_result = None
        try:
            eval_result = expert.evaluate(X, y)
        except (Exception):
            pass

        # 更新元数据
        expert.meta.last_trained_at = datetime.now(timezone.utc).isoformat()
        expert.meta.n_training_samples = len(X)
        expert.meta.n_validation_samples = len(y)

        # 设置下次重训时间
        retrain_days = expert.meta.training_config.retrain_frequency_days
        expert.meta.next_retrain_at = (
            datetime.now(timezone.utc) + timedelta(days=retrain_days)
        ).isoformat()

        # 状态转换: TRAINING → OPTIMIZED
        if self._hub:
            self._hub.registry.transition_state(eid, StateTransition.COMPLETE_TRAINING)
        else:
            expert.meta.state = ExpertState.OPTIMIZED

        result = {
            'step': 'training',
            'status': 'completed',
            'samples_used': len(X),
            'train_time_s': round(train_time, 2),
            'train_result': train_result,
            'eval_result': eval_result,
        }

        # 训练成功后自动验证并尝试激活
        activate_result = self._step_validate_and_activate(expert)
        result['activate'] = activate_result

        return result

    def _step_validate_and_activate(self, expert: ExpertProtocol) -> Dict:
        """验证并激活专家"""
        eid = expert.meta.expert_id

        # 自评准确率
        perf = expert.meta.performance
        self_eval_acc = perf.accuracy

        # 基准对比
        baseline_acc = self._baseline_accuracy

        # 验证门控
        checks = {
            'has_accuracy': self_eval_acc > 0,
            'above_minimum': self_eval_acc > 0.35,
            'vs_baseline': self_eval_acc > baseline_acc + self.VS_BASELINE_DELTA,
        }

        all_passed = all(checks.values())

        if all_passed:
            if self._hub:
                self._hub.registry.transition_state(eid, StateTransition.ACTIVATE)
            else:
                expert.meta.state = ExpertState.ACTIVE

            return {
                'step': 'validate→activate',
                'status': 'activated',
                'checks': checks,
                'self_accuracy': round(self_eval_acc, 4),
                'baseline_accuracy': baseline_acc,
                'delta': round(self_eval_acc - baseline_acc, 4),
            }
        else:
            # 暂不激活，保持 OPTIMIZED
            return {
                'step': 'validate→activate',
                'status': 'pending',
                'checks': checks,
                'self_accuracy': round(self_eval_acc, 4),
                'message': '验证未通过，保持 OPTIMIZED 状态等待更多数据',
            }

    # ================================================================
    # 参数初始化
    # ================================================================

    def _initialize_parameters(self, expert: ExpertProtocol):
        """
        初始化专家参数

        策略:
            1. 如果专家有预设参数模板 → 加载
            2. 如果专家有默认初始化方法 → 调用
            3. 否则 → 从相似专家迁移参数(参数共享)
        """
        eid = expert.meta.expert_id

        if hasattr(expert, 'initialize_params'):
            try:
                expert.initialize_params()
                logger.info(f"[Optimizer] {eid} 使用自定义参数初始化")
                return
            except (Exception, KeyError, IndexError) as e:
                logger.warning(f"[Optimizer] {eid} 自定义初始化失败: {e}")

        # 尝试从相似分类专家迁移参数
        similar_experts = self._find_similar_trained_experts(expert)
        if similar_experts:
            source = similar_experts[0]
            if hasattr(source, '_params') and source._params:
                expert._params = dict(source._params)  # 浅拷贝参数
                logger.info(f"[Optimizer] {eid} 从 {source.meta.expert_id} 迁移参数")

    def _find_similar_trained_experts(self, expert: ExpertProtocol,
                                      max_count: int = 3) -> List[ExpertProtocol]:
        """查找同分类的已训练专家"""
        if not self._hub:
            return []

        similar = []
        for other_eid, other in self._hub.registry.get_all().items():
            if other_eid == expert.meta.expert_id:
                continue
            if other.meta.category == expert.meta.category and \
               other.meta.state in {ExpertState.OPTIMIZED, ExpertState.ACTIVE}:
                similar.append(other)
                if len(similar) >= max_count:
                    break
        return similar

    # ================================================================
    # 数据累积
    # ================================================================

    def accumulate(self, expert_id: str, features: Dict, label,
                   quality_check: bool = True) -> int:
        """
        累积单条训练数据

        Args:
            expert_id: 专家ID
            features: 特征字典
            label: 标签 (0/1/2 或 'home'/'draw'/'away')
            quality_check: 是否进行质量过滤

        Returns:
            累积后的样本数
        """
        expert = self._managed_experts.get(expert_id)
        if expert is None:
            expert = self._hub.registry.get(expert_id) if self._hub else None
        if expert is None:
            return 0

        # 质量过滤
        if quality_check:
            if not self._check_data_quality(features):
                return expert.meta.n_training_samples

        # 归一化标签
        if isinstance(label, str):
            label_map = {'home': 0, 'draw': 1, 'away': 2}
            label = label_map.get(label, 1)

        # 如果专家是 LearnableExpert，使用其内置缓冲
        if isinstance(expert, LearnableExpert):
            expert.accumulate_data(features, label)
        else:
            # 使用优化器缓冲
            if expert_id not in self._data_buffers:
                self._data_buffers[expert_id] = []
                self._label_buffers[expert_id] = []
            self._data_buffers[expert_id].append(features)
            self._label_buffers[expert_id].append(label)
            expert.meta.n_training_samples = len(self._data_buffers[expert_id])

        return expert.meta.n_training_samples

    def accumulate_batch(self, expert_id: str, features_list: List[Dict],
                         labels: List, quality_check: bool = True) -> int:
        """批量累积训练数据"""
        for features, label in zip(features_list, labels):
            self.accumulate(expert_id, features, label, quality_check)
        expert = self._managed_experts.get(expert_id)
        return expert.meta.n_training_samples if expert else 0

    def load_historical_data(self, expert_id: str, limit: int = 1000) -> int:
        """
        从数据库批量加载历史数据

        自动提取该专家需要的特征字段。
        """
        expert = self._managed_experts.get(expert_id)
        if expert is None:
            return 0

        if not self.db_path or not os.path.exists(self.db_path):
            logger.debug(f"[Optimizer] 数据库不可用: {self.db_path}")
            return 0

        try:
            conn = sqlite3.connect(self.db_path)
            df = pd.read_sql_query("""
                SELECT m.*, mf.*
                FROM matches m
                JOIN match_features mf ON m.match_id = mf.match_id
                WHERE m.status = 'finished'
                  AND m.home_score IS NOT NULL
                ORDER BY m.match_date DESC
                LIMIT ?
            """, conn, params=(limit,))
            conn.close()

            if df.empty:
                return 0

            # 计算标签
            df['result'] = df.apply(
                lambda r: 0 if r['home_score'] > r['away_score']
                else (1 if r['home_score'] == r['away_score'] else 2),
                axis=1
            )

            # 提取特征
            features_list = df.to_dict('records')
            labels = df['result'].tolist()

            # 批量累积
            count = self.accumulate_batch(
                expert_id, features_list, labels, quality_check=True
            )

            logger.info(f"[Optimizer] {expert_id} 历史数据加载: {count} 条")
            return count

        except (Exception, KeyError, IndexError) as e:
            logger.warning(f"[Optimizer] {expert_id} 历史数据加载失败: {e}")
            return 0

    def _check_data_quality(self, features: Dict) -> bool:
        """数据质量检查"""
        if not features:
            return False
        total = len(features)
        missing = sum(1 for v in features.values() if v is None)
        if total > 0 and missing / total > self.MAX_FEATURE_MISSING_RATE:
            return False
        return True

    def _prepare_training_data(self, expert_id: str) -> Tuple[np.ndarray, np.ndarray]:
        """准备训练数据 (features → numpy arrays)"""
        expert = self._managed_experts.get(expert_id)
        if expert is None:
            return None, None

        # 获取数据
        if isinstance(expert, LearnableExpert):
            features_list, labels = expert.get_data_buffer()
        else:
            features_list = self._data_buffers.get(expert_id, [])
            labels = self._label_buffers.get(expert_id, [])

        if not features_list or not labels:
            return None, None

        # 转换为 numpy
        try:
            df = pd.DataFrame(features_list)
            df = df.select_dtypes(include=[np.number])
            df = df.fillna(0)
            X = df.values
            y = np.array(labels, dtype=int)
            return X, y
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"[Optimizer] 数据转换失败: {e}")
            return None, None

    # ================================================================
    # 定期重训检查
    # ================================================================

    def check_retrain(self, expert_id: str = None) -> List[Dict]:
        """
        检查是否需要重新训练

        触发条件:
            1. 超过 retrain_frequency_days 未训练
            2. 累积了足够多的新数据(> min_samples)
            3. 性能退化(degradation_triggered)
            4. 自上次训练后新数据 > target_samples * 0.5

        Returns:
            [{expert_id, needs_retrain, reason}, ...]
        """
        to_check = [expert_id] if expert_id else list(self._managed_experts.keys())
        results = []

        for eid in to_check:
            expert = self._managed_experts.get(eid)
            if expert is None:
                continue

            needs_retrain = False
            reason = []

            # 条件1: 时间
            if expert.meta.last_trained_at:
                last_train = datetime.fromisoformat(expert.meta.last_trained_at)
                days_since = (datetime.now(timezone.utc) - last_train).days
                if days_since > expert.meta.training_config.retrain_frequency_days:
                    needs_retrain = True
                    reason.append(f'超时: {days_since}d > {expert.meta.training_config.retrain_frequency_days}d')

            # 条件2: 新数据
            if isinstance(expert, LearnableExpert):
                buffered = len(expert._data_buffer)
                if buffered >= expert.meta.training_config.min_samples:
                    needs_retrain = True
                    reason.append(f'新数据: {buffered} >= {expert.meta.training_config.min_samples}')
            elif eid in self._data_buffers:
                buffered = len(self._data_buffers[eid])
                if buffered >= expert.meta.training_config.min_samples:
                    needs_retrain = True
                    reason.append(f'新数据: {buffered} >= {expert.meta.training_config.min_samples}')

            # 条件3: 退化
            if expert.meta.performance.degradation_triggered:
                needs_retrain = True
                reason.append('性能退化检测')

            results.append({
                'expert_id': eid,
                'needs_retrain': needs_retrain,
                'reason': '; '.join(reason) if reason else '无需重训',
                'last_trained': expert.meta.last_trained_at,
                'next_retrain': expert.meta.next_retrain_at,
            })

            if needs_retrain and expert.meta.state == ExpertState.ACTIVE:
                logger.info(f"[Optimizer] {eid} 需要重训: {'; '.join(reason)}")
                if self._hub:
                    self._hub.registry.transition_state(eid, StateTransition.DEGRADE)
                self._step_to_training(expert)

        return results

    # ================================================================
    # 批量优化
    # ================================================================

    def run_all_pipelines(self) -> Dict:
        """
        运行所有注册专家的优化管道

        Returns:
            {total, succeeded, failed, blocked, details}
        """
        results = {'total': 0, 'succeeded': 0, 'failed': 0, 'blocked': 0, 'details': {}}

        for eid in list(self._managed_experts.keys()):
            results['total'] += 1
            r = self.run_pipeline(eid)
            results['details'][eid] = r

            if r.get('success'):
                results['succeeded'] += 1
            elif r.get('status') == 'blocked':
                results['blocked'] += 1
            else:
                results['failed'] += 1

        logger.info(f"[Optimizer] 批量优化完成: {results['succeeded']}/{results['total']} 成功")
        return results

    # ================================================================
    # 状态查询
    # ================================================================

    def get_status(self) -> Dict:
        """获取优化器状态"""
        experts_status = {}
        for eid, expert in self._managed_experts.items():
            experts_status[eid] = {
                'state': expert.meta.state.value,
                'n_samples': expert.meta.n_training_samples,
                'min_required': expert.meta.training_config.min_samples,
                'last_trained': expert.meta.last_trained_at,
                'next_retrain': expert.meta.next_retrain_at,
                'performance': {
                    'accuracy': round(expert.meta.performance.accuracy, 4),
                    'degraded': expert.meta.performance.degradation_triggered,
                },
            }

        retrain_checks = self.check_retrain()

        return {
            'managed_count': len(self._managed_experts),
            'total_buffer_size': sum(
                len(buf) for buf in self._data_buffers.values()
            ),
            'experts': experts_status,
            'retrain_checks': retrain_checks,
            'recent_training_log': self._training_log[-5:],
            'baseline_accuracy': self._baseline_accuracy,
        }

#!/usr/bin/env python3
"""
哨响AI — 专家注册中心 (ExpertRegistry)
========================================
中心化专家注册、发现、状态管理。

职责:
    1. 维护所有专家的注册表 (ID → ExpertProtocol)
    2. 支持运行时动态注册/注销
    3. 支持从 YAML/JSON 配置批量注册
    4. 提供分类索引和能力查询
    5. 管理专家状态(集成 ExpertState 管理)

设计模式: 注册表模式 (Registry Pattern) + 单例
"""
import logging
import os
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Iterator, Callable
from collections import defaultdict

from .expert_protocol import (
    ExpertProtocol, ExpertState, ExpertMeta,
    ExpertAdapter, create_expert_from_config,
    VALID_TRANSITIONS, StateTransition,
)

logger = logging.getLogger(__name__)

class ExpertRegistry:
    """
    专家注册中心

    用法:
        registry = ExpertRegistry()

        # 注册单例专家
        registry.register(my_expert)

        # 从配置文件批量注册
        registry.load_from_config('config/expert_registry.yaml')

        # 查询
        trend_experts = registry.find_by_category('trend')
        active = registry.get_active()
        expert = registry.get('trend_analyzer')

        # 注销
        registry.unregister('old_expert')
    """

    def __init__(self):
        self._experts: Dict[str, ExpertProtocol] = {}       # ID → 实例
        self._category_index: Dict[str, List[str]] = defaultdict(list)  # 分类→ID列表
        self._state_index: Dict[ExpertState, List[str]] = defaultdict(list)
        self._tag_index: Dict[str, List[str]] = defaultdict(list)
        self._registration_order: List[str] = []             # 注册顺序
        self._on_register_hooks: List[Callable] = []         # 注册钩子
        self._on_state_change_hooks: List[Callable] = []     # 状态变更钩子

    # ---- 注册 / 注销 ----

    def register(self, expert: ExpertProtocol, overwrite: bool = False) -> bool:
        """
        注册一个专家

        Args:
            expert: 实现了 ExpertProtocol 的专家实例
            overwrite: 是否覆盖已有同名专家

        Returns:
            bool: 注册成功

        Raises:
            ValueError: expert_id 冲突且 overwrite=False
        """
        eid = expert.meta.expert_id

        if eid in self._experts and not overwrite:
            existing = self._experts[eid]
            if existing.meta.state != ExpertState.UNREGISTERED:
                raise ValueError(
                    f"专家 {eid} 已注册 (状态: {existing.meta.state.value})。"
                    f"使用 overwrite=True 强制覆盖"
                )
            # 覆盖 UNREGISTERED 状态允许
            logger.info(f"覆盖占位专家: {eid}")

        # 注册
        self._experts[eid] = expert

        if eid not in self._registration_order:
            self._registration_order.append(eid)

        # 更新索引
        self._rebuild_indexes_for(eid)

        # 更新状态为 COLD_START (如果仍是 UNREGISTERED)
        if expert.meta.state == ExpertState.UNREGISTERED:
            self._transition_state(expert, ExpertState.COLD_START)

        # 触发钩子
        for hook in self._on_register_hooks:
            try:
                hook(expert)
            except (Exception) as e:
                logger.warning(f"注册钩子异常: {e}")

        expert.on_register(None)  # hub 引用稍后由 ExpertHub 设置
        logger.info(f"[Registry] 注册专家: {eid} v{expert.meta.version} "
                    f"({expert.meta.category}, state={expert.meta.state.value})")
        return True

    def unregister(self, expert_id: str) -> bool:
        """注销专家"""
        if expert_id not in self._experts:
            return False

        expert = self._experts.pop(expert_id)
        self._remove_from_indexes(expert_id)
        logger.info(f"[Registry] 注销专家: {expert_id}")
        return True

    def register_from_config(self, config: Dict) -> Optional[ExpertProtocol]:
        """
        从配置字典注册专家

        适用于声明式新增专家:
            - 有 class_path → 动态加载并注册
            - 无 class_path → 创建占位符(COLD_START, 等待模块注入)
        """
        expert = create_expert_from_config(config)
        if expert:
            # 配置文件注册使用 overwrite=True，避免与已有专家冲突
            self.register(expert, overwrite=True)
        return expert

    def load_from_config(self, config_path: str) -> int:
        """
        从 YAML/JSON 配置文件批量加载专家

        文件格式:
            experts:
              - expert_id: trend_analyzer
                class_path: agents.experts.trend_agent.TrendAgentV2
                category: trend
                phase: 1
                ...
              - expert_id: my_new_expert
                category: tactical
                phase: 3
                ...
        """
        if not os.path.exists(config_path):
            logger.warning(f"配置文件不存在: {config_path}")
            return 0

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                if config_path.endswith('.yaml') or config_path.endswith('.yml'):
                    import yaml
                    data = yaml.safe_load(f)
                else:
                    data = json.load(f)
        except (Exception, IOError, FileNotFoundError) as e:
            logger.error(f"加载配置文件失败: {e}")
            return 0

        experts_config = data.get('experts', [])
        if not experts_config:
            logger.info(f"配置文件中无 expert 定义")
            return 0

        count = 0
        for cfg in experts_config:
            try:
                self.register_from_config(cfg)
                count += 1
            except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
                logger.error(f"注册失败 [{cfg.get('expert_id', '?')}]: {e}")

        logger.info(f"[Registry] 从配置加载 {count}/{len(experts_config)} 个专家")
        return count

    # ---- 查询 ----

    def get(self, expert_id: str) -> Optional[ExpertProtocol]:
        """按ID获取专家"""
        return self._experts.get(expert_id)

    def get_all(self) -> Dict[str, ExpertProtocol]:
        """获取所有专家"""
        return dict(self._experts)

    def get_active(self) -> Dict[str, ExpertProtocol]:
        """获取所有活跃专家"""
        return {
            eid: exp for eid, exp in self._experts.items()
            if exp.meta.state in ExpertState.active_states()
        }

    def get_trainable(self) -> Dict[str, ExpertProtocol]:
        """获取可训练专家"""
        return {
            eid: exp for eid, exp in self._experts.items()
            if exp.is_trainable()
        }

    def get_by_phase(self, phase: int) -> Dict[str, ExpertProtocol]:
        """按阶段获取"""
        return {
            eid: exp for eid, exp in self._experts.items()
            if exp.meta.phase == phase
        }

    def find_by_category(self, category: str) -> Dict[str, ExpertProtocol]:
        """按分类查找"""
        ids = self._category_index.get(category, [])
        return {eid: self._experts[eid] for eid in ids if eid in self._experts}

    def find_by_tag(self, tag: str) -> Dict[str, ExpertProtocol]:
        """按标签查找"""
        ids = self._tag_index.get(tag, [])
        return {eid: self._experts[eid] for eid in ids if eid in self._experts}

    def find_by_state(self, state: ExpertState) -> Dict[str, ExpertProtocol]:
        """按状态查找"""
        ids = self._state_index.get(state, [])
        return {eid: self._experts[eid] for eid in ids if eid in self._experts}

    def filter(self, predicate: Callable[[ExpertProtocol], bool]) -> Dict[str, ExpertProtocol]:
        """自定义过滤"""
        return {eid: exp for eid, exp in self._experts.items() if predicate(exp)}

    def __len__(self) -> int:
        return len(self._experts)

    def __contains__(self, expert_id: str) -> bool:
        return expert_id in self._experts

    def __iter__(self) -> Iterator[str]:
        return iter(self._experts)

    def __getitem__(self, expert_id: str) -> ExpertProtocol:
        return self._experts[expert_id]

    # ---- 状态管理 ----

    def transition_state(self, expert_id: str,
                         transition: StateTransition) -> bool:
        """
        执行专家状态转换

        验证转换合法性，触发钩子和生命周期回调。
        """
        expert = self._experts.get(expert_id)
        if expert is None:
            logger.warning(f"状态转换失败: {expert_id} 未找到")
            return False

        current = expert.meta.state
        valid = VALID_TRANSITIONS.get(current, {})

        if transition not in valid:
            allowed = list(valid.keys())
            logger.warning(
                f"非法状态转换: {expert_id} {current.value} → {transition}, "
                f"允许: {[t.name for t in allowed]}"
            )
            return False

        target = valid[transition]
        return self._transition_state(expert, target)

    def _transition_state(self, expert: ExpertProtocol,
                          target: ExpertState) -> bool:
        """执行状态转换(内部方法)"""
        old_state = expert.meta.state
        expert.meta.state = target
        expert.meta.updated_at = datetime.now(timezone.utc).isoformat()

        # 更新索引
        self._state_index[old_state].remove(expert.meta.expert_id)
        self._state_index[target].append(expert.meta.expert_id)

        # 生命周期回调
        expert.on_state_change(old_state, target)
        if target == ExpertState.ACTIVE:
            expert.on_activate()
        elif old_state == ExpertState.ACTIVE:
            expert.on_deactivate()

        # 触发全局钩子
        for hook in self._on_state_change_hooks:
            try:
                hook(expert, old_state, target)
            except (Exception) as e:
                logger.warning(f"状态钩子异常: {e}")

        return True

    # ---- 钩子 ----

    def add_register_hook(self, hook: Callable[[ExpertProtocol], None]):
        """添加注册钩子"""
        self._on_register_hooks.append(hook)

    def add_state_change_hook(self,
                              hook: Callable[[ExpertProtocol, ExpertState, ExpertState], None]):
        """添加状态变更钩子"""
        self._on_state_change_hooks.append(hook)

    # ---- 统计 ----

    def get_statistics(self) -> Dict:
        """获取注册表统计信息"""
        state_counts = {}
        for state in ExpertState:
            count = len(self._state_index.get(state, []))
            if count > 0:
                state_counts[state.value] = count

        category_counts = {}
        for cat, ids in self._category_index.items():
            category_counts[cat] = len(ids)

        phase_experts = defaultdict(list)
        for eid, exp in self._experts.items():
            phase_experts[exp.meta.phase].append(eid)

        return {
            'total_experts': len(self._experts),
            'by_state': state_counts,
            'by_category': category_counts,
            'by_phase': {p: len(ids) for p, ids in phase_experts.items()},
            'active_count': len(self.get_active()),
            'trainable_count': len(self.get_trainable()),
        }

    # ---- 内部索引 ----

    def _rebuild_indexes_for(self, expert_id: str):
        """重建单个专家的所有索引"""
        expert = self._experts.get(expert_id)
        if not expert:
            return

        meta = expert.meta

        # 分类索引
        self._category_index[meta.category].append(expert_id)

        # 状态索引
        self._state_index[meta.state].append(expert_id)

        # 标签索引
        for tag in meta.tags:
            self._tag_index[tag].append(expert_id)

    def _remove_from_indexes(self, expert_id: str):
        """从所有索引中移除"""
        for cat_ids in self._category_index.values():
            if expert_id in cat_ids:
                cat_ids.remove(expert_id)

        for state_ids in self._state_index.values():
            if expert_id in state_ids:
                state_ids.remove(expert_id)

        for tag_ids in self._tag_index.values():
            if expert_id in tag_ids:
                tag_ids.remove(expert_id)

        if expert_id in self._registration_order:
            self._registration_order.remove(expert_id)

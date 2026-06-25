#!/usr/bin/env python3
"""
哨响AI — 可扩展多专家系统协议层
================================
核心抽象定义，所有专家模块必须遵循的统一接口。

设计原则:
    1. 声明式接口 — 专家声明"我需要什么输入、我能产出什么输出"
    2. 状态驱动 — 全生命周期状态机，从 COLD_START 到 ACTIVE
    3. 渐进式优化 — 参数初始化 → 数据累积 → 迭代训练 → 验证 → 激活
    4. 向后兼容 — 通过 ExpertAdapter 包装旧版 ExpertAgent

作者: footballAI Architecture v3.0
日期: 2026-05-31
"""

from __future__ import annotations
import abc
import logging
import time
from datetime import datetime
from enum import Enum, auto
from typing import Dict, List, Optional, Any, Set, Callable, Tuple
from dataclasses import dataclass, field

from utils.constants import DEFAULT_HOME_PROB, DEFAULT_DRAW_PROB, DEFAULT_AWAY_PROB

logger = logging.getLogger(__name__)


# ================================================================
# 专家生命周期状态机
# ================================================================
class ExpertState(Enum):
    """
    专家状态枚举 — 定义专家从冷启动到上线的完整生命周期

    状态流转:
        UNREGISTERED → COLD_START → DATA_ACCUMULATING → TRAINING → OPTIMIZED → ACTIVE
                                              ↓               ↑          ↓
                                          (跳过训练) ──────────┘     DEGRADED
                                                                         ↓
                                                                     ARCHIVED

    各状态说明:
        UNREGISTERED      - 未注册，仅存在于配置描述中
        COLD_START        - 已注册但无参数/无数据/无模型
        DATA_ACCUMULATING - 正在收集训练数据
        TRAINING          - 正在训练/调参
        OPTIMIZED         - 训练完成，待激活验证
        ACTIVE            - 已激活，参与预测
        DEGRADED          - 性能下降，需重新训练
        ARCHIVED          - 已归档，不再使用
        ERROR             - 异常状态
    """
    UNREGISTERED = "unregistered"
    COLD_START = "cold_start"
    DATA_ACCUMULATING = "data_accumulating"
    TRAINING = "training"
    OPTIMIZED = "optimized"
    ACTIVE = "active"
    DEGRADED = "degraded"
    ARCHIVED = "archived"
    ERROR = "error"

    @classmethod
    def active_states(cls) -> Set["ExpertState"]:
        """可参与预测的状态"""
        return {cls.ACTIVE, cls.OPTIMIZED}


class StateTransition(Enum):
    """允许的状态转换"""
    REGISTER = auto()          # UNREGISTERED → COLD_START
    START_ACCUMULATING = auto()  # COLD_START → DATA_ACCUMULATING
    START_TRAINING = auto()    # DATA_ACCUMULATING → TRAINING
    COMPLETE_TRAINING = auto() # TRAINING → OPTIMIZED
    ACTIVATE = auto()          # OPTIMIZED → ACTIVE
    DEGRADE = auto()           # ACTIVE/OPTIMIZED → DEGRADED
    RETRAIN = auto()           # DEGRADED → TRAINING
    ARCHIVE = auto()           # ANY → ARCHIVED
    RESET = auto()             # ANY → COLD_START
    ERROR = auto()             # ANY → ERROR


# 合法状态转换表
VALID_TRANSITIONS: Dict[ExpertState, Dict[StateTransition, ExpertState]] = {
    ExpertState.UNREGISTERED:     {StateTransition.REGISTER: ExpertState.COLD_START},
    ExpertState.COLD_START:       {
        StateTransition.START_ACCUMULATING: ExpertState.DATA_ACCUMULATING,
        StateTransition.ARCHIVE: ExpertState.ARCHIVED,
    },
    ExpertState.DATA_ACCUMULATING: {
        StateTransition.START_TRAINING: ExpertState.TRAINING,
        StateTransition.ACTIVATE: ExpertState.ACTIVE,  # 跳过训练(规则型专家)
        StateTransition.ARCHIVE: ExpertState.ARCHIVED,
    },
    ExpertState.TRAINING:         {
        StateTransition.COMPLETE_TRAINING: ExpertState.OPTIMIZED,
        StateTransition.ERROR: ExpertState.ERROR,
    },
    ExpertState.OPTIMIZED:        {
        StateTransition.ACTIVATE: ExpertState.ACTIVE,
        StateTransition.DEGRADE: ExpertState.DEGRADED,
        StateTransition.ARCHIVE: ExpertState.ARCHIVED,
    },
    ExpertState.ACTIVE:           {
        StateTransition.DEGRADE: ExpertState.DEGRADED,
        StateTransition.ARCHIVE: ExpertState.ARCHIVED,
    },
    ExpertState.DEGRADED:         {
        StateTransition.RETRAIN: ExpertState.TRAINING,
        StateTransition.RESET: ExpertState.COLD_START,
        StateTransition.ARCHIVE: ExpertState.ARCHIVED,
    },
    ExpertState.ERROR:            {
        StateTransition.RESET: ExpertState.COLD_START,
        StateTransition.ARCHIVE: ExpertState.ARCHIVED,
    },
    ExpertState.ARCHIVED:         {},  # 终态
}


# ================================================================
# 数据类
# ================================================================
@dataclass
class InputSchema:
    """
    专家输入模式声明

    每个专家声明自己需要的输入字段，系统据此判断:
    - 当前比赛数据是否足够调用该专家
    - 数据累积时需要收集哪些字段
    """
    required: List[str] = field(default_factory=list)    # 必须字段
    optional: List[str] = field(default_factory=list)     # 可选字段
    data_types: Dict[str, str] = field(default_factory=dict)  # 字段→类型映射

    def check_availability(self, match_data: Dict) -> float:
        """检查数据可用性，返回 0-1 分数"""
        if not self.required:
            return 1.0
        available = sum(1 for f in self.required
                       if self._resolve_field(match_data, f) is not None)
        return available / len(self.required)

    @staticmethod
    def _resolve_field(data: Dict, field: str) -> Any:
        """支持点号路径的字段解析，如 'odds.home'"""
        parts = field.split('.')
        current = data
        for p in parts:
            if isinstance(current, dict):
                current = current.get(p)
            else:
                return None
        return current


@dataclass
class OutputSchema:
    """专家输出模式声明"""
    prediction_format: str = "3way"        # "3way" | "binary" | "score" | "custom"
    required_keys: List[str] = field(default_factory=lambda: ["home", "draw", "away"])
    confidence_range: Tuple[float, float] = (0.0, 1.0)
    supports_proba: bool = False           # 是否支持 predict_proba


@dataclass
class TrainingConfig:
    """训练配置"""
    min_samples: int = 100                 # 最小训练样本数
    target_samples: int = 500              # 目标训练样本数
    batch_size: int = 32
    learning_rate: float = 0.001
    epochs: int = 50
    early_stopping_patience: int = 10
    validation_split: float = 0.2
    cv_folds: int = 5
    retrain_frequency_days: int = 30       # 多久重新训练一次


@dataclass
class ExpertPerformance:
    """专家性能追踪"""
    accuracy: float = 0.0
    precision: float = 0.0
    recall: float = 0.0
    f1_score: float = 0.0
    n_predictions: int = 0
    n_correct: int = 0
    avg_confidence: float = 0.0
    avg_execution_ms: float = 0.0
    last_n_accuracy: List[float] = field(default_factory=list)  # 最近N次滑动窗口
    degradation_triggered: bool = False


@dataclass
class ExpertMeta:
    """专家元数据"""
    expert_id: str                                      # 唯一标识符
    display_name: str                                   # 显示名称
    version: str = "1.0.0"
    category: str = "general"                           # trend | risk | value | tactical | temporal | auxiliary
    description: str = ""
    author: str = ""
    tags: List[str] = field(default_factory=list)
    priority: int = 0                                   # 调度优先级(越大越优先)
    phase: int = 0                                      # 分阶段归属
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # 渐进式优化状态
    state: ExpertState = ExpertState.UNREGISTERED
    state_history: List[Dict] = field(default_factory=list)

    # 数据
    n_training_samples: int = 0
    n_validation_samples: int = 0
    last_trained_at: Optional[str] = None
    next_retrain_at: Optional[str] = None

    # 配置
    training_config: TrainingConfig = field(default_factory=TrainingConfig)
    input_schema: InputSchema = field(default_factory=InputSchema)
    output_schema: OutputSchema = field(default_factory=OutputSchema)

    # 性能
    performance: ExpertPerformance = field(default_factory=ExpertPerformance)

    # 可扩展元数据
    extra: Dict = field(default_factory=dict)


# ================================================================
# 抽象专家协议 (ABC)
# ================================================================
class ExpertProtocol(abc.ABC):
    """
    可扩展多专家统一协议

    所有专家(现有的和未来的)必须实现此接口。
    系统通过此协议与专家交互，不关心内部实现细节。

    必须实现:
        - predict(): 执行预测
        - get_input_schema(): 声明输入需求
        - get_output_schema(): 声明输出格式

    可选实现:
        - fit(): 批量训练
        - partial_fit(): 增量训练
        - evaluate(): 自我评估
        - preprocess(): 数据预处理

    生命周期钩子:
        - on_register(): 注册时回调
        - on_state_change(): 状态变更回调
        - on_activate(): 激活回调
        - on_deactivate(): 停用回调
    """

    def __init__(self, meta: ExpertMeta):
        self.meta = meta
        self._model = None
        self._params: Dict = {}

    # ---- 必须实现的接口 ----

    @abc.abstractmethod
    def predict(self, match_data: Dict, context: Dict = None) -> Dict:
        """
        执行预测

        Args:
            match_data: 比赛数据字典
            context: 上下文(适用性分数、特征等)

        Returns:
            {
                'expert_id': str,
                'prediction': {'home': float, 'draw': float, 'away': float},
                'confidence': float,
                'reasoning': str,
                'status': 'success' | 'fallback' | 'error',
                'execution_time_ms': float,
            }
        """
        ...

    @abc.abstractmethod
    def get_input_schema(self) -> InputSchema:
        """声明该专家需要的输入字段"""
        ...

    @abc.abstractmethod
    def get_output_schema(self) -> OutputSchema:
        """声明该专家的输出格式"""
        ...

    # ---- 可选训练接口 ----

    def fit(self, X, y, **kwargs) -> Dict:
        """
        批量训练

        Returns:
            {'status': 'success' | 'not_supported', 'metrics': {...}}
        """
        return {'status': 'not_supported', 'reason': f'{self.meta.expert_id} 不支持训练'}

    def partial_fit(self, X, y, **kwargs) -> Dict:
        """
        增量训练(在线学习)

        Returns:
            {'status': 'success' | 'not_supported', 'metrics': {...}}
        """
        return {'status': 'not_supported', 'reason': f'{self.meta.expert_id} 不支持增量训练'}

    def evaluate(self, X, y) -> Dict:
        """
        自我评估

        Returns:
            {'accuracy': float, 'f1': float, ...}
        """
        return {'status': 'not_supported', 'reason': '未实现 evaluate()'}

    def preprocess(self, raw_data: Dict) -> Dict:
        """数据预处理钩子(默认透传)"""
        return raw_data

    # ---- 模型持久化 ----

    def save(self, path: str) -> bool:
        """保存模型参数"""
        try:
            import pickle, os
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                pickle.dump({'meta': self.meta, 'params': self._params}, f)
            return True
        except (Exception, IOError, FileNotFoundError) as e:
            logger.error(f"[{self.meta.expert_id}] 保存失败: {e}")
            return False

    def load(self, path: str) -> bool:
        """加载模型参数"""
        try:
            import pickle
            with open(path, 'rb') as f:
                data = pickle.load(f)
            self._params = data.get('params', {})
            return True
        except FileNotFoundError:
            logger.debug(f"[{self.meta.expert_id}] 模型文件不存在: {path}")
            return False
        except (Exception, KeyError, IndexError, IOError, FileNotFoundError, requests.exceptions.RequestException) as e:
            logger.error(f"[{self.meta.expert_id}] 加载失败: {e}")
            return False

    # ---- 生命周期钩子 ----

    def on_register(self, hub) -> None:
        """注册到 ExpertHub 时调用"""
        logger.info(f"[{self.meta.expert_id}] 已注册到 Hub")

    def on_state_change(self, from_state: ExpertState, to_state: ExpertState) -> None:
        """状态变更回调"""
        self.meta.state_history.append({
            'from': from_state.value,
            'to': to_state.value,
            'timestamp': datetime.now().isoformat(),
        })
        logger.info(f"[{self.meta.expert_id}] {from_state.value} → {to_state.value}")

    def on_activate(self) -> None:
        """激活为 ACTIVE 时调用"""
        logger.info(f"[{self.meta.expert_id}] 已激活")

    def on_deactivate(self) -> None:
        """停用时调用"""
        logger.info(f"[{self.meta.expert_id}] 已停用")

    # ---- 便利方法 ----

    def get_state(self) -> ExpertState:
        return self.meta.state

    def is_active(self) -> bool:
        return self.meta.state == ExpertState.ACTIVE

    def is_trainable(self) -> bool:
        """是否需要训练"""
        return self.meta.state in {
            ExpertState.COLD_START,
            ExpertState.DATA_ACCUMULATING,
            ExpertState.TRAINING,
            ExpertState.DEGRADED,
        }

    def get_status_report(self) -> Dict:
        """获取状态报告"""
        perf = self.meta.performance
        return {
            'expert_id': self.meta.expert_id,
            'display_name': self.meta.display_name,
            'version': self.meta.version,
            'category': self.meta.category,
            'state': self.meta.state.value,
            'phase': self.meta.phase,
            'accuracy': round(perf.accuracy, 4),
            'f1_score': round(perf.f1_score, 4),
            'n_predictions': perf.n_predictions,
            'n_training_samples': self.meta.n_training_samples,
            'last_trained_at': self.meta.last_trained_at,
            'avg_confidence': round(perf.avg_confidence, 4),
            'avg_execution_ms': round(perf.avg_execution_ms, 2),
            'degraded': perf.degradation_triggered,
        }

    def update_performance(self, actual: str, predicted: Dict) -> None:
        """更新性能指标"""
        perf = self.meta.performance
        pred = predicted.get('prediction', {})
        pred_label = max(pred, key=pred.get) if pred else 'draw'
        is_correct = (pred_label == actual)

        perf.n_predictions += 1
        if is_correct:
            perf.n_correct += 1

        n = perf.n_predictions
        if n > 0:
            perf.accuracy = perf.n_correct / n

        conf = predicted.get('confidence', 0.5)
        perf.avg_confidence = ((perf.avg_confidence * (n - 1)) + conf) / n

        exec_ms = predicted.get('execution_time_ms', 0)
        perf.avg_execution_ms = ((perf.avg_execution_ms * (n - 1)) + exec_ms) / n

        # 滑动窗口准确率
        perf.last_n_accuracy.append(1.0 if is_correct else 0.0)
        if len(perf.last_n_accuracy) > 50:
            perf.last_n_accuracy.pop(0)

        # 退化检测: 最近20次准确率低于历史均值15pp
        if len(perf.last_n_accuracy) >= 20:
            recent_acc = sum(perf.last_n_accuracy[-20:]) / 20
            if recent_acc < perf.accuracy - 0.15 and perf.n_predictions > 50:
                perf.degradation_triggered = True

    def _build_fallback(self, reason: str = "模块未就绪") -> Dict:
        """构建降级预测"""
        return {
            'expert_id': self.meta.expert_id,
            'prediction': {'home': DEFAULT_HOME_PROB, 'draw': DEFAULT_DRAW_PROB, 'away': DEFAULT_AWAY_PROB},
            'confidence': 0.1,
            'reasoning': f"[{self.meta.display_name}] 降级预测: {reason}",
            'execution_time_ms': 0.0,
            'status': 'fallback',
        }


# ================================================================
# 规则型专家基类 (无需训练即可激活)
# ================================================================
class RuleBasedExpert(ExpertProtocol):
    """
    规则型专家 — 基于确定性规则，无需训练

    适用场景: 裁判画像、德比检测、时空断裂等纯规则逻辑
    特点: 注册后即可激活(跳过 TRAINING 阶段)
    """

    def fit(self, X, y, **kwargs) -> Dict:
        return {'status': 'not_needed', 'reason': '规则型专家无需训练'}

    def is_trainable(self) -> bool:
        return False


# ================================================================
# 学习型专家基类 (需要训练)
# ================================================================
class LearnableExpert(ExpertProtocol):
    """
    学习型专家 — 需要数据累积和模型训练

    适用场景: LSTM时序预测、梯度提升、逻辑回归等ML模型
    特点: 必须经过 DATA_ACCUMULATING → TRAINING → OPTIMIZED 流程
    """

    def __init__(self, meta: ExpertMeta):
        super().__init__(meta)
        self._data_buffer: List[Dict] = []       # 训练数据缓冲
        self._label_buffer: List = []             # 标签缓冲
        self._model_weights: Dict = {}            # 模型权重

    def accumulate_data(self, features: Dict, label) -> int:
        """累积训练数据"""
        self._data_buffer.append(features)
        self._label_buffer.append(label)
        self.meta.n_training_samples = len(self._data_buffer)
        return self.meta.n_training_samples

    def has_sufficient_data(self) -> bool:
        """数据是否足够开始训练"""
        return len(self._data_buffer) >= self.meta.training_config.min_samples

    def clear_buffer(self):
        """清空数据缓冲"""
        self._data_buffer.clear()
        self._label_buffer.clear()

    def get_data_buffer(self) -> Tuple[List, List]:
        return self._data_buffer, self._label_buffer

    def get_model_weights(self) -> Dict:
        return self._model_weights


# ================================================================
# 旧版 ExpertAgent 适配器 (向后兼容)
# ================================================================
class ExpertAdapter(ExpertProtocol):
    """
    将旧版 ExpertAgent 包装为新协议

    使现有10个专家无需修改即可接入新架构。
    """

    def __init__(self, legacy_agent, expert_id: str, meta: ExpertMeta = None):
        """
        Args:
            legacy_agent: agents.base_agent.ExpertAgent 实例
            expert_id: 在新系统中的唯一标识
            meta: 预配置的元数据(可选)
        """
        if meta is None:
            meta = ExpertMeta(
                expert_id=expert_id,
                display_name=legacy_agent.name if hasattr(legacy_agent, 'name') else expert_id,
                category='legacy',
                description=f'Legacy adapter for {expert_id}',
                state=ExpertState.ACTIVE,  # 旧版专家视为已激活
            )
        super().__init__(meta)
        self._legacy = legacy_agent
        self._expert_id = expert_id

    def predict(self, match_data: Dict, context: Dict = None) -> Dict:
        start = time.perf_counter()
        try:
            result = self._legacy.predict(match_data, context or {})
            elapsed_ms = (time.perf_counter() - start) * 1000

            # 统一返回格式
            return {
                'expert_id': self._expert_id,
                'prediction': result.get('prediction', {'home': DEFAULT_HOME_PROB, 'draw': DEFAULT_DRAW_PROB, 'away': DEFAULT_AWAY_PROB}),
                'confidence': result.get('confidence', 0.5),
                'reasoning': result.get('reasoning', ''),
                'execution_time_ms': round(elapsed_ms, 2),
                'status': result.get('status', 'success'),
            }
        except (Exception, requests.exceptions.RequestException) as e:
            elapsed_ms = (time.perf_counter() - start) * 1000
            return {
                'expert_id': self._expert_id,
                'prediction': {'home': DEFAULT_HOME_PROB, 'draw': DEFAULT_DRAW_PROB, 'away': DEFAULT_AWAY_PROB},
                'confidence': 0.05,
                'reasoning': f'Legacy agent error: {str(e)}',
                'execution_time_ms': round(elapsed_ms, 2),
                'status': 'error',
            }

    def get_input_schema(self) -> InputSchema:
        return InputSchema(required=['home_team', 'away_team'])

    def get_output_schema(self) -> OutputSchema:
        return OutputSchema()


# ================================================================
# 专家工厂: 从配置创建专家实例
# ================================================================
def create_expert_from_config(config: Dict) -> Optional[ExpertProtocol]:
    """
    从配置字典创建专家实例

    配置格式:
        {
            "expert_id": "my_new_expert",
            "display_name": "我的新专家",
            "category": "trend",
            "class_path": "modules.my_expert.MyExpert",
            "phase": 2,
            "input_schema": {"required": ["home_team"], "optional": ["odds"]},
            "training_config": {"min_samples": 200, "epochs": 30},
        }
    """
    expert_id = config.get('expert_id', '')
    if not expert_id:
        logger.error("配置缺少 expert_id")
        return None

    meta = ExpertMeta(
        expert_id=expert_id,
        display_name=config.get('display_name', expert_id),
        version=config.get('version', '1.0.0'),
        category=config.get('category', 'general'),
        description=config.get('description', ''),
        author=config.get('author', ''),
        tags=config.get('tags', []),
        priority=config.get('priority', 0),
        phase=config.get('phase', 0),
        state=ExpertState.COLD_START,
        input_schema=InputSchema(**config.get('input_schema', {})),
        output_schema=OutputSchema(**config.get('output_schema', {})),
        training_config=TrainingConfig(**config.get('training_config', {})),
    )

    # 尝试动态加载类
    class_path = config.get('class_path')
    if class_path:
        try:
            module_path, class_name = class_path.rsplit('.', 1)
            import importlib
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)

            # 尝试两种构造方式: 新版 ExpertProtocol(meta=meta) / 旧版 ExpertAgent()
            try:
                instance = cls(meta=meta)
                logger.info(f"从配置创建专家: {expert_id} ({class_path}) [v3 Protocol]")
            except TypeError:
                # 旧版 ExpertAgent 不接受 meta 参数 → 使用 ExpertAdapter 包装
                legacy_instance = cls()
                instance = ExpertAdapter(legacy_instance, expert_id, meta)
                logger.info(f"从配置创建专家: {expert_id} ({class_path}) [Legacy Adapter]")

            return instance
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"无法创建专家 {expert_id}: {e}")
            return None

    # 无类路径 → 返回占位符(COLD_START状态，等待后续注入)
    logger.info(f"创建占位专家: {expert_id} (COLD_START, 等待模块注入)")
    from abc import ABC

    class PlaceholderExpert(ExpertProtocol):
        def predict(self, match_data, context=None):
            return self._build_fallback("专家模块尚未实现")
        def get_input_schema(self):
            return meta.input_schema
        def get_output_schema(self):
            return meta.output_schema

    return PlaceholderExpert(meta=meta)

"""
哨响AI v4.0 — 容错降级守护 (Degradation Guard)
==================================================
保障线上服务稳定性: 单点故障不导致整体不可用。

三层降级链路:
  L1 v4.1 Stacking → L2 v3.2 基线 → L3 赔率反推 → L4 均匀兜底

核心能力:
  1. 模块健康检查: 每个组件独立监控, 故障自动隔离
  2. 自动降级链: 数据缺失/模型故障时逐级回退, 不中断服务
  3. 低置信度标记: D-Gate + 防线评分 → 自动附加风险标签
  4. 输出安全校验: 无论降级到哪一级, 输出必须符合 PredictionGuard 规范

设计原则:
  - 宁降级不错判: 不确定时宁可降级用简单模型, 不假装自信
  - 故障隔离: 单个专家模块崩溃不影响其他模块
  - 全链路可追溯: 每次降级都记录原因和时间戳
  - 恢复自动: 模块恢复后自动切回高级链路

作者: Architecture v4.0 · Resilience Phase
日期: 2026-06-19
"""
from __future__ import annotations
import logging, time, traceback
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger('DegradationGuard')

# ═══════════════════════════════════════════════════════════════
# 1. 数据结构
# ═══════════════════════════════════════════════════════════════

class DegradationLevel(Enum):
    """降级级别"""
    L1 = 1   # v4.1 Stacking (全功能)
    L2 = 2   # v3.2 基线模型
    L3 = 3   # 赔率反推 (无模型)
    L4 = 4   # 均匀兜底 (0.33/0.33/0.33)

class RiskTag(Enum):
    """风险标签"""
    OK = "ok"
    LOW_CONFIDENCE = "low_confidence"
    DEGRADED = "degraded"
    MISSING_DATA = "missing_data"
    COLD_START = "cold_start"
    EXPERT_FAILURE = "expert_failure"
    FALLBACK_BASELINE = "fallback_baseline"
    UNIFORM_FALLBACK = "uniform_fallback"
    BARRIER_TRIGGERED = "barrier_triggered"

@dataclass
class ModuleHealth:
    """模块健康状态"""
    name: str
    healthy: bool = True
    last_error: str = ""
    fail_count: int = 0
    last_check_time: str = ""
    recovery_count: int = 0

    def record_failure(self, error: str):
        self.healthy = False
        self.fail_count += 1
        self.last_error = error[:200]
        self.last_check_time = time.strftime('%H:%M:%S')

    def record_recovery(self):
        self.healthy = True
        self.recovery_count += 1
        self.last_check_time = time.strftime('%H:%M:%S')

@dataclass
class DegradationTrace:
    """降级追踪记录"""
    level: DegradationLevel
    reason: str
    timestamp: str
    component: str = ""
    recovery_possible: bool = True

@dataclass
class GuardedResult:
    """容错守护输出"""
    # 概率 (无论降级到哪层, 始终有值)
    h_prob: float = 0.33
    d_prob: float = 0.33
    a_prob: float = 0.33

    # 降级信息
    degradation_level: DegradationLevel = DegradationLevel.L4
    degradation_trace: List[DegradationTrace] = field(default_factory=list)

    # 风险标签
    risk_tags: List[RiskTag] = field(default_factory=list)
    risk_summary: str = ""

    # 模块健康
    health_report: Dict[str, ModuleHealth] = field(default_factory=dict)

    # 是否正常 (L1无降级 = True)
    is_healthy: bool = True

    def add_tag(self, tag: RiskTag):
        if tag not in self.risk_tags:
            self.risk_tags.append(tag)

    def has_tag(self, tag: RiskTag) -> bool:
        return tag in self.risk_tags

    def risk_label(self) -> str:
        if self.has_tag(RiskTag.UNIFORM_FALLBACK):
            return "🔴 高风险"
        if self.has_tag(RiskTag.FALLBACK_BASELINE):
            return "🟠 中风险"
        if self.has_tag(RiskTag.LOW_CONFIDENCE) or self.has_tag(RiskTag.DEGRADED):
            return "🟡 注意"
        return "🟢 正常"

# ═══════════════════════════════════════════════════════════════
# 2. 容错降级守护引擎
# ═══════════════════════════════════════════════════════════════

class DegradationGuard:
    """
    容错降级守护

    每个模块独立检查, 故障自动降级到更稳定但更简单的链路。
    """

    def __init__(self):
        # 模块健康注册表
        self.modules: Dict[str, ModuleHealth] = {
            'v4.1_model': ModuleHealth('v4.1_model'),
            'v3.2_baseline': ModuleHealth('v3.2_baseline'),
            'draw_expert': ModuleHealth('draw_expert'),
            'odds_expert': ModuleHealth('odds_expert'),
            'nn_module': ModuleHealth('nn_module'),
            'sky_predictor': ModuleHealth('sky_predictor'),
            'vip_predictor': ModuleHealth('vip_predictor'),
            'odds_analyzer': ModuleHealth('odds_analyzer'),
            'trap_detector': ModuleHealth('trap_detector'),
            'risk_barrier': ModuleHealth('risk_barrier'),
            'cross_opponent': ModuleHealth('cross_opponent'),
            'balance_simulator': ModuleHealth('balance_simulator'),
            'scorer_tracker': ModuleHealth('scorer_tracker'),
            'data_source': ModuleHealth('data_source'),
        }
        logger.info("[DegradationGuard] 容错降级守护初始化 (14个监控模块)")

    # ═══════════════════════════════════════════════════════════
    # 核心API
    # ═══════════════════════════════════════════════════════════

    def guarded_predict(self,
                        home: str, away: str, league: str,
                        odds: Dict[str, float],
                        predict_fn_v41=None,   # v4.1预测函数
                        predict_fn_v32=None,   # v3.2回退函数
                        ) -> GuardedResult:
        """
        带容错的三级降级预测

        尝试链: v4.1 → v3.2 → odds-derived → uniform
        任何一层失败自动降级到下一层, 不抛异常。
        """
        result = GuardedResult()
        trace = []

        # ── L1: 尝试 v4.1 ──
        if predict_fn_v41 and self._check_module('v4.1_model'):
            try:
                pred = predict_fn_v41(home, away, odds)
                if pred and self._validate_probs(pred):
                    result.h_prob, result.d_prob, result.a_prob = pred
                    result.degradation_level = DegradationLevel.L1
                    result.is_healthy = True
                    self._record_success('v4.1_model')
                else:
                    raise ValueError("v4.1返回无效概率")
            except Exception as e:
                trace.append(DegradationTrace(
                    DegradationLevel.L1,
                    f"v4.1降级: {str(e)[:80]}",
                    time.strftime('%H:%M:%S'),
                    'v4.1_model'))
                self._record_failure('v4.1_model', str(e))
                result.add_tag(RiskTag.DEGRADED)

        # ── L2: 回退 v3.2 ──
        if result.degradation_level > DegradationLevel.L1:
            if predict_fn_v32 and self._check_module('v3.2_baseline', tolerate_failure=True):
                try:
                    pred = predict_fn_v32(home, away, odds)
                    if pred and self._validate_probs(pred):
                        result.h_prob, result.d_prob, result.a_prob = pred
                        result.degradation_level = DegradationLevel.L2
                        result.add_tag(RiskTag.FALLBACK_BASELINE)
                        self._record_success('v3.2_baseline')
                    else:
                        raise ValueError("v3.2返回无效概率")
                except Exception as e:
                    trace.append(DegradationTrace(
                        DegradationLevel.L2,
                        f"v3.2降级: {str(e)[:80]}",
                        time.strftime('%H:%M:%S'),
                        'v3.2_baseline'))
                    self._record_failure('v3.2_baseline', str(e))

        # ── L3: 赔率反推 ──
        if result.degradation_level > DegradationLevel.L2:
            try:
                oh, od_, oa = odds.get('home', 2.5), odds.get('draw', 3.2), odds.get('away', 2.8)
                if oh > 1.0:
                    inv = 1/oh + 1/od_ + 1/oa
                    result.h_prob = (1/oh) / inv
                    result.d_prob = (1/od_) / inv
                    result.a_prob = (1/oa) / inv
                    result.degradation_level = DegradationLevel.L3
                    result.add_tag(RiskTag.MISSING_DATA)
                    trace.append(DegradationTrace(
                        DegradationLevel.L3,
                        "模型不可用, 降级到赔率反推",
                        time.strftime('%H:%M:%S'),
                        'all_models'))
                else:
                    raise ValueError("赔率无效")
            except Exception as e:
                trace.append(DegradationTrace(
                    DegradationLevel.L3,
                    f"赔率反推失败: {str(e)[:80]}",
                    time.strftime('%H:%M:%S')))

        # ── L4: 均匀兜底 ──
        if result.degradation_level > DegradationLevel.L3:
            result.h_prob = result.d_prob = result.a_prob = 0.3333
            result.degradation_level = DegradationLevel.L4
            result.add_tag(RiskTag.UNIFORM_FALLBACK)
            trace.append(DegradationTrace(
                DegradationLevel.L4,
                "所有链路失败, 均匀兜底",
                time.strftime('%H:%M:%S')))

        # ── 风险评估 ──
        result.degradation_trace = trace
        self._assess_risk(result)

        return result

    def check_health(self) -> Dict[str, ModuleHealth]:
        """全模块健康检查"""
        return self.modules

    def check_expert(self, expert_name: str,
                     fallback_fn: callable = None) -> Tuple[bool, Any, List[str]]:
        """
        单个专家的容错执行

        Returns:
            (success, result, warnings)
        """
        warnings = []
        if not self._check_module(expert_name, tolerate_failure=True):
            warnings.append(f"{expert_name}模块不健康")
            if fallback_fn:
                try:
                    return False, fallback_fn(), warnings
                except Exception as e:
                    warnings.append(f"降级函数执行失败: {e}")
            return False, None, warnings

        return True, None, warnings  # caller fills result

    def safe_execute(self, module_name: str, fn: callable,
                     fallback_value: Any = None) -> Tuple[Any, bool]:
        """
        安全执行函数, 异常自动捕获并记录

        Returns:
            (result, success)
        """
        try:
            result = fn()
            self._record_success(module_name)
            return result, True
        except Exception as e:
            self._record_failure(module_name, str(e))
            logger.warning(f"[DegradationGuard] {module_name} 执行失败, 回退: {e}")
            return fallback_value, False

    # ═══════════════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════════════

    def _check_module(self, name: str, tolerate_failure: bool = False) -> bool:
        """检查模块是否可用"""
        module = self.modules.get(name)
        if not module:
            return tolerate_failure
        # 连续失败3次后需要人工介入
        if not module.healthy and module.fail_count >= 3 and not tolerate_failure:
            return False
        return True

    def _record_failure(self, name: str, error: str):
        module = self.modules.get(name)
        if module:
            module.record_failure(error)

    def _record_success(self, name: str):
        module = self.modules.get(name)
        if module and not module.healthy:
            module.record_recovery()

    @staticmethod
    def _validate_probs(probs) -> bool:
        """验证概率有效性"""
        if not probs or len(probs) != 3:
            return False
        h, d, a = probs
        if not all(isinstance(p, (int, float)) for p in [h, d, a]):
            return False
        if any(p < 0 or p > 1 for p in [h, d, a]):
            return False
        if abs(h + d + a - 1.0) > 0.15:  # 允许15%偏差
            return False
        return True

    def _assess_risk(self, result: GuardedResult):
        """综合风险评估"""
        tags = result.risk_tags
        risk_items = []

        if RiskTag.UNIFORM_FALLBACK in tags:
            risk_items.append("所有模型不可用, 使用均匀分布(33%/33%/33%)")
            result.risk_summary = "🔴 高风险: 预测无参考价值, 仅保证服务可用"

        elif RiskTag.FALLBACK_BASELINE in tags:
            risk_items.append(f"回退到v3.2基线 (Acc=59%, v4.1不可用)")
            result.risk_summary = "🟠 中风险: 基线模型预测, 准确率低于v4.1"

        elif RiskTag.MISSING_DATA in tags:
            risk_items.append("特征数据缺失, 使用赔率反推概率")
            result.risk_summary = "🟡 注意: 纯赔率预测, 无模型特征增强"

        elif RiskTag.DEGRADED in tags:
            risk_items.append("部分模块降级, 主链路仍可用")
            result.risk_summary = "🟡 部分降级"

        else:
            result.risk_summary = "🟢 正常: v4.1全功能链路"

    # ═══════════════════════════════════════════════════════════
    # D-Gate集成: 低置信度标记
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def d_gate_assess(h: float, d: float, a: float) -> Tuple[str, List[str]]:
        """
        D-Gate 精度过滤 + 风险标记

        Returns:
            (gate_result, risk_flags)
        """
        flags = []
        margin = d - max(h, a)

        if margin < 0.02:
            gate = "垃圾区"
            flags.append("平局信号极弱, 预测D不可靠")
        elif margin < 0.05:
            gate = "模糊区"
            flags.append("平局信号模糊, D需谨慎参考")
        elif margin < 0.08:
            gate = "可用区"
        elif margin < 0.20:
            gate = "高置信区"
        else:
            gate = "强D信号"

        # 附加风险
        if abs(h - a) < 0.05:
            flags.append("三分类概率接近, 比赛不确定性极高")
        if max(h, d, a) < 0.45:
            flags.append("最高概率<45%, 预测置信度不足")
        if d < 0.15:
            flags.append("P(D)<15%, 平局概率被压低(可能为杯赛热门局)")

        return gate, flags

    # ═══════════════════════════════════════════════════════════
    # 格式化输出
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def format_guard_status(result: GuardedResult) -> str:
        """格式化为6层报告片段"""
        lines = []
        lines.append(f"\n{'─' * 40}")
        lines.append(f"🛡️ 容错守护 (Degradation Guard)")

        level_labels = {DegradationLevel.L1: "v4.1全功能", DegradationLevel.L2: "v3.2基线",
                       DegradationLevel.L3: "赔率反推", DegradationLevel.L4: "均匀兜底"}
        lines.append(f"  🔗 链路: {level_labels.get(result.degradation_level, '未知')}")

        # D-Gate
        gate, d_flags = DegradationGuard.d_gate_assess(
            result.h_prob, result.d_prob, result.a_prob)
        d_margin = result.d_prob - max(result.h_prob, result.a_prob)
        lines.append(f"  🚦 D-Gate: {gate} (margin={d_margin:+.4f})")

        for flag in d_flags[:2]:
            lines.append(f"    ⚠ {flag}")

        # 风险标签
        if result.risk_tags:
            tags_str = ", ".join(t.name for t in result.risk_tags)
            lines.append(f"  🏷️ 风险标签: {tags_str}")
        lines.append(f"  📊 {result.risk_summary}")

        # 降级链
        if result.degradation_trace:
            lines.append(f"  📋 降级记录:")
            for t in result.degradation_trace[-3:]:  # 最近3条
                lines.append(f"    {t.timestamp} {t.level.name}: {t.reason[:70]}")

        return "\n".join(lines)

# ═══════════════════════════════════════════════════════════════
# 3. 单例
# ═══════════════════════════════════════════════════════════════

_guard_instance: Optional[DegradationGuard] = None

def get_degradation_guard() -> DegradationGuard:
    global _guard_instance
    if _guard_instance is None:
        _guard_instance = DegradationGuard()
    return _guard_instance

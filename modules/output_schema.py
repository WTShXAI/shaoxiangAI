"""
哨响AI v4.0 — 统一输出Schema (Unified Output Schema)
=====================================================
v4.0架构核心模块。所有专家、模型、分析模块的输出都必须遵循此标准。

五元组标准:
    probability   — 三分类概率 (H/D/A)，和为1
    distribution  — 扩展分布 (比分、进球、盘口等)
    reasoning     — 推理链条 (可解释性)
    confidence    — 置信度评估 (含不确定性)
    evidence      — 证据支撑 (数据来源、特征贡献)

设计原则:
    1. 向后兼容 v3.2 输出格式
    2. 向前扩展支持多市场 (1X2/AH/OU/Goals)
    3. 可序列化 (JSON Schema) 供前端消费
    4. 自带校验 (37项PredictionGuard规则)

作者: Architecture v4.0
日期: 2026-06-18
"""
from __future__ import annotations
import math
import logging
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, Tuple, Union

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 1. 核心数据类型
# ═══════════════════════════════════════════════════════════════

class MarketType(Enum):
    """市场类型枚举"""
    MATCH_RESULT = "1X2"        # 胜平负
    ASIAN_HANDICAP = "AH"       # 亚盘让球
    OVER_UNDER = "OU"           # 大小球
    TOTAL_GOALS = "GOALS"       # 总进球数
    CORRECT_SCORE = "SCORE"     # 波胆比分
    BOTH_SCORE = "BTTS"         # 双方进球


class ConfidenceLevel(Enum):
    """置信度等级"""
    VERY_HIGH = "very_high"     # >0.85
    HIGH = "high"               # 0.70-0.85
    MEDIUM = "medium"           # 0.50-0.70
    LOW = "low"                 # 0.30-0.50
    VERY_LOW = "very_low"       # <0.30


@dataclass
class ThreeWayProbability:
    """三分类概率 (H/D/A) — 核心输出"""
    home: float     # P(主胜)
    draw: float     # P(平局)
    away: float     # P(客胜)

    def __post_init__(self):
        """自动归一化"""
        total = self.home + self.draw + self.away
        if total > 0 and abs(total - 1.0) > 0.001:
            self.home /= total
            self.draw /= total
            self.away /= total

    def to_dict(self) -> Dict:
        return {"home": round(self.home, 6), "draw": round(self.draw, 6), "away": round(self.away, 6)}

    def top_prediction(self) -> str:
        """返回最高概率类别"""
        best = max(("H", self.home), ("D", self.draw), ("A", self.away), key=lambda x: x[1])
        return best[0]

    def margin(self) -> float:
        """D概率优势 = P(D) - max(P(H), P(A))"""
        return self.draw - max(self.home, self.away)

    @classmethod
    def from_dict(cls, d: Dict) -> "ThreeWayProbability":
        return cls(home=float(d.get("home", 0.33)), draw=float(d.get("draw", 0.34)), away=float(d.get("away", 0.33)))


@dataclass
class DistributionExtension:
    """扩展概率分布 — 比分、进球等"""
    score_probs: Optional[Dict[str, float]] = None       # 波胆概率 {"1-0": 0.12, ...}
    goal_probs: Optional[Dict[int, float]] = None         # 进球数分布 {0: 0.08, 1: 0.15, ...}
    handicap_probs: Optional[Dict[str, float]] = None     # 让球盘概率
    over_under_probs: Optional[Dict[str, float]] = None   # 大小球概率

    def to_dict(self) -> Dict:
        result = {}
        if self.score_probs:
            result["score"] = self.score_probs
        if self.goal_probs:
            result["goals"] = {str(k): v for k, v in self.goal_probs.items()}
        if self.handicap_probs:
            result["handicap"] = self.handicap_probs
        if self.over_under_probs:
            result["over_under"] = self.over_under_probs
        return result


@dataclass
class ReasoningChain:
    """推理链条 — 可解释性"""
    summary: str = ""                                     # 一句话总结
    steps: List[Dict[str, str]] = field(default_factory=list)  # [{"expert": "...", "finding": "...", "impact": "..."}]
    contradictions: List[str] = field(default_factory=list)    # 发现的矛盾点
    key_factors: List[str] = field(default_factory=list)       # 关键决策因子

    def to_dict(self) -> Dict:
        return {
            "summary": self.summary,
            "steps": self.steps,
            "contradictions": self.contradictions,
            "key_factors": self.key_factors,
        }


@dataclass
class ConfidenceAssessment:
    """置信度评估 — 含不确定性量化"""
    overall: float = 0.5                                  # 综合置信度 [0,1]
    level: str = "medium"                                 # 置信度等级
    calibration_score: Optional[float] = None             # 校准分数
    uncertainty_band: Optional[Tuple[float, float]] = None  # 不确定性带 (lower, upper)
    expert_agreement: Optional[float] = None              # 专家一致度 [0,1]

    def to_dict(self) -> Dict:
        return {
            "overall": round(self.overall, 4),
            "level": self.level,
            "calibration_score": round(self.calibration_score, 4) if self.calibration_score else None,
            "uncertainty": list(self.uncertainty_band) if self.uncertainty_band else None,
            "expert_agreement": round(self.expert_agreement, 4) if self.expert_agreement else None,
        }


@dataclass
class EvidencePackage:
    """证据支撑 — 数据来源与特征贡献"""
    data_sources: List[str] = field(default_factory=list)       # ["Interwetten赔率", "球队近期战绩", ...]
    feature_contributions: Dict[str, float] = field(default_factory=dict)  # {"spread": 0.15, "drift": 0.08, ...}
    model_version: str = ""                                     # 模型版本
    data_freshness: str = ""                                    # 数据新鲜度
    degradation_indicators: List[str] = field(default_factory=list)  # 降级指标

    def to_dict(self) -> Dict:
        return {
            "data_sources": self.data_sources,
            "feature_contributions": {k: round(v, 4) for k, v in self.feature_contributions.items()},
            "model_version": self.model_version,
            "data_freshness": self.data_freshness,
            "degradation_indicators": self.degradation_indicators,
        }


# ═══════════════════════════════════════════════════════════════
# 2. 统一输出主体
# ═══════════════════════════════════════════════════════════════

@dataclass
class UnifiedPrediction:
    """
    v4.0 统一预测输出 — 所有专家、模型、分析模块的标准输出格式

    五元组:
        probability:   ThreeWayProbability — 核心三分类概率
        distribution:  DistributionExtension — 扩展分布
        reasoning:     ReasoningChain — 可解释性
        confidence:    ConfidenceAssessment — 置信度
        evidence:      EvidencePackage — 证据支撑
    """
    probability: ThreeWayProbability
    distribution: DistributionExtension = field(default_factory=DistributionExtension)
    reasoning: ReasoningChain = field(default_factory=ReasoningChain)
    confidence: ConfidenceAssessment = field(default_factory=ConfidenceAssessment)
    evidence: EvidencePackage = field(default_factory=EvidencePackage)

    # 元信息
    expert_id: str = ""
    market_type: MarketType = MarketType.MATCH_RESULT
    created_at: str = ""
    execution_time_ms: float = 0.0

    def to_dict(self) -> Dict:
        """序列化为字典 — 前端消费标准格式"""
        return {
            "prediction": {
                "market": self.market_type.value,
                "probabilities": self.probability.to_dict(),
                "top_pick": self.probability.top_prediction(),
                "distribution": self.distribution.to_dict(),
            },
            "reasoning": self.reasoning.to_dict(),
            "confidence": self.confidence.to_dict(),
            "evidence": self.evidence.to_dict(),
            "meta": {
                "expert_id": self.expert_id,
                "created_at": self.created_at,
                "execution_time_ms": round(self.execution_time_ms, 2),
            }
        }

    def to_v3_compat(self) -> Dict:
        """向后兼容 v3.2 输出格式"""
        return {
            "prediction": self.probability.to_dict(),
            "confidence": self.confidence.overall,
            "top_pick": self.probability.top_prediction(),
            "reasoning_summary": self.reasoning.summary,
            "model_version": self.evidence.model_version,
        }

    def validate(self) -> Tuple[bool, List[str]]:
        """自校验 — 37项规则子集"""
        errors = []
        # 概率和校验
        total = self.probability.home + self.probability.draw + self.probability.away
        if abs(total - 1.0) > 0.01:
            errors.append(f"Probability sum={total:.4f}, expected 1.0")
        # 概率范围校验
        for label, val in [("H", self.probability.home), ("D", self.probability.draw), ("A", self.probability.away)]:
            if not 0 <= val <= 1:
                errors.append(f"P({label})={val} out of [0,1]")
        # 置信度校验
        if not 0 <= self.confidence.overall <= 1:
            errors.append(f"Confidence={self.confidence.overall} out of [0,1]")
        # NaN检查
        if math.isnan(self.probability.home) or math.isnan(self.probability.draw) or math.isnan(self.probability.away):
            errors.append("Probability contains NaN")
        return len(errors) == 0, errors


# ═══════════════════════════════════════════════════════════════
# 3. 多专家融合输出
# ═══════════════════════════════════════════════════════════════

@dataclass
class ExpertContribution:
    """单个专家在融合中的贡献"""
    expert_id: str
    expert_name: str
    domain: str                                 # quantization | game_theory | imbalance | ensemble | temporal | math | engineering
    probability: ThreeWayProbability
    weight: float                               # 融合权重
    confidence: float                           # 该专家自身置信度
    reasoning_summary: str
    execution_time_ms: float
    status: str                                 # success | fallback | error


@dataclass
class FusedPrediction(UnifiedPrediction):
    """
    多专家融合输出 — 包含各专家独立输出和融合过程

    继承 UnifiedPrediction 的五元组作为最终融合结果。
    """
    expert_outputs: List[ExpertContribution] = field(default_factory=list)
    fusion_method: str = "weighted_vote"          # weighted_vote | soft_voting | stacking | bayesian
    conflicts: List[Dict] = field(default_factory=list)  # 冲突记录
    arbitration_result: Optional[str] = None      # 仲裁结果

    def to_dict(self) -> Dict:
        base = super().to_dict()
        base["fusion"] = {
            "method": self.fusion_method,
            "expert_count": len(self.expert_outputs),
            "contributions": [
                {
                    "expert_id": c.expert_id,
                    "expert_name": c.expert_name,
                    "domain": c.domain,
                    "probabilities": c.probability.to_dict(),
                    "weight": round(c.weight, 4),
                    "confidence": round(c.confidence, 4),
                    "reasoning": c.reasoning_summary,
                    "status": c.status,
                }
                for c in self.expert_outputs
            ],
            "conflicts": self.conflicts,
            "arbitration": self.arbitration_result,
        }
        return base


# ═══════════════════════════════════════════════════════════════
# 4. Schema 校验器
# ═══════════════════════════════════════════════════════════════

class SchemaValidator:
    """输出Schema校验器 — 确保所有输出符合标准"""

    REQUIRED_FIELDS_TOP = {
        "prediction": ["market", "probabilities", "top_pick"],
        "confidence": ["overall", "level"],
        "reasoning": ["summary"],
        "meta": ["expert_id"],
    }
    REQUIRED_FIELDS_PROBS = ["home", "draw", "away"]

    @classmethod
    def validate_full(cls, output: Dict) -> Tuple[bool, List[str]]:
        """全量校验 — 返回 (是否通过, 错误列表)"""
        errors = []

        # 结构校验 — 顶层字段
        for section, fields in cls.REQUIRED_FIELDS_TOP.items():
            if section not in output:
                errors.append(f"Missing section: {section}")
                continue
            section_data = output.get(section, {})
            for f in fields:
                if f not in section_data:
                    errors.append(f"Missing field: {section}.{f}")

        # 概率字段专项校验 (嵌套在 prediction.probabilities 内)
        probs = output.get("prediction", {}).get("probabilities", {})
        if not probs:
            errors.append("Missing section: prediction.probabilities")
        else:
            for f in cls.REQUIRED_FIELDS_PROBS:
                if f not in probs:
                    errors.append(f"Missing field: prediction.probabilities.{f}")

        # 概率和校验
        probs = output.get("prediction", {}).get("probabilities", {})
        if probs:
            total = sum(probs.values())
            if abs(total - 1.0) > 0.01:
                errors.append(f"Probability sum={total:.4f}, expected 1.0")

        # 置信度范围校验
        conf = output.get("confidence", {})
        overall = conf.get("overall")
        if overall is not None and not (0 <= overall <= 1):
            errors.append(f"Confidence={overall} out of [0,1]")

        return len(errors) == 0, errors

    @classmethod
    def validate_light(cls, output: Dict) -> bool:
        """轻量校验 — 仅检查核心字段存在"""
        try:
            probs = output["prediction"]["probabilities"]
            return all(k in probs for k in ["home", "draw", "away"])
        except (KeyError, TypeError):
            return False


# ═══════════════════════════════════════════════════════════════
# 5. 工厂方法与便捷构建器
# ═══════════════════════════════════════════════════════════════

def create_simple_prediction(home: float, draw: float, away: float,
                              expert_id: str = "unknown",
                              confidence: float = 0.5,
                              summary: str = "") -> UnifiedPrediction:
    """快速创建简单预测"""
    return UnifiedPrediction(
        probability=ThreeWayProbability(home=home, draw=draw, away=away),
        confidence=ConfidenceAssessment(
            overall=confidence,
            level=_confidence_to_level(confidence),
        ),
        reasoning=ReasoningChain(summary=summary),
        expert_id=expert_id,
    )


def create_fallback_prediction(reason: str = "降级兜底") -> UnifiedPrediction:
    """创建降级兜底预测 (均匀分布)"""
    return UnifiedPrediction(
        probability=ThreeWayProbability(home=0.33, draw=0.34, away=0.33),
        confidence=ConfidenceAssessment(overall=0.1, level="very_low"),
        reasoning=ReasoningChain(summary=f"降级预测: {reason}"),
        evidence=EvidencePackage(degradation_indicators=[reason]),
    )


def create_from_v3_output(v3_output: Dict) -> UnifiedPrediction:
    """从 v3.2 输出格式转换"""
    pred = v3_output.get("prediction", {})
    return UnifiedPrediction(
        probability=ThreeWayProbability(
            home=float(pred.get("home", 0.33)),
            draw=float(pred.get("draw", 0.34)),
            away=float(pred.get("away", 0.33)),
        ),
        confidence=ConfidenceAssessment(
            overall=float(v3_output.get("confidence", 0.5)),
            level=_confidence_to_level(float(v3_output.get("confidence", 0.5))),
        ),
        reasoning=ReasoningChain(summary=v3_output.get("reasoning_summary", "")),
        evidence=EvidencePackage(model_version=v3_output.get("model_version", "")),
    )


def _confidence_to_level(conf: float) -> str:
    if conf >= 0.85: return "very_high"
    if conf >= 0.70: return "high"
    if conf >= 0.50: return "medium"
    if conf >= 0.30: return "low"
    return "very_low"


# ═══════════════════════════════════════════════════════════════
# 6. 术语注入器 (TBD: 加载 terminology.yaml)
# ═══════════════════════════════════════════════════════════════

class TerminologyInjector:
    """
    术语注入器 — 为输出注入专业术语

    从 terminology.yaml 加载六大领域术语，
    根据专家domain自动匹配术语注入到reasoning中。
    """

    DOMAIN_TERMS = {
        "quantization": ["泊松强度λ", "Dixon-Coles修正", "xG预期进球", "赔率漂移一阶导数", "过度离散"],
        "game_theory": ["庄家风控信号", "抽水率分解", "凯利指数", "诱盘模式", "RP风险溢价", "信息不对称"],
        "imbalance": ["类别不平衡度", "Focal Loss γ参数", "代价敏感权重", "阈值联合寻优"],
        "ensemble": ["Stacking集成", "OOF诚实回测", "D-Gate融合", "动态权重衰减", "元学习器"],
        "temporal": ["Transformer注意力", "LSTM隐状态", "序列漂移", "状态向量"],
        "math": ["95%置信区间", "McNemar检验", "Cohen's d效应量", "贝叶斯因子", "功效分析"],
        "probability": ["泊松强度λ", "xG预期进球", "比分分布", "联赛风格DNA", "过度离散"],
    }

    _yaml_loaded = False

    @classmethod
    def load_from_yaml(cls, yaml_path: str = None) -> bool:
        """从 terminology.yaml 加载完整术语表"""
        import os
        if yaml_path is None:
            # 自动查找
            candidates = [
                os.path.join(os.path.dirname(__file__), '..', 'config', 'terminology.yaml'),
                'config/terminology.yaml',
            ]
            for c in candidates:
                if os.path.exists(c):
                    yaml_path = c
                    break
        if yaml_path is None or not os.path.exists(yaml_path):
            return False

        try:
            import yaml
            with open(yaml_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)

            terms_loaded = {}
            for domain_key, domain_data in data.get('domains', {}).items():
                term_names = []
                for term in domain_data.get('terms', []):
                    term_names.append(term.get('zh', ''))
                if term_names:
                    terms_loaded[domain_key] = term_names

            if terms_loaded:
                cls.DOMAIN_TERMS.update(terms_loaded)
                cls._yaml_loaded = True
                return True
        except Exception:
            pass
        return False

    @classmethod
    def inject(cls, reasoning: ReasoningChain, domain: str) -> ReasoningChain:
        """为推理链注入领域术语"""
        terms = cls.DOMAIN_TERMS.get(domain, [])
        if terms and not reasoning.key_factors:
            reasoning.key_factors = terms[:3]  # 注入前3个术语作为关键因子
        return reasoning

    @classmethod
    def get_terminology_glossary(cls) -> Dict[str, List[str]]:
        """获取完整术语表"""
        return dict(cls.DOMAIN_TERMS)

    @classmethod
    def lookup_term(cls, keyword: str) -> Optional[str]:
        """根据关键词查找术语定义 (需yaml已加载)"""
        for domain_terms in cls.DOMAIN_TERMS.values():
            for term in domain_terms:
                if keyword in term:
                    return term
        return None

"""
哨响AI v4.0 — 赛后复盘归因引擎 (Post-Match Attribution Analyzer)
==================================================================
P2 深度专项模块。对比预测 vs 实际赛果，多维度根因分析。

核心能力:
    1. 预测偏差量化 — 概率偏差 / 方向错误 / 置信度误判
    2. 根因归因 — D-Gate / 赔率信号 / 特征贡献 / 模型漂移
    3. 专家分诊 — 按错误类型分配负责专家
    4. 改进建议 — 基于知识库的针对性建议

输出:
    PostMatchReport — 结构化复盘报告
    - 预测vs实际对比
    - 偏差分类 (probability/direction/confidence)
    - 根因诊断
    - 专家建议
    - 知识库关联教训

作者: Architecture v4.0 · P2 Phase
日期: 2026-06-18
"""
from __future__ import annotations
import logging
import time
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 1. 数据结构
# ═══════════════════════════════════════════════════════════════

class DeviationType(Enum):
    """偏差类型"""
    EXACT = "exact"              # 预测正确
    PROB_SHIFT = "prob_shift"    # 概率偏差 (方向对但概率不准)
    DIRECTION = "direction"      # 方向错误 (预测错了结果)
    CONFIDENCE = "confidence"    # 置信度误判 (高置信预测错误)
    SYSTEMATIC = "systematic"    # 系统性偏差 (连续多场同方向错误)

class RootCause(Enum):
    """根因分类"""
    D_GATE_FAILURE = "d_gate_failure"      # D-Gate误判 (应降级未降级 or 不应降级降级了)
    ODDS_SIGNAL_DECEPTION = "odds_deception"  # 赔率信号误导 (庄家诱盘)
    FEATURE_DRIFT = "feature_drift"         # 特征漂移
    MODEL_OVERFIT = "model_overfit"         # 模型过拟合
    COLD_START = "cold_start"               # 冷启动信息不足
    LEAGUE_ANOMALY = "league_anomaly"        # 联赛异常 (该联赛模式变化)
    UNKNOWN = "unknown"                     # 不明原因

@dataclass
class AttributionResult:
    """单条归因"""
    root_cause: str
    confidence: float              # 归因置信度 [0,1]
    responsible_expert: str        # 负责专家
    evidence: str                  # 证据
    suggestion: str                # 改进建议
    related_lessons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "root_cause": self.root_cause,
            "confidence": round(self.confidence, 4),
            "responsible_expert": self.responsible_expert,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
            "related_lessons": self.related_lessons,
        }

@dataclass
class PostMatchReport:
    """赛后复盘报告"""
    home_team: str
    away_team: str
    league: Optional[str]

    # 赛果
    actual_result: str           # H/D/A
    actual_score: Optional[str] = None

    # 预测
    predicted_probs: Dict[str, float] = field(default_factory=dict)
    predicted_result: str = ""
    prediction_confidence: float = 0.5

    # 偏差分析
    deviation_type: str = ""
    probability_error: float = 0.0    # |P(actual) - predicted|

    # 归因
    attributions: List[AttributionResult] = field(default_factory=list)
    primary_cause: str = ""
    primary_expert: str = ""

    # 建议
    recommendations: List[str] = field(default_factory=list)
    severity: str = "info"       # info / warning / critical

    # 元信息
    analysis_time_ms: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "match": {
                "home": self.home_team, "away": self.away_team,
                "league": self.league,
                "actual": {"result": self.actual_result, "score": self.actual_score},
                "predicted": {
                    "probabilities": {k: round(v, 4) for k, v in self.predicted_probs.items()},
                    "result": self.predicted_result,
                    "confidence": round(self.prediction_confidence, 4),
                },
            },
            "deviation": {
                "type": self.deviation_type,
                "probability_error": round(self.probability_error, 4),
            },
            "attribution": {
                "primary_cause": self.primary_cause,
                "primary_expert": self.primary_expert,
                "details": [a.to_dict() for a in self.attributions],
            },
            "recommendations": self.recommendations,
            "severity": self.severity,
            "meta": {"analysis_time_ms": round(self.analysis_time_ms, 2)},
        }

# ═══════════════════════════════════════════════════════════════
# 2. 赛后复盘引擎
# ═══════════════════════════════════════════════════════════════

class PostMatchAnalyzer:
    """
    赛后复盘归因引擎

    对比预测 vs 实际赛果，多维度根因归因。
    """

    # 根因→负责专家映射
    CAUSE_EXPERT_MAP = {
        "d_gate_failure": "曾均衡",
        "odds_deception": "杜博弈",
        "feature_drift": "季泊松",
        "model_overfit": "荣合众",
        "cold_start": "舒治理",
        "league_anomaly": "季泊松",
        "unknown": "郝优算",
    }

    def analyze(self,
                home_team: str, away_team: str, league: str,
                actual_result: str,
                h_prob: float, d_prob: float, a_prob: float,
                confidence: float = 0.5,
                odds: Dict[str, float] = None,
                actual_score: str = None,
                spread: float = None) -> PostMatchReport:
        """执行赛后复盘"""
        start = time.perf_counter()
        odds = odds or {}

        # 预测结果
        pred_result = max(("H", h_prob), ("D", d_prob), ("A", a_prob), key=lambda x: x[1])[0]
        probs = {"H": h_prob, "D": d_prob, "A": a_prob}
        actual_p = probs.get(actual_result, 0.0)

        # ── 1. 偏差分析 ──
        deviation, prob_error = self._analyze_deviation(
            pred_result, actual_result, actual_p, confidence
        )

        # ── 2. 根因归因 ──
        attributions = self._attribute_root_cause(
            pred_result, actual_result, h_prob, d_prob, a_prob,
            confidence, odds, actual_p, spread
        )

        # ── 3. 主因 + 负责专家 ──
        primary = attributions[0] if attributions else AttributionResult(
            root_cause="unknown", confidence=0.3,
            responsible_expert="郝优算",
            evidence="无法确定根因", suggestion="建议人工复查",
        )
        primary_expert = self.CAUSE_EXPERT_MAP.get(
            primary.root_cause, "郝优算"
        )

        # ── 4. 改进建议 ──
        recommendations = self._generate_recommendations(attributions)

        # ── 5. 严重度 ──
        severity = self._assess_severity(deviation, confidence, attributions)

        report = PostMatchReport(
            home_team=home_team, away_team=away_team, league=league,
            actual_result=actual_result, actual_score=actual_score,
            predicted_probs=probs, predicted_result=pred_result,
            prediction_confidence=confidence,
            deviation_type=deviation, probability_error=prob_error,
            attributions=attributions,
            primary_cause=primary.root_cause,
            primary_expert=primary_expert,
            recommendations=recommendations, severity=severity,
            analysis_time_ms=(time.perf_counter() - start) * 1000,
        )
        return report

    def _analyze_deviation(self, pred: str, actual: str,
                            actual_p: float, confidence: float) -> Tuple[str, float]:
        """偏差分析"""
        prob_error = abs(actual_p - 1.0)  # 理想情况 P(actual)=1.0

        if pred == actual:
            if prob_error < 0.30:
                return "exact", prob_error
            else:
                return "prob_shift", prob_error  # 方向对了但概率不准
        else:
            if confidence > 0.70:
                return "confidence", prob_error  # 高置信预测错了
            else:
                return "direction", prob_error   # 普通的预测错误

    def _attribute_root_cause(self, pred: str, actual: str,
                                h_p: float, d_p: float, a_p: float,
                                confidence: float, odds: Dict,
                                actual_p: float, spread: float) -> List[AttributionResult]:
        """根因归因"""
        results = []

        # 归因1: D-Gate误判
        if (pred == "D" and actual != "D") or (pred != "D" and actual == "D"):
            margin = d_p - max(h_p, a_p)
            if pred == "D" and margin < 0.08:
                results.append(AttributionResult(
                    root_cause="d_gate_failure",
                    confidence=0.85, responsible_expert="曾均衡",
                    evidence=f"预测D但margin={margin:.3f}<0.08(D-Gate阈值), 应触发降级",
                    suggestion="D-Gate Precision Filter阈值可能需要上调",
                    related_lessons=["d_gate_precision_filter"],
                ))
            elif actual == "D" and margin > 0.08:
                results.append(AttributionResult(
                    root_cause="d_gate_failure",
                    confidence=0.70, responsible_expert="曾均衡",
                    evidence=f"实际D但margin={margin:.3f}>0.08, D-Gate可能过于激进地降级了有效的D信号",
                    suggestion="检查D-Gate阈值是否过于激进",
                    related_lessons=["d_gate_aggressiveness"],
                ))

        # 归因2: 赔率信号误导
        if odds:
            h = odds.get("home", 2.0)
            d = odds.get("draw", 3.4)
            a = odds.get("away", 3.8)
            inv_sum = 1.0/h + 1.0/d + 1.0/a
            implied = {"H": (1.0/h)/inv_sum, "D": (1.0/d)/inv_sum, "A": (1.0/a)/inv_sum}
            odds_pred = max(implied, key=implied.get)

            if odds_pred != actual:
                results.append(AttributionResult(
                    root_cause="odds_deception",
                    confidence=0.65, responsible_expert="杜博弈",
                    evidence=f"赔率隐含概率指向{odds_pred}但实际赛果为{actual}, 庄家赔率可能为诱盘",
                    suggestion="关注庄家诱盘模式, 结合多机构赔率交叉验证",
                    related_lessons=["beta_calibration_destroy_draw"],
                ))

        # 归因3: 高spread冷门
        if spread is not None and abs(spread) > 5:
            favorite = "H" if spread > 0 else "A"
            if actual != favorite:
                results.append(AttributionResult(
                    root_cause="odds_deception",
                    confidence=0.75, responsible_expert="杜博弈",
                    evidence=f"spread={spread:.1f}下的冷门, 热门方向{implied.get(favorite, 0):.0%}被颠覆",
                    suggestion="spread>5的冷门概率应单独评估, 考虑浅盘诱盘检测",
                    related_lessons=["trap_odds_patterns"],
                ))

        # 归因4: 高置信误判
        if confidence > 0.70 and pred != actual:
            results.append(AttributionResult(
                root_cause="model_overfit",
                confidence=0.60, responsible_expert="荣合众",
                evidence=f"高置信({confidence:.0%})预测{pred}但实际为{actual}, 模型对该类比赛过度自信",
                suggestion="检查校准度(ECE), 考虑对该类比赛降低融合权重",
                related_lessons=["dimension_disaster_173", "meta_class_weight_draw_tradeoff"],
            ))

        # 如果没有匹配的归因 → unknown
        if not results:
            results.append(AttributionResult(
                root_cause="unknown",
                confidence=0.30, responsible_expert="郝优算",
                evidence=f"预测{pred}→实际{actual}, 无特征归因匹配",
                suggestion="建议人工复查 + 全量特征SHAP分析",
            ))

        return results

    def _generate_recommendations(self, attributions: List[AttributionResult]) -> List[str]:
        """生成改进建议"""
        recs = []
        seen = set()
        for attr in attributions:
            if attr.suggestion and attr.suggestion not in seen:
                recs.append(attr.suggestion)
                seen.add(attr.suggestion)
        return recs

    def _assess_severity(self, deviation: str, confidence: float,
                          attributions: List[AttributionResult]) -> str:
        """评估严重度"""
        if deviation == "confidence":
            return "critical"
        if any(a.root_cause == "d_gate_failure" and a.confidence > 0.7 for a in attributions):
            return "warning"
        if deviation == "direction":
            return "warning"
        return "info"

# ═══════════════════════════════════════════════════════════════
# 3. 全局单例
# ═══════════════════════════════════════════════════════════════

_pm_analyzer: Optional[PostMatchAnalyzer] = None

def get_pm_analyzer() -> PostMatchAnalyzer:
    global _pm_analyzer
    if _pm_analyzer is None:
        _pm_analyzer = PostMatchAnalyzer()
    return _pm_analyzer

def reset_pm_analyzer():
    global _pm_analyzer
    _pm_analyzer = None

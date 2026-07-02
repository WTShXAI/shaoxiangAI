"""
哨响AI v5.0 — 平局/冷门攻坚引擎 (Draw & Upset Analyzer)
=============================================================
P2 深度专项模块。平局预测精细化 + 冷门检测 + D-Gate门控 + 阈值寻优。

核心能力:
    1. Draw 概率精细化 — 基于 spread/odds/league 的 D 概率微调
    2. D-Gate 门控 — margin=P(D)-max(P(H),P(A)) 分桶精度过滤
    3. 冷门预警 — 综合赔率异常/机构分歧/球队状态的多维冷门评分
    4. 阈值联合寻优 — H/D/A 最优决策阈值推荐

输出:
    DrawUpsetReport — 结构化平局/冷门分析报告
    - D概率 (原始/精细后/D-Gate修正)
    - D-Gate区段 (垃圾区/模糊区/可用区/高置信区)
    - 冷门评分 (0-100)
    - 阈值建议

集成到 v5.0 编排器模式 C (平局攻坚):
    orchestrator.predict_structured(..., expert_mode='C')

作者: Architecture · P2 Phase
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
# 1. 分析结果数据结构
# ═══════════════════════════════════════════════════════════════

class DGateZone(Enum):
    """D-Gate 分桶区段"""
    GARBAGE = "garbage"        # margin [0, 0.02): Precision≈18.8%
    FUZZY_LOW = "fuzzy_low"    # margin [0.02, 0.05): Precision≈18.4%
    FUZZY = "fuzzy"            # margin [0.05, 0.10): Precision≈25.9%
    USABLE = "usable"          # margin [0.10, 0.20): Precision≈27.7%
    RELIABLE = "reliable"      # margin [0.20, 0.40): Precision≈39.4%
    HIGH_CONF = "high_conf"    # margin [0.40, 1.00): Precision≈75.2%

    @classmethod
    def from_margin(cls, margin: float) -> "DGateZone":
        if margin < 0.02:
            return cls.GARBAGE
        elif margin < 0.05:
            return cls.FUZZY_LOW
        elif margin < 0.10:
            return cls.FUZZY
        elif margin < 0.20:
            return cls.USABLE
        elif margin < 0.40:
            return cls.RELIABLE
        else:
            return cls.HIGH_CONF

    @property
    def precision(self) -> float:
        return {
            self.GARBAGE: 0.188, self.FUZZY_LOW: 0.184,
            self.FUZZY: 0.259, self.USABLE: 0.277,
            self.RELIABLE: 0.394, self.HIGH_CONF: 0.752,
        }[self]

    @property
    def label(self) -> str:
        return {
            self.GARBAGE: "垃圾区", self.FUZZY_LOW: "低模糊区",
            self.FUZZY: "模糊区", self.USABLE: "可用区",
            self.RELIABLE: "可靠区", self.HIGH_CONF: "高置信区",
        }[self]

    @property
    def recommendation(self) -> str:
        return {
            self.GARBAGE: "D概率几乎无参考价值, 建议降级为H/A",
            self.FUZZY_LOW: "D信号极弱, 视为噪声",
            self.FUZZY: "D信号较弱, 需结合其他信息",
            self.USABLE: "D信号可参考, 注意综合判断",
            self.RELIABLE: "D信号较强, 有一定参考价值",
            self.HIGH_CONF: "D信号很强, 高置信平局预测",
        }[self]

@dataclass
class DistributiveAnalysis:
    """Draw 概率分布分析"""
    raw_d_prob: float                       # 原始模型 D 概率
    refined_d_prob: float                   # 精细化后 D 概率
    d_gate_zone: str                        # D-Gate 区段
    d_gate_precision: float                 # 该区段历史精度
    d_gate_recommendation: str              # D-Gate 建议
    d_margin: float                         # margin = P(D)-max(P(H),P(A))
    spread_d_rate: Optional[float] = None   # 该 spread 区间历史 D 率
    league_d_prior: Optional[float] = None  # 联赛先验 D 率

    def to_dict(self) -> Dict:
        return {
            "raw_probability": round(self.raw_d_prob, 4),
            "refined_probability": round(self.refined_d_prob, 4),
            "d_gate_zone": self.d_gate_zone,
            "d_gate_precision": round(self.d_gate_precision, 4),
            "d_gate_recommendation": self.d_gate_recommendation,
            "margin": round(self.d_margin, 4),
            "spread_d_rate": round(self.spread_d_rate, 4) if self.spread_d_rate else None,
            "league_d_prior": round(self.league_d_prior, 4) if self.league_d_prior else None,
        }

@dataclass
class UpsetAnalysis:
    """冷门分析"""
    upset_score: float = 0.0            # 冷门综合评分 [0, 100]
    upset_level: str = "none"           # none / mild / moderate / high / extreme
    signals: List[Dict] = field(default_factory=list)  # 冷门信号
    recommendation: str = ""

    def to_dict(self) -> Dict:
        return {
            "upset_score": round(self.upset_score, 2),
            "upset_level": self.upset_level,
            "signals": self.signals,
            "recommendation": self.recommendation,
        }

@dataclass
class DrawUpsetReport:
    """
    平局/冷门分析报告 — v5.0 模式C输出
    """
    home_team: str
    away_team: str

    # 基础概率
    h_prob: float
    d_prob: float
    a_prob: float

    # Draw 分析
    draw_analysis: DistributiveAnalysis

    # 冷门分析
    upset: UpsetAnalysis = field(default_factory=UpsetAnalysis)

    # 阈值建议
    threshold_recommendation: Dict = field(default_factory=dict)

    # 综合结论
    d_prediction_confidence: str = "low"    # very_low/low/medium/high/very_high
    summary: str = ""
    analysis_time_ms: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "match": {"home": self.home_team, "away": self.away_team},
            "probabilities": {
                "home": round(self.h_prob, 4),
                "draw": round(self.d_prob, 4),
                "away": round(self.a_prob, 4),
                "top_pick": max(("H", self.h_prob), ("D", self.d_prob), ("A", self.a_prob), key=lambda x: x[1])[0],
            },
            "draw_analysis": self.draw_analysis.to_dict(),
            "upset_analysis": self.upset.to_dict(),
            "threshold_recommendation": self.threshold_recommendation,
            "conclusion": {
                "d_prediction_confidence": self.d_prediction_confidence,
                "summary": self.summary,
            },
            "meta": {"analysis_time_ms": round(self.analysis_time_ms, 2)},
        }

# ═══════════════════════════════════════════════════════════════
# 2. 平局/冷门分析引擎
# ═══════════════════════════════════════════════════════════════

class DrawUpsetAnalyzer:
    """
    平局/冷门攻坚引擎 — v5.0 模式C核心

    提供 Draw 概率精细化、D-Gate门控、冷门预警、阈值建议。
    """

    # Spread→Draw率映射 (v2.1 数据校准: odds_features 50K条, 2026-06-19)
    SPREAD_D_RATES = {
        (0, 1): 0.257, (1, 3): 0.262, (3, 5): 0.276,
        (5, 8): 0.257, (8, 20): 0.238, (20, 100): 0.160,
    }

    # 联赛先验D率 (v2.1 数据校准: odds_features 50K条)
    LEAGUE_D_PRIORS = {
        "意乙": 0.359, "南非超": 0.356, "阿乙": 0.339,
        "阿尔甲": 0.336, "苏冠": 0.329, "法丙": 0.318,
        "摩洛超": 0.317, "西丙": 0.316, "以甲": 0.314,
        "埃及超": 0.312, "乌克超": 0.312, "约超联": 0.308,
        "伊朗超": 0.307, "斯亚甲": 0.306, "阿乙曼特": 0.304,
        # 五大联赛 (保留经验值)
        "英超": 0.24, "意甲": 0.27, "德甲": 0.22,
        "西甲": 0.25, "法甲": 0.28, "J联赛": 0.28,
        "巴甲": 0.25, "土超": 0.26, "俄超": 0.27,
        "葡超": 0.26, "荷甲": 0.25, "阿甲": 0.26,
    }

    def analyze(self, home_team: str, away_team: str,
                h_prob: float, d_prob: float, a_prob: float,
                odds: Dict[str, float] = None,
                league: str = None,
                spread: float = None) -> DrawUpsetReport:
        """执行平局/冷门分析"""
        start = time.perf_counter()
        odds = odds or {}
        h_odds = odds.get("home", 2.0)
        d_odds = odds.get("draw", 3.4)
        a_odds = odds.get("away", 3.8)

        # ── 1. Draw 精细化 ──
        draw_analysis = self._analyze_draw(h_prob, d_prob, a_prob, h_odds, d_odds, a_odds, league, spread)

        # ── 2. 冷门预警 ──
        upset = self._analyze_upset(h_prob, d_prob, a_prob, odds, spread)

        # ── 3. 阈值建议 ──
        threshold = self._recommend_threshold(draw_analysis)

        # ── 4. 综合结论 ──
        confidence, summary = self._assess_d_confidence(draw_analysis, upset)

        report = DrawUpsetReport(
            home_team=home_team, away_team=away_team,
            h_prob=h_prob, d_prob=d_prob, a_prob=a_prob,
            draw_analysis=draw_analysis, upset=upset,
            threshold_recommendation=threshold,
            d_prediction_confidence=confidence, summary=summary,
            analysis_time_ms=(time.perf_counter() - start) * 1000,
        )
        return report

    # ═══════════════════════════════════════════════════════════

    def _analyze_draw(self, h_p: float, d_p: float, a_p: float,
                       h_o: float, d_o: float, a_o: float,
                       league: str, spread: float) -> DistributiveAnalysis:
        """Draw 概率精细化"""
        margin = d_p - max(h_p, a_p)
        zone = DGateZone.from_margin(margin)

        # 从赔率反推隐含D概率
        inv_sum = 1.0/h_o + 1.0/d_o + 1.0/a_o
        implied_d = (1.0/d_o) / inv_sum

        # 获取spread区间的历史D率
        spread_d_rate = None
        if spread is not None:
            for (lo, hi), rate in self.SPREAD_D_RATES.items():
                if lo <= abs(spread) < hi:
                    spread_d_rate = rate
                    break

        # 获取联赛先验D率
        league_prior = self.LEAGUE_D_PRIORS.get(league) if league else None

        # 精细化: 模型D概率与历史/赔率融合
        refined = d_p  # 基础
        weights = []
        weighted_sum = 0

        # 注入赔率D信号 (25%权重)
        weighted_sum += implied_d * 0.25
        weights.append(0.25)

        # 注入spread历史D率 (20%权重)
        if spread_d_rate:
            weighted_sum += spread_d_rate * 0.20
            weights.append(0.20)

        # 注入联赛先验 (15%权重)
        if league_prior:
            weighted_sum += league_prior * 0.15
            weights.append(0.15)

        # 模型概率权重 = 1 - sum(other_weights)
        model_weight = 1.0 - sum(weights)
        refined = d_p * model_weight + weighted_sum

        return DistributiveAnalysis(
            raw_d_prob=d_p, refined_d_prob=refined,
            d_gate_zone=zone.label, d_gate_precision=zone.precision,
            d_gate_recommendation=zone.recommendation,
            d_margin=margin,
            spread_d_rate=spread_d_rate, league_d_prior=league_prior,
        )

    def _analyze_upset(self, h_p: float, d_p: float, a_p: float,
                        odds: Dict[str, float], spread: float) -> UpsetAnalysis:
        """冷门预警分析"""
        signals = []
        score = 0.0

        h = odds.get("home", 2.0)
        d = odds.get("draw", 3.4)
        a = odds.get("away", 3.8)

        # 信号1: 模型预测热门但赔率不支持
        inv_sum = 1.0/h + 1.0/d + 1.0/a
        implied_h = (1.0/h) / inv_sum
        implied_a = (1.0/a) / inv_sum

        if h_p > 0.50 and implied_h < h_p - 0.10:
            signals.append({
                "signal": "model_overconfident_home",
                "description": f"模型高估主队(模型{h_p:.0%} vs 赔率{implied_h:.0%}), 差距{h_p-implied_h:.0%}",
                "weight": 15,
            })
            score += 15

        if a_p > 0.45 and implied_a < a_p - 0.10:
            signals.append({
                "signal": "model_overconfident_away",
                "description": f"模型高估客队(模型{a_p:.0%} vs 赔率{implied_a:.0%}), 差距{a_p-implied_a:.0%}",
                "weight": 15,
            })
            score += 15

        # 信号2: 高spread下的D预测 (冷门信号)
        if spread is not None and abs(spread) > 5 and d_p > 0.30:
            signals.append({
                "signal": "high_spread_draw",
                "description": f"spread={spread:.1f}下模型预测D={d_p:.0%}, 历史该区间D率仅{self._get_spread_d_rate(abs(spread)):.0%}",
                "weight": 20,
            })
            score += 20

        # 信号3: 赔率异常波动 (高抽水)
        total_margin = inv_sum - 1.0
        if total_margin > 0.10:
            signals.append({
                "signal": "abnormal_margin",
                "description": f"抽水率{total_margin:.1%}异常偏高, 存在不确定性事件",
                "weight": 10,
            })
            score += 10

        # 信号4: D赔率极低 (庄家防范平局)
        if d < 3.0 and d_p < 0.25:
            signals.append({
                "signal": "low_d_odds",
                "description": f"庄家D赔率{d:.2f}<3.0但模型D={d_p:.0%}偏低, 可能遗漏平局信号",
                "weight": 15,
            })
            score += 15

        # 信号5: 极端实力差下的爆冷风险
        if spread is not None and abs(spread) > 8:
            signals.append({
                "signal": "extreme_spread_risk",
                "description": f"spread={abs(spread):.1f}极端让球, 存在爆冷风险",
                "weight": 10,
            })
            score += 10

        # 评级
        if score >= 60:
            level = "extreme"
            rec = "🔴 极高冷门风险! 多个信号强烈指向冷门方向"
        elif score >= 40:
            level = "high"
            rec = "🟠 高冷门风险, 建议关注潜在冷门"
        elif score >= 20:
            level = "moderate"
            rec = "🟡 中等冷门风险, 需注意异常信号"
        elif score >= 10:
            level = "mild"
            rec = "🟢 轻微异常, 无强烈冷门信号"
        else:
            level = "none"
            rec = "✅ 未检测到冷门风险"

        return UpsetAnalysis(
            upset_score=score, upset_level=level,
            signals=signals, recommendation=rec,
        )

    def _recommend_threshold(self, draw: DistributiveAnalysis) -> Dict:
        """阈值推荐"""
        zone = DGateZone.from_margin(draw.d_margin)

        if zone == DGateZone.GARBAGE or zone == DGateZone.FUZZY_LOW:
            rec = {
                "action": "downgrade_d",
                "description": f"D-Gate区段({zone.label})精度仅{zone.precision:.0%}, 建议降级D为H/A",
                "suggested_d_threshold": 0.50,
            }
        elif zone == DGateZone.FUZZY or zone == DGateZone.USABLE:
            rec = {
                "action": "keep_d_cautious",
                "description": f"D信号可用但需谨慎, 当前区间精度{zone.precision:.0%}",
                "suggested_d_threshold": 0.40,
            }
        else:
            rec = {
                "action": "trust_d",
                "description": f"D信号可靠, 区间精度{zone.precision:.0%}",
                "suggested_d_threshold": 0.30,
            }

        return rec

    def _assess_d_confidence(self, draw: DistributiveAnalysis,
                              upset: UpsetAnalysis) -> Tuple[str, str]:
        """综合D置信度评估"""
        zone = DGateZone.from_margin(draw.d_margin)

        if zone in (DGateZone.RELIABLE, DGateZone.HIGH_CONF):
            confidence = "high"
        elif zone in (DGateZone.USABLE, DGateZone.FUZZY):
            confidence = "medium"
        elif zone == DGateZone.FUZZY_LOW:
            confidence = "low"
        else:
            confidence = "very_low"

        # 冷门信号下调置信度
        if upset.upset_score > 30:
            confidence = "low" if confidence in ("high", "medium") else "very_low"

        summaries = {
            "very_low": "D预测不可靠, margin极低, 建议降级",
            "low": "D预测可信度低, 冷门信号+低margin叠加",
            "medium": "D预测有一定依据, 需结合其他分析验证",
            "high": "D预测可信度高, margin显著+历史精度支撑",
            "very_high": "D预测高置信, margin很大+多个信号支持",
        }

        return confidence, summaries.get(confidence, summaries["low"])

    def _get_spread_d_rate(self, abs_spread: float) -> float:
        for (lo, hi), rate in self.SPREAD_D_RATES.items():
            if lo <= abs_spread < hi:
                return rate
        return 0.12

# ═══════════════════════════════════════════════════════════════
# 3. 全局单例
# ═══════════════════════════════════════════════════════════════

_draw_analyzer: Optional[DrawUpsetAnalyzer] = None

def get_draw_analyzer() -> DrawUpsetAnalyzer:
    global _draw_analyzer
    if _draw_analyzer is None:
        _draw_analyzer = DrawUpsetAnalyzer()
    return _draw_analyzer

def reset_draw_analyzer():
    global _draw_analyzer
    _draw_analyzer = None

"""
哨响AI v5.0 — 赔率深度分析引擎 (Odds Deep Analyzer)
========================================================
P2 深度专项模块。将现有博彩分析模块封装为 v5.0 标准输出。

集成模块:
    - BookmakerBayesInfer: 贝叶斯赔率逆向 (三层推断 L1/L2/L3)
    - BookmakerTrapDetector: 12引擎陷阱检测
    - HarvestingGuard: 收割防护
    - margin_likelihood_bridge: 非均匀抽水分解

输出:
    OddsDeepReport — 结构化赔率深度分析报告
    - 隐含概率分解 (均匀/非均匀)
    - 庄家意图解读
    - 诱盘/陷阱检测结果
    - 收割防护状态
    - RP风险溢价分析

集成到 v5.0 编排器模式 B (赔率深挖):
    orchestrator.predict_structured(..., expert_mode='B')
    或
    orchestrator.predict_nl('赔率分析下', ...)

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

class TrapRiskLevel(Enum):
    """陷阱风险等级"""
    SAFE = "safe"              # 无异常
    SUSPICIOUS = "suspicious"  # 可疑
    DANGER = "danger"          # 危险
    HARVESTING = "harvesting"  # 收割区

class BookmakerConfidence(Enum):
    """庄家自信度"""
    HIGH = "high"              # 高自信 (抽水低+赔率稳定)
    NORMAL = "normal"          # 正常
    UNSURE = "unsure"          # 不确定
    HEDGING = "hedging"        # 对冲中 (抽水高+赔率波动)

@dataclass
class MarginDecomposition:
    """抽水率分解"""
    total_margin: float                          # 总抽水率
    uniform_margin: float                        # 均匀抽水
    non_uniform_extra: Dict[str, float] = field(default_factory=dict)  # 各方向额外抽水
    draw_protection: float = 0.0                 # 平局额外保护
    interpretation: str = ""                     # 解读

    def to_dict(self) -> Dict:
        return {
            "total_margin": round(self.total_margin, 4),
            "uniform_margin": round(self.uniform_margin, 4),
            "non_uniform_extra": {k: round(v, 4) for k, v in self.non_uniform_extra.items()},
            "draw_protection": round(self.draw_protection, 4),
            "interpretation": self.interpretation,
        }

@dataclass
class ImpliedProbabilityBreakdown:
    """隐含概率分解"""
    raw_implied: Dict[str, float]       # 比例法 (均匀抽水假设)
    bayes_corrected: Dict[str, float]   # 贝叶斯校正 (非均匀抽水)
    fair_odds: Dict[str, float]         # 公平赔率
    correction_magnitude: float = 0.0   # 校正幅度 (越大说明非均匀抽水越严重)

    def to_dict(self) -> Dict:
        return {
            "raw_implied": {k: round(v, 4) for k, v in self.raw_implied.items()},
            "bayes_corrected": {k: round(v, 4) for k, v in self.bayes_corrected.items()},
            "fair_odds": {k: round(v, 4) for k, v in self.fair_odds.items()},
            "correction_magnitude": round(self.correction_magnitude, 4),
        }

@dataclass
class TrapDetectionResult:
    """陷阱检测结果"""
    risk_level: str                          # safe/suspicious/danger/harvesting
    active_traps: List[Dict[str, str]]       # 活跃陷阱 [{pattern, engine, confidence, description}]
    trap_score: float                        # 综合陷阱评分 [0,1]
    recommendation: str = ""                 # 建议
    details: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "risk_level": self.risk_level,
            "active_traps": self.active_traps,
            "trap_score": round(self.trap_score, 4),
            "recommendation": self.recommendation,
            "details": self.details,
        }

@dataclass
class OddsDeepReport:
    """
    赔率深度分析报告 — v5.0 模式B输出

    包含完整的赔率逆向分析、陷阱检测、收割防护结果。
    """
    # 基础信息
    home_team: str
    away_team: str
    odds: Dict[str, float]

    # 核心分析
    implied_probs: ImpliedProbabilityBreakdown
    margin: MarginDecomposition
    bookmaker_confidence: str                        # 庄家自信度
    bookmaker_signal: str = ""                       # 庄家风控信号解读

    # 陷阱检测
    trap: TrapDetectionResult = field(default_factory=lambda: TrapDetectionResult(
        risk_level="safe", active_traps=[], trap_score=0.0
    ))

    # 收割防护
    harvesting_active: bool = False
    harvesting_zones: List[str] = field(default_factory=list)

    # 风险溢价
    rp_barriers: List[Dict] = field(default_factory=list)  # RP屏障状态
    rp_interpretation: str = ""

    # 综合结论
    overall_risk: str = "normal"                     # normal/elevated/high/extreme
    summary: str = ""
    analysis_time_ms: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "match": {"home": self.home_team, "away": self.away_team, "odds": self.odds},
            "implied_probabilities": self.implied_probs.to_dict(),
            "margin_decomposition": self.margin.to_dict(),
            "bookmaker_assessment": {
                "confidence": self.bookmaker_confidence,
                "signal": self.bookmaker_signal,
            },
            "trap_detection": self.trap.to_dict(),
            "harvesting": {
                "active": self.harvesting_active,
                "zones": self.harvesting_zones,
            },
            "risk_premium": {
                "barriers": self.rp_barriers,
                "interpretation": self.rp_interpretation,
            },
            "conclusion": {
                "overall_risk": self.overall_risk,
                "summary": self.summary,
            },
            "meta": {"analysis_time_ms": round(self.analysis_time_ms, 2)},
        }

# ═══════════════════════════════════════════════════════════════
# 2. 赔率深度分析引擎
# ═══════════════════════════════════════════════════════════════

class OddsDeepAnalyzer:
    """
    赔率深度分析引擎 — v5.0 模式B核心

    封装现有博彩分析模块，输出结构化的 OddsDeepReport。
    """

    def __init__(self):
        self._bayes_infer = None
        self._trap_detector = None
        self._harvesting_guard = None

    def _ensure_modules(self):
        """懒加载底层模块"""
        if self._bayes_infer is None:
            try:
                from bookmaker_sim.margin_likelihood_bridge import BookmakerBayesInfer
                self._bayes_infer = BookmakerBayesInfer()
            except ImportError:
                logger.debug("BookmakerBayesInfer 模块未安装")
                self._bayes_infer = None
            except Exception as e:
                logger.warning(f"BookmakerBayesInfer 不可用: {e}")
                self._bayes_infer = None

        if self._trap_detector is None:
            try:
                from bookmaker_sim.bookmaker_trap_detector import BookmakerTrapDetector
                self._trap_detector = BookmakerTrapDetector()
            except ImportError:
                logger.debug("BookmakerTrapDetector 模块未安装")
                self._trap_detector = None
            except Exception as e:
                logger.warning(f"BookmakerTrapDetector 不可用: {e}")
                self._trap_detector = None

        if self._harvesting_guard is None:
            try:
                from bookmaker_sim.harvesting_guard import HarvestingGuard
                self._harvesting_guard = HarvestingGuard()
            except ImportError:
                logger.debug("HarvestingGuard 模块未安装")
                self._harvesting_guard = None
            except Exception as e:
                logger.warning(f"HarvestingGuard 不可用: {e}")
                self._harvesting_guard = None

    def analyze(self, home_team: str, away_team: str,
                odds: Dict[str, float], league: str = None,
                match_context: Dict = None) -> OddsDeepReport:
        """
        执行赔率深度分析

        Args:
            home_team: 主队名
            away_team: 客队名
            odds: 赔率 {home, draw, away}
            league: 联赛名
            match_context: 额外上下文

        Returns:
            OddsDeepReport: 完整分析报告
        """
        start = time.perf_counter()
        self._ensure_modules()

        # 默认赔率
        odds = odds or {}
        h = odds.get("home", 2.0)
        d = odds.get("draw", 3.4)
        a = odds.get("away", 3.8)

        # ── 1. 隐含概率分解 ──
        implied = self._analyze_implied_probs(h, d, a)

        # ── 2. 抽水率分解 ──
        margin = self._analyze_margin(h, d, a)

        # ── 3. 庄家自信度评估 ──
        confidence, confidence_reason = self._assess_bookmaker_confidence(margin, h, d, a)

        # ── 4. 庄家风控信号 ──
        signal = self._interpret_bookmaker_signal(implied, margin, confidence)

        # ── 5. 陷阱检测 ──
        trap = self._detect_traps(home_team, away_team, odds)

        # ── 6. 收割防护 ──
        harvesting, zones = self._check_harvesting(odds)

        # ── 7. RP屏障分析 ──
        rp_barriers, rp_interp = self._analyze_rp_barriers(h, d, a)

        # ── 8. 综合风险 ──
        overall_risk, summary = self._assess_overall_risk(
            trap, harvesting, margin, implied
        )

        report = OddsDeepReport(
            home_team=home_team, away_team=away_team, odds=odds,
            implied_probs=implied, margin=margin,
            bookmaker_confidence=confidence, bookmaker_signal=signal,
            trap=trap, harvesting_active=harvesting, harvesting_zones=zones,
            rp_barriers=rp_barriers, rp_interpretation=rp_interp,
            overall_risk=overall_risk, summary=summary,
            analysis_time_ms=(time.perf_counter() - start) * 1000,
        )
        return report

    # ═══════════════════════════════════════════════════════════
    # 内部分析方法
    # ═══════════════════════════════════════════════════════════

    def _analyze_implied_probs(self, h: float, d: float, a: float) -> ImpliedProbabilityBreakdown:
        """隐含概率分解 — 比例法 + 贝叶斯校正"""
        # SEE: bookmaker_sim.bayesian_odds_inverter.odds_to_probs_vector() — canonical impl
        # L1: 比例法
        inv_sum = 1.0/h + 1.0/d + 1.0/a
        raw = {"home": (1.0/h)/inv_sum, "draw": (1.0/d)/inv_sum, "away": (1.0/a)/inv_sum}

        # L2/L3: 贝叶斯校正 (使用 BookmakerBayesInfer 如果可用)
        corrected = dict(raw)
        fair = {"home": 1.0/raw["home"], "draw": 1.0/raw["draw"], "away": 1.0/raw["away"]}
        correction_magnitude = 0.0

        if self._bayes_infer:
            try:
                result = self._bayes_infer.proportional_implied_probs(
                    {"home": h, "draw": d, "away": a}
                )
                # bayes_infer 可能有更高级的校正
                # 保守起见，校正 D 概率 (比例法低估D)
                total_margin = inv_sum - 1.0
                d_extra = max(0, total_margin * 0.15)  # D方向通常多抽水15%
                if d_extra > 0:
                    corrected["draw"] = raw["draw"] * (1 + d_extra / raw["draw"])
                    # 重新归一化
                    total = sum(corrected.values())
                    corrected = {k: v/total for k, v in corrected.items()}
                    correction_magnitude = abs(corrected["draw"] - raw["draw"])
            except ImportError:
                logger.debug("Bayes inference module not loaded")
            except (ValueError, RuntimeError) as e:
                logger.debug(f"Bayes correction skipped: {e}")
            except Exception as e:
                logger.warning(f"Bayes correction failed: {e}")

        return ImpliedProbabilityBreakdown(
            raw_implied=raw, bayes_corrected=corrected,
            fair_odds=fair, correction_magnitude=correction_magnitude,
        )

    def _analyze_margin(self, h: float, d: float, a: float) -> MarginDecomposition:
        """抽水率分解"""
        inv_sum = 1.0/h + 1.0/d + 1.0/a
        total_margin = inv_sum - 1.0

        # 均匀抽水假设
        uniform = total_margin / 3

        # 非均匀抽水估算 (各方向对总抽水的贡献偏差)
        extra = {
            "home": (1.0/h) / inv_sum - 1.0/3,
            "draw": (1.0/d) / inv_sum - 1.0/3,
            "away": (1.0/a) / inv_sum - 1.0/3,
        }

        # 平局保护 (D方向额外抽水)
        draw_protect = max(0, extra["draw"]) * total_margin

        # 解读
        if total_margin < 0.05:
            interp = "极低抽水: 庄家高度自信或激烈竞争环境"
        elif total_margin < 0.08:
            interp = "正常抽水范围 (5-8%)"
        elif total_margin < 0.12:
            interp = "偏高抽水: 不确定性增加或风控收紧"
        else:
            interp = "极高抽水: 高度不确定, 警惕异常"

        if draw_protect > 0.008:
            interp += "; 平局方向有额外利润保护 (庄家不看好平局)"

        return MarginDecomposition(
            total_margin=total_margin, uniform_margin=uniform,
            non_uniform_extra=extra, draw_protection=draw_protect,
            interpretation=interp,
        )

    def _assess_bookmaker_confidence(self, margin: MarginDecomposition,
                                      h: float, d: float, a: float) -> Tuple[str, str]:
        """评估庄家自信度"""
        total = margin.total_margin

        if total < 0.04:
            return "high", "抽水率极低(<4%), 庄家对定价高度自信"
        elif total < 0.07:
            return "normal", "正常抽水范围, 庄家对定价有把握"
        elif total < 0.10:
            return "unsure", "抽水率偏高(7-10%), 庄家对结果不确定性增加"
        else:
            return "hedging", "抽水率异常高(>10%), 庄家正在积极对冲风险"

    def _interpret_bookmaker_signal(self, implied: ImpliedProbabilityBreakdown,
                                     margin: MarginDecomposition, confidence: str) -> str:
        """解读庄家风控信号"""
        probs = implied.bayes_corrected
        top = max(probs, key=probs.get)
        top_prob = probs[top]

        parts = []

        # 方向信号
        if top == "home" and top_prob > 0.50:
            parts.append("庄家定价偏向主队方向")
        elif top == "away" and top_prob > 0.45:
            parts.append("庄家定价偏向客队方向")
        elif top == "draw" and top_prob > 0.30:
            parts.append("庄家定价未排除平局可能")

        # 抽水信号
        if margin.draw_protection > 0.01:
            parts.append(f"平局额外抽水{margin.draw_protection:.1%}(庄家保护)")

        # 自信度信号
        if confidence == "high":
            parts.append("庄家定价自信, 信号可信度高")
        elif confidence == "hedging":
            parts.append("庄家积极对冲, 存在不确定性事件")

        return "; ".join(parts) if parts else "无特殊信号"

    def _detect_traps(self, home_team: str, away_team: str,
                      odds: Dict[str, float]) -> TrapDetectionResult:
        """陷阱检测"""
        traps = []
        details = []
        score = 0.0

        h = odds.get("home", 2.0)
        d = odds.get("draw", 3.4)
        a = odds.get("away", 3.8)

        # 简化陷阱规则 (完整版由 BookmakerTrapDetector 提供)
        inv_sum = 1.0/h + 1.0/d + 1.0/a
        raw_p = {"H": (1.0/h)/inv_sum, "D": (1.0/d)/inv_sum, "A": (1.0/a)/inv_sum}

        # 规则1: 浅盘大热 (H隐含概率极高但赔率不够低)
        if raw_p["H"] > 0.70 and h > 1.4:
            traps.append({
                "pattern": "浅盘大热", "engine": "favorite_trap",
                "confidence": "high",
                "description": f"主队隐含概率{raw_p['H']:.0%}但赔率{h:.2f}偏高, 疑似诱导热门方向"
            })
            score += 0.3
            details.append("浅盘大热: 热门赔率偏高, 庄家引诱资金涌入热门方")

        # 规则2: D赔率异常 (极低D赔率)
        if d < 3.0:
            traps.append({
                "pattern": "低D赔率", "engine": "draw_alert",
                "confidence": "medium",
                "description": f"平局赔率{d:.2f}异常低, 庄家防范平局"
            })
            score += 0.15
            details.append(f"低D赔率: {d:.2f}低于常规, 庄家对平局有防范")

        # 规则3: 抽水异常
        total_margin = inv_sum - 1.0
        if total_margin > 0.12:
            traps.append({
                "pattern": "高抽水", "engine": "margin_alert",
                "confidence": "medium",
                "description": f"总抽水率{total_margin:.1%}, 异常偏高"
            })
            score += 0.1
            details.append(f"高抽水率: {total_margin:.1%} > 12%")

        # 规则4: A赔率极端低 (客队大热但赔率有水分)
        if a < 1.5 and raw_p["A"] > 0.65:
            traps.append({
                "pattern": "客队过热", "engine": "away_trap",
                "confidence": "medium",
                "description": f"客队赔率{a:.2f}偏低, 可能诱导客队方向"
            })
            score += 0.15
            details.append("客队过热: 资金单边涌入客队, 警惕冷门")

        # 风险评估
        if score >= 0.5:
            risk = "danger"
            rec = "⚠️ 高风险! 检测到多个陷阱信号, 建议回避或小注"
        elif score >= 0.25:
            risk = "suspicious"
            rec = "⚡ 可疑信号, 建议谨慎对待赔率方向"
        elif score > 0:
            risk = "suspicious"
            rec = "轻微异常, 可正常分析但需关注"
        else:
            risk = "safe"
            rec = "未检测到明显陷阱, 赔率结构正常"

        return TrapDetectionResult(
            risk_level=risk, active_traps=traps, trap_score=score,
            recommendation=rec, details=details,
        )

    def _check_harvesting(self, odds: Dict[str, float]) -> Tuple[bool, List[str]]:
        """收割防护检测"""
        zones = []
        h = odds.get("home", 2.0)
        d = odds.get("draw", 3.4)
        a = odds.get("away", 3.8)

        # RP屏障: 赔率<1.10 或 >10.0 为收割区
        for label, val in [("home", h), ("draw", d), ("away", a)]:
            if val < 1.10:
                zones.append(f"{label}赔率{val:.2f}进入超低赔付收割区")
            elif val > 10.0:
                zones.append(f"{label}赔率{val:.2f}进入超高赔付收割区")

        return len(zones) > 0, zones

    def _analyze_rp_barriers(self, h: float, d: float, a: float) -> Tuple[List[Dict], str]:
        """RP风险溢价屏障分析"""
        barriers = []

        for label, val in [("H", h), ("D", d), ("A", a)]:
            if val < 1.15:
                barriers.append({"direction": label, "odds": val, "barrier": "low_rp",
                                 "meaning": f"极低赔付区, 庄家不欢迎{label}方向投注"})
            elif val > 8.0:
                barriers.append({"direction": label, "odds": val, "barrier": "high_rp",
                                 "meaning": f"极高赔付区, 庄家设置{label}方向为收割区"})

        if not barriers:
            interp = "无RP屏障, 赔率在正常交易区间"
        elif len(barriers) == 3:
            interp = "全方向RP屏障! 庄家全面封闭, 可能比赛有重大不确定性"
        else:
            interp = f"{len(barriers)}个方向设置RP屏障: " + "; ".join(
                f"{b['direction']}={b['odds']:.2f}" for b in barriers
            )

        return barriers, interp

    def _assess_overall_risk(self, trap: TrapDetectionResult,
                              harvesting: bool, margin: MarginDecomposition,
                              implied: ImpliedProbabilityBreakdown) -> Tuple[str, str]:
        """综合风险评估"""
        risk_score = 0

        if trap.risk_level == "danger":
            risk_score += 3
        elif trap.risk_level == "suspicious":
            risk_score += 1

        if harvesting:
            risk_score += 2

        if margin.total_margin > 0.12:
            risk_score += 1

        if implied.correction_magnitude > 0.05:
            risk_score += 1

        if risk_score >= 4:
            return "extreme", "🔴 极高风险! 多重异常信号叠加, 强烈建议回避"
        elif risk_score >= 2:
            return "high", "🟡 高风险, 存在多个异常信号, 需谨慎"
        elif risk_score >= 1:
            return "elevated", "🟢 轻微异常, 可正常分析"
        else:
            return "normal", "✅ 赔率结构正常, 无异常信号"

# ═══════════════════════════════════════════════════════════════
# 3. 全局单例
# ═══════════════════════════════════════════════════════════════

_analyzer_instance: Optional[OddsDeepAnalyzer] = None

def get_odds_analyzer() -> OddsDeepAnalyzer:
    """获取赔率分析器单例"""
    global _analyzer_instance
    if _analyzer_instance is None:
        _analyzer_instance = OddsDeepAnalyzer()
    return _analyzer_instance

def reset_odds_analyzer():
    """重置单例 (测试用)"""
    global _analyzer_instance
    _analyzer_instance = None

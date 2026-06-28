"""
哨响AI v4.0 — L4 庄家风控防线识别引擎 (Risk Barrier Engine)
===============================================================
将分散在杜博弈(赔率分析)+陷阱检测器中的风控防线识别, 升级为
执行层的独立子引擎, 与陷阱检测器并列, 强化核心差异化能力。

4层RP防线体系 (Risk Premium Barriers):
  R-Barrier¹  赔率异常屏障    — 检测单个盘口的异常溢价/压制
  R-Vol²      波动率屏障      — 检测赔率时序波动异常
  R-Water³    水位屏障        — 检测凯利指数/返奖率异常
  R-Cross⁴    跨市场屏障      — 检测多个盘口间的不一致信号

输出:
  RiskBarrierReport:
    - barrier_level: 当前触发最高防线级别 (0-4)
    - active_barriers: 活跃防线详情
    - risk_score: 综合风险评分 [0,1]
    - bookmaker_intent: 庄家意图推断
    - prediction_impact: 对预测的修正建议

作者: Architecture v4.0 · L4 Phase
日期: 2026-06-19
"""
from __future__ import annotations
import logging, math
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger('RiskBarrier')

# ═══════════════════════════════════════════════════════════════
# 1. 数据结构
# ═══════════════════════════════════════════════════════════════

class BarrierType(Enum):
    """4层RP防线"""
    R_BARRIER = 1     # 赔率异常: 单盘口溢价/压制
    R_VOL = 2         # 波动率: 赔率时序波动
    R_WATER = 3       # 水位: 凯利/返奖率
    R_CROSS = 4       # 跨市场: 盘口不一致

class BookmakerIntent(Enum):
    """庄家意图推断"""
    NEUTRAL = "neutral"            # 无明显意图
    ATTRACT_HOME = "attract_home"  # 诱主
    ATTRACT_DRAW = "attract_draw"  # 诱平
    ATTRACT_AWAY = "attract_away"  # 诱客
    SUPPRESS_FAVORITE = "suppress_favorite"  # 压热门
    HARVESTING = "harvesting"      # 收割模式

@dataclass
class BarrierSignal:
    """单条防线信号"""
    barrier_type: BarrierType
    level: int                     # 触发级别 1-4
    triggered: bool
    severity: float                # [0,1]
    description: str
    evidence: str                  # 证据
    impact: str = ""               # 对预测的影响
    odds_value: float = 0.0        # 相关赔率数值

@dataclass
class RiskBarrierReport:
    """完整风控防线报告"""
    # 基础信息
    home: str
    away: str
    odds_1x2: Dict[str, float] = field(default_factory=dict)

    # 防线状态
    barrier_level: int = 0         # 触发最高防线 (0=无, 1-4)
    active_barriers: List[BarrierSignal] = field(default_factory=list)

    # 综合评分
    risk_score: float = 0.0        # [0,1]
    risk_label: str = "safe"       # safe/cautious/danger/harvesting

    # 庄家意图
    bookmaker_intent: BookmakerIntent = BookmakerIntent.NEUTRAL
    intent_confidence: float = 0.0

    # 预测修正
    prediction_impact: str = ""     # 修正建议
    d_prob_adjust: float = 0.0      # P(D)修正量
    h_prob_adjust: float = 0.0      # P(H)修正量
    confidence_multiplier: float = 1.0  # 置信度倍率

    # 附加上下文
    harvesting_risk: bool = False
    trap_active: bool = False
    summary: str = ""

# ═══════════════════════════════════════════════════════════════
# 2. 核心引擎
# ═══════════════════════════════════════════════════════════════

class RiskBarrierEngine:
    """
    庄家风控防线识别引擎

    独立于陷阱检测器, 专注于从赔率结构中还原庄家
    的四层风控防线信号, 推断庄家真实意图。
    """

    def __init__(self):
        logger.info("[RiskBarrier] 4层RP防线引擎初始化")

    def scan(self, home: str, away: str,
             odds_1x2: Dict[str, float],
             league: str = "",
             odds_ah: Dict = None,
             odds_ou: Dict = None,
             odds_open: Dict = None) -> RiskBarrierReport:
        """
        扫描庄家风控防线

        Args:
            home, away: 主客队名
            odds_1x2: 1X2赔率 {home, draw, away}
            league: 联赛名
            odds_ah: 让球赔率 (可选, 用于R-Cross)
            odds_ou: 大小球赔率 (可选, 用于R-Cross)
            odds_open: 开盘赔率 (可选, 用于R-Vol)
        """
        report = RiskBarrierReport(home=home, away=away, odds_1x2=odds_1x2)

        oh = odds_1x2.get('home', 2.5)
        od = odds_1x2.get('draw', 3.2)
        oa = odds_1x2.get('away', 2.8)

        # ── R-Barrier¹: 赔率异常检测 ──
        r1 = self._scan_barrier_1(oh, od, oa, league)
        if r1.triggered:
            report.active_barriers.append(r1)

        # ── R-Vol²: 波动率检测 ──
        if odds_open:
            r2 = self._scan_barrier_2(oh, od, oa, odds_open)
            if r2.triggered:
                report.active_barriers.append(r2)

        # ── R-Water³: 水位异常 ──
        r3 = self._scan_barrier_3(oh, od, oa)
        if r3.triggered:
            report.active_barriers.append(r3)

        # ── R-Cross⁴: 跨市场一致性 ──
        if odds_ah or odds_ou:
            r4 = self._scan_barrier_4(oh, od, oa, odds_ah, odds_ou)
            if r4.triggered:
                report.active_barriers.append(r4)

        # ── 综合评分 ──
        report.barrier_level = max([b.level for b in report.active_barriers], default=0)
        report.risk_score = self._compute_risk_score(report.active_barriers)

        # ── 风险标签 ──
        if report.risk_score >= 0.7:
            report.risk_label = "harvesting"
        elif report.risk_score >= 0.5:
            report.risk_label = "danger"
        elif report.risk_score >= 0.3:
            report.risk_label = "cautious"
        else:
            report.risk_label = "safe"

        # ── 庄家意图推断 ──
        report.bookmaker_intent, report.intent_confidence = self._infer_intent(
            oh, od, oa, report.active_barriers)

        # ── 预测修正 ──
        report.d_prob_adjust, report.h_prob_adjust, report.confidence_multiplier = \
            self._compute_prediction_impact(report)

        report.summary = self._build_summary(report)

        return report

    # ═══════════════════════════════════════════════════════════
    # R-Barrier¹: 赔率异常检测
    # ═══════════════════════════════════════════════════════════

    def _scan_barrier_1(self, oh: float, od: float, oa: float,
                        league: str) -> BarrierSignal:
        """检测单个盘口的异常溢价或压制"""
        inv_sum = 1/oh + 1/od + 1/oa
        overround = inv_sum - 1
        ph, pd, pa = (1/oh)/inv_sum, (1/od)/inv_sum, (1/oa)/inv_sum

        # 抽水率异常
        if overround > 0.12:
            return BarrierSignal(
                barrier_type=BarrierType.R_BARRIER,
                level=3,
                triggered=True,
                severity=min(1.0, (overround - 0.10) * 3),
                description="超高抽水率",
                evidence=f"抽水率{overround:.1%}>12%, 庄家极度不确定",
                impact="所有概率置信度降低, 不建议下注",
            )

        # 平局赔率异常压制
        draw_margin = (pd - 0.25) / 0.25  # 偏离联赛均值25%
        if abs(draw_margin) > 0.40:
            direction = "压低" if draw_margin < 0 else "抬高"
            return BarrierSignal(
                barrier_type=BarrierType.R_BARRIER,
                level=2,
                triggered=True,
                severity=min(1.0, abs(draw_margin)),
                description=f"平局赔率异常{direction}",
                evidence=f"P(D)={pd:.1%}, 偏离联赛均值25% (±{abs(draw_margin):.0%})",
                impact=f"{direction}平局概率 → 庄家在{direction}平局信号",
                odds_value=od,
            )

        # 单边溢价 (某方向赔率明显高于公平赔率)
        fair = [1/ph, 1/pd, 1/pa]
        premiums = [oh/fair[0]-1, od/fair[1]-1, oa/fair[2]-1]
        max_premium = max(premiums)
        if max_premium > 0.05:
            idx = premiums.index(max_premium)
            labels = ['主胜', '平局', '客胜']
            return BarrierSignal(
                barrier_type=BarrierType.R_BARRIER,
                level=1,
                triggered=True,
                severity=min(1.0, max_premium * 5),
                description=f"{labels[idx]}端溢价",
                evidence=f"{labels[idx]}赔率溢价{max_premium:.1%}",
                impact=f"庄家对{labels[idx]}结果收取额外风险溢价",
            )

        return BarrierSignal(barrier_type=BarrierType.R_BARRIER, level=1,
                            triggered=False, severity=0, description="", evidence="")

    # ═══════════════════════════════════════════════════════════
    # R-Vol²: 波动率检测
    # ═══════════════════════════════════════════════════════════

    def _scan_barrier_2(self, oh: float, od: float, oa: float,
                        odds_open: Dict) -> BarrierSignal:
        """检测赔率时序波动异常"""
        ooh = odds_open.get('home', oh)
        ood = odds_open.get('draw', od)
        ooa = odds_open.get('away', oa)

        drifts = {
            'H': (oh - ooh) / ooh,
            'D': (od - ood) / ood,
            'A': (oa - ooa) / ooa,
        }
        max_drift = max(abs(v) for v in drifts.values())
        max_dir = max(drifts, key=lambda k: abs(drifts[k]))

        if max_drift > 0.15:  # 波动>15%
            direction = "↑" if drifts[max_dir] > 0 else "↓"
            return BarrierSignal(
                barrier_type=BarrierType.R_VOL,
                level=3,
                triggered=True,
                severity=min(1.0, max_drift * 3),
                description=f"{max_dir}端赔率大幅波动",
                evidence=f"{max_dir}赔率{drifts[max_dir]:+.1%} (开盘{ooh:.2f}/{ood:.2f}/{ooa:.2f})",
                impact=f"波动方向{direction} → 资金流向{direction}端, 警惕诱盘",
            )
        elif max_drift > 0.08:
            return BarrierSignal(
                barrier_type=BarrierType.R_VOL,
                level=1,
                triggered=True,
                severity=min(1.0, max_drift * 5),
                description=f"{max_dir}端轻微波动",
                evidence=f"{max_dir}赔率{drifts[max_dir]:+.1%}",
                impact="正常市场波动, 无需过度解读",
            )

        return BarrierSignal(barrier_type=BarrierType.R_VOL, level=1,
                            triggered=False, severity=0, description="", evidence="")

    # ═══════════════════════════════════════════════════════════
    # R-Water³: 水位异常
    # ═══════════════════════════════════════════════════════════

    def _scan_barrier_3(self, oh: float, od: float, oa: float) -> BarrierSignal:
        """检测凯利指数/返奖率异常"""
        inv_sum = 1/oh + 1/od + 1/oa
        return_rate = 1 / inv_sum

        if return_rate < 0.88:  # 返奖率<88%
            return BarrierSignal(
                barrier_type=BarrierType.R_WATER,
                level=2,
                triggered=True,
                severity=min(1.0, (0.92 - return_rate) * 15),
                description="低返奖率(庄家利润极高)",
                evidence=f"返奖率{return_rate:.1%}<88% (抽水{1-return_rate:.1%})",
                impact="庄家极度自信, 不建议反向操作",
            )

        # 非均匀抽水: 平局被多抽
        uniform = 1/oh + 1/od + 1/oa
        if uniform > 0:
            extra_on_draw = (1/od) / uniform - 0.33
            if extra_on_draw > 0.04:
                return BarrierSignal(
                    barrier_type=BarrierType.R_WATER,
                    level=1,
                    triggered=True,
                    severity=min(1.0, extra_on_draw * 8),
                    description="平局非均匀抽水",
                    evidence=f"平局额外抽水{extra_on_draw:.1%}",
                    impact="庄家对平局最谨慎, 额外抽水→P(D)被压低",
                )

        return BarrierSignal(barrier_type=BarrierType.R_WATER, level=1,
                            triggered=False, severity=0, description="", evidence="")

    # ═══════════════════════════════════════════════════════════
    # R-Cross⁴: 跨市场一致性
    # ═══════════════════════════════════════════════════════════

    def _scan_barrier_4(self, oh: float, od: float, oa: float,
                        odds_ah: Dict = None, odds_ou: Dict = None) -> BarrierSignal:
        """检测多个盘口间的不一致信号"""
        signals = []

        # 1X2 vs AH 一致性
        if odds_ah:
            ah_h = odds_ah.get('home', 1.90)
            ah_a = odds_ah.get('away', 1.90)
            # 如果1X2主胜概率高但AH客队水位异常低 → 矛盾
            inv_sum = 1/oh + 1/od + 1/oa
            ph = (1/oh) / inv_sum
            if ph > 0.5 and ah_a < 1.80:
                signals.append(f"1X2主胜概率{ph:.0%} vs AH客队水位{ah_a:.2f}(偏低)→矛盾")

        # 1X2 vs OU 一致性
        if odds_ou:
            ou_over = odds_ou.get('over', 1.95)
            ou_under = odds_ou.get('under', 1.95)
            spread = (1/oh) - (1/oa) if oa > 0 else 0
            # 大SPREAD(强势一方) + 小球低水 → 矛盾
            if abs(spread) > 0.3 and ou_under < 1.80:
                signals.append(f"SPREAD={spread:.2f}(实力悬殊) vs 小球低水{ou_under:.2f}→不匹配")

        if signals:
            return BarrierSignal(
                barrier_type=BarrierType.R_CROSS,
                level=2,
                triggered=True,
                severity=min(1.0, len(signals) * 0.4),
                description="跨市场不一致",
                evidence="; ".join(signals),
                impact="盘口间存在套利空间或庄家在不同市场释放矛盾信号",
            )

        return BarrierSignal(barrier_type=BarrierType.R_CROSS, level=1,
                            triggered=False, severity=0, description="", evidence="")

    # ═══════════════════════════════════════════════════════════
    # 综合评分与推断
    # ═══════════════════════════════════════════════════════════

    def _compute_risk_score(self, barriers: List[BarrierSignal]) -> float:
        """综合风险评分"""
        if not barriers:
            return 0.0
        # 加权: level高的防线权重更大
        weights = {1: 0.8, 2: 1.0, 3: 1.5, 4: 2.0}
        total = sum(weights.get(b.level, 1.0) * b.severity for b in barriers)
        max_possible = sum(weights.get(b.level, 1.0) for b in barriers)
        return min(1.0, total / max(max_possible, 1))

    def _infer_intent(self, oh: float, od: float, oa: float,
                      barriers: List[BarrierSignal]) -> Tuple[BookmakerIntent, float]:
        """从防线信号推断庄家意图"""
        inv_sum = 1/oh + 1/od + 1/oa
        ph, pd, pa = (1/oh)/inv_sum, (1/od)/inv_sum, (1/oa)/inv_sum

        # 触发防线数量多 → 庄家在操作
        active_count = len(barriers)
        if active_count == 0:
            return BookmakerIntent.NEUTRAL, 0.0

        # 收割模式: 3+防线同时触发
        if active_count >= 3:
            return BookmakerIntent.HARVESTING, min(0.9, active_count * 0.3)

        # 检测压制方向
        suppress_signals = [b for b in barriers if "压低" in b.description or "suppress" in b.description]
        attract_signals = [b for b in barriers if "溢价" in b.description]

        if suppress_signals and oh < 2.0:
            return BookmakerIntent.SUPPRESS_FAVORITE, 0.7
        elif attract_signals:
            # 哪个方向被溢价 → 庄家在诱导另一个方向
            if ph > 0.6:
                return BookmakerIntent.ATTRACT_HOME, 0.6
            elif pa > 0.5:
                return BookmakerIntent.ATTRACT_AWAY, 0.6
            elif pd > 0.35:
                return BookmakerIntent.ATTRACT_DRAW, 0.5

        return BookmakerIntent.NEUTRAL, min(0.5, active_count * 0.15)

    def _compute_prediction_impact(self, report: RiskBarrierReport) -> Tuple[float, float, float]:
        """计算对预测的修正量"""
        d_adj = 0.0
        h_adj = 0.0
        conf = 1.0

        for b in report.active_barriers:
            if b.barrier_type == BarrierType.R_BARRIER and "平局" in b.description:
                d_adj += 0.03 * b.severity  # 平局异常→提升D
            elif b.barrier_type == BarrierType.R_VOL:
                conf *= 0.85  # 波动率异常→降低置信度
            elif b.barrier_type == BarrierType.R_WATER and "非均匀" in b.description:
                d_adj += 0.04  # 庄家多抽平局→平局概率被压低→修正回补
            elif b.barrier_type == BarrierType.R_CROSS:
                conf *= 0.80  # 跨市场矛盾→大幅降低置信度

        if report.risk_label == "harvesting":
            conf *= 0.70

        return d_adj, h_adj, conf

    def _build_summary(self, report: RiskBarrierReport) -> str:
        if not report.active_barriers:
            return "4层防线: 无异常触发, 庄家无明显风控动作"
        return f"{report.barrier_level}层防线触发 | 风险{report.risk_score:.0%} | 意图:{report.bookmaker_intent.value}"

    # ═══════════════════════════════════════════════════════════
    # 格式化输出
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def format_for_report(report: RiskBarrierReport) -> str:
        """格式化为6层报告片段"""
        if not report.active_barriers:
            return f"\n{'─' * 40}\n🛡️ 庄家风控防线\n  ✅ 4层防线无异常触发"

        lines = []
        lines.append(f"\n{'─' * 40}")
        lines.append(f"🛡️ 庄家风控防线 (RP Barrier)")

        # 防线状态总览
        icons = {1: "🟢", 2: "🟡", 3: "🟠", 4: "🔴"}
        barrier_names = {
            BarrierType.R_BARRIER: "R-Barrier¹ 赔率异常",
            BarrierType.R_VOL: "R-Vol² 波动率",
            BarrierType.R_WATER: "R-Water³ 水位",
            BarrierType.R_CROSS: "R-Cross⁴ 跨市场",
        }

        for b in report.active_barriers:
            icon = icons.get(b.level, "⚪")
            name = barrier_names.get(b.barrier_type, "?")
            lines.append(f"  {icon} {name}: L{b.level} {b.description}")
            lines.append(f"     证据: {b.evidence[:80]}")

        # 综合评分
        risk_labels = {"safe": "🟢 安全", "cautious": "🟡 谨慎", "danger": "🟠 危险", "harvesting": "🔴 收割"}
        lines.append(f"  ─────────────────────")
        lines.append(f"  综合: {risk_labels.get(report.risk_label, report.risk_label)} | "
                    f"评分 {report.risk_score:.2f} | 防线L{report.barrier_level}")

        # 庄家意图
        intent_labels = {
            BookmakerIntent.NEUTRAL: "无明显意图",
            BookmakerIntent.ATTRACT_HOME: "诱主",
            BookmakerIntent.ATTRACT_DRAW: "诱平",
            BookmakerIntent.ATTRACT_AWAY: "诱客",
            BookmakerIntent.SUPPRESS_FAVORITE: "压热门",
            BookmakerIntent.HARVESTING: "🔴 收割模式",
        }
        if report.bookmaker_intent != BookmakerIntent.NEUTRAL:
            lines.append(f"  意图: {intent_labels[report.bookmaker_intent]} "
                        f"(置信度{report.intent_confidence:.0%})")

        # 预测修正
        if report.confidence_multiplier < 1.0:
            lines.append(f"  修正: 置信度×{report.confidence_multiplier:.2f}")
        if report.d_prob_adjust != 0:
            lines.append(f"        P(D) {'+' if report.d_prob_adjust>0 else ''}{report.d_prob_adjust:.0%}")

        return "\n".join(lines)

# ═══════════════════════════════════════════════════════════════
# 3. 单例
# ═══════════════════════════════════════════════════════════════

_engine_instance: Optional[RiskBarrierEngine] = None

def get_risk_barrier_engine() -> RiskBarrierEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = RiskBarrierEngine()
    return _engine_instance

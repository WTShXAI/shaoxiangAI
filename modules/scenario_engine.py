"""
哨响AI v5.0 — L4 场景适配引擎 (Scenario Adaptation Engine)
===============================================================
针对不同比赛场景, 自动切换模型参数、阈值、权重, 让适配更灵活。

场景体系:
  联赛级 (LEAGUE)     → 标准参数, 历史数据丰富
  杯赛级 (CUP)        → D-boost, 陷阱阈值敏感, 冷启动处理
  决赛级 (FINAL)      → 心理因子, 保守预测, 加时赛补偿
  强弱悬殊 (GAP)      → 热门翻车检测, D-Gate增强, 交叉对比权重↑
  德比战 (DERBY)      → 主场优势打折, 平局率提升

切换维度:
  1. 概率校准参数 (D-boost, 冷启动混合系数)
  2. 陷阱检测阈值 (动态陷阱阈值 ×0.8~1.5)
  3. 专家权重 (杜博弈↑决赛, 曾均衡↑杯赛, 季泊松↑联赛)
  4. 置信度折扣 (决赛×0.70, 强弱悬殊×0.85)
  5. 输出风险提示级别

用法:
    engine = ScenarioEngine()
    config = engine.adapt(home='巴西', away='阿根廷', league='世界杯', 
                          is_final=False, matchday=1)
    # → CupConfig(d_boost=0.375, trap_mult=1.2, cold_start=True, ...)

作者: Architecture · L4 Phase
日期: 2026-06-19
"""
from __future__ import annotations
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger('ScenarioEngine')

# ═══════════════════════════════════════════════════════════════
# 1. 场景定义
# ═══════════════════════════════════════════════════════════════

class Scenario(Enum):
    """比赛场景"""
    LEAGUE = "league"            # 联赛 (意甲/英超/西甲...)
    CUP_GROUP = "cup_group"      # 杯赛小组赛
    CUP_KNOCKOUT = "cup_knockout" # 杯赛淘汰赛
    FINAL = "final"              # 决赛
    STRONG_FAVORITE = "gap"      # 强弱悬殊
    DERBY = "derby"              # 德比战

@dataclass
class ScenarioConfig:
    """场景配置快照"""
    scenario: Scenario

    # ── 概率校准 ──
    d_boost: float = 0.0            # D概率提升量
    home_advantage: float = 0.08    # 主场优势
    cold_start_mix: float = 0.0     # 冷启动混合系数 (0=不混合)
    draw_target_rate: float = 0.25  # 目标平局率

    # ── 陷阱检测 ──
    trap_threshold_mult: float = 1.0  # 陷阱阈值倍率 (1.0=默认)
    barrier_sensitivity: float = 1.0  # 防线敏感度

    # ── 专家权重 ──
    expert_weights: Dict[str, float] = field(default_factory=lambda: {
        'quant': 1.0,    # 季泊松(量化)
        'game_theory': 1.0,  # 杜博弈(博弈)
        'imbalance': 1.0, # 曾均衡(平局)
        'ensemble': 1.0,  # 荣合众(集成)
    })

    # ── 信心与控制 ──
    confidence_mult: float = 1.0     # 置信度倍率
    max_pred_stake: float = 1.0      # 最大投注建议倍率
    risk_warning_level: str = "normal"  # normal/elevated/high

    # ── 特殊规则 ──
    forbid_heavy_favorite_bet: bool = False  # 禁止重注热门
    require_cross_check: bool = False        # 必须交叉验证
    extra_time_compensation: bool = False    # 加时赛补偿

    # 描述
    description: str = ""

    def summary(self) -> str:
        parts = [f"[{self.scenario.value}]"]
        if self.d_boost > 0:
            parts.append(f"D+{self.d_boost:.0%}")
        if self.cold_start_mix > 0:
            parts.append(f"冷启动{self.cold_start_mix:.0%}")
        if self.confidence_mult < 1.0:
            parts.append(f"置信×{self.confidence_mult:.2f}")
        if self.trap_threshold_mult != 1.0:
            parts.append(f"陷阱×{self.trap_threshold_mult:.1f}")
        return " | ".join(parts)

# ═══════════════════════════════════════════════════════════════
# 2. 场景适配引擎
# ═══════════════════════════════════════════════════════════════

class ScenarioEngine:
    """
    场景适配引擎

    根据比赛属性自动配置最优参数组合。
    """

    # ── 联赛识别 ──
    CUP_LEAGUES = ['世界杯', 'World Cup', '欧洲杯', 'Euro', '亚洲杯', 'Asian Cup',
                   '美洲杯', 'Copa America', '非洲杯', 'AFCON', '欧冠', 'Champions League',
                   '欧联', 'Europa League', '亚冠', 'AFC Champions League']

    FINAL_KEYWORDS = ['决赛', 'Final', '决赛圈', '半决赛', 'Semi', 'Quarter',
                      '八强', '四强', '半决']

    DERBY_KEYWORDS = ['德比', 'Derby', '同城', '国家德比', 'Clasico']

    # 已知德比对 (主队, 客队 任意方向匹配)
    KNOWN_DERBIES = [
        ('皇马', '巴萨'), ('巴萨', '皇马'), ('国米', 'AC米兰'), ('AC米兰', '国米'),
        ('曼联', '利物浦'), ('利物浦', '曼联'), ('阿森纳', '热刺'), ('热刺', '阿森纳'),
        ('多特', '拜仁'), ('拜仁', '多特'), ('曼城', '曼联'), ('曼联', '曼城'),
        ('尤文', '国米'), ('国米', '尤文'), ('马竞', '皇马'), ('皇马', '马竞'),
        ('巴黎', '马赛'), ('马赛', '巴黎'), ('河床', '博卡'), ('博卡', '河床'),
    ]

    TOP_LEAGUES = ['英超', 'Premier League', '西甲', 'La Liga', '意甲', 'Serie A',
                   '德甲', 'Bundesliga', '法甲', 'Ligue 1']

    def classify(self, league: str, home: str = "", away: str = "",
                 matchday: int = 1, is_final: bool = False,
                 odds_spread: float = 0.0) -> Scenario:
        """
        分类比赛场景

        优先级: 决赛 > 德比 > 强弱悬殊 > 杯赛 > 联赛
        """
        league_lower = (league or "").lower()

        # 1. 决赛/淘汰赛
        if is_final or any(k.lower() in league_lower for k in self.FINAL_KEYWORDS):
            return Scenario.FINAL

        # 2. 德比 (关键词+已知配对各占50%)
        match_text = f"{home} {away} {league}".lower()
        is_derby_kw = any(k.lower() in match_text for k in self.DERBY_KEYWORDS)
        is_derby_pair = (home, away) in self.KNOWN_DERBIES or (away, home) in self.KNOWN_DERBIES
        if is_derby_kw or is_derby_pair:
            return Scenario.DERBY

        # 3. 强弱悬殊 (赔率比>3:1 或 最强方概率>70%)
        if abs(odds_spread) > 0.35:  # spread > 0.35 ≈ 赔率比 > 3:1
            return Scenario.STRONG_FAVORITE

        # 4. 杯赛
        if any(c.lower() in league_lower for c in self.CUP_LEAGUES):
            if matchday <= 3:
                return Scenario.CUP_GROUP
            else:
                return Scenario.CUP_KNOCKOUT

        # 5. 联赛
        return Scenario.LEAGUE

    def adapt(self, home: str = "", away: str = "", league: str = "",
              odds: Dict[str, float] = None, matchday: int = 1,
              is_final: bool = False) -> ScenarioConfig:
        """
        根据比赛上下文, 输出最优场景配置

        Args:
            home, away: 球队名
            league: 联赛名
            odds: 赔率 (可选, 用于spread计算)
            matchday: 比赛轮次
            is_final: 是否为决赛
        """
        # 计算spread
        spread = 0.0
        if odds:
            oh = odds.get('home', 2.5)
            oa = odds.get('away', 2.8)
            if oa > 0:
                spread = (1/oh) - (1/oa)

        scenario = self.classify(league, home, away, matchday, is_final, spread)
        config = ScenarioConfig(scenario=scenario)

        # ═══════════════════════════════════════════════════════
        # 按场景配置参数
        # ═══════════════════════════════════════════════════════

        if scenario == Scenario.LEAGUE:
            config.description = "联赛标准模式: 数据充分, 参数稳定"
            config.draw_target_rate = 0.25
            config.home_advantage = 0.08

        elif scenario == Scenario.CUP_GROUP:
            config.description = "杯赛小组赛: 冷启动, 高平局率, 谨慎预测"
            config.d_boost = 0.375
            config.draw_target_rate = 0.375
            config.home_advantage = 0.03
            config.cold_start_mix = 0.15 if matchday == 1 else 0.05
            config.trap_threshold_mult = 1.2        # 陷阱更敏感
            config.barrier_sensitivity = 1.3
            config.confidence_mult = 0.85           # 降低置信度
            config.expert_weights['game_theory'] = 1.4  # 杜博弈↑
            config.expert_weights['imbalance'] = 1.3    # 曾均衡↑
            config.max_pred_stake = 0.5                  # 轻仓
            config.description = f"杯赛小组赛R{matchday}: D率37.5%, 陷阱敏感×1.2"

        elif scenario == Scenario.CUP_KNOCKOUT:
            config.description = "杯赛淘汰赛: 谨慎+防守心态, 平局更多"
            config.d_boost = 0.40
            config.draw_target_rate = 0.40
            config.home_advantage = 0.02            # 中立场地
            config.cold_start_mix = 0.05
            config.trap_threshold_mult = 1.3
            config.confidence_mult = 0.80
            config.expert_weights['game_theory'] = 1.5
            config.expert_weights['imbalance'] = 1.4
            config.require_cross_check = True        # 必须交叉验证
            config.extra_time_compensation = True

        elif scenario == Scenario.FINAL:
            config.description = "🏆 决赛模式: 心理因素主导, 极度保守"
            config.d_boost = 0.42
            config.draw_target_rate = 0.42
            config.home_advantage = 0.0             # 中立场地, 无主场
            config.cold_start_mix = 0.10
            config.trap_threshold_mult = 1.5        # 极度敏感
            config.barrier_sensitivity = 1.5
            config.confidence_mult = 0.70           # 大幅降低
            config.expert_weights['game_theory'] = 1.8  # 博弈主导
            config.expert_weights['imbalance'] = 1.5
            config.expert_weights['quant'] = 0.6    # 降权量化(数据少)
            config.max_pred_stake = 0.30
            config.forbid_heavy_favorite_bet = True
            config.require_cross_check = True
            config.risk_warning_level = "high"

        elif scenario == Scenario.STRONG_FAVORITE:
            config.description = "⚠️ 强弱悬殊: 热门翻车频发, 警惕杯赛陷阱"
            config.d_boost = 0.15                   # 弱队方向平局概率↑
            config.cold_start_mix = 0.05
            config.trap_threshold_mult = 0.80       # 陷阱阈值↓ (更容易触发)
            config.barrier_sensitivity = 1.4
            config.confidence_mult = 0.85
            config.expert_weights['game_theory'] = 1.6  # 博弈权重↑(检测诱盘)
            config.expert_weights['imbalance'] = 1.4
            config.forbid_heavy_favorite_bet = True
            config.require_cross_check = True
            config.risk_warning_level = "elevated"

        elif scenario == Scenario.DERBY:
            config.description = "🔥 德比模式: 主场打折, 情绪>实力"
            config.d_boost = 0.10
            config.draw_target_rate = 0.32
            config.home_advantage = 0.04            # 主场优势打折
            config.trap_threshold_mult = 1.1
            config.confidence_mult = 0.90
            config.expert_weights['imbalance'] = 1.5
            config.description = f"德比战: 主场优势×0.5, 情绪因素主导"

        return config

    # ═══════════════════════════════════════════════════════════
    # 应用场景配置到概率
    # ═══════════════════════════════════════════════════════════

    def apply_to_probs(self, h: float, d: float, a: float,
                       config: ScenarioConfig,
                       league: str = "", matchday: int = 1) -> Tuple[float, float, float]:
        """
        将场景配置应用到概率

        Args:
            h, d, a: 原始概率
            config: 场景配置
            league, matchday: 上下文

        Returns:
            (h, d, a): 调整后概率
        """
        # D-boost
        if config.d_boost > 0 and d < config.draw_target_rate:
            d_gap = config.draw_target_rate - d
            steal = min(d_gap, 0.50 - d) * 0.5
            h = max(0.02, h - steal)
            a = max(0.02, a - steal)
            d = d + steal * 2

        # 主场优势修正
        if config.home_advantage < 0.08:
            ha_diff = (0.08 - config.home_advantage) * 0.3
            h = max(0.02, h - ha_diff * 0.5)
            a = min(0.98, a + ha_diff * 0.5)

        # 冷启动混合
        if config.cold_start_mix > 0:
            alpha = config.cold_start_mix
            u = 1.0 / 3.0
            h = h * (1 - alpha) + u * alpha
            d = d * (1 - alpha) + u * alpha
            a = a * (1 - alpha) + u * alpha

        # 归一化
        total = h + d + a
        if total > 0:
            h, d, a = h/total, d/total, a/total

        return h, d, a

    # ═══════════════════════════════════════════════════════════
    # 格式化输出
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def format_for_report(config: ScenarioConfig) -> str:
        """格式化为6层报告片段"""
        icons = {
            Scenario.LEAGUE: "🏟️",
            Scenario.CUP_GROUP: "🏆",
            Scenario.CUP_KNOCKOUT: "⚔️",
            Scenario.FINAL: "👑",
            Scenario.STRONG_FAVORITE: "⚠️",
            Scenario.DERBY: "🔥",
        }
        icon = icons.get(config.scenario, "⚙️")

        lines = []
        lines.append(f"\n{'─' * 40}")
        lines.append(f"🎛️ 场景适配 (Scenario Engine)")
        lines.append(f"  {icon} 场景: {config.scenario.value} | {config.description[:60]}")

        # 参数快照
        params = []
        if config.d_boost > 0:
            params.append(f"D-target={config.draw_target_rate:.0%}")
        if config.cold_start_mix > 0:
            params.append(f"冷启动={config.cold_start_mix:.0%}")
        if config.trap_threshold_mult != 1.0:
            params.append(f"陷阱×{config.trap_threshold_mult:.1f}")
        if config.confidence_mult < 1.0:
            params.append(f"置信×{config.confidence_mult:.2f}")
        if params:
            lines.append(f"  📐 参数: {' | '.join(params)}")

        # 专家权重
        experts = config.expert_weights
        boosted = {k: v for k, v in experts.items() if v > 1.2}
        reduced = {k: v for k, v in experts.items() if v < 0.8}
        if boosted or reduced:
            ew_parts = []
            for k, v in boosted.items():
                names = {'quant': '季泊松', 'game_theory': '杜博弈', 'imbalance': '曾均衡', 'ensemble': '荣合众'}
                ew_parts.append(f"{names.get(k,k)}↑×{v:.1f}")
            for k, v in reduced.items():
                names = {'quant': '季泊松', 'game_theory': '杜博弈', 'imbalance': '曾均衡', 'ensemble': '荣合众'}
                ew_parts.append(f"{names.get(k,k)}↓×{v:.1f}")
            lines.append(f"  👥 专家权重: {', '.join(ew_parts)}")

        # 特殊规则
        rules = []
        if config.forbid_heavy_favorite_bet:
            rules.append("🚫 禁止重注热门")
        if config.require_cross_check:
            rules.append("🔍 必须交叉验证")
        if config.extra_time_compensation:
            rules.append("⏱️ 加时赛补偿")
        if rules:
            lines.append(f"  📋 特殊规则: {' | '.join(rules)}")

        if config.risk_warning_level != "normal":
            lines.append(f"  ⚠️ 风险级别: {config.risk_warning_level.upper()}")

        return "\n".join(lines)

# ═══════════════════════════════════════════════════════════════
# 3. 单例
# ═══════════════════════════════════════════════════════════════

_engine_instance: Optional[ScenarioEngine] = None

def get_scenario_engine() -> ScenarioEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = ScenarioEngine()
    return _engine_instance

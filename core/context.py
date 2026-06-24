"""
哨响AI v4.1 — 统一上下文对象 (MatchContext)
===============================================
全链路唯一数据载体: 所有层只读写这个对象, 不做跨层直接调用。

这是P0核心必做项 — 改完这个, 后面加任何新功能都不怕碰老代码。

用法:
  ctx = MatchContext(user_input="巴西vs阿根廷谁赢", home="巴西", away="阿根廷")
  ctx.set_odds(2.10, 3.30, 3.60)
  # L2写入路由结果
  ctx.set_intent("predict", "match_result", 0.85)
  # L4写入预测
  ctx.set_prediction(0.45, 0.28, 0.27)
  # 任何层读
  print(ctx.odds_1x2)

作者: Architecture v4.0 · P0 Phase
日期: 2026-06-19
"""
from __future__ import annotations
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field


@dataclass
class MatchContext:
    """
    全局统一上下文 — 6层架构唯一数据载体

    分区:
      input   — L1写入, 下层只读
      routing  — L2写入, L3-L6读取
      analysis — L3-L4读写, 中间结果
      output   — L5写入, 对外输出
      trace    — 全链路追加, 调试/日志用
    """

    # ═══════════════════════════════════════════════════════════
    # 输入区 (L1写入, 下层只读)
    # ═══════════════════════════════════════════════════════════
    user_input: str = ""
    home_team: str = ""
    away_team: str = ""
    league: str = ""
    matchday: int = 1
    is_final: bool = False

    # 原始赔率
    odds_1x2: Dict[str, float] = field(default_factory=dict)   # {home, draw, away}
    odds_open: Dict[str, float] = field(default_factory=dict)   # 开盘赔率
    odds_ah: Dict[str, float] = field(default_factory=dict)     # 让球赔率
    odds_ou: Dict[str, float] = field(default_factory=dict)     # 大小球赔率

    # OCR/截图来源
    input_source: str = "text"      # text / image_ocr / image_upload

    # ═══════════════════════════════════════════════════════════
    # 路由区 (L2写入, L3-L6读取)
    # ═══════════════════════════════════════════════════════════
    intent_category: str = ""        # predict / analyze / explain / backtest
    intent_subtype: str = ""         # match_result / odds / bookmaker_intent / ...
    intent_confidence: float = 0.0

    collaboration_mode: str = ""     # A / B / C / D
    experts_activated: List[str] = field(default_factory=list)
    scenario: str = ""              # league / cup_group / final / derby / ...

    # ═══════════════════════════════════════════════════════════
    # 分析区 (L3-L4读写)
    # ═══════════════════════════════════════════════════════════

    # 预测概率
    h_prob: float = 0.0
    d_prob: float = 0.0
    a_prob: float = 0.0
    d_gate_result: str = ""

    # 三线预测
    unified_pred: Dict = field(default_factory=dict)    # {h, d, a, pred}
    sky_pred: Dict = field(default_factory=dict)
    vip_pred: Dict = field(default_factory=dict)

    # 比分
    score_predictions: List[Dict] = field(default_factory=list)

    # 专家分析结果
    expert_insights: List[str] = field(default_factory=list)

    # 赔率分析
    odds_analysis: Dict = field(default_factory=dict)
    trap_detection: Dict = field(default_factory=dict)
    barrier_report: Dict = field(default_factory=dict)
    cross_opponent: Dict = field(default_factory=dict)
    balance_sim: Dict = field(default_factory=dict)
    scorer_compare: Dict = field(default_factory=dict)

    # L0知识注入
    knowledge_context: Dict = field(default_factory=dict)

    # 场景配置
    scenario_config: Dict = field(default_factory=dict)

    # ═══════════════════════════════════════════════════════════
    # 输出区 (L5写入)
    # ═══════════════════════════════════════════════════════════
    final_prediction: str = ""       # H / D / A
    confidence_level: str = ""      # high / medium / low
    risk_tags: List[str] = field(default_factory=list)
    recommendation: str = ""
    analysis_report: str = ""

    # ═══════════════════════════════════════════════════════════
    # 追踪区 (全链路追加)
    # ═══════════════════════════════════════════════════════════
    trace_log: List[Dict] = field(default_factory=list)
    start_time: float = 0.0
    layer_timings: Dict[str, float] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    fallback_triggered: bool = False
    degradation_level: int = 1      # 1=v4.1 2=v3.2 3=odds 4=uniform
    pipeline_version: str = "v4.1"

    # ═══════════════════════════════════════════════════════════
    # 便捷方法
    # ═══════════════════════════════════════════════════════════

    def set_odds(self, home: float, draw: float, away: float):
        self.odds_1x2 = {'home': home, 'draw': draw, 'away': away}

    def set_intent(self, category: str, subtype: str, confidence: float):
        self.intent_category = category
        self.intent_subtype = subtype
        self.intent_confidence = confidence

    def set_prediction(self, h: float, d: float, a: float):
        self.h_prob = h
        self.d_prob = d
        self.a_prob = a

    def set_final(self, prediction: str, confidence: str, tags: List[str] = None):
        self.final_prediction = prediction
        self.confidence_level = confidence
        if tags:
            self.risk_tags = tags

    def add_trace(self, layer: str, action: str, detail: str = ""):
        self.trace_log.append({
            'layer': layer,
            'action': action,
            'detail': detail,
            'elapsed_ms': (time.perf_counter() - self.start_time) * 1000,
        })

    def record_layer_time(self, layer: str):
        elapsed = (time.perf_counter() - self.start_time) * 1000
        self.layer_timings[layer] = elapsed

    def is_cup(self) -> bool:
        cup_keywords = ['世界杯', 'World Cup', '欧洲杯', 'Euro', '亚洲杯', 'Asian Cup',
                        '美洲杯', 'Copa', '非洲杯', 'AFCON', '欧冠', 'Champions League']
        return any(k.lower() in (self.league or '').lower() for k in cup_keywords)

    def has_odds(self) -> bool:
        return all(self.odds_1x2.get(k, 0) > 1.0 for k in ['home', 'draw', 'away'])

    def top_prediction(self) -> str:
        """返回当前最高概率的方向"""
        return max([('H', self.h_prob), ('D', self.d_prob), ('A', self.a_prob)],
                   key=lambda x: x[1])[0]

    def summary(self) -> str:
        """单行摘要 — 调试用"""
        return (f"[{self.home_team}vs{self.away_team}] "
                f"intent={self.intent_category} mode={self.collaboration_mode} "
                f"P(H)={self.h_prob:.0%} P(D)={self.d_prob:.0%} P(A)={self.a_prob:.0%} "
                f"degrad={self.degradation_level}")


# ═══════════════════════════════════════════════════════════════
# 辅助: 从现有 SixLayerResult 迁移到 MatchContext
# ═══════════════════════════════════════════════════════════════

def from_sixlayer_result(result) -> MatchContext:
    """兼容过渡: 从旧的 SixLayerResult 创建 MatchContext"""
    ctx = MatchContext()
    ctx.user_input = result.user_input
    ctx.intent_category = result.intent_category
    ctx.intent_subtype = result.intent_subtype
    ctx.intent_confidence = result.intent_confidence
    ctx.collaboration_mode = result.collaboration_mode
    ctx.experts_activated = result.experts_activated
    ctx.h_prob = result.h_prob
    ctx.d_prob = result.d_prob
    ctx.a_prob = result.a_prob
    ctx.d_gate_result = result.d_gate_result or ""
    ctx.expert_insights = result.expert_insights
    ctx.analysis_report = result.analysis_report
    ctx.fallback_triggered = result.fallback_triggered
    ctx.errors = result.errors
    return ctx

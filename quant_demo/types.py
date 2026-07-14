# -*- coding: utf-8 -*-
"""量化模拟系统 · 演示用类型定义 (不依赖 DB / 模型).

说明: 本包是「类股票量化交易系统」的演示骨架.
- 行情/赛果全部合成生成 (synthetic), 不读 football_data.db.
- 策略数学复用现有 SSoT 纯函数: compute_value_layer / bet_core / reverse_odds_engine.
- 绝不 import pipeline.engine / wc_engine / league_engine (那会加载模型/连DB).
"""
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from enum import Enum


class Decision(str, Enum):
    BET = "BET"
    PASS = "PASS"
    SCAN = "SCAN"


@dataclass
class SyntheticMatch:
    """一场合成比赛 (含多庄赔率 + 模拟赛果)."""
    mid: str
    home: str
    away: str
    league: str
    books: List[Dict[str, float]]          # 多庄 [{"h":, "d":, "a":}] (十进制赔率)
    best_odds: List[float]                  # [h, d, a] 跨庄最优
    consensus_prob: List[float]             # [h, d, a] 共识隐含概率(含抽水)
    scenarios: List[str] = field(default_factory=list)   # 注入的演示场景标签
    winner: str = "D"                        # 模拟赛果 H/D/A


@dataclass
class StrategySignal:
    """单个策略对单场比赛的信号. 复用 compute_value_layer 产物."""
    strategy_id: str
    strategy_name: str
    decision: str                            # BET / PASS / SCAN
    direction: Optional[str] = None          # H / D / A
    best_odds: Optional[float] = None
    edge_pct: Optional[float] = None
    ev_pct: Optional[float] = None
    kelly_half: Optional[float] = None
    note: str = ""


@dataclass
class PendingOrder:
    """待确认订单 (执行层核心): 自动算注, 真实下单前需手动确认."""
    oid: str
    mid: str
    home: str
    away: str
    strategy_id: str
    strategy_name: str
    direction: str                           # H / D / A
    odds: float
    stake: float                             # 已用规范半凯利算好
    equity_before: float
    mode: str = "sim"                        # sim=模拟盘自动结算; live=真实需确认
    confidence: float = 0.0
    created_at: str = ""


@dataclass
class Position:
    """已结算持仓 (组合层)."""
    oid: str
    mid: str
    home: str
    away: str
    strategy_id: str
    direction: str
    odds: float
    stake: float
    win: bool
    pnl: float
    equity_after: float


@dataclass
class StrategyMeta:
    """策略元数据 (用于终端开关/权重)."""
    id: str
    name: str
    desc: str
    enabled: bool = True
    weight: float = 1.0

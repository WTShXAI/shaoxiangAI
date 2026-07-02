"""Full Linkage Predictor — 拆分子模块"""
import os, sys, json, math
import logging
from typing import Dict, List, Tuple, Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

from ._compat import np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

@dataclass
class MatchInput:
    """比赛原始输入"""
    home: str
    away: str
    odds_h: float
    odds_d: float
    odds_a: float
    hcp: float           # 让球 (-1=主让1球, +0.5=主受让0.5) — 外围初盘
    ou_line: float       # 大小球盘口 (2.0, 2.25, 2.5, 2.75, 3.0)
    over_water: float = 1.90
    under_water: float = 1.92
    matchday: int = 3
    r3_rotation: bool = False  # R3轮换信号
    stage: str = 'group'       # 比赛阶段: group/knockout/final
    # Chain -1 阵容信息 (从首发分析获得)
    home_formation: str = ''       # 主队阵型 (如 '4-1-2-3')
    away_formation: str = ''       # 客队阵型
    home_full_strength: bool = True  # 主队是否全主力
    away_full_strength: bool = True  # 客队是否全主力
    home_missing_stars: str = ''     # 主队缺阵球星 (如 '哈兰德,厄德高')
    away_missing_stars: str = ''     # 客队缺阵球星
    sporttery_hcp: float = 0.0       # 竞彩让球 (0=无竞彩数据, 非零=竞彩实盘)

    @property
    def hcp_depth(self) -> float:
        """让球深度 (优先竞彩, 回退外围)"""
        if self.sporttery_hcp:
            return abs(self.sporttery_hcp)
        return abs(self.hcp)

    @property
    def hcp_direction(self) -> str:
        """让球方向"""
        if self.hcp < 0:
            return '主让'
        elif self.hcp > 0:
            return '客让'
        return '平手'

    @classmethod
    def from_odds_snapshot(cls, home: str, away: str,
                           odds_1x2: str, hcp_str: str, ou_str: str,
                           ou_odds: str = "1.90/1.92",
                           r3: bool = False) -> 'MatchInput':
        """从截图格式快速构造"""
        oh, od, oa = map(float, odds_1x2.split(','))
        hcp = float(hcp_str)
        ou_line = float(ou_str)
        over_w, under_w = map(float, ou_odds.split('/'))
        return cls(
            home=home, away=away,
            odds_h=oh, odds_d=od, odds_a=oa,
            hcp=hcp, ou_line=ou_line,
            over_water=over_w, under_water=under_w,
            r3_rotation=r3
        )

@dataclass
class ChainResult:
    """单链输出"""
    chain_name: str
    verdict: str           # H/A/D
    draw_prob: float
    confidence: float
    signals: List[str]
    metadata: Dict = field(default_factory=dict)

class RiskTag(str):
    """统一风险标签 — 哨响AI v5.2 标准枚举"""
    CLEAN = 'clean'
    IGNORE_DRAW = 'ignore_draw'           # 陷阱信号: 庄家诱平
    WEAK_IGNORE_DRAW = 'weak_ignore_draw'  # 弱陷阱信号
    FAVOR_DRAW = 'favor_draw'              # 倾向平局
    WEAK_DRAW = 'weak_draw'                # 弱平局信号
    UPSET_WARNING = 'upset_warning'         # 冷门预警(超热门翻车)
    DRAW_ALERT = 'draw_alert'              # 平局警报
    NEUTRAL = 'neutral'                    # 中性

    @classmethod
    def normalize(cls, tag):
        """标准化risk_tag: 合并历史3态和新5态"""
        if not tag:
            return cls.NEUTRAL
        if 'ignore_draw' in tag:
            return cls.IGNORE_DRAW if tag == 'ignore_draw' else tag
        if tag in ('clean',):
            return cls.NEUTRAL
        return tag


# ════════════════════════════════════════════════════
# Layer 1: OU联动推理引擎 (核心)
# ════════════════════════════════════════════════════

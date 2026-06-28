"""
PredictorBase — 预测器统一抽象接口 (2026-06-28)
==============================================
所有预测器实现统一的 predict(match) 接口, 消除三条管线接口差异.

用法:
    from predictors.base import PredictorBase, MatchData, PredictionResult
    predictor: PredictorBase = UnifiedPredictor()  # 或 SKY / VIP
    result = predictor.predict_match(match)

已适配:
  - UnifiedPredictor  → 已实现 predict_match()
  - SKYPredictor      → 已实现 predict_match()
  - VIPFinalPredictor → 已实现 predict_match()
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any

@dataclass
class MatchData:
    """标准化的比赛输入数据"""
    home: str                        # 主队名称
    away: str                        # 客队名称
    odds_h: float = 0.0              # 主胜赔率
    odds_d: float = 0.0              # 平局赔率
    odds_a: float = 0.0              # 客胜赔率
    handicap: float = 0.0            # 让球盘口 (正=客让)
    ou_line: float = 2.5             # 大小球盘口
    over_water: float = 1.90         # 大球水位
    under_water: float = 1.92        # 小球水位
    open_h: float = 0.0              # 主胜开盘赔率 (0=未知)
    open_d: float = 0.0              # 平局开盘赔率
    open_a: float = 0.0              # 客胜开盘赔率
    league: str = "unknown"          # 联赛/赛事名称
    match_type: str = "tournament"   # tournament / league
    elo_home: Optional[float] = None # 主队 ELO
    elo_away: Optional[float] = None # 客队 ELO
    extra: Dict[str, Any] = field(default_factory=dict)  # 扩展字段

    @property
    def odds_dict(self) -> Dict[str, float]:
        return {'H': self.odds_h, 'D': self.odds_d, 'A': self.odds_a}

    @classmethod
    def from_match_dict(cls, d: dict) -> 'MatchData':
        """从任意 dict 构建 (兼容 VIP 的 match 参数)"""
        return cls(
            home=d.get('home', ''),
            away=d.get('away', ''),
            odds_h=d.get('odds_h', d.get('home', 0.0)),
            odds_d=d.get('odds_d', d.get('draw', 0.0)),
            odds_a=d.get('odds_a', d.get('away', 0.0)),
            handicap=d.get('handicap', d.get('asian_handicap', 0.0)),
            ou_line=d.get('ou_line', d.get('ou', 2.5)),
            over_water=d.get('over_water', 1.90),
            under_water=d.get('under_water', 1.92),
            open_h=d.get('open_h', 0.0),
            open_d=d.get('open_d', 0.0),
            open_a=d.get('open_a', 0.0),
            league=d.get('league', 'unknown'),
            match_type=d.get('match_type', 'tournament'),
            elo_home=d.get('elo_home'),
            elo_away=d.get('elo_away'),
            extra={k: v for k, v in d.items()
                   if k not in ('home','away','odds_h','odds_d','odds_a',
                                'handicap','ou_line','league','match_type')},
        )

@dataclass
class PredictionResult:
    """标准化的预测输出"""
    probabilities: Dict[str, float]   # {'H': 0.45, 'D': 0.28, 'A': 0.27}
    prediction: str                   # 'H' / 'D' / 'A'
    confidence: float = 0.0           # 置信度 (0~1)
    model_version: str = "unknown"    # 模型版本
    # 可选扩展字段
    expected_goals: Optional[Dict[str, float]] = None  # {'home': 1.5, 'away': 0.8, 'total': 2.3}
    scores: Optional[List[Dict]] = None                 # 比分预测排名
    trap_score: float = 0.0
    draw_signal: float = 0.0
    risk_tag: str = "neutral"
    dgate_mode: str = "none"
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def verdict_cn(self) -> str:
        return {'H': '主胜', 'D': '平局', 'A': '客胜'}.get(self.prediction, '?')

class PredictorBase(ABC):
    """预测器统一基类"""

    @abstractmethod
    def predict_match(self, match: MatchData) -> PredictionResult:
        """统一预测入口 — 子类必须实现"""
        ...

    @abstractmethod
    def model_version(self) -> str:
        """返回模型版本标识"""
        ...

    @abstractmethod
    def is_loaded(self) -> bool:
        """检查模型是否已加载"""
        ...

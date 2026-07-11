"""
哨响AI v7.1 — 预测引擎抽象层
=============================
按赛事类型分离:
  - wc: 世界杯引擎 (淘汰赛平局率高/小样本/屠杀/生死战/轮换)
  - league: 五大联赛引擎 (大样本/主场优势/联赛动机/高市场信任)

用法:
  from pipeline.engine import create_engine
  engine = create_engine("wc")       # 世界杯
  engine = create_engine("league")   # 五大联赛
  result = engine.predict(match)
"""

import os
import dataclasses
from abc import ABC, abstractmethod
from typing import Optional


def apply_softline_to_result(result, softline):
    """预测层第7步 (G10): 将 bridge 层已算出的跨庄 soft-line 调整回灌预测层。

    护栏解耦: 仅当调用方传入 softline 且 disagreement_detected=True 才覆盖 prediction
    (调用方据 ENABLE_SOFTLINE_DECISION 决定传不传; 护栏OFF则不传 -> 完全等价现状)。
    一致场 / 无 softline -> 完全不改 result, 保持规则/市场 argmax 兜底。
    返回新对象 (dataclasses.replace), 不突变入参。
    """
    if not softline or not softline.get("disagreement_detected"):
        return result
    probs = softline.get("softline_adjusted_probs")
    if not probs or len(probs) != 3:
        return result
    p_h, p_d, p_a = float(probs[0]), float(probs[1]), float(probs[2])
    # 软-line argmax (分歧场用它覆盖规则/市场 argmax); 平局优先序与 parse_odds 一致
    if p_h >= p_d and p_h >= p_a:
        new_pred = "H"
    elif p_d >= p_a:
        new_pred = "D"
    else:
        new_pred = "A"
    try:
        new = dataclasses.replace(result, prediction=new_pred)
    except TypeError:
        new = result  # 非 dataclass 不强制覆盖, 保护现状
    # 附加 soft-line 元数据 (动态属性, 下游可选消费)
    try:
        new.softline_applied = True
        new.softline_adjusted_probs = (p_h, p_d, p_a)
        new.softline_disagreement = True
    except Exception:
        pass
    return new


class PredictionEngine(ABC):
    """预测引擎抽象基类"""

    name: str = "base"
    version: str = "7.1.0"
    competition: str = "unknown"

    @abstractmethod
    def predict(self, match, softline=None):
        ...

    @property
    def loaded(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return f"{self.name} v{self.version} ({self.competition})"

    @property
    def features(self) -> dict:
        return {}


class WCEngine(PredictionEngine):
    """世界杯/杯赛专用引擎 — 淘汰赛逻辑/小样本保守/屠杀预警"""

    name = "wc_engine"
    competition = "世界杯/杯赛"

    def __init__(self):
        try:
            from pipeline.wc_engine import predict as _predict, MatchInput as _V7Match
            self._predict = _predict
            self._V7Match = _V7Match
            self._loaded = True
        except Exception as e:
            self._loaded = False
            self._error = str(e)

    @property
    def loaded(self) -> bool:
        return self._loaded

    def predict(self, match, softline=None):
        if not self._loaded:
            raise RuntimeError(f"WC引擎加载失败: {self._error}")
        if not hasattr(match, 'r3_rotation'):
            match = self._V7Match(
                home=match.home, away=match.away,
                odds_h=match.odds_h, odds_d=match.odds_d, odds_a=match.odds_a,
                hcp=match.hcp, ou_line=match.ou_line,
                stage=match.stage, matchday=getattr(match, 'matchday', 3),
                r3_rotation=getattr(match, 'r3_rotation', False),
            )
        result = self._predict(match, mode="optimized")
        return apply_softline_to_result(result, softline)

    @property
    def description(self) -> str:
        return "哨响AI v7.1 WC引擎 (淘汰赛平局+屠杀预警+小样本保守+R3轮换)"

    @property
    def features(self) -> dict:
        return {
            "draw_expert": True,
            "competition": "tournament",
            "survival_clash": True,
            "massacre_detect": True,
            "dead_rubber_degrade": True,
            "r3_rotation": True,
        }


class LeagueEngine(PredictionEngine):
    """五大联赛专用引擎 — 大样本/主场优势/联赛动机/高市场信任"""

    name = "league_engine"
    competition = "五大联赛"

    def __init__(self):
        try:
            from pipeline.league_engine import predict as _predict, MatchInput as _V7Match
            self._predict = _predict
            self._V7Match = _V7Match
            self._loaded = True
        except Exception as e:
            self._loaded = False
            self._error = str(e)

    @property
    def loaded(self) -> bool:
        return self._loaded

    def predict(self, match, softline=None):
        if not self._loaded:
            raise RuntimeError(f"League引擎加载失败: {self._error}")
        if not hasattr(match, 'r3_rotation'):
            match = self._V7Match(
                home=match.home, away=match.away,
                odds_h=match.odds_h, odds_d=match.odds_d, odds_a=match.odds_a,
                hcp=match.hcp, ou_line=match.ou_line,
                stage=match.stage, matchday=getattr(match, 'matchday', 3),
                r3_rotation=getattr(match, 'r3_rotation', False),
            )
        result = self._predict(match)
        return apply_softline_to_result(result, softline)

    @property
    def description(self) -> str:
        return "哨响AI v7.1 联赛引擎 (大样本+主场优势+联赛动机+高市场信任)"

    @property
    def features(self) -> dict:
        return {
            "draw_expert": False,
            "competition": "league",
            "home_advantage": True,
            "market_trust": "high",
            "motivation_analysis": True,
        }


# ═══ 引擎注册表 ═══
_ENGINE_REGISTRY = {
    "wc": WCEngine,
    "league": LeagueEngine,
}


def create_engine(name: Optional[str] = None) -> PredictionEngine:
    """工厂方法

    Args:
        name: "wc" (世界杯) | "league" (五大联赛).
              默认: 环境变量 ENGINE 或 "wc"
    """
    if name is None:
        name = os.getenv("ENGINE", "wc")

    if name not in _ENGINE_REGISTRY:
        available = ", ".join(_ENGINE_REGISTRY.keys())
        raise ValueError(f"未知引擎 '{name}', 可用: {available}")

    engine = _ENGINE_REGISTRY[name]()
    if not engine.loaded:
        raise RuntimeError(f"引擎 '{name}' 加载失败")
    return engine

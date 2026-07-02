"""
哨响AI — 专家代理模块 (Base + 10 Legacy Agents)
==============================================
2026-07-01: 从 ExpertHub legacy_map 提取, 统一注册入口
"""
import logging
logger = logging.getLogger(__name__)


class BaseAgent:
    """旧版专家代理基类 (ExpertAdapter 兼容)"""
    def __init__(self, name="base"):
        self.name = name

    def predict(self, context: dict) -> dict:
        return {"prediction": "home", "confidence": 0.5}

    def train(self, data):
        pass

    def evaluate(self, data) -> dict:
        return {"accuracy": 0.5}


class TrendAgent(BaseAgent):
    """趋势分析专家"""
    def __init__(self): super().__init__("trend_analyzer")

class AlphaAgent(BaseAgent):
    """Alpha决策专家"""
    def __init__(self): super().__init__("alpha_decision")

class RefereeAgent(BaseAgent):
    """裁判分析专家"""
    def __init__(self): super().__init__("referee_model")

class UpsetAgent(BaseAgent):
    """冷门检测专家"""
    def __init__(self): super().__init__("upset_detector")

class MediaAgent(BaseAgent):
    """媒体情报专家"""
    def __init__(self): super().__init__("media_intelligence")

class CoachAgent(BaseAgent):
    """教练战术专家"""
    def __init__(self): super().__init__("coach_tactics")

class QuantAgent(BaseAgent):
    """量化交易专家"""
    def __init__(self): super().__init__("quant_trader")

class TimeSpaceAgent(BaseAgent):
    """时空检测专家"""
    def __init__(self): super().__init__("timespace_detector")

class ArbitrageAgent(BaseAgent):
    """套利检测专家"""
    def __init__(self): super().__init__("arbitrage_detector")

class GoalTimingAgent(BaseAgent):
    """进球时序专家"""
    def __init__(self): super().__init__("goal_timing")

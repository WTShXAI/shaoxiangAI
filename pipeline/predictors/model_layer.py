"""Full Linkage Predictor — 拆分子模块"""
import os, sys, json, math
import logging
from typing import Dict, List, Tuple, Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None
    _HAS_NUMPY = False
    class _FakeArray:
        def __init__(self, data):
            self.data = list(data)
        def copy(self): return _FakeArray(self.data)
        def __iter__(self): return iter(self.data)
        def __getitem__(self, i): return self.data[i]
        def __len__(self): return len(self.data)
        def sum(self): return sum(self.data)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from pipeline.predictors.data_classes import *  # noqa: F401, F403

class ModelLayer:
    """UnifiedPredictor v4.1 模型推理适配器"""

    @staticmethod
    def assess(match: MatchInput) -> ChainResult:
        """运行 v4.1 Stacking 模型推理"""
        signals = []
        try:
            from predictors.unified_predictor import UnifiedPredictor
            up = UnifiedPredictor()
            result = up.predict(
                home=match.home, away=match.away,
                odds_h=match.odds_h, odds_d=match.odds_d, odds_a=match.odds_a,
                asian_handicap=match.hcp,
                ou_line=match.ou_line,
                over_water=match.over_water,
                under_water=match.under_water,
            )

            probs = result.get('probabilities', {})
            draw_prob = probs.get('D', probs.get('draw', 0.0))
            trap = result.get('trap_level', 'none')
            raw_verdict = result.get('prediction', '?')

            if trap != 'none':
                signals.append(f'陷阱:{trap}({result.get("trap_type","?")})')

            return ChainResult(
                chain_name='UnifiedPredictor v4.1',
                verdict=raw_verdict,
                draw_prob=float(draw_prob),
                confidence=float(result.get('confidence', 0.5)),
                signals=signals,
                metadata={
                    'probs': {k: float(v) for k, v in probs.items()} if isinstance(probs, dict) else {},
                    'lambda_info': result.get('lambda_info', {}),
                    'trap_level': trap,
                }
            )
        except Exception as e:
            imp_sum = 1/match.odds_h + 1/match.odds_d + 1/match.odds_a
            draw_imp = 1/(match.odds_d * imp_sum)
            return ChainResult(
                chain_name='UnifiedPredictor v4.1',
                verdict='?',
                draw_prob=draw_imp,
                confidence=0.3,
                signals=[f'MODEL_ERR:{e}'],
            )

# ════════════════════════════════════════════════════
# Layer 3.5: 临场升盘信号层 (Live Movement Signal)
# ════════════════════════════════════════════════════

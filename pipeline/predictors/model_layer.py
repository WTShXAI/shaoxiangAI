"""Full Linkage Predictor — 拆分子模块"""
import os, sys, json, math
import logging
from typing import Dict, List, Tuple, Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

from ._compat import np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from pipeline.predictors.data_classes import *  # noqa: F401, F403

class ModelLayer:
    """UnifiedPredictor v4.1 模型推理适配器"""

    _predictor_cache = None  # 类级缓存: 复用 UnifiedPredictor 实例 (v6.0)

    @classmethod
    def _get_predictor(cls):
        """延迟加载+缓存 UnifiedPredictor, 避免每次 assess() 重建"""
        if cls._predictor_cache is None:
            from pipeline.predictors.unified_predictor import UnifiedPredictor
            cls._predictor_cache = UnifiedPredictor()
        return cls._predictor_cache

    @classmethod
    def assess(cls, match: MatchInput) -> ChainResult:
        """运行 v4.1 Stacking 模型推理"""
        signals = []
        try:
            up = cls._get_predictor()
            # 注(2026-07-30 修C1): 原调用传 asian_handicap/ou_line/over_water/under_water
            #   四个 UnifiedPredictor.predict 不接受的关键字参数 → TypeError 被 except 捕获,
            #   永远返回 MODEL_ERR 兜底(verdict='?'), 生产链路 v7.4 盘口锚定模型从未执行.
            #   UnifiedPredictor 的盘口锚定走 1X2 去水(op)+可选开盘价(open_*)+跨庄(odds2_*)，
            #   不需要亚盘/大小球水位参数, 故删除这 4 个非法 kwarg, 让核心模型真正跑通.
            result = up.predict(
                home=match.home, away=match.away,
                odds_h=match.odds_h, odds_d=match.odds_d, odds_a=match.odds_a,
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

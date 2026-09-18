"""
pipeline/odds_math.py — 赔率数学原语 SSoT (2026-09-18 devig 收敛第一步)
========================================================================
背景: 全仓曾散落 ~59 处 de-vig/去水平行实现 (审计见 docs/prediction_refactor_checklist.md)。
本模块收敛三族原语, 各历史入口 (score_model.deoverround 等) 保留签名、内部委托至此:

  devig_n(odds)            N 元比例法去水 → List[float] (和=1)
  devig2(o1, o2)           2 项 (OU 大小 / AH 主客)
  devig3(oh, od, oa)       3 项 1X2 → Tuple
  devig_power(odds, tol)   迭代 power 法去水 (自 multibook_consensus 迁入)
  devig_flb(h, d, a, γ)    FLB 偏差修正去水 (自 flb_adjust 迁入约定, 实现仍以 flb_adjust 为准)

纯函数, 零依赖 pipeline 其他模块 (防循环 import)。
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple


def _valid(odds: Sequence[float]) -> Optional[List[float]]:
    vals: List[float] = []
    for o in odds:
        try:
            v = float(o)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(v) or v <= 1.0:
            return None
        vals.append(v)
    return vals


def devig_n(odds: Sequence[float]) -> Optional[List[float]]:
    """N 元比例法去水: p_i = (1/o_i) / Σ(1/o_j)。任一赔率非法返回 None。"""
    vals = _valid(odds)
    if vals is None:
        return None
    inv = [1.0 / v for v in vals]
    s = sum(inv)
    if s <= 0:
        return None
    return [x / s for x in inv]


def devig2(o1: float, o2: float) -> Optional[Tuple[float, float]]:
    """2 项去水 (OU 大/小, AH 主/客)。非法返回 None。"""
    p = devig_n([o1, o2])
    return (p[0], p[1]) if p is not None else None


def devig3(oh: float, od: float, oa: float) -> Optional[Tuple[float, float, float]]:
    """1X2 三项比例法去水。非法返回 None。"""
    p = devig_n([oh, od, oa])
    return (p[0], p[1], p[2]) if p is not None else None


def devig_power(odds: Sequence[float], tol: float = 1e-9) -> Optional[List[float]]:
    """幂法去水: 找 k 使 sum(p_i**k)=1。比比例法更抑 FLB。失败回退比例法。
    自 pipeline/multibook_consensus.devig_power 原样迁入 (改编自 rrclaw/worldcup-predictor, MIT)。"""
    vals = _valid(odds)
    if vals is None:
        return None
    raw = [1.0 / v for v in vals]
    lo, hi = 0.5, 2.0
    k = 1.0
    for _ in range(60):
        k = 0.5 * (lo + hi)
        s = sum(x ** k for x in raw)
        if abs(s - 1) < tol:
            break
        if s > 1:
            lo = k
        else:
            hi = k
    out = [x ** k for x in raw]
    s = sum(out)
    out = [x / s for x in out]
    return out if all(math.isfinite(x) for x in out) else devig_n(odds)


def overround(odds: Sequence[float]) -> float:
    """抽水 = Σ(1/o) - 1。非法赔率按 0 处理。"""
    vals = _valid(odds)
    if vals is None:
        return 0.0
    return sum(1.0 / v for v in vals) - 1.0

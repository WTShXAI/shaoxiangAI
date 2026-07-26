"""
pipeline/deep_report.py
========================
深度决策报告核心模块（L0 价值层）。从足球AI预测结果推导"该不该下、下多少、预期赚多少"。

核心函数：
  - poisson_hda(lam_h, lam_a)          由 OIP 期望进球推导 1X2 概率
  - kelly_fraction(p, odds)           凯利注码比
  - compute_value_layer(...)          价值层主函数：edge / EV / 凯利 / 情景PnL / 决策

设计要点：
  - 模型概率来自 OIP 比分矩阵边缘（bridge_service 内由 predict_score 的 M 推导），
    或独立由 lambda 推导；市场概率来自赔率去抽水。
  - edge = 模型概率 − 市场隐含概率；EV = 模型概率×赔率 − 1；凯利 = (p·odds−1)/(odds−1)。
  - ⚠️ compute_value_layer 的 stake_unit 仅供展示分析，实际下注必须走 scripts/bet_core.py
    的 safe_stake (含 MAX_STAKE_FRAC=10% 封顶 + 分歧闸门守卫)。
  - 所有结论可复现、纯标准库，无外部依赖。
"""
from __future__ import annotations
import math
from typing import Dict, Any, List, Optional, Tuple

# ── 价值层单一事实源: pipeline.compute_value_layer (SSoT) ──
# 以下符号统一从 compute_value_layer 导入, 杜绝 deep_report 内部平行副本漂移:
#   _MAX_STAKE_FRAC / kelly_fraction / _capped_stake / market_implied /
#   compute_value_layer / compute_submarket_value
# 注码/凯利最终仍走 scripts.bet_core (SSoT, 含 10% 封顶 + 分歧闸门 + 审计).
from pipeline.compute_value_layer import (
    _MAX_STAKE_FRAC,
    kelly_fraction,
    _capped_stake,
    market_implied,
    compute_value_layer,
    compute_submarket_value,
    compute_ou_value,
    draw_consensus_value,
    correct_score_value,
)

# ou_value 现为 SSoT compute_ou_value 的别名, 保持 bridge_service / backfill_bet_records 兼容性
ou_value = compute_ou_value


def poisson_hda(lam_h: float, lam_a: float, max_goals: int = 12) -> tuple:
    """由双方期望进球 λ 推导 1X2 概率 (P主胜, P平, P客胜)。"""
    def pmf(lam: float, k: int) -> float:
        return math.exp(-lam) * (lam ** k) / math.factorial(k)
    ph = pd = pa = 0.0
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            p = pmf(lam_h, i) * pmf(lam_a, j)
            if i > j:
                ph += p
            elif i == j:
                pd += p
            else:
                pa += p
    return ph, pd, pa


def model_probs_from_matrix(M: Any) -> List[float]:
    """由 OIP 比分概率矩阵 M (numpy 2D) 推导 1X2 边缘概率 [P主, P平, P客]。"""
    import numpy as np
    M = np.asarray(M, dtype=float)
    n = M.shape[0]
    ph = float(sum(M[i, j] for i in range(n) for j in range(n) if i > j))
    pd = float(sum(M[i, j] for i in range(n) for j in range(n) if i == j))
    pa = float(sum(M[i, j] for i in range(n) for j in range(n) if i < j))
    return [ph, pd, pa]


def consensus_probs(books: List[List[float]]) -> List[float]:
    """跨庄共识隐含概率：各庄去抽水隐含概率取均值再归一。

    设计依据（FootballAI v6 铁律）：模型对 1X2 无超越赔率的信息优势，
    故"模型概率"的最佳估计 = 跨庄共识隐含概率；1X2 的真实 edge 仅来自
    跨庄价差（soft line / 套利空间），由 compute_value_layer 用 best_odds 计算。
    books = [[oh, od, oa], ...]，单庄时共识=该庄 → edge≈0 → PASS。"""
    valid = [b for b in books if b and all(x > 0 for x in b)]
    if not valid:
        return [0.0, 0.0, 0.0]
    imp = [market_implied(b) for b in valid]
    n = len(imp)
    avg = [sum(p[i] for p in imp) / n for i in range(3)]
    s = sum(avg) or 1.0
    return [x / s for x in avg]


# ───────────────────────────────────────────────────────────────────────────
# 子市场价值层 (SSoT 已迁出)
# 大小球 ou_value / 平局共识 draw_consensus_value / 波胆 correct_score_value
# 的价值层数学现已全部迁入 pipeline.compute_value_layer (SSoT)。
# 本模块仅保留 re-export: ou_value 别名 + draw/correct_score 由上方 import 直接绑定 SSoT。
# ───────────────────────────────────────────────────────────────────────────



"""
条件波胆（Truncated Poisson）— v1.0

赛前 OIP λ,μ 截断到当前比分 + 剩余时间，输出条件波胆矩阵 + top5。
差值 = 独立 Poisson（剩余时间进球），与赛前全矩阵无关——简化计算且数学等价。

用法:
    from pipeline.conditional_score import conditional_score_matrix, top_scores
    # 赛前 λ,μ（来自 OIP 反解）
    M = conditional_score_matrix(lam=1.45, mu=1.10, score_h=1, score_a=0, minutes_played=60)
    top = top_scores(M, k=5)
"""

import numpy as np
from scipy.stats import poisson


def conditional_score_matrix(
    lam: float,
    mu: float,
    score_h: int = 0,
    score_a: int = 0,
    minutes_played: float = 0,
    max_goals: int = 12,
    match_length: int = 90,
) -> np.ndarray:
    """条件波胆矩阵 P(最终比分 = i,j | 当前 = s_h,s_a, 已过 t 分钟)。

    数学:
        剩余时间 R = match_length - minutes_played
        剩余进球 ~ 独立 Poisson(λ * R/90, μ * R/90)
        最终比分 = 当前比分 + 剩余进球

    参数:
        lam, mu: 赛前 OIP 反解的独立 Poisson 率（全场 90 分钟）
        score_h, score_a: 当前比分
        minutes_played: 已过分钟数（含伤停补时估算）
        max_goals: 比分上限（矩阵维度）
        match_length: 常规比赛分钟数（默认 90）

    返回:
        (max_goals, max_goals) 概率矩阵，已归一化
    """
    remaining = max(match_length - minutes_played, 1.0)  # 至少 1 分钟，防除零
    lam_rem = lam * remaining / match_length
    mu_rem = mu * remaining / match_length

    # 未来进球
    f_h = poisson.pmf(np.arange(max_goals), lam_rem)
    f_a = poisson.pmf(np.arange(max_goals), mu_rem)

    # 条件矩阵：P(最终 = i,j) = P(未来主 = i-s_h) * P(未来客 = j-s_a)
    M = np.zeros((max_goals, max_goals))
    for i in range(score_h, max_goals):
        for j in range(score_a, max_goals):
            M[i, j] = f_h[i - score_h] * f_a[j - score_a]

    total = M.sum()
    if total > 0:
        M /= total
    return M


def top_scores(M: np.ndarray, k: int = 5) -> list[tuple[int, int, float]]:
    """从波胆矩阵取 top-k 比分及其概率。

    返回: [(主队进球, 客队进球, 概率%), ...]  概率% 四舍五入到小数点后 1 位
    """
    flat = M.flatten()
    idx = np.argsort(-flat)[:k]
    max_goals = M.shape[0]
    return [
        (int(i // max_goals), int(i % max_goals), round(float(flat[i]) * 100, 1))
        for i in idx
    ]


def hda_from_matrix(M: np.ndarray) -> tuple[float, float, float]:
    """从波胆矩阵提取 H/D/A 边缘概率。"""
    max_goals = M.shape[0]
    h = M[np.tril_indices(max_goals, -1)].sum()  # i > j
    d = np.trace(M)                               # i == j
    a = M[np.triu_indices(max_goals, 1)].sum()     # i < j
    return float(h), float(d), float(a)


def ou_from_matrix(M: np.ndarray, line: float = 2.5) -> tuple[float, float]:
    """从波胆矩阵提取大小球概率（总进球 vs line）。"""
    max_goals = M.shape[0]
    over = sum(float(M[i, j]) for i in range(max_goals) for j in range(max_goals) if i + j > line)
    under = sum(float(M[i, j]) for i in range(max_goals) for j in range(max_goals) if i + j <= line)
    return over, under

# -*- coding: utf-8 -*-
"""scoring_metrics: RPS / ECE 概率校准与序数评估工具 (报告 P2#3).

替代/补充纯准确率: RPS 惩罚"高置信但错误"的序数结果(1X2 主<平<客排序),
是职业博彩模型标准评估; ECE 衡量概率校准度(预测置信 vs 真实频率)。

公式:
  RPS(序数, J 类) = (1/(J-1)) * Σ_{j=1}^{J-1} (cumF_pred(j) - cumF_obs(j))^2
    二分类(J=2) 退化为 (p - y)^2 (即 Brier 二分类)。
  ECE = Σ_b (n_b/n) * |acc(b) - conf(b)|  (按置信度分箱)。

所有函数纯函数、无副作用、可单测。
"""
from __future__ import annotations
import numpy as np
from typing import Sequence


def rps_ordinal(y_true_idx: Sequence[int], p_matrix, n_classes: int | None = None) -> float:
    """Ranked Probability Score (序数).

    Args:
        y_true_idx: 真实类别下标 (0..J-1), 如 1X2 用 0=主/1=平/2=客 (须按序数含义排序)。
        p_matrix: 预测概率矩阵 (n, J), 每行和为 1。
        n_classes: 类别数 (默认取 p_matrix 列数)。

    Returns:
        RPS (越小越好; 完美预测=0)。
    """
    p = np.asarray(p_matrix, dtype=float)
    if p.ndim == 1:
        # 二分类: RPS = (p - y)^2
        y = np.asarray(y_true_idx, dtype=float)
        return float(np.mean((p - y) ** 2))
    n = int(p.shape[1]) if n_classes is None else int(n_classes)
    y_idx = np.asarray(y_true_idx, dtype=int)
    if len(y_idx) != p.shape[0]:
        raise ValueError("y_true_idx 与 p_matrix 行数不一致")
    onehot = np.zeros((len(y_idx), n), dtype=float)
    onehot[np.arange(len(y_idx)), np.clip(y_idx, 0, n - 1)] = 1.0
    cum_pred = np.cumsum(p[:, :n], axis=1)[:, :-1]   # 排除末列(恒=1)
    cum_obs = np.cumsum(onehot, axis=1)[:, :-1]
    per = np.sum((cum_pred - cum_obs) ** 2, axis=1)
    return float(np.mean(per) / (n - 1))


def rps_binary(y_true: Sequence[int], p: Sequence[float]) -> float:
    """RPS 二分类退化形式 = 均方误差 (同 Brier 二分类)."""
    y = np.asarray(y_true, dtype=float)
    pp = np.asarray(p, dtype=float)
    return float(np.mean((pp - y) ** 2))


def ece_binary(y_true: Sequence[int], p: Sequence[float], n_bins: int = 10) -> float:
    """Expected Calibration Error (二分类).

    按预测置信度(max(p,1-p))分箱, 加权 |分箱准确率 - 分箱平均置信|。
    返回 [0,1], 越小越校准。
    """
    y = np.asarray(y_true, dtype=float)
    pp = np.clip(np.asarray(p, dtype=float), 1e-6, 1.0 - 1e-6)
    conf = np.maximum(pp, 1.0 - pp)
    pred = (pp >= 0.5).astype(float)
    correct = (pred == y).astype(float)
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(conf, bins) - 1, 0, n_bins - 1)
    ece = 0.0
    for b in range(n_bins):
        m = idx == b
        nb = int(m.sum())
        if nb == 0:
            continue
        acc_b = float(correct[m].mean())
        conf_b = float(conf[m].mean())
        ece += (nb / len(y)) * abs(acc_b - conf_b)
    return float(ece)


def brier_binary(y_true: Sequence[int], p: Sequence[float]) -> float:
    """Brier score (二分类), 与 rps_binary 等价, 单列便于报告对照."""
    return rps_binary(y_true, p)


if __name__ == "__main__":
    # 自检: 完美预测 RPS=0; 错估拉高 RPS; ECE 在 [0,1]
    yt = [0, 1, 2, 0, 2]
    perfect = [[1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 0, 0], [0, 0, 1]]
    wrong = [[0, 0, 1], [1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 0, 0]]
    r_p = rps_ordinal(yt, perfect)
    r_w = rps_ordinal(yt, wrong)
    assert r_p == 0.0, f"perfect RPS 应=0, 实={r_p}"
    assert r_w > r_p, f"wrong RPS 应>perfect, 实={r_w}"
    # 二分类 ECE
    yb = [1, 0, 1, 0, 1]
    pb = [0.9, 0.1, 0.8, 0.2, 0.7]
    e = ece_binary(yb, pb)
    assert 0.0 <= e <= 1.0, f"ECE 应在[0,1], 实={e}"
    print(f"[ok] RPS perfect={r_p:.4f} wrong={r_w:.4f}  ECE={e:.4f}")

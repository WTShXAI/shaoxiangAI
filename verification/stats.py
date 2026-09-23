"""ROI 点估计 / 置信区间 / 二项方向检验 (T03).

纯统计原语, 仅依赖 numpy + scipy (设计选型). 不在此重写任何业务逻辑。
- ROI 95% CI: 默认 bootstrap (n=10000 重采样), 回退 t 区间
- 二项方向检验: scipy.stats.binomtest (G5 独立显著性证据)
"""
from __future__ import annotations

from typing import List, Tuple

import numpy as np
from scipy import stats


def roi_point(returns: List[float]) -> float:
    """ROI 点估计 = 平均纸盘收益."""
    if not returns:
        return 0.0
    return float(np.mean(np.asarray(returns, dtype=float)))


def roi_ci_bootstrap(returns: List[float], alpha: float = 0.05,
                     n_boot: int = 10000) -> Tuple[float, float]:
    """bootstrap 百分位 95% CI (lo, hi). 样本 <2 返回 (0,0)."""
    arr = np.asarray(returns, dtype=float)
    if arr.size < 2:
        return (0.0, 0.0)
    rng = np.random.default_rng(12345)
    idx = rng.integers(0, arr.size, size=(n_boot, arr.size))
    boot = arr[idx].mean(axis=1)
    lo = float(np.percentile(boot, 100.0 * alpha / 2.0))
    hi = float(np.percentile(boot, 100.0 * (1.0 - alpha / 2.0)))
    return (lo, hi)


def roi_ci_t(returns: List[float], alpha: float = 0.05) -> Tuple[float, float]:
    """基于 t 分布的 95% CI (lo, hi). 样本 <2 或 se=0 退化为中心."""
    arr = np.asarray(returns, dtype=float)
    n = arr.size
    if n < 2:
        return (0.0, 0.0)
    mean = float(arr.mean())
    se = arr.std(ddof=1) / np.sqrt(n)
    if se == 0.0:
        return (mean, mean)
    tcrit = float(stats.t.ppf(1.0 - alpha / 2.0, df=n - 1))
    return (mean - tcrit * se, mean + tcrit * se)


def binomial_p(k: int, n: int, p0: float = 0.5) -> float:
    """二项检验 p 值: 观测 k/n 命中率 vs 零假设 p0.
    用于 G5 方向显著性 (1X2 取 p0=1/3). n<=0 返回 1.0.
    """
    if n <= 0:
        return 1.0
    return float(stats.binomtest(int(k), int(n), p0).pvalue)


__all__ = ["roi_point", "roi_ci_bootstrap", "roi_ci_t", "binomial_p"]

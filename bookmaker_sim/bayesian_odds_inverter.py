"""
哨响AI — 贝叶斯赔率逆推引擎 v1.0
===================================
从赔率反推博彩公司的贝叶斯参数以校准模型。

核心理念:
  赔率 = 博彩公司内部状态(λ_H, λ_A, ρ) → 概率 → 施加margin变换 → 赔率
  我们拥有的是赔率(密文)，需要逆向解码出(λ_H, λ_A, ρ)的后验分布。

三层逆推:
  Layer 1: 赔率 → 去抽水概率 → 隐含概率向量 [P(H), P(D), P(A)]
  Layer 2: 隐含概率 → 泊松/Dixon-Coles逆问题 → λ_H, λ_A, ρ的点估计
  Layer 3: 贝叶斯框架 → λ_H, λ_A, ρ的完整后验分布

集成:
  - 作为 bookmaker_sim 第四层，被 AdversarialOddsVerifier 调用
  - 输出贝叶斯特征注入模型 (9维: bayes_lambda_*)
  - OTSM 状态作为贝叶斯先验精度调节器

依赖:
  - score_distribution.py (正向: λ→概率)
  - market_derivation.py (正向: 概率→赔率)
  - odds_temporal_sm.py (OTSM状态 → 先验精度)

作者: 季泊松
日期: 2026-06-16
"""

import numpy as np
from scipy.stats import poisson, gamma, norm, beta as beta_dist
from scipy.optimize import minimize, Bounds, differential_evolution
from scipy.special import logsumexp
from typing import Dict, Tuple, Optional, List
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# 基础数据结构
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class BayesianLambdaPosterior:
    """λ_H, λ_A, ρ 的贝叶斯后验"""
    # 点估计 (MAP)
    lambda_h_map: float
    lambda_a_map: float
    rho_map: float

    # 后验均值
    lambda_h_mean: float
    lambda_a_mean: float
    rho_mean: float

    # 后验标准差
    lambda_h_std: float
    lambda_a_std: float
    rho_std: float

    # 完整后验样本 (MCMC/SIR)
    samples_lambda_h: np.ndarray = None
    samples_lambda_a: np.ndarray = None
    samples_rho: np.ndarray = None

    # 诊断信息
    convergence_ok: bool = True
    n_eff: int = 0
    r_hat: float = 1.0

    # 后验预测: P(H), P(D), P(A) 的分布
    prob_home_samples: np.ndarray = None
    prob_draw_samples: np.ndarray = None
    prob_away_samples: np.ndarray = None

    @property
    def prob_home_ci95(self) -> Tuple[float, float]:
        if self.prob_home_samples is not None:
            return (float(np.percentile(self.prob_home_samples, 2.5)),
                    float(np.percentile(self.prob_home_samples, 97.5)))
        return (0.0, 0.0)

    @property
    def prob_draw_ci95(self) -> Tuple[float, float]:
        if self.prob_draw_samples is not None:
            return (float(np.percentile(self.prob_draw_samples, 2.5)),
                    float(np.percentile(self.prob_draw_samples, 97.5)))
        return (0.0, 0.0)

    @property
    def prob_away_ci95(self) -> Tuple[float, float]:
        if self.prob_away_samples is not None:
            return (float(np.percentile(self.prob_away_samples, 2.5)),
                    float(np.percentile(self.prob_away_samples, 97.5)))
        return (0.0, 0.0)

    @property
    def bayes_draw_confidence(self) -> float:
        """贝叶斯平局置信度: 平局概率后验标准差越小→置信越高 (1-σ/mean 映射到[0,1])"""
        if self.prob_draw_samples is not None:
            mean_d = float(np.mean(self.prob_draw_samples))
            std_d = float(np.std(self.prob_draw_samples))
            if mean_d > 1e-8:
                return float(np.clip(1.0 - std_d / mean_d, 0.0, 1.0))
        return 0.0

    @property
    def bayes_signal_strength(self) -> float:
        """综合贝叶斯信号强度: 后验精度总和的归一化值"""
        prec_h = 1.0 / (self.lambda_h_std ** 2 + 1e-6)
        prec_a = 1.0 / (self.lambda_a_std ** 2 + 1e-6)
        prec_r = 1.0 / (self.rho_std ** 2 + 1e-6)
        total_precision = prec_h + prec_a + prec_r
        # 典型范围: [0, 100+], 用 sigmoid 映射到 [0, 1]
        return float(1.0 / (1.0 + np.exp(-(total_precision - 20) / 10)))

@dataclass
class InversionResult:
    """完整的逆推结果"""
    # 原始输入
    raw_odds: Tuple[float, float, float]  # (H, D, A)
    implied_probs: Tuple[float, float, float]  # 去抽水后概率

    # 确定性逆推
    lambda_h_det: float
    lambda_a_det: float
    rho_det: float
    det_residual: float           # 确定性解的残差

    # 贝叶斯后验
    posterior: BayesianLambdaPosterior = None

    # 额外上下文
    league_prior: Optional[Dict] = None
    otsm_state: Optional[str] = None
    otsm_lock_confidence: float = 0.0

# ═══════════════════════════════════════════════════════════════════════════════
# 第1层: 赔率 → 去抽水 → 隐含概率
# ═══════════════════════════════════════════════════════════════════════════════

def odds_to_probs_vector(odds_h: float, odds_d: float, odds_a: float,
                         remove_method: str = 'proportional') -> np.ndarray:
    """
    赔率 → 去抽水后的隐含概率向量 [P(H), P(D), P(A)]

    三种去抽水方法:
      - proportional:  p_i = (1/o_i) / Σ(1/o_j)  ← 当前方法
      - shin:          Shin (1993) 模型, 考虑庄家内部不确定性
      - power:         p_i = (1/o_i)^k / Σ(1/o_j)^k, k∈(0,1] 为幂参数, k<1 收缩分布

    Args:
        odds_h, odds_d, odds_a: Interwetten 赔率
        remove_method: 去抽水方法

    Returns:
        np.ndarray([p_h, p_d, p_a]), sum=1.0
    """
    raw = np.array([1.0 / odds_h, 1.0 / odds_d, 1.0 / odds_a])

    if remove_method == 'proportional':
        # 当前比例法
        return raw / raw.sum()

    elif remove_method == 'shin':
        # Shin (1993): 庄家对赛果有内部不确定性 z
        # 隐含概率 ∝ sqrt(z + (1-z)*raw_i)
        # z 通过求解 overround 方程得到
        from scipy.optimize import fsolve

        def shin_eq(z):
            z = float(np.clip(z, 0.0, 0.99))
            adjusted = np.sqrt(z + (1 - z) * raw)
            return float(np.sum(adjusted) - 1.0)

        z0 = max(0.0, min(0.5, raw.sum() - 1.0))
        try:
            z_opt = float(fsolve(shin_eq, z0, maxfev=100)[0])
            z_opt = np.clip(z_opt, 0.0, 0.5)
        except (ValueError, RuntimeError):
            z_opt = 0.0  # fsolve 未收敛, 回退均匀抽水

        adjusted = np.sqrt(z_opt + (1 - z_opt) * raw)
        return adjusted / adjusted.sum()

    elif remove_method == 'power':
        # 幂方法: k 通过 overround 迭代确定
        k = 1.0
        for _ in range(20):
            powered = raw ** k
            total = powered.sum()
            if abs(total - 1.0) < 1e-6:
                break
            k = k * (1.0 - 0.5 * (total - 1.0))
            k = np.clip(k, 0.3, 1.0)
        powered = raw ** k
        return powered / powered.sum()

    else:
        return raw / raw.sum()

# ═══════════════════════════════════════════════════════════════════════════════
# 第2层: 概率 → λ 的逆问题 (well-posedness 分析)
# ═══════════════════════════════════════════════════════════════════════════════

class LambdaInverter:
    """
    从 [P(H), P(D), P(A)] 逆推 λ_H, λ_A, ρ

    === Well-Posedness 分析 ===

    正向模型 (Dixon-Coles):
      P(H) = Σ_{i>j} Poisson(i|λ_H) · Poisson(j|λ_A) · τ(i,j,λ_H,λ_A,ρ)
      P(D) = Σ_{i=j} Poisson(i|λ_H) · Poisson(j|λ_A) · τ(i,j,λ_H,λ_A,ρ)
      P(A) = Σ_{i<j} Poisson(i|λ_H) · Poisson(j|λ_A) · τ(i,j,λ_H,λ_A,ρ)

    其中 τ(·) 仅在 {(0,0),(1,0),(0,1),(1,1)} 非1。

    逆问题: 3 方程, 3 未知数 (λ_H, λ_A, ρ)

    Well-posed? 条件分析:

    1. 简化为独立泊松 (ρ=0):
       P(H) = f_H(λ_H, λ_A)  连续且单调递增于 λ_H, 递减于 λ_A
       P(D) = f_D(λ_H, λ_A)
       P(A) = f_A(λ_H, λ_A)

       由于 P(H)+P(D)+P(A)=1, 实际上只有 2 个独立方程。
       但 λ_H, λ_A 是 2 个未知数 → (ρ=0) 下方程组是 closed 的。

       加上总进球信息: E[total] = λ_H + λ_A ≈ -ln(P(0,0))
       从数据库: 312K样本中 avg_goals=2.75, 这提供了隐式正则化。

    2. Dixon-Coles (ρ≠0): 3 未知数, 3 方程 (但 P(H)+P(D)+P(A)=1 减一度)
       → 2 独立方程 + 1 约束 = 需要额外信息!

       实际上 ρ 主要影响低分平局 (0-0, 1-1), 因此:
       - 如果 P(D) 主要由 0-0 和 1-1 贡献 → ρ 有信息
       - 如果 P(D) 的高分平局比例大 → ρ 欠定

    3. 解法:
       方案A: 先用总进球信息 E[total] 约束 λ_H+λ_A, 再优化
       方案B: 加入跨市场信息 (大小球 O2.5) 增加第3个独立方程
       方案C: 贝叶斯先验 (league-level λ 分布) 正则化
    """

    MAX_GOALS = 8

    @staticmethod
    def det_invert(probs: np.ndarray, rho_fixed: float = None,
                   use_total_goals: bool = True,
                   avg_total_goals: float = 2.75) -> Tuple[float, float, float, float]:
        """
        确定性逆推: probs → (λ_H, λ_A, ρ)

        Args:
            probs: [P(H), P(D), P(A)]
            rho_fixed: 如果给定, 固定 ρ 只优化 λ
            use_total_goals: 是否使用总进球约束

        Returns:
            (λ_H, λ_A, ρ, residual)
        """
        p_h, p_d, p_a = probs[0], probs[1], probs[2]

        # ── 约束: 从 P(0,0) 估计总 λ ──
        # P(0,0) ≈ exp(-λ_H) * exp(-λ_A) * τ(0,0)
        # 粗略: p_d 中低分平局占比 → 估计 P(0,0)
        # 启发式: P(0,0) ≈ p_d * 0.15 (平局中约15%是0-0)
        p_00_approx = p_d * 0.15
        total_lambda_bound = max(0.5, -np.log(max(p_00_approx, 1e-6)))
        total_lambda_bound = min(total_lambda_bound, 8.0)

        # ── 优化 ──
        if rho_fixed is not None:
            # 固定 ρ, 优化 λ_H, λ_A
            def objective(params):
                lam_h, lam_a = params[0], params[1]
                if lam_h < 0.02 or lam_a < 0.02:
                    return 1e10
                if lam_h > 8 or lam_a > 8:
                    return 1e10

                pred = LambdaInverter._forward_dc(lam_h, lam_a, rho_fixed)
                loss = np.sum((pred - probs) ** 2)

                # 总进球约束正则化
                if use_total_goals:
                    loss += 0.1 * ((lam_h + lam_a - total_lambda_bound) / total_lambda_bound) ** 2

                return loss

            result = minimize(objective, x0=[1.5, 1.2],
                            bounds=Bounds([0.05, 0.05], [5.0, 5.0]),
                            method='L-BFGS-B')
            lam_h, lam_a = result.x
            rho = rho_fixed
        else:
            # 全优化 λ_H, λ_A, ρ
            def objective3(params):
                lam_h, lam_a, rho = params[0], params[1], params[2]
                if lam_h < 0.02 or lam_a < 0.02:
                    return 1e10
                if lam_h > 8 or lam_a > 8:
                    return 1e10
                if rho < -0.5 or rho > 0.3:
                    return 1e10

                pred = LambdaInverter._forward_dc(lam_h, lam_a, rho)
                loss = np.sum((pred - probs) ** 2)

                if use_total_goals:
                    loss += 0.1 * ((lam_h + lam_a - total_lambda_bound) / total_lambda_bound) ** 2

                # ρ 的先验: 通常轻微负值 (-0.1 到 0.05)
                loss += 0.01 * (rho + 0.03) ** 2

                return loss

            result = minimize(objective3, x0=[1.5, 1.2, -0.03],
                            bounds=Bounds([0.05, 0.05, -0.4], [5.0, 5.0, 0.2]),
                            method='L-BFGS-B')
            lam_h, lam_a, rho = result.x

        residual = float(result.fun)
        return lam_h, lam_a, rho, residual

    @staticmethod
    def det_invert_with_totals(probs_1x2: np.ndarray,
                                p_over_25: float,
                                avg_total_goals: float = 2.75) -> Tuple[float, float, float, float]:
        """
        增强确定性逆推: 利用大小球信息

        现在有 3 个独立方程:
          P(H) = f1(λ_H, λ_A, ρ)
          P(D) = f2(λ_H, λ_A, ρ)
          P(O2.5) = f3(λ_H, λ_A, ρ)  ← 额外的独立信息

        Well-posed: 3 独立方程, 3 未知数 → 良好

        Args:
            probs_1x2: [P(H), P(D), P(A)]
            p_over_25: 大于 2.5 球的概率

        Returns:
            (λ_H, λ_A, ρ, residual)
        """

        def objective(params):
            lam_h, lam_a, rho = params
            if lam_h < 0.02 or lam_a < 0.02:
                return 1e10

            pred_1x2 = LambdaInverter._forward_dc(lam_h, lam_a, rho)
            pred_over = LambdaInverter._forward_over25(lam_h, lam_a, rho)

            loss = np.sum((pred_1x2 - probs_1x2) ** 2)
            loss += 1.0 * (pred_over - p_over_25) ** 2
            loss += 0.01 * (rho + 0.03) ** 2

            return loss

        result = minimize(objective, x0=[1.5, 1.2, -0.03],
                        bounds=Bounds([0.05, 0.05, -0.4], [5.0, 5.0, 0.2]),
                        method='L-BFGS-B')

        return result.x[0], result.x[1], result.x[2], float(result.fun)

    @staticmethod
    def _forward_dc(lam_h: float, lam_a: float, rho: float) -> np.ndarray:
        """正向: λ → [P(H), P(D), P(A)] (Dixon-Coles)"""
        M = LambdaInverter.MAX_GOALS
        p_h, p_d, p_a = 0.0, 0.0, 0.0
        total = 0.0

        for i in range(M + 1):
            for j in range(M + 1):
                p = poisson.pmf(i, lam_h) * poisson.pmf(j, lam_a)
                tau = LambdaInverter._tau(i, j, lam_h, lam_a, rho)
                p *= tau
                if i > j:
                    p_h += p
                elif i == j:
                    p_d += p
                else:
                    p_a += p
                total += p

        if total > 0:
            return np.array([p_h, p_d, p_a]) / total
        return np.array([p_h, p_d, p_a])

    @staticmethod
    def _forward_over25(lam_h: float, lam_a: float, rho: float) -> float:
        """正向: λ → P(total > 2.5)"""
        M = LambdaInverter.MAX_GOALS
        p_over = 0.0
        total = 0.0
        for i in range(M + 1):
            for j in range(M + 1):
                p = poisson.pmf(i, lam_h) * poisson.pmf(j, lam_a)
                tau = LambdaInverter._tau(i, j, lam_h, lam_a, rho)
                p *= tau
                if i + j > 2.5:
                    p_over += p
                total += p
        return p_over / total if total > 0 else 0.0

    @staticmethod
    def _tau(s_h: int, s_a: int, lam_h: float, lam_a: float, rho: float) -> float:
        """Dixon-Coles τ 因子"""
        if s_h == 0 and s_a == 0:
            return 1.0 - lam_h * lam_a * rho
        elif s_h == 0 and s_a == 1:
            return 1.0 + lam_h * rho
        elif s_h == 1 and s_a == 0:
            return 1.0 + lam_a * rho
        elif s_h == 1 and s_a == 1:
            return 1.0 - rho
        else:
            return 1.0

# ═══════════════════════════════════════════════════════════════════════════════
# 第3层: 贝叶斯后验推断 (核心)
# ═══════════════════════════════════════════════════════════════════════════════

class BayesianOddsInverter:
    """
    贝叶斯赔率逆推引擎

    从赔率推导 λ_H, λ_A, ρ 的完整后验分布。

    数学框架:
      P(λ_H, λ_A, ρ | odds) ∝ P(odds | λ_H, λ_A, ρ) × P(λ_H, λ_A, ρ)

    先验 P(λ_H, λ_A, ρ):
      - λ_H, λ_A ~ Gamma(α, β): 经验贝叶斯从312K条历史估计
      - ρ ~ Normal(-0.03, 0.08): 弱正则化先验
      - λ_H 和 λ_A 不独立: 通过 λ_H+λ_A ~ Gamma(α_sum, β_sum) 关联

    似然 P(odds | λ):
      - 正向: λ → 比分分布 → 概率 → 施加margin → 赔率
      - 观测模型: log(odds) ~ Normal(log(1/p_adjusted), σ²_obs)
      - σ_obs 反映庄家"加密噪声"程度

    推断方法:
      1. MAP (最大后验): 梯度优化, 快速点估计
      2. Laplace 近似: MAP + Hessian → 正态近似后验
      3. SIR (Sampling Importance Resampling): 非正态后验采样
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path
        self.max_goals = 8

        # 经验先验参数 (从312K数据估计)
        self.prior_lam_h_alpha = 8.0    # 形状 → mean = α/β ≈ 1.6
        self.prior_lam_h_beta = 5.0     # 速率
        self.prior_lam_a_alpha = 6.5    # 客队略弱
        self.prior_lam_a_beta = 5.0
        self.prior_rho_mean = -0.03
        self.prior_rho_std = 0.08

        # 似然参数
        self.sigma_obs = 0.05           # 赔率观测噪声 (对数空间)

        # 联赛特有先验缓存
        self.league_priors: Dict[str, Dict] = {}

    # ── 先验设置 ───────────────────────────────────────────────────────

    def set_empirical_prior_from_db(self, league: str = None, sample_size: int = 100000):
        """
        从历史数据库 (312K条) 估计经验贝叶斯先验。

        方法:
          1. 对每场比赛: 用比例法从赔率去抽水
          2. 用确定性逆推获得 λ_H, λ_A 点估计
          3. 用 MLE 拟合 Gamma 分布超参数

        λ 的矩估计:
          Gamma(α, β): mean=α/β, var=α/β²
          → β = mean/var, α = mean²/var
        """
        import sqlite3

        db = self.db_path or "data/football_data.db"
        conn = sqlite3.connect(db)
        params = []
        query = """
        SELECT open_home, open_draw, open_away, league_name
        FROM training_extended
        WHERE open_home IS NOT NULL
        """
        if league:
            query += " AND league_name = ?"
            params.append(league)
        query += f" LIMIT {sample_size}"

        cur = conn.execute(query, params)
        rows = cur.fetchall()
        conn.close()

        lam_h_ests, lam_a_ests, rho_ests = [], [], []

        for row in rows:
            try:
                probs = odds_to_probs_vector(float(row[0]), float(row[1]), float(row[2]))
                lam_h, lam_a, rho, _ = LambdaInverter.det_invert(probs)
                lam_h_ests.append(lam_h)
                lam_a_ests.append(lam_a)
                rho_ests.append(rho)
            except (ValueError, RuntimeError):
                continue  # 数值求解除外, 跳过该样本

        if len(lam_h_ests) < 100:
            logger.warning(f"经验先验估计样本不足: {len(lam_h_ests)}")
            return

        lam_h_arr = np.array(lam_h_ests)
        lam_a_arr = np.array(lam_a_ests)
        rho_arr = np.array(rho_ests)

        # Gamma MLE via 矩估计
        for arr, prefix in [(lam_h_arr, 'lam_h'), (lam_a_arr, 'lam_a')]:
            mean_val = float(np.mean(arr))
            var_val = float(np.var(arr))
            if var_val > 1e-6:
                beta_val = mean_val / var_val
                alpha_val = mean_val * beta_val
                setattr(self, f'prior_{prefix}_alpha', alpha_val)
                setattr(self, f'prior_{prefix}_beta', beta_val)

        self.prior_rho_mean = float(np.mean(rho_arr))
        self.prior_rho_std = float(np.std(rho_arr)) + 0.02

        prior_info = {
            'lam_h': (self.prior_lam_h_alpha, self.prior_lam_h_beta),
            'lam_a': (self.prior_lam_a_alpha, self.prior_lam_a_beta),
            'rho': (self.prior_rho_mean, self.prior_rho_std),
            'n_samples': len(lam_h_ests),
        }

        if league:
            self.league_priors[league] = prior_info

        logger.info(f"经验先验估计完成 (n={len(lam_h_ests)}): "
                    f"λ_H~Gamma({self.prior_lam_h_alpha:.1f},{self.prior_lam_h_beta:.1f}), "
                    f"λ_A~Gamma({self.prior_lam_a_alpha:.1f},{self.prior_lam_a_beta:.1f}), "
                    f"ρ~N({self.prior_rho_mean:.4f},{self.prior_rho_std:.4f})")

    # ── 似然函数 ───────────────────────────────────────────────────────

    def log_likelihood(self, lam_h: float, lam_a: float, rho: float,
                       odds: np.ndarray, margin: float = 0.06) -> float:
        """
        似然: log P(odds | λ_H, λ_A, ρ)

        赔率观测模型:
          odds_i = 1 / (p_i(λ) × (1 + margin/N)) × exp(ε_i)
          ε_i ~ N(0, σ²_obs)

        因此: log(odds_i) ~ N(log(1/(p_i×(1+m/N))), σ²_obs)
        """
        if lam_h < 0.02 or lam_a < 0.02 or lam_h > 8 or lam_a > 8:
            return -1e10

        # 正向: λ → 概率
        probs = LambdaInverter._forward_dc(lam_h, lam_a, rho)

        # 施加抽水
        margin_per = margin / 3.0
        adjusted = probs * (1 + margin_per)
        adjusted = np.clip(adjusted, 0.01, 0.99)
        adjusted = adjusted / adjusted.sum()

        # 理论赔率
        fair_odds = 1.0 / adjusted

        # 高斯似然 (对数空间)
        log_odds = np.log(np.array(odds))
        log_fair = np.log(fair_odds)

        ll = -0.5 * np.sum((log_odds - log_fair) ** 2) / (self.sigma_obs ** 2)

        return float(ll)

    def log_prior(self, lam_h: float, lam_a: float, rho: float) -> float:
        """先验: log P(λ_H, λ_A, ρ)"""
        if lam_h <= 0 or lam_a <= 0:
            return -1e10

        # λ 的 Gamma 先验
        lp = gamma.logpdf(lam_h, self.prior_lam_h_alpha, scale=1.0/self.prior_lam_h_beta)
        lp += gamma.logpdf(lam_a, self.prior_lam_a_alpha, scale=1.0/self.prior_lam_a_beta)

        # ρ 的正态先验
        lp += norm.logpdf(rho, self.prior_rho_mean, self.prior_rho_std)

        return float(lp)

    def log_posterior(self, lam_h: float, lam_a: float, rho: float,
                      odds: np.ndarray) -> float:
        """对数后验: log P(λ_H, λ_A, ρ | odds)"""
        lp = self.log_prior(lam_h, lam_a, rho)
        ll = self.log_likelihood(lam_h, lam_a, rho, odds)
        return lp + ll

    # ── 后验推断 ───────────────────────────────────────────────────────

    def infer_map(self, odds_h: float, odds_d: float, odds_a: float,
                  verbose: bool = False) -> InversionResult:
        """
        MAP 估计: 最大化后验

        这是最快速的推断方式，适合在线推理。
        """
        odds = np.array([odds_h, odds_d, odds_a])
        probs = odds_to_probs_vector(odds_h, odds_d, odds_a, 'shin')

        # 先用确定性解做初始点
        lam_h0, lam_a0, rho0, _ = LambdaInverter.det_invert(probs)

        def neg_log_post(params):
            return -self.log_posterior(params[0], params[1], params[2], odds)

        result = minimize(neg_log_post, x0=[lam_h0, lam_a0, rho0],
                         bounds=Bounds([0.05, 0.05, -0.4], [5.0, 5.0, 0.2]),
                         method='L-BFGS-B')

        lam_h_map, lam_a_map, rho_map = result.x

        # Laplace 近似: Hessian → 后验标准差
        try:
            hess = self._hessian_log_post(lam_h_map, lam_a_map, rho_map, odds)
            eigvals = np.linalg.eigvalsh(hess)
            if np.all(eigvals > 0):
                cov = np.linalg.inv(hess)
                stds = np.sqrt(np.diag(cov))
                lam_h_std, lam_a_std, rho_std = stds[0], stds[1], stds[2]
            else:
                lam_h_std = lam_a_std = rho_std = 0.5  # 退化情况
        except (ValueError, RuntimeError):
            lam_h_std = lam_a_std = rho_std = 0.5  # Hessian奇异, 退化

        # 后验预测: 从 Laplace 近似采样
        n_samples = 1000
        samples = np.random.multivariate_normal(
            mean=[lam_h_map, lam_a_map, rho_map],
            cov=np.diag([lam_h_std**2, lam_a_std**2, rho_std**2]) * 0.5,
            size=n_samples
        )

        prob_samples = np.zeros((n_samples, 3))
        for k in range(n_samples):
            prob_samples[k] = LambdaInverter._forward_dc(
                max(0.02, samples[k, 0]),
                max(0.02, samples[k, 1]),
                np.clip(samples[k, 2], -0.4, 0.2)
            )

        posterior = BayesianLambdaPosterior(
            lambda_h_map=lam_h_map,
            lambda_a_map=lam_a_map,
            rho_map=rho_map,
            lambda_h_mean=lam_h_map,
            lambda_a_mean=lam_a_map,
            rho_mean=rho_map,
            lambda_h_std=lam_h_std,
            lambda_a_std=lam_a_std,
            rho_std=rho_std,
            prob_home_samples=prob_samples[:, 0],
            prob_draw_samples=prob_samples[:, 1],
            prob_away_samples=prob_samples[:, 2],
        )

        # 确定性逆推作为baseline
        lam_h_det, lam_a_det, rho_det, det_res = LambdaInverter.det_invert(probs)

        return InversionResult(
            raw_odds=(odds_h, odds_d, odds_a),
            implied_probs=tuple(probs),
            lambda_h_det=lam_h_det,
            lambda_a_det=lam_a_det,
            rho_det=rho_det,
            det_residual=det_res,
            posterior=posterior,
        )

    def infer_sir(self, odds_h: float, odds_d: float, odds_a: float,
                  n_particles: int = 10000, n_resample: int = 2000,
                  otsm_lock_conf: float = 0.0) -> InversionResult:
        """
        SIR (Sampling Importance Resampling): 非正态后验采样

        相比 MAP + Laplace 近似, SIR 能捕捉后验的多模态和非对称性。
        适合离线分析和高精度需求。

        OTSM 集成: lock_confidence 用来调节 proposal 分布的宽度。
          lock_confidence 高 → proposal 窄 (庄家确信,后验集中)
          lock_confidence 低 → proposal 宽 (庄家不确定,后验分散)
        """
        odds = np.array([odds_h, odds_d, odds_a])
        probs = odds_to_probs_vector(odds_h, odds_d, odds_a, 'shin')
        lam_h0, lam_a0, rho0, _ = LambdaInverter.det_invert(probs)

        # Proposal 分布: 围绕确定性解的宽分布
        # OTSM 调节: 高置信→缩小 proposal 宽度
        proposal_scale = 0.8 * (1.0 - 0.6 * otsm_lock_conf)

        # 采样 λ_H, λ_A 的 proposal (截断对数正态)
        particles_lam_h = np.random.lognormal(
            mean=np.log(max(lam_h0, 0.1)),
            sigma=proposal_scale,
            size=n_particles
        )
        particles_lam_a = np.random.lognormal(
            mean=np.log(max(lam_a0, 0.1)),
            sigma=proposal_scale,
            size=n_particles
        )
        particles_rho = np.random.normal(rho0, proposal_scale * 0.3, n_particles)
        particles_rho = np.clip(particles_rho, -0.4, 0.2)

        # 重要性权重
        log_weights = np.zeros(n_particles)
        for k in range(n_particles):
            log_weights[k] = self.log_posterior(
                particles_lam_h[k], particles_lam_a[k], particles_rho[k], odds
            )
            # 减去 proposal 的 log 密度 (简化: 假设均匀先验范围内的proposal)
            # 实践中用 ratio of uniforms 近似即可

        # 数值稳定
        log_weights -= logsumexp(log_weights)
        weights = np.exp(log_weights)
        weights /= weights.sum()

        # 重采样
        indices = np.random.choice(n_particles, size=n_resample, p=weights, replace=True)
        resampled_h = particles_lam_h[indices]
        resampled_a = particles_lam_a[indices]
        resampled_rho = particles_rho[indices]

        # 后验预测
        prob_samples = np.zeros((n_resample, 3))
        for k in range(n_resample):
            prob_samples[k] = LambdaInverter._forward_dc(
                max(0.02, resampled_h[k]),
                max(0.02, resampled_a[k]),
                np.clip(resampled_rho[k], -0.4, 0.2)
            )

        posterior = BayesianLambdaPosterior(
            lambda_h_map=float(resampled_h[np.argmax(weights[indices])]),
            lambda_a_map=float(resampled_a[np.argmax(weights[indices])]),
            rho_map=float(resampled_rho[np.argmax(weights[indices])]),
            lambda_h_mean=float(np.mean(resampled_h)),
            lambda_a_mean=float(np.mean(resampled_a)),
            rho_mean=float(np.mean(resampled_rho)),
            lambda_h_std=float(np.std(resampled_h)),
            lambda_a_std=float(np.std(resampled_a)),
            rho_std=float(np.std(resampled_rho)),
            samples_lambda_h=resampled_h,
            samples_lambda_a=resampled_a,
            samples_rho=resampled_rho,
            prob_home_samples=prob_samples[:, 0],
            prob_draw_samples=prob_samples[:, 1],
            prob_away_samples=prob_samples[:, 2],
            n_eff=int(1.0 / np.sum(weights ** 2)),  # 有效样本量
        )

        lam_h_det, lam_a_det, rho_det, det_res = LambdaInverter.det_invert(probs)

        return InversionResult(
            raw_odds=(odds_h, odds_d, odds_a),
            implied_probs=tuple(probs),
            lambda_h_det=lam_h_det,
            lambda_a_det=lam_a_det,
            rho_det=rho_det,
            det_residual=det_res,
            posterior=posterior,
        )

    def _hessian_log_post(self, lam_h: float, lam_a: float, rho: float,
                          odds: np.ndarray, eps: float = 0.01) -> np.ndarray:
        """数值 Hessian (中心差分)"""
        n = 3
        hess = np.zeros((n, n))
        f0 = self.log_posterior(lam_h, lam_a, rho, odds)

        params = np.array([lam_h, lam_a, rho])

        for i in range(n):
            for j in range(i, n):
                if i == j:
                    p_plus = params.copy(); p_plus[i] += eps
                    p_minus = params.copy(); p_minus[i] -= eps
                    fp = self.log_posterior(p_plus[0], p_plus[1], p_plus[2], odds)
                    fm = self.log_posterior(p_minus[0], p_minus[1], p_minus[2], odds)
                    hess[i, i] = (fp - 2*f0 + fm) / (eps**2)
                else:
                    p_pp = params.copy(); p_pp[i] += eps; p_pp[j] += eps
                    p_pm = params.copy(); p_pm[i] += eps; p_pm[j] -= eps
                    p_mp = params.copy(); p_mp[i] -= eps; p_mp[j] += eps
                    p_mm = params.copy(); p_mm[i] -= eps; p_mm[j] -= eps
                    fpp = self.log_posterior(p_pp[0], p_pp[1], p_pp[2], odds)
                    fpm = self.log_posterior(p_pm[0], p_pm[1], p_pm[2], odds)
                    fmp = self.log_posterior(p_mp[0], p_mp[1], p_mp[2], odds)
                    fmm = self.log_posterior(p_mm[0], p_mm[1], p_mm[2], odds)
                    hess[i, j] = (fpp - fpm - fmp + fmm) / (4 * eps**2)
                    hess[j, i] = hess[i, j]

        return hess

# ═══════════════════════════════════════════════════════════════════════════════
# 第4层: OTSM 集成 — 赔率漂移作为贝叶斯似然修正信号
# ═══════════════════════════════════════════════════════════════════════════════

class OTSMDriftBayesianIntegrator:
    """
    将 OTSM 赔率漂移信号集成到贝叶斯框架中。

    核心理念:
      赔率从开盘到收盘的漂移 = 庄家的"似然更新"过程。
      - 开盘赔率 → 先验预测 (基于赛前信息)
      - 收盘赔率 → 后验预测 (基于临场信息)
      - 漂移量 → 信息增益的证据强度

    贝叶斯解读:
      开盘: P(λ | I_open)     — 先验基于赛前基本面
      收盘: P(λ | I_close)    — 后验融合了更多信息
      漂移: KL(P_close || P_open) — 信息增益量

      OTSM 维度映射:
      D1 熵漂移 → 后验 vs 先验的熵减 = 信息增益
      D2 水位加速度 → 庄家对自身似然函数的确信度
      D3 凯利涨落 → 热门方后验概率的更新幅度
    """

    def __init__(self, inverter: BayesianOddsInverter = None):
        self.inverter = inverter or BayesianOddsInverter()

    def integrate(self,
                  open_odds: Tuple[float, float, float],
                  close_odds: Tuple[float, float, float],
                  otsm_state: str = "ACTIVE",
                  otsm_lock_conf: float = 0.0,
                  otsm_entropy_drift: float = 0.0,
                  otsm_water_accel: float = 0.0) -> Dict:
        """
        完整的 OTSM-贝叶斯集成分析

        流程:
          1. 从开盘赔率推断先验后验 P(λ | odds_open)
          2. 从收盘赔率推断似然后验 P(λ | odds_close)
          3. 计算两者之间的 KL 散度 = 信息增益
          4. 用 OTSM 状态调制贝叶斯置信度

        Returns:
            {
                'open_posterior': BayesianLambdaPosterior,
                'close_posterior': BayesianLambdaPosterior,
                'kl_divergence': float,         # 开→收 信息增益
                'drift_direction': str,         # 漂移方向解读
                'bayes_confidence': float,      # 综合贝叶斯置信度 [0,1]
                'otms_adjusted_prior_precision': float,  # OTSM 调制的先验精度
            }
        """
        # 开盘 → 先验后验
        result_open = self.inverter.infer_map(*open_odds)

        # 收盘 → 似然后验
        result_close = self.inverter.infer_map(*close_odds)

        # ── KL 散度: 开→收 的信息增益 ──
        # 简化: 用 1X2 概率的 KL
        p_open = np.array(result_open.implied_probs)
        p_close = np.array(result_close.implied_probs)
        eps = 1e-10
        kl = float(np.sum(p_close * np.log((p_close + eps) / (p_open + eps))))

        # ── 漂移方向解读 ──
        fav_open = np.argmax(p_open)
        fav_close = np.argmax(p_close)

        if fav_open == fav_close:
            if abs(kl) < 0.001:
                drift_direction = "STABLE"      # 市场无变化
            else:
                drift_direction = "CONFIRM"     # 确认原方向
        else:
            if fav_close == 1:  # 平局成为新热门
                drift_direction = "SWITCH_TO_DRAW"
            else:
                drift_direction = "REVERSAL"    # 方向反转

        # ── OTSM 调制贝叶斯置信度 ──
        # 策略: OTSM LOCKED → 提高先验精度 (缩小先验宽度)
        #       OTSM NOISE  → 降低先验精度 (放宽先验宽度,让数据说话)

        if otsm_state == "LOCKED":
            prior_precision_multiplier = 1.5 + otsm_lock_conf  # [1.5, 2.5]
        elif otsm_state == "ACTIVE":
            prior_precision_multiplier = 1.0 + 0.5 * otsm_lock_conf  # [1.0, 1.5]
        else:  # NOISE
            prior_precision_multiplier = 0.5  # 放宽先验

        # 熵漂移方向修正: 负向熵漂移(收敛) → 提高精度
        if otsm_entropy_drift < -0.02:
            prior_precision_multiplier += 0.2

        # 水位加速度修正: 降抽水(庄家自信) → 提高精度
        if otsm_water_accel < -0.03:
            prior_precision_multiplier += 0.15

        prior_precision_multiplier = np.clip(prior_precision_multiplier, 0.3, 3.0)

        # 综合贝叶斯置信度
        bayes_conf_close = result_close.posterior.bayes_signal_strength
        bayes_confidence = float(np.clip(
            bayes_conf_close * prior_precision_multiplier, 0.0, 1.0
        ))

        return {
            'open_posterior': result_open.posterior,
            'close_posterior': result_close.posterior,
            'kl_divergence': kl,
            'drift_direction': drift_direction,
            'bayes_confidence': bayes_confidence,
            'otms_adjusted_prior_precision': prior_precision_multiplier,
            'open_implied': result_open.implied_probs,
            'close_implied': result_close.implied_probs,
            'draw_prob_shift': result_close.posterior.prob_draw_samples.mean()
                               - result_open.posterior.prob_draw_samples.mean()
                               if (result_open.posterior.prob_draw_samples is not None
                                   and result_close.posterior.prob_draw_samples is not None)
                               else 0.0,
        }

# ═══════════════════════════════════════════════════════════════════════════════
# 第5层: 特征生成 — 贝叶斯特征注入模型
# ═══════════════════════════════════════════════════════════════════════════════

BAYESIAN_FEATURE_DEFS = [
    # 方案A: 贝叶斯参数作为新特征 (9维)
    ("bayes_lambda_h_map",        "λ_H MAP估计",                         0.0, 5.0),
    ("bayes_lambda_a_map",        "λ_A MAP估计",                         0.0, 5.0),
    ("bayes_rho_map",             "ρ MAP估计",                          -0.4, 0.2),
    ("bayes_lambda_h_std",        "λ_H 后验标准差 (不确定性)",           0.0, 2.0),
    ("bayes_lambda_a_std",        "λ_A 后验标准差",                     0.0, 2.0),
    ("bayes_rho_std",             "ρ 后验标准差",                        0.0, 0.5),
    ("bayes_signal_strength",     "贝叶斯信号强度 [0,1]",                0.0, 1.0),
    ("bayes_draw_confidence",     "贝叶斯平局置信度 [0,1]",              0.0, 1.0),
    ("bayes_kl_divergence",       "开→收 信息增益 (KL散度)",             0.0, 1.0),
]

BAYESIAN_DEFAULTS = {feat[0]: 0.0 for feat in BAYESIAN_FEATURE_DEFS}
# 覆盖有合理默认值的字段
BAYESIAN_DEFAULTS.update({
    "bayes_lambda_h_map": 1.4,
    "bayes_lambda_a_map": 1.2,
    "bayes_rho_map": -0.03,
    "bayes_lambda_h_std": 0.5,
    "bayes_lambda_a_std": 0.5,
    "bayes_rho_std": 0.1,
})

def compute_bayesian_features(odds_h: float, odds_d: float, odds_a: float,
                               open_h: float = None, open_d: float = None, open_a: float = None,
                               otsm_state: str = "ACTIVE",
                               otsm_lock_conf: float = 0.0,
                               otsm_entropy_drift: float = 0.0,
                               otsm_water_accel: float = 0.0,
                               inverter: BayesianOddsInverter = None) -> Dict[str, float]:
    """
    计算贝叶斯特征 (9维), 用于注入 v3.2 模型。

    这是生产级特征生成函数, 直接替代或增强当前的简单比例法。

    Args:
        odds_h, odds_d, odds_a: 最新赔率 (Interwetten)
        open_h, open_d, open_a: 开盘赔率 (用于漂移分析, 可选)
        otsm_*: OTSM 状态信号 (可选, 用于贝叶斯置信度调制)

    Returns:
        Dict[feature_name → value]
    """
    inv = inverter or BayesianOddsInverter()

    # 核心贝叶斯逆推
    result = inv.infer_map(odds_h, odds_d, odds_a)

    features = {
        'bayes_lambda_h_map': float(np.clip(result.posterior.lambda_h_map, 0.02, 5.0)),
        'bayes_lambda_a_map': float(np.clip(result.posterior.lambda_a_map, 0.02, 5.0)),
        'bayes_rho_map': float(np.clip(result.posterior.rho_map, -0.4, 0.2)),
        'bayes_lambda_h_std': float(np.clip(result.posterior.lambda_h_std, 0.0, 2.0)),
        'bayes_lambda_a_std': float(np.clip(result.posterior.lambda_a_std, 0.0, 2.0)),
        'bayes_rho_std': float(np.clip(result.posterior.rho_std, 0.0, 0.5)),
        'bayes_signal_strength': float(result.posterior.bayes_signal_strength),
        'bayes_draw_confidence': float(result.posterior.bayes_draw_confidence),
    }

    # 如果有开盘赔率, 计算漂移 KL
    if all(x is not None for x in [open_h, open_d, open_a]):
        integrator = OTSMDriftBayesianIntegrator(inv)
        integration = integrator.integrate(
            (open_h, open_d, open_a),
            (odds_h, odds_d, odds_a),
            otsm_state=otsm_state,
            otsm_lock_conf=otsm_lock_conf,
            otsm_entropy_drift=otsm_entropy_drift,
            otsm_water_accel=otsm_water_accel,
        )
        features['bayes_kl_divergence'] = float(np.clip(integration['kl_divergence'], 0.0, 1.0))
        features['bayes_draw_confidence'] = float(integration['bayes_confidence'])
    else:
        features['bayes_kl_divergence'] = 0.0

    return features

# ═══════════════════════════════════════════════════════════════════════════════
# 便捷API
# ═══════════════════════════════════════════════════════════════════════════════

def create_inverter(db_path: str = "data/football_data.db") -> BayesianOddsInverter:
    """创建贝叶斯逆推引擎"""
    return BayesianOddsInverter(db_path)

def quick_bayesian_invert(odds_h: float, odds_d: float, odds_a: float) -> dict:
    """
    快速贝叶斯逆推 (单次调用, 用于调试和探索)

    Returns: 字典含 λ 估计和后验不确定性
    """
    inv = BayesianOddsInverter()
    result = inv.infer_map(odds_h, odds_d, odds_a)

    return {
        'lambda_h': round(result.posterior.lambda_h_map, 3),
        'lambda_a': round(result.posterior.lambda_a_map, 3),
        'rho': round(result.posterior.rho_map, 4),
        'lambda_h_std': round(result.posterior.lambda_h_std, 3),
        'lambda_a_std': round(result.posterior.lambda_a_std, 3),
        'bayes_draw_conf': round(result.posterior.bayes_draw_confidence, 3),
        'signal_strength': round(result.posterior.bayes_signal_strength, 3),
        'prob_home': round(result.implied_probs[0], 4),
        'prob_draw': round(result.implied_probs[1], 4),
        'prob_away': round(result.implied_probs[2], 4),
        'prob_draw_ci95': (
            round(result.posterior.prob_draw_ci95[0], 4),
            round(result.posterior.prob_draw_ci95[1], 4),
        ),
        'det_residual': round(result.det_residual, 6),
    }

# ═══════════════════════════════════════════════════════════════════════════════
# 测试
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 70)
    print("贝叶斯赔率逆推引擎 — 测试")
    print("=" * 70)

    # 测试1: 基本逆推
    print("\n[测试1] 赔率→贝叶斯后验")
    print("-" * 40)

    # 模拟一场标准比赛: 主队略优
    test_odds = [
        (2.0, 3.4, 3.8),   # 典型主队优势
        (2.8, 3.1, 2.6),   # 接近比赛
        (1.6, 3.8, 5.5),   # 主队大热
        (3.8, 3.4, 2.0),   # 客队优势
    ]

    for oh, od, oa in test_odds:
        result = quick_bayesian_invert(oh, od, oa)
        print(f"\n赔率: H={oh:.2f} D={od:.2f} A={oa:.2f}")
        print(f"  λ_H={result['lambda_h']:.3f} ± {result['lambda_h_std']:.3f}")
        print(f"  λ_A={result['lambda_a']:.3f} ± {result['lambda_a_std']:.3f}")
        print(f"  ρ={result['rho']:.4f}")
        print(f"  平局后验: P(D)={result['prob_draw']:.4f} "
              f"95%CI=[{result['prob_draw_ci95'][0]:.4f},{result['prob_draw_ci95'][1]:.4f}]")
        print(f"  Bayes_Draw_Conf={result['bayes_draw_conf']:.3f}")

    # 测试2: OTSM 集成
    print("\n\n[测试2] OTSM-贝叶斯集成")
    print("-" * 40)

    open_odds = (2.2, 3.3, 3.4)
    close_odds = (2.0, 3.5, 3.8)

    inv = BayesianOddsInverter()
    integrator = OTSMDriftBayesianIntegrator(inv)
    integration = integrator.integrate(
        open_odds, close_odds,
        otsm_state="LOCKED",
        otsm_lock_conf=0.72,
        otsm_entropy_drift=-0.045,
        otsm_water_accel=-0.08,
    )

    print(f"开→收 KL散度: {integration['kl_divergence']:.6f}")
    print(f"漂移方向: {integration['drift_direction']}")
    print(f"贝叶斯置信度: {integration['bayes_confidence']:.3f}")
    print(f"平局概率偏移: {integration['draw_prob_shift']:+.4f}")

    # 测试3: 经验先验
    print("\n\n[测试3] 经验先验估计")
    print("-" * 40)
    print("(需要SQLite数据库, 如无则使用默认先验)")
    try:
        inv2 = BayesianOddsInverter("data/football_data.db")
        inv2.set_empirical_prior_from_db(sample_size=10000)
    except ImportError:
        print("  数据库模块未安装, 使用默认先验")
    except Exception as e:
        print(f"  数据库不可用 ({e}), 使用默认先验")

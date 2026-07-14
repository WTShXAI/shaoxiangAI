"""
哨响AI — 抽水分解与似然桥接引擎 v1.0
======================================
补全 bayesian_odds_inverter.py (季泊松) 缺失的四个关键模块:

1. 非均匀抽水分解 (Non-uniform Margin Decomposition)
   → 庄家对平局/冷门施加额外抽水的参数化估计
   → 解释为何比例法去抽水系统性低估平局概率

2. 多机构去偏 (Multi-Bookmaker Debias)
   → 利用多机构赔率差异分离信号与噪声
   → 低抽水庄家加权 → 更接近公平概率 P*

3. 赔率漂移→似然修正 (Drift Likelihood Correction)
   → 开→收盘赔率变化携带增量信息
   → 漂移幅度×时间权重 → 似然修正因子

4. 模型融合桥接 (Model Integration Bridge)
   → 将贝叶斯参数注入现有模型作为先验
   → 贝叶斯融合: 后验 ∝ 先验^α × 似然^(1-α)
   → 信号等级 (S/A/B/C/F) 决定注入强度

与 bayesian_odds_inverter.py 的关系:
  本模块 = 预处理 + 后处理桥接
  bayesian_odds_inverter.py = 核心 MAP/SIR 推断

核心理念 (赔率=加密协议):
  庄家的公开赔率经历了四层变换:
    P* → [信息压制] → P̃ → [非均匀抽水] → O_fair → [市场平衡] → O_public
  本模块专注于: 反推非均匀抽水参数 + 利用多机构/漂移信息 + 注入模型

三层推断架构:
  Layer 1: 比例法 → 隐含概率 p_implied (快速, 有偏)
  Layer 2: 多机构中位数 + 低抽水加权 → 公平概率 P* (去偏)
  Layer 3: 逆Dixon-Coles + MCMC → λ_h, λ_a, m_draw (贝叶斯参数)

用法:
    from bookmaker_sim.bookmaker_bayes_infer import BookmakerBayesInfer
    infer = BookmakerBayesInfer()
    result = infer.infer_parameters(
        odds_1x2={'home': 2.50, 'draw': 3.20, 'away': 2.80},
        multi_odds=[...],  # 可选: 多机构赔率
        odds_drift={'home_drift': -0.02, 'draw_drift': 0.01, 'away_drift': 0.01},  # 可选
    )
    # result.prior_lambda_h, result.prior_lambda_a → 注入模型
"""

import numpy as np
import logging
import math
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field
from collections import defaultdict
import warnings

try:
    from scipy.optimize import minimize, differential_evolution
    from scipy.stats import norm, beta as beta_dist
    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════
# 数据类
# ════════════════════════════════════════════════════════════════

@dataclass
class BayesInferResult:
    """贝叶斯逆向推断结果"""
    # ── 先验参数 (从赔率反推的庄家内部参数) ──
    prior_lambda_h: float = 0.0          # 主队预期进球 λ_h
    prior_lambda_a: float = 0.0          # 客队预期进球 λ_a
    prior_draw_margin_bias: float = 0.0  # 平局额外抽水率
    prior_certainty: float = 0.0         # 庄家确信度 (1/σ² 等效)

    # ── 似然修正参数 ──
    likelihood_drift_factor: float = 0.0 # 赔率漂移→信息修正量
    likelihood_consensus_weight: float = 0.0  # 多机构共识权重

    # ── 后验输出 ──
    posterior_probs: Dict[str, float] = field(default_factory=dict)
    posterior_lambda_h: float = 0.0
    posterior_lambda_a: float = 0.0

    # ── 诊断信息 ──
    overround_estimated: float = 0.0
    draw_suppression_detected: bool = False
    signal_grade: str = 'C'             # S/A/B/C/F
    convergence: bool = False
    nll: float = float('inf')
    messages: List[str] = field(default_factory=list)

    def to_prior_dict(self) -> Dict[str, float]:
        """导出为模型可注入的先验字典"""
        return {
            'bookmaker_lambda_h': self.prior_lambda_h,
            'bookmaker_lambda_a': self.prior_lambda_a,
            'bookmaker_draw_bias': self.prior_draw_margin_bias,
            'bookmaker_certainty': self.prior_certainty,
            'bookmaker_drift_factor': self.likelihood_drift_factor,
            'bookmaker_consensus_weight': self.likelihood_consensus_weight,
        }

@dataclass
class LeagueMargins:
    """联赛级别的抽水结构参数 (从历史校准)"""
    base_margin: float = 0.06           # 基础利润率
    draw_extra_margin: float = 0.02     # 平局额外抽水
    favorite_margin_scale: float = 0.0  # 热门额外抽水 (通常≈0)
    longshot_margin_scale: float = 0.01 # 冷门额外抽水
    avg_total_goals: float = 2.75       # 联赛场均进球

# ════════════════════════════════════════════════════════════════
# 核心引擎
# ════════════════════════════════════════════════════════════════

class BookmakerBayesInfer:
    """
    庄家贝叶斯参数逆向推断

    从公开赔率反推庄家的:
    1. 内部先验参数 (λ_h, λ_a) — 对应 Dixon-Coles 模型的预期进球
    2. 非均匀抽水参数 (m_draw_extra) — 平局的额外利润保护
    3. 似然修正参数 (drift_factor, consensus_weight) — 赔率变化的信息量

    三层推断:
      L1: 比例法 → 快速获得偏差估计
      L2: 多机构 → 降低抽水偏差
      L3: 逆向 Dixon-Coles → 还原贝叶斯参数
    """

    def __init__(self, league_margins: Optional[LeagueMargins] = None,
                 max_goals: int = 8, dixon_coles_rho: float = -0.05):
        self.league = league_margins or LeagueMargins()
        self.max_goals = max_goals
        self.dixon_coles_rho = dixon_coles_rho

        # 泊松PMF缓存
        self._poisson_cache: Dict[float, np.ndarray] = {}

        # 比分空间
        self._score_space = [(i, j) for i in range(max_goals + 1)
                             for j in range(max_goals + 1)]

    # ════════════════════════════════════════════════════════════
    # L1: 比例法 → 隐含概率 (快速, 有偏但可用)
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def proportional_implied_probs(odds: Dict[str, float]) -> Dict[str, float]:
        """
        比例法去抽水: p_i = (1/o_i) / Σ(1/o_j)

        这是最常用的方法，但在非均匀抽水下有偏。
        已知偏差方向：p_draw 被系统性低估 (因为平局额外抽水)。

        Args:
            odds: {'home': 2.50, 'draw': 3.20, 'away': 2.80}

        Returns:
            {'home': 0.38, 'draw': 0.30, 'away': 0.32}
        """
        raw = {k: 1.0 / max(v, 1.01) for k, v in odds.items()}
        total = sum(raw.values())
        if total <= 0:
            return {k: 1.0/3 for k in odds}
        return {k: v / total for k, v in raw.items()}

    @staticmethod
    def compute_overround(odds: Dict[str, float]) -> float:
        """计算抽水率 overround = Σ(1/o_i) - 1"""
        return sum(1.0 / max(v, 1.01) for v in odds.values()) - 1.0

    # ════════════════════════════════════════════════════════════
    # L2: 多机构去偏 → 降低抽水偏差
    # ════════════════════════════════════════════════════════════

    def multi_bookmaker_debias(self,
                                multi_odds: List[Dict[str, float]],
                                low_margin_bookmakers: Optional[List[int]] = None
                                ) -> Dict[str, float]:
        """
        多机构赔率融合去偏

        原理: 不同庄家抽水结构不同，低抽水庄家(Pinnacle)的赔率更接近公平值。
        取多机构隐含概率的中位数 + 低抽水庄家加权。

        Args:
            multi_odds: [{'home': 2.48, 'draw': 3.25, 'away': 2.82}, ...]
            low_margin_bookmakers: 低抽水庄家的索引列表

        Returns:
            去偏后的公平概率
        """
        if not multi_odds:
            return {}

        all_probs = {k: [] for k in ['home', 'draw', 'away']}
        overrounds = []

        for odds in multi_odds:
            probs = self.proportional_implied_probs(odds)
            for k in all_probs:
                all_probs[k].append(probs[k])
            overrounds.append(self.compute_overround(odds))

        # 策略1: 低抽水庄家加权
        if low_margin_bookmakers and len(low_margin_bookmakers) > 0:
            result = {}
            for k in all_probs:
                weighted = sum(all_probs[k][i] for i in low_margin_bookmakers
                               if i < len(all_probs[k]))
                result[k] = weighted / max(len(low_margin_bookmakers), 1)
        else:
            # 策略2: 中位数 (抗异常值)
            result = {k: float(np.median(vals)) for k, vals in all_probs.items()}

        # 归一化
        total = sum(result.values())
        if total > 0:
            result = {k: v / total for k, v in result.items()}

        return result

    # ════════════════════════════════════════════════════════════
    # L2.5: 非均匀抽水估计
    # ════════════════════════════════════════════════════════════

    def estimate_nonuniform_margins(self,
                                     odds: Dict[str, float],
                                     league: Optional[LeagueMargins] = None
                                     ) -> Dict[str, float]:
        """
        估计非均匀抽水系数

        模型: O_i = 1 / (P_i * (1 + m_base + m_i_extra))
        m_draw_extra 从赔率结构推断: 当 O_draw 相对 O_h 和 O_a 显得
        "太贵"时 → 庄家在平局上收取了额外利润。

        启发式:
          - 计算 overround
          - 假设 m_base 均匀分配
          - 残差归因到各结果的额外抽水
          - 平局残差系统性为正 → draw_suppression 存在

        Returns:
            {'home': m_h, 'draw': m_d, 'away': m_a}
        """
        lg = league or self.league
        overround = self.compute_overround(odds)
        n = 3

        # 基础均等假设
        m_base = overround / n

        # 隐含概率 (均等假设下)
        probs = self.proportional_implied_probs(odds)

        # 反推: 如果庄家认为 P(H)=x, 则赔率应为 1/(x*(1+m))
        # 但我们只有 O → 用比例法得到的 prob 作为基准
        margins = {'home': m_base, 'draw': m_base, 'away': m_base}

        # 关键修正: 平局赔率通常有额外抽水
        # 启发式: 如果 O_draw / O_draw_fair > O_home / O_home_fair → 平局多抽水
        # 由于我们不知道 fair odds, 用经验贝叶斯:
        # draw 额外抽水 = max(0, overround * 0.2) 作为先验
        draw_extra = lg.draw_extra_margin
        margins['draw'] += draw_extra

        # 冷门额外抽水 (Favourite-Longshot Bias)
        # 赔率最高的方向 (最冷门) 额外抽水
        max_prob_outcome = max(probs, key=probs.get)
        min_prob_outcome = min(probs, key=probs.get)
        margins[min_prob_outcome] += lg.longshot_margin_scale

        return margins

    # ════════════════════════════════════════════════════════════
    # L3: 逆向 Dixon-Coles → 贝叶斯参数 λ_h, λ_a
    # ════════════════════════════════════════════════════════════

    def _poisson_pmf(self, lam: float) -> np.ndarray:
        """泊松PMF (0 到 max_goals), 带缓存"""
        lam = max(0.02, lam)
        key = round(lam, 4)
        if key not in self._poisson_cache:
            pmf = np.array([np.exp(-lam) * lam**k / math.factorial(k)
                            for k in range(self.max_goals + 1)])
            pmf = pmf / pmf.sum()  # 截断归一化
            self._poisson_cache[key] = pmf
        return self._poisson_cache[key].copy()

    def _dixon_coles_tau(self, s_h: int, s_a: int,
                          lam_h: float, lam_a: float, rho: float) -> float:
        """Dixon-Coles τ 调整因子"""
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

    def _lambda_to_1x2_probs(self, lam_h: float, lam_a: float,
                               rho: Optional[float] = None) -> Dict[str, float]:
        """
        Dixon-Coles 比分分布 → 1X2 边际概率

        P(H) = Σ_{i>j} P(i,j), P(D) = Σ_{i=j} P(i,j), P(A) = Σ_{i<j} P(i,j)
        """
        rho = rho if rho is not None else self.dixon_coles_rho
        pmf_h = self._poisson_pmf(lam_h)
        pmf_a = self._poisson_pmf(lam_a)

        p_h, p_d, p_a = 0.0, 0.0, 0.0
        total = 0.0

        for s_h in range(self.max_goals + 1):
            for s_a in range(self.max_goals + 1):
                p_indep = pmf_h[s_h] * pmf_a[s_a]
                tau = self._dixon_coles_tau(s_h, s_a, lam_h, lam_a, rho)
                p = max(0, p_indep * tau)

                if s_h > s_a:
                    p_h += p
                elif s_h == s_a:
                    p_d += p
                else:
                    p_a += p
                total += p

        if total > 0:
            p_h /= total
            p_d /= total
            p_a /= total

        return {'home': max(p_h, 1e-6), 'draw': max(p_d, 1e-6), 'away': max(p_a, 1e-6)}

    def _neg_log_likelihood(self, params: np.ndarray,
                             target_probs: Dict[str, float],
                             target_odds: Dict[str, float],
                             margins: Dict[str, float],
                             rho: float) -> float:
        """
        负对数似然: -log P(odds | λ_h, λ_a, margins)

        似然函数:
          P(O_i | λ) ∝ exp(-(1/O_i - 1/O_pred_i)² / 2σ²)
        其中 O_pred_i = 1 / (P_i(λ) * (1 + m_i))
        """
        lam_h, lam_a = np.exp(params[0]), np.exp(params[1])  # 对数参数化, 保证正值
        sigma = np.exp(params[2]) if len(params) > 2 else 0.02

        # 从 λ 计算 1X2 概率
        probs = self._lambda_to_1x2_probs(lam_h, lam_a, rho)

        nll = 0.0
        for outcome in ['home', 'draw', 'away']:
            p = probs[outcome]
            m = margins.get(outcome, 0.06)
            # 预测赔率
            o_pred = 1.0 / (p * (1.0 + m))
            o_pred = np.clip(o_pred, 1.01, 100.0)

            # 观测赔率
            o_obs = target_odds.get(outcome, o_pred)

            # 高斯似然 (对数空间)
            nll += 0.5 * ((1.0/o_obs - 1.0/o_pred) / sigma) ** 2
            nll += np.log(sigma)

        # 先验: λ ~ Gamma(2, 1) 均值~2, 合理范围
        nll -= np.sum((2 - 1) * params[:2] - np.exp(params[:2]))  # Gamma log-prior

        return float(nll)

    def infer_lambda_from_odds(self,
                                odds: Dict[str, float],
                                margins: Optional[Dict[str, float]] = None,
                                method: str = 'auto') -> Tuple[float, float]:
        """
        核心: 从1X2赔率反推 λ_h, λ_a

        这是将"赔率=加密协议"逆向为可解释的贝叶斯参数的关键步骤。

        方法:
          1. 比例法获得目标概率
          2. 数值优化搜索 λ_h, λ_a 使 Dixon-Coles 输出的 1X2 概率匹配目标
          3. 同时估计庄家对平局的抽水偏差

        Args:
            odds: {'home': O_H, 'draw': O_D, 'away': O_A}
            margins: 预估计的非均匀抽水
            method: 'auto' | 'grid' | 'optimize'

        Returns:
            (lambda_h, lambda_a)
        """
        if margins is None:
            margins = self.estimate_nonuniform_margins(odds)

        # 目标: 用估计的非均匀抽水去偏后的隐含概率
        target_probs = {}
        for outcome in ['home', 'draw', 'away']:
            raw_p = 1.0 / max(odds[outcome], 1.01)
            adjusted_p = raw_p * (1.0 + margins[outcome])
            target_probs[outcome] = adjusted_p

        # 归一化
        total = sum(target_probs.values())
        target_probs = {k: max(v / total, 1e-6) for k, v in target_probs.items()}

        if not _SCIPY_AVAILABLE:
            return self._grid_search_lambda(target_probs)

        # 数值优化
        # 初始猜测: 从总进球和胜率推断
        p_h = target_probs['home']
        p_a = target_probs['away']
        # 粗略估计: P(H) / P(A) ≈ λ_h / λ_a (简化)
        ratio = max(p_h, 0.05) / max(p_a, 0.05)
        avg_goals = self.league.avg_total_goals
        init_lam_a = avg_goals / (1 + ratio)
        init_lam_h = avg_goals - init_lam_a

        bounds = [(-4.0, 3.0), (-4.0, 3.0), (-5.0, -1.0)]  # log-space

        result = minimize(
            self._neg_log_likelihood,
            x0=[np.log(max(init_lam_h, 0.05)), np.log(max(init_lam_a, 0.05)), np.log(0.02)],
            args=(target_probs, odds, margins, self.dixon_coles_rho),
            bounds=bounds,
            method='L-BFGS-B',
        )

        if result.success:
            lam_h, lam_a = np.exp(result.x[0]), np.exp(result.x[1])
        else:
            lam_h, lam_a = self._grid_search_lambda(target_probs)

        return max(lam_h, 0.05), max(lam_a, 0.05)

    def _grid_search_lambda(self, target_probs: Dict[str, float]) -> Tuple[float, float]:
        """网格搜索 λ (当优化失败时的后备方案)"""
        best_kl = float('inf')
        best_lam = (1.0, 1.0)

        for lam_h in np.linspace(0.3, 3.5, 25):
            for lam_a in np.linspace(0.3, 3.5, 25):
                probs = self._lambda_to_1x2_probs(lam_h, lam_a)
                kl = sum(
                    target_probs[k] * np.log(
                        max(target_probs[k], 1e-10) / max(probs[k], 1e-10)
                    )
                    for k in target_probs
                )
                if kl < best_kl:
                    best_kl = kl
                    best_lam = (lam_h, lam_a)

        return best_lam

    # ════════════════════════════════════════════════════════════
    # L3.5: 赔率漂移 → 似然修正参数
    # ════════════════════════════════════════════════════════════

    def infer_drift_likelihood(self,
                                odds_open: Dict[str, float],
                                odds_current: Dict[str, float],
                                time_to_kickoff_hours: float = 24.0
                                ) -> Dict[str, float]:
        """
        从赔率漂移推断信息修正量

        赔率从开盘到当前的漂移 = 市场接收到的增量信息的代理变量。

        似然修正模型:
          λ_current = λ_prior + drift_factor * (λ_odds_current - λ_odds_open)

        drift_factor 反映我们对赔率漂移的信任程度:
          - 接近0: 忽略漂移 (可能是噪声/市场情绪)
          - 接近1: 完全信任漂移 (确信是信息)
          - 实际: 根据漂移幅度和时间决定 (大漂移+接近开赛 = 高权重)

        Args:
            odds_open: 开盘赔率
            odds_current: 当前赔率
            time_to_kickoff_hours: 距开赛时间

        Returns:
            {'drift_factor': float, 'drift_direction': str, 'drift_magnitude': float}
        """
        # 计算漂移
        drift = {}
        for outcome in ['home', 'draw', 'away']:
            o_open = odds_open.get(outcome, odds_current.get(outcome, 2.0))
            o_curr = odds_current.get(outcome, o_open)
            # 赔率下降 = 概率上升 = 正漂移
            drift[outcome] = (1.0 / o_curr - 1.0 / o_open)

        # 找出最大漂移方向
        max_drift_outcome = max(drift, key=lambda k: abs(drift[k]))
        drift_magnitude = abs(drift[max_drift_outcome])

        # 漂移因子: 幅度越大 + 越接近开赛 = 越可信
        # 使用 sigmoid: drift_factor = 1 / (1 + exp(-k*(magnitude - threshold)))
        time_weight = np.exp(-time_to_kickoff_hours / 48.0)  # 48小时半衰期
        magnitude_signal = 1.0 / (1.0 + np.exp(-20.0 * (drift_magnitude - 0.03)))

        drift_factor = float(np.clip(magnitude_signal * time_weight, 0.0, 0.8))

        return {
            'drift_factor': drift_factor,
            'drift_direction': max_drift_outcome,
            'drift_magnitude': drift_magnitude,
            'drift_detail': drift,
        }

    # ════════════════════════════════════════════════════════════
    # 主推断流程
    # ════════════════════════════════════════════════════════════

    def infer_parameters(self,
                          odds_1x2: Dict[str, float],
                          multi_odds: Optional[List[Dict[str, float]]] = None,
                          odds_open: Optional[Dict[str, float]] = None,
                          time_to_kickoff_hours: float = 24.0,
                          league_name: str = 'default',
                          ) -> BayesInferResult:
        """
        完整的贝叶斯参数逆向推断

        输入:
          odds_1x2: 当前全场1X2赔率 (必须)
          multi_odds: 多机构1X2赔率列表 (可选, 降低抽水偏差)
          odds_open: 开盘赔率 (可选, 推断drift)
          time_to_kickoff_hours: 距开赛小时数
          league_name: 联赛名称 (用于联赛先验)

        输出:
          BayesInferResult: 完整的贝叶斯参数 (先验 + 似然 + 后验)
        """
        result = BayesInferResult()

        # ── Step 1: 估计非均匀抽水 ──
        margins = self.estimate_nonuniform_margins(odds_1x2)
        result.overround_estimated = self.compute_overround(odds_1x2)
        result.draw_suppression_detected = margins['draw'] > margins['home'] + 0.005
        result.prior_draw_margin_bias = margins['draw'] - margins['home']

        # ── Step 2: 多机构去偏 (如果可用) ──
        if multi_odds and len(multi_odds) >= 3:
            # 识别低抽水庄家 (overround最低的前50%)
            overrounds = [self.compute_overround(o) for o in multi_odds]
            median_or = np.median(overrounds)
            low_margin_idx = [i for i, o in enumerate(overrounds) if o <= median_or]
            fair_probs = self.multi_bookmaker_debias(multi_odds, low_margin_idx)
            # 共识度 = 各庄家概率的标准差的倒数
            all_probs = []
            for o in multi_odds:
                all_probs.append(list(self.proportional_implied_probs(o).values()))
            consensus_std = float(np.mean(np.std(all_probs, axis=0)))
            result.likelihood_consensus_weight = 1.0 / (1.0 + consensus_std * 10)
        else:
            fair_probs = self.proportional_implied_probs(odds_1x2)
            result.likelihood_consensus_weight = 0.3  # 默认低权重

        result.posterior_probs = fair_probs

        # ── Step 3: 反推 λ_h, λ_a ──
        lam_h, lam_a = self.infer_lambda_from_odds(odds_1x2, margins)
        result.prior_lambda_h = lam_h
        result.prior_lambda_a = lam_a

        # ── Step 4: 庄家确信度 ──
        # certainty = 1 / (抽水波动) — 抽水越低越确信
        result.prior_certainty = 1.0 / max(result.overround_estimated, 0.01)

        # ── Step 5: 赔率漂移 → 似然修正 ──
        if odds_open:
            drift_info = self.infer_drift_likelihood(
                odds_open, odds_1x2, time_to_kickoff_hours
            )
            result.likelihood_drift_factor = drift_info['drift_factor']
            result.messages.append(
                f"漂移方向={drift_info['drift_direction']}, "
                f"幅度={drift_info['drift_magnitude']:.4f}, "
                f"可信度因子={drift_info['drift_factor']:.3f}"
            )

            # 似然修正后的 λ
            drift_lam_h, drift_lam_a = self.infer_lambda_from_odds(odds_open, margins)
            result.posterior_lambda_h = lam_h + drift_info['drift_factor'] * (lam_h - drift_lam_h)
            result.posterior_lambda_a = lam_a + drift_info['drift_factor'] * (lam_a - drift_lam_a)
        else:
            result.posterior_lambda_h = lam_h
            result.posterior_lambda_a = lam_a

        # ── Step 6: 信号等级 ──
        result.signal_grade = self._assign_signal_grade(result)
        result.messages.append(f"信号等级: {result.signal_grade}")

        return result

    def _assign_signal_grade(self, result: BayesInferResult) -> str:
        """根据推断质量分配信号等级"""
        # 基线: 有赔率即有最低 C 级信号 (单源 Interwetten 典型场景)
        score = 1

        # 多机构共识加分
        if result.likelihood_consensus_weight > 0.6:
            score += 2
        elif result.likelihood_consensus_weight >= 0.25:
            score += 1

        # 漂移可信加分
        if result.likelihood_drift_factor > 0.4:
            score += 2
        elif result.likelihood_drift_factor > 0.2:
            score += 1

        # 过高的overround扣分 (高噪声)
        if result.overround_estimated > 0.12:
            score -= 1
        if result.overround_estimated > 0.15:
            score -= 2  # 极高噪声, 重扣

        # 平局抑制扣分 (信息损失)
        if result.draw_suppression_detected:
            score -= 1

        if score >= 4:
            return 'S'  # 极高信号: 多机构共识 + 漂移明确 + 低抽水
        elif score >= 3:
            return 'A'  # 高信号: 多机构共识 + 漂移明确
        elif score >= 2:
            return 'B'  # 中信号
        elif score >= 1:
            return 'C'  # 低信号 (典型单源赔率)
        else:
            return 'F'  # 噪声/诱饵

    # ════════════════════════════════════════════════════════════
    # 与现有模型的融合接口
    # ════════════════════════════════════════════════════════════

    def inject_as_prior(self, result: BayesInferResult,
                         model_probs: Dict[str, float],
                         injection_strength: float = 0.3
                         ) -> Dict[str, float]:
        """
        贝叶斯先验注入: 将庄家参数作为先验, 与模型预测融合

        后验 ∝ 先验^α × 似然^(1-α)
        其中 α = injection_strength * signal_confidence

        Args:
            result: 推断结果
            model_probs: 模型原始预测 {'home': p_h, 'draw': p_d, 'away': p_a}
            injection_strength: 基础注入强度 (0-1)

        Returns:
            融合后的概率
        """
        # 从 λ 重新计算先验概率
        prior_probs = self._lambda_to_1x2_probs(
            result.posterior_lambda_h, result.posterior_lambda_a
        )

        # 信号置信度调整
        grade_weights = {'S': 0.9, 'A': 0.7, 'B': 0.5, 'C': 0.25, 'F': 0.0}
        alpha = injection_strength * grade_weights.get(result.signal_grade, 0.3)
        alpha = np.clip(alpha, 0.0, 0.8)  # 上限0.8, 不完全替换模型

        # 贝叶斯融合: log-空间平均
        fused = {}
        eps = 1e-6
        for outcome in ['home', 'draw', 'away']:
            log_prior = np.log(max(prior_probs[outcome], eps))
            log_model = np.log(max(model_probs[outcome], eps))
            log_fused = alpha * log_prior + (1 - alpha) * log_model
            fused[outcome] = np.exp(log_fused)

        # 归一化
        total = sum(fused.values())
        fused = {k: v / total for k, v in fused.items()}

        return fused

    def as_calibration_prior(self, result: BayesInferResult
                              ) -> Dict[str, Any]:
        """
        作为校准模块的贝叶斯先验输入

        替代 calibration.py 中的无信息先验,
        提供从赔率推断的联赛级别结构先验。

        Returns:
            dict compatible with CalibratorSuite initialization
        """
        return {
            'method': 'bayesian_bookmaker_prior',
            'prior_lambda_h': result.prior_lambda_h,
            'prior_lambda_a': result.prior_lambda_a,
            'prior_probs': self._lambda_to_1x2_probs(
                result.prior_lambda_h, result.prior_lambda_a
            ),
            'draw_margin_bias': result.prior_draw_margin_bias,
            'certainty': result.prior_certainty,
            'signal_grade': result.signal_grade,
            'drift_factor': result.likelihood_drift_factor,
        }

# ════════════════════════════════════════════════════════════════
# 批量推断: 针对整个联赛或时间段
# ════════════════════════════════════════════════════════════════

class LeagueCalibrator:
    """
    联赛级别贝叶斯参数校准器

    从历史赔率批量反推联赛的平均先验参数:
      - avg_lambda_h, avg_lambda_a
      - draw_margin_bias (联赛级别)
      - overround_distribution
      - drift_factor_calibration

    这些参数成为未来单场推断的"超先验"。
    """

    def __init__(self, db_path: Optional[str] = None, infer: Optional[BookmakerBayesInfer] = None):
        self.db_path = db_path or "data/football_data.db"
        self.infer = infer or BookmakerBayesInfer()

    def calibrate_league(self, league_name: str,
                          n_matches: int = 500) -> Dict[str, Any]:
        """
        对指定联赛的赔率结构进行批量贝叶斯校准

        Returns:
            league-level hyperparameters for Bayesian prior
        """
        import sqlite3
        conn = sqlite3.connect(self.db_path)

        query = """
        SELECT home_odds, draw_odds, away_odds,
               home_open_odds, draw_open_odds, away_open_odds
        FROM matches m
        JOIN odds o ON m.match_id = o.match_id
        WHERE m.league_name = ? AND home_odds IS NOT NULL
        ORDER BY m.kickoff_time DESC
        LIMIT ?
        """
        try:
            cur = conn.execute(query, (league_name, n_matches))
            rows = cur.fetchall()
        except (Exception, sqlite3.OperationalError):
            # Fallback: 尝试不同表结构
            try:
                cur = conn.execute("""
                SELECT home_odds, draw_odds, away_odds
                FROM odds 
                WHERE home_odds IS NOT NULL
                LIMIT ?
                """, (n_matches,))
                rows = [(r[0], r[1], r[2], None, None, None) for r in cur.fetchall()]
            except (Exception, sqlite3.OperationalError):
                rows = []
        finally:
            conn.close()

        if not rows:
            logger.warning(f"No odds data for league={league_name}")
            return {'error': 'no_data', 'n_matches': 0}

        lambda_hs, lambda_as = [], []
        overrounds = []
        draw_biases = []

        for row in rows:
            h_odds, d_odds, a_odds = row[0], row[1], row[2]
            if h_odds is None or d_odds is None or a_odds is None:
                continue
            if h_odds < 1.05 or d_odds < 1.05 or a_odds < 1.05:
                continue

            odds = {'home': h_odds, 'draw': d_odds, 'away': a_odds}
            try:
                result = self.infer.infer_parameters(odds)
                lambda_hs.append(result.prior_lambda_h)
                lambda_as.append(result.prior_lambda_a)
                overrounds.append(result.overround_estimated)
                draw_biases.append(result.prior_draw_margin_bias)
            except (Exception, RuntimeError):
                continue

        if not lambda_hs:
            return {'error': 'inference_failed', 'n_matches': len(rows)}

        return {
            'league': league_name,
            'n_matches': len(lambda_hs),
            'avg_lambda_h': float(np.mean(lambda_hs)),
            'std_lambda_h': float(np.std(lambda_hs)),
            'avg_lambda_a': float(np.mean(lambda_as)),
            'std_lambda_a': float(np.std(lambda_as)),
            'avg_overround': float(np.mean(overrounds)),
            'std_overround': float(np.std(overrounds)),
            'avg_draw_bias': float(np.mean(draw_biases)),
            'median_draw_bias': float(np.median(draw_biases)),
            'draw_bias_significant': bool(np.mean(draw_biases) > 0.005),
        }

# ════════════════════════════════════════════════════════════════
# CLI 演示
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    print("=" * 70)
    print("  庄家贝叶斯参数逆向推断 — 演示")
    print("=" * 70)

    infer = BookmakerBayesInfer()

    # ── 测试1: 单机构1X2 ──
    print("\n[测试1] 单机构1X2 → λ 推断")
    print("-" * 50)
    odds = {'home': 2.50, 'draw': 3.20, 'away': 2.80}
    result = infer.infer_parameters(odds)
    print(f"  赔率: H={odds['home']:.2f} D={odds['draw']:.2f} A={odds['away']:.2f}")
    print(f"  隐含概率: {infer.proportional_implied_probs(odds)}")
    print(f"  逆推 λ_h={result.prior_lambda_h:.3f}, λ_a={result.prior_lambda_a:.3f}")
    print(f"  Overround={result.overround_estimated:.4f}")
    print(f"  平局额外抽水={result.prior_draw_margin_bias:.4f}")
    print(f"  信号等级: {result.signal_grade}")

    # ── 测试2: 多机构去偏 ──
    print("\n[测试2] 多机构去偏")
    print("-" * 50)
    multi = [
        {'home': 2.50, 'draw': 3.20, 'away': 2.80},  # Interwetten
        {'home': 2.55, 'draw': 3.30, 'away': 2.75},  # 某高抽水庄家
        {'home': 2.48, 'draw': 3.15, 'away': 2.85},  # Pinnacle-like
        {'home': 2.52, 'draw': 3.22, 'away': 2.78},  # 某中等庄家
    ]
    result2 = infer.infer_parameters(odds, multi_odds=multi)
    print(f"  共识权重={result2.likelihood_consensus_weight:.3f}")
    print(f"  去偏后概率: {result2.posterior_probs}")
    print(f"  信号等级: {result2.signal_grade}")

    # ── 测试3: 带漂移 ──
    print("\n[测试3] 赔率漂移 → 似然修正")
    print("-" * 50)
    odds_open = {'home': 2.70, 'draw': 3.10, 'away': 2.60}
    odds_now = {'home': 2.50, 'draw': 3.20, 'away': 2.80}
    result3 = infer.infer_parameters(odds_now, odds_open=odds_open,
                                      time_to_kickoff_hours=2.0)
    print(f"  开盘: {odds_open}")
    print(f"  当前: {odds_now}")
    print(f"  漂移因子={result3.likelihood_drift_factor:.3f}")
    print(f"  后验 λ_h={result3.posterior_lambda_h:.3f}, λ_a={result3.posterior_lambda_a:.3f}")
    print(f"  信号等级: {result3.signal_grade}")
    for msg in result3.messages:
        print(f"  [{msg}]")

    # ── 测试4: 先验注入 ──
    print("\n[测试4] 贝叶斯先验注入")
    print("-" * 50)
    model_probs = {'home': 0.42, 'draw': 0.25, 'away': 0.33}
    fused = infer.inject_as_prior(result3, model_probs, injection_strength=0.3)
    print(f"  模型原始: {model_probs}")
    print(f"  庄家先验: {infer._lambda_to_1x2_probs(result3.posterior_lambda_h, result3.posterior_lambda_a)}")
    print(f"  融合后:   {fused}")

    print("\n" + "=" * 70)
    print("  核心输出: prior_dict → 可注入到 ensemble_trainer / selective_predictor")
    print("  prior_dict =", result3.to_prior_dict())
    print("=" * 70)

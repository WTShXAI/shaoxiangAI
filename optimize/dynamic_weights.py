#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
哨响AI — T03 动态权重系统
================================
基于比赛上下文特征实时调整 XGBoost / Ridge / Heuristic 集成权重。

设计思路:
  - 静态权重 = 全局最优（Optuna 搜索）
  - 动态调整 = 在静态权重基础上，根据比赛特征施加 ± 修正
  - 最终权重 = normalize(w_base × (1 + Σ adjustment_factors))
  - 严格裁剪 + 归一化，防止极端情况

调整因子:
  1. 盘口波动 (sigma_trap)      — 高波动 → 降低 XGBoost, 提升启发式
  2. 实力差 (rank_diff_factor)   — 实力悬殊 → 提升 XGBoost (数据驱动更准)
  3. 交锋历史 (h2h_factor)       — H2H 明确 → 提升启发式
  4. 盘口偏差 (beta_dev)         — 市场异常 → 降低 Ridge
  5. 模型一致性 (a4)             — 背离时 → 偏向最稳定模型 (XGBoost)
  6. 联赛类型 (league)           — 不同联赛不同基础权重

用法:
    calc = DynamicWeightCalculator(config_dict)
    weights = calc.compute(features, league_name='Premier League')
    # weights = {'xgboost': 0.72, 'ridge': 0.04, 'heuristic': 0.24}
"""

import logging
import numpy as np
from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass, field

logger = logging.getLogger("DynamicWeights")

# ── 联赛特征统计 (用于联赛级权重微调) ──
# 来源: 历史数据统计 + 经验
LEAGUE_PROFILES: Dict[str, Dict[str, float]] = {
    # 联赛名 -> {draw_rate, home_advantage, avg_goals, volatility}
    'Premier League':        {'draw_rate': 0.253, 'home_adv': 0.42, 'avg_goals': 2.75, 'volatility': 0.65},
    'Bundesliga':            {'draw_rate': 0.243, 'home_adv': 0.46, 'avg_goals': 3.03, 'volatility': 0.62},
    'Serie A':               {'draw_rate': 0.264, 'home_adv': 0.40, 'avg_goals': 2.68, 'volatility': 0.58},
    'Ligue 1':               {'draw_rate': 0.278, 'home_adv': 0.38, 'avg_goals': 2.63, 'volatility': 0.56},
    'La Liga':               {'draw_rate': 0.255, 'home_adv': 0.44, 'avg_goals': 2.62, 'volatility': 0.60},
    'Primeira Liga':         {'draw_rate': 0.268, 'home_adv': 0.45, 'avg_goals': 2.48, 'volatility': 0.55},
    'Eredivisie':            {'draw_rate': 0.231, 'home_adv': 0.43, 'avg_goals': 3.10, 'volatility': 0.68},
    'Championship':          {'draw_rate': 0.272, 'home_adv': 0.41, 'avg_goals': 2.42, 'volatility': 0.63},
    'MLS':                   {'draw_rate': 0.259, 'home_adv': 0.48, 'avg_goals': 2.85, 'volatility': 0.72},
    'Jupiler Pro League':    {'draw_rate': 0.255, 'home_adv': 0.41, 'avg_goals': 2.78, 'volatility': 0.64},
    'Brasileirão':           {'draw_rate': 0.270, 'home_adv': 0.49, 'avg_goals': 2.38, 'volatility': 0.67},
    'UEFA Champions League': {'draw_rate': 0.248, 'home_adv': 0.40, 'avg_goals': 2.76, 'volatility': 0.66},
    'Liga Profesional':      {'draw_rate': 0.275, 'home_adv': 0.44, 'avg_goals': 2.22, 'volatility': 0.59},
    # 兜底
    'default':               {'draw_rate': 0.261, 'home_adv': 0.43, 'avg_goals': 2.65, 'volatility': 0.63},
}

@dataclass
class WeightAdjustment:
    """单次权重调整的完整记录"""
    base_weights: Dict[str, float]
    factors: Dict[str, float]
    alphas: Dict[str, float]       # 每个模型的调整系数 Σ
    adjusted_weights: Dict[str, float]
    league: str
    sigma_trap: float
    rank_diff: float
    h2h_factor: float
    beta_dev: float
    a4: float

class DynamicWeightCalculator:
    """
    动态权重计算器

    根据比赛上下文特征，在静态最优权重基础上施加调整。

    调整算法:
      α_i = Σ (k_j × context_factor_j)  for each model i
      w_i = w_base_i × (1 + clamp(α_i, -0.30, +0.30))
      w_i = normalize(w_i)

    受保护范围:
      - 单个模型权重 ∈ [0.02, 0.85]
      - 调整幅度 ∈ [-30%, +30%] (相对于基础权重)
    """

    # ── 默认敏感度系数 ──
    # XGBoost 调整系数
    K_XGB_RANK_DIFF  = 0.25   # |rank_diff| 越大 → XGBoost 越可靠
    K_XGB_SIGMA      = 0.15   # sigma_trap 越大 → XGBoost 越不可靠
    K_XGB_CONSENSUS  = 0.12   # 模型偏离大 → 倾向保守(降低XGB)

    # Ridge 调整系数
    K_RIDGE_BETA_DEV  = 0.20  # beta_dev 越大 → Ridge 越不可靠
    K_RIDGE_RANK_DIFF  = 0.10  # 实力差距大 → Ridge 目标差预测更可靠
    K_RIDGE_SIGMA     = 0.10  # 波动大 → Ridge 略降

    # Heuristic 调整系数
    K_HEUR_H2H       = 0.22   # H2H 信号明确 → 启发式权重提升
    K_HEUR_SIGMA     = 0.18   # 市场不稳定 → 启发式更稳定
    K_HEUR_RANK_DIFF = 0.15   # 实力差距大 → 启发式略降(数据驱动更好)
    K_HEUR_CONSENSUS = 0.10   # 模型偏离大 → 启发式也可能不可靠

    def __init__(self, config: Optional[Dict] = None):
        """
        Args:
            config: 完整 config.yaml 字典，或包含 models.dynamic_weights 的部分
        """
        self.config = config or {}

        # 加载基础权重（静态最优）
        ens_cfg = self.config.get('models', {}).get('ensemble', {})
        self.w_xgb_base = ens_cfg.get('xgboost_weight', 0.69)
        self.w_ridge_base = ens_cfg.get('ridge_weight', 0.05)
        self.w_heuristic_base = ens_cfg.get('heuristic_weight', 0.26)

        # 加载动态权重参数（可覆盖）
        dyn_cfg = self.config.get('models', {}).get('dynamic_weights', {})
        self.enabled = dyn_cfg.get('enabled', True)
        self.max_adjustment = dyn_cfg.get('max_adjustment', 0.30)  # 最大 ±30%
        self.min_weight = dyn_cfg.get('min_weight', 0.02)
        self.max_weight = dyn_cfg.get('max_weight', 0.85)
        self.use_league_profile = dyn_cfg.get('use_league_profile', True)

        # 覆盖系数（如果配置中有）
        coeff = dyn_cfg.get('coefficients', {})
        self.K_XGB_RANK_DIFF  = coeff.get('xgb_rank_diff',  self.K_XGB_RANK_DIFF)
        self.K_XGB_SIGMA      = coeff.get('xgb_sigma',       self.K_XGB_SIGMA)
        self.K_XGB_CONSENSUS  = coeff.get('xgb_consensus',   self.K_XGB_CONSENSUS)
        self.K_RIDGE_BETA_DEV = coeff.get('ridge_beta_dev',  self.K_RIDGE_BETA_DEV)
        self.K_RIDGE_RANK_DIFF = coeff.get('ridge_rank_diff', self.K_RIDGE_RANK_DIFF)
        self.K_RIDGE_SIGMA    = coeff.get('ridge_sigma',     self.K_RIDGE_SIGMA)
        self.K_HEUR_H2H       = coeff.get('heur_h2h',        self.K_HEUR_H2H)
        self.K_HEUR_SIGMA     = coeff.get('heur_sigma',      self.K_HEUR_SIGMA)
        self.K_HEUR_RANK_DIFF = coeff.get('heur_rank_diff',  self.K_HEUR_RANK_DIFF)
        self.K_HEUR_CONSENSUS = coeff.get('heur_consensus',  self.K_HEUR_CONSENSUS)

        # 联赛级基础权重偏移
        self.league_offsets = dyn_cfg.get('league_offsets', {})

        logger.info(f"DynamicWeightCalculator 初始化: enabled={self.enabled}, "
                    f"base=({self.w_xgb_base:.3f}, {self.w_ridge_base:.3f}, {self.w_heuristic_base:.3f}), "
                    f"max_adj=±{self.max_adjustment:.0%}")

    # ──────────────────────────────────────────────
    # 公共 API
    # ──────────────────────────────────────────────

    def compute(self, features: Dict[str, float],
                league_name: str = 'default') -> Dict[str, float]:
        """
        计算单场比赛的动态权重。

        Args:
            features: 特征字典，至少包含:
                      sigma_trap, rank_diff_factor, h2h_factor,
                      beta_dev, a4 (可选)
            league_name: 联赛名称，触发联赛级基础权重微调

        Returns:
            {'xgboost': 0.72, 'ridge': 0.04, 'heuristic': 0.24}
        """
        if not self.enabled:
            return {
                'xgboost': self.w_xgb_base,
                'ridge': self.w_ridge_base,
                'heuristic': self.w_heuristic_base,
            }

        # 提取上下文因子
        sigma_trap = features.get('sigma_trap', 0.0)
        rank_diff = features.get('rank_diff_factor', 0.0)
        h2h = features.get('h2h_factor', 0.0)
        beta_dev = features.get('beta_dev', 0.0)
        a4 = features.get('a4', 0.0)

        # 获取联赛调整后的基础权重
        w_xgb, w_ridge, w_heuristic = self._get_base_with_league(league_name)

        # 计算每个模型的调整系数 α
        alpha_xgb = self._calc_alpha_xgb(rank_diff, sigma_trap, a4)
        alpha_ridge = self._calc_alpha_ridge(beta_dev, rank_diff, sigma_trap)
        alpha_heur = self._calc_alpha_heur(h2h, sigma_trap, rank_diff, a4)

        # 应用调整
        adj_xgb = w_xgb * (1.0 + self._clamp(alpha_xgb, -self.max_adjustment, self.max_adjustment))
        adj_ridge = w_ridge * (1.0 + self._clamp(alpha_ridge, -self.max_adjustment, self.max_adjustment))
        adj_heur = w_heuristic * (1.0 + self._clamp(alpha_heur, -self.max_adjustment, self.max_adjustment))

        # 裁剪 + 归一化
        w_xgb_adj = np.clip(adj_xgb, self.min_weight, self.max_weight)
        w_ridge_adj = np.clip(adj_ridge, self.min_weight, self.max_weight)
        w_heur_adj = np.clip(adj_heur, self.min_weight, self.max_weight)

        total = w_xgb_adj + w_ridge_adj + w_heur_adj
        if total <= 0:
            # 回退到基础权重
            logger.warning("动态权重计算出错(总权重=0)，回退到静态权重")
            return {
                'xgboost': w_xgb, 'ridge': w_ridge, 'heuristic': w_heuristic
            }

        w_xgb_final = round(w_xgb_adj / total, 4)
        w_ridge_final = round(w_ridge_adj / total, 4)
        w_heuristic_final = round(w_heur_adj / total, 4)

        logger.debug(
            f"动态权重: {league_name} | σ={sigma_trap:.3f} ΔR={rank_diff:.3f} "
            f"H2H={h2h:.3f} β={beta_dev:.3f} α4={a4:.3f} | "
            f"({w_xgb:.3f},{w_ridge:.3f},{w_heuristic:.3f}) → "
            f"({w_xgb_final:.3f},{w_ridge_final:.3f},{w_heuristic_final:.3f})"
        )

        return {
            'xgboost': w_xgb_final,
            'ridge': w_ridge_final,
            'heuristic': w_heuristic_final,
        }

    def compute_with_details(self, features: Dict[str, float],
                             league_name: str = 'default') -> WeightAdjustment:
        """
        计算动态权重并返回完整调整详情（用于调试/报告）。
        """
        sigma_trap = features.get('sigma_trap', 0.0)
        rank_diff = features.get('rank_diff_factor', 0.0)
        h2h = features.get('h2h_factor', 0.0)
        beta_dev = features.get('beta_dev', 0.0)
        a4 = features.get('a4', 0.0)

        w_base = self._get_base_with_league(league_name)
        base_weights = {
            'xgboost': w_base[0], 'ridge': w_base[1], 'heuristic': w_base[2]
        }

        alpha_xgb = self._calc_alpha_xgb(rank_diff, sigma_trap, a4)
        alpha_ridge = self._calc_alpha_ridge(beta_dev, rank_diff, sigma_trap)
        alpha_heur = self._calc_alpha_heur(h2h, sigma_trap, rank_diff, a4)

        weights = self.compute(features, league_name)

        return WeightAdjustment(
            base_weights=base_weights,
            factors={
                'sigma_trap': sigma_trap,
                'rank_diff': rank_diff,
                'h2h_factor': h2h,
                'beta_dev': beta_dev,
                'a4': a4,
            },
            alphas={'xgb': round(alpha_xgb, 4), 'ridge': round(alpha_ridge, 4), 'heur': round(alpha_heur, 4)},
            adjusted_weights=weights,
            league=league_name,
            sigma_trap=sigma_trap,
            rank_diff=rank_diff,
            h2h_factor=h2h,
            beta_dev=beta_dev,
            a4=a4,
        )

    def compute_batch(self, features_list: list,
                      league_names: list = None) -> np.ndarray:
        """
        批量计算动态权重。

        Args:
            features_list: 特征字典列表
            league_names: 联赛名列表（可选）

        Returns:
            np.ndarray shape (n, 3): 每行 [w_xgb, w_ridge, w_heuristic]
        """
        n = len(features_list)
        weights = np.zeros((n, 3))
        for i, feat in enumerate(features_list):
            league = league_names[i] if league_names else 'default'
            w = self.compute(feat, league)
            weights[i] = [w['xgboost'], w['ridge'], w['heuristic']]
        return weights

    def explain(self, features: Dict[str, float],
                league_name: str = 'default') -> str:
        """
        生成可读的权重调整解释。
        """
        detail = self.compute_with_details(features, league_name)
        lines = [
            f"═══ 动态权重调整 ═══",
            f"联赛: {league_name}",
            f"基础权重: XGB={detail.base_weights['xgboost']:.4f} "
            f"Ridge={detail.base_weights['ridge']:.4f} "
            f"Heur={detail.base_weights['heuristic']:.4f}",
            f"",
            f"上下文因子:",
            f"  盘口波动(σ):  {detail.sigma_trap:+.4f}",
            f"  实力差(ΔR):    {detail.rank_diff:+.4f}",
            f"  交锋(H2H):     {detail.h2h_factor:+.4f}",
            f"  盘口偏差(β):   {detail.beta_dev:+.4f}",
            f"  一致性(A4):    {detail.a4:+.4f}",
            f"",
            f"调整系数 α:",
            f"  α_xgb = {detail.alphas['xgb']:+.4f} "
            f"({_alpha_effect(detail.alphas['xgb'])})",
            f"  α_ridge = {detail.alphas['ridge']:+.4f} "
            f"({_alpha_effect(detail.alphas['ridge'])})",
            f"  α_heur = {detail.alphas['heur']:+.4f} "
            f"({_alpha_effect(detail.alphas['heur'])})",
            f"",
            f"最终权重:",
            f"  XGBoost:    {detail.adjusted_weights['xgboost']:.4f} "
            f"({detail.adjusted_weights['xgboost']-detail.base_weights['xgboost']:+.4f})",
            f"  Ridge:      {detail.adjusted_weights['ridge']:.4f} "
            f"({detail.adjusted_weights['ridge']-detail.base_weights['ridge']:+.4f})",
            f"  Heuristic:  {detail.adjusted_weights['heuristic']:.4f} "
            f"({detail.adjusted_weights['heuristic']-detail.base_weights['heuristic']:+.4f})",
        ]
        return '\n'.join(lines)

    # ──────────────────────────────────────────────
    # 内部方法: 联赛级基础权重微调
    # ──────────────────────────────────────────────

    def _get_base_with_league(self, league_name: str) -> Tuple[float, float, float]:
        """
        返回联赛感知的基础权重。

        联赛调整策略:
          - 高平局率联赛 (Ligue1 27.8%, 巴甲 27.0%) → 略微提升启发式 (因其平局能力)
          - 高波动联赛 (MLS 72%, 荷甲 68%) → 略微降低 XGBoost
        """
        if not self.use_league_profile or league_name == 'default':
            return self.w_xgb_base, self.w_ridge_base, self.w_heuristic_base

        # 检查是否有显式联赛偏移配置
        if league_name in self.league_offsets:
            offsets = self.league_offsets[league_name]
            w_xgb = self.w_xgb_base + offsets.get('xgb', 0.0)
            w_ridge = self.w_ridge_base + offsets.get('ridge', 0.0)
            w_heur = self.w_heuristic_base + offsets.get('heuristic', 0.0)
            return w_xgb, w_ridge, w_heur

        # 自动推导联赛调整
        profile = LEAGUE_PROFILES.get(league_name, LEAGUE_PROFILES['default'])
        draw_rate = profile['draw_rate']
        volatility = profile['volatility']

        # 平局率高 → 启发式 +2% (启发式有实力差距→平局逻辑)
        draw_bonus = (draw_rate - 0.261) * 0.15  # ~ ±0.003
        # 波动率高 → XGBoost -1.5% (数据驱动变弱)
        vol_penalty = (volatility - 0.63) * 0.10  # ~ ±0.005

        w_xgb = self.w_xgb_base - vol_penalty
        w_ridge = self.w_ridge_base
        w_heur = self.w_heuristic_base + draw_bonus

        # 微调幅度很小 (<1%)，在正常范围内
        return w_xgb, w_ridge, w_heur

    # ──────────────────────────────────────────────
    # 内部方法: 调整系数计算
    # ──────────────────────────────────────────────

    def _calc_alpha_xgb(self, rank_diff: float, sigma_trap: float,
                        a4: float) -> float:
        """
        XGBoost 调整系数 α_xgb

        逻辑:
          + rank_diff 贡献: 实力差距越明显，数据驱动的 XGBoost 越准确
          - sigma_trap 贡献: 赔率波动越大，基于市场特征的 XGBoost 越不可靠
          + consensus 贡献: 当 A4≈0 (模型一致性好) 时强化 XGBoost
                            当 |A4| 大 (模型背离) 时略降
        """
        alpha = 0.0
        alpha += self.K_XGB_RANK_DIFF * np.abs(rank_diff)
        alpha -= self.K_XGB_SIGMA * self._clip_sigma(sigma_trap)
        alpha += self.K_XGB_CONSENSUS * (1.0 - np.abs(a4) * 2.0)  # A4∈[-0.5,0.5]
        return alpha

    def _calc_alpha_ridge(self, beta_dev: float, rank_diff: float,
                          sigma_trap: float) -> float:
        """
        Ridge 调整系数 α_ridge

        逻辑:
          - beta_dev 贡献: 盘口偏差越大 → 净胜球预测越不可靠
          + rank_diff 贡献: 实力差距大 → 净胜球模式清晰
          - sigma_trap: 市场混乱 → 略降
        """
        alpha = 0.0
        alpha -= self.K_RIDGE_BETA_DEV * self._clip_beta_dev(beta_dev)
        alpha += self.K_RIDGE_RANK_DIFF * np.abs(rank_diff)
        alpha -= self.K_RIDGE_SIGMA * self._clip_sigma(sigma_trap)
        return alpha

    def _calc_alpha_heur(self, h2h: float, sigma_trap: float,
                         rank_diff: float, a4: float) -> float:
        """
        Heuristic 调整系数 α_heur

        逻辑:
          + h2h 贡献: H2H 信号明确 → 启发式规则更可信
          + sigma_trap 贡献: 市场混乱 → 启发式更稳定(不依赖市场)
          - rank_diff 贡献: 实力悬殊 → 数据驱动更好,启发式略降
          - consensus: 模型背离大 → 启发式也可能不可靠
        """
        alpha = 0.0
        alpha += self.K_HEUR_H2H * np.abs(h2h)
        alpha += self.K_HEUR_SIGMA * self._clip_sigma(sigma_trap)
        alpha -= self.K_HEUR_RANK_DIFF * np.abs(rank_diff)
        alpha -= self.K_HEUR_CONSENSUS * np.abs(a4) * 2.0
        return alpha

    # ──────────────────────────────────────────────
    # 辅助方法
    # ──────────────────────────────────────────────

    @staticmethod
    def _clip_sigma(sigma: float) -> float:
        """将 sigma_trap 裁剪到 [0, 3] 防止极端值"""
        return np.clip(np.abs(sigma), 0.0, 3.0)

    @staticmethod
    def _clip_beta_dev(beta: float) -> float:
        """将 beta_dev 裁剪到 [0, 2] 防止极端值"""
        return np.clip(np.abs(beta), 0.0, 2.0)

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        """通用钳位"""
        return max(lo, min(hi, value))

def _alpha_effect(alpha: float) -> str:
    """α 系数 → 可读效果描述"""
    if alpha > 0.08:
        return "↑↑ 显著提升"
    elif alpha > 0.03:
        return "↑ 略微提升"
    elif alpha > -0.03:
        return "→ 几乎不变"
    elif alpha > -0.08:
        return "↓ 略微降低"
    else:
        return "↓↓ 显著降低"

# ══════════════════════════════════════════════════════
# 便捷工厂函数 (供预测服务直接使用)
# ══════════════════════════════════════════════════════

_global_calculator: Optional[DynamicWeightCalculator] = None

def get_calculator(config: Dict = None) -> DynamicWeightCalculator:
    """获取全局单例 DynamicWeightCalculator (延迟初始化)"""
    global _global_calculator
    if _global_calculator is None and config is not None:
        _global_calculator = DynamicWeightCalculator(config)
    elif _global_calculator is None:
        _global_calculator = DynamicWeightCalculator()
    return _global_calculator

def reset_calculator():
    """重置全局单例 (用于测试)"""
    global _global_calculator
    _global_calculator = None

# ══════════════════════════════════════════════════════
# 自测
# ══════════════════════════════════════════════════════

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format='%(message)s')

    calc = DynamicWeightCalculator()

    # 场景1: 普通比赛 (低位波动, 均势, 无H2H信号)
    print("\n" + "=" * 60)
    print("场景1: 普通平局 (低位波动, 均势, 无H2H)")
    print(calc.explain({
        'sigma_trap': 0.1, 'rank_diff_factor': 0.05,
        'h2h_factor': 0.1, 'beta_dev': 0.2, 'a4': -0.05,
    }, 'Premier League'))

    # 场景2: 高波动强强对话 (sigma高, 实力接近, 强H2H)
    print("\n" + "=" * 60)
    print("场景2: 高波动强强对话 (市场动荡, 实力接近, 强烈H2H)")
    print(calc.explain({
        'sigma_trap': 1.8, 'rank_diff_factor': 0.02,
        'h2h_factor': 0.85, 'beta_dev': 1.2, 'a4': -0.35,
    }, 'UEFA Champions League'))

    # 场景3: 实力悬殊 (大盘口, 低波动)
    print("\n" + "=" * 60)
    print("场景3: 实力悬殊 (大盘口, 低波动, 无H2H)")
    print(calc.explain({
        'sigma_trap': 0.05, 'rank_diff_factor': 0.82,
        'h2h_factor': 0.0, 'beta_dev': 0.1, 'a4': 0.12,
    }, 'La Liga'))

    # 场景4: 高平局联赛 (法甲)
    print("\n" + "=" * 60)
    print("场景4: 法甲典型比赛")
    print(calc.explain({
        'sigma_trap': 0.3, 'rank_diff_factor': 0.15,
        'h2h_factor': 0.3, 'beta_dev': 0.5, 'a4': -0.1,
    }, 'Ligue 1'))

    # 场景5: MLS高波动
    print("\n" + "=" * 60)
    print("场景5: MLS高波动比赛")
    print(calc.explain({
        'sigma_trap': 2.0, 'rank_diff_factor': 0.2,
        'h2h_factor': 0.2, 'beta_dev': 0.8, 'a4': -0.2,
    }, 'MLS'))

    # 场景6: 禁用动态权重
    print("\n" + "=" * 60)
    print("场景6: 禁用动态权重 (回退到静态)")
    calc_static = DynamicWeightCalculator({'models': {
        'ensemble': {'xgboost_weight': 0.69, 'ridge_weight': 0.05, 'heuristic_weight': 0.26},
        'dynamic_weights': {'enabled': False},
    }})
    w = calc_static.compute({'sigma_trap': 1.5, 'rank_diff_factor': 0.5, 'h2h_factor': 0.8})
    print(f"  → {w}")

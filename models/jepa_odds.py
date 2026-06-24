"""
JEPA Odds Encoder — 赔率到嵌入空间的压缩映射
==============================================
LeCun JEPA 核心组件之一: Encoder

将高维原始赔率信息压缩为低维抽象嵌入表征。
- 输入: 原始赔率向量 (odds_h, odds_d, odds_a, spread, overround, water_level, ...)
- 输出: 嵌入 z ∈ R^d (d=16~64, 博弈论可解释维度)
- 正则化: VICReg (Variance-Invariance-Covariance) 防坍塌

关键设计:
  1. 嵌入维度语义化: 每个维度对应一个博弈论概念
     - z[0:4]   : 实力差分量 (λ_H-λ_A, spread映射, ...)
     - z[4:8]   : 不确定性分量 (overround, 赔率波动, ...)
     - z[8:12]  : 庄家态度分量 (draw_protection, skew, ...)
     - z[12:16] : 资金流分量 (volume_ratio, water_trend, ...)

  2. VICReg约束防止坍塌:
     - Variance: 每个维度在batch上的方差 > γ (避免所有输入映射到同一点)
     - Invariance: 相似市场状态 → 相似嵌入 (L2距离约束)
     - Covariance: 不同维度间去相关 (信息最大化)

  3. 赔率漂移 → 嵌入位移向量:
     Δz = Encoder(o_{t+1}) - Encoder(o_t)
     → ||Δz|| 量化信息冲击强度
     → Δz/||Δz|| 指示信息冲击方向

作者: 杜博弈 / FootballAI v4.1 JEPA Redesign
日期: 2026-06-20
"""

import numpy as np
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 嵌入空间维度定义
# ═══════════════════════════════════════════════════════════════

@dataclass
class EmbeddingDimensions:
    """嵌入空间的语义维度定义
    
    每个维度组对应一个博弈论可解释维度。
    总维度 d = 16 (可扩展到32/64)
    """
    # 实力差分量 (dim 0-3)
    strength_gap: int = 0         # λ_H - λ_A 映射
    spread_implied: int = 1       # spread隐含的实力差
    lambda_ratio: int = 2         # λ_H / λ_A 对数比
    fair_handicap: int = 3        # 公平盘口编码
    
    # 不确定性分量 (dim 4-7)
    overround_signal: int = 4     # 抽水率 → 庄家不确定性
    odds_volatility: int = 5      # 赔率时序波动
    market_entropy: int = 6       # 市场的熵 (1X2分布的均匀度)
    confidence_decay: int = 7     # 庄家置信度衰减因子
    
    # 庄家态度分量 (dim 8-11)
    draw_protection: int = 8      # 平局额外保护 (非均匀抽水中D方向)
    skew_direction: int = 9       # 偏斜方向 (H偏 vs A偏)
    price_rigidity: int = 10      # 价格刚性 (调盘阻力)
    bookmaker_confidence: int = 11 # 庄家自信度 (抽水低+稳定 = 高自信)
    
    # 资金流分量 (dim 12-15)
    volume_pressure: int = 12     # 资金压力指数
    water_trend_encode: int = 13  # 水位趋势编码
    hot_money_ratio: int = 14     # 热钱比例
    fund_divergence: int = 15     # 多机构资金方向分歧
    
    DIM = 16


# ═══════════════════════════════════════════════════════════════
# VICReg 正则化损失
# ═══════════════════════════════════════════════════════════════

class VICRegLoss:
    """
    VICReg (Variance-Invariance-Covariance) 正则化
    
    防止编码器坍塌到平凡解(对所有输入输出相同值)。
    
    三项损失:
      L_var  : 每个嵌入维度的方差应 ≥ γ_variance
      L_inv  : 相似输入应有相似嵌入 (数据增强不变性)
      L_cov  : 不同嵌入维度应去相关 (最大化信息容量)
    
    L_total = λ_var * L_var + λ_inv * L_inv + λ_cov * L_cov
    """
    
    def __init__(self, 
                 lambda_var: float = 1.0,
                 lambda_inv: float = 25.0, 
                 lambda_cov: float = 1.0,
                 gamma_variance: float = 1.0):
        self.lambda_var = lambda_var
        self.lambda_inv = lambda_inv
        self.lambda_cov = lambda_cov
        self.gamma_variance = gamma_variance
    
    def variance_loss(self, z: np.ndarray) -> float:
        """
        L_var: 鼓励每个嵌入维度的方差 ≥ γ_variance
        
        z: (batch_size, dim) 嵌入矩阵
        
        赔率场景意义:
          防止所有比赛(无论强弱悬殊还是势均力敌)被编码到相同的嵌入点。
          强队 vs 弱队的嵌入应该在实力差距维度上有较大方差。
        """
        # 每个维度的标准差
        std_z = np.sqrt(z.var(axis=0) + 1e-6)
        # Hinge loss: 只惩罚方差低于阈值的维度
        loss = np.sum(np.maximum(0, self.gamma_variance - std_z))
        return loss / z.shape[1]
    
    def invariance_loss(self, z1: np.ndarray, z2: np.ndarray) -> float:
        """
        L_inv: 相似市场状态应有相似嵌入
        
        z1, z2: 同一场比赛的不同"视角"编码
        - z1: 编码自当前赔率快照
        - z2: 编码自加了微小噪声/时序扰动的赔率快照
        
        赔率场景意义:
          短时间内的微小赔率波动不应导致嵌入大幅跳跃。
          这使嵌入对散户噪声鲁棒。
        """
        return np.mean((z1 - z2) ** 2)
    
    def covariance_loss(self, z: np.ndarray) -> float:
        """
        L_cov: 不同嵌入维度应去相关
        
        赔率场景意义:
          避免实力差分量和资金流分量冗余编码相同信息。
          例如: spread信息和λ差值信息不应完全相关(它们有不同的信息来源)。
        """
        z_centered = z - z.mean(axis=0, keepdims=True)
        # 协方差矩阵
        cov = (z_centered.T @ z_centered) / (z.shape[0] - 1)
        # 对角线归零后取Frobenius范数 (惩罚非对角线元素)
        n = cov.shape[0]
        off_diag = cov - np.eye(n) * cov.diagonal()
        return (off_diag ** 2).sum() / n
    
    def total_loss(self, z: np.ndarray, z_aug: np.ndarray) -> Dict[str, float]:
        """计算总VICReg损失"""
        var = self.variance_loss(z)
        inv = self.invariance_loss(z, z_aug)
        cov = self.covariance_loss(z)
        total = self.lambda_var * var + self.lambda_inv * inv + self.lambda_cov * cov
        return {"total": total, "var": var, "inv": inv, "cov": cov}


# ═══════════════════════════════════════════════════════════════
# 赔率编码器
# ═══════════════════════════════════════════════════════════════

class OddsEncoder:
    """
    赔率编码器 — 将原始赔率压缩为博弈论嵌入
    
    输入特征空间 (高维, 含噪声):
      - 1X2赔率 (3维)
      - 亚盘信息 (spread, water_level, handicap_change)
      - 大小球 (line, over_water, under_water)
      - 抽水率分解 (total_margin, draw_protection, skew)
      - 时序特征 (odds_trend, water_trend, volatility)
    
    输出嵌入空间 (低维, 去噪):
      - z ∈ R^16: 语义化博弈论嵌入
    
    关键设计原则:
      1. 非线性压缩: 从~20维原始赔率 → 16维语义嵌入
      2. 信息瓶颈: 强制编码器丢弃噪声，保留预测性信息
      3. 可解释性: 每个维度有明确的博弈论含义
    """
    
    def __init__(self, dim: int = 16):
        self.dim = dim
        self.ed = EmbeddingDimensions()
        # 权重矩阵 (可学习; 此处为初始化值)
        self._init_weights()
    
    def _init_weights(self):
        """初始化编码权重 (概念性实现)"""
        # 实际使用时替换为可训练参数
        # W ∈ R^(input_dim × embedding_dim)
        rng = np.random.RandomState(42)
        self.W = rng.randn(20, self.dim) * 0.1
        self.b = np.zeros(self.dim)
    
    def encode(self, odds_raw: Dict[str, float]) -> np.ndarray:
        """
        将原始赔率编码为嵌入向量
        
        Args:
            odds_raw: 原始赔率字典
              - odds_h, odds_d, odds_a: 1X2赔率
              - asian_handicap: 亚盘让球
              - water_level: 水位
              - overround: 抽水率
              - fair_handicap: 公平盘口
              - lam_h, lam_a: λ值
              - volume_ratio_fav: 热门方资金比例
              - water_trend: 水位趋势 (-1/0/+1)
              - odds_trend: 赔率趋势 (-1/0/+1)
              - draw_protection: 平局额外保护
              - volatility: 赔率波动率
              - spread: 实力差距
        
        Returns:
            z: np.ndarray shape (16,) 嵌入向量
        """
        z = np.zeros(self.dim)
        
        # ── 实力差分量 (dim 0-3) ──
        lam_h = odds_raw.get('lam_h', 1.0)
        lam_a = odds_raw.get('lam_a', 1.0)
        spread = odds_raw.get('spread', 0.0)
        fair_hc = odds_raw.get('fair_handicap', 0.0)
        
        # z[0]: λ差值 → tanh归一化到[-1, 1]
        z[0] = np.tanh((lam_h - lam_a) / 3.0)
        # z[1]: spread隐含实力差
        z[1] = np.tanh(spread / 4.0)
        # z[2]: λ比值对数
        ratio = np.log(max(lam_h, 0.01) / max(lam_a, 0.01))
        z[2] = np.tanh(ratio / 2.0)
        # z[3]: 公平盘口编码
        z[3] = np.tanh(fair_hc / 3.0)
        
        # ── 不确定性分量 (dim 4-7) ──
        overround = odds_raw.get('overround', 0.06)
        volatility = odds_raw.get('volatility', 0.01)
        odds_h = odds_raw.get('odds_h', 2.0)
        odds_d = odds_raw.get('odds_d', 3.5)
        odds_a = odds_raw.get('odds_a', 4.0)
        
        # z[4]: 抽水率 → 庄家不确定性 (高抽水=不确定)
        z[4] = np.clip((overround - 0.05) / 0.10, -1.0, 1.0)
        # z[5]: 赔率时序波动
        z[5] = np.clip(volatility * 100, 0, 1.0)
        # z[6]: 市场熵 H = -Σ p_i log p_i
        raw_sum = 1/odds_h + 1/odds_d + 1/odds_a
        probs = np.array([1/(odds_h*raw_sum), 1/(odds_d*raw_sum), 1/(odds_a*raw_sum)])
        entropy = -np.sum(probs * np.log2(np.clip(probs, 1e-8, 1)))
        z[6] = entropy / np.log2(3)  # 归一化到[0,1]
        # z[7]: 置信度衰减
        z[7] = 1.0 - np.clip(volatility * 50, 0, 1.0)
        
        # ── 庄家态度分量 (dim 8-11) ──
        draw_protection = odds_raw.get('draw_protection', 0.0)
        skew = odds_raw.get('skew', 0.0)
        price_rigidity = odds_raw.get('price_rigidity', 0.5)
        bookmaker_conf = odds_raw.get('bookmaker_confidence_signal', 0.5)
        
        # z[8]: 平局保护
        z[8] = np.clip(draw_protection * 20, -1.0, 1.0)
        # z[9]: 偏斜方向
        z[9] = np.tanh(skew * 5.0)
        # z[10]: 价格刚性
        z[10] = np.clip(price_rigidity, 0, 1.0)
        # z[11]: 庄家自信度
        z[11] = np.clip(bookmaker_conf, 0, 1.0)
        
        # ── 资金流分量 (dim 12-15) ──
        vol_fav = odds_raw.get('volume_ratio_fav', 0.5)
        water_trend = odds_raw.get('water_trend', 0)
        hot_money = odds_raw.get('hot_money_ratio', 0.3)
        fund_div = odds_raw.get('fund_divergence', 0.0)
        
        # z[12]: 资金压力
        z[12] = (vol_fav - 0.5) * 2.0  # → [-1, 1]
        # z[13]: 水位趋势编码
        z[13] = np.clip(water_trend, -1.0, 1.0)
        # z[14]: 热钱比例
        z[14] = np.clip(hot_money, 0, 1.0)
        # z[15]: 多机构资金方向分歧
        z[15] = np.clip(fund_div, 0, 1.0)
        
        return z
    
    def encode_delta(self, odds_before: Dict[str, float], 
                     odds_after: Dict[str, float]) -> np.ndarray:
        """
        赔率漂移 → 嵌入位移向量
        
        Δz = Encoder(o_{t+1}) - Encoder(o_t)
        
        这是博弈论视角的核心量:
        - ||Δz|| 量化信息冲击强度
        - Δz/||Δz|| 指示庄家调盘的信息方向
        - Δz[d] 的正负解释为"维度d上庄家态度变化"
        """
        z_before = self.encode(odds_before)
        z_after = self.encode(odds_after)
        delta = z_after - z_before
        return delta
    
    def interpret_delta(self, delta: np.ndarray) -> Dict[str, str]:
        """
        解释嵌入位移向量的博弈论含义
        
        将||Δz||分解到各语义维度，输出人类可读的调盘意图解读。
        """
        ed = EmbeddingDimensions()
        interpretations = {}
        
        # 实力差变化
        if abs(delta[ed.strength_gap]) > 0.1:
            direction = "增强" if delta[ed.strength_gap] > 0 else "减弱"
            interpretations['strength'] = f"实力差认知{direction} (Δ={delta[ed.strength_gap]:.3f})"
        
        # 不确定性变化
        if abs(delta[ed.overround_signal]) > 0.1:
            direction = "升高" if delta[ed.overround_signal] > 0 else "降低"
            interpretations['uncertainty'] = f"庄家不确定性{direction} (Δ={delta[ed.overround_signal]:.3f})"
        
        # 平局保护变化
        if abs(delta[ed.draw_protection]) > 0.1:
            direction = "增强" if delta[ed.draw_protection] > 0 else "减弱"
            interpretations['draw'] = f"平局保护{direction} (Δ={delta[ed.draw_protection]:.3f})"
        
        # 资金压力变化
        if abs(delta[ed.volume_pressure]) > 0.1:
            direction = "增加" if delta[ed.volume_pressure] > 0 else "减少"
            interpretations['fund'] = f"热门方资金压力{direction} (Δ={delta[ed.volume_pressure]:.3f})"
        
        if not interpretations:
            interpretations['info'] = "无显著调盘信号 (Δz ≈ 0)"
        
        return interpretations


# ═══════════════════════════════════════════════════════════════
# 赔率嵌入辅助函数
# ═══════════════════════════════════════════════════════════════

def compute_odds_embedding(odds_h: float, odds_d: float, odds_a: float,
                           asian_handicap: Optional[float] = None,
                           water_level: float = 0.92,
                           overround: Optional[float] = None,
                           lam_h: Optional[float] = None,
                           lam_a: Optional[float] = None,
                           fair_handicap: Optional[float] = None,
                           volume_ratio_fav: float = 0.5,
                           **kwargs) -> np.ndarray:
    """
    快速入口: 从1X2赔率生成JEPA嵌入向量
    
    示例:
        z = compute_odds_embedding(1.80, 3.50, 4.20)
        # z ∈ R^16, 可在嵌入空间中进行各种操作
    
    Returns:
        np.ndarray shape (16,) 嵌入向量
    """
    encoder = OddsEncoder()
    
    # 自动计算衍生量
    if overround is None:
        raw_sum = 1/odds_h + 1/odds_d + 1/odds_a
        overround = raw_sum - 1.0
    
    # λ估算 (简化版)
    if lam_h is None or lam_a is None:
        raw_sum = 1/odds_h + 1/odds_d + 1/odds_a
        p_implied = np.array([1/(odds_h*raw_sum), 1/(odds_d*raw_sum), 1/(odds_a*raw_sum)])
        # 简化λ估计
        goal_diff_estimate = (p_implied[0] - p_implied[2]) * 3.0
        lam_h = max(0.3, 1.0 + goal_diff_estimate * 0.5)
        lam_a = max(0.3, 1.0 - goal_diff_estimate * 0.5)
    
    if fair_handicap is None:
        goal_diff = abs(lam_h - lam_a)
        if goal_diff > 2.0: fair_handicap = 2.5
        elif goal_diff > 1.5: fair_handicap = 2.0
        elif goal_diff > 1.2: fair_handicap = 1.75
        elif goal_diff > 0.9: fair_handicap = 1.5
        elif goal_diff > 0.6: fair_handicap = 1.0
        elif goal_diff > 0.35: fair_handicap = 0.75
        elif goal_diff > 0.2: fair_handicap = 0.5
        elif goal_diff > 0.08: fair_handicap = 0.25
        else: fair_handicap = 0.0
    
    if asian_handicap is None:
        asian_handicap = fair_handicap
    
    spread = abs(lam_h - lam_a) * 2.0  # 近似的实力差距
    
    odds_raw = {
        'odds_h': odds_h, 'odds_d': odds_d, 'odds_a': odds_a,
        'asian_handicap': asian_handicap,
        'water_level': water_level,
        'overround': overround,
        'fair_handicap': fair_handicap,
        'lam_h': lam_h, 'lam_a': lam_a,
        'volume_ratio_fav': volume_ratio_fav,
        'spread': spread,
        'volatility': kwargs.get('volatility', 0.01),
        'draw_protection': kwargs.get('draw_protection', 0.0),
        'skew': kwargs.get('skew', 0.0),
        'price_rigidity': kwargs.get('price_rigidity', 0.5),
        'bookmaker_confidence_signal': kwargs.get('bookmaker_confidence_signal', 0.5),
        'water_trend': kwargs.get('water_trend', 0),
        'odds_trend': kwargs.get('odds_trend', 0),
        'hot_money_ratio': kwargs.get('hot_money_ratio', 0.3),
        'fund_divergence': kwargs.get('fund_divergence', 0.0),
    }
    
    return encoder.encode(odds_raw)


def embedding_distance(z1: np.ndarray, z2: np.ndarray, 
                       metric: str = 'cosine') -> float:
    """
    嵌入空间距离度量
    
    - cosine: 余弦距离 ∈ [0, 2], 关注方向差异
    - euclidean: 欧氏距离, 关注幅度+方向差异
    - mahalanobis: (待实现) 考虑维度相关性的马氏距离
    """
    if metric == 'cosine':
        norm1 = np.linalg.norm(z1)
        norm2 = np.linalg.norm(z2)
        if norm1 < 1e-8 or norm2 < 1e-8:
            return 0.0
        cos_sim = np.dot(z1, z2) / (norm1 * norm2)
        return 1.0 - cos_sim  # 余弦距离
    elif metric == 'euclidean':
        return float(np.linalg.norm(z1 - z2))
    else:
        return float(np.linalg.norm(z1 - z2))

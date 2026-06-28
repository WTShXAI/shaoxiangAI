"""
JEPA Market Predictor — 嵌入空间状态转移预言机
================================================
LeCun JEPA 核心组件之二: Predictor

在嵌入空间中预测市场状态演化: z_t → ẑ_{t+1} = Predictor(z_t)

庄家调盘 = Predictor的滚动预测更新:
  - 初盘嵌入 z_0 = Encoder(odds_open)  →  庄家初始世界模型
  - 预测嵌入 ẑ_1 = Predictor(z_0)       →  庄家预演的"终盘应该在哪"
  - 终盘嵌入 z_1 = Encoder(odds_close)  →  实际终盘所在位置
  - 预测误差 e = z_1 - ẑ_1              →  市场冲击/新信息的嵌入表征

关键区别:
  - 传统: 赔率漂移 = 庄家"意图"信号
  - JEPA:  赔率漂移 = Predictor修正误差的可观察投影
          庄家不是"故意"调盘，而是世界模型收到预测误差后的贝叶斯更新

漂移动力学:
  - 一阶 (速度):  v_t = dz/dt ≈ (z_t - z_{t-1}) / Δt
    - 大 v_t → 强信号, 可能是真实信息或大资金冲击
    - 判别: v_t 的方向一致性 (连续同向 = 信息; 来回摇摆 = 噪声)
  
  - 二阶 (加速度): a_t = dv/dt ≈ (v_t - v_{t-1}) / Δt
    - 正加速 (a > 0) → 庄家越来越确定, 加速修正
    - 负加速 (a < 0) → 修正减速, 正接近目标嵌入
    - a 符号翻转 → 庄家改变了方向判断 (重要信号!)

  - 三阶 (急动度): j_t = da/dt 
    - 急动度大 → 紧急信息冲击 (红牌/伤病突发)

作者: 杜博弈 / FootballAI v4.1 JEPA Redesign
日期: 2026-06-20
"""

import numpy as np
from typing import Dict, List, Tuple, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum
import logging

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 漂移模式枚举
# ═══════════════════════════════════════════════════════════════

class DriftPattern(Enum):
    """赔率漂移的嵌入空间模式分类"""
    CONFIDENCE_BUILD = "confidence_build"       # 信心增强型: 单向匀速 → 单边小幅连续修正
    HEDGING_PRESSURE = "hedging_pressure"       # 对冲压力型: 振荡 → 来回修正
    INFORMATION_SHOCK = "information_shock"     # 信息冲击型: 脉冲 → 突然大 Δ
    TRAP_INDUCEMENT = "trap_inducement"         # 诱盘型: 逆信息方向 → Δz vs Δẑ矛盾
    STABILITY = "stability"                     # 稳定型: Δz ≈ 0
    REVERSAL = "reversal"                       # 反转型: a符号翻转 → 庄家改变判断

# ═══════════════════════════════════════════════════════════════
# 漂移分析结果
# ═══════════════════════════════════════════════════════════════

@dataclass
class DriftAnalysis:
    """嵌入空间漂移分析结果"""
    # 基础量
    z_start: np.ndarray              # 初盘嵌入
    z_end: np.ndarray                # 终盘嵌入
    delta_z: np.ndarray              # 总位移
    
    # 一阶: 速度
    mean_velocity: float             # 平均速度 ||v||
    velocity_stability: float        # 速度方向一致性 [0,1] (1=完全同向)
    
    # 二阶: 加速度
    mean_acceleration: float         # 平均加速度
    accel_sign_flips: int            # 加速度符号翻转次数
    
    # 三阶: 急动度
    max_jerk: float                  # 最大急动度
    
    # 分类
    drift_pattern: str               # 漂移模式 (DriftPattern)
    confidence: float                # 模式分类置信度
    information_strength: float      # 信息冲击总强度
    
    # 解释
    interpretation: str = ""
    
    def to_dict(self) -> Dict:
        return {
            "total_displacement": round(float(np.linalg.norm(self.delta_z)), 4),
            "mean_velocity": round(self.mean_velocity, 4),
            "velocity_stability": round(self.velocity_stability, 4),
            "mean_acceleration": round(self.mean_acceleration, 4),
            "accel_sign_flips": self.accel_sign_flips,
            "max_jerk": round(self.max_jerk, 4),
            "drift_pattern": self.drift_pattern,
            "confidence": round(self.confidence, 4),
            "information_strength": round(self.information_strength, 4),
            "interpretation": self.interpretation,
        }

# ═══════════════════════════════════════════════════════════════
# 市场状态预测器
# ═══════════════════════════════════════════════════════════════

class MarketStatePredictor:
    """
    嵌入空间 Predictor — 预测市场的下一个嵌入状态
    
    庄家作为"世界模型"，在嵌入空间中运行以下循环:
      1. 编码当前状态:     z_t = Encoder(odds_t)
      2. 预测下一状态:     ẑ_{t+1} = Predictor(z_t)
      3. 观察实际状态:     z_{t+1} = Encoder(odds_{t+1})
      4. 计算预测误差:     e_t = z_{t+1} - ẑ_{t+1}
      5. 修正预测器:       Predictor ← Predictor - η·∇L(e_t)
      6. 调盘 = 使 odds_{t+1} 接近 Encoder^{-1}(ẑ_{t+1})
    
    关键洞察:
      赔率漂移不是"庄家在想什么"，而是"庄家世界模型的预测误差修正过程"。
      我们观察到的赔率变化 = Encoder^{-1}(Predictor(z_t) + Δ_correction_t)
    """
    
    def __init__(self, embed_dim: int = 16, 
                 predictor_type: str = 'linear'):
        """
        Args:
            embed_dim: 嵌入维度
            predictor_type: 预测器类型
              - 'linear': z_{t+1} = W·z_t + b (基础)
              - 'mlp': z_{t+1} = MLP(z_t) (非线性)
              - 'rnn_style': z_{t+1} = RNN(z_t, h_t) (序列)
        """
        self.embed_dim = embed_dim
        self.predictor_type = predictor_type
        
        # 初始化线性预测器权重 (概念性)
        rng = np.random.RandomState(123)
        self.W = rng.randn(embed_dim, embed_dim) * 0.01
        self.b = np.zeros(embed_dim)
        
        # 预测历史 (用于学习)
        self.prediction_history: List[Tuple[np.ndarray, np.ndarray]] = []
    
    def predict(self, z_t: np.ndarray) -> np.ndarray:
        """
        预测下一个嵌入状态
        
        ẑ_{t+1} = Predictor(z_t)
        
        庄家世界模型的核心函数:
          给定当前市场状态的嵌入表征，
          预测市场状态将如何演化。
        
        在理想情况下 (无新信息):
          ẑ_{t+1} ≈ z_t  (市场状态应该稳定)
        
        在实际情况下:
          ẑ_{t+1} = z_t + δ  (预期会有微小漂移)
        """
        if self.predictor_type == 'linear':
            return self.W @ z_t + self.b
        elif self.predictor_type == 'mlp':
            # 简化MLP: W2·ReLU(W1·z_t + b1) + b2
            # 实际使用时用完整MLP
            return self.W @ z_t + self.b
        else:
            return self.W @ z_t + self.b
    
    def predict_n_steps(self, z_0: np.ndarray, n: int) -> List[np.ndarray]:
        """
        多步滚动预测 — 世界模型"预演"比赛走向
        
        z_0 → ẑ_1 = P(z_0) → ẑ_2 = P(ẑ_1) → ... → ẑ_n
        
        庄家在开盘时已经在嵌入空间中预演了整场比赛期间的市场演化。
        这对应 LeCun 世界模型的核心: "在行动前预演后果"。
        
        应用:
          - 预演赔率终盘位置
          - 评估"如果不干预，赔率会漂到哪"
          - 发现庄家的"目标嵌入"(稳定点)
        """
        trajectory = [z_0.copy()]
        z = z_0.copy()
        for _ in range(n):
            z = self.predict(z)
            trajectory.append(z.copy())
        return trajectory
    
    def prediction_error(self, z_t: np.ndarray, z_next: np.ndarray) -> float:
        """
        计算预测误差 (世界模型的"惊讶"程度)
        
        e = ||z_{t+1} - ẑ_{t+1}|| / ||z_t||
        
        大误差 = 发生了预测器未预料到的事件 (信息冲击)
        小误差 = 市场按预期演化 (庄家世界模型准确)
        """
        pred = self.predict(z_t)
        error = np.linalg.norm(z_next - pred)
        # 归一化
        norm = np.linalg.norm(z_t) + 1e-8
        return float(error / norm)
    
    def update(self, z_t: np.ndarray, z_next: np.ndarray, 
               learning_rate: float = 0.01):
        """
        Predictor的在线学习更新
        
        庄家收到新信息后的贝叶斯更新:
          P' ← P - η·∇L(z_next, P(z_t))
        
        这里用简化梯度下降模拟。
        """
        pred = self.predict(z_t)
        error = z_next - pred
        # 简化参数更新: W ← W + η·error·z_t^T
        self.W += learning_rate * np.outer(error, z_t)
        self.b += learning_rate * error
        self.prediction_history.append((z_t.copy(), z_next.copy()))
    
    def find_stable_point(self, z_0: np.ndarray, 
                          max_steps: int = 50,
                          tol: float = 1e-4) -> np.ndarray:
        """
        寻找 Predictor 的稳定点 (固定点)
        
        z* 满足: P(z*) = z*
        
        庄家世界模型的"吸引子":
          - 稳定点是市场在没有新信息时的自然归宿
          - 庄家开盘时瞄准的就是这个稳定点
          - 实际终盘偏离稳定点 = 新信息的累积效应
        
        博弈论意义:
          如果终盘嵌入 z_close ≈ z_stable → 没有意外信息
          如果 z_close 远离 z_stable → 发生了庄家未预见的事件
        """
        z = z_0.copy()
        trajectory = [z.copy()]
        for _ in range(max_steps):
            z_new = self.predict(z)
            if np.linalg.norm(z_new - z) < tol:
                break
            z = z_new
            trajectory.append(z.copy())
        return z

# ═══════════════════════════════════════════════════════════════
# 赔率漂移动力学分析器
# ═══════════════════════════════════════════════════════════════

class DriftDynamicsAnalyzer:
    """
    赔率漂移动力学分析器 — 在嵌入空间中量化漂移模式
    
    核心功能:
      1. 从赔率时间序列计算嵌入漂移的一/二/三阶导数
      2. 分类漂移模式 (信心增强 vs 对冲压力 vs 诱盘)
      3. 提取"信息冲击"的嵌入空间签名
    """
    
    def __init__(self, encoder=None, predictor: Optional[MarketStatePredictor] = None):
        from .odds_encoder import OddsEncoder
        self.encoder = encoder or OddsEncoder()
        self.predictor = predictor or MarketStatePredictor()
    
    def analyze_drift(self, 
                      odds_sequence: List[Dict[str, float]],
                      times: Optional[List[float]] = None) -> DriftAnalysis:
        """
        分析赔率序列的嵌入空间漂移动力学
        
        Args:
            odds_sequence: 赔率时间序列 [odds_t0, odds_t1, ..., odds_tn]
            times: 对应时间戳 (可选, 默认均匀间隔)
        
        Returns:
            DriftAnalysis: 漂移分析结果
        """
        if len(odds_sequence) < 2:
            return DriftAnalysis(
                z_start=np.zeros(16), z_end=np.zeros(16),
                delta_z=np.zeros(16),
                mean_velocity=0, velocity_stability=1.0,
                mean_acceleration=0, accel_sign_flips=0,
                max_jerk=0, drift_pattern="stability",
                confidence=1.0, information_strength=0.0,
                interpretation="数据点不足, 无法分析漂移"
            )
        
        # 编码所有时间点
        embeddings = [self.encoder.encode(odds) for odds in odds_sequence]
        n = len(embeddings)
        
        if times is None:
            times = list(range(n))
        
        # ── 一阶: 速度 v_t = (z_{t+1} - z_t) / Δt ──
        velocities = []
        for i in range(n - 1):
            dt = max(times[i+1] - times[i], 1e-6)
            v = (embeddings[i+1] - embeddings[i]) / dt
            velocities.append(v)
        
        mean_v = float(np.mean([np.linalg.norm(v) for v in velocities]))
        
        # 速度方向一致性: cos_sim(v_t, v_{t+1}) 的均值
        if len(velocities) >= 2:
            cos_sims = []
            for i in range(len(velocities) - 1):
                v1, v2 = velocities[i], velocities[i+1]
                n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
                if n1 > 1e-8 and n2 > 1e-8:
                    cos_sims.append(np.dot(v1, v2) / (n1 * n2))
            v_stability = float(np.mean(cos_sims)) if cos_sims else 1.0
            # 映射到[0,1]
            v_stability = (v_stability + 1.0) / 2.0
        else:
            v_stability = 1.0
        
        # ── 二阶: 加速度 a_t = (v_{t+1} - v_t) / Δt ──
        accelerations = []
        for i in range(len(velocities) - 1):
            dt = max(times[i+2] - times[i+1], 1e-6)
            a = (velocities[i+1] - velocities[i]) / dt
            accelerations.append(a)
        
        mean_a = float(np.mean([np.linalg.norm(a) for a in accelerations])) if accelerations else 0.0
        
        # 加速度符号翻转次数
        sign_flips = 0
        if len(accelerations) >= 2:
            for i in range(len(accelerations) - 1):
                # 检查主方向(最大绝对值维度)是否翻转
                max_dim_prev = np.argmax(np.abs(accelerations[i]))
                max_dim_next = np.argmax(np.abs(accelerations[i+1]))
                if (accelerations[i][max_dim_prev] * 
                    accelerations[i+1][max_dim_prev]) < 0:
                    sign_flips += 1
        
        # ── 三阶: 急动度 j_t = (a_{t+1} - a_t) / Δt ──
        jerks = []
        for i in range(len(accelerations) - 1):
            dt = max(times[i+3] - times[i+2], 1e-6)
            j = (accelerations[i+1] - accelerations[i]) / dt
            jerks.append(np.linalg.norm(j))
        
        max_jerk = float(max(jerks)) if jerks else 0.0
        
        # ── 总信息冲击强度 ──
        total_delta = embeddings[-1] - embeddings[0]
        info_strength = float(np.linalg.norm(total_delta))
        
        # ── 模式分类 ──
        pattern, confidence, interpretation = self._classify_drift(
            mean_v, v_stability, mean_a, sign_flips, max_jerk, 
            info_strength, velocities, embeddings
        )
        
        return DriftAnalysis(
            z_start=embeddings[0], z_end=embeddings[-1],
            delta_z=total_delta,
            mean_velocity=mean_v,
            velocity_stability=v_stability,
            mean_acceleration=mean_a,
            accel_sign_flips=sign_flips,
            max_jerk=max_jerk,
            drift_pattern=pattern,
            confidence=confidence,
            information_strength=info_strength,
            interpretation=interpretation,
        )
    
    def _classify_drift(self, 
                        mean_v: float, v_stability: float,
                        mean_a: float, sign_flips: int,
                        max_jerk: float, info_strength: float,
                        velocities: List[np.ndarray],
                        embeddings: List[np.ndarray]) -> Tuple[str, float, str]:
        """
        分类漂移模式
        
        信号等级映射:
        - CONFIDENCE_BUILD → S/A级: 庄家收到真实信息, 持续修正
        - INFORMATION_SHOCK → A级: 突发事件, 快速反应
        - HEDGING_PRESSURE → C级: 资金压力, 噪声
        - TRAP_INDUCEMENT → F级: 诱盘信号, 需规避
        - STABILITY → C级: 无信息
        - REVERSAL → B级: 庄家改变判断
        """
        
        # 规则1: 几乎没有漂移 → 稳定
        if info_strength < 0.05:
            return (DriftPattern.STABILITY.value, 0.95,
                    "赔率几乎无变化, 庄家世界模型预测稳定, 无需调整")
        
        # 规则2: 急动度极大 → 信息冲击
        if max_jerk > 0.3:
            return (DriftPattern.INFORMATION_SHOCK.value, 
                    min(0.95, 0.6 + max_jerk * 0.5),
                    f"急动度={max_jerk:.3f}极大, 突发事件冲击(伤病/红牌/首发变更)")
        
        # 规则3: 速度方向高度一致 + 低加速度翻转 → 信心增强
        if v_stability > 0.7 and sign_flips <= 1 and mean_v > 0.02:
            # 检查是否单边 (所有速度向量大致同向)
            if len(velocities) >= 2:
                main_dir = velocities[-1] / (np.linalg.norm(velocities[-1]) + 1e-8)
                consensus = all(
                    np.dot(v, main_dir) / (np.linalg.norm(v) + 1e-8) > 0.3
                    for v in velocities
                )
                if consensus:
                    return (DriftPattern.CONFIDENCE_BUILD.value, 
                            min(0.90, 0.55 + v_stability * 0.3),
                            f"单向匀速漂移(v_consistency={v_stability:.2f}), "
                            f"庄家收到真实信息, 持续修正定价")
        
        # 规则4: 频繁翻转 + 中高速度 → 对冲压力
        if sign_flips >= 2 and mean_v > 0.02:
            return (DriftPattern.HEDGING_PRESSURE.value,
                    0.7 + sign_flips * 0.05,
                    f"加速度频繁翻转({sign_flips}次), 资金对冲压力, "
                    f"非信息型漂移")
        
        # 规则5: Predictor误差与观察位移方向相反 → 诱盘
        if len(embeddings) >= 2 and self.predictor is not None:
            z_start = embeddings[0]
            z_observed_delta = embeddings[-1] - z_start
            z_predicted_delta = self.predictor.predict(z_start) - z_start
            
            # 如果预测方向和实际方向夹角 > 90° → 庄家行为与模型预演矛盾
            if (np.dot(z_predicted_delta, z_observed_delta) < 0 
                and info_strength > 0.1):
                return (DriftPattern.TRAP_INDUCEMENT.value,
                        0.6 + min(0.3, info_strength * 0.5),
                        f"Predictor预测方向与实际漂移方向相反, "
                        f"庄家行为与预演矛盾 → 潜在诱盘")
        
        # 规则6: 加速度符号翻转 → 反转
        if sign_flips >= 1 and mean_a > 0.1:
            return (DriftPattern.REVERSAL.value,
                    0.65 + min(0.2, sign_flips * 0.05),
                    f"漂移方向发生反转, 庄家改变了判断, 值得关注")
        
        # 默认: 稳定性
        return (DriftPattern.STABILITY.value, 0.5,
                "漂移模式不明确, 视为稳定/噪声")

# ═══════════════════════════════════════════════════════════════
# 庄家调盘意图解码器
# ═══════════════════════════════════════════════════════════════

class BookmakerAdjustmentDecoder:
    """
    庄家调盘意图解码器 — 从嵌入漂移反推庄家意图
    
    将 Predictor 的滚动更新过程映射为可解释的庄家行为:
      1. 初盘嵌入: z_0 = 庄家的先验
      2. 滚动预测: ẑ_t = 庄家的信念更新
      3. 实际调盘: z_t = 市场力量+庄家意志的合成
      4. 差距分析: z_t - ẑ_t = 庄家"被迫"vs"主动"的分解
    """
    
    def __init__(self, predictor: MarketStatePredictor):
        self.predictor = predictor
    
    def decompose_adjustment(self,
                             odds_open: Dict[str, float],
                             odds_close: Dict[str, float],
                             encoder=None) -> Dict:
        """
        分解调盘为"主动调盘"和"被动调盘"分量
        
        庄家调盘 = 主动调盘(世界模型预演) + 被动调盘(市场压力)
        
        主动: Δz_active = Predictor(z_open) - z_open
        被动: Δz_passive = z_close - Predictor(z_open)
        总调盘: Δz_total = z_close - z_open = Δz_active + Δz_passive
        """
        from .odds_encoder import OddsEncoder
        enc = encoder or OddsEncoder()
        
        z_open = enc.encode(odds_open)
        z_close = enc.encode(odds_close)
        z_predicted = self.predictor.predict(z_open)
        
        delta_active = z_predicted - z_open
        delta_passive = z_close - z_predicted
        delta_total = z_close - z_open
        
        # 主动调盘占比
        total_norm = np.linalg.norm(delta_total) + 1e-8
        active_ratio = np.linalg.norm(delta_active) / total_norm
        
        # 方向一致性: cos(Δz_active, Δz_passive)
        na = np.linalg.norm(delta_active)
        np_ = np.linalg.norm(delta_passive)
        if na > 1e-8 and np_ > 1e-8:
            alignment = float(np.dot(delta_active, delta_passive) / (na * np_))
        else:
            alignment = 0.0
        
        # 解读
        if alignment > 0.5:
            intent = "主动调盘与市场压力方向一致 → 庄家顺势而为"
        elif alignment < -0.5:
            intent = "主动调盘与市场压力方向相反 → 庄家在抵抗市场, 锚定信念"
        else:
            intent = "主动调盘与市场压力近似正交 → 庄家独立操作, 不受市场影响"
        
        return {
            'delta_total_norm': float(np.linalg.norm(delta_total)),
            'delta_active_norm': float(np.linalg.norm(delta_active)),
            'delta_passive_norm': float(np.linalg.norm(delta_passive)),
            'active_ratio': float(active_ratio),
            'alignment': alignment,
            'intent': intent,
            'bookmaker_stance': 'resistant' if alignment < -0.3 else 
                              ('aligned' if alignment > 0.3 else 'neutral'),
        }

# ═══════════════════════════════════════════════════════════════
# 信号等级映射
# ═══════════════════════════════════════════════════════════════

def drift_to_signal_grade(drift: DriftAnalysis) -> str:
    """
    将JEPA漂移模式映射到FootballAI信号等级体系
    
    S级: 庄家跨机构共识 → 多机构Predictor同向预测 (暂未实现)
    A级: 庄家单边大幅调整 → CONFIDENCE_BUILD / INFORMATION_SHOCK
    B级: 方向反转 → REVERSAL
    C级: 噪声 → STABILITY / HEDGING_PRESSURE
    F级: 诱盘 → TRAP_INDUCEMENT
    """
    mapping = {
        DriftPattern.CONFIDENCE_BUILD.value: ('A', '庄家信心增强型调盘'),
        DriftPattern.INFORMATION_SHOCK.value: ('A', '信息冲击型调盘'),
        DriftPattern.REVERSAL.value: ('B', '庄家方向反转'),
        DriftPattern.HEDGING_PRESSURE.value: ('C', '资金对冲压力'),
        DriftPattern.STABILITY.value: ('C', '稳定/噪声'),
        DriftPattern.TRAP_INDUCEMENT.value: ('F', '潜在诱盘信号'),
    }
    grade, label = mapping.get(drift.drift_pattern, ('C', '未分类'))
    return f"[{grade}] {label} (置信度={drift.confidence:.2f})"

"""
JEPA D-Gate — 嵌入空间平局门控 (JEPA范式)
=============================================
传统 D-Gate: margin = P(D) - max(P(H), P(A)), 分桶精度过滤

JEPA D-Gate: 在嵌入空间中计算平局信号的几何可信度

核心理念:
  D-Gate的本质不是"概率边际"，而是"嵌入空间中平局原型的距离"。
  
  在嵌入空间中:
    - 每种赛果 (H, D, A) 对应嵌入空间中的原型点 (prototype)
    - D-Gate zone = 当前嵌入 z 与 D-prototype 的归一化距离
    - 传统 margin → 嵌入距离的单调映射

JEPA重定义:
  传统:  D_margin = P(D) - max(P(H), P(A))  → 6个zone
  JEPA:  D_distance = d(z, z_D_proto) / (d(z, z_H_proto) + d(z, z_D_proto) + d(z, z_A_proto))
         → 连续可信度而非离散zone

为什么JEPA D-Gate更好:
  1. 不再依赖单一概率维度 (P(D))，而是利用嵌入空间的全部16维信息
  2. 嵌入距离天然考虑了赔率结构的所有方面 (实力差、不确定性、资金流)
  3. 传统margin=0.02 → GARBAGE zone 是离散边界; 
     JEPA连续距离能区分 margin=0.01 vs margin=0.019 的不同可信度
  4. 防坍塌: VICReg保证嵌入空间有足够的区分度

作者: 杜博弈 / FootballAI v4.1 JEPA Redesign
日期: 2026-06-20
"""

import numpy as np
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 赛果原型嵌入
# ═══════════════════════════════════════════════════════════════

# 三种赛果在嵌入空间中的"原型"位置
# 这些原型是历史数据中"典型主胜"、"典型平局"、"典型客胜"的嵌入中心
# 通过学习得到; 此处为概念性初始化

OUTCOME_PROTOTYPES = {
    # 主胜原型: 实力差大(z[0]高)、不确定性低(z[4]低)、资金向主(z[12]高)
    'H': np.array([
        0.7,   # z[0] 实力差: 主强
        0.6,   # z[1] spread: 让球
        0.5,   # z[2] λ比: 主>客
        0.4,   # z[3] 公平盘: 深盘
        -0.3,  # z[4] 抽水: 低不确定
        -0.2,  # z[5] 波动: 低波动
        -0.5,  # z[6] 熵: 低熵(概率集中)
        0.8,   # z[7] 置信衰减: 高置信
        -0.4,  # z[8] 平局保护: 低保护
        0.5,   # z[9] 偏斜: 向H
        0.3,   # z[10] 价格刚性: 适中
        0.7,   # z[11] 庄家自信: 高自信
        0.6,   # z[12] 资金压力: 主热
        -0.1,  # z[13] 水位趋势: 中性
        0.4,   # z[14] 热钱: 向主
        0.2,   # z[15] 资金分歧: 低分歧
    ]),
    # 平局原型: 实力差小(z[0]≈0)、熵高(z[6]高)、平局保护高(z[8]高)
    'D': np.array([
        0.0,   # z[0] 实力差: 均衡
        0.0,   # z[1] spread: 无让球
        0.0,   # z[2] λ比: ~1
        -0.1,  # z[3] 公平盘: 浅盘
        0.1,   # z[4] 抽水: 适中不确定
        0.0,   # z[5] 波动: 适中
        0.6,   # z[6] 熵: 高熵(概率均匀)
        -0.2,  # z[7] 置信衰减: 中置信
        0.5,   # z[8] 平局保护: 高保护
        0.0,   # z[9] 偏斜: 对称
        -0.2,  # z[10] 价格刚性: 灵活
        0.3,   # z[11] 庄家自信: 中自信
        0.0,   # z[12] 资金压力: 均衡
        0.0,   # z[13] 水位趋势: 稳定
        -0.2,  # z[14] 热钱: 冷
        0.6,   # z[15] 资金分歧: 高分歧(方向不明)
    ]),
    # 客胜原型: 与主胜镜像
    'A': np.array([
        -0.7,  # z[0] 实力差: 客强
        -0.6,  # z[1] spread: 受让
        -0.5,  # z[2] λ比: 客>主
        -0.4,  # z[3] 公平盘: 客让
        -0.3,  # z[4] 抽水: 低不确定
        -0.2,  # z[5] 波动: 低波动
        -0.5,  # z[6] 熵: 低熵
        0.8,   # z[7] 置信衰减: 高置信
        -0.4,  # z[8] 平局保护: 低保护
        -0.5,  # z[9] 偏斜: 向A
        0.3,   # z[10] 价格刚性: 适中
        0.7,   # z[11] 庄家自信: 高自信
        -0.6,  # z[12] 资金压力: 客热
        0.1,   # z[13] 水位趋势: 中性
        -0.4,  # z[14] 热钱: 向客
        0.2,   # z[15] 资金分歧: 低分歧
    ]),
}


# ═══════════════════════════════════════════════════════════════
# JEPA D-Gate 数据类
# ═══════════════════════════════════════════════════════════════

@dataclass
class JEPADGateResult:
    """JEPA范式D-Gate分析结果"""
    
    # 嵌入空间距离
    dist_to_H: float              # 到主胜原型的距离
    dist_to_D: float              # 到平局原型的距离
    dist_to_A: float              # 到客胜原型的距离
    
    # 基于距离的"概率"(softmax归一化)
    dist_prob_H: float
    dist_prob_D: float
    dist_prob_A: float
    
    # D-Gate可信度 (连续值, 0~1)
    d_credibility: float          # 0 = 完全不可信, 1 = 高度可信
    
    # 与传统D-Gate zone的映射
    traditional_zone: str         # garbage/fuzzy_low/fuzzy/usable/reliable/high_conf
    traditional_margin: float     # 等效的P(D)-max(P(H),P(A))
    
    # 嵌入空间D信号强度
    d_signal_strength: float      # 综合D信号 (0~1)
    
    # 是否建议降级
    should_downgrade: bool
    downgrade_reason: str = ""
    
    # 解释
    interpretation: str = ""
    
    def to_dict(self) -> Dict:
        return {
            'embedding_distances': {
                'to_H': round(self.dist_to_H, 4),
                'to_D': round(self.dist_to_D, 4),
                'to_A': round(self.dist_to_A, 4),
            },
            'distance_probs': {
                'H': round(self.dist_prob_H, 4),
                'D': round(self.dist_prob_D, 4),
                'A': round(self.dist_prob_A, 4),
            },
            'd_credibility': round(self.d_credibility, 4),
            'traditional_zone': self.traditional_zone,
            'traditional_margin': round(self.traditional_margin, 4),
            'd_signal_strength': round(self.d_signal_strength, 4),
            'should_downgrade': self.should_downgrade,
            'downgrade_reason': self.downgrade_reason,
            'interpretation': self.interpretation,
        }


# ═══════════════════════════════════════════════════════════════
# JEPA D-Gate 分析器
# ═══════════════════════════════════════════════════════════════

class JEPADGate:
    """
    JEPA范式 D-Gate — 嵌入空间的平局可信度评估
    
    核心公式:
      d_credibility = softmax_d(-d(z, z_proto_D)) / Σ softmax_i(-d(z, z_proto_i))
      
    传统margin与d_credibility的映射:
      d_credibility ≈ sigmoid(α * margin + β)
      其中 margin = P(D) - max(P(H), P(A))
      
    相比传统D-Gate的优势:
      1. 连续可信度 → 不依赖硬边界
      2. 使用全部16维信息 → 不依赖单一P(D)值
      3. 嵌入距离天然包含赔率结构 → 自动考虑spread/抽水/资金等因素
    """
    
    def __init__(self, encoder=None, 
                 prototypes: Optional[Dict[str, np.ndarray]] = None):
        from .odds_encoder import OddsEncoder
        self.encoder = encoder or OddsEncoder()
        self.prototypes = prototypes or OUTCOME_PROTOTYPES
    
    def compute(self, odds: Dict[str, float],
                model_probs: Optional[Dict[str, float]] = None) -> JEPADGateResult:
        """
        计算JEPA D-Gate结果
        
        Args:
            odds: 赔率字典
            model_probs: (可选) 模型输出的概率 {H, D, A}
        
        Returns:
            JEPADGateResult
        """
        # 编码当前赔率到嵌入空间
        z = self.encoder.encode(odds)
        
        # 计算到三个原型的距离
        d_H = float(np.linalg.norm(z - self.prototypes['H']))
        d_D = float(np.linalg.norm(z - self.prototypes['D']))
        d_A = float(np.linalg.norm(z - self.prototypes['A']))
        
        # Softmax: 距离越小 → 概率越高
        # 使用负距离作为logits (温度τ=1.0)
        tau = 1.0
        logits = np.array([-d_H, -d_D, -d_A]) / tau
        logits -= logits.max()  # 数值稳定
        probs = np.exp(logits)
        probs /= probs.sum()
        
        # D-Gate可信度: 基于距离的D概率
        d_credibility = float(probs[1])
        
        # 传统margin的等效值 (从嵌入距离反推)
        # margin_approx = (probs[1] - max(probs[0], probs[2]))
        traditional_margin = d_credibility - max(float(probs[0]), float(probs[2]))
        
        # 传统zone映射
        traditional_zone = self._map_to_zone(traditional_margin)
        
        # D信号强度: 结合嵌入距离和(可选的)模型概率
        if model_probs:
            model_d = model_probs.get('D', 0.3)
            # 模型D概率 × 嵌入空间d_credibility的几何平均
            d_signal = np.sqrt(model_d * d_credibility)
        else:
            d_signal = d_credibility
        
        # 降级判断
        should_downgrade, downgrade_reason = self._judge_downgrade(
            d_credibility, traditional_margin, z
        )
        
        # 解释
        interpretation = self._interpret(d_credibility, traditional_margin, z)
        
        return JEPADGateResult(
            dist_to_H=d_H, dist_to_D=d_D, dist_to_A=d_A,
            dist_prob_H=float(probs[0]),
            dist_prob_D=d_credibility,
            dist_prob_A=float(probs[2]),
            d_credibility=d_credibility,
            traditional_zone=traditional_zone,
            traditional_margin=traditional_margin,
            d_signal_strength=float(d_signal),
            should_downgrade=should_downgrade,
            downgrade_reason=downgrade_reason,
            interpretation=interpretation,
        )
    
    def _map_to_zone(self, margin: float) -> str:
        """将传统margin映射到D-Gate zone"""
        if margin < 0.02:
            return "garbage"
        elif margin < 0.05:
            return "fuzzy_low"
        elif margin < 0.10:
            return "fuzzy"
        elif margin < 0.20:
            return "usable"
        elif margin < 0.40:
            return "reliable"
        else:
            return "high_conf"
    
    def _judge_downgrade(self, 
                          d_credibility: float, 
                          traditional_margin: float,
                          z: np.ndarray) -> Tuple[bool, str]:
        """
        判断是否应该降级D预测
        
        使用多维信息而非单一阈值:
          1. 嵌入空间的D可信度 < 0.2 → 降级
          2. 传统margin < 0 (D不是top pick) → 降级
          3. 嵌入中平局保护维(z[8])低但熵维(z[6])高 → 噪声, 不降级
          4. 嵌入中实力差维(z[0])极端 → D概率低, 不需要降级(本身D就不高)
        """
        reasons = []
        
        # 条件1: D可信度过低
        if d_credibility < 0.2:
            reasons.append(f"D可信度极低({d_credibility:.3f})")
        
        # 条件2: D在嵌入空间中距离原型最远
        if d_credibility < 0.25:
            reasons.append("D在嵌入空间中距离原型最远")
        
        # 条件3: 综合判断
        # 检查嵌入特征
        strength_dim = abs(z[0])  # 实力差
        entropy_dim = z[6]        # 市场熵
        draw_protection = z[8]    # 平局保护
        
        # 实力悬殊 → D本来就低，不需额外降级
        if strength_dim > 0.7 and d_credibility < 0.3:
            return True, "实力悬殊(embedding z[0]={:.2f}) + D可信度低".format(strength_dim)
        
        # 高熵+低平局保护 → D信号混乱
        if entropy_dim > 0.5 and draw_protection < 0 and d_credibility < 0.4:
            return True, f"高熵({entropy_dim:.2f})+低平局保护({draw_protection:.2f})"
        
        if len(reasons) >= 2:
            return True, "; ".join(reasons)
        elif len(reasons) == 1:
            return True, reasons[0]
        
        return False, ""
    
    def _interpret(self,
                   d_credibility: float,
                   traditional_margin: float,
                   z: np.ndarray) -> str:
        """生成人类可读的D-Gate解释"""
        
        if d_credibility > 0.6:
            base = "嵌入空间中平局信号极强: "
        elif d_credibility > 0.4:
            base = "嵌入空间中平局信号中等: "
        elif d_credibility > 0.2:
            base = "嵌入空间中平局信号较弱: "
        else:
            base = "嵌入空间中平局信号极弱: "
        
        # 从嵌入维度提取原因
        reasons = []
        strength = abs(z[0])
        entropy = z[6]
        draw_prot = z[8]
        
        if strength < 0.2:
            reasons.append("实力接近")
        if entropy > 0.4:
            reasons.append("市场不确定性高")
        if draw_prot > 0.3:
            reasons.append("庄家积极保护平局")
        if abs(z[12]) < 0.2:
            reasons.append("资金均衡")
        
        if reasons:
            base += ", ".join(reasons)
        else:
            base += "无显著平局特征"
        
        base += f" (传统margin={traditional_margin:.3f})"
        
        return base
    
    def compare_with_traditional(self,
                                  odds: Dict[str, float],
                                  h_prob: float, d_prob: float, a_prob: float
                                  ) -> Dict:
        """
        与D-Gate进行对比分析
        
        显示JEPA D-Gate和传统D-Gate的差异及原因。
        """
        trad_margin = d_prob - max(h_prob, a_prob)
        jepa_result = self.compute(odds, model_probs={'H': h_prob, 'D': d_prob, 'A': a_prob})
        
        # 差异分析
        trad_zone = self._map_to_zone(trad_margin)
        jepa_zone = jepa_result.traditional_zone
        
        if trad_zone != jepa_zone:
            disagreement = (
                f"JEPA D-Gate与传统D-Gate存在分歧: "
                f"传统={trad_zone}(margin={trad_margin:.3f}) vs "
                f"JEPA={jepa_zone}(d_cred={jepa_result.d_credibility:.3f})"
            )
            # 分析分歧原因
            if trad_margin > 0.1 and jepa_result.d_credibility < 0.3:
                disagreement += (
                    " — 传统认为D可信但JEPA不认，"
                    "可能是因为嵌入空间中存在矛盾信号(如资金过热/熵高等)"
                )
            elif trad_margin < 0.05 and jepa_result.d_credibility > 0.4:
                disagreement += (
                    " — 传统认为D不可信但JEPA认可，"
                    "可能是因为嵌入空间的非概率维度(庄家态度/市场结构)支持D"
                )
        else:
            disagreement = f"JEPA与传统一致: {trad_zone}"
        
        return {
            'jepa_credibility': jepa_result.d_credibility,
            'traditional_margin': trad_margin,
            'jepa_zone': jepa_zone,
            'traditional_zone': trad_zone,
            'agreement': trad_zone == jepa_zone,
            'disagreement_analysis': disagreement,
        }


# ═══════════════════════════════════════════════════════════════
# 嵌入空间D-Gate快速入口
# ═══════════════════════════════════════════════════════════════

_d_gate_instance: Optional[JEPADGate] = None


def get_jepa_d_gate() -> JEPADGate:
    global _d_gate_instance
    if _d_gate_instance is None:
        _d_gate_instance = JEPADGate()
    return _d_gate_instance


def quick_d_gate(odds_h: float, odds_d: float, odds_a: float,
                 h_prob: float = None, d_prob: float = None, a_prob: float = None,
                 **kwargs) -> JEPADGateResult:
    """
    快速D-Gate分析入口
    
    示例:
        result = quick_d_gate(1.80, 3.50, 4.20)
        print(f"D可信度: {result.d_credibility:.3f}, zone: {result.traditional_zone}")
    """
    from .odds_encoder import compute_odds_embedding
    
    odds = {
        'odds_h': odds_h, 'odds_d': odds_d, 'odds_a': odds_a,
        **kwargs,
    }
    
    model_probs = None
    if all(p is not None for p in [h_prob, d_prob, a_prob]):
        model_probs = {'H': h_prob, 'D': d_prob, 'A': a_prob}
    
    return get_jepa_d_gate().compute(odds, model_probs)

"""
哨响AI — 赔率信号质量评估模块 v1.0
==============================================
杜博弈 (博弈论赔率逆向专家) — 基于31万条回测诊断

核心理念:
  赔率是庄家的加密协议。信号的有效性取决于庄家在定价中暴露了多少信息。
  庄家在极度热门区间无法隐藏 → 信号可信。
  庄家在模糊区间主动制造不确定性 → 信号不可信。
  庄家在赔率稳定时已锁定判断 → 信号可信。
  庄家在赔率剧烈波动时跟随市场 → 信号不可信。

回测数据源: pipeline/reports/backtest_312k_20260619_172335.json
  - OTSM LOCKED>0.8: 9K场, Acc=62.98% (S级信号)
  - drift≤0.02: 14K场, Acc=55.99% (A级信号)
  - spread>0.50: 186K场, Acc=55.41% (A级信号)
  - drift_sharp=1: 57K场, Acc=51.26% (F级伪信号)
  - spread 0.03-0.08: 1.4K场, Acc=33.08% (F级陷阱区)

使用:
  from features.signal_quality import SignalQualityEvaluator
  sq = SignalQualityEvaluator()
  result = sq.evaluate(row)  # row 来自 training_extended
"""

import numpy as np
from typing import Dict, Tuple, Optional


class SignalQualityEvaluator:
    """
    赔率信号质量评估器
    
    对每场比赛从三个维度评估信号质量:
    1. SPREAD: 赔率差反映的庄家定价信心
    2. DRIFT:  赔率漂移反映的庄家判断稳定性
    3. OTSM:  时序状态机反映的庄家风控状态
    
    输出:
    - signal_quality: 综合信号质量分 [0, 1]
    - signal_tier:    信号等级 (S/A/B/C/F)
    - can_bet:        是否可投注 (布尔)
    - reason:         判定理由 (字符串)
    """

    # ── 阈值（来自31万条回测）──
    # Spread 阈值
    SPREAD_STRONG = 0.50          # 强热门下限
    SPREAD_DANGER_LOW = 0.03      # 危险区下限
    SPREAD_DANGER_HIGH = 0.50     # 危险区上限
    
    # Drift 阈值 (imp_home_change = imp_open - imp_close)
    DRIFT_STABLE = 0.02           # 稳定赔率上限
    DRIFT_CHAOTIC = 0.12          # 剧烈波动下限（对应 drift_sharp=1 的阈值）
    DRIFT_MODERATE = 0.05         # 中等波动下限
    
    # OTSM 阈值
    OTSM_HIGH = 0.8               # 极高置信（已验证 Acc=63%）
    OTSM_MEDIUM = 0.5             # 中高置信（待回测验证）
    OTSM_LOW = 0.2                # 低置信（基线附近）

    def __init__(self):
        pass

    # ═══════════════════════════════════════════════════════════
    # 核心评估
    # ═══════════════════════════════════════════════════════════

    def evaluate(self, row: Dict) -> Dict:
        """
        单场信号质量评估
        
        Args:
            row: 一行 training_extended 数据，需包含:
                 - odds_spread: float
                 - drift_magnitude: float (隐含概率变化幅度的缩放值)
                 - otsm_state_LOCKED: float (0/1 或 lock_confidence)
                 - otsm_lock_confidence: float (可选，更精确)
                 - drift_sharp_signal: float (可选，用于检测伪信号)
                 
        Returns:
            {
                'signal_quality': float,   # [0,1] 综合质量分
                'signal_tier': str,        # S/A/B/C/F
                'can_bet': bool,
                'spread_safety': int,      # 2=SAFE, 1=UNCERTAIN, 0=DANGER
                'drift_quality': int,      # 2=STABLE, 1=MODERATE, 0=CHAOTIC
                'otsm_level': int,         # 3=HIGH, 2=MED, 1=LOW, 0=NOISE
                'reason': str,
                'conf_boost': float,       # 准确率加成预估
            }
        """
        spread = row.get('odds_spread', 0.0)
        drift_mag = row.get('drift_magnitude', 0.0)
        
        # OTSM lock_confidence (优先使用连续值，其次 one-hot)
        lock_conf = row.get('otsm_lock_confidence', 
                   row.get('otsm_state_LOCKED', 0.0))
        
        # ── SPREAD 评估 ──
        spread_safety, spread_reason, spread_boost = self._eval_spread(spread)
        
        # ── DRIFT 评估 ──
        drift_quality, drift_reason, drift_boost = self._eval_drift(drift_mag)
        
        # ── OTSM 评估 ──
        otsm_level, otsm_reason, otsm_boost = self._eval_otsm(lock_conf)
        
        # ── 综合评分 ──
        # 基础分 0.5 (基线 Acc=51.74%)
        base_score = 0.5
        
        # 加权融合
        # spread 权重最高 (样本量最大，区分度最好)
        # otsm 其次 (区分度最大但覆盖少)
        # drift 辅助
        composite = base_score + spread_boost * 0.45 + drift_boost * 0.25 + otsm_boost * 0.30
        composite = max(0.0, min(1.0, composite))
        
        # ── 信号等级 ──
        if composite >= 0.85:
            tier = 'S'
            can_bet = True
        elif composite >= 0.70:
            tier = 'A'
            can_bet = True
        elif composite >= 0.55:
            tier = 'B'
            can_bet = True
        elif composite >= 0.45:
            tier = 'C'
            can_bet = False
        else:
            tier = 'F'
            can_bet = False
        
        # 如果 spread 在危险区，强制不可投注
        if spread_safety == 0:
            can_bet = False
            if tier in ('S', 'A', 'B'):
                tier = 'C'  # 降级
        
        total_boost = spread_boost * 0.45 + drift_boost * 0.25 + otsm_boost * 0.30
        
        reasons = [spread_reason, drift_reason, otsm_reason]
        reason = ' | '.join([r for r in reasons if r])
        
        return {
            'signal_quality': round(composite, 4),
            'signal_tier': tier,
            'can_bet': can_bet,
            'spread_safety': spread_safety,
            'drift_quality': drift_quality,
            'otsm_level': otsm_level,
            'reason': reason,
            'conf_boost': round(total_boost, 4),
            'est_accuracy': round(0.5174 + total_boost, 4),  # 基线 + 加成
        }

    # ═══════════════════════════════════════════════════════════
    # SPREAD 评估
    # ═══════════════════════════════════════════════════════════

    def _eval_spread(self, spread: float) -> Tuple[int, str, float]:
        """
        Spread 安全等级评估
        
        回测依据:
        - |spread| > 0.50: 186K场 Acc=55.41% → SAFE (+4pp vs baseline)
        - 0.03 ≤ |spread| ≤ 0.50: 28K场 Acc=35-38% → DANGER (-14pp!)
        - |spread| < 0.03: 98K场 Acc=48.93% → UNCERTAIN (-3pp)
        """
        abs_spread = abs(spread)
        
        if abs_spread > self.SPREAD_STRONG:
            return 2, 'SPREAD:强热门(SAFE,+4pp)', 0.04
        elif abs_spread < self.SPREAD_DANGER_LOW:
            return 1, 'SPREAD:极度均衡(UNCERTAIN,-3pp)', -0.03
        elif self.SPREAD_DANGER_LOW <= abs_spread <= self.SPREAD_DANGER_HIGH:
            return 0, 'SPREAD:模糊区(DANGER,-14pp)', -0.14
        else:
            # |spread| > 0.50 但负值（客队强热门）— 对称处理
            return 2, 'SPREAD:客强热门(SAFE,+4pp)', 0.04

    def compute_spread_safety(self, spread: float) -> int:
        """简化接口：仅返回安全等级"""
        safety, _, _ = self._eval_spread(spread)
        return safety

    # ═══════════════════════════════════════════════════════════
    # DRIFT 评估
    # ═══════════════════════════════════════════════════════════

    def _eval_drift(self, drift_magnitude: float) -> Tuple[int, str, float]:
        """
        Drift 质量评估
        
        回测依据:
        - drift ≤ 0.02: 14K场 Acc=55.99% → STABLE (+4pp)
        - drift > 0.05: 165K场 Acc=50.97% → (-1pp vs baseline)
        - drift_sharp=1 (~drift>0.12): 57K场 Acc=51.26% → CHAOTIC (-0.5pp)
        
        注: drift_magnitude = clip(|imp_home_change| * 5, 0, 1)
             所以 drift_mag=0.02 → |imp_change|=0.004 (极小变化)
             drift_mag=0.05 → |imp_change|=0.01
             drift_mag=0.12 → |imp_change|=0.024
             drift_sharp=1 → |imp_change|≥0.125 (对应drift_mag≈0.625)
        """
        if drift_magnitude is None or drift_magnitude < 0:
            drift_magnitude = 0.0
        
        if drift_magnitude <= self.DRIFT_STABLE:
            return 2, f'DRIFT:稳定(STABLE,+4pp)', 0.04
        elif drift_magnitude <= self.DRIFT_MODERATE:
            return 1, 'DRIFT:正常(MODERATE,基线)', 0.0
        elif drift_magnitude <= self.DRIFT_CHAOTIC:
            return 1, 'DRIFT:偏高(MODERATE,-1pp)', -0.01
        else:
            return 0, 'DRIFT:剧烈(CHAOTIC,-2pp)', -0.02

    def compute_drift_quality(self, drift_magnitude: float) -> int:
        """简化接口：仅返回质量等级"""
        quality, _, _ = self._eval_drift(drift_magnitude)
        return quality

    # ═══════════════════════════════════════════════════════════
    # OTSM 评估
    # ═══════════════════════════════════════════════════════════

    def _eval_otsm(self, lock_confidence: float) -> Tuple[int, str, float]:
        """
        OTSM LOCKED 等级评估
        
        回测依据:
        - LOCKED>0.8: 9K场 Acc=62.98% → HIGH (+11pp!!)
        - LOCKED<0.2: 303K场 Acc=51.40% → NOISE (-0.3pp)
        - LOCKED 0.5-0.8: 待验证，预计 Acc≈60% (+8pp)
        
        注: lock_confidence 线性映射自熵漂移分位数:
             P20=0 → P80=0.6 → P90=0.9 → P95+≈1.0
        """
        if lock_confidence is None:
            lock_confidence = 0.0
        
        if lock_confidence > self.OTSM_HIGH:
            return 3, f'OTSM:极高锁定(HIGH,+11pp)', 0.11
        elif lock_confidence > self.OTSM_MEDIUM:
            return 2, f'OTSM:中高锁定(MED,+8pp)', 0.08
        elif lock_confidence > self.OTSM_LOW:
            return 1, 'OTSM:低信号(LOW,基线)', 0.0
        else:
            return 0, 'OTSM:噪声(NOISE,-0.3pp)', -0.003

    def compute_otsm_level(self, lock_confidence: float) -> int:
        """简化接口：仅返回OTSM等级"""
        level, _, _ = self._eval_otsm(lock_confidence)
        return level

    # ═══════════════════════════════════════════════════════════
    # 复合信号（便捷方法）
    # ═══════════════════════════════════════════════════════════

    def compute_composite_score(self, row: Dict) -> float:
        """快速计算复合信号分"""
        result = self.evaluate(row)
        return result['signal_quality']

    def is_bettable(self, row: Dict) -> bool:
        """快速判断是否可投注"""
        result = self.evaluate(row)
        return result['can_bet']

    def filter_bettable(self, rows: list) -> list:
        """从列表中筛选可投注场次"""
        return [r for r in rows if self.is_bettable(r)]


# ═══════════════════════════════════════════════════════════════
# 便捷函数（无需实例化）
# ═══════════════════════════════════════════════════════════════

_evaluator = SignalQualityEvaluator()

def evaluate_signal(row: Dict) -> Dict:
    """便捷函数：单场信号评估"""
    return _evaluator.evaluate(row)

def compute_composite_score(row: Dict) -> float:
    """便捷函数：复合信号分"""
    return _evaluator.compute_composite_score(row)

def is_bettable(row: Dict) -> bool:
    """便捷函数：是否可投注"""
    return _evaluator.is_bettable(row)


# ═══════════════════════════════════════════════════════════════
# SQL 片段（供 pipeline 使用）
# ═══════════════════════════════════════════════════════════════

# 可投注场次筛选 SQL
BETTABLE_SQL = """
    -- 可投注场次筛选（来自31万条回测诊断）
    -- 条件1: 强热门（|spread| > 0.50）
    -- 条件2: 赔率稳定（drift_magnitude ≤ 0.02）
    -- 条件3: OTSM高置信锁定（lock_confidence > 0.5）
    -- 任一满足即纳入，但需排除危险spread区间
    
    SELECT * FROM training_extended
    WHERE (
        -- 强热门 + 非混沌漂移
        (ABS(odds_spread) > 0.50 AND drift_magnitude <= 0.12)
        OR
        -- 稳定赔率
        (drift_magnitude <= 0.02)
        OR  
        -- OTSM高锁定
        (otsm_lock_confidence > 0.5)
    )
    -- 排除危险区（中热/微热/均衡且无OTSM确认）
    AND NOT (
        ABS(odds_spread) BETWEEN 0.03 AND 0.50
        AND otsm_lock_confidence <= 0.5
    )
"""


if __name__ == "__main__":
    # 简单测试
    sq = SignalQualityEvaluator()
    
    # 测试案例1: 强热门 + 稳定赔率 + 高OTSM
    test1 = {
        'odds_spread': 0.80,           # 主队强热门
        'drift_magnitude': 0.01,       # 极稳定
        'otsm_lock_confidence': 0.85,  # 极高锁定
    }
    r1 = sq.evaluate(test1)
    print(f"案例1 (三重确认): {r1['signal_tier']}级, Q={r1['signal_quality']:.3f}, "
          f"估准={r1['est_accuracy']:.1%}, {r1['reason']}")
    
    # 测试案例2: 模糊区 + 大漂移
    test2 = {
        'odds_spread': 0.15,           # 微热（危险区）
        'drift_magnitude': 0.15,       # 剧烈波动
        'otsm_lock_confidence': 0.1,   # 噪声
    }
    r2 = sq.evaluate(test2)
    print(f"案例2 (三重否定): {r2['signal_tier']}级, Q={r2['signal_quality']:.3f}, "
          f"估准={r2['est_accuracy']:.1%}, {r2['reason']}")
    
    # 测试案例3: 极度均衡
    test3 = {
        'odds_spread': 0.01,
        'drift_magnitude': 0.03,
        'otsm_lock_confidence': 0.3,
    }
    r3 = sq.evaluate(test3)
    print(f"案例3 (极度均衡): {r3['signal_tier']}级, Q={r3['signal_quality']:.3f}, "
          f"估准={r3['est_accuracy']:.1%}, {r3['reason']}")
    
    print("\n✅ 信号质量评估模块就绪")

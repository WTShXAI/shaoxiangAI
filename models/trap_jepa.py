"""
JEPA Trap Detector — 嵌入空间陷阱检测 (JEPA范式重写)
======================================================
LeCun JEPA 重解释传统诱盘/陷阱概念

传统解释:
  "庄家故意误导投注者" → 这是一种拟人化解读，缺乏数学基础

JEPA 解释:
  陷阱 = 庄家 Predictor 预测的嵌入 vs 市场定价的嵌入之间的不一致
  
  庄家的世界模型预演到某个"高概率路径" (ẑ_target)，
  但资金结构/市场力量不允许按这个路径定价。
  于是庄家必须维持一个与Predictor预测不一致的定价 (z_market)。

  我们观察到的"诱盘" = 庄家Predictor的预测方向与定价方向之间的角度:
    cos(Δẑ, Δz_market) < 0 → 陷阱 (庄家定价与信念相反)
    cos(Δẑ, Δz_market) ≈ 0 → 中性 (庄家定价独立于信念)
    cos(Δẑ, Δz_market) > 0 → 真诚 (庄家定价反映信念)

新陷阱检测公式:
  trap_score = W_align * alignment_loss + W_mag * magnitude_loss
  alignment_loss = max(0, -cos(Δẑ, Δz_market))  ← 方向背离
  magnitude_loss = | ||Δz_market|| - ||Δẑ|| | / max(||Δz_market||, ||Δẑ||) ← 幅度矛盾

相比传统TrapDetector (16引擎规则):
  - 传统: 基于经验规则的"模式匹配"
  - JEPA: 基于嵌入空间几何的"结构检测"
  - 统一了16种陷阱类型为嵌入空间中的单一几何度量
  - 可证明的防坍塌 (VICReg保证嵌入空间的语义一致性)

作者: 杜博弈 / FootballAI v4.1 JEPA Redesign
日期: 2026-06-20
"""

import numpy as np
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
import logging

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# JEPA陷阱检测数据结构
# ═══════════════════════════════════════════════════════════════

class JEPATrapType(Enum):
    """JEPA范式陷阱类型 — 统一为嵌入空间几何类别"""
    PREDICTION_MISPRICING = "prediction_mispricing"    # 预测-定价背离
    EMBEDDING_COLLAPSE = "embedding_collapse"          # 嵌入坍塌 (编码器退化)
    LATENT_CONFLICT = "latent_conflict"                # 潜在矛盾 (多维度冲突)
    DIVERGENCE_ANOMALY = "divergence_anomaly"          # 分歧异常 (单机构反常)
    TRAJECTORY_INCONSISTENCY = "trajectory_inconsistency"  # 轨迹不一致

@dataclass
class JEPATrapSignal:
    """JEPA范式陷阱信号"""
    trap_type: str
    severity: float                     # 严重度 [0, 1]
    embedding_evidence: Dict[str, float]  # 嵌入空间中的证据
    affected_dimensions: List[int]       # 受影响的嵌入维度
    interpretation: str
    signal_grade: str = "C"             # S/A/B/C/F

@dataclass
class JEPATrapReport:
    """JEPA范式陷阱检测报告"""
    match_id: Optional[int] = None
    home: str = ""
    away: str = ""
    
    # 核心度量
    trap_score: float = 0.0            # 综合陷阱分 [0, 1]
    alignment_loss: float = 0.0        # 方向背离度
    magnitude_loss: float = 0.0        # 幅度矛盾度
    
    # 嵌入空间证据
    z_market: Optional[np.ndarray] = None    # 市场定价嵌入
    z_predicted: Optional[np.ndarray] = None  # Predictor预测嵌入
    prediction_market_angle: float = 0.0     # 预测与定价的角度 (度)
    
    # 分解
    active_signals: List[JEPATrapSignal] = field(default_factory=list)
    
    # 与传统TrapType的映射
    traditional_mapping: Dict[str, float] = field(default_factory=dict)
    
    # 结论
    risk_level: str = "SAFE"           # SAFE/SUSPICIOUS/DANGER/HARVESTING
    recommendation: str = ""
    
    def to_dict(self) -> Dict:
        return {
            "trap_score": round(self.trap_score, 4),
            "alignment_loss": round(self.alignment_loss, 4),
            "magnitude_loss": round(self.magnitude_loss, 4),
            "prediction_market_angle": round(self.prediction_market_angle, 2),
            "risk_level": self.risk_level,
            "recommendation": self.recommendation,
            "signals": [
                {"type": s.trap_type, "severity": round(s.severity, 3),
                 "interpretation": s.interpretation}
                for s in self.active_signals
            ],
            "traditional_trap_types": {
                k: round(v, 3) for k, v in self.traditional_mapping.items()
            },
        }

# ═══════════════════════════════════════════════════════════════
# JEPA陷阱检测器
# ═══════════════════════════════════════════════════════════════

class JEPATrapDetector:
    """
    嵌入空间陷阱检测器 — JEPA范式核心组件
    
    核心理念:
      不依赖经验规则 (16引擎)，而是检测嵌入空间的几何结构异常。
      这提供了一种统一的、可证明的陷阱检测框架。
    
    三个检测轴:
      1. 预测-定价对齐: Predictor预测的ẑ vs 市场实际定价的z
      2. 嵌入坍塌检测: 编码器是否在对不同市场状态输出相似嵌入
      3. 多维度冲突: 嵌入的不同子空间是否给出矛盾信号
    """
    
    def __init__(self, encoder=None, predictor=None):
        from .odds_encoder import OddsEncoder
        from .market_predictor import MarketStatePredictor
        self.encoder = encoder or OddsEncoder()
        self.predictor = predictor or MarketStatePredictor()
        
        # 传统TrapType到嵌入维度的映射
        self._trap_to_dims = self._build_trap_dimension_map()
    
    def _build_trap_dimension_map(self) -> Dict[str, List[int]]:
        """
        传统16种TrapType → 嵌入维度映射
        
        每种传统陷阱类型在嵌入空间中对应特定的维度扰动模式。
        这使得JEPA检测器可以向后兼容传统分类。
        """
        return {
            "SHALLOW_HOT":         [0, 1, 3, 12],     # 实力差+盘口+资金
            "DROP_ODDS_RISE_WATER": [5, 10, 12, 13],  # 波动+刚性+资金+水位
            "RISE_HANDICAP_DROP_WATER": [3, 5, 12, 14],  # 盘口+波动+资金+热钱
            "HALF_BALL_HIGH_WATER": [3, 8, 13],        # 盘口+平局保护+水位
            "LAST_MINUTE_CHANGE":  [5, 7, 10],          # 波动+衰减+刚性
            "DEEP_HANDICAP_TRAP":  [0, 1, 3, 9],       # 实力差+spread+盘口+偏斜
            "OVERROUND_ANOMALY":   [4, 11, 8],          # 抽水+自信+平局保护
            "SCORE_ODDS_BARRIER":  [4, 5, 8, 11],      # 抽水+波动+平局保护+自信
            "KELLY_DIVERGENCE":    [6, 11, 14],         # 熵+自信+热钱
            "DEEP_COOLING":        [3, 9, 13],          # 盘口+偏斜+水位
            "FUND_IMBALANCE":      [12, 14, 15],        # 资金+热钱+分歧
            "HISTORICAL_BIAS":     [0, 7],              # 实力差+衰减
            "COUNTER_THREAT_LOW":  [0, 2, 9],           # 实力差+λ比+偏斜
        }
    
    def detect(self,
               odds_current: Dict[str, float],
               odds_open: Optional[Dict[str, float]] = None,
               multi_institution: Optional[Dict[str, Dict[str, float]]] = None,
               home: str = "", away: str = "") -> JEPATrapReport:
        """
        JEPA范式陷阱检测
        
        Args:
            odds_current: 当前赔率
            odds_open: 初盘赔率 (用于计算漂移轨迹)
            multi_institution: 多机构赔率 (用于检测单机构异常)
            home, away: 球队名
        
        Returns:
            JEPATrapReport
        """
        report = JEPATrapReport(home=home, away=away)
        
        # ── 轴1: 预测-定价对齐 ──
        z_market = self.encoder.encode(odds_current)
        z_predicted = self.predictor.predict(z_market)
        report.z_market = z_market
        report.z_predicted = z_predicted
        
        # 计算对齐度
        alignment, magnitude = self._compute_prediction_market_alignment(
            odds_current, odds_open
        )
        report.alignment_loss = alignment
        report.magnitude_loss = magnitude
        
        # 综合陷阱分
        report.trap_score = min(1.0, 
            0.6 * alignment + 0.4 * magnitude
        )
        
        # ── 轴2: 嵌入坍塌检测 ──
        collapse_signal = self._detect_embedding_collapse(odds_current, odds_open)
        if collapse_signal:
            report.active_signals.append(collapse_signal)
            report.trap_score = min(1.0, report.trap_score + collapse_signal.severity * 0.3)
        
        # ── 轴3: 多维度冲突 ──
        conflict_signals = self._detect_latent_conflicts(z_market)
        report.active_signals.extend(conflict_signals)
        for s in conflict_signals:
            report.trap_score = min(1.0, report.trap_score + s.severity * 0.2)
        
        # ── 轴4: 多机构分歧异常 (如有) ──
        if multi_institution:
            div_signals = self._detect_divergence_anomaly(multi_institution)
            report.active_signals.extend(div_signals)
            for s in div_signals:
                report.trap_score = min(1.0, report.trap_score + s.severity * 0.25)
        
        # ── 传统TrapType映射 ──
        report.traditional_mapping = self._map_to_traditional(z_market, odds_current)
        
        # ── 角度计算 ──
        if odds_open:
            z_open = self.encoder.encode(odds_open)
            delta_pred = z_predicted - z_open
            delta_market = z_market - z_open
            report.prediction_market_angle = self._angle_between(delta_pred, delta_market)
        
        # ── 风险评级 ──
        report.risk_level = self._assess_risk(report.trap_score)
        report.recommendation = self._generate_recommendation(report)
        
        return report
    
    def _compute_prediction_market_alignment(self,
                                              odds_current: Dict[str, float],
                                              odds_open: Optional[Dict[str, float]] = None
                                              ) -> Tuple[float, float]:
        """
        计算预测-定价对齐度
        
        核心公式:
          alignment_loss = max(0, -cos(Δẑ, Δz_market))
          magnitude_loss = | ||Δz_market|| - ||Δẑ|| | / max(...)
        """
        z_current = self.encoder.encode(odds_current)
        z_predicted = self.predictor.predict(z_current)
        
        if odds_open is None:
            # 无初盘数据时，比较当前嵌入与预测嵌入的方向
            delta_pred = z_predicted - z_current
            delta_market = np.zeros_like(z_current)  # 假设当前为稳定点
            
            # 此时主要看Predictor预测的幅度
            magnitude_loss = min(1.0, np.linalg.norm(delta_pred) / 0.5)
            alignment_loss = 0.5  # 中性
        else:
            z_open = self.encoder.encode(odds_open)
            delta_pred = z_predicted - z_open
            delta_market = z_current - z_open
            
            # 方向对齐
            dot = np.dot(delta_pred, delta_market)
            norm_pred = np.linalg.norm(delta_pred) + 1e-8
            norm_market = np.linalg.norm(delta_market) + 1e-8
            cos_angle = dot / (norm_pred * norm_market)
            
            # alignment_loss: 当方向相反时高
            alignment_loss = max(0.0, float(-cos_angle))
            
            # magnitude_loss: 当幅度差异大时高
            magnitude_loss = abs(norm_pred - norm_market) / max(norm_pred, norm_market, 1e-8)
            magnitude_loss = min(1.0, float(magnitude_loss))
        
        return alignment_loss, magnitude_loss
    
    def _detect_embedding_collapse(self,
                                    odds_current: Dict[str, float],
                                    odds_open: Optional[Dict[str, float]] = None
                                    ) -> Optional[JEPATrapSignal]:
        """
        检测嵌入坍塌 — 编码器是否退化为对所有输入输出相似值
        
        LeCun的防坍塌原理:
          VICReg通过方差/协方差约束防止坍塌。
          当多个不同赔率结构被映射到几乎相同的嵌入时 → 坍塌已发生。
        
        检测方法:
          如果 ||Encoder(odds_H_dominant) - Encoder(odds_A_dominant)|| < ε
          → 编码器无法区分强弱对比 → 坍塌
        """
        # 构造两个极端情况的赔率
        odds_strong_home = {
            **odds_current,
            'odds_h': 1.30, 'odds_d': 5.00, 'odds_a': 10.00,
            'lam_h': 3.0, 'lam_a': 0.5,
        }
        odds_strong_away = {
            **odds_current,
            'odds_h': 10.00, 'odds_d': 5.00, 'odds_a': 1.30,
            'lam_h': 0.5, 'lam_a': 3.0,
        }
        
        z_home = self.encoder.encode(odds_strong_home)
        z_away = self.encoder.encode(odds_strong_away)
        
        # 计算两个嵌入的距离
        dist = np.linalg.norm(z_home - z_away)
        
        # 如果距离太小 (< 0.3 after 16-dim normalization) → 坍塌
        if dist < 0.2:
            return JEPATrapSignal(
                trap_type=JEPATrapType.EMBEDDING_COLLAPSE.value,
                severity=min(1.0, (0.2 - dist) / 0.2),
                embedding_evidence={'collapse_distance': float(dist)},
                affected_dimensions=list(range(16)),
                interpretation=f"编码器坍塌风险: 主强vs客強的嵌入距离仅{dist:.3f}, "
                              f"编码器无法有效区分强弱对比",
                signal_grade='F',  # 坍塌是系统性问题, 最高警报
            )
        
        return None
    
    def _detect_latent_conflicts(self, z: np.ndarray) -> List[JEPATrapSignal]:
        """
        检测嵌入空间中的潜在矛盾
        
        将嵌入分为4个子空间:
          - z[0:4]:   实力差分量
          - z[4:8]:   不确定性分量
          - z[8:12]:  庄家态度分量
          - z[12:16]: 资金流分量
        
        矛盾检测:
          - 当实力差分量指向"主强"但资金流分量指向"客热" → 矛盾
          - 当庄家态度分量指向"自信"但不确定性分量指向"高不确定" → 矛盾
        """
        signals = []
        
        strength = z[0:4]       # 实力差
        uncertainty = z[4:8]    # 不确定性
        attitude = z[8:12]      # 庄家态度
        fund = z[12:16]         # 资金流
        
        # 矛盾1: 实力差 vs 资金流
        # 如果实力差指向主强 (z[0]>0) 但资金流向客 (z[12]<0)
        strength_dir = np.mean(strength)
        fund_dir = np.mean(fund)
        
        if abs(strength_dir) > 0.3 and abs(fund_dir) > 0.3:
            if strength_dir * fund_dir < 0:
                # 实力和资金方向相反
                severity = min(0.9, abs(strength_dir) * abs(fund_dir) * 3.0)
                signals.append(JEPATrapSignal(
                    trap_type=JEPATrapType.LATENT_CONFLICT.value,
                    severity=severity,
                    embedding_evidence={
                        'strength_dir': float(strength_dir),
                        'fund_dir': float(fund_dir),
                    },
                    affected_dimensions=[0, 1, 2, 3, 12, 13, 14, 15],
                    interpretation=(
                        f"实力-资金矛盾: 实力差指向{'主强' if strength_dir>0 else '客强'} "
                        f"但资金流向{'主热' if fund_dir>0 else '客热'}"
                    ),
                    signal_grade='B',
                ))
        
        # 矛盾2: 庄家态度 vs 不确定性
        # 如果庄家态度指向"自信"(z[11]>0.5)但不确定性高(z[4]>0.3)
        confidence = float(attitude[3])   # z[11]: bookmaker_confidence
        uncertainty_sig = float(uncertainty[0])  # z[4]: overround_signal
        
        if confidence > 0.5 and uncertainty_sig > 0.3:
            severity = min(0.8, (confidence - 0.5) * uncertainty_sig * 4.0)
            signals.append(JEPATrapSignal(
                trap_type=JEPATrapType.LATENT_CONFLICT.value,
                severity=severity,
                embedding_evidence={
                    'confidence': confidence,
                    'uncertainty': uncertainty_sig,
                },
                affected_dimensions=[4, 5, 11],
                interpretation=(
                    f"庄家自信({confidence:.2f})但不确定性也高({uncertainty_sig:.2f}), "
                    f"存在信号矛盾"
                ),
                signal_grade='B',
            ))
        
        # 矛盾3: 平局保护 vs 资金集中
        draw_prot = float(attitude[0])    # z[8]: draw_protection
        vol_press = float(fund[0])        # z[12]: volume_pressure
        
        if abs(draw_prot) > 0.3 and abs(vol_press) > 0.5:
            # 庄家在保护平局但资金集中在热门 → 矛盾
            severity = abs(draw_prot) * abs(vol_press) * 1.5
            signals.append(JEPATrapSignal(
                trap_type=JEPATrapType.LATENT_CONFLICT.value,
                severity=min(0.85, severity),
                embedding_evidence={
                    'draw_protection': float(draw_prot),
                    'volume_pressure': float(vol_press),
                },
                affected_dimensions=[8, 12, 14],
                interpretation=(
                    f"庄家保护平局(保护度={draw_prot:.2f})但资金集中在热门"
                    f"(压力={vol_press:.2f}) → 庄家在铺设安全网"
                ),
                signal_grade='A',
            ))
        
        return signals
    
    def _detect_divergence_anomaly(self,
                                    multi_institution: Dict[str, Dict[str, float]]
                                    ) -> List[JEPATrapSignal]:
        """
        检测多机构分歧中的异常模式
        
        单机构在信息维度上偏离 = 可能掌握了额外信息
        这个信息如果与其他机构的定价方向矛盾 → 陷阱信号
        """
        from .divergence_analyzer import MultiInstitutionDivergenceAnalyzer
        div_analyzer = MultiInstitutionDivergenceAnalyzer(self.encoder)
        div_report = div_analyzer.analyze(multi_institution)
        
        signals = []
        
        for outlier in div_report.outliers:
            if outlier['is_info_dim_outlier']:
                signals.append(JEPATrapSignal(
                    trap_type=JEPATrapType.DIVERGENCE_ANOMALY.value,
                    severity=min(0.90, outlier['z_score'] / 3.0),
                    embedding_evidence={
                        'z_score': outlier['z_score'],
                        'distance': outlier['distance'],
                    },
                    affected_dimensions=[
                        d['dim'] for d in outlier['top_divergent_dims']
                    ],
                    interpretation=(
                        f"{outlier['institution']}在信息维度上显著偏离"
                        f"(z={outlier['z_score']:.1f}σ), "
                        f"{outlier['interpretation']}"
                    ),
                    signal_grade='S' if outlier['z_score'] > 2.5 else 'A',
                ))
        
        return signals
    
    def _map_to_traditional(self, 
                             z: np.ndarray, 
                             odds: Dict[str, float]) -> Dict[str, float]:
        """
        将嵌入空间的异常映射回传统16种TrapType
        
        每种TrapType对应嵌入空间特定维度的激活模式。
        通过计算嵌入各维度偏离度与TrapType模式的cosine相似度来映射。
        """
        mapping = {}
        
        for trap_name, dims in self._trap_to_dims.items():
            # 计算该TrapType在对应维度上的平均激活强度
            dim_values = [abs(z[d]) for d in dims]
            activation = float(np.mean(dim_values))
            
            # 归一化: 将激活映射到[0,1]
            # 一般z的各维度在[-1,1], abs后在[0,1]
            mapping[trap_name] = round(activation, 3)
        
        return mapping
    
    def _angle_between(self, v1: np.ndarray, v2: np.ndarray) -> float:
        """计算两个向量的夹角 (度)"""
        dot = np.dot(v1, v2)
        norms = np.linalg.norm(v1) * np.linalg.norm(v2)
        if norms < 1e-8:
            return 0.0
        cos_angle = np.clip(dot / norms, -1.0, 1.0)
        return float(np.degrees(np.arccos(cos_angle)))
    
    def _assess_risk(self, trap_score: float) -> str:
        if trap_score > 0.7:
            return "HARVESTING"
        elif trap_score > 0.50:
            return "DANGER"
        elif trap_score > 0.30:
            return "SUSPICIOUS"
        else:
            return "SAFE"
    
    def _generate_recommendation(self, report: JEPATrapReport) -> str:
        if report.risk_level == "HARVESTING":
            return (f"🔴 重度陷阱(trap={report.trap_score:.2f}) — "
                    f"预测-定价矛盾显著(angle={report.prediction_market_angle:.1f}°), "
                    f"强烈建议规避或反向操作")
        elif report.risk_level == "DANGER":
            return (f"🟠 陷阱危险(trap={report.trap_score:.2f}) — "
                    f"存在嵌入空间矛盾, 建议谨慎投注")
        elif report.risk_level == "SUSPICIOUS":
            return (f"🟡 轻微异常(trap={report.trap_score:.2f}) — "
                    f"有若干信号, 但不足以判定为陷阱")
        else:
            return (f"🟢 安全(trap={report.trap_score:.2f}) — "
                    f"嵌入空间无明显异常")

# ═══════════════════════════════════════════════════════════════
# 兼容层: 将JEPA TrapDetector包装为BookmakerTrapDetector接口
# ═══════════════════════════════════════════════════════════════

class JEPATrapDetectorCompat:
    """
    兼容层 — 使JEPA TrapDetector的输出与现有BookmakerTrapDetector兼容
    
    用法: 直接替换 BookmakerTrapDetector 实例
    """
    
    def __init__(self):
        self._jepa = JEPATrapDetector()
    
    def detect(self, match_data: Dict[str, Any]):
        """
        兼容 detect() 接口，返回类TrapReport结构
        
        输入: 与原BookmakerTrapDetector.detect()相同的match_data
        输出: 包含传统字段的兼容报告
        """
        odds_current = {
            'odds_h': match_data.get('odds_h', 2.0),
            'odds_d': match_data.get('odds_d', 3.5),
            'odds_a': match_data.get('odds_a', 4.0),
            'asian_handicap': match_data.get('asian_handicap'),
            'water_level': match_data.get('water_level', 0.92),
            'overround': match_data.get('overround', 0.06),
            'volume_ratio_fav': match_data.get('volume_ratio_fav', 0.5),
            'volatility': match_data.get('volatility', 0.01),
            'draw_protection': match_data.get('draw_protection', 0.0),
        }
        
        jepa_report = self._jepa.detect(
            odds_current=odds_current,
            home=match_data.get('home', ''),
            away=match_data.get('away', ''),
        )
        
        # 转换为传统格式
        from bookmaker_sim.bookmaker_trap_detector import TrapReport, TrapSignal, TrapType
        
        trad_report = TrapReport(
            home=jepa_report.home,
            away=jepa_report.away,
            aggregate_score=jepa_report.trap_score * 5.0,  # 映射到[0,5]分制
            raw_score=jepa_report.trap_score * 5.0,
            recommendation=jepa_report.recommendation,
            trap_features={
                'jepa_trap_score': jepa_report.trap_score,
                'alignment_loss': jepa_report.alignment_loss,
                'magnitude_loss': jepa_report.magnitude_loss,
                'prediction_market_angle': jepa_report.prediction_market_angle,
                'risk_level': jepa_report.risk_level,
                **{f'jepa_{k}': v for k, v in jepa_report.traditional_mapping.items()},
            },
        )
        
        return trad_report

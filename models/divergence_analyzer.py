"""
JEPA 多机构分歧分析器 — 多个世界模型的预测差异
================================================
LeCun JEPA 核心组件之三: 多模型分歧

不同博彩机构 (Interwetten, Bet365, Pinnacle, ...) 各自维护自己的世界模型:
  M_i = (Encoder_i, Predictor_i)

对于同一场比赛:
  - 每个机构的嵌入: z_i = Encoder_i(o_i)
  - 每个机构的预测: ẑ_i = Predictor_i(z_i)
  - 分歧度 = Var(ẑ_i) → 不同世界模型对同一状态next_step的预测方差

关键判别:
  - 分歧缩小 (convergence) → 市场达成共识 → 赛果大概率落地共识方向
  - 分歧扩大 (divergence) → 信息不对称 → 某机构可能掌握了额外信息
  - 单机构偏离 → 该机构有独家信息 → 追踪该机构的预测方向

博弈论解读:
  不是所有分歧都是"信号" — 有些是"噪声":
    - 噪声型分歧: 不同抽水策略、不同客户群导致的定价惯性差异
    - 信号型分歧: 某机构对赛果有不同判断，愿意承担更高风险
  
  量化方法:
    分解分歧的协方差矩阵:
      Σ_divergence = Σ_info + Σ_noise
    其中 Σ_info 在各维度上高度结构化 (集中在实力差/不确定性维度)
    Σ_noise 近似各向同性 (均匀分布在所有维度)

作者: 杜博弈 / FootballAI v4.1 JEPA Redesign
日期: 2026-06-20
"""

import numpy as np
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass, field
from enum import Enum
import logging

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 分歧分析数据结构
# ═══════════════════════════════════════════════════════════════

class DivergenceType(Enum):
    """分歧类型"""
    NOISE = "noise"                    # 噪声 (抽水策略差异)
    INFORMATION = "information"        # 信息 (某机构有额外信息)
    STRUCTURAL = "structural"          # 结构 (不同客户群/风控策略)
    CONVERGENCE = "convergence"        # 收敛 (市场达成共识)


@dataclass
class InstitutionEmbedding:
    """单一机构的嵌入 + 预测"""
    name: str                          # 机构名 (e.g. "Interwetten", "Bet365")
    odds: Dict[str, float]             # 该机构的原始赔率
    z: np.ndarray                      # 当前嵌入
    z_predicted: Optional[np.ndarray]  # 预测嵌入 (如有Predictor)
    overround: float                   # 该机构的抽水率
    timestamp: float = 0.0             # 时间戳


@dataclass
class DivergenceReport:
    """多机构分歧分析报告"""
    match_id: Optional[int] = None
    home: str = ""
    away: str = ""
    
    # 参与机构数
    n_institutions: int = 0
    institution_names: List[str] = field(default_factory=list)
    
    # 嵌入空间分歧度量
    embedding_variance: float = 0.0       # 嵌入方差 (总分歧)
    info_dim_variance: float = 0.0        # 信息维度上的方差
    noise_dim_variance: float = 0.0       # 噪声维度上的方差
    info_noise_ratio: float = 0.0         # 信息/噪声比 (>1 = 信号分歧; <0.5 = 噪声分歧)
    
    # 预测分歧
    prediction_variance: float = 0.0      # 各机构Predictor的预测方差
    prediction_direction_consensus: float = 0.0  # 预测方向一致性
    
    # 单机构偏离检测
    outliers: List[Dict] = field(default_factory=list)  # 偏离机构
    
    # 时序变化
    divergence_trend: str = "stable"      # stable/diverging/converging
    convergence_rate: float = 0.0         # 收敛速率
    
    # 分类与解释
    divergence_type: str = "noise"
    signal_grade: str = "C"               # S/A/B/C/F
    interpretation: str = ""
    
    def to_dict(self) -> Dict:
        return {
            "match": f"{self.home} vs {self.away}",
            "n_institutions": self.n_institutions,
            "institutions": self.institution_names,
            "embedding_variance": round(self.embedding_variance, 5),
            "info_dim_variance": round(self.info_dim_variance, 5),
            "noise_dim_variance": round(self.noise_dim_variance, 5),
            "info_noise_ratio": round(self.info_noise_ratio, 3),
            "prediction_variance": round(self.prediction_variance, 5),
            "prediction_consensus": round(self.prediction_direction_consensus, 3),
            "outliers": self.outliers,
            "divergence_trend": self.divergence_trend,
            "divergence_type": self.divergence_type,
            "signal_grade": self.signal_grade,
            "interpretation": self.interpretation,
        }


# ═══════════════════════════════════════════════════════════════
# 多机构分歧分析器
# ═══════════════════════════════════════════════════════════════

class MultiInstitutionDivergenceAnalyzer:
    """
    多机构世界模型分歧分析器
    
    不同博彩机构 = 不同的世界模型:
      M_i = (Encoder_i, Predictor_i, RiskModel_i)
    
    核心假设:
      - 噪声: 抽水策略差异在各方向均匀分布 → 高熵嵌入分歧
      - 信号: 信息优势集中在特定嵌入维度 → 低熵嵌入分歧
    
    输入:
      - 多个机构的1X2赔率
      - (可选) 各机构的历史Predictor
    
    输出:
      - 分歧度量 (总方差、信息维方差、噪声维方差)
      - 分歧类型分类 (噪声/信息/结构)
      - 信号等级 (S/A/B/C/F)
      - 单机构偏离检测
    """
    
    # 嵌入空间中"信息维度"的索引 (实力差距、不确定性、庄家态度)
    # 这些维度上的分歧更可能是"信号"而非"噪声"
    INFO_DIMS = {0, 1, 2, 3, 4, 8, 9, 11}   # 8个信息维
    # "噪声维度"的索引 (资金流、短期波动)
    NOISE_DIMS = {5, 6, 7, 10, 12, 13, 14, 15}  # 8个噪声维
    
    def __init__(self, encoder=None):
        from .odds_encoder import OddsEncoder
        self.encoder = encoder or OddsEncoder()
    
    def analyze(self, 
                institution_odds: Dict[str, Dict[str, float]],
                home: str = "", away: str = "",
                historical_predictors: Optional[Dict[str, 'MarketStatePredictor']] = None,
                ) -> DivergenceReport:
        """
        分析多机构分歧
        
        Args:
            institution_odds: {机构名: {odds_h, odds_d, odds_a, ...}}
            historical_predictors: (可选) 各机构的Predictor
        
        Returns:
            DivergenceReport
        """
        names = list(institution_odds.keys())
        n = len(names)
        
        if n < 2:
            return DivergenceReport(
                home=home, away=away,
                n_institutions=n, institution_names=names,
                interpretation="机构数不足 (<2), 无法分析分歧"
            )
        
        # ── 1. 编码所有机构的赔率到嵌入空间 ──
        embeddings: Dict[str, np.ndarray] = {}
        overrounds: Dict[str, float] = {}
        
        for name, odds in institution_odds.items():
            odds_h = odds.get('odds_h', 2.0)
            odds_d = odds.get('odds_d', 3.5)
            odds_a = odds.get('odds_a', 4.0)
            
            # 计算该机构的抽水率
            raw_sum = 1/odds_h + 1/odds_d + 1/odds_a
            overrounds[name] = raw_sum - 1.0
            
            # 编码
            from .odds_encoder import compute_odds_embedding
            z = compute_odds_embedding(
                odds_h, odds_d, odds_a,
                asian_handicap=odds.get('asian_handicap'),
                water_level=odds.get('water_level', 0.92),
                overround=overrounds[name],
            )
            embeddings[name] = z
        
        # ── 2. 计算嵌入空间分歧 ──
        z_matrix = np.stack(list(embeddings.values()))  # (n, 16)
        z_mean = z_matrix.mean(axis=0)
        z_centered = z_matrix - z_mean
        
        # 总方差
        total_var = float((z_centered ** 2).sum() / n)
        
        # 信息维度方差 vs 噪声维度方差
        info_vars = []
        noise_vars = []
        for d in range(16):
            var_d = float(z_centered[:, d].var())
            if d in self.INFO_DIMS:
                info_vars.append(var_d)
            else:
                noise_vars.append(var_d)
        
        info_var = float(np.mean(info_vars))
        noise_var = float(np.mean(noise_vars))
        info_noise_ratio = info_var / max(noise_var, 1e-8)
        
        # ── 3. 检测单机构偏离 ──
        outliers = self._detect_outliers(embeddings, z_mean, names)
        
        # ── 4. 预测分歧 (如有Predictor) ──
        pred_variance = 0.0
        pred_consensus = 1.0
        if historical_predictors:
            pred_variance, pred_consensus = self._compute_prediction_divergence(
                embeddings, historical_predictors, names
            )
        
        # ── 5. 分类分歧类型 ──
        div_type, signal_grade, interpretation = self._classify_divergence(
            info_noise_ratio, total_var, outliers, n,
            overrounds, pred_consensus
        )
        
        return DivergenceReport(
            home=home, away=away,
            n_institutions=n,
            institution_names=names,
            embedding_variance=total_var,
            info_dim_variance=info_var,
            noise_dim_variance=noise_var,
            info_noise_ratio=info_noise_ratio,
            prediction_variance=float(pred_variance),
            prediction_direction_consensus=float(pred_consensus),
            outliers=outliers,
            divergence_type=div_type,
            signal_grade=signal_grade,
            interpretation=interpretation,
        )
    
    def _detect_outliers(self, 
                         embeddings: Dict[str, np.ndarray],
                         z_mean: np.ndarray,
                         names: List[str]) -> List[Dict]:
        """
        检测嵌入空间中的离群机构
        
        离群 = 该机构的嵌入与均值嵌入的距离 > 2σ
        离群机构可能掌握了其他机构没有的信息。
        
        关键信号: 当离群机构的Predictor也预测了与众不同的方向时
        → 该机构有独家信息 → 追踪该机构
        """
        # 计算每个机构到均值的距离
        distances = {}
        for name in names:
            dist = float(np.linalg.norm(embeddings[name] - z_mean))
            distances[name] = dist
        
        if len(distances) < 3:
            return []
        
        # Z-score
        dists = list(distances.values())
        mean_dist = np.mean(dists)
        std_dist = np.std(dists) + 1e-8
        
        outliers = []
        for name in names:
            z_score = (distances[name] - mean_dist) / std_dist
            if z_score > 1.5:  # 超过1.5σ
                # 分析该机构在哪些维度上偏离
                diff = embeddings[name] - z_mean
                top_dims = np.argsort(np.abs(diff))[::-1][:3]
                
                outlier_info = {
                    'institution': name,
                    'z_score': round(float(z_score), 2),
                    'distance': round(distances[name], 4),
                    'top_divergent_dims': [
                        {'dim': int(d), 'deviation': round(float(diff[d]), 4)}
                        for d in top_dims
                    ],
                    'is_info_dim_outlier': any(
                        int(d) in self.INFO_DIMS for d in top_dims
                    ),
                    'interpretation': (
                        '该机构在信息维度上偏离 → 可能掌握独家信息'
                        if any(int(d) in self.INFO_DIMS for d in top_dims)
                        else '该机构在噪声维度上偏离 → 可能是风控/抽水策略差异'
                    ),
                }
                outliers.append(outlier_info)
        
        return sorted(outliers, key=lambda x: x['z_score'], reverse=True)
    
    def _compute_prediction_divergence(self,
                                       embeddings: Dict[str, np.ndarray],
                                       predictors: Dict[str, 'MarketStatePredictor'],
                                       names: List[str]) -> Tuple[float, float]:
        """
        计算各机构Predictor的预测分歧
        
        预测方差大 = 各世界模型对下一状态有不同预期
        预测方向一致 = 各世界模型认同演化方向 (即使速度不同)
        """
        predictions = {}
        for name in names:
            if name in predictors and name in embeddings:
                pred = predictors[name].predict(embeddings[name])
                predictions[name] = pred
        
        if len(predictions) < 2:
            return 0.0, 1.0
        
        pred_matrix = np.stack(list(predictions.values()))
        pred_var = float(pred_matrix.var())
        
        # 方向一致性: 各预测向量之间的平均余弦相似度
        pred_deltas = pred_matrix - np.stack([
            embeddings[n] for n in predictions.keys()
        ])
        
        cos_sims = []
        pnames = list(predictions.keys())
        for i in range(len(pnames)):
            for j in range(i+1, len(pnames)):
                v1 = pred_deltas[i]
                v2 = pred_deltas[j]
                n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
                if n1 > 1e-8 and n2 > 1e-8:
                    cos_sims.append(np.dot(v1, v2) / (n1 * n2))
        
        consensus = float(np.mean(cos_sims) + 1) / 2 if cos_sims else 1.0
        
        return pred_var, consensus
    
    def _classify_divergence(self,
                             info_noise_ratio: float,
                             total_var: float,
                             outliers: List[Dict],
                             n: int,
                             overrounds: Dict[str, float],
                             pred_consensus: float) -> Tuple[str, str, str]:
        """
        分类分歧类型 → 信号等级
        
        分类逻辑:
          - info_noise_ratio > 2.0 → 信息型分歧 (信号)
          - info_noise_ratio < 0.5 → 噪声型分歧 (抽水差异)
          - 有单机构在信息维偏离 → 信息不对称
          - 多个机构在噪声维偏离 → 风控策略差异
        """
        # 检查单机构在信息维的偏离
        info_outliers = [o for o in outliers if o['is_info_dim_outlier']]
        
        # 检查抽水率差异 (结构差异)
        if len(overrounds) >= 2:
            ovr_values = list(overrounds.values())
            ovr_range = max(ovr_values) - min(ovr_values)
        else:
            ovr_range = 0.0
        
        # ── 分类 ──
        if info_noise_ratio > 2.0 and info_outliers:
            # 信息维分歧大 + 有机构在信息维偏离 → 信息信号
            div_type = DivergenceType.INFORMATION.value
            signal_grade = 'S' if len(info_outliers) == 1 else 'A'
            interpretation = (
                f"发现{len(info_outliers)}个机构在信息维度上显著偏离, "
                f"信息/噪声比={info_noise_ratio:.1f}, "
                f"追踪偏离机构: {[o['institution'] for o in info_outliers]}"
            )
        
        elif info_noise_ratio > 2.0:
            # 信息维分歧大但无明确单机构偏离 → 市场整体分歧
            div_type = DivergenceType.INFORMATION.value
            signal_grade = 'B'
            interpretation = (
                f"多机构在信息维度上存在分歧(比例={info_noise_ratio:.1f}), "
                f"但无明确单机构引领, 市场未达成共识"
            )
        
        elif info_noise_ratio < 0.5:
            # 噪声维分歧为主 → 抽水策略差异
            div_type = DivergenceType.NOISE.value
            signal_grade = 'C'
            interpretation = (
                f"分歧主要在噪声维度(比例={info_noise_ratio:.2f}), "
                f"各机构抽水策略差异(范围={ovr_range*100:.1f}%), "
                f"不构成有效预测信号"
            )
        
        elif ovr_range > 0.03:
            # 抽水差异大 → 结构性分歧
            div_type = DivergenceType.STRUCTURAL.value
            signal_grade = 'C'
            interpretation = (
                f"结构性分歧: 抽水率范围={ovr_range*100:.1f}%, "
                f"不同机构的风险偏好/客户群不同"
            )
        
        elif pred_consensus > 0.8 and total_var < 0.02:
            # 嵌入和预测都一致 → 收敛
            div_type = DivergenceType.CONVERGENCE.value
            signal_grade = 'A'
            interpretation = (
                f"多机构高度一致(pred_consensus={pred_consensus:.2f}), "
                f"市场达成共识, 赛果大概率落地共识方向"
            )
        
        else:
            div_type = DivergenceType.NOISE.value
            signal_grade = 'C'
            interpretation = f"分歧不显著, 视为噪声 (IR={info_noise_ratio:.2f})"
        
        return div_type, signal_grade, interpretation


# ═══════════════════════════════════════════════════════════════
# 跨机构共识度评分
# ═══════════════════════════════════════════════════════════════

def compute_consensus_score(
    institution_odds: Dict[str, Dict[str, float]],
    encoder=None
) -> Dict[str, float]:
    """
    快速计算多机构共识度
    
    Returns:
        {
            'consensus_score': 0-1 (1=完全一致),
            'divergence_level': 'low'/'medium'/'high',
            'direction': 'home'/'draw'/'away' (共识方向),
            'n_outliers': 离群机构数,
        }
    """
    analyzer = MultiInstitutionDivergenceAnalyzer(encoder)
    report = analyzer.analyze(institution_odds)
    
    # 将嵌入方差映射到共识度
    max_var = 0.1  # 归一化参考值
    consensus = max(0.0, 1.0 - report.embedding_variance / max_var)
    
    if consensus > 0.8:
        level = 'high'
    elif consensus > 0.5:
        level = 'medium'
    else:
        level = 'low'
    
    return {
        'consensus_score': round(consensus, 3),
        'divergence_level': level,
        'n_outliers': len(report.outliers),
        'outlier_names': [o['institution'] for o in report.outliers],
        'divergence_type': report.divergence_type,
        'signal_grade': report.signal_grade,
    }

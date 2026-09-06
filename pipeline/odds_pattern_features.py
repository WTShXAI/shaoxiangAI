"""
哨响AI · 赔率尾数模式特征提取器 v1.0
基于 涛哥 4大操盘尾数理论 + 大小球交叉验证 + 平手盘信号
从312K历史验证中提炼的7个高信噪比特征

验证基础: historical_matches (312K, 2012-2025) + feature_library (2.5K OU)
数据粒度限制: 历史赔率为离散整数分位 (如1.43/1.44/1.45但无1.435)
策略: 用区间聚类代替精确尾数, 只保留统计显著的信号
"""

from typing import Dict, Optional, Tuple, List
import math

# ============================================================
# F1: 1.44 死亡尾数信号 (已验证: +0.9pp爆冷 vs 基线)
#   精确1.44: 爆冷33.9% | 1.42-1.46区间: 33.7% | 基线1.30-1.60: 33.0%
# ============================================================
def feature_death_tail_144(home_odds: float) -> float:
    """
    1.44死亡尾数信号
    返回: 0.0-1.0, 数值越高越危险 (越可能爆冷)
    """
    if home_odds is None or home_odds <= 0:
        return 0.0
    
    # Core zone: 1.42-1.46 (nearest to 1.44)
    if 1.42 <= home_odds <= 1.46:
        # Within the death zone, grade proximity to 1.44
        distance = abs(home_odds - 1.44)
        return max(0.0, 1.0 - distance * 10)  # decay: 1.44=1.0, 1.43/1.45=0.9, 1.42/1.46=0.8
    elif 1.40 <= home_odds < 1.42 or 1.46 < home_odds <= 1.50:
        return 0.3  # extended risk zone
    return 0.0


# ============================================================
# F2: 临界整数边界信号 (已验证: 1.90-1.99打出率48.6% > 2.00-2.10的46.1%)
#   历史数据显示REVERSE模式: 临界位打出率更高, 不是诱盘
#   但在现代精细赔率中, 这个模式可能相反
#   当前: 标记边界存在性, 不完全信任方向
# ============================================================
def feature_boundary_tail(home_odds: float) -> float:
    """
    临界整数边界信号
    检测赔率是否接近整数边界 (n.90-n.99区间)
    返回: 0.0-1.0, 表示"临界感"强度
    """
    if home_odds is None or home_odds <= 0:
        return 0.0
    
    # Extract the fractional part
    fractional = home_odds - math.floor(home_odds)
    
    # 0.90-0.99 = "almost at the next integer"
    if fractional >= 0.90:
        return fractional  # 0.90→0.90, 0.99→0.99
    # 0.00-0.05 = "just crossed integer"
    elif fractional <= 0.05 and home_odds >= 1.5:
        return 0.3
    return 0.0


# ============================================================
# F3: 平赔尾数带8信号 (验证: 历史数据无此粒度, 但现代实时赔率可检测)
#   检测 draw_odds 小数部分是否以 .08/.18/.28/.38/.48 结尾
# ============================================================
def feature_draw_tail_8(draw_odds: float) -> float:
    """
    平赔尾数带8信号
    现代实时赔率可检测: 2.88, 3.18, 3.28 etc.
    返回: 1.0=检测到尾数8, 0.0=未检测到
    """
    if draw_odds is None or draw_odds <= 0:
        return 0.0
    
    # Get last 2 decimal digits
    cents = round(draw_odds * 100) % 100
    last_digit = cents % 10
    
    if last_digit == 8:
        # Grade by context: 2.5-4.0 range is most meaningful
        if 2.5 <= draw_odds <= 4.0:
            return 1.0
        else:
            return 0.5  # outside main range, weaker signal
    return 0.0


# ============================================================
# F4: 高赔率尾数带4信号 (验证: 历史数据无此粒度)
#   检测 underdog odds 小数部分是否以 .04/.14/.24/.34/.44... 结尾
# ============================================================
def feature_high_odds_tail_4(odds: float, threshold: float = 4.0) -> float:
    """
    高赔率尾数带4信号
    现代实时赔率可检测: 4.44, 5.44, 3.54 etc.
    返回: 1.0=检测到尾数4, 0.0=未检测到
    """
    if odds is None or odds <= threshold:
        return 0.0
    
    cents = round(odds * 100) % 100
    last_digit = cents % 10
    
    if last_digit == 4:
        # Stronger if it's a repeating pattern (4.44, 5.44)
        tens_digit = (cents // 10) % 10
        if tens_digit == 4:  # 4.44 pattern
            return 1.0
        return 0.7
    return 0.0


# ============================================================
# F5: 1X2+OU 交叉验证信号
#   5a: 强队低赔(≤1.55) + OU=2.5球 → 小球倾向 (验证: 小球51.3%, 均值50%)
#   5b: 平赔3.0-3.5 + OU=2.75 → 平局+大球 (样本不足, 标记)
# ============================================================
def feature_1x2_ou_cross(
    home_odds: float, draw_odds: float, 
    ou_line: float, ou_over_prob: float
) -> Dict[str, float]:
    """
    1X2+OU交叉验证信号包
    返回多个子特征
    """
    features = {
        'f5a_small_win_signal': 0.0,   # 强队小胜信号
        'f5b_draw_high_goal_signal': 0.0,  # 平局大球信号
        'f5_ou_vs_draw_divergence': 0.0,  # OU与平赔背离度
    }
    
    if home_odds is None or draw_odds is None:
        return features
    
    # 5a: 强队低赔 + 2.5球 → 小球信号
    if home_odds <= 1.55 and ou_line is not None and abs(ou_line - 2.5) < 0.1:
        # Higher over prob → market expects goals → contrarian small ball signal
        if ou_over_prob is not None and ou_over_prob > 0.50:
            features['f5a_small_win_signal'] = min(1.0, (ou_over_prob - 0.50) * 2)
    
    # 5b: 平赔3.0-3.5 + OU=2.75 → 平局大球信号
    if 3.0 <= draw_odds <= 3.5 and ou_line is not None and abs(ou_line - 2.75) < 0.1:
        if ou_over_prob is not None:
            # Stronger signal if ou_over is high AND draw odds are mid-range
            features['f5b_draw_high_goal_signal'] = ou_over_prob
    
    # OU vs draw divergence
    if ou_over_prob is not None and 2.5 <= draw_odds <= 4.0:
        # Normalize: higher ou_over with mid draw = potential high-scoring draw
        draw_norm = 1.0 - abs(draw_odds - 3.25) / 1.5
        features['f5_ou_vs_draw_divergence'] = ou_over_prob * draw_norm
    
    return features


# ============================================================
# F6: 赔率漂移信号 (升赔=危险, 降赔=可信)
#   验证: 升赔≥0.08 → 主胜34.9% | 降赔≥0.08 → 主胜46.6%
# ============================================================
def feature_odds_drift(
    open_home: float, close_home: float,
    open_away: float = None, close_away: float = None
) -> Dict[str, float]:
    """
    赔率漂移信号
    """
    features = {
        'f6_home_drift': 0.0,       # 标准化主赔漂移 (-1 to +1)
        'f6_drift_magnitude': 0.0,  # 漂移幅度
        'f6_drift_danger': 0.0,     # 升赔危险度 (0-1)
        'f6_drift_trust': 0.0,      # 降赔可信度 (0-1)
    }
    
    if open_home is None or close_home is None or open_home <= 0 or close_home <= 0:
        return features
    
    drift = close_home - open_home
    features['f6_home_drift'] = drift
    features['f6_drift_magnitude'] = abs(drift)
    
    if drift >= 0.05:  # odds rising = bookmaker cooling on home
        features['f6_drift_danger'] = min(1.0, drift / 0.20)  # max at 0.20 rise
    elif drift <= -0.05:  # odds dropping = genuine signal
        features['f6_drift_trust'] = min(1.0, abs(drift) / 0.20)
    
    return features


# ============================================================
# F7: 平手盘等价信号 (用1X2赔率差近似)
#   验证: 赔率差<0.3 → 平局29.4% | 赔率差<0.1 → 平局29.9%
#   均高于整体平局基线25.7%
# ============================================================
def feature_handicap_equivalent(home_odds: float, away_odds: float) -> Dict[str, float]:
    """
    平手盘等价信号 (无AH数据时的1X2近似)
    """
    features = {
        'f7_odds_closeness': 0.0,      # 赔率接近度 (0=远, 1=完全相同)
        'f7_balanced_draw_signal': 0.0, # 均衡盘平局信号
        'f7_clear_favorite': 0.0,       # 明确优势方信号
    }
    
    if home_odds is None or away_odds is None or home_odds <= 0 or away_odds <= 0:
        return features
    
    diff = abs(home_odds - away_odds)
    
    # Closeness: normalized to 0-1 (0 = very different, 1 = identical)
    max_odds = max(home_odds, away_odds)
    if max_odds > 0:
        features['f7_odds_closeness'] = max(0.0, 1.0 - diff / max_odds)
    
    # Balanced draw signal: both in 2.0-4.5 range AND diff < 0.5
    if 2.0 <= home_odds <= 4.5 and 2.0 <= away_odds <= 4.5 and diff < 0.5:
        features['f7_balanced_draw_signal'] = max(0.0, 1.0 - diff / 0.5)
    
    # Clear favorite: one side <= 1.55
    min_odds = min(home_odds, away_odds)
    if min_odds <= 1.55:
        features['f7_clear_favorite'] = max(0.0, 1.0 - (min_odds - 1.10) / 0.45)
    
    return features


# ============================================================
# 聚合: 从原始赔率提取所有模式特征
# ============================================================
def extract_all(
    home_odds: float,
    draw_odds: float, 
    away_odds: float,
    ou_line: Optional[float] = None,
    ou_over_prob: Optional[float] = None,
    open_home: Optional[float] = None,
    open_draw: Optional[float] = None,
    open_away: Optional[float] = None,
) -> Dict[str, float]:
    """
    一站式特征提取
    
    Args:
        home_odds: 主胜赔率 (当前/临场)
        draw_odds: 平局赔率
        away_odds: 客胜赔率
        ou_line: 大小球盘口线 (如 2.5, 2.75)
        ou_over_prob: 大球去水概率
        open_home/draw/away: 开盘赔率 (可选, 用于漂移计算)
    
    Returns:
        Dict of feature_name → value
    """
    feats = {}
    
    # F1: 1.44 death tail
    feats['f1_death_tail_144'] = feature_death_tail_144(home_odds)
    feats['f1_death_tail_144_away'] = feature_death_tail_144(away_odds)
    
    # F2: Boundary tail
    feats['f2_boundary_tail_home'] = feature_boundary_tail(home_odds)
    feats['f2_boundary_tail_away'] = feature_boundary_tail(away_odds)
    
    # F3: Draw tail 8
    feats['f3_draw_tail_8'] = feature_draw_tail_8(draw_odds)
    
    # F4: High odds tail 4
    feats['f4_high_tail_4_home'] = feature_high_odds_tail_4(home_odds)
    feats['f4_high_tail_4_away'] = feature_high_odds_tail_4(away_odds)
    
    # F5: 1X2+OU cross
    ou_feats = feature_1x2_ou_cross(home_odds, draw_odds, ou_line, ou_over_prob)
    feats.update(ou_feats)
    
    # F6: Drift
    if open_home is not None:
        drift_feats = feature_odds_drift(open_home, home_odds, open_away, away_odds)
        feats.update(drift_feats)
    
    # F7: Handicap equivalent
    hcp_feats = feature_handicap_equivalent(home_odds, away_odds)
    feats.update(hcp_feats)
    
    return feats


# ============================================================
# 世界杯模型特别加权
# ============================================================
WC_LEAGUE_WEIGHTS = {
    # World Cup and continental tournaments get higher weight for tail patterns
    '世界杯': 1.5,
    '欧洲杯': 1.3,
    '美洲杯': 1.2,
    '亚洲杯': 1.1,
    '非洲杯': 1.1,
    '欧冠': 1.15,
    '欧联杯': 1.05,
    '解放者杯': 1.05,
}

def apply_wc_weights(features: Dict[str, float], league: str) -> Dict[str, float]:
    """对世界杯/大赛联赛加大尾数模式权重"""
    weight = WC_LEAGUE_WEIGHTS.get(league, 1.0)
    if weight == 1.0:
        return features
    
    weighted = dict(features)
    for key in ['f1_death_tail_144', 'f2_boundary_tail_home', 'f3_draw_tail_8', 'f4_high_tail_4_home']:
        if key in weighted:
            weighted[key] = min(1.0, weighted[key] * weight)
    return weighted


# ============================================================
# 诊断: 解释特征含义
# ============================================================
def explain(features: Dict[str, float]) -> List[str]:
    """将特征值转成可读解释"""
    explanations = []
    
    if features.get('f1_death_tail_144', 0) > 0.3:
        explanations.append(f"⚠ 1.44死亡尾数风险 ({features['f1_death_tail_144']:.2f})")
    if features.get('f2_boundary_tail_home', 0) > 0.5:
        explanations.append(f"🔍 临界整数尾数 ({features['f2_boundary_tail_home']:.2f})")
    if features.get('f3_draw_tail_8', 0) > 0:
        explanations.append(f"📌 平赔尾数带8 - 控赔付信号")
    if features.get('f4_high_tail_4_home', 0) > 0:
        explanations.append(f"💎 高赔尾数带4 - 冷门精准定价")
    if features.get('f5a_small_win_signal', 0) > 0.3:
        explanations.append(f"🔒 强队低赔+高OU→小球倾向")
    if features.get('f5b_draw_high_goal_signal', 0) > 0.3:
        explanations.append(f"⚽ 平赔中位+OU2.75→进球大战")
    if features.get('f6_drift_danger', 0) > 0.3:
        explanations.append(f"🚨 赔率上升→机构看衰 ({features['f6_drift_danger']:.2f})")
    if features.get('f6_drift_trust', 0) > 0.3:
        explanations.append(f"✅ 赔率下降→机构看好 ({features['f6_drift_trust']:.2f})")
    if features.get('f7_balanced_draw_signal', 0) > 0.5:
        explanations.append(f"🤝 均衡盘平局信号 ({features['f7_balanced_draw_signal']:.2f})")
    
    return explanations if explanations else ['无明显模式信号']

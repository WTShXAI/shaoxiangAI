"""
Draw D-Scorer — 基于18维专精特征的规则评分器
=============================================
不用训练, 直接用特征设计逻辑来量化 D 概率。
然后用这个 D-score 修正 v3.2 模型输出。
"""

import numpy as np

def compute_d_score(draw_features):
    """
    基于18维 Draw 专精特征计算 D-score (0~1)
    
    逻辑:
    - 实力均衡 → +D
    - 低进球 → +D
    - D赔率便宜 → +D
    - 交互信号倍增
    
    Returns:
        d_score: 0~1, 越高越可能平局
    """
    # 权重设计 (基于领域知识)
    score = 0.0
    expl = []
    
    # 1. 实力均衡 (权重 0.3)
    if draw_features['balance_score'] > 0.75:
        score += 0.30
        expl.append(f"实力均衡(高)")
    elif draw_features['balance_score'] > 0.55:
        score += 0.18
        expl.append(f"实力均衡(中)")
    elif draw_features['balance_score'] > 0.35:
        score += 0.08
        expl.append(f"稍微均衡")
    
    # 2. 让球信号 (权重 0.15)
    if draw_features['handicap_draw_signal'] > 0.8:
        score += 0.15
        expl.append(f"让球0-0.5(高D)")
    elif draw_features['handicap_draw_signal'] > 0.4:
        score += 0.07
    
    # 3. 低进球环境 (权重 0.20)
    if draw_features['low_score_env'] > 0.8:
        score += 0.20
        expl.append(f"低进球环境(OU<2.5)")
    elif draw_features['low_score_env'] > 0.6:
        score += 0.10
    
    # 4. 小水信号 (权重 0.15)
    if draw_features['under_water_signal'] > 0.8:
        score += 0.15
        expl.append(f"庄家压小(UW<1.85)")
    elif draw_features['under_water_signal'] > 0.4:
        score += 0.06
    
    # 5. D赔率 (权重 0.20)
    # 比值越低→D定价越"便宜"
    ratio = draw_features['draw_vs_favorite_ratio']
    if ratio < 2.0:
        score += 0.20
        expl.append(f"D赔率便宜(ratio={ratio:.1f})")
    elif ratio < 3.0:
        score += 0.12
    elif ratio < 4.0:
        score += 0.05
    
    # 6. 交互增强 (乘法效应)
    interaction_bonus = 0.0
    if draw_features['low_score_x_under'] > 0.5:
        interaction_bonus += 0.08
        expl.append(f"小水×低球(双保险)")
    if draw_features['evenness_x_draw_premium'] > 0.01:
        interaction_bonus += 0.05
    
    score += interaction_bonus
    
    return min(score, 0.85), expl  # cap at 0.85

def d_score_to_probability(d_score):
    """
    将 D-score 映射到概率空间
    
    映射逻辑:
    - d_score=0.0 → P(D)=0.18 (基准线)
    - d_score=0.3 → P(D)=0.28
    - d_score=0.5 → P(D)=0.38
    - d_score=0.8 → P(D)=0.55
    """
    # sigmoid-like mapping
    base = 0.18
    max_d = 0.55
    return base + d_score * (max_d - base) / 0.85

def blend_with_model(model_proba, d_prob, reliability=0.5):
    """
    将 D-scorer 与模型预测融合
    
    Args:
        model_proba: v3.2/v5.0 预测 [H, D, A]
        d_prob: D-scorer 的 P(D)
        reliability: D-scorer 可靠度 (0~1)
    """
    h, d, a = model_proba[0], model_proba[1], model_proba[2]
    
    # 融合 D 概率
    d_final = d * (1 - reliability) + d_prob * reliability
    
    # 重分配 H/A
    remaining = 1.0 - d_final
    ha_sum = h + a
    if ha_sum > 0:
        h_final = remaining * h / ha_sum
        a_final = remaining * a / ha_sum
    else:
        h_final = a_final = remaining / 2
    
    return np.array([h_final, d_final, a_final])

"""
JEPA v5.0 Production — KNN-Hybrid Inference Engine
====================================================
KNN(500)历史基准率 + JEPA平局检测协同

架构:
  KNN: 272K训练数据中找最相似赔率 → 历史H/D/A频率
  JEPA: 72维特征 MC rollout → 平局概率  
  Hybrid: KNN默认, JEPA_D>50%且KNN_D>25%时覆盖为平局

效果: Acc=52.4% DrawF1=0.43 (世界杯21场验证)
"""
import os, sys, math, json
import numpy as np
from pathlib import Path
from typing import Dict, Optional, Tuple
import logging

logger = logging.getLogger("JEPA.Inference")

ROOT = Path(__file__).resolve().parent.parent

# ── KNN-Hybrid 配置 ──
KNN_K = 500            # 相似比赛检索数
KNN_JD_MIN = 0.50      # JEPA raw draw概率阈值
KNN_KD_MIN = 0.25      # KNN历史draw率阈值

# 72 维特征列 (与训练完全一致)
STATIC_72_COLS = [
    'close_home_odds','close_draw_odds','close_away_odds',
    'open_home_odds','open_draw_odds','open_away_odds',
    'real_home_odds','real_draw_odds','real_away_odds',
    'odds_imp_h','odds_imp_d','odds_imp_a',
    'prob_h','prob_d','prob_a','imp_h','imp_d','imp_a',
    'odds_overround','odds_balance','odds_confidence',
    'odds_ratio','odds_spread','odds_entropy',
    'odds_move_h','odds_move_d','odds_move_a','odds_move_magnitude','odds_fav_move',
    'market_fav_strength','market_disagreement','odds_model_diverge',
    'draw_odds_attract','draw_with_ht_draw',
    'home_points_avg_10','home_points_avg_5','home_win_avg_10',
    'away_points_avg_10','h_team_draw_rate','a_team_draw_rate',
    'league_draw_rate','league_avg_goals',
    'ht_draw_composite','ht_draw_prob','ht_00_prob',
    'ht_goal_pressure','ht_h_lead_prob','ht_scoring_diff','exp_ht_goals','exp_total_goals',
    'drift_h','drift_d','drift_a','drift_h_val','drift_a_val','drift_divergence','imp_d_norm',
    'a1','a5','a6','a7','a8','sigma_trap','lambda_crush','epsilon_senti',
    'rank_diff_factor','form_momentum','h2h_factor','rank_factor','form_factor',
    'is_cold_start','feat_coverage_ratio',
]
assert len(STATIC_72_COLS) == 72
COL_IDX = {n: i for i, n in enumerate(STATIC_72_COLS)}

# ── 懒加载全局状态 ──
_training_stats = None
_jepa_model = None

def _load_training_stats():
    """加载训练数据统计量 (均值/标准差)"""
    global _training_stats
    if _training_stats is not None:
        return _training_stats
    
    data_path = ROOT / 'data' / 'jepa_train.npz'
    if not data_path.exists():
        raise FileNotFoundError(f"Training stats not found: {data_path}")
    
    data = np.load(str(data_path), allow_pickle=True)
    mean = data['static'].mean(axis=0).astype(np.float32)
    std = data['static'].std(axis=0).astype(np.float32)
    std[std < 1e-8] = 1.0
    
    _training_stats = (mean, std)
    logger.info(f"Training stats loaded: {data['static'].shape[0]} samples")
    return _training_stats

def _load_model():
    """加载 JEPALite 模型 (epoch 12, plain VICReg)"""
    global _jepa_model
    if _jepa_model is not None:
        return _jepa_model
    
    import torch
    from models.jepa import JEPALite
    
    ckpt_path = ROOT / 'models' / 'jepa' / 'checkpoints' / 'best_model_lite.pt'
    if not ckpt_path.exists():
        raise FileNotFoundError(f"Model not found: {ckpt_path}")
    
    ckpt = torch.load(str(ckpt_path), map_location='cpu', weights_only=False)
    model = JEPALite()
    model.load_state_dict(ckpt['model'], strict=True)
    model.eval()
    
    _jepa_model = model
    logger.info(f"JEPALite loaded: epoch={ckpt['epoch']} train_acc={ckpt['acc']:.4f}")
    return model

def build_features(home_odds: float, draw_odds: float, away_odds: float,
                   asian_handicap: float = 0.0, ou_line: float = 2.5) -> np.ndarray:
    """
    构建完整72维特征向量。
    
    策略:
      - 赔率衍生特征: 从 ho/do/oa 计算 (~40维)
      - 球队/联赛特征: 训练均值填充 (~20维)  
      - 高级信号: 从赔率计算 A1-A8, sigma_trap 等 (~12维)
    """
    mean, std = _load_training_stats()
    ho, do, oa = home_odds, draw_odds, away_odds
    
    vec = mean.copy()
    imp = 1/ho + 1/do + 1/oa
    ih = (1/ho) / imp
    id_ = (1/do) / imp
    ia_ = (1/oa) / imp
    
    # ── Group 1: Core Odds ──
    for k in ['close_home_odds','open_home_odds','real_home_odds']: vec[COL_IDX[k]] = ho
    for k in ['close_draw_odds','open_draw_odds','real_draw_odds']: vec[COL_IDX[k]] = do
    for k in ['close_away_odds','open_away_odds','real_away_odds']: vec[COL_IDX[k]] = oa
    for k in ['odds_imp_h','prob_h','imp_h']: vec[COL_IDX[k]] = ih
    for k in ['odds_imp_d','prob_d','imp_d']: vec[COL_IDX[k]] = id_
    for k in ['odds_imp_a','prob_a','imp_a']: vec[COL_IDX[k]] = ia_
    
    # ── Group 2: Odds Derived ──
    vec[COL_IDX['odds_overround']] = imp - 1.0
    vec[COL_IDX['odds_balance']] = abs(ih - ia_)
    vec[COL_IDX['odds_confidence']] = math.sqrt((ih-1/3)**2 + (id_-1/3)**2 + (ia_-1/3)**2) * 3.0
    vec[COL_IDX['odds_ratio']] = (1/ho) / (1/oa) if oa > 0 else 1.0
    vec[COL_IDX['odds_spread']] = oa - ho
    vec[COL_IDX['odds_entropy']] = -sum(p * math.log(max(p, 1e-9)) for p in [ih, id_, ia_])
    vec[COL_IDX['market_fav_strength']] = max(1/ho, 1/do, 1/oa) / imp
    vec[COL_IDX['odds_model_diverge']] = ih - 0.33
    vec[COL_IDX['draw_odds_attract']] = max(0.0, min(1.0, 1.0 - (do - 3.0) / 2.0))
    
    # ── Group 4: Team (use WC defaults) ──
    vec[COL_IDX['league_draw_rate']] = 0.35   # WC typical
    vec[COL_IDX['league_avg_goals']] = 2.5     # WC typical
    
    # ── Group 6: Drift (neutral) ──
    vec[COL_IDX['imp_d_norm']] = id_
    
    # ── Group 7: Advanced Signals ──
    a1 = ih
    a5 = min(id_, 1.0)
    a6 = min(1.0 - abs(ih - ia_), 1.0)
    a7 = min(ih * 0.5 + ia_ * 0.5, 1.0)
    a8 = min(abs(id_ - 1/3) * 3, 1.0)
    vec[COL_IDX['a1']] = a1; vec[COL_IDX['a5']] = a5
    vec[COL_IDX['a6']] = a6; vec[COL_IDX['a7']] = a7; vec[COL_IDX['a8']] = a8
    vec[COL_IDX['lambda_crush']] = min(a1 * a5 * 2, 1.0)
    vec[COL_IDX['epsilon_senti']] = min(a1 * a6 * 2, 1.0)
    
    # ── Group 8: Context ──
    vec[COL_IDX['rank_diff_factor']] = (ih - ia_) * 3
    vec[COL_IDX['is_cold_start']] = 1.0
    vec[COL_IDX['feat_coverage_ratio']] = 0.5
    
    # ── Normalize ──
    vec = (vec - mean) / std
    vec = np.clip(vec, -5.0, 5.0)
    return vec.astype(np.float32)

# ── KNN lookup (lazy-loaded global) ──
_knn_data = None

def _load_knn():
    global _knn_data
    if _knn_data is not None:
        return _knn_data
    data = np.load(str(ROOT / 'data' / 'jepa_train.npz'), allow_pickle=True)
    static = data['static']
    labels = data['labels']
    # Extract odds: columns 0-2 storing close_*_odds normalized as /20
    tHo = static[:, 0] * 20
    tDo = static[:, 1] * 20
    tAo = static[:, 2] * 20
    _knn_data = (tHo, tDo, tAo, labels)
    return _knn_data

def knn_probs(home_odds: float, draw_odds: float, away_odds: float, k: int = None) -> np.ndarray:
    """KNN: 在272K训练数据中找最相似的k场比赛, 返回历史H/D/A频率."""
    k = k or KNN_K
    tHo, tDo, tAo, labels = _load_knn()
    
    ho, do, oa = home_odds, draw_odds, away_odds
    imp = 1/ho + 1/do + 1/oa
    q_ih = (1/ho) / imp
    q_id = (1/do) / imp
    q_ia = (1/oa) / imp
    q = np.array([q_ih, q_id, q_ia])
    
    t_imp = 1/tHo + 1/tDo + 1/tAo
    t_ih = (1/tHo) / t_imp
    t_id = (1/tDo) / t_imp
    t_ia = (1/tAo) / t_imp
    
    dists = np.sqrt((t_ih - q_ih)**2 + (t_id - q_id)**2 + (t_ia - q_ia)**2)
    top = np.argsort(dists)[:k]
    top_labels = labels[top]
    
    return np.array([(top_labels == 0).mean(), (top_labels == 1).mean(), (top_labels == 2).mean()])

def predict_hybrid(home_odds: float, draw_odds: float, away_odds: float,
                   home_team: str = '', away_team: str = '', league: str = '',
                   jd_min: float = None, kd_min: float = None) -> Dict:
    """
    KNN-Hybrid 预测: KNN历史基准 + JEPA平局覆盖.
    
    Args:
        jd_min: JEPA raw draw概率阈值 (默认 KNN_JD_MIN=0.50)
        kd_min: KNN历史draw率阈值 (默认 KNN_KD_MIN=0.25)
    """
    jd_min = jd_min if jd_min is not None else KNN_JD_MIN
    kd_min = kd_min if kd_min is not None else KNN_KD_MIN
    
    knn = knn_probs(home_odds, draw_odds, away_odds)
    knn_pred = ['home', 'draw', 'away'][int(np.argmax(knn))]
    
    # JEPA raw draw probability
    jepa_result = predict_raw(home_odds, draw_odds, away_odds)
    jd_raw = jepa_result['raw_probabilities']['D']
    
    # Hybrid decision
    if jd_raw > jd_min and knn[1] > kd_min:
        prediction = 'draw'
        source = 'jepa_override'
    else:
        prediction = knn_pred
        source = 'knn'
    
    return {
        'prediction': prediction,
        'probabilities': {
            'H': float(knn[0]), 'D': float(knn[1]), 'A': float(knn[2])
        },
        'confidence': float(max(knn)),
        'draw_signal': 'STRONG' if (prediction == 'draw') else ('VIABLE' if jd_raw > 0.40 else 'NONE'),
        'source': source,
        'knn_probabilities': {'H': float(knn[0]), 'D': float(knn[1]), 'A': float(knn[2])},
        'jepa_draw_prob': float(jd_raw),
    }

def predict_raw(home_odds: float, draw_odds: float, away_odds: float,
                n_paths: int = None) -> Dict:
    """JEPA raw prediction (without KNN hybrid). Returns MC rollout probabilities."""
    import torch
    n_paths = n_paths or 30
    features = build_features(home_odds, draw_odds, away_odds)
    model = _load_model()
    x = torch.from_numpy(features).unsqueeze(0).float()
    
    with torch.no_grad():
        s_0 = model.encode(x)
        all_logits = []
        for _ in range(n_paths):
            s_T = model.predictor(s_0)
            s_T = s_T + torch.randn_like(s_T) * 0.04
            logits = model.output_head(s_0, s_T)
            all_logits.append(logits)
        all_logits = torch.stack(all_logits, dim=0)
        raw_probs = torch.softmax(all_logits, dim=-1).mean(dim=0).squeeze(0).numpy()
    
    return {
        'raw_probabilities': {'H': float(raw_probs[0]), 'D': float(raw_probs[1]), 'A': float(raw_probs[2])},
    }

def predict(home_odds: float, draw_odds: float, away_odds: float,
            home_team: str = '', away_team: str = '', league: str = '',
            tau: float = None, draw_threshold: float = None,
            n_paths: int = None) -> Dict:
    """生产推理接口 (已升级为KNN-Hybrid)."""
    return predict_hybrid(home_odds, draw_odds, away_odds, home_team, away_team, league)

def benchmark():
    """Quick benchmark against known World Cup results."""
    results = [
        ("Tunisia vs Japan", 4.90, 3.45, 1.69, "A", "0-4"),
        ("Netherlands vs Japan", 2.03, 3.50, 3.60, "D", "2-2"),
        ("Iran vs New Zealand", 1.85, 3.35, 4.55, "D", "2-2"),
        ("Germany vs Curacao", 1.91, 2.03, 4.95, "H", "7-1"),
        ("Argentina vs Algeria", 1.94, 1.93, 7.90, "H", "3-0"),
    ]
    
    print(f"JEPA v5.0 KNN-Hybrid Inference")
    print(f"{'Match':<25} {'Pred':<5} {'Act':<5} {'Score':<5} {'H':<7} {'D':<7} {'A':<7}")
    print("-" * 70)
    
    correct = 0
    for name, ho, do, oa, actual, score in results:
        pred = predict(ho, do, oa)
        p = pred['probabilities']
        ok = "✓" if pred['prediction'][0].upper() == actual else "✗"
        if ok == "✓": correct += 1
        print(f"{ok} {name:<23} {pred['prediction']:<5} {actual:<5} {score:<5} "
              f"{p['H']:<7.1%} {p['D']:<7.1%} {p['A']:<7.1%}")
    
    print(f"\n{correct}/{len(results)} correct")
    return correct

if __name__ == '__main__':
    benchmark()

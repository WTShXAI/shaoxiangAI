"""
Enriched feature builder — Real FIFA rankings + team data
============================================================
Replaces training means with real data for:
  - rank_diff_factor (FIFA rankings)
  - league_draw_rate (WC=35%)
  - home_points_avg_10, away_points_avg_10 (estimated from ranking)
  - h_team_draw_rate, a_team_draw_rate (estimated from ranking)
  - form_momentum, form_factor (estimated from ranking gap)
"""
import json, math, os
import numpy as np
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Load FIFA rankings
with open(ROOT / 'config' / 'fifa_rankings_2026.json', 'r', encoding='utf-8') as f:
    FIFA_RANKS = json.load(f)
# Remove meta
FIFA_RANKS = {k: v for k, v in FIFA_RANKS.items() if not k.startswith('_')}

# STATIC_72_COLS and COL_IDX
COLS = [
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
CIDX = {n: i for i, n in enumerate(COLS)}

# Training stats (global, loaded once)
_train_mean = None
_train_std = None

def _load_stats():
    global _train_mean, _train_std
    if _train_mean is not None:
        return
    data = np.load(str(ROOT / 'data' / 'jepa_train.npz'), allow_pickle=True)
    _train_mean = data['static'].mean(axis=0).astype(np.float32)
    _train_std = data['static'].std(axis=0).astype(np.float32)
    _train_std[_train_std < 1e-8] = 1.0

def get_team_rank(team_name: str) -> int:
    """Get FIFA ranking. Try English→Chinese mapping first, then direct."""
    # English → Chinese mapping (World Cup teams)
    EN_TO_ZH = {
        'Argentina': '阿根廷', 'Spain': '西班牙', 'France': '法国', 'England': '英格兰',
        'Portugal': '葡萄牙', 'Brazil': '巴西', 'Morocco': '摩洛哥', 'Netherlands': '荷兰',
        'Belgium': '比利时', 'Germany': '德国', 'Croatia': '克罗地亚', 'Colombia': '哥伦比亚',
        'Mexico': '墨西哥', 'Senegal': '塞内加尔', 'Uruguay': '乌拉圭', 'USA': '美国', 'United States': '美国',
        'Japan': '日本', 'Switzerland': '瑞士', 'Iran': '伊朗', 'Turkey': '土耳其',
        'Ecuador': '厄瓜多尔', 'Austria': '奥地利', 'South Korea': '韩国', 'Korea Republic': '韩国',
        'Australia': '澳大利亚', 'Algeria': '阿尔及利亚', 'Egypt': '埃及', 'Canada': '加拿大',
        'Norway': '挪威', 'Ivory Coast': '科特迪瓦', "Cote d'Ivoire": '科特迪瓦',
        'Panama': '巴拿马', 'Sweden': '瑞典', 'Czech Republic': '捷克', 'Czechia': '捷克',
        'Paraguay': '巴拉圭', 'Scotland': '苏格兰', 'Tunisia': '突尼斯',
        'DR Congo': '民主刚果', 'Congo DR': '民主刚果',
        'Uzbekistan': '乌兹别克斯坦', 'Qatar': '卡塔尔', 'Iraq': '伊拉克',
        'South Africa': '南非', 'Saudi Arabia': '沙特阿拉伯',
        'Jordan': '约旦', 'Bosnia': '波黑', 'Bosnia-Herzegovina': '波黑',
        'Cape Verde': '佛得角', 'Ghana': '加纳', 'Curacao': '库拉索',
        'Haiti': '海地', 'New Zealand': '新西兰',
    }
    
    zh_name = EN_TO_ZH.get(team_name, team_name)
    if zh_name in FIFA_RANKS:
        return FIFA_RANKS[zh_name]
    # Direct match
    if team_name in FIFA_RANKS:
        return FIFA_RANKS[team_name]
    return 100

def estimate_team_strength(rank: int) -> float:
    """Convert FIFA rank to normalized strength [0, 1].
    Rank 1 → 1.0, Rank 100 → 0.0"""
    return max(0.0, min(1.0, 1.0 - (rank - 1) / 100.0))

def estimate_draw_rate(rank: int) -> float:
    """Estimate team draw rate from ranking.
    Mid-ranked teams (30-60) draw more often than top/bottom teams."""
    if rank <= 10:
        return 0.18  # Top teams rarely draw
    elif rank <= 30:
        return 0.22
    elif rank <= 60:
        return 0.28  # Mid-table draw most
    else:
        return 0.22  # Weak teams also draw less (lose more)

def build_enriched_features(
    home_odds: float, draw_odds: float, away_odds: float,
    home_team: str = '', away_team: str = '',
) -> np.ndarray:
    """
    Build 72-dim features with REAL FIFA ranking data.
    Replaces training means with computed team strength features.
    """
    _load_stats()
    mean = _train_mean
    std = _train_std
    
    ho, do, oa = home_odds, draw_odds, away_odds
    imp = 1/ho + 1/do + 1/oa
    ih = (1/ho)/imp; id_ = (1/do)/imp; ia_ = (1/oa)/imp
    
    # Start with training means
    vec = mean.copy()
    
    # ── Core Odds (unchanged) ──
    for k in ['close_home_odds','open_home_odds','real_home_odds']: vec[CIDX[k]] = ho
    for k in ['close_draw_odds','open_draw_odds','real_draw_odds']: vec[CIDX[k]] = do
    for k in ['close_away_odds','open_away_odds','real_away_odds']: vec[CIDX[k]] = oa
    for k in ['odds_imp_h','prob_h','imp_h']: vec[CIDX[k]] = ih
    for k in ['odds_imp_d','prob_d','imp_d']: vec[CIDX[k]] = id_
    for k in ['odds_imp_a','prob_a','imp_a']: vec[CIDX[k]] = ia_
    
    # ── Odds Derived (unchanged) ──
    vec[CIDX['odds_overround']] = imp - 1.0
    vec[CIDX['odds_balance']] = abs(ih - ia_)
    vec[CIDX['odds_confidence']] = math.sqrt((ih-1/3)**2+(id_-1/3)**2+(ia_-1/3)**2)*3
    vec[CIDX['odds_ratio']] = (1/ho)/(1/oa) if oa>0 else 1
    vec[CIDX['odds_spread']] = oa - ho
    vec[CIDX['odds_entropy']] = -sum(p*math.log(max(p,1e-9)) for p in [ih,id_,ia_])
    vec[CIDX['market_fav_strength']] = max(1/ho,1/do,1/oa)/imp
    vec[CIDX['odds_model_diverge']] = ih - 0.33
    vec[CIDX['draw_odds_attract']] = max(0, min(1, 1-(do-3)/2))
    
    # ── ★ NEW: Real FIFA ranking features ──
    h_rank = get_team_rank(home_team) if home_team else 100
    a_rank = get_team_rank(away_team) if away_team else 100
    rank_diff = a_rank - h_rank  # positive = home stronger
    
    # rank_diff_factor: normalized to [-1, 1], -1=home much weaker, +1=home much stronger
    vec[CIDX['rank_diff_factor']] = np.clip(rank_diff / 50.0, -1.0, 1.0)
    
    # rank_factor: [0, 1], 0=home weakest, 1=home strongest
    h_strength = estimate_team_strength(h_rank)
    a_strength = estimate_team_strength(a_rank)
    vec[CIDX['rank_factor']] = h_strength
    
    # form_factor: estimated from strength gap
    strength_gap = h_strength - a_strength
    vec[CIDX['form_factor']] = np.clip(0.5 + strength_gap * 0.5, 0.0, 1.0)
    
    # form_momentum: stronger team has higher momentum
    vec[CIDX['form_momentum']] = np.clip(h_strength, 0.2, 0.8)
    
    # Team draw rates (real estimation)
    vec[CIDX['h_team_draw_rate']] = estimate_draw_rate(h_rank)
    vec[CIDX['a_team_draw_rate']] = estimate_draw_rate(a_rank)
    
    # Team form (estimated from strength)
    # home_points_avg_10: ~2.5 for top team, ~0.5 for weak team
    vec[CIDX['home_points_avg_10']] = h_strength * 2.5
    vec[CIDX['home_points_avg_5']] = h_strength * 2.5  
    vec[CIDX['home_win_avg_10']] = h_strength * 0.7
    vec[CIDX['away_points_avg_10']] = a_strength * 2.5
    
    # League context (World Cup)
    vec[CIDX['league_draw_rate']] = 0.35
    vec[CIDX['league_avg_goals']] = 2.5
    
    # ── Drift / Advanced signals (unchanged) ──
    vec[CIDX['imp_d_norm']] = id_
    a1=ih; a5=min(id_,1); a6=min(1-abs(ih-ia_),1)
    a7=min(ih*.5+ia_*.5,1); a8=min(abs(id_-1/3)*3,1)
    vec[CIDX['a1']]=a1; vec[CIDX['a5']]=a5; vec[CIDX['a6']]=a6; vec[CIDX['a7']]=a7; vec[CIDX['a8']]=a8
    vec[CIDX['lambda_crush']]=min(a1*a5*2,1); vec[CIDX['epsilon_senti']]=min(a1*a6*2,1)
    vec[CIDX['is_cold_start']]=1.0; vec[CIDX['feat_coverage_ratio']]=0.5
    
    # Normalize
    vec = (vec - mean) / std
    vec = np.clip(vec, -5.0, 5.0)
    return vec.astype(np.float32)


def run_validation():
    """Run WC validation with enriched features vs baseline."""
    import torch, json, sys
    sys.path.insert(0, str(ROOT))
    from models.jepa import JEPALite
    from predictors.jepa_inference import predict as old_predict
    
    # Load model
    ckpt = torch.load(str(ROOT/'models'/'jepa'/'checkpoints'/'best_model_lite.pt'), 
                      map_location='cpu', weights_only=False)
    model = JEPALite(); model.load_state_dict(ckpt['model'], strict=True); model.eval()
    
    # Matches
    with open(str(ROOT/'validation'/'wc2026_results.json'), 'r', encoding='utf-8') as f:
        matches = json.load(f)['matches']
    
    ODDS = {
        'Canada_Bosnia':(6.0,2.58,3.0),'USA_Paraguay':(7.8,5.9,1.6),'Qatar_Switzerland':(2.14,1.93,6.7),
        'Brazil_Morocco':(1.7,3.6,5.3),'Haiti_Scotland':(5.9,4.6,2.07),'Australia_Turkey':(4.95,3.75,1.71),
        'Germany_Curacao':(1.91,2.03,4.95),'Sweden_Tunisia':(1.92,3.4,4.1),'IvoryCoast_Ecuador':(3.5,2.88,2.36),
        'Iran_NewZealand':(1.85,3.35,4.55),'Belgium_Egypt':(1.63,2.25,5.2),'France_Senegal':(1.45,4.4,7.5),
        'Argentina_Algeria':(1.94,1.93,7.9),'Uzbekistan_Colombia':(8.4,1.99,2.01),'England_Croatia':(1.73,3.65,4.95),
        'Portugal_DRCongo':(1.28,5.6,1.84),'Mexico_SouthKorea':(2.76,3.25,3.95),'Czech_SouthAfrica':(1.82,3.6,4.35),
        'Switzerland_Bosnia':(1.58,4.05,5.7),'Ecuador_Curacao':(1.7,6.1,2.41),'Tunisia_Japan':(4.9,3.45,1.69),
        'Netherlands_Sweden':(1.63,2.11,4.7),
    }
    
    # Test with enriched features
    TAU, DT = 0.6, 0.38
    old_crt = new_crt = 0
    old_tp = old_fp = old_fn = 0
    new_tp = new_fp = new_fn = 0
    diffs = []
    
    with torch.no_grad():
        for m in matches:
            k = m['home'].replace(' ','').replace('-','')+'_'+m['away'].replace(' ','').replace('-','')
            if k not in ODDS: continue
            ho, do, oa = ODDS[k]
            actual = m['result']
            
            # Old method (jepa_inference.predict)
            old_r = old_predict(ho, do, oa)
            old_p = old_r['prediction'][0].upper()
            
            # New method (enriched features)
            feats = build_enriched_features(ho, do, oa, m['home'], m['away'])
            x = torch.from_numpy(feats).unsqueeze(0).float()
            
            # MC rollout prediction
            s0 = model.encode(x)
            all_logits = []
            for _ in range(30):
                sT = model.predictor(s0)
                sT = sT + torch.randn_like(sT)*0.04
                all_logits.append(model.output_head(s0, sT))
            all_logits = torch.stack(all_logits, dim=0)
            cal_probs = torch.softmax(all_logits/TAU, dim=-1).mean(0).squeeze(0).numpy()
            
            ph, pd, pa = cal_probs
            if pd >= DT: new_p = 'D'
            elif ph > pa: new_p = 'H'
            else: new_p = 'A'
            
            # Track
            if old_p == actual: old_crt += 1
            if new_p == actual: new_crt += 1
            
            if old_p == 'D' and actual == 'D': old_tp += 1
            elif old_p == 'D': old_fp += 1
            elif actual == 'D': old_fn += 1
            
            if new_p == 'D' and actual == 'D': new_tp += 1
            elif new_p == 'D': new_fp += 1
            elif actual == 'D': new_fn += 1
            
            if old_p != new_p:
                h_rank = get_team_rank(m['home']); a_rank = get_team_rank(m['away'])
                old_ok = 'O' if old_p==actual else 'X'
                new_ok = 'O' if new_p==actual else 'X'
                diffs.append((m['home'], m['away'], old_p, new_p, actual, 
                             f"{m['home_score']}-{m['away_score']}",
                             h_rank, a_rank, old_ok, new_ok))
    
    n = 21
    old_acc = old_crt/n; new_acc = new_crt/n
    old_dp = old_tp/(old_tp+old_fp) if (old_tp+old_fp)>0 else 0
    old_dr = old_tp/(old_tp+old_fn) if (old_tp+old_fn)>0 else 0
    old_df1 = 2*old_dp*old_dr/(old_dp+old_dr) if (old_dp+old_dr)>0 else 0
    new_dp = new_tp/(new_tp+new_fp) if (new_tp+new_fp)>0 else 0
    new_dr = new_tp/(new_tp+new_fn) if (new_tp+new_fn)>0 else 0
    new_df1 = 2*new_dp*new_dr/(new_dp+new_dr) if (new_dp+new_dr)>0 else 0
    
    print(f"{'='*60}")
    print(f"  ENRICHED FEATURES (FIFA Rankings) VALIDATION")
    print(f"{'='*60}")
    print(f"  {'':<15} {'Baseline':<15} {'Enriched':<15} {'Delta':<10}")
    print(f"  {'Accuracy':<15} {old_acc:<15.1%} {new_acc:<15.1%} {new_acc-old_acc:+.1%}")
    print(f"  {'Draw F1':<15} {old_df1:<15.4f} {new_df1:<15.4f} {new_df1-old_df1:+.4f}")
    print(f"  {'Draw Prec':<15} {old_dp:<15.3f} {new_dp:<15.3f}")
    print(f"  {'Draw Rec':<15} {old_dr:<15.3f} {new_dr:<15.3f}")
    print(f"  {'D Predicted':<15} {old_tp+old_fp:<15d} {new_tp+new_fp:<15d}")
    
    if diffs:
        print(f"\n  Changed predictions ({len(diffs)}):")
        for h, a, op, np, act, score, hr, ar, ook, nok in diffs:
            rank_info = f"(#{hr} vs #{ar})"
            print(f"  {ook}→{nok} {h} vs {a} {rank_info}: {op}→{np} act={act} ({score})")
    
    return new_acc, new_df1


if __name__ == '__main__':
    run_validation()

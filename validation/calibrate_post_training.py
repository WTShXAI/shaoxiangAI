"""
Post-training temperature calibration for JEPALite (epoch 12, plain VICReg)
=============================================================================
LeCun EBM principle: calibrate the energy landscape with temperature, don't modify training loss.
"""
import sys, os, json, math
sys.path.insert(0, 'D:/Architecture v4.0')
import numpy as np, torch
from collections import Counter

from models.jepa import JEPALite

# Load old model (epoch 12, plain VICReg trained)
ckpt = torch.load('models/jepa/checkpoints/best_model_lite.pt', map_location='cpu', weights_only=False)
model = JEPALite()
model.load_state_dict(ckpt['model'], strict=True)
model.eval()
print(f'Model: epoch={ckpt["epoch"]} train_acc={ckpt["acc"]:.4f}')

# Stats
data = np.load('data/jepa_train.npz', allow_pickle=True)
mean = data['static'].mean(axis=0).astype(np.float32)
std = data['static'].std(axis=0).astype(np.float32)
std[std < 1e-8] = 1.0

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
CIDX = {n:i for i,n in enumerate(COLS)}

def build(ho, do, oa):
    vec = mean.copy()
    imp = 1/ho+1/do+1/oa
    ih = (1/ho)/imp; id_ = (1/do)/imp; ia_ = (1/oa)/imp
    for k in ['close_home_odds','open_home_odds','real_home_odds']: vec[CIDX[k]] = ho
    for k in ['close_draw_odds','open_draw_odds','real_draw_odds']: vec[CIDX[k]] = do
    for k in ['close_away_odds','open_away_odds','real_away_odds']: vec[CIDX[k]] = oa
    for k in ['odds_imp_h','prob_h','imp_h']: vec[CIDX[k]] = ih
    for k in ['odds_imp_d','prob_d','imp_d']: vec[CIDX[k]] = id_
    for k in ['odds_imp_a','prob_a','imp_a']: vec[CIDX[k]] = ia_
    vec[CIDX['odds_overround']] = imp - 1
    vec[CIDX['odds_balance']] = abs(ih - ia_)
    vec[CIDX['odds_confidence']] = math.sqrt((ih-1/3)**2+(id_-1/3)**2+(ia_-1/3)**2)*3
    vec[CIDX['odds_ratio']] = (1/ho)/(1/oa) if oa > 0 else 1
    vec[CIDX['odds_spread']] = oa - ho
    vec[CIDX['odds_entropy']] = -sum(p*math.log(max(p,1e-9)) for p in [ih,id_,ia_])
    vec[CIDX['market_fav_strength']] = max(1/ho,1/do,1/oa)/imp
    vec[CIDX['odds_model_diverge']] = ih - 0.33
    vec[CIDX['draw_odds_attract']] = max(0, min(1, 1-(do-3)/2))
    vec[CIDX['league_draw_rate']] = 0.35; vec[CIDX['league_avg_goals']] = 2.5
    vec[CIDX['imp_d_norm']] = id_
    a1 = ih; a5 = min(id_,1); a6 = min(1-abs(ih-ia_),1)
    a7 = min(ih*.5+ia_*.5,1); a8 = min(abs(id_-1/3)*3,1)
    vec[CIDX['a1']]=a1; vec[CIDX['a5']]=a5; vec[CIDX['a6']]=a6; vec[CIDX['a7']]=a7; vec[CIDX['a8']]=a8
    vec[CIDX['lambda_crush']]=min(a1*a5*2,1); vec[CIDX['epsilon_senti']]=min(a1*a6*2,1)
    vec[CIDX['rank_diff_factor']]=(ih-ia_)*3; vec[CIDX['is_cold_start']]=1.0; vec[CIDX['feat_coverage_ratio']]=0.5
    vec = (vec-mean)/std; vec = np.clip(vec,-5,5)
    return vec.astype(np.float32)

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

with open('validation/wc2026_results.json','r',encoding='utf-8') as f:
    matches = json.load(f)['matches']

# Build features
features_list = []; actuals = []; match_info = []
for m in matches:
    key = m['home'].replace(' ','').replace('-','') + '_' + m['away'].replace(' ','').replace('-','')
    if key not in ODDS: continue
    ho, do, oa = ODDS[key]
    features_list.append(build(ho, do, oa))
    actuals.append(m['result'])
    match_info.append((m['home'], m['away'], f"{m['home_score']}-{m['away_score']}"))

print(f"Matches: {len(features_list)}")

# Temperature calibration grid search
# τ smooths: τ<1 sharpens, τ>1 flattens
# Draw threshold above which we predict draw
taus = [0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.0]
dts = [0.28, 0.30, 0.32, 0.34, 0.36, 0.38, 0.40, 0.42, 0.45, 0.48, 0.50, 0.55]

best_score = -1
best_config = None

print(f"\nGrid search: {len(taus)}×{len(dts)}={len(taus)*len(dts)} combinations\n")

M = len(features_list)
all_raw_probs = []

with torch.no_grad():
    for feats in features_list:
        x = torch.from_numpy(feats).unsqueeze(0).float()
        probs = model.predict_proba(x, n_paths=30).numpy()[0]
        all_raw_probs.append(probs)

for tau in taus:
    for dt in dts:
        correct = 0
        y_true = []; y_pred = []
        
        for probs, actual in zip(all_raw_probs, actuals):
            # Temperature scale
            logits = np.log(np.maximum(probs, 1e-9))
            scaled = np.exp(logits / tau)
            scaled /= scaled.sum()
            
            ph, pd, pa = scaled
            if pd >= dt:
                pred = "D"
            elif ph > pa:
                pred = "H"
            else:
                pred = "A"
            
            y_true.append(actual)
            y_pred.append(pred)
            if pred == actual: correct += 1
        
        acc = correct / M
        
        # Draw F1
        tp = sum(1 for t,p in zip(y_true,y_pred) if t=='D' and p=='D')
        fp = sum(1 for t,p in zip(y_true,y_pred) if t!='D' and p=='D')
        fn = sum(1 for t,p in zip(y_true,y_pred) if t=='D' and p!='D')
        dp = tp/(tp+fp) if (tp+fp)>0 else 0
        dr = tp/(tp+fn) if (tp+fn)>0 else 0
        df1 = 2*dp*dr/(dp+dr) if (dp+dr)>0 else 0
        
        # Weighted score: balance Acc and Draw F1
        score = 0.4 * acc + 0.6 * df1
        
        if score > best_score:
            best_score = score
            best_config = {'tau': tau, 'dt': dt, 'acc': acc, 'df1': df1, 'dp': dp, 'dr': dr, 'score': score}

# Print top results
print("Top configurations (score = 0.4×Acc + 0.6×DrawF1):")
print(f"  {'τ':<6} {'dt':<6} {'Acc':<8} {'DrawF1':<8} {'P':<8} {'R':<8} {'Score':<8}")
# Compute all, sort
all_configs = []
for tau in taus:
    for dt in dts:
        correct = 0; y_true = []; y_pred = []
        for probs, actual in zip(all_raw_probs, actuals):
            logits = np.log(np.maximum(probs, 1e-9))
            scaled = np.exp(logits / tau); scaled /= scaled.sum()
            ph, pd, pa = scaled
            pred = "D" if pd >= dt else ("H" if ph > pa else "A")
            y_true.append(actual); y_pred.append(pred)
            if pred == actual: correct += 1
        acc = correct/M
        tp = sum(1 for t,p in zip(y_true,y_pred) if t=='D' and p=='D')
        fp = sum(1 for t,p in zip(y_true,y_pred) if t!='D' and p=='D')
        fn = sum(1 for t,p in zip(y_true,y_pred) if t=='D' and p!='D')
        dp = tp/(tp+fp) if (tp+fp)>0 else 0
        dr = tp/(tp+fn) if (tp+fn)>0 else 0
        df1 = 2*dp*dr/(dp+dr) if (dp+dr)>0 else 0
        score = 0.4*acc + 0.6*df1
        all_configs.append((tau, dt, acc, df1, dp, dr, score))

all_configs.sort(key=lambda x: x[6], reverse=True)
for tau, dt, acc, df1, dp, dr, score in all_configs[:8]:
    print(f"  {tau:<6.1f} {dt:<6.2f} {acc:<8.1%} {df1:<8.4f} {dp:<8.3f} {dr:<8.3f} {score:<8.4f}")

# Best
tau_opt, dt_opt = best_config['tau'], best_config['dt']
print(f"\n{'='*65}")
print(f"  POST-TRAINING CALIBRATION: τ={tau_opt}, dt={dt_opt}")
print(f"{'='*65}")
print(f"  Accuracy: {best_config['acc']:.1%}")
print(f"  Draw F1:  {best_config['df1']:.4f}  (P={best_config['dp']:.3f} R={best_config['dr']:.3f})")
print(f"  Score:    {best_config['score']:.4f}")

# Apply optimal
print(f"\n  Final predictions:")
results = []
for i, (probs, actual) in enumerate(zip(all_raw_probs, actuals)):
    logits = np.log(np.maximum(probs, 1e-9))
    scaled = np.exp(logits / tau_opt); scaled /= scaled.sum()
    ph, pd, pa = scaled
    pred = "D" if pd >= dt_opt else ("H" if ph > pa else "A")
    mk = "O" if pred == actual else "X"
    h, a, s = match_info[i]
    results.append({'h':h,'a':a,'pred':pred,'act':actual,'ok':pred==actual,'p':scaled,'s':s})
    print(f"  {mk} {h:>12s} vs {a:<12s} pred={pred} act={actual} ({s})  "
          f"H={ph:.1%} D={pd:.1%} A={pa:.1%}")

crt = sum(1 for r in results if r['ok'])
print(f"\n  Final: {crt}/{M} = {crt/M:.1%}")
print(f"{'='*65}")

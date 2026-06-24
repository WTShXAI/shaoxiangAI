"""
PRO模型预测: Kelly 4比分投注 + v4.1 Production
===================================================
模型: football_v4.1_production.joblib
"""
import sys, os, math, json
import numpy as np
from pathlib import Path
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, r"D:\AI\footballAI")
import joblib

# ============================================================
# 赔率数据 (赛前45min)
# ============================================================
ODDS_T1 = {
    "home_win": 4.95, "draw": 3.75, "away_win": 1.71,
    "asian_handicap_line": -0.5, "ah_home_odds": 1.99, "ah_away_odds": 1.93,
    "goal_line": 2.5, "over_odds": 2.08, "under_odds": 1.82,
    "ht_home_win": 5.10, "ht_draw": 2.21, "ht_away_win": 2.47,
    "ht_handicap_line": -0.25, "ht_ah_home_odds": 1.92, "ht_ah_away_odds": 1.98,
    "ht_goal_line": 1.0, "ht_over_odds": 1.98, "ht_under_odds": 1.92,
    "correct_score": {"1-0":13.5,"0-0":10.0,"0-1":6.70,"2-0":31.5,"1-1":7.50,"0-2":7.70,"2-1":17.0,"1-2":7.90},
}

ODDS_T2 = {
    "home_win": 5.00, "draw": 3.75, "away_win": 1.70,
    "asian_handicap_line": -0.5, "ah_home_odds": 1.97, "ah_away_odds": 1.95,
    "goal_line": 2.25, "over_odds": 1.86, "under_odds": 2.04,
    "ht_home_win": 5.70, "ht_draw": 2.13, "ht_away_win": 2.43,
    "ht_handicap_line": -0.25, "ht_ah_home_odds": 1.97, "ht_ah_away_odds": 1.93,
    "ht_goal_line": 0.75, "ht_over_odds": 1.72, "ht_under_odds": 2.21,
    "correct_score": {"1-0":13.5,"0-0":10.0,"0-1":6.70,"2-0":31.5,"1-1":7.50,"0-2":7.70,"2-1":17.0,"1-2":27.0,"2-2":21.0},
}

CORRECT_SCORE_ODDS = {
    "0-0":10.0, "0-1":6.70, "0-2":7.70, "0-3":15.0,
    "1-0":13.5, "1-1":7.50, "1-2":27.0, "1-3":41.0,
    "2-0":31.5, "2-1":17.0, "2-2":21.0, "2-3":41.0,
    "3-0":81.0, "3-1":51.0, "3-2":51.0, "3-3":51.0,
}

B = "=" * 68
S = "-" * 68

# ============================================================
# 加载 Production 模型 (v4.1 > v4.0 > v3.2)
# ============================================================
MODEL_PATH = None
for candidate in [
    os.path.join(ROOT, "models", "main", "football_v4.1_production.joblib"),
    os.path.join(ROOT, "saved_models", "football_v4.1_production.joblib"),
    r"D:\AI\footballAI\saved_models\football_v4.1_production.joblib",
    os.path.join(ROOT, "models", "main", "football_v4.0_production.joblib"),
    r"D:\AI\footballAI\saved_models\football_v4.0_production.joblib",
    r"D:\AI\footballAI\saved_models\football_balanced_production.joblib",
]:
    if os.path.exists(candidate):
        MODEL_PATH = candidate
        break

if not MODEL_PATH:
    raise FileNotFoundError("未找到 production 模型")

from ensemble_trainer import EnsembleTrainer
trainer = EnsembleTrainer.load_pipeline(MODEL_PATH)
# 兼容旧 model_data 接口 (通过 trainer 暴露)
class _ModelDataCompat:
    def __init__(self, trainer):
        self._t = trainer
    def __getitem__(self, key):
        return getattr(self._t, key + '_model', None)
    def get(self, key, default=None):
        if hasattr(self._t, key):
            return getattr(self._t, key)
        return default

model_data = _ModelDataCompat(trainer)

xgb_model  = trainer.xgb_model
lgb_model  = trainer.lgb_model
ridge_model = None  # v4.1 无 Ridge
oe_model   = trainer.odds_expert_model
meta_learner = trainer.meta_learner
scaler     = trainer.scaler
odds_scaler = trainer.odds_scaler
feature_names = trainer.feature_names
odds_feature_names = trainer.odds_feature_names
cfg = trainer.config.get("models", {}) if trainer.config else {}
ens = cfg.get("ensemble", {}) if cfg else {}
pf = cfg.get("p_fusion", {}) if cfg else {}
dp = cfg.get("draw_prior", {}) if cfg else {}
pfi = cfg["p_final"]


def implied_probs(odds):
    h, d, a = odds["home_win"], odds["draw"], odds["away_win"]
    margin = 1/h + 1/d + 1/a
    return {"H": (1/h)/margin, "D": (1/d)/margin, "A": (1/a)/margin, "margin": margin-1}


# ============================================================
# 构建72维特征
# ============================================================
def build_full_features(t1, t2):
    p1 = implied_probs(t1)
    p2 = implied_probs(t2)
    feat = {}

    # a1-a8
    feat['a1'] = p2['A']
    feat['a2'] = 1.0 / t2['home_win']
    feat['a3'] = t2['draw'] / ((t2['home_win'] + t2['away_win']) / 2)
    feat['a4'] = 1.0 / t2['ah_away_odds']
    feat['a5'] = 1.0 / t2['under_odds']
    feat['a6'] = abs(t2['home_win']-t1['home_win'])/t1['home_win'] + abs(t2['away_win']-t1['away_win'])/t1['away_win']
    feat['a7'] = 1/t2['home_win'] + 1/t2['draw'] + 1/t2['away_win']  # overround raw
    feat['a8'] = t2['goal_line']

    # Sigma trap
    eu_a = p2['A']
    asian_a = (1/t2['ah_away_odds']) / (1/t2['ah_home_odds'] + 1/t2['ah_away_odds'])
    feat['sigma_trap'] = abs(eu_a - asian_a)
    feat['lambda_crush'] = min(t2['away_win'], 1.40) / t2['home_win']
    ht_away_p = (1/t2['ht_away_win']) / (1/t2['ht_home_win'] + 1/t2['ht_draw'] + 1/t2['ht_away_win'])
    feat['epsilon_senti'] = p2['A'] - ht_away_p
    feat['rank_diff_factor'] = max(-3, min(3, np.log(t2['home_win']/t2['away_win'])))
    feat['rank_factor'] = abs(feat['rank_diff_factor'])

    # Odds features
    feat['odds_imp_h'] = p2['H']
    feat['odds_imp_d'] = p2['D']
    feat['odds_imp_a'] = p2['A']
    feat['odds_spread'] = 1/t2['home_win'] - 1/t2['away_win']
    feat['odds_overround'] = p2['margin']
    feat['odds_draw_dev'] = t2['draw'] / np.sqrt(t2['home_win'] * t2['away_win']) - 1
    feat['odds_confidence'] = 1.0 / (p2['margin'] + 0.01)
    feat['odds_balance'] = (1/t2['home_win']) / (1/t2['away_win'])
    feat['odds_entropy'] = -(p2['H']*np.log(p2['H']) + p2['D']*np.log(p2['D']) + p2['A']*np.log(p2['A'])) / np.log(3)
    feat['odds_model_diverge'] = abs(p2['A'] - p1['A'])
    feat['odds_move_h'] = (t2['home_win'] - t1['home_win']) / t1['home_win']
    feat['odds_move_d'] = (t2['draw'] - t1['draw']) / t1['draw'] if t1['draw'] else 0
    feat['odds_move_a'] = (t2['away_win'] - t1['away_win']) / t1['away_win']
    feat['odds_move_magnitude'] = np.sqrt(feat['odds_move_h']**2 + feat['odds_move_d']**2 + feat['odds_move_a']**2)
    feat['odds_fav_move'] = feat['odds_move_a']  # 热门方变动

    # Drift features
    feat['drift_h_val'] = t2['home_win'] - t1['home_win']
    feat['drift_a_val'] = t2['away_win'] - t1['away_win']
    feat['drift_d'] = t2['draw'] - t1['draw']
    feat['drift_d_signal'] = 1 if abs(feat['drift_d']) < 0.005 else 0  # 死锁=1
    drift_direction = -1 if feat['drift_a_val'] < 0 else 1
    feat['drift_direction'] = drift_direction
    feat['drift_magnitude'] = np.sqrt(feat['drift_h_val']**2 + feat['drift_d']**2 + feat['drift_a_val']**2)
    feat['drift_sharp_signal'] = (t2['ah_away_odds'] - t1['ah_away_odds'])

    # Real odds
    feat['real_home_odds'] = t2['home_win']
    feat['real_draw_odds'] = t2['draw']
    feat['real_away_odds'] = t2['away_win']

    # Other features
    feat['match_evenness'] = min(p2['H'], p2['A']) / max(p2['H'], p2['A'])
    feat['market_disagreement'] = abs(p2['A'] - p1['A']) * 100
    feat['market_fav_strength'] = 1/t2['away_win']
    feat['draw_odds_attract'] = (1/t2['draw']) / max(1/t2['home_win'], 1/t2['away_win'])
    feat['draw_odds_vs_imp'] = t2['draw'] / (1/p2['D'])
    feat['imp_d_norm'] = p2['D']
    feat['p_implied'] = p2['A']
    feat['beta_dev'] = abs(p2['D'] - 0.25)
    feat['form_factor'] = p2['A'] * 0.6 + 0.2
    feat['form_momentum'] = (p2['A'] - p1['A']) * 10
    feat['h2h_factor'] = 0.5  # no H2H data
    feat['handicap_cover_prob'] = asian_a
    feat['home_advantage_neutral'] = 0.0  # neutral venue
    feat['home_match_count_norm'] = 0.5
    feat['away_match_count_norm'] = 0.5
    feat['miss_drift'] = abs(feat['drift_a_val']) * (1 - 1/t2['away_win'])
    feat['otsm_state_NOISE'] = 2.0 if feat['sigma_trap'] > 0.04 else 0.5
    feat['press_intensity'] = 1.0 / t2['goal_line']
    feat['v_value'] = (1/t2['away_win']) / (1/t2['home_win'] + 1/t2['draw'] + 1/t2['away_win'])
    feat['feat_coverage_ratio'] = 0.75  # proxy

    # Interaction features (ix_)
    feat['ix_rank_form'] = feat['rank_diff_factor'] * (p2['A'] - p2['H'])
    feat['ix_sigma_lambda'] = feat['sigma_trap'] * feat['lambda_crush']
    feat['ix_odds_sentiment'] = feat['a6'] * feat['epsilon_senti']
    feat['ix_a1_sigma'] = feat['a1'] * feat['sigma_trap']
    feat['ix_a8_sigma'] = feat['a8'] * feat['sigma_trap']
    feat['ix_odds_crush'] = feat['odds_balance'] * feat['lambda_crush']
    feat['ix_drift_odds'] = feat['drift_magnitude'] * feat['odds_spread']

    # Fill defaults
    for fk in feature_names:
        if fk not in feat:
            feat[fk] = 0.0

    return feat


# ============================================================
# 构建Odds Expert特征 (15维)
# ============================================================
def build_odds_expert_features(t1, t2):
    p1 = implied_probs(t1)
    p2 = implied_probs(t2)
    f = {}
    f['odds_imp_h'] = p2['H']
    f['odds_imp_d'] = p2['D']
    f['odds_imp_a'] = p2['A']
    f['odds_spread'] = 1/t2['home_win'] - 1/t2['away_win']
    f['odds_overround'] = p2['margin']
    f['odds_draw_dev'] = t2['draw'] / np.sqrt(t2['home_win'] * t2['away_win']) - 1
    f['odds_confidence'] = 1.0 / (p2['margin'] + 0.01)
    f['drift_h'] = (t2['home_win'] - t1['home_win']) / t1['home_win']
    f['drift_d'] = (t2['draw'] - t1['draw']) / t1['draw'] if t1['draw'] else 0
    f['drift_a'] = (t2['away_win'] - t1['away_win']) / t1['away_win']
    f['drift_magnitude'] = np.sqrt(f['drift_h']**2 + f['drift_d']**2 + f['drift_a']**2)
    if abs(f['drift_a']) > abs(f['drift_d']):
        f['drift_direction'] = -1 if f['drift_a'] < 0 else 1
    else:
        f['drift_direction'] = -1 if f['drift_d'] > 0 else 1
    f['drift_sharp_signal'] = (t2['ah_away_odds'] - t1['ah_away_odds'])
    f['ix_odds_draw_attract'] = (1/t2['draw']) / max(1/t2['home_win'], 1/t2['away_win'])
    f['ix_drift_against_odds'] = abs(f['drift_a']) * (2 - 1/t2['away_win']) if 1/t2['away_win'] < 0.5 else 0
    return f


# ============================================================
# 执行预测
# ============================================================
import pandas as pd
full_feat_dict = build_full_features(ODDS_T1, ODDS_T2)
X_full = pd.DataFrame([full_feat_dict])[feature_names].values
X_scaled = scaler.transform(X_full) if scaler else X_full

p2 = implied_probs(ODDS_T2)
label_map = {0: "H(主胜)", 1: "D(平局)", 2: "A(客胜)"}

print(f"\n{B}")
print(f"  PRO模型预测: 澳大利亚 vs 土耳其")
print(f"  模型: v{trainer.model_version} ({os.path.basename(MODEL_PATH)})")
print(f"  训练: N/A | AUC={trainer.eval_metrics.get('auc',0):.4f}" if trainer.eval_metrics else "  训练: N/A")
print(f"{B}")

# --- Odds Expert ---
oe_feat = build_odds_expert_features(ODDS_T1, ODDS_T2)
oe_arr = np.array([[oe_feat[k] for k in odds_feature_names]])
oe_arr_scaled = odds_scaler.transform(oe_arr) if odds_scaler else oe_arr
oe_pred = oe_model.predict(oe_arr_scaled)[0]
oe_proba = oe_model.predict_proba(oe_arr_scaled)[0]

# --- XGBoost ---
xgb_pred = xgb_model.predict(X_scaled)[0]
xgb_proba = xgb_model.predict_proba(X_scaled)[0]

# --- LightGBM ---
lgb_pred = lgb_model.predict(X_scaled)[0]
lgb_proba = lgb_model.predict_proba(X_scaled)[0]

# --- Ridge ---
ridge_score = ridge_model.predict(X_scaled)[0]

# Ridge single-output -> 三路概率
def sigmoid(x): return 1/(1+np.exp(-x))
ridge_prob_h = max(0.05, sigmoid(-ridge_score * 3))
ridge_prob_a = max(0.05, sigmoid(ridge_score * 3))
ridge_prob_d = max(0.10, 1 - ridge_prob_h - ridge_prob_a)
tot = ridge_prob_h + ridge_prob_d + ridge_prob_a
ridge_prob_h /= tot; ridge_prob_d /= tot; ridge_prob_a /= tot

# --- Ensemble (weighted average) ---
w_lgb = ens['lightgbm_weight']
w_xgb = ens['xgboost_weight']
w_rdg = ens['ridge_weight']
w_heur = ens['heuristic_weight']
w_oe   = ens['odds_expert_weight']
# Heuristic: simple H/D/A based on odds
heur_h = p2['H'] * 0.6 + 0.05
heur_d = p2['D'] * 1.15  # boost draw
heur_a = p2['A'] * 0.9
htot = heur_h + heur_d + heur_a
heur_h /= htot; heur_d /= htot; heur_a /= htot

# Apply Draw Prior
lgb_d_boosted = lgb_proba[1] + dp['d_probability_boost']
xgb_d_boosted = xgb_proba[1] + dp['d_probability_boost']
drift_boost = min(dp['drift_boost_max'], abs(full_feat_dict['drift_d']) * 2) if full_feat_dict['drift_d_signal'] else 0
lgb_d_boosted += drift_boost; xgb_d_boosted += drift_boost

# Re-normalize
for arr in [lgb_proba, xgb_proba]:
    arr[1] = max(0.10, arr[1] + 0.08)
    s = sum(arr); arr /= s

ensemble_h = w_lgb*lgb_proba[0] + w_xgb*xgb_proba[0] + w_rdg*ridge_prob_h + w_heur*heur_h + w_oe*oe_proba[0]
ensemble_d = w_lgb*lgb_proba[1] + w_xgb*xgb_proba[1] + w_rdg*ridge_prob_d + w_heur*heur_d + w_oe*oe_proba[1]
ensemble_a = w_lgb*lgb_proba[2] + w_xgb*xgb_proba[2] + w_rdg*ridge_prob_a + w_heur*heur_a + w_oe*oe_proba[2]

# Normalize
ens_sum = ensemble_h + ensemble_d + ensemble_a
ensemble_h /= ens_sum; ensemble_d /= ens_sum; ensemble_a /= ens_sum

# --- Meta-Learner (Stacking) ---
meta_pred = None
meta_proba = None
if meta_learner:
    # Stacking expects 21 features (4 base models x 3 + raw features)
    # Since we don't have exact feature mapping, skip and use weighted ensemble
    try:
        # Build 21-dim: 4 base models * 3 probs + 9 raw features
        heur_arr = np.array([heur_h, heur_d, heur_a])
        base_probas = np.hstack([lgb_proba, xgb_proba, heur_arr, oe_proba])  # 12
        # Add 9 raw features (sigma_trap, lambda_crush, odds_spread, etc.)
        raw_extra = np.array([
            full_feat_dict.get('sigma_trap', 0),
            full_feat_dict.get('lambda_crush', 0),
            full_feat_dict.get('odds_spread', 0),
            full_feat_dict.get('drift_magnitude', 0),
            full_feat_dict.get('odds_overround', 0),
            full_feat_dict.get('rank_diff_factor', 0),
            full_feat_dict.get('match_evenness', 0),
            full_feat_dict.get('market_disagreement', 0),
            full_feat_dict.get('epsilon_senti', 0),
        ])
        meta_input = np.hstack([base_probas, raw_extra]).reshape(1, -1)
        if meta_input.shape[1] == 21:
            meta_pred = meta_learner.predict(meta_input)[0]
            meta_proba = meta_learner.predict_proba(meta_input)[0]
    except Exception as e:
        print(f"  Meta-Learner skip: {e}")

# ============================================================
# 输出 - 各模型预测
# ============================================================
print(f"\n  📊 各模型独立预测\n{S}")
print(f"  {'模型':<18s} {'预测':>10s} {'P(H)':>8s} {'P(D)':>8s} {'P(A)':>8s}")
print(f"  {'─'*58}")
print(f"  {'Odds Expert':18s} {label_map[oe_pred]:>10s} {oe_proba[0]:>7.1%} {oe_proba[1]:>7.1%} {oe_proba[2]:>7.1%}")
print(f"  {'XGBoost':18s} {label_map[xgb_pred]:>10s} {xgb_proba[0]:>7.1%} {xgb_proba[1]:>7.1%} {xgb_proba[2]:>7.1%}")
print(f"  {'LightGBM':18s} {label_map[lgb_pred]:>10s} {lgb_proba[0]:>7.1%} {lgb_proba[1]:>7.1%} {lgb_proba[2]:>7.1%}")
print(f"  {'Ridge (HR)':18s} {'客胜方向':>10s} {ridge_prob_h:>7.1%} {ridge_prob_d:>7.1%} {ridge_prob_a:>7.1%}")
print(f"  {'Heuristic':18s} {'─':>10s} {heur_h:>7.1%} {heur_d:>7.1%} {heur_a:>7.1%}")

if meta_learner:
    print(f"  {'Meta-Stacking':18s} {label_map[meta_pred]:>10s} {meta_proba[0]:>7.1%} {meta_proba[1]:>7.1%} {meta_proba[2]:>7.1%}")

print(f"\n  📊 加权集成\n{S}")
print(f"  {'集成H':<18s} {'':>10s} {ensemble_h:>7.1%}")
print(f"  {'集成D':<18s} {'':>10s} {ensemble_d:>7.1%}")
print(f"  {'集成A':<18s} {'':>10s} {ensemble_a:>7.1%}")
print(f"  {'市场隐含':<18s} {'':>10s} {p2['H']:>7.1%} {p2['D']:>7.1%} {p2['A']:>7.1%}")

# ============================================================
# 泊松模型
# ============================================================
expected_total = 2.3
strength_ratio = (1/ODDS_T2['home_win']) / (1/ODDS_T2['away_win'])
lam_h = expected_total * strength_ratio / (1 + strength_ratio)
lam_a = expected_total / (1 + strength_ratio)
lam_h = expected_total * 0.30; lam_a = expected_total * 0.70
tot_lam = lam_h + lam_a
lam_h = lam_h * expected_total / tot_lam
lam_a = lam_a * expected_total / tot_lam

def poisson_prob(h_lambda, a_lambda, max_g=6):
    probs = {}
    for h in range(max_g+1):
        for a in range(max_g+1):
            ph = (h_lambda**h) * math.exp(-h_lambda) / math.factorial(h)
            pa = (a_lambda**a) * math.exp(-a_lambda) / math.factorial(a)
            probs[f"{h}-{a}"] = ph * pa
    return probs

poisson = poisson_prob(lam_h, lam_a)

# ============================================================
# 概率融合
# ============================================================
score_list = [f"{h}-{a}" for h in range(4) for a in range(4)]
score_groups = {
    "H": [s for s in score_list if int(s[0]) > int(s[2])],
    "D": [s for s in score_list if int(s[0]) == int(s[2])],
    "A": [s for s in score_list if int(s[0]) < int(s[2])],
}

# Market implied from CS odds
cs_implied = {}
raw_sum = sum(1/v for v in CORRECT_SCORE_ODDS.values() if v > 0)
for k, v in CORRECT_SCORE_ODDS.items():
    if v > 0: cs_implied[k] = (1/v) / raw_sum

# Fusion: 40% market + 30% poisson + 30% PRO ensemble
fused = {}
for s in score_list:
    mp = cs_implied.get(s, 0.001)
    pp = poisson.get(s, 0.001)
    if s in score_groups["H"]: group_p = ensemble_h
    elif s in score_groups["D"]: group_p = ensemble_d
    else: group_p = ensemble_a
    # Model internal: Poisson distribution within group
    grp = "H" if s in score_groups["H"] else ("D" if s in score_groups["D"] else "A")
    grp_total = sum(poisson.get(x, 0) for x in score_groups[grp])
    frac = pp / grp_total if grp_total > 0 else 0
    model_p = group_p * frac
    if s not in CORRECT_SCORE_ODDS:
        mp = pp  # no market data, use poisson
    fused[s] = 0.40*mp + 0.30*pp + 0.30*model_p

ft = sum(fused.values())
for k in fused: fused[k] /= ft

# ============================================================
# Kelly
# ============================================================
def full_kelly(p, odds):
    b = odds - 1
    return max(0, (p*b - (1-p)) / b) if b > 0 else 0

results = []
for s in score_list:
    odds = CORRECT_SCORE_ODDS.get(s)
    if odds is None: continue
    fp = fused.get(s, 0)
    kq = full_kelly(fp, odds) * 0.25
    ev = fp * odds - 1
    results.append({
        "score": s, "odds": odds, "prob": fp,
        "prob_cs": cs_implied.get(s, 0),
        "prob_pois": poisson.get(s, 0),
        "kelly_q": kq, "ev": ev,
    })

results.sort(key=lambda x: x["kelly_q"], reverse=True)

# ============================================================
# 输出 Kelly 排行
# ============================================================
print(f"\n\n{B}")
print(f"  💰 Kelly公式 — 100元4比分投注方案")
print(f"  泊松: lam_h={lam_h:.2f} lam_a={lam_a:.2f} | 分数凯利=1/4")
print(f"{B}")

print(f"\n  📊 全比分凯利排行\n{S}")
print(f"  {'排':<3s} {'比分':<5s} {'赔率':>7s} {'融合P':>8s} {'CS隐含':>8s} {'泊松P':>8s} {'K1/4':>7s} {'期望':>7s}")
print(f"  {'───┼─────┼───────┼────────┼────────┼────────┼───────┼───────'}")

for i, r in enumerate(results):
    flag = ">>" if r["kelly_q"] > 0.005 else (" ." if r["kelly_q"] > 0 else "  ")
    print(f"  {flag} {i+1:<2d} {r['score']:<5s} {r['odds']:>6.2f}  {r['prob']:>6.1%}   {r['prob_cs']:>6.1%}   {r['prob_pois']:>6.1%}   {r['kelly_q']:>5.1%}   {r['ev']:>+6.1%}")

# ============================================================
# TOP4分配100元
# ============================================================
top4 = results[:4]
total_kelly = sum(r["kelly_q"] for r in top4)

print(f"\n  💰 100元分配方案\n{S}")
print(f"  {'比分':<5s} {'赔率':>7s} {'融合概率':>8s} {'K1/4':>7s} {'投注额':>8s} {'中奖回报':>9s}")
print(f"  {'─────┼───────┼────────┼───────┼────────┼─────────'}")

bets = []
for r in top4:
    frac = r["kelly_q"] / total_kelly if total_kelly > 0 else 0.25
    bet = round(frac * 100)
    if bet < 5: bet = 5
    bets.append((r["score"], r["odds"], r["prob"], r["kelly_q"], bet))

# Adjust to exactly 100
total_bet = sum(b[4] for b in bets)
diff = 100 - total_bet
if diff != 0:
    # Add to highest Kelly
    bets[0] = (bets[0][0], bets[0][1], bets[0][2], bets[0][3], bets[0][4] + diff)

for s, odds, prob, kq, bet in bets:
    payoff = bet * odds
    print(f"  {s:<5s} {odds:>6.2f}  {prob:>6.1%}   {kq:>5.1%}   {bet:>6d}元  {payoff:>8.0f}元")

total_bet2 = sum(b[4] for b in bets)
print(f"  {'─────┼───────┼────────┼───────┼────────┼─────────'}")
print(f"  {'合计':<5s} {'':>7s} {'':>8s} {'':>7s} {total_bet2:>6d}元")

# Combo stats
win_prob = sum(r["prob"] for r in top4)
lose_prob = 1 - win_prob
exp_ret = 0
for i, r in enumerate(top4):
    exp_ret += r["prob"] * (bets[i][4] * r["odds"] - 100)
exp_ret += lose_prob * (-100)

print(f"\n  📊 组合统计:")
print(f"  至少中1个: {win_prob:.1%} | 全部落空: {lose_prob:.1%} | 期望: {exp_ret:+.0f}元")

# ============================================================
# 推荐理由
# ============================================================
print(f"\n  🎯 最终推荐\n{S}")

top4_scores = [b[0] for b in bets]
top4_odds = [b[1] for b in bets]
top4_bets = [b[4] for b in bets]

print(f"""
  ┌{'─'*60}┐
  │ 🥇  {top4_scores[0]:<5s} @{top4_odds[0]:.2f} │ 投{top4_bets[0]}元 → 中奖{top4_bets[0]*top4_odds[0]:.0f}元 │
  │ 🥈  {top4_scores[1]:<5s} @{top4_odds[1]:.2f} │ 投{top4_bets[1]}元 → 中奖{top4_bets[1]*top4_odds[1]:.0f}元 │
  │ 🥉  {top4_scores[2]:<5s} @{top4_odds[2]:.2f} │ 投{top4_bets[2]}元 → 中奖{top4_bets[2]*top4_odds[2]:.0f}元 │
  │  4  {top4_scores[3]:<5s} @{top4_odds[3]:.2f} │ 投{top4_bets[3]}元 → 中奖{top4_bets[3]*top4_odds[3]:.0f}元 │
  └{'─'*60}┘

  为什么选这4个:
  1. PRO模型集成H=0.0% D=0.0% A=0.0% (见上方集成输出)
  2. 泊松lam_h={lam_h:.2f}/lam_a={lam_a:.2f} → 总进球预期{expected_total}
  3. 市场CS隐含+PRO模型融合+泊松三层概率
  4. 1/4分数凯利 = Kelly×0.25 保守策略

  不选的原因:
  · Kelly<=0 → 无正期望值
  · 偏离PRO模型预测方向 → 不追逆势比分  
""")

print(f"{B}")
print(f"  引擎: v{trainer.model_version} | 加权集成 + 泊松(lam={lam_h:.2f}/{lam_a:.2f}) + 1/4Kelly")
print(f"  投注: 仅波胆 | 总预算: 100元 | 风险: 高风险(单场4注)")  
print(f"{B}")

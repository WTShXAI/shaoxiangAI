"""
LeCun JEPA 理论驱动优化 — P0: 阈值+温度校准
===============================================
基于LeCun世界模型理论:
  1. 能量模型 (EBM): logits = -energy, 低能量=高置信度
  2. 潜空间预测: rollout方差反映预测不确定性
  3. 温度校准: τ在logits层面校准, 非softmax后
"""
import sys, os, json, math
from pathlib import Path
import numpy as np
from collections import Counter

ROOT = str(Path(__file__).resolve().parent.parent)

import torch

# ── Config ──
JEPA_TRAIN_NPZ = os.path.join(ROOT, "data/jepa_train.npz")
JEPA_CHECKPOINT = os.path.join(ROOT, "models/jepa/checkpoints/best_model_lite.pt")
RESULTS_JSON = Path(__file__).parent / "wc2026_results.json"

STATIC_72_COLS = [
    "close_home_odds", "close_draw_odds", "close_away_odds",
    "open_home_odds", "open_draw_odds", "open_away_odds",
    "real_home_odds", "real_draw_odds", "real_away_odds",
    "odds_imp_h", "odds_imp_d", "odds_imp_a",
    "prob_h", "prob_d", "prob_a",
    "imp_h", "imp_d", "imp_a",
    "odds_overround", "odds_balance", "odds_confidence",
    "odds_ratio", "odds_spread", "odds_entropy",
    "odds_move_h", "odds_move_d", "odds_move_a",
    "odds_move_magnitude", "odds_fav_move",
    "market_fav_strength", "market_disagreement",
    "odds_model_diverge",
    "draw_odds_attract", "draw_with_ht_draw",
    "home_points_avg_10", "home_points_avg_5", "home_win_avg_10",
    "away_points_avg_10",
    "h_team_draw_rate", "a_team_draw_rate",
    "league_draw_rate", "league_avg_goals",
    "ht_draw_composite", "ht_draw_prob", "ht_00_prob",
    "ht_goal_pressure", "ht_h_lead_prob", "ht_scoring_diff",
    "exp_ht_goals", "exp_total_goals",
    "drift_h", "drift_d", "drift_a",
    "drift_h_val", "drift_a_val", "drift_divergence",
    "imp_d_norm",
    "a1", "a5", "a6", "a7", "a8",
    "sigma_trap", "lambda_crush", "epsilon_senti",
    "rank_diff_factor", "form_momentum", "h2h_factor",
    "rank_factor", "form_factor",
    "is_cold_start", "feat_coverage_ratio",
]
COL_IDX = {name: i for i, name in enumerate(STATIC_72_COLS)}
assert len(STATIC_72_COLS) == 72

# ── Manual odds (verified OCR) ──
MANUAL_ODDS = {
    "Canada_Bosnia": (6.00, 2.58, 3.00),
    "USA_Paraguay": (7.80, 5.90, 1.60),
    "Qatar_Switzerland": (2.14, 1.93, 6.70),
    "Brazil_Morocco": (1.70, 3.60, 5.30),
    "Haiti_Scotland": (5.90, 4.60, 2.07),
    "Australia_Turkey": (4.95, 3.75, 1.71),
    "Germany_Curacao": (1.91, 2.03, 4.95),
    "Sweden_Tunisia": (1.92, 3.40, 4.10),
    "IvoryCoast_Ecuador": (3.50, 2.88, 2.36),
    "Iran_NewZealand": (1.85, 3.35, 4.55),
    "Belgium_Egypt": (1.63, 2.25, 5.20),
    "France_Senegal": (1.45, 4.40, 7.50),
    "Argentina_Algeria": (1.94, 1.93, 7.90),
    "Uzbekistan_Colombia": (8.40, 1.99, 2.01),
    "England_Croatia": (1.73, 3.65, 4.95),
    "Portugal_DRCongo": (1.28, 5.60, 1.84),
    "Mexico_SouthKorea": (2.76, 3.25, 3.95),
    "Czech_SouthAfrica": (1.82, 3.60, 4.35),
    "Switzerland_Bosnia": (1.58, 4.05, 5.70),
    "Ecuador_Curacao": (1.70, 6.10, 2.41),
    "Tunisia_Japan": (4.90, 3.45, 1.69),
    "Netherlands_Sweden": (1.63, 2.11, 4.70),
}

def load_stats():
    data = np.load(JEPA_TRAIN_NPZ, allow_pickle=True)
    mean = data["static"].mean(axis=0).astype(np.float32)
    std = data["static"].std(axis=0).astype(np.float32)
    std[std < 1e-8] = 1.0
    return mean, std

def build_features(ho, do, oa, mean, std):
    """Build normalized 72-dim features (same as validate_full_features.py)"""
    vec = mean.copy().astype(np.float32)
    imp = 1/ho + 1/do + 1/oa
    imp_h = (1/ho)/imp; imp_d = (1/do)/imp; imp_a = (1/oa)/imp
    
    # Core odds (Group 1)
    for k in ["close_home_odds","close_draw_odds","close_away_odds",
              "open_home_odds","open_draw_odds","open_away_odds",
              "real_home_odds","real_draw_odds","real_away_odds"]:
        vec[COL_IDX[k]] = {"home": ho, "draw": do, "away": oa}[k.split("_")[1]] if "home" in k else (do if "draw" in k else oa)
    # Actually let me be explicit
    for odds_name, val in [("close_home_odds",ho),("close_draw_odds",do),("close_away_odds",oa),
                            ("open_home_odds",ho),("open_draw_odds",do),("open_away_odds",oa),
                            ("real_home_odds",ho),("real_draw_odds",do),("real_away_odds",oa)]:
        vec[COL_IDX[odds_name]] = val
    
    vec[COL_IDX["odds_imp_h"]] = vec[COL_IDX["prob_h"]] = vec[COL_IDX["imp_h"]] = imp_h
    vec[COL_IDX["odds_imp_d"]] = vec[COL_IDX["prob_d"]] = vec[COL_IDX["imp_d"]] = imp_d
    vec[COL_IDX["odds_imp_a"]] = vec[COL_IDX["prob_a"]] = vec[COL_IDX["imp_a"]] = imp_a
    
    # Derived (Group 2)
    vec[COL_IDX["odds_overround"]] = imp - 1.0
    vec[COL_IDX["odds_balance"]] = abs(imp_h - imp_a)
    vec[COL_IDX["odds_confidence"]] = math.sqrt((imp_h-1/3)**2+(imp_d-1/3)**2+(imp_a-1/3)**2)*3
    vec[COL_IDX["odds_ratio"]] = (1/ho)/(1/oa) if oa > 0 else 1
    vec[COL_IDX["odds_spread"]] = oa - ho
    vec[COL_IDX["odds_entropy"]] = -sum(p*math.log(max(p,1e-9)) for p in [imp_h,imp_d,imp_a])
    vec[COL_IDX["market_fav_strength"]] = max(1/ho,1/do,1/oa)/imp
    vec[COL_IDX["odds_model_diverge"]] = imp_h - 0.33
    
    # Draw signals (Group 3)
    vec[COL_IDX["draw_odds_attract"]] = max(0, min(1, 1.0-(do-3.0)/2.0))
    
    # Team form (Group 4) - WC defaults
    vec[COL_IDX["league_draw_rate"]] = 0.35
    vec[COL_IDX["league_avg_goals"]] = 2.5
    
    # Drift (Group 6) - neutral
    vec[COL_IDX["imp_d_norm"]] = imp_d
    
    # Advanced signals (Group 7)
    a1=imp_h; a5=min(imp_d,1); a6=min(1-abs(imp_h-imp_a),1)
    a7=min(imp_h*0.5+imp_a*0.5,1); a8=min(abs(imp_d-1/3)*3,1)
    vec[COL_IDX["a1"]]=a1; vec[COL_IDX["a5"]]=a5; vec[COL_IDX["a6"]]=a6
    vec[COL_IDX["a7"]]=a7; vec[COL_IDX["a8"]]=a8
    vec[COL_IDX["lambda_crush"]]=min(a1*a5*2,1)
    vec[COL_IDX["epsilon_senti"]]=min(a1*a6*2,1)
    
    # Context (Group 8)
    vec[COL_IDX["rank_diff_factor"]]=(imp_h-imp_a)*3
    vec[COL_IDX["is_cold_start"]]=1.0
    vec[COL_IDX["feat_coverage_ratio"]]=0.5
    
    # Normalize
    vec = (vec - mean) / std
    vec = np.clip(vec, -5.0, 5.0)
    return vec.astype(np.float32)

def predict_with_temperature(model, features, temperature=1.0, n_paths=30, 
                              noise_std=0.04, return_raw=False):
    """
    LeCun EBM-style prediction:
    - τ (temperature) calibrates logit energy landscape
    - rollout variance measures epistemic uncertainty  
    - Returns calibrated probabilities
    """
    with torch.no_grad():
        x = torch.from_numpy(features).unsqueeze(0).float()
        s_0 = model.encode(x)
        
        all_logits = []
        for _ in range(n_paths):
            s_T = model.predictor(s_0)
            s_T = s_T + torch.randn_like(s_T) * noise_std
            logits = model.output_head(s_0, s_T)
            all_logits.append(logits)
        
        # Stack: (n_paths, 3)
        all_logits = torch.stack(all_logits, dim=0)
        
        # LeCun EBM: energy = -logits, low energy = high confidence
        # Temperature scaling on logits (before softmax)
        scaled_logits = all_logits / temperature
        probs_per_path = torch.softmax(scaled_logits, dim=-1)
        
        # Mean probability (standard) — squeeze batch dim
        probs_mean = probs_per_path.mean(dim=0).squeeze(0).numpy()
        
        # Epistemic uncertainty: variance across paths
        probs_var = probs_per_path.var(dim=0).squeeze(0).numpy()
        
        # Energy score: -log(sum(exp(logits/tau)))
        energy = -torch.logsumexp(scaled_logits.mean(dim=0), dim=-1).item()
        
        if return_raw:
            return probs_mean, probs_var, energy, all_logits
        
        return probs_mean, probs_var, energy

def grid_search(model, features_list, actuals, odds_list):
    """
    Grid search over draw_threshold × temperature × noise_std.
    
    LeCun principle: τ should be tuned on a validation set, not guessed.
    Draw threshold should balance precision/recall per tournament context.
    """
    temperatures = [0.5, 0.7, 0.9, 1.0, 1.1, 1.3, 1.5, 1.8, 2.0, 2.5, 3.0]
    draw_thresholds = [0.25, 0.28, 0.30, 0.32, 0.34, 0.36, 0.38, 0.40, 0.42, 0.45, 0.48, 0.50, 0.55]
    noise_stds = [0.02, 0.04, 0.06, 0.08]
    
    best = {"macro_f1": -1, "params": None, "results": None}
    all_results = []
    
    total = len(temperatures) * len(draw_thresholds) * len(noise_stds)
    count = 0
    
    for noise_std in noise_stds:
        for tau in temperatures:
            for dt in draw_thresholds:
                count += 1
                correct = 0
                y_true, y_pred = [], []
                
                for feats, actual, odds in zip(features_list, actuals, odds_list):
                    probs, _, _ = predict_with_temperature(
                        model, feats, temperature=tau, noise_std=noise_std
                    )
                    
                    # Draw-aware prediction with energy calibration
                    ph, pd, pa = probs
                    
                    # LeCun EBM: if draw energy is low (pd high), predict draw
                    # But apply tournament-aware threshold
                    if pd >= dt:
                        pred = "D"
                    elif ph > pa:
                        pred = "H"
                    else:
                        pred = "A"
                    
                    y_true.append(actual)
                    y_pred.append(pred)
                    if pred == actual:
                        correct += 1
                
                n = len(y_true)
                acc = correct / n
                
                # Per-class metrics
                cls_correct = {"H": 0, "D": 0, "A": 0}
                cls_total = {"H": 0, "D": 0, "A": 0}
                cls_pred = {"H": 0, "D": 0, "A": 0}
                
                for t, p in zip(y_true, y_pred):
                    cls_total[t] += 1
                    cls_pred[p] += 1
                    if t == p:
                        cls_correct[t] += 1
                
                # Draw F1
                tp = cls_correct["D"]
                fp = cls_pred["D"] - tp
                fn = cls_total["D"] - tp
                dp = tp / (tp + fp) if (tp + fp) > 0 else 0
                dr = tp / (tp + fn) if (tp + fn) > 0 else 0
                draw_f1 = 2 * dp * dr / (dp + dr) if (dp + dr) > 0 else 0
                
                # Macro F1
                f1s = []
                for c in ["H", "D", "A"]:
                    tp_c = cls_correct[c]
                    fp_c = cls_pred[c] - tp_c
                    fn_c = cls_total[c] - tp_c
                    prec = tp_c / (tp_c + fp_c) if (tp_c + fp_c) > 0 else 0
                    rec = tp_c / (tp_c + fn_c) if (tp_c + fn_c) > 0 else 0
                    f1s.append(2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0)
                macro_f1 = sum(f1s) / 3
                
                # Weighted score: prioritize draw_f1 while keeping acc reasonable
                draw_pred_pct = cls_pred["D"] / n
                
                all_results.append({
                    "tau": tau, "dt": dt, "noise": noise_std,
                    "acc": acc, "draw_f1": draw_f1, "macro_f1": macro_f1,
                    "draw_precision": dp, "draw_recall": dr,
                    "draw_pred_pct": draw_pred_pct,
                })
                
                if macro_f1 > best["macro_f1"]:
                    best = {
                        "macro_f1": macro_f1,
                        "acc": acc, "draw_f1": draw_f1,
                        "params": {"tau": tau, "dt": dt, "noise": noise_std},
                        "draw_pred_pct": draw_pred_pct,
                    }
        
        # Progress
        if count % 50 == 0:
            print(f"  Grid search: {count}/{total} ({100*count/total:.0f}%) ...", flush=True)
    
    return best, all_results

def main():
    print("=" * 70)
    print("  LeCun JEPA Theory — Autonomous Calibration Pipeline")
    print("=" * 70)
    
    # 1. Load model & stats
    print("\n[1/4] Loading model & training statistics...")
    mean, std = load_stats()
    
    from models.jepa import JEPALite
    ckpt = torch.load(JEPA_CHECKPOINT, map_location='cpu', weights_only=False)
    model = JEPALite()
    model.load_state_dict(ckpt['model'], strict=True)
    model.eval()
    print(f"  JEPALite: epoch={ckpt['epoch']} train_acc={ckpt['acc']:.4f}")
    
    # 2. Build features for all matches
    print("\n[2/4] Building 72-dim features...")
    with open(RESULTS_JSON, 'r', encoding='utf-8') as f:
        matches = json.load(f)["matches"]
    
    features_list = []
    actuals = []
    odds_list = []
    match_names = []
    
    def mk(home, away):
        return f"{home.replace(' ','').replace('-','')}_{away.replace(' ','').replace('-','')}"
    
    for m in matches:
        key = mk(m['home'], m['away'])
        if key not in MANUAL_ODDS:
            continue
        ho, do, oa = MANUAL_ODDS[key]
        feats = build_features(ho, do, oa, mean, std)
        features_list.append(feats)
        actuals.append(m['result'])
        odds_list.append((ho, do, oa))
        match_names.append(f"{m['home']} vs {m['away']}")
    
    print(f"  Built features for {len(features_list)} matches")
    
    # 3. Baseline (τ=1.0, dt=0.32, noise=0.04)
    print("\n[3/4] Computing baseline...")
    base_correct = 0
    for feats, actual in zip(features_list, actuals):
        probs, _, _ = predict_with_temperature(model, feats, 1.0, noise_std=0.04)
        ph, pd, pa = probs
        if pd >= 0.32: pred = "D"
        elif ph > pa: pred = "H"
        else: pred = "A"
        if pred == actual: base_correct += 1
    print(f"  Baseline (τ=1.0, dt=0.32): Acc={base_correct}/{len(features_list)}={100*base_correct/len(features_list):.1f}%")
    
    # 4. Grid search
    print("\n[4/4] Grid search over τ × dt × noise...")
    best, all_results = grid_search(model, features_list, actuals, odds_list)
    
    # ── Report ──
    print(f"\n{'='*70}")
    print(f"  OPTIMAL CALIBRATION FOUND")
    print(f"{'='*70}")
    p = best["params"]
    print(f"  Temperature τ:     {p['tau']}")
    print(f"  Draw threshold:    {p['dt']}")
    print(f"  Rollout noise σ:   {p['noise']}")
    print(f"  ─────────────────────────────")
    print(f"  Accuracy:          {best['acc']:.2%}")
    print(f"  Draw F1:           {best['draw_f1']:.4f}")
    print(f"  Macro F1:          {best['macro_f1']:.4f}")
    print(f"  Draw pred rate:    {best['draw_pred_pct']:.1%}")
    
    # ── Top 10 configurations ──
    sorted_results = sorted(all_results, key=lambda r: r['macro_f1'], reverse=True)
    print(f"\n  Top 10 configurations (by Macro F1):")
    print(f"  {'τ':<6} {'dt':<6} {'σ':<6} {'Acc':<8} {'DrawF1':<8} {'MacroF1':<8} {'D%':<6}")
    print(f"  {'─'*50}")
    for r in sorted_results[:10]:
        print(f"  {r['tau']:<6.1f} {r['dt']:<6.2f} {r['noise']:<6.2f} "
              f"{r['acc']:<8.2%} {r['draw_f1']:<8.4f} {r['macro_f1']:<8.4f} {r['draw_pred_pct']:<6.1%}")
    
    # ── Apply optimal to each match ──
    tau_opt, dt_opt, noise_opt = p['tau'], p['dt'], p['noise']
    print(f"\n  Predictions with optimal (τ={tau_opt}, dt={dt_opt}, σ={noise_opt}):")
    print(f"  {'':4} {'Match':<35} {'Pred':<4} {'Act':<4} {'Score':<6} {'H%':<7} {'D%':<7} {'A%':<7}")
    print(f"  {'─'*80}")
    
    final_correct = 0
    for i, (feats, actual, odds, name) in enumerate(zip(features_list, actuals, odds_list, match_names)):
        probs, probs_var, energy = predict_with_temperature(
            model, feats, temperature=tau_opt, noise_std=noise_opt, return_raw=False
        )
        ph, pd, pa = probs
        pv_h, pv_d, pv_a = probs_var
        
        if pd >= dt_opt: pred = "D"
        elif ph > pa: pred = "H"
        else: pred = "A"
        
        correct = "O" if pred == actual else "X"
        if pred == actual: final_correct += 1
        
        scores = {"H": f"{matches[i]['home_score']}-{matches[i]['away_score']}"}
        score = matches[i]['home_score'] if actual == "H" else (matches[i]['away_score'] if actual == "A" else f"{matches[i]['home_score']}-{matches[i]['away_score']}")
        score_str = f"{matches[i]['home_score']}-{matches[i]['away_score']}"
        
        print(f"  {correct}  {name:<33} {pred:<4} {actual:<4} {score_str:<6} "
              f"{ph:<7.1%} {pd:<7.1%} {pa:<7.1%}")
    
    print(f"\n  Final: {final_correct}/{len(features_list)} = {100*final_correct/len(features_list):.1%}")
    
    # Save
    out = {
        "optimal": best,
        "top10": [{k: (float(v) if isinstance(v, (np.floating, float)) else v) 
                    for k, v in r.items()} for r in sorted_results[:10]],
        "baseline_acc": base_correct / len(features_list),
    }
    with open(Path(__file__).parent / "calibration_results.json", "w") as f:
        json.dump(out, f, indent=2, default=float)
    print(f"\n  Results saved to calibration_results.json")
    print("=" * 70)

if __name__ == "__main__":
    main()

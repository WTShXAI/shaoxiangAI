"""
JEPA v5.0 世界杯验证 — 完整72维特征版
========================================
从JEPA训练数据提取特征统计，为世界杯比赛构建完整72维特征向量。
将"仅赔率"验证升级为"近似全特征"验证。
"""
import sys, os, json, re, time, math
from pathlib import Path
from collections import Counter
import numpy as np

ROOT = str(Path(__file__).resolve().parent.parent)

import torch

# ── Config ──
JEPA_TRAIN_NPZ = os.path.join(ROOT, "data/jepa_train.npz")
JEPA_CHECKPOINT = os.path.join(ROOT, "models/jepa/checkpoints/best_model_lite.pt")
RESULTS_JSON = Path(__file__).parent / "wc2026_results.json"

# STATIC_72_COLS from jepa_pipeline.py
STATIC_72_COLS = [
    # Group 1: Core Odds (18)
    "close_home_odds", "close_draw_odds", "close_away_odds",
    "open_home_odds", "open_draw_odds", "open_away_odds",
    "real_home_odds", "real_draw_odds", "real_away_odds",
    "odds_imp_h", "odds_imp_d", "odds_imp_a",
    "prob_h", "prob_d", "prob_a",
    "imp_h", "imp_d", "imp_a",
    # Group 2: Odds Derived (14)
    "odds_overround", "odds_balance", "odds_confidence",
    "odds_ratio", "odds_spread", "odds_entropy",
    "odds_move_h", "odds_move_d", "odds_move_a",
    "odds_move_magnitude", "odds_fav_move",
    "market_fav_strength", "market_disagreement",
    "odds_model_diverge",
    # Group 3: Draw / Market Signals (2)
    "draw_odds_attract", "draw_with_ht_draw",
    # Group 4: Team Form (8)
    "home_points_avg_10", "home_points_avg_5", "home_win_avg_10",
    "away_points_avg_10",
    "h_team_draw_rate", "a_team_draw_rate",
    "league_draw_rate", "league_avg_goals",
    # Group 5: HT-Specific (8)
    "ht_draw_composite", "ht_draw_prob", "ht_00_prob",
    "ht_goal_pressure", "ht_h_lead_prob", "ht_scoring_diff",
    "exp_ht_goals", "exp_total_goals",
    # Group 6: Drift Dynamics (7)
    "drift_h", "drift_d", "drift_a",
    "drift_h_val", "drift_a_val", "drift_divergence",
    "imp_d_norm",
    # Group 7: Advanced Signals (8)
    "a1", "a5", "a6", "a7", "a8",
    "sigma_trap", "lambda_crush", "epsilon_senti",
    # Group 8: Context (7)
    "rank_diff_factor", "form_momentum", "h2h_factor",
    "rank_factor", "form_factor",
    "is_cold_start", "feat_coverage_ratio",
]
assert len(STATIC_72_COLS) == 72, f"Expected 72, got {len(STATIC_72_COLS)}"

# Column index map
COL_IDX = {name: i for i, name in enumerate(STATIC_72_COLS)}

def load_jepa_stats():
    """Load mean/std for each of 72 features from JEPA training data."""
    data = np.load(JEPA_TRAIN_NPZ, allow_pickle=True)
    static = data["static"]  # (N, 72)
    print(f"JEPA training data: {static.shape[0]} samples, {static.shape[1]} features")
    
    mean = static.mean(axis=0).astype(np.float32)
    std = static.std(axis=0).astype(np.float32)
    std[std < 1e-8] = 1.0  # Avoid div by zero
    
    # Also store median for some features
    median = np.median(static, axis=0).astype(np.float32)
    
    return mean, std, median

def build_full_features(ho: float, do: float, oa: float, 
                         train_mean: np.ndarray, train_std: np.ndarray,
                         train_median: np.ndarray) -> np.ndarray:
    """
    Build 72-dim feature vector for a match.
    
    Strategy:
    - Odds-derived features: compute from ho/do/ao
    - Team/league-specific features: use training mean (neutral assumption)
    - Advanced signals (A1-A8, sigma_trap, etc.): compute from odds
    """
    vec = train_mean.copy().astype(np.float32)  # Start with means
    
    imp = 1/ho + 1/do + 1/oa
    imp_h = (1/ho) / imp
    imp_d = (1/do) / imp
    imp_a = (1/oa) / imp
    
    # ── Group 1: Core Odds ──
    # Close odds = current odds
    vec[COL_IDX["close_home_odds"]] = ho
    vec[COL_IDX["close_draw_odds"]] = do
    vec[COL_IDX["close_away_odds"]] = oa
    # Open odds: assume same as close (no historical open data)
    vec[COL_IDX["open_home_odds"]] = ho
    vec[COL_IDX["open_draw_odds"]] = do
    vec[COL_IDX["open_away_odds"]] = oa
    # Real odds = current odds
    vec[COL_IDX["real_home_odds"]] = ho
    vec[COL_IDX["real_draw_odds"]] = do
    vec[COL_IDX["real_away_odds"]] = oa
    # Implied probabilities
    vec[COL_IDX["odds_imp_h"]] = imp_h
    vec[COL_IDX["odds_imp_d"]] = imp_d
    vec[COL_IDX["odds_imp_a"]] = imp_a
    # prob_h/d/a = implied probabilities (same)
    vec[COL_IDX["prob_h"]] = imp_h
    vec[COL_IDX["prob_d"]] = imp_d
    vec[COL_IDX["prob_a"]] = imp_a
    # imp_h/d/a = implied probabilities (same)
    vec[COL_IDX["imp_h"]] = imp_h
    vec[COL_IDX["imp_d"]] = imp_d
    vec[COL_IDX["imp_a"]] = imp_a
    
    # ── Group 2: Odds Derived ──
    vec[COL_IDX["odds_overround"]] = imp - 1.0
    vec[COL_IDX["odds_balance"]] = abs(imp_h - imp_a)
    odds_conf = math.sqrt((imp_h - 1/3)**2 + (imp_d - 1/3)**2 + (imp_a - 1/3)**2) * 3.0
    vec[COL_IDX["odds_confidence"]] = odds_conf
    vec[COL_IDX["odds_ratio"]] = (1/ho) / (1/oa) if oa > 0 else 1.0
    vec[COL_IDX["odds_spread"]] = oa - ho
    entropy = -sum(p * math.log(max(p, 1e-9)) for p in [imp_h, imp_d, imp_a])
    vec[COL_IDX["odds_entropy"]] = entropy
    # Odds movement: assume minimal (no historical data)
    vec[COL_IDX["odds_move_h"]] = 0.0
    vec[COL_IDX["odds_move_d"]] = 0.0
    vec[COL_IDX["odds_move_a"]] = 0.0
    vec[COL_IDX["odds_move_magnitude"]] = 0.0
    vec[COL_IDX["odds_fav_move"]] = 0.0
    vec[COL_IDX["market_fav_strength"]] = max(1/ho, 1/do, 1/oa) / imp
    vec[COL_IDX["market_disagreement"]] = 0.0
    vec[COL_IDX["odds_model_diverge"]] = imp_h - 0.33
    
    # ── Group 3: Draw / Market Signals ──
    vec[COL_IDX["draw_odds_attract"]] = max(0, min(1, 1.0 - (do - 3.0) / 2.0))
    # draw_with_ht_draw: use mean (requires HT data)
    
    # ── Group 4: Team Form ──
    # These require team history. Use World Cup-level defaults.
    # World Cup: higher draw rate (~35%), fewer goals (~2.5)
    vec[COL_IDX["league_draw_rate"]] = 0.35  # WC typical draw rate
    vec[COL_IDX["league_avg_goals"]] = 2.5   # WC typical goals
    
    # ── Group 5: HT-Specific ──
    # Not available pre-match, use training mean
    
    # ── Group 6: Drift Dynamics ──
    # No drift data, use neutral values
    vec[COL_IDX["drift_h"]] = 0.0
    vec[COL_IDX["drift_d"]] = 0.0
    vec[COL_IDX["drift_a"]] = 0.0
    vec[COL_IDX["drift_h_val"]] = 0.0
    vec[COL_IDX["drift_a_val"]] = 0.0
    vec[COL_IDX["drift_divergence"]] = 0.0
    vec[COL_IDX["imp_d_norm"]] = imp_d
    
    # ── Group 7: Advanced Signals ──
    a1 = imp_h
    a5 = min(imp_d, 1)
    a6 = min(1 - abs(imp_h - imp_a), 1)
    a7 = min(imp_h * 0.5 + imp_a * 0.5, 1)
    a8 = min(abs(imp_d - 1/3) * 3, 1)
    sigma_trap = 0.0  # No drift = no trap
    lambda_crush = min(a1 * a5 * 2, 1) if min(ho, do, oa) == ho else 0.5
    epsilon_senti = min(a1 * a6 * 2, 1)
    
    vec[COL_IDX["a1"]] = a1
    vec[COL_IDX["a5"]] = a5
    vec[COL_IDX["a6"]] = a6
    vec[COL_IDX["a7"]] = a7
    vec[COL_IDX["a8"]] = a8
    vec[COL_IDX["sigma_trap"]] = sigma_trap
    vec[COL_IDX["lambda_crush"]] = lambda_crush
    vec[COL_IDX["epsilon_senti"]] = epsilon_senti
    
    # ── Group 8: Context ──
    vec[COL_IDX["rank_diff_factor"]] = (imp_h - imp_a) * 3
    vec[COL_IDX["is_cold_start"]] = 1.0  # World cup = cold start
    vec[COL_IDX["feat_coverage_ratio"]] = 0.5
    
    # ── Normalize ──
    vec_norm = (vec - train_mean) / train_std
    # Clip outliers to ±5 sigma
    vec_norm = np.clip(vec_norm, -5.0, 5.0)
    
    return vec_norm.astype(np.float32)

def load_jepa_model():
    """Load JEPALite with trained weights."""
    from models.jepa import JEPALite
    
    ckpt = torch.load(JEPA_CHECKPOINT, map_location='cpu', weights_only=False)
    model = JEPALite()
    model.load_state_dict(ckpt['model'], strict=True)
    model.eval()
    print(f"JEPALite loaded: epoch={ckpt['epoch']} acc={ckpt['acc']:.4f}")
    return model

def predict(model, features: np.ndarray) -> dict:
    """Run JEPA prediction with 30-path MC rollout."""
    with torch.no_grad():
        x = torch.from_numpy(features).unsqueeze(0).float()
        probs = model.predict_proba(x, n_paths=30).numpy()[0]
    
    labels = ["home", "draw", "away"]
    pred = labels[int(np.argmax(probs))]
    return {
        "probabilities": {"H": float(probs[0]), "D": float(probs[1]), "A": float(probs[2])},
        "prediction": pred,
        "confidence": float(probs.max()),
    }

def main():
    print("=" * 65)
    print("  JEPA v5.0 — WORLD CUP 2026 FULL FEATURE VALIDATION")
    print("=" * 65)
    
    # 1. Load training stats
    train_mean, train_std, train_median = load_jepa_stats()
    
    # 2. Load model
    model = load_jepa_model()
    
    # 3. Load match results
    with open(RESULTS_JSON, "r", encoding="utf-8") as f:
        matches = json.load(f)["matches"]
    
    # 4. Manually verified odds from OCR
    manual_odds = {
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
    
    def match_key(home, away):
        return f"{home.replace(' ','').replace('-','')}_{away.replace(' ','').replace('-','')}"
    
    results = []
    print(f"\nPredicting {len(manual_odds)} matches with full 72-dim features...\n")
    
    for m in matches:
        key = match_key(m['home'], m['away'])
        if key not in manual_odds:
            continue
        
        ho, do, ao = manual_odds[key]
        
        # Build full features
        features = build_full_features(ho, do, ao, train_mean, train_std, train_median)
        
        # Predict
        pred = predict(model, features)
        pred_label = {"home": "H", "draw": "D", "away": "A"}[pred["prediction"]]
        actual = m['result']
        correct = "O" if pred_label == actual else "X"
        
        probs = pred["probabilities"]
        
        results.append({
            "home": m['home'], "away": m['away'], "date": m['date'],
            "odds": {"home": ho, "draw": do, "away": ao},
            "prediction": pred_label, "actual": actual,
            "correct": pred_label == actual,
            "probabilities": probs,
            "score": f"{m['home_score']}-{m['away_score']}"
        })
        
        feature_nonzero = (np.abs(features) > 0.01).sum()
        print(f"  {correct} {m['home']:>12s} vs {m['away']:<12s} "
              f"pred={pred_label} act={actual} ({m['home_score']}-{m['away_score']})  "
              f"H={probs['H']:.1%} D={probs['D']:.1%} A={probs['A']:.1%}  "
              f"[{feature_nonzero}/72 nonzero]")
    
    # ── Stats ──
    n = len(results)
    if n == 0:
        print("\nNo results!")
        return
    
    correct_count = sum(1 for r in results if r["correct"])
    acc = correct_count / n
    
    class_correct = {"H": 0, "D": 0, "A": 0}
    class_total = {"H": 0, "D": 0, "A": 0}
    conf_matrix = {"H": {"H": 0, "D": 0, "A": 0}, "D": {"H": 0, "D": 0, "A": 0}, "A": {"H": 0, "D": 0, "A": 0}}
    
    for r in results:
        class_total[r["actual"]] += 1
        conf_matrix[r["actual"]][r["prediction"]] += 1
        if r["correct"]:
            class_correct[r["actual"]] += 1
    
    actual_dist = Counter(r["actual"] for r in results)
    
    # Draw F1
    tp = class_correct["D"]
    fp = sum(1 for r in results if r["prediction"] == "D" and r["actual"] != "D")
    fn = class_total["D"] - tp
    dp = tp / (tp + fp) if (tp + fp) > 0 else 0
    dr = tp / (tp + fn) if (tp + fn) > 0 else 0
    draw_f1 = 2 * dp * dr / (dp + dr) if (dp + dr) > 0 else 0
    
    # Macro F1
    f1s = []
    for cls in ["H", "D", "A"]:
        tp_c = class_correct[cls]
        fp_c = sum(1 for r in results if r["prediction"] == cls and r["actual"] != cls)
        fn_c = class_total[cls] - tp_c
        prec = tp_c / (tp_c + fp_c) if (tp_c + fp_c) > 0 else 0
        rec = tp_c / (tp_c + fn_c) if (tp_c + fn_c) > 0 else 0
        f1s.append(2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0)
    macro_f1 = sum(f1s) / 3
    
    print(f"\n{'='*65}")
    print(f"  JEPA v5.0 — WORLD CUP 2026 FINAL REPORT (72-dim features)")
    print(f"{'='*65}")
    print(f"  Matches:      {n}")
    print(f"  Accuracy:     {acc:.2%} ({correct_count}/{n})")
    print(f"  Draw F1:      {draw_f1:.4f}  (P={dp:.3f} R={dr:.3f})")
    print(f"  Macro F1:     {macro_f1:.4f}")
    print(f"  Distribution: H={actual_dist.get('H',0)} D={actual_dist.get('D',0)} A={actual_dist.get('A',0)}")
    print(f"  Draw rate:    {actual_dist.get('D',0)/n:.1%}")
    print()
    for cls in ["H", "D", "A"]:
        acc_c = class_correct[cls] / class_total[cls] if class_total[cls] > 0 else 0
        p_d = sum(1 for r in results if r["prediction"] == cls)
        print(f"  {cls}: Acc={acc_c:.2%} ({class_correct[cls]}/{class_total[cls]})  Predicted={p_d}")
    
    print(f"\n  Confusion Matrix:")
    print(f"         pred_H pred_D pred_A")
    for cls in ["H", "D", "A"]:
        cm = conf_matrix[cls]
        print(f"  act_{cls}:  {cm['H']:6d} {cm['D']:6d} {cm['A']:6d}")
    
    # ── Compare with odds-only ──
    print(f"\n{'─'*65}")
    print(f"  COMPARISON: odds-only vs full features")
    print(f"  {'Metric':<15} {'Odds-only':<12} {'Full 72-dim':<12} {'Change':<10}")
    print(f"  {'─'*50}")
    print(f"  {'Accuracy':<15} {'14.29%':<12} {acc:.2%}  ")
    print(f"  {'Draw F1':<15} {'0.0000':<12} {draw_f1:.4f}  ")
    print(f"  {'Macro F1':<15} {'—':<12} {macro_f1:.4f}  ")
    print(f"{'='*65}")
    
    # Save
    out = Path(__file__).parent / "wc2026_full_feature_results.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "model": "JEPALite (epoch=12, acc=56.5%)",
            "features": "72-dim from JEPA training data stats + odds computation",
            "summary": {
                "total": n, "accuracy": acc, "draw_f1": draw_f1,
                "macro_f1": macro_f1,
                "actual_distribution": dict(actual_dist),
            },
            "confusion_matrix": conf_matrix,
            "results": results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  Full results: {out}")

if __name__ == "__main__":
    main()

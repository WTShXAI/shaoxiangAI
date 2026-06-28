"""
快速重算: 用修复后的JEPA模型重新预测已OCR的赔率
"""
import sys, os, json
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)

import torch, numpy as np
from collections import Counter

RESULTS_JSON = Path(__file__).parent / "wc2026_results.json"

# Load model once
from models.jepa import JEPALite
ckpt = torch.load(
    os.path.join(ROOT, 'models/jepa/checkpoints/best_model_lite.pt'),
    map_location='cpu', weights_only=False
)
model = JEPALite()
model.load_state_dict(ckpt['model'], strict=True)
model.eval()
print(f"Model: JEPALite epoch={ckpt['epoch']} acc={ckpt['acc']:.4f}")

# Load match results
with open(RESULTS_JSON, 'r', encoding='utf-8') as f:
    matches = json.load(f)["matches"]

# Manually entered odds from first OCR run (verified correct)
# Format: (home, draw, away) odds
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

# Build key → match mapping
def match_key(home, away):
    # Normalize
    home = home.replace(' ', '').replace('-', '')
    away = away.replace(' ', '').replace('-', '')
    return f"{home}_{away}"

results = []

with torch.no_grad():
    for m in matches:
        key = match_key(m['home'], m['away'])
        if key not in manual_odds:
            continue  # OCR failed matches
        
        ho, do, ao = manual_odds[key]
        imp = 1/ho + 1/do + 1/ao
        
        # Build static features (same as v5_server: 72-dim with odds in first 3)
        static = np.zeros(72, dtype=np.float32)
        static[0:3] = [ho/20.0, do/20.0, ao/20.0]
        static[3:6] = [(1/ho)/imp, (1/do)/imp, (1/ao)/imp]
        static[6] = imp
        static[7] = 1/ho - 1/ao
        
        x = torch.from_numpy(static).unsqueeze(0).float()
        probs = model.predict_proba(x, n_paths=30).numpy()[0]
        
        labels = ["H", "D", "A"]
        pred_label = labels[int(np.argmax(probs))]
        actual = m['result']
        
        results.append({
            "home": m['home'], "away": m['away'], "date": m['date'],
            "odds": {"home": ho, "draw": do, "away": ao},
            "prediction": pred_label, "actual": actual,
            "correct": pred_label == actual,
            "probabilities": {"H": float(probs[0]), "D": float(probs[1]), "A": float(probs[2])},
            "score": f"{m['home_score']}-{m['away_score']}"
        })

# Stats
n = len(results)
correct = sum(1 for r in results if r['correct'])
acc = correct / n

class_correct = {"H": 0, "D": 0, "A": 0}
class_total = {"H": 0, "D": 0, "A": 0}
conf_matrix = {"H": {"H": 0, "D": 0, "A": 0}, "D": {"H": 0, "D": 0, "A": 0}, "A": {"H": 0, "D": 0, "A": 0}}

for r in results:
    class_total[r['actual']] += 1
    conf_matrix[r['actual']][r['prediction']] += 1
    if r['correct']:
        class_correct[r['actual']] += 1

actual_dist = Counter(r['actual'] for r in results)

# Draw F1
tp = class_correct["D"]
fp = sum(1 for r in results if r['prediction'] == "D" and r['actual'] != "D")
fn = class_total["D"] - tp
dp = tp / (tp + fp) if (tp + fp) > 0 else 0
dr = tp / (tp + fn) if (tp + fn) > 0 else 0
draw_f1 = 2 * dp * dr / (dp + dr) if (dp + dr) > 0 else 0

# Report
print(f"\n{'='*60}")
print(f"  JEPA v5.0 WORLD CUP 2026 FINAL REPORT")
print(f"{'='*60}")
print(f"  Matches:      {n}")
print(f"  Accuracy:     {acc:.2%} ({correct}/{n})")
print(f"  Draw F1:      {draw_f1:.4f} (P={dp:.3f} R={dr:.3f})")
print(f"  Distribution: H={actual_dist.get('H',0)} D={actual_dist.get('D',0)} A={actual_dist.get('A',0)}")
print(f"  Draw rate:    {actual_dist.get('D',0)/n:.1%}")
print()
for cls in ["H", "D", "A"]:
    acc_c = class_correct[cls] / class_total[cls] if class_total[cls] > 0 else 0
    print(f"  {cls}-Acc:    {acc_c:.2%} ({class_correct[cls]}/{class_total[cls]})")
print()
print("  Confusion Matrix:")
print(f"         pred_H pred_D pred_A")
for cls in ["H", "D", "A"]:
    cm = conf_matrix[cls]
    print(f"  act_{cls}:  {cm['H']:6d} {cm['D']:6d} {cm['A']:6d}")
print()
for r in results:
    mark = "O" if r['correct'] else "X"
    probs = r['probabilities']
    print(f"  {mark} {r['home']:>12s} vs {r['away']:<12s} pred={r['prediction']} act={r['actual']} ({r['score']})  "
          f"H={probs['H']:.1%} D={probs['D']:.1%} A={probs['A']:.1%}")
print(f"{'='*60}")

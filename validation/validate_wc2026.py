"""
World Cup 2026 JEPA v5.0 验证脚本
===============================
OCR提取赔率 → JEPA v5预测 → 赛果对比 → 准确率统计
"""
import sys, os, json, re, time, base64
from pathlib import Path
from collections import Counter
import requests

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OCR_URL = "http://localhost:8000/api/v1/ocr/upload"
PREDICT_URL = "http://localhost:8000/api/v1/v5/predict"
RESULTS_JSON = Path(__file__).parent / "wc2026_results.json"
TIMEOUT = 30


def load_results() -> list:
    with open(RESULTS_JSON, "r", encoding="utf-8") as f:
        return json.load(f)["matches"]


def ocr_screenshot(filepath: str) -> str:
    """Send screenshot to FootballAI OCR endpoint, return text"""
    with open(filepath, "rb") as f:
        files = {"file": (os.path.basename(filepath), f, "image/png")}
        resp = requests.post(OCR_URL, files=files, timeout=TIMEOUT)
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"OCR failed: {data}")
    return data["text"]


def parse_odds(text: str) -> dict:
    """Extract 1X2 odds from OCR text using multi-pattern matching"""
    # Clean
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text).strip()

    # Pattern 1: Interwetten label
    m = re.search(r"[Ii]nterwetten\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)", text)
    if m:
        return {"home": float(m.group(1)), "draw": float(m.group(2)), "away": float(m.group(3))}

    # Pattern 2: Three consecutive decimal odds with >=2 digits
    odds_matches = re.findall(r"(?<!\d)(\d+\.\d{2,3})(?!\d)", text)
    if len(odds_matches) >= 3:
        # Find the first triplet of 1X2-like odds (all > 1.0)
        for i in range(len(odds_matches) - 2):
            h, d, a = float(odds_matches[i]), float(odds_matches[i+1]), float(odds_matches[i+2])
            if 1.01 < h < 99 and 1.01 < d < 99 and 1.01 < a < 99:
                return {"home": h, "draw": d, "away": a}

    # Pattern 3: Match-specific odds (label-based)
    m = re.search(r"(?:胜|主|Home|1)[^\d]*?(\d+\.\d+)\s+(?:平|和|Draw|X)[^\d]*?(\d+\.\d+)\s+(?:负|客|Away|2)[^\d]*?(\d+\.\d+)", text)
    if m:
        return {"home": float(m.group(1)), "draw": float(m.group(2)), "away": float(m.group(3))}

    # Pattern 4: Loose - any 3 floats in sequence after vs/VS
    m = re.search(r"(?:vs\.?|VS\.?)\s*\S*\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)", text)
    if m:
        return {"home": float(m.group(1)), "draw": float(m.group(2)), "away": float(m.group(3))}

    return None


def predict_jepa(odds: dict) -> dict:
    """Call JEPA v5 API"""
    resp = requests.post(PREDICT_URL, json={
        "home_odds": odds["home"],
        "draw_odds": odds["draw"],
        "away_odds": odds["away"]
    }, timeout=TIMEOUT)
    return resp.json()


def result_to_label(result: str) -> str:
    return {"H": "home", "D": "draw", "A": "away"}[result]


def jepa_prediction(pred: dict) -> str:
    """Convert JEPA prediction to H/D/A label"""
    probs = pred.get("probabilities", {})
    if not probs:
        pred_label = pred.get("prediction", "")
        return {"home": "H", "draw": "D", "away": "A"}.get(pred_label, "?")
    # Argmax
    max_label = max(probs, key=probs.get)
    return {"H": "H", "home": "H", "D": "D", "draw": "D", "A": "A", "away": "A"}.get(max_label, "?")


def main():
    matches = load_results()
    print(f"Loaded {len(matches)} matches for validation\n")

    results = []
    ocr_failures = 0
    predict_failures = 0

    for i, m in enumerate(matches):
        fname = os.path.basename(m["screenshot"])
        print(f"[{i+1}/{len(matches)}] {m['home']} vs {m['away']} ({m['date']})")

        # OCR
        try:
            ocr_text = ocr_screenshot(m["screenshot"])
            odds = parse_odds(ocr_text)
        except Exception as e:
            print(f"  OCR FAILED: {e}")
            ocr_failures += 1
            continue

        if not odds:
            print(f"  PARSE FAILED: Could not extract odds from OCR text")
            print(f"  OCR preview: {ocr_text[:200]}...")
            ocr_failures += 1
            continue

        print(f"  Odds: H={odds['home']:.2f} D={odds['draw']:.2f} A={odds['away']:.2f}")

        # Predict
        try:
            pred = predict_jepa(odds)
        except Exception as e:
            print(f"  PREDICT FAILED: {e}")
            predict_failures += 1
            continue

        pred_label = jepa_prediction(pred)
        probs = pred.get("probabilities", {})
        actual = m["result"]
        correct = "✓" if pred_label == actual else "✗"

        results.append({
            "home": m["home"],
            "away": m["away"],
            "date": m["date"],
            "odds": odds,
            "prediction": pred_label,
            "actual": actual,
            "correct": pred_label == actual,
            "probabilities": probs,
            "score": f"{m['home_score']}-{m['away_score']}"
        })

        prob_str = f"H={probs.get('H',0):.1%} D={probs.get('D',0):.1%} A={probs.get('A',0):.1%}"
        print(f"  Pred: {pred_label} | Actual: {actual} ({m['home_score']}-{m['away_score']}) {correct}")
        print(f"  Probs: {prob_str}")
        print()

        time.sleep(0.5)  # Rate limit

    # ── Statistics ──
    n = len(results)
    if n == 0:
        print("No valid results to analyze!")
        return

    correct = sum(1 for r in results if r["correct"])
    acc = correct / n

    # Per-class
    y_true = [r["actual"] for r in results]
    y_pred = [r["prediction"] for r in results]

    # Count actual distribution
    actual_dist = Counter(y_true)

    # Per-class accuracy
    class_correct = {"H": 0, "D": 0, "A": 0}
    class_total = {"H": 0, "D": 0, "A": 0}
    for r in results:
        class_total[r["actual"]] += 1
        if r["correct"]:
            class_correct[r["actual"]] += 1

    # Draw F1
    tp = class_correct["D"]
    fp = sum(1 for r in results if r["prediction"] == "D" and not r["correct"])
    fn = class_total["D"] - tp
    draw_precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    draw_recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    draw_f1 = 2 * draw_precision * draw_recall / (draw_precision + draw_recall) if (draw_precision + draw_recall) > 0 else 0

    # Macro F1
    f1_scores = []
    for cls in ["H", "D", "A"]:
        tp_c = class_correct[cls]
        fp_c = sum(1 for r in results if r["prediction"] == cls and r["actual"] != cls)
        fn_c = class_total[cls] - tp_c
        prec = tp_c / (tp_c + fp_c) if (tp_c + fp_c) > 0 else 0
        rec = tp_c / (tp_c + fn_c) if (tp_c + fn_c) > 0 else 0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
        f1_scores.append(f1)
    macro_f1 = sum(f1_scores) / 3

    # Print report
    print("=" * 70)
    print("  JEPA v5.0 WORLD CUP 2026 VALIDATION REPORT")
    print("=" * 70)
    print(f"  Matches validated:  {n}")
    print(f"  OCR failures:       {ocr_failures}")
    print(f"  Predict failures:   {predict_failures}")
    print(f"  Accuracy:           {acc:.2%} ({correct}/{n})")
    print(f"  Draw F1:            {draw_f1:.4f}")
    print(f"  Macro F1:           {macro_f1:.4f}")
    print()
    print(f"  Actual distribution:  H={actual_dist.get('H',0)} D={actual_dist.get('D',0)} A={actual_dist.get('A',0)}")
    print(f"  Draw rate:           {actual_dist.get('D',0)/n:.1%}")
    print()
    for cls in ["H", "D", "A"]:
        acc_cls = class_correct[cls] / class_total[cls] if class_total[cls] > 0 else 0
        print(f"  {cls}-Acc: {acc_cls:.2%} ({class_correct[cls]}/{class_total[cls]})")

    print()
    print("  Per-match details:")
    for r in results:
        mark = "✓" if r["correct"] else "✗"
        print(f"  {mark} {r['date']} {r['home']} vs {r['away']}: pred={r['prediction']} actual={r['actual']} ({r['score']})")

    # Save detailed results
    out_file = Path(__file__).parent / "wc2026_validation_output.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({
            "summary": {
                "total": n,
                "accuracy": acc,
                "draw_f1": draw_f1,
                "macro_f1": macro_f1,
                "ocr_failures": ocr_failures,
                "predict_failures": predict_failures,
                "actual_distribution": dict(actual_dist)
            },
            "results": results
        }, f, ensure_ascii=False, indent=2)

    print(f"\n  Full results saved to: {out_file}")
    print("=" * 70)


if __name__ == "__main__":
    main()

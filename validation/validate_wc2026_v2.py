"""
World Cup 2026 JEPA v5.0 验证脚本 (Standalone)
================================================
直接调用火山OCR + 本地JEPA模型，避免服务器导入问题
"""
import sys, os, json, re, time, base64, hashlib, hmac
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlencode
import numpy as np

# Add project root
ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)

# ── OCR config (from api/ocr.py) ──
OCR_AK = "AKLTN2FkMmY5NmNlZDVkNDNjZTgwMTFiNjBkNWY2ZTk1MjA"
OCR_SK = "T0RJMllqZGlOakk1TW1Gak5HTmlaV0l5T0RjelptTmxZbVJsTW1Fek16SQ=="
OCR_HOST = "visual.volcengineapi.com"
OCR_REGION = "cn-north-1"
OCR_SERVICE = "cv"

RESULTS_JSON = Path(__file__).parent / "wc2026_results.json"


def ocr_sign(method, body_str):
    """HMAC-SHA256 signing"""
    now = datetime.now(timezone.utc)
    date = now.strftime("%Y%m%d")
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    payload_hash = hashlib.sha256(body_str.encode()).hexdigest()
    canonical_headers = f"content-type:application/x-www-form-urlencoded\nhost:{OCR_HOST}\n"
    signed_headers = "content-type;host"
    canonical_request = f"{method}\n/\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    scope = f"{date}/{OCR_REGION}/{OCR_SERVICE}/request"
    string_to_sign = f"HMAC-SHA256\n{ts}\n{scope}\n{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    sk = OCR_SK
    k_date = hmac.new(sk.encode(), date.encode(), hashlib.sha256).digest()
    k_region = hmac.new(k_date, OCR_REGION.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, OCR_SERVICE.encode(), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"request", hashlib.sha256).digest()
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Authorization": f"HMAC-SHA256 Credential={OCR_AK}/{scope}, SignedHeaders={signed_headers}, Signature={signature}",
        "X-Date": ts,
    }


def ocr_screenshot(filepath: str) -> str:
    """Direct Volcengine OCR call"""
    import httpx
    with open(filepath, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()
    body_data = {
        "Action": "OCRNormal",
        "Version": "2020-08-26",
        "image_base64": img_b64,
        "detect_layout": "true",
        "sort_page": "true",
    }
    body_str = urlencode(body_data)
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Host": OCR_HOST}
    headers.update(ocr_sign("POST", body_str))
    resp = httpx.post(f"https://{OCR_HOST}/", data=body_str, headers=headers, timeout=20.0)
    data = resp.json()
    err = data.get("ResponseMetadata", {}).get("Error", {})
    if err and err.get("Code") != "NoError":
        raise RuntimeError(f"OCR API error: {err.get('Message', 'unknown')}")
    chars_2d = data.get("data", {}).get("chars", [])
    if chars_2d:
        texts = ["".join(c.get("char", "") if isinstance(c, dict) else str(c) for c in (line if isinstance(line, list) else [])) for line in chars_2d]
        return "\n".join(texts)
    blocks = data.get("Result", {}).get("TextBlocks", [])
    return "\n".join(b.get("Text", "") for b in blocks) if blocks else ""


def parse_odds(text: str) -> dict:
    """
    从OCR文本中提取1X2赔率
    Interwetten截图格式: 主场队名...主X.XX...平X.XX...客场队名...客X.XX
    """
    # 合并为单行便于正则
    flat = re.sub(r'\s+', ' ', text)
    
    # Strategy 1: 找"全场独赢"行附近的赔率
    # 格式: ...主 X.XX ... 平 X.XX ... 客 X.XX
    m = re.search(r'主\s*(\d+\.\d{2,3})', flat)
    m_d = re.search(r'平\s*(\d+\.\d{2,3})', flat)
    m_a = re.search(r'客\s*(\d+\.\d{2,3})', flat)
    if m and m_d and m_a:
        return {"home": float(m.group(1)), "draw": float(m_d.group(1)), "away": float(m_a.group(1))}
    
    # Strategy 2: Interwetten pattern
    m = re.search(r'[Ii]nterwetten.*?(\d+\.\d{2,3})\s+(\d+\.\d{2,3})\s+(\d+\.\d{2,3})', flat)
    if m:
        return {"home": float(m.group(1)), "draw": float(m.group(2)), "away": float(m.group(3))}
    
    # Strategy 3: Three consecutive decimal odds
    odds = re.findall(r'(?<!\d)(\d+\.\d{2,3})(?!\d)', flat)
    # Filter by realistic odds range and find best triplet near 1X2 pattern
    for i in range(len(odds) - 2):
        h, d, a = float(odds[i]), float(odds[i+1]), float(odds[i+2])
        if 1.05 < h < 30 and 1.5 < d < 30 and 1.05 < a < 30:
            # Check if imp is reasonable
            imp = 1/h + 1/d + 1/a
            if 0.85 < imp < 1.25:
                return {"home": h, "draw": d, "away": a}
    
    return None


def predict_jepa(odds: dict) -> dict:
    """Local JEPA model prediction"""
    from models.jepa import JEPALite
    import torch
    
    device = torch.device('cpu')
    model = JEPALite(static_dim=72, embed_dim=128)
    # Load checkpoint (training format: {'model': state_dict, 'acc': ..., 'epoch': ...})
    ckpt = torch.load(
        os.path.join(ROOT, 'models/jepa/checkpoints/best_model_lite.pt'),
        map_location='cpu', weights_only=False
    )
    
    if 'model' in ckpt and isinstance(ckpt['model'], dict):
        state = ckpt['model']
    elif 'model_state_dict' in ckpt:
        state = ckpt['model_state_dict']
    elif 'state_dict' in ckpt:
        state = ckpt['state_dict']
    else:
        state = ckpt
    
    state = {k.replace('module.', ''): v for k, v in state.items()}
    model.load_state_dict(state, strict=True)
    model.eval()
    
    ho, do, ao = odds["home"], odds["draw"], odds["away"]
    imp = 1/ho + 1/do + 1/ao
    
    # Build static features (72-dim, same as training pipeline)
    static = np.zeros(72, dtype=np.float32)
    static[0:3] = [ho/20.0, do/20.0, ao/20.0]
    static[3:6] = [(1/ho)/imp, (1/do)/imp, (1/ao)/imp]
    static[6] = imp
    static[7] = 1/ho - 1/ao
    static[8] = min(1/ho, 1/ao)
    static[9] = abs(1/ho - 1/ao) / max(1/ho, 1/do, 1/ao) if max(1/ho, 1/do, 1/ao) > 0 else 0
    
    # Compute JEPA Lite prediction using the model's predict_proba interface
    # (30-path Monte Carlo rollout + Gaussian noise σ=0.04 + softmax τ=1.0 + average)
    static_t = torch.from_numpy(static).unsqueeze(0).float()
    with torch.no_grad():
        probs = model.predict_proba(static_t, n_paths=30).numpy()[0]
    
    labels = ["home", "draw", "away"]
    pred_label = labels[int(np.argmax(probs))]
    
    return {
        "probabilities": {"H": float(probs[0]), "D": float(probs[1]), "A": float(probs[2])},
        "prediction": pred_label,
        "confidence": float(probs.max()),
    }


def result_to_label(result: str) -> str:
    return {"H": "home", "D": "draw", "A": "away"}[result]


def main():
    with open(RESULTS_JSON, "r", encoding="utf-8") as f:
        matches = json.load(f)["matches"]
    
    print(f"World Cup 2026 JEPA v5.0 Validation")
    print(f"====================================")
    print(f"Total matches: {len(matches)}")
    print(f"Model: JEPALite (167K params, Acc=55.9% F1_D=0.507 on 23K test set)")
    print()
    
    results = []
    ocr_failures = 0
    
    for i, m in enumerate(matches):
        fname = os.path.basename(m["screenshot"])
        print(f"[{i+1:2d}/{len(matches)}] {m['home']} vs {m['away']} ", end="", flush=True)
        
        # Step 1: OCR
        try:
            ocr_text = ocr_screenshot(m["screenshot"])
            odds = parse_odds(ocr_text)
        except Exception as e:
            print(f"OCR FAIL: {e}")
            ocr_failures += 1
            continue
        
        if not odds:
            print(f"PARSE FAIL")
            ocr_failures += 1
            continue
        
        print(f"({odds['home']:.2f}/{odds['draw']:.2f}/{odds['away']:.2f}) ", end="", flush=True)
        
        # Step 2: Predict
        try:
            pred = predict_jepa(odds)
        except Exception as e:
            print(f"PREDICT FAIL: {e}")
            continue
        
        pred_label = {"home": "H", "draw": "D", "away": "A"}[pred["prediction"]]
        actual = m["result"]
        correct = "O" if pred_label == actual else "X"
        
        probs = pred["probabilities"]
        score = f"{m['home_score']}-{m['away_score']}"
        
        results.append({
            "home": m["home"], "away": m["away"], "date": m["date"],
            "odds": odds, "prediction": pred_label, "actual": actual,
            "correct": pred_label == actual,
            "probabilities": probs, "score": score
        })
        
        print(f"→ {pred_label} (act={actual} {score}) {correct}")
        
        # Delay to avoid rate limiting
        time.sleep(0.3)
    
    # ═════════════ Statistics ═════════════
    n = len(results)
    if n == 0:
        print("\nNo valid results!")
        return
    
    correct = sum(1 for r in results if r["correct"])
    acc = correct / n
    
    # Per-class
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
    print()
    print("=" * 65)
    print("  JEPA v5.0 — WORLD CUP 2026 VALIDATION REPORT")
    print("=" * 65)
    print(f"  Matches:      {n} (OCR fail: {ocr_failures})")
    print(f"  Accuracy:     {acc:.2%} ({correct}/{n})")
    print(f"  Draw F1:      {draw_f1:.4f}  (P={draw_precision:.3f} R={draw_recall:.3f})")
    print(f"  Macro F1:     {macro_f1:.4f}")
    print()
    print(f"  Distribution: H={actual_dist.get('H',0)} D={actual_dist.get('D',0)} A={actual_dist.get('A',0)}")
    print(f"  Draw rate:    {actual_dist.get('D',0)/n:.1%}")
    print()
    
    for cls in ["H", "D", "A"]:
        acc_c = class_correct[cls] / class_total[cls] if class_total[cls] > 0 else 0
        print(f"  {cls}-Acc:    {acc_c:.2%} ({class_correct[cls]}/{class_total[cls]})")
    
    print()
    print("  Confusion Matrix (actual\\pred):")
    print(f"         pred_H pred_D pred_A")
    for cls in ["H", "D", "A"]:
        cm = conf_matrix[cls]
        print(f"  act_{cls}:  {cm['H']:6d} {cm['D']:6d} {cm['A']:6d}")
    
    print()
    print("  Per-match:")
    for i, r in enumerate(results):
        mark = "O" if r["correct"] else "X"
        probs = r["probabilities"]
        print(f"  {mark} {r['date']} {r['home']:>12s} vs {r['away']:<12s}  "
              f"pred={r['prediction']} act={r['actual']} ({r['score']})  "
              f"H={probs['H']:.1%} D={probs['D']:.1%} A={probs['A']:.1%}")
    
    # Save
    out_file = Path(__file__).parent / "wc2026_validation_output.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({
            "summary": {
                "total": n, "accuracy": acc, "draw_f1": draw_f1,
                "macro_f1": macro_f1, "ocr_failures": ocr_failures,
                "actual_distribution": dict(actual_dist),
                "model": "JEPALite-167K",
            },
            "class_metrics": {
                cls: {
                    "accuracy": class_correct[cls] / class_total[cls] if class_total[cls] > 0 else 0,
                    "correct": class_correct[cls], "total": class_total[cls],
                    "precision": draw_precision if cls == "D" else (class_correct[cls] / (sum(1 for r in results if r["prediction"] == cls)) if sum(1 for r in results if r["prediction"] == cls) > 0 else 0),
                } for cls in ["H", "D", "A"]
            },
            "confusion_matrix": conf_matrix,
            "results": results
        }, f, ensure_ascii=False, indent=2)
    
    print(f"\n  Full output: {out_file}")
    print("=" * 65)


if __name__ == "__main__":
    main()

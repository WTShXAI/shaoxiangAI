"""
Batch OCR all failed World Cup screenshots using Volcengine API.
Extracts 1X2 odds for matches that failed first pass.
"""
import sys, os, json, re, base64, hashlib, hmac, time
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlencode

OCR_AK = "AKLTN2FkMmY5NmNlZDVkNDNjZTgwMTFiNjBkNWY2ZTk1MjA"
OCR_SK = "T0RJMllqZGlOakk1TW1Gak5HTmlaV0l5T0RjelptTmxZbVJsTW1Fek16SQ=="
OCR_HOST = "visual.volcengineapi.com"
OCR_REGION = "cn-north-1"
OCR_SERVICE = "cv"

def ocr_sign(method, body_str):
    now = datetime.now(timezone.utc)
    date = now.strftime("%Y%m%d"); ts = now.strftime("%Y%m%dT%H%M%SZ")
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

def ocr_image(filepath):
    import httpx
    with open(filepath, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()
    body_data = {"Action": "OCRNormal", "Version": "2020-08-26",
                  "image_base64": img_b64, "detect_layout": "true", "sort_page": "true"}
    body_str = urlencode(body_data)
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Host": OCR_HOST}
    headers.update(ocr_sign("POST", body_str))
    resp = httpx.post(f"https://{OCR_HOST}/", data=body_str, headers=headers, timeout=20.0)
    data = resp.json()
    err = data.get("ResponseMetadata", {}).get("Error", {})
    if err and err.get("Code") != "NoError":
        raise RuntimeError(f"API error: {err.get('Message','unknown')}")
    chars_2d = data.get("data", {}).get("chars", [])
    if chars_2d:
        texts = ["".join(c.get("char","") if isinstance(c,dict) else str(c) for c in (line if isinstance(line,list) else [])) for line in chars_2d]
        return "\n".join(texts)
    blocks = data.get("Result", {}).get("TextBlocks", [])
    return "\n".join(b.get("Text","") for b in blocks) if blocks else ""

def parse_odds(text):
    flat = re.sub(r'\s+', ' ', text)
    m = re.search(r'主\s*(\d+\.\d{1,3})', flat)
    m_d = re.search(r'平\s*(\d+\.\d{1,3})', flat)
    m_a = re.search(r'客\s*(\d+\.\d{1,3})', flat)
    if m and m_d and m_a:
        return {"home": round(float(m.group(1)),2), "draw": round(float(m_d.group(1)),2), "away": round(float(m_a.group(1)),2)}
    odds = re.findall(r'(?<!\d)(\d+\.\d{2,3})(?!\d)', flat)
    for i in range(len(odds)-2):
        h,d,a = float(odds[i]),float(odds[i+1]),float(odds[i+2])
        imp=1/h+1/d+1/a
        if 1.05<h<30 and 1.5<d<30 and 1.05<a<30 and 0.85<imp<1.25:
            return {"home":h,"draw":d,"away":a}
    return None

# ── Missing screenshots list ──
BASE = "C:/Users/ShXAI/AppData/Roaming/Desktop/世界杯"
MISSING = [
    ("6.15/荷兰vs日本.png", "Netherlands", "Japan"),
    ("6.16/沙特阿拉伯vs乌拉圭.png", "Saudi Arabia", "Uruguay"),
    ("6.16/西班牙vs佛得角共和国.png", "Spain", "Cape Verde"),
    ("6.17/伊拉克vs挪威.png", "Iraq", "Norway"),
    ("6.17/奥地利vs约旦.png", "Austria", "Jordan"),
    ("6.18/加纳vs巴拿马.png", "Ghana", "Panama"),
    ("6.19/加拿大vs卡特尔.png", "Canada", "Qatar"),
    ("6.20/土耳其vs巴拉圭.png", "Turkey", "Paraguay"),
    ("6.20/巴西vs海地.png", "Brazil", "Haiti"),
    ("6.20/美国vs澳大利亚.png", "USA", "Australia"),
    ("6.20/苏格兰vs摩洛哥.png", "Scotland", "Morocco"),
    ("6.21/德国vs科特迪瓦.png", "Germany", "Ivory Coast"),
]

results = {}
for fname, home, away in MISSING:
    full_path = os.path.join(BASE, fname)
    if not os.path.exists(full_path):
        print(f"NOT FOUND: {fname}")
        continue
    try:
        text = ocr_image(full_path)
        odds = parse_odds(text)
        if odds:
            key = f"{home}_{away}"
            results[key] = odds
            print(f"OK  {home} vs {away}: H={odds['home']} D={odds['draw']} A={odds['away']}")
        else:
            print(f"PARSE FAIL: {home} vs {away}")
            print(f"  Text preview: {text[:200]}")
    except Exception as e:
        print(f"OCR FAIL: {home} vs {away}: {e}")
    time.sleep(0.5)

print(f"\nExtracted {len(results)} new odds:")
print(json.dumps(results, indent=2, ensure_ascii=False))

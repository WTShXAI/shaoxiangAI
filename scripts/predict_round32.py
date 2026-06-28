"""2026世界杯32强淘汰赛全链路预测"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.full_linkage_predictor import MatchInput, FullLinkagePipeline, OULinkageEngine

MATCHES = [
    ("南非","加拿大", 6.31, 3.75, 1.75, 0.0, 2.0,  "6/28 21:00"),
    ("巴西","日本", 1.74, 3.93, 5.34, -0.75, 2.5, "6/29 19:00"),
    ("德国","巴拉圭", 1.38, 5.60, 10.27, -1.5, 3.0, "6/29 22:30"),
    ("荷兰","摩洛哥", 2.25, 3.30, 3.80, -0.25, 2.5, "6/30 03:00"),
    ("科特迪瓦","挪威", 3.85, 3.65, 2.10, +0.5, 2.5, "6/30 19:00"),
    ("法国","瑞典", 1.30, 6.46, 12.50, -1.25, 3.0, "6/30 23:00"),
    ("墨西哥","厄瓜多尔", 2.33, 3.12, 3.85, -0.25, 2.5, "7/1 03:00"),
    ("英格兰","民主刚果", 1.31, 5.81, 15.00, -1.5, 2.75, "7/1 18:00"),
    ("比利时","塞内加尔", 2.21, 3.36, 3.77, -0.25, 2.5, "7/1 22:00"),
    ("美国","波黑", 1.42, 5.14, 9.90, -1.0, 2.5, "7/2 02:00"),
    ("西班牙","奥地利", 1.32, 5.70, 13.00, -1.25, 2.75, "7/2 21:00"),
    ("葡萄牙","克罗地亚", 1.90, 3.62, 5.24, -0.5, 2.5, "7/3 01:00"),
    ("瑞士","阿尔及利亚", 1.98, 3.54, 4.42, -0.5, 2.5, "7/3 05:00"),
    ("澳大利亚","埃及", 3.38, 3.06, 2.55, 0.0, 2.0, "7/3 20:00"),
    ("阿根廷","佛得角", 1.18, 8.60, 23.00, -2.0, 3.0, "7/4 00:00"),
    ("哥伦比亚","加纳", 1.63, 3.94, 7.60, -0.75, 2.5, "7/4 03:30"),
]

p = FullLinkagePipeline()
results = []

print("=" * 70)
print("  2026世界杯 32强淘汰赛预测")
print("=" * 70)

for h,a,oh,od,oa,hcp,ou,time in MATCHES:
    m = MatchInput(h, a, oh, od, oa, hcp, ou)
    r = p.predict(m)
    r['_time'] = time
    s = r['final_verdict']
    ou_link = r['chains'].get('OU_linkage', {})
    
    print(f"\n{'─'*60}")
    print(f"🔮 {time} | {h} vs {a}")
    print(f"   赔率: {oh}/{od}/{oa} | HCP={hcp:+.1f} | OU={ou}")
    print(f"   判决: {s['primary']}+{s['secondary']} | 比分: {s['best_score']}")
    print(f"   备选: {s.get('alt_scores', [])}")
    
    results.append({
        "time": time, "home": h, "away": a,
        "odds": f"{oh}/{od}/{oa}", "hcp": hcp, "ou": ou,
        "verdict": f"{s['primary']}+{s['secondary']}",
        "best_score": s['best_score'],
        "alt_scores": s.get('alt_scores', []),
    })

# 生成JSON
with open('data/round32_predictions.json', 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print(f"\n{'='*70}")
print(f"✅ {len(results)}场预测已保存 → data/round32_predictions.json")

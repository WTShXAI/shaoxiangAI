"""WC2026 12场用户指定预测 — 9场R32 + 3场模拟分析"""
import sys, json, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.full_linkage_predictor import MatchInput, FullLinkagePipeline, OULinkageEngine

# ── 12场比赛: 9场R32实际 + 3场模拟估计 ──
# 模拟场次说明: 非R32实际对阵, 基于球队已知赔率+实力档位估算
MATCHES = [
    # === 9场实际R32 ===
    ("英格兰","民主刚果", 1.31, 5.81, 15.00, -1.5, 2.75, "7/1 18:00", True),
    ("比利时","塞内加尔", 2.21, 3.36, 3.77, -0.25, 2.5, "7/1 22:00", True),
    ("美国","波黑", 1.42, 5.14, 9.90, -1.0, 2.5, "7/2 02:00", True),
    ("西班牙","奥地利", 1.32, 5.70, 13.00, -1.25, 2.75, "7/2 21:00", True),
    ("葡萄牙","克罗地亚", 1.90, 3.62, 5.24, -0.5, 2.5, "7/3 01:00", True),
    ("瑞士","阿尔及利亚", 1.98, 3.54, 4.42, -0.5, 2.5, "7/3 05:00", True),
    ("澳大利亚","埃及", 3.38, 3.06, 2.55, 0.0, 2.0, "7/3 20:00", True),
    ("阿根廷","佛得角", 1.18, 8.60, 23.00, -2.0, 3.0, "7/4 00:00", True),
    ("哥伦比亚","加纳", 1.63, 3.94, 7.60, -0.75, 2.5, "7/4 03:30", True),
    # === 3场模拟（非R32实际对阵） ===
    ("加拿大","摩洛哥", 2.00, 3.20, 4.00, -0.25, 2.5, "7/3 22:00", False),
    ("法国","巴拉圭", 1.25, 6.50, 18.00, -1.5, 2.75, "7/4 01:00", False),
    ("巴西","挪威", 1.60, 4.00, 5.50, -0.75, 2.5, "7/4 05:00", False),
]

# 模拟场次说明
SIM_NOTES = {
    "加拿大vs摩洛哥": {
        "estimated_from": "加拿大 vs 南非(1.75) + 荷兰 vs 摩洛哥(3.80)",
        "rationale": "加拿大B2-imp57%, 摩洛哥C2-imp26%, 跨组摊分调整",
        "real_matchup": "加拿大(A2)→南非 | 摩洛哥(C2)→荷兰",
    },
    "法国vs巴拉圭": {
        "estimated_from": "法国 vs 瑞典(1.30) + 德国 vs 巴拉圭(10.27)",
        "rationale": "法国I1-imp77%, 巴拉圭D3-imp10%, 实力差距极大",
        "real_matchup": "法国(I1)→瑞典 | 巴拉圭(D3)→德国",
    },
    "巴西vs挪威": {
        "estimated_from": "巴西 vs 日本(1.74) + 科特迪瓦 vs 挪威(2.10)",
        "rationale": "巴西C1-imp57%, 挪威I2-imp47%, 中强vs中弱",
        "real_matchup": "巴西(C1)→日本 | 挪威(I2)→科特迪瓦",
    },
}

p = FullLinkagePipeline()
results = []
sim_results = []

print("=" * 70)
print("  2026世界杯 用户12场预测报告")
print("=" * 70)

for h, a, oh, od, oa, hcp, ou, time, is_real in MATCHES:
    key = f"{h}vs{a}"
    note = SIM_NOTES.get(key, None)
    
    if not is_real and note:
        print(f"\n{'⚠'*30}")
        print(f"  [模拟] {h} vs {a} (非R32实际对阵!)")
        print(f"  数据来源: {note['estimated_from']}")
        print(f"  逻辑: {note['rationale']}")
        print(f"{'⚠'*30}")
    
    m = MatchInput(h, a, oh, od, oa, hcp, ou)
    
    try:
        r = p.predict(m)
        r['_time'] = time
        r['_is_real'] = is_real
        s = r['final_verdict']
        
        entry = {
            "time": time,
            "home": h,
            "away": a,
            "odds": f"{oh}/{od}/{oa}",
            "hcp": hcp,
            "ou": ou,
            "verdict": f"{s['primary']}+{s['secondary']}",
            "best_score": s['best_score'],
            "alt_scores": s.get('alt_scores', []),
            "is_real": is_real,
            "sim_note": note,
        }
        
        if is_real:
            results.append(entry)
        else:
            sim_results.append(entry)
        
        print(f"\n{'─'*60}")
        tag = "REAL" if is_real else "SIM"
        print(f"[{tag}] {time} | {h} vs {a}")
        print(f"   赔率: {oh}/{od}/{oa} | HCP={hcp:+.1f} | OU={ou}")
        print(f"   判决: {s['primary']}+{s['secondary']} | 比分: {s['best_score']}")
        print(f"   备选: {s.get('alt_scores', [])}")
        
    except Exception as e:
        print(f"   ❌ 预测失败: {e}")
        if is_real:
            results.append({
                "time": time, "home": h, "away": a,
                "odds": f"{oh}/{od}/{oa}", "hcp": hcp, "ou": ou,
                "verdict": "ERROR", "best_score": "N/A", "alt_scores": [],
                "is_real": True, "error": str(e),
            })
        else:
            sim_results.append({
                "time": time, "home": h, "away": a,
                "odds": f"{oh}/{od}/{oa}", "hcp": hcp, "ou": ou,
                "verdict": "ERROR", "best_score": "N/A", "alt_scores": [],
                "is_real": False, "error": str(e),
                "sim_note": note,
            })

all_results = results + sim_results

# 保存
with open('data/user_12_predictions.json', 'w', encoding='utf-8') as f:
    json.dump({"real_matches": results, "sim_matches": sim_results, "all": all_results}, f, ensure_ascii=False, indent=2)

# 摘要
print(f"\n{'='*70}")
print(f"✅ 完成: {len(results)}场R32 + {len(sim_results)}场模拟 = {len(all_results)}场")
print(f"   已保存 → data/user_12_predictions.json")

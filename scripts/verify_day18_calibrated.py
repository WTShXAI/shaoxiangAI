"""哨响AI v6.0 — Day18 (6/18) 回测 — 校准让球 + 防泄露"""
import sys, os, json
os.chdir('D:/Architecture v4.0')
sys.path.insert(0, 'D:/Architecture v4.0')
sys.path.insert(0, 'D:/Architecture v4.0/predictors/components')

from pipeline.predictors.data_classes import MatchInput
from pipeline.predictors.pipeline import FullLinkagePipeline

# ─── Day18 校准数据 ───
# 1x2 and actual from 72matches, handicap/OU from OCR + API analysis
# 乌兹别克vs哥伦比亚: MD=1 (API), HCP=-1.5(OCR), OU=2.5(OCR)
# 捷克vs南非: MD=2 (API), odds suggest ~-0.5 hcp, OU~2.25
# 瑞士vs波黑: MD=2 (API), odds suggest ~-0.75 hcp, OU~2.5
# 加拿大vs卡塔尔: MD=2 (API), odds suggest ~-1.25 hcp, OU~2.75

MATCHES = [
    {
        "home": "乌兹别克", "away": "哥伦比亚",
        "odds_h": 8.4, "odds_d": 2.35, "odds_a": 1.38,
        "hcp": -1.5, "ou_line": 2.5,
        "hs": 1, "aws": 3, "matchday": 1,  # MD1
    },
    {
        "home": "捷克", "away": "南非",
        "odds_h": 1.82, "odds_d": 3.6, "odds_a": 4.35,
        "hcp": -0.5, "ou_line": 2.25,  # 从赔率估算
        "hs": 1, "aws": 1, "matchday": 2,  # MD2
    },
    {
        "home": "瑞士", "away": "波黑",
        "odds_h": 1.58, "odds_d": 4.05, "odds_a": 5.7,
        "hcp": -0.75, "ou_line": 2.5,  # 从赔率估算
        "hs": 4, "aws": 1, "matchday": 2,  # MD2
    },
    {
        "home": "加拿大", "away": "卡塔尔",
        "odds_h": 1.31, "odds_d": 5.2, "odds_a": 9.8,
        "hcp": -1.25, "ou_line": 2.75,  # 从赔率估算
        "hs": 6, "aws": 0, "matchday": 2,  # MD2
    },
]

print("=" * 72)
print("哨响AI v6.0 — Day18 (6/18) 回测 — 让球校准 + 防泄露")
print("=" * 72)

pipeline = FullLinkagePipeline()
results = []

for m in MATCHES:
    home, away = m['home'], m['away']
    actual = f"{m['hs']}-{m['aws']}"
    
    mi = MatchInput(
        home=home, away=away,
        odds_h=m['odds_h'], odds_d=m['odds_d'], odds_a=m['odds_a'],
        hcp=m['hcp'], ou_line=m['ou_line'],
        matchday=m['matchday'], stage='group',
    )
    
    try:
        result = pipeline.predict(mi)
        v = result.get('final_verdict', {})
        pred = str(v.get('primary', '?'))
        pred_score = str(v.get('best_score', '?'))
        strategy = v.get('rec_type', '?')
        
        hs, aws = m['hs'], m['aws']
        if hs > aws: actual_dir = 'H'
        elif aws > hs: actual_dir = 'A'
        else: actual_dir = 'D'
        
        dir_ok = False
        if actual_dir == 'H' and ('主胜' in pred or '让胜' in pred): dir_ok = True
        elif actual_dir == 'A' and ('客胜' in pred or '让负' in pred): dir_ok = True
        elif actual_dir == 'D' and '平' in pred: dir_ok = True
        
        exact_ok = (pred_score == actual)
        
        results.append({
            'home': home, 'away': away, 'actual': actual,
            'pred': pred, 'score': pred_score, 'md': m['matchday'],
            'dir': 'OK' if dir_ok else 'X', 'exact': 'OK' if exact_ok else '',
        })
        
        icon = '✅' if dir_ok else '❌'
        e = ' 🎯' if exact_ok else ''
        print(f"\n  {icon} {home} vs {away}: {pred}({pred_score}) vs {actual}{e} | MD={m['matchday']} | hcp={m['hcp']:+.2f} | {strategy}")
        
    except Exception as e:
        print(f"\n  ❌ {home} vs {away}: ERROR - {str(e)[:100]}")

print()
print("=" * 72)
dir_ok = sum(1 for r in results if r['dir'] == 'OK')
exact_ok = sum(1 for r in results if r['exact'] == 'OK')
total = len(results)
print(f"Day18 结果: {dir_ok}/{total} = {dir_ok/total*100:.0f}% | 精确比分: {exact_ok}/{total}")
print()

# Compare with uncalibrated run
print("对比 (未校准 → 已校准):")
print(f"  乌兹别克vs哥伦比亚: 让负 ✅ → 让负 ✅")
print(f"  捷克vs南非: 平局 ✅ → 待验证")
print(f"  瑞士vs波黑: 平局 ❌ → 待验证 (关键!)")
print(f"  加拿大vs卡塔尔: 让胜 ✅ → 待验证")
print("=" * 72)

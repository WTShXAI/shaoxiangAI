"""哨响AI v5.2 — Day18 (6/18) 回测 — 验证Day17修复效果"""
import sys, os, json
os.chdir('D:/Architecture')
sys.path.insert(0, 'D:/Architecture')
sys.path.insert(0, 'D:/Architecture/predictors/components')

from pipeline.predictors.data_classes import MatchInput
from pipeline.predictors.pipeline import FullLinkagePipeline

with open('D:/Architecture/data/wc2026_72matches_with_odds.json', encoding='utf-8') as f:
    all_matches = json.load(f)

target = '6/18'
test_matches = [m for m in all_matches if m['date'] == target]

print("=" * 65)
print(f"哨响AI v5.2 — Day 18 ({target}) 回测验证")
print(f"目标比赛: {len(test_matches)} 场")
print("=" * 65)

pipeline = FullLinkagePipeline()
results = []

for m in test_matches:
    home = m.get('home', '')
    away = m.get('away', '')
    actual = f"{m.get('hs', 0)}-{m.get('aws', 0)}"
    
    mi = MatchInput(
        home=home, away=away,
        odds_h=m.get('1x2_home', 2.0) or 2.0,
        odds_d=m.get('1x2_draw', 3.4) or 3.4,
        odds_a=m.get('1x2_away', 3.8) or 3.8,
        hcp=m.get('handicap_float') or m.get('handicap', 0.0) or 0.0,
        ou_line=m.get('ou_line_num') or m.get('ou_line', 2.5) or 2.5,
    )
    
    try:
        result = pipeline.predict(mi)
        verdict = result.get('final_verdict', {})
        pred = verdict.get('primary', '?')
        pred_score = verdict.get('best_score', '?')
        
        hs, aws = m.get('hs', 0), m.get('aws', 0)
        if hs > aws: actual_dir = 'H'
        elif aws > hs: actual_dir = 'A'
        else: actual_dir = 'D'
        
        dir_ok = False
        if actual_dir == 'H' and ('主胜' in str(pred) or '让胜' in str(pred)): dir_ok = True
        elif actual_dir == 'A' and ('客胜' in str(pred) or '让负' in str(pred)): dir_ok = True
        elif actual_dir == 'D' and '平' in str(pred): dir_ok = True
        
        exact_ok = (str(pred_score) == actual)
        
        results.append({
            'home': home, 'away': away, 'actual': actual,
            'pred': pred, 'score': pred_score,
            'dir': 'OK' if dir_ok else 'X', 'exact': 'OK' if exact_ok else '',
        })
        
        print(f"  {home} vs {away}: pred={pred}({pred_score}) actual={actual} dir={'OK' if dir_ok else 'X'}")
        
    except Exception as e:
        print(f"  {home} vs {away}: ERROR - {str(e)[:100]}")
        import traceback; traceback.print_exc()

print()
print("=" * 65)
dir_ok = sum(1 for r in results if r['dir'] == 'OK')
exact_ok = sum(1 for r in results if r['exact'] == 'OK')
draw_pred = sum(1 for r in results if '平' in str(r.get('pred', '')))
total = len(results)

print(f"Day 18 回测结果:")
print(f"  方向准确率: {dir_ok}/{total} = {dir_ok/total*100:.0f}%")
print(f"  精确比分: {exact_ok}/{total}")
print(f"  平局预测数: {draw_pred}/{total}")
print(f"  基线: TBD")

print()
for r in results:
    s = '✅' if r['dir'] == 'OK' else '❌'
    e = ' 🎯' if r['exact'] == 'OK' else ''
    print(f"  {s} {r['home']} vs {r['away']}: {r['pred']}({r['score']}) vs actual {r['actual']}{e}")

# 滚动汇总
print()
print("滚动验证汇总:")
print(f"  Day14-15: 4/9=44%")
print(f"  Day16: 3/3=100%")
print(f"  Day17: 5/5=100%")
print(f"  Day18: {dir_ok}/{total}={dir_ok/total*100:.0f}%")
if total > 0:
    cum_ok = 12 + dir_ok
    cum_total = 17 + total
    print(f"  累计: {cum_ok}/{cum_total}={cum_ok/cum_total*100:.0f}%")
print("=" * 65)

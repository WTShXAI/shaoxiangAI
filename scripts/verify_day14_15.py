"""哨响AI v5.2 — Day14-15回测验证P0修复效果"""
import sys, os, json
# P0修复: 确保从项目根目录运行, draw_expert模块在sys.path
os.chdir('D:/Architecture')
sys.path.insert(0, 'D:/Architecture')
sys.path.insert(0, 'D:/Architecture/predictors/components')

print("=" * 60)
print("哨响AI v5.2 — Day 14-15 回测验证")
print("P0修复: de_mult=0.45, draw_threshold=0.28, PriorityGate=3球")
print("=" * 60)

# 加载72场
with open('D:/Architecture/data/wc2026_72matches_with_odds.json', encoding='utf-8') as f:
    all_matches = json.load(f)

target_dates = ['6/14', '6/15']
test_matches = [m for m in all_matches if m['date'] in target_dates]
print(f"目标比赛: {len(test_matches)} 场 ({', '.join(target_dates)})")
print()

from pipeline.predictors.data_classes import MatchInput
from pipeline.predictors.pipeline import FullLinkagePipeline

pipeline = FullLinkagePipeline()
results = []
errors = []

for m in test_matches:
    home = m.get('home_team', m.get('home', ''))
    away = m.get('away_team', m.get('away', ''))
    actual = f"{m.get('hs', 0)}-{m.get('aws', 0)}"
    
    mi = MatchInput(
        home=home,
        away=away,
        odds_h=m.get('1x2_home', 2.0) or 2.0,
        odds_d=m.get('1x2_draw', 3.4) or 3.4,
        odds_a=m.get('1x2_away', 3.8) or 3.8,
        hcp=m.get('handicap_float') or m.get('handicap', 0.0) or 0.0,
        ou_line=m.get('ou_line_num') or m.get('ou_line', 2.5) or 2.5,
    )
    
    try:
        result = pipeline.predict(mi)
        
        hs, aws = m.get('hs', 0), m.get('aws', 0)
        if hs > aws:
            actual_dir = 'H'
        elif aws > hs:
            actual_dir = 'A'
        else:
            actual_dir = 'D'
        
        pred = result.get('final_verdict', {}).get('primary', '?')
        pred_score = result.get('final_verdict', {}).get('best_score', '?')
        risk_tag = result.get('final_verdict', {}).get('risk_tag', 'clean')
        
        dir_ok = False
        if actual_dir == 'H' and ('主胜' in str(pred) or '让胜' in str(pred)):
            dir_ok = True
        elif actual_dir == 'A' and ('客胜' in str(pred) or '让负' in str(pred)):
            dir_ok = True
        elif actual_dir == 'D' and '平' in str(pred):
            dir_ok = True
        
        exact_ok = (str(pred_score) == actual)
        
        results.append({
            'date': m['date'], 'home': home, 'away': away,
            'actual': actual, 'pred': pred, 'score': pred_score,
            'dir': 'OK' if dir_ok else 'X',
            'exact': 'OK' if exact_ok else '',
            'risk_tag': risk_tag,
        })
        
        print(f"  {m['date']} {home} vs {away}: pred={pred} score={pred_score} actual={actual} dir={'✅' if dir_ok else '❌'} risk={risk_tag}")
        
    except Exception as e:
        import traceback
        msg = f"  {m['date']} {home} vs {away}: ERROR - {str(e)[:120]}"
        print(msg)
        errors.append(msg)
        traceback.print_exc()

print()
print("=" * 60)
dir_ok = sum(1 for r in results if r['dir'] == 'OK')
exact_ok = sum(1 for r in results if r['exact'] == 'OK')
draw_pred = sum(1 for r in results if '平' in str(r.get('pred', '')))
total = len(results)

print(f"Day 14-15 回测结果:")
print(f"  方向准确率: {dir_ok}/{total} = {dir_ok/total*100:.1f}%")
print(f"  精确比分: {exact_ok}/{total} = {exact_ok/total*100:.1f}%")
print(f"  平局预测数: {draw_pred}/{total}")
print(f"  基线对比: 旧版4/9=44.4%, 新版目标>55%")

for dt in target_dates:
    dm = [r for r in results if r['date'] == dt]
    if dm:
        d_ok = sum(1 for r in dm if r['dir'] == 'OK')
        print(f"  {dt}: {d_ok}/{len(dm)} = {d_ok/len(dm)*100:.0f}%")

if errors:
    print(f"\n错误数: {len(errors)}")
print("=" * 60)

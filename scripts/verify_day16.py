"""哨响AI v5.2 — Day16 (6/16) 回测 — 分析→修复→Day17验证"""
import sys, os, json
os.chdir('D:/Architecture')
sys.path.insert(0, 'D:/Architecture')
sys.path.insert(0, 'D:/Architecture/predictors/components')

from pipeline.predictors.data_classes import MatchInput
from pipeline.predictors.pipeline import FullLinkagePipeline

with open('D:/Architecture/data/wc2026_72matches_with_odds.json', encoding='utf-8') as f:
    all_matches = json.load(f)

target = '6/16'
test_matches = [m for m in all_matches if m['date'] == target]

print("=" * 65)
print(f"哨响AI v5.2 — Day 16 ({target}) 回测分析")
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
            'date': m['date'], 'home': home, 'away': away,
            'actual': actual, 'pred': pred, 'score': pred_score,
            'dir': 'OK' if dir_ok else 'X', 'exact': 'OK' if exact_ok else '',
        })
        
        # 紧凑输出
        evidence = verdict.get('reasoning', verdict.get('evidence', []))
        ev_str = str(evidence)[:80] if evidence else ''
        print(f"  {home} vs {away}: pred={pred}({pred_score}) actual={actual} dir={'OK' if dir_ok else 'X'}  [{ev_str}]")
        
    except Exception as e:
        print(f"  {home} vs {away}: ERROR - {str(e)[:100]}")
        import traceback; traceback.print_exc()

# 统计
print()
print("=" * 65)
dir_ok = sum(1 for r in results if r['dir'] == 'OK')
exact_ok = sum(1 for r in results if r['exact'] == 'OK')
draw_pred = sum(1 for r in results if '平' in str(r.get('pred', '')))
total = len(results)

baseline_dir = 2  # 旧版6/16: 2/3=67%
baseline_total = 3

print(f"Day 16 回测结果:")
print(f"  方向准确率: {dir_ok}/{total} = {dir_ok/total*100:.0f}%")
print(f"  精确比分: {exact_ok}/{total}")
print(f"  平局预测数: {draw_pred}/{total}")
print(f"  基线对比: 旧版 {baseline_dir}/{baseline_total}=67%")

# 逐场分析缺陷
print()
print("逐场缺陷分析:")
for r in results:
    if r['dir'] == 'X':
        print(f"  ❌ {r['home']} vs {r['away']}: pred={r['pred']} actual={r['actual']}")
        # 分类错误
        h, a = r['actual'].split('-')
        if h == a and '平' not in str(r['pred']):
            print(f"     类型: 漏判平局 — 模型过度预测主/客胜")
        elif h != a and '平' in str(r['pred']):
            print(f"     类型: 过度预测平局")
        else:
            print(f"     类型: 方向逆转 — 预测赢家与实际相反")
    else:
        print(f"  ✅ {r['home']} vs {r['away']}: pred={r['pred']} actual={r['actual']} correct!")

print("=" * 65)

"""哨响AI v6.0 — Day15 (6/15) 回测 — MD1防泄露全链路验证"""
import sys, os, json
os.chdir('D:/Architecture v4.0')
sys.path.insert(0, 'D:/Architecture v4.0')
sys.path.insert(0, 'D:/Architecture v4.0/predictors/components')

from pipeline.predictors.data_classes import MatchInput
from pipeline.predictors.pipeline import FullLinkagePipeline

# ─── Day15 (6/15) 4场小组赛MD1 ───
# 赔率来自 wc2026_72matches_with_odds.json
# 让球/大小球来自 wc2026_ocr_full.json (赛前OCR)
# 比赛数据来自 worldcup26.ir API (赛前)

MATCHES = [
    {
        "home": "瑞典", "away": "突尼斯",
        "odds_h": 1.92, "odds_d": 3.4, "odds_a": 4.1,
        "hcp": -0.5, "ou_line": 2.5, "over_water": 2.26, "under_water": 1.73,
        "hs": 5, "aws": 1,
    },
    {
        "home": "西班牙", "away": "佛得角",
        "odds_h": 1.09, "odds_d": 10.0, "odds_a": 19.0,
        "hcp": -2.5, "ou_line": 3.5, "over_water": 1.99, "under_water": 1.89,
        "hs": 0, "aws": 0,
    },
    {
        "home": "比利时", "away": "埃及",
        "odds_h": 1.63, "odds_d": 2.25, "odds_a": 5.2,
        "hcp": -1.0, "ou_line": 1.0, "over_water": 1.93, "under_water": 1.85,
        "hs": 1, "aws": 1,
    },
    {
        "home": "沙特", "away": "乌拉圭",
        "odds_h": 2.17, "odds_d": 2.25, "odds_a": 4.45,
        "hcp": 1.5, "ou_line": 2.75, "over_water": 1.64, "under_water": 2.35,
        "hs": 1, "aws": 1,
    },
]

print("=" * 72)
print("哨响AI v6.0 — Day 15 (6/15) MD1回测验证 [防泄露]")
print("应用修复: v5.12/5.13/5.14/v6.0 | matchday=1")
print(f"目标比赛: {len(MATCHES)} 场")
print("=" * 72)

pipeline = FullLinkagePipeline()
results = []

for m in MATCHES:
    home = m['home']
    away = m['away']
    actual_score = f"{m['hs']}-{m['aws']}"
    
    mi = MatchInput(
        home=home, away=away,
        odds_h=m['odds_h'], odds_d=m['odds_d'], odds_a=m['odds_a'],
        hcp=m['hcp'], ou_line=m['ou_line'],
        over_water=m['over_water'], under_water=m['under_water'],
        matchday=1,  # Day15 = MD1 小组赛首轮
        stage='group',
    )
    
    try:
        result = pipeline.predict(mi)
        verdict = result.get('final_verdict', {})
        pred = verdict.get('primary', '?')
        pred_score = verdict.get('best_score', '?')
        rec_type = verdict.get('rec_type', '?')
        dgate = verdict.get('d_gate_verdict', '?')
        
        # 获取动机和上下文信息
        dg_signal = verdict.get('d_gate_signal', '')
        drawgate = verdict.get('drawgate', '')
        ou_verdict = verdict.get('ou_verdict', '')
        
        hs, aws = m['hs'], m['aws']
        if hs > aws: actual_dir = 'H'
        elif aws > hs: actual_dir = 'A'
        else: actual_dir = 'D'
        
        dir_ok = False
        if actual_dir == 'H' and ('主胜' in str(pred) or '让胜' in str(pred)): dir_ok = True
        elif actual_dir == 'A' and ('客胜' in str(pred) or '让负' in str(pred)): dir_ok = True
        elif actual_dir == 'D' and '平' in str(pred): dir_ok = True
        
        exact_ok = (str(pred_score) == actual_score)
        
        results.append({
            'home': home, 'away': away, 'actual': actual_score,
            'pred': pred, 'score': pred_score, 'rec_type': rec_type,
            'dir': 'OK' if dir_ok else 'X', 'exact': 'OK' if exact_ok else '',
            'dg_signal': dg_signal, 'drawgate': drawgate, 'ou_verdict': ou_verdict,
        })
        
        icon = '✅' if dir_ok else '❌'
        print(f"\n  {icon} {home} vs {away}")
        print(f"     预测: {pred} ({pred_score}) | D-Gate: {dgate} | 类型: {rec_type}")
        print(f"     实际: {actual_score} | {actual_dir} | 方向: {'OK' if dir_ok else 'X'}")
        print(f"     D-Gate信号: {dg_signal} | DrawGate: {drawgate} | OU: {ou_verdict}")
        
    except Exception as e:
        print(f"\n  ❌ {home} vs {away}: ERROR - {str(e)[:150]}")
        import traceback
        traceback.print_exc()

print()
print("=" * 72)
dir_ok = sum(1 for r in results if r['dir'] == 'OK')
exact_ok = sum(1 for r in results if r['exact'] == 'OK')
draw_pred = sum(1 for r in results if '平' in str(r.get('pred', '')))
total = len(results)

print(f"Day15 回测结果:")
print(f"  方向准确率: {dir_ok}/{total} = {dir_ok/max(total,1)*100:.0f}%")
print(f"  精确比分: {exact_ok}/{total}")
print(f"  平局预测数: {draw_pred}/{total}")

print()
for r in results:
    s = '✅' if r['dir'] == 'OK' else '❌'
    e = ' 🎯' if r['exact'] == 'OK' else ''
    print(f"  {s} {r['home']} vs {r['away']}: {r['pred']}({r['score']}) vs actual {r['actual']}{e} | {r['rec_type']}")

print()
print("历史对比:")
print(f"  Day14 (MD1): 4/5=80%")
print(f"  Day16 (MD2): 3/3=100%")
print(f"  Day17 (MD3): 5/5=100%")
print(f"  Day18 (MD3): 4/4=100%")
print(f"  Day15 (MD1): {dir_ok}/{total}={dir_ok/max(total,1)*100:.0f}%")
cumulative = sum([
    4+3+5+4,  # Day14+16+17+18 correct
    dir_ok     # Day15
])
cum_total = sum([5, 3, 5, 4, total])
print(f"  累计: {cumulative}/{cum_total}={cumulative/cum_total*100:.0f}%")
print("=" * 72)

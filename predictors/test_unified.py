"""
UnifiedPredictor v4.1 回测验证 (World Cup 6.14-6.18 历史数据)
============================================================
20场已完成比赛 → v4.1 阈值预测 vs 实际结果对比
"""
import os, sys, json, time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'predictors'))

from unified_predictor import UnifiedPredictor

# 6.14-6.18 实际结果
MATCHES = [
    {'id': 1, 'home': '卡塔尔', 'away': '瑞士', 'H': 5.60, 'D': 3.75, 'A': 1.61, 'hc': 1.0, 'ou': 2.5, 'act': 'D'},
    {'id': 2, 'home': '巴西', 'away': '摩洛哥', 'H': 1.39, 'D': 4.50, 'A': 7.50, 'hc': -1.5, 'ou': 2.5, 'act': 'D'},
    {'id': 3, 'home': '海地', 'away': '苏格兰', 'H': 6.90, 'D': 4.50, 'A': 1.40, 'hc': 1.5, 'ou': 2.5, 'act': 'A'},
    {'id': 4, 'home': '澳大利亚', 'away': '土耳其', 'H': 4.55, 'D': 3.35, 'A': 1.76, 'hc': 0.5, 'ou': 2.5, 'act': 'H'},
    {'id': 5, 'home': '德国', 'away': '库拉索', 'H': 1.53, 'D': 4.15, 'A': 5.20, 'hc': -1.0, 'ou': 3.5, 'act': 'H'},
    {'id': 6, 'home': '瑞典', 'away': '突尼斯', 'H': 1.76, 'D': 3.35, 'A': 4.70, 'hc': -0.5, 'ou': 2.5, 'act': 'H'},
    {'id': 7, 'home': '科特迪瓦', 'away': '厄瓜多尔', 'H': 2.60, 'D': 3.35, 'A': 2.60, 'hc': 0.0, 'ou': 2.5, 'act': 'H'},
    {'id': 8, 'home': '荷兰', 'away': '日本', 'H': 1.63, 'D': 3.90, 'A': 4.70, 'hc': -0.5, 'ou': 2.5, 'act': 'D'},
    {'id': 9, 'home': '伊朗', 'away': '新西兰', 'H': 1.44, 'D': 4.25, 'A': 6.30, 'hc': -1.25, 'ou': 2.5, 'act': 'D'},
    {'id': 10, 'home': '比利时', 'away': '埃及', 'H': 1.39, 'D': 4.50, 'A': 7.10, 'hc': -1.5, 'ou': 2.5, 'act': 'D'},
    {'id': 11, 'home': '沙特阿拉伯', 'away': '乌拉圭', 'H': 7.10, 'D': 4.50, 'A': 1.39, 'hc': 1.5, 'ou': 2.5, 'act': 'D'},
    {'id': 12, 'home': '西班牙', 'away': '佛得角共和国', 'H': 1.08, 'D': 8.80, 'A': 18.0, 'hc': -2.5, 'ou': 3.5, 'act': 'D'},
    {'id': 13, 'home': '伊拉克', 'away': '挪威', 'H': 3.10, 'D': 3.40, 'A': 2.14, 'hc': 0.25, 'ou': 2.5, 'act': 'A'},
    {'id': 14, 'home': '奥地利', 'away': '约旦', 'H': 1.46, 'D': 4.15, 'A': 6.20, 'hc': -1.0, 'ou': 2.5, 'act': 'H'},
    {'id': 15, 'home': '法国', 'away': '塞内加尔', 'H': 1.08, 'D': 8.80, 'A': 20.0, 'hc': -2.5, 'ou': 3.5, 'act': 'H'},
    {'id': 16, 'home': '阿根廷', 'away': '阿尔及利亚', 'H': 1.60, 'D': 3.85, 'A': 5.00, 'hc': -0.5, 'ou': 2.5, 'act': 'H'},
    {'id': 17, 'home': '乌兹别克斯坦', 'away': '哥伦比亚', 'H': 5.60, 'D': 4.05, 'A': 1.52, 'hc': 1.0, 'ou': 2.5, 'act': 'A'},
    {'id': 18, 'home': '加纳', 'away': '巴拿马', 'H': 1.52, 'D': 3.95, 'A': 5.70, 'hc': -1.0, 'ou': 2.5, 'act': 'H'},
    {'id': 19, 'home': '英格兰', 'away': '克罗地亚', 'H': 1.30, 'D': 5.00, 'A': 8.30, 'hc': -1.5, 'ou': 2.5, 'act': 'H'},
    {'id': 20, 'home': '葡萄牙', 'away': '民主刚果', 'H': 1.22, 'D': 5.90, 'A': 10.0, 'hc': -1.75, 'ou': 3.0, 'act': 'D'},
]

print("=" * 80)
print("  UnifiedPredictor v4.1 • World Cup 回测 (6.14-6.18)")
print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 80)

up = UnifiedPredictor(enable_trap=False, enable_dh=False, use_threshold=True)
if not up._ready:
    print("❌ 模型未就绪, 无法运行回测")
    sys.exit(1)

correct = 0
draw_hit = 0
draw_total = 0
total_time = 0

print(f"\n{'ID':>3} {'比赛':<28} {'赔率':<20} {'预测':>4} {'实际':>4} {'✅/❌':>5} {'置信度':>7} {'用时':>6}")
print("-" * 95)

for m in MATCHES:
    t0 = time.time()
    r = up.predict(
        home=m['home'], away=m['away'],
        odds_h=m['H'], odds_d=m['D'], odds_a=m['A'],
        asian_handicap=m['hc'], ou_line=m['ou'],
        match_type='world_cup'
    )
    elapsed = r['elapsed_ms']
    total_time += elapsed

    pred = r['prediction']
    actual = m['act']
    is_correct = pred == actual
    if is_correct:
        correct += 1
    if actual == 'D':
        draw_total += 1
        if is_correct:
            draw_hit += 1

    tag = '✅' if is_correct else '❌'
    match_name = f"{m['home']} vs {m['away']}"
    odds_str = f"H={m['H']:.2f}/D={m['D']:.2f}/A={m['A']:.2f}"

    print(f"{m['id']:>3} {match_name:<28} {odds_str:<20} {pred:>4} {actual:>4} {tag:>5} "
          f"{r['confidence']:>6.3f}  {elapsed:>4.0f}ms")

print("-" * 95)
acc = correct / len(MATCHES) * 100
draw_acc = draw_hit / draw_total * 100 if draw_total > 0 else 0
avg_time = total_time / len(MATCHES)

print(f"\n  📊 结果汇总:")
print(f"     总准确率: {correct}/{len(MATCHES)} = {acc:.1f}%")
print(f"     平局准确率: {draw_hit}/{draw_total} = {draw_acc:.1f}%")
print(f"     平均耗时: {avg_time:.0f}ms")

# 陷阱检测统计
trap_count = 0
for m in MATCHES:
    r = up.predict(
        home=m['home'], away=m['away'],
        odds_h=m['H'], odds_d=m['D'], odds_a=m['A'],
        asian_handicap=m['hc'], ou_line=m['ou'],
        match_type='world_cup'
    )
    if r['trap_level'] != 'none':
        trap_count += 1
print(f"     陷阱检出: {trap_count}/{len(MATCHES)} 场")

print(f"\n{'='*80}")
print(f"  ✅ UnifiedPredictor 回测完成")

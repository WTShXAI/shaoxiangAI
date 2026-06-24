"""
哨响AI v4.0 — 沙盒实验验证脚本
=================================
单人维护版: 新功能/新参数在沙盒里跑历史验证集, 自动对比基线。
验证通过才能合并到核心链路。

用法:
  python sandbox/validate_experiment.py                          # 跑默认基线对比
  python sandbox/validate_experiment.py --draw-threshold 0.48   # 测试新阈值
  python sandbox/validate_experiment.py --scenario cup_group    # 测试特定场景

输出:
  sandbox/validation_report_YYYYMMDD_HHMMSS.json  — 完整验证报告
"""
import sys, os, json, time, argparse
from datetime import datetime

# 项目根路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ═══════════════════════════════════════════════════════════════
# 1. 测试数据集 (世界杯6/13-6/18回测样本)
# ═══════════════════════════════════════════════════════════════

TEST_MATCHES = [
    ('卡塔尔', '瑞士', '世界杯', {'home': 3.50, 'draw': 3.25, 'away': 2.10}, 'D'),
    ('海地', '苏格兰', '世界杯', {'home': 2.38, 'draw': 3.20, 'away': 3.10}, 'A'),
    ('澳大利亚', '土耳其', '世界杯', {'home': 3.50, 'draw': 3.10, 'away': 2.15}, 'H'),
    ('巴西', '摩洛哥', '世界杯', {'home': 14.0, 'draw': 6.50, 'away': 1.19}, 'D'),
    ('德国', '库拉索', '世界杯', {'home': 2.45, 'draw': 3.80, 'away': 2.30}, 'H'),
    ('瑞典', '突尼斯', '世界杯', {'home': 4.33, 'draw': 3.80, 'away': 1.62}, 'H'),
    ('科特迪瓦', '厄瓜多尔', '世界杯', {'home': 2.05, 'draw': 3.30, 'away': 3.30}, 'H'),
    ('荷兰', '日本', '世界杯', {'home': 1.40, 'draw': 4.60, 'away': 6.50}, 'D'),
    ('葡萄牙', '民主刚果', '世界杯', {'home': 1.35, 'draw': 4.80, 'away': 8.50}, 'D'),
    ('英格兰', '克罗地亚', '世界杯', {'home': 2.30, 'draw': 3.10, 'away': 3.30}, 'H'),
    ('加纳', '巴拿马', '世界杯', {'home': 2.10, 'draw': 3.20, 'away': 3.60}, 'H'),
    ('乌兹别克斯坦', '哥伦比亚', '世界杯', {'home': 3.80, 'draw': 3.20, 'away': 2.05}, 'A'),
]

BING_RESULTS = {
    ('德国','库拉索'): 'H', ('荷兰','日本'): 'D',
    ('科特迪瓦','厄瓜多尔'): 'H', ('瑞典','突尼斯'): 'H',
    ('葡萄牙','民主刚果'): 'D', ('英格兰','克罗地亚'): 'H',
    ('加纳','巴拿马'): 'H', ('乌兹别克斯坦','哥伦比亚'): 'A',
    ('澳大利亚','土耳其'): 'H', ('卡塔尔','瑞士'): 'D',
    ('海地','苏格兰'): 'A', ('巴西','摩洛哥'): 'D',
}


# ═══════════════════════════════════════════════════════════════
# 2. 验证逻辑
# ═══════════════════════════════════════════════════════════════

def run_baseline() -> dict:
    """跑基线 (当前默认配置)"""
    from six_layer_conversation import SixLayerConversationEngine
    engine = SixLayerConversationEngine(enable_l6=False)
    
    correct = 0
    details = []
    for home, away, league, odds, actual in TEST_MATCHES:
        r = engine.process(f'{home} vs {away} 谁赢', home, away, league, odds)
        top = max([('H', r.h_prob), ('D', r.d_prob), ('A', r.a_prob)], key=lambda x: x[1])
        ok = (top[0] == actual)
        if ok: correct += 1
        details.append({
            'match': f'{home} vs {away}',
            'pred': top[0], 'actual': actual, 'correct': ok,
            'probs': f'{r.h_prob:.0%}/{r.d_prob:.0%}/{r.a_prob:.0%}'
        })
    
    return {
        'accuracy': correct / len(TEST_MATCHES),
        'correct': correct,
        'total': len(TEST_MATCHES),
        'details': details,
    }


def run_experiment(**overrides) -> dict:
    """跑实验 (可选参数覆盖)"""
    from six_layer_conversation import SixLayerConversationEngine
    
    draw_threshold = overrides.get('draw_threshold', 0.46)
    engine = SixLayerConversationEngine(enable_l6=False,
                                         draw_threshold=draw_threshold)
    
    correct = 0
    details = []
    for home, away, league, odds, actual in TEST_MATCHES:
        r = engine.process(f'{home} vs {away} 谁赢', home, away, league, odds)
        top = max([('H', r.h_prob), ('D', r.d_prob), ('A', r.a_prob)], key=lambda x: x[1])
        ok = (top[0] == actual)
        if ok: correct += 1
        details.append({
            'match': f'{home} vs {away}',
            'pred': top[0], 'actual': actual, 'correct': ok,
            'probs': f'{r.h_prob:.0%}/{r.d_prob:.0%}/{r.a_prob:.0%}'
        })
    
    return {
        'accuracy': correct / len(TEST_MATCHES),
        'correct': correct,
        'total': len(TEST_MATCHES),
        'params': overrides,
        'details': details,
    }


def compare(baseline: dict, experiment: dict) -> dict:
    """对比基线和实验"""
    delta = experiment['accuracy'] - baseline['accuracy']
    verdict = 'PASS' if delta >= -0.01 else 'FAIL'
    
    # 逐场对比
    field_comparison = []
    for i, (b, e) in enumerate(zip(baseline['details'], experiment['details'])):
        changed = b['pred'] != e['pred']
        if changed:
            field_comparison.append({
                'match': b['match'],
                'baseline_pred': b['pred'],
                'experiment_pred': e['pred'],
                'actual': b['actual'],
                'improved': e['correct'] and not b['correct'],
            })
    
    return {
        'baseline_acc': baseline['accuracy'],
        'experiment_acc': experiment['accuracy'],
        'delta': delta,
        'verdict': verdict,
        'changes': field_comparison,
        'details': {
            'baseline': f"{baseline['correct']}/{baseline['total']}",
            'experiment': f"{experiment['correct']}/{experiment['total']}",
        }
    }


# ═══════════════════════════════════════════════════════════════
# 3. 主流程
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='沙盒实验验证')
    parser.add_argument('--draw-threshold', type=float, help='Draw阈值覆盖')
    parser.add_argument('--ha-gap', type=float, help='HA gap覆盖')
    parser.add_argument('--scenario', type=str, help='场景标签 (仅记录)')
    
    args = parser.parse_args()
    
    print('═' * 50)
    print('  沙盒实验验证')
    print(f'  数据集: {len(TEST_MATCHES)}场世界杯 (6/13-6/18)')
    print('═' * 50)
    
    # 跑基线
    print('\n[1/3] 跑基线...')
    t0 = time.perf_counter()
    baseline = run_baseline()
    print(f'  基线: Acc={baseline["accuracy"]:.0%} ({baseline["correct"]}/{baseline["total"]})')
    
    # 跑实验
    overrides = {}
    if args.draw_threshold:
        overrides['draw_threshold'] = args.draw_threshold
    if args.ha_gap:
        overrides['ha_gap'] = args.ha_gap
    
    if overrides:
        print(f'\n[2/3] 跑实验: {overrides}...')
        experiment = run_experiment(**overrides)
        print(f'  实验: Acc={experiment["accuracy"]:.0%} ({experiment["correct"]}/{experiment["total"]})')
        
        # 对比
        print(f'\n[3/3] 对比分析...')
        cmp = compare(baseline, experiment)
        icon = '✅' if cmp['verdict'] == 'PASS' else '❌'
        print(f'  {icon} {cmp["verdict"]}: Δ={cmp["delta"]:+.1%}')
        if cmp['changes']:
            print(f'  变化: {len(cmp["changes"])}场预测不同')
            for ch in cmp['changes']:
                direction = '✅改善' if ch['improved'] else '⚠️方向变化'
                print(f'    {ch["match"]}: {ch["baseline_pred"]}→{ch["experiment_pred"]} (实际{ch["actual"]}) {direction}')
    else:
        print(f'\n[2/3] 无参数覆盖, 仅跑基线')
        experiment = baseline
        cmp = {'verdict': 'BASELINE', 'baseline_acc': baseline['accuracy']}
    
    # 保存报告
    report = {
        'timestamp': datetime.now().isoformat(),
        'scenario': args.scenario or 'default',
        'params': overrides,
        'baseline': baseline,
        'experiment': experiment if overrides else None,
        'comparison': cmp,
    }
    
    report_dir = os.path.join(PROJECT_ROOT, 'sandbox')
    fname = f'validation_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    with open(os.path.join(report_dir, fname), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    
    print(f'\n📄 报告已保存: sandbox/{fname}')
    exp_acc = experiment['accuracy'] if experiment else 0
    print(f'   基线={baseline["accuracy"]:.0%} | 实验={exp_acc:.0%}')
    print(f'   {cmp["verdict"]}')


if __name__ == '__main__':
    main()
